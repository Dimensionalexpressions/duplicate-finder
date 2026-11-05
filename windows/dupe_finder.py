#!/usr/bin/env python3
"""
dupe_finder.py — find duplicate images & videos in a directory, then verify them
in a GUI window where each duplicate group is laid out side-by-side so you can
eyeball, keep, trash and delete.

Usage:
  python3 dupe_finder.py                           # folder picker, then GUI
  python3 dupe_finder.py [directory]               # scan dir + open GUI window
  python3 dupe_finder.py [directory] --no-gui      # scan + print report
  python3 dupe_finder.py [directory] --csv out.csv
Inside the GUI, "📁 scan directory…" re-scans any other folder.

Detection:
  exact  — identical byte hash (blake2b), grouped by size first
  near   — perceptual hashes (dHash + aHash) for images; for videos the dHash of
           frames at 10/50/90% of duration plus duration tolerance
Dependencies: Python 3.10+, Pillow, PySide6 (GUI only), ffmpeg/ffprobe (videos)
Windows: os.startfile for opening files/folders, Recycle Bin trash via
IFileOperation; ffmpeg/ffprobe may be bundled next to the PyInstaller
onedir executable (resolved first, then PATH).
"""

import argparse
from collections import deque
import datetime as _dt
import html as _html
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="PIL.*")
import hashlib
import io
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

IS_WIN = sys.platform == "win32"   # Windows branch: open/trash/focus

if IS_WIN:
    # GUI app (no console): every child console app (ffmpeg/ffprobe,
    # pdftotext, tesseract, ...) would otherwise pop its own visible
    # command-prompt window during a scan. CREATE_NO_WINDOW hides them.
    _sub_run_orig = subprocess.run
    _sub_popen_orig = subprocess.Popen

    def _sub_run(*a, **kw):
        kw.setdefault("creationflags", 0x08000000)
        return _sub_run_orig(*a, **kw)

    class _sub_popen(_sub_popen_orig):
        def __init__(self, *a, **kw):
            kw.setdefault("creationflags", 0x08000000)
            super().__init__(*a, **kw)

    subprocess.run = _sub_run
    subprocess.Popen = _sub_popen

# ---------------------------------------------------------------- media kinds

IMAGE_EXT = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif",
             ".tiff", ".tif", ".bmp", ".avif"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ogv", ".wmv"}
SKIP_DIRS = {".git", ".svn", ".hg", ".venv", "__pycache__", ".trash", "trash"}

IMAGE_NEAR_THRESH = 50     # images: max Hamming (of 256 bits, 16x16) for near-dup
IMAGE_COLOR_TOL = 40       # images: max mean-RGB euclidean distance (blocks
                           # different documents with similar text density,
                           # e.g. a colorful picture vs a white page)
VIDEO_NEAR_THRESH = 12     # videos: max per-frame Hamming (of 256 bits, 16x16)
VIDEO_DUR_TOL = 0.03       # relative duration tolerance for video pairs
VIDEO_DUR_ABS = 1.0        # or absolute seconds, whichever is larger

# Documents: exact copies group at scan time (byte hash); "🔍 OCR" on a
# group extracts/OCR's the text of its files on demand and re-groups the
# cards by text content (MinHash Jaccard), so true duplicates of the same
# document — even with different encodings, crops or scan quality — sit
# together. PDFs with a text layer are read directly (pdftotext); scanned
# PDFs and images go through tesseract.
DOC_EXT = {".pdf", ".docx", ".odt", ".epub"}
OCR_TEXT_CAP = 20_000      # chars of normalized text kept per file
OCR_SIM_THRESH = 0.75      # MinHash Jaccard >= this = same document
                           # (measured: identical page, PDF vs scan ≈ 0.87;
                           # two different pages of similar material ≈ 0.45)
OCR_PDF_RENDER_PAGES = 10  # scanned PDFs: max pages rendered + OCR'd
# scan_files: a walker wedged in a directory read is abandoned after this
# long once no work remains (a blocked readdir on a stuck volume must not
# freeze the whole scan; its unlisted subtree is counted as skipped)
DIR_JOIN_TIMEOUT = 60.0
SCANDIR_TIMEOUT = 30.0      # per-attempt bound on a single dir read
WALK_HARD_DEADLINE = 1200.0  # absolute cap: the walk must end
# fingerprint_all: a worker blocked inside a native read/stat for this
# long is abandoned and its file marked skipped (same stuck-volume failure
# mode, one file at a time; big videos hash well inside this)
FILE_FP_TIMEOUT = 240.0
# fingerprint cache pass: a per-file stat() that is abandoned after this
# long (treated as a cache miss; the worker pass re-stats with its own
# timeout). Keeps one stuck volume from freezing the whole scan.
STAT_TIMEOUT = 30.0
# the parallel stat pass above: overall deadline for ALL stats; files
# still pending at the deadline are treated as unknown and re-fingerprinted
STAT_PASS_DEADLINE = 60.0
OCR_BATCH_WARN = 500       # groups above this ask how many files to OCR

# ------------------------------------------------------------- fingerprints

def _dhash(img: Image.Image, size: int = 8) -> int:
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = g.tobytes()
    bits = 0
    for r in range(size):
        row = r * (size + 1)
        for c in range(size):
            bits = (bits << 1) | (1 if px[row + c] > px[row + c + 1] else 0)
    return bits

def _ahash(img: Image.Image, size: int = 8) -> int:
    g = img.convert("L").resize((size, size), Image.LANCZOS)
    px = g.tobytes()
    avg = sum(px) / len(px)
    bits = 0
    for p in px:
        bits = (bits << 1) | (1 if p > avg else 0)
    return bits

def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()

def _b256(x) -> bytes:
    """32 big-endian bytes for a 256-bit hash value. Tolerates floats
    (lossy legacy cache rows) and over-wide values: the Hamming
    comparisons use the low 256 bits, and a bare to_bytes(32) raises
    OverflowError on a value that rounded up to 2**256."""
    return (int(x) & ((1 << 256) - 1)).to_bytes(32, "big")

def _mean_rgb(img: Image.Image):
    """(r, g, b) channel means of a 64x64 downscale — cheap document-tone check."""
    px = img.convert("RGB").resize((64, 64)).tobytes()
    n = len(px) // 3
    return (sum(px[0::3]) / n, sum(px[1::3]) / n, sum(px[2::3]) / n)

def _file_hash(path: Path, digest_size: int = 16) -> str | None:
    h = hashlib.blake2b(digest_size=digest_size)
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()

def _image_fingerprint(path: Path):
    """Return (dhash, ahash, dims, thumb_bytes (JPEG), mean_rgb) or Nones."""
    try:
        with Image.open(path) as im:
            im.load()
            # 16x16 (256 bits): 8x8 lumped different document scans/photos
            # together because text-block layouts hash alike at coarse res.
            # Same page photographed repeatedly lands at ~15-50; different
            # pages of the same package jump to ~110+, so 50 is the split.
            d = _dhash(im, 16)
            a = _ahash(im, 16)
            color = _mean_rgb(im)
            dims = im.size
            thumb = ImageOps.exif_transpose(im)
            if thumb.mode not in ("RGB", "L"):
                thumb = thumb.convert("RGB")
            thumb.thumbnail((320, 320), Image.LANCZOS)
            buf = io.BytesIO()
            # JPEG, not PNG: ~8x smaller in RAM and faster to encode —
            # matters for 10k-file scans where every thumb lives in memory.
            thumb.save(buf, "JPEG", quality=85)
            return d, a, dims, buf.getvalue(), color
    except Exception:
        return None, None, None, None, None

# Video fingerprinting spawns ffmpeg/ffprobe subprocesses that decode
# real video. 16 concurrent decoders (the scan worker count) can exhaust
# the RAM of a modest machine (e.g. an 8 GB VM) and take the OS down;
# cap decodes in flight. Images/documents are unaffected.
_VIDEO_DECODE_SEM = threading.Semaphore(4)

def _limited_run(args, **kw):
    """subprocess.run bounded by _VIDEO_DECODE_SEM (ffmpeg/ffprobe)."""
    with _VIDEO_DECODE_SEM:
        return subprocess.run(args, **kw)

def _win_dark_titlebar(widget):
    """Dark window caption on Win10 2004+/Win11 (app UI is always dark)."""
    if not IS_WIN:
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        val = ctypes.c_int(1)
        dwm = ctypes.windll.dwmapi
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 2004+ / Win11);
        # 19 = the pre-2004 attribute, tried as fallback.
        if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), 4) != 0:
            dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(val), 4)
    except Exception:
        pass


def _apply_dark_theme(app):
    """Dark palette + (on Windows) dark caption bars, app-wide.

    The stylesheet paints the main window's surfaces, but QMessageBox/
    QDialog bodies fall back to the SYSTEM palette for anything it does
    not state explicitly — white on a light Windows, where the
    stylesheet's light text becomes unreadable (white on white). The
    palette closes that gap for every window the app creates; on
    Windows additionally every top-level window shown gets a dark DWM
    caption bar (dialogs are spawned in many places, so an app-wide
    Show filter is the only reliable way to cover them all).
    """
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#1b1f27"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#14171d"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#20262f"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#dde4ee"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#2a323e"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#e6ebf3"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#dde4ee"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#2b4d75"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2a323e"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#e6ebf3"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8a93a2"))
    app.setPalette(pal)
    if IS_WIN:
        class _DarkCaption(QObject):
            def eventFilter(self, obj, ev):
                try:
                    if (ev.type() == QEvent.Type.Show
                            and obj.isWidgetType()
                            and not obj.parentWidget()):
                        _win_dark_titlebar(obj)
                except Exception:
                    pass
                return False
        app.installEventFilter(_DarkCaption(app))


