"""Source preservation and readable fallback for crowded screenshots."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops, ImageDraw

import screenshot_translator as app


class LayoutQualityTest(unittest.TestCase):
    def test_near_full_width_abstract_replaces_english_in_place(self):
        paragraphs = [{"text": "Abstract—This article considers robot control. " * 20,
                       "box": (3, 14, 416, 275)}]
        layouts, canvas = app.calculate_layout(
            paragraphs, ["摘要——本文研究机器人控制。" * 15], (426, 300))
        self.assertEqual(len(layouts), 1)
        self.assertFalse(layouts[0]["detached"])
        x, y, width, height = layouts[0]["rect"]
        self.assertLess(y, 20)
        self.assertLessEqual(x, 3)
        self.assertGreaterEqual(x + width, 419)
        self.assertGreaterEqual(y + height, 289)
        self.assertTrue(app.validate_layout(layouts, canvas)[0])

    def test_bottom_overflow_grows_translation_at_anchor(self):
        paragraphs = [{"text": "A long abstract", "box": (0, 0, 200, 40)}]
        layouts, canvas = app.calculate_layout(paragraphs, ["机器人控制研究。" * 30], (200, 40))
        self.assertFalse(layouts[0]["detached"])
        self.assertEqual(layouts[0]["rect"][:2], (0, 0))
        self.assertGreater(canvas[1], 40)
        self.assertTrue(app.validate_layout(layouts, canvas)[0])

    def test_unchanged_and_failed_paragraphs_leave_source_pixels_intact(self):
        paragraphs = [{"text": "PPO 训练得到的权重", "box": (30, 25, 150, 20)},
                      {"text": "Load settings", "box": (30, 65, 100, 20)}]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            image = Image.new("RGB", (220, 120), (20, 20, 20))
            ImageDraw.Draw(image).text((30, 25), paragraphs[0]["text"], font=app.font(14), fill="white")
            image.save(source)
            with mock.patch.object(app, "ROOT", Path(directory)):
                out = app.render_translation(paragraphs, [paragraphs[0]["text"], None], source)
            with Image.open(out) as result:
                self.assertEqual(result.size, image.size)
                self.assertIsNone(ImageChops.difference(image, result).getbbox())

    def test_replacement_uses_free_horizontal_space_without_covering_chinese(self):
        paragraphs = [{"text": "Settings", "box": (20, 20, 50, 20)},
                      {"text": "已有中文", "box": (200, 20, 80, 20)}]
        layouts, canvas = app.calculate_layout(paragraphs, ["保存当前设置", "已有中文"], (320, 140))
        self.assertEqual(len(layouts), 1)
        self.assertFalse(layouts[0]["detached"])
        self.assertEqual(layouts[0]["lines"], ["保存当前设置"])
        self.assertFalse(app._overlaps_any(layouts[0]["rect"], [paragraphs[1]["box"]]))
        self.assertTrue(app.validate_layout(layouts, canvas)[0])

    def test_crowded_translation_goes_below_image_without_repeating_english(self):
        paragraphs = [{"text": "Settings", "box": (20, 20, 80, 20)},
                      {"text": "中文", "box": (105, 20, 70, 20)},
                      {"text": "下一行", "box": (20, 44, 150, 30)}]
        layouts, canvas = app.calculate_layout(
            paragraphs, ["保存当前设置并重新启动程序", "中文", "下一行"], (200, 90))
        self.assertEqual(len(layouts), 1)
        layout = layouts[0]
        self.assertTrue(layout["detached"])
        self.assertGreaterEqual(layout["rect"][1], 90)
        self.assertEqual("".join(layout["lines"]), "保存当前设置并重新启动程序")
        self.assertNotIn("Settings", "".join(layout["lines"]))
        self.assertTrue(app.validate_layout(layouts, canvas)[0])

    def test_long_translated_paragraphs_flow_down_instead_of_showing_source_pairs(self):
        paragraphs = [{"text": "First paragraph", "box": (10, 10, 170, 20)},
                      {"text": "Second paragraph", "box": (10, 40, 170, 20)}]
        translated = ["第一段机器人控制译文。" * 20, "第二段研究方法译文。" * 20]
        layouts, canvas = app.calculate_layout(paragraphs, translated, (200, 90))
        self.assertEqual(len(layouts), 2)
        self.assertTrue(all(not lay["detached"] for lay in layouts))
        self.assertGreaterEqual(layouts[1]["rect"][1],
                                layouts[0]["rect"][1] + layouts[0]["rect"][3] + app.BOX_GAP)
        for layout, text in zip(layouts, translated):
            self.assertEqual("".join(layout["lines"]), text)
        self.assertTrue(app.validate_layout(layouts, canvas)[0])

    def test_detached_translation_erases_original_english_and_keeps_chinese_pixels(self):
        paragraphs = [{"text": "Settings", "box": (20, 20, 80, 20)},
                      {"text": "中文", "box": (105, 20, 70, 20)},
                      {"text": "下一行", "box": (20, 44, 150, 30)}]
        image = Image.new("RGB", (200, 90), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 99, 39), fill="red")
        draw.text((105, 20), "中文", font=app.font(14), fill="black")
        draw.text((20, 44), "下一行", font=app.font(14), fill="black")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            image.save(source)
            with mock.patch.object(app, "ROOT", Path(directory)):
                out = app.render_translation(paragraphs,
                    ["保存当前设置并重新启动程序", "中文", "下一行"], source)
            with Image.open(out) as result:
                original_region = result.crop((20, 20, 100, 40))
                self.assertIsNone(ImageChops.difference(original_region,
                    Image.new("RGB", original_region.size, "white")).getbbox())
                for x, y, w, h in [paragraphs[1]["box"], paragraphs[2]["box"]]:
                    box = (x, y, x + w, y + h)
                    self.assertIsNone(ImageChops.difference(image.crop(box), result.crop(box)).getbbox())

    def test_dark_background_translation_does_not_create_white_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            Image.new("RGB", (300, 100), (20, 20, 20)).save(source)
            paragraphs = [{"text": "Settings", "box": (30, 30, 80, 20)}]
            with mock.patch.object(app, "ROOT", Path(directory)):
                out = app.render_translation(paragraphs, ["设置"], source)
            with Image.open(out) as result:
                self.assertEqual(result.getpixel((25, 27)), (20, 20, 20))


if __name__ == "__main__":
    unittest.main()
