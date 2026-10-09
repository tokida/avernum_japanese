#!/usr/bin/env python3
"""Phase 22.14 private, click-free boot-window capture runner.

Default mode is a no-side-effect dry run. --run starts the existing Phase 22.13
detect-only pilot and captures only a uniquely identified active Avernum window.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, Mapping

PILOT_DIR = Path.home() / "ダウンロード" / "avernum_jp_phase22_13_auto_pilot"
PILOT_SCRIPT = PILOT_DIR / "run_auto.sh"
PILOT_SOURCE = PILOT_DIR / "auto_pilot.py"
PILOT_RESULT = Path.home() / "Downloads" / "avernum_jp_phase22_13_results.zip"
PRIVATE_ROOT = Path.home() / ".cache" / "avernum-jp-smoke" / "phase22_14"
CAPTURE_SECONDS = (2, 8, 20)
PILOT_TIMEOUT = 75
PILOT_EXIT_GRACE = 125
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUIRED_SPECTACLE_OPTIONS = ("--activewindow", "--background", "--nonotify", "--output")
GAME_TITLE = re.compile(r"\bAvernum\b", re.IGNORECASE)
PILOT_RESULT_ENUM = {
    "PREFLIGHT_DONE", "MISSING_TOOLS", "WINDOW_NOT_DETECTED",
    "WINDOW_DETECTED_SCENE_NOT_REACHED", "SCENE_DETECTED_NO_CLICK",
    "CLICK_SENT_VERIFICATION_PENDING", "ERROR",
}


def _desktop_is_kde(value: str | None) -> bool | None:
    if value is None:
        return None
    return any(part.lower() in {"kde", "plasma"} for part in value.split(":"))


def _process_running(proc_root: Path = Path("/proc")) -> bool | None:
    """Read comm only; return None when /proc could not be checked reliably."""
    if not proc_root.is_dir():
        return None
    saw_error = False
    try:
        entries = list(proc_root.glob("[0-9]*/comm"))
    except OSError:
        return None
    for entry in entries:
        try:
            if entry.read_text(encoding="utf-8").strip().casefold() in {"avernum.exe", "avernum"}:
                return True
        except (OSError, UnicodeError):
            saw_error = True
    return None if saw_error else False


def detect_only_contract_safe(source: str) -> bool:
    """Verify existing pilot guards click and focus activation behind opt-in."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, TypeError):
        return False
    main = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main is None:
        return False
    detect_flag = False
    default_click_enabled = False
    for node in ast.walk(main):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument" and node.args:
            if isinstance(node.args[0], ast.Constant) and node.args[0].value == "--detect-only":
                kws = {kw.arg: kw.value for kw in node.keywords}
                detect_flag = (
                    isinstance(kws.get("dest"), ast.Constant) and kws["dest"].value == "click_choice"
                    and isinstance(kws.get("action"), ast.Constant) and kws["action"].value == "store_false"
                )
        if isinstance(node.func, ast.Attribute) and node.func.attr == "set_defaults":
            kws = {kw.arg: kw.value for kw in node.keywords}
            default_click_enabled = (
                isinstance(kws.get("click_choice"), ast.Constant) and kws["click_choice"].value is True
            )
    guarded_nodes = []
    for node in ast.walk(main):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Attribute):
            if node.test.attr == "click_choice" and isinstance(node.test.value, ast.Name) and node.test.value.id == "args":
                guarded_nodes.extend(node.body)
    guarded_calls = {id(n) for body in guarded_nodes for n in ast.walk(body) if isinstance(n, ast.Call)}
    action_calls = []
    for node in ast.walk(main):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in {"click_ydotool", "ensure_active"}:
                action_calls.append(node)
    actions_guarded = bool(action_calls) and all(id(n) in guarded_calls for n in action_calls)
    activation_only_in_helper = True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "cmd":
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "windowactivate":
            helper = next((f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "ensure_active" and node in ast.walk(f)), None)
            if helper is None:
                activation_only_in_helper = False
    return detect_flag and default_click_enabled and actions_guarded and activation_only_in_helper


