# Duplicate Finder — Linux

This folder contains the Linux version of Duplicate Finder. The application
source (`dupe_finder.py`) is the same cross-platform codebase as
`windows/dupe_finder.py` (platform-specific behavior is gated inside the
code); this folder adds the Linux run/build scripts and desktop entry.

## Requirements

- Python 3.10+ (3.12 recommended)
- `PySide6` and `Pillow` (installed by `requirements.txt`)
- System packages (all free/open-source, in every major distro repo):
  - **ffmpeg** — video frame extraction and duration probing
  - **poppler-utils** — `pdftotext` / `pdftoppm` for document text
  - **tesseract-ocr** + **tesseract-ocr-eng** — OCR for the per-group
    document de-duplication feature

```bash
# Debian / Ubuntu / Mint
sudo apt install ffmpeg poppler-utils tesseract-ocr tesseract-ocr-eng

# Fedora / RHEL
sudo dnf install ffmpeg-free poppler-utils tesseract tesseract-langpack-eng

# Arch / CachyOS / Manjaro
sudo pacman -S ffmpeg poppler tesseract tesseract-data-eng
```

## Run

```bash
cd linux
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 dupe_finder.py ~/Downloads     # any folder; omit for the picker
```

Or drag a folder onto the `dupe-finder.desktop` entry after installing the
app to your system. The app finds ffmpeg/poppler/tesseract on `PATH`.

## Standalone build (PyInstaller)

```bash
./build_linux.sh
# output: dist/dupe_finder/   (copy the folder anywhere, run ./dupe_finder)
```

The standalone build assumes ffmpeg, poppler-utils and tesseract remain
installed system-wide (they are not bundled on Linux).

## License

Same as the project root: FUAL-1.0 (see ../LICENSE, ../NOTICE,
../THIRD-PARTY-NOTICES.md).
