#!/usr/bin/env python3
"""
Wallpaper Prep — GUI for preparing wallpapers for publishing.

Features:
  * Add images via button or drag-and-drop
  * See each image's dimensions and how many metadata tags it carries
  * Inspect the full metadata of any image before processing (spot AI tags!)
  * Adjustable resize width (default 1920, never upscales)
  * Process: resize -> random 5-char name -> strip ALL metadata -> output folder

Dependencies (Ubuntu):
  sudo apt install python3-gi gir1.2-gtk-3.0 imagemagick libimage-exiftool-perl
Run:
  python3 wallpaper-prep-gui.py
"""

import json
import os
import random
import string
import subprocess
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_OUTPUT = Path.home() / "wallpapers" / "ready"

# Metadata tags that exiftool reports but that aren't real embedded metadata
# (file system info, image structure). We ignore these when counting.
BORING_TAGS = {
    "SourceFile", "ExifToolVersion", "FileName", "Directory", "FileSize",
    "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "FilePermissions", "FileType", "FileTypeExtension", "MIMEType",
    "ImageWidth", "ImageHeight", "ImageSize", "Megapixels",
    "EncodingProcess", "BitsPerSample", "ColorComponents", "YCbCrSubSampling",
    "JFIFVersion", "ResolutionUnit", "XResolution", "YResolution",
    "BitDepth", "ColorType", "Compression", "Filter", "Interlace",
}

# Tags that strongly suggest AI generation — highlighted in the inspector.
AI_HINTS = ("software", "artist", "creator", "generator", "prompt", "stable",
            "diffusion", "midjourney", "dall", "comfyui", "parameters",
            "usercomment", "description", "model")