def make_report(mode: str = "dry-run") -> dict:
    """Construct a status-only report without paths, titles, IDs, or env values."""
    env = os.environ
    return {
        "schema_version": 1,
        "mode": mode,
        "status": "dry_run",
        "session": {
            "wayland": None if "XDG_SESSION_TYPE" not in env else env.get("XDG_SESSION_TYPE", "").lower() == "wayland",
            "desktop_is_kde": _desktop_is_kde(env.get("XDG_CURRENT_DESKTOP")),
            "wayland_display_present": bool(env.get("WAYLAND_DISPLAY")),
        },
        "commands_available": {name: shutil.which(name) is not None for name in ("bash", "kdotool", "spectacle")},
        "pilot_files_present": {
            "directory": PILOT_DIR.is_dir(), "runner": PILOT_SCRIPT.is_file(), "source": PILOT_SOURCE.is_file()
        },
        "detect_only_contract_safe": None,
        "existing_game_process": "not_checked_in_dry_run",
        "existing_game_window": "not_checked_in_dry_run",
        "spectacle_cli_verified": None,
        "backup_status": "not_attempted",
        "pilot_started": False,
        "pilot_exit_confirmed": False,
        "screenshots": [],
        "result_zip_snapshot": "not_attempted",
        "andrew_screen": "unverified",
        "japanese_title_screen": "unverified",
        "input_attempted": False,
        "focus_change_status": "not_requested",
    }


def parse_window_candidates(search_output: str, title_by_id: Mapping[str, str]) -> tuple[str, str | None]:
    """Return status and internal ID; IDs/titles must never enter reports."""
    ids = list(dict.fromkeys(line.strip() for line in search_output.splitlines() if line.strip()))
    if not ids:
        return "none", None
    if len(title_by_id) != len(ids):
        return "unknown", None
    matches = [wid for wid in ids if GAME_TITLE.search(title_by_id[wid])]
    if len(matches) == 1:
        return "unique", matches[0]
    return ("ambiguous", None) if len(matches) > 1 else ("none", None)


def screenshot_eligible(window_status: str, target_id: str | None, active_id: str | None) -> bool:
    return window_status == "unique" and bool(target_id) and target_id == active_id


def valid_png(data: bytes) -> bool:
    if not data.startswith(PNG_SIGNATURE):
        return False
    offset = len(PNG_SIGNATURE)
    saw_header = False
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(data):
            return False
        if kind == b"IHDR":
            if saw_header or size != 13:
                return False
            saw_header = True
        if kind == b"IEND":
            return saw_header and size == 0 and end == len(data)
        offset = end
    return False


def spectacle_options_supported(help_text: str, exit_code: int) -> bool:
    return exit_code == 0 and all(option in help_text for option in REQUIRED_SPECTACLE_OPTIONS)