def _resolve_bin(name: str) -> str:
    """Runtime path for a bundled helper binary (ffmpeg/ffprobe, tesseract,
    pdftotext/pdftoppm).

    Resolution order: (1) bundled next to the frozen executable - and in
    PyInstaller 6's _internal/ folder, where onedir --add-binaries files
    land by default - (2) PATH. Unfrozen (python dupe_finder.py) always
    resolves via PATH, so a dev machine behaves exactly as before.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        suffix = ".exe" if IS_WIN else ""
        for cand in (exe_dir / (name + suffix),
                     exe_dir / "_internal" / (name + suffix)):
            if cand.is_file():
                return str(cand)
    return name


def _video_meta(path: Path):
    """Return (duration_s, [16x16 frame hashes x5], thumb_bytes (JPEG)) or Nones.

    Five frames are sampled across the video and hashed at 16x16 (256 bits):
    coarse 8x8 hashes lump together different lectures from the same
    presenter/room (whiteboard courses), and 3 sample points can be fooled
    by shared intro slides. Re-encoded copies still hash within a few bits.

    Fast path: one ffprobe (duration + fps) and ONE ffmpeg pass that emits
    all five frames in a single decode (select filter + image2pipe); the
    old per-frame -ss seeking remains as the fallback.
    """
    try:
        out = _limited_run(
            [_resolve_bin("ffprobe"), "-v", "error",
             "-show_entries", "format=duration:stream=avg_frame_rate",
             "-of", "default=nw=1", str(path)],
            capture_output=True, text=True, timeout=30)
        dur = None
        fps = None
        for ln in out.stdout.splitlines():
            k, _, v = ln.partition("=")
            if k == "duration":
                try:
                    dur = float(v)
                except ValueError:
                    pass
            elif k == "avg_frame_rate":
                try:
                    num, _, den = v.partition("/")
                    fps = float(num) / (float(den) or 1.0)
                except ValueError:
                    pass
    except Exception:
        dur = None
        fps = None
    if not dur or dur <= 0:
        dur = 1.0
    frames = [(None, None)] * 5
    thumb = None
    fracs = (0.10, 0.30, 0.50, 0.70, 0.90)
    if fps and fps > 0:
        # Single decode pass: map the five sample points to frame indices,
        # de-duplicate (short videos collapse to fewer distinct frames)
        # and select them all at once.
        n_total = max(1, int(fps * dur))
        target = [min(n_total - 1, max(0, int(fps * dur * f))) for f in fracs]
        slot_of = {}
        for slot, idx in enumerate(target):
            slot_of.setdefault(idx, slot)
        got = False
        try:
            sel = "+".join(f"eq(n\\,{i})" for i in sorted(slot_of))
            r = _limited_run(
                [_resolve_bin("ffmpeg"), "-threads", "2", "-i", str(path),
                 "-vf", f"select='{sel}',scale=320:-2",
                 "-vsync", "vfr", "-frames:v", str(len(slot_of)),
                 "-f", "image2pipe", "-vcodec", "png", "-an",
                 "-loglevel", "error", "-"],
                capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout:
                sig = b"\x89PNG\r\n\x1a\n"
                offs = []
                i = r.stdout.find(sig)
                while i != -1:
                    offs.append(i)
                    i = r.stdout.find(sig, i + len(sig))
                imgs = []
                for k, off in enumerate(offs):
                    end = offs[k + 1] if k + 1 < len(offs) else len(r.stdout)
                    try:
                        im = Image.open(io.BytesIO(r.stdout[off:end]))
                        im.load()
                        imgs.append(im)
                    except Exception:
                        pass
                if imgs:
                    got = True
                    order = sorted(slot_of)
                    for j, im in enumerate(imgs):
                        if j >= len(order):
                            break
                        slot = slot_of[order[j]]
                        frames[slot] = (_dhash(im, 16), _ahash(im, 16))
                    t_src = None
                    for j, im in enumerate(imgs):
                        if j < len(order) and slot_of[order[j]] == 2:
                            t_src = im
                            break
                    if t_src is None:
                        t_src = imgs[-1]
                    t = t_src.copy()
                    if t.mode not in ("RGB", "L"):
                        t = t.convert("RGB")
                    t.thumbnail((320, 320), Image.LANCZOS)
                    buf = io.BytesIO()
                    t.save(buf, "JPEG", quality=85)
                    thumb = buf.getvalue()
        except Exception:
            got = False
        if got:
            return dur, frames, thumb
    # fallback: per-frame seeking (original behaviour, one ffmpeg call each)
    for slot, frac in enumerate(fracs):
        t = max(0.0, dur * frac)
        try:
            r = _limited_run(
                [_resolve_bin("ffmpeg"), "-threads", "2", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=320:-2", "-f", "image2",
                 "-vcodec", "png", "-an", "-loglevel", "error", "-"],
                capture_output=True, timeout=60)
            if r.returncode == 0 and r.stdout:
                with Image.open(io.BytesIO(r.stdout)) as im:
                    im.load()
                    frames[slot] = (_dhash(im, 16), _ahash(im, 16))
                    if slot == 2:
                        im2 = im.copy()
                        if im2.mode not in ("RGB", "L"):
                            im2 = im2.convert("RGB")
                        im2.thumbnail((320, 320), Image.LANCZOS)
                        buf = io.BytesIO()
                        im2.save(buf, "JPEG", quality=85)
                        thumb = buf.getvalue()
        except Exception:
            frames[slot] = (None, None)
    return dur, frames, thumb

def _animated_webp_meta(path: Path):
    """Fingerprint an *animated* WebP as video, without ffmpeg: 5 frames
    sampled at the same 10/30/50/70/90% points as real videos (by frame
    index), each hashed at 16x16; duration = sum of the per-frame
    durations. Returns (dur, [frames], thumb); all-Nones means the WebP is
    static (or unreadable) so the caller falls back to the image path."""
    try:
        with Image.open(path) as im:
            n = getattr(im, "n_frames", 1)
            if n < 2:
                return None, None, None
            total_ms = 0
            for i in range(n):
                try:
                    im.seek(i)
                    total_ms += im.info.get("duration", 100) or 100
                except EOFError:
                    break
            dur = total_ms / 1000.0 or 1.0
            frames = []
            thumb = None
            for frac, is_thumb in ((0.10, False), (0.30, False),
                                   (0.50, True), (0.70, False),
                                   (0.90, False)):
                idx = min(n - 1, max(0, int(n * frac)))
                try:
                    im.seek(idx)
                    im.load()
                    frames.append((_dhash(im, 16), _ahash(im, 16)))
                    if is_thumb:
                        t = im.copy()
                        if t.mode not in ("RGB", "L"):
                            t = t.convert("RGB")
                        t.thumbnail((320, 320), Image.LANCZOS)
                        buf = io.BytesIO()
                        t.save(buf, "JPEG", quality=85)
                        thumb = buf.getvalue()
                except Exception:
                    frames.append((None, None))
            return dur, frames, thumb
    except Exception:
        return None, None, None

# ---------------------------------------------------------------- scanning

class FileRec:
    __slots__ = ("path", "size", "mtime", "kind", "hash", "ph", "ah",
                 "dims", "thumb", "dur", "frames", "error", "color",
                 "text", "mh")

    def __init__(self, path: Path):
        self.path = path
        self.error = None
        try:
            st = path.stat()
            self.size = st.st_size
            self.mtime = st.st_mtime
        except OSError:
            # The file vanished (or became unreadable) between the listing
            # and here — record it on the record; a single bad file must
            # not kill a whole scan.
            self.size = 0
            self.mtime = 0.0
            self.error = "file no longer exists"
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            self.kind = "image"
        elif ext in VIDEO_EXT:
            self.kind = "video"
        else:
            self.kind = "doc"
        self.hash = None
        self.ph = self.ah = self.dims = self.dur = self.frames = None
        self.thumb = None
        self.color = None
        self.text = None      # normalized OCR'd / extracted text (on demand)
        self.mh = None        # its 128-perm MinHash sketch

    def __str__(self):
        return str(self.path)

def scan_files(root: Path, include_hidden: bool, all_dirs: bool = False,
               progress=None, workers: int = 4):
    """Walk `root` and collect the media files.

    Per-directory scandir, not os.walk: on a network share (SMB/CIFS,
    gvfs) a directory read can fail mid-walk (stale handle, session
    reset) and os.walk would let that OSError kill the whole scan. A
    failed directory is retried twice (those hiccups usually clear on
    their own) and only then skipped and counted.

    The walk runs on a small thread pool: on a network share every
    directory read costs round-trips, so a sequential walk of a big
    share takes many minutes — concurrent listing helps (measured on a
    real kio-fuse SMB share: 2-4 workers ≈ +35%, more workers give no
    further gain — the FUSE/KIO layer serializes anyway). A local
    disk walk stays just as fast.
    progress(n_files, n_dirs, n_skipped) fires as the walk proceeds
    (at least every half second) so the GUI shows live counts.
    """
    found = []
    lock = threading.Lock()
    state = {"dirs": 0, "skipped": 0, "last_prog": 0.0}
    queue = deque([root])
    qlock = threading.Lock()

    def _is_junction(entry) -> bool:
        # Windows mount-point reparse point (a "junction"). is_symlink()
        # is False for these, so os.walk-style code follows them and
        # loops. Python 3.12+ has DirEntry.is_junction(); older Pythons
        # (and Linux, where it does not exist) fall back to False.
        if not IS_WIN:
            return False
        try:
            return entry.is_junction()
        except (OSError, AttributeError):
            return False

    def list_dir(d: Path):
        # A single directory read can block INDEFINITELY on this class
        # of VM (Defender mid-scan, vss snapshot, stuck filter driver:
        # 4 workers were seen parked inside os.scandir for 200+ s with
        # the queue still non-empty — the join-loop deadline extends
        # while the queue has items, so the walk deadlocked). OSError
        # retries only cover raised errors, not hangs: bound the read
        # itself with a daemon thread + hard deadline. A wedged dir is
        # abandoned (its thread dies with the process) and counted as
        # skipped, like a dir that raised.
        entries = None
        for delay in (0.0, 0.3, 1.0):
            if delay:
                time.sleep(delay)
            res = []
            def _go():
                try:
                    res.append(list(os.scandir(d)))
                except OSError:
                    pass    # vanished/too-deep dir: same as a raised retry
            t = threading.Thread(target=_go, daemon=True)
            t.start()
            t.join(SCANDIR_TIMEOUT)
            if t.is_alive():
                continue             # wedged: retry, then skip below
            if res:
                entries = res[0]
                break
        subs = []
        files = []
        if entries is not None:
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir:
                    name = entry.name
                    if all_dirs:
                        # .trash-<uid>: the on-volume FreeDesktop trash
                        # that `gio trash` creates on removable mounts —
                        # never rescan it (or trashed files "come back")
                        keep = (name != ".git"
                                and not name.startswith(".trash-"))
                    else:
                        keep = (not name.startswith(".trash-")
                                and (include_hidden
                                     or not (name.startswith(".")
                                             or name in SKIP_DIRS)))
                    # os.walk(followlinks=False) semantics: symlinked
                    # dirs are listed (and pruned) but not descended
                    if keep and not entry.is_symlink() and not _is_junction(entry):
                        subs.append(Path(d) / name)
                else:
                    fn = entry.name
                    ext = Path(fn).suffix.lower()
                    if ext in IMAGE_EXT or ext in VIDEO_EXT or ext in DOC_EXT:
                        files.append(Path(d) / fn)
        fire = None
        with lock:
            if entries is None:
                state["skipped"] += 1
            else:
                state["dirs"] += 1
                found.extend(files)
            # network directories are slow: the count must start
            # moving within a second or the scan looks frozen
            if (state["dirs"] % 8 == 0
                    or time.time() - state["last_prog"] > 0.5):
                state["last_prog"] = time.time()
                fire = (len(found), state["dirs"], state["skipped"])
        if fire and progress:
            progress(*fire)
        if subs:
            with qlock:
                queue.extend(subs)

    # in-flight counter: a worker may only exit once the queue is empty
    # AND no other worker is still inside list_dir. Without this, a
    # worker that sees an empty queue while another is mid-listing exits
    # early and that directory's whole subtree is silently dropped —
    # the walk then "finishes" with a partial file list (seen in the
    # wild: 17k files instead of 38k on the same drive).
    inflight = 0

    def worker():
        nonlocal inflight
        while True:
            with qlock:
                if queue:
                    d = queue.popleft()
                    inflight += 1
                elif inflight == 0:
                    return
                else:
                    # others still in flight may push more dirs;
                    # re-check (a brief sleep under the lock is cheap)
                    time.sleep(0.01)
                    continue
            try:
                list_dir(d)
            finally:
                # even a worker that dies inside list_dir must release
                # its in-flight slot, or the other workers would spin
                # on the re-check path forever
                with qlock:
                    inflight -= 1

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    # A directory read can block INDEFINITELY on a wedged volume/lock
    # (Defender mid-scan of a huge file, a stuck SMB mount, ...): the
    # retries above only cover raised OSErrors, not a hang. Wait for
    # each worker, but only beyond the grace period once the queue has
    # drained; a worker wedged past that is abandoned (daemon: it dies
    # at process exit) and its unlisted dirs count as skipped.
    t_end = time.monotonic() + DIR_JOIN_TIMEOUT
    t_hard = time.monotonic() + WALK_HARD_DEADLINE
    for t in threads:
        while True:
            t.join(0.5)
            if not t.is_alive():
                break
            now = time.monotonic()
            if now > t_hard:
                break            # absolute cap: never walk forever
            with qlock:
                q_left = len(queue)
            if q_left > 0:
                t_end = now + DIR_JOIN_TIMEOUT
            elif now > t_end:
                break
    if progress:
        with lock:
            with qlock:
                if any(t.is_alive() for t in threads):
                    state["skipped"] += len(queue)
                    queue.clear()
            progress(len(found), state["dirs"], state["skipped"])
    return sorted(found)

# ------------------------------------------------------------------ cache

def _stable_smb_key(path: str) -> str:
    """Map a mounted SMB path to its stable smb:// form.

    The on-disk mount prefix is NOT stable: kio-fuse regenerates
    /run/user/<uid>/kio-fuse-<random>/ on every login, and gvfs mount
    points vary too. Using the raw path as the fingerprint-cache key
    would force a full re-fingerprint of the whole share after every
    reboot. smb://<server>/<share>/<sub> is stable, so the cache
    survives. Non-SMB paths pass through unchanged."""
    m = re.match(r"/run/user/\d+/kio-fuse-[^/]+/smb/([^/]+)/([^/]+)(/.*)?$",
                 path)
    if m:
        return f"smb://{m.group(1)}/{m.group(2)}" + (m.group(3) or "")
    m = re.match(
        r"/run/(?:user/\d+/gvfs|media/[^/]+)/smb-share:server=([^,]+),"
        r"share=([^,]+)(/.*)?$", path)
    if m:
        return f"smb://{m.group(1)}/{m.group(2)}" + (m.group(3) or "")
    return path


class FPCache:
    """SQLite fingerprint cache under XDG_CACHE_HOME/dupe-finder.

    Keyed on (path, size, mtime): files unchanged since the previous scan
    skip every decode/hash and rebuild straight from the cache, so
    re-scanning a 10k-file library takes seconds instead of minutes.
    Set DUPE_FINDER_NO_CACHE=1 to disable.
    """
    def __init__(self):
        self.db = None
        try:
            import sqlite3
            d = (Path(os.environ.get("XDG_CACHE_HOME",
                                     Path.home() / ".cache"))
                 / "dupe-finder")
            d.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(d / "fingerprints.sqlite3"),
                                      timeout=15)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS fp (path TEXT PRIMARY KEY,"
                " size INTEGER, mtime REAL, kind TEXT, hash TEXT,"
                " ph TEXT, ah TEXT, dims TEXT, dur REAL,"
                " color TEXT, thumb BLOB, frames TEXT)")
            # old versions declared ph/ah as INTEGER: SQLite's numeric
            # affinity then converted the 256-bit decimal strings to
            # LOSSY REALs (float64) on insert — the low ~200 bits of
            # every hash were gone (false "near" matches), and a hash
            # near 2**256 rounded past it and crashed the near pass
            # (OverflowError: int too big to convert). Rebuild the table
            # with TEXT columns once; the lossy REAL rows are detected
            # as misses by get() and re-fingerprinted the next scan.
            sql = self.db.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'fp'"
            ).fetchone()
            if sql and "ph INTEGER" in (sql[0] or ""):
                self.db.execute(
                    "CREATE TABLE fp_new (path TEXT PRIMARY KEY,"
                    " size INTEGER, mtime REAL, kind TEXT, hash TEXT,"
                    " ph TEXT, ah TEXT, dims TEXT, dur REAL,"
                    " color TEXT, thumb BLOB, frames TEXT)")
                self.db.execute(
                    "INSERT INTO fp_new SELECT path, size, mtime, kind,"
                    " hash, ph, ah, dims, dur, color, thumb, frames"
                    " FROM fp")
                self.db.execute("DROP TABLE fp")
                self.db.execute("ALTER TABLE fp_new RENAME TO fp")
                self.db.commit()
            # on-demand OCR / text-extraction results (path-keyed,
            # validated by size+mtime): re-scans restore r.text/r.mh
            # without re-running tesseract
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS ocr (path TEXT PRIMARY KEY,"
                " size INTEGER, mtime REAL, text TEXT, mh BLOB)")
        except Exception:
            self.db = None

    def get(self, path: str, size: int, mtime: float):
        if not self.db:
            return None
        try:
            row = self.db.execute(
                "SELECT kind, hash, ph, ah, dims, dur, color, thumb, frames"
                " FROM fp WHERE path = ? AND size = ? AND mtime = ?",
                (_stable_smb_key(path), size, mtime)).fetchone()
        except Exception:
            return None
        if not row:
            return None
        try:
            import json
            kind, hash_, ph, ah, dims_s, dur, color_s, thumb, frames_s = row
            # old cache versions stored the 256-bit hashes as REAL
            # (float64): the low ~200 bits were silently lost (false
            # "near" matches), and a hash near 2**256 rounded up past it
            # and crashed the near pass (OverflowError in to_bytes).
            # A row with a float hash is lossy — treat it as a miss and
            # re-fingerprint the file once (it is then stored as text).
            if isinstance(ph, float) or isinstance(ah, float):
                return None
            r = FileRec.__new__(FileRec)
            r.path = Path(path)
            r.size = size
            r.mtime = mtime
            r.kind = kind
            r.hash = hash_
            r.ph = int(ph) if ph is not None else None
            r.ah = int(ah) if ah is not None else None
            r.dur = dur
            r.dims = (tuple(int(x) for x in dims_s.split("x"))
                      if dims_s else None)
            r.color = (tuple(float(c) for c in color_s.split(","))
                       if color_s else None)
            r.thumb = thumb
            r.frames = ([tuple(f) for f in json.loads(frames_s)]
                        if frames_s else None)
            r.text = None
            r.mh = None
            r.error = None
            return r
        except Exception:
            return None

    def put(self, r):
        if not self.db or r.error is not None:
            return          # only cache fully successful fingerprints
        try:
            import json
            self.db.execute(
                "INSERT OR REPLACE INTO fp VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (_stable_smb_key(str(r.path)), r.size, r.mtime, r.kind,
                 r.hash,
                 # 256-bit hashes don't fit a SQLite INTEGER — store as text
                 str(r.ph) if r.ph is not None else None,
                 str(r.ah) if r.ah is not None else None,
                 f"{r.dims[0]}x{r.dims[1]}" if r.dims else None, r.dur,
                 ",".join(str(c) for c in r.color) if r.color else None,
                 r.thumb,
                 json.dumps(r.frames) if r.frames else None))
        except Exception:
            pass

    def commit(self):
        if self.db:
            try:
                self.db.commit()
            except Exception:
                pass

    def load_ocr(self, recs):
        """Restore cached OCR text/sketches onto unchanged records."""
        if not self.db:
            return
        try:
            import struct
            by_path = {}
            # chunk the IN clause: SQLite caps bound parameters
            # (32766), and scans can hold 100k records
            for a in range(0, len(recs), 4000):
                chunk = recs[a:a + 4000]
                qs = ",".join("?" * len(chunk))
                rows = self.db.execute(
                    f"SELECT path, size, mtime, text, mh FROM ocr"
                    f" WHERE path IN ({qs})",
                    [str(r.path) for r in chunk]).fetchall()
                by_path.update({row[0]: row for row in rows})
            for r in recs:
                row = by_path.get(str(r.path))
                if (row is not None and row[1] == r.size
                        and row[2] == r.mtime and row[3]):
                    r.text = row[3]
                    if row[4]:
                        r.mh = tuple(struct.unpack(
                            f"{len(row[4]) // 8}Q", row[4]))
        except Exception:
            pass

    def put_ocr(self, recs):
        """Cache OCR text/sketches (only records that have one)."""
        if not self.db:
            return
        try:
            import struct
            for r in recs:
                if r.text is None:
                    continue
                mh_b = (struct.pack(f"{len(r.mh)}Q", *r.mh)
                        if r.mh else None)
                self.db.execute(
                    "INSERT OR REPLACE INTO ocr VALUES (?,?,?,?,?)",
                    (str(r.path), r.size, r.mtime, r.text, mh_b))
            self.db.commit()
        except Exception:
            pass

def _safe_stat(p, timeout=STAT_TIMEOUT):
    """p.stat() that cannot wedge the caller.

    stat() can block indefinitely on a stuck volume/lock (the same
    failure mode that wedges os.scandir); running it in a daemon
    thread and giving up after `timeout` turns a permanent freeze into
    one file being treated as unknown (re-fingerprinted in the worker
    pass, which has its own per-file timeout).
    """
    res = []

    def _f():
        try:
            res.append(p.stat())
        except OSError:
            res.append(None)

    t = threading.Thread(target=_f, daemon=True)
    t.start()
    t.join(timeout)
    return res[0] if res and not t.is_alive() else None


def _fp_cache_dir() -> Path:
    return (Path(os.environ.get("XDG_CACHE_HOME",
                                Path.home() / ".cache"))
            / "dupe-finder")


def _fp_cache_info():
    """(fingerprint_count, size_bytes) of the cache DB; (0, 0) if absent."""
    p = _fp_cache_dir() / "fingerprints.sqlite3"
    try:
        sz = p.stat().st_size
    except OSError:
        return 0, 0
    n = 0
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        n = int(c.execute("SELECT COUNT(*) FROM fp").fetchone()[0])
        c.close()
    except Exception:
        pass
    return n, sz


def _clear_fp_cache() -> None:
    """Delete the fingerprint cache (DB + any SQLite side files).

    FPCache instances are short-lived (open per scan stage, closed by
    GC), so when no scan is running nothing holds the file open. If a
    background worker does hold an old connection, its writes go to the
    unlinked inode and the next FPCache() starts a fresh database —
    never a corrupt one."""
    d = _fp_cache_dir()
    for name in ("fingerprints.sqlite3", "fingerprints.sqlite3-wal",
                 "fingerprints.sqlite3-shm"):
        try:
            (d / name).unlink()
        except OSError:
            pass


def _prefs_path() -> Path:
    return (Path(os.environ.get("XDG_CONFIG_HOME",
                                Path.home() / ".config"))
            / "dupe-finder" / "preferences.json")


def _load_prefs() -> dict:
    try:
        import json
        d = json.loads(_prefs_path().read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_prefs(d: dict) -> None:
    try:
        import json
        p = _prefs_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1))
    except Exception:
        pass


def _fingerprint_with_cache(paths, progress, interrupted=None):
    """Fingerprint all paths, using the SQLite cache for unchanged files.

    Returns (paths, recs). Files whose size+mtime match a previous scan
    skip every decode/hash; a file with a unique size skips its full-file
    hash (it can never have an exact copy).
    """
    cache = (FPCache()
             if not os.environ.get("DUPE_FINDER_NO_CACHE") else None)
    recs = [None] * len(paths)
    todo = list(range(len(paths)))
    n_cached = 0
    ocr_todo = []
    stats = [None] * len(paths)
    if cache is not None:
        # parallel stat pass: stat() can wedge on a stuck volume; the
        # bounded pool + overall deadline keep one stuck file from
        # serializing the whole pass (a sequential 30 s per file made a
        # full-C: rescan take an hour). Wedged files come back as None
        # and are re-fingerprinted, which has its own per-file timeout.
        import concurrent.futures
        _orig_thread = threading.Thread

        class _DaemonThread(_orig_thread):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.daemon = True

        threading.Thread = _DaemonThread
        try:
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=16)
        finally:
            threading.Thread = _orig_thread
        futs = {}
        for i in todo:
            futs[ex.submit(paths[i].stat)] = i
        try:
            for fut in concurrent.futures.as_completed(
                    futs, timeout=STAT_PASS_DEADLINE):
                i = futs[fut]
                try:
                    stats[i] = fut.result()
                except OSError:
                    stats[i] = None
        except concurrent.futures.TimeoutError:
            pass                # the rest stay None
        ex.shutdown(wait=False, cancel_futures=True)
        keep = []
        for i in todo:
            st = stats[i]
            if st is None:        # stat wedged/failed: re-fingerprint it
                keep.append(i)
                continue
            r = cache.get(str(paths[i]), st.st_size, st.st_mtime)
            if r is not None:
                recs[i] = r
                n_cached += 1
            else:
                keep.append(i)
        todo = keep
    if n_cached:
        progress(f"loaded {n_cached}/{len(paths)} from cache "
                 "(unchanged files skip re-decoding)")
    sizes = {}
    for i in range(len(paths)):
        r = recs[i]
        if r is not None:
            s = r.size
        else:
            st = stats[i]
            s = st.st_size if st is not None else 0
        sizes[s] = sizes.get(s, 0) + 1
    dup_sizes = {s for s, c in sizes.items() if c > 1}
    if todo:
        out = fingerprint_all([paths[i] for i in todo], progress, dup_sizes,
                              interrupted)
        for j, rec in enumerate(out):
            if isinstance(rec, FileRec):
                # interrupted: unfinished entries are still Path objects
                recs[todo[j]] = rec
                if cache is not None:
                    cache.put(rec)
                    if rec.text is not None:
                        ocr_todo.append(rec)
        if cache is not None:
            if ocr_todo:
                cache.put_ocr(ocr_todo)
            cache.commit()
    # restore on-demand OCR results so a re-scan does not re-run
    # tesseract for files that were already OCR'd
    if cache is not None:
        cache.load_ocr([r for r in recs if r is not None])
    return paths, recs

def fingerprint_all(paths, progress, dup_sizes=None, interrupted=None):
    total = len(paths)
    import concurrent.futures

    # SMB/CIFS drops reads transiently (EIO, connection reset, stale
    # handle, ...): retry once, with a beat in between, before marking
    # a file skipped
    _TRANSIENT_OS = (5, 32, 104, 107, 108, 110, 112, 117, 125)

    def _do_fingerprint(rec, p, dup_sizes):
        # Exact duplicates must share a byte size, so a file with a
        # unique size can never have an exact copy — skip its
        # full-file hash (saves most of the hashing on big scans).
        rec.hash = (_file_hash(p)
                    if dup_sizes is None or rec.size in dup_sizes
                    else None)
        ext = p.suffix.lower()
        if ext == ".webp":
            # animated WebP is video-like: 5 sampled frames hashed via
            # Pillow; a static WebP falls through to the image path
            rec.dur, rec.frames, rec.thumb = _animated_webp_meta(p)
            if rec.frames and any(f[0] is not None for f in rec.frames):
                rec.kind = "video"
        if rec.kind == "image":
            rec.ph, rec.ah, rec.dims, rec.thumb, rec.color = \
                _image_fingerprint(p)
            if rec.ph is None and ext not in (".heic", ".heif"):
                rec.error = "unreadable image"
        elif rec.kind == "doc":
            # extract the text at scan time (digital PDFs via
            # pdftotext, office files from their XML) so copies
            # of the same document with different bytes group
            # together by content; scanned PDFs yield little
            # text here and are handled on demand by the group's
            # 🔍 OCR button (page render + tesseract)
            rec.text = extract_doc_text(rec, render=False)
            rec.mh = (minhash_of(rec.text)
                      if len(rec.text) >= 40 else None)
        else:
            if ext != ".webp":
                rec.dur, rec.frames, rec.thumb = _video_meta(p)
            if (not rec.frames
                    or all(f[0] is None for f in rec.frames)):
                rec.error = "could not read video frames"

    def work(i, p):
        rec = FileRec(p)
        if rec.error is None:
            for attempt in (1, 2):
                try:
                    _do_fingerprint(rec, p, dup_sizes)
                    break
                except OSError as e:
                    if attempt == 1 and e.errno in _TRANSIENT_OS:
                        time.sleep(0.5)
                        continue
                    # one bad file (vanished mid-scan, unreadable share)
                    # must not kill a 10k-file scan: mark it and move on
                    if rec.error is None:
                        rec.error = f"skipped: {type(e).__name__}: {e}"
                    break
                except Exception as e:
                    # one bad file (vanished mid-scan, broken container,
                    # ...) must not kill a 10k-file scan: mark it and
                    # move on
                    if rec.error is None:
                        rec.error = f"skipped: {type(e).__name__}: {e}"
                    break
        return i, rec

    # ffmpeg calls are subprocess-bound, so threads parallelize well.
    # Use up to half the logical CPUs (capped at 16) so a full scan stays
    # under ~80% CPU; per-ffmpeg threads are pinned to 2 as well.
    workers = min(16, max(4, (os.cpu_count() or 4) // 2))
    done = 0
    # A worker can block INDEFINITELY inside a native read/stat (stuck
    # volume, mid-scan lock): the future it holds then never completes
    # and as_completed would wait forever. A daemon reaper abandons
    # such a file after FILE_FP_TIMEOUT (marks it skipped, resolves the
    # future so the pipeline moves on). Workers run as daemon threads
    # and the pool is shut down non-blockingly, so an abandoned worker
    # cannot wedge the scan or the process exit.
    _orig_thread = threading.Thread

    class _DaemonThread(_orig_thread):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.daemon = True

    threading.Thread = _DaemonThread
    try:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    finally:
        threading.Thread = _orig_thread
    futs = {}
    pendings = {}      # fut -> (i, submit_time)
    for i, p in enumerate(paths):
        if interrupted is not None and interrupted():
            break      # stop submitting; the rest stay Path objects
        fut = ex.submit(work, i, p)
        futs[fut] = i
        pendings[fut] = (i, time.monotonic())

    def _reaper():
        while True:
            time.sleep(5)
            now = time.monotonic()
            for fut in [f for f, (_, t0) in pendings.items()
                        if not f.done() and now - t0 > FILE_FP_TIMEOUT]:
                i, _ = pendings.pop(fut)
                rec = FileRec(paths[i])
                rec.error = "skipped: worker timed out"
                try:
                    # resolving the future releases as_completed; the
                    # wedged worker's own (late) result is discarded
                    fut.set_result((i, rec))
                except Exception:
                    pass
            if not pendings:
                return

    reap = _orig_thread(target=_reaper, daemon=True)
    reap.start()
    try:
        for fut in concurrent.futures.as_completed(futs):
            pendings.pop(fut, None)
            try:
                i, rec = fut.result()
            except Exception as e:   # last-resort guard: work() should not raise
                i = futs[fut]
                rec = FileRec(paths[i])
                rec.error = f"skipped: {type(e).__name__}: {e}"
            paths[i] = rec
            done += 1
            if done % 10 == 0 or done == total:
                progress(f"fingerprinting {done}/{total}  {rec.path.name}")
            if (interrupted is not None and interrupted()
                    and done < len(futs)):
                # window closed mid-scan: stop collecting, cancel the
                # not-yet-started work (the in-flight ones finish fast),
                # and leave the rest as Path objects
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return paths

# ---------------------------------------------------------------- grouping

class DSU:
    def __init__(self, n):
        self.p = list(range(n))
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra

class Group:
    def __init__(self, idx, members, exact, kind):
        self.idx = idx
        self.members = members          # list[FileRec] sorted old->new
        self.exact = exact
        self.kind = kind
        # on-demand OCR layout (list of int member indexes + str divider
        # labels), set when the group's 🔍 OCR button has run; survives
        # page rebuilds because it lives on the group, not the page
        self._ocr_layout = None
        # match layout (exact clusters + near similarity blocks),
        # computed in the background for big groups; None until ready
        self._match_layout = None

_pp_state = {}   # near-image data shared with pool workers (initializer)


def _pp_init(ph, ah, cols, m, mode):
    _pp_state["ph"] = ph
    _pp_state["ah"] = ah
    _pp_state["cols"] = cols
    _pp_state["m"] = m
    _pp_state["mode"] = mode
    if mode == "numpy":
        import numpy as _np
        # 256-bit hashes as 4 uint64 words: XOR + popcount become
        # vectorized C loops with small, cleanly-freed temporaries
        ph_w = _np.empty((m, 4), dtype=_np.uint64)
        ah_w = _np.empty((m, 4), dtype=_np.uint64)
        for i in range(m):
            x = ph[i]
            ph_w[i, 0] = x & 0xFFFFFFFFFFFFFFFF
            ph_w[i, 1] = (x >> 64) & 0xFFFFFFFFFFFFFFFF
            ph_w[i, 2] = (x >> 128) & 0xFFFFFFFFFFFFFFFF
            ph_w[i, 3] = (x >> 192) & 0xFFFFFFFFFFFFFFFF
            x = ah[i]
            ah_w[i, 0] = x & 0xFFFFFFFFFFFFFFFF
            ah_w[i, 1] = (x >> 64) & 0xFFFFFFFFFFFFFFFF
            ah_w[i, 2] = (x >> 128) & 0xFFFFFFFFFFFFFFFF
            ah_w[i, 3] = (x >> 192) & 0xFFFFFFFFFFFFFFFF
        _pp_state["phw"] = ph_w
        _pp_state["ahw"] = ah_w


def _popcount_u64(x):
    import numpy as _np
    try:
        return _np.bitwise_count(x)          # numpy >= 2.3
    except AttributeError:
        x = x - ((x >> 1) & 0x5555555555555555)
        x = (x & 0x3333333333333333) + ((x >> 2) & 0x3333333333333333)
        x = (x + (x >> 4)) & 0x0F0F0F0F0F0F0F0F
        return ((x * 0x0101010101010101) >> 56).astype(_np.uint64)


def _dsu_find(p, x):
    while p[x] != x:
        p[x] = p[p[x]]
        x = p[x]
    return x


def _image_near_chunk_np(i0, i1):
    """Near-image unions for rows a in [i0, i1), b > a — numpy path.
    Same predicate as the serial grouping check. Returns a union-find
    parent array over all m images (a compact ~m*4-byte result instead
    of a list of every close pair — this library has ~16% close pairs,
    so at 100k files the pair LIST would be tens of GB)."""
    import numpy as _np
    from array import array
    phw, ahw = _pp_state["phw"], _pp_state["ahw"]
    cols, m = _pp_state["cols"], _pp_state["m"]
    p = array("i", range(m))
    x = _np.empty((m - 1, 4), dtype=_np.uint64)   # reusable XOR scratch
    dph = _np.empty(m - 1, dtype=_np.uint16)
    dah = _np.empty(m - 1, dtype=_np.uint16)
    dmin = _np.empty(m - 1, dtype=_np.uint16)
    for a in range(i0, i1):
        n = m - 1 - a
        _np.bitwise_xor(phw[a + 1:], phw[a], out=x[:n])
        dph[:n] = _popcount_u64(x[:n, 0]).astype(_np.uint16)
        dph[:n] += _popcount_u64(x[:n, 1]).astype(_np.uint16)
        dph[:n] += _popcount_u64(x[:n, 2]).astype(_np.uint16)
        dph[:n] += _popcount_u64(x[:n, 3]).astype(_np.uint16)
        _np.bitwise_xor(ahw[a + 1:], ahw[a], out=x[:n])
        dah[:n] = _popcount_u64(x[:n, 0]).astype(_np.uint16)
        dah[:n] += _popcount_u64(x[:n, 1]).astype(_np.uint16)
        dah[:n] += _popcount_u64(x[:n, 2]).astype(_np.uint16)
        dah[:n] += _popcount_u64(x[:n, 3]).astype(_np.uint16)
        _np.minimum(dph[:n], dah[:n], out=dmin[:n])
        cand = _np.flatnonzero(dmin[:n] <= IMAGE_NEAR_THRESH)
        ca = cols[a]
        for bi in cand:
            b = int(bi) + a + 1
            cb = cols[b]
            if ca is not None and cb is not None \
                    and math.dist(ca, cb) > IMAGE_COLOR_TOL:
                continue
            ra = _dsu_find(p, a)
            rb = _dsu_find(p, b)
            if ra != rb:
                p[rb] = ra
    return p


_PC8 = [bin(i).count("1") for i in range(256)]


def _byte_ham(ba, bb):
    """Popcount of the byte-XOR of two 32-byte hashes, early exit > 50.
    Byte-level ops stay in the small-int cache: zero per-pair allocation,
    so a worker's memory stays flat no matter how many pairs it checks."""
    t = _PC8
    d = 0
    for i in range(32):
        d += t[ba[i] ^ bb[i]]
        if d > IMAGE_NEAR_THRESH:
            return d
    return d


def _image_near_chunk(i0, i1):
    """Near-image unions for rows a in [i0, i1), b > a — pure-Python
    fallback (no numpy): allocation-free byte popcount. Returns a
    union-find parent array, same predicate as the serial loop."""
    from array import array
    ph, ah = _pp_state["ph"], _pp_state["ah"]
    cols, m = _pp_state["cols"], _pp_state["m"]
    p = array("i", range(m))
    for a in range(i0, i1):
        pa_b, qa_b = ph[a], ah[a]
        ca = cols[a]
        for b in range(a + 1, m):
            if _byte_ham(pa_b, ah[b]) > IMAGE_NEAR_THRESH and \
               _byte_ham(qa_b, ah[b]) > IMAGE_NEAR_THRESH:
                continue
            cb = cols[b]
            if ca is not None and cb is not None \
                    and math.dist(ca, cb) > IMAGE_COLOR_TOL:
                continue
            ra = _dsu_find(p, a)
            rb = _dsu_find(p, b)
            if ra != rb:
                p[rb] = ra
    return p


