#!/usr/bin/env python3
"""A small Youdao-style screenshot translator for X11/GNOME.

Ctrl+Alt+O draws a rectangle on the frozen desktop (black-screen safe: the
frozen screenshot is painted before the dark overlay).  Mixed-language OCR
separates distant labels and groups prose; English paragraphs are translated by
a local Argos en->zh CPU model with a robotics/mechanics glossary and disk
cache. An explicitly selected remote backend remains available. Chinese is rendered back
into the captured image while Chinese, code names and icons retain their source
pixels. Crowded paragraphs flow downward and display their translations only.
A non-blocking progress window tracks capture/OCR/translation on a worker
thread while a single-instance lock keeps repeated shortcut presses from
starting a second task.
"""
import ast
import csv
import fcntl
import html
import io
import json
import logging
import math
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, Future, as_completed, wait
from pathlib import Path
from urllib.parse import urlencode

import requests
import local_translation
from PIL import Image, ImageDraw, ImageFont, ImageOps
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib  # noqa: F401


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
CACHE = ROOT / "cache.png"
TESSDATA = ROOT / "tessdata"
LOG_FILE = ROOT / "screenshot_translator.log"
CACHE_FILE = ROOT / "translation_cache.json"
LOCK_FILE = ROOT / "task.lock"
RESULT_FOCUS_Y = 0
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

TRANSLATION_FONT_SIZE = 18
MIN_FONT_SIZE = 14
MAX_FONT_SIZE = 20
FONT_STEP = 2
BOX_GAP = 4            # minimum px gap between translation boxes
PAD_H, PAD_V = 6, 4    # inner padding of a translation box
OCR_PSM = "3"          # automatic segmentation keeps separate columns as blocks
OCR_LANG = os.environ.get("SCREENSHOT_TRANSLATOR_OCR_LANG", "eng+chi_sim").strip() or "eng+chi_sim"
SOURCE_LANG = os.environ.get("SCREENSHOT_TRANSLATOR_SOURCE_LANG", "en").strip() or "en"
TRANSLATION_BACKEND = os.environ.get("SCREENSHOT_TRANSLATOR_BACKEND", "local").strip().lower() or "local"
_local_client = local_translation.LocalClient()
_local_error = ""
OCR_BORDER = 10        # pixels added after scaling; removed from returned boxes
OCR_SCALE = 2          # raise small glyphs above Tesseract's minimum x-height
OCR_MAX_PIXELS = 5_000_000   # cap on the upscaled image area fed to Tesseract


def _ocr_scale_for(width, height):
    """Adaptive upscale factor: 2x for small regions (small text), tapering
    toward 1x for huge regions so a full-screen selection does not feed
    Tesseract a 5120x2880 image (a ~7s OCR pass)."""
    area = width * height
    if area <= 0:
        return OCR_SCALE
    return min(OCR_SCALE, math.sqrt(OCR_MAX_PIXELS / area))

# Mozhi endpoint is GET /api/translate with query params (POST returns HTTP 500).
# The order matters only for the very first request: after the first success the
# working node is promoted to the front (_ordered_nodes), so a dead first node
# stops stalling every translation.
DEFAULT_NODES = [
    "https://mozhi.adminforge.de",
    "https://mozhi.ducks.party",
    "https://translate.projectsegfau.lt",
]
# allow overriding the node list for testing/firewalls: MOZHI_NODES=a,b,c
_env_nodes = os.environ.get("MOZHI_NODES", "").strip()
NODES = [n.strip() for n in _env_nodes.split(",") if n.strip()] if _env_nodes else DEFAULT_NODES
# DuckDuckGo (Bing-backed) is the fast/default engine.  A second engine can be
# enabled explicitly when a deployment needs it, but trying it automatically
# after a public-node HTTP 500 doubles the wait during an outage.
_engine_override = os.environ.get("SCREENSHOT_TRANSLATOR_ENGINES", "").strip()
ENGINES = ([e.strip() for e in _engine_override.split(",")
            if e.strip()] if _engine_override else ["duckduckgo"])
ENGINE_TARGET = {"duckduckgo": "zh", "google": "zh-CN"}
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_MAX_BYTES = 500
MAX_TRANSLATE_WORKERS = 3
# Public nodes are frequently unreachable through a desktop proxy.  Keep the
# failure path bounded; successful requests still get the full read timeout.
CONNECT_TIMEOUT = 4
READ_TIMEOUT = 15
# One node request per worker; when the preferred node fails, the remaining
# nodes are raced instead of being tried serially.
MAX_NODE_WORKERS = max(1, len(NODES))

# The request path is intentionally separate from the paragraph pool.  A
# paragraph worker may race several nodes without deadlocking its own pool.
MAX_ATTEMPTS_PER_PARAGRAPH = len(NODES) * len(ENGINES)
# One GET query per batch: Bing itself rejects ~>1000 chars (HTTP 500), and the
# reverse proxies in front of the public nodes reject long URLs (HTTP 431/414),
# which hits CJK text especially hard (each CJK char is ~9 percent-encoded
# bytes).  Size batches by BOTH raw chars and percent-encoded URL length.
BATCH_MAX_CHARS = 950
BATCH_MAX_URL = 3500

logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def _gsettings_value(schema, key):
    """Read a simple GNOME setting without failing when gsettings is absent."""
    try:
        value = subprocess.run(
            ["gsettings", "get", schema, key],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=2, check=True,
        ).stdout.strip()
        return value.strip("'")
    except Exception:
        return ""


def detect_desktop_proxy():
    """Return the desktop HTTP proxy, including a Clash mixed proxy port."""
    override = os.environ.get("SCREENSHOT_TRANSLATOR_PROXY", "").strip()
    if override:
        return override
    if _gsettings_value("org.gnome.system.proxy", "mode") != "manual":
        return ""
    for kind in ("https", "http"):
        schema = "org.gnome.system.proxy." + kind
        host = _gsettings_value(schema, "host")
        port = _gsettings_value(schema, "port")
        if host and port.isdigit() and int(port) > 0:
            return "http://%s:%s" % (host, port)
    return ""


DESKTOP_PROXY = detect_desktop_proxy() if TRANSLATION_BACKEND == "remote" else ""
if DESKTOP_PROXY:
    logging.info("desktop proxy enabled")

# requests.Session is only used from one worker thread at a time; still, keep a
# thread-local session per worker as requested instead of a shared object.
_tls = threading.local()

# Persistent worker pools: the threads (and their thread-local HTTP sessions)
# stay alive between screenshots, so the multi-second TLS handshake through the
# desktop proxy happens once per thread per launch, not once per batch.  The
# chunk pool is separate from the batch pool so a paragraph split into chunks
# can never deadlock against the batch workers.
class _WorkerPool:
    """Tiny daemon-thread pool that keeps its workers (and their thread-local
    HTTP sessions) alive across screenshots.

    Daemon threads are used deliberately: concurrent.futures' executor threads
    are non-daemon and are joined at interpreter shutdown, so quitting the app
    while a request is in flight would block exit until the socket times out.
    """

    def __init__(self, workers, name):
        self._q = queue.Queue()
        self._threads = [threading.Thread(target=self._run,
                                          name="%s-%d" % (name, i),
                                          daemon=True)
                         for i in range(workers)]
        for t in self._threads:
            t.start()

    def _run(self):
        while True:
            fn, fut = self._q.get()
            if fn is None:
                return
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                result = fn()
            except BaseException as exc:
                fut.set_exception(exc)
            else:
                fut.set_result(result)

    def submit(self, fn, *args):
        fut = Future()
        self._q.put((lambda: fn(*args), fut))
        return fut


_translate_executor = _WorkerPool(MAX_TRANSLATE_WORKERS, "translate")
_chunk_executor = _WorkerPool(MAX_TRANSLATE_WORKERS, "chunk")
_node_executor = _WorkerPool(MAX_NODE_WORKERS, "node")


def _tls_session():
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) screenshot-translator"})
        if DESKTOP_PROXY:
            s.proxies.update({"http": DESKTOP_PROXY, "https": DESKTOP_PROXY})
        _tls.session = s
    return s


CACHE_MAX_ENTRIES = 1000
CACHE_SAVE_INTERVAL = 0.75   # seconds; cache_set writes at most this often

_cache = {}
_cache_lock = threading.Lock()
_cache_last_save = 0.0


def load_translation_cache():
    global _cache
    try:
        _cache = json.loads(CACHE_FILE.read_text())
    except Exception:
        _cache = {}


