"""Offline unit tests for the translation network layer.

These tests exercise pure logic only (batch sizing, chunking, node circuit
breaker, cache eviction, OCR scale) and never touch the network, GTK, or
Tesseract, so they run in any environment.  The cache tests point CACHE_FILE at
a temp file so the user's real translation cache is never touched.
"""
import os
import tempfile
import unittest
from pathlib import Path

import screenshot_translator as app


class FitsBatchTest(unittest.TestCase):
    def test_ascii_within_limits(self):
        self.assertTrue(app._fits_batch("x" * app.BATCH_MAX_CHARS))

    def test_ascii_over_char_limit(self):
        self.assertFalse(app._fits_batch("x" * (app.BATCH_MAX_CHARS + 1)))

    def test_cjk_url_limit_binds(self):
        # 950 CJK chars fit the char limit but blow the URL limit (9 bytes each)
        text = "中" * app.BATCH_MAX_CHARS
        self.assertFalse(app._fits_batch(text))

    def test_short_cjk_fits(self):
        # ~380 CJK chars: encoded URL stays under the cap
        text = "中" * 380
        self.assertTrue(app._fits_batch(text))

    def test_empty_fits(self):
        self.assertTrue(app._fits_batch(""))


class ChunkingTest(unittest.TestCase):
    def test_short_text_unchunked(self):
        self.assertEqual(app._split_translation_chunks("hello world"), ["hello world"])

    def test_long_ascii_chunks_all_fit(self):
        base = "The quick brown fox jumps over the lazy dog. "
        text = (base * 40)[:3000]
        chunks = app._split_translation_chunks(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertTrue(app._fits_batch(c), "chunk too large: %d" % len(c))
        # content is preserved (whitespace may differ)
        self.assertEqual("".join(chunks).replace(" ", ""),
                         text.replace(" ", ""))

    def test_long_cjk_chunks_all_fit(self):
        text = "这是一段非常长的中文文本，用于测试分块逻辑是否遵守 URL 长度限制。" * 60
        chunks = app._split_translation_chunks(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertTrue(app._fits_batch(c), "chunk too large: %d" % len(c))

    def test_max_fit_len_monotonic(self):
        text = "A" * 2000
        self.assertLessEqual(app._max_fit_len(text, app.BATCH_MAX_CHARS),
                             app.BATCH_MAX_CHARS)


class MakeBatchesTest(unittest.TestCase):
    def test_batches_respect_url_cap(self):
        texts = ["The quick brown fox jumps over the lazy dog. "] * 120
        pending = list(range(len(texts)))
        batches = app._make_batches(pending, texts)
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(b) for b in batches), len(texts))
        for b in batches:
            size = sum(app._batch_item_size(texts[i]) for i in b)
            self.assertLessEqual(size, app.BATCH_MAX_URL, "batch oversized")


class TranslationConfigTest(unittest.TestCase):
    def test_default_engine_is_single_fast_engine(self):
        self.assertEqual(app.ENGINES, ["duckduckgo"])
        self.assertEqual(app.ENGINE_TARGET["duckduckgo"], "zh")
        self.assertTrue(app.MYMEMORY_URL.endswith("/get"))

    def test_failure_timeouts_are_bounded(self):
        self.assertLessEqual(app.CONNECT_TIMEOUT, 4)
        self.assertLessEqual(app.READ_TIMEOUT, 15)


class CircuitBreakerTest(unittest.TestCase):
    def tearDown(self):
        for node in list(app._node_failures) + list(app._node_cooldown_until):
            app._node_ok(node)

    def test_failures_trigger_cooldown(self):
        node = app.NODES[0]
        app._node_failed(node)
        self.assertTrue(app._node_available(node))
        app._node_failed(node)
        self.assertFalse(app._node_available(node))

    def test_ordered_nodes_skips_cooled_node(self):
        node = app.NODES[0]
        for _ in range(app.NODE_FAILURES_TO_COOLDOWN):
            app._node_failed(node)
        self.assertNotIn(node, app._ordered_nodes())

    def test_ok_resets_breaker(self):
        node = app.NODES[0]
        for _ in range(app.NODE_FAILURES_TO_COOLDOWN):
            app._node_failed(node)
        app._node_ok(node)
        self.assertTrue(app._node_available(node))
        self.assertIn(node, app._ordered_nodes())

    def test_last_good_node_promoted(self):
        app._last_good_node = app.NODES[1]
        try:
            order = app._ordered_nodes()
            self.assertEqual(order[0], app.NODES[1])
        finally:
            app._last_good_node = None


class CacheTest(unittest.TestCase):
    def setUp(self):
        # Redirect cache writes to a temp file so the real cache is untouched.
        self._real_cache_file = app.CACHE_FILE
        fd, self._tmp = tempfile.mkstemp(prefix="st_cache_")
        os.close(fd)
        app.CACHE_FILE = Path(self._tmp)
        app._cache.clear()

    def tearDown(self):
        app.CACHE_FILE = self._real_cache_file
        app._cache.clear()
        try:
            Path(self._tmp).unlink()
        except OSError:
            pass

    def test_roundtrip_and_eviction(self):
        for i in range(app.CACHE_MAX_ENTRIES + 50):
            app.cache_set("key%d" % i, "value%d" % i)
        self.assertEqual(len(app._cache), app.CACHE_MAX_ENTRIES)
        # oldest keys were evicted (dict preserves insertion order)
        self.assertIsNone(app.cache_get("key0"))
        self.assertEqual(app.cache_get("key%d" % (app.CACHE_MAX_ENTRIES + 49)),
                         "value%d" % (app.CACHE_MAX_ENTRIES + 49))


class OcrScaleTest(unittest.TestCase):
    def test_small_region_keeps_2x(self):
        self.assertEqual(app._ocr_scale_for(500, 300), app.OCR_SCALE)

    def test_fullscreen_tapered(self):
        scale = app._ocr_scale_for(2560, 1440)
        self.assertLess(scale, app.OCR_SCALE)
        self.assertGreater(scale, 0.5)
        # upscaled area never exceeds the cap
        self.assertLessEqual(2560 * 1440 * scale * scale,
                             app.OCR_MAX_PIXELS * 1.01)

    def test_huge_area_never_exceeds_cap(self):
        for w, h in ((3840, 2160), (7680, 4320)):
            scale = app._ocr_scale_for(w, h)
            self.assertLessEqual(w * h * scale * scale,
                                 app.OCR_MAX_PIXELS * 1.01)


if __name__ == "__main__":
    unittest.main()
