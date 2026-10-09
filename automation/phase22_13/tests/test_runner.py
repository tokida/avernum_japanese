import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "runner.py"
spec = importlib.util.spec_from_file_location("phase22_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def evidence(self):
        observation = {
            "run_id": "r1", "frame_id": "f1", "window_id": "w1", "choice_id": "c1",
            "x": 10, "y": 20, "captured_at": 100.0, "event_seq": 8, "latest_event_seq": 9,
        }
        focus = {
            "run_id": "r1", "frame_id": "f1", "window_id": "w1", "choice_id": "c1",
            "focused": True, "captured_at": 100.2, "latest_event_seq": 9,
        }
        return observation, focus

    def test_current_same_context_evidence_is_accepted(self):
        obs, focus = self.evidence()
        self.assertEqual(runner.validate_click_evidence(obs, focus, now=100.5), (True, "evidence_current"))

    def test_old_run_or_frame_coordinates_are_rejected(self):
        obs, focus = self.evidence()
        focus["frame_id"] = "old-frame"
        self.assertEqual(runner.validate_click_evidence(obs, focus, now=100.5)[1], "evidence_context_mismatch")

    def test_stale_evidence_is_rejected(self):
        obs, focus = self.evidence()
        self.assertEqual(runner.validate_click_evidence(obs, focus, now=104.0)[1], "evidence_stale")

    def test_unfocused_window_is_rejected(self):
        obs, focus = self.evidence()
        focus["focused"] = False
        self.assertEqual(runner.validate_click_evidence(obs, focus, now=100.5)[1], "window_not_focused")

    def test_report_does_not_record_environment_secrets_or_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = runner.build_report(root, root, {
                "HOME": str(root), "GH_TOKEN": "fake-secret", "XDG_SESSION_TYPE": "wayland",
                "XDG_CURRENT_DESKTOP": "KDE", "WAYLAND_DISPLAY": "wayland-0",
            }, which=lambda _name: None, process_check=lambda _name: False)
            encoded = json.dumps(report)
            self.assertNotIn(str(root), encoded)
            self.assertNotIn("fake-secret", encoded)
            self.assertFalse(report["secrets_or_absolute_paths_recorded"])
            self.assertEqual(report["verification"]["japanese_display"], "未検証")

    def test_run_requires_timeout(self):
        with self.assertRaises(SystemExit) as result:
            runner.main(["--run"])
        self.assertEqual(result.exception.code, 2)

    def test_run_with_timeout_is_blocked_without_launch_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            result = runner.main(["--run", "--timeout", "10", "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(report["mode"], "run-request-blocked")
            self.assertFalse(report["game_launch_safe_ready"])
            self.assertTrue(all(
                not item.get("will_execute", False)
                for item in report["plan"]
                if item["step"] in {"ゲーム起動", "GUI入力/クリック"}
            ))


if __name__ == "__main__":
    unittest.main()
