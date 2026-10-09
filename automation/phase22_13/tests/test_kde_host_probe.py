import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

MODULE_PATH = Path(__file__).parents[1] / "kde_host_probe.py"
spec = importlib.util.spec_from_file_location("kde_host_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class KDEHostProbeTests(unittest.TestCase):
    def environment(self):
        return {
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "KDE",
            "DISPLAY": ":0-secret-display",
            "WAYLAND_DISPLAY": "wayland-0-secret",
            "XDG_RUNTIME_DIR": "/isolated/runtime-secret",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private/session-bus",
            "YDOTOOL_SOCKET": "/private/ydotool-socket",
        }

    def ready_mocks(self):
        return {
            "command_lookup": Mock(side_effect=lambda name: "/private/bin/" + name),
            "command_runner": Mock(return_value=SimpleNamespace(returncode=0, stdout="hidden-window-id", stderr="hidden-title")),
            "daemon_checker": Mock(return_value="ready"),
            "socket_checker": Mock(return_value="ready"),
            "uinput_checker": Mock(return_value="ready"),
            "bus_checker": Mock(return_value="ready"),
        }

    def test_report_contains_presence_only_and_does_not_leak_values(self):
        report = probe.collect_report(self.environment(), **self.ready_mocks())
        encoded = json.dumps(report)
        for secret in ("private", "secret-display", "wayland-0-secret", "hidden-window-id", "hidden-title", "runtime-secret"):
            self.assertNotIn(secret, encoded)
        self.assertTrue(report["session"]["wayland"])
        self.assertTrue(report["session"]["desktop_is_kde"])
        self.assertTrue(report["input_attempted"] is False)

    def test_kdotool_failure_is_recorded_without_stderr(self):
        mocks = self.ready_mocks()
        mocks["command_runner"] = Mock(return_value=SimpleNamespace(returncode=1, stdout="window id", stderr="D-Bus private error"))
        report = probe.collect_report(self.environment(), **mocks)
        self.assertEqual(report["kde_connection_status"], "blocked")
        self.assertNotIn("private error", json.dumps(report))

    def test_kdotool_timeout_is_unknown(self):
        mocks = self.ready_mocks()
        mocks["command_runner"] = Mock(side_effect=subprocess.TimeoutExpired("kdotool", 3))
        report = probe.collect_report(self.environment(), **mocks)
        self.assertEqual(report["kde_connection_status"], "unknown")
        self.assertEqual(report["actual_input_status"], "unknown")

    def test_missing_daemon_socket_or_device_blocks_input_preconditions(self):
        mocks = self.ready_mocks()
        mocks["daemon_checker"].return_value = "blocked"
        mocks["socket_checker"].return_value = "blocked"
        mocks["uinput_checker"].return_value = "blocked"
        report = probe.collect_report(self.environment(), **mocks)
        self.assertEqual(report["ydotool_input_preconditions"], "blocked")
        self.assertFalse(report["input_attempted"])

    def test_main_writes_report_without_echoing_output_path(self):
        report = {
            "schema_version": 1,
            "probe_status": "unknown",
            "actual_input_status": "unknown",
            "input_attempted": False,
            "environment_values_recorded": False,
            "user_or_absolute_paths_recorded": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "probe.json"
            with unittest.mock.patch.object(probe, "collect_report", return_value=report):
                self.assertEqual(probe.main(["--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)


if __name__ == "__main__":
    unittest.main()
