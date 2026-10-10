"""Offline Argos en->zh model, with source-aware robotics terminology.

The GTK application talks to an isolated, persistent CPU worker over JSON
pipes. The model never downloads packages or contacts translation services.
"""
import atexit
import bisect
import functools
import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "models" / "translate-en_zh-1_9"
GLOSSARY_FILE = ROOT / "glossaries" / "robotics_mechanics.json"
PYTHON = ROOT / ".venv-local" / "bin" / "python"
PIPELINE_VERSION = "local-argos-en-zh-1.9-v4"
TECHNICAL = re.compile(
    r"`[^`\n]+`|"
    r"\$[^$\n]+\$|"
    r"(?<!\w)[A-Za-zΑ-Ωα-ω][\wᵀᵗ]*\s*=\s*[A-Za-zΑ-Ωα-ω][\wᵀᵗ]*(?:[*/+^-][A-Za-z0-9Α-Ωα-ω]+)*|"
    r"\b\d+(?:\.\d+)?\s*(?:N(?:m|·m)?|kg|mm|ms|m|rad|Hz|Pa|MPa|GPa|rpm)(?:/(?:s|rad|m))?(?!\w)|"
    r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\([^()\n]*\)|"
    r"\b[A-Za-z]\w*(?:\.[A-Za-z_]\w*)+\b|"
    r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b|"
    r"\b[A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*\b|"
    r"\b[A-Z]{2,}[A-Z][a-z][A-Za-z0-9]*\b|"
    r"\b[A-Z][A-Z0-9]{1,11}\b"
)


class LocalTranslationError(RuntimeError):
    pass


def glossary_path():
    value = os.environ.get("SCREENSHOT_TRANSLATOR_GLOSSARY", "").strip()
    if value.lower() in {"none", "off"}:
        return None
    return Path(value).expanduser().resolve() if value else GLOSSARY_FILE


def glossary_identity():
    path = glossary_path()
    if path is None:
        return "disabled"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing:" + str(path)


@functools.lru_cache(maxsize=4)
def _literal_terms(path, identity):
    glossary = Glossary(path)
    return {source: row["target"] for source, row in glossary.entries.items()
            if source == row["target"].casefold()}


@functools.lru_cache(maxsize=4)
def _cached_glossary(path, identity):
    return Glossary(path)


_OCR_LITERAL = re.compile(
    r"`[^`\n]+`|\$[^$\n]+\$|"
    r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\([^()\n]*\)|"
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|"
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
)


def repair_ocr(text, glossary=None):
    """Repair only listed OCR spellings, never infer arbitrary words/models."""
    if re.search(r"[\u3400-\u9fff]", text):
        return text
    if glossary is None:
        glossary = _cached_glossary(glossary_path(), glossary_identity())
    replacements = {"T his": "This", "t his": "this", "T he": "The", "t he": "the"}
    replacements.update(glossary.ocr_replacements)
    pattern = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(
        re.escape(s) for s in sorted(replacements, key=len, reverse=True)))
    # Keep quoted code, function calls, dotted/snake_case names intact.
    pieces, previous = [], 0
    for match in _OCR_LITERAL.finditer(text):
        pieces.append(pattern.sub(lambda m: replacements[m.group()], text[previous:match.start()]))
        pieces.append(match.group())
        previous = match.end()
    pieces.append(pattern.sub(lambda m: replacements[m.group()], text[previous:]))
    return "".join(pieces)


def preserves_text(text):
    """A standalone name explicitly marked unchanged in the domain glossary."""
    try:
        target = _literal_terms(glossary_path(), glossary_identity()).get(text.casefold())
        return target is not None and target.casefold() == text.casefold()
    except LocalTranslationError:
        # Let the translation worker report malformed/missing glossary files.
        return False


