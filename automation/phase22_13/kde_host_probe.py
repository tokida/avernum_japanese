#!/usr/bin/env python3
"""Safe KDE/Wayland host probe. It never sends GUI input."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Callable, Mapping

STATUS_VALUES = {"ready", "blocked", "unknown"}
GUI_ENVIRONMENT = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
COMMANDS = ("kdotool", "ydotool", "ydotoold")


def _status(value: bool | None) -> str:
    if value is True:
        return "ready"
    if value is False:
        return "blocked"
    return "unknown"


def _session_bus_socket_status(environ: Mapping[str, str]) -> str:
    runtime = environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return "blocked"
    try:
        info = (Path(runtime) / "bus").stat()
        return "ready" if stat.S_ISSOCK(info.st_mode) else "blocked"
    except FileNotFoundError:
        return "blocked"
    except (OSError, ValueError):
        return "unknown"


def _ydotool_socket_status(environ: Mapping[str, str]) -> str:
    configured = environ.get("YDOTOOL_SOCKET")
    runtime = environ.get("XDG_RUNTIME_DIR")
    if configured:
        candidates = [Path(configured)]
    else:
        candidates = [Path("/tmp/.ydotool_socket")]
        if runtime:
            candidates.extend((Path(runtime) / ".ydotool_socket", Path(runtime) / "ydotool_socket"))
    saw_error = False
    for candidate in candidates:
        try:
            if stat.S_ISSOCK(candidate.stat().st_mode):
                return "ready"
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            saw_error = True
    return "unknown" if saw_error else "blocked"


def _daemon_process_status(proc_root: Path = Path("/proc")) -> str:
    if not proc_root.is_dir():
        return "unknown"
    saw_error = False
    try:
        entries = list(proc_root.glob("[0-9]*/comm"))
    except OSError:
        return "unknown"
    for entry in entries:
        try:
            if entry.read_text(encoding="utf-8").strip() == "ydotoold":
                return "ready"
        except (OSError, UnicodeError):
            saw_error = True
    return "unknown" if saw_error else "blocked"


def _uinput_access_status(path: Path = Path("/dev/uinput")) -> str:
    try:
        if not path.exists():
            return "blocked"
        return "ready" if os.access(path, os.W_OK) else "blocked"
    except OSError:
        return "unknown"


def _kdotool_status(
    executable: str | None,
    command_runner: Callable = subprocess.run,
    timeout: float = 3.0,
) -> str:
    if not executable:
        return "blocked"
    try:
        result = command_runner(
            [executable, "getactivewindow"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return "ready" if result.returncode == 0 else "blocked"
    except subprocess.TimeoutExpired:
        return "unknown"
    except FileNotFoundError:
        return "blocked"
    except Exception:
        return "unknown"


def _combine(statuses: list[str]) -> str:
    if any(value not in STATUS_VALUES for value in statuses):
        return "unknown"
    if "blocked" in statuses:
        return "blocked"
    if "unknown" in statuses:
        return "unknown"
    return "ready"


def collect_report(
    environ: Mapping[str, str] | None = None,
    command_lookup: Callable[[str], str | None] = shutil.which,
    command_runner: Callable = subprocess.run,
    daemon_checker: Callable[[], str] = _daemon_process_status,
    socket_checker: Callable[[Mapping[str, str]], str] = _ydotool_socket_status,
    uinput_checker: Callable[[], str] = _uinput_access_status,
    bus_checker: Callable[[Mapping[str, str]], str] = _session_bus_socket_status,
) -> dict:
    env = os.environ if environ is None else environ
    try:
        values = {name: env.get(name) for name in GUI_ENVIRONMENT}
        gui_presence = {name: bool(values[name]) for name in GUI_ENVIRONMENT}
        session_wayland = None if "XDG_SESSION_TYPE" not in env else env.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        current_desktop = env.get("XDG_CURRENT_DESKTOP")
        desktop_kde = None if current_desktop is None else any(
            part.lower() in {"kde", "plasma"} for part in current_desktop.split(":")
        )
        runtime = values["XDG_RUNTIME_DIR"]
        runtime_exists = None
        runtime_accessible = None
        if runtime:
            try:
                runtime_exists = Path(runtime).is_dir()
                runtime_accessible = runtime_exists and os.access(runtime, os.R_OK | os.X_OK)
            except (OSError, ValueError):
                runtime_exists = False
                runtime_accessible = False
        commands = {name: command_lookup(name) is not None for name in COMMANDS}
        kdotool_status = _kdotool_status(command_lookup("kdotool"), command_runner)
        daemon_status = daemon_checker()
        socket_status = socket_checker(env)
        uinput_status = uinput_checker()
        bus_status = bus_checker(env)
        session_status = _combine([
            _status(session_wayland),
            _status(desktop_kde),
            _status(gui_presence["WAYLAND_DISPLAY"]),
            _status(gui_presence["XDG_RUNTIME_DIR"]),
            _status(gui_presence["DBUS_SESSION_BUS_ADDRESS"]),
            _status(runtime_exists),
            _status(runtime_accessible),
            bus_status,
            kdotool_status,
        ])
        input_preconditions = _combine([
            _status(commands["ydotool"]),
            _status(commands["ydotoold"]),
            daemon_status,
            socket_status,
            uinput_status,
        ])
        return {
            "schema_version": 1,
            "probe_status": _combine([session_status, input_preconditions]),
            "session": {
                "wayland": session_wayland,
                "desktop_is_kde": desktop_kde,
                "gui_environment_present": gui_presence,
                "runtime_directory_exists": runtime_exists,
                "runtime_directory_accessible": runtime_accessible,
                "session_bus_socket_status": bus_status,
            },
            "commands_available": commands,
            "kde_connection_status": kdotool_status,
            "ydotoold_process_status": daemon_status,
            "ydotool_socket_status": socket_status,
            "uinput_access_status": uinput_status,
            "ydotool_input_preconditions": input_preconditions,
            "actual_input_status": "unknown",
            "input_attempted": False,
            "environment_values_recorded": False,
            "user_or_absolute_paths_recorded": False,
        }
    except Exception:
        return {
            "schema_version": 1,
            "probe_status": "unknown",
            "error": "probe_internal_error",
            "actual_input_status": "unknown",
            "input_attempted": False,
            "environment_values_recorded": False,
            "user_or_absolute_paths_recorded": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".relaydeck/phase22_13_kde_host_probe.json"),
        help="JSON出力先（既定: .relaydeck/phase22_13_kde_host_probe.json）",
    )
    args = parser.parse_args(argv)
    report = collect_report()
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        print("probe_status=unknown; json_write=failed")
        return 2
    print(f"probe_status={report['probe_status']}; actual_input=unknown; json_write=success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
