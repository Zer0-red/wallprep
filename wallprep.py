#!/usr/bin/env python3
"""
wallprep — prepare wallpapers for publishing (GTK GUI, v6)

Workflow:
  1. Add images (whole folder, individual files, or drag & drop).
     Images you already processed in the past show "done ✓" —
     the app remembers them (~/.config/wallprep/processed.json).
  2. Select rows and stage operations (preview only, nothing written):
       Resize  -> longest side becomes the chosen size
                  (1920 makes landscape 1920 wide, portrait 1920 tall)
       Rename  -> random 5-character name
       Clean   -> remove ALL metadata
     Click the same button again to un-stage.
  3. Click Apply — each image is processed ONCE into a single output
     file, then the app VERIFIES the result (dimensions correct,
     metadata gone) and shows a green check when it passes.

The drawer on the right shows a preview thumbnail and full metadata
of the selected image. Output folder and width are remembered
between sessions. Originals are never modified.

Dependencies (Ubuntu):
  sudo apt install python3-gi gir1.2-gtk-3.0 imagemagick libimage-exiftool-perl
Run:
  python3 wallprep.py
"""

import hashlib
import json
import os
import random
import shutil
import string
import subprocess
import threading
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}
NAME_LENGTH = 12
CONFIG_DIR = Path.home() / ".config" / "wallprep"
CONFIG_FILE = CONFIG_DIR / "config.json"
PROCESSED_FILE = CONFIG_DIR / "processed.json"
DEFAULT_OUTPUT = Path.home() / "wallpapers" / "ready"
THUMB_WIDTH = 280

BORING_TAGS = {
    "SourceFile", "ExifToolVersion", "FileName", "Directory", "FileSize",
    "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "FilePermissions", "FileType", "FileTypeExtension", "MIMEType",
    "ImageWidth", "ImageHeight", "ImageSize", "Megapixels",
    "EncodingProcess", "BitsPerSample", "ColorComponents", "YCbCrSubSampling",
    "JFIFVersion", "ResolutionUnit", "XResolution", "YResolution",
    "BitDepth", "ColorType", "Compression", "Filter", "Interlace",
    # PNG color-calibration values (re-added by ImageMagick, harmless,
    # present in virtually every PNG — not identifying metadata)
    "WhitePointX", "WhitePointY", "RedX", "RedY", "GreenX", "GreenY",
    "BlueX", "BlueY", "BackgroundColor", "Gamma", "SRGBRendering",
    "PixelsPerUnitX", "PixelsPerUnitY", "PixelUnits",
}

AI_HINTS = ("software", "artist", "creator", "generator", "prompt", "stable",
            "diffusion", "midjourney", "dall", "comfyui", "parameters",
            "usercomment", "description", "model")

COL_PATH, COL_NAME, COL_FMT, COL_DIMS, COL_META, COL_STATUS = range(6)

CSS = b"""
treeview.file-list { font-size: 10.5px; }
button.apply-btn {
    background-image: none;
    background-color: #7c3aed;
    color: #ffffff;
    font-weight: bold;
    text-shadow: none;
    border-color: #6d28d9;
    padding-left: 40px;
    padding-right: 40px;
}
button.apply-btn:hover { background-color: #8b5cf6; }
button.apply-btn:active { background-color: #6d28d9; }
button.apply-btn:disabled { background-color: #b9a7e8; color: #f3f0fa; }

button.profile-card {
    background-image: none;
    padding: 8px 14px;
    border-radius: 10px;
}
button.profile-card.selected {
    background-color: alpha(#7c3aed, 0.18);
    border-color: #7c3aed;
    color: #c4b5fd;
}
.pill {
    background-color: alpha(#7c3aed, 0.22);
    color: #c4b5fd;
    border-radius: 8px;
    padding: 1px 8px;
    font-size: 9px;
    font-weight: bold;
}
.pill-off {
    background-color: alpha(#888888, 0.18);
    color: #999999;
    border-radius: 8px;
    padding: 1px 8px;
    font-size: 9px;
    font-weight: bold;
}
.report-ok { color: #34d399; font-size: 11px; }
.report-pending { color: #888888; font-size: 11px; }
.report-banner {
    background-color: alpha(#f59e0b, 0.12);
    border-radius: 8px;
    padding: 8px 10px;
}
"""



