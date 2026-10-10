"""Monitor selection and crop regressions, without opening desktop windows."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import screenshot_translator as app


class MonitorCaptureTest(unittest.TestCase):
    def test_target_follows_pointer_instead_of_primary_monitor(self):
        display = mock.Mock()
        monitor = mock.Mock()
        display.get_default_seat().get_pointer().get_position.return_value = (
            mock.sentinel.screen, 3100, 400,
        )
        display.get_monitor_at_point.return_value = monitor
        with mock.patch.object(app.Gdk.Display, "get_default", return_value=display):
            self.assertIs(app.pointer_monitor(), monitor)
        display.get_monitor_at_point.assert_called_once_with(3100, 400)

    def test_reads_only_target_monitor_from_root(self):
        monitor = mock.Mock()
        monitor.get_geometry.return_value = SimpleNamespace(
            x=2560, y=0, width=1920, height=1080,
        )
        with mock.patch.object(app.Gdk, "get_default_root_window") as root, \
                mock.patch.object(app.Gdk, "pixbuf_get_from_window") as read:
            self.assertIs(app.capture_screen(monitor), read.return_value)
        read.assert_called_once_with(root.return_value, 2560, 0, 1920, 1080)

    def test_selection_uses_monitor_offset_and_reversed_drag(self):
        allocation = SimpleNamespace(width=1920, height=1080)
        bounds = (2560, 0, 1920, 1080)
        self.assertEqual(
            app.selection_region((500, 300), (100, 80), allocation, bounds),
            (2660, 80, 400, 220),
        )

    def test_drag_past_edges_is_clamped_to_this_monitor(self):
        allocation = SimpleNamespace(width=1920, height=1080)
        bounds = (-1920, -200, 1920, 1080)
        self.assertEqual(
            app.selection_region((-40, -10), (2400, 1200), allocation, bounds),
            bounds,
        )

    def test_selection_maps_window_allocation_to_logical_coordinates(self):
        allocation = SimpleNamespace(width=960, height=540)
        self.assertEqual(
            app.selection_region((100, 50), (300, 150), allocation,
                                 (2560, 0, 1920, 1080)),
            (2760, 100, 400, 200),
        )

    def test_saved_region_cannot_reuse_another_monitor_or_span_monitors(self):
        bounds = (2560, 0, 1920, 1080)
        self.assertTrue(app.region_within_bounds([2700, 100, 200, 100], bounds))
        for rect in ([100, 100, 200, 100], [2500, 100, 200, 100],
                     [4400, 100, 200, 100], None, [], [2700, 100, 0, 100]):
            with self.subTest(rect=rect):
                self.assertFalse(app.region_within_bounds(rect, bounds))

    def test_crop_preserves_monitor_pixels_with_offset_and_scaling(self):
        for bounds, scale in (((2560, 0, 80, 60), 1),
                              ((-80, -60, 80, 60), 2)):
            with self.subTest(bounds=bounds, scale=scale), \
                    tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                image = Image.new("RGB", (80 * scale, 60 * scale))
                # A coordinate pattern detects both a misplaced crop and
                # inadvertent downscaling of a HiDPI screenshot.
                image.putdata([(x, y, 50) for y in range(image.height)
                               for x in range(image.width)])
                source = path / "monitor.png"
                image.save(source)
                pixbuf = app.GdkPixbuf.Pixbuf.new_from_file(str(source))
                target = path / "crop.png"
                rect = (bounds[0] + 10, bounds[1] + 5, 20, 15)
                with mock.patch.object(app, "CACHE", target):
                    app.capture(rect, pixbuf, bounds)
                with Image.open(target) as result:
                    expected = image.crop((10 * scale, 5 * scale,
                                           30 * scale, 20 * scale))
                    self.assertEqual(result.size, expected.size)
                    self.assertEqual(result.tobytes(), expected.tobytes())

    def test_out_of_monitor_crop_is_rejected(self):
        with self.assertRaises(ValueError):
            app.capture((100, 100, 20, 20), mock.Mock(), (2560, 0, 1920, 1080))

    def test_saved_region_on_other_screen_opens_selector(self):
        monitor = mock.Mock()
        monitor.get_geometry.return_value = SimpleNamespace(
            x=2560, y=0, width=1920, height=1080,
        )
        with mock.patch.object(app.sys, "argv", ["app", "--capture"]), \
                mock.patch.object(app, "load_translation_cache"), \
                mock.patch.object(app, "load_state", return_value={
                    "region": [100, 100, 200, 100]}), \
                mock.patch.object(app, "acquire_task_lock"), \
                mock.patch.object(app, "pointer_monitor", return_value=monitor), \
                mock.patch.object(app, "capture_screen") as capture, \
                mock.patch.object(app, "Selector") as selector, \
                mock.patch.object(app, "show_progress") as progress, \
                mock.patch.object(app.Gtk, "main"):
            app.main()
        capture.assert_called_once_with(monitor)
        self.assertIs(selector.call_args.args[0], capture.return_value)
        self.assertIs(selector.call_args.args[2], monitor)
        progress.assert_not_called()


if __name__ == "__main__":
    unittest.main()
