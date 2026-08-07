#!/usr/bin/env python3
"""A small Youdao-style screenshot translator for X11/GNOME.

Ctrl+Alt+O draws a rectangle on the frozen desktop (black-screen safe: the
frozen screenshot is painted before the dark overlay).  OCR text is grouped
into paragraphs by (block_num, par_num); each paragraph is translated once via
the Mozhi GET /api/translate API (query params, session reuse, node/engine
fallback, disk cache, max 4 attempts); the Chinese translation is rendered back
into the captured image at the original paragraph positions with a unified font
size, per-column collision-free boxes, and a final validate_layout() assertion.
A non-blocking progress window tracks capture/OCR/translation on a worker
thread while a single-instance lock keeps repeated shortcut presses from
starting a second task.
"""
import ast
import csv
import fcntl
import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
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
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

TRANSLATION_FONT_SIZE = 18
MIN_FONT_SIZE = 14
MAX_FONT_SIZE = 20
FONT_STEP = 2
BOX_GAP = 4            # minimum px gap between translation boxes
PAD_H, PAD_V = 6, 4    # inner padding of a translation box
OCR_PSM = "3"          # automatic segmentation keeps separate columns as blocks
OCR_SCALE = 2          # raise small glyphs above Tesseract's minimum x-height

# Mozhi endpoint is GET /api/translate with query params (POST returns HTTP 500).
DEFAULT_NODES = [
    "https://translate.projectsegfau.lt",
    "https://mozhi.adminforge.de",
    "https://mozhi.ducks.party",
]
# allow overriding the node list for testing/firewalls: MOZHI_NODES=a,b,c
_env_nodes = os.environ.get("MOZHI_NODES", "").strip()
NODES = [n.strip() for n in _env_nodes.split(",") if n.strip()] if _env_nodes else DEFAULT_NODES
# DuckDuckGo is backed by Bing in Mozhi and is the endpoint verified against
# all configured instances. Trying extra engines after a network failure only
# makes an interactive screenshot wait longer.
ENGINES = ["duckduckgo"]
MAX_TRANSLATE_WORKERS = 3
MAX_ATTEMPTS_PER_PARAGRAPH = 3
MAX_BATCH_CHARS = 650

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


DESKTOP_PROXY = detect_desktop_proxy()
if DESKTOP_PROXY:
    logging.info("desktop proxy enabled")

# requests.Session is only used from one worker thread at a time; still, keep a
# thread-local session per worker as requested instead of a shared object.
_tls = threading.local()


def _tls_session():
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) screenshot-translator"})
        if DESKTOP_PROXY:
            s.proxies.update({"http": DESKTOP_PROXY, "https": DESKTOP_PROXY})
        _tls.session = s
    return s


_cache = {}
_cache_lock = threading.Lock()


def load_translation_cache():
    global _cache
    try:
        _cache = json.loads(CACHE_FILE.read_text())
    except Exception:
        _cache = {}


def cache_get(text):
    with _cache_lock:
        return _cache.get(text)


def cache_set(text, value):
    with _cache_lock:
        _cache[text] = value
        if len(_cache) > 300:
            for k in list(_cache)[:len(_cache) - 300]:
                del _cache[k]
        try:
            CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False))
        except Exception as exc:
            logging.warning("cache save failed: %s", exc.__class__.__name__)


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


def screen_pixbuf():
    root = Gdk.get_default_root_window()
    if root is None:
        raise RuntimeError("无法访问桌面画面，请确认当前为 X11 会话")
    pixbuf = Gdk.pixbuf_get_from_window(root, 0, 0, root.get_width(), root.get_height())
    if pixbuf is None:
        raise RuntimeError("无法读取桌面画面，请确认当前为 X11 会话")
    return pixbuf


def capture_screen():
    return screen_pixbuf()


def capture(rect, pix=None):
    pix = pix or screen_pixbuf()
    x, y, w, h = rect
    crop = pix.new_subpixbuf(x, y, w, h).copy()
    crop.savev(str(CACHE), "png", [], [])
    return rect


# ---------------------------------------------------------------------------
# OCR -> paragraphs
# ---------------------------------------------------------------------------

