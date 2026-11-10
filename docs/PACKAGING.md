# Packaging

How Duplicate Finder gets to end users, and the trade-offs between the
options that were evaluated.

## Current distribution

| Channel | What the user gets |
|---|---|
| Windows release zip | PyInstaller **onedir** bundle, double-click the `.exe` |
| Linux release zip | PyInstaller **onedir** bundle, run the binary |
| Nix / NixOS | flake (this repo) — `nix run` / `nix profile install` |
| From source | `linux/` (or `windows/`) venv + `python dupe_finder.py` |

Build scripts: [`linux/build_linux.sh`](../linux/build_linux.sh),
[`windows/build_windows.py`](../windows/build_windows.py) (see
[windows/BUILDING.md](../windows/BUILDING.md)).

## Why the PyInstaller bundles are big

Measured breakdown of a local `linux/build_linux.sh` build
(`dist/dupe_finder/`, 1.4 GB on disk; the release zips are ~280 MB /
~530 MB compressed):

| Component | Size | Note |
|---|---|---|
| `ffmpeg` + `ffprobe` (bundled copies) | 338 MB | full ffmpeg build, every codec |
| `PySide6/` (python module) | 208 MB | `--collect-all PySide6` |
| `libQt6WebEngineCore.so.6` | 195 MB | **unused** — the app has no web view |
| `libicudata.so.78` | 32 MB | ICU data pulled by Qt |
| `libavcodec` / `libx265` / `libaom` | ~50 MB | ffmpeg codec libs |
| `numpy` + `liblapack` | ~27 MB | math stack dragged in transitively |
| `libKF6BreezeIcons` / `libkwin` | ~24 MB | KDE icon/kwin libs (PyInstaller hook false positives) |
| `libgtk-3` | 8 MB | Qt's gtk3 platform theme |

The two biggest levers are the WebEngine module (195 MB, never used) and
the full ffmpeg/ffprobe pair (338 MB).

## Options considered

### PyInstaller (current)

- **Pros:** already working on both platforms; onedir starts fast;
  no toolchain on the user's machine; code-signing story (Windows
  cert) already in place.
- **Cons:** size; `--collect-all PySide6` pulls everything; antivirus
  false positives on frozen Qt apps; not obfuscated.
- **Cheap wins (recommended next step):**
  - Replace `--collect-all PySide6` with targeted collection:
    `--collect-submodules PySide6.QtWidgets PySide6.QtGui PySide6.QtCore`
    plus only the Qt plugins actually needed (platforms, imageformats
    for the formats the app opens, styles). Drops WebEngine + unused
    modules (~300–400 MB).
  - `--exclude-module` for `numpy`, `PyQt5`, test helpers, etc.
  - Stop bundling full ffmpeg/ffprobe: either require them on `PATH`
    (the build scripts already document this) or bundle a lean static
    build (~30–60 MB).
  - Expected result: ~300–500 MB onedir, ~100–180 MB zipped.
  - Keep **onedir** (not onefile): onefile re-extracts to a temp dir on
    every start and gets flagged by AV more often.

### Nuitka

Compiles the app to C and links the CPython runtime — a single native
binary.

- **Pros:** smaller and faster than PyInstaller in most benchmarks;
  better against casual reverse-engineering; no `__pycache__`/`_internal`
  sprawl.
- **Cons:** noticeably longer builds; PySide6 works but needs
  `--include-data-dir`/plugin care per Qt release; less battle-tested
  for big Qt apps than PyInstaller.
- **Verdict:** good candidate if we ever prioritise a smaller/faster
  binary over build simplicity. Worth a one-day benchmark, not a rewrite
  right now.

### shiv / zipapp

A zip containing the venv, run as `python dupe-finder.shiv`
(`zipapp`/`shiv`/`pex`).

- **Pros:** tiny build, ~the size of the real dependencies.
- **Cons:** the user still needs `python3` on PATH — not zero-install,
  so it doesn't meet the "non-technical user" bar for the GUI.
- **Verdict:** fine for CLI tooling; not the primary target here.

### cx_Freeze

PyInstaller-style bundler.

- **Verdict:** similar trade-offs, less active maintenance — no reason
  to switch from PyInstaller.

### Briefcase (BeeWare)

Wraps a Python app in **native installers**: `.msi` (Windows), `.dmg`
(macOS), `.deb`/`.rpm` (Linux).

- **Pros:** the "just install it" UX for non-technical users; proper
  uninstall; integrates code signing.
- **Cons:** another build layer on top of a bundler; smaller community
  for heavy Qt GUI apps.
- **Verdict:** attractive for Windows (`.msi`) if the zip keeps being a
  friction point; revisit if/when a macOS build is wanted.

### AppImage (Linux)

Wrap the onedir bundle in a single portable file (linuxdeploy + the
Qt plugin).

- **Pros:** one file, no root, no dependencies, `--appimage-extract`
  for the curious; very approachable for non-technical Linux users.
- **Cons:** FUSE-less kernels need `--appimage-extract-and-run`; a
  second artifact to maintain.
- **Verdict:** recommended addition for Linux once the PyInstaller
  bundle is slimmed (the AppImage will inherit the slimming).

### Nix flake (shipped with this repo)

- `nix run github:Dimensionalexpressions/duplicate-finder` and friends
  (see the README). Hermetic, reproducible, no bundling bloat — every
  dependency is the nixpkgs build at its natural size.
- **Verdict:** the best path for Nix users; also serves as the
  definition of the real runtime dependencies (Python 3.10+, PySide6,
  Pillow, ffmpeg, poppler-utils, tesseract-ocr + eng data).

### winget (Windows)

Publish a winget manifest pointing at the release zip →
`winget install DuplicateFinder`.

- **Verdict:** cheap to add later; turns the zip into a one-liner for
  Windows users.

## Recommendations

1. **Keep PyInstaller onedir** for the Windows/Linux release zips.
2. **Slim the bundle** (targeted Qt collection, exclude numpy, lean or
   external ffmpeg) — biggest single win, ~60–70 % smaller downloads.
3. **Add an AppImage** for Linux non-technical users.
4. **Nix users get the flake** (done).
5. Revisit **Nuitka** and **Briefcase/winget** if the zips keep being a
   friction point or a macOS build is requested.
