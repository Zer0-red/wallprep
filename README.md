# wallprep

A small GTK app for Ubuntu/Linux that prepares wallpapers for publishing:

- 🔍 **Inspect metadata** before publishing — spots AI-generation tags (Stable Diffusion, ComfyUI, Midjourney, prompts, etc.)
- 📐 **Resize** to a chosen width (default 1920px, keeps aspect ratio, never upscales)
- 🎲 **Rename** to a random 5-character string (`k3x9q.png`)
- 🧹 **Strip all metadata** with exiftool (EXIF, XMP, IPTC — everything)

Originals are left untouched; processed copies go to an output folder of your choice.


## Install

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 imagemagick libimage-exiftool-perl
git clone https://github.com/Zer0-red/wallprep.git
cd wallprep
```

## Run

```bash
python3 wallprep.py
```

## Usage

1. Click **Add images…** or drag image files into the window
2. Each image shows its dimensions and a metadata summary — `clean ✓` or e.g. `11 tags ⚠ AI?`
3. **Double-click** any row to see the full metadata, tag by tag
4. Set the resize width and output folder if the defaults aren't right
5. Hit **Process all**

Supported formats: JPG, PNG, WebP.

## Why

Publishing AI-assisted wallpapers without cleaning them leaks the generator
name, prompts, and creation details in the file's metadata. This tool makes
the resize → rename → strip routine a one-click habit.