def _run_tesseract(path, lang, psm, output=None):
    """Run one OCR pass on a 2x image and return decoded stdout."""
    with Image.open(path) as source:
        resized = source.convert("RGB").resize(
            (source.width * OCR_SCALE, source.height * OCR_SCALE),
            Image.LANCZOS,
        )
    encoded = io.BytesIO()
    resized.save(encoded, "PNG", dpi=(300, 300))
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


def parse_ocr_paragraphs(path, lang="eng+chi_sim"):
    """Run tesseract TSV and group words into paragraphs by block_num+par_num.

    Each paragraph gets its overall bounding box and multi-line text.  Columns
    survive because --psm 3 gives distinct block_num to separate text blocks.
    """
    tsv = _run_tesseract(path, lang, OCR_PSM, "tsv")
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
                    left == 0 and top == 0 and page_height and
                    height >= page_height * 0.8 and width * 2 < height):
                continue
            words.append({
                "block_num": int(row["block_num"]),
                "par_num": int(row["par_num"]),
                "line_num": int(row["line_num"]),
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
        ws.sort(key=lambda w: w["left"])
        line_items.append({
            "block_num": block_num, "par_num": par_num,
            "text": _join_ocr_tokens(w["text"] for w in ws),
            "left": min(w["left"] for w in ws),
            "top": min(w["top"] for w in ws),
            "right": max(w["left"] + w["width"] for w in ws),
            "bottom": max(w["top"] + w["height"] for w in ws),
        })

    paras = {}
    for li in line_items:
        paras.setdefault((li["block_num"], li["par_num"]), []).append(li)

    paragraphs = []
    for (block_num, par_num), lis in paras.items():
        lis.sort(key=lambda l: (l["top"], l["left"]))
        left = min(l["left"] for l in lis)
        top = min(l["top"] for l in lis)
        right = max(l["right"] for l in lis)
        bottom = max(l["bottom"] for l in lis)
        source_left = left // OCR_SCALE
        source_top = top // OCR_SCALE
        source_right = (right + OCR_SCALE - 1) // OCR_SCALE
        source_bottom = (bottom + OCR_SCALE - 1) // OCR_SCALE
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


def _translate_remote(text):
    """Translate one string via Mozhi without consulting the local cache.

    Prefers the duckduckgo engine, caps total attempts at 3 per request, and
    switches node quickly when a node is unreachable.  Returns the translated
    text or raises TranslationError.  Log lines carry host + engine + exception
    class only, never the source text or a URL with a query string.
    """
    s = _tls_session()
    attempts = 0
    for node in NODES:
        for engine in ENGINES:
            attempts += 1
            if attempts > MAX_ATTEMPTS_PER_PARAGRAPH:
                raise TranslationError("attempts exhausted")
            try:
                response = s.get(
                    node + "/api/translate",
                    params={"from": "auto", "to": "zh", "engine": engine, "text": text},
                    timeout=(2, 8),
                )
                if response.status_code == 200:
                    data = response.json()
                    value = (data.get("translated-text")
                             or data.get("translation")
                             or data.get("translatedText"))
                    if value:
                        return value
                    logging.warning("translate empty node=%s engine=%s", node, engine)
                else:
                    logging.warning("translate http node=%s engine=%s status=%s",
                                    node, engine, response.status_code)
            except requests.exceptions.RequestException as exc:
                logging.warning("translate request error node=%s engine=%s exc=%s",
                                node, engine, exc.__class__.__name__)
                if isinstance(exc, (requests.exceptions.ConnectTimeout,
                                    requests.exceptions.ConnectionError,
                                    requests.exceptions.ReadTimeout)):
                    # node unreachable/slow: switch node instead of burning tries
                    break
            except Exception as exc:
                logging.warning("translate error node=%s engine=%s exc=%s",
                                node, engine, exc.__class__.__name__)
    raise TranslationError("all translation services unavailable")


def _clean_ocr_text(text):
    """Undo PDF line wrapping before sending OCR text for translation."""
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return _join_ocr_tokens(re.split(r"[ \t]*\n[ \t]*", text))


def _split_translation_chunks(text, limit=MAX_BATCH_CHARS):
    """Split long prose near sentence/word boundaries under the API limit."""
    text = _clean_ocr_text(text)
    chunks = []
    while len(text) > limit:
        window = text[:limit + 1]
        cuts = [window.rfind(token) for token in (". ", "? ", "! ", "; ", ": ")]
        cut = max(cuts)
        if cut >= limit // 2:
            cut += 1  # keep sentence punctuation in the preceding chunk
        else:
            cut = window.rfind(" ")
            if cut < limit // 2:
                cut = limit
        chunk = text[:cut].strip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def translate_paragraph(text):
    cached = cache_get(text)
    if cached is not None:
        return cached
    chunks = _split_translation_chunks(text)
    if len(chunks) == 1:
        value = _translate_remote(chunks[0])
    else:
        translated = [None] * len(chunks)
        with ThreadPoolExecutor(max_workers=min(MAX_TRANSLATE_WORKERS, len(chunks))) as pool:
            futures = {pool.submit(_translate_remote, chunk): i
                       for i, chunk in enumerate(chunks)}
            for future in as_completed(futures):
                translated[futures[future]] = future.result()
        value = " ".join(translated)
    cache_set(text, value)
    return value


def _make_batches(pending, texts):
    batches = []
    current = []
    current_size = 0
    for index in pending:
        item_size = len(_clean_ocr_text(texts[index])) + 24
        if current and current_size + item_size > MAX_BATCH_CHARS:
            batches.append(current)
            current = []
            current_size = 0
        current.append(index)
        current_size += item_size
    if current:
        batches.append(current)
    return batches


def _translate_batch(indices, texts):
    """Translate several paragraphs in one round trip and split by markers."""
    combined = "\n".join("ZXQPX%dZX %s" % (i, _clean_ocr_text(texts[i]))
                         for i in indices)
    output = _translate_remote(combined)
    pattern = re.compile(r"ZXQPX(\d+)ZX")
    markers = list(pattern.finditer(output))
    expected = set(indices)
    found = {int(match.group(1)) for match in markers}
    if found != expected:
        raise TranslationError("batch markers were not preserved")
    values = {}
    for pos, match in enumerate(markers):
        index = int(match.group(1))
        end = markers[pos + 1].start() if pos + 1 < len(markers) else len(output)
        value = output[match.end():end].strip()
        if not value:
            raise TranslationError("empty batch paragraph")
        values[index] = value
    return values


def translate_paragraphs(texts, on_progress=None):
    """Translate paragraphs in batches, with per-paragraph fallback.

    Cached values return immediately.  Uncached short paragraphs share one
    Mozhi request; if markers are changed or the request fails, only that batch
    falls back to the existing concurrent paragraph requests.
    """
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
        cached = cache_get(text)
        if cached is None:
            pending.append(i)
        else:
            results[i] = cached
            progress()

    # One oversized OCR paragraph must be split into sentence-sized requests;
    # never send it as a single GET query (Mozhi/Bing returns HTTP 500).
    unresolved = [i for i in pending
                  if len(_clean_ocr_text(texts[i])) > MAX_BATCH_CHARS]
    unresolved_set = set(unresolved)
    short_pending = [i for i in pending if i not in unresolved_set]
    batches = _make_batches(short_pending, texts)
    if batches:
        with ThreadPoolExecutor(max_workers=min(MAX_TRANSLATE_WORKERS, len(batches))) as pool:
            futures = {pool.submit(_translate_batch, batch, texts): batch for batch in batches}
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
        with ThreadPoolExecutor(max_workers=MAX_TRANSLATE_WORKERS) as pool:
            futures = [pool.submit(work, i, texts[i]) for i in unresolved]
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
    """Lay out every translated paragraph with per-column vertical stacking.

    A box is anchored at its original paragraph's top-left (same column x, no
    horizontal drift) and is only pushed down when it would overlap an already
    placed box.  Returns (layouts, canvas).  Raises LayoutError when a box
    cannot fit in the canvas and font reduction is still allowed (grow=False).
    """
    img_w, img_h = canvas
    f = font(font_size)
    ascent, descent = f.getmetrics()
    line_h = ascent + descent + 2
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    columns = detect_columns(paragraphs)
    col_right = {}
    for col in columns:
        for i in col["indices"]:
            col_right[i] = col["right"]

    # reading order: top to bottom, then left to right
    global_order = sorted(range(len(paragraphs)),
                          key=lambda i: (paragraphs[i]["box"][1], paragraphs[i]["box"][0]))

    placed = []                 # every placed rect, across all columns
    layouts = [None] * len(paragraphs)
    out_h = img_h

    for i in global_order:
        para = paragraphs[i]
        x, y, ow, oh = para["box"]
        out_text = (translated[i] or para["text"]).strip()
        # cap width to the column's horizontal extent so the box never drifts
        # across a column boundary; also fit the canvas.
        avail_w = max(40, min(img_w - x - PAD_H, col_right[i] - x - PAD_H))
        max_text_width = min(max(ow, 100), avail_w)
        lines = wrap_text_by_pixels(out_text, f, max_text_width, probe)
        content_h = line_h * len(lines)
        box_w = min(max_text_width + 2 * PAD_H, img_w)
        # Cover the complete source paragraph, not only the (often shorter)
        # Chinese translation, otherwise untranslated English remains visible
        # below the overlay and looks like overlapping text.
        box_h = max(content_h, oh) + 2 * PAD_V

        bx = max(0, min(x - PAD_H, img_w - box_w))
        by = max(0, y - PAD_V)

        # vertical collision: smallest y >= anchor that clears placed boxes
        yy = by
        guard = 0
        while _overlaps_any((bx, yy, box_w, box_h), placed) and guard < 1_000_000:
            yy += BOX_GAP
            guard += 1
        if guard >= 1_000_000:
            raise LayoutError("could not place paragraph %d without overlap" % i)
        if yy + box_h > out_h:
            if font_size > MIN_FONT_SIZE and not grow:
                raise LayoutError("cannot fit at font %d" % font_size)
            out_h = yy + box_h

        layouts[i] = {
            "text": out_text,
            "orig": (x, y, ow, oh),
            "lines": lines,
            "rect": (bx, yy, box_w, box_h),
            "font": f,
            "font_size": font_size,
            "line_h": line_h,
        }
        placed.append((bx, yy, box_w, box_h))

    return layouts, (img_w, out_h)


def calculate_layout(paragraphs, translated, img_size, start_size=None):
    """Full layout: try unified font 18 -> 16 -> 14, then grow the canvas.

    Returns (layouts, canvas).  Overlap is never allowed; only at the minimum
    font size is the canvas height grown to fit genuinely overflowing boxes.
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
                                     max(l["rect"][1] + l["rect"][3] for l in layouts)))
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
    """Render all translated paragraphs into the captured image.

    All paragraphs share one font size (18 default; 16 or 14 for the whole image
    when needed).  Each box gets a light semi-transparent background; boxes never
    overlap and the final layout is validated before drawing.  Returns the
    output PNG path.
    """
    image = Image.open(source or CACHE).convert("RGB")
    layouts, canvas = calculate_layout(paragraphs, translated, image.size, start_size)
    image = _grow_canvas(image, canvas)

    ok, reason = validate_layout(layouts, canvas)
    if not ok:
        raise RuntimeError("refusing to draw invalid layout: %s" % reason)

    draw = ImageDraw.Draw(image, "RGBA")
    for lay in layouts:
        x, y, w, h = lay["rect"]
        draw.rounded_rectangle((x, y, x + w, y + h), radius=4, fill=(255, 255, 255, 255))
        ty = y + PAD_V
        for line in lay["lines"]:
            draw.text((x + PAD_H, ty), line, font=lay["font"], fill=(25, 25, 25, 255))
            ty += lay["line_h"]
    out = ROOT / "translated.png"
    image.save(out)
    return out


# ---------------------------------------------------------------------------
# GUI: selector, progress, result, settings
# ---------------------------------------------------------------------------

class Selector(Gtk.Window):
    def __init__(self, screenshot, callback):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        # Paint a frozen screenshot as the window background.  Relying on an
        # RGBA/transparent toplevel produces an opaque black window on some
        # GNOME/X11 compositing configurations.
        self.screenshot = screenshot
        self.callback = callback
        self.start = None
        self.end = None
        self.keyboard_grabbed = False
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_keep_above(True)
        self.set_accept_focus(True)
        self.set_modal(True)
        self.fullscreen()
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
        self.start = (int(event.x), int(event.y))
        self.end = self.start
        self.queue_draw()
        return True

    def motion(self, _widget, event):
        if self.start:
            self.end = (int(event.x), int(event.y))
            self.queue_draw()
        return True

    def release(self, _widget, event):
        if not self.start:
            return True
        self.end = (int(event.x), int(event.y))
        x1, y1 = self.start
        x2, y2 = self.end
        rect = (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        if rect[2] >= 8 and rect[3] >= 8:
            self.callback(rect)
        else:
            Gtk.main_quit()
        self.destroy()
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

    def __init__(self):
        super().__init__(title="截图翻译")
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
        self.set_position(Gtk.WindowPosition.CENTER)
        self.show_all()
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


def show_progress():
    return ProgressWindow()


class ResultWindow(Gtk.Window):
    def __init__(self, rect, translated_text):
        super().__init__(title="截图翻译")
        self.rect = rect
        self.translated_text = translated_text
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(min(1100, max(520, rect[2])), min(800, max(260, rect[3] + 48)))
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.key)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar = Gtk.Box(spacing=6)
        for label, action in [("提取中文", self.extract_zh), ("提取英文", self.extract_en), ("重新框选", self.reselect), ("快捷键设置", self.settings), ("复制翻译", self.copy_translation)]:
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, fn=action: fn())
            bar.pack_start(button, False, False, 4)
        hint = Gtk.Label(label="Ctrl+Alt+O：重新框选截图    Esc：关闭")
        bar.pack_end(hint, False, False, 8)
        box.pack_start(bar, False, False, 0)
        image = Gtk.Image.new_from_file(str(ROOT / "translated.png"))
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(image)
        box.pack_start(scroll, True, True, 0)
        self.add(box)
        self.show_all()
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


def show_result(rect, translated_text):
    ResultWindow(rect, translated_text)


def show_friendly_error(message="翻译服务暂时无法连接，请稍后重试。截图和文字识别已经完成。",
                        progress=None):
    """Show a short Chinese error; details already went to the log file."""
    def _show():
        if progress is not None:
            progress.destroy()
        dialog = Gtk.MessageDialog(None, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "截图翻译失败")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        Gtk.main_quit()
        return False
    GLib.idle_add(_show)


def show_busy_dialog():
    dialog = Gtk.MessageDialog(None, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.CLOSE, "截图翻译")
    dialog.format_secondary_text("已有截图翻译任务正在进行，请稍候再试。")
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
        self.show_all()

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

def process(rect, pix=None, progress=None, lock=None):
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
        show_result(rect, translated_text)
        return False

    def finish_error(message):
        release_task_lock(lock)
        show_friendly_error(message, progress)
        return False

    try:
        stage("正在截取画面……")
        capture(rect, pix)
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
        stage("正在翻译 0/%d……" % total)

        def on_translate_progress(done, total_count):
            if progress is not None:
                GLib.idle_add(lambda: progress.set_text("正在翻译 %d/%d……" % (done, total_count)) or False)

        results = translate_paragraphs(texts, on_translate_progress)
        if progress is not None and progress.is_cancelled():
            return

        succeeded = sum(1 for r in results if r)
        if succeeded == 0:
            logging.warning("all %d paragraphs failed to translate", total)
            GLib.idle_add(finish_error, "翻译服务暂时无法连接，请稍后重试。截图和文字识别已经完成。")
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
    forced = mode == "--select"
    rect = state.get("region")
    if forced or not rect:
        # Capture before showing the dark selection layer.  Otherwise the
        # overlay itself can be captured and the result appears black.
        base = capture_screen()

        def selected(r):
            progress = show_progress()
            threading.Thread(target=process, args=(r, base),
                             kwargs={"progress": progress, "lock": lock}, daemon=True).start()

        Selector(base, selected)
        Gtk.main()
    else:
        # Capture on the main thread (Gdk is not thread-safe), then translate on
        # a worker while the progress window keeps the loop responsive.
        base = capture_screen()
        progress = show_progress()
        threading.Thread(target=process, args=(tuple(rect), base),
                         kwargs={"progress": progress, "lock": lock}, daemon=True).start()
        Gtk.main()


if __name__ == "__main__":
    main()
