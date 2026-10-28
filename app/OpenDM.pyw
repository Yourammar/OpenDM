"""
OpenDM | Open source download manager with browser integration.

Features
  - Multi-connection downloads with pause/resume and dynamic segment splitting
  - Video/audio from 1000+ sites (YouTube, Instagram, X, Facebook, TikTok...) via yt-dlp
  - Browser extension button on videos and images, browser download takeover
  - Clipboard watching, download queue, speed limit, categories, tray icon

Run:  pythonw OpenDM.pyw          (or double-click the file on Windows)
      python  OpenDM.pyw --tray   (start hidden in the tray)

License: MIT
"""

import os
import sys

# When installed with the setup, a private Python runtime ships next to the app with
# FFmpeg and Deno (yt-dlp uses Deno for YouTube). Make its DLLs and tools findable
# before anything else is imported.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALL_DIR = os.path.dirname(APP_DIR)
RUNTIME_DIR = os.path.dirname(os.path.abspath(sys.executable))
for _extra in (os.path.join(RUNTIME_DIR, "Library", "bin"), os.path.join(RUNTIME_DIR, "deno"),
               os.path.join(RUNTIME_DIR, "ffmpeg")):
    if os.path.isdir(_extra):
        if _extra not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _extra + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory") and _extra.endswith("bin"):
            try:
                os.add_dll_directory(_extra)
            except OSError:
                pass
# the bundled Tcl/Tk would otherwise look for its scripts in the folder it was built in
for _var, _sub in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
    _p = os.path.join(RUNTIME_DIR, "Library", "lib", _sub)
    if os.path.isdir(_p):
        os.environ[_var] = _p

import http.server
import json
import logging
import mimetypes
import re
import shutil
import ssl
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from email.message import Message
from queue import Empty, Queue

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "OpenDM"
VERSION = "1.2.0"
HOST = "127.0.0.1"
PORT = 9614
IS_WIN = sys.platform.startswith("win")

DATA_DIR = os.path.join(os.path.expanduser("~"), ".opendm")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LIST_FILE = os.path.join(DATA_DIR, "downloads.json")
COOKIE_DIR = os.path.join(DATA_DIR, "cookies")
LOG_FILE = os.path.join(DATA_DIR, "opendm.log")
os.makedirs(COOKIE_DIR, exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(APP_NAME)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

DEFAULT_SETTINGS = {
    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads", "OpenDM"),
    "connections": 8,
    "max_parallel": 3,
    "speed_limit_kbps": 0,
    "clipboard_watch": True,
    "show_dialog": True,
    "use_categories": True,
    "close_to_tray": True,
    "start_with_windows": False,
    "completion_popup": True,
    "name_template": "{uploader} - {title} [{id}]",
    "if_exists": "rename",       # rename | overwrite
}

# Name patterns offered in Settings. {id} is the post/video ID, which is what keeps
# two Instagram videos from the same account apart.
NAME_PRESETS = [
    "{uploader} - {title} [{id}]",
    "{title} [{id}]",
    "{date} - {uploader} - {title} [{id}]",
    "{uploader} - {date} - {id}",
    "{site} - {uploader} - {id}",
    "{title}",
]
NAME_TOKENS = ("title", "uploader", "id", "date", "site")
NAME_SAMPLE = {"title": "Sunset over the Alps", "uploader": "natgeo", "id": "C8xYz12AbC",
               "date": "2026-10-27", "site": "Instagram"}

CATEGORIES = {
    "Video": ["mp4", "mkv", "webm", "avi", "mov", "flv", "wmv", "m4v", "3gp", "ts", "mpeg", "mpg"],
    "Music": ["mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "wma"],
    "Images": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "heic", "tif", "tiff", "ico"],
    "Compressed": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "img", "cab"],
    "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "epub", "odt", "rtf"],
    "Programs": ["exe", "msi", "apk", "dmg", "deb", "rpm", "appimage", "bat", "msix"],
}
ALL_EXTS = {e for exts in CATEGORIES.values() for e in exts}

CHUNK = 64 * 1024
MIN_SPLIT = 1024 * 1024  # do not split segments with less than 1 MB left


# --------------------------------------------------------------------------- helpers

def human_size(n):
    if n is None or n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0


def human_speed(bps):
    return f"{human_size(bps)}/s" if bps else ""


def human_eta(sec):
    if sec is None or sec <= 0 or sec > 360000:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def sanitize_filename(name, default="download"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    if not name:
        name = default
    base, ext = os.path.splitext(name)
    if base.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10))}:
        base = "_" + base
    return (base[:180] + ext[:20]) or default


def unique_path(path):
    if not os.path.exists(path) and not os.path.exists(path + ".opendm"):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}") or os.path.exists(f"{base} ({i}){ext}.opendm"):
        i += 1
    return f"{base} ({i}){ext}"


# Titles sites hand out that say nothing about the video ("Video by natgeo", "Instagram").
_GENERIC_TITLE = re.compile(
    r"^(?:(?:video|videos|reel|reels|post|photo|image|story|stories|clip|short|shorts|"
    r"tiktok|instagram|facebook|twitter|x)(?:\s+(?:by|from)\s+.*)?|untitled|no title|"
    r"[a-z0-9_.]+ on instagram)$", re.I)


def _clean_text(s, limit):
    """One tidy line: no links, hashtags, emoji-only noise or runs of spaces."""
    s = re.sub(r"https?://\S+", " ", s or "")
    s = re.sub(r"#[\w.]+", " ", s)
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s)
    s = re.sub(r"[^\w\s\-.,'!&()+@\[\]]", " ", s)   # drops emoji and symbols
    s = re.sub(r"\s+", " ", s).strip(" -_.,|'")
    if len(s) > limit:
        cut = s[:limit]
        s = (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).strip(" -_.,")
    return s


def media_fields(info):
    """Pull the values the name template can use out of a yt-dlp info dict."""
    info = info or {}
    vid = str(info.get("id") or "")
    uploader = _clean_text(info.get("uploader") or info.get("channel") or info.get("uploader_id")
                           or info.get("creator") or "", 40)
    title = _clean_text(info.get("title") or "", 80)
    if (not title or _GENERIC_TITLE.match(title)
            or title.lower() in (uploader.lower(), vid.lower())):
        # Instagram/TikTok: the real text is in the caption
        title = ""
        for line in (info.get("description") or "").splitlines():
            title = _clean_text(line, 80)
            if title and not _GENERIC_TITLE.match(title):
                break
            title = ""
    date = ""
    d = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", d):
        date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    elif info.get("timestamp"):
        try:
            date = time.strftime("%Y-%m-%d", time.localtime(int(info["timestamp"])))
        except Exception:
            pass
    site = _clean_text(info.get("extractor_key") or info.get("extractor") or "", 20)
    site = {"Youtube": "YouTube", "Tiktok": "TikTok", "Generic": ""}.get(site, site)
    return {"title": title, "uploader": uploader, "id": vid, "date": date, "site": site}


def render_name(template, fields):
    """Fill a template like '{uploader} - {title} [{id}]' and tidy up empty parts."""
    def sub(m):
        key = m.group(1).lower()
        return fields.get(key, "") if key in NAME_TOKENS else ""
    out = re.sub(r"\{(\w*)\}", sub, template or "")
    for _ in range(3):
        out = re.sub(r"\[\s*\]|\(\s*\)", "", out)              # empty brackets
        out = re.sub(r"(\s*[-_|]\s*){2,}", " - ", out)         # separators left by empty tokens
        out = re.sub(r"\s+[-_|]\s*(?=\[)", " ", out)            # 'natgeo - [id]' -> 'natgeo [id]'
    out = re.sub(r"\s+", " ", out).strip(" -_|.")
    return out


def media_name(info_or_fields, template):
    f = info_or_fields if "uploader" in info_or_fields and "site" in info_or_fields \
        else media_fields(info_or_fields)
    f = dict(f)
    up, ti = f.get("uploader", ""), f.get("title", "")
    if up and "{uploader}" in (template or "") and ti.lower().startswith(up.lower()):
        # 'Rick Astley - Rick Astley - Never Gonna...' -> 'Rick Astley - Never Gonna...'
        rest = ti[len(up):].lstrip(" -_|:.,")
        if rest:
            f["title"] = rest
    name = render_name(template, f)
    if len(name) > 150 and f.get("title"):              # keep paths under Windows limits
        f["title"] = _clean_text(f["title"], max(20, len(f["title"]) - (len(name) - 150)))
        name = render_name(template, f)
    if not name or not re.search(r"\w", name):
        name = render_name("{site} {uploader} {id}", f) or "video"
    return sanitize_filename(name[:150], "video")


