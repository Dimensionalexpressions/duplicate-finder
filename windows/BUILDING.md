# dupe-finder — Windows

Windows port of the duplicate image/video finder. Same detection as Linux
(byte-identical results on the same folder), same GUI.

## What changed for Windows

- **Open file / open folder** — `os.startfile` (ShellExecuteW): filename
  links open the file in its default app, the path line opens Explorer at
  that folder. After ~1.5 s the app calls `SetForegroundWindow`
  (with the `AttachThreadInput` foreground-lock workaround, retrying a few
  times for slow cold starts) so the viewer/window actually comes to front.
  No KWin script, no D-Bus, no portals — those are Linux-only now.
- **Trash** — Recycle Bin via `IFileOperation`
  (`SHCreateItemFromParsingName` + `DeleteItem` with
  `FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT`), i.e. **recoverable**
  (undo from the Recycle Bin works). Legacy `SHFileOperationW` with the
  same flags is the fallback. Paths with spaces are handled.
- **ffmpeg/ffprobe** — resolution order: bundled next to the executable
  (and in PyInstaller's `_internal/` folder), then `PATH`. The CI build
  bundles both, so a clean Windows machine needs nothing installed.
- **OCR / document text** — the 🔍 OCR button's helpers are bundled too:
  `pdftotext` + `pdftoppm` (poppler-windows v26.09.0, oschwartz10612) and
  `tesseract` (UB-Mannheim v5.4.0) with `eng` tessdata. These are MinGW/
  MSYS2 builds, so every runtime DLL ships next to the exes (flat layout);
  `tessdata/eng.traineddata` sits beside `tesseract.exe`, which finds it
  via its default search order. Resolution is the same next-to-exe →
  `PATH` rule, so on Linux the app still uses whatever is installed.
  Bundled: digital PDFs (text layer), scanned PDFs (render pages → OCR),
  images (OCR), and docx/odt/epub (pure-Python XML extraction, no extras).
- **Folder picker** — native Windows dialog (Qt native dialogs; no console
  window, `--windowed` build).
- Linux-only machinery (`gio trash`, xdg-mime/desktop-file resolution,
  xdg-desktop-portal `ShowFolders`, seeded XDG env, KWin raise script)
  stays in the code behind `sys.platform != "win32"` branches; Linux
  behavior is unchanged.

## Run (from the CI artifact)

1. Download `dupe-finder-<version>-win64.zip` from the repo's Actions
   artifacts (or a release, once you publish one).
2. Unzip to anywhere, e.g. `C:\Tools\dupe-finder\`.
3. Double-click `dupe_finder.exe`. No install, no Python, no ffmpeg, no
   tesseract/poppler needed — everything (including the OCR/document-text
   helpers for the 🔍 OCR button) is bundled. No console window appears.
4. Pick the folder to scan (or drag a folder onto the exe shortcut).

## SmartScreen (until a cert exists)

The build is **unsigned** until you add the cert secrets below, so the
first launch shows SmartScreen: *“Windows protected your PC”* →
**More info → Run anyway**. (After unzipping, Windows may also mark the
files “unblocked” automatically; if a file is blocked, right-click →
Properties → **Unblock**.)

## Signing (make the build signed)

Add **two secrets** to the private repo — Settings → Secrets and
variables → Actions → New repository secret:

1. `CERT_P12_B64` — a code-signing certificate in `.p12`/`.pfx` format,
   **base64-encoded**. On Windows:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.p12"))
   ```
   (Export from your browser or `pkcs12 -export -out cert.p12 ...` on
   Linux. The .p12 must include the private key.)
2. `CERT_P12_PASSWORD` — the password protecting that .p12 (empty string
   if it has none).

Then any push re-runs the workflow and the build:
- signs **every `.exe` and `.dll`** in the onedir output with
  `signtool sign /f cert.p12 /p <pw> /fd sha256 /tr http://timestamp.digicert.com /td sha256`,
- signs the **zip** the same way,
- runs `signtool verify /pa /v` on the main exe and **fails the build** if
  verification fails,
- deletes `cert.p12` afterward (it is never committed).

Until the secrets exist, CI logs `signing skipped (no cert secret)` and
ships the unsigned build.

## Test folder (verify detection on Windows)

Build the acceptance test folder (20 files) with:

```powershell
py -3 make_test_folder.py D:\dupe-test   # needs ffmpeg on PATH or in .\vendor
```

It creates:
- (a) `photoA.jpg` + 2 byte-identical copies → **exact image ×3**
- (b) `photoB_full.jpg` + a 60 %/q70 re-encode of the same scene →
  **near image ×2**
- (c) `videoA.mp4` + 1 byte-identical copy, plus a different `videoB.mp4`
  → **exact video ×2** (videoB must NOT join)
- (d) `anim.webp` + a 92 % re-encoded animated copy → **near video ×2**
- (e) 10 unrelated images/videos → no groups

Run:

```powershell
.\dupe-finder\dupe_finder.exe D:\dupe-test
```

**Expected: exactly 4 groups** — `✓ image ×3`, `~ image ×2`,
`✓ video ×2`, `~ video ×2` — nothing else, and no unrelated file in any
group. Then click through the acceptance checklist:

- [ ] click a filename → default viewer opens the file **and comes to the front**
- [ ] click a path line → Explorer opens at that folder, in front
- [ ] “🗑 trash checked” → file disappears and is in the Recycle Bin
      (right-click Recycle Bin → Restore → it comes back)
- [ ] “📁 scan directory…” → rescanning another folder works
- [ ] no console window at any point

## Build it yourself (optional)

```powershell
py -3 -m pip install PySide6 Pillow pyinstaller
# put ffmpeg.exe + ffprobe.exe into .\vendor  (BtbN win64 build)
# optional (OCR button): .\vendor\poppler\  (whole poppler-windows
#   Library\bin dir) and .\vendor\tesseract\  (tesseract.exe + its
#   DLLs + tessdata\eng.traineddata)
py -3 build_windows.py          # -> dist\dupe_finder\
```

The CI workflow (`.github/workflows/windows-build.yml`) does exactly this
on `windows-latest` with Python 3.12: download BtbN win64 ffmpeg, the
poppler-windows binaries and tesseract + `eng` tessdata into `vendor/`,
PyInstaller onedir `--windowed` with `--add-binary` for ffmpeg/ffprobe,
copy the OCR helpers flat next to the exe, optional signing, zip,
upload artifact `dupe-finder-<version>-win64`.
