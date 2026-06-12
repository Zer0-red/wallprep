#!/usr/bin/env python3
"""
wallprep — prepare wallpapers for publishing (GTK GUI, v4)

Workflow:
  1. Add images (whole folder or individual files, or drag & drop)
  2. Select rows and click Resize / Rename / Strip metadata —
     these only STAGE the operation and show a PREVIEW in the list
     (e.g. "3000×2000 → 1920×1280"). Click again to un-stage.
  3. Click DO — each image is processed ONCE, with all its staged
     operations combined, into a single output file.

A side drawer (toggle in the header bar, or just select a row) shows
the full metadata of the selected image.

Originals are never modified — DO writes copies to the output folder.

Dependencies (Ubuntu):
  sudo apt install python3-gi gir1.2-gtk-3.0 imagemagick libimage-exiftool-perl
Run:
  python3 wallprep.py
"""

import json
import random
import shutil
import string
import subprocess
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}
NAME_LENGTH = 5
DEFAULT_OUTPUT = Path.home() / "wallpapers" / "ready"

BORING_TAGS = {
    "SourceFile", "ExifToolVersion", "FileName", "Directory", "FileSize",
    "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "FilePermissions", "FileType", "FileTypeExtension", "MIMEType",
    "ImageWidth", "ImageHeight", "ImageSize", "Megapixels",
    "EncodingProcess", "BitsPerSample", "ColorComponents", "YCbCrSubSampling",
    "JFIFVersion", "ResolutionUnit", "XResolution", "YResolution",
    "BitDepth", "ColorType", "Compression", "Filter", "Interlace",
}

AI_HINTS = ("software", "artist", "creator", "generator", "prompt", "stable",
            "diffusion", "midjourney", "dall", "comfyui", "parameters",
            "usercomment", "description", "model")

COL_PATH, COL_NAME, COL_DIMS, COL_META, COL_STATUS = range(5)


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


class WallprepApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="wallprep")
        self.set_default_size(1040, 600)
        # per-file state: path -> {w, h, meta, resize_to, new_name, strip}
        self.info = {}
        self.busy = False

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

        self.do_btn = Gtk.Button(label="DO")
        self.do_btn.get_style_context().add_class("suggested-action")
        self.do_btn.set_tooltip_text(
            "Apply all staged operations — one output file per image")
        self.do_btn.connect("clicked", self.on_do)
        header.pack_end(self.do_btn)

        self.drawer_btn = Gtk.ToggleButton()
        self.drawer_btn.set_image(Gtk.Image.new_from_icon_name(
            "view-dual-symbolic", Gtk.IconSize.BUTTON))
        self.drawer_btn.set_tooltip_text("Show/hide metadata drawer")
        self.drawer_btn.set_active(True)
        self.drawer_btn.connect("toggled", self.on_drawer_toggle)
        header.pack_end(self.drawer_btn)

        # ---------------- toolbar ----------------
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("set_margin_top", "set_margin_bottom",
                  "set_margin_start", "set_margin_end"):
            getattr(bar, m)(10)

        self.resize_btn = Gtk.Button(label="Resize")
        self.resize_btn.set_tooltip_text(
            "Stage a resize for the selected images (preview only — "
            "nothing is written until DO)")
        self.resize_btn.connect("clicked", self.on_stage_resize)
        bar.pack_start(self.resize_btn, False, False, 0)

        self.width_spin = Gtk.SpinButton.new_with_range(480, 7680, 10)
        self.width_spin.set_value(1920)
        bar.pack_start(self.width_spin, False, False, 0)
        bar.pack_start(Gtk.Label(label="px"), False, False, 0)

        self.upscale_check = Gtk.CheckButton(label="Allow upscaling")
        self.upscale_check.set_tooltip_text(
            "If checked, smaller images are enlarged to the target width "
            "(can look soft/blurry)")
        bar.pack_start(self.upscale_check, False, False, 0)

        bar.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        self.rename_btn = Gtk.Button(label="Rename")
        self.rename_btn.set_tooltip_text(
            f"Stage a random {NAME_LENGTH}-character name (preview only)")
        self.rename_btn.connect("clicked", self.on_stage_rename)
        bar.pack_start(self.rename_btn, False, False, 0)

        self.strip_btn = Gtk.Button(label="Strip metadata")
        self.strip_btn.set_tooltip_text(
            "Stage metadata removal (preview only)")
        self.strip_btn.connect("clicked", self.on_stage_strip)
        bar.pack_start(self.strip_btn, False, False, 0)

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

        self.out_btn = Gtk.FileChooserButton(
            title="Choose output folder (use 'Create Folder' for a new one)",
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.out_btn.set_filename(str(DEFAULT_OUTPUT))
        self.out_btn.set_tooltip_text(
            "DO saves processed copies here. Originals are never modified.")
        out_row.pack_start(self.out_btn, True, True, 0)

        open_out = Gtk.Button.new_from_icon_name(
            "folder-open-symbolic", Gtk.IconSize.BUTTON)
        open_out.set_tooltip_text("Open output folder in file manager")
        open_out.connect("clicked", self.on_open_output)
        out_row.pack_start(open_out, False, False, 0)

        # ---------------- file list ----------------
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.get_selection().connect("changed", self.on_selection_changed)
        for i, (title, expand) in enumerate(
                [("File", True), ("Dimensions", False),
                 ("Metadata", False), ("Status", False)]):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(),
                                     text=i + 1)
            col.set_expand(expand)
            col.set_resizable(True)
            self.tree.append_column(col)

        self.tree.drag_dest_set(Gtk.DestDefaults.ALL, [],
                                Gdk.DragAction.COPY)
        self.tree.drag_dest_add_uri_targets()
        self.tree.connect("drag-data-received", self.on_drop)

        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_vexpand(True)
        list_scroll.add(self.tree)

        # ---------------- metadata drawer (right side) ----------------
        self.drawer_title = Gtk.Label(label="Metadata")
        self.drawer_title.set_margin_top(8)
        self.drawer_title.set_margin_bottom(4)
        self.drawer_title.set_ellipsize(3)  # Pango.EllipsizeMode.END

        self.meta_store = Gtk.ListStore(str, str)
        meta_tree = Gtk.TreeView(model=self.meta_store)
        meta_tree.set_headers_visible(True)
        for i, title in enumerate(("Tag", "Value")):
            r = Gtk.CellRendererText()
            r.set_property("wrap-width", 180)
            r.set_property("wrap-mode", 2)  # Pango.WrapMode.WORD_CHAR
            c = Gtk.TreeViewColumn(title, r, text=i)
            c.set_resizable(True)
            meta_tree.append_column(c)
        meta_scroll = Gtk.ScrolledWindow()
        meta_scroll.set_vexpand(True)
        meta_scroll.add(meta_tree)

        drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        drawer_box.pack_start(self.drawer_title, False, False, 0)
        drawer_box.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        drawer_box.pack_start(meta_scroll, True, True, 0)

        self.drawer = Gtk.Revealer()
        self.drawer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.drawer.set_reveal_child(True)
        self.drawer.add(drawer_box)
        self.drawer.set_size_request(320, -1)

        paned = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        paned.pack_start(list_scroll, True, True, 0)
        paned.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        paned.pack_start(self.drawer, False, False, 0)

        # ---------------- hint bar ----------------
        self.hint = Gtk.Label(
            label="Add images, select them, stage operations with the "
                  "buttons (preview only), then press DO.")
        self.hint.set_margin_top(6)
        self.hint.set_margin_bottom(8)
        self.hint.get_style_context().add_class("dim-label")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(bar, False, False, 0)
        vbox.pack_start(out_row, False, False, 0)
        vbox.pack_start(paned, True, True, 0)
        vbox.pack_start(self.hint, False, False, 0)
        self.add(vbox)
        self.set_ops_sensitive(False)

    # ================= adding files =================
    def on_add_folder(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Add all images from a folder", parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        if dialog.run() == Gtk.ResponseType.OK:
            folder = Path(dialog.get_filename())
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
                            "strip": False}
            self.store.append([p, Path(p).name, "…", "…", ""])
        if new:
            self.set_ops_sensitive(True)
            self.hint.set_label(
                f"{len(self.store)} image(s) loaded. Stage operations, "
                "then press DO.")
            threading.Thread(target=self._scan, args=(new,),
                             daemon=True).start()

    def on_clear(self, _btn):
        self.store.clear()
        self.info.clear()
        self.meta_store.clear()
        self.drawer_title.set_label("Metadata")
        self.set_ops_sensitive(False)
        self.hint.set_label("List cleared. Add a folder or images to start.")

    def _scan(self, paths):
        for p in paths:
            w, h = image_dimensions(p)
            meta = read_metadata(p)
            self.info[p].update(w=w, h=h, meta=meta)
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
                                  ("strip", st["strip"])) if on]
        status = "staged: " + "+".join(staged) if staged else ""
        for row in self.store:
            if row[COL_PATH] == path:
                row[COL_NAME] = name
                row[COL_DIMS] = dims
                row[COL_META] = meta_lbl
                if not row[COL_STATUS].startswith(("✓", "error")):
                    row[COL_STATUS] = status
                elif staged:
                    row[COL_STATUS] = status
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

    def on_drawer_toggle(self, btn):
        self.drawer.set_reveal_child(btn.get_active())

    def set_ops_sensitive(self, state):
        for b in (self.resize_btn, self.rename_btn,
                  self.strip_btn, self.do_btn):
            b.set_sensitive(state)

    def output_dir(self):
        out = Path(self.out_btn.get_filename() or DEFAULT_OUTPUT)
        out.mkdir(parents=True, exist_ok=True)
        return out

    # ================= staging (preview only) =================
    def on_stage_resize(self, _btn):
        width = int(self.width_spin.get_value())
        upscale = self.upscale_check.get_active()
        paths = self.selected_paths()
        all_staged = all(self.info[p]["resize_to"] for p in paths)
        for p in paths:
            st = self.info[p]
            if all_staged:
                st["resize_to"] = None
            elif st["w"]:
                if st["w"] > width or upscale:
                    nh = max(1, round(st["h"] * width / st["w"]))
                    st["resize_to"] = (width, nh)
                else:
                    st["resize_to"] = (st["w"], st["h"])  # no change
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

    def on_stage_strip(self, _btn):
        paths = self.selected_paths()
        all_staged = all(self.info[p]["strip"] for p in paths)
        for p in paths:
            self.info[p]["strip"] = not all_staged
            GLib.idle_add(self._refresh_row, p)
        self._staging_hint()

    def _staging_hint(self):
        n = sum(1 for st in self.info.values()
                if st["resize_to"] or st["new_name"] or st["strip"])
        self.hint.set_label(
            f"{n} image(s) have staged operations — press DO to apply. "
            "Nothing is written until then. Click a button again to "
            "un-stage." if n else
            "No operations staged. Select images and stage with the "
            "buttons above.")

    # ================= DO =================
    def on_do(self, _btn):
        if self.busy:
            return
        jobs = [(p, dict(self.info[p])) for p in self.selected_paths()
                if self.info[p]["resize_to"] or self.info[p]["new_name"]
                or self.info[p]["strip"]]
        if not jobs:
            self.hint.set_label(
                "Nothing staged — click Resize / Rename / Strip metadata "
                "first to preview, then DO.")
            return
        out = self.output_dir()
        self.busy = True
        self.set_ops_sensitive(False)
        threading.Thread(target=self._do_all, args=(jobs, out),
                         daemon=True).start()

    def _do_all(self, jobs, out):
        ok = 0
        for path, st in jobs:
            GLib.idle_add(self._set_status, path, "working…")
            try:
                dest = self._do_one(Path(path), st, out)
                ok += 1
                # clear staged ops for this file
                self.info[path].update(resize_to=None, new_name=None,
                                       strip=False)
                GLib.idle_add(self._refresh_row, path)
                GLib.idle_add(self._set_status, path, f"✓ → {dest.name}")
            except Exception as e:
                GLib.idle_add(self._set_status, path,
                              f"error: {e.__class__.__name__}")
        GLib.idle_add(self._done, ok, len(jobs), out)

    def _do_one(self, src: Path, st: dict, out: Path) -> Path:
        # decide the output name (ONE file per image)
        if st["new_name"]:
            dest = out / st["new_name"]
            while dest.exists():
                ext = Path(st["new_name"]).suffix
                dest = out / (random_name() + ext)
        else:
            dest = out / src.name
            i = 1
            while dest.exists():
                dest = out / f"{src.stem}-{i}{src.suffix}"
                i += 1
        # resize (writes dest) or plain copy
        if st["resize_to"] and (st["resize_to"][0] != st["w"]
                                or st["resize_to"][1] != st["h"]):
            geometry = f"{st['resize_to'][0]}x{st['resize_to'][1]}!"
            subprocess.run(["convert", str(src), "-resize", geometry,
                            str(dest)],
                           check=True, capture_output=True, timeout=120)
        else:
            shutil.copy2(src, dest)
        # strip metadata
        if st["strip"]:
            subprocess.run(["exiftool", "-all=", "-overwrite_original",
                            "-quiet", str(dest)],
                           check=True, capture_output=True, timeout=60)
        return dest

    def _set_status(self, path, status):
        for row in self.store:
            if row[COL_PATH] == path:
                row[COL_STATUS] = status
        return False

    def _done(self, ok, total, out):
        self.busy = False
        self.set_ops_sensitive(True)
        self.hint.set_label(f"Done — {ok}/{total} file(s) written to {out} "
                            "(one output per image, originals untouched)")
        return False

    # ================= misc =================
    def on_open_output(self, _btn):
        subprocess.Popen(["xdg-open", str(self.output_dir())])


if __name__ == "__main__":
    win = WallprepApp()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