def read_metadata(path: str) -> dict:
    """Return {tag: value} of meaningful metadata via exiftool."""
    try:
        out = subprocess.run(
            ["exiftool", "-j", "-G0", path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout)[0]
    except Exception:
        return {}
    meta = {}
    for key, val in data.items():
        bare = key.split(":")[-1]
        if bare in BORING_TAGS:
            continue
        meta[key] = val
    return meta


def image_dimensions(path: str):
    try:
        out = subprocess.run(
            ["identify", "-format", "%w %h", path + "[0]"],
            capture_output=True, text=True, timeout=30,
        )
        w, h = out.stdout.split()
        return int(w), int(h)
    except Exception:
        return None, None


def random_name(length=5):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


class WallpaperApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="Wallpaper Prep")
        self.set_default_size(820, 520)
        self.set_border_width(0)
        self.output_dir = DEFAULT_OUTPUT

        # ----- Header bar -----
        header = Gtk.HeaderBar(title="Wallpaper Prep", show_close_button=True)
        self.set_titlebar(header)

        add_btn = Gtk.Button(label="Add images…")
        add_btn.connect("clicked", self.on_add_clicked)
        header.pack_start(add_btn)

        self.process_btn = Gtk.Button(label="Process all")
        self.process_btn.get_style_context().add_class("suggested-action")
        self.process_btn.connect("clicked", self.on_process_clicked)
        self.process_btn.set_sensitive(False)
        header.pack_end(self.process_btn)

        # ----- Toolbar row: width + output folder -----
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(8)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)

        toolbar.pack_start(Gtk.Label(label="Resize width:"), False, False, 0)
        self.width_spin = Gtk.SpinButton.new_with_range(480, 7680, 10)
        self.width_spin.set_value(1920)
        toolbar.pack_start(self.width_spin, False, False, 0)
        toolbar.pack_start(Gtk.Label(label="px  (never upscales)"), False, False, 0)

        self.out_btn = Gtk.FileChooserButton(
            title="Choose output folder", action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.out_btn.set_filename(str(DEFAULT_OUTPUT))
        toolbar.pack_end(self.out_btn, False, False, 0)
        toolbar.pack_end(Gtk.Label(label="Output:"), False, False, 0)

        # ----- File list -----
        # columns: path, name, dims, meta count str, status, meta dict (hidden)
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.meta_cache = {}  # path -> metadata dict

        self.tree = Gtk.TreeView(model=self.store)
        for i, (title, expand) in enumerate(
            [("File", True), ("Dimensions", False), ("Metadata", False), ("Status", False)]
        ):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=i + 1)
            col.set_expand(expand)
            self.tree.append_column(col)
        self.tree.connect("row-activated", self.on_row_activated)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.add(self.tree)

        # Drag and drop onto the list
        self.tree.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.tree.drag_dest_add_uri_targets()
        self.tree.connect("drag-data-received", self.on_drop)

        # ----- Hint / status bar -----
        self.hint = Gtk.Label(label="Add images or drag them here. Double-click a row to inspect its metadata.")
        self.hint.set_margin_top(6)
        self.hint.set_margin_bottom(8)
        self.hint.get_style_context().add_class("dim-label")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(toolbar, False, False, 0)
        vbox.pack_start(scrolled, True, True, 0)
        vbox.pack_start(self.hint, False, False, 0)
        self.add(vbox)

    # ---------- adding files ----------
    def on_add_clicked(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Choose images", parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        dialog.set_select_multiple(True)
        f = Gtk.FileFilter()
        f.set_name("Images")
        for pat in ("*.jpg", "*.jpeg", "*.png", "*.webp",
                    "*.JPG", "*.JPEG", "*.PNG", "*.WEBP"):
            f.add_pattern(pat)
        dialog.add_filter(f)
        if dialog.run() == Gtk.ResponseType.OK:
            self.add_files(dialog.get_filenames())
        dialog.destroy()

    def on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        paths = [GLib.filename_from_uri(u)[0] for u in data.get_uris()]
        self.add_files(paths)

    def add_files(self, paths):
        existing = {row[0] for row in self.store}
        new = [p for p in paths
               if Path(p).suffix.lower() in SUPPORTED and p not in existing]
        for p in new:
            self.store.append([p, Path(p).name, "…", "…", "pending"])
        if new:
            self.process_btn.set_sensitive(True)
            threading.Thread(target=self._scan_files, args=(new,), daemon=True).start()

    def _scan_files(self, paths):
        """Background: read dimensions + metadata for newly added files."""
        for p in paths:
            w, h = image_dimensions(p)
            meta = read_metadata(p)
            self.meta_cache[p] = meta
            dims = f"{w}×{h}" if w else "?"
            n = len(meta)
            has_ai = any(any(hint in k.lower() for hint in AI_HINTS) for k in meta)
            label = "clean ✓" if n == 0 else f"{n} tags" + (" ⚠ AI?" if has_ai else "")
            GLib.idle_add(self._update_row, p, dims, label)

    def _update_row(self, path, dims, meta_label):
        for row in self.store:
            if row[0] == path:
                row[2] = dims
                row[3] = meta_label
        return False

    # ---------- metadata inspector ----------
    def on_row_activated(self, tree, tree_path, _col):
        row = self.store[tree_path]
        path, meta = row[0], self.meta_cache.get(row[0], {})
        dialog = Gtk.Dialog(title=f"Metadata — {Path(path).name}",
                            parent=self, flags=0)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(560, 420)

        if not meta:
            box = dialog.get_content_area()
            lbl = Gtk.Label(label="\nNo meaningful metadata found — this image is clean. ✓\n")
            box.add(lbl)
        else:
            store = Gtk.ListStore(str, str)
            for k, v in sorted(meta.items()):
                store.append([k, str(v)])
            tv = Gtk.TreeView(model=store)
            for i, title in enumerate(("Tag", "Value")):
                r = Gtk.CellRendererText()
                r.set_property("wrap-width", 320 if i else 200)
                c = Gtk.TreeViewColumn(title, r, text=i)
                c.set_resizable(True)
                tv.append_column(c)
            sw = Gtk.ScrolledWindow()
            sw.set_vexpand(True)
            sw.add(tv)
            dialog.get_content_area().pack_start(sw, True, True, 0)

        dialog.show_all()
        dialog.run()
        dialog.destroy()

    # ---------- processing ----------
    def on_process_clicked(self, _btn):
        self.output_dir = Path(self.out_btn.get_filename() or DEFAULT_OUTPUT)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        width = int(self.width_spin.get_value())
        pending = [row[0] for row in self.store if row[4] == "pending"]
        if not pending:
            return
        self.process_btn.set_sensitive(False)
        threading.Thread(target=self._process_all, args=(pending, width),
                         daemon=True).start()

    def _process_all(self, paths, width):
        done = 0
        for p in paths:
            GLib.idle_add(self._set_status, p, "working…")
            try:
                ext = Path(p).suffix.lower().lstrip(".")
                ext = "jpg" if ext == "jpeg" else ext
                while True:
                    name = random_name()
                    dest = self.output_dir / f"{name}.{ext}"
                    if not dest.exists():
                        break
                subprocess.run(
                    ["convert", p, "-resize", f"{width}x>", str(dest)],
                    check=True, capture_output=True, timeout=120,
                )
                subprocess.run(
                    ["exiftool", "-all=", "-overwrite_original", "-quiet", str(dest)],
                    check=True, capture_output=True, timeout=60,
                )
                done += 1
                GLib.idle_add(self._set_status, p, f"✓ {dest.name}")
            except Exception as e:
                GLib.idle_add(self._set_status, p, f"error: {e.__class__.__name__}")
        GLib.idle_add(self._finished, done)

    def _set_status(self, path, status):
        for row in self.store:
            if row[0] == path:
                row[4] = status
        return False

    def _finished(self, count):
        self.hint.set_label(
            f"Done — {count} image(s) saved to {self.output_dir}  "
            "(originals untouched)"
        )
        self.process_btn.set_sensitive(True)
        return False


if __name__ == "__main__":
    win = WallpaperApp()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