class Glossary:
    def __init__(self, path):
        self.entries = {}
        self.ocr_replacements = {}
        if path is not None:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                for row in data["terms"]:
                    source, target = row["source"].strip(), row["target"].strip()
                    if not source or not target:
                        raise ValueError("empty term")
                    aliases = row.get("aliases", [])
                    variants = row.get("source_variants", [])
                    ocr_variants = row.get("ocr_variants", [])
                    for values in (aliases, variants, ocr_variants):
                        if not isinstance(values, list) or not all(isinstance(a, str) and a.strip() for a in values):
                            raise ValueError("variants/aliases must be nonempty strings")
                    entry = {"target": target, "aliases": aliases}
                    for spelling in [source] + variants:
                        key = spelling.strip().casefold()
                        if key in self.entries:
                            raise ValueError("duplicate source spelling")
                        self.entries[key] = entry
                    for spelling in ocr_variants:
                        if spelling in self.ocr_replacements:
                            raise ValueError("duplicate OCR spelling")
                        self.ocr_replacements[spelling] = source
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise LocalTranslationError("专业术语表无法读取，请检查 JSON 格式：%s" % path) from exc
        sources = sorted(self.entries, key=len, reverse=True)
        self.pattern = re.compile(r"(?<![\w])(?:%s)(?![\w])" % "|".join(map(re.escape, sources)),
                                  re.IGNORECASE) if sources else None

    def matches(self, text):
        """Longest source phrases win; never match a substring of an identifier."""
        if self.pattern is None:
            return []
        return [(m.start(), m.end(), self.entries[m.group().casefold()])
                for m in self.pattern.finditer(text)]

    def normalize(self, source, translated):
        # Only apply an alias when its English term actually occurs in the
        # source. Long aliases win and substitutions do not cascade.
        replacements = {}
        for _start, _end, entry in self.matches(source):
            # An alias may be a prefix of the correct translation, e.g.
            # 四足机器 vs 四足机器人. Correct targets must win and stay intact.
            replacements[entry["target"]] = entry["target"]
            for alias in entry["aliases"]:
                replacements.setdefault(alias, entry["target"])
        if replacements:
            pattern = re.compile("|".join(map(re.escape, sorted(replacements, key=len, reverse=True))))
            translated = pattern.sub(lambda m: replacements[m.group()], translated)
        return translated

    def protected_spans(self, source):
        """Keep complete proper names ahead of their acronym/code substrings."""
        spans = [(s, e, row["target"]) for s, e, row in self.matches(source)
                 if source[s:e].casefold() == row["target"].casefold()]
        for match in TECHNICAL.finditer(source):
            if not any(match.start() < e and match.end() > s for s, e, _ in spans):
                spans.append((match.start(), match.end(), match.group()))
        return sorted(spans)


def polish_translation(source, translated):
    """Format Chinese prose without changing decimal points or quoted code."""
    # Screenshots can start partway through "To the authors' knowledge".
    # Only repair this attribution when it introduces a statement; never
    # replace references to the authors' knowledge as an ordinary noun.
    if re.match(r"authors['’] knowledge,\s*(?:this|it) is\b", source, re.IGNORECASE):
        translated = re.sub(r"^作者的知识(?:是)?[，,]", "据作者所知，", translated)
    # A multiplicative speed/frequency result is a ratio, not an additive
    # number of times. Only repair a corresponding, complete Chinese clause.
    factor = re.search(
        r"\b(?:improve|increase) solving frequency by\s*([~∼≈]?)\s*"
        r"(\d+(?:\.\d+)?)\s*(?:[x×]|times)(?!\w)", source, re.IGNORECASE)
    if factor:
        approximation, number = factor.groups()
        value = ("约" if approximation else "") + number + "倍"
        written = r"(?:[~∼≈约]\s*)?" + re.escape(number) + r"\s*(?:[x×]|倍)"
        translated = re.sub(r"(?:提高|提升)\s*" + written + r"\s*的求解频率",
                            "使求解频率达到原来的" + value, translated)
    def format_prose(value):
        value = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", value)
        value = re.sub(r"(?<=[\u3400-\u9fff])([,.!?;:])",
                       lambda m: dict(zip(",.!?;:", "，。！？；："))[m.group()], value)
        value = re.sub(r",(?!\d)", "，", value)
        value = re.sub(r"\.(?=\s|$)", "。", value)
        return re.sub(r"\s+([,.!?;:，。！？；：])", r"\1", value)
    pieces, previous = [], 0
    for match in _OCR_LITERAL.finditer(translated):
        pieces.extend((format_prose(translated[previous:match.start()]), match.group()))
        previous = match.end()
    pieces.append(format_prose(translated[previous:]))
    return "".join(pieces).strip()


def split_sentences(text, limit=400):
    """Bound model input at sentence and word boundaries without losing text."""
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z\d])|(?<=[。！？])", text)
    chunks = []
    for sentence in sentences:
        sentence = sentence.strip()
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit + 1)
            if cut < limit // 2:
                cut = limit
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            chunks.append(sentence)
    return chunks