def _save_cache_now():
    """Write the cache to disk; callers must hold ``_cache_lock``."""
    global _cache_last_save
    try:
        CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False))
        _cache_last_save = time.monotonic()
    except Exception as exc:
        logging.warning("cache save failed: %s", exc.__class__.__name__)


def save_cache():
    """Flush any debounced cache writes (called when a task completes)."""
    with _cache_lock:
        _save_cache_now()


def _cache_key(text):
    # Do not reuse old autodetected translations, or translations produced
    # under different language/engine settings. Whitespace variants share a key.
    identity = ([local_translation.PIPELINE_VERSION, local_translation.glossary_identity()]
                if TRANSLATION_BACKEND == "local" else ["remote", ENGINES])
    return json.dumps(["en-zh-v4", SOURCE_LANG, identity, _clean_ocr_text(text)],
                      ensure_ascii=False)


def cache_get(text):
    with _cache_lock:
        return _cache.get(_cache_key(text))


def cache_set(text, value):
    with _cache_lock:
        _cache[_cache_key(text)] = value
        if len(_cache) > CACHE_MAX_ENTRIES:
            for k in list(_cache)[:len(_cache) - CACHE_MAX_ENTRIES]:
                del _cache[k]
        if time.monotonic() - _cache_last_save > CACHE_SAVE_INTERVAL:
            _save_cache_now()


def acquire_task_lock():
    """Single-instance lock so repeated shortcut presses cannot start a second task."""
    try:
        f = open(LOCK_FILE, "w")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        try:
            f.close()
        except Exception:
            pass
        return None


def release_task_lock(lock):
    if lock is None:
        return
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# state / capture
# ---------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def pointer_monitor():
    """Resolve the target once, before any of our windows can take focus."""
    display = Gdk.Display.get_default()
    if display is None:
        raise RuntimeError("无法访问桌面画面，请确认当前为 X11 会话")
    _screen, x, y = display.get_default_seat().get_pointer().get_position()
    monitor = display.get_monitor_at_point(x, y)
    if monitor is None:
        raise RuntimeError("无法确定鼠标所在屏幕")
    return monitor


def monitor_bounds(monitor):
    geometry = monitor.get_geometry()
    return (geometry.x, geometry.y, geometry.width, geometry.height)


def region_within_bounds(rect, bounds):
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        return False
    if not all(isinstance(value, int) for value in rect):
        return False
    x, y, width, height = rect
    left, top, screen_width, screen_height = bounds
    return (width > 0 and height > 0 and x >= left and y >= top
            and x + width <= left + screen_width
            and y + height <= top + screen_height)


def selection_region(start, end, allocation, bounds):
    """Clamp a drag to this monitor and map window coordinates to the desktop."""
    left, top, width, height = bounds
    def point(value):
        return (round(min(width, max(0, value[0] * width / allocation.width))),
                round(min(height, max(0, value[1] * height / allocation.height))))
    x1, y1 = point(start)
    x2, y2 = point(end)
    return (left + min(x1, x2), top + min(y1, y2),
            abs(x2 - x1), abs(y2 - y1))


def screen_pixbuf(monitor=None):
    monitor = monitor if monitor is not None else pointer_monitor()
    root = Gdk.get_default_root_window()
    if root is None:
        raise RuntimeError("无法访问桌面画面，请确认当前为 X11 会话")
    pixbuf = Gdk.pixbuf_get_from_window(root, *monitor_bounds(monitor))
    if pixbuf is None:
        raise RuntimeError("无法读取桌面画面，请确认当前为 X11 会话")
    return pixbuf


def capture_screen(monitor=None):
    return screen_pixbuf(monitor)


def capture(rect, pix=None, bounds=None):
    if pix is None:
        monitor = pointer_monitor()
        bounds = monitor_bounds(monitor)
        pix = screen_pixbuf(monitor)
    x, y, w, h = rect
    if bounds is not None:
        if not region_within_bounds(rect, bounds):
            raise ValueError("截图区域必须位于鼠标所在屏幕内，请重新框选")
        # GDK geometry uses logical pixels; the pixbuf includes the root
        # window's scale factor. Map both edges to retain the native resolution.
        left, top, width, height = bounds
        sx, sy = pix.get_width() / width, pix.get_height() / height
        x1, y1 = round((x - left) * sx), round((y - top) * sy)
        x2, y2 = round((x + w - left) * sx), round((y + h - top) * sy)
        x, y, w, h = x1, y1, x2 - x1, y2 - y1
    crop = pix.new_subpixbuf(x, y, w, h).copy()
    crop.savev(str(CACHE), "png", [], [])
    return rect


# ---------------------------------------------------------------------------
# OCR -> paragraphs
# ---------------------------------------------------------------------------

def _run_tesseract(path, lang, psm, output=None, scale=None):
    """Run one OCR pass on a (adaptively) upscaled image, return stdout.

    ``scale`` defaults to ``_ocr_scale_for`` of the source image, so huge
    selections never blow up to a 2x full-screen bitmap.
    """
    with Image.open(path) as source:
        if scale is None:
            scale = _ocr_scale_for(source.width, source.height)
        resized = ImageOps.grayscale(source).resize(
            (max(1, round(source.width * scale)),
             max(1, round(source.height * scale))),
            Image.LANCZOS,
        )
    # A small sample keeps background detection cheap even for full screens.
    # Tesseract expects dark glyphs on a light background.
    sample = resized.resize((64, 64))
    histogram = sample.histogram()
    sample.close()
    if sum(histogram[:100]) > sum(histogram) * 0.6:
        inverted = ImageOps.invert(resized)
        resized.close()
        resized = inverted
    padded = ImageOps.expand(resized, border=OCR_BORDER, fill=255)
    encoded = io.BytesIO()
    padded.save(encoded, "PNG", dpi=(300, 300))
    padded.close()
    resized.close()

    env = os.environ.copy()
    env["TESSDATA_PREFIX"] = str(TESSDATA)
    cmd = ["tesseract", "stdin", "stdout", "--psm", str(psm), "-l", lang]
    if output:
        cmd.append(output)
    proc = subprocess.run(cmd, input=encoded.getvalue(), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=25)
    proc.check_returncode()
    return proc.stdout.decode("utf-8", errors="replace")


_NO_SPACE_BEFORE = set(",.!?;:%)]}，。！？；：、）》】」』”’")
_NO_SPACE_AFTER = set("([{（《【「『，。！？；：、“‘$£¥￥")


def _is_cjk(char):
    return unicodedata.east_asian_width(char) in ("W", "F")


def _join_ocr_tokens(tokens):
    """Join OCR fragments without adding spaces around CJK or punctuation."""
    joined = ""
    for token in (str(token).strip() for token in tokens):
        if not token:
            continue
        if (joined and not (_is_cjk(joined[-1]) or _is_cjk(token[0]) or
                            token[0] in _NO_SPACE_BEFORE or
                            joined[-1] in _NO_SPACE_AFTER)):
            joined += " "
        joined += token
    return joined


