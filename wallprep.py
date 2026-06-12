#!/usr/bin/env python3
"""
wallprep — prepare wallpapers for publishing (GTK GUI, v3)

Workflow:
  1. Add images: a whole folder ("Add folder…") or individual files
     ("Add images…")
  2. Pick the OUTPUT folder in the toolbar — select an existing one, or
     create a new one right in the chooser dialog (Create Folder button)
  3. Select one, several (Ctrl/Shift+click), or none (= all), then:
       Resize          -> shrink to chosen width (never upscales)
       Rename          -> random 5-character name
       Strip metadata  -> remove ALL embedded metadata (exiftool)
       Do all 3        -> resize + strip + rename in one go

Originals are NEVER modified — every operation writes a processed COPY
into the output folder. Double-click a row to inspect its metadata.

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

# Tags exiftool reports that aren't real embedded metadata.
BORING_TAGS = {
    "SourceFile", "ExifToolVersion", "FileName", "Directory", "FileSize",
    "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "FilePermissions", "FileType", "FileTypeExtension", "MIMEType",
    "ImageWidth", "ImageHeight", "ImageSize", "Megapixels",
    "EncodingProcess", "BitsPerSample", "ColorComponents", "YCbCrSubSampling",
    "JFIFVersion", "ResolutionUnit", "XResolution", "YResolution",
    "BitDepth", "ColorType", "Compression", "Filter", "Interlace",
}

# Tag-name fragments that suggest AI generation.
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
        self.set_default_size(880, 580)
        self.meta_cache = {}
        self.busy = False

        # ---------- header bar ----------
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

        self.all_btn = Gtk.Button(label="Do all 3")
        self.all_btn.get_style_context().add_class("suggested-action")
        self.all_btn.set_tooltip_text("Resize + strip metadata + rename")
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

        # ---------- output folder row ----------
        out_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(out_row, m)(10)

        out_label = Gtk.Label()
        out_label.set_markup("<b>Output folder:</b>")
        out_row.pack_start(out_label, False, False, 0)

        self.out_btn = Gtk.FileChooserButton(
            title="Choose output folder (use 'Create Folder' to make a new one)",
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.out_btn.set_filename(str(DEFAULT_OUTPUT))
        self.out_btn.set_tooltip_text(
            "Processed copies are saved here. Originals are never modified. "
            "You can create a new folder inside the chooser dialog.")
        out_row.pack_start(self.out_btn, True, True, 0)

        open_out = Gtk.Button.new_from_icon_name(
            "folder-open-symbolic", Gtk.IconSize.BUTTON)
        open_out.set_tooltip_text("Open output folder in file manager")
        open_out.connect("clicked", self.on_open_output)
        out_row.pack_start(open_out, False, False, 0)

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

        # drag & drop files/folders onto the list
        self.tree.drag_dest_set(Gtk.DestDefaults.ALL, [],
                                Gdk.DragAction.COPY)
        self.tree.drag_dest_add_uri_targets()
        self.tree.connect("drag-data-received", self.on_drop)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.add(self.tree)

        # ---------- hint bar ----------
        self.hint = Gtk.Label(
            label="Add a folder or individual images (or drag them here). "
                  "Processed copies go to the output folder — originals "
                  "are never touched.")
        self.hint.set_margin_top(6)
        self.hint.set_margin_bottom(8)
        self.hint.get_style_context().add_class("dim-label")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(bar, False, False, 0)
        vbox.pack_start(out_row, False, False, 0)
        vbox.pack_start(scrolled, True, True, 0)
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
        existing = {row[COL_PATH] for row in self.store}
        new = [str(p) for p in paths if str(p) not in existing]
        for p in new:
            self.store.append([p, Path(p).name, "…", "…", ""])
        if new:
            self.set_ops_sensitive(True)
            self.hint.set_label(f"{len(self.store)} image(s) loaded — "
                                "processed copies go to the output folder")
            threading.Thread(target=self._scan, args=(new,),
                             daemon=True).start()

    def on_clear(self, _btn):
        self.store.clear()
        self.meta_cache.clear()
        self.set_ops_sensitive(False)
        self.hint.set_label("List cleared. Add a folder or images to start.")

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

    # ================= helpers =================
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

    def output_dir(self):
        out = Path(self.out_btn.get_filename() or DEFAULT_OUTPUT)
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _dest_same_name(self, src: Path, out: Path) -> Path:
        """Output path keeping the original name; add -1, -2… if taken."""
        dest = out / src.name
        i = 1
        while dest.exists() and dest != src:
            dest = out / f"{src.stem}-{i}{src.suffix}"
            i += 1
        return dest

    def _dest_random(self, src: Path, out: Path) -> Path:
        ext = src.suffix.lower().replace(".jpeg", ".jpg")
        while True:
            dest = out / (random_name() + ext)
            if not dest.exists():
                return dest

    # ================= async runner =================
    def _run_async(self, worker, *args):
        paths = self.selected_paths()
        if self.busy or not paths:
            return
        out = self.output_dir()
        self.busy = True
        self.set_ops_sensitive(False)
        threading.Thread(target=self._wrap,
                         args=(worker, paths, out) + args,
                         daemon=True).start()

    def _wrap(self, worker, paths, out, *args):
        ok = 0
        for p in paths:
            GLib.idle_add(self._set_cols, p, {COL_STATUS: "working…"})
            try:
                dest = worker(Path(p), out, *args)
                ok += 1
                GLib.idle_add(self._set_cols, p,
                              {COL_STATUS: f"✓ → {dest.name}"})
            except Exception as e:
                GLib.idle_add(self._set_cols, p,
                              {COL_STATUS: f"error: {e.__class__.__name__}"})
        GLib.idle_add(self._done, ok, len(paths), out)

    def _done(self, ok, total, out):
        self.busy = False
        self.set_ops_sensitive(True)
        self.hint.set_label(f"Done — {ok}/{total} copy(ies) saved to {out} "
                            "(originals untouched)")
        return False

    # ================= operations (each returns dest Path) ============
    def on_resize(self, _btn):
        self._run_async(self._op_resize, int(self.width_spin.get_value()))

    def _op_resize(self, src, out, width):
        dest = self._dest_same_name(src, out)
        subprocess.run(["convert", str(src), "-resize", f"{width}x>",
                        str(dest)],
                       check=True, capture_output=True, timeout=120)
        return dest

    def on_rename(self, _btn):
        self._run_async(self._op_rename)

    def _op_rename(self, src, out):
        dest = self._dest_random(src, out)
        shutil.copy2(src, dest)
        return dest

    def on_strip(self, _btn):
        self._run_async(self._op_strip)

    def _op_strip(self, src, out):
        dest = self._dest_same_name(src, out)
        shutil.copy2(src, dest)
        subprocess.run(["exiftool", "-all=", "-overwrite_original",
                        "-quiet", str(dest)],
                       check=True, capture_output=True, timeout=60)
        return dest

    def on_do_all(self, _btn):
        self._run_async(self._op_all, int(self.width_spin.get_value()))

    def _op_all(self, src, out, width):
        dest = self._dest_random(src, out)
        subprocess.run(["convert", str(src), "-resize", f"{width}x>",
                        str(dest)],
                       check=True, capture_output=True, timeout=120)
        subprocess.run(["exiftool", "-all=", "-overwrite_original",
                        "-quiet", str(dest)],
                       check=True, capture_output=True, timeout=60)
        return dest

    # ================= misc =================
    def on_open_output(self, _btn):
        subprocess.Popen(["xdg-open", str(self.output_dir())])

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
