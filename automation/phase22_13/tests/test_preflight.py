import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "preflight.py"
spec = importlib.util.spec_from_file_location("preflight", MODULE_PATH)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

class PreflightTests(unittest.TestCase):
    def test_report_has_only_safe_diagnostics(self):
        env = {
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "KDE",
            "WAYLAND_DISPLAY": "wayland-0",
            "HOME": "/secret/home",
            "GH_TOKEN": "secret-token",
        }
        report = preflight.collect_report(env, which=lambda name: "/bin/" + name if name in ("python3", "git") else None)
        self.assertEqual(report["xdg_session_type"], "wayland")
        self.assertEqual(report["xdg_current_desktop"], "KDE")
        self.assertTrue(report["wayland_display_present"])
        self.assertEqual(report["required_commands"], {"python3": True, "git": True, "kdotool": False, "ydotool": False})
        encoded = json.dumps(report)
        self.assertNotIn("/secret/home", encoded)
        self.assertNotIn("secret-token", encoded)

    def test_cli_writes_requested_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "diagnostic.json"
            self.assertEqual(preflight.main(["--output", str(output)]), 0)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"xdg_session_type", "xdg_current_desktop", "required_commands", "wayland_display_present"})

if __name__ == "__main__":
    unittest.main()
