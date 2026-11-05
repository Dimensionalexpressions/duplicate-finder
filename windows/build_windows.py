#!/usr/bin/env python3
"""PyInstaller onedir build for dupe_finder (Windows primary, works on Linux).

onedir + --windowed (no console); ffmpeg/ffprobe are bundled via
--add-binaries. PyInstaller 6 places --add-binaries files in the
_internal/ folder; dupe_finder.py resolves them next to the executable
first and _internal/ second, so after the build we move the two binaries
up next to the .exe for a flat, predictable layout.

Prereq: vendor/ffmpeg(.exe) + vendor/ffprobe(.exe) (downloaded by the
CI workflow; on a dev machine copy them into vendor/ yourself).

Optional OCR/doc helpers (Windows): vendor/poppler/pdftotext(.exe) +
vendor/poppler/pdftoppm(.exe) (poppler-windows, oschwartz10612) and
vendor/tesseract/tesseract(.exe) + vendor/tesseract/tessdata/eng.
traineddata (UB-Mannheim tesseract). When present they are copied next
to the .exe; the app resolves them there first, then PATH - so a Linux
dev build without them behaves exactly as before.

Usage:  python build_windows.py
Output: dist/dupe_finder/dupe_finder(.exe) [+ ffmpeg(.exe), ffprobe(.exe),
        pdftotext(.exe), pdftoppm(.exe), tesseract(.exe), tessdata/]
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    win = sys.platform == "win32"
    suffix = ".exe" if win else ""
    vendor = HERE / "vendor"
    bins = []
    for name in ("ffmpeg", "ffprobe"):
        src = vendor / (name + suffix)
        if not src.is_file():
            sys.exit(f"missing {src}\n"
                     f"put win64 ffmpeg/ffprobe into {vendor}/ first "
                     f"(the CI workflow does this automatically)")
        bins += ["--add-binary", f"{src}:."]

    # BUILD_CONSOLE=1 -> console variant (-c) for scripted testing:
    # a --windowed (-w) exe has no stdout, so --no-gui output is lost.
    windowed = os.environ.get("BUILD_CONSOLE") != "1"
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean",
           "--onedir", "-w" if windowed else "-c",
           "--name", "dupe_finder",
           *bins,
           str(HERE / "dupe_finder.py")]
    subprocess.run(cmd, cwd=HERE, check=True)

    # PyInstaller 6 places --add-binary files in _internal/ (contents dir);
    # the app resolves next-to-exe first, _internal/ second, so move the
    # binaries up for a flat layout regardless of PyInstaller version.
    outdir = HERE / "dist" / "dupe_finder"
    internal = outdir / "_internal"
    for name in ("ffmpeg", "ffprobe"):
        f = internal / (name + suffix)
        if f.is_file() and not (outdir / (name + suffix)).is_file():
            shutil.move(str(f), str(outdir / f.name))

    # OCR/doc helpers, copied flat next to the .exe (the app resolves
    # them there first via _resolve_bin, then PATH). These are
    # MinGW/MSYS2 builds: the exes need their sibling runtime DLLs, so
    # copy every .exe/.dll from the vendored dirs. Tesseract first, then
    # poppler (poppler is the newer toolchain; on a same-name DLL
    # collision its copy wins).
    have_poppler = have_tess = False
    for sub in ("tesseract", "poppler"):
        srcdir = vendor / sub
        if not srcdir.is_dir():
            if win:
                print(f"note: {srcdir} missing - OCR falls back to PATH")
            continue
        if sub == "poppler":
            have_poppler = True
        else:
            have_tess = True
        for f in srcdir.iterdir():
            if f.is_file() and f.suffix.lower() in (".exe", ".dll"):
                nl = f.name.lower()
                # Do NOT ship the MSYS2 api-ms-win-*.dll UCRT/API-set copies
                # flat next to the exe. They shadow the OS's native (virtual)
                # API sets, which have the FULL export set; the MSYS2 copies
                # are curated SUBSETS (~40 exports each), so Qt6Core's deps
                # (MPR, USERENV, ...) die with WinError 127 the moment one
                # needs a symbol outside the subset. Windows 10+ provides the
                # API sets natively - the MSYS2 exes resolve them fine without
                # the local copies. (The MSVCP140*/VCRUNTIME140* MSYS2 copies
                # DO have to ship: pdftotext.exe is an MSYS2-CL build that
                # imports them, and 14.5x is a forward-compatible superset of
                # the 14.4x runtime PySide6 uses.)
                if nl.startswith("api-ms-"):
                    continue
                # The vendored poppler set ships a 1 KB icudt.dll STUB next
                # to the real icudt78.dll (33 MB). Copied flat next to the
                # exe it shadows the system icudt.dll in the frozen app's
                # DLL search (exe dir is first) - a latent
                # "DLL load failed while importing QtWidgets" hazard for
                # Qt6Core's unversioned icuuc.dll import. Skip unversioned
                # DLLs that are tiny stubs of a versioned sibling.
                if f.suffix.lower() == ".dll" and f.stat().st_size < 100_000:
                    stem, ver = f.stem, f.name
                    has_ver_sib = any(
                        g.is_file()
                        for g in srcdir.iterdir()
                        if g.suffix.lower() == ".dll"
                        and g.name.startswith(stem)
                        and g.name[len(stem):len(stem) + 1].isdigit())
                    if has_ver_sib:
                        continue
                shutil.copy2(f, outdir / f.name)
    # The vendored poppler set (MSYS2/MinGW) ships a flat UNVERSIONED
    # icuuc.dll. Its ICU 78 exports only *versioned* converter symbols
    # (ucnv_open_78 etc.), while Qt6Core.dll imports the UNVERSIONED names
    # (ucnv_open etc.). Copied flat next to the exe it shadows every other
    # icuuc.dll in the frozen app's DLL search (exe dir is first), so the
    # first PySide6 import dies with
    # "DLL load failed while importing QtWidgets: The specified procedure
    # could not be found" (WinError 127).
    #
    # Fix: replace the top-level icuuc.dll with a tiny forwarder shim
    # (icu-shim/icuuc.dll, built from icu-shim/icushim.def) that re-exports
    # exactly the 20 plain names Qt6Core imports, forwarding each to the
    # versioned icuuc78.dll that already sits next to the OCR tools.
    # Self-contained on Win10 and Win11 (no System32 ICU required).
    shim = HERE / "icu-shim" / "icuuc.dll"
    if shim.is_file():
        shutil.copy2(shim, outdir / "icuuc.dll")
        print("icu fix: icuuc.dll <- forwarder shim -> icuuc78.dll (versioned symbols)")
    else:
        print(f"WARNING: {shim} missing - the app needs a System32 ICU to start")

    td = vendor / "tesseract" / "tessdata"
    if td.is_dir():
        (outdir / "tessdata").mkdir(exist_ok=True)
        for f in td.iterdir():
            if f.is_file():
                shutil.copy2(f, outdir / "tessdata" / f.name)
    if not (have_poppler or have_tess) and win:
        print("note: no OCR helpers vendored - tesseract/pdftotext/pdftoppm fall back to PATH")
    print(f"build done: {outdir}")


if __name__ == "__main__":
    main()
