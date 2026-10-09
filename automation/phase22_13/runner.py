#!/usr/bin/env python3
"""Phase 22.13 safe preflight and dry-run planner; no game or GUI actions."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Mapping

GUI_COMMANDS = (
    "kdotool", "ydotool", "ydotoold", "xdotool", "qdbus6",
    "kscreen-doctor", "spectacle", "grim", "wtype",
)
SAFE_LABEL = re.compile(r"[^A-Za-z0-9._:-]+")
PHASE_DLL = re.compile(r"(?:phase.?22|22[._-]?12[._-]?4).*\.dll$", re.I)


def _label(value: str | None) -> str | None:
    if not value:
        return None
    return SAFE_LABEL.sub("", value)[:100] or None


def _walk_files(root: Path, max_depth: int = 10):
    if not root.is_dir():
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        depth = len(base.relative_to(root).parts)
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".cache", "shadercache", "logs"}]
        if depth >= max_depth:
            dirs[:] = []
        for name in files:
            yield base / name


def _relative(root: Path, path: Path, label: str) -> str:
    rel = path.relative_to(root).as_posix()
    rel = re.sub(r"/users/[^/]+/", "/users/<profile>/", rel)
    return f"{label}/{rel}"


def _process_running(name: str) -> bool:
    """Check only /proc/<pid>/comm; never read process command lines."""
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.glob("[0-9]*/comm"):
        try:
            if entry.read_text(encoding="utf-8").strip() == name:
                return True
        except (OSError, UnicodeError):
            continue
    return False


def inspect_assets(workspace: Path, home: Path) -> dict:
    roots = [
        ("workspace", workspace),
        ("downloads_jp", home / "ダウンロード"),
        ("downloads", home / "Downloads"),
        ("steam", home / ".local/share/Steam"),
    ]
    found: dict[str, list[str]] = {
        "phase22_13_package": [], "phase22_12_4_test_dll": [],
        "phase22_12_4_evidence_archive": [], "isolation_preflight_archive": [],
        "isolated_launcher": [], "game_executable": [], "test_save_files": [],
        "save_directories": [], "save_file_inventory": [], "proton_bundles": [],
    }
    seen: set[tuple[str, str]] = set()
    pilot_archive = "avernum_jp_phase22_13_auto_pilot.zip"
    for label, root in roots:
        if not root.exists():
            continue
        if label in {"downloads_jp", "downloads"}:
            candidate = root / pilot_archive
            folder = root / pilot_archive[:-4]
            if candidate.is_file():
                found["phase22_13_package"].append(f"{label}/{pilot_archive}")
            if folder.is_dir():
                found["phase22_13_package"].append(f"{label}/{folder.name}/")
            for filename, category in (
                ("avernum_jp_phase22_12_4_results.zip", "phase22_12_4_evidence_archive"),
                ("t04_isolation_preflight_v1.zip", "isolation_preflight_archive"),
            ):
                if (root / filename).is_file():
                    found[category].append(f"{label}/{filename}")
        for path in _walk_files(root):
            name = path.name
            low = name.lower()
            category = None
            if path.suffix.lower() == ".dll" and PHASE_DLL.search(name):
                category = "phase22_12_4_test_dll"
            elif "isolat" in low and any(x in low for x in ("launcher", "proton", "pilot")):
                category = "isolated_launcher"
            elif low.startswith("avernum") and low.endswith(".exe"):
                category = "game_executable"
            elif path.suffix.lower() in {".sav", ".save"} and "avernum" in str(path).lower():
                category = "test_save_files"
            if category:
                key = (category, str(path.resolve()))
                if key not in seen:
                    seen.add(key)
                    found[category].append(_relative(root, path, label))
        if label == "steam":
            common = root / "steamapps/common"
            if common.is_dir():
                for p in common.iterdir():
                    proton = p / "proton"
                    if p.is_dir() and "proton" in p.name.lower() and proton.is_file():
                        found["proton_bundles"].append(f"steamapps/common/{p.name}/proton")
            compat = root / "steamapps/compatdata"
            for p in _walk_files(compat, max_depth=14):
                if p.name.lower().endswith((".sav", ".save")) and "avernum" in str(p).lower():
                    item = _relative(root, p, "steam")
                    if item not in found["test_save_files"]:
                        found["test_save_files"].append(item)
            if compat.is_dir():
                for current, dirs, _ in os.walk(compat, followlinks=False):
                    dirs[:] = [d for d in dirs if d not in {".git", ".cache"}]
                    for d in dirs:
                        if "avernum saved games" in d.lower():
                            path = Path(current) / d
                            rel = _relative(root, path, "steam")
                            if rel not in found["save_directories"]:
                                found["save_directories"].append(rel)
                            files = [f for f in path.iterdir() if f.is_file()]
                            found["save_file_inventory"].append({
                                "location": rel,
                                "file_count": len(files),
                                "extensions": sorted({f.suffix.lower() or "<none>" for f in files}),
                                "test_save_verified": False,
                            })
    result = {}
    for key, values in found.items():
        result[key] = sorted(values, key=lambda item: item.get("location", "") if isinstance(item, dict) else item)
    return result


def validate_click_evidence(
    observation: Mapping, focus: Mapping, *, now: float | None = None, max_age: float = 2.0
) -> tuple[bool, str]:
    """Accept only fresh, same-run/frame/window/choice evidence and current focus."""
    required_obs = ("run_id", "frame_id", "window_id", "choice_id", "x", "y", "captured_at", "event_seq", "latest_event_seq")
    required_focus = ("run_id", "frame_id", "window_id", "choice_id", "focused", "captured_at", "latest_event_seq")
    if any(k not in observation for k in required_obs) or any(k not in focus for k in required_focus):
        return False, "evidence_incomplete"
    if not all(observation[k] == focus[k] for k in ("run_id", "frame_id", "window_id", "choice_id", "latest_event_seq")):
        return False, "evidence_context_mismatch"
    if not focus["focused"]:
        return False, "window_not_focused"
    if not isinstance(observation["x"], int) or not isinstance(observation["y"], int) or observation["x"] < 0 or observation["y"] < 0:
        return False, "coordinates_invalid"
    if not isinstance(observation["event_seq"], int) or observation["event_seq"] > observation["latest_event_seq"]:
        return False, "event_not_latest"
    now = time.time() if now is None else now
    for stamp in (observation["captured_at"], focus["captured_at"]):
        if not isinstance(stamp, (int, float)) or stamp > now or now - stamp > max_age:
            return False, "evidence_stale"
    return True, "evidence_current"


def build_report(
    workspace: Path | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    *, process_check: Callable[[str], bool] = _process_running,
    mode: str = "dry-run", timeout: int | None = None,
) -> dict:
    workspace = Path.cwd() if workspace is None else Path(workspace)
    home = Path.home() if home is None else Path(home)
    env = os.environ if environ is None else environ
    commands = {name: which(name) is not None for name in (*GUI_COMMANDS, "steam", "proton", "wine")}
    assets = inspect_assets(workspace, home)
    proton_available = commands["proton"] or bool(assets["proton_bundles"])
    window_control_ready = commands["kdotool"]
    ydotoold_running = process_check("ydotoold")
    ydotool_stack_ready = commands["ydotool"] and commands["ydotoold"] and ydotoold_running
    gui_input_ready = window_control_ready and ydotool_stack_ready
    reasons = []
    if not gui_input_ready:
        reasons.append("KDEウィンドウ制御とydotooldを含むGUI入力スタックが未準備")
    if not proton_available:
        reasons.append("Proton実行環境を確認できない")
    if not assets["phase22_12_4_test_dll"]:
        reasons.append("Phase 22.12.4テストDLLを確認できない")
    if not assets["isolated_launcher"]:
        reasons.append("隔離ランチャーを確認できない")
    if not assets["test_save_files"]:
        reasons.append("テスト用セーブファイルを確認できない")
    if mode == "run":
        reasons.append("実行アダプター未実装:安全条件を満たすゲーム起動/GUI入力は未対応")
    session = _label(env.get("XDG_SESSION_TYPE"))
    desktop = _label(env.get("XDG_CURRENT_DESKTOP"))
    return {
        "schema_version": 1,
        "mode": mode,
        "timeout_seconds": timeout,
        "environment": {
            "xdg_session_type": session,
            "xdg_current_desktop": desktop,
            "wayland_display_present": bool(env.get("WAYLAND_DISPLAY")),
        },
        "commands_available": commands,
        "proton_available": proton_available,
        "window_control_ready": window_control_ready,
        "ydotoold_process_running": ydotoold_running,
        "ydotool_stack_ready": ydotool_stack_ready,
        "gui_input_ready": gui_input_ready,
        "game_launch_safe_ready": bool(proton_available and assets["game_executable"] and assets["isolated_launcher"] and assets["test_save_files"]),
        "assets": assets,
        "plan": [
            {"step": "環境・候補ファイルの存在確認", "will_execute": True, "read_only": True},
            {"step": "ゲーム起動", "will_execute": False},
            {"step": "GUI入力/クリック", "will_execute": False},
            {"step": "日本語表示確認", "status": "未検証"},
            {"step": "クリック成功確認", "status": "未検証"},
        ],
        "click_policy": {
            "requires_same_run_frame_window_choice": True,
            "requires_current_focus_and_coordinates": True,
            "requires_latest_event_sequence_match": True,
            "max_evidence_age_seconds": 2,
            "old_log_coordinates_allowed": False,
        },
        "verification": {"japanese_display": "未検証", "click_success": "未検証"},
        "blocked_reasons": reasons,
        "secrets_or_absolute_paths_recorded": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--dry-run", action="store_true", help="安全な診断と操作計画のみ出力（既定）")
    choice.add_argument("--run", action="store_true", help="将来の実操作用。必ず--timeoutが必要")
    parser.add_argument("--timeout", type=int, help="--run時の必須タイムアウト秒数（1..3600）")
    parser.add_argument("--output", type=Path, default=Path(".relaydeck/phase22_13_dry_run.json"))
    args = parser.parse_args(argv)
    if args.run and args.timeout is None:
        parser.error("--runには--timeout秒数が必須です")
    if args.timeout is not None and (not args.run or not 1 <= args.timeout <= 3600):
        parser.error("--timeoutは--runと併用し、1..3600秒で指定してください")
    mode = "run-request-blocked" if args.run else "dry-run"
    report = build_report(mode=mode, timeout=args.timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"mode={report['mode']} proton={report['proton_available']} gui_input_ready={report['gui_input_ready']} game_launch_safe_ready={report['game_launch_safe_ready']} launch=false input=false")
    print("result_json_written=true")
    print("verification=japanese_display:未検証,click_success:未検証")
    if args.run:
        print("run_status=blocked (安全な実行アダプター未実装)")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
