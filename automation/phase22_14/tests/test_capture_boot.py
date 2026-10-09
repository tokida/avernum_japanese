import importlib.util
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "capture_boot.py"
spec = importlib.util.spec_from_file_location("phase22_14_capture_boot", MODULE_PATH)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


SAFE_PILOT = '''
def main():
    parser.add_argument("--detect-only", dest="click_choice", action="store_false")
    parser.set_defaults(click_choice=True)
    if args.click_choice:
        game.ensure_active()
        click_ydotool(1, 2)
'''


class CaptureBootTests(unittest.TestCase):
    def test_detect_only_guards_click_and_focus(self):
        self.assertTrue(capture.detect_only_contract_safe(SAFE_PILOT))

    def test_unconditional_click_fails_static_guard(self):
        self.assertFalse(capture.detect_only_contract_safe(SAFE_PILOT + "\n    click_ydotool(3, 4)\n"))

    def test_window_search_requires_one_game_title(self):
        self.assertEqual(capture.parse_window_candidates("opaque-1\n", {"opaque-1": "Avernum"}), ("unique", "opaque-1"))
        self.assertEqual(capture.parse_window_candidates("opaque-1\nopaque-2\n", {"opaque-1": "Avernum", "opaque-2": "Avernum"}), ("ambiguous", None))

    def test_non_active_or_ambiguous_target_is_not_capture_eligible(self):
        self.assertFalse(capture.screenshot_eligible("unique", "id-a", "id-b"))
        self.assertFalse(capture.screenshot_eligible("ambiguous", "id-a", "id-a"))
        self.assertTrue(capture.screenshot_eligible("unique", "id-a", "id-a"))

    def test_capture_skips_when_focus_does_not_match(self):
        calls = []
        def runner(command, timeout=8):
            calls.append(command)
            if command[1] == "search":
                return SimpleNamespace(returncode=0, stdout="opaque-game\n", stderr="")
            if command[1] == "getwindowname":
                return SimpleNamespace(returncode=0, stdout="Avernum", stderr="")
            if command[1] == "getactivewindow":
                return SimpleNamespace(returncode=0, stdout="another-window\n", stderr="")
            raise AssertionError("capture must not be invoked")
        status = capture.capture_active_window("kdotool", "spectacle", "opaque-game", Path("frame.png"), runner=runner)
        self.assertEqual(status, "inactive_window")
        self.assertFalse(any("--output" in call for call in calls))

    def test_png_signature_and_spectacle_cli_validation(self):
        ihdr = (13).to_bytes(4, "big") + b"IHDR" + b"\x00" * 13 + b"\x00" * 4
        iend = (0).to_bytes(4, "big") + b"IEND" + b"\x00" * 4
        self.assertTrue(capture.valid_png(capture.PNG_SIGNATURE + ihdr + iend))
        self.assertFalse(capture.valid_png(b"not a png"))
        text = " ".join(capture.REQUIRED_SPECTACLE_OPTIONS)
        self.assertTrue(capture.spectacle_options_supported(text, 0))
        self.assertFalse(capture.spectacle_options_supported(text, 1))
        self.assertFalse(capture.spectacle_options_supported("--activewindow", 0))

    def test_png_without_iend_is_rejected(self):
        ihdr = (13).to_bytes(4, "big") + b"IHDR" + b"\x00" * 13 + b"\x00" * 4
        self.assertFalse(capture.valid_png(capture.PNG_SIGNATURE + ihdr))

    def test_current_phase22_13_pilot_contract_is_safe(self):
        if not capture.PILOT_SOURCE.is_file():
            self.skipTest("Phase22.13 pilot not present")
        source = capture.PILOT_SOURCE.read_text(encoding="utf-8")
        self.assertTrue(capture.detect_only_contract_safe(source))

    def test_dry_run_report_claims_no_side_effects(self):
        report = capture.make_report()
        self.assertEqual(report["status"], "dry_run")
        self.assertFalse(report["pilot_started"])
        self.assertEqual(report["focus_change_status"], "not_requested")
        self.assertFalse(report["input_attempted"])
        self.assertEqual(report["screenshots"], [])

    def test_report_has_no_user_path_or_window_identifier(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "/private/session/secret"}):
            encoded = json.dumps(capture.make_report(), ensure_ascii=False)
        self.assertNotIn(str(Path.home()), encoded)
        self.assertNotIn("opaque-game", encoded)
        self.assertNotIn("/private/session/secret", encoded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
