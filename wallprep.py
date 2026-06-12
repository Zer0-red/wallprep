#!/usr/bin/env python3
"""
wallprep — prepare wallpapers for publishing (GTK GUI, v2)

Workflow:
  1. Open a folder -> every image in it appears in the list
  2. Select one, several (Ctrl/Shift+click), or all images
  3. Apply operations with the buttons:
       Resize          -> shrink to chosen width (never upscales)
       Rename          -> random 5-character name
       Strip metadata  -> remove ALL embedded metadata (exiftool)
       Do all 3        -> resize + rename + strip in one go

Files are modified IN PLACE, directly in the folder you opened.
Double-click a row to inspect its full metadata.

Dependencies (Ubuntu):
  sudo apt install python3-gi gir1.2-gtk-3.0 imagemagick libimage-exiftool-perl
Run:
  python3 wallprep.py
"""

import json
import random
import string
import subprocess
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}
NAME_LENGTH = 5

# Tags exiftool reports that aren't real embedded metadata (file system info,
# image structure). Ignored when counting / displaying.
BORING_TAGS = {
    "SourceFile", "ExifToolVersion", "FileName", "Directory", "FileSize",
    "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "FilePermissions", "FileType", "FileTypeExtension", "MIMEType",
    "ImageWidth", "ImageHeight", "ImageSize", "Megapixels",
    "EncodingProcess", "BitsPerSample", "ColorComponents", "YCbCrSubSampling",
    "JFIFVersion", "ResolutionUnit", "XResolution", "YResolution",
    "BitDepth", "ColorType", "Compression", "Filter", "Interlace",
}

# Tag-name fragments that suggest AI generation — flagged in the list.
AI_HINTS = ("software", "artist", "creator", "generator", "prompt", "stable",
            "diffusion", "midjourney", "dall", "comfyui", "parameters",
            "usercomment", "description", "model")

