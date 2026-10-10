import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFont

import screenshot_translator as app


SMALL_TEXT = "Contains brace with quote character"
IMAGE_SIZE = (260, 35)
SMALL_TEXT_PNG = base64.b64decode(
    "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"
)


class OcrTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temp_dir.name) / "small-text.png"
        self.image_path.write_bytes(SMALL_TEXT_PNG)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_small_text_is_recognized_in_source_coordinates(self):
        paragraphs = app.parse_ocr_paragraphs(self.image_path)

        self.assertEqual([paragraph["text"] for paragraph in paragraphs], [SMALL_TEXT])
        for paragraph in paragraphs:
            left, top, width, height = paragraph["box"]
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(left + width, IMAGE_SIZE[0])
            self.assertLessEqual(top + height, IMAGE_SIZE[1])

    def test_language_extraction_recognizes_small_text(self):
        original_cache = app.CACHE
        app.CACHE = self.image_path
        try:
            self.assertEqual(app.run_extract("eng"), SMALL_TEXT)
        finally:
            app.CACHE = original_cache

    def test_tesseract_failure_is_not_reported_as_empty_text(self):
        with self.assertRaises(subprocess.CalledProcessError):
            app._run_tesseract(
                self.image_path,
                "language_model_that_does_not_exist",
                app.OCR_PSM,
            )

    def test_ignores_full_height_border_misread_as_text(self):
        image = Image.new("RGB", (360, 45), (36, 0, 31))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 8, 44), fill=(180, 180, 180))
        draw.text(
            (20, 8),
            SMALL_TEXT,
            font=ImageFont.truetype(app.FONT, 12),
            fill=(255, 255, 255),
        )
        image.save(self.image_path)

        paragraphs = app.parse_ocr_paragraphs(self.image_path)

        self.assertEqual([paragraph["text"] for paragraph in paragraphs], [SMALL_TEXT])

    def test_ignores_full_height_edge_glyphs(self):
        header = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext\n"
        )
        page = "1\t1\t0\t0\t0\t0\t0\t0\t600\t80\t-1\t\n"
        for glyph in ("|", "/", "1", "I", "l"):
            word = "5\t1\t1\t1\t1\t1\t0\t0\t12\t80\t95\t%s\n" % glyph
            with self.subTest(glyph=glyph), mock.patch.object(
                app, "_run_tesseract", return_value=header + page + word
            ):
                self.assertEqual(app.parse_ocr_paragraphs(self.image_path), [])

    def test_joins_ocr_tokens_by_script_and_punctuation(self):
        self.assertEqual(
            app._join_ocr_tokens(["截图", "翻译", "，", "fast"]),
            "截图翻译，fast",
        )
        self.assertEqual(app._join_ocr_tokens(["fast", "OCR"]), "fast OCR")
        self.assertEqual(app._join_ocr_tokens(["hello", ",", "world"]), "hello, world")
        self.assertEqual(
            app._join_ocr_tokens(["He", "said", "“", "hello", "”"]),
            "He said “hello”",
        )
        self.assertEqual(app._join_ocr_tokens(["$", "100"]), "$100")

    def test_cleans_wrapped_english_and_chinese_lines(self):
        self.assertEqual(
            app._clean_ocr_text("trans-\nlation 中文\n断行"),
            "translation 中文断行",
        )

    def test_repairs_pdf_ligatures_and_soft_hyphen_wraps(self):
        self.assertEqual(app._clean_ocr_text("Ef\ufb01cient reinforce\u00ad\nment"),
                         "Efficient reinforcement")

    def test_preserves_clear_hyphenated_compounds(self):
        self.assertEqual(
            app._clean_ocr_text("self-\nsupervised state-of-\nthe-art trans-\nlation"),
            "self-supervised state-of-the-art translation",
        )

    def test_repairs_contractions_without_joining_quoted_words(self):
        self.assertEqual(app._clean_ocr_text("We ' re ready. It is 'valid'."),
                         "We're ready. It is 'valid'.")

    def test_dark_background_and_tight_crop_still_use_source_coordinates(self):
        image = Image.new("RGB", (300, 25), (30, 30, 30))
        draw = ImageDraw.Draw(image)
        text = "The model learns from feedback."
        draw.text((0, 0), text,
                  font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12),
                  fill="white")
        image.save(self.image_path)
        paragraphs = app.parse_ocr_paragraphs(self.image_path)
        self.assertEqual([p["text"] for p in paragraphs], [text])
        x, y, width, height = paragraphs[0]["box"]
        self.assertLess(x, 5)
        self.assertLess(y, 5)
        self.assertLessEqual(x + width, image.width)
        self.assertLessEqual(y + height, image.height)

    def test_empty_small_capture_retries_with_mixed_language_detection(self):
        header = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
                  "left\ttop\twidth\theight\tconf\ttext\n")
        page = "1\t1\t0\t0\t0\t0\t0\t0\t540\t90\t-1\t\n"
        word = "5\t1\t1\t1\t1\t1\t10\t10\t80\t20\t95\tSettings\n"
        with mock.patch.object(app, "_run_tesseract", side_effect=[
                header + page, header + page + word]) as run:
            paragraphs = app.parse_ocr_paragraphs(self.image_path)
        self.assertEqual(paragraphs, [{"text": "Settings", "box": (0, 0, 40, 10)}])
        self.assertEqual([call.args[1:3] for call in run.call_args_list],
                         [("eng+chi_sim", "3"), ("eng+chi_sim", "6")])

    def test_distant_diagram_labels_in_one_ocr_line_stay_separate(self):
        header = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
                  "left\ttop\twidth\theight\tconf\ttext\n")
        rows = (
            "1\t1\t0\t0\t0\t0\t0\t0\t540\t90\t-1\t\n"
            "5\t1\t1\t1\t1\t1\t30\t20\t80\t20\t95\tMLPModel\n"
            "5\t1\t1\t1\t1\t2\t350\t20\t130\t20\t95\tactor_state_dict\n"
        )
        with mock.patch.object(app, "_run_tesseract", return_value=header + rows):
            paragraphs = app.parse_ocr_paragraphs(self.image_path)
        self.assertEqual([p["text"] for p in paragraphs], ["MLPModel", "actor_state_dict"])
        self.assertEqual([p["box"] for p in paragraphs], [(10, 5, 40, 10), (170, 5, 65, 10)])

    def test_adjacent_sign_in_and_sign_up_buttons_are_separate_phrases(self):
        # Real TSV positions from the user's 419x39 capture. Keep the word
        # space within each label; split the larger gap between the buttons.
        Image.new("RGB", (419, 39)).save(self.image_path)
        header = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
                  "left\ttop\twidth\theight\tconf\ttext\n")
        rows = "1\t1\t0\t0\t0\t0\t0\t0\t858\t98\t-1\t\n"
        for n, (x, y, w, h, text) in enumerate([
            (62, 24, 101, 36, "Search"), (410, 28, 11, 22, "/"),
            (519, 28, 63, 28, "Sign"), (594, 28, 24, 22, "in"),
            (696, 28, 63, 28, "Sign"), (771, 34, 35, 22, "up"),
        ], 1):
            rows += f"5\t1\t1\t1\t1\t{n}\t{x}\t{y}\t{w}\t{h}\t95\t{text}\n"
        with mock.patch.object(app, "_run_tesseract", return_value=header + rows):
            paragraphs = app.parse_ocr_paragraphs(self.image_path)
        self.assertEqual([p["text"] for p in paragraphs], ["Search", "/", "Sign in", "Sign up"])
        self.assertEqual(paragraphs[2]["box"], (254, 9, 50, 14))
        self.assertEqual(paragraphs[3]["box"], (343, 9, 55, 14))

    def test_overlapping_chinese_boxes_keep_tesseract_word_order(self):
        header = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
                  "left\ttop\twidth\theight\tconf\ttext\n")
        rows = (
            "1\t1\t0\t0\t0\t0\t0\t0\t540\t90\t-1\t\n"
            "5\t1\t1\t1\t1\t1\t30\t20\t20\t20\t95\t网\n"
            "5\t1\t1\t1\t1\t2\t50\t20\t20\t20\t95\t络\n"
            "5\t1\t1\t1\t1\t3\t46\t20\t60\t20\t95\t结构\n"
        )
        with mock.patch.object(app, "_run_tesseract", return_value=header + rows):
            paragraphs = app.parse_ocr_paragraphs(self.image_path)
        self.assertEqual([p["text"] for p in paragraphs], ["网络结构"])


if __name__ == "__main__":
    unittest.main()