class NameRegistry:
    """Hands out names that are free on disk AND not promised to another download.

    Checking the disk alone is not enough: two queued downloads with the same name both
    see a free name until one of them finishes. Every task holds a claim on its name
    until it completes, so a second task with the same name gets ' (2)' instead.
    Media names are compared by stem (name without extension), because the final
    extension (.mp4, .webm, .mp3) is only known after yt-dlp has finished.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.claims = {}   # task id -> (folder key, lowercase name, lowercase stem, by_stem)

    @staticmethod
    def _fkey(folder):
        return os.path.normcase(os.path.abspath(folder or ".")).lower()

    @staticmethod
    def _listing(folder):
        try:
            return {e.lower() for e in os.listdir(folder)}
        except OSError:
            return set()

    def _taken_by_task(self, owner, fkey, low, stem, by_stem):
        for tid, (f, n, s, st) in self.claims.items():
            if tid == owner or f != fkey:
                continue
            if by_stem or st:
                if s == stem:
                    return True
            elif n == low:
                return True
        return False

    @staticmethod
    def _taken_on_disk(listing, low, by_stem):
        if by_stem:
            # 'name.mp4', 'name.f137.mp4.part', 'name.temp.mp4', or a playlist folder 'name'
            return any(e == low or e.startswith(low + ".") for e in listing)
        return low in listing or (low + ".opendm") in listing or (low + ".part") in listing

    def claim(self, owner, folder, name, by_stem=False, check_disk=True):
        with self.lock:
            fkey = self._fkey(folder)
            listing = self._listing(folder) if check_disk else set()
            base, ext = (name, "") if by_stem else os.path.splitext(name)
            cand, i = name, 2
            while True:
                low = cand.lower()
                stem = low if by_stem else os.path.splitext(low)[0]
                if not (self._taken_by_task(owner, fkey, low, stem, by_stem)
                        or (check_disk and self._taken_on_disk(listing, low, by_stem))):
                    break
                cand = f"{base} ({i}){ext}"
                i += 1
            self.claims[owner] = (fkey, low, stem, by_stem)
            return cand

    def hold(self, owner, folder, name, by_stem=False):
        """Re-register a name already in use by a saved, unfinished task."""
        with self.lock:
            low = name.lower()
            self.claims[owner] = (self._fkey(folder), low,
                                  low if by_stem else os.path.splitext(low)[0], by_stem)

    def release(self, owner):
        with self.lock:
            self.claims.pop(owner, None)


NAMES = NameRegistry()


def ext_of(name):
    return os.path.splitext(name or "")[1].lower().lstrip(".")


def category_for(name, kind="file", audio_only=False):
    if kind == "media":
        return "Music" if audio_only else "Video"
    e = ext_of(name)
    for cat, exts in CATEGORIES.items():
        if e in exts:
            return cat
    return "Other"


def filename_from_response(url, headers):
    cd = headers.get("Content-Disposition")
    if cd:
        m = Message()
        m["content-disposition"] = cd
        fn = m.get_filename()
        if fn:
            return sanitize_filename(os.path.basename(fn))
    path = urllib.parse.urlparse(url).path
    fn = urllib.parse.unquote(os.path.basename(path))
    if not ext_of(fn):
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip()
        guess = mimetypes.guess_extension(ctype) if ctype else None
        if guess and ctype not in ("text/html", "text/plain"):
            fn = (fn or "download") + guess
    return sanitize_filename(fn)


def cookie_matches(cookie, host):
    d = (cookie.get("domain") or "").lstrip(".").lower()
    host = (host or "").lower()
    return bool(d) and (host == d or host.endswith("." + d))


def cookie_header(cookies, url):
    host = urllib.parse.urlparse(url).hostname or ""
    pairs, seen = [], set()
    for c in cookies or []:
        if cookie_matches(c, host) and c.get("name") not in seen:
            seen.add(c.get("name"))
            pairs.append(f"{c.get('name')}={c.get('value', '')}")
    return "; ".join(pairs)


def write_netscape_cookies(cookies, path):
    lines = ["# Netscape HTTP Cookie File", "# Written by OpenDM", ""]
    for c in cookies or []:
        domain = c.get("domain") or ""
        if not domain:
            continue
        host_only = c.get("hostOnly", False)
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        include_sub = "FALSE" if host_only else "TRUE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expiry = int(c.get("expirationDate") or 0)
        name, value = c.get("name", ""), c.get("value", "")
        if "\t" in name or "\n" in value:
            continue
        lines.append("\t".join([domain, include_sub, c.get("path") or "/", secure,
                                str(expiry), name, value]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def open_path(path):
    try:
        if IS_WIN:
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror(APP_NAME, f"Could not open:\n{path}\n\n{e}")


def show_in_folder(path):
    if IS_WIN and os.path.exists(path) and os.path.isfile(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        open_path(folder)


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    name = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    for local in (os.path.join(RUNTIME_DIR, "ffmpeg", name), os.path.join(DATA_DIR, "ffmpeg", name)):
        if os.path.exists(local):
            return local
    return None


def ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_context()))


def http_open(url, headers, rng=None, timeout=25):
    h = dict(headers)
    if rng:
        h["Range"] = rng
    h.setdefault("Accept-Encoding", "identity")
    req = urllib.request.Request(url, headers=h)
    return OPENER.open(req, timeout=timeout)


# --------------------------------------------------------------------------- yt-dlp glue

_YTDLP = None
_EXTRACTORS = None


def ytdlp():
    global _YTDLP
    if _YTDLP is None:
        try:
            import yt_dlp
            _YTDLP = yt_dlp
        except Exception:
            _YTDLP = False
    return _YTDLP or None


def is_media_site(url):
    """True when a dedicated yt-dlp extractor (not the generic one) handles the URL."""
    global _EXTRACTORS
    y = ytdlp()
    if not y or not url.startswith(("http://", "https://")):
        return False
    if _EXTRACTORS is None:
        try:
            _EXTRACTORS = [ie for ie in y.extractor.gen_extractor_classes()
                           if ie.IE_NAME != "generic" and getattr(ie, "_VALID_URL", None)]
        except Exception:
            _EXTRACTORS = []
    for ie in _EXTRACTORS:
        try:
            if ie.suitable(url):
                return True
        except Exception:
            pass
    return False


class YtdlpLogger:
    def __init__(self):
        self.skipped = []      # files yt-dlp did not download because they already existed

    def debug(self, msg):
        # yt-dlp skips a download silently when the output file already exists
        m = re.search(r"\[download\]\s+(.+?)\s+has already been downloaded", msg or "")
        if m:
            self.skipped.append(os.path.basename(m.group(1)))
            log.info("yt-dlp: %s", msg)

    def skipped_final(self, stem):
        """True when the finished file itself (not a resumed video/audio part) was skipped."""
        pat = re.compile(re.escape(stem) + r"\.[A-Za-z0-9]{2,5}$", re.I)
        return any(pat.fullmatch(s) for s in self.skipped)

    def info(self, msg):
        pass

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)


# --------------------------------------------------------------------------- speed limiter

class SpeedLimiter:
    """Global token bucket shared by every download thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rate = 0
        self.tokens = 0.0
        self.last = time.monotonic()

    def set_rate(self, kbps):
        self.rate = max(0, int(kbps)) * 1024

    def consume(self, n):
        if not self.rate:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n or self.tokens >= self.rate:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            time.sleep(min(wait, 0.25))


LIMITER = SpeedLimiter()


# --------------------------------------------------------------------------- task model

class Task:
    FIELDS = ["id", "kind", "url", "page_url", "referer", "user_agent", "filename", "folder",
              "size", "resumable", "segments", "status", "error", "added", "finished",
              "media_format", "audio_only", "playlist", "cookie_file", "filepath", "title",
              "connections", "base_name", "stem", "started", "overwrite"]

    def __init__(self, **kw):
        self.id = kw.get("id") or uuid.uuid4().hex[:12]
        self.kind = kw.get("kind", "file")            # file | media
        self.url = kw.get("url", "")
        self.page_url = kw.get("page_url", "")
        self.referer = kw.get("referer", "")
        self.user_agent = kw.get("user_agent") or DEFAULT_UA
        self.filename = kw.get("filename", "download")
        self.folder = kw.get("folder", "")
        self.size = kw.get("size")                    # bytes or None
        self.resumable = kw.get("resumable", False)
        self.segments = kw.get("segments") or []      # [{"s":start,"e":end,"p":pos}]
        self.status = kw.get("status", "queued")      # queued downloading paused complete error
        self.error = kw.get("error", "")
        self.added = kw.get("added") or time.time()
        self.finished = kw.get("finished")
        self.media_format = kw.get("media_format", "best")
        self.audio_only = kw.get("audio_only", False)
        self.playlist = kw.get("playlist", False)
        self.cookie_file = kw.get("cookie_file", "")
        self.filepath = kw.get("filepath", "")
        self.title = kw.get("title", "")
        self.connections = kw.get("connections", 8)
        # naming: base_name is what was asked for; files keep their real name in
        # `filename`, media keep the reserved name without extension in `stem`
        self.base_name = kw.get("base_name") or self.filename
        stem = kw.get("stem")
        if not stem and self.kind == "media":       # tasks saved by OpenDM 1.1
            stem = os.path.splitext(self.filename)[0] if self.status == "complete" else self.filename
        self.stem = stem or ""
        started = kw.get("started")
        if started is None:                         # tasks saved by OpenDM 1.1
            started = self.status != "queued" or bool(self.segments)
        self.started = started
        self.overwrite = kw.get("overwrite", False)
        # runtime only
        self.lock = threading.Lock()
        self.stop_flag = None                         # None | "pause" | "cancel"
        self.thread = None
        self.media_done = 0
        self.media_total = None
        self.media_note = ""
        self.speed = 0.0
        self._last_done = None
        self._last_t = None

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @property
    def path(self):
        return os.path.join(self.folder, self.filename)

    @property
    def part_path(self):
        return self.path + ".opendm"

    @property
    def done(self):
        if self.kind == "media":
            return self.media_done
        total = 0
        for seg in self.segments:
            end = seg["e"] if seg["e"] >= 0 else seg["p"] - 1
            total += max(0, min(seg["p"], end + 1) - seg["s"])
        return total

    @property
    def total(self):
        return self.media_total if self.kind == "media" else self.size

    def progress(self):
        if self.status == "complete":
            return 100.0
        t = self.total
        return (self.done / t * 100.0) if t else None

    def update_speed(self):
        now = time.monotonic()
        d = self.done
        if self.status != "downloading":
            self.speed, self._last_done, self._last_t = 0.0, None, None
            return
        if self._last_t is not None and now > self._last_t:
            inst = max(0, d - self._last_done) / (now - self._last_t)
            self.speed = inst if self.speed == 0 else self.speed * 0.7 + inst * 0.3
        self._last_done, self._last_t = d, now

    def eta(self):
        t = self.total
        if not t or not self.speed:
            return None
        return (t - self.done) / self.speed


