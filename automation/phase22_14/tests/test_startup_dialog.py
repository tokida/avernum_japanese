import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "startup_dialog.py"
spec = importlib.util.spec_from_file_location("phase22_14_startup_dialog", MODULE_PATH)
dialog = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dialog
spec.loader.exec_module(dialog)


GOOD_WINDOW = dialog.WindowInfo(
    "12345678-1234-1234-1234-123456789abc", "Avernum", 100, 200, 400, 554
)


class StartupDialogTests(unittest.TestCase):
    def test_reference_points_are_inside_expected_dialog(self):
        self.assertTrue(all(
            dialog.point_inside_window(point["x"], point["y"], 400, 554)
            for point in dialog.RELATIVE_CANDIDATES
        ))
        self.assertFalse(dialog.point_inside_window(400, 100, 400, 554))

    def test_relative_to_screen_coordinate_calculation(self):
        self.assertEqual(dialog.relative_to_screen(100, 200, 145, 161), (245, 361))

    def test_reference_geometry_match_and_mismatch(self):
        self.assertTrue(dialog.geometry_matches_reference(400, 554))
        self.assertTrue(dialog.geometry_matches_reference(408, 546))
        result = dialog.evaluate_candidate(
            [dialog.WindowInfo(GOOD_WINDOW.window_id, "Avernum", 0, 0, 430, 554)],
            GOOD_WINDOW.window_id,
            wayland_kde=True,
        )
        self.assertFalse(result["geometry_matches_reference"])
        self.assertIn("window_geometry_mismatch", result["block_reasons"])

    def test_multiple_windows_and_focus_mismatch_are_blocked(self):
        second = dialog.WindowInfo(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "Avernum", 0, 0, 400, 554
        )
        multiple = dialog.evaluate_candidate([GOOD_WINDOW, second], GOOD_WINDOW.window_id, wayland_kde=True)
        self.assertIn("multiple_exact_windows", multiple["block_reasons"])
        focus = dialog.evaluate_candidate([GOOD_WINDOW], second.window_id, wayland_kde=True)
        self.assertIn("active_window_mismatch", focus["block_reasons"])

    def test_plan_never_authorizes_input_without_visual_verification(self):
        result = dialog.evaluate_candidate([GOOD_WINDOW], GOOD_WINDOW.window_id, wayland_kde=True)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["settings_content_verified"])
        self.assertFalse(result["operation_plan_approved"])
        self.assertFalse(result["input_implemented"])
        self.assertFalse(result["input_attempted"])
        self.assertIn("settings_content_unverified_without_ocr", result["block_reasons"])
        self.assertFalse(any(
            callable(value) and any(term in name.lower() for term in ("click", "send", "press", "type", "input"))
            for name, value in vars(dialog).items()
        ))

    def test_current_window_probe_uses_metadata_queries_only(self):
        calls = []
        def runner(command, timeout=5.0):
            calls.append(command[1])
            if command[1] == "search":
                return SimpleNamespace(returncode=0, stdout="{12345678-1234-1234-1234-123456789abc}\n", stderr="")
            if command[1] == "getwindowname":
                return SimpleNamespace(returncode=0, stdout="Avernum", stderr="")
            if command[1] == "getwindowgeometry":
                return SimpleNamespace(returncode=0, stdout="x: 100 y: 200 width: 400 height: 554", stderr="")
            if command[1] == "getactivewindow":
                return SimpleNamespace(returncode=0, stdout="12345678-1234-1234-1234-123456789abc\n", stderr="")
            raise AssertionError("unexpected command")
        windows, active, status = dialog.probe_current_window("kdotool", runner)
        self.assertEqual(status, "ok")
        self.assertEqual(len(windows), 1)
        self.assertEqual(active, "12345678-1234-1234-1234-123456789abc")
        self.assertEqual(calls, ["search", "getwindowname", "getwindowgeometry", "getactivewindow"])
        self.assertFalse(any(command in calls for command in ("windowactivate", "click", "type", "key")))

    def test_non_wayland_kde_is_blocked(self):
        result = dialog.evaluate_candidate([GOOD_WINDOW], GOOD_WINDOW.window_id, wayland_kde=False)
        self.assertIn("wayland_kde_not_confirmed", result["block_reasons"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