def parse_ocr_paragraphs(path, lang=None):
    """Run mixed-language TSV OCR and group words by paragraph and position.

    Each paragraph gets its overall bounding box and multi-line text. Distant
    same-line labels are split even when --psm 3 assigns them a shared block.
    The same adaptive scale is used for OCR and for mapping boxes back to
    source coordinates.
    """
    with Image.open(path) as probe:
        source_width, source_height = probe.size
        scale = _ocr_scale_for(source_width, source_height)
    lang = lang or OCR_LANG
    tsv = _run_tesseract(path, lang, OCR_PSM, "tsv", scale=scale)
    rows = list(csv.DictReader(io.StringIO(tsv), delimiter="\t"))
    if (source_height <= 160 and
            not any(row.get("text", "").strip() for row in rows)):
        # Auto page segmentation can discard a small label/single text block.
        # Retry only empty small captures, keeping full-page OCR to one pass.
        tsv = _run_tesseract(path, lang, "6", "tsv", scale=scale)
        rows = list(csv.DictReader(io.StringIO(tsv), delimiter="\t"))
    try:
        page_height = int(rows[0]["height"])
    except (IndexError, KeyError, ValueError):
        page_height = 0
    words = []
    for row in rows:
        try:
            text = row["text"].strip()
            conf = float(row["conf"])
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
            if not text or conf < 18:
                continue
            if (text in {"|", "/", "1", "I", "l"} and
                    left <= OCR_BORDER and top <= OCR_BORDER and page_height and
                    height >= (page_height - 2 * OCR_BORDER) * 0.8 and width * 2 < height):
                continue
            words.append({
                "block_num": int(row["block_num"]),
                "par_num": int(row["par_num"]),
                "line_num": int(row["line_num"]),
                "word_num": int(row["word_num"]),
                "left": left, "top": top, "width": width, "height": height,
                "text": text,
            })
        except (ValueError, KeyError):
            continue

    lines = {}
    for w in words:
        lines.setdefault((w["block_num"], w["par_num"], w["line_num"]), []).append(w)

    line_items = []
    for (block_num, par_num, _line_num), ws in lines.items():
        # Tesseract may put distant diagram labels on the same line. Split
        # at large gaps, while retaining word_num order: Chinese word boxes
        # can overlap, so sorting them by x alone scrambles their characters.
        ws.sort(key=lambda w: w["word_num"])
        heights = sorted(w["height"] for w in ws)
        if source_height <= 80:
            # Short UI captures contain neighbouring buttons. Their gap is
            # much smaller than the column gap in a paper, but larger than
            # the space within labels such as "Sign in" and "Sign up".
            gap_limit = max(10 * scale, heights[len(heights) // 2])
        else:
            gap_limit = max(32 * scale, 3 * heights[len(heights) // 2])
        runs = [[]]
        right = 0
        for word in ws:
            if runs[-1] and word["left"] - right > gap_limit:
                runs.append([])
            runs[-1].append(word)
            right = max(w["left"] + w["width"] for w in runs[-1])
        for run in runs:
            line_items.append({
                "block_num": block_num, "par_num": par_num,
                "text": _join_ocr_tokens(w["text"] for w in run),
                "left": min(w["left"] for w in run),
                "top": min(w["top"] for w in run),
                "right": max(w["left"] + w["width"] for w in run),
                "bottom": max(w["top"] + w["height"] for w in run),
            })

    paras = {}
    for li in line_items:
        paras.setdefault((li["block_num"], li["par_num"]), []).append(li)

    paragraphs = []
    groups = []
    for lis in paras.values():
        columns = []
        for li in sorted(lis, key=lambda l: (l["top"], l["left"])):
            for column in columns:
                previous = column[-1]
                overlap = min(li["right"], previous["right"]) - max(li["left"], previous["left"])
                min_width = min(li["right"] - li["left"], previous["right"] - previous["left"])
                if (overlap > min_width * 0.3 and
                        li["top"] >= previous["bottom"] - scale * 4 and
                        _needs_translation(li["text"]) == _needs_translation(previous["text"])):
                    column.append(li)
                    break
            else:
                columns.append([li])
        groups.extend(columns)
    for lis in groups:
        lis.sort(key=lambda l: (l["top"], l["left"]))
        left = min(l["left"] for l in lis)
        top = min(l["top"] for l in lis)
        right = max(l["right"] for l in lis)
        bottom = max(l["bottom"] for l in lis)
        source_left = max(0, int((left - OCR_BORDER) / scale))
        source_top = max(0, int((top - OCR_BORDER) / scale))
        source_right = min(source_width, int(math.ceil((right - OCR_BORDER) / scale)))
        source_bottom = min(source_height, int(math.ceil((bottom - OCR_BORDER) / scale)))
        if source_right <= source_left or source_bottom <= source_top:
            continue
        paragraphs.append({
            "text": "\n".join(l["text"] for l in lis),
            "box": (source_left, source_top,
                    source_right - source_left, source_bottom - source_top),
        })
    paragraphs.sort(key=lambda p: (p["box"][1], p["box"][0]))
    return paragraphs


# ---------------------------------------------------------------------------
# translation network layer (GET /api/translate)
# ---------------------------------------------------------------------------

class TranslationError(RuntimeError):
    pass


# Most recently successful node, promoted to the front of the next request so a
# dead or slow first node stops stalling every translation.  Written from worker
# threads; a plain string assignment is atomic under the GIL.
_last_good_node = None

# Circuit breaker: a node that fails connection-level requests N times in a row
# is skipped for a cooldown period, so one dead node no longer costs a full
# connect-timeout on every subsequent request.
NODE_FAILURES_TO_COOLDOWN = 2
NODE_COOLDOWN_SECONDS = 30.0
_node_failures = {}
_node_cooldown_until = {}
_node_lock = threading.Lock()


def _node_ok(node):
    with _node_lock:
        _node_failures.pop(node, None)
        _node_cooldown_until.pop(node, None)


def _node_failed(node):
    with _node_lock:
        count = _node_failures.get(node, 0) + 1
        _node_failures[node] = count
        if count >= NODE_FAILURES_TO_COOLDOWN:
            _node_cooldown_until[node] = time.monotonic() + NODE_COOLDOWN_SECONDS


def _node_available(node):
    with _node_lock:
        return time.monotonic() > _node_cooldown_until.get(node, 0.0)


def _ordered_nodes():
    nodes = [n for n in NODES if _node_available(n)] or list(NODES)
    if _last_good_node in nodes:
        return [_last_good_node] + [n for n in nodes if n != _last_good_node]
    return nodes


def _warm_node(node):
    """Establish the TLS connection through the desktop proxy in the background
    so the first translation does not pay the multi-second handshake.  Best
    effort: failures are ignored (the real request retries anyway)."""
    try:
        _tls_session().get(node, timeout=(10, 15))
    except Exception:
        pass


def _translate_on_node(node, engine, text):
    """Perform one bounded request; called by a dedicated node worker."""
    session = _tls_session()
    response = session.get(
        node + "/api/translate",
        params={"from": SOURCE_LANG,
                "to": ENGINE_TARGET.get(engine, "zh"),
                "engine": engine, "text": text},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    if response.status_code != 200:
        logging.warning("translate http node=%s engine=%s status=%s",
                        node, engine, response.status_code)
        raise TranslationError("http %s" % response.status_code)
    data = response.json()
    value = (data.get("translated-text")
             or data.get("translation")
             or data.get("translatedText"))
    if not value:
        logging.warning("translate empty node=%s engine=%s", node, engine)
        raise TranslationError("empty translation")
    return value


def _translate_mymemory_segment(text):
    response = _tls_session().get(
        MYMEMORY_URL,
        params={"q": text, "langpair": SOURCE_LANG + "|zh-CN"},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    if response.status_code != 200:
        raise TranslationError("MyMemory http %s" % response.status_code)
    data = response.json()
    if data.get("responseStatus") not in (None, 200):
        raise TranslationError("MyMemory status %s" % data.get("responseStatus"))
    value = (data.get("responseData") or {}).get("translatedText")
    if not value or value == text:
        raise TranslationError("MyMemory empty translation")
    return html.unescape(value)


def _translate_mymemory(text):
    """Translate within the fallback's UTF-8 byte limit; retain batch markers."""
    lines = text.splitlines()
    marked = [re.fullmatch(r"(ZXQPX\d+ZX) (.+)", line) for line in lines]
    if marked and all(marked):
        return "\n".join(match.group(1) + " " + _translate_mymemory(match.group(2))
                         for match in marked)
    chunks = _split_translation_chunks(
        text, fits=lambda value: len(value.encode("utf-8")) <= MYMEMORY_MAX_BYTES)
    return _join_ocr_tokens(_translate_mymemory_segment(chunk) for chunk in chunks)


def _translate_remote(text):
    """Translate one string via Mozhi, then MyMemory as a last resort.

    The preferred node is still tried first.  If it fails, the remaining
    endpoints for that engine are raced in parallel, so a dead public node
    cannot add one full timeout after another.  Log lines carry host + engine +
    exception class only, never the source text or a URL with a query string.
    """
    global _last_good_node
    nodes = _ordered_nodes()
    if not nodes:
        raise TranslationError("no translation nodes configured")

    for engine in ENGINES:
        preferred = nodes[0]
        try:
            value = _translate_on_node(preferred, engine, text)
        except Exception as exc:
            logging.warning("translate request error node=%s engine=%s exc=%s",
                            preferred, engine, exc.__class__.__name__)
            if isinstance(exc, (requests.exceptions.ConnectTimeout,
                                requests.exceptions.ConnectionError,
                                requests.exceptions.ReadTimeout,
                                requests.exceptions.SSLError,
                                requests.exceptions.ProxyError)):
                _node_failed(preferred)
        else:
            _last_good_node = preferred
            _node_ok(preferred)
            return value

        # The first node failed.  Race the other nodes instead of waiting for
        # them serially.  Cancel only prevents queued work; in-flight requests
        # remain bounded by CONNECT_TIMEOUT/READ_TIMEOUT and use daemon workers.
        remaining = [node for node in nodes[1:] if _node_available(node)]
        futures = {_node_executor.submit(_translate_on_node, node, engine, text): node
                   for node in remaining}
        while futures:
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for future in done:
                node = futures.pop(future)
                try:
                    value = future.result()
                except Exception as exc:
                    logging.warning("translate request error node=%s engine=%s exc=%s",
                                    node, engine, exc.__class__.__name__)
                    if isinstance(exc, (requests.exceptions.ConnectTimeout,
                                        requests.exceptions.ConnectionError,
                                        requests.exceptions.ReadTimeout,
                                        requests.exceptions.SSLError,
                                        requests.exceptions.ProxyError)):
                        _node_failed(node)
                else:
                    for pending in futures:
                        pending.cancel()
                    _last_good_node = node
                    _node_ok(node)
                    return value

    try:
        value = _translate_mymemory(text)
    except Exception as exc:
        logging.warning("MyMemory fallback failed exc=%s", exc.__class__.__name__)
        raise TranslationError("all translation services unavailable") from exc
    logging.info("translation fallback=MyMemory")
    return value


def _clean_ocr_text(text):
    """Undo PDF line wrapping before sending OCR text for translation."""
    text = re.sub(r"\u00ad\s*\n\s*", "", text)
    text = unicodedata.normalize("NFC", text).replace("\u00ad", "")
    text = text.replace("\ufb00", "ff").replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = text.replace("\ufb03", "ffi").replace("\ufb04", "ffl")
    def unwrap_word(match):
        word = match.group(1)
        # Preserve clear compound prefixes and an existing compound chain.
        # Ordinary PDF word fragments such as "trans-\nlation" are rejoined.
        compound = ("-" in word or word.lower() in {
            "self", "well", "cross", "non", "multi", "quasi", "semi",
        })
        return word + ("-" if compound else "")
    text = re.sub(r"([A-Za-z]+(?:-[A-Za-z]+)*)-\s*\n\s*(?=[A-Za-z])",
                  unwrap_word, text)
    text = _join_ocr_tokens(re.split(r"[ \t]*\n[ \t]*", text))
    text = re.sub(r"\s+", " ", text).strip()
    # Repair OCR token spacing in contractions and possessives without
    # guessing at technical terms, identifiers, or ordinary spelling.
    text = re.sub(r"(?<=[A-Za-z])[ \t]*(['’])[ \t]*(s|t|re|ve|ll|d|m)\b",
                  r"\1\2", text, flags=re.IGNORECASE)
    # The worker also does this for callers that bypass screenshot OCR.
    # Keep malformed glossary errors in the translation error path.
    if TRANSLATION_BACKEND == "local":
        try:
            text = local_translation.repair_ocr(text)
        except local_translation.LocalTranslationError:
            pass
    return text


def _encoded_len(text):
    """Percent-encoded URL length of a query value (requests' encoding)."""
    return len(urlencode({"text": text}))


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_TECHNICAL = local_translation.TECHNICAL


def _prepare_translation(text):
    """Protect code names and acronyms in English prose with checked tokens."""
    terms = []

    def protect(match):
        token = "QZXTERM%dZXQ" % len(terms)
        terms.append(match.group())
        return token

    return _TECHNICAL.sub(protect, _clean_ocr_text(text)), terms


def _restore_terms(value, terms):
    for index, term in enumerate(terms):
        pattern = re.compile(r"QZXTERM\s*%d\s*ZXQ" % index, re.IGNORECASE)
        if len(pattern.findall(value)) != 1:
            raise TranslationError("technical terms were not preserved")
        value = pattern.sub(lambda _match: term, value)
    if re.search(r"QZXTERM\s*\d+\s*ZXQ", value, re.IGNORECASE):
        raise TranslationError("unexpected technical term marker")
    return value


def _needs_translation(text):
    # Chinese paragraphs (including their embedded technical vocabulary) are
    # already readable. Never send them as English to a translation service.
    # Pure identifiers, numbers and icon glyphs also retain their source pixels.
    text = _clean_ocr_text(text)
    if TRANSLATION_BACKEND == "local" and local_translation.preserves_text(text):
        return False
    return not _HAN.search(text) and bool(re.search(r"[A-Za-z]", _TECHNICAL.sub("", text)))


def _fits_batch(text):
    """True when ``text`` fits one GET request (Bing char limit + URL limit)."""
    return (len(text) <= BATCH_MAX_CHARS
            and _encoded_len(text) + 20 <= BATCH_MAX_URL)


def _max_fit_len(text, cap):
    """Largest prefix length of ``text`` that still fits one request."""
    lo, hi = 0, min(cap, len(text))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _fits_batch(text[:mid]):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _split_translation_chunks(text, fits=None):
    """Split long prose near sentence/word boundaries under the API limits."""
    text = _clean_ocr_text(text)
    fits = fits or _fits_batch
    if fits(text):
        return [text]
    chunks = []
    while text and not fits(text):
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(text[:mid]):
                lo = mid
            else:
                hi = mid - 1
        max_fit = lo
        if not max_fit:
            raise TranslationError("translation limit too small for one character")
        window = text[:max_fit]
        # Keep punctuation with its sentence; the old split left it at the
        # beginning of the next request. Avoid decimal/URL dots and initials.
        cuts = [match.end() for match in re.finditer(
            r"(?<!\b[A-Z])[.!?][\"'”’)]*(?=\s|$)|[。！？；;:]", window)]
        cut = max(cuts, default=0)
        if cut < max_fit // 2:
            cut = window.rfind(" ")
            if cut < max_fit // 2:
                cut = max_fit
        if cut <= 0:
            cut = max(1, max_fit)
        chunk = text[:cut].strip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def translate_paragraph(text):
    if not _needs_translation(text):
        return text
    cached = cache_get(text)
    if cached is not None:
        return cached
    if TRANSLATION_BACKEND == "local":
        if SOURCE_LANG != "en":
            raise local_translation.LocalTranslationError("本地模型只支持英文→简体中文。")
        value = _local_client.translate(_clean_ocr_text(text))
        if not value or not _HAN.search(value):
            raise local_translation.LocalTranslationError("本地模型未生成中文译文，请检查识别内容或缩小选区。")
        cache_set(text, value)
        return value
    if TRANSLATION_BACKEND != "remote":
        raise TranslationError("unknown translation backend: %s" % TRANSLATION_BACKEND)
    prepared, terms = _prepare_translation(text)
    chunks = _split_translation_chunks(prepared)
    if len(chunks) == 1:
        value = _translate_remote(chunks[0])
    else:
        translated = [None] * len(chunks)
        futures = {_chunk_executor.submit(_translate_remote, chunk): i
                   for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            translated[futures[future]] = future.result()
        value = _join_ocr_tokens(translated)
    value = _restore_terms(value, terms)
    cache_set(text, value)
    return value


def _batch_item_size(text):
    """Approx URL size of one batched paragraph, including its marker prefix."""
    return _encoded_len("ZXQPX0ZX " + _prepare_translation(text)[0])


def _make_batches(pending, texts):
    batches = []
    current = []
    current_text = ""
    for index in pending:
        item = "ZXQPX%dZX %s" % (index, _prepare_translation(texts[index])[0])
        combined = current_text + "\n" + item if current else item
        if current and not _fits_batch(combined):
            batches.append(current)
            current = []
            current_text = ""
        current.append(index)
        current_text = current_text + "\n" + item if current_text else item
    if current:
        batches.append(current)
    return batches


def _translate_batch(indices, texts):
    """Translate several paragraphs in one round trip and split by markers."""
    prepared = {i: _prepare_translation(texts[i]) for i in indices}
    if len(indices) == 1:
        index = indices[0]
        value = _translate_remote(prepared[index][0])
        return {index: _restore_terms(value, prepared[index][1])}
    combined = "\n".join("ZXQPX%dZX %s" % (i, prepared[i][0])
                         for i in indices)
    output = _translate_remote(combined)
    pattern = re.compile(r"ZXQPX\s*(\d+)\s*ZX", re.IGNORECASE)
    markers = list(pattern.finditer(output))
    found = [int(match.group(1)) for match in markers]
    if found != indices or output[:markers[0].start()].strip():
        raise TranslationError("batch markers were not preserved")
    values = {}
    for pos, match in enumerate(markers):
        index = int(match.group(1))
        end = markers[pos + 1].start() if pos + 1 < len(markers) else len(output)
        value = output[match.end():end].strip()
        if not value:
            raise TranslationError("empty batch paragraph")
        values[index] = _restore_terms(value, prepared[index][1])
    return values


def translate_paragraphs(texts, on_progress=None):
    """Translate paragraphs in batches, with per-paragraph fallback.

    Cached values return immediately.  Uncached short paragraphs share one
    Mozhi request; if markers are changed or the request fails, only that batch
    falls back to the existing concurrent paragraph requests.
    """
    global _local_error
    if TRANSLATION_BACKEND == "local":
        _local_error = ""
        results = []
        for i, text in enumerate(texts):
            try:
                results.append(translate_paragraph(text))
            except Exception as exc:
                logging.warning("local paragraph %d failed: %s", i, str(exc))
                _local_error = str(exc)
                results.append(None)
            if on_progress:
                on_progress(i + 1, len(texts))
        return results
    results = [None] * len(texts)
    total = len(texts)
    done = 0
    pending = []

    def progress(amount=1):
        nonlocal done
        done += amount
        if on_progress:
            on_progress(done, total)

    for i, text in enumerate(texts):
        if not _needs_translation(text):
            results[i] = text
            progress()
            continue
        cached = cache_get(text)
        if cached is None:
            pending.append(i)
        else:
            results[i] = cached
            progress()

    # An oversized OCR paragraph (too many chars or too long a URL when
    # encoded) must be split into sentence-sized requests; never send it as a
    # single GET query (Mozhi/Bing returns HTTP 500, proxies return 431/414).
    unresolved = [i for i in pending
                  if not _fits_batch("ZXQPX%dZX %s" % (i, _prepare_translation(texts[i])[0]))]
    unresolved_set = set(unresolved)
    short_pending = [i for i in pending if i not in unresolved_set]
    batches = _make_batches(short_pending, texts)
    if batches:
        futures = {_translate_executor.submit(_translate_batch, batch, texts): batch
                   for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                values = future.result()
            except Exception as exc:
                logging.warning("batch translation failed: %s", exc.__class__.__name__)
                # If all nodes were already tried, repeating the same
                # outage once per paragraph only makes the UI wait.  A
                # marker/format problem can still benefit from individual
                # fallback requests.
                if isinstance(exc, TranslationError) and str(exc) in (
                        "all translation services unavailable",
                        "attempts exhausted"):
                    for _ in batch:
                        progress()
                else:
                    unresolved.extend(batch)
                continue
            for i in batch:
                results[i] = values[i]
                cache_set(texts[i], values[i])
                progress()

    def work(i, text):
        try:
            return i, translate_paragraph(text)
        except Exception as exc:
            logging.warning("paragraph %d translation failed: %s", i, exc.__class__.__name__)
            return i, None

    if unresolved:
        futures = [_translate_executor.submit(work, i, texts[i]) for i in unresolved]
        for future in as_completed(futures):
            i, value = future.result()
            results[i] = value
            progress()
    return results


# ---------------------------------------------------------------------------
# layout: pixel-width wrapping, unified font, per-column collision-free boxes
# ---------------------------------------------------------------------------

class LayoutError(Exception):
    pass


def font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT, max(MIN_FONT_SIZE, size))
    except Exception:
        return ImageFont.load_default()


def wrap_text_by_pixels(text, font, max_width, draw):
    """Wrap ``text`` into lines whose measured pixel width never exceeds max_width.

    CJK characters may break anywhere; latin words are kept whole and hard-split
    only when a single word is wider than the box.
    """
    if max_width <= 0:
        return [text]
    tokens = []
    buf = []

    def flush():
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    for ch in text:
        if ch == "\n":
            flush()
            tokens.append("\n")
        elif ch.isspace():
            flush()
            tokens.append(" ")
        elif _is_cjk(ch):
            flush()
            tokens.append(ch)
        else:
            buf.append(ch)
    flush()

    lines = []
    current = ""
    for tok in tokens:
        if tok == "\n":
            if current:
                lines.append(current)
                current = ""
            continue
        candidate = current + tok if current else tok.lstrip(" \t")
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # a single over-wide token (long latin word / number) is hard-split
        while draw.textlength(tok, font=font) > max_width and len(tok) > 1:
            lo, hi = 1, len(tok)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if draw.textlength(tok[:mid], font=font) <= max_width:
                    lo = mid
                else:
                    hi = mid - 1
            lines.append(tok[:lo])
            tok = tok[lo:]
        current = tok
    if current or not lines:
        lines.append(current)
    return lines


def detect_columns(paragraphs):
    """Cluster paragraph indices into columns by x range overlap.

    Two paragraphs belong to the same column when their horizontal extents
    overlap (or nearly touch, within BOX_GAP).  Each column is later stacked
    vertically on its own anchors, so columns never merge into one chain.
    """
    order = sorted(range(len(paragraphs)), key=lambda i: (paragraphs[i]["box"][0], paragraphs[i]["box"][1]))
    columns = []  # each: {"left", "right", "indices"}
    for i in order:
        x, w = paragraphs[i]["box"][0], paragraphs[i]["box"][2]
        p_right = x + w
        for col in columns:
            if p_right + BOX_GAP >= col["left"] and x - BOX_GAP <= col["right"]:
                col["left"] = min(col["left"], x)
                col["right"] = max(col["right"], p_right)
                col["indices"].append(i)
                break
        else:
            columns.append({"left": x, "right": p_right, "indices": [i]})
    for col in columns:
        col["indices"].sort(key=lambda i: (paragraphs[i]["box"][1], paragraphs[i]["box"][0]))
    columns.sort(key=lambda c: c["left"])
    return columns


def _overlaps_any(rect, placed, gap=BOX_GAP):
    x, y, w, h = rect
    for px, py, pw, ph in placed:
        if not (x + w + gap <= px or px + pw + gap <= x
                or y + h + gap <= py or py + ph + gap <= y):
            return True
    return False


def validate_layout(layouts, image_size):
    """Independent final assertion: no intersecting boxes, no out-of-canvas,
    no clipped text.  Returns (ok: bool, reason: str)."""
    iw, ih = image_size
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for i, lay in enumerate(layouts):
        x, y, w, h = lay["rect"]
        if not (0 <= x and 0 <= y and x + w <= iw and y + h <= ih):
            return False, "box %d out of canvas %r in %r" % (i, (x, y, w, h), image_size)
        content_w = w - 2 * PAD_H
        for ln in lay["lines"]:
            if probe.textlength(ln, font=lay["font"]) > content_w + 0.5:
                return False, "paragraph %d line clipped horizontally: %r" % (i, ln)
        text_bottom = y + PAD_V + lay["line_h"] * len(lay["lines"])
        if text_bottom > y + h + 0.5:
            return False, "paragraph %d text clipped vertically" % i
    for i in range(len(layouts)):
        for j in range(i + 1, len(layouts)):
            if _overlaps_any(layouts[i]["rect"], [layouts[j]["rect"]], gap=BOX_GAP):
                return False, "boxes %d and %d intersect" % (i, j)
    return True, "ok"


def place_paragraphs(paragraphs, translated, font_size, canvas, grow=False):
    """Replace changed text at its anchor, reserving all other source text.

    Bottom overflow can grow the replacement at its anchor. In the growing
    layout, translated paragraphs can move down together. Unchanged labels
    stay reserved; translations that cannot fit around them appear below.
    """
    img_w, img_h = canvas
    f = font(font_size)
    ascent, descent = f.getmetrics()
    line_h = ascent + descent + 2
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # reading order: top to bottom, then left to right
    global_order = sorted(range(len(paragraphs)),
                          key=lambda i: (paragraphs[i]["box"][1], paragraphs[i]["box"][0]))

    placed = []                 # every placed rect, across all columns
    layouts = []
    out_h = img_h
    changed = {i for i, text in enumerate(translated)
               if text and _clean_ocr_text(text) != _clean_ocr_text(paragraphs[i]["text"])}

    for i in global_order:
        para = paragraphs[i]
        if i not in changed:
            continue
        x, y, ow, oh = para["box"]
        out_text = translated[i].strip()
        other_sources = [p["box"] for j, p in enumerate(paragraphs) if j != i]
        bx = max(0, x - PAD_H)
        by = max(0, y - PAD_V)
        right_limit = img_w
        for sx, sy, sw, sh in other_sources:
            if sx >= x + ow and sy < y + max(oh, line_h) + PAD_V and sy + sh > by:
                right_limit = min(right_limit, sx - BOX_GAP)
        avail_w = right_limit - bx - 2 * PAD_H
        natural_w = math.ceil(max(probe.textlength(line, font=f)
                                  for line in out_text.split("\n")))
        max_text_width = max(1, min(max(ow, natural_w), avail_w))
        lines = wrap_text_by_pixels(out_text, f, max_text_width, probe)
        content_h = line_h * len(lines)
        box_w = min(max_text_width + 2 * PAD_H, img_w)
        # Cover the complete source paragraph, not only the (often shorter)
        # Chinese translation, otherwise untranslated English remains visible
        # below the overlay and looks like overlapping text.
        box_h = max(content_h, oh) + 2 * PAD_V

        if grow:
            # Changed source text is erased before rendering. It can safely
            # be used for the preceding translation and reflowed below it.
            other_sources = [p["box"] for j, p in enumerate(paragraphs) if j != i and j not in changed]
            while _overlaps_any((bx, by, box_w, box_h), placed):
                by = max(py + ph + BOX_GAP for px, py, pw, ph in placed
                         if _overlaps_any((bx, by, box_w, box_h), [(px, py, pw, ph)]))

        # A source at the image edge can extend beyond the padded *text*
        # width while still being fully covered by the replacement rectangle.
        # Check the actual rectangle, not its inner text width.
        detached = (bx + box_w < x + ow or bx + box_w > img_w or
                    _overlaps_any((bx, by, box_w, box_h), other_sources + placed))
        if not detached and by + box_h > img_h:
            if not grow:
                raise LayoutError("replacement needs more canvas height at font %d" % font_size)
            out_h = max(out_h, by + box_h)
        if detached:
            if not grow:
                raise LayoutError("replacement would cover source text at font %d" % font_size)
            bx, by = 0, out_h + BOX_GAP
            max_text_width = max(1, img_w - 2 * PAD_H)
            lines = wrap_text_by_pixels(out_text, f, max_text_width, probe)
            box_w = img_w
            box_h = line_h * len(lines) + 2 * PAD_V
            out_h = by + box_h

        layouts.append({
            "text": out_text,
            "orig": (x, y, ow, oh),
            "lines": lines,
            "rect": (bx, by, box_w, box_h),
            "font": f,
            "font_size": font_size,
            "line_h": line_h,
            "detached": detached,
        })
        placed.append((bx, by, box_w, box_h))

    return layouts, (img_w, out_h)


def calculate_layout(paragraphs, translated, img_size, start_size=None):
    """Full layout: try unified font 18 -> 16 -> 14, then grow the canvas.

    Returns (layouts, canvas). Only changed paragraphs get boxes. At the
    minimum font size, reflow translated paragraphs downward. Labels that
    remain unchanged are reserved; crowded translations go below the image.
    """
    size = start_size or TRANSLATION_FONT_SIZE
    canvas = tuple(img_size)
    while True:
        try:
            layouts, canvas = place_paragraphs(paragraphs, translated, size, canvas, grow=False)
            ok, reason = validate_layout(layouts, canvas)
            if ok:
                return layouts, canvas
            raise LayoutError("invalid layout at font %d: %s" % (size, reason))
        except LayoutError:
            if size > MIN_FONT_SIZE:
                size -= FONT_STEP
                logging.info("layout did not fit at font %d, retrying at %d",
                             size + FONT_STEP, size)
                continue
            # minimum font size: grow the canvas instead of overlapping
            layouts, canvas = place_paragraphs(paragraphs, translated, size, canvas, grow=True)
            canvas = (canvas[0], max(canvas[1],
                                     max((l["rect"][1] + l["rect"][3] for l in layouts), default=canvas[1])))
            ok, reason = validate_layout(layouts, canvas)
            if not ok:
                raise RuntimeError("layout cannot be made valid: %s" % reason)
            return layouts, canvas


def _grow_canvas(image, canvas):
    cw, ch = canvas
    if cw <= image.width and ch <= image.height:
        return image
    # fill the added strip with the bottom edge color so it blends in
    try:
        fill = image.getpixel((image.width // 2, image.height - 1))
    except Exception:
        fill = (255, 255, 255)
    new = Image.new("RGB", (cw, ch), fill)
    new.paste(image, (0, 0))
    return new


def render_translation(paragraphs, translated, source=None, start_size=None):
    """Render changed paragraphs without covering preserved source content.

    All paragraphs share one font size (18 default; 16 or 14 for the whole image
    when needed). Replacement backgrounds follow the source theme; boxes never
    overlap and the final layout is validated before drawing. Returns the
    output PNG path.
    """
    global RESULT_FOCUS_Y
    source_image = Image.open(source or CACHE).convert("RGB")
    layouts, canvas = calculate_layout(paragraphs, translated, source_image.size, start_size)
    image = _grow_canvas(source_image.copy(), canvas)

    ok, reason = validate_layout(layouts, canvas)
    if not ok:
        raise RuntimeError("refusing to draw invalid layout: %s" % reason)

    draw = ImageDraw.Draw(image, "RGBA")
    backgrounds = []
    # Erase all successfully translated originals first, including paragraphs
    # that moved. Otherwise their English source would remain above the output.
    for lay in layouts:
        sx, sy, sw, sh = lay["orig"]
        samples = [source_image.getpixel((min(source_image.width - 1, max(0, px)),
                                         min(source_image.height - 1, max(0, py))))
                   for px in (sx - 2, sx + sw // 2, sx + sw + 2)
                   for py in (sy - 2, sy + sh + 2)]
        background = sorted(samples, key=lambda c: sum(c))[len(samples) // 2]
        backgrounds.append(background)
        draw.rectangle((sx, sy, sx + sw - 1, sy + sh - 1), fill=background + (255,))
    changed_boxes = {lay["orig"] for lay in layouts}
    for para in paragraphs:
        if tuple(para["box"]) not in changed_boxes:
            sx, sy, sw, sh = para["box"]
            image.paste(source_image.crop((sx, sy, sx + sw, sy + sh)), (sx, sy))
    for lay, background in zip(layouts, backgrounds):
        x, y, w, h = lay["rect"]
        foreground = (240, 240, 240) if sum(background) < 384 else (25, 25, 25)
        draw.rectangle((x, y, x + w - 1, y + h - 1), fill=background + (255,))
        ty = y + PAD_V
        for line in lay["lines"]:
            draw.text((x + PAD_H, ty), line, font=lay["font"], fill=foreground + (255,))
            ty += lay["line_h"]
    out = ROOT / "translated.png"
    image.save(out)
    RESULT_FOCUS_Y = min((lay["rect"][1] for lay in layouts), default=0)
    return out


# ---------------------------------------------------------------------------
# GUI: selector, progress, result, settings
# ---------------------------------------------------------------------------

def show_on_monitor(window, monitor):
    """Give the window manager an explicit position on the capture monitor."""
    area = monitor.get_workarea()
    window.set_position(Gtk.WindowPosition.NONE)
    window.get_child().show_all()
    _minimum, natural = window.get_preferred_size()
    default_width, default_height = window.get_default_size()
    width = min(max(default_width, natural.width), max(1, area.width - 48))
    height = min(max(default_height, natural.height), max(1, area.height - 48))
    window.set_default_size(width, height)
    window.move(area.x + (area.width - width) // 2,
                area.y + (area.height - height) // 2)

    def center(mapped_window, _event):
        width, height = mapped_window.get_size()
        mapped_window.move(area.x + max(0, (area.width - width) // 2),
                           area.y + max(0, (area.height - height) // 2))
        return False

    window.connect("map-event", center)
    window.show_all()


class Selector(Gtk.Window):
    def __init__(self, screenshot, callback, monitor=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        # Paint a frozen screenshot as the window background.  Relying on an
        # RGBA/transparent toplevel produces an opaque black window on some
        # GNOME/X11 compositing configurations.
        self.screenshot = screenshot
        self.monitor = monitor if monitor is not None else pointer_monitor()
        self.bounds = monitor_bounds(self.monitor)
        self.callback = callback
        self.start = None
        self.end = None
        self.keyboard_grabbed = False
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_keep_above(True)
        self.set_accept_focus(True)
        self.set_modal(True)
        x, y, width, height = self.bounds
        self.move(x, y)
        self.set_default_size(width, height)
        screen = self.get_screen()
        monitor_index = screen.get_monitor_at_point(x + width // 2, y + height // 2)
        self.fullscreen_on_monitor(screen, monitor_index)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self.draw_overlay)
        self.connect("button-press-event", self.press)
        self.connect("button-release-event", self.release)
        self.connect("motion-notify-event", self.motion)
        self.connect("key-press-event", self.key)
        self.connect("destroy", self.release_keyboard)
        self.show_all()
        self.present()
        self.grab_focus()
        GLib.idle_add(self.grab_keyboard)
        cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "crosshair")
        if self.get_window():
            self.get_window().set_cursor(cursor)

    def grab_keyboard(self):
        window = self.get_window()
        if window is not None:
            status = Gdk.keyboard_grab(window, False, Gdk.CURRENT_TIME)
            self.keyboard_grabbed = status == Gdk.GrabStatus.SUCCESS
            if not self.keyboard_grabbed:
                window.focus(Gdk.CURRENT_TIME)
        return False

    def release_keyboard(self, _widget):
        if self.keyboard_grabbed:
            Gdk.keyboard_ungrab(Gdk.CURRENT_TIME)
            self.keyboard_grabbed = False

    def key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            Gtk.main_quit()
        return True

    def press(self, _widget, event):
        self.start = self.event_point(event)
        self.end = self.start
        self.queue_draw()
        return True

    def motion(self, _widget, event):
        if self.start:
            self.end = self.event_point(event)
            self.queue_draw()
        return True

    def event_point(self, event):
        allocation = self.get_allocation()
        return (min(allocation.width, max(0, int(event.x))),
                min(allocation.height, max(0, int(event.y))))

    def release(self, _widget, event):
        if not self.start:
            return True
        self.end = self.event_point(event)
        rect = selection_region(self.start, self.end, self.get_allocation(), self.bounds)
        self.destroy()
        if rect[2] >= 8 and rect[3] >= 8:
            self.callback(rect)
        else:
            Gtk.main_quit()
        return True

    def draw_overlay(self, _widget, cr):
        alloc = self.get_allocation()
        image_width = self.screenshot.get_width()
        image_height = self.screenshot.get_height()
        cr.save()
        cr.scale(alloc.width / image_width, alloc.height / image_height)
        Gdk.cairo_set_source_pixbuf(cr, self.screenshot, 0, 0)
        cr.paint()
        cr.restore()

        # A light mask keeps the frozen screen visible while selecting.
        cr.set_source_rgba(0.02, 0.02, 0.02, 0.28)
        cr.rectangle(0, 0, alloc.width, alloc.height)
        cr.fill()
        if self.start and self.end:
            x1, y1 = self.start
            x2, y2 = self.end
            x, y, w, h = min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)
            cr.set_source_rgba(0.15, 0.65, 1.0, 0.18)
            cr.rectangle(x, y, w, h)
            cr.fill()
            cr.set_source_rgba(0.15, 0.75, 1.0, 1.0)
            cr.set_line_width(2)
            cr.rectangle(x, y, w, h)
            cr.stroke()
        return False


class ProgressWindow(Gtk.Window):
    """Non-blocking progress window updated from the worker thread via GLib.idle_add."""

    def __init__(self, monitor=None):
        super().__init__(title="截图翻译")
        self.monitor = monitor if monitor is not None else pointer_monitor()
        self.cancelled = False
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(360, 96)
        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self._key)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(18)
        self.label = Gtk.Label(label="正在截取画面……")
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        box.pack_start(self.label, True, True, 0)
        box.pack_start(self.spinner, False, False, 0)
        self.add(box)
        show_on_monitor(self, self.monitor)
        self.present()
        self.grab_focus()

    def _on_destroy(self, _widget):
        pass

    def _key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.cancel()
            return True
        return False

    def cancel(self):
        self.cancelled = True
        self.destroy()
        Gtk.main_quit()

    def set_text(self, text):
        self.label.set_text(text)

    def is_cancelled(self):
        return self.cancelled


def show_progress(monitor=None):
    return ProgressWindow(monitor)


class ResultWindow(Gtk.Window):
    def __init__(self, rect, translated_text, monitor=None):
        super().__init__(title="截图翻译")
        self.monitor = monitor if monitor is not None else pointer_monitor()
        self.rect = rect
        self.translated_text = translated_text
        area = self.monitor.get_workarea()
        viewport_width = min(rect[2], max(1, area.width - 48))
        viewport_height = min(rect[3], max(1, area.height - 100))
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.key)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bar = Gtk.Box(spacing=4)
        menu = Gtk.Menu()
        for label, action in [("提取中文", self.extract_zh), ("提取英文", self.extract_en),
                              ("重新框选", self.reselect), ("快捷键设置", self.settings),
                              ("复制翻译", self.copy_translation)]:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _item, fn=action: fn())
            menu.append(item)
        menu.show_all()
        menu_button = Gtk.MenuButton(label="操作 ▾") if viewport_width >= 100 else Gtk.MenuButton()
        menu_button.set_popup(menu)
        bar.pack_start(menu_button, False, False, 0)
        for icon, label, action in [("view-refresh-symbolic", "重新框选（Ctrl+Alt+O）", self.reselect),
                                    ("edit-copy-symbolic", "复制翻译", self.copy_translation)]:
            if viewport_width < 200:
                continue
            button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            button.set_tooltip_text(label)
            button.connect("clicked", lambda _b, fn=action: fn())
            bar.pack_start(button, False, False, 0)
        bar.set_tooltip_text("Ctrl+Alt+O：重新框选截图；Esc：关闭")
        box.pack_start(bar, False, False, 0)
        # Match the viewport to the logical capture dimensions; do not let
        # a long toolbar or the image's HiDPI pixel dimensions enlarge it.
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(ROOT / "translated.png"))
        image_scale = rect[2] / pixbuf.get_width()
        if pixbuf.get_width() != rect[2]:
            pixbuf = pixbuf.scale_simple(rect[2], max(1, round(pixbuf.get_height() * rect[2] / pixbuf.get_width())),
                                        GdkPixbuf.InterpType.BILINEAR)
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.set_valign(Gtk.Align.START)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_width(viewport_width)
        scroll.set_min_content_height(viewport_height)
        scroll.add(image)
        # GTK learns the image height during allocation. Position the first
        # view at translated content, including a fallback below the capture.
        focused = False

        def focus_translation(_widget, _allocation):
            nonlocal focused
            if not focused:
                focused = True
                adjustment = scroll.get_vadjustment()
                adjustment.set_value(max(0, min(RESULT_FOCUS_Y * image_scale - 8,
                                               adjustment.get_upper() - adjustment.get_page_size())))

        scroll.connect("size-allocate", focus_translation)
        box.pack_start(scroll, True, True, 0)
        self.add(box)
        bar.show_all()
        _minimum, toolbar_size = bar.get_preferred_size()
        self.set_default_size(viewport_width, viewport_height + toolbar_size.height)
        show_on_monitor(self, self.monitor)
        self.present()

    def key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def extract_zh(self):
        self.show_text(run_extract("chi_sim"))

    def extract_en(self):
        self.show_text(run_extract("eng"))

    def show_text(self, text):
        dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.CLOSE, "提取结果")
        dialog.format_secondary_text(text or "未识别到文字")
        dialog.run()
        dialog.destroy()

    def copy_translation(self):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.translated_text, -1)

    def reselect(self):
        subprocess.Popen([sys.executable, str(Path(__file__)), "--select"])
        self.destroy()

    def settings(self):
        subprocess.Popen([sys.executable, str(Path(__file__)), "--settings"])


def show_result(rect, translated_text, monitor=None):
    ResultWindow(rect, translated_text, monitor)


def show_friendly_error(message="翻译服务暂时无法连接，请稍后重试。截图和文字识别已经完成。",
                        progress=None):
    """Show a short Chinese error; details already went to the log file."""
    def _show():
        if progress is not None:
            progress.destroy()
        dialog = Gtk.MessageDialog(None, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "截图翻译失败")
        dialog.format_secondary_text(message)
        show_on_monitor(dialog, progress.monitor if progress is not None else pointer_monitor())
        dialog.run()
        dialog.destroy()
        Gtk.main_quit()
        return False
    GLib.idle_add(_show)


def show_busy_dialog():
    dialog = Gtk.MessageDialog(None, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.CLOSE, "截图翻译")
    dialog.format_secondary_text("已有截图翻译任务正在进行，请稍候再试。")
    show_on_monitor(dialog, pointer_monitor())
    dialog.run()
    dialog.destroy()


def run_extract(lang):
    return _run_tesseract(CACHE, lang, "6").strip()


KEYBINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/screenshot-translator/"


def shortcut_to_gnome(value):
    parts = [p.strip().lower() for p in value.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return ""
    mods = {"ctrl": "<Primary>", "control": "<Primary>", "alt": "<Alt>", "shift": "<Shift>", "super": "<Super>", "win": "<Super>"}
    return "".join(mods.get(p, "") for p in parts[:-1]) + parts[-1]


def gnome_to_shortcut(value):
    text = value.replace("<Primary>", "Ctrl+").replace("<Alt>", "Alt+").replace("<Shift>", "Shift+").replace("<Super>", "Super+")
    return text[:-1] + text[-1:].upper() if text else ""


class SettingsWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="截图翻译设置")
        self.set_default_size(460, 220)
        self.set_border_width(16)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.key)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        backend_hint = ("翻译方式：本地英→中 · 机器人／机械术语增强"
                        if TRANSLATION_BACKEND == "local" else "翻译方式：在线英→中")
        box.pack_start(Gtk.Label(label=backend_hint, xalign=0), False, False, 0)
        box.pack_start(Gtk.Label(label="快捷键（例如 Ctrl+Alt+O）", xalign=0), False, False, 0)
        self.entry = Gtk.Entry()
        current = subprocess.run(["gsettings", "get", "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:" + KEYBINDING_PATH, "binding"], text=True, stdout=subprocess.PIPE).stdout.strip().strip("'")
        self.entry.set_text(gnome_to_shortcut(current) or "Ctrl+Alt+O")
        box.pack_start(self.entry, False, False, 0)
        buttons = Gtk.Box(spacing=8)
        save = Gtk.Button(label="保存快捷键")
        save.connect("clicked", self.save_shortcut)
        reset = Gtk.Button(label="清除固定区域")
        reset.connect("clicked", self.reset_region)
        select = Gtk.Button(label="重新框选")
        select.connect("clicked", lambda _b: (subprocess.Popen([sys.executable, str(Path(__file__)), "--select"]), self.destroy()))
        buttons.pack_start(save, False, False, 0)
        buttons.pack_start(reset, False, False, 0)
        buttons.pack_start(select, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        box.pack_start(Gtk.Label(label="按快捷键后框选要识别和翻译的区域；按 Esc 可随时取消。", xalign=0), False, False, 0)
        self.add(box)
        show_on_monitor(self, pointer_monitor())

    def key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def save_shortcut(self, _button):
        binding = shortcut_to_gnome(self.entry.get_text())
        if not binding:
            return
        parent_schema = "org.gnome.settings-daemon.plugins.media-keys"
        shortcut_schema = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:" + KEYBINDING_PATH
        current_text = subprocess.run(
            ["gsettings", "get", parent_schema, "custom-keybindings"],
            text=True, stdout=subprocess.PIPE, check=True,
        ).stdout.strip()
        try:
            paths = ast.literal_eval(current_text)
        except (SyntaxError, ValueError):
            paths = []
        if KEYBINDING_PATH not in paths:
            paths.append(KEYBINDING_PATH)
            subprocess.run(["gsettings", "set", parent_schema, "custom-keybindings", repr(paths)], check=True)
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "--select"])
        subprocess.run(["gsettings", "set", shortcut_schema, "name", "截图翻译"], check=True)
        subprocess.run(["gsettings", "set", shortcut_schema, "command", command], check=True)
        subprocess.run(["gsettings", "set", shortcut_schema, "binding", binding], check=True)
        dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.CLOSE, "快捷键已保存")
        dialog.format_secondary_text("下次按 " + self.entry.get_text() + " 截图。")
        dialog.run()
        dialog.destroy()

    def reset_region(self, _button):
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def process(rect, pix=None, progress=None, lock=None, bounds=None, monitor=None):
    """Capture -> OCR -> translate -> layout -> render, off the GTK main thread.

    GTK widgets are only touched through GLib.idle_add().  Partial translation
    failure is fine: failed paragraphs fall back to their original text.  A
    friendly error is shown only when nothing could be translated (or the
    pipeline broke).  The task lock is released once the result/error is shown.
    """
    def stage(text):
        if progress is not None:
            GLib.idle_add(lambda: progress.set_text(text) or False)

    def finish_success():
        release_task_lock(lock)
        if progress is not None:
            progress.destroy()
        show_result(rect, translated_text, monitor)
        return False

    def finish_error(message):
        release_task_lock(lock)
        show_friendly_error(message, progress)
        return False

    try:
        stage("正在截取画面……")
        capture(rect, pix, bounds)
        if progress is not None and progress.is_cancelled():
            return
        stage("正在识别文字……")
        paragraphs = parse_ocr_paragraphs(CACHE)
        if progress is not None and progress.is_cancelled():
            return
        if not paragraphs:
            logging.warning("OCR found no text in %s", rect)
            GLib.idle_add(finish_error, "没有识别到文字，请调整选区后重试。")
            return

        texts = [p["text"] for p in paragraphs]
        total = len(texts)
        translation_label = "正在本地翻译" if TRANSLATION_BACKEND == "local" else "正在翻译"
        stage("%s 0/%d……" % (translation_label, total))

        def on_translate_progress(done, total_count):
            if progress is not None:
                GLib.idle_add(lambda: progress.set_text("%s %d/%d……" % (translation_label, done, total_count)) or False)

        results = translate_paragraphs(texts, on_translate_progress)
        save_cache()
        if progress is not None and progress.is_cancelled():
            return

        required = [i for i, text in enumerate(texts) if _needs_translation(text)]
        succeeded = sum(1 for i in required if results[i])
        if required and succeeded == 0:
            logging.warning("all %d English paragraphs failed to translate", len(required))
            message = ("本地翻译失败：%s" % (_local_error or "请检查本地模型和运行依赖。")
                       if TRANSLATION_BACKEND == "local" else
                       "翻译服务暂时无法连接，请稍后重试。截图和文字识别已经完成。")
            GLib.idle_add(finish_error, message)
            return

        stage("正在生成翻译图片……")
        render_translation(paragraphs, results)
        if progress is not None and progress.is_cancelled():
            return

        state = load_state()
        state["region"] = list(rect)
        save_state(state)
        translated_text = "\n\n".join(r if r else p["text"] for r, p in zip(results, paragraphs))
        GLib.idle_add(finish_success)
    except Exception as exc:
        logging.error("pipeline failed", exc_info=True)
        GLib.idle_add(finish_error, "处理截图时出错：%s" % exc)


def main():
    # Opening the program itself should show its controls, not immediately
    # take over the screen with a selection window.  The registered keyboard
    # shortcut explicitly invokes --select.
    load_translation_cache()
    mode = sys.argv[1] if len(sys.argv) > 1 else "--settings"
    if mode == "--settings":
        SettingsWindow()
        Gtk.main()
        return

    lock = acquire_task_lock()
    if lock is None:
        show_busy_dialog()
        return

    state = load_state()
    monitor = pointer_monitor()
    bounds = monitor_bounds(monitor)
    forced = mode == "--select"
    rect = state.get("region")
    # A saved region on another monitor must never capture that monitor.
    if forced or not region_within_bounds(rect, bounds):
        # Capture before showing the dark selection layer.  Otherwise the
        # overlay itself can be captured and the result appears black.
        base = capture_screen(monitor)

        def selected(r):
            progress = show_progress(monitor)
            threading.Thread(target=process, args=(r, base),
                             kwargs={"progress": progress, "lock": lock,
                                     "bounds": bounds, "monitor": monitor}, daemon=True).start()

        Selector(base, selected, monitor)
        Gtk.main()
    else:
        # Capture on the main thread (Gdk is not thread-safe), then translate on
        # a worker while the progress window keeps the loop responsive.
        base = capture_screen(monitor)
        progress = show_progress(monitor)
        threading.Thread(target=process, args=(tuple(rect), base),
                         kwargs={"progress": progress, "lock": lock,
                                 "bounds": bounds, "monitor": monitor}, daemon=True).start()
        Gtk.main()


if __name__ == "__main__":
    main()