def _image_near_pairs(idxs, recs, on_done=None):
    """Connectivity of the near-image predicate, as a union-find parent
    array over [0, len(idxs)). Same result as the old serial pair loop;
    for big sets the O(n^2) work is split across worker PROCESSES (pure
    Python Hamming is GIL-bound, so a thread pool cannot speed it up —
    processes can). With numpy the inner loop is vectorized; without it,
    an allocation-free byte popcount is used. Workers return compact
    parent arrays (~m*4 bytes each), NOT the pair lists — this library
    has ~16% close pairs, so at 100k files the list would be tens of GB.
    on_done(pairs_completed) fires as chunks finish."""
    m = len(idxs)
    ph = [recs[i].ph for i in idxs]
    ah = [recs[i].ah for i in idxs]
    cols = [recs[i].color for i in idxs]
    total = m * (m - 1) // 2
    mode = "serial"
    ncpu = os.cpu_count() or 1
    if m >= 4000 and ncpu >= 2:
        try:
            import numpy          # noqa: F401
            mode = "numpy"
        except ImportError:
            mode = "bytes"
    if mode == "serial":
        phb = [_b256(x) for x in ph]
        ahb = [_b256(x) for x in ah]
        _pp_init(phb, ahb, cols, m, "bytes")
        p = _image_near_chunk(0, m)
        if on_done:
            on_done(total)
        return p
    if mode == "numpy":
        _pp_init(ph, ah, cols, m, "numpy")
        chunk_fn = _image_near_chunk_np
    else:
        phb = [_b256(x) for x in ph]
        ahb = [_b256(x) for x in ah]
        _pp_init(phb, ahb, cols, m, "bytes")
        chunk_fn = _image_near_chunk
    import concurrent.futures
    from concurrent.futures.process import BrokenProcessPool
    from array import array
    workers = min(ncpu, 16)
    # Row a has m-1-a pairs, so split into chunks of ~equal PAIR counts
    # (equal row counts would leave the early rows ~m times heavier).
    nchunk = workers * 2
    edges, acc, a = [0], 0, 0
    target = total / nchunk
    while len(edges) < nchunk and a < m - 1:
        acc += m - 1 - a
        a += 1
        if acc >= target:
            edges.append(a)
            acc = 0
    edges.append(m)
    tasks = [(edges[k], edges[k + 1]) for k in range(len(edges) - 1)
             if edges[k] < edges[k + 1]]
    master = array("i", range(m))
    done = 0

    def _merge(wp, i0, i1):
        nonlocal done
        # merge this worker's union-find into the master (each
        # non-trivial parent entry is one union edge)
        for i in range(m):
            j = wp[i]
            if j != i:
                ra = _dsu_find(master, i)
                rb = _dsu_find(master, j)
                if ra != rb:
                    master[rb] = ra
        done += ((i1 - i0) * (m - 1)
                 - (i1 * (i1 - 1) - i0 * (i0 - 1)) // 2)
        if on_done:
            on_done(done)

    def _pool_has_dead_worker(ex):
        """True if any pool worker process has exited, False if all are
        alive, None when the layout can't be inspected. poll() is called
        explicitly: when the pool's manager thread is stuck (e.g. mid
        replacement-spawn on a frozen build) it never polls its workers,
        so p.exitcode alone stays None forever and a dead worker is
        invisible — that is exactly the 90%-forever hang, and it forced
        every retry to wait out the full no-progress budget instead of
        firing within seconds of the death."""
        try:
            procs = list(ex._processes.values())
        except Exception:
            return None
        for p in procs:
            try:
                if p.poll() is not None:
                    return True
            except Exception:
                pass
        return False

    initargs = (ph if mode == "numpy" else phb,
                ah if mode == "numpy" else ahb, cols, m, mode)

    def _chunk_pairs(i0, i1):
        return ((i1 - i0) * (m - 1)
                - (i1 * (i1 - 1) - i0 * (i0 - 1)) // 2)

    def _abandon(ex):
        """Tear a pool down without ever blocking the scan. The manager
        thread is marked daemon first — a manager stuck mid-spawn (its
        child never waking up) is NON-daemon by default and would hold
        up process exit. shutdown() itself only blocks on the manager's
        _shutdown_lock, which a stuck manager holds, so it runs on a
        helper thread with a small budget; a leftover helper is daemon
        and the pool's own teardown then happens via GC (the manager
        watches the executor with a weakref and exits on collection)."""
        try:
            t = getattr(ex, "_executor_manager_thread", None)
            if t is not None and t.is_alive():
                t.daemon = True
        except Exception:
            pass
        def _shut():
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        h = threading.Thread(target=_shut, daemon=True)
        h.start()
        h.join(3)

    pending = list(tasks)
    # A worker that dies mid-chunk (killed at the memory commit limit on
    # a low-RAM machine, a native fault, ...) can leave its future
    # pending FOREVER — observed on a frozen Windows build, where a scan
    # sat at 90% for hours with all workers gone and no error (the
    # pool's manager thread was stuck spawning a replacement whose child
    # never woke up, so no result ever arrived). So: watch the pool —
    # a dead worker, a broken pool, or no chunk completing within a
    # scaled budget all retry the unfinished chunks on a fresh pool (up
    # to 3 attempts), then the rest runs serially in this process. A
    # scan must never hang.
    for _attempt in range(3):
        futs = {}
        # a worker that dies mid-chunk on a low-RAM machine is usually an
        # OOM kill at the commit limit, so each retry runs with FEWER
        # workers (16 -> 8 -> 4): less peak commit, same total work.
        attempt_workers = max(2, workers // (2 ** _attempt))
        ex = concurrent.futures.ProcessPoolExecutor(
            max_workers=attempt_workers, initializer=_pp_init,
            initargs=initargs)
        try:
            for i0, i1 in pending:
                futs[ex.submit(chunk_fn, i0, i1)] = (i0, i1)
            last_progress = time.time()
            while futs:
                try:
                    fut = next(concurrent.futures.as_completed(
                        futs, timeout=5))
                except StopIteration:
                    break
                except concurrent.futures.TimeoutError:
                    # nothing completed in 5 s — a dead worker or a
                    # manager stuck in a replacement spawn means no
                    # progress will ever come; a merely slow chunk is
                    # fine until its scaled budget runs out
                    max_left = max((_chunk_pairs(i0, i1)
                                    for i0, i1 in futs.values()),
                                   default=0)
                    budget = max(180.0, 3.0 * max_left / 300_000.0 + 60.0)
                    if (time.time() - last_progress > budget
                            or _pool_has_dead_worker(ex)):
                        break
                    continue
                i0, i1 = futs[fut]
                wp = fut.result()      # may raise BrokenProcessPool —
                futs.pop(fut)          # pop only after a clean result, so
                _merge(wp, i0, i1)     # a broken chunk stays in `pending`
                last_progress = time.time()
        except BrokenProcessPool:
            pass
        _abandon(ex)
        pending = list(futs.values())
        if not pending:
            break
    if pending:
        # the pool kept losing workers (or never made progress): run the
        # remaining chunks serially here — slower, but the scan finishes.
        # The main process' _pp_state was initialized above, so the
        # chunk function runs as-is.
        for i0, i1 in pending:
            _merge(chunk_fn(i0, i1), i0, i1)
    return master


def _is_dense_blob(pw, aw, thresh, samples=6, frac=0.99, min_n=256):
    """Cheap test for whether the whole set is ONE similarity blob: `samples`
    probe points spread across the index range each need >= `frac` of the
    set within `thresh` (min of the ph/ah Hamming distances). When that
    holds, the greedy NN chain below can never find a tail with no
    in-threshold unplaced neighbour, so it produces exactly one
    undifferentiated block — and the caller name-sorts each block and takes
    a single keep (the oldest file). The chain order is therefore discarded
    either way, so the caller may skip the O(n^2) ordering entirely and get
    an identical layout for ~6 O(n) probes instead."""
    import numpy as np
    n = pw.shape[0]
    if n < min_n:
        return False
    for k in range(samples):
        s = int(k * (n - 1) / (samples - 1))
        d = _popcount_u64(pw[:, 0] ^ pw[s, 0]).astype(np.uint16) \
            + _popcount_u64(pw[:, 1] ^ pw[s, 1]).astype(np.uint16) \
            + _popcount_u64(pw[:, 2] ^ pw[s, 2]).astype(np.uint16) \
            + _popcount_u64(pw[:, 3] ^ pw[s, 3]).astype(np.uint16)
        e = _popcount_u64(aw[:, 0] ^ aw[s, 0]).astype(np.uint16) \
            + _popcount_u64(aw[:, 1] ^ aw[s, 1]).astype(np.uint16) \
            + _popcount_u64(aw[:, 2] ^ aw[s, 2]).astype(np.uint16) \
            + _popcount_u64(aw[:, 3] ^ aw[s, 3]).astype(np.uint16)
        np.minimum(d, e, out=d)
        if int((d <= thresh).sum()) < int(frac * n):
            return False
    return True


def _blob_blocks(pw, aw, thresh, min_frac=0.005, max_k=8, probe_frac=0.99):
    """Deterministic try to split the set into 1..max_k well-separated
    dense blobs (one-hop anchor neighbourhoods). Returns a list of
    boolean masks (disjoint, covering all n) or None when the structure
    is ambiguous and the greedy NN chain should run instead:
      * a neighbourhood smaller than min_frac of the set (a sparse tail
        could be one family or several);
      * a blob that is not dense (its probes see < probe_frac within
        thresh — a fuzzy chain the NN would merge differently);
      * two blobs whose anchors are within thresh (probably one family).
    For well-separated dense blobs this is the same partition the NN
    chain would produce (it stays inside a blob until exhausted, then
    jumps to the next) — so the layout (blocks, keeps, name-sorted
    cards) is identical, in O(k*n) instead of O(n^2)."""
    import numpy as np
    n = pw.shape[0]
    if n < 256:
        return None

    def fdist(s):
        d = _popcount_u64(pw[:, 0] ^ pw[s, 0]).astype(np.uint16) \
            + _popcount_u64(pw[:, 1] ^ pw[s, 1]).astype(np.uint16) \
            + _popcount_u64(pw[:, 2] ^ pw[s, 2]).astype(np.uint16) \
            + _popcount_u64(pw[:, 3] ^ pw[s, 3]).astype(np.uint16)
        e = _popcount_u64(aw[:, 0] ^ aw[s, 0]).astype(np.uint16) \
            + _popcount_u64(aw[:, 1] ^ aw[s, 1]).astype(np.uint16) \
            + _popcount_u64(aw[:, 2] ^ aw[s, 2]).astype(np.uint16) \
            + _popcount_u64(aw[:, 3] ^ aw[s, 3]).astype(np.uint16)
        np.minimum(d, e, out=d)
        return d

    unplaced = np.ones(n, dtype=bool)
    blocks = []
    anchors = []
    while unplaced.any():
        if len(blocks) == max_k:
            return None
        anchor = int(np.flatnonzero(unplaced)[0])
        blob = fdist(anchor) <= thresh
        blob &= unplaced
        if int(blob.sum()) < max(10, int(min_frac * n)):
            return None
        idx = np.flatnonzero(blob)
        dense = True
        for k in range(4):
            s = int(idx[k * (len(idx) - 1) // 3])
            if int((fdist(s) <= thresh)[blob].sum()) < int(probe_frac * len(idx)):
                dense = False
                break
        if not dense:
            return None
        blocks.append(blob)
        anchors.append(anchor)
        unplaced &= ~blob
    for i in range(len(anchors)):
        for j in range(i + 1, len(anchors)):
            if int(fdist(anchors[i])[anchors[j]]) <= thresh:
                return None
    return blocks


def _nn_order_np(ph, ah, thresh, on_done=None):
    """Greedy nearest-neighbor chain over [0, n): each step places the
    unplaced member whose min(ph,ah) Hamming distance to the current
    tail is smallest, so directly-similar files end up next to each
    other. A step beyond `thresh` starts a new similarity block; each
    block is returned name-sorted by the caller. Fast path: candidates
    are pre-filtered on the popcount of the first two words (the true
    nearest neighbour within `thresh` always survives the filter, since
    its word0+word1 distance is <= its full distance)."""
    import numpy as np
    n = len(ph)
    if n < 2:
        return list(range(n)), []
    W = 0xFFFFFFFFFFFFFFFF
    pw = np.empty((n, 4), dtype=np.uint64)
    aw = np.empty((n, 4), dtype=np.uint64)
    for i in range(n):
        x = ph[i]
        pw[i, 0] = x & W
        pw[i, 1] = (x >> 64) & W
        pw[i, 2] = (x >> 128) & W
        pw[i, 3] = (x >> 192) & W
        x = ah[i]
        aw[i, 0] = x & W
        aw[i, 1] = (x >> 64) & W
        aw[i, 2] = (x >> 128) & W
        aw[i, 3] = (x >> 192) & W

    # one dense similarity blob: the chain below would walk all n points
    # and never jump (O(n^2) work for a single undifferentiated block,
    # whose order is name-sorted away by the caller anyway) — skip it
    if _is_dense_blob(pw, aw, thresh):
        if on_done:
            on_done(1.0)
        return list(range(n)), []

    # a few well-separated dense blobs: the chain would produce exactly
    # this partition (stay inside a blob, jump to the next) — build it
    # in O(k*n) probes instead of the O(n^2) walk
    masks = _blob_blocks(pw, aw, thresh)
    if masks is not None:
        order = []
        bounds = []
        for m in masks:
            if order:
                bounds.append(len(order))
            order.extend(np.flatnonzero(m).tolist())
        if on_done:
            on_done(1.0)
        return order, bounds

    def pc4(x):
        """Sum of popcounts over the 4 uint64 words of each row."""
        d = _popcount_u64(x[:, 0]).astype(np.uint32)
        d = d + _popcount_u64(x[:, 1]).astype(np.uint32)
        d = d + _popcount_u64(x[:, 2]).astype(np.uint32)
        d = d + _popcount_u64(x[:, 3]).astype(np.uint32)
        return d

    x = np.empty((n, 4), dtype=np.uint64)
    placed = np.zeros(n, dtype=bool)
    placed[0] = True
    order = [0]
    bounds = []
    prev = 0
    for step in range(1, n):
        # phase 1 (cheap, full scan): popcount of the first two words
        # only — a neighbour within `thresh` always has
        # word0+word1 <= its full distance <= thresh, so it survives
        np.bitwise_xor(pw[prev, 0], pw[:, 0], out=x[:, 0])
        np.bitwise_xor(pw[prev, 1], pw[:, 1], out=x[:, 1])
        fw = _popcount_u64(x[:, 0]).astype(np.uint32) \
            + _popcount_u64(x[:, 1]).astype(np.uint32)
        fw[placed] = 0xFFFFFFFF
        cand = np.flatnonzero(fw <= thresh)
        if cand.size:
            # phase 2 (exact, subset): full min(ph,ah) distance on the
            # candidates only
            np.bitwise_xor(pw[prev].reshape(1, 4), pw[cand],
                           out=x[:cand.size])
            cd = pc4(x[:cand.size])
            np.bitwise_xor(aw[prev].reshape(1, 4), aw[cand],
                           out=x[:cand.size])
            cd = np.minimum(cd, pc4(x[:cand.size]))
            k = int(np.argmin(cd))
            j = int(cand[k])
            dist = int(cd[k])
        else:
            # nothing within the threshold: jump to the closest outside
            j = int(np.argmin(fw))
            dist = int(fw[j])
        placed[j] = True
        order.append(j)
        if dist > thresh:
            bounds.append(len(order) - 1)
        prev = j
        if on_done and step % 256 == 0:
            on_done(step / n)
    if on_done:
        on_done(1.0)
    return order, bounds


def _video_pair_dist(ra, rb):
    """Distance between two records without a shared perceptual hash
    (videos) using the grouping rule itself: durations must be close
    and every sampled frame pair must be within the near threshold.
    Returns the worst frame distance (0..thresh = same file) or a
    large value when the rule says "different"."""
    fa = ra.frames or []
    fb = rb.frames or []
    if not fa or not fb:
        return 9999
    if ra.dur and rb.dur:
        tol = max(VIDEO_DUR_ABS, VIDEO_DUR_TOL * max(ra.dur, rb.dur))
        if abs(ra.dur - rb.dur) > tol:
            return 9999
    worst = 0
    for (d1, a1), (d2, a2) in zip(fa, fb):
        if d1 is None or d2 is None:
            continue
        worst = max(worst, min(_hamming(d1, d2), _hamming(a1, a2)))
    return worst

def _nn_order_scalar(members, idxs, thresh, on_done=None):
    """Same nearest-neighbour chain without numpy (bytes path, early
    exit) and for mixed image/video groups. Returns (order, bounds)."""
    n = len(idxs)
    if n < 2:
        return list(idxs), []
    pbb = [_b256(members[i].ph)
           if members[i].ph is not None else None for i in idxs]
    abh = [_b256(members[i].ah)
           if members[i].ah is not None else None for i in idxs]

    def dist(i, j):
        # i/j are positions into idxs. Records with a shared perceptual
        # hash compare byte hamming (min over the hashes that exist);
        # records without one (videos) use the grouping rule itself,
        # so the chain splits a video group into the same "sets" that
        # put it in one group
        if pbb[i] is not None and pbb[j] is not None:
            d = _byte_ham(pbb[i], pbb[j])
            if abh[i] is not None and abh[j] is not None:
                d = min(d, _byte_ham(abh[i], abh[j]))
            return d
        if abh[i] is not None and abh[j] is not None:
            return _byte_ham(abh[i], abh[j])
        return _video_pair_dist(members[idxs[i]], members[idxs[j]])

    remaining = set(range(n))
    remaining.discard(0)
    order = [idxs[0]]
    bounds = []
    prev = 0
    step = 0
    while remaining:
        best, bestd = None, None
        for j in remaining:
            d = dist(prev, j)
            if bestd is None or d < bestd:
                bestd, best = d, j
                if d == 0:
                    break
        remaining.discard(best)
        order.append(idxs[best])
        if bestd > thresh:
            bounds.append(len(order) - 1)
        prev = best
        step += 1
        if on_done and step % 64 == 0:
            on_done(step / n)
    if on_done:
        on_done(1.0)
    return order, bounds


def _match_layout(members, thresh, on_done=None):
    """Display layout for one group: EXACT copies first, clustered per
    hash (the true identical files sit together), then NEAR matches in
    a nearest-neighbour chain so directly-similar files (the different
    versions of one file) sit next to each other. Returns a list of
    entries: int = member index, str = section divider label."""
    n = len(members)
    layout = []
    keeps = []    # oldest member index of every set — the defaults for
                  # "keep this one" (members are sorted old->new, so the
                  # smallest index of a set is its oldest file)
    by_hash = defaultdict(list)
    for i, r in enumerate(members):
        if r.hash:
            by_hash[(r.size, r.hash)].append(i)
    clusters = [c for c in by_hash.values() if len(c) > 1]
    if clusters:
        clusters.sort(key=lambda c: (-(len(c) - 1) * members[c[0]].size,
                                     members[c[0]].path.name.lower()))
        n_exact = sum(len(c) for c in clusters)
        layout.append(f"EXACT COPIES — {len(clusters)} set(s) · "
                      f"{n_exact} files · identical bytes")
        for c in clusters:
            keeps.append(min(c))
            layout.extend(sorted(c, key=lambda i: members[i].path.name.lower()))
    near = [i for i in range(n) if i not in set(
        x for c in clusters for x in c)]
    if near:
        if all(members[i].kind == "image" and members[i].ph is not None
               for i in near):
            order, bounds = _nn_order_np(
                [members[i].ph for i in near],
                [members[i].ah for i in near], thresh, on_done)
        else:
            order, bounds = _nn_order_scalar(members, near, thresh, on_done)
        # each similarity block is name-sorted inside, so versions of
        # one file read in order instead of in chain order
        layout.append(f"NEAR MATCHES — {len(near)} files · "
                      f"{len(bounds) + 1} similarity set(s) · "
                      f"similar files sit together")
        start = 0
        for b in bounds + [len(order)]:
            block = [near[k] for k in order[start:b]]
            keeps.append(min(block))
            layout.extend(sorted(block,
                                 key=lambda i: members[i].path.name.lower()))
            start = b
    if not keeps:
        keeps = [0]
    return layout, keeps


# ------------------------------------------------------------ OCR de-dupe
# On demand per group: extract the text of every file (tesseract for
# images / scanned PDFs, pdftotext for digital PDFs, raw XML for
# docx/odt/epub), sketch it with a 128-permutation MinHash, cluster
# files whose texts are >= OCR_SIM_THRESH similar (estimated Jaccard)
# and lay the cards out so files of the same document sit together.

_MH_PERMS = 128
_MH_P = (1 << 31) - 1          # Mersenne prime; a*h+b fits uint64 exactly
import random as _random
_mh_rng = _random.Random(20260712)
_MH_A = [_mh_rng.randrange(1, _MH_P - 1) for _ in range(_MH_PERMS)]
_MH_B = [_mh_rng.randrange(_MH_P) for _ in range(_MH_PERMS)]
_LSH_BANDS, _LSH_ROWS = 16, _MH_PERMS // 16   # S-curve centred on OCR_SIM_THRESH

def _norm_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t[:OCR_TEXT_CAP]

def _strip_tags(xml: str) -> str:
    t = re.sub(r"<[^>]+>", " ", xml)
    return _norm_text(_html.unescape(t))

def _run_bin(args, timeout=90):
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
        return r.stdout
    except Exception:
        return b""

def _ocr_image_text(p) -> str:
    """OCR one image with tesseract (stdout mode, no temp files)."""
    out = _run_bin([_resolve_bin("tesseract"), str(p), "-", "-l", "eng"], timeout=120)
    return _norm_text(out.decode("utf-8", "replace"))

def _pdf_text(p, render: bool) -> str:
    out = _run_bin([_resolve_bin("pdftotext"), "-q", "-l", "40", str(p), "-"])
    t = _norm_text(out.decode("utf-8", "replace"))
    if len(t) >= 100 or not render:
        return t
    # scanned PDF (no text layer): render a few pages, OCR them
    import tempfile
    tmp = tempfile.mkdtemp(prefix="df-ocr-")
    try:
        _run_bin([_resolve_bin("pdftoppm"), "-png", "-r", "150", "-f", "1",
                  "-l", str(OCR_PDF_RENDER_PAGES), str(p), tmp + "/pg"],
                 timeout=300)
        parts = [_ocr_image_text(pg)
                 for pg in sorted(Path(tmp).glob("pg*.png"))]
        return _norm_text(" ".join(parts))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _zip_xml_text(p) -> str:
    import zipfile
    try:
        z = zipfile.ZipFile(p)
        ext = p.suffix.lower()
        if ext == ".docx":
            names = ["word/document.xml"]
        elif ext == ".odt":
            names = ["content.xml"]
        else:  # epub: every page document
            names = [n for n in z.namelist()
                     if n.lower().endswith((".xhtml", ".html", ".htm"))]
        parts = []
        for n in names:
            try:
                parts.append(z.read(n).decode("utf-8", "replace"))
            except Exception:
                pass
        return _strip_tags(" ".join(parts))
    except Exception:
        return ""

def extract_doc_text(rec, render: bool = True) -> str:
    """Normalized text of a file (OCR for images, extraction for docs).
    render=False skips the slow scanned-PDF render+OCR path (scan-time
    use); the group's 🔍 OCR button uses render=True."""
    ext = rec.path.suffix.lower()
    try:
        if ext in DOC_EXT:
            if ext == ".pdf":
                return _pdf_text(rec.path, render=render)
            return _zip_xml_text(rec.path)
        return _ocr_image_text(rec.path)
    except Exception:
        return ""

def _shingle_hashes(text: str):
    """31-bit hashes of the 5-char shingles (exact for MinHash math)."""
    if len(text) < 5:
        return [zlib.crc32(text.encode("utf-8", "replace")) & 0x7FFFFFFF
                ] if text else []
    seen = {}
    out = []
    for i in range(len(text) - 4):
        s = text[i:i + 5]
        h = zlib.crc32(s.encode("utf-8", "replace")) & 0x7FFFFFFF
        if h not in seen:
            seen[h] = 1
            out.append(h)
    return out

def minhash_of(text: str):
    """128-perm MinHash sketch (tuple of uint32) or None for empty text."""
    hs = _shingle_hashes(text)
    if not hs:
        return None
    try:
        import numpy as np
        H = np.array(hs, dtype=np.uint64)
        A = np.array(_MH_A, dtype=np.uint64)[:, None]
        B = np.array(_MH_B, dtype=np.uint64)[None, :]
        M = (A * H[None, :] + B) % _MH_P      # (128, n) — no overflow
        return M.min(axis=1).tolist()
    except Exception:
        return [min((a * h + b) % _MH_P for h in hs)
                for a, b in zip(_MH_A, _MH_B)]

def mh_sim(m1, m2) -> float:
    return sum(x == y for x, y in zip(m1, m2)) / _MH_PERMS

def ocr_clusters(members):
    """Cluster member indexes by text similarity (MinHash + LSH).

    Returns (clusters, singles): clusters = list of lists of indexes
    (each >= 2 files, sorted largest first), singles = indexes whose
    text matched nothing (including files with no readable text).
    """
    n = len(members)
    sk = [(i, members[i].mh) for i in range(n)
          if members[i].mh is not None]
    if not sk:
        return [], list(range(n))
    by_idx = {i: mh for i, mh in sk}
    cands = defaultdict(set)
    for i, mh in sk:
        for b in range(_LSH_BANDS):
            key = tuple(mh[b * _LSH_ROWS:(b + 1) * _LSH_ROWS])
            cands[(b, key)].add(i)
    dsu = DSU(n)
    for s in cands.values():
        if len(s) < 2:
            continue
        sl = sorted(s)
        for a in range(len(sl)):
            ia = sl[a]
            for b in range(a + 1, len(sl)):
                ib = sl[b]
                if mh_sim(by_idx[ia], by_idx[ib]) >= OCR_SIM_THRESH:
                    dsu.union(ia, ib)
    comps = defaultdict(list)
    for i, mh in sk:
        comps[dsu.find(i)].append(i)
    clusters = sorted((sorted(v) for v in comps.values() if len(v) > 1),
                      key=lambda c: (-(len(c) - 1) * members[c[0]].size,
                                     members[c[0]].path.name.lower()))
    in_cluster = {i for c in clusters for i in c}
    singles = [i for i in range(n) if i not in in_cluster]
    singles.sort(key=lambda i: members[i].path.name.lower())
    return clusters, singles

def _remap_layout(lay, remap):
    """Filter a cached display layout after members were removed:
    remap the surviving member indexes and regenerate the section
    divider counts, so a group does not have to re-run its
    (multi-minute) ordering after a trash."""
    out = []
    i = 0
    n = len(lay)
    while i < n:
        e = lay[i]
        if isinstance(e, int):
            if e in remap:
                out.append(remap[e])
            i += 1
            continue
        j = i + 1
        while j < n and not isinstance(lay[j], str):
            j += 1
        section = lay[i + 1:j]
        files = sum(1 for x in section if x in remap)
        runs = 0
        prev = False
        for x in section:
            alive = x in remap
            if alive and not prev:
                runs += 1
            prev = alive
        if files:
            if e.startswith("EXACT COPIES"):
                label = (f"EXACT COPIES — {runs} set(s) · {files} files · "
                         f"identical bytes")
            elif e.startswith("NEAR MATCHES"):
                label = (f"NEAR MATCHES — {files} files · {runs} "
                         f"similarity set(s) · similar files sit together")
            elif e.startswith("OCR"):
                label = (f"OCR — {runs} set(s) · {files} files · "
                         f"same text content")
            elif e.startswith("UNIQUE TEXT"):
                label = f"UNIQUE TEXT — {files} file(s) · (unique)"
            else:
                label = e
            out.append(label)
            out.extend(remap[x] for x in section if x in remap)
        i = j
    return out

def ocr_layout(members):
    """Card layout entries from OCR clusters: divider labels (str) and
    member indexes (int); files of the same document sit together,
    name-sorted inside each set. Files without a sketch yet (not
    OCR'd, or no readable text) go to the unique section."""
    clusters, singles = ocr_clusters(members)
    layout = []
    keeps = []    # oldest of every same-document set + the unique files
    if clusters:
        m_in = sum(len(c) for c in clusters)
        layout.append(f"OCR — {len(clusters)} set(s) · {m_in} files · "
                      f"same text content")
        for c in clusters:
            keeps.append(min(c))
            layout.extend(sorted(c,
                                 key=lambda i: members[i].path.name.lower()))
    if singles:
        not_ocrd = any(m.mh is None for m in members)
        note = ("not OCR'd yet" if not_ocrd
                else "no text match in this group")
        layout.append(f"UNIQUE TEXT — {len(singles)} file(s) · {note}")
        layout.extend(singles)
        keeps.extend(singles)   # unique files are never duplicates
    if not layout:
        layout.append("no readable text")
        keeps = list(range(len(members)))
    return layout, keeps

def ocr_estimate(members) -> str:
    """Rough wall-clock estimate for OCR'ing the group (for the prompt)."""
    n_img = sum(1 for m in members if m.kind == "image")
    n_doc = len(members) - n_img
    secs = n_img * 1.5 + n_doc * 0.3
    if secs >= 3600:
        return f"~{secs / 3600:.0f} h"
    if secs >= 90:
        return f"~{secs / 60:.0f} min"
    return f"~{secs:.0f} s"


def build_groups(recs, progress=None):
    """Group records into duplicate sets.

    The near-image pair pass runs in a process pool (up to 16 workers);
    the predicate and the resulting groups are identical to the old
    serial loop. progress(done_pairs, total_pairs, detail) is called as
    chunks complete (a few dozen calls on 100k-file scans).
    """
    n = len(recs)
    dsu = DSU(n)
    # exact: same size + hash
    by_size = defaultdict(list)
    for i, r in enumerate(recs):
        if r.hash:
            by_size[(r.size, r.hash)].append(i)
    for members in by_size.values():
        if len(members) > 1:
            for m in members[1:]:
                dsu.union(members[0], m)
    # near: same kind, hash distance
    kinds = {"image": [], "video": []}
    for i, r in enumerate(recs):
        if (r.kind == "image" and r.ph is not None) or \
           (r.kind == "video" and any(f[0] is not None for f in (r.frames or []))):
            kinds[r.kind].append(i)
    grand = sum(len(v) * (len(v) - 1) // 2 for v in kinds.values())
    # ~2000 reports over the whole pass: the first real count lands
    # within a couple of seconds of the pool warming up (with 200 the
    # bar sat at 0% for the whole pool-startup + first-chunk time)
    step = max(1, grand // 2000) if grand else 1
    last_rep = 0
    # announce the pair-pass total up front: without it the UI sits in an
    # indeterminate sweep until the first chunk completes (tens of
    # seconds on 100k-file scans) and the bar appears to "go back to
    # spinning" after the file-count stages
    if progress and grand:
        progress(0, grand, "matching fingerprints")

    def rep(done_pairs, detail):
        nonlocal last_rep
        if progress and grand and done_pairs - last_rep >= step:
            last_rep = done_pairs
            progress(done_pairs, grand, detail)

    # images: O(n^2) Hamming pass — parallel across processes
    im = kinds["image"]
    m = len(im)
    img_total = m * (m - 1) // 2
    if m > 1:
        near = _image_near_pairs(im, recs,
                                 lambda d: rep(d,
                                               f"images {d * 100 // img_total}%"))
        for i in range(m):
            if near[i] != i:
                dsu.union(im[i], im[near[i]])
        rep(img_total, "images 100%")
    # videos: a few thousand files at most — kept serial (the frame
    # comparison is expensive per pair but the pair count is tiny)
    vids = kinds["video"]
    vm = len(vids)
    for a in range(vm):
        ia = vids[a]
        ra = recs[ia]
        fa = ra.frames or []
        for b in range(a + 1, vm):
            ib = vids[b]
            rb = recs[ib]
            fb = rb.frames or []
            if not fa or not fb:
                continue
            ok_dur = True
            if ra.dur and rb.dur:
                tol = max(VIDEO_DUR_ABS, VIDEO_DUR_TOL * max(ra.dur, rb.dur))
                ok_dur = abs(ra.dur - rb.dur) <= tol
            if not ok_dur:
                continue
            for (d1, a1), (d2, a2) in zip(fa, fb):
                if d1 is None or d2 is None:
                    continue
                if min(_hamming(d1, d2), _hamming(a1, a2)) > VIDEO_NEAR_THRESH:
                    break
            else:
                dsu.union(ia, ib)
        if progress and grand:
            done_pairs = img_total + (a + 1) * (vm - 1) - a * (a + 1) // 2
            rep(done_pairs, f"videos {done_pairs * 100 // grand}%")
    # documents: text MinHash near pass — copies of the same document
    # with different bytes (re-saved, re-exported) group by content;
    # the extraction already happened at fingerprint time
    docs = [i for i, r in enumerate(recs)
            if r.kind == "doc" and r.mh is not None]
    if len(docs) > 1:
        doc_recs = [recs[i] for i in docs]
        clusters, _ = ocr_clusters(doc_recs)
        for c in clusters:
            for k in c[1:]:
                dsu.union(docs[c[0]], docs[k])
        if progress:
            progress(grand, grand, "done")
    if progress:
        progress(grand, grand, "done")
    comps = defaultdict(list)
    for i in range(n):
        comps[dsu.find(i)].append(i)
    groups = []
    gi = 0
    for members_idx in comps.values():
        if len(members_idx) < 2:
            continue
        members = [recs[i] for i in members_idx]
        kinds_set = {m.kind for m in members}
        sizes_set = {m.size for m in members}
        hashes_set = {m.hash for m in members if m.hash}
        exact = len(kinds_set) == 1 and len(sizes_set) == 1 and len(hashes_set) == 1
        members.sort(key=lambda m: (m.mtime, -m.size))
        gi += 1
        groups.append(Group(gi, members, exact, next(iter(kinds_set))))
    groups.sort(key=lambda g: (-len(g.members), g.kind, g.idx))
    return groups

# ----------------------------------------------------------------- helpers

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def _human_secs(s: float) -> str:
    if s < 90:
        return f"{max(0, round(s))}s"
    if s < 5400:
        m = s / 60
        return f"{m:.1f} min" if m < 10 else f"{round(m)} min"
    return f"{s / 3600:.1f} h"

def _fmt_cnt(n: int) -> str:
    return f"{n:,}"

def default_keep(group: Group) -> FileRec:
    return group.members[0]   # sorted old->new, biggest on ties

def default_keep_recs(group: Group) -> set:
    """What is kept when the user has not chosen otherwise: the oldest
    file of EVERY duplicate set in the group (one per exact-copy cluster
    and one per similarity block), not just the single oldest file — so
    a group of many sets never looks like "everything is trash"."""
    kee = getattr(group, "_match_keep_recs", None)
    if kee:
        return set(kee)
    return {default_keep(group)}

def redundant_bytes(groups):
    tot = 0
    for g in groups:
        keeps = default_keep_recs(g)
        tot += sum(m.size for m in g.members if m not in keeps)
    return tot

def _manual_trash(paths, progress=None):
    """Move files to the FreeDesktop trash dir (respects XDG_DATA_HOME).
    Returns (failed, files_dir); failed = [(path, reason)] for files that
    existed but could not be moved. Files that vanish in the meantime
    (FUSE volumes do this mid-operation) are skipped silently — they are
    gone either way."""
    data_home = Path(os.environ.get("XDG_DATA_HOME",
                                    Path.home() / ".local" / "share"))
    base = data_home / "Trash"
    files_d, info_d = base / "files", base / "info"
    files_d.mkdir(parents=True, exist_ok=True)
    info_d.mkdir(parents=True, exist_ok=True)
    failed = []
    for k, p in enumerate(paths):
        if progress:
            progress(k, len(paths))
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.exists():
            # vanished in the meantime (FUSE volumes do this mid-
            # operation) or already moved by the gio pass — nothing
            # to do, the file is gone either way
            continue
        dest = files_d / p.name
        k2 = 1
        while dest.exists():
            dest = files_d / f"{p.stem}.{k2}{p.suffix}"
            k2 += 1
        try:
            shutil.move(str(p), str(dest))  # copy+delete across fs
            info = info_d / f"{dest.name}.trashinfo"
            info.write_text(
                "[Trash Info]\n"
                f"Path={urllib_quote(str(p))}\n"
                f"DeletionDate={_dt.datetime.now().isoformat()}\n",
                encoding="utf-8")
        except OSError as e:
            # one bad file must not abort the rest: a FUSE file can
            # vanish between exists() and move, and permissions can
            # fail per file — record it and keep going
            failed.append((p, f"{type(e).__name__}: {e}"))
    return failed, files_d

def trash_files(paths, progress=None):
    """Move files to trash. Returns (ok, method, where, failed);
    failed = [(path, reason)] for files that existed but could not be
    moved. Files that vanish in the meantime (FUSE volumes do this
    mid-operation) are skipped silently — they are gone either way.

    `gio trash` runs in batches of 500: one call with ~100k paths
    overflows the process argument limit (E2BIG) before gio even starts,
    and the old single call then reported success while nothing moved.
    A batch that fails (non-zero rc, timeout, spawn error) is re-checked
    and its survivors are moved by the manual path, so the report is
    always honest."""
    paths = [p.path if isinstance(p, FileRec) else Path(p) for p in paths]
    if IS_WIN:
        ok, m, w = _win_trash(paths)
        return ok, m, w, []
    n = len(paths)
    failed = []
    if shutil.which("gio"):
        BATCH = 500
        for b0 in range(0, n, BATCH):
            chunk = paths[b0:b0 + BATCH]
            if progress:
                progress(b0, n)
            try:
                r = subprocess.run(["gio", "trash"] + [str(p) for p in chunk],
                                   capture_output=True, timeout=600)
            except (subprocess.TimeoutExpired, OSError):
                r = None      # E2BIG / spawn failure / timeout
            if r is not None and r.returncode == 0:
                continue
            # non-zero rc or spawn failure: gio may have moved part of
            # this batch — trash whatever is still there manually
            survivors = [p for p in chunk if p.exists()]
            if survivors:
                def prog2(k, _t, b0=b0):
                    if progress:
                        progress(b0 + k, n)
                f, _d = _manual_trash(survivors, progress=prog2)
                failed.extend(f)
        if progress:
            progress(n, n)
        return (not failed), "gio trash", None, failed
    failed, files_d = _manual_trash(paths, progress=progress)
    return (not failed), "manual trash", str(files_d), failed

def urllib_quote(s):
    import urllib.parse
    return urllib.parse.quote(s, safe="/")


# ------------------------------------------------- Windows: shell & trash

_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_SILENT = 0x0004
_FOF_NOERRORUI = 0x0400

def _win_trash(paths):
    """Recoverable trash on Windows (Recycle Bin, undo-able).

    Primary: IFileOperation - SHCreateItemFromParsingName per path, then
    DeleteItem with FOF_ALLOWUNDO|FOF_NOCONFIRMATION|FOF_SILENT. Pure
    ctypes, no extra deps; handles paths with spaces. Fallback: legacy
    SHFileOperationW with the same flags. Returns (ok, method, where)
    like the POSIX path.
    """
    if _win_trash_ifo(paths):
        return True, "Recycle Bin (IFileOperation)", None
    if _win_trash_shfo(paths):
        return True, "Recycle Bin (SHFileOperationW)", None
    return False, None, None

def _win_trash_ifo(paths) -> bool:
    """IFileOperation via ctypes (vtable slots: 3 SetOperationFlags,
    11 DeleteItem, 15 PerformOperations)."""
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    class _IFOvt(ctypes.Structure):
        _fields_ = [("m%02d" % i, ctypes.c_void_p) for i in range(18)]

    shell32.SHCreateItemFromParsingName.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]

    class _GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                    ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

    def _g(s):
        h = s.strip("{}").upper().split("-")
        d4 = (ctypes.c_ubyte * 8)(*bytes.fromhex(h[3] + h[4]))
        return _GUID(int(h[0], 16), int(h[1], 16), int(h[2], 16), d4)

    CLSID_FileOperation = _g("{26BB4AC1-5C6F-4E98-BF0B-27A6A2218A08}")
    IID_IFileOperation = _g("{1BBFC5DE-0D49-403A-942A-13055FCF4985}")
    # COM must be initialized on this thread BEFORE CoCreateInstance
    # (else CO_E_NOTINITIALIZED). MTA suits worker threads;
    # RPC_E_CHANGED_MODE means the thread already has an apartment.
    _own_com = ole32.CoInitializeEx(None, 2) == 0
    pifo = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_FileOperation), None, 1,
                                ctypes.byref(IID_IFileOperation),
                                ctypes.byref(pifo))
    if hr != 0 or not pifo:
        return False
    vtbl = _IFOvt.from_address(pifo)
    _release = ctypes.cast(
        vtbl.m02, ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p))
    _set_flags = ctypes.cast(
        vtbl.m03, ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                   ctypes.c_ulong))
    _delete = ctypes.cast(
        vtbl.m11, ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_void_p))
    _perform = ctypes.cast(
        vtbl.m15, ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p))
    items = []
    try:
        if _set_flags(pifo, _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION |
                      _FOF_SILENT) != 0:
            return False
        for p in paths:
            item = ctypes.c_void_p()
            if shell32.SHCreateItemFromParsingName(
                    str(p), None, ctypes.byref(item)) != 0:
                return False
            if _delete(pifo, item, None) != 0:
                return False
            items.append(item)
        return _perform(pifo) == 0
    except Exception:
        return False
    finally:
        for it in items:
            _release(it)
        _release(pifo)
        if _own_com:
            ole32.CoUninitialize()