class Engine:
    def __init__(self, glossary=None):
        try:
            import ctranslate2
            import sentencepiece
        except ImportError as exc:
            raise LocalTranslationError("本地翻译运行依赖未安装，请运行 scripts/setup_local_translation.sh。") from exc
        if not (MODEL_DIR / "model" / "model.bin").is_file():
            raise LocalTranslationError("本地英→中模型未安装，请运行 scripts/setup_local_translation.sh。")
        self.tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(MODEL_DIR / "sentencepiece.model"))
        self.model = ctranslate2.Translator(str(MODEL_DIR / "model"), device="cpu", compute_type="int8",
                                           inter_threads=1, intra_threads=4)
        self.glossary = Glossary(glossary)

    def decode(self, tokens):
        return self.tokenizer.decode(tokens).replace("▁", " ")

    def correct_aligned_terms(self, source, proto, result):
        """Use model attention to repair a mistranslated technical phrase.

        Prefer an existing correct spelling/known alias. Only replace a compact
        contiguous target span aligned to that source phrase; leave uncertain
        spans alone rather than overwriting unrelated words.
        """
        tokens = result.hypotheses[0]
        raw = self.decode(tokens)
        # SentencePiece proto offsets are UTF-8 bytes, while Python regex
        # spans use characters. Robotics papers often contain Greek symbols.
        byte_offsets = [0]
        for char in source:
            byte_offsets.append(byte_offsets[-1] + len(char.encode("utf-8")))
        spans = [(s, e, row["target"], False) for s, e, row in self.glossary.matches(source)]
        for start, end, target in self.glossary.protected_spans(source):
            spans = [span for span in spans if span[1] <= start or span[0] >= end]
            spans.append((start, end, target, True))
        edits = []
        for start, end, target, is_code in spans:
            if target in raw:
                continue
            entry_source = source[start:end]
            normalized = self.glossary.normalize(entry_source, raw)
            if target in normalized:
                continue
            indices = set()
            for i, piece in enumerate(proto.pieces):
                begin = max(0, bisect.bisect_right(byte_offsets, piece.begin) - 1)
                pend = bisect.bisect_left(byte_offsets, piece.end)
                while begin < pend and source[begin].isspace():
                    begin += 1
                if begin < end and pend > start:
                    indices.add(i)
                elif pend == start and (
                        getattr(piece, "piece", "") == "▁" or
                        source[bisect.bisect_left(byte_offsets, piece.begin):pend].isspace()):
                    # SentencePiece sometimes emits the preceding space as
                    # its own token (▁). A translated word's first Chinese
                    # character can align to that space (including a zero-
                    # width initial ▁): omitting it would
                    # replace only 重奏乐 in 四重奏乐, leaving 四四足运动.
                    indices.add(i)
            selected = []
            for i, attention in enumerate(result.attention[0]):
                if i >= len(tokens) or not attention or tokens[i] in {".", ",", ";", ":", "!", "?", "。", "，"}:
                    continue
                peak = max(range(len(attention)), key=attention.__getitem__)
                mass = sum(attention[j] for j in indices if j < len(attention))
                # A multi-token English phrase can receive most of the
                # attention even when one unrelated token has the largest
                # individual weight. Use phrase mass too, so its leading
                # Chinese word is not left outside the replacement.
                if (peak in indices and mass >= 0.25) or mass >= 0.5:
                    selected.append(i)
            if not selected:
                continue
            left, right = selected[0], selected[-1] + 1
            if right - left > max(8, len(indices) * 3) or len(selected) < (right - left) * 0.7:
                continue
            # Attention may select only the middle of a Latin word (e.g.
            # co+b+ian in Jacobian). Replace complete words, not subwords.
            def latin(piece):
                return bool(re.fullmatch(r"▁?[A-Za-z]+", piece))
            if latin(tokens[left]):
                while left > 0 and not tokens[left].startswith("▁") and latin(tokens[left - 1]):
                    left -= 1
            if latin(tokens[right - 1]):
                while right < len(tokens) and latin(tokens[right]) and not tokens[right].startswith("▁"):
                    right += 1
            cstart = len(self.decode(tokens[:left]))
            cend = len(self.decode(tokens[:right]))
            if (not is_code and target.endswith("学") and right < len(tokens) and
                    tokens[right] == "学" and re.match(r"\s*(?:and|is|are|was|were|[.,;])", source[end:])):
                cend = len(self.decode(tokens[:right + 1]))
            if any(cstart < e and cend > s for s, e, _t in edits):
                continue
            edits.append((cstart, cend, target))
        for start, end, target in sorted(edits, reverse=True):
            raw = raw[:start] + target + raw[end:]
        return self.glossary.normalize(source, raw).strip()

    def translate_sentence(self, sentence):
        exact = self.glossary.entries.get(sentence.rstrip(" .!?;:").casefold())
        if exact:
            return exact["target"] + sentence[len(sentence.rstrip(" .!?;:")):].strip()
        proto = self.tokenizer.encode(sentence, return_type="proto")
        if len(proto.pieces) > 256:
            # SentencePiece can produce many tiny tokens for unusual text.
            # Split at a smaller boundary rather than truncating silently.
            middle = sentence.rfind(" ", 0, len(sentence) // 2 + 1)
            if middle <= 0:
                middle = max(1, len(sentence) // 2)
            return self.translate_sentence(sentence[:middle]) + self.translate_sentence(sentence[middle:].strip())
        result = self.model.translate_batch(
            [[piece.piece for piece in proto.pieces]], beam_size=4,
            length_penalty=0.2, replace_unknowns=True, return_attention=True,
            max_input_length=0, max_decoding_length=768,
        )[0]
        if len(result.hypotheses[0]) >= 768:
            raise LocalTranslationError("本地译文超过长度上限，请缩小截图选区。")
        translated = self.correct_aligned_terms(sentence, proto, result)
        # If a rare identifier was dropped or changed beyond reliable
        # alignment, preserve it with a bounded fragment fallback.
        protected = self.glossary.protected_spans(sentence)
        if any(target not in translated for _s, _e, target in protected):
            pieces, previous = [], 0
            for start, end, target in protected:
                if sentence[previous:start].strip():
                    pieces.append(self.translate_plain(sentence[previous:start]))
                pieces.append(target)
                previous = end
            if sentence[previous:].strip():
                pieces.append(self.translate_plain(sentence[previous:]))
            translated = " ".join(piece for piece in pieces if piece)
        # Fragment fallback still needs the original sentence's terminology:
        # a phrase may cross the boundary of a protected acronym/name.
        return polish_translation(sentence, self.glossary.normalize(sentence, translated))

    def translate_plain(self, text):
        text = text.strip()
        if not re.search(r"[A-Za-z]", text):
            return text
        proto = self.tokenizer.encode(text, return_type="proto")
        result = self.model.translate_batch([[p.piece for p in proto.pieces]], beam_size=4,
                                             length_penalty=0.2, replace_unknowns=True,
                                             return_attention=True, max_input_length=0,
                                             max_decoding_length=768)[0]
        return self.correct_aligned_terms(text, proto, result)

    def translate(self, text):
        text = repair_ocr(text, self.glossary)
        values = [self.translate_sentence(sentence) for sentence in split_sentences(text)]
        result = "".join(values)
        if not result:
            raise LocalTranslationError("本地模型没有返回译文，请重新框选。")
        return result


class LocalClient:
    """One persistent model worker per application; no listening network port."""
    def __init__(self):
        self.process = None
        self.identity = None
        self.lock = threading.Lock()
        atexit.register(self.close)

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()

    def translate(self, text):
        with self.lock:
            identity = glossary_identity()
            if self.process is not None and (self.identity != identity or self.process.poll() is not None):
                self.close()
            if self.process is None:
                if not PYTHON.is_file():
                    raise LocalTranslationError("本地翻译环境未安装，请运行 scripts/setup_local_translation.sh。")
                self.process = subprocess.Popen([str(PYTHON), str(Path(__file__).resolve()), "--worker"],
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
                self.identity = identity
            try:
                self.process.stdin.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                with selectors.DefaultSelector() as selector:
                    selector.register(self.process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=120):
                        raise LocalTranslationError("本地翻译超时，请缩小截图选区。")
                line = self.process.stdout.readline()
                if not line:
                    raise LocalTranslationError("本地翻译进程退出，请检查模型和运行依赖。")
                result = json.loads(line)
                if result.get("error"):
                    raise LocalTranslationError(result["error"])
                return result["translation"]
            except (OSError, ValueError, KeyError) as exc:
                self.close()
                raise LocalTranslationError("本地翻译进程通信失败，请重新截图。") from exc
            except LocalTranslationError:
                self.close()
                raise


def worker():
    engine = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if engine is None:
                engine = Engine(glossary_path())
            response = {"translation": engine.translate(request["text"])}
        except Exception as exc:
            response = {"error": str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__" and "--worker" in sys.argv:
    worker()
