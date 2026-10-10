"""English-to-Chinese request boundaries, fallback and cache regressions."""
import json
import threading
import unittest
from unittest import mock

import screenshot_translator as app


class TranslationQualityTest(unittest.TestCase):
    def setUp(self):
        # Legacy remote backend is opt-in; keep its existing regressions.
        backend = mock.patch.object(app, "TRANSLATION_BACKEND", "remote")
        backend.start()
        self.addCleanup(backend.stop)

    def test_chinese_code_and_icons_bypass_network_and_old_cache(self):
        texts = ["搭建好的网络结构", "PPO 训练得到的权重", "加载权重后的 Actor",
                 "MLPModel", "actor_state_dict", "load_state_dict()", "8 6", "©"]
        progress = mock.Mock()
        with mock.patch.object(app, "_translate_remote") as remote, \
                mock.patch.object(app, "cache_get", return_value="错误的旧译文") as cache:
            self.assertEqual(app.translate_paragraphs(texts, progress), texts)
            for text in texts:
                self.assertEqual(app.translate_paragraph(text), text)
        remote.assert_not_called()
        cache.assert_not_called()
        self.assertEqual(progress.call_args.args, (len(texts), len(texts)))

    def test_english_prose_protects_code_and_restores_it_verbatim(self):
        text = "The MLPModel loads actor_state_dict using load_state_dict()."
        with mock.patch.object(app, "_translate_remote", return_value=
                               "QZXTERM0ZXQ 使用 QZXTERM2ZXQ 加载 QZXTERM1ZXQ。") as remote, \
                mock.patch.object(app, "cache_get", return_value=None), \
                mock.patch.object(app, "cache_set"):
            value = app.translate_paragraph(text)
        self.assertEqual(value, "MLPModel 使用 load_state_dict() 加载 actor_state_dict。")
        self.assertNotIn("actor_state_dict", remote.call_args.args[0])

    def test_technical_tokens_are_restored_per_batch_paragraph(self):
        texts = ["Load actor_state_dict.", "Use MLPModel."]
        with mock.patch.object(app, "_translate_remote", return_value=
                               "ZXQPX0ZX 加载 QZXTERM0ZXQ。\nZXQPX1ZX 使用 QZXTERM0ZXQ。"):
            self.assertEqual(app._translate_batch([0, 1], texts),
                             {0: "加载 actor_state_dict。", 1: "使用 MLPModel。"})

    def test_missing_or_duplicate_technical_tokens_are_not_cached(self):
        for value in ("加载演员权重。", "QZXTERM0ZXQ QZXTERM0ZXQ", "QZXTERM0ZXQ QZXTERM7ZXQ"):
            with self.subTest(value=value), \
                    mock.patch.object(app, "_translate_remote", return_value=value), \
                    mock.patch.object(app, "cache_get", return_value=None), \
                    mock.patch.object(app, "cache_set") as cache:
                with self.assertRaises(app.TranslationError):
                    app.translate_paragraph("Load actor_state_dict.")
                cache.assert_not_called()

    def test_previous_unprotected_cache_version_is_not_reused(self):
        key = json.dumps(["en-zh-v2", app.SOURCE_LANG, app.ENGINES, "Save settings"],
                         ensure_ascii=False)
        with mock.patch.object(app, "_cache", {key: "旧译文"}):
            self.assertIsNone(app.cache_get("Save settings"))

    def test_preserved_chinese_does_not_hide_failure_of_all_english_requests(self):
        paragraphs = [{"text": "已有中文", "box": (10, 10, 100, 20)},
                      {"text": "Save settings", "box": (10, 40, 100, 20)}]
        with mock.patch.object(app, "capture"), \
                mock.patch.object(app, "parse_ocr_paragraphs", return_value=paragraphs), \
                mock.patch.object(app, "translate_paragraphs", return_value=["已有中文", None]), \
                mock.patch.object(app, "save_cache"), \
                mock.patch.object(app, "render_translation") as render, \
                mock.patch.object(app, "show_friendly_error") as error, \
                mock.patch.object(app.GLib, "idle_add", side_effect=lambda fn, *args: fn(*args)):
            app.process((0, 0, 200, 100))
        render.assert_not_called()
        self.assertIn("翻译服务暂时无法连接", error.call_args.args[0])

    def test_chinese_and_code_only_screenshot_succeeds_without_translation_requests(self):
        paragraphs = [{"text": "已有中文", "box": (10, 10, 100, 20)},
                      {"text": "actor_state_dict", "box": (10, 40, 100, 20)}]
        with mock.patch.object(app, "capture"), \
                mock.patch.object(app, "parse_ocr_paragraphs", return_value=paragraphs), \
                mock.patch.object(app, "_translate_remote") as remote, \
                mock.patch.object(app, "save_cache"), \
                mock.patch.object(app, "render_translation") as render, \
                mock.patch.object(app, "load_state", return_value={}), \
                mock.patch.object(app, "save_state"), \
                mock.patch.object(app, "show_result") as result, \
                mock.patch.object(app, "show_friendly_error") as error, \
                mock.patch.object(app.GLib, "idle_add", side_effect=lambda fn, *args: fn(*args)):
            app.process((0, 0, 200, 100))
        remote.assert_not_called()
        error.assert_not_called()
        render.assert_called_once()
        self.assertEqual(result.call_args.args[1], "已有中文\n\nactor_state_dict")

    def test_batch_limits_include_markers_and_both_character_and_url_caps(self):
        texts = ["A" * 310] * 12 + ["技术术语 " * 30] * 12
        batches = app._make_batches(list(range(len(texts))), texts)
        self.assertEqual([i for batch in batches for i in batch], list(range(len(texts))))
        for batch in batches:
            combined = "\n".join("ZXQPX%dZX %s" % (i, app._clean_ocr_text(texts[i]))
                                 for i in batch)
            self.assertTrue(app._fits_batch(combined), (len(combined), batch))

    def test_sentence_punctuation_stays_with_its_sentence(self):
        text = "A" * 700 + ". " + "B" * 700 + "."
        chunks = app._split_translation_chunks(text)
        self.assertEqual(chunks, ["A" * 700 + ".", "B" * 700 + "."])

    def test_chunks_respect_utf8_byte_limit_and_preserve_content(self):
        text = "An English sentence with 中文 and a Unicode symbol Ω. " * 35
        chunks = app._split_translation_chunks(
            text, fits=lambda value: len(value.encode("utf-8")) <= 500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 500 for chunk in chunks))
        self.assertEqual("".join(chunks).replace(" ", ""), text.replace(" ", ""))

    def test_single_paragraph_is_sent_without_a_batch_marker(self):
        with mock.patch.object(app, "_translate_remote", return_value="保存设置") as remote:
            self.assertEqual(app._translate_batch([0], ["Save settings"]), {0: "保存设置"})
        remote.assert_called_once_with("Save settings")

    def test_batch_marker_case_and_spaces_are_tolerated(self):
        with mock.patch.object(app, "_translate_remote",
                               return_value="zxqpx 0 zx 第一段\nZXQPX1ZX 第二段"):
            self.assertEqual(app._translate_batch([0, 1], ["One", "Two"]),
                             {0: "第一段", 1: "第二段"})

    def test_duplicate_reordered_and_missing_markers_are_rejected(self):
        for output in ("ZXQPX0ZX 一 ZXQPX0ZX 二 ZXQPX1ZX 三",
                       "ZXQPX1ZX 二 ZXQPX0ZX 一", "ZXQPX0ZX 一", "",
                       "未标记内容 ZXQPX0ZX 一 ZXQPX1ZX 二"):
            with self.subTest(output=output), mock.patch.object(
                    app, "_translate_remote", return_value=output):
                with self.assertRaises(app.TranslationError):
                    app._translate_batch([0, 1], ["One", "Two"])

    def test_mozhi_explicitly_requests_english_to_chinese(self):
        session = mock.Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"translated-text": "你好"}
        with mock.patch.object(app, "_tls_session", return_value=session):
            self.assertEqual(app._translate_on_node("https://node.invalid", "duckduckgo", "Hello"),
                             "你好")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual((params["from"], params["to"]), ("en", "zh"))

    def test_mymemory_explicitly_requests_english_and_decodes_entities(self):
        session = mock.Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {
            "responseStatus": 200, "responseData": {"translatedText": "模型 &amp; 数据"}}
        with mock.patch.object(app, "_tls_session", return_value=session):
            self.assertEqual(app._translate_mymemory("Model and data"), "模型 & 数据")
        self.assertEqual(session.get.call_args.kwargs["params"]["langpair"], "en|zh-CN")

    def test_mymemory_splits_long_text_and_retains_markers_locally(self):
        text = "ZXQPX0ZX " + "Sentence ends here. " * 50 + "\nZXQPX1ZX Settings"
        with mock.patch.object(app, "_translate_mymemory_segment", return_value="译文") as segment:
            translated = app._translate_mymemory(text)
        self.assertTrue(translated.startswith("ZXQPX0ZX 译文"))
        self.assertTrue(translated.endswith("\nZXQPX1ZX 译文"))
        self.assertTrue(all(len(call.args[0].encode("utf-8")) <= 500
                            and "ZXQPX" not in call.args[0]
                            for call in segment.call_args_list))

    def test_cache_normalizes_wraps_and_separates_source_languages(self):
        with mock.patch.object(app, "_cache", {}), mock.patch.object(app, "_save_cache_now"):
            app.cache_set("The model\n learns.", "模型学习。")
            self.assertEqual(app.cache_get("The model learns."), "模型学习。")
            with mock.patch.object(app, "SOURCE_LANG", "auto"):
                self.assertIsNone(app.cache_get("The model learns."))

    def test_old_autodetected_cache_is_not_reused(self):
        with mock.patch.object(app, "_cache", {"Settings": "旧翻译"}):
            self.assertIsNone(app.cache_get("Settings"))

    def test_cancelled_queued_request_does_not_kill_worker(self):
        pool = app._WorkerPool(1, "cancel-regression")
        entered, unblock = threading.Event(), threading.Event()
        cancelled_work = mock.Mock()

        def blocking():
            entered.set()
            return unblock.wait(2)

        try:
            first = pool.submit(blocking)
            self.assertTrue(entered.wait(1))
            cancelled = pool.submit(cancelled_work)
            self.assertTrue(cancelled.cancel())
            following = pool.submit(lambda: "worker alive")
            unblock.set()
            first.result(timeout=2)
            self.assertEqual(following.result(timeout=2), "worker alive")
            cancelled_work.assert_not_called()
        finally:
            unblock.set()
            pool._q.put((None, None))


if __name__ == "__main__":
    unittest.main()
