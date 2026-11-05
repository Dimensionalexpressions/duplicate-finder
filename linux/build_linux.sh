#!/usr/bin/env bash
# Build a standalone Linux onedir bundle of Duplicate Finder (PyInstaller).
# Requires: python3 (3.10+), ffmpeg, poppler-utils, tesseract-ocr on PATH.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip >/dev/null
pip install -r requirements.txt "pyinstaller>=6,<7"

pyinstaller --noconfirm --clean --windowed \
    --name dupe_finder \
    --collect-all PySide6 \
    dupe_finder.py

echo ""
echo "Done. Output: $(pwd)/dist/dupe_finder/"
echo "Run: ./dist/dupe_finder/dupe_finder [folder]"