# --------------------------------------------------------------------------- probing

def probe_file(url, headers):
    """Find size, resume support and file name for a direct link."""
    final_url, size, resumable, filename, ctype = url, None, False, None, ""
    try:
        resp = http_open(url, headers, rng="bytes=0-0")
    except urllib.error.HTTPError as e:
        if e.code == 416:
            resp = http_open(url, headers)
        else:
            raise
    with resp:
        final_url = resp.geturl()
        ctype = resp.headers.get("Content-Type", "")
        if resp.status == 206:
            cr = resp.headers.get("Content-Range", "")
            m = re.search(r"/(\d+)\s*$", cr)
            if m:
                size = int(m.group(1))
                resumable = True
        else:
            cl = resp.headers.get("Content-Length")
            size = int(cl) if cl and cl.isdigit() else None
        filename = filename_from_response(final_url, resp.headers)
    return {"final_url": final_url, "size": size, "resumable": resumable,
            "filename": filename, "content_type": ctype}


def probe_media(url, cookie_file, headers):
    y = ytdlp()
    if not y:
        raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -U yt-dlp")
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": "in_playlist", "logger": YtdlpLogger(),
        "http_headers": headers, "noplaylist": False,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    with y.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    is_playlist = info.get("_type") in ("playlist", "multi_video") and info.get("entries") is not None
    entries = list(info.get("entries") or []) if is_playlist else []
    if is_playlist and len(entries) == 1 and entries[0].get("formats"):
        info, is_playlist, entries = entries[0], False, []
    ffmpeg = find_ffmpeg()
    heights = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        has_v = f.get("vcodec") not in (None, "none")
        has_a = f.get("acodec") not in (None, "none")
        if h and has_v and (ffmpeg or has_a):
            heights.add(int(h))
    title = info.get("title") or info.get("id") or "video"
    return {
        "title": title, "name_fields": media_fields(info),
        "playlist": is_playlist, "count": len(entries),
        "heights": sorted(heights, reverse=True), "duration": info.get("duration"),
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "webpage_url": info.get("webpage_url") or url,
        "filesize": info.get("filesize") or info.get("filesize_approx"),
    }


def media_format_string(choice, ffmpeg):
    """choice: 'best' | 'audio' | '<height>'"""
    if choice == "audio":
        return "ba[ext=m4a]/ba/b"
    if choice == "best":
        return "bv*+ba/b" if ffmpeg else "b/bv*+ba"
    h = int(choice)
    if ffmpeg:
        return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
    return f"b[height<={h}]/b"


# --------------------------------------------------------------------------- workers

class FileWorker:
    def __init__(self, task, on_done):
        self.t = task
        self.on_done = on_done
        self.errors = []

    def headers(self):
        h = {"User-Agent": self.t.user_agent, "Accept": "*/*"}
        if self.t.referer:
            h["Referer"] = self.t.referer
        ck = getattr(self.t, "_cookie_header", "")
        if ck:
            h["Cookie"] = ck
        return h

    def run(self):
        t = self.t
        try:
            os.makedirs(t.folder, exist_ok=True)
            fresh = not os.path.exists(t.part_path)
            if fresh and not t.overwrite:
                # a file with this name may have appeared since the task was queued
                t.filename = NAMES.claim(t.id, t.folder, t.base_name or t.filename)
            t.started = True
            if fresh or not t.resumable or not t.segments:
                t.segments = self.plan()
                with open(t.part_path, "wb") as f:
                    if t.resumable and t.size:
                        f.truncate(t.size)
            threads = []
            for seg in list(t.segments):
                if seg["e"] < 0 or seg["p"] <= seg["e"]:
                    th = threading.Thread(target=self.segment_loop, args=(seg,), daemon=True)
                    th.start()
                    threads.append(th)
            # extra threads that only steal work
            want = t.connections if t.resumable else 1
            for _ in range(max(0, want - len(threads))):
                th = threading.Thread(target=self.segment_loop, args=(None,), daemon=True)
                th.start()
                threads.append(th)
            for th in threads:
                th.join()

            if t.stop_flag == "pause":
                t.status = "paused"
            elif t.stop_flag == "cancel":
                t.status = "paused"
            elif self.complete():
                if t.size is None:
                    t.size = t.done
                final = t.path
                if os.path.exists(final) and not t.overwrite:
                    final = os.path.join(t.folder, NAMES.claim(
                        t.id, t.folder, t.base_name or t.filename))
                os.replace(t.part_path, final)
                t.filename = os.path.basename(final)
                t.filepath = final
                t.status = "complete"
                t.finished = time.time()
            else:
                t.status = "error"
                t.error = self.errors[-1] if self.errors else "Download incomplete"
        except Exception as e:
            log.exception("file task failed")
            t.status = "error"
            t.error = str(e)
        finally:
            self.on_done(t)

    def plan(self):
        t = self.t
        if not (t.resumable and t.size):
            return [{"s": 0, "e": (t.size - 1) if t.size else -1, "p": 0}]
        n = max(1, min(t.connections, 32))
        if t.size < n * MIN_SPLIT:
            n = max(1, t.size // MIN_SPLIT)
        step = t.size // n
        segs = []
        for i in range(n):
            s = i * step
            e = t.size - 1 if i == n - 1 else (i + 1) * step - 1
            segs.append({"s": s, "e": e, "p": s})
        return segs

    def complete(self):
        t = self.t
        if not t.segments:
            return False
        for seg in t.segments:
            if seg["e"] >= 0 and seg["p"] <= seg["e"]:
                return False
            if seg["e"] < 0 and not seg.get("eof"):
                return False
        return True

    def steal(self):
        """Split the segment with the most bytes left (IDM style dynamic segmentation)."""
        t = self.t
        with t.lock:
            best, best_rem = None, 0
            for seg in t.segments:
                if seg["e"] < 0:
                    continue
                rem = seg["e"] - seg["p"] + 1
                if rem > best_rem:
                    best, best_rem = seg, rem
            if not best or best_rem < 2 * MIN_SPLIT:
                return None
            mid = best["p"] + best_rem // 2
            new = {"s": mid, "e": best["e"], "p": mid}
            best["e"] = mid - 1
            t.segments.append(new)
            return new

    def segment_loop(self, seg):
        t = self.t
        if seg is None:
            seg = self.steal() if t.resumable else None
        while seg is not None and t.stop_flag is None:
            self.download_segment(seg)
            if t.stop_flag is not None or not t.resumable:
                return
            if seg["e"] >= 0 and seg["p"] <= seg["e"]:
                return  # failed after retries
            seg = self.steal()

    def download_segment(self, seg):
        t = self.t
        attempts = 0
        while t.stop_flag is None:
            if seg["e"] >= 0 and seg["p"] > seg["e"]:
                return
            try:
                if t.resumable:
                    rng = f"bytes={seg['p']}-{seg['e']}"
                else:
                    seg["p"] = 0
                    rng = None
                with http_open(t.url, self.headers(), rng=rng) as resp:
                    if t.resumable and resp.status != 206:
                        raise RuntimeError("Server stopped supporting resume")
                    mode = "r+b" if os.path.exists(t.part_path) else "wb"
                    with open(t.part_path, mode) as f:
                        if not t.resumable:
                            f.truncate(0)
                        f.seek(seg["p"])
                        while t.stop_flag is None:
                            end = seg["e"]
                            want = CHUNK if end < 0 else min(CHUNK, end - seg["p"] + 1)
                            if want <= 0:
                                return
                            data = resp.read(want)
                            if not data:
                                if end < 0:
                                    seg["eof"] = True
                                    return
                                raise RuntimeError("Connection closed early")
                            LIMITER.consume(len(data))
                            f.write(data)
                            with t.lock:
                                seg["p"] += len(data)
                            attempts = 0
                return
            except Exception as e:
                attempts += 1
                msg = str(e)
                if isinstance(e, urllib.error.HTTPError):
                    msg = f"HTTP {e.code} {e.reason}"
                    if e.code in (401, 403, 404, 410):
                        attempts = 99
                log.warning("segment error (%s): %s", attempts, msg)
                if attempts > 6:
                    self.errors.append(msg)
                    return
                for _ in range(int(min(2 ** attempts, 20) * 4)):
                    if t.stop_flag is not None:
                        return
                    time.sleep(0.25)


class MediaWorker:
    def __init__(self, task, on_done):
        self.t = task
        self.on_done = on_done

    def hook(self, d):
        t = self.t
        if t.stop_flag is not None:
            raise ytdlp().utils.DownloadCancelled("stopped by user")
        if d.get("status") == "downloading":
            t.media_done = d.get("downloaded_bytes") or 0
            t.media_total = d.get("total_bytes") or d.get("total_bytes_estimate")
            info = d.get("info_dict") or {}
            idx, n = info.get("playlist_index"), info.get("n_entries")
            fid = info.get("format_id", "")
            note = f"item {idx}/{n}" if idx and n else ""
            if "+" in (info.get("format_id") or "") or d.get("filename", "").endswith(".part"):
                pass
            t.media_note = note or fid
        elif d.get("status") == "finished":
            t.media_done = t.media_total or t.media_done

    def run(self):
        t = self.t
        y = ytdlp()
        try:
            if not y:
                raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -U yt-dlp")
            os.makedirs(t.folder, exist_ok=True)
            ffmpeg = find_ffmpeg()
            if not t.stem:
                t.stem = sanitize_filename(t.base_name or t.filename, "video")
            first_run = not t.started
            if first_run and not t.overwrite:
                # first run: make sure nothing took this name while the task was queued
                t.stem = NAMES.claim(t.id, t.folder, t.base_name or t.stem, by_stem=True)
            t.started = True
            info = None
            for attempt in range(3):
                t.filename = t.stem
                logger = YtdlpLogger()
                info = self.download(y, ffmpeg, logger)
                if t.stop_flag is not None:
                    t.status = "paused"
                    return
                # Safety net: if yt-dlp found a file with our name and skipped the download,
                # take the next free name and download for real instead of "pretending".
                if (first_run and not t.playlist and not t.overwrite
                        and logger.skipped_final(t.stem)):
                    log.info("name '%s' was taken, choosing another", t.stem)
                    t.stem = NAMES.claim(t.id, t.folder, t.base_name or t.stem, by_stem=True)
                    continue
                break
            path = ""
            reqs = (info or {}).get("requested_downloads") or []
            if reqs:
                path = reqs[0].get("filepath") or ""
            if t.playlist:
                path = os.path.join(t.folder, t.stem)
            t.filepath = path
            if path and os.path.isfile(path):
                t.filename = os.path.basename(path)
                t.media_total = t.media_done = os.path.getsize(path)
            t.status = "complete"
            t.finished = time.time()
        except Exception as e:
            if t.stop_flag is not None:
                t.status = "paused"
            else:
                log.exception("media task failed")
                t.status = "error"
                t.error = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        finally:
            self.on_done(t)

    def download(self, y, ffmpeg, logger):
        t = self.t
        if t.playlist:
            sub = os.path.join(t.folder, t.stem)
            # every item carries its own ID, so two clips with the same title never collide
            outtmpl = os.path.join(sub, "%(playlist_index)03d - %(title).100B [%(id)s].%(ext)s")
        else:
            outtmpl = os.path.join(t.folder, t.stem + ".%(ext)s")
        headers = {"User-Agent": t.user_agent}
        if t.referer:
            headers["Referer"] = t.referer
        opts = {
            "outtmpl": outtmpl, "format": media_format_string(t.media_format, ffmpeg),
            "progress_hooks": [self.hook], "quiet": True, "no_warnings": True,
            "noprogress": True, "continuedl": True, "noplaylist": not t.playlist,
            "windowsfilenames": True, "logger": logger, "http_headers": headers,
            "concurrent_fragment_downloads": max(1, min(t.connections, 16)),
            "retries": 10, "fragment_retries": 10,
        }
        if t.overwrite and not t.playlist:
            opts["overwrites"] = True
        if LIMITER.rate:
            opts["ratelimit"] = LIMITER.rate
        if t.cookie_file and os.path.exists(t.cookie_file):
            opts["cookiefile"] = t.cookie_file
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
            if t.audio_only:
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                           "preferredcodec": "mp3", "preferredquality": "192"}]
            else:
                opts["merge_output_format"] = "mp4"
        with y.YoutubeDL(opts) as ydl:
            return ydl.extract_info(t.page_url or t.url, download=True)


