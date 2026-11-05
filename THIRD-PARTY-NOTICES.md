# Third-Party Notices

This file records third-party software included in Duplicate Finder (Windows
release 1.0.0) and the licenses governing it. FUAL-1.0 does not override any
of these licenses.

**How the bundled tools relate to this project's license:** the application
launches ffmpeg, ffprobe, pdftotext, pdftoppm, and tesseract as *separate
programs* (command line + temporary files). They are not linked into the
application. Their licenses therefore apply to those binaries themselves,
not to the application's own code, which is licensed under FUAL-1.0.

## Application runtime (inside the executable)

| Component | Version | License |
|---|---|---|
| Python (CPython) | 3.12.10 | Python Software Foundation License |
| PySide6 (Qt for Python bindings) | 6.12.0 | LGPL-3.0 |
| Qt libraries (Qt6Core, Qt6Gui, Qt6Widgets, ...) | 6.12.0 | Dual-licensed: **GPL-3.0** or Qt Commercial License (the PySide6 wheel ships the GPL-3.0 build) |
| shiboken6 | 6.12.0 | LGPL-3.0 |
| PyInstaller bootloader | 6.22.3 | GPL-2.0 (embedded in the executable; per PyInstaller's license and FAQ, a built application is not a derivative work of PyInstaller) |
| ICU forwarder shim (`icuuc.dll`) | 1.0 | Part of this project (FUAL-1.0) |

**Qt (GPL-3.0) note:** the application dynamically links the Qt libraries.
The Qt GPL includes a linking exception, so the application's own code may
keep its FUAL-1.0 license. When redistributing, include or offer a means to
obtain the corresponding Qt 6.12.0 GPL source (see
https://www.qt.io/lp/license and https://code.qt.io/). Users who prefer a
non-GPL Qt build may relink with a commercially licensed Qt.

## Media tools (BtbN FFmpeg build)

`ffmpeg.exe` and `ffprobe.exe` are the BtbN community build
**N-128352-gec887b13a8 (2026-11-03)** for Windows x86-64 (MinGW-w64),
configured with `--enable-gpl --enable-version3`.

- **FFmpeg core** — GPL-3.0. Source: https://ffmpeg.org (git). The exact
  configure line is printed by `ffmpeg.exe -buildconf` in the release.
  Build recipe: https://github.com/BtbN/FFmpeg-Builds
- External libraries enabled in this build (each keeps its own license):
  - GPL: libx264 (GPL-3.0 + annex), libx265 (GPL-3.0 + x265 annex),
    libvvenc (GPL-3.0), libxavs2 (GPL-3.0), libkvazaar (GPL-3.0),
    libxvid (GPL-2.0), libgme (GPL-2.0+), libtwolame (GPL-2.0+),
    libzvbi (GPL-2.0+)
  - LGPL: libssh (LGPL-2.1+/GPL-2.0+), libzmq (LGPL-3.0),
    librubberband (LGPL-3.0), libsoxr (LGPL-3.0), libuavs3d (LGPL-3.0),
    libvidstab (LGPL-3.0), libopenjpeg (LGPL-2.1/BSD), libsrt
    (LGPL-2.1+/MPL-2.0/EPL-1.0)
  - BSD/MIT/ISC: libaom-family AV1 decoding via libsvtav1 (BSD-3),
    libvpx (BSD-3), libwebp (BSD-3), libjxl (BSD-3), libopus (BSD-3),
    libtheora (BSD), libopenh264 (BSD-3, Cisco), libopencore-amrnb and
    libopencore-amrwb (BSD-3), libass (BSD-3), libaribcaption (BSD-3),
    liboapv (BSD-3), libsnappy (BSD), libzimg (BSD), libplacebo (ISC),
    librist (ISC), libaribb24 (MIT), libbluray (MIT), libcurl (curl
    license, MIT-style), libopenmpt (zlib license), libmp3lame (LAME
    license), libvpl (Intel oneVPL license)
- **libfdk-aac is NOT included in this build** (`--disable-libfdk-aac`), so
  no Fraunhofer FDK AAC commercial license is required.
- The application only *decodes* video (frame extraction) and reads
  metadata (ffprobe); it performs no encoding, but the decoders/encoders
  above are present in the binary and remain under their own licenses.
- Redistributing these binaries: provide or offer a means to obtain the
  corresponding source (FFmpeg git at the tagged commit + the configure line
  above + the BtbN build recipe).

## Document / OCR tools (MSYS2 builds)

| Component | Version | License |
|---|---|---|
| poppler-utils (`pdftotext.exe`, `pdftoppm.exe`, bundled `poppler.dll`) | 26.09.0 | GPL-2.0-or-later (poppler C++ library carries a linking exception; the tools are separate programs) |
| tesseract (`tesseract.exe`, `libtesseract-5.dll`) | 5.4.0 | Apache-2.0 |
| tessdata_fast (`eng.traineddata`) | 5.x | Apache-2.0 |

Sources — poppler: https://gitlab.freedesktop.org/poppler/poppler ;
tesseract: https://github.com/tesseract-ocr/tesseract ;
tessdata_fast: https://github.com/tesseract-ocr/tessdata_fast .

### Runtime libraries shipped flat next to the executable

These MSYS2/MinGW-w64 and Microsoft runtime DLLs are copied next to the
executable so the tools above run on machines without them installed:

- **GCC runtime:** `libstdc++-6.dll`, `libgcc_s_seh-1.dll` — GPL-3.0 with the
  GCC Runtime Library Exception (https://gcc.gnu.org/onlinedocs/libstdc++/license.html)
- **Winpthreads:** `libwinpthread-1.dll` — LGPL-2.1
- **ICU** `icuuc78.dll`, `icuin78.dll`, `icudt78.dll` (and ICU 75 set
  `icuuc75/icuin75/icudt75` for the tesseract stack) — Unicode, Inc. License
  (MIT-style; https://www.unicode.org/copyright.html)
- **Microsoft Visual C++ runtime 14.51:** `MSVCP140.dll`, `MSVCP140_1.dll`,
  `MSVCP140_2.dll`, `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`,
  `vccorlib140.dll`, `vcomp140.dll`, `vcamp140.dll`,
  `vcruntime140_threads.dll` — Microsoft Visual C++ Redistributable license
  (free to redistribute; https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)
- **Windows Universal CRT:** `ucrtbase.dll` — part of Windows; the bundled
  copy follows the Windows software license terms
- **Image/format codecs** (tesseract stack): libjpeg/IJG and turbojpeg
  (IJG license), libtiff (libtiff permissive license), libpng (libpng
  license), libwebp (BSD-3), libgif (MIT-style), libjbig (JbigKit
  MIT-style), libopenjp2 (LGPL-2.1/BSD), libLerc (BSD-3),
  libsharpyuv (MIT), libzstd (BSD-3), libdeflate (zlib license),
  libexpat (MIT), libpcre2 (BSD-style PCRE2 license), libb2 (Apache-2.0),
  libarchive (BSD/Apache-2.0), libbrotli (MIT), libbz2 (bzip2 license),
  liblz4 (BSD-2), liblzma (public domain / 0BSD), libsnappy (BSD), zlib
  (zlib license)
- **Text shaping/font stack** (tesseract stack): pango + pangoft2 +
  pangocairo + pangowin32 (LGPL-2.1), cairo (MPL-2.0), glib/gio/gobject/
  gmodule (LGPL-2.1), harfbuzz (MPL-2.0/MIT), graphite2 (MIT-style
  HarfBuzz-Engines license), fribidi (MIT), fontconfig (MIT), freetype
  (Freetype Project License, permissive), pixman (MIT/X11), libthai
  (MPL-2.0), libdatrie (MPL-2.0)
- **OpenSSL 3** (`libcrypto-3-x64.dll`) — Apache-2.0 (with SSLeay legacy
  notice where applicable)
- **libiconv / libintl** — GPL-3.0+ with Runtime Library Exception /
  LGPL-2.1+ with Runtime Library Exception (dual)
- **leptonica** (`libleptonica-6.dll`) — Leptonica License (MIT-style)
- **Intel xprofiler runtime** (`xpprof64.dll`) — Intel license, free for use
  and redistribution; pulled in by the MSYS2 poppler build

## Provided by the operating system (not shipped)

Windows kernel APIs and the `api-ms-win-*` API sets (Win10/Win11 native)
are supplied by Windows itself. The release intentionally does **not** ship
MSYS2 `api-ms-win-*.dll` subset files so the OS's full API sets are used.

## Obligation summary for redistributors of Duplicate Finder

1. Keep this file (or its substance) with redistributed copies.
2. For the GPL components (FFmpeg build, poppler-utils, Qt GPL build):
   include or offer a means to obtain the corresponding source — URLs and
   versions above. This is standard practice for bundling GPL tools as
   separate programs.
3. Everything else (LGPL/BSD/MIT/Apache/ISC/Unicode/PSF/Microsoft) requires
   only preserving the notices, as done in this file.
