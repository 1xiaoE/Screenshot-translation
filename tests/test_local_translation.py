"""Offline backend selection, editable domain terms, and real CPU inference."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import local_translation as local
import screenshot_translator as app


class GlossaryTest(unittest.TestCase):
    def test_plural_and_irregular_variants_match_without_rewriting_identifiers(self):
        glossary = local.Glossary(local.GLOSSARY_FILE)
        text = "quadruped robots; Jacobian matrices; servo motors; quadruped_robots; robot_models"
        self.assertEqual([(text[s:e], row["target"]) for s, e, row in glossary.matches(text)],
                         [("quadruped robots", "四足机器人"), ("Jacobian matrices", "雅可比矩阵"),
                          ("servo motors", "伺服电机")])

    def test_correct_terms_are_not_replaced_again_by_shorter_aliases(self):
        glossary = local.Glossary(local.GLOSSARY_FILE)
        self.assertEqual(glossary.normalize("quadruped robots", "四足机器人和四重机器人"),
                         "四足机器人和四足机器人")
        for source, row in glossary.entries.items():
            with self.subTest(source=source):
                self.assertEqual(glossary.normalize(source, row["target"]), row["target"])

    def test_ocr_repairs_are_limited_to_known_words_and_model_names(self):
        source = "Abstract—T his article mentions ANY mal, Unitree Al, and Unitree Gol."
        self.assertEqual(local.repair_ocr(source),
                         "Abstract—This article mentions ANYmal, Unitree A1, and Unitree Go1.")
        unchanged = ["T history; any malfunction; Al alloys; Unitree A2; x = l",
                     "`T his Unitree Al` and $T his + Al$; actor_state_dict; foo.UnitreeAl",
                     "已有中文 Unitree Al"]
        for text in unchanged:
            self.assertEqual(local.repair_ocr(text), text)

    def test_complete_robot_names_take_priority_over_acronym_substrings(self):
        glossary = local.Glossary(local.GLOSSARY_FILE)
        source = "MIT Cheetah and Cheetah III use actor_state_dict."
        self.assertEqual([target for _s, _e, target in glossary.protected_spans(source)],
                         ["MIT Cheetah", "Cheetah III", "actor_state_dict"])

    def test_custom_source_and_ocr_variants_and_invalid_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terms.json"
            row = {"source": "TestRobot A1", "target": "TestRobot A1",
                   "source_variants": ["test robot A1"], "ocr_variants": ["TestRobot Al"]}
            path.write_text(json.dumps({"terms": [row]}))
            glossary = local.Glossary(path)
            self.assertEqual(glossary.matches("test robot A1")[0][2]["target"], "TestRobot A1")
            self.assertEqual(local.repair_ocr("TestRobot Al", glossary), "TestRobot A1")
            for field, bad in [("source_variants", "robots"), ("ocr_variants", [1]),
                               ("source_variants", ["TestRobot A1"]), ("ocr_variants", [""])]:
                invalid = dict(row, **{field: bad})
                path.write_text(json.dumps({"terms": [invalid]}))
                with self.assertRaises(local.LocalTranslationError):
                    local.Glossary(path)

    def test_quantified_frequency_and_punctuation_preserve_math_and_code(self):
        source = "The algorithm can improve solving frequency by ~11x compared with Gurobi."
        self.assertEqual(local.polish_translation(source,
            "与Gurobi相比，算法可以提高~11x的求解频率."),
            "与Gurobi相比，算法可以使求解频率达到原来的约11倍。")
        self.assertEqual(local.polish_translation("The gain is 11x.", "提高11x的求解频率"),
                         "提高11x的求解频率")
        text = "刚度为3.5 Nm/rad，调用foo.bar(x,y)，使用`a,b.`和$x,y$，见[1],结束."
        self.assertEqual(local.polish_translation("", text),
                         "刚度为3.5 Nm/rad，调用foo.bar(x,y)，使用`a,b.`和$x,y$，见[1]，结束。")

    def test_cropped_attribution_is_repaired_only_in_its_statement_context(self):
        self.assertEqual(local.polish_translation("authors’ knowledge, this is the first time.",
                         "作者的知识是，这是第一次。"), "据作者所知，这是第一次。")
        self.assertEqual(local.polish_translation("The authors' knowledge is limited.",
                         "作者的知识是有限的。"), "作者的知识是有限的。")

    def test_alignment_includes_first_character_attached_to_a_space_token(self):
        # Reproduce the model's actual split: 四 aligns to a standalone ▁,
        # while 重奏乐 aligns to Quadruped Locomotion. Repair the whole term.
        engine = local.Engine.__new__(local.Engine)
        engine.glossary = local.Glossary(local.GLOSSARY_FILE)
        engine.tokenizer = mock.Mock()
        engine.tokenizer.decode.side_effect = lambda tokens: "".join(tokens)
        proto = SimpleNamespace(pieces=[SimpleNamespace(begin=b, end=e)
                                       for b, e in [(0, 2), (2, 3), (3, 12), (12, 23), (23, 24)]])
        result = SimpleNamespace(hypotheses=[["1)", "▁四", "重", "奏", "乐", "."]], attention=[[
            [1, 0, 0, 0, 0], [0, .55, .4, .05, 0], [0, 0, .9, .1, 0],
            [0, 0, .9, .1, 0], [0, 0, .1, .9, 0], [0, 0, 0, 0, 1]]])
        text = engine.correct_aligned_terms("1) Quadruped Locomotion.", proto, result)
        self.assertEqual(text.replace(" ", ""), "1)四足运动.")

    def test_repairs_do_not_remove_legitimate_repeated_chinese_characters(self):
        text = "人人参与，天天运行，四足机器人在四足平台上运动。"
        glossary = local.Glossary(local.GLOSSARY_FILE)
        self.assertEqual(glossary.normalize("quadruped robots", text), text)
        self.assertEqual(local.polish_translation("Everyone works daily.", text), text)

    def test_alignment_uses_phrase_mass_when_the_largest_token_is_elsewhere(self):
        engine = local.Engine.__new__(local.Engine)
        engine.glossary = local.Glossary(local.GLOSSARY_FILE)
        engine.tokenizer = mock.Mock()
        engine.tokenizer.decode.side_effect = lambda tokens: "".join(tokens)
        proto = SimpleNamespace(pieces=[SimpleNamespace(begin=b, end=e)
                                       for b, e in [(0, 4), (4, 6), (6, 9), (9, 20), (20, 32)]])
        result = SimpleNamespace(hypotheses=[["四方", "机", "车", "研究", "."]], attention=[[
            [.22, .18, .17, .10, .33], [.1, .1, .1, .7, 0],
            [.1, .1, .1, .7, 0], [0, 0, 0, .1, .9], [0, 0, 0, 0, 1]]])
        text = engine.correct_aligned_terms("Quadruped locomotion is studied.", proto, result)
        self.assertEqual(text, "四足运动研究.")

    def test_long_phrases_win_and_identifiers_are_not_matched_inside(self):
        glossary = local.Glossary(local.GLOSSARY_FILE)
        text = "joint torque and torque; robot_torque_control"
        matches = glossary.matches(text)
        self.assertEqual([(text[s:e], row["target"]) for s, e, row in matches],
                         [("joint torque", "关节力矩"), ("torque", "力矩")])

    def test_alias_repair_requires_the_corresponding_english_source(self):
        glossary = local.Glossary(local.GLOSSARY_FILE)
        self.assertEqual(glossary.normalize("The stiffness increases.", "僵硬性增加。"), "刚度增加。")
        self.assertEqual(glossary.normalize("Joint compliance reduces contact force.", "关节合规性减小接触力。"),
                         "关节柔顺性减小接触力。")
        self.assertEqual(glossary.normalize("The gearbox increases friction.", "僵硬性增加。"), "僵硬性增加。")

    def test_malformed_and_duplicate_entries_have_a_readable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "glossary.json"
            for content in ("invalid json", json.dumps({"terms": [
                    {"source": "robot", "target": "机器人"},
                    {"source": "ROBOT", "target": "机器"}]})):
                path.write_text(content)
                with self.assertRaisesRegex(local.LocalTranslationError, "专业术语表"):
                    local.Glossary(path)

    def test_disabling_the_glossary_keeps_general_translation_available(self):
        with mock.patch.dict(os.environ, {"SCREENSHOT_TRANSLATOR_GLOSSARY": "off"}):
            self.assertIsNone(local.glossary_path())
            self.assertEqual(local.glossary_identity(), "disabled")
            self.assertEqual(local.Glossary(None).matches("inverse kinematics"), [])

    def test_sentence_chunking_does_not_drop_english_or_symbols(self):
        text = "An English sentence with torque τ and a reference [1]. " * 30
        chunks = local.split_sentences(text, limit=100)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        self.assertEqual("".join(chunks).replace(" ", ""), text.replace(" ", ""))


class LocalBackendTest(unittest.TestCase):
    def setUp(self):
        backend = mock.patch.object(app, "TRANSLATION_BACKEND", "local")
        backend.start()
        self.addCleanup(backend.stop)

    def test_local_is_the_default_when_no_backend_override_is_set(self):
        self.assertEqual(os.environ.get("SCREENSHOT_TRANSLATOR_BACKEND", "local"), "local")

    def test_local_selection_never_falls_back_to_http(self):
        texts = ["The robot learns.", "已有中文", "actor_state_dict", "Gurobi"]
        progress = mock.Mock()
        with mock.patch.object(app, "cache_get", return_value=None), \
                mock.patch.object(app, "cache_set"), \
                mock.patch.object(app._local_client, "translate", return_value="机器人学习。") as worker, \
                mock.patch.object(app, "_translate_remote", side_effect=AssertionError("HTTP forbidden")):
            self.assertEqual(app.translate_paragraphs(texts, progress),
                             ["机器人学习。", "已有中文", "actor_state_dict", "Gurobi"])
        worker.assert_called_once_with("The robot learns.")
        self.assertEqual(progress.call_args.args, (4, 4))

    def test_returned_english_is_rejected_without_caching_or_http_fallback(self):
        with mock.patch.object(app, "cache_get", return_value=None), \
                mock.patch.object(app, "cache_set") as cache, \
                mock.patch.object(app._local_client, "translate", return_value="The robot learns."), \
                mock.patch.object(app, "_translate_remote", side_effect=AssertionError("HTTP forbidden")):
            self.assertEqual(app.translate_paragraphs(["The robot learns."]), [None])
        cache.assert_not_called()
        self.assertIn("未生成中文", app._local_error)

    def test_local_error_is_not_misreported_as_a_network_failure(self):
        paragraphs = [{"text": "The robot learns.", "box": (10, 10, 100, 20)}]
        with mock.patch.object(app, "capture"), \
                mock.patch.object(app, "parse_ocr_paragraphs", return_value=paragraphs), \
                mock.patch.object(app, "cache_get", return_value=None), \
                mock.patch.object(app._local_client, "translate", side_effect=
                                  local.LocalTranslationError("本地模型未安装")), \
                mock.patch.object(app, "save_cache"), \
                mock.patch.object(app, "render_translation") as render, \
                mock.patch.object(app, "show_friendly_error") as error, \
                mock.patch.object(app.GLib, "idle_add", side_effect=lambda fn, *args: fn(*args)):
            app.process((0, 0, 200, 100))
        render.assert_not_called()
        self.assertIn("本地模型未安装", error.call_args.args[0])
        self.assertNotIn("无法连接", error.call_args.args[0])

    def test_cache_separates_backends_and_invalidates_after_term_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terms.json"
            path.write_text('{"terms": []}')
            with mock.patch.dict(os.environ, {"SCREENSHOT_TRANSLATOR_GLOSSARY": str(path)}):
                first = app._cache_key("The robot learns.")
                path.write_text('{"terms": [{"source":"robot","target":"机器人"}]}')
                self.assertNotEqual(first, app._cache_key("The robot learns."))
                with mock.patch.object(app, "TRANSLATION_BACKEND", "remote"):
                    self.assertNotEqual(first, app._cache_key("The robot learns."))

    def test_old_pipeline_cache_is_ignored_even_with_same_glossary(self):
        for version in ["v1", "v2", "v3"]:
            key = json.dumps(["en-zh-v4", app.SOURCE_LANG,
                              ["local-argos-en-zh-1.9-" + version, local.glossary_identity()],
                              "The robot learns."], ensure_ascii=False)
            with mock.patch.object(app, "_cache", {key: "旧译文"}):
                self.assertIsNone(app.cache_get("The robot learns."))

    def test_cache_shares_known_ocr_repairs_with_correct_source(self):
        self.assertEqual(app._cache_key("T his article mentions Unitree Al."),
                         app._cache_key("This article mentions Unitree A1."))


@unittest.skipUnless(local.PYTHON.is_file() and (local.MODEL_DIR / "model/model.bin").is_file(),
                     "Run scripts/setup_local_translation.sh for real offline inference tests")
class RealLocalInferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = local.LocalClient()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_robotics_terminology_uses_recommended_chinese(self):
        samples = [
            ("The robot uses model predictive control and inverse kinematics.", ["模型预测控制", "逆运动学"]),
            ("The robot uses the end effector and harmonic drive.", ["末端执行器", "谐波传动"]),
            ("Joint torque and stiffness improve quadruped locomotion.", ["关节力矩", "刚度", "四足运动"]),
            ("Forward kinematics and inverse dynamics are used in motion planning.", ["正运动学", "逆动力学", "运动规划"]),
            ("The actuator drives a planetary gearbox with a gear ratio of 10.", ["执行器", "行星齿轮箱", "传动比"]),
            ("A robust controller solves a convex optimization problem using Gurobi.", ["鲁棒", "控制器", "凸", "Gurobi"]),
        ]
        for text, terms in samples:
            with self.subTest(text=text):
                translated = self.client.translate(text)
                for term in terms:
                    self.assertIn(term, translated)
                self.assertNotIn("运动学学", translated)

    def test_user_paper_segments_keep_names_conclusions_and_citations(self):
        samples = json.loads((Path(__file__).parent / "fixtures/robotics_paper_segments.json").read_text())
        for sample in samples:
            with self.subTest(name=sample["name"]):
                translated = self.client.translate(sample["source"])
                for term in sample["required"]:
                    self.assertIn(term, translated)
                for term in sample["forbidden"]:
                    self.assertNotIn(term, translated)
                self.assertEqual(re.findall(r"\[\d+\]", translated),
                                 re.findall(r"\[\d+\]", sample["source"]))

    def test_plural_mechanical_terms_do_not_leave_partial_english_words(self):
        self.assertEqual(self.client.translate("The Jacobian matrices map joint torques to contact forces."),
                         "雅可比矩阵将关节力矩映射到接触力。")
        text = self.client.translate("The servo motors drive ball screws and revolute joints.")
        for term in ["伺服电机", "滚珠丝杠", "转动关节"]:
            self.assertIn(term, text)

    def test_headings_and_repeated_terms_do_not_duplicate_chinese_prefixes(self):
        for source, term, count in [
            ("1) Quadruped Locomotion: Following the pioneering work on the classic heuristic controller [19].",
             "四足运动", 1),
            ("1) Quadruped Locomotion Control: Following the pioneering work on the classic heuristic controller [19].",
             "四足运动控制", 1),
            ("Quadruped locomotion improves, and quadruped locomotion becomes robust.", "四足运动", 2),
            ("Quadruped locomotion and quadruped locomotion control are studied.", "四足运动", 2),
        ]:
            with self.subTest(source=source):
                translated = self.client.translate(source)
                self.assertEqual(translated.count(term), count)
                self.assertNotIn("四四足", translated)
                self.assertNotIn("四方四足", translated)
                self.assertNotIn("运动学学", translated)

    def test_technical_names_formula_units_and_greek_letters_survive(self):
        for text, terms in [
            ("The MLPModel loads actor_state_dict using load_state_dict().",
             ["MLPModel", "actor_state_dict", "load_state_dict()"]),
            ("The joint torque is τ = JᵀF and the stiffness is 3.5 Nm/rad.",
             ["关节力矩", "τ = JᵀF", "刚度", "3.5 Nm/rad"]),
            ("The PID controller uses an IMU for state estimation.", ["PID", "IMU", "状态估计"]),
        ]:
            with self.subTest(text=text):
                translated = self.client.translate(text)
                for term in terms:
                    self.assertIn(term, translated)

    def test_worker_is_reused_between_paragraphs(self):
        self.client.translate("The robot moves.")
        pid = self.client.process.pid
        self.client.translate("The robot stops.")
        self.assertEqual(self.client.process.pid, pid)

    def test_common_ui_labels_translate_as_distinct_actions(self):
        self.assertEqual([self.client.translate(text) for text in ["Search", "Sign in", "Sign up"]],
                         ["搜索", "登录", "注册"])

    def test_button_screenshot_recognizes_translates_and_renders_all_labels(self):
        source = Path(__file__).parent / "fixtures/ui_buttons.png"
        paragraphs = app.parse_ocr_paragraphs(source)
        with mock.patch.object(app, "_local_client", self.client), \
                mock.patch.object(app, "cache_get", return_value=None), \
                mock.patch.object(app, "cache_set"), \
                mock.patch.object(app, "_translate_remote", side_effect=AssertionError("HTTP forbidden")):
            translated = app.translate_paragraphs([p["text"] for p in paragraphs])
        self.assertEqual(translated, ["搜索", "/", "登录", "注册"])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(app, "ROOT", Path(directory)):
                result = app.render_translation(paragraphs, translated, source)
            with Image.open(result) as image:
                self.assertEqual(image.size, (419, 39))

    def test_custom_term_file_is_loaded_again_when_it_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terms.json"
            client = local.LocalClient()
            try:
                with mock.patch.dict(os.environ, {"SCREENSHOT_TRANSLATOR_GLOSSARY": str(path)}):
                    path.write_text(json.dumps({"terms": [{"source": "inverse kinematics", "target": "逆运动学"}]}))
                    self.assertEqual(client.translate("inverse kinematics"), "逆运动学")
                    path.write_text(json.dumps({"terms": [{"source": "inverse kinematics", "target": "自定义逆解"}]}))
                    self.assertEqual(client.translate("inverse kinematics"), "自定义逆解")
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