def _win_trash_shfo(paths) -> bool:
    """Legacy SHFileOperationW fallback. pFrom is one NULL-terminated
    path per file plus a final NULL, so paths with spaces are safe."""
    import ctypes
    from ctypes import wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ulong),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3                                   # FO_DELETE
    op.pFrom = "\0".join(str(p) for p in paths) + "\0"
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT |
                 _FOF_NOERRORUI)
    try:
        return shell32.SHFileOperationW(ctypes.byref(op)) == 0
    except Exception:
        return False

def _win_raise_to_front(retries: int = 3, delay: float = 0.5) -> bool:
    """Bring the just-launched window to the front: SetForegroundWindow
    with the AttachThreadInput foreground-lock workaround. The target is
    the topmost visible top-level window of another process - the app
    ShellExecute just started maps above our window, so it is the first
    hit in Z-order. Retries a few times for slow cold starts."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _top_foreign():
        found = []

        def _cb(hwnd, _l):
            if (user32.IsWindowVisible(hwnd) and
                    user32.GetWindowThreadProcessId(hwnd, None) !=
                    kernel32.GetCurrentProcessId()):
                found.append(hwnd)
            return True

        cb = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                wintypes.LPARAM)(_cb)
        user32.EnumWindows(cb, 0)
        return found[0] if found else None

    for _ in range(max(1, retries)):
        h = _top_foreign()
        if h and user32.GetForegroundWindow() != h:
            fg = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            my_tid = kernel32.GetCurrentThreadId()
            attached = bool(fg_tid) and bool(
                user32.AttachThreadInput(my_tid, fg_tid, True))
            try:
                user32.ShowWindow(h, 9)            # SW_RESTORE
                user32.BringWindowToTop(h)
                user32.SetForegroundWindow(h)
            finally:
                if attached:
                    user32.AttachThreadInput(my_tid, fg_tid, False)
            if user32.GetForegroundWindow() == h:
                return True
        time.sleep(delay)
    return False


def export_csv(groups, path, keep_map):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "kind", "relation", "is_keep", "size", "mtime", "path"])
        for g in groups:
            keeps = keep_map.get(g.idx) or default_keep_recs(g)
            for m in g.members:
                w.writerow([g.idx, g.kind, "exact" if g.exact else "near",
                            "yes" if m in keeps else "no", m.size,
                            _dt.datetime.fromtimestamp(m.mtime).isoformat(), m.path])

# ---------------------------------------------------------------------- GUI

STYLE_QSS = """
QWidget { color: #dde4ee; font-size: 13px; }
QLabel { background: transparent; }
QMainWindow { background: #1b1f27; }
QDialog, QMessageBox { background: #1b1f27; }
QListWidget {
    background: #14171d; border: 1px solid #2c333f; border-radius: 8px;
    padding: 4px; selection-background-color: #2b4d75;
}
QListWidget::item {
    padding: 8px; border-radius: 5px; margin: 1px 2px; color: #c6cfdc;
}
QListWidget::item:selected { background: #2b4d75; color: #ffffff; }
QListWidget::item:hover { background: #20262f; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #14171d; width: 12px; border-radius: 6px; }
QScrollBar::handle:vertical { background: #39424f; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4a5566; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #14171d; height: 12px; border-radius: 6px; }
QScrollBar::handle:horizontal { background: #39424f; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #4a5566; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QPushButton {
    background: #2a323e; border: 1px solid #414c5d; border-radius: 7px;
    padding: 8px 13px; font-weight: 600; color: #e6ebf3;
}
QPushButton:hover { background: #333e4d; border-color: #57677f; }
QPushButton:pressed { background: #222831; }
QPushButton:disabled { color: #6b7482; }
QComboBox {
    background: #2a323e; border: 1px solid #414c5d; border-radius: 7px;
    padding: 7px 10px;
}
QComboBox:hover { border-color: #57677f; }
QComboBox QAbstractItemView {
    background: #232a35; border: 1px solid #414c5d;
    selection-background-color: #2b4d75;
}
QRadioButton, QCheckBox { spacing: 7px; color: #c6cfdc; }
QStatusBar { background: #14171d; color: #8a93a2; }
QStatusBar::item { border: none; }
QToolTip {
    background: #2a323e; color: #e6ebf3; border: 1px solid #414c5d;
    padding: 5px 8px;
}
QWidget#bottombar {
    background: #14171d; border: 1px solid #2c333f; border-radius: 8px;
}
QTabWidget::pane { border: none; background: transparent; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: #232a35; color: #8a93a2; padding: 6px 16px;
    border: 1px solid #2c333f; border-bottom: none;
    border-top-left-radius: 7px; border-top-right-radius: 7px;
}
QTabBar::tab:selected {
    background: #14171d; color: #dde4ee;
    border-bottom: 2px solid #4a90d9;
}
QTabBar::tab:hover { color: #c6cfdc; }
"""

CARD_KEEP = ("QFrame{background:#1f2e28;border:2px solid #3fae7c;"
             "border-radius:10px;}")
CARD_DUP = ("QFrame{background:#2a252b;border:1px solid #6e4650;"
            "border-radius:10px;}")
CARD_NEUTRAL = ("QFrame{background:#252b35;border:1px solid #39414f;"
                "border-radius:10px;}")

def _active_window():
    """(caption, resource) of the currently active window, or ('', '')."""
    try:
        r = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.kde.KWin", "--object-path", "/KWin",
             "--method", "org.kde.KWin.queryWindowInfo"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            cap = re.search(r"'caption': <'([^']*)'>", r.stdout)
            res = re.search(r"'resourceName': <'([^']*)'>", r.stdout)
            return (cap.group(1) if cap else "",
                    res.group(1) if res else "")
    except Exception:
        pass
    return ("", "")

_FM_RESOURCES = {"nautilus", "dolphin", "nemo", "thunar", "caja",
                 "pcmanfm", "spacefm", "krusader"}

_open_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="open-folder")

# QThreads kept alive after the window closed mid-scan: the C++ QThread
# must not be destroyed while it still runs (Qt qFatal aborts the app),
# and the interpreter must not tear down while it executes Python.
_live_workers = set()

# Scan results handed to the main thread by TOKEN: the "done" signal used
# to carry (groups, recs, secs) directly, and Qt's queued-signal delivery
# deep-copies arguments across threads — a 100k-file results tree (~2 GB
# with thumbnails) copied on the main thread froze the window for minutes.
# The worker stores the result here and the signal carries only an int.
_scan_results = {}
_scan_next_token = 0

STALE_NOTIFY = None   # set by Main(): status-bar message for stale paths

def open_folder(path: Path):
    """Open a folder in the user's file manager, brought to the front.

    Runs on a worker thread so the GUI never freezes while the D-Bus calls
    and the focus check take a few seconds. The GUI may run inside a
    sandbox, where a plain xdg-open resolves the default app to a bare
    desktop-file id and fails with "'org.kde.dolphin' was not found". So
    open it from the user's desktop session over D-Bus instead:
      1. org.freedesktop.FileManager1.ShowFolders — the XDG file-manager
         service; the session bus activates it (Dolphin/Nautilus on KDE)
         and opens the folder, or routes it to an already-running instance.
         The active window is then polled via KWin; if the manager still
         has not taken focus, the call is repeated once (a second call
         reliably activates the window).
      2. xdg-desktop-portal OpenURI — second opinion.
      3. xdg-open — last resort (works when run outside a sandbox).
    Folders moved after the scan are reported as stale, not opened.
    """
    p = Path(path)
    if not p.exists():
        if STALE_NOTIFY:
            STALE_NOTIFY("folder no longer exists — rescan to refresh")
        return
    _open_pool.submit(_open_folder_worker, p)

def open_file(path: Path):
    """Open a single file in its default app (image viewer / video player)
    so the user can inspect it. Launches the default app directly
    (xdg-mime -> desktop-file Exec) and brings the window to the front
    with the 'dupe-raise' KWin script. Files moved after the scan are
    reported as stale instead of being handed to the viewer."""
    p = Path(path)
    if not p.exists():
        if STALE_NOTIFY:
            STALE_NOTIFY(f"{p.name} no longer exists — rescan to refresh")
        return
    _open_pool.submit(_open_file_worker, p)

_MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tiff": "image/tiff", ".tif": "image/tiff", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".mov": "video/quicktime", ".m4v": "video/x-m4v",
    ".ogv": "video/ogg", ".wmv": "video/x-ms-wmv",
}

_APP_DIRS = (Path.home() / ".local/share/applications",
             Path("/usr/local/share/applications"),
             Path("/usr/share/applications"))

def _default_app_argv(path: Path):
    """(app_id, argv) for this file's default app, or (None, None).
    Resolves the MIME type by extension, the default app via xdg-mime,
    and expands the desktop file's Exec line: %u/%U -> URI, %f/%F ->
    path, other %tokens dropped."""
    mime = _MIME_BY_EXT.get(path.suffix.lower())
    if not mime:
        return None, None
    try:
        out = subprocess.run(["xdg-mime", "query", "default", mime],
                             capture_output=True, text=True, timeout=5)
        app_id = out.stdout.strip()
    except Exception:
        return None, None
    if not app_id:
        return None, None
    for d in _APP_DIRS:
        f = d / app_id
        if not f.is_file():
            continue
        try:
            txt = f.read_text()
        except OSError:
            continue
        m = re.search(r"^Exec=(.+)$", txt, re.M)
        if not m:
            continue
        argv = []
        for tok in shlex.split(m.group(1)):
            if tok in ("%u", "%U"):
                argv.append(path.as_uri())
            elif tok in ("%f", "%F"):
                argv.append(str(path))
            elif len(tok) == 2 and tok.startswith("%"):
                continue                    # %i %c %k %n %s %v %m ...
            else:
                argv.append(tok)
        return app_id, argv
    return app_id, None

_KWIN_RAISE_SCRIPTS = (
    Path.home() / ".local/share/kwin/scripts/dupe-raise/main.js",
    Path("/usr/share/kwin/scripts/dupe-raise/main.js"),   # AUR install
)

def _raise_script_path():
    for p in _KWIN_RAISE_SCRIPTS:
        if p.is_file():
            return p
    return _KWIN_RAISE_SCRIPTS[0]

def _kwin_call(method: str, path: str, args: tuple = ()):
    iface = ("org.kde.kwin.Scripting" if path == "/Scripting"
             else "org.kde.kwin.Script")
    return subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.kde.KWin",
         "--object-path", path, "--method", f"{iface}.{method}", *args],
        capture_output=True, text=True, timeout=6)

def _raise_top_window():
    """Bring the just-opened viewer window to the front via the
    'dupe-raise' KWin script (~/.local/share/kwin/scripts/dupe-raise,
    installed next to this app). The script waits for the new
    window's windowAdded signal (up to 10 s) and raises it, so even a
    slow cold start ends up in front. Deliberately never
    queryWindowInfo — that is KWin's interactive crosshair picker.
    The script is reloaded on every call because it does not survive
    a KWin restart."""
    try:
        _kwin_call("unloadScript", "/Scripting", ("dupe-raise",))
        r = _kwin_call("loadScript", "/Scripting",
                       (str(_raise_script_path()), "dupe-raise"))
        m = re.search(r"\((-?\d+),\)", r.stdout or "")
        if r.returncode != 0 or not m:
            return
        sid = int(m.group(1))
        if sid < 0:                        # already loaded: find node
            ri = subprocess.run(
                ["gdbus", "introspect", "--session",
                 "--dest", "org.kde.KWin", "--object-path", "/Scripting"],
                capture_output=True, text=True, timeout=6)
            m2 = re.search(r"node (Script\d+)", ri.stdout or "")
            if not m2:
                return
            _kwin_call("run", f"/Scripting/{m2.group(1)}")
            return
        _kwin_call("run", f"/Scripting/Script{sid}")
    except Exception:
        pass

def _open_file_worker(path: Path):
    """Open the file in its default app (image viewer / video player)
    and bring that window to the front. The portal OpenURI turned out
    unreliable on this box, so the default app is launched directly
    (xdg-mime -> desktop-file Exec line) with the same seeded
    environment the Dolphin launch uses. The viewers are NOT
    single-instance — a second launch opens a second window — so
    exactly one launch is made and the 'dupe-raise' KWin script then
    raises the window when it maps."""
    p = Path(path)
    if not p.exists():
        return
    if IS_WIN:
        try:
            os.startfile(str(p))     # ShellExecuteW -> default app
        except OSError:
            return
        time.sleep(1.5)              # let the viewer map its window
        _win_raise_to_front()
        return
    app_id, argv = _default_app_argv(p)
    if argv:
        env = _dolphin_launch_env() if SANDBOXED else dict(os.environ)
        try:
            subprocess.Popen(argv, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            _raise_top_window()
        except Exception:
            pass
        return
    subprocess.Popen(["xdg-open", str(path)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)

SANDBOXED = False   # set in main(): True when ~/.config is not writable

_dolphin_state = {"env": None, "last": (None, 0.0)}

def _dolphin_launch_env():
    """Environment for launching Dolphin from this (possibly sandboxed)
    process. The KIO worker creates its socket in XDG_RUNTIME_DIR and
    KConfig writes XDG_CONFIG_HOME — both are read-only inside a
    sandbox, where Dolphin then dies with "Unable to create KIO worker".
    Give it a writable temp runtime dir plus a fresh copy of the user's
    kdeglobals / dolphinrc (dark theme, window settings). A name-based
    WAYLAND_DISPLAY must be resolved against the user's real
    /run/user/<uid>, since the runtime dir was swapped.
    """
    if _dolphin_state["env"] is None:
        import tempfile
        base = tempfile.mkdtemp(prefix="dupe-dolphin-")
        rt, cfg = base + "/rt", base + "/cfg"
        os.makedirs(rt, mode=0o700, exist_ok=True)
        os.makedirs(cfg, exist_ok=True)
        for f in ("kdeglobals", "dolphinrc"):
            src = Path.home() / ".config" / f
            if src.is_file():
                try:
                    shutil.copyfile(src, Path(cfg) / f)
                except OSError:
                    pass
        env = dict(os.environ)
        env.update({
            "XDG_RUNTIME_DIR": rt,
            "XDG_CONFIG_HOME": cfg,
            "XDG_CACHE_HOME": base + "/cache",
            "XDG_DATA_HOME": base + "/data",
        })
        wd = env.get("WAYLAND_DISPLAY", "")
        if wd and not wd.startswith("/"):
            sock = Path(f"/run/user/{os.getuid()}") / wd
            if sock.exists():
                env["WAYLAND_DISPLAY"] = str(sock)
        _dolphin_state["env"] = env
    return _dolphin_state["env"]

def _launch_dolphin(path: Path):
    now = time.monotonic()
    last_path, last_t = _dolphin_state["last"]
    if last_path == str(path) and now - last_t < 2.0:
        return                       # a launch is already in flight
    _dolphin_state["last"] = (str(path), now)
    subprocess.Popen(
        ["dolphin", str(path)], env=_dolphin_launch_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)

def _dolphin_instance_name():
    """Bus name of a running Dolphin instance (org.kde.dolphin-<id>).
    If several exist, prefer one outside this (possibly sandboxed)
    PID namespace — the user's own instance, not one of ours."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        names = [n for n in bus.list_names_sync()
                 if re.match(r"^org\.kde\.dolphin-\d+$", n)]
    except Exception:
        return None
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    try:
        mine = set(os.listdir("/proc"))
        for n in names:
            r = bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "GetConnectionUnixProcessName",
                GLib.Variant("(s)", (n,)), None,
                Gio.DBusCallFlags.NONE, 3000, None)
            if str(r.unpack()[0]) not in mine:
                return n
    except Exception:
        pass
    return names[0]

def _dolphin_open_running(uri: str) -> bool:
    """Ask a running Dolphin instance to open the folder via the
    standard org.freedesktop.Application.Open D-Bus method — no process
    spawn, no cold start, so no startup busy cursor. Returns False if
    no instance is running."""
    name = _dolphin_instance_name()
    if not name:
        return False
    try:
        r = subprocess.run(
            ["gdbus", "call", "--session", "--dest", name,
             "--object-path", "/org/kde/dolphin",
             "--method", "org.freedesktop.Application.Open",
             f"['{uri}']", "{}"],
            capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False

def _open_folder_worker(path: Path):
    """Open the folder in the system file manager and bring it to the
    front. Prefers the XDG FileManager1 service (ShowFolders) — the
    manager runs in the user session. Falls back to Dolphin (D-Bus
    Open on a running instance, else a launch), then the portal, then
    xdg-open.

    Deliberately NO KWin window queries in this path: on KWin 6,
    org.kde.KWin.queryWindowInfo is the *interactive* window picker —
    it turns the cursor into a crosshair and blocks until the user
    clicks a window. (See CURSOR-DEBUG.md.)

    The manager does not raise its own window reliably, so the open
    command is sent twice: the first call opens the folder (and maps
    the window, possibly behind), the second — after a short pause —
    re-activates it, which reliably brings it to the front.
    """
    if not Path(path).exists():
        return
    if IS_WIN:
        try:
            os.startfile(str(path))  # Explorer at this folder
        except OSError:
            return
        time.sleep(1.8)
        _win_raise_to_front()
        return
    uri = Path(path).as_uri()
    fm_call = ["gdbus", "call", "--session",
               "--dest", "org.freedesktop.FileManager1",
               "--object-path", "/org/freedesktop/FileManager1",
               "--method", "org.freedesktop.FileManager1.ShowFolders",
               f"['{uri}']", ""]
    try:
        r = subprocess.run(fm_call, capture_output=True, timeout=20)
        if r.returncode == 0:
            time.sleep(1.8)   # give the window time to map
            subprocess.run(fm_call, capture_output=True, timeout=10)
            return
    except Exception:
        pass
    if shutil.which("dolphin"):
        try:
            if not _dolphin_open_running(uri):
                _launch_dolphin(path)
            time.sleep(1.8)
            if not _dolphin_open_running(uri):
                _launch_dolphin(path)
            return
        except Exception:
            pass
    try:
        r = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.OpenURI.OpenURI",
             "", uri, "@a{sv} {}"],
            capture_output=True, timeout=8)
        if r.returncode == 0:
            return
    except Exception:
        pass
    subprocess.Popen(["xdg-open", str(path)])

def _apply_kde_platform_theme():
    """Select the 'plasma' Qt platform theme explicitly on KDE/Plasma
    desktops — must run BEFORE any QApplication is created.

    The frozen bundle ships the Plasma platform theme plugin (the one
    that provides the native KFileDialog with the location bar), but
    Qt's automatic theme detection does not find it in the PyInstaller
    context — the folder picker silently fell back to the built-in Qt
    dialog, whose file list could render empty. On non-KDE desktops
    Qt's own choice (GTK / xdg-desktop-portal) is left alone."""
    if os.name != "posix":
        return
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if ("KDE" in desktop or "Plasma" in desktop) and \
            not os.environ.get("QT_QPA_PLATFORMTHEME"):
        os.environ["QT_QPA_PLATFORMTHEME"] = "plasma"

def _mounted_drive_dirs():
    """User-visible mounts under /run/media, /media and /mnt (USB sticks,
    Veracrypt volumes, ...). Shown in the built-in folder picker's
    sidebar, which otherwise only lists 'Computer' and the home dir —
    the native KDE picker's drive list is not available there. Only
    mounts that actually exist are listed, so a drive appears here as
    soon as it is mounted and disappears when unmounted."""
    out = []
    try:
        mounted = set()
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mounted.add(parts[1])
        for base in ("/run/media", "/media", "/mnt"):
            if not os.path.isdir(base):
                continue
            for entry in sorted(os.listdir(base)):
                p = os.path.join(base, entry)
                if os.path.isdir(p) and p in mounted:
                    out.append(p)
    except Exception:
        pass
    return out

def _home_place_dirs():
    """Standard home sub-locations that exist on disk (Dolphin's
    'Places' equivalents for the built-in picker sidebar)."""
    out = []
    try:
        home = os.path.expanduser("~")
        for name in ("Desktop", "Documents", "Downloads", "Music",
                     "Pictures", "Videos", "Public"):
            p = os.path.join(home, name)
            if os.path.isdir(p):
                out.append(p)
    except Exception:
        pass
    return out

def pick_directory(parent, title, start_dir):
    """Folder picker. On Linux the Qt built-in dialog is used (not the
    native KDE one): the native picker runs KIO in a worker process, and
    KF6 6.31-era builds fail to create its socket ("Unable to create KIO
    worker ... protocol 'tags'"), leaving an empty file list in frozen
    apps. The built-in picker lists mounted drives as sidebar bookmarks,
    and a full path can be typed into the 'Directory' field. On Windows
    the native picker is used. Returns a resolved Path, or None if the
    user cancels."""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QFileDialog
    app = QApplication.instance()
    if app is not None:
        # Dark palette BEFORE the dialog is shown (run_gui applies it
        # later, but the picker opens first).
        try:
            if not app.property("_df_dark_applied"):
                _apply_dark_theme(app)
                app.setProperty("_df_dark_applied", True)
        except Exception:
            pass
        # Linux: deterministic built-in picker (see docstring).
        app.setAttribute(Qt.AA_DontUseNativeDialogs, os.name == "posix")
    dlg = QFileDialog(parent, title, str(start_dir))
    dlg.setFileMode(QFileDialog.FileMode.Directory)  # Qt6 name of DirectoryOnly
    opts = [QFileDialog.Option.ShowDirsOnly]
    if SANDBOXED or os.name == "posix":
        opts.append(QFileDialog.Option.DontUseNativeDialog)
    for opt in opts:
        try:
            dlg.setOption(opt, True)
        except TypeError:
            pass
    if os.name == "posix":
        # Restore the drive list the native KDE picker showed in its
        # sidebar: setSidebarUrls() (Qt 6.12) replaces the sidebar's
        # default entries ('Computer' + home), so include them first.
        try:
            from PySide6.QtCore import QDir, QUrl
            urls = [QUrl("file:"), QUrl.fromLocalFile(QDir.homePath())]
            urls += [QUrl.fromLocalFile(d) for d in _home_place_dirs()]
            urls += [QUrl.fromLocalFile(d) for d in _mounted_drive_dirs()]
            dlg.setSidebarUrls(urls)
        except Exception:
            pass
    # The built-in dialog (used when no platform theme provides a native
    # picker): its internal QFileSystemModel loads the initial directory
    # ASYNCHRONOUSLY, and if it is not ready when the dialog sets the
    # view's root index, the list falls back to the filesystem top level
    # (the "Computer" view) and STAYS there — the user sees an empty
    # list in every directory (Qt 6.12). The view's root index cannot be
    # fixed before the model exists (the dialog creates it in
    # showEvent), so instead we watch the model and re-root the view as
    # soon as the current directory is loaded. Harmless when a platform
    # (native) dialog is in use: no QFileSystemModel is found.
    _hooked = []
    _dbg = os.environ.get("DF_PICKER_DEBUG") == "1"
    def _resync_picker_view(*_args):
        if _dbg:
            print("RESYNC-CALL", flush=True)
        try:
            from PySide6.QtWidgets import QAbstractItemView, QFileSystemModel
            cur = str(dlg.directory().path() or str(start_dir))
            for v in dlg.findChildren(QAbstractItemView):
                m = v.model()
                if not isinstance(m, QFileSystemModel):
                    continue
                idx = m.index(cur)  # str overload (QUrl is not wrapped)
                if _dbg:
                    print("RESYNC-VIEW cur=%s valid=%s rootPath=%s"
                          % (cur, idx.isValid(), m.rootPath()), flush=True)
                if idx.isValid():
                    v.setRootIndex(idx)
                if m not in _hooked:
                    _hooked.append(m)
                    m.directoryLoaded.connect(_resync_picker_view)
        except Exception:
            pass
    for _delay in (30, 100, 250, 500, 1000, 2000, 3000, 5000):
        QTimer.singleShot(_delay, _resync_picker_view)
    if dlg.exec() != QFileDialog.DialogCode.Accepted:
        return None
    sel = dlg.selectedFiles()
    if not sel:
        return None
    return Path(sel[0]).expanduser().resolve()

def run_gui(root: Path, include_hidden=False, all_dirs=False):
    from PySide6.QtCore import Qt, QThread, Signal, QTimer
    from PySide6.QtGui import (QPixmap, QAction, QShortcut, QKeySequence,
                               QColor)
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QListWidget, QListWidgetItem, QScrollArea, QFrame, QLabel,
        QRadioButton, QCheckBox, QPushButton, QComboBox, QMessageBox,
        QStatusBar, QSizePolicy, QFileDialog, QMenu, QGridLayout,
        QProgressBar, QTabBar, QLineEdit, QProxyStyle, QStyle, QDialog)

    class ArrangeWaitDialog(QDialog):
        """Small centered 'please wait' window shown while a big
        group is being arranged in the background. The user can
        dismiss it with OK (the arrangement keeps running and the
        cards re-sort themselves when it finishes); the window also
        closes itself the moment the arrangement completes. Instant
        arrangements (small groups, cached layouts, documents) never
        show it — no window when there is nothing to wait for."""
        def __init__(self, page, mainwin, count=0):
            super().__init__(page)
            self._page = page
            title = (f"Arranging {count:,} cards…" if count
                     else "Arranging cards…")
            self.setWindowTitle(title)
            # self-contained dark look: this window is shown mid-build
            # while the main thread is busy painting the card grid, and
            # on Windows the first visible frame can land before the
            # app-wide stylesheet/palette reach it — a white, empty
            # "flash bang". An explicit per-widget stylesheet is in
            # place from construction, so the very first paint is
            # already dark with content.
            self.setStyleSheet(
                "QDialog { background: #1b1f27; }"
                "QLabel { color: #dde4ee; background: transparent; }"
                "QProgressBar { background: #14171d;"
                " border: 1px solid #2c333f; border-radius: 5px;"
                " min-height: 14px; text-align: center; color: #dde4ee; }"
                "QProgressBar::chunk { background: #4a90d9;"
                " border-radius: 4px; }"
                "QPushButton { background: #2a323e;"
                " border: 1px solid #414c5d; border-radius: 7px;"
                " padding: 6px 14px; font-weight: 600; color: #e6ebf3; }"
                "QPushButton:hover { background: #333e4d; }")
            self.setModal(False)
            # Qt (not the Python GC) deletes the C++ dialog when it
            # closes: a Python-owned wrapper collected later would
            # queue a second C++ deletion and could hit a half-painted
            # button (segfault in the binding layer)
            self.setAttribute(Qt.WA_DeleteOnClose)
            # every widget created here is Python-owned by shiboken at
            # construction (no C++ parent yet): the wrapper must stay
            # referenced, or the GC queues a C++ deletion of a button
            # that is still on screen (segfault in the paint path).
            # The C++ tree alone does NOT keep the wrappers alive.
            lay = QVBoxLayout(self)
            lay.setContentsMargins(16, 14, 16, 12)
            lay.setSpacing(9)
            self._head = QLabel(title)
            self._head.setStyleSheet("font-weight:bold;font-size:15px;")
            lay.addWidget(self._head)
            self.info = QLabel(
                "The duplicate sort is NOT ready until the "
                "arrangement is complete.\n\n"
                "You can keep working — this window closes "
                "itself when the arrangement is done.")
            self.info.setWordWrap(True)
            lay.addWidget(self.info)
            self.bar = QProgressBar()
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
            lay.addWidget(self.bar)
            self._btnrow = QHBoxLayout()
            self._btnrow.addStretch(1)
            self._ok_btn = QPushButton("OK")
            self._ok_btn.setToolTip("Close this window. The arrangement "
                                    "keeps running in the background and "
                                    "the cards re-sort themselves when "
                                    "it is done.")
            self._ok_btn.clicked.connect(self.close)
            self._btnrow.addWidget(self._ok_btn)
            lay.addLayout(self._btnrow)
            self._lay = lay
            self.setFixedWidth(380)
            self.adjustSize()
            if mainwin is not None:
                try:
                    geo = mainwin.frameGeometry()
                    self.move(geo.center().x() - self.width() // 2,
                              geo.center().y() - self.height() // 2)
                except Exception:
                    pass

        def set_progress(self, pct):
            try:
                v = int(pct)
            except (TypeError, ValueError):
                v = 0
            self.bar.setValue(max(0, min(100, v)))

        def closeEvent(self, ev):
            if getattr(self, "_page", None) is not None:
                try:
                    self._page._wait_dlg = None
                except Exception:
                    pass
            super().closeEvent(ev)

    class Card(QFrame):
        def __init__(self, parent, group, rec, is_keep, root=None):
            super().__init__(parent)
            self.rec = rec
            self.group = group
            self.root = root
            self.group_widget = None   # set by GroupPage after __init__
            self.setFixedWidth(236)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(10, 10, 10, 10)
            lay.setSpacing(5)
            thumb = QLabel()
            thumb.setFixedSize(220, 160)
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setStyleSheet(
                "background:#171b22;border:1px solid #2c333f;border-radius:6px;")
            if rec.thumb:
                pm = QPixmap()
                ok = pm.loadFromData(rec.thumb)   # auto-detect (JPEG thumbs)
                if ok:
                    thumb.setPixmap(pm.scaled(216, 156,
                                              Qt.KeepAspectRatio,
                                              Qt.SmoothTransformation))
                else:
                    thumb.setText("(no preview)")
            elif rec.kind == "image" and rec.error:
                thumb.setText(f"⚠ {rec.error}\n(no preview)")
            elif rec.kind == "image":
                thumb.setText("HEIC — no decoder")
            else:
                thumb.setText("⚠ no preview")
            if rec.kind == "video" and rec.dur:
                badge = QLabel(f"🎬 video · {rec.dur:.1f}s")
                badge.setStyleSheet("color:#7ec8ff;")
                lay.addWidget(badge)
            lay.addWidget(thumb)
            # filename is a link: opens the file in its default app so the
            # user can inspect it (e.g. compare two "identical" images)
            name = QLabel(f'<a href="{_html.escape(str(rec.path), quote=True)}">'
                          f'{_html.escape(rec.path.name)}</a>')
            name.setWordWrap(True)
            name.setStyleSheet(
                "color:#c9d6e8;font-weight:600;background:transparent;")
            name.setToolTip(f"{rec.path}  (click to open in default app)")
            name.linkActivated.connect(
                lambda _h, p=rec.path: open_file(p))
            lay.addWidget(name)
            meta = QLabel(f"{human(rec.size)}\n{_dt.datetime.fromtimestamp(rec.mtime):%Y-%m-%d %H:%M}"
                          + (f"\n{rec.dims[0]}×{rec.dims[1]}" if rec.dims else ""))
            meta.setStyleSheet("color:#9aa4b2;")
            lay.addWidget(meta)
            # clickable location: relative to the scanned root when possible
            parent_dir = rec.path.parent
            try:
                shown = parent_dir.relative_to(root)
                shown_s = str(shown) if str(shown) != "." else str(parent_dir)
            except (ValueError, TypeError):
                shown_s = str(parent_dir)
            if len(shown_s) > 42:
                shown_s = "…" + shown_s[-41:]
            loc_html = (
                f'<a href="{_html.escape(str(parent_dir), quote=True)}">'
                f"{_html.escape(shown_s)}</a>")
            loc = QLabel(loc_html)
            loc.setStyleSheet(
                "color:#7ea6d9;font-size:11px;background:transparent;")
            loc.setToolTip(f"{parent_dir}  (click to open)")

            def _clicked(_h, l=loc, d=parent_dir, oh=loc_html):
                # immediate feedback while the manager window comes up
                l.setText('<span style="color:#9fb6d9">'
                          "opening in file manager…</span>")

                def _restore():
                    if "opening in file manager" in l.text():
                        l.setText(oh)
                QTimer.singleShot(6000, _restore)
                open_folder(d)
            loc.linkActivated.connect(_clicked)
            lay.addWidget(loc)
            if rec.error:
                err = QLabel(f"⚠ {rec.error}")
                err.setStyleSheet("color:#ff9d6b;")
                lay.addWidget(err)
            # "keep this one" is a per-file toggle, not an exclusive
            # radio: every duplicate set has its own kept original, so a
            # group legitimately shows several green cards
            self.keep_radio = QRadioButton("keep this one")
            self.keep_radio.setAutoExclusive(False)
            self.keep_radio.setToolTip(
                "Mark this file as the original of its set — it survives "
                "the trash buttons. By default the oldest file of each "
                "duplicate set is marked; un-marked files are the copies.")
            if is_keep:
                self.keep_radio.setChecked(True)
            self.dup_check = QCheckBox("duplicate → trash")
            self.dup_check.setToolTip(
                "Mark this file as a duplicate: the trash buttons will "
                "move it to your system trash.\n\n"
                "Uncheck it (or tick \u201ckeep this one\u201d) to "
                "protect this file from being trashed.")
            self.dup_check.setChecked(not is_keep)
            self.keep_radio.toggled.connect(self._toggled)
            lay.addWidget(self.keep_radio)
            lay.addWidget(self.dup_check)
            self.setContextMenuPolicy(Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._menu)
            self._apply_state()

        def _apply_state(self):
            # keep-all locks the whole group green — the boxes are
            # disabled too, so nothing can be re-marked by accident
            if getattr(self.group_widget, "_keep_all_on", False):
                self.setStyleSheet(CARD_KEEP)
            elif self.keep_radio.isChecked():
                self.setStyleSheet(CARD_KEEP)
            elif self.dup_check.isChecked():
                self.setStyleSheet(CARD_DUP)
            else:
                self.setStyleSheet(CARD_NEUTRAL)

        def _toggled(self, on):
            # marking a file as original clears only ITS OWN trash box —
            # the other sets' originals stay green
            if on:
                self.dup_check.blockSignals(True)
                self.dup_check.setChecked(False)
                self.dup_check.blockSignals(False)
            self._apply_state()
            page = self.group_widget
            if page is not None and hasattr(page, "_keep_toggled"):
                page._keep_toggled(getattr(self, "_member_idx", 0), on)

        def _menu(self, pos):
            m = self._build_menu()
            m.exec(self.mapToGlobal(pos))

        def _build_menu(self):
            menu = QMenu(self)
            a1 = QAction("Open containing folder", menu)
            a1.triggered.connect(lambda: open_folder(self.rec.path.parent))
            a2 = QAction("Copy path", menu)
            a2.triggered.connect(lambda: QApplication.clipboard().setText(str(self.rec.path)))
            menu.addAction(a1)
            menu.addAction(a2)
            return menu

    class GroupPage(QWidget):
        """Side-by-side cards for one group.

        Cards are built in batches (CARD_BATCH at a time, first batch
        up front, the rest behind a "show more" button): a group can hold
        tens of thousands of files when the near threshold chains them
        into one component, and building that many Card widgets at once
        froze the window and ate 26 GiB of RAM on a real 100k library.
        """
        CARD_BATCH = 60
        _ocr_prog = Signal(int)          # 0..99, OCR progress
        _ocr_done_sig = Signal(object)   # finished OCR result dict

        def __init__(self, parent, group, keeps, root=None, keep_all=False,
                     on_keep_all=None, on_trash=None, on_keep=None):
            super().__init__(parent)
            self.group = group
            self.root = root
            # keeps = the set of member FileRecs marked "keep this one"
            # (default: oldest file of every duplicate set)
            self._keep_set = set()
            self._keep_all_on = keep_all
            self.cards = []
            self.on_keep_all = on_keep_all
            self.on_trash = on_trash
            self.on_keep = on_keep
            self._shown = 0
            self.more_btn = None
            # display layout: a list of entries — int = member index,
            # str = section divider. "match" mode = exact copies first
            # (clustered per hash) then near matches in a
            # nearest-neighbour chain (versions of one file adjacent);
            # name/size modes are plain index lists.
            self._layout = []
            self._layout_ready = True
            self._match_members = None
            self._dup_state = {}          # member idx -> dup box checked
            self._keep_state = {}         # member idx -> keep box checked
            self._seed_keep = {}          # the default at first build:
            self._seed_dup = {}           # re-marking only touches cards
                                           # the user has not changed
            if keeps:
                for i, m in enumerate(group.members):
                    if m in keeps:
                        self._keep_set.add(i)
            if not self._keep_set:
                self._keep_set = {0}      # oldest of the group
            self._next_row = 0
            outer = QVBoxLayout(self)
            outer.setContentsMargins(6, 6, 6, 6)
            self._outer = outer   # _add_cards() (called below) adds the
                                  # "show more" button to this layout
            if getattr(group, "_ocr_parent", None) is not None:
                rel = "OCR duplicates (same text content)"
            elif group.exact:
                rel = "EXACT duplicates"
            elif group.kind == "doc":
                rel = "NEAR duplicates (same text content)"
            else:
                rel = "NEAR duplicates (perceptual match)"
            color = "#7dff9e" if group.exact else "#ffd166"
            self.head = QLabel(f"Group {group.idx} — {group.kind} — {rel} — "
                          f"{len(group.members)} files · "
                          f"{human(sum(m.size for m in group.members[1:]))} redundant")
            self.head.setStyleSheet(
                f"color:{color};font-weight:bold;font-size:15px;"
                f"padding:2px 2px 10px 2px;border-bottom:1px solid #313845;")
            headrow = QHBoxLayout()
            headrow.addWidget(self.head, 1)
            # "keep all": for near-duplicate groups the user often wants to
            # keep everything (the copies differ enough). Toggling it
            # un-marks every "duplicate → trash" box in this group, and
            # "trash unkept (per group)" skips the group while it is on.
            self.keep_all_btn = QPushButton("✓ keep all")
            self.keep_all_btn.setCheckable(True)
            self.keep_all_btn.setChecked(keep_all)
            self.keep_all_btn.setToolTip(
                "Keep every file in this group: all cards turn green "
                "and the keep/trash boxes are locked (disabled), so "
                "nothing can be re-marked by accident. "
                "\"🗑 trash unkept\" skips this group while it is on. "
                "Click again to restore the default marks.")
            self.keep_all_btn.toggled.connect(self._keep_all_toggled)
            headrow.addWidget(self.keep_all_btn)
            # explicit per-group trash so it is obvious which button
            # affects this group and which (bottom bar) affects all
            self.trash_btn = QPushButton(
                "🗑 trash ALL duplicates in this group")
            self.trash_btn.setObjectName("trashBtn")
            self.trash_btn.setToolTip(
                "ONE CLICK FOR THE WHOLE GROUP — every file in it, "
                "not just the cards visible on screen.\n\n"
                "•  Kept originals stay: the green \u201ckeep this "
                "one\u201d cards (the oldest file of each duplicate "
                "set, or whichever files you marked).\n"
                "•  Everything else in this group goes to your "
                "system trash (recoverable).\n\n"
                "A confirmation dialog shows the exact count and "
                "names the kept files before anything is trashed.\n"
                "(For EVERY group at once, use the \u201c🗑 trash "
                "unkept (all groups)\u201d button in the bottom bar.)")
            self.trash_btn.clicked.connect(self._trash_clicked)
            headrow.addWidget(self.trash_btn)
            # on-demand OCR de-duplication: read the text of every file
            # (tesseract for images / scanned PDFs, direct extraction for
            # digital PDFs and office files), then re-group the cards by
            # text content so true duplicates of one document sit together
            self.ocr_btn = QPushButton("🔍 OCR")
            self.ocr_btn.setToolTip(
                "De-duplicate this group by CONTENT, not by pixels.\n"
                "Reads the text of the files — images and scanned PDFs "
                "are OCR'd with tesseract, digital PDFs and .docx/.odt/"
                ".epub are read directly — then moves the OCR'd files "
                "into a NEW group where they sit together by same-"
                "document set (any encoding, crop or scan quality): the "
                "oldest file of each set is kept (green), the other "
                "copies are marked for trash, so you can review and "
                "trash the suspected duplicates there like any "
                "ordinary group. The original group keeps the rest.\n"
                "Takes ~1.5 s per image; results are cached, so "
                "re-scanning does not repeat the OCR.")
            self.ocr_btn.setEnabled(len(group.members) >= 2)
            self.ocr_btn.clicked.connect(self._ocr_clicked)
            headrow.addWidget(self.ocr_btn)
            self._ocr_lbl = QLabel("")
            self._ocr_lbl.setStyleSheet("color:#8fa3c7;padding-right:4px;")
            headrow.addWidget(self._ocr_lbl)
            # in-group sort: "match" keeps duplicates adjacent (exact
            # copies clustered first, then near matches in similarity
            # blocks so all versions of one file sit side by side);
            # name/size sorts for finding a specific file
            self.sort_combo = QComboBox()
            self.sort_combo.addItems([
                "sort: match (duplicates together)",
                "sort: name A → Z",
                "sort: name Z → A",
                "sort: size big → small",
                "sort: size small → big",
            ])
            if group._ocr_layout is not None:
                self.sort_combo.addItem(
                    "sort: OCR text (same document together)")
            self.sort_combo.setToolTip(
                "Order of the cards in THIS group.\n"
                "match — exact copies first (clustered per file), then "
                "near matches arranged so similar files (the versions "
                "of one file) sit next to each other. For huge groups "
                "this ordering is computed in the background (~1 min "
                "for 100k files); until it is ready the cards are "
                "in size order. You can switch groups while it "
                "runs — the work continues and the new order is "
                "applied when it finishes.\n"
                "OCR text — by document content (appears after 🔍 OCR "
                "has run on this group).\n"
                "name / size — plain sorts, instant.")
            headrow.addWidget(self.sort_combo)
            self._match_lbl = QLabel("")
            self._match_lbl.setStyleSheet("color:#8fa3c7;padding-right:4px;")
            headrow.addWidget(self._match_lbl)
            outer.addLayout(headrow)
            # always show WHICH file is the original: in huge groups the
            # kept card is far away from the first visible batch, so
            # without this the group looks like "everything is trash"
            self.keep_lbl = QLabel("")
            self.keep_lbl.setStyleSheet(
                "color:#7dff9e;font-size:13px;padding:2px 2px 8px 2px;")
            self.keep_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._set_keep_lbl()
            outer.addWidget(self.keep_lbl)
            self._grid = QGridLayout()
            self._grid.setSpacing(12)
            self._grid.setColumnStretch(5, 1)
            self._ocr_prog.connect(self._ocr_prog_cb)
            self._ocr_done_sig.connect(self._ocr_done)
            self._start_match()
            # connect after the initial paint: addItems() already moved
            # the index to 0, and that change must not trigger a second
            # build
            self.sort_combo.currentIndexChanged.connect(self._sort_changed)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            inner = QWidget()
            inner.setLayout(self._grid)
            scroll.setWidget(inner)
            outer.addWidget(scroll, 1)

        def _add_cards(self, i0, i1):
            """Build layout entries [i0, i1) into the grid (batched
            construction). Entries are member indices (cards) or section
            divider labels."""
            per_row = 5
            members = self.group.members
            n = min(i1, len(self._layout))
            row = self._next_row
            col = 0
            for pos in range(i0, n):
                e = self._layout[pos]
                if isinstance(e, str):
                    if col > 0:
                        # a section divider must start a fresh row: if the
                        # current row already holds cards, placing the
                        # full-width label in the same row made it render
                        # ON TOP of those cards (mixed exact+near groups)
                        row += 1
                        col = 0
                    lab = QLabel(e)
                    lab.setStyleSheet(
                        "color:#8fa3c7;font-size:13px;font-weight:bold;"
                        "padding:16px 2px 2px 2px;")
                    self._grid.addWidget(lab, row, 0, 1, per_row)
                    row += 1
                    col = 0
                    continue
                rec = members[e]
                is_keep = (e in self._keep_set)
                card = Card(self, self.group, rec, is_keep, self.root)
                card.group_widget = self
                card._member_idx = e
                # state lives in page dicts, not widgets: re-sorting can
                # rebuild the visible cards without losing the user's
                # marks (defaults: keep box = member of the keep set,
                # dup box = not kept)
                self._seed_keep.setdefault(e, is_keep)
                self._seed_dup.setdefault(e, not is_keep)
                card.keep_radio.setChecked(
                    self._keep_state.get(e, is_keep))
                card.dup_check.setChecked(self._dup_state.get(e, not is_keep))
                card.dup_check.toggled.connect(
                    lambda on, idx=e: self._dup_toggled(idx, on))
                if self._keep_all_on:
                    # batch built while keep-all is locked: same state
                    # as a live toggle (green, boxes disabled)
                    card.dup_check.blockSignals(True)
                    card.keep_radio.blockSignals(True)
                    card.dup_check.setChecked(False)
                    card.dup_check.setEnabled(False)
                    card.keep_radio.setEnabled(False)
                    card.dup_check.blockSignals(False)
                    card.keep_radio.blockSignals(False)
                    card._apply_state()
                self._grid.addWidget(card, row, col)
                self.cards.append(card)
                col += 1
                if col == per_row:
                    row += 1
                    col = 0
            self._next_row = row
            self._shown = n
            self._grid.setRowStretch(row + 1, 1)
            self._update_more_btn()

        def _keep_toggled(self, idx, on):
            self._keep_state[idx] = on
            if on:
                self._keep_set.add(idx)
            else:
                self._keep_set.discard(idx)
            self._set_keep_lbl()
            if self.on_keep:
                self.on_keep(self.group.idx,
                             {self.group.members[i] for i in self._keep_set})

        def _dup_toggled(self, idx, on):
            self._dup_state[idx] = on
            if on:
                # a card can't be both kept and trashed
                self._keep_state[idx] = False
                self._keep_set.discard(idx)
                for card in self.cards:
                    if card._member_idx == idx and card.keep_radio.isChecked():
                        card.keep_radio.blockSignals(True)
                        card.keep_radio.setChecked(False)
                        card.keep_radio.blockSignals(False)
                        card._apply_state()
                        break
                self._set_keep_lbl()
                if self.on_keep:
                    self.on_keep(self.group.idx,
                                 {self.group.members[i]
                                  for i in self._keep_set})

        def _set_keep_lbl(self):
            if getattr(self, "_keep_all_on", False):
                self.keep_lbl.setText(
                    "🛡 keep all is on — nothing in this group will be "
                    "trashed")
                return
            n = len(self._keep_set)
            oldest = min(self._keep_set) if self._keep_set else 0
            m = self.group.members[oldest]
            if not self._keep_set:
                self.keep_lbl.setText(
                    "⚠ nothing in this group is marked keep — the trash "
                    "buttons will not touch any file here")
                return
            if n == 1:
                d = _dt.datetime.fromtimestamp(m.mtime).strftime("%Y-%m-%d")
                self.keep_lbl.setText(
                    f"🔒 original kept: {m.path.name} · {human(m.size)} · "
                    f"{d} — only the other files are marked for trash")
            else:
                self.keep_lbl.setText(
                    f"🔒 {n:,} originals kept — the oldest file of each "
                    f"duplicate set (e.g. {m.path.name}) — only the "
                    f"unmarked copies are trashed")

        def _clear_grid(self):
            while self._grid.count():
                it = self._grid.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            for r in range(self._grid.rowCount() + 2):
                self._grid.setRowStretch(r, 0)
            self.cards = []
            self._next_row = 0
            self._shown = 0

        def _resort(self):
            """Rebuild the visible cards in the new layout, preserving
            the number of entries already shown and all user marks."""
            shown = min(self._shown, len(self._layout))
            self._clear_grid()
            self._add_cards(0, shown)

        def _size_desc_layout(self):
            return sorted(range(len(self.group.members)),
                          key=lambda i: -self.group.members[i].size)

        def _name_layout(self, reverse):
            return sorted(range(len(self.group.members)),
                          key=lambda i: self.group.members[i]
                          .path.name.lower(), reverse=reverse)

        def _size_layout(self, reverse):
            return sorted(range(len(self.group.members)),
                          key=lambda i: self.group.members[i].size,
                          reverse=reverse)

        def _paint_page(self):
            """Show the first batch if nothing is shown yet, else
            rebuild the visible cards in the current layout."""
            if self._shown == 0:
                self._add_cards(0, self.CARD_BATCH)
            else:
                self._resort()

        def _sort_changed(self, idx):
            if idx == 0:
                self._match_lbl.setText(
                    "" if self._layout_ready else
                    self._match_lbl.text() or "ordering…")
                self._start_match()
            else:
                self._match_lbl.setText("")
                if idx == 1:
                    self._layout = self._name_layout(False)
                elif idx == 2:
                    self._layout = self._name_layout(True)
                elif idx == 3:
                    self._layout = self._size_layout(True)
                elif idx == 4:
                    self._layout = self._size_layout(False)
                else:  # OCR text layout (added after 🔍 OCR has run)
                    if self.group._ocr_layout is None:
                        self._match_lbl.setText("run 🔍 OCR first")
                        return
                    self._layout = list(self.group._ocr_layout)
                self._layout_ready = True
                self._resort()

        def _start_match(self):
            """Use (or compute) the match layout: exact copies clustered
            first, near matches in nearest-neighbour similarity blocks.
            Small groups compute instantly; huge groups show in size
            order first and re-sort when the background thread finishes.
            """
            g = self.group
            cached = getattr(g, "_match_layout", None)
            if cached is not None:
                self._layout = cached
                self._layout_ready = True
                self._match_lbl.setText("")
                self._paint_page()
                return
            if g.kind == "doc":
                # documents: the match signal is the text content —
                # same-document files sit together (files without a
                # sketch yet, e.g. scanned PDFs, go to the unique
                # section; 🔍 OCR fills them in)
                lay, kix = ocr_layout(g.members)
                g._match_layout = lay
                g._match_keep_idxs = kix
                g._match_keep_recs = {g.members[i] for i in kix}
                self._keep_set = set(kix)
                self._layout = lay
                self._layout_ready = True
                self._match_lbl.setText("")
                self._paint_page()
                return
            n = len(g.members)
            if n <= 120:
                try:
                    lay, kix = _match_layout(g.members, IMAGE_NEAR_THRESH)
                    g._match_keep_idxs = kix
                    g._match_keep_recs = {g.members[i] for i in kix}
                except BaseException:
                    # a layout failure must never leave the pane blank
                    lay, kix = self._size_desc_layout(), [0]
                self._keep_set = set(kix)
                self._layout = lay
                self._layout_ready = True
                self._paint_page()
                return
            # the ordering job lives on the GROUP, not the page: switching
            # away and back must not restart a multi-minute computation
            job = getattr(g, "_match_job", None)
            if job is None:
                job = {"done": threading.Event(), "lay": None,
                       "pct": 0, "started": False, "finishing": False}
                g._match_job = job
            # the page watches its own reference: the group pointer can
            # be cleared by the worker or a trash while we are waiting
            self._match_job_ref = job
            self._layout = self._size_desc_layout()
            self._layout_ready = False
            self._match_lbl.setText("arranging cards… 0%")
            self._paint_page()
            if not job["started"]:
                job["started"] = True
                members = g.members

                def work():
                    def prog(f):
                        job["pct"] = min(99, int(f * 100))
                        if f >= 1.0:
                            job["finishing"] = True
                    try:
                        lay, kix = _match_layout(members,
                                                 IMAGE_NEAR_THRESH, prog)
                    except BaseException:
                        lay, kix = None, None
                    if lay is None:
                        job["lay"] = None
                        g._match_job = None    # allow a retry next time
                    elif members is not g.members:
                        # the group changed while we were working (e.g.
                        # files were trashed mid-computation) — instead
                        # of discarding the finished layout and running
                        # the whole multi-minute ordering again, remap
                        # it onto the surviving members
                        new_pos = {id(m): i for i, m in
                                   enumerate(g.members)}
                        remap = {old: new_pos[id(m)]
                                 for old, m in enumerate(members)
                                 if id(m) in new_pos}
                        if remap:
                            lay = _remap_layout(lay, remap)
                            kix = [remap[i] for i in kix if i in remap]
                        else:
                            lay, kix = None, []
                        job["lay"] = lay
                        g._match_layout = lay
                        g._match_keep_idxs = kix
                        g._match_keep_recs = {g.members[i] for i in kix}
                    else:
                        job["lay"] = lay
                        g._match_layout = lay
                        g._match_keep_idxs = kix
                        g._match_keep_recs = {members[i] for i in kix}
                    job["done"].set()
                threading.Thread(target=work, daemon=True).start()
            self._start_match_poll()
            # the arrangement is genuinely slow for groups this big —
            # tell the user (small centered window with the progress).
            # Instant paths (cached layout / small group / documents)
            # never reach this line, so there is no window when there
            # is nothing to wait for; if the job already finished by
            # the time the page is built, no window either.
            if not job["done"].is_set():
                self._show_wait_window(job)

        def _show_wait_window(self, job):
            dlg = getattr(self, "_wait_dlg", None)
            if dlg is not None and dlg.isVisible():
                dlg.set_progress(job["pct"])
                return
            try:
                mainwin = self.window()
            except Exception:
                mainwin = None
            self._wait_dlg = ArrangeWaitDialog(
                self, mainwin, len(self.group.members))
            self._wait_dlg.set_progress(job["pct"])
            self._wait_dlg.show()
            self._wait_dlg.raise_()
            # This is shown while the page build keeps running on this
            # thread: Windows maps the new top-level window at once
            # (default white class brush) but Qt's first paint of it
            # cannot happen until the event loop turns — which is
            # minutes away for a huge group's card grid. The user sees
            # an empty white window the whole time. Flush one event
            # round NOW so the dialog paints (dark, with content)
            # before the heavy build continues.
            try:
                QApplication.processEvents()
            except Exception:
                pass

        def _start_match_poll(self):
            """Watch the shared job; when it finishes, apply its layout
            to this page (if the user is still looking at it)."""
            self._match_poll = QTimer(self)
            self._match_poll.setInterval(100)
            self._match_poll.timeout.connect(self._match_poll_cb)
            self._match_poll.start()

        def _match_poll_cb(self):
            try:
                if self._layout_ready \
                        or self.sort_combo.currentIndex() != 0:
                    self._match_poll.stop()
                    self._match_lbl.setText("")
                    wdlg = getattr(self, "_wait_dlg", None)
                    if wdlg is not None:
                        # the user left the match sort — the window
                        # no longer applies to what is on screen
                        try:
                            wdlg.close()
                        except Exception:
                            pass
                        self._wait_dlg = None
                    return
                job = getattr(self, "_match_job_ref", None)
                if job is None or job["done"].is_set():
                    self._match_poll.stop()
                    self._match_lbl.setText("")
                    wdlg = getattr(self, "_wait_dlg", None)
                    if wdlg is not None:
                        # the arrangement is finished — close the
                        # please-wait window (if the user kept it)
                        try:
                            wdlg.set_progress(100)
                            wdlg.close()
                        except Exception:
                            pass
                        self._wait_dlg = None
                    lay = job["lay"] if job is not None else None
                    # a layout computed for the old member list (trash
                    # raced the worker) must not be applied: its
                    # indexes would point at the wrong files
                    if (lay is not None
                            and all(not isinstance(e, int) or
                                    e < len(self.group.members)
                                    for e in lay)):
                        self.group._match_layout = lay
                        self._layout = lay
                        self._layout_ready = True
                        self._adopt_cluster_keeps()
                        self._resort()
                    return
                if job.get("finishing"):
                    self._match_lbl.setText(
                        "arranging cards… almost done")
                else:
                    self._match_lbl.setText(
                        f"arranging cards… {job['pct']}%")
                wdlg = getattr(self, "_wait_dlg", None)
                if wdlg is not None and wdlg.isVisible():
                    wdlg.set_progress(job["pct"])
            except BaseException:
                # the page can be torn down between the timer firing and
                # this running (queued dispatch); never let that kill the
                # event loop silently
                try:
                    self._match_poll.stop()
                except BaseException:
                    pass

        def _adopt_cluster_keeps(self):
            """When the match layout arrives, the default keeps become
            the oldest file of every set — re-mark only the cards the
            user has not touched yet."""
            g = self.group
            new = set(getattr(g, "_match_keep_idxs", None) or ())
            if not new:
                return
            changed = False
            for e in list(self._seed_keep):
                keep_now = self._keep_state.get(e) == self._seed_keep.get(e)
                dup_now = self._dup_state.get(e) == self._seed_dup.get(e)
                if (e in new) != (e in self._keep_set):
                    if keep_now:
                        self._keep_state[e] = e in new
                        changed = True
                    if dup_now:
                        self._dup_state[e] = not (e in new)
                        changed = True
            self._keep_set = new
            if changed:
                for card in self.cards:
                    e = card._member_idx
                    card.keep_radio.blockSignals(True)
                    card.dup_check.blockSignals(True)
                    card.keep_radio.setChecked(
                        self._keep_state.get(e, False))
                    card.dup_check.setChecked(
                        self._dup_state.get(e, False))
                    card.keep_radio.blockSignals(False)
                    card.dup_check.blockSignals(False)
                    card._apply_state()
            self._set_keep_lbl()
            if self.on_keep:
                self.on_keep(self.group.idx,
                             {self.group.members[i] for i in self._keep_set})

        # -------------------------------------------------- OCR (on demand)
        def _ocr_clicked(self):
            if getattr(self, "_ocr_running", False):
                return
            g = self.group
            n = len(g.members)
            scope = n
            if n > OCR_BATCH_WARN:
                box = QMessageBox(self)
                box.setWindowTitle("OCR this group?")
                box.setText(
                    f"This group has {n} files. Reading their text takes "
                    f"about {ocr_estimate(g.members)} for all of them "
                    f"(~1.5 s per image, much less for PDFs).\n\nHow many "
                    f"files should be OCR'd?")
                ok_all = box.addButton("OCR all",
                                       QMessageBox.AcceptRole)
                b250 = box.addButton("first 250",
                                     QMessageBox.ActionRole)
                b500 = box.addButton("first 500",
                                     QMessageBox.ActionRole)
                box.addButton("Cancel", QMessageBox.RejectRole)
                box.setDefaultButton(ok_all)
                box.exec()
                chosen = box.clickedButton()
                if chosen is b250:
                    scope = 250
                elif chosen is b500:
                    scope = 500
                elif chosen is not ok_all:
                    return
            self._ocr_running = True
            self._ocr_scope = scope
            self.ocr_btn.setEnabled(False)
            self._ocr_lbl.setText(f"OCR 0/{scope}…")

            members = g.members
            n_done = 0
            def work():
                nonlocal n_done
                try:
                    sub = members[:scope]
                    for i in range(scope):
                        r = sub[i]
                        if r.text is None:
                            r.text = extract_doc_text(r)
                        elif (r.text and len(r.text) < 100
                              and r.path.suffix.lower() == ".pdf"):
                            # scanned PDF: the scan-time extraction found
                            # almost no text — render the pages and OCR
                            # them now (the button is where that cost
                            # belongs)
                            new = extract_doc_text(r)
                            if len(new) > len(r.text):
                                r.text = new
                        if r.mh is None:
                            r.mh = minhash_of(r.text or "")
                        n_done += 1
                        try:
                            self._ocr_prog.emit(
                                min(99, n_done * 100 // scope))
                        except BaseException:
                            pass
                    # the OCR'd files will form their OWN group (see
                    # _ocr_done): layout/keeps are relative to `sub`
                    lay, kix = ocr_layout(sub)
                    clusters, singles = ocr_clusters(sub)
                    marked = []
                    for c in clusters:
                        ck = min(c)   # oldest file of the set is kept
                        marked.extend(i for i in c if i != ck)
                    marked_bytes = sum(sub[i].size for i in marked)
                    res = {"layout": lay,
                           "keeps": kix,
                           "n_sets": len(clusters),
                           "n_in_sets": sum(len(c) for c in clusters),
                           "marked": marked,
                           "marked_bytes": marked_bytes,
                           "scope": scope,
                           "total": n}
                    # cache the texts so a re-scan skips the OCR
                    try:
                        FPCache().put_ocr(sub)
                    except Exception:
                        pass
                    try:
                        self._ocr_done_sig.emit(res)
                    except BaseException:
                        pass
                except BaseException:
                    import traceback as _tb
                    print("OCR worker failed:\n" + _tb.format_exc(),
                          flush=True)
                    try:
                        self._ocr_done_sig.emit(None)
                    except BaseException:
                        pass
            threading.Thread(target=work, daemon=True).start()

        def _ocr_prog_cb(self, pct):
            scope = getattr(self, "_ocr_scope", 0)
            self._ocr_lbl.setText(f"OCR {pct}% ({scope} files)…")

        def _ocr_done(self, res):
            """The OCR'd files leave this group and form their OWN new
            group, arranged by same-document sets with the oldest file
            of each set kept — the user reviews and trashes the
            suspected duplicates there like any ordinary group.
            Re-OCRing returns the previous OCR group's leftover files
            to this one first, so each file sits in exactly one group."""
            self._ocr_running = False
            try:
                self.ocr_btn.setEnabled(True)
                self._ocr_lbl.setText("")
            except BaseException:
                pass    # page destroyed while OCR ran — widgets are gone
            try:
                win = self.window()
            except BaseException:
                win = None
            g = self.group
            # the page may have been destroyed while OCR was running
            # (page switch, rescan): if the group left the window, this
            # result refers to stale records — drop it instead of adding
            # a ghost group whose files already sit in the new scan
            if win is None or not any(x is g for x in win.all_groups):
                return
            if res is None:
                QMessageBox.warning(self, "OCR",
                                    "OCR failed — see console output.")
                return
            scope = res["scope"]
            sub = list(g.members[:scope])
            if len(sub) < 2:
                box = QMessageBox(win)
                box.setWindowTitle("OCR")
                box.setIcon(QMessageBox.Information)
                box.setText("Not enough files were OCR'd to form a "
                            "group.")
                box.exec()
                return
            # --- return the leftovers of an older OCR group made from
            #     this same source group (files sit in one group only) ---
            old_idx = getattr(g, "_ocr_child_idx", None)
            if old_idx is not None:
                old = next((x for x in win.all_groups if x.idx == old_idx),
                           None)
                if old is not None:
                    g.members = list(g.members) + list(old.members)
                    # membership changed: the cached ordering/keeps no
                    # longer match — recomputed the next time it opens
                    g._match_layout = None
                    g._match_keep_idxs = None
                    g._match_keep_recs = None
                    win.keep_map.pop(g.idx, None)
                    win.all_groups.remove(old)
                    win.keep_map.pop(old_idx, None)
            new_idx = max((x.idx for x in win.all_groups), default=0) + 1
            child = Group(new_idx, sub, False, g.kind)
            child._ocr_parent = g.idx
            child._match_layout = res["layout"]
            child._match_keep_idxs = res["keeps"]
            child._match_keep_recs = {sub[i] for i in res["keeps"]}
            g._ocr_child_idx = new_idx
            g._ocr_layout = None
            # --- move the OCR'd files out of the source group ---
            sub_ids = {id(m) for m in sub}
            old_members = g.members
            g.members = [m for m in old_members if id(m) not in sub_ids]
            new_ids = {id(m) for m in g.members}
            if g._match_layout is not None:
                new_pos = {id(m): i for i, m in enumerate(g.members)}
                remap = {oi: new_pos[id(m)]
                         for oi, m in enumerate(old_members)
                         if id(m) in new_pos}
                if remap:
                    g._match_layout = _remap_layout(g._match_layout, remap)
                    kix = getattr(g, "_match_keep_idxs", None)
                    if kix is not None:
                        g._match_keep_idxs = [remap[i] for i in kix
                                              if i in remap]
                        g._match_keep_recs = {g.members[i] for i in
                                              g._match_keep_idxs}
                else:
                    g._match_layout = None
                    g._match_keep_idxs = None
                    g._match_keep_recs = None
            else:
                # small groups do not cache their layout (recomputed
                # when the page opens), but the cached keep marks still
                # point at the old membership: if they reference a
                # moved file, drop them so the group keeps its (new)
                # oldest file instead of one that left the group
                krecs = getattr(g, "_match_keep_recs", None)
                if krecs is not None and any(id(m) not in new_ids
                                             for m in krecs):
                    g._match_keep_idxs = None
                    g._match_keep_recs = None
            if g.idx in win.keep_map:
                left = {m for m in win.keep_map[g.idx]
                        if id(m) in {id(x) for x in g.members}}
                if left:
                    win.keep_map[g.idx] = left
                else:
                    win.keep_map.pop(g.idx, None)
            # --- register the new group and switch the window to it ---
            win.all_groups.append(child)
            win.all_groups.sort(key=lambda x: (-len(x.members), x.kind,
                                               x.idx))
            win.keep_map[new_idx] = {sub[i] for i in res["keeps"]}
            # a group that fell below 2 files is gone — drop its keep
            # marks with it, or a group that later reuses its idx would
            # inherit stale marks (FileRecs that match nothing = the
            # whole group counts as "unkept")
            for x in win.all_groups:
                if len(x.members) < 2:
                    win.keep_map.pop(x.idx, None)
            win.all_groups = [x for x in win.all_groups
                              if len(x.members) >= 2]
            n_sets = res["n_sets"]
            n_in = res["n_in_sets"]
            n_mark = len(res["marked"])
            # the report box is parented to the WINDOW, not this page:
            # _refilter destroys the page right after, and a dialog
            # parented to a deleted widget is deleted with it
            txt = (f"{res['scope']} of {res['total']} files read.\n\n"
                   f"New group G{new_idx:02d} now holds the OCR'd files:\n"
                   f"{n_sets} set(s) of the same document · "
                   f"{n_in} files in sets\n"
                   f"{n_mark} copies marked for trash "
                   f"(≈ {human(res['marked_bytes'])} recoverable)\n\n"
                   "The oldest file of each set is kept (green). "
                   "Un-mark any copy you want to keep, then use the "
                   "group's trash button.")
            if res["scope"] < res["total"]:
                txt += (f"\n\nOnly the first {res['scope']} files were "
                        "OCR'd (the group is large) — run 🔍 OCR again "
                        "on the original group to read more.")
            box = QMessageBox(win)
            box.setWindowTitle("OCR complete")
            box.setIcon(QMessageBox.Information)
            box.setText(txt)
            win._refilter(select_idx=new_idx)
            box.exec()

        def _update_more_btn(self):
            total_cards = sum(1 for e in self._layout if not isinstance(e, str))
            shown_cards = sum(1 for e in self._layout[:self._shown]
                              if not isinstance(e, str))
            remaining = total_cards - shown_cards
            if remaining <= 0:
                if self.more_btn is not None:
                    self.more_btn.hide()
                    self.more_btn = None
                return
            if self.more_btn is None:
                self.more_btn = QPushButton("")
                self.more_btn.setToolTip(
                    "This group is very large. Cards are shown in "
                    f"batches of {self.CARD_BATCH} so the window stays "
                    "responsive — click to load the next batch.")
                self.more_btn.clicked.connect(
                    lambda: self._add_cards(self._shown,
                                            self._shown + self.CARD_BATCH))
                self._outer.addWidget(self.more_btn)
            self.more_btn.setText(
                f"⬇  show {min(self.CARD_BATCH, remaining):,} more cards"
                f"  ({remaining:,} not shown)")

        def _cards(self):
            return self.cards

        def _trash_clicked(self):
            if self.on_trash:
                self.on_trash(self.group.idx)

        def _keep_all_toggled(self, on):
            self._keep_all_on = on
            self._set_keep_lbl()
            for c in self.cards:
                c.dup_check.blockSignals(True)
                c.keep_radio.blockSignals(True)
                # locked while keep-all is on: everything green, the
                # keep/trash boxes disabled so nothing can be re-marked
                # by accident; turning it off restores the default marks
                c.dup_check.setEnabled(not on)
                c.keep_radio.setEnabled(not on)
                if on:
                    c.dup_check.setChecked(False)
                else:
                    # restore the marks made before the lock (saved in
                    # the state dicts; signals are blocked above, so the
                    # dicts keep the pre-lock state)
                    c.dup_check.setChecked(
                        self._dup_state.get(c._member_idx,
                                            c._member_idx
                                            not in self._keep_set))
                    c.keep_radio.setChecked(
                        self._keep_state.get(c._member_idx,
                                             c._member_idx
                                             in self._keep_set))
                c.dup_check.blockSignals(False)
                c.keep_radio.blockSignals(False)
                c._apply_state()
            if self.on_keep_all:
                self.on_keep_all(self.group.idx, on)

    class RescanWorker(QThread):
        done = Signal(int)                # result token (see _scan_results)
        progress = Signal(object)               # dict: stage/done/total/detail

        _FP_RE = re.compile(r"fingerprinting (\d+)/(\d+)(?:\s{2}(.+))?")
        _LOAD_RE = re.compile(r"loaded (\d+)/(\d+) from cache")

        def __init__(self, root, include_hidden, all_dirs):
            super().__init__()
            self.root = root
            self.include_hidden = include_hidden
            self.all_dirs = all_dirs
            self._fp_cached = 0      # files restored from cache this scan
            self._fp_all = 0

        def _fp_progress(self, msg):
            """Translate the string progress of the fingerprint pass into
            structured updates; cached files count toward the stage total so
            a mostly-cached rescan's bar starts near full."""
            m = self._LOAD_RE.match(msg)
            if m:
                self._fp_cached = int(m.group(1))
                self._fp_all = int(m.group(2))
                self.progress.emit({"stage": "fingerprinting",
                                    "done": self._fp_cached,
                                    "total": self._fp_all,
                                    "detail": "unchanged files from cache"})
                return
            m = self._FP_RE.match(msg)
            if m:
                i, t = int(m.group(1)), int(m.group(2))
                done = self._fp_cached + i if self._fp_all else i
                total = self._fp_all or t
                self.progress.emit({"stage": "fingerprinting", "done": done,
                                    "total": total,
                                    "detail": m.group(3) or ""})

        def run(self):
            global _scan_next_token
            t0 = time.time()
            try:
                self.progress.emit({"stage": "listing files", "done": 0,
                                    "total": 0, "detail": ""})

                def listing_prog(n_files, n_dirs, n_skipped):
                    # live counts: on SMB a walk takes minutes and a
                    # silent bar looks like a freeze
                    d = f"{n_files:,} files in {n_dirs:,} dirs"
                    if n_skipped:
                        d += f" · {n_skipped} unreadable dir(s) skipped"
                    self.progress.emit({"stage": "listing files",
                                        "done": 0, "total": 0,
                                        "detail": d})

                paths = scan_files(self.root, self.include_hidden,
                                   self.all_dirs, listing_prog)
                # closeEvent may requestInterruption() while the window is
                # closing: skip stages that have not started (the cache
                # keeps whatever was already computed)
                if paths and not self.isInterruptionRequested():
                    self.progress.emit(
                        {"stage": "fingerprinting", "done": 0,
                         "total": len(paths),
                         "detail": f"{len(paths)} media files"})
                    _, recs = _fingerprint_with_cache(paths,
                                                      self._fp_progress,
                                                      self.isInterruptionRequested)
                else:
                    recs = []
                if recs and not self.isInterruptionRequested():
                    self.progress.emit({"stage": "grouping duplicates",
                                        "done": 0, "total": 0,
                                        "detail": "matching fingerprints"})
                    def gp(done, total, detail):
                        self.progress.emit({"stage": "grouping duplicates",
                                            "done": done, "total": total,
                                            "detail": detail})
                    groups = build_groups(recs, gp)
                else:
                    groups = []
                # precompute the Files-tab rows HERE (worker thread), not
                # in _rescan_done: with 100k files that string building +
                # sort takes seconds and used to freeze the window right
                # at the moment the results landed ("non responsive, then
                # the images flashed")
                file_rows = []
                if recs:
                    for r in recs:
                        s = str(r.path)
                        file_rows.append(
                            ((os.path.dirname(s).lower(),
                              r.path.name.lower()),
                             s.lower(), s, r))
                    file_rows.sort(key=lambda t: t[0])
                self._emit_done(groups, recs, time.time() - t0, file_rows)
            except Exception as e:
                self._emit_done(None, e, time.time() - t0, None)

        def _emit_done(self, groups, recs_or_err, secs, file_rows=None):
            """Store the result and signal the main thread with a token
            only — the payload never crosses the thread boundary by copy."""
            global _scan_next_token
            _scan_next_token += 1
            _scan_results[_scan_next_token] = (groups, recs_or_err, secs,
                                               file_rows)
            self.done.emit(_scan_next_token)

    class Main(QMainWindow):
        _trash_done_sig = Signal(object)    # (result, targets) from worker
        _trash_prog_sig = Signal(int, int)  # (done, total) manual trash

        def showEvent(self, ev):
            super().showEvent(ev)
            _win_dark_titlebar(self)

        def __init__(self, root, include_hidden=False, all_dirs=False):
            super().__init__()
            self._trash_done_sig.connect(self._trash_done)
            self._trash_prog_sig.connect(self._trash_progress_cb)
            self.root = root
            self.include_hidden = include_hidden
            self.all_dirs = all_dirs
            self.recs = []
            self.scan_secs = 0.0
            self._worker = None
            self._scanning = True          # initial scan runs in a thread
            self._scan_detail = None
            self._scan_bar = None
            self._scan_head = None
            # staged progress state: the bar/ETA are repainted by a timer
            # every 400 ms (extrapolating from the last worker update), so
            # the UI keeps moving even while the worker is inside a long
            # single-threaded stage like grouping.
            self._prog_stage = None
            self._prog_total = 0
            self._prog_done = 0
            self._prog_t = 0.0
            self._prog_hist = deque()
            self._prog_detail = ""
            self._prog_timer = QTimer(self)
            self._prog_timer.setInterval(400)
            self._prog_timer.timeout.connect(self._paint_progress)
            self.all_groups = []
            self.keep_map = {}
            self.visible = []
            self.cur = 0
            self._panel = "groups"
            self.keep_all = set()      # group idxs the user marked "keep all"
            self.dirs = {}            # dir Path -> (file_count, dup_count)
            self.dup_paths = set()    # file paths inside duplicate groups
            # large-list fill pump: Folders/Files rows are appended in small
            # timer-driven chunks (a few ms of work per tick) so a 100k-file
            # library never freezes the event loop while the list fills.
            self._fill_timer = QTimer(self)
            self._fill_timer.setInterval(8)
            self._fill_timer.timeout.connect(self._fill_tick)
            self._fill_deb = QTimer(self)
            self._fill_deb.setSingleShot(True)
            self._fill_deb.setInterval(250)
            self._fill_deb.timeout.connect(self._fill_files)
            self._fill_rows = []
            self._fill_i = 0
            self._file_rows_all = []  # precomputed (key, path, rec) for files
            self.setWindowTitle(f"Duplicate finder — {root}")
            central = QWidget()
            self.setCentralWidget(central)
            main_v = QVBoxLayout(central)
            main_v.setContentsMargins(6, 6, 6, 6)
            top_h = QHBoxLayout()

            # left: Groups / Folders / Files. Groups is the duplicate list;
            # Folders and Files are location links — click a directory to
            # open it in the file manager, click a file to open it in its
            # default app (handy for navigating big libraries).
            leftw = QWidget()
            leftw.setFixedWidth(360)
            lv = QVBoxLayout(leftw)
            lv.setContentsMargins(0, 0, 0, 0)
            lv.setSpacing(3)
            self.tabbar = QTabBar()
            self.tabbar.setExpanding(False)
            self.tabbar.addTab("Groups")
            self.tabbar.addTab("Folders")
            self.tabbar.addTab("Files")
            self.tabbar.currentChanged.connect(self._panel_mode)
            lv.addWidget(self.tabbar)
            self.summary_lbl = QLabel("")
            self.summary_lbl.setWordWrap(True)
            self.summary_lbl.setStyleSheet(
                "color:#9fb6d9;font-size:12px;padding:2px 4px 6px 4px;"
                "border-bottom:1px solid #232936;")
            self.summary_lbl.hide()
            lv.addWidget(self.summary_lbl)
            self.listw = QListWidget()
            # every row is a single line: mark the item sizes as uniform so
            # Qt doesn't re-measure the whole list on every event — adding
            # 100k rows otherwise takes seconds and freezes the window
            self.listw.setUniformItemSizes(True)
            self.listw.itemDoubleClicked.connect(self._list_activated)
            self.listw.setContextMenuPolicy(Qt.CustomContextMenu)
            self.listw.customContextMenuRequested.connect(self._list_menu)
            self.listw.currentRowChanged.connect(self._row_changed)
            lv.addWidget(self.listw, 1)
            self.file_filter = QLineEdit()
            self.file_filter.setPlaceholderText("filter files…")
            self.file_filter.setClearButtonEnabled(True)
            self.file_filter.textChanged.connect(self._filter_changed)
            self.file_filter.hide()
            lv.addWidget(self.file_filter)
            top_h.addWidget(leftw)

            # center: page area
            self.stack = QWidget()
            self.stacklay = QVBoxLayout(self.stack)
            self.stacklay.setContentsMargins(0, 0, 0, 0)
            top_h.addWidget(self.stack, 1)

            # bottom bar
            bar = QHBoxLayout()
            self.cb_kind = QComboBox()
            self.cb_kind.addItems(["All media", "Images only",
                                   "Videos only", "Documents only"])
            self.cb_kind.currentIndexChanged.connect(self._refilter)
            bar.addWidget(self.cb_kind)
            self.cb_exact = QCheckBox("exact only")
            self.cb_exact.toggled.connect(lambda _: self._refilter())
            bar.addWidget(self.cb_exact)
            bar.addStretch(1)
            b = lambda txt, fn, tip="": _mkbtn(bar, txt, fn, tip)
            b("📁 scan directory…", self._pick_dir,
              "Pick a folder to scan.\n\n"
              "Replaces the current list — the fingerprint cache is "
              "kept, so an unchanged library re-scans in seconds.\n\n"
              "Picking the CURRENT folder re-scans it (use the "
              "\"↻ rescan this folder\" button for that without a "
              "picker).")
            b("↻ rescan this folder", self._rescan_same,
              "Re-scan the CURRENT folder without a picker — picks up "
              "files you added, changed or deleted outside the app "
              "(the trash included), and refreshes the list after an "
              "external cleanup.\n\n"
              "The fingerprint cache is kept, so an unchanged library "
              "re-scans in seconds.")
            b("◀ prev", lambda: self._nav(-1),
              "Show the previous group.")
            b("next ▶", lambda: self._nav(1),
              "Show the next group.")
            b("auto-keep oldest (all groups)", self._auto_keep,
              "Reset the keep marks in EVERY visible group to the "
              "default: the oldest file of each duplicate set.\n\n"
              "Your manual \"keep this one\" / \"duplicate\" edits "
              "are replaced. (For groups whose ordering has not "
              "finished yet, this marks the oldest file of the whole "
              "group; the per-set marks are applied automatically "
              "when the ordering completes.)")
            b("🗑 trash checked (this group)", self._trash_checked,
              "Trashes only the files whose \"duplicate → trash\" "
              "box is CHECKED on the cards currently visible in the "
              "group on screen.\n\n"
              "Unmarked files and every other group are not "
              "touched. (For the whole group at once, use the "
              "\"🗑 trash ALL duplicates in this group\" button in "
              "the group header.)")
            b("🗑 trash unkept (all groups)", self._trash_unkept,
              "THE BIG CLEANUP — affects EVERY visible group, not "
              "just the group on screen.\n\n"
              "•  In each group the green \"keep this one\" "
              "originals stay (the oldest file of every duplicate "
              "set, or your own marks).\n"
              "•  EVERY other file in those groups goes to your "
              "system trash (recoverable) — the whole group, not "
              "just the visible cards.\n"
              "•  Groups switched to \"✓ keep all\" (🛡) are "
              "skipped.\n\n"
              "A confirmation dialog shows the total before "
              "anything is trashed. For a single group, use "
              "\"🗑 trash ALL duplicates in this group\" in the "
              "group header instead.")
            b("export CSV", self._export,
              "Write a CSV list of every group, with one line per "
              "file: group, name, size, date, keep/dup mark.")
            b("🧹 clear cache", self._clear_cache,
              "Delete the stored fingerprints (hashes, thumbnails, "
              "OCR text) so the next scan re-fingerprints every file "
              "from scratch.\n\n"
              "Your results on screen are NOT touched — only the "
              "cache is deleted. The first scan after clearing is "
              "slow again; every following scan of unchanged files "
              "is fast until the cache grows back.")
            self._clear_close = QCheckBox("clear cache on close")
            self._clear_close.setChecked(
                bool(_load_prefs().get("clear_cache_on_close")))
            self._clear_close.setToolTip(
                "Delete the fingerprint cache automatically when the "
                "app closes.\n\n"
                "Use it if you do not want multi-gigabyte caches "
                "lingering on disk. The next scan re-fingerprints "
                "everything (slower), and the cache rebuilds as you "
                "scan. Your results and keep-marks are not touched.")
            self._clear_close.toggled.connect(self._clear_close_toggled)
            bar.addWidget(self._clear_close)
            b("close", self.close,
              "Close dupe-finder.")
            bar.setContentsMargins(12, 8, 12, 8)
            barw = QWidget()
            barw.setObjectName("bottombar")
            barw.setLayout(bar)
            main_v.addLayout(top_h, 1)
            main_v.addWidget(barw)

            self.status = QStatusBar()
            global STALE_NOTIFY
            STALE_NOTIFY = lambda m: self.status.showMessage(m, 8000)
            self.setStatusBar(self.status)
            for key in ("Left", "Right"):
                sc = QShortcut(QKeySequence(f"Arrow{key}" if False else ("Left" if key=="Left" else "Right")), self)
                sc.activated.connect(lambda k=key: self._nav(-1 if k == "Left" else 1))
            self._show_scanning(str(self.root))
            self.status.showMessage("scanning …")
            self._start_worker(self.root)

        # -- helpers
        def _cards_of_current(self):
            w = self.stacklay.itemAt(0)
            page = w.widget() if w else None
            return page.cards if page else []

        def _start_worker(self, root):
            self._worker = RescanWorker(root, self.include_hidden,
                                        self.all_dirs)
            self._worker.progress.connect(self._scan_progress)
            self._worker.done.connect(self._rescan_done)
            _live_workers.add(self._worker)   # survives a mid-scan window close
            if getattr(self, "summary_lbl", None) is not None:
                self.summary_lbl.hide()      # totals refresh when results land
            self._worker.start()

        def _scan_progress(self, info):
            """Worker progress: dict(stage, done, total, detail). Keeps
            (time, done) samples for ~30 s; the bar, ETA and status text
            are redrawn by _paint_progress (400 ms timer), which
            extrapolates between worker updates at the windowed AVERAGE
            rate — parallel chunk completions arrive in bursts, so an
            instantaneous rate spikes (it used to print "0 s left" while
            the stage was half done and the bar jumped to ~100%)."""
            stage = info.get("stage") or ""
            done = int(info.get("done") or 0)
            total = int(info.get("total") or 0)
            detail = info.get("detail") or ""
            if stage != self._prog_stage:
                self._prog_stage = stage
                self._prog_done = 0
                self._prog_hist.clear()
                self._prog_t = time.time()
                self._prog_t0 = time.time()   # stage start (for the ETA)
            self._prog_total = total
            self._prog_detail = detail
            if total > 0:
                now = time.time()
                self._prog_done, self._prog_t = done, now
                self._prog_hist.append((now, done))
                while (len(self._prog_hist) > 2 and
                       self._prog_hist[0][0] < now - 30):
                    self._prog_hist.popleft()
            self._paint_progress()

        def _paint_progress(self):
            if not self._scanning or self._scan_bar is None:
                return
            stage = self._prog_stage or "scanning"
            total = self._prog_total
            shown = self._prog_done
            etas = ""
            live = total > 0
            now = time.time()
            if live:
                # rate over the last ~30 s of worker samples (windowed
                # average): bursty parallel chunk completions spike any
                # per-update rate, which used to make the bar jump to
                # ~100% and print "0 s left" while half the pairs were
                # still undone
                avg = 0.0
                if len(self._prog_hist) >= 2:
                    t0h, d0h = self._prog_hist[0]
                    t1h, d1h = self._prog_hist[-1]
                    if t1h - t0h > 1e-6 and d1h > d0h:
                        avg = (d1h - d0h) / (t1h - t0h)
                if avg > 1e-6:
                    # extrapolate between worker updates so the bar never
                    # appears stuck
                    shown = self._prog_done + avg * (now - self._prog_t)
                    if shown > total - 1:
                        # hold the display at ~100% (total-1) until the
                        # stage really finishes
                        shown = float(total - 1)
                    shown = int(shown)
                    if now - getattr(self, "_prog_t0", now) > 3:
                        # ETA from the LAST WORKER-REPORTED count (not the
                        # extrapolated display value) over the windowed
                        # rate
                        etas = (f" · {_human_secs((total - self._prog_done) / avg)} left")
            # no worker count has arrived yet (the pool is warming up
            # and the first chunk is in flight): sweep the bar and say
            # so, instead of sitting at a frozen 0%
            warming = (live and self._prog_done == 0 and
                       now - getattr(self, "_prog_t0", now) > 3)
            bar = self._scan_bar
            if live and not warming:
                bar.setRange(0, 1000)
                bar.setValue(max(0, min(1000, shown * 1000 // total)))
            else:
                # no total (e.g. listing files on a network share): a
                # plain sweep looks dead. Sweep for the first few
                # seconds, then creep the bar slowly — it never reaches
                # the end, so it reads as "still going", while the live
                # count in the text carries the real state
                elapsed = now - getattr(self, "_prog_t0", now)
                if elapsed > 3:
                    bar.setRange(0, 1000)
                    bar.setValue(int(900 * (1.0 - math.exp(-elapsed / 45.0))))
                else:
                    bar.setRange(0, 0)      # indeterminate sweep
            txt = stage
            if total and not warming:
                txt = f"{stage} {_fmt_cnt(shown)}/{_fmt_cnt(total)}"
            if self._prog_detail:
                txt += f" · {self._prog_detail}"
            if warming:
                txt += " · please wait — still working"
            txt += etas
            self.status.showMessage(txt)
            if self._scan_detail is not None:
                fm = self._scan_detail.fontMetrics()
                self._scan_detail.setText(fm.elidedText(
                    txt, Qt.ElideMiddle, self._scan_detail.width() - 4))
            if self._scan_head is not None:
                self._scan_head.setText(
                    "🧩 Grouping duplicates …"
                    if stage.startswith("grouping")
                    else "🔍 Scanning for duplicates …")

        def _show_scanning(self, root_s):
            """Placeholder shown while a scan runs, so the window never
            looks dead."""
            self._scan_detail = None
            while self.stacklay.count():
                it = self.stacklay.takeAt(0)
                w = it.widget()
                if w:
                    w.hide()              # destroy the old page in place —
                    w.setParent(None)     # it must not pop out as a floating
                    w.deleteLater()       # top-level "ghost" window
            w = QWidget()
            v = QVBoxLayout(w)
            v.setAlignment(Qt.AlignCenter)
            l1 = QLabel("🔍 Scanning for duplicates …")
            l1.setStyleSheet("font-size:20px;font-weight:bold;")
            self._scan_head = l1
            self._prog_stage = None
            self._prog_total = 0
            self._prog_done = 0
            self._prog_t = time.time()
            self._prog_t0 = time.time()   # stage start (for the ETA)
            self._prog_hist = deque()
            self._prog_detail = ""
            self._prog_timer.start()
            l2 = QLabel(root_s)
            l2.setStyleSheet("color:#8a93a2;")
            self._scan_detail = QLabel("")
            self._scan_detail.setStyleSheet(
                "color:#9aa4b2;font-size:12px;")
            # Fixed-size, left-aligned box: the per-file progress text
            # updates in place instead of swinging around (center-aligned
            # text of varying length jumps left and right on every update).
            self._scan_detail.setFixedSize(560, 20)
            self._scan_detail.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            bar = QProgressBar()
            bar.setRange(0, 0)             # indeterminate until counts arrive
            self._scan_bar = bar
            bar.setFixedWidth(360)
            bar.setFixedHeight(18)
            bar.setStyleSheet(
                "QProgressBar{border:1px solid #414c5d;border-radius:9px;"
                "background:#14171d;}"
                "QProgressBar::chunk{background:#2b6cb0;border-radius:8px;}")
            v.addWidget(l1)
            v.addWidget(l2)
            v.addSpacing(8)
            v.addWidget(self._scan_detail)
            v.addSpacing(8)
            v.addWidget(bar)
            self.stacklay.addWidget(w)

        def _show_message(self, head, sub=""):
            self.listw.clear()
            while self.stacklay.count():
                it = self.stacklay.takeAt(0)
                w = it.widget()
                if w:
                    w.hide()              # destroy the old page in place —
                    w.setParent(None)     # it must not pop out as a floating
                    w.deleteLater()       # top-level "ghost" window
            w = QWidget()
            v = QVBoxLayout(w)
            v.setAlignment(Qt.AlignCenter)
            l1 = QLabel(head)
            l1.setStyleSheet("font-size:20px;font-weight:bold;")
            v.addWidget(l1)
            if sub:
                l2 = QLabel(sub)
                l2.setWordWrap(True)
                l2.setStyleSheet("color:#8a93a2;")
                v.addWidget(l2)
            self.stacklay.addWidget(w)

        def _refresh_page(self):
            while self.stacklay.count():
                it = self.stacklay.takeAt(0)
                w = it.widget()
                if w:
                    w.hide()              # destroy the old page in place —
                    w.setParent(None)     # it must not pop out as a floating
                    w.deleteLater()       # top-level "ghost" window
            g = self.visible[self.cur]
            keeps = self.keep_map.get(g.idx) or default_keep_recs(g)
            try:
                page = GroupPage(self.stack, g, keeps, self.root,
                                 keep_all=(g.idx in self.keep_all),
                                 on_keep_all=self._group_keep_all,
                                 on_trash=self._trash_group_only,
                                 on_keep=self._set_keep)
            except BaseException:
                # a dead page leaves the pane blank and this slot's
                # exception would be swallowed by Qt — keep a trace
                import traceback
                print(f"page build failed for group {g.idx} "
                      f"({len(g.members)} {g.kind} files):\n"
                      + traceback.format_exc(), flush=True)
                raise
            page.cards = _walk_cards(page)
            if g.idx in self.keep_all:
                # rebuilt page: honour the keep-all choice on the new cards
                for c in page.cards:
                    c.dup_check.blockSignals(True)
                    c.dup_check.setChecked(False)
                    c.dup_check.blockSignals(False)
                    c._apply_state()
            self.stacklay.addWidget(page)

        def _group_keep_all(self, gidx, on):
            if on:
                self.keep_all.add(gidx)
            else:
                self.keep_all.discard(gidx)
            for i, g in enumerate(self.visible):
                if g.idx == gidx:
                    it = self.listw.item(i)
                    if it is not None:
                        it.setText(self._group_row_text(g))
                    break

        def _set_keep(self, gidx, recs):
            # the user changed the "keep this one" marks in a group page:
            # remember the whole kept set so "trash unkept" and a later
            # page rebuild honour the choice
            self.keep_map[gidx] = set(recs)
            self._status()

        def _nav(self, d):
            if not self.visible:
                return
            self.cur = (self.cur + d) % len(self.visible)
            self.listw.setCurrentRow(self.cur)
            self.show_current()

        def _row_changed(self, row):
            if self._panel != "groups":
                return                  # Folders/Files rows are links
            if row >= 0 and row < len(self.visible):
                self.cur = row
                self.show_current()

        def _panel_mode(self, idx):
            self._panel = ("groups", "folders", "files")[idx]
            self.file_filter.setVisible(self._panel == "files")
            self._fill_timer.stop()
            self._fill_rows = []
            self._fill_i = 0
            if self._panel == "groups":
                self._refilter()
            elif self._panel == "folders":
                self._fill_folders()
            else:
                self._fill_files()

        # ---- Folders / Files fill pump -------------------------------------
        # rows are appended a few-millisecond batch at a time by _fill_tick so
        # switching to a 100k-row list stays responsive the whole time.

        def _start_fill(self, rows):
            self._fill_timer.stop()
            self.listw.blockSignals(True)
            self.listw.clear()
            self.listw.blockSignals(False)
            self._fill_rows = list(rows)
            self._fill_i = 0
            if self._fill_rows:
                self._fill_timer.start()

        def _fill_tick(self):
            rows = self._fill_rows
            i = self._fill_i
            if i >= len(rows):
                self._fill_timer.stop()
                return
            t0 = time.time()
            while i < len(rows) and time.time() - t0 < 0.006:
                text, data, tip = rows[i]
                it = QListWidgetItem(text)
                if data is not None:
                    it.setData(Qt.UserRole, data)
                if tip:
                    it.setToolTip(tip)
                self.listw.addItem(it)
                i += 1
            self._fill_i = i
            if i >= len(rows):
                self._fill_timer.stop()
                self._fill_rows = []
                self._fill_i = 0
                self.status.clearMessage()

        def _filter_changed(self, _text):
            if self._panel != "files":
                return
            self._fill_timer.stop()
            self._fill_deb.start()

        def _list_activated(self, item):
            data = item.data(Qt.UserRole)
            if not data:
                if self._panel == "groups":
                    self.show_current()
                return
            if self._panel == "folders":
                open_folder(Path(data))
            elif self._panel == "files":
                open_file(Path(data))

        def _list_menu(self, pos):
            item = self.listw.itemAt(pos)
            if not item:
                return
            data = item.data(Qt.UserRole)
            if not data:
                return
            p = Path(data)
            m = QMenu(self)
            if self._panel == "folders":
                m.addAction("Open folder", lambda: open_folder(p))
            elif self._panel == "files":
                m.addAction("Open file", lambda: open_file(p))
                m.addAction("Open containing folder",
                            lambda: open_folder(p.parent))
            m.addAction("Copy path",
                        lambda: QApplication.clipboard().setText(str(p)))
            m.exec(self.listw.mapToGlobal(pos))

        def _fill_folders(self):
            """Compute folder rows and hand them to the fill pump."""
            try:
                root_r = self.root.resolve()
            except OSError:
                root_r = self.root
            rows = []
            for d, (cnt, dup) in self.dirs.items():
                try:
                    rel = d.relative_to(root_r)
                    rel_s = "(root)" if str(rel) == "." else str(rel)
                except ValueError:
                    rel_s = str(d)
                extra = f", {dup} in dupes" if dup else ""
                rows.append((
                    (-cnt, rel_s),
                    (f"📁 {rel_s}  "
                     f"({cnt} file{'s' if cnt != 1 else ''}{extra})",
                     str(d),
                     f"{d}\n{cnt} media file(s){extra} — click to open in "
                     f"the file manager")))
            rows.sort(key=lambda t: t[0])
            self._start_fill((r for _, r in rows))

        def _fill_files(self, _text=None):
            """Compute file rows (filtered) and hand them to the fill pump."""
            filt = self.file_filter.text().strip().lower()
            rows = []
            for _key, path_low, path_s, r in self._file_rows_all:
                if filt and filt not in path_low:
                    continue
                err = r.error
                rows.append((
                    ("⚠ " if err else "") + r.path.name,
                    path_s,
                    f"{path_s}\n{human(r.size)}"
                    + (f"\n⚠ {err}" if err else "")
                    + "\nclick to open in the default app"))
            self._start_fill(iter(rows))

        def show_current(self):
            if self.visible:
                self._refresh_page()

        def _refilter(self, select_idx=None):
            # select_idx: land on this group (by idx) instead of row 0 —
            # used when a fresh OCR group is created, so the window
            # switches straight to it
            if self._scanning:
                return                  # placeholder page is showing
            if self._panel != "groups":
                # a rescan always lands back on the Groups tab
                self.tabbar.blockSignals(True)
                self.tabbar.setCurrentIndex(0)
                self.tabbar.blockSignals(False)
                self._panel = "groups"
                self.file_filter.setVisible(False)
            kind = {"All media": None, "Images only": "image",
                    "Videos only": "video",
                    "Documents only": "doc"}[self.cb_kind.currentText()]
            exact_only = self.cb_exact.isChecked()
            self.visible = [g for g in self.all_groups
                            if (kind is None or g.kind == kind)
                            and (not exact_only or g.exact)]
            self._update_summary()
            self._fill_timer.stop()
            self._fill_rows = []
            self._fill_i = 0
            self.listw.blockSignals(True)
            self.listw.clear()
            for i, g in enumerate(self.visible):
                it = QListWidgetItem(self._group_row_text(g))
                it.setForeground(QColor(
                    "#4fd691" if g.exact else "#e8edf5"))
                it.setToolTip(str(g.members[0].path))
                self.listw.addItem(it)
            if self.listw.count():
                if select_idx is not None:
                    pos = next((i for i, x in enumerate(self.visible)
                                if x.idx == select_idx), 0)
                else:
                    pos = 0
                self.cur = pos
                # still signal-blocked: setCurrentRow would otherwise
                # trigger its own page build, doubling the work
                self.listw.setCurrentRow(pos)
                self.listw.blockSignals(False)
                self._refresh_page()
            else:
                self.listw.blockSignals(False)
                self._refresh_page_empty()
            self._status()

        def _group_row_text(self, g):
            keeps = self.keep_map.get(g.idx) or default_keep_recs(g)
            red = sum(m.size for m in g.members if m not in keeps)
            mark = ("🛡 " if g.idx in self.keep_all
                    else ("🔍 "
                          if getattr(g, "_ocr_parent", None) is not None
                          else ""))
            return (f"{'✔' if g.exact else '≈'}  {mark}G{g.idx:02d}  "
                    f"{g.kind:<5} ×{len(g.members)}  {human(red)} free")

        def _update_summary(self):
            """Total duplicates + recoverable space, shown above the
            group list (always over ALL groups, filter-independent)."""
            if getattr(self, "summary_lbl", None) is None:
                return
            if not self.all_groups:
                self.summary_lbl.hide()
                return
            dupes = sum(len(g.members) - 1 for g in self.all_groups)
            free = 0
            for g in self.all_groups:
                keeps = self.keep_map.get(g.idx) or default_keep_recs(g)
                free += sum(m.size for m in g.members if m not in keeps)
            self.summary_lbl.setText(
                f"{len(self.all_groups):,} groups · {dupes:,} duplicate "
                f"files · {human(free)} recoverable")
            self.summary_lbl.show()

        def _refresh_page_empty(self):
            while self.stacklay.count():
                it = self.stacklay.takeAt(0)
                w = it.widget()
                if w:
                    w.hide()              # destroy the old page in place —
                    w.setParent(None)     # it must not pop out as a floating
                    w.deleteLater()       # top-level "ghost" window
            txt = ("No duplicates found 🎉" if not self.all_groups
                   else "No groups match the current filter.")
            lbl = QLabel(txt)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color:#8a93a2;padding:24px;")
            self.stacklay.addWidget(lbl)

        def _status(self):
            if self._scanning:
                return
            vis = self.visible
            red = 0
            for g in vis:
                keeps = self.keep_map.get(g.idx) or default_keep_recs(g)
                red += sum(m.size for m in g.members if m not in keeps)
            nexact = sum(1 for g in vis if g.exact)
            skipped = sum(1 for r in self.recs if r.error)
            self.status.showMessage(
                f"scanned {len(self.recs)} files in {self.scan_secs:.1f}s · "
                f"{len(self.all_groups)} groups total · visible: {len(vis)} "
                f"({nexact} exact, {len(vis)-nexact} near) · "
                f"{human(red)} recoverable"
                + (f" · {skipped} skipped" if skipped else ""))

        def _rescan_same(self):
            """Re-scan the CURRENT folder without a picker — the user's
            files may have changed outside the app (or they just trashed
            a lot and want to verify the list). The fingerprint cache
            makes an unchanged library finish in seconds."""
            if self._worker is not None:
                return                  # a scan is already running
            self.setWindowTitle(f"Duplicate finder — {self.root} (scanning…)")
            self._scanning = True
            self._show_scanning(str(self.root))
            self.status.showMessage(f"rescanning {self.root} …")
            self._start_worker(self.root)

        def _pick_dir(self):
            if self._worker is not None:
                return                  # a scan is already running
            d = pick_directory(self, "Choose directory to scan", self.root)
            if d is None:
                return
            new_root = d
            if new_root == self.root:
                self._rescan_same()    # picking the current folder = rescan
                return
            self.root = new_root
            self.setWindowTitle(f"Duplicate finder — {new_root} (scanning…)")
            self._scanning = True
            self._show_scanning(str(new_root))
            self.status.showMessage(f"scanning {new_root} …")
            self._start_worker(new_root)

        def _clear_close_toggled(self, on):
            d = _load_prefs()
            d["clear_cache_on_close"] = bool(on)
            _save_prefs(d)

        def _clear_cache(self):
            if self._scanning:
                self.status.showMessage(
                    "wait for the scan to finish, then clear the cache",
                    6000)
                return
            n, sz = _fp_cache_info()
            if n == 0 and sz == 0:
                self.status.showMessage(
                    "fingerprint cache is already empty", 6000)
                return
            r = QMessageBox.question(
                self, "Clear fingerprint cache",
                f"Delete the fingerprint cache?\n"
                f"({n:,} stored fingerprints, {human(sz)} on disk)\n\n"
                "The results on screen stay as they are. The next "
                "scan re-fingerprints every file from scratch — a "
                "first scan on a big library takes minutes again.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
            _clear_fp_cache()
            self.status.showMessage(
                "fingerprint cache cleared — the next scan will "
                "re-fingerprint everything", 8000)

        def closeEvent(self, ev):
            w = self._worker
            if w is not None:
                r = QMessageBox.question(
                    self, "Close",
                    "A scan is still running. Close anyway?\n"
                    "(fingerprints already computed stay cached, so the "
                    "next scan of this folder is fast)",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if r != QMessageBox.Yes:
                    ev.ignore()
                    return
                self._prog_timer.stop()
                self._fill_timer.stop()
                # keep the C++ QThread alive past the window's death —
                # destroying it while it runs is a Qt fatal abort; the
                # worker finishes (or skips its remaining stages) in the
                # background and main() joins it before we exit
                _live_workers.add(w)
                self._worker = None
                w.requestInterruption()
            if (getattr(self, "_clear_close", None) is not None
                    and self._clear_close.isChecked()):
                # the user asked for no cache to outlive the app. A
                # still-running worker may commit to the (now
                # unlinked) database a moment later — harmless, and if
                # it opens a fresh FPCache afterwards that file is a
                # valid partial cache, never a corrupt one.
                _clear_fp_cache()
            ev.accept()

        def _rescan_done(self, token):
            groups, recs_or_err, secs, file_rows = _scan_results.pop(
                token, (None, RuntimeError("scan result lost"), 0.0, None))
            w = self._worker
            self._worker = None
            if w is not None:
                _live_workers.discard(w)
                # "done" arrives as a QUEUED signal: join the worker
                # thread before the wrapper object can be released, or
                # Qt's QThread destructor can run while the native thread
                # is still alive and abort the whole app (qFatal — the
                # app "vanishing" right after a scan finished). run()
                # has returned by now, but the teardown (GC of tens of
                # thousands of recs, C++ destructors) can outlive a
                # short wait on a slow machine — wait generously and
                # keep the wrapper alive if it is still finishing.
                if not w.wait(15000):
                    _live_workers.add(w)      # wrapper must not be GC'd
                    w.wait(90000)
                    _live_workers.discard(w)
            self._scanning = False
            self._prog_timer.stop()
            if not isinstance(recs_or_err, list):
                self._show_message("Scan failed",
                                   f"{type(recs_or_err).__name__}: "
                                   f"{recs_or_err}")
                self.status.showMessage("scan failed")
                return
            self.recs = recs_or_err
            self.scan_secs = secs
            self.all_groups = groups
            self.keep_map = {g.idx: default_keep_recs(g) for g in groups}
            self.keep_all = set()
            # location index for the Folders/Files panels
            self.dup_paths = {m.path for g in groups for m in g.members}
            dirs = {}
            for r in recs_or_err:
                d = r.path.parent
                cnt, dup = dirs.get(d, (0, 0))
                if r.path in self.dup_paths:
                    dirs[d] = (cnt + 1, dup + 1)
                else:
                    dirs[d] = (cnt + 1, dup)
            self.dirs = dirs
            # the Files-tab rows were precomputed in the worker thread
            # (the sort takes seconds at 100k files and must not freeze
            # the window at the moment the results land)
            self._file_rows_all = file_rows or []
            self._fill_timer.stop()
            self._fill_rows = []
            self._fill_i = 0
            self.setWindowTitle(f"Duplicate finder — {self.root}")
            if not self.recs:
                self._show_message("No image or video files found",
                                   str(self.root))
                self.status.showMessage(f"no media files in {self.root}")
                return
            self._refilter()

        def _auto_keep(self):
            for g in self.visible:
                self.keep_map[g.idx] = default_keep_recs(g)
            if self.listw.count():
                self.listw.setCurrentRow(self.cur)
                self._refresh_page()
            self._status()

        def _trash_checked(self):
            page = _current_page(self.stacklay)
            if not page:
                return
            targets = [c.rec for c in page.cards if c.dup_check.isChecked()]
            keep_recs = {c.rec for c in page.cards if c.keep_radio.isChecked()}
            targets = [t for t in targets if t not in keep_recs]
            if not targets:
                QMessageBox.information(self, "Nothing checked",
                                        "No files are marked for trashing in this group.")
                return
            if _confirm(self, targets, f"Trash {len(targets)} file(s) from group "
                                       f"{page.group.idx}?"):
                try:
                    self._do_trash(targets)
                except Exception as e:
                    QMessageBox.warning(self, "Trash failed",
                                        f"{type(e).__name__}: {e}")

        def _trash_group_only(self, gidx):
            """Per-group trash (header button): uses the group's full
            member list, so it is complete even for huge groups whose
            cards are still being shown in batches."""
            g = next((x for x in self.all_groups if x.idx == gidx), None)
            if g is None:
                return
            if gidx in self.keep_all:
                QMessageBox.information(self, "Keep all is on",
                    f"Group {gidx} is marked \"✓ keep all\" — nothing to "
                    "trash. Turn keep-all off first (restores the "
                    "default marks).")
                return
            page = _current_page(self.stacklay)
            stored = self.keep_map.get(g.idx) or default_keep_recs(g)
            stored = set(stored)
            if page is not None and page.group is g:
                # the live keep boxes on screen are the source of truth
                # for what is visible; the stored set covers the rest
                vis = {c.rec for c in page.cards}
                keeps = ({c.rec for c in page.cards
                          if c.keep_radio.isChecked()} | (stored - vis))
            else:
                keeps = stored
            targets = [m for m in g.members if m not in keeps]
            if not targets:
                QMessageBox.information(self, "Nothing to trash",
                                        "Every file in this group is "
                                        "marked keep.")
                return
            names = ", ".join(sorted(m.path.name for m in keeps)[:3])
            extra = (f" and {len(keeps) - 3:,} more"
                     if len(keeps) > 3 else "")
            if _confirm(self, targets,
                        f"Trash {len(targets):,} file(s) from group "
                        f"{gidx} only?\n\n"
                        f"{len(keeps):,} kept file(s) are NOT trashed: "
                        f"{names}{extra}"):
                try:
                    self._do_trash(targets)
                except Exception as e:
                    QMessageBox.warning(self, "Trash failed",
                                        f"{type(e).__name__}: {e}")

        def _trash_unkept(self):
            page = _current_page(self.stacklay)
            cur_idx = page.group.idx if page else None
            targets = []
            skipped = 0
            for g in self.visible:
                if g.idx in self.keep_all:
                    skipped += 1
                    continue
                if g.idx == cur_idx and page is not None:
                    # the WHOLE group on screen, not just the visible
                    # cards: the on-screen keep boxes are the source of
                    # truth for what is kept, the stored marks cover
                    # the cards off screen
                    stored = (self.keep_map.get(g.idx)
                              or default_keep_recs(g))
                    vis = {c.rec for c in page.cards}
                    keeps = ({c.rec for c in page.cards
                              if c.keep_radio.isChecked()}
                             | (stored - vis))
                    targets.extend(m for m in g.members
                                   if m not in keeps)
                else:
                    keeps = self.keep_map.get(g.idx) or default_keep_recs(g)
                    targets.extend(m for m in g.members if m not in keeps)
            if not targets:
                return
            # a file can sit in two groups at once (an OCR group and its
            # source group) — trash it only once
            targets = list(dict.fromkeys(targets))
            extra = (f"\n\n{skipped} group(s) marked \"✓ keep all\" are "
                     f"skipped.") if skipped else ""
            if _confirm(self, targets,
                        f"Trash {len(targets)} file(s) — every unkept "
                        f"file in every visible group?\n\n"
                        f"(the oldest file of each duplicate set is "
                        f"kept unless you changed the boxes){extra}"):
                try:
                    self._do_trash(targets)
                except Exception as e:
                    QMessageBox.warning(self, "Trash failed",
                                        f"{type(e).__name__}: {e}")

        def _export(self):
            fn, _ = QFileDialog.getSaveFileName(self, "Export CSV", str(self.root / "dupes.csv"))
            if fn:
                export_csv(self.visible, fn, self.keep_map)
                QMessageBox.information(self, "Exported", fn)

        def _do_trash(self, targets):
            """Trash in a background thread — hundreds of moves on a FUSE
            volume take a while and must not freeze the UI. Group updates
            happen on the main thread once the worker signals done."""
            if getattr(self, "_trash_running", False):
                QMessageBox.information(self, "Trash running",
                                        "A trash operation is already in "
                                        "progress — see the status bar.")
                return
            self._trash_running = True
            for b in self.findChildren(QPushButton):
                if b.objectName().startswith("trashBtn"):
                    b.setEnabled(False)
            self.status.showMessage(f"trashing {len(targets):,} file(s)…",
                                    8000)

            def work():
                try:
                    res = trash_files(targets,
                                      progress=self._trash_progress)
                except Exception as e:
                    # record EVERY target as failed — an empty failed
                    # list means "everything was trashed" and the report
                    # would claim success (the E2BIG bug: gio never even
                    # started, nothing moved, box said "Cleanup complete")
                    res = (False, "error", None,
                           [(t.path, f"{type(e).__name__}: {e}")
                            for t in targets])
                    print(f"trash worker failed: {type(e).__name__}: {e}",
                          flush=True)
                try:
                    self._trash_done_sig.emit((res, targets))
                except BaseException:
                    pass
            threading.Thread(target=work, daemon=True).start()

        def _trash_progress(self, done, total):
            try:
                self._trash_prog_sig.emit(done, total)
            except BaseException:
                pass

        def _trash_progress_cb(self, done, total):
            self.status.showMessage(
                f"trashing file {done + 1:,} of {total:,}…", 8000)

        def _trash_done(self, payload):
            (ok, method, where, failed), targets = payload
            self._trash_running = False
            for b in self.findChildren(QPushButton):
                if b.objectName().startswith("trashBtn"):
                    b.setEnabled(True)
            failed_set = set()
            for fp, _reason in failed:
                try:
                    failed_set.add(str(fp.resolve()))
                except OSError:
                    failed_set.add(str(fp))
            trashed = {str(t.path.resolve()) for t in targets
                       if str(t.path.resolve()) not in failed_set}
            if not trashed:
                # nothing moved — show exactly which files failed and why
                lines = [f"  {fp}\n    {reason}"
                         for fp, reason in failed[:25]]
                more = (f"\n  … and {len(failed) - 25} more file(s)"
                        if len(failed) > 25 else "")
                box = QMessageBox(self)
                box.setWindowTitle("Trash failed")
                box.setIcon(QMessageBox.Warning)
                box.setText("Could not move any files to trash."
                            if not failed else
                            f"Could not move {len(failed)} file(s) to trash:")
                box.setDetailedText("\n".join(lines) + more)
                box.exec()
                self._status()
                return
            freed = sum(t.size for t in targets
                        if str(t.path.resolve()) in trashed)
            # per-group breakdown for the report (before members are removed)
            by_group = {}
            for g in self.all_groups:
                hit = [m for m in g.members
                       if str(m.path.resolve()) in trashed]
                if hit:
                    by_group[g.idx] = (len(hit), sum(m.size for m in hit))
            for g in self.all_groups:
                new = []
                remap = {}
                for old_i, m in enumerate(g.members):
                    if str(m.path.resolve()) in trashed:
                        continue
                    remap[old_i] = len(new)
                    new.append(m)
                if remap:
                    # remap the cached layout instead of dropping it — a
                    # huge group must not re-run its multi-minute ordering
                    # after every trash
                    lay = getattr(g, "_match_layout", None)
                    if lay is not None:
                        g._match_layout = _remap_layout(lay, remap)
                    kix = g.__dict__.get("_match_keep_idxs")
                    if kix is not None:
                        g._match_keep_idxs = [remap[i] for i in kix
                                              if i in remap]
                        g._match_keep_recs = {new[i] for i in
                                              g._match_keep_idxs}
                g.members = new
            # pruned groups' keep marks must go with them (same idx-
            # reuse hazard as in _ocr_done)
            for g in self.all_groups:
                if len(g.members) < 2:
                    self.keep_map.pop(g.idx, None)
            self.all_groups = [g for g in self.all_groups if len(g.members) >= 2]
            self.recs = [r for r in self.recs
                         if str(r.path.resolve()) not in trashed]
            self._refilter()
            msg = f"trashed {len(trashed):,} file(s) via {method}"
            if failed:
                msg += f" · {len(failed):,} could not be trashed"
            if where:
                msg += f"  →  {where}"
            self.status.showMessage(msg, 8000)
            # end-of-cleanup report: how much was freed, and from where
            lines = [f"G{idx:02d}   {n:,} files   {human(sz)} freed"
                     for idx, (n, sz) in
                     sorted(by_group.items(), key=lambda kv: -kv[1][1])[:15]]
            more = (f"\n… and {len(by_group) - 15} more group(s)"
                    if len(by_group) > 15 else "")
            if failed:
                flines = [f"\n\nCould not be trashed ({len(failed):,}):\n"
                          + "\n".join(f"  {fp}\n    {reason}"
                                     for fp, reason in failed[:25])
                          + (f"\n  … and {len(failed) - 25} more file(s)"
                             if len(failed) > 25 else "")]
                more += "".join(flines)
            box = QMessageBox(self)
            box.setWindowTitle("Cleanup complete")
            box.setIcon(QMessageBox.Information)
            box.setText(f"Freed {human(freed)}\n\n"
                        f"{len(trashed):,} file(s) trashed from "
                        f"{len(by_group):,} group(s).")
            box.setDetailedText("\n".join(lines) + more)
            box.exec()
            self._status()


    def _mkbtn(bar, txt, fn, tip=""):
        btn = QPushButton(txt)
        btn.setToolTip(tip)
        btn.clicked.connect(fn)
        bar.addWidget(btn)
        return btn

    def _walk_cards(page):
        cards = []
        for c in page.findChildren(Card):
            cards.append(c)
        return cards

    def _current_page(stacklay):
        it = stacklay.itemAt(0)
        return it.widget() if it else None

    def _confirm(win, targets, msg):
        box = QMessageBox(win)
        box.setWindowTitle("Confirm trash")
        box.setText(msg)
        box.setDetailedText("\n".join(str(t) for t in targets[:40])
                            + (f"\n… and {len(targets)-40} more" if len(targets) > 40 else ""))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    _apply_kde_platform_theme()
    app = QApplication.instance() or QApplication(sys.argv)
    # dark palette for dialogs + dark caption bars on every window
    # (see _apply_dark_theme — without it a QMessageBox body falls back
    # to the system palette: white on a light Windows, white on white)
    _apply_dark_theme(app)
    # hover descriptions: hold the mouse over a button for 2 seconds
    # before the explanation appears (the user asked for a real wait,
    # so a casual hover never covers the buttons with text; 5 s was
    # tried first and felt too long)
    _wake = getattr(QStyle.StyleHint, "SH_ToolTip_WakeUpDelay",
                    None) or getattr(QStyle, "SH_ToolTipDelay", None)

    class _FastTipStyle(QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, result=None):
            if _wake is not None and int(hint) == int(_wake):
                return 2000
            return super().styleHint(hint, option, widget, result)
    app.setStyle(_FastTipStyle())
    app.setApplicationName("dupe-finder")
    app.setOrganizationName("deepseek-harness")
    app.setStyleSheet(STYLE_QSS)
    win = Main(root, include_hidden, all_dirs)
    win.showMaximized()   # full window so card grid + button bar fit
    return app.exec()

# --------------------------------------------------------------------- main

def _smb_name_matches(name: str, server: str, share: str) -> bool:
    # name like smb-share:server=192.168.1.45,share=backups (optionally
    # with ,user=... / ,port=...); host compared case-insensitively,
    # share exactly
    try:
        kv = {}
        for t in name.split(":", 1)[1].split(","):
            if "=" in t:
                k, v = t.split("=", 1)
                kv[k] = v
    except Exception:
        return False
    return (kv.get("server", "").lower() == server.lower()
            and kv.get("share") == share)


def _find_smb_mount(server: str, share: str, pats=None):
    """On-disk path of an already-connected SMB share (kio-fuse, gvfs
    or Dolphin mount points), or None.

    Two on-disk naming schemes exist: the kio-fuse layout
    <base>/smb/<server>/<share> and the gvfs/Dolphin layout
    <base>/smb-share:server=...,share=...
    """
    import glob
    cands = []
    if pats is None:
        uid = os.getuid()
        for base in glob.glob(f"/run/user/{uid}/kio-fuse-*"):
            cands.append(os.path.join(base, "smb", server, share))
            cands += glob.glob(os.path.join(base, "smb-share:*"))
        cands += glob.glob(f"/run/user/{uid}/gvfs/smb-share:*")
        cands += glob.glob("/run/media/*/*")
    else:
        for pat in pats:
            cands += glob.glob(pat)
    for c in cands:
        if not os.path.isdir(c):
            continue
        parts = c.split(os.sep)
        if (len(parts) >= 3 and "smb" in parts
                and parts[-2].lower() == server.lower()
                and parts[-1] == share):
            return Path(c)
        name = os.path.basename(c)
        if (name.startswith("smb-share:")
                and _smb_name_matches(name, server, share)):
            return Path(c)
    return None


def _smb_url_parts(d: str):
    """(server, share, subpath) if d holds an SMB location — smb://
    host/share, smb:/host/share, or the mangled picker result
    /home/user/smb:/host/share — else None.

    The mangled form is real: the native KDE picker turned
    smb://192.168.1.45/backups into /home/user/smb:/192.168.1.45/
    backups; the old app then printed 'not a directory' to an invisible
    console and the window vanished ('instant crash').
    """
    low = d.lower()
    i = low.find("smb://")
    j = low.find("smb:/")
    if j >= 0 and (i < 0 or j < i):
        i = j
    if i < 0:
        return None
    parts = d[i + 4:].lstrip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1], "/".join(parts[2:])


def _kiofuse_call(method: str, arg: str) -> str:
    """One blocking call on the session's kio-fuse daemon
    (org.kde.KIOFuse). Returns the string result, or '' when the
    daemon is absent or the call failed."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtDBus import (QDBus, QDBusConnection,
                                QDBusInterface, QDBusMessage)
    if QCoreApplication.instance() is None:
        QCoreApplication([])
    vfs = QDBusInterface("org.kde.KIOFuse", "/org/kde/KIOFuse",
                         "org.kde.KIOFuse.VFS",
                         QDBusConnection.sessionBus())
    if not vfs.isValid():
        return ""
    r = vfs.call(QDBus.CallMode.Block, method, arg)
    if r.type() == QDBusMessage.MessageType.ReplyMessage:
        return str(r.arguments()[0])
    return ""


def _try_connect_smb(server: str, share: str):
    """Connect an unconnected share. A KWallet-saved password is used
    silently; without one, KDE pops its normal password dialog.

    Primary: the session's kio-fuse daemon
    (org.kde.KIOFuse.VFS.mountUrl) — the same KIO machinery Dolphin
    uses, so no special package is needed on this machine; the share
    becomes a regular on-disk mount under
    /run/user/<uid>/kio-fuse-*/smb/<host>/<share>/.
    Fallback for non-KDE sessions: gvfs `gio mount` (needs the
    gvfs-smb package).
    """
    url = f"smb://{server}/{share}"
    try:
        if _kiofuse_call("mountUrl", url):
            return
    except Exception:
        pass
    try:
        subprocess.run(["gio", "mount", url], timeout=60,
                       capture_output=True)
    except Exception:
        pass


def _smb_root(d: str, pats=None):
    """Turn an SMB location (see _smb_url_parts) into its on-disk mount
    path. Returns None when it is not an SMB location or the share is
    still not connected after trying to connect it."""
    parts = _smb_url_parts(str(d))
    if parts is None:
        return None
    server, share, subpath = parts
    m = _find_smb_mount(server, share, pats)
    if m is None:
        _try_connect_smb(server, share)
        for _ in range(24):        # the mount point appears async
            m = _find_smb_mount(server, share, pats)
            if m is not None:
                break
            time.sleep(0.5)
    if m is None:
        return None
    return (m / subpath) if subpath else m


def _not_a_directory_dialog(root, smb_parts):
    """A failed launch used to print 'not a directory: …' to a console
    nobody can see and exit — the window just vanished. Show a dialog
    instead; for SMB locations it offers a Retry that re-connects and
    re-checks. Returns 'retry' or 'ok'."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None:
            return "ok"
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Duplicate Finder")
        if smb_parts:
            server, share, _sub = smb_parts
            box.setText(
                f"The SMB share smb://{server}/{share} is not connected "
                "(yet).\n\n"
                f"Connect it in Dolphin (Connect to Server, or type "
                f"smb://{server}/{share} in the location bar) and click "
                "Retry.")
            retry = box.addButton("Retry connect",
                                  QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            return "retry" if box.clickedButton() is retry else "ok"
        box.setText(f"That location does not exist (yet):\n\n{root}\n\n"
                    "If it is an SMB share, connect it first — Dolphin →"
                    " Connect to Server, or type smb://host/share in the"
                    " picker's location bar — then try again.")
        box.exec()
    except Exception:
        pass
    return "ok"


def main():
    # Windows consoles default to cp1252: let the report's emoji marks
    # degrade instead of crashing the --no-gui run (UnicodeEncodeError).
    if IS_WIN:
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(description="Find & verify duplicate images/videos")
    ap.add_argument("directory", nargs="?", default=None,
                    help="directory to scan (default: ask in a picker window)")
    ap.add_argument("--no-gui", action="store_true", help="print report instead of GUI")
    ap.add_argument("--csv", metavar="FILE", help="write CSV report (with --no-gui)")
    ap.add_argument("--include-hidden", action="store_true",
                    help="also scan hidden directories")
    ap.add_argument("--all-dirs", action="store_true",
                    help="do not skip node_modules/.venv/etc. at all")
    args = ap.parse_args()

    # KDE auto-loads its Plasma platform theme, which writes
    # $XDG_CONFIG_HOME/<appname>.rc via KConfig on exit. If ~/.config is not
    # writable (e.g. running under a read-only sandbox), KConfig pops a
    # "Configuration file not writable" dialog. Point KConfig at a temp dir.
    global SANDBOXED
    if IS_WIN:
        SANDBOXED = False
    else:
        _cfg = os.path.expanduser("~/.config")
        SANDBOXED = not (os.path.isdir(_cfg) and os.access(_cfg, os.W_OK))
        if SANDBOXED:
            import tempfile
            _base = tempfile.mkdtemp(prefix="dupe-finder-tmp-")
            os.environ["XDG_CONFIG_HOME"] = _base + "/config"
            os.environ["XDG_DATA_HOME"] = _base + "/data"

    if args.directory is None:
        from PySide6.QtWidgets import QApplication
        _apply_kde_platform_theme()
        app = QApplication.instance() or QApplication(sys.argv)
        app.setApplicationName("dupe-finder")
        app.setOrganizationName("deepseek-harness")
        d = pick_directory(None, "Choose the directory to scan for duplicates",
                           Path.home())
        if d is None:
            print("no directory selected — nothing to do")
            return
    else:
        d = args.directory
        if d.startswith("file://"):   # drag-and-drop onto a .desktop shortcut
            from urllib.parse import unquote
            d = unquote(d[len("file://"):])
        if not args.no_gui:
            # a dialog-capable app is needed if the location turns out to
            # be wrong (a silent exit with no message reads as an
            # "instant crash")
            from PySide6.QtWidgets import QApplication
            _apply_kde_platform_theme()
            app = QApplication.instance() or QApplication(sys.argv)
            app.setApplicationName("dupe-finder")
            app.setOrganizationName("deepseek-harness")
    # SMB shares: smb://host/share (or the mangled picker variant)
    # resolves to its on-disk mount path via gvfs / KIO / kio-fuse
    sm = _smb_root(str(d))
    root = sm if sm is not None else Path(d).expanduser().resolve()
    smb_parts = _smb_url_parts(str(d))
    while not root.is_dir():
        if args.no_gui:
            break
        if _not_a_directory_dialog(root, smb_parts) != "retry":
            break
        sm = _smb_root(str(d))
        if sm is not None:
            root = sm
    if not root.is_dir():
        sys.exit(f"not a directory: {root}")

    if args.no_gui:
        print(f"scanning {root} …")
        t0 = time.time()
        paths = scan_files(root, args.include_hidden, args.all_dirs)
        print(f"  {len(paths)} image/video files found")
        if not paths:
            sys.exit("nothing to do")
        _, recs = _fingerprint_with_cache(paths, print)
        _gp = {"last": -1}
        def _gp_print(done, total, detail):
            pct = done * 100 // total if total else 100
            if pct != _gp["last"]:
                _gp["last"] = pct
                print(f"grouping {pct}%  ({detail})")
        groups = build_groups(recs, _gp_print)
        dt = time.time() - t0
        red = redundant_bytes(groups)
        nexact = sum(1 for g in groups if g.exact)

        if args.csv:
            export_csv(groups, args.csv,
                       {g.idx: default_keep_recs(g) for g in groups})
            print(f"  csv written: {args.csv}")

        nskip = sum(1 for r in recs if r.error)
        tail = f", {nskip} skipped" if nskip else ""
        print(f"\ndone in {dt:.1f}s — {len(groups)} duplicate groups "
              f"({nexact} exact, {len(groups)-nexact} near), "
              f"{human(red)} redundant{tail}\n")
        for g in groups:
            tag = "exact" if g.exact else "near "
            print(f"  G{g.idx:3d} [{tag}] {g.kind:<5} x{len(g.members)}  "
                  f"keep: {g.members[0].path.name}")
            for m in g.members:
                mark = "  " if m is g.members[0] else "🗑"
                print(f"        {mark} {human(m.size):>9}  {m.path}")
        return

    # GUI: the window opens immediately with a scanning indicator; the scan
    # runs in a background thread and the results fill in when it finishes.
    rc = run_gui(root, args.include_hidden, args.all_dirs)
    # If the window closed mid-scan, the worker thread may still be
    # finishing; join it before the interpreter tears down (it executes
    # Python code in a C thread that shutdown would otherwise abandon).
    for w in list(_live_workers):
        w.wait()
        _live_workers.discard(w)
    sys.exit(rc)

if __name__ == "__main__":
    import multiprocessing
    # A frozen spawn child (PyInstaller) re-runs this module as __main__ —
    # there is no __mp_main__ re-import under freeze — so without this
    # guard every pool worker would run main() and sit blocked in the
    # modal directory-picker dialog, which is the 89%-forever hang on
    # Windows (all workers alive but no chunk ever completes). The
    # bootstrap names such a child "SpawnMain"; capture that BEFORE
    # freeze_support() diverts it into the worker loop, then skip main()
    # so the child does not open a ghost dialog after the loop ends.
    _is_pool_child = (multiprocessing.current_process().name
                      == "SpawnMain")
    multiprocessing.freeze_support()
    if not _is_pool_child:
        main()
