# Duplicate Finder

A Python-based duplicate file manager for pictures, videos, and documents.

## Publisher

**Dimensional Expressions Software**  
Published by **Dimensional Expressions LLC** (Ohio).

Originally conceived and developed by **Coty Julien**.

## What it does

- Scans any folder (drag it onto the window, or use the picker) for:
  - **exact duplicates** — identical size + BLAKE2b content hash
  - **near-duplicate images** — perceptual dHash/aHash + color distance
  - **near-duplicate videos** — sampled-frame fingerprints + duration tolerance
  - **documents** (PDF/DOCX/ODT/EPUB) — by bytes and by extracted text
    (MinHash); a per-group **OCR** button de-duplicates scans and images
    by their text (tesseract, on demand)
- Shows every group side-by-side as cards so you can verify, keep, and
  trash duplicates (trash is undoable until emptied)
- Runs on Windows 10/11 (x64, standalone) and Linux (x64)

## Install & run

### Windows (prebuilt — no install)

1. Download `dupe-finder-<version>-win64.zip` from the
   [Releases page](https://github.com/Dimensionalexpressions/duplicate-finder/releases).
2. Unzip anywhere (e.g. your Desktop). No installer, no dependencies.
3. Double-click `dupe_finder\dupe_finder.exe` and pick a folder.

Building the Windows executable from source: see [windows/BUILDING.md](windows/BUILDING.md).

### Linux (from source)

```bash
sudo apt install ffmpeg poppler-utils tesseract-ocr tesseract-ocr-eng   # Debian/Ubuntu
# (or: dnf / pacman equivalents)
cd linux
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 dupe_finder.py ~/Downloads        # any folder, or omit for the picker
```

A standalone Linux build (PyInstaller) can be produced with
`linux/build_linux.sh`. See [linux/README.md](linux/README.md).

## User guide

**[docs/GUIDE.md](docs/GUIDE.md)** — step-by-step how-to with annotated
screenshots of every button.

## Project layout

| Path | Contents |
|---|---|
| `windows/` | Windows app source, standalone build script, ICU shim, build docs |
| `linux/` | Linux app source (same code, platform-gated), run/build scripts |
| `docs/` | User guide + screenshots |
| `.github/workflows/` | CI: builds the Windows EXE on every push |

The application source is one cross-platform codebase; the `windows/` and
`linux/` copies are kept in sync and differ only in build/packaging around it.

## License

This project is distributed under the **Free Use Attribution License (FUAL-1.0)**.

- Personal and other noncommercial use is free; **Attribution is required for all use** (credit + clickable link to this project).
- Commercial use is free and does not require payment, royalties, or donations; it requires the same Attribution, including for internal company use.
- You may modify the software and keep your modifications and proprietary additions private.
- The software is provided "AS IS", without warranties, subject to the license terms.

Read the complete [FUAL-1.0 license](LICENSE) before using or redistributing this project.

### Required attribution for all use

Include this notice in a reasonably accessible location appropriate to your product or deployment:

> Originally conceived and developed by Coty Julien. Published by Dimensional Expressions LLC.  
> Original project: [Duplicate Finder](https://github.com/Dimensionalexpressions/duplicate-finder)

For a graphical application, an About, Credits, or Acknowledgments screen is suitable. For internal software without a user interface, use accessible internal documentation, a software inventory record, or an internal credits page.

When redistributing the software, include `THIRD-PARTY-NOTICES.md` (or an equivalent notice) as well (FUAL-1.0 §6).

## Donations

Support for continued development may be offered voluntarily through the official Square payment account of Dimensional Expressions LLC. Donations are payable to Dimensional Expressions LLC, not to an individual developer. Donations are never required to download, use, modify, or distribute the software and do not affect the rights granted by FUAL-1.0. Donations to Dimensional Expressions LLC can be made through its [Square donation link](https://square.link/u/tSU1Wmzf).

## Third-party components

Duplicate Finder bundles open-source media and OCR tools (FFmpeg, poppler,
tesseract) plus the Qt/Python runtime; each keeps its own license. The full,
versioned list is in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Project status

- **Windows 1.0.0**: standalone x64 build, built and verified on Windows 11
  (scan, near-duplicate grouping, OCR document de-duplication all pass);
  CI rebuilds it on every push to `main`.
- **Linux**: run from source as above; standalone build script provided.