# --------------------------------------------------------------------------- browser bridge

class BridgeHandler(http.server.BaseHTTPRequestHandler):
    app = None
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True  # local programs, not web pages
        return origin.startswith(("chrome-extension://", "moz-extension://", "extension://"))

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(403, {"ok": False})

    def do_GET(self):
        if not self._host_ok() or not self._origin_ok():
            return self._send(403, {"ok": False, "error": "forbidden"})
        if self.path == "/ping":
            return self._send(200, {"ok": True, "app": APP_NAME, "version": VERSION})
        if self.path == "/show":
            self.app.post(self.app.show_window)
            return self._send(200, {"ok": True})
        self._send(404, {"ok": False})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._send(403, {"ok": False, "error": "forbidden"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"ok": False, "error": "json only"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                return self._send(413, {"ok": False})
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"ok": False, "error": "bad json"})
        if self.path == "/quit":
            # used by the installer/uninstaller; needs a custom header and no Origin,
            # which a web page cannot send without a blocked CORS preflight
            if self.headers.get("Origin") or self.headers.get("X-OpenDM") != "quit":
                return self._send(403, {"ok": False})
            self.app.post(self.app.quit)
            return self._send(200, {"ok": True})
        if self.path == "/download":
            url = str(data.get("url") or "")
            page = str(data.get("page_url") or "")
            if not (url.startswith(("http://", "https://")) or page.startswith(("http://", "https://"))):
                return self._send(400, {"ok": False, "error": "no usable url"})
            self.app.post(lambda: self.app.handle_request(data))
            return self._send(200, {"ok": True})
        self._send(404, {"ok": False})


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not IS_WIN  # Windows reuse would let other apps hijack the port


# --------------------------------------------------------------------------- GUI