# ---------------- persistence helpers ----------------
def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_json(path: Path, data: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def fingerprint(path: str) -> str:
    """Content-based ID of a file: size + sha1 of the first 64 KB.
    Survives the file being moved or its folder renamed."""
    p = Path(path)
    h = hashlib.sha1()
    h.update(str(p.stat().st_size).encode())
    with open(p, "rb") as f:
        h.update(f.read(65536))
    return h.hexdigest()


# ---------------- image helpers ----------------
def read_metadata(path: str) -> dict:
    """Lenient read for the preview column — returns {} on any failure."""
    try:
        out = subprocess.run(["exiftool", "-j", "-G0", path],
                             capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout)[0]
    except Exception:
        return {}
    return {k: v for k, v in data.items()
            if k.split(":")[-1] not in BORING_TAGS}


def read_metadata_strict(path: str) -> dict:
    """Strict read for VERIFICATION — raises on failure instead of
    returning {}, so a crashed/timed-out exiftool can never be mistaken
    for a clean result and pass verification."""
    out = subprocess.run(["exiftool", "-j", "-G0", path],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(
            f"exiftool exited {out.returncode}: {out.stderr.strip()[:120]}")
    data = json.loads(out.stdout)[0]  # raises if output is malformed/empty
    return {k: v for k, v in data.items()
            if k.split(":")[-1] not in BORING_TAGS}


def image_dimensions(path: str):
    try:
        out = subprocess.run(["identify", "-format", "%w %h", path + "[0]"],
                             capture_output=True, text=True, timeout=30)
        w, h = out.stdout.split()
        return int(w), int(h)
    except Exception:
        return None, None


def random_name(length=NAME_LENGTH):
    return "".join(random.choices(string.ascii_lowercase + string.digits,
                                  k=length))


def display_path(p) -> str:
    """Shorten a path for display: /home/user/x -> ~/x."""
    try:
        return "~/" + str(Path(p).relative_to(Path.home()))
    except Exception:
        return str(p)


def scaled_to_longest(w: int, h: int, target: int):
    """New (w, h) so the LONGEST side equals target, aspect kept."""
    longest = max(w, h)
    scale = target / longest
    return max(1, round(w * scale)), max(1, round(h * scale))


class WallprepApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="wallprep")
        self.set_default_size(1080, 640)
        self.cfg = load_json(CONFIG_FILE)
        self.processed = load_json(PROCESSED_FILE)
        # per-file state: path -> {w,h,meta,resize_to,new_name,strip,done}
        self.info = {}
        self._status_plain = {}  # path -> un-marked status string
        self.busy = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # ---------------- header bar ----------------
        header = Gtk.HeaderBar(title="wallprep", show_close_button=True)
        self.set_titlebar(header)

        add_folder = Gtk.Button(label="Add folder…")
        add_folder.connect("clicked", self.on_add_folder)
        header.pack_start(add_folder)

        add_files = Gtk.Button(label="Add images…")
        add_files.connect("clicked", self.on_add_files)
        header.pack_start(add_files)

        clear_btn = Gtk.Button.new_from_icon_name(
            "edit-clear-all-symbolic", Gtk.IconSize.BUTTON)
        clear_btn.set_tooltip_text("Clear the list")
        clear_btn.connect("clicked", self.on_clear)
        header.pack_start(clear_btn)

        # Purge history sits on the far (right) side
        self.purge_btn = Gtk.Button(label="Purge history")
        self.purge_btn.set_tooltip_text(
            "Delete the entire local processing log (with confirmation). "
            "Affects only the log, not your image files.")
        self.purge_btn.connect("clicked", self.on_purge_history)
        header.pack_end(self.purge_btn)

        # ---------------- toolbar (grouped) ----------------
        def group(title, *widgets):
            """A labelled vertical group: small caption + a row of widgets."""
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cap = Gtk.Label(xalign=0.0)
            cap.set_markup(
                f"<span size='x-small' weight='bold'>{title}</span>")
            cap.get_style_context().add_class("dim-label")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for w in widgets:
                row.pack_start(w, False, False, 0)
            col.pack_start(cap, False, False, 0)
            col.pack_start(row, False, False, 0)
            return col

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for m in ("set_margin_top", "set_margin_bottom",
                  "set_margin_start", "set_margin_end"):
            getattr(bar, m)(10)

        # --- main operation: Clean + rename (one toggle) ---
        self.cleanrename_btn = Gtk.ToggleButton(label="Clean + rename")
        self.cleanrename_btn.set_tooltip_text(
            "Remove all metadata + ICC profile, re-encode (kills the source "
            "encoder trace), normalize the timestamp, and give a random "
            "filename. This is the core privacy prep. Click to arm it for "
            "all loaded images; click again to disarm.")
        self.cleanrename_btn.connect("toggled", self.on_toggle_cleanrename)

        # --- optional resize ---
        self.resize_check = Gtk.CheckButton(label="Resize to")
        self.resize_check.set_tooltip_text(
            "Also resize so the longest side matches the chosen size "
            "(landscape -> width, portrait -> height).")
        self.resize_check.connect("toggled", self.on_toggle_resize)

        self.presets = [("720p (HD)", 1280), ("1080p (FHD)", 1920),
                        ("1440p (QHD)", 2560), ("4K UHD", 3840),
                        ("8K UHD", 7680), ("Custom", None)]
        self.preset_combo = Gtk.ComboBoxText()
        for label, _v in self.presets:
            self.preset_combo.append_text(label)
        self.preset_combo.set_tooltip_text("Common resolutions")

        self.width_spin = Gtk.SpinButton.new_with_range(480, 7680, 10)
        self.width_spin.set_value(int(self.cfg.get("width", 1920)))
        self.width_spin.set_tooltip_text("Target size of the longest side")
        self.px_label = Gtk.Label(label="px")

        self._syncing = False
        self._sync_preset_combo()
        self.preset_combo.connect("changed", self.on_preset_changed)
        self.width_spin.connect("value-changed", self.on_width_changed)

        ops_group = group("PREP", self.cleanrename_btn, self.resize_check,
                          self.preset_combo, self.width_spin, self.px_label)

        # --- Output group ---
        self.formats = [("Keep original", None), ("JPG", "jpg"),
                        ("PNG", "png")]
        self.format_combo = Gtk.ComboBoxText()
        for label, _v in self.formats:
            self.format_combo.append_text(label)
        saved_fmt = self.cfg.get("out_format", "jpg")  # None / "jpg" / "png"
        self.format_combo.set_active(
            next((i for i, (_l, v) in enumerate(self.formats)
                  if v == saved_fmt), 1))  # index 1 = JPG fallback
        self.format_combo.set_tooltip_text(
            "Output format. 'Keep original' preserves each image's format. "
            "JPG = small (default), PNG = lossless.")
        self.format_combo.connect("changed", self.on_format_changed)

        self.no_history_check = Gtk.CheckButton(label="No history")
        self.no_history_check.set_tooltip_text(
            "Forward-looking: when on, images you process from now on are "
            "NOT recorded to the local log. (The log normally lets the app "
            "show 'done ✓'.) To erase an existing log, use Purge history.")
        self.no_history_check.set_active(bool(self.cfg.get("no_history",
                                                           False)))
        self.no_history_check.connect("toggled", self.on_no_history_toggled)

        output_group = group("OUTPUT", self.format_combo,
                             self.no_history_check)

        # assemble groups with dividers
        def divider():
            return Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)

        bar.pack_start(ops_group, False, False, 0)
        bar.pack_start(divider(), False, False, 0)
        bar.pack_start(output_group, False, False, 0)

        # ---------------- output folder row ----------------
        out_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(out_row, m)(10)

        out_label = Gtk.Label()
        out_label.set_markup("<b>Output folder:</b>")
        out_row.pack_start(out_label, False, False, 0)

        saved_out = Path(self.cfg.get("output_dir", str(DEFAULT_OUTPUT)))
        saved_out.mkdir(parents=True, exist_ok=True)
        self.out_btn = Gtk.FileChooserButton(
            title="Choose output folder (use 'Create Folder' for a new one)",
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.out_btn.set_filename(str(saved_out))
        self.out_btn.set_size_request(420, -1)
        self.out_btn.set_tooltip_text(
            "Apply saves processed copies here. Remembered between sessions.")
        self.out_btn.connect("file-set", self.on_output_changed)
        out_row.pack_start(self.out_btn, False, False, 0)

        open_out = Gtk.Button.new_from_icon_name(
            "folder-open-symbolic", Gtk.IconSize.BUTTON)
        open_out.set_tooltip_text("Open output folder in file manager")
        open_out.connect("clicked", self.on_open_output)
        out_row.pack_start(open_out, False, False, 0)

        # ---------------- file list ----------------
        self.store = Gtk.ListStore(str, str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.get_style_context().add_class("file-list")
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.get_selection().connect("changed", self.on_selection_changed)
        for i, title in enumerate(("File", "Format", "Dimensions",
                                   "Metadata", "Status")):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(),
                                     markup=i + 1)
            col.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
            col.set_resizable(True)
            self.tree.append_column(col)

        self.tree.drag_dest_set(Gtk.DestDefaults.ALL, [],
                                Gdk.DragAction.COPY)
        self.tree.drag_dest_add_uri_targets()
        self.tree.connect("drag-data-received", self.on_drop)

        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_vexpand(True)
        list_scroll.add(self.tree)

        # ---------------- drawer: thumbnail + metadata ----------------
        self.drawer_title = Gtk.Label(label="Preview", xalign=0.0)
        self.drawer_title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.drawer_title.set_margin_top(8)
        self.drawer_title.set_margin_bottom(4)

        self.thumb = Gtk.Image()
        self.thumb.set_margin_bottom(6)

        self.meta_store = Gtk.ListStore(str, str)
        meta_tree = Gtk.TreeView(model=self.meta_store)
        meta_tree.get_style_context().add_class("file-list")
        for i, title in enumerate(("Tag", "Value")):
            r = Gtk.CellRendererText()
            r.set_property("wrap-width", 150)
            r.set_property("wrap-mode", 2)  # Pango.WrapMode.WORD_CHAR
            c = Gtk.TreeViewColumn(title, r, text=i)
            c.set_resizable(True)
            meta_tree.append_column(c)
        meta_scroll = Gtk.ScrolledWindow()
        meta_scroll.set_vexpand(True)
        meta_scroll.add(meta_tree)

        # privacy report: collapsible. The title acts as a show/hide toggle.
        self.report_toggle = Gtk.Button()
        self.report_toggle.set_relief(Gtk.ReliefStyle.NONE)
        self.report_toggle_label = Gtk.Label(xalign=0.0)
        self.report_toggle.add(self.report_toggle_label)
        self.report_toggle.connect("clicked", self.on_report_toggle)
        self.report_open = bool(self.cfg.get("report_open", False))

        self.report_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=3)
        self.report_box.set_margin_bottom(6)
        self.report_banner = Gtk.Label(xalign=0.0)
        self.report_banner.set_line_wrap(True)
        self.report_banner.get_style_context().add_class("report-banner")
        report_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                               spacing=4)
        report_inner.pack_start(self.report_box, False, False, 0)
        report_inner.pack_start(self.report_banner, False, False, 0)
        self.report_reveal = Gtk.Revealer()
        self.report_reveal.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.report_reveal.add(report_inner)
        self.report_reveal.set_reveal_child(self.report_open)

        drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        drawer_box.pack_start(self.drawer_title, False, False, 0)
        drawer_box.pack_start(self.thumb, False, False, 0)
        drawer_box.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        drawer_box.pack_start(meta_scroll, True, True, 0)
        drawer_box.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        drawer_box.pack_start(self.report_toggle, False, False, 0)
        drawer_box.pack_start(self.report_reveal, False, False, 0)
        self._update_report_toggle_label()
        self.drawer_box = drawer_box
        drawer_box.set_size_request(THUMB_WIDTH + 40, -1)

        # collapse button sits just outside the drawer, so it stays visible
        # even when the drawer is hidden
        self.drawer_btn = Gtk.ToggleButton()
        self.drawer_btn.set_image(Gtk.Image.new_from_icon_name(
            "view-dual-symbolic", Gtk.IconSize.BUTTON))
        self.drawer_btn.set_tooltip_text("Show/hide the preview panel")
        self.drawer_btn.set_active(True)
        self.drawer_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.drawer_btn.set_valign(Gtk.Align.START)
        self.drawer_btn.connect("toggled", self.on_drawer_toggle)

        # a horizontal Paned makes the divider draggable -> drawer is
        # manually resizable with the cursor
        self.drawer_paned = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL)
        self.drawer_paned.pack1(list_scroll, resize=True, shrink=False)
        self.drawer_paned.pack2(drawer_box, resize=False, shrink=True)
        # remember/restore the divider position
        self._drawer_pos = int(self.cfg.get("drawer_pos", 0)) or None

        main_h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        main_h.pack_start(self.drawer_paned, True, True, 0)
        main_h.pack_start(self.drawer_btn, False, False, 0)

        # ---------------- bottom bar: Export button ----------------
        self.apply_btn = Gtk.Button(label="Export  →")
        self.apply_btn.get_style_context().add_class("apply-btn")
        self.apply_btn.set_tooltip_text(
            "Apply all staged operations — one output file per image, "
            "then verify the result")
        self.apply_btn.connect("clicked", self.on_apply)

        sel_all = Gtk.Button(label="Select all")
        sel_all.set_tooltip_text("Select every image in the list")
        sel_all.connect("clicked",
                        lambda *_: self.tree.get_selection().select_all())

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_margin_top(8)
        bottom.set_margin_bottom(4)
        bottom.set_margin_start(10)
        bottom.set_margin_end(10)
        bottom.pack_start(sel_all, False, False, 0)
        self.counter = Gtk.Label(xalign=0.0)
        self.counter.get_style_context().add_class("dim-label")
        bottom.pack_start(self.counter, True, True, 8)
        bottom.pack_end(self.apply_btn, False, False, 0)

        self.hint = Gtk.Label(
            label="Add images, select them, stage operations "
                  "(Resize / Rename / Clean), then press Apply.")
        self.hint.set_margin_top(4)
        self.hint.set_margin_bottom(8)
        self.hint.get_style_context().add_class("dim-label")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(bar, False, False, 0)
        vbox.pack_start(out_row, False, False, 0)
        vbox.pack_start(main_h, True, True, 0)
        vbox.pack_start(bottom, False, False, 0)
        vbox.pack_start(self.hint, False, False, 0)
        self.add(vbox)
        self.set_ops_sensitive(False)

        self.connect("destroy", self.on_destroy)

    # ================= config persistence =================
    def on_output_changed(self, _btn):
        self.cfg["output_dir"] = self.out_btn.get_filename()
        save_json(CONFIG_FILE, self.cfg)

    def on_format_changed(self, _combo):
        # the resulting-format column depends on this, so refresh every row
        for row in self.store:
            self._refresh_row(row[COL_PATH])

    def on_no_history_toggled(self, btn):
        # forward-looking only: controls whether FUTURE processing is logged.
        # (To erase the EXISTING log, use the "Purge history" button.)
        if btn.get_active():
            self.cfg.pop("last_folder", None)  # stop leaking source folder
            save_json(CONFIG_FILE, self.cfg)
            self.hint.set_label(
                "No-history on — images you process from now on won't be "
                "logged. (Existing log is untouched; use Purge history to "
                "erase it.)")
        else:
            self.hint.set_label(
                "No-history off — processed images will be remembered "
                "(shown as done ✓).")

    def on_purge_history(self, _btn):
        n = len(self.processed)
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Purge processing history?")
        dialog.format_secondary_text(
            f"This permanently deletes the local log of {n} processed "
            f"image(s) ({display_path(PROCESSED_FILE)}). Your actual image "
            "files are not affected. This cannot be undone.")
        resp = dialog.run()
        dialog.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        self.processed = {}
        try:
            PROCESSED_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        # clear any "done ✓" markers now that the log is gone
        for row in self.store:
            p = row[COL_PATH]
            if p in self.info and self.info[p].get("done"):
                self.info[p]["done"] = None
                self._refresh_row(p)
        self.hint.set_label(f"Purged {n} history entry(ies). The local log "
                            "is gone; image files are untouched.")

    def on_destroy(self, _win):
        self.cfg["output_dir"] = self.out_btn.get_filename()
        self.cfg["width"] = int(self.width_spin.get_value())
        self.cfg["out_format"] = self.target_format()
        self.cfg["no_history"] = self.no_history_check.get_active()
        self.cfg["report_open"] = self.report_open
        try:
            if self.drawer_btn.get_active():
                self.cfg["drawer_pos"] = self.drawer_paned.get_position()
        except Exception:
            pass
        save_json(CONFIG_FILE, self.cfg)
        if not self.no_history_check.get_active():
            save_json(PROCESSED_FILE, self.processed)
        Gtk.main_quit()

    # ================= adding files =================
    def on_add_folder(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Add all images from a folder", parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        last = self.cfg.get("last_folder")
        if last and Path(last).is_dir():
            dialog.set_current_folder(last)
        if dialog.run() == Gtk.ResponseType.OK:
            folder = Path(dialog.get_filename())
            # don't store a source-folder clue when privacy mode is on
            if not self.no_history_check.get_active():
                self.cfg["last_folder"] = str(folder)
                save_json(CONFIG_FILE, self.cfg)
            self.add_paths(sorted(p for p in folder.iterdir()
                                  if p.is_file()
                                  and p.suffix.lower() in SUPPORTED))
        dialog.destroy()

    def on_add_files(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Add images", parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        dialog.set_select_multiple(True)
        f = Gtk.FileFilter()
        f.set_name("Images (jpg, png, webp)")
        for pat in ("*.jpg", "*.jpeg", "*.png", "*.webp",
                    "*.JPG", "*.JPEG", "*.PNG", "*.WEBP"):
            f.add_pattern(pat)
        dialog.add_filter(f)
        if dialog.run() == Gtk.ResponseType.OK:
            self.add_paths([Path(p) for p in dialog.get_filenames()])
        dialog.destroy()

    def on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        paths = []
        for uri in data.get_uris():
            p = Path(GLib.filename_from_uri(uri)[0])
            if p.is_dir():
                paths += sorted(q for q in p.iterdir()
                                if q.is_file()
                                and q.suffix.lower() in SUPPORTED)
            elif p.suffix.lower() in SUPPORTED:
                paths.append(p)
        self.add_paths(paths)

    def add_paths(self, paths):
        new = [str(p) for p in paths if str(p) not in self.info]
        for p in new:
            self.info[p] = {"w": None, "h": None, "meta": {},
                            "resize_to": None, "new_name": None,
                            "strip": False, "done": None}
            self.store.append([p, GLib.markup_escape_text(Path(p).name),
                               GLib.markup_escape_text(
                                   Path(p).suffix.lower().lstrip(".")),
                               "…", "…", ""])
        if new:
            self.set_ops_sensitive(True)
            self._update_counter()
            self.hint.set_label(
                f"{len(self.store)} image(s) loaded. Stage operations, "
                "then press Apply.")
            threading.Thread(target=self._scan, args=(new,),
                             daemon=True).start()

    def on_clear(self, _btn):
        self.store.clear()
        self.info.clear()
        self._status_plain.clear()
        self.meta_store.clear()
        self.thumb.clear()
        self.drawer_title.set_label("Preview")
        self.set_ops_sensitive(False)
        self._update_counter()
        self.hint.set_label("List cleared. Add a folder or images to start.")

    def _scan(self, paths):
        for p in paths:
            w, h = image_dimensions(p)
            meta = read_metadata(p)
            done = None
            try:
                rec = self.processed.get(fingerprint(p))
                if rec:
                    done = rec.get("output", "yes")
            except Exception:
                pass
            self.info[p].update(w=w, h=h, meta=meta, done=done)
            GLib.idle_add(self._refresh_row, p)

    # ================= row display =================
    def _resulting_ext(self, path, st):
        """The extension the output will have, given the Format choice and
        the WebP→JPG-on-clean rule. Mirrors _apply_one."""
        out_format = self.target_format()
        if out_format:
            ext = out_format
        elif st["new_name"]:
            ext = Path(st["new_name"]).suffix.lstrip(".")
        else:
            ext = Path(path).suffix.lstrip(".")
        ext = ext.lower().replace("jpeg", "jpg")
        if st["strip"] and ext == "webp":
            ext = "jpg"
        return ext

    def _refresh_row(self, path):
        st = self.info.get(path)
        if not st:
            return False
        esc = GLib.markup_escape_text

        # File: original name, with the future random name in bold after →
        name = esc(Path(path).name)
        if st["new_name"]:
            name += f"  →  <b>{esc(st['new_name'])}</b>"

        # Format: src ext, with the resulting ext in bold after → if changed
        src_ext = Path(path).suffix.lower().lstrip(".")
        out_ext = self._resulting_ext(path, st)
        if out_ext == src_ext:
            fmt = esc(out_ext)
        else:
            fmt = f"{esc(src_ext)} → <b>{esc(out_ext)}</b>"

        # Dimensions: original, with the new size in bold after →
        if st["w"]:
            dims = f"{st['w']}×{st['h']}"
            if st["resize_to"]:
                nw, nh = st["resize_to"]
                if (nw, nh) != (st["w"], st["h"]):
                    dims += f"  →  <b>{nw}×{nh}</b>"
                else:
                    dims += "  (no change)"
        else:
            dims = "?"

        # Metadata: current state, with the bold "clean" outcome after →
        n = len(st["meta"])
        has_ai = any(any(h_ in k.lower() for h_ in AI_HINTS)
                     for k in st["meta"])
        meta_lbl = ("clean ✓" if n == 0
                    else f"{n} tags" + (" ⚠ AI?" if has_ai else ""))
        if st["strip"] and n > 0:
            meta_lbl += "  →  <b>clean</b>"

        staged = [s for s, on in (("resize", st["resize_to"]),
                                  ("rename", st["new_name"]),
                                  ("clean", st["strip"])) if on]
        if staged:
            status = "staged: " + "+".join(staged)
        elif st["done"]:
            status = f"✅ done → {esc(display_path(st['done']))}"
        else:
            status = ""
        # plain marker to detect a finished/working status set elsewhere
        cur_status_plain = self._status_plain.get(path, "")
        for row in self.store:
            if row[COL_PATH] == path:
                row[COL_NAME] = name
                row[COL_FMT] = fmt
                row[COL_DIMS] = dims
                row[COL_META] = meta_lbl
                if not cur_status_plain.startswith(("✓", "⚠", "error")):
                    row[COL_STATUS] = status
                    self._status_plain[path] = status
                elif staged:
                    row[COL_STATUS] = status
                    self._status_plain[path] = status
        self.tree.columns_autosize()
        return False

    # ================= selection / drawer =================
    def selected_paths(self):
        model, tree_paths = self.tree.get_selection().get_selected_rows()
        if tree_paths:
            return [model[tp][COL_PATH] for tp in tree_paths]
        return [row[COL_PATH] for row in self.store]

    def on_selection_changed(self, selection):
        model, tree_paths = selection.get_selected_rows()
        if not tree_paths:
            return
        path = model[tree_paths[-1]][COL_PATH]
        st = self.info.get(path, {})
        self.drawer_title.set_label(Path(path).name)
        self.meta_store.clear()
        meta = st.get("meta", {})
        if not meta:
            self.meta_store.append(["—", "No meaningful metadata (clean ✓)"])
        else:
            for k, v in sorted(meta.items()):
                self.meta_store.append([k, str(v)])
        threading.Thread(target=self._load_thumb, args=(path,),
                         daemon=True).start()
        self._update_report()

    def _load_thumb(self, path):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, THUMB_WIDTH, THUMB_WIDTH, True)
        except Exception:
            pb = None
        GLib.idle_add(self._set_thumb, path, pb)

    def _set_thumb(self, path, pb):
        model, tree_paths = self.tree.get_selection().get_selected_rows()
        if tree_paths and model[tree_paths[-1]][COL_PATH] == path:
            if pb:
                self.thumb.set_from_pixbuf(pb)
            else:
                self.thumb.clear()
        return False

    def on_drawer_toggle(self, btn):
        if btn.get_active():
            self.drawer_box.show()
            if self._drawer_pos:
                self.drawer_paned.set_position(self._drawer_pos)
        else:
            # remember where the divider was, then hide the panel
            self._drawer_pos = self.drawer_paned.get_position()
            self.drawer_box.hide()

    def _update_report_toggle_label(self):
        arrow = "▾" if self.report_open else "▸"
        self.report_toggle_label.set_markup(
            f"<span weight='bold'>{arrow}  Privacy report</span>")

    def on_report_toggle(self, _btn):
        self.report_open = not self.report_open
        self.report_reveal.set_reveal_child(self.report_open)
        self._update_report_toggle_label()
        if self.report_open:
            self._update_report()

    def set_ops_sensitive(self, state):
        for b in (self.cleanrename_btn, self.resize_check, self.apply_btn):
            b.set_sensitive(state)

    def output_dir(self):
        out = Path(self.out_btn.get_filename()
                   or self.cfg.get("output_dir", str(DEFAULT_OUTPUT)))
        out.mkdir(parents=True, exist_ok=True)
        return out

    def target_format(self):
        """Chosen output extension ('jpg'/'png') or None to keep original."""
        idx = self.format_combo.get_active()
        return self.formats[idx][1] if idx >= 0 else None

    # ================= presets / reset =================
    def _update_width_visibility(self):
        """Show the manual width spinner only when 'Custom' is chosen."""
        idx = self.preset_combo.get_active()
        is_custom = (idx >= 0 and self.presets[idx][1] is None)
        self.width_spin.set_visible(is_custom)
        self.px_label.set_visible(is_custom)

    def _sync_preset_combo(self):
        """Point the combo at the preset matching the spinner, or Custom."""
        self._syncing = True
        val = int(self.width_spin.get_value())
        idx = next((i for i, (_l, v) in enumerate(self.presets)
                    if v == val), len(self.presets) - 1)
        self.preset_combo.set_active(idx)
        self._syncing = False
        self._update_width_visibility()

    def on_preset_changed(self, combo):
        if self._syncing:
            return
        idx = combo.get_active()
        if idx < 0:
            return
        value = self.presets[idx][1]
        if value is not None:
            self._syncing = True
            self.width_spin.set_value(value)
            self._syncing = False
            self._restage_resize()
        self._update_width_visibility()

    def on_width_changed(self, _spin):
        if self._syncing:
            return
        self._sync_preset_combo()
        self._restage_resize()

    # ================= staging (preview only) =================
    def on_toggle_cleanrename(self, btn):
        """Arm/disarm Clean + rename for all loaded images."""
        on = btn.get_active()
        taken = {st["new_name"] for st in self.info.values()
                 if st["new_name"]}
        for p in self.info:
            st = self.info[p]
            st["strip"] = on
            if on:
                if not st["new_name"]:
                    ext = Path(p).suffix.lower().replace(".jpeg", ".jpg")
                    while True:
                        cand = random_name() + ext
                        if cand not in taken:
                            taken.add(cand)
                            break
                    st["new_name"] = cand
            else:
                st["new_name"] = None
            self._refresh_row(p)
        self._staging_hint()

    def on_toggle_resize(self, check):
        """Arm/disarm resize for all loaded images."""
        on = check.get_active()
        target = int(self.width_spin.get_value())
        for p in self.info:
            st = self.info[p]
            if on and st["w"]:
                st["resize_to"] = scaled_to_longest(st["w"], st["h"], target)
            elif not on:
                st["resize_to"] = None
            self._refresh_row(p)
        self._staging_hint()

    def _restage_resize(self):
        """Re-apply resize to all images at the current size (used when the
        size selector changes while resize is armed)."""
        if not self.resize_check.get_active():
            return
        target = int(self.width_spin.get_value())
        for p in self.info:
            st = self.info[p]
            if st["w"]:
                st["resize_to"] = scaled_to_longest(st["w"], st["h"], target)
            self._refresh_row(p)
        self._staging_hint()

    def _update_report(self):
        """Refresh the privacy-report checklist from the currently staged
        operations (uses the selection if any, else the whole list)."""
        if not getattr(self, "report_open", False):
            return
        for child in self.report_box.get_children():
            self.report_box.remove(child)
        sel = self.selected_paths()
        states = [self.info[p] for p in sel if p in self.info]
        if not states:
            self.report_banner.set_text("")
            return

        def all_true(pred):
            return all(pred(s) for s in states)

        strip = all_true(lambda s: s["strip"])
        renamed = all_true(lambda s: s["new_name"])
        resized = all_true(lambda s: s["resize_to"])
        fmt = self.target_format()
        no_hist = self.no_history_check.get_active()

        items = [
            ("Metadata removed", strip),
            ("ICC profile removed", strip),
            ("Re-encoded (encoder trace gone)", strip),
            ("Timestamp normalized", strip),
            ("Random filename", renamed),
            ("Resized", resized),
            ("Forced JPG", fmt == "jpg"),
            ("No app history", no_hist),
        ]
        for label, ok in items:
            row = Gtk.Label(xalign=0.0)
            mark = "✓" if ok else "○"
            row.set_text(f"{mark}  {label}")
            row.get_style_context().add_class(
                "report-ok" if ok else "report-pending")
            self.report_box.pack_start(row, False, False, 0)
        self.report_box.show_all()

        if strip:
            self.report_banner.set_markup(
                "<span weight='bold'>Your files are safe to share.</span>\n"
                "<span size='small'>No metadata, profile, or timestamp "
                "trace. (Does not affect AI watermarks or provider "
                "records.)</span>")
        else:
            self.report_banner.set_markup(
                "<span size='small'>Clean not staged — metadata will "
                "remain. Stage Clean or pick a profile.</span>")

    def _update_counter(self):
        total = len(self.store)
        staged = sum(1 for st in self.info.values()
                     if st["resize_to"] or st["new_name"] or st["strip"])
        if total == 0:
            self.counter.set_text("")
        else:
            self.counter.set_text(f"{total} image(s) · {staged} staged")

    def _staging_hint(self):
        n = sum(1 for st in self.info.values()
                if st["resize_to"] or st["new_name"] or st["strip"])
        self._update_counter()
        self._update_report()
        self.hint.set_label(
            f"{n} image(s) have staged operations — press Apply. "
            "Nothing is written until then." if n else
            "No operations staged. Select images and stage with "
            "Resize / Rename / Clean.")

    # ================= Apply =================
    def on_apply(self, _btn):
        if self.busy:
            return
        out_format = self.target_format()
        jobs = []
        for p in self.selected_paths():
            st = self.info[p]
            has_staged = (st["resize_to"] or st["new_name"] or st["strip"])
            if has_staged or out_format:
                job = dict(st)
                job["out_format"] = out_format
                job["no_history"] = self.no_history_check.get_active()
                jobs.append((p, job))
        if not jobs:
            self.hint.set_label(
                "Nothing staged — click Clean / Rename / Resize first "
                "to preview, then Apply. (Or pick a Format.)")
            return
        out = self.output_dir()
        self.busy = True
        self.set_ops_sensitive(False)
        threading.Thread(target=self._apply_all, args=(jobs, out),
                         daemon=True).start()

    def _apply_all(self, jobs, out):
        ok = 0
        for path, st in jobs:
            GLib.idle_add(self._set_status, path, "working…")
            try:
                dest = self._apply_one(Path(path), st, out)
                GLib.idle_add(self._set_status, path, "verifying…")
                verified, problem = self._verify(dest, st)
                if verified:
                    ok += 1
                    if not st.get("no_history"):
                        # remember this source file as processed (the log
                        # links originals to outputs — skipped in no-history)
                        try:
                            self.processed[fingerprint(path)] = {
                                "output": str(dest),
                                "date": time.strftime("%Y-%m-%d %H:%M"),
                            }
                            save_json(PROCESSED_FILE, self.processed)
                        except Exception:
                            pass
                    self.info[path].update(resize_to=None, new_name=None,
                                           strip=False, done=str(dest))
                    GLib.idle_add(self._refresh_row, path)
                    GLib.idle_add(self._set_status, path,
                                  f"✅ verified → {display_path(dest)}")
                else:
                    GLib.idle_add(self._set_status, path,
                                  f"⚠ check failed: {problem}")
            except Exception as e:
                GLib.idle_add(self._set_status, path,
                              f"error: {e.__class__.__name__}")
        GLib.idle_add(self._done, ok, len(jobs), out)

    def _apply_one(self, src: Path, st: dict, out: Path) -> Path:
        out_format = st.get("out_format")  # None / "jpg" / "png"
        strip = st["strip"]
        # resolve target extension
        if out_format:
            ext = "." + out_format
        elif st["new_name"]:
            ext = Path(st["new_name"]).suffix
        else:
            ext = src.suffix
        ext = ext.lower().replace(".jpeg", ".jpg")
        # A Clean operation re-encodes with standardized JPG/PNG settings, so
        # it can't sensibly emit WebP — redirect a WebP-bound Clean to JPG.
        if strip and ext == ".webp":
            ext = ".jpg"
        # resolve output stem
        stem = Path(st["new_name"]).stem if st["new_name"] else src.stem
        dest = out / (stem + ext)
        i = 1
        while dest.exists():
            dest = out / (random_name() + ext if st["new_name"]
                          else f"{stem}-{i}{ext}")
            i += 1

        resized = st["resize_to"] and (st["resize_to"][0] != st["w"]
                                       or st["resize_to"][1] != st["h"])
        # a format change forces a re-encode even if nothing else changed
        changing_format = ext != src.suffix.lower().replace(".jpeg", ".jpg")
        # JPEG has no alpha — flatten transparency onto white so transparent
        # regions don't become black/garbage in the output.
        flatten = ext == ".jpg"

        if strip or resized or changing_format or flatten:
            cmd = ["convert", str(src)]
            # bake any EXIF orientation into the pixels BEFORE stripping —
            # otherwise removing the orientation tag can leave the image
            # physically sideways
            cmd += ["-auto-orient"]
            if resized:
                # use a longest-side FIT geometry ('NNNxNNN>') rather than
                # forced exact dimensions: this stays correct even after
                # -auto-orient swaps a rotated image's width/height, and the
                # '>' means it only ever shrinks past the target
                longest = max(st["resize_to"])
                cmd += ["-resize", f"{longest}x{longest}>"]
            if flatten:
                cmd += ["-background", "white", "-alpha", "remove",
                        "-alpha", "off"]
            # normalize to plain 8-bit sRGB so 16-bit / CMYK / wide-gamut
            # sources all become bog-standard images (uniform + no exotic
            # bit-depth or colorspace acting as a fingerprint)
            cmd += ["-colorspace", "sRGB", "-depth", "8"]
            if strip:
                # full clean: drop metadata/profiles AND re-encode with
                # standardized generic settings so the output carries no
                # source-tool fingerprint (structural or attached)
                cmd += ["-strip"]
                if ext == ".png":
                    cmd += ["-define", "png:include-chunk=none",
                            "-define", "png:compression-level=9"]
                else:  # jpg
                    cmd += ["-sampling-factor", "4:2:0",
                            "-quality", "95", "-interlace", "none"]
            cmd += [str(dest)]
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        else:
            shutil.copy2(src, dest)

        if strip:
            subprocess.run(["exiftool", "-all=", "-overwrite_original",
                            "-quiet", str(dest)],
                           check=True, capture_output=True, timeout=60)
            now = time.time()
            os.utime(dest, (now, now))
        return dest

    def _verify(self, dest: Path, st: dict):
        """Independently re-check the output file against what was staged.
        Raises on tool failure (caught by the caller) rather than silently
        passing — a clean check must never come from a crashed reader."""
        strip = st["strip"]
        if not dest.exists():
            return False, "output file missing"
        if st["resize_to"]:
            w, h = image_dimensions(str(dest))
            target = max(st["resize_to"])
            # after auto-orient + fit resize, the LONGEST side should equal
            # the target (unless the source was already smaller, in which
            # case '>' left it unchanged and the longest side is ≤ target)
            if w is None:
                return False, "could not read output dimensions"
            if max(w, h) > target:
                return False, f"longest side is {max(w, h)}, expected " \
                              f"≤ {target}"
        if strip:
            # strict read: raises if exiftool fails, so we can't mistake an
            # unreadable file for a clean one
            leftover = read_metadata_strict(str(dest))
            if leftover:
                return False, f"{len(leftover)} metadata tag(s) remain"
            icc = subprocess.run(
                ["exiftool", "-ICC_Profile:all", str(dest)],
                capture_output=True, text=True, timeout=30)
            if icc.returncode != 0:
                return False, "could not verify ICC profile (exiftool failed)"
            if icc.stdout.strip():
                return False, "ICC color profile remains"
        if dest.suffix.lower() not in SUPPORTED:
            return False, "unexpected file extension"
        return True, ""

    def _set_status(self, path, status):
        self._status_plain[path] = status
        markup = GLib.markup_escape_text(status)
        for row in self.store:
            if row[COL_PATH] == path:
                row[COL_STATUS] = markup
        return False

    def _done(self, ok, total, out):
        self.busy = False
        self.set_ops_sensitive(True)
        note = ("✓ file-level anonymity (metadata, profile, timestamp, "
                "encoder) — does not remove AI watermarks or provider "
                "records; check visible content yourself")
        self.hint.set_label(
            f"Done — {ok}/{total} verified, written to {out}. {note}")
        return False

    # ================= misc =================
    def on_open_output(self, _btn):
        subprocess.Popen(["xdg-open", str(self.output_dir())])


def check_dependencies():
    """Return a list of (command, install_hint) for any missing tools."""
    needed = [
        ("exiftool", "sudo apt install libimage-exiftool-perl"),
        ("convert", "sudo apt install imagemagick"),
        ("identify", "sudo apt install imagemagick"),
    ]
    missing = []
    for cmd, hint in needed:
        if shutil.which(cmd) is None:
            missing.append((cmd, hint))
    return missing


def show_dependency_error(missing):
    lines = "\n".join(f"  • {cmd} — install with:  {hint}"
                      for cmd, hint in missing)
    dialog = Gtk.MessageDialog(
        transient_for=None, modal=True,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text="Missing required tools")
    dialog.format_secondary_text(
        f"wallprep needs these command-line tools, which weren't found:\n\n"
        f"{lines}\n\nInstall them and restart.")
    dialog.run()
    dialog.destroy()


if __name__ == "__main__":
    missing = check_dependencies()
    if missing:
        show_dependency_error(missing)
        raise SystemExit(1)
    win = WallprepApp()
    win.show_all()
    win._update_width_visibility()  # after show_all, which would reveal it
    Gtk.main()