def _run_capture(command: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def _query_windows(kdotool: str, runner: Callable = _run_capture) -> tuple[str, str | None]:
    try:
        found = runner([kdotool, "search", "--name", "Avernum"], timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return "unknown", None
    if found.returncode != 0:
        return "unknown", None
    ids = list(dict.fromkeys(line.strip() for line in found.stdout.splitlines() if line.strip()))
    if not ids:
        return "none", None
    titles: dict[str, str] = {}
    for wid in ids:
        try:
            result = runner([kdotool, "getwindowname", wid], timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            return "unknown", None
        if result.returncode != 0:
            return "unknown", None
        titles[wid] = result.stdout.strip()
    return parse_window_candidates("\n".join(ids), titles)


def _active_window(kdotool: str, runner: Callable = _run_capture) -> str | None:
    try:
        result = runner([kdotool, "getactivewindow"], timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if len(lines) == 1 else None


def capture_active_window(
    kdotool: str,
    spectacle: str,
    target_id: str,
    destination: Path,
    *,
    activate: bool = False,
    runner: Callable = _run_capture,
    window_query: Callable = _query_windows,
    active_query: Callable = _active_window,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Capture one verified active window; window IDs/titles stay in memory."""
    status, current_id = window_query(kdotool, runner)
    if status != "unique" or current_id != target_id:
        return "window_not_unique"
    active_id = active_query(kdotool, runner)
    if active_id != target_id and activate:
        try:
            activated = runner([kdotool, "windowactivate", target_id], timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            return "activation_failed"
        if activated.returncode != 0:
            return "activation_failed"
        sleeper(0.25)
        status, current_id = window_query(kdotool, runner)
        active_id = active_query(kdotool, runner)
        if status != "unique" or current_id != target_id or active_id != target_id:
            return "focus_verification_failed"
    elif active_id != target_id:
        return "inactive_window"
    if destination.exists():
        return "output_collision"
    pending = destination.with_name(destination.stem + ".pending" + destination.suffix)
    if pending.exists():
        return "output_collision"
    try:
        shot = runner([spectacle, "--activewindow", "--background", "--nonotify", "--output", str(pending)], timeout=15)
    except subprocess.TimeoutExpired:
        return "capture_timeout"
    except OSError:
        return "spectacle_unavailable"
    if shot.returncode != 0 or not pending.is_file():
        return "capture_failed"
    try:
        data = pending.read_bytes()
        status_after, id_after = window_query(kdotool, runner)
        active_after = active_query(kdotool, runner)
        if not valid_png(data):
            pending.unlink(missing_ok=True)
            return "invalid_image"
        if status_after != "unique" or id_after != target_id or active_after != target_id:
            pending.unlink(missing_ok=True)
            return "focus_changed_during_capture"
        os.chmod(pending, 0o600)
        pending.rename(destination)
        return "captured"
    except OSError:
        pending.unlink(missing_ok=True)
        return "capture_verification_failed"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


def _private_run_dir() -> Path:
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    for _ in range(10):
        name = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(4).hex()}"
        candidate = PRIVATE_ROOT / name
        try:
            candidate.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise OSError("private_output_collision")
    mode = stat.S_IMODE(candidate.stat().st_mode)
    if mode & 0o077:
        raise PermissionError("private_output_permissions")
    if hasattr(os, "getuid") and candidate.stat().st_uid != os.getuid():
        raise PermissionError("private_output_owner")
    return candidate


def _event(log, event: str, elapsed: float | None = None) -> None:
    record = {"event": event}
    if elapsed is not None:
        record["elapsed_seconds"] = round(elapsed, 2)
    log.write(json.dumps(record, separators=(",", ":")) + "\n")
    log.flush()


def _write_report(run_dir: Path, report: dict) -> None:
    path = run_dir / "report.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)


def _finish_report(run_dir: Path, report: dict, code: int) -> tuple[int, dict]:
    report["capture_count"] = sum(s.get("status") == "captured" for s in report.get("screenshots", []))
    try:
        _write_report(run_dir, report)
    except OSError:
        report.update(status="failed", error="report_write_failed")
        return 2, report
    return code, report


def _classify_spectacle(spectacle: str, runner: Callable = _run_capture) -> bool:
    try:
        result = runner([spectacle, "--help"], timeout=8)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return spectacle_options_supported(result.stdout + "\n" + result.stderr, result.returncode)


def _copy_zip_snapshot(source: Path, target: Path) -> bool:
    if not source.is_file() or target.exists() or not _valid_zip(source):
        return False
    before = _sha256(source)
    try:
        shutil.copy2(source, target)
        os.chmod(target, 0o600)
        return _sha256(target) == before and _valid_zip(target)
    except OSError:
        return False


def run_phase(*, activate_game: bool = False) -> tuple[int, dict]:
    report = make_report("run")
    try:
        run_dir = _private_run_dir()
    except OSError:
        report.update(status="blocked", error="private_output_unavailable")
        return 2, report
    log_path = run_dir / "events.jsonl"
    try:
        log = log_path.open("x", encoding="utf-8")
        os.chmod(log_path, 0o600)
    except OSError:
        report.update(status="blocked", error="private_log_unavailable")
        return 2, report
    started = time.monotonic()
    child: subprocess.Popen | None = None
    pilot_started: float | None = None
    try:
        env = os.environ
        report["session"] = {
            "wayland": env.get("XDG_SESSION_TYPE", "").lower() == "wayland",
            "desktop_is_kde": _desktop_is_kde(env.get("XDG_CURRENT_DESKTOP")),
            "wayland_display_present": bool(env.get("WAYLAND_DISPLAY")),
        }
        tools = {name: shutil.which(name) for name in ("bash", "kdotool", "spectacle")}
        report["commands_available"] = {name: value is not None for name, value in tools.items()}
        report["pilot_files_present"] = {"directory": PILOT_DIR.is_dir(), "runner": PILOT_SCRIPT.is_file(), "source": PILOT_SOURCE.is_file()}
        try:
            source = PILOT_SOURCE.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            source = ""
        safe_detect = detect_only_contract_safe(source)
        report["detect_only_contract_safe"] = safe_detect
        proc = _process_running()
        report["existing_game_process"] = "running" if proc is True else "absent" if proc is False else "unknown"
        conditions = [
            report["session"]["wayland"] is True,
            report["session"]["desktop_is_kde"] is True,
            report["session"]["wayland_display_present"] is True,
            all(report["commands_available"].values()),
            all(report["pilot_files_present"].values()),
            safe_detect,
            proc is False,
        ]
        if not all(conditions):
            report.update(status="blocked", error="preflight_conditions_not_met")
            _event(log, "preflight_blocked")
            return _finish_report(run_dir, report, 2)
        assert tools["kdotool"] and tools["spectacle"] and tools["bash"]
        window_status, _ = _query_windows(tools["kdotool"])
        report["existing_game_window"] = window_status
        if window_status != "none":
            report.update(status="blocked", error="existing_or_unknown_game_window")
            _event(log, "prelaunch_window_guard_blocked")
            return _finish_report(run_dir, report, 2)
        options_ok = _classify_spectacle(tools["spectacle"])
        report["spectacle_cli_verified"] = options_ok
        if not options_ok:
            report.update(status="blocked", error="spectacle_cli_unverified")
            _event(log, "spectacle_options_unverified")
            return _finish_report(run_dir, report, 2)
        backup = run_dir / "preexisting_phase22_13_results_backup.zip"
        if PILOT_RESULT.exists():
            report["backup_status"] = "verified" if _copy_zip_snapshot(PILOT_RESULT, backup) else "failed"
            if report["backup_status"] != "verified":
                report.update(status="blocked", error="result_backup_failed")
                _event(log, "result_backup_failed")
                return _finish_report(run_dir, report, 2)
        else:
            report["backup_status"] = "not_needed"
        before_hash = _sha256(PILOT_RESULT) if PILOT_RESULT.is_file() else None
        started_wall = time.time()
        pilot_log = run_dir / "pilot_console.log"
        with pilot_log.open("x", encoding="utf-8") as output:
            os.chmod(pilot_log, 0o600)
            try:
                child = subprocess.Popen(
                    [tools["bash"], str(PILOT_SCRIPT), "--detect-only", "--timeout", str(PILOT_TIMEOUT)],
                    cwd=PILOT_DIR,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
            except OSError:
                report.update(status="blocked", error="pilot_start_failed")
                _event(log, "pilot_start_failed")
                return _finish_report(run_dir, report, 2)
            pilot_started = time.monotonic()
            report["pilot_started"] = True
            _event(log, "pilot_started")
            capture_index = 0
            exit_code: int | None = None
            while time.monotonic() - pilot_started < PILOT_EXIT_GRACE:
                elapsed = time.monotonic() - pilot_started
                polled = child.poll()
                if polled is not None:
                    exit_code = polled
                    break
                if capture_index < len(CAPTURE_SECONDS) and elapsed >= CAPTURE_SECONDS[capture_index]:
                    scheduled = CAPTURE_SECONDS[capture_index]
                    window_status, target_id = _query_windows(tools["kdotool"])
                    if window_status != "unique" or not target_id:
                        capture_status = "window_not_unique" if window_status in {"ambiguous", "unknown"} else "window_not_found"
                    else:
                        active_id = _active_window(tools["kdotool"])
                        if active_id != target_id and not activate_game:
                            capture_status = "inactive_window"
                        else:
                            if active_id != target_id and activate_game:
                                report["focus_change_status"] = "requested"
                            capture_status = capture_active_window(
                                tools["kdotool"], tools["spectacle"], target_id,
                                run_dir / f"capture_{scheduled:02d}s.png", activate=activate_game,
                            )
                            if capture_status == "captured" and active_id != target_id:
                                report["focus_change_status"] = "target_verified"
                            elif active_id != target_id and activate_game:
                                report["focus_change_status"] = "attempted_unverified"
                    report["screenshots"].append({"scheduled_seconds": scheduled, "status": capture_status})
                    _event(log, "capture_" + capture_status, elapsed)
                    capture_index += 1
                time.sleep(0.1)
            if exit_code is None:
                exit_code = child.poll()
            report["pilot_exit_confirmed"] = exit_code is not None
            report["pilot_exit_code"] = exit_code if exit_code is not None else "unconfirmed"
            if exit_code is None:
                report.update(status="partial", error="pilot_exit_unconfirmed")
                _event(log, "pilot_exit_unconfirmed")
            else:
                _event(log, "pilot_exited", time.monotonic() - started)
                report["status"] = "completed" if exit_code == 0 else "failed"
            if report["pilot_exit_confirmed"] and PILOT_RESULT.is_file():
                fresh = PILOT_RESULT.stat().st_mtime_ns > int(started_wall * 1_000_000_000)
                changed = before_hash is None or _sha256(PILOT_RESULT) != before_hash
                if fresh and changed and _valid_zip(PILOT_RESULT):
                    snapshot = run_dir / "phase22_13_results_snapshot.zip"
                    report["result_zip_snapshot"] = "verified" if _copy_zip_snapshot(PILOT_RESULT, snapshot) else "failed"
                else:
                    report["result_zip_snapshot"] = "not_fresh"
                try:
                    with zipfile.ZipFile(PILOT_RESULT) as archive:
                        if "auto_report.json" in archive.namelist():
                            pilot_report = json.loads(archive.read("auto_report.json"))
                            report["andrew_screen"] = "detected" if pilot_report.get("observed_ui") else "not_detected"
                            report["input_attempted"] = pilot_report.get("input_attempted") is True
                            result = pilot_report.get("automation_result")
                            report["pilot_automation_result"] = result if result in PILOT_RESULT_ENUM else "unknown"
                except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
                    report["result_zip_snapshot"] = "unreadable"
            post_proc = _process_running()
            report["game_process_after"] = "running" if post_proc is True else "absent" if post_proc is False else "unknown"
            report["capture_count"] = sum(s["status"] == "captured" for s in report["screenshots"])
            if report["status"] == "completed" and report["capture_count"] == 0:
                report["status"] = "partial"
            # A clean pilot exit without a verified PNG is still an incomplete
            # capture run and must not look like a successful command.
            return _finish_report(run_dir, report, 0 if report["status"] == "completed" else 2)
    except Exception:
        report.update(status="failed", error="runtime_internal_error")
        _event(log, "runtime_internal_error")
        if child is not None and child.poll() is None:
            remaining = PILOT_EXIT_GRACE
            if pilot_started is not None:
                remaining = max(0.0, PILOT_EXIT_GRACE - (time.monotonic() - pilot_started))
            try:
                report["pilot_exit_code"] = child.wait(timeout=remaining)
                report["pilot_exit_confirmed"] = True
            except subprocess.TimeoutExpired:
                report["pilot_exit_code"] = "unconfirmed"
                report["pilot_exit_confirmed"] = False
        try:
            post_proc = _process_running()
            report["game_process_after"] = "running" if post_proc is True else "absent" if post_proc is False else "unknown"
            _finish_report(run_dir, report, 2)
        except OSError:
            pass
        return 2, report
    finally:
        log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="既定。起動・撮影・フォーカス変更なし")
    mode.add_argument("--run", action="store_true", help="明示的に隔離detect-only起動と撮影を実施")
    parser.add_argument("--activate-game", action="store_true", help="一意に検証したAvernumだけを前面化")
    args = parser.parse_args(argv)
    if args.activate_game and not args.run:
        parser.error("--activate-gameは--runと併用してください")
    if not args.run:
        print(json.dumps(make_report(), ensure_ascii=False, indent=2))
        return 0
    code, report = run_phase(activate_game=args.activate_game)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