class App:
    def __init__(self, start_hidden=False):
        self.settings = self.load_settings()
        LIMITER.set_rate(self.settings["speed_limit_kbps"])
        self.tasks = {}
        self.order = []
        self.uiq = Queue()
        self.dirty = False
        self.tray = None
        self.last_clip = None
        self.progress_windows = {}

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} | Download Manager")
        self.root.geometry("1060x540")
        self.root.minsize(720, 360)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.tk_error
        self.style()
        self.icon_img = self.make_icon()
        if self.icon_img:
            try:
                self.root.iconphoto(True, self.icon_img)
            except Exception:
                pass
        self.build_ui()
        self.load_list()
        self.start_server()
        self.root.after(100, self.pump)
        self.root.after(500, self.tick)
        self.root.after(1500, self.clip_tick)
        try:
            self.last_clip = self.root.clipboard_get()
        except Exception:
            self.last_clip = None
        if start_hidden and self.setup_tray():
            self.root.withdraw()

    # ---- infrastructure
    def post(self, fn):
        self.uiq.put(fn)

    def pump(self):
        try:
            while True:
                fn = self.uiq.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("ui callback failed")
        except Empty:
            pass
        self.root.after(100, self.pump)

    def tk_error(self, *args):
        log.error("Tk error: %s", "".join(traceback.format_exception(*args)))

    def style(self):
        st = ttk.Style(self.root)
        for theme in ("vista", "winnative", "clam"):
            if theme in st.theme_names():
                st.theme_use(theme)
                break
        st.configure("Treeview", rowheight=24)
        st.configure("Tool.TButton", padding=(6, 4))
        st.configure("Accent.TButton", padding=(14, 6))

    def make_icon(self, size=64):
        try:
            from PIL import Image, ImageDraw, ImageTk
            img = self.icon_pil(size)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    @staticmethod
    def icon_pil(size=64):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((2, 2, size - 3, size - 3), fill=(26, 115, 232, 255))
        s = size / 64.0
        d.rectangle((27 * s, 14 * s, 37 * s, 34 * s), fill="white")
        d.polygon([(17 * s, 32 * s), (47 * s, 32 * s), (32 * s, 48 * s)], fill="white")
        d.rectangle((18 * s, 50 * s, 46 * s, 54 * s), fill="white")
        return img

    # ---- settings / persistence
    def load_settings(self):
        s = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                s.update(json.load(f))
        except Exception:
            pass
        return s

    def save_settings(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=2)

    def load_list(self):
        try:
            with open(LIST_FILE, encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            items = []
        for d in items:
            t = Task(**d)
            if t.status in ("downloading", "queued"):
                t.status = "paused"
            if t.status != "complete":
                if t.kind == "media":
                    NAMES.hold(t.id, t.folder, t.stem or t.filename, by_stem=True)
                else:
                    NAMES.hold(t.id, t.folder, t.filename)
            self.add_task(t, save=False)

    def save_list(self):
        data = [self.tasks[i].to_dict() for i in self.order if i in self.tasks]
        tmp = LIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, LIST_FILE)
        self.dirty = False

    # ---- server
    def start_server(self):
        BridgeHandler.app = self
        try:
            self.server = ThreadingServer((HOST, PORT), BridgeHandler)
        except OSError:
            self.server = None
            self.status_var.set(f"Browser link OFF | port {PORT} is in use by another program")
            return
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.status_var.set(f"Browser link ON | listening on {HOST}:{PORT}")

    # ---- UI
    def build_ui(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        buttons = [
            ("Add URL", self.add_url_dialog), ("Resume", self.resume_selected),
            ("Pause", self.pause_selected), ("Pause all", self.pause_all),
            ("Delete", self.delete_selected), ("Open", self.open_selected),
            ("Folder", self.folder_selected), ("Settings", self.settings_dialog),
            ("Browser extension", self.extension_setup), ("About", self.about),
        ]
        for text, cmd in buttons:
            ttk.Button(bar, text=text, command=cmd, style="Tool.TButton",
                       width=len(text) + 2).pack(side="left", padx=2)

        body = ttk.Frame(self.root, padding=(8, 0, 8, 0))
        body.pack(fill="both", expand=True)
        cols = ("name", "size", "status", "speed", "eta", "added")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
        heads = {"name": ("File name", 360), "size": ("Size", 90), "status": ("Status", 150),
                 "speed": ("Speed", 100), "eta": ("Time left", 80), "added": ("Added", 120)}
        for c in cols:
            title, w = heads[c]
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor="w" if c == "name" else "center",
                             stretch=(c == "name"))
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("complete", foreground="#1e7e34")
        self.tree.tag_configure("error", foreground="#c62828")
        self.tree.tag_configure("paused", foreground="#8a6d00")
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Button-3>", self.on_right)
        self.tree.bind("<Delete>", lambda e: self.delete_selected())

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Open", command=self.open_selected)
        self.menu.add_command(label="Open folder", command=self.folder_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Resume", command=self.resume_selected)
        self.menu.add_command(label="Pause", command=self.pause_selected)
        self.menu.add_command(label="Redownload", command=self.redownload_selected)
        self.menu.add_command(label="Show progress", command=self.progress_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Copy address", command=self.copy_url)
        self.menu.add_command(label="Remove from list", command=self.delete_selected)
        self.menu.add_command(label="Delete file too", command=lambda: self.delete_selected(True))

        foot = ttk.Frame(self.root, padding=(8, 4))
        foot.pack(fill="x")
        self.status_var = tk.StringVar(value="Starting...")
        self.total_var = tk.StringVar(value="")
        ttk.Label(foot, textvariable=self.status_var).pack(side="left")
        ttk.Label(foot, textvariable=self.total_var).pack(side="right")

    def row_values(self, t):
        pct = t.progress()
        if t.status == "downloading":
            st = f"{pct:.1f}%" if pct is not None else f"{human_size(t.done)}"
            if t.kind == "media" and t.media_note.startswith("item"):
                st += f" | {t.media_note}"
        elif t.status == "complete":
            st = "Complete"
        elif t.status == "error":
            st = "Error"
        elif t.status == "paused":
            st = f"Paused {pct:.0f}%" if pct else "Paused"
        else:
            st = "Queued"
        size = t.total if t.status != "complete" or t.kind == "file" else t.media_total
        name = t.filename
        if t.kind == "media" and t.status != "complete":
            name = f"{t.filename}  [{'audio' if t.audio_only else 'video'}]"
        return (name, human_size(size) if size else "", st,
                human_speed(t.speed) if t.status == "downloading" else "",
                human_eta(t.eta()) if t.status == "downloading" else "",
                time.strftime("%b %d %H:%M", time.localtime(t.added)))

    def add_task(self, t, save=True):
        self.tasks[t.id] = t
        self.order.append(t.id)
        self.tree.insert("", 0, iid=t.id, values=self.row_values(t), tags=(t.status,))
        if save:
            self.save_list()

    def selected(self):
        return [self.tasks[i] for i in self.tree.selection() if i in self.tasks]

    # ---- main loop
    def tick(self):
        active = 0
        total_speed = 0.0
        for tid in list(self.order):
            t = self.tasks.get(tid)
            if not t:
                continue
            t.update_speed()
            if t.status == "downloading":
                active += 1
                total_speed += t.speed
            if self.tree.exists(tid):
                self.tree.item(tid, values=self.row_values(t), tags=(t.status,))
        # start queued tasks
        slots = self.settings["max_parallel"] - active
        if slots > 0:
            for tid in self.order:
                t = self.tasks.get(tid)
                if t and t.status == "queued" and slots > 0:
                    self.start_task(t)
                    slots -= 1
        self.total_var.set(f"{active} active | {human_speed(total_speed) or '0 B/s'}")
        if self.tray:
            try:
                self.tray.title = f"{APP_NAME} | {active} active"
            except Exception:
                pass
        for tid, win in list(self.progress_windows.items()):
            win.refresh()
        if self.dirty or active:
            self._save_counter = getattr(self, "_save_counter", 0) + 1
            if self.dirty or self._save_counter % 6 == 0:
                try:
                    self.save_list()
                except Exception:
                    log.exception("save failed")
        self.root.after(500, self.tick)

    def start_task(self, t):
        if t.thread and t.thread.is_alive():
            return
        t.stop_flag = None
        t.status = "downloading"
        t.error = ""
        t.connections = self.settings["connections"]
        if t.kind == "file":
            cookies = []
            if t.cookie_file and os.path.exists(t.cookie_file + ".json"):
                try:
                    with open(t.cookie_file + ".json", encoding="utf-8") as f:
                        cookies = json.load(f)
                except Exception:
                    cookies = []
            t._cookie_header = cookie_header(cookies, t.url)
            worker = FileWorker(t, self.task_finished)
        else:
            worker = MediaWorker(t, self.task_finished)
        t.thread = threading.Thread(target=worker.run, daemon=True)
        t.thread.start()
        self.dirty = True

    def task_finished(self, t):
        def ui():
            self.dirty = True
            if t.status == "complete":
                NAMES.release(t.id)          # the file itself now holds the name on disk
                self.cleanup_cookies(t)
                if self.settings["completion_popup"]:
                    CompletePopup(self, t)
            elif t.status == "error":
                log.info("task %s error: %s", t.id, t.error)
        self.post(ui)

    def cleanup_cookies(self, t):
        for p in (t.cookie_file, t.cookie_file + ".json" if t.cookie_file else ""):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    # ---- actions
    def resume_selected(self):
        for t in self.selected():
            if t.status in ("paused", "error"):
                t.status = "queued"
        self.dirty = True

    def pause_selected(self):
        for t in self.selected():
            self.pause(t)

    def pause_all(self):
        for t in self.tasks.values():
            self.pause(t)

    def pause(self, t):
        if t.status == "downloading":
            t.stop_flag = "pause"
        elif t.status == "queued":
            t.status = "paused"
        self.dirty = True

    def redownload_selected(self):
        for t in self.selected():
            if t.status == "downloading":
                continue
            if t.kind == "file":
                if os.path.exists(t.part_path):
                    os.remove(t.part_path)
                t.segments = []
                NAMES.hold(t.id, t.folder, t.filename)
            else:
                NAMES.hold(t.id, t.folder, t.stem or t.filename, by_stem=True)
            # a fresh start picks a new free name (or overwrites, if that is the setting),
            # instead of yt-dlp quietly skipping because the old file is still there
            t.started = False
            t.media_done = 0
            t.status = "queued"
        self.dirty = True

    def delete_selected(self, with_file=False):
        sel = self.selected()
        if not sel:
            return
        msg = f"Remove {len(sel)} item(s) from the list?"
        if with_file:
            msg = f"Remove {len(sel)} item(s) and DELETE the downloaded files?"
        if not messagebox.askyesno(APP_NAME, msg, parent=self.root):
            return
        for t in sel:
            t.stop_flag = "cancel"

        def finish(items=sel):
            for t in items:
                if t.thread and t.thread.is_alive():
                    self.root.after(300, finish)
                    return
            for t in items:
                if t.kind == "file" and os.path.exists(t.part_path):
                    try:
                        os.remove(t.part_path)
                    except Exception:
                        pass
                if with_file and t.status == "complete":
                    p = t.filepath or t.path
                    try:
                        if os.path.isfile(p):
                            os.remove(p)
                        elif os.path.isdir(p):
                            shutil.rmtree(p)
                    except Exception as e:
                        log.warning("delete failed: %s", e)
                self.cleanup_cookies(t)
                NAMES.release(t.id)
                self.tasks.pop(t.id, None)
                if t.id in self.order:
                    self.order.remove(t.id)
                if self.tree.exists(t.id):
                    self.tree.delete(t.id)
                win = self.progress_windows.pop(t.id, None)
                if win:
                    win.win.destroy()
            self.save_list()
        finish()

    def open_selected(self):
        for t in self.selected()[:1]:
            if t.status == "complete":
                p = t.filepath or t.path
                if os.path.exists(p):
                    open_path(p)
                else:
                    messagebox.showwarning(APP_NAME, "File not found. It may have been moved.")
            else:
                self.progress_selected()

    def folder_selected(self):
        sel = self.selected()
        if sel:
            t = sel[0]
            p = t.filepath or t.path
            show_in_folder(p if os.path.exists(p) else t.folder)
        else:
            os.makedirs(self.settings["download_dir"], exist_ok=True)
            open_path(self.settings["download_dir"])

    def progress_selected(self):
        for t in self.selected()[:1]:
            if t.id in self.progress_windows:
                self.progress_windows[t.id].win.lift()
            else:
                self.progress_windows[t.id] = ProgressWindow(self, t)

    def copy_url(self):
        sel = self.selected()
        if sel:
            u = sel[0].page_url or sel[0].url
            self.root.clipboard_clear()
            self.root.clipboard_append(u)
            self.last_clip = u

    def on_double(self, e):
        if self.tree.identify_row(e.y):
            self.open_selected()

    def on_right(self, e):
        row = self.tree.identify_row(e.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(e.x_root, e.y_root)

    # ---- incoming requests
    def handle_request(self, data):
        """Called on UI thread for every browser / clipboard / manual request."""
        kind = data.get("kind") or "file"
        url = (data.get("url") or "").strip()
        page = (data.get("page_url") or "").strip()
        req = {
            "kind": kind, "url": url, "page_url": page,
            "referer": data.get("referer") or "", "title": data.get("title") or "",
            "filename": data.get("filename") or "", "cookies": data.get("cookies") or [],
            "user_agent": data.get("user_agent") or DEFAULT_UA,
        }
        DownloadDialog(self, req)

    def create_from_request(self, req, info, choice, folder, filename, start=True):
        t = Task(
            kind=req["kind"], url=info.get("final_url") or req["url"],
            page_url=req.get("page_url", ""), referer=req.get("referer", ""),
            user_agent=req.get("user_agent"), filename=filename, folder=folder,
            size=info.get("size"), resumable=info.get("resumable", False),
            title=req.get("title", ""), status="queued" if start else "paused",
            connections=self.settings["connections"], base_name=filename, started=False,
            overwrite=self.settings.get("if_exists") == "overwrite",
        )
        if t.kind == "media":
            t.media_format = choice
            t.audio_only = choice == "audio"
            t.playlist = bool(info.get("playlist"))
            t.page_url = info.get("webpage_url") or req.get("page_url") or req.get("url")
        if req.get("cookies"):
            path = os.path.join(COOKIE_DIR, t.id + ".txt")
            write_netscape_cookies(req["cookies"], path)
            with open(path + ".json", "w", encoding="utf-8") as f:
                json.dump(req["cookies"], f)
            t.cookie_file = path
        # reserve the name now, so a second download with the same name queued a moment
        # later gets "(2)" instead of landing on top of this one
        check = not t.overwrite
        if t.kind == "file":
            t.filename = NAMES.claim(t.id, folder, filename, check_disk=check)
        else:
            t.stem = NAMES.claim(t.id, folder, filename, by_stem=True, check_disk=check)
            t.filename = t.stem
        self.add_task(t)
        self.tree.selection_set(t.id)
        self.tree.see(t.id)
        return t

    def folder_for(self, filename, kind="file", audio_only=False):
        base = self.settings["download_dir"]
        if self.settings["use_categories"]:
            return os.path.join(base, category_for(filename, kind, audio_only))
        return base

    # ---- clipboard watching
    def clip_tick(self):
        if self.settings["clipboard_watch"]:
            try:
                text = self.root.clipboard_get()
            except Exception:
                text = None
            if text and text != self.last_clip:
                self.last_clip = text
                u = text.strip()
                if re.match(r"^https?://\S+$", u) and len(u) < 4000:
                    path = urllib.parse.urlparse(u).path
                    if ext_of(path) in ALL_EXTS:
                        self.handle_request({"kind": "file", "url": u})
        self.root.after(1000, self.clip_tick)

    # ---- dialogs
    def add_url_dialog(self):
        AddUrlDialog(self)

    def settings_dialog(self):
        SettingsDialog(self)

    def about(self):
        y = ytdlp()
        ff = find_ffmpeg()
        messagebox.showinfo(
            APP_NAME,
            f"{APP_NAME} {VERSION}\nOpen source download manager (MIT License)\n\n"
            f"yt-dlp: {y.version.__version__ if y else 'NOT installed'}\n"
            f"FFmpeg: {ff or 'not found (HD YouTube and MP3 need it)'}\n"
            f"Browser link: {HOST}:{PORT}\n\nData folder: {DATA_DIR}",
            parent=self.root)

    # ---- window / tray
    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def setup_tray(self):
        if self.tray:
            return True
        try:
            import pystray
            img = self.icon_pil(64)
            menu = pystray.Menu(
                pystray.MenuItem("Show OpenDM", lambda *_: self.post(self.show_window), default=True),
                pystray.MenuItem("Add URL", lambda *_: self.post(self.add_url_dialog)),
                pystray.MenuItem("Pause all", lambda *_: self.post(self.pause_all)),
                pystray.MenuItem("Exit", lambda *_: self.post(self.quit)),
            )
            self.tray = pystray.Icon(APP_NAME, img, APP_NAME, menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
            return True
        except Exception:
            log.info("tray not available: %s", traceback.format_exc(limit=1))
            self.tray = None
            return False

    def on_close(self):
        if self.settings["close_to_tray"] and self.setup_tray():
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        active = [t for t in self.tasks.values() if t.status == "downloading"]
        for t in active:
            t.stop_flag = "pause"
        for t in active:
            if t.thread:
                t.thread.join(timeout=3)
        try:
            self.save_list()
        except Exception:
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        self.root.destroy()

    def set_autostart(self, enable):
        if not IS_WIN:
            return
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                 winreg.KEY_SET_VALUE)
            if enable:
                launcher = os.environ.get("OPENDM_LAUNCHER") or os.path.join(INSTALL_DIR, "OpenDM.exe")
                if os.path.isfile(launcher):
                    cmd = f'"{launcher}" --tray'
                else:
                    exe = sys.executable
                    if exe.lower().endswith("python.exe"):
                        exe = exe[:-10] + "pythonw.exe"
                    cmd = f'"{exe}" "{os.path.abspath(__file__)}" --tray'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not change startup setting:\n{e}")

    @staticmethod
    def autostart_enabled():
        if not IS_WIN:
            return False
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run")
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False

    def extension_setup(self):
        guide = os.path.join(INSTALL_DIR, "extension-setup.html")
        ext = os.path.join(INSTALL_DIR, "extension")
        if os.path.isfile(guide):
            webbrowser.open("file:///" + guide.replace("\\", "/"))
        if os.path.isdir(ext):
            show_in_folder(ext)
        else:
            messagebox.showinfo(APP_NAME, "Extension folder not found next to the app.\n"
                                "Reinstall OpenDM or load the 'extension' folder from the download.",
                                parent=self.root)

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------- dialog windows

_CASCADE = [0]


def bring_to_front(win, center=True):
    if center:
        win.update_idletasks()
        w, h = max(win.winfo_reqwidth(), 560), win.winfo_reqheight()
        off = (_CASCADE[0] % 6) * 28
        _CASCADE[0] += 1
        x = (win.winfo_screenwidth() - w) // 2 + off
        y = max(20, (win.winfo_screenheight() - h) // 3 + off)
        win.geometry(f"+{x}+{y}")
    win.lift()
    win.attributes("-topmost", True)
    win.focus_force()


class DownloadDialog:
    """The 'Download File Info' window, like IDM."""

    def __init__(self, app, req):
        self.app, self.req = app, req
        self.info = None
        self.win = tk.Toplevel(app.root)
        w = self.win
        w.title(f"{APP_NAME} | Download File Info")
        w.resizable(True, False)
        w.minsize(560, 0)
        frm = ttk.Frame(w, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        self.url_var = tk.StringVar(value=req.get("page_url") if req["kind"] == "media" else req["url"])
        ttk.Label(frm, text="URL").grid(row=0, column=0, sticky="w", pady=3)
        e = ttk.Entry(frm, textvariable=self.url_var, state="readonly")
        e.grid(row=0, column=1, columnspan=2, sticky="we", pady=3)

        ttk.Label(frm, text="Save as").grid(row=1, column=0, sticky="w", pady=3)
        self.name_var = tk.StringVar(value=req.get("filename") or "")
        ttk.Entry(frm, textvariable=self.name_var).grid(row=1, column=1, columnspan=2, sticky="we", pady=3)

        ttk.Label(frm, text="Folder").grid(row=2, column=0, sticky="w", pady=3)
        self.folder_var = tk.StringVar(value=app.settings["download_dir"])
        ttk.Entry(frm, textvariable=self.folder_var).grid(row=2, column=1, sticky="we", pady=3)
        ttk.Button(frm, text="Browse", command=self.browse).grid(row=2, column=2, padx=(6, 0))
        self.folder_touched = False

        self.quality_lbl = ttk.Label(frm, text="Quality")
        self.quality_var = tk.StringVar()
        self.quality = ttk.Combobox(frm, textvariable=self.quality_var, state="readonly", width=40)
        self.quality.bind("<<ComboboxSelected>>", lambda e: self.update_folder())

        self.info_var = tk.StringVar(value="Getting file info...")
        ttk.Label(frm, textvariable=self.info_var, foreground="#555").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 4))
        self.pb = ttk.Progressbar(frm, mode="indeterminate")
        self.pb.grid(row=5, column=0, columnspan=3, sticky="we")
        self.pb.start(12)

        self.dont_ask = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Don't show this window again (start automatically)",
                        variable=self.dont_ask).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
        self.start_btn = ttk.Button(btns, text="Start Download", style="Accent.TButton",
                                    command=lambda: self.go(True), state="disabled")
        self.later_btn = ttk.Button(btns, text="Download Later", command=lambda: self.go(False),
                                    state="disabled")
        self.start_btn.pack(side="left", padx=4)
        self.later_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=w.destroy).pack(side="left", padx=4)
        w.bind("<Return>", lambda e: self.go(True) if str(self.start_btn["state"]) != "disabled" else None)
        w.bind("<Escape>", lambda e: w.destroy())

        if not app.settings["show_dialog"]:
            w.withdraw()
        else:
            bring_to_front(w)
        threading.Thread(target=self.probe, daemon=True).start()

    def browse(self):
        d = filedialog.askdirectory(parent=self.win, initialdir=self.folder_var.get())
        if d:
            self.folder_var.set(d)
            self.folder_touched = True

    def update_folder(self):
        if self.folder_touched or not self.info:
            return
        audio = self.choice() == "audio"
        name = self.name_var.get()
        self.folder_var.set(self.app.folder_for(name, self.req["kind"], audio))

    def choice(self):
        if self.req["kind"] != "media":
            return None
        v = self.quality_var.get()
        if v.startswith("Audio"):
            return "audio"
        m = re.match(r"(\d+)p", v)
        return m.group(1) if m else "best"

    def probe(self):
        req = self.req
        cookie_tmp = None
        try:
            # route: links to video sites go to yt-dlp, everything else is a plain file
            if req["kind"] in ("file", "auto"):
                target = req["url"] or req["page_url"]
                if is_media_site(target):
                    req["kind"], req["page_url"] = "media", target
                elif req["kind"] == "auto":
                    req["kind"], req["url"] = "file", target
                self.app.post(self.refresh_url)
            if req["kind"] == "media":
                headers = {"User-Agent": req["user_agent"]}
                if req.get("cookies"):
                    cookie_tmp = os.path.join(COOKIE_DIR, "probe-" + uuid.uuid4().hex[:8] + ".txt")
                    write_netscape_cookies(req["cookies"], cookie_tmp)
                try:
                    info = probe_media(req["page_url"] or req["url"], cookie_tmp, headers)
                except Exception as e:
                    # page not supported: fall back to the raw media URL if we have one
                    raw = req.get("url") or ""
                    if raw.startswith("http") and raw != req.get("page_url"):
                        log.info("yt-dlp failed (%s), falling back to direct url", e)
                        req["kind"] = "file"
                        info = self.probe_direct(req)
                    else:
                        raise
            else:
                info = self.probe_direct(req)
            self.app.post(lambda: self.ready(info))
        except Exception as e:
            msg = str(e)
            if isinstance(e, urllib.error.HTTPError):
                msg = f"HTTP {e.code} {e.reason}"
            msg = re.sub(r"\x1b\[[0-9;]*m", "", msg).replace("ERROR: ", "")
            self.app.post(lambda: self.failed(msg))
        finally:
            if cookie_tmp and os.path.exists(cookie_tmp):
                try:
                    os.remove(cookie_tmp)
                except Exception:
                    pass

    def refresh_url(self):
        if self.win.winfo_exists():
            r = self.req
            self.url_var.set(r["page_url"] if r["kind"] == "media" else r["url"])

    def probe_direct(self, req):
        h = {"User-Agent": req["user_agent"], "Accept": "*/*"}
        if req.get("referer"):
            h["Referer"] = req["referer"]
        ck = cookie_header(req.get("cookies"), req["url"])
        if ck:
            h["Cookie"] = ck
        info = probe_file(req["url"], h)
        if req.get("filename"):
            info["filename"] = sanitize_filename(req["filename"])
        return info

    def failed(self, msg):
        if not self.win.winfo_exists():
            return
        self.pb.stop()
        self.pb.grid_remove()
        self.info_var.set("Could not get info: " + msg[:300])
        if not self.win.winfo_viewable():
            self.win.deiconify()
            bring_to_front(self.win)
        # still allow a blind attempt for direct files
        if self.req["kind"] == "file":
            if not self.name_var.get():
                self.name_var.set(filename_from_response(self.req["url"], {}))
            self.info = {"size": None, "resumable": False}
            self.update_folder()
            self.start_btn.configure(state="normal")
            self.later_btn.configure(state="normal")

    def ready(self, info):
        if not self.win.winfo_exists():
            return
        self.info = info
        self.pb.stop()
        self.pb.grid_remove()
        if self.req["kind"] == "media":
            fields = info.get("name_fields") or media_fields({"title": info.get("title")})
            self.name_var.set(media_name(fields, self.app.settings.get("name_template")
                                         or DEFAULT_SETTINGS["name_template"]))
            ff = find_ffmpeg()
            opts = ["Best available"] + [f"{h}p" for h in info["heights"]]
            opts.append("Audio only (MP3)" if ff else "Audio only (M4A)")
            self.quality["values"] = opts
            self.quality.current(0)
            self.quality_lbl.grid(row=3, column=0, sticky="w", pady=3)
            self.quality.grid(row=3, column=1, sticky="w", pady=3)
            parts = [info.get("extractor") or "Video"]
            if info["playlist"]:
                parts.append(f"playlist with {info['count']} items")
            if info.get("duration"):
                parts.append(human_eta(info["duration"]) or "")
            if info.get("filesize"):
                parts.append("about " + human_size(info["filesize"]))
            if not ff:
                parts.append("FFmpeg not found: max quality is limited")
            self.info_var.set(" | ".join(p for p in parts if p))
        else:
            if not self.name_var.get():
                self.name_var.set(info.get("filename") or "download")
            size = human_size(info["size"]) if info.get("size") else "unknown size"
            res = "resume supported" if info.get("resumable") else "resume NOT supported"
            self.info_var.set(f"{size} | {res}")
        self.update_folder()
        self.start_btn.configure(state="normal")
        self.later_btn.configure(state="normal")
        if not self.app.settings["show_dialog"]:
            self.go(True)

    def go(self, start):
        if not self.info:
            return
        name = sanitize_filename(self.name_var.get())
        folder = self.folder_var.get().strip() or self.app.settings["download_dir"]
        if self.dont_ask.get():
            self.app.settings["show_dialog"] = False
            self.app.save_settings()
        self.app.create_from_request(self.req, self.info, self.choice(), folder, name, start)
        self.win.destroy()


class AddUrlDialog:
    def __init__(self, app):
        self.app = app
        w = self.win = tk.Toplevel(app.root)
        w.title(f"{APP_NAME} | Add URL")
        w.resizable(True, False)
        frm = ttk.Frame(w, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Paste a file link or a video page link (YouTube, Instagram...)").pack(anchor="w")
        self.var = tk.StringVar()
        try:
            c = app.root.clipboard_get().strip()
            if re.match(r"^https?://\S+$", c):
                self.var.set(c)
        except Exception:
            pass
        ent = ttk.Entry(frm, textvariable=self.var, width=80)
        ent.pack(fill="x", pady=8)
        ent.focus_set()
        ent.select_range(0, "end")
        b = ttk.Frame(frm)
        b.pack(anchor="e")
        ttk.Button(b, text="OK", style="Accent.TButton", command=self.ok).pack(side="left", padx=4)
        ttk.Button(b, text="Cancel", command=w.destroy).pack(side="left")
        w.bind("<Return>", lambda e: self.ok())
        w.bind("<Escape>", lambda e: w.destroy())
        bring_to_front(w)

    def ok(self):
        u = self.var.get().strip()
        if not re.match(r"^https?://", u):
            messagebox.showwarning(APP_NAME, "Enter a link starting with http:// or https://", parent=self.win)
            return
        self.win.destroy()
        self.app.handle_request({"kind": "auto", "url": u, "page_url": u})


class SettingsDialog:
    def __init__(self, app):
        self.app = app
        s = app.settings
        w = self.win = tk.Toplevel(app.root)
        w.title(f"{APP_NAME} | Settings")
        w.resizable(False, False)
        frm = ttk.Frame(w, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        self.v = {
            "download_dir": tk.StringVar(value=s["download_dir"]),
            "connections": tk.IntVar(value=s["connections"]),
            "max_parallel": tk.IntVar(value=s["max_parallel"]),
            "speed_limit_kbps": tk.IntVar(value=s["speed_limit_kbps"]),
            "clipboard_watch": tk.BooleanVar(value=s["clipboard_watch"]),
            "show_dialog": tk.BooleanVar(value=s["show_dialog"]),
            "use_categories": tk.BooleanVar(value=s["use_categories"]),
            "close_to_tray": tk.BooleanVar(value=s["close_to_tray"]),
            "start_with_windows": tk.BooleanVar(value=app.autostart_enabled() if IS_WIN
                                                else s["start_with_windows"]),
            "completion_popup": tk.BooleanVar(value=s["completion_popup"]),
        }
        r = 0
        ttk.Label(frm, text="Download folder").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.v["download_dir"], width=50).grid(row=r, column=1, sticky="we")
        ttk.Button(frm, text="Browse", command=self.browse).grid(row=r, column=2, padx=(6, 0))
        r += 1
        for key, label, lo, hi in (("connections", "Connections per download", 1, 32),
                                   ("max_parallel", "Downloads at the same time", 1, 10),
                                   ("speed_limit_kbps", "Speed limit KB/s (0 = no limit)", 0, 1000000)):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Spinbox(frm, from_=lo, to=hi, textvariable=self.v[key], width=10).grid(row=r, column=1, sticky="w")
            r += 1
        checks = [
            ("show_dialog", "Show the download info window before starting"),
            ("clipboard_watch", "Catch file links I copy to the clipboard"),
            ("use_categories", "Sort into folders (Video, Music, Images, Programs...)"),
            ("completion_popup", "Show a popup when a download finishes"),
            ("close_to_tray", "Closing the window keeps OpenDM running in the tray"),
        ]
        if IS_WIN:
            checks.append(("start_with_windows", "Start OpenDM with Windows"))
        for key, label in checks:
            ttk.Checkbutton(frm, text=label, variable=self.v[key]).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=2)
            r += 1

        # ---- file naming
        ttk.Separator(frm).grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 6))
        r += 1
        ttk.Label(frm, text="Video file names").grid(row=r, column=0, sticky="w", pady=4)
        self.template_var = tk.StringVar(value=s.get("name_template") or DEFAULT_SETTINGS["name_template"])
        cb = ttk.Combobox(frm, textvariable=self.template_var, values=NAME_PRESETS, width=48)
        cb.grid(row=r, column=1, columnspan=2, sticky="we")
        r += 1
        self.preview_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.preview_var, foreground="#555").grid(
            row=r, column=1, columnspan=2, sticky="w")
        r += 1
        ttk.Label(frm, foreground="#555", text="Tags: {title} {uploader} {id} {date} {site}. "
                  "Keep {id} in the name so posts with the same caption never clash.").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(2, 4))
        r += 1
        self.template_var.trace_add("write", lambda *a: self.update_preview())
        self.update_preview()
        ttk.Label(frm, text="If the name is taken").grid(row=r, column=0, sticky="w", pady=4)
        self.exists_labels = {"rename": "Add a number: name (2), name (3)...",
                              "overwrite": "Replace the old file"}
        self.exists_var = tk.StringVar(value=self.exists_labels.get(s.get("if_exists"),
                                                                    self.exists_labels["rename"]))
        ttk.Combobox(frm, textvariable=self.exists_var, state="readonly", width=36,
                     values=list(self.exists_labels.values())).grid(row=r, column=1, sticky="w")
        r += 1
        ff = find_ffmpeg()
        ttk.Label(frm, foreground="#555",
                  text=("FFmpeg found: " + ff) if ff else
                  "FFmpeg not found. For 1080p+ YouTube and MP3, run in Command Prompt:\n"
                  "winget install Gyan.FFmpeg     then restart OpenDM.").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        r += 1
        b = ttk.Frame(frm)
        b.grid(row=r, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Save", style="Accent.TButton", command=self.save).pack(side="left", padx=4)
        ttk.Button(b, text="Cancel", command=w.destroy).pack(side="left")
        bring_to_front(w)

    def update_preview(self):
        self.preview_var.set("Example: " + media_name(NAME_SAMPLE, self.template_var.get()) + ".mp4")

    def browse(self):
        d = filedialog.askdirectory(parent=self.win, initialdir=self.v["download_dir"].get())
        if d:
            self.v["download_dir"].set(d)

    def save(self):
        try:
            new = {k: var.get() for k, var in self.v.items()}
        except tk.TclError:
            messagebox.showwarning(APP_NAME, "Numbers only in the number fields.", parent=self.win)
            return
        new["connections"] = max(1, min(32, int(new["connections"])))
        new["max_parallel"] = max(1, min(10, int(new["max_parallel"])))
        new["speed_limit_kbps"] = max(0, int(new["speed_limit_kbps"]))
        tpl = self.template_var.get().strip()
        if not re.search(r"\{(?:" + "|".join(NAME_TOKENS) + r")\}", tpl):
            messagebox.showwarning(APP_NAME, "The file name pattern needs at least one tag, "
                                   "for example {title} or {id}.", parent=self.win)
            return
        new["name_template"] = tpl
        new["if_exists"] = "overwrite" if self.exists_var.get() == self.exists_labels["overwrite"] \
            else "rename"
        old_auto = self.app.autostart_enabled() if IS_WIN else self.app.settings.get("start_with_windows")
        self.app.settings.update(new)
        self.app.save_settings()
        LIMITER.set_rate(new["speed_limit_kbps"])
        if IS_WIN and new["start_with_windows"] != old_auto:
            self.app.set_autostart(new["start_with_windows"])
        self.win.destroy()


class ProgressWindow:
    """Per-download window with an IDM style segment bar."""

    def __init__(self, app, t):
        self.app, self.t = app, t
        w = self.win = tk.Toplevel(app.root)
        w.title(f"{APP_NAME} | {t.filename}")
        w.resizable(True, False)
        w.minsize(520, 0)
        w.protocol("WM_DELETE_WINDOW", self.close)
        frm = ttk.Frame(w, padding=12)
        frm.pack(fill="both", expand=True)
        self.lines = {}
        for i, key in enumerate(("File", "Status", "Size", "Downloaded", "Speed", "Time left", "Resume")):
            ttk.Label(frm, text=key).grid(row=i, column=0, sticky="w", padx=(0, 12))
            var = tk.StringVar()
            ttk.Label(frm, textvariable=var).grid(row=i, column=1, sticky="w")
            self.lines[key] = var
        self.pb = ttk.Progressbar(frm, maximum=100)
        self.pb.grid(row=8, column=0, columnspan=2, sticky="we", pady=(10, 4))
        self.canvas = tk.Canvas(frm, height=16, bg="#e9eef5", highlightthickness=0)
        self.canvas.grid(row=9, column=0, columnspan=2, sticky="we")
        frm.columnconfigure(1, weight=1)
        b = ttk.Frame(frm)
        b.grid(row=10, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.pr_btn = ttk.Button(b, text="Pause", command=self.toggle)
        self.pr_btn.pack(side="left", padx=4)
        ttk.Button(b, text="Close", command=self.close).pack(side="left")
        self.refresh()

    def toggle(self):
        if self.t.status == "downloading":
            self.app.pause(self.t)
        elif self.t.status in ("paused", "error"):
            self.t.status = "queued"
        elif self.t.status == "complete":
            open_path(self.t.filepath or self.t.path)

    def close(self):
        self.app.progress_windows.pop(self.t.id, None)
        self.win.destroy()

    def refresh(self):
        t = self.t
        if not self.win.winfo_exists():
            return
        pct = t.progress()
        self.lines["File"].set(t.filename)
        self.lines["Status"].set(t.error if t.status == "error" else t.status.capitalize()
                                 + (f" | {t.media_note}" if t.kind == "media" and t.media_note else ""))
        self.lines["Size"].set(human_size(t.total) if t.total else "unknown")
        self.lines["Downloaded"].set(f"{human_size(t.done)}" + (f" ({pct:.1f}%)" if pct is not None else ""))
        self.lines["Speed"].set(human_speed(t.speed))
        self.lines["Time left"].set(human_eta(t.eta()))
        self.lines["Resume"].set("Yes" if (t.resumable or t.kind == "media") else "No")
        self.pb["value"] = pct or 0
        self.pr_btn.configure(text={"downloading": "Pause", "complete": "Open"}.get(t.status, "Resume"))
        c = self.canvas
        c.delete("all")
        width = max(1, c.winfo_width())
        if t.kind == "file" and t.size:
            for seg in list(t.segments):
                x0 = seg["s"] / t.size * width
                x1 = min(seg["p"], seg["e"] + 1 if seg["e"] >= 0 else seg["p"]) / t.size * width
                if x1 > x0:
                    c.create_rectangle(x0, 0, x1, 16, fill="#1a73e8", width=0)
                xe = (seg["e"] + 1) / t.size * width if seg["e"] >= 0 else x1
                c.create_line(xe, 0, xe, 16, fill="#ffffff")
        elif pct:
            c.create_rectangle(0, 0, pct / 100 * width, 16, fill="#1a73e8", width=0)


class CompletePopup:
    def __init__(self, app, t):
        self.app = app
        w = self.win = tk.Toplevel(app.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        frm = tk.Frame(w, bg="#202124", padx=14, pady=12)
        frm.pack(fill="both", expand=True)
        tk.Label(frm, text="Download complete", fg="#8ab4f8", bg="#202124",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        name = t.filename if len(t.filename) < 55 else t.filename[:52] + "..."
        tk.Label(frm, text=name, fg="white", bg="#202124", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 8))
        b = tk.Frame(frm, bg="#202124")
        b.pack(anchor="e")
        p = t.filepath or t.path
        for text, cmd in (("Open", lambda: (open_path(p), self.close())),
                          ("Open folder", lambda: (show_in_folder(p), self.close())),
                          ("Close", self.close)):
            tk.Button(b, text=text, command=cmd, relief="flat", bg="#3c4043", fg="white",
                      activebackground="#5f6368", activeforeground="white", padx=10).pack(side="left", padx=3)
        w.update_idletasks()
        ww, wh = max(340, w.winfo_reqwidth()), w.winfo_reqheight()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"{ww}x{wh}+{sw - ww - 16}+{sh - wh - 60}")
        w.after(9000, self.close)

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------- entry

def already_running():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{HOST}:{PORT}/show", timeout=1.5) as r:
            return json.loads(r.read() or b"{}").get("ok", False)
    except Exception:
        return False


def main():
    if already_running():
        return
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenDM.App")
        except Exception:
            pass
    app = App(start_hidden="--tray" in sys.argv)
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("fatal")
        raise
