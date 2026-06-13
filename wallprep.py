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
NAME_LENGTH = 5
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

COL_PATH, COL_NAME, COL_DIMS, COL_META, COL_STATUS = range(5)

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
    try:
        out = subprocess.run(["exiftool", "-j", "-G0", path],
                             capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout)[0]
    except Exception:
        return {}
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

        self.drawer_btn = Gtk.ToggleButton()
        self.drawer_btn.set_image(Gtk.Image.new_from_icon_name(
            "view-dual-symbolic", Gtk.IconSize.BUTTON))
        self.drawer_btn.set_tooltip_text("Show/hide preview drawer")
        self.drawer_btn.set_active(True)
        self.drawer_btn.connect("toggled", self.on_drawer_toggle)
        header.pack_end(self.drawer_btn)

        # ---------------- toolbar ----------------
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("set_margin_top", "set_margin_bottom",
                  "set_margin_start", "set_margin_end"):
            getattr(bar, m)(10)

        self.clean_btn = Gtk.Button(label="Clean")
        self.clean_btn.set_tooltip_text(
            "Stage metadata removal (preview only)")
        self.clean_btn.connect("clicked", self.on_stage_clean)
        bar.pack_start(self.clean_btn, False, False, 0)

        self.rename_btn = Gtk.Button(label="Rename")
        self.rename_btn.set_tooltip_text(
            f"Stage a random {NAME_LENGTH}-character name (preview only)")
        self.rename_btn.connect("clicked", self.on_stage_rename)
        bar.pack_start(self.rename_btn, False, False, 0)

        self.resize_btn = Gtk.Button(label="Resize")
        self.resize_btn.set_tooltip_text(
            "Stage a resize: the LONGEST side becomes this size "
            "(landscape -> width, portrait -> height). Preview only.")
        self.resize_btn.connect("clicked", self.on_stage_resize)
        bar.pack_start(self.resize_btn, False, False, 0)

        # resolution presets (value = longest side)
        self.presets = [("720p (HD)", 1280), ("1080p (FHD)", 1920),
                        ("1440p (QHD)", 2560), ("4K UHD", 3840),
                        ("8K UHD", 7680), ("Custom", None)]
        self.preset_combo = Gtk.ComboBoxText()
        for label, _v in self.presets:
            self.preset_combo.append_text(label)
        self.preset_combo.set_tooltip_text("Common resolutions")
        bar.pack_start(self.preset_combo, False, False, 0)

        self.width_spin = Gtk.SpinButton.new_with_range(480, 7680, 10)
        self.width_spin.set_value(int(self.cfg.get("width", 1920)))
        self.width_spin.set_tooltip_text("Target size of the longest side")
        bar.pack_start(self.width_spin, False, False, 0)
        bar.pack_start(Gtk.Label(label="px"), False, False, 0)

        # keep combo and spinner in sync (guard against signal loops)
        self._syncing = False
        self._sync_preset_combo()
        self.preset_combo.connect("changed", self.on_preset_changed)
        self.width_spin.connect("value-changed", self.on_width_changed)

        bar.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        self.all_btn = Gtk.Button(label="All 3")
        self.all_btn.set_tooltip_text(
            "Stage Clean + Rename + Resize together (preview only)")
        self.all_btn.connect("clicked", self.on_stage_all)
        bar.pack_start(self.all_btn, False, False, 0)

        self.neutralize_check = Gtk.CheckButton(label="Neutralize")
        self.neutralize_check.set_tooltip_text(
            "Maximum anonymity: re-encode every output with standardized, "
            "metadata-free settings and normalized timestamps, so all "
            "outputs look like generic, identical exports. Implies Clean.")
        self.neutralize_check.set_active(bool(self.cfg.get("neutralize",
                                                           False)))
        bar.pack_start(self.neutralize_check, False, False, 0)

        bar.pack_start(Gtk.Label(label="Format:"), False, False, 0)
        self.formats = [("Keep original", None), ("JPG", "jpg"),
                        ("PNG", "png")]
        self.format_combo = Gtk.ComboBoxText()
        for label, _v in self.formats:
            self.format_combo.append_text(label)
        saved_fmt = self.cfg.get("out_format")  # None / "jpg" / "png"
        self.format_combo.set_active(
            next((i for i, (_l, v) in enumerate(self.formats)
                  if v == saved_fmt), 0))
        self.format_combo.set_tooltip_text(
            "Output format. 'Keep original' preserves each image's format "
            "(except portraits already handled by resize). JPG = small, "
            "PNG = lossless.")
        bar.pack_start(self.format_combo, False, False, 0)

        self.reset_btn = Gtk.Button(label="Reset status")
        self.reset_btn.set_tooltip_text(
            "Clear staged operations AND the remembered 'done' state of "
            "the selected images, so they can be processed fresh")
        self.reset_btn.connect("clicked", self.on_reset_status)
        bar.pack_end(self.reset_btn, False, False, 0)

        sel_all = Gtk.Button(label="Select all")
        sel_all.connect("clicked",
                        lambda *_: self.tree.get_selection().select_all())
        bar.pack_end(sel_all, False, False, 0)

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
        self.out_btn.set_tooltip_text(
            "Apply saves processed copies here. Remembered between sessions.")
        self.out_btn.connect("file-set", self.on_output_changed)
        out_row.pack_start(self.out_btn, True, True, 0)

        open_out = Gtk.Button.new_from_icon_name(
            "folder-open-symbolic", Gtk.IconSize.BUTTON)
        open_out.set_tooltip_text("Open output folder in file manager")
        open_out.connect("clicked", self.on_open_output)
        out_row.pack_start(open_out, False, False, 0)

        # ---------------- file list ----------------
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.get_style_context().add_class("file-list")
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.get_selection().connect("changed", self.on_selection_changed)
        for i, title in enumerate(("File", "Dimensions",
                                   "Metadata", "Status")):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(),
                                     text=i + 1)
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
        self.drawer_title = Gtk.Label(label="Preview")
        self.drawer_title.set_margin_top(8)
        self.drawer_title.set_margin_bottom(4)
        self.drawer_title.set_ellipsize(3)  # Pango.EllipsizeMode.END

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

        drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        drawer_box.pack_start(self.drawer_title, False, False, 0)
        drawer_box.pack_start(self.thumb, False, False, 0)
        drawer_box.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        drawer_box.pack_start(meta_scroll, True, True, 0)

        self.drawer = Gtk.Revealer()
        self.drawer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.drawer.set_reveal_child(True)
        self.drawer.add(drawer_box)
        self.drawer.set_size_request(THUMB_WIDTH + 40, -1)

        main_h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        main_h.pack_start(list_scroll, True, True, 0)
        main_h.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        main_h.pack_start(self.drawer, False, False, 0)

        # ---------------- bottom bar: Apply button ----------------
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.get_style_context().add_class("apply-btn")
        self.apply_btn.set_tooltip_text(
            "Apply all staged operations — one output file per image, "
            "then verify the result")
        self.apply_btn.connect("clicked", self.on_apply)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bottom.set_margin_top(8)
        bottom.set_margin_bottom(4)
        bottom.set_center_widget(self.apply_btn)

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

    def on_destroy(self, _win):
        self.cfg["output_dir"] = self.out_btn.get_filename()
        self.cfg["width"] = int(self.width_spin.get_value())
        self.cfg["neutralize"] = self.neutralize_check.get_active()
        self.cfg["out_format"] = self.target_format()
        save_json(CONFIG_FILE, self.cfg)
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
            self.store.append([p, Path(p).name, "…", "…", ""])
        if new:
            self.set_ops_sensitive(True)
            self.hint.set_label(
                f"{len(self.store)} image(s) loaded. Stage operations, "
                "then press Apply.")
            threading.Thread(target=self._scan, args=(new,),
                             daemon=True).start()

    def on_clear(self, _btn):
        self.store.clear()
        self.info.clear()
        self.meta_store.clear()
        self.thumb.clear()
        self.drawer_title.set_label("Preview")
        self.set_ops_sensitive(False)
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
    def _refresh_row(self, path):
        st = self.info.get(path)
        if not st:
            return False
        name = Path(path).name
        if st["new_name"]:
            name += f"  →  {st['new_name']}"
        if st["w"]:
            dims = f"{st['w']}×{st['h']}"
            if st["resize_to"]:
                nw, nh = st["resize_to"]
                if (nw, nh) != (st["w"], st["h"]):
                    dims += f"  →  {nw}×{nh}"
                else:
                    dims += "  (no change)"
        else:
            dims = "?"
        n = len(st["meta"])
        has_ai = any(any(h_ in k.lower() for h_ in AI_HINTS)
                     for k in st["meta"])
        meta_lbl = ("clean ✓" if n == 0
                    else f"{n} tags" + (" ⚠ AI?" if has_ai else ""))
        if st["strip"] and n > 0:
            meta_lbl += "  →  clean"
        staged = [s for s, on in (("resize", st["resize_to"]),
                                  ("rename", st["new_name"]),
                                  ("clean", st["strip"])) if on]
        if staged:
            status = "staged: " + "+".join(staged)
        elif st["done"]:
            status = f"✅ done → {display_path(st['done'])}"
        else:
            status = ""
        for row in self.store:
            if row[COL_PATH] == path:
                row[COL_NAME] = name
                row[COL_DIMS] = dims
                row[COL_META] = meta_lbl
                if not row[COL_STATUS].startswith(("✓", "⚠", "error")):
                    row[COL_STATUS] = status
                elif staged:
                    row[COL_STATUS] = status
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
        self.drawer.set_reveal_child(btn.get_active())

    def set_ops_sensitive(self, state):
        for b in (self.resize_btn, self.rename_btn,
                  self.clean_btn, self.all_btn, self.apply_btn):
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
    def _sync_preset_combo(self):
        """Point the combo at the preset matching the spinner, or Custom."""
        self._syncing = True
        val = int(self.width_spin.get_value())
        idx = next((i for i, (_l, v) in enumerate(self.presets)
                    if v == val), len(self.presets) - 1)
        self.preset_combo.set_active(idx)
        self._syncing = False

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

    def on_width_changed(self, _spin):
        if self._syncing:
            return
        self._sync_preset_combo()

    def on_reset_status(self, _btn):
        paths = self.selected_paths()
        for p in paths:
            st = self.info.get(p)
            if not st:
                continue
            st.update(resize_to=None, new_name=None, strip=False, done=None)
            try:
                self.processed.pop(fingerprint(p), None)
            except Exception:
                pass
            self._set_status(p, "")
            GLib.idle_add(self._refresh_row, p)
        save_json(PROCESSED_FILE, self.processed)
        self.hint.set_label(
            f"Status reset for {len(paths)} image(s) — staged operations "
            "and 'done' memory cleared.")

    # ================= staging (preview only) =================
    def on_stage_resize(self, _btn):
        target = int(self.width_spin.get_value())
        paths = self.selected_paths()
        all_staged = all(self.info[p]["resize_to"] for p in paths)
        for p in paths:
            st = self.info[p]
            if all_staged:
                st["resize_to"] = None
            elif st["w"]:
                st["resize_to"] = scaled_to_longest(st["w"], st["h"], target)
            GLib.idle_add(self._refresh_row, p)
        self._staging_hint()

    def on_stage_rename(self, _btn):
        paths = self.selected_paths()
        all_staged = all(self.info[p]["new_name"] for p in paths)
        taken = {st["new_name"] for st in self.info.values()
                 if st["new_name"]}
        for p in paths:
            st = self.info[p]
            if all_staged:
                st["new_name"] = None
            else:
                ext = Path(p).suffix.lower().replace(".jpeg", ".jpg")
                while True:
                    cand = random_name() + ext
                    if cand not in taken:
                        taken.add(cand)
                        break
                st["new_name"] = cand
            GLib.idle_add(self._refresh_row, p)
        self._staging_hint()

    def on_stage_clean(self, _btn):
        paths = self.selected_paths()
        all_staged = all(self.info[p]["strip"] for p in paths)
        for p in paths:
            self.info[p]["strip"] = not all_staged
            GLib.idle_add(self._refresh_row, p)
        self._staging_hint()

    def on_stage_all(self, _btn):
        """Stage Clean + Rename + Resize for the selection in one click."""
        target = int(self.width_spin.get_value())
        paths = self.selected_paths()
        taken = {st["new_name"] for st in self.info.values()
                 if st["new_name"]}
        for p in paths:
            st = self.info[p]
            st["strip"] = True
            if st["w"]:
                st["resize_to"] = scaled_to_longest(st["w"], st["h"], target)
            if not st["new_name"]:
                ext = Path(p).suffix.lower().replace(".jpeg", ".jpg")
                while True:
                    cand = random_name() + ext
                    if cand not in taken:
                        taken.add(cand)
                        break
                st["new_name"] = cand
            GLib.idle_add(self._refresh_row, p)
        self._staging_hint()

    def _staging_hint(self):
        n = sum(1 for st in self.info.values()
                if st["resize_to"] or st["new_name"] or st["strip"])
        self.hint.set_label(
            f"{n} image(s) have staged operations — press Apply. "
            "Nothing is written until then." if n else
            "No operations staged. Select images and stage with "
            "Resize / Rename / Clean.")

    # ================= Apply =================
    def on_apply(self, _btn):
        if self.busy:
            return
        neutralize = self.neutralize_check.get_active()
        out_format = self.target_format()
        jobs = []
        for p in self.selected_paths():
            st = self.info[p]
            has_staged = (st["resize_to"] or st["new_name"] or st["strip"])
            if has_staged or neutralize or out_format:
                job = dict(st)
                job["neutralize"] = neutralize
                job["out_format"] = out_format
                jobs.append((p, job))
        if not jobs:
            self.hint.set_label(
                "Nothing staged — click Resize / Rename / Clean first "
                "to preview, then Apply. (Or tick Neutralize / pick a "
                "Format.)")
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
                    # remember this source file as processed
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
        neutralize = st.get("neutralize", False)
        out_format = st.get("out_format")  # None / "jpg" / "png"
        strip = st["strip"] or neutralize
        # resolve target extension
        if out_format:
            ext = "." + out_format
        elif st["new_name"]:
            ext = Path(st["new_name"]).suffix
        else:
            ext = src.suffix
        ext = ext.lower().replace(".jpeg", ".jpg")
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

        if neutralize or strip or resized or changing_format:
            cmd = ["convert", str(src)]
            if resized:
                cmd += ["-resize",
                        f"{st['resize_to'][0]}x{st['resize_to'][1]}!"]
            if strip:
                cmd += ["-strip"]
            if neutralize:
                # standardized, generic encoder settings so outputs are
                # uniform regardless of the source tool
                if ext == ".png":
                    cmd += ["-define", "png:include-chunk=none",
                            "-define", "png:compression-level=9"]
                else:  # jpg
                    cmd += ["-sampling-factor", "4:2:0",
                            "-quality", "90", "-interlace", "none"]
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
        """Independently re-check the output file against what was staged."""
        neutralize = st.get("neutralize", False)
        out_format = st.get("out_format")
        strip = st["strip"] or neutralize
        if not dest.exists():
            return False, "output file missing"
        if out_format and dest.suffix.lower().replace(".jpeg", ".jpg") \
                != "." + out_format:
            return False, f"output is not {out_format.upper()}"
        if st["resize_to"]:
            w, h = image_dimensions(str(dest))
            if (w, h) != tuple(st["resize_to"]):
                return False, f"size is {w}×{h}, expected " \
                              f"{st['resize_to'][0]}×{st['resize_to'][1]}"
        if strip:
            leftover = read_metadata(str(dest))
            if leftover:
                return False, f"{len(leftover)} metadata tag(s) remain"
            icc = subprocess.run(
                ["exiftool", "-ICC_Profile:all", str(dest)],
                capture_output=True, text=True, timeout=30)
            if icc.stdout.strip():
                return False, "ICC color profile remains"
        if dest.suffix.lower() not in SUPPORTED:
            return False, "unexpected file extension"
        return True, ""

    def _set_status(self, path, status):
        for row in self.store:
            if row[COL_PATH] == path:
                row[COL_STATUS] = status
        return False

    def _done(self, ok, total, out):
        self.busy = False
        self.set_ops_sensitive(True)
        self.hint.set_label(
            f"Done — {ok}/{total} file(s) written to {out} and verified "
            "(originals untouched)")
        return False

    # ================= misc =================
    def on_open_output(self, _btn):
        subprocess.Popen(["xdg-open", str(self.output_dir())])


if __name__ == "__main__":
    win = WallprepApp()
    win.show_all()
    Gtk.main()
