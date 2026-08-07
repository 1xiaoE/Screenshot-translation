# Small Text OCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve small-font OCR accuracy and Chinese/English line joining without adding OCR passes or dependencies.

**Architecture:** A shared `_run_tesseract` boundary performs one 2× Lanczos preprocessing pass and returns Tesseract output. Paragraph parsing maps TSV geometry back to source coordinates, while small pure helpers normalize OCR token and wrapped-line boundaries.

**Tech Stack:** Python 3.10, standard-library `unittest`, Pillow, Tesseract 4.1.

## Global Constraints

- Keep one Tesseract process per OCR request.
- Keep `--psm 3` for paragraph OCR and `--psm 6` for language extraction.
- Keep confidence threshold 18.
- Add no dependency.
- Return every paragraph box in source-image coordinates.

---

### Task 1: Shared scaled OCR and language-aware joining

**Files:**
- Create: `tests/test_ocr.py`
- Modify: `screenshot_translator.py:50-56,222-283,340-343,1032-1038`
- Modify: `README.md`

**Interfaces:**
- Produces: `_run_tesseract(path, lang, psm, output=None) -> str`
- Produces: `_join_ocr_tokens(tokens) -> str`
- Preserves: `parse_ocr_paragraphs(path, lang="eng+chi_sim") -> list[dict]`
- Preserves: `_clean_ocr_text(text) -> str`

- [x] **Step 1: Write failing behavioral tests**

```python
def test_small_text_is_recognized_in_source_coordinates(self):
    # Render 10 px text to a 260×35 image, call the real Tesseract pipeline,
    # assert the complete literal sentence and a box bounded by 260×35.

def test_joins_ocr_tokens_by_script_and_punctuation(self):
    self.assertEqual(app._join_ocr_tokens(["截图", "翻译", "，", "fast"]), "截图翻译，fast")

def test_cleans_wrapped_english_and_chinese_lines(self):
    self.assertEqual(app._clean_ocr_text("trans-\nlation 中文\n断行"), "translation 中文断行")

def test_ignores_full_height_border_misread_as_text(self):
    # Render a full-height colored left border beside small text and assert
    # only the literal sentence is returned.
```

- [x] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest -v tests.test_ocr`

Expected: FAIL because the current OCR returns low-confidence garbage and `_join_ocr_tokens` does not exist.

- [x] **Step 3: Implement the minimal shared OCR preprocessing**

```python
OCR_SCALE = 2

def _run_tesseract(path, lang, psm, output=None):
    with Image.open(path) as source:
        resized = source.convert("RGB").resize(
            (source.width * OCR_SCALE, source.height * OCR_SCALE),
            Image.LANCZOS,
        )
    encoded = io.BytesIO()
    resized.save(encoded, "PNG", dpi=(300, 300))
    cmd = ["tesseract", "stdin", "stdout", "--psm", str(psm), "-l", lang]
    if output:
        cmd.append(output)
    proc = subprocess.run(cmd, input=encoded.getvalue(), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=25)
    proc.check_returncode()
    return proc.stdout.decode("utf-8", errors="replace")
```

Use it from paragraph OCR and extraction. Divide paragraph left/top with floor division and right/bottom with ceiling division before returning the box.

- [x] **Step 4: Implement minimal boundary joining**

Join adjacent CJK tokens without a space, omit a space before closing punctuation, and otherwise insert one space. In `_clean_ocr_text`, remove English hyphen-newline boundaries and CJK-to-CJK newlines before replacing remaining newlines with one space.

Ignore a narrow `|/1/I/l` token only when it starts at `(0, 0)` and spans at least 80% of the image height. Raise `CalledProcessError` when Tesseract exits nonzero.

- [x] **Step 5: Run focused and full verification**

Run: `python3 -m unittest -v tests.test_ocr`

Expected: all tests PASS.

Run: `python3 -m py_compile screenshot_translator.py tests/test_ocr.py`

Expected: exit 0 with no output.

Run both the fixed small-text PNG fixture and the current live `cache.png` through `parse_ocr_paragraphs`; assert visible key phrases and source-coordinate bounds. Run the suite under both Pillow 12.2 and the documented Pillow 9.0.1.

- [x] **Step 6: Update documentation**

Document 2× small-text preprocessing and language-aware wrapped-line joining in `README.md`.

- [x] **Step 7: Record integration status**

```bash
git status --short
```

Expected: modified `screenshot_translator.py` and `README.md`, plus new `tests/` and `docs/`. The current managed workspace exposes `.git` read-only, so execution leaves the verified working tree uncommitted for the repository owner to integrate.