# ListStore columns
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
        self.set_default_size(860, 560)
        self.folder = None
        self.meta_cache = {}   # path -> metadata dict
        self.busy = False

        # ---------- header bar ----------
        header = Gtk.HeaderBar(title="wallprep", show_close_button=True)
        self.set_titlebar(header)

        open_btn = Gtk.Button(label="Open folder…")
        open_btn.connect("clicked", self.on_open_folder)
        header.pack_start(open_btn)

        refresh_btn = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh_btn.set_tooltip_text("Rescan folder")
        refresh_btn.connect("clicked", lambda *_: self.load_folder())
        header.pack_start(refresh_btn)

        self.all_btn = Gtk.Button(label="Do all 3")
        self.all_btn.get_style_context().add_class("suggested-action")
        self.all_btn.set_tooltip_text("Resize + rename + strip metadata")
        self.all_btn.connect("clicked", self.on_do_all)
        header.pack_end(self.all_btn)

        # ---------- toolbar: operations + width ----------
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("set_margin_top", "set_margin_bottom",
                  "set_margin_start", "set_margin_end"):
            getattr(bar, m)(10)

        self.resize_btn = Gtk.Button(label="Resize")
        self.resize_btn.connect("clicked", self.on_resize)
        bar.pack_start(self.resize_btn, False, False, 0)

        self.width_spin = Gtk.SpinButton.new_with_range(480, 7680, 10)
        self.width_spin.set_value(1920)
        bar.pack_start(self.width_spin, False, False, 0)
        bar.pack_start(Gtk.Label(label="px"), False, False, 0)

        bar.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        self.rename_btn = Gtk.Button(label="Rename")
        self.rename_btn.set_tooltip_text(f"Random {NAME_LENGTH}-character name")
        self.rename_btn.connect("clicked", self.on_rename)
        bar.pack_start(self.rename_btn, False, False, 0)

        self.strip_btn = Gtk.Button(label="Strip metadata")
        self.strip_btn.connect("clicked", self.on_strip)
        bar.pack_start(self.strip_btn, False, False, 0)

        sel_all = Gtk.Button(label="Select all")
        sel_all.connect("clicked",
                        lambda *_: self.tree.get_selection().select_all())
        bar.pack_end(sel_all, False, False, 0)

        # ---------- file list ----------
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        for i, (title, expand) in enumerate(
                [("File", True), ("Dimensions", False),
                 ("Metadata", False), ("Status", False)]):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(),
                                     text=i + 1)
            col.set_expand(expand)
            col.set_resizable(True)
            self.tree.append_column(col)
        self.tree.connect("row-activated", self.on_inspect)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.add(self.tree)

        # ---------- hint bar ----------
        self.hint = Gtk.Label(
            label="Open a folder to load its images. Files are modified "
                  "in place. Double-click a row to inspect metadata.")
        self.hint.set_margin_top(6)
        self.hint.set_margin_bottom(8)
        self.hint.get_style_context().add_class("dim-label")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(bar, False, False, 0)
        vbox.pack_start(scrolled, True, True, 0)
        vbox.pack_start(self.hint, False, False, 0)
        self.add(vbox)
        self.set_ops_sensitive(False)

    # ================= folder loading =================
    def on_open_folder(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Choose wallpaper folder", parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        if dialog.run() == Gtk.ResponseType.OK:
            self.folder = Path(dialog.get_filename())
            self.load_folder()
        dialog.destroy()

    def load_folder(self):
        if not self.folder:
            return
        self.store.clear()
        self.meta_cache.clear()
        files = sorted(p for p in self.folder.iterdir()
                       if p.suffix.lower() in SUPPORTED and p.is_file())
        for p in files:
            self.store.append([str(p), p.name, "…", "…", ""])
        self.set_ops_sensitive(bool(files))
        self.hint.set_label(
            f"{len(files)} image(s) in {self.folder} — files are modified "
            "in place" if files else f"No images found in {self.folder}")
        if files:
            threading.Thread(target=self._scan,
                             args=([str(p) for p in files],),
                             daemon=True).start()

    def _scan(self, paths):
        for p in paths:
            w, h = image_dimensions(p)
            meta = read_metadata(p)
            self.meta_cache[p] = meta
            dims = f"{w}×{h}" if w else "?"
            has_ai = any(any(h_ in k.lower() for h_ in AI_HINTS)
                         for k in meta)
            label = ("clean ✓" if not meta
                     else f"{len(meta)} tags" + (" ⚠ AI?" if has_ai else ""))
            GLib.idle_add(self._set_cols, p,
                          {COL_DIMS: dims, COL_META: label})

    def _set_cols(self, path, updates):
        for row in self.store:
            if row[COL_PATH] == path:
                for col, val in updates.items():
                    row[col] = val
        return False

    # ================= selection helpers =================
    def selected_paths(self):
        """Selected rows; if nothing is selected, all rows."""
        model, tree_paths = self.tree.get_selection().get_selected_rows()
        if tree_paths:
            return [model[tp][COL_PATH] for tp in tree_paths]
        return [row[COL_PATH] for row in self.store]

    def set_ops_sensitive(self, state):
        for b in (self.resize_btn, self.rename_btn,
                  self.strip_btn, self.all_btn):
            b.set_sensitive(state)

    # ================= operations =================
    def _run_async(self, worker, paths, *args):
        if self.busy or not paths:
            return
        self.busy = True
        self.set_ops_sensitive(False)
        threading.Thread(target=self._wrap, args=(worker, paths) + args,
                         daemon=True).start()

    def _wrap(self, worker, paths, *args):
        ok = 0
        for p in paths:
            GLib.idle_add(self._set_cols, p, {COL_STATUS: "working…"})
            try:
                worker(p, *args)
                ok += 1
            except Exception as e:
                GLib.idle_add(self._set_cols, p,
                              {COL_STATUS: f"error: {e.__class__.__name__}"})
        GLib.idle_add(self._done, ok, len(paths))

    def _done(self, ok, total):
        self.busy = False
        self.set_ops_sensitive(True)
        self.hint.set_label(f"Done — {ok}/{total} file(s) processed "
                            f"(in place, in {self.folder})")
        return False

    # --- resize ---
    def on_resize(self, _btn):
        self._run_async(self._op_resize, self.selected_paths(),
                        int(self.width_spin.get_value()))

    def _op_resize(self, path, width):
        subprocess.run(["mogrify", "-resize", f"{width}x>", path],
                       check=True, capture_output=True, timeout=120)
        w, h = image_dimensions(path)
        GLib.idle_add(self._set_cols, path,
                      {COL_DIMS: f"{w}×{h}", COL_STATUS: "✓ resized"})

    # --- rename ---
    def on_rename(self, _btn):
        self._run_async(self._op_rename, self.selected_paths())

    def _op_rename(self, path):
        src = Path(path)
        ext = src.suffix.lower().replace(".jpeg", ".jpg")
        while True:
            dest = src.with_name(random_name() + ext)
            if not dest.exists():
                break
        src.rename(dest)
        self.meta_cache[str(dest)] = self.meta_cache.pop(path, {})
        GLib.idle_add(self._after_rename, path, str(dest))

    def _after_rename(self, old, new):
        for row in self.store:
            if row[COL_PATH] == old:
                row[COL_PATH] = new
                row[COL_NAME] = Path(new).name
                row[COL_STATUS] = "✓ renamed"
        return False

    # --- strip metadata ---
    def on_strip(self, _btn):
        self._run_async(self._op_strip, self.selected_paths())

    def _op_strip(self, path):
        subprocess.run(["exiftool", "-all=", "-overwrite_original",
                        "-quiet", path],
                       check=True, capture_output=True, timeout=60)
        self.meta_cache[path] = read_metadata(path)
        n = len(self.meta_cache[path])
        label = "clean ✓" if n == 0 else f"{n} tags"
        GLib.idle_add(self._set_cols, path,
                      {COL_META: label, COL_STATUS: "✓ stripped"})

    # --- all three ---
    def on_do_all(self, _btn):
        self._run_async(self._op_all, self.selected_paths(),
                        int(self.width_spin.get_value()))

    def _op_all(self, path, width):
        self._op_resize(path, width)
        self._op_strip(path)
        self._op_rename(path)   # last, so earlier steps use the old path

    # ================= metadata inspector =================
    def on_inspect(self, _tree, tree_path, _col):
        row = self.store[tree_path]
        path = row[COL_PATH]
        meta = self.meta_cache.get(path) or read_metadata(path)
        dialog = Gtk.Dialog(title=f"Metadata — {Path(path).name}",
                            parent=self, flags=0)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(560, 420)
        if not meta:
            dialog.get_content_area().add(Gtk.Label(
                label="\nNo meaningful metadata — this image is clean. ✓\n"))
        else:
            store = Gtk.ListStore(str, str)
            for k, v in sorted(meta.items()):
                store.append([k, str(v)])
            tv = Gtk.TreeView(model=store)
            for i, title in enumerate(("Tag", "Value")):
                c = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
                c.set_resizable(True)
                tv.append_column(c)
            sw = Gtk.ScrolledWindow()
            sw.set_vexpand(True)
            sw.add(tv)
            dialog.get_content_area().pack_start(sw, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()


if __name__ == "__main__":
    win = WallprepApp()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
