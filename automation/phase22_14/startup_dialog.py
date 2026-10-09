#!/usr/bin/env python3
"""Phase 22.14.1 read-only startup-dialog detector and candidate planner."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

REFERENCE_IMAGE_SIZE = (474, 602)
REFERENCE_DIALOG = {"left": 35, "top": 22, "width": 400, "height": 554}
EXPECTED_WINDOW_SIZE = (400, 554)
WINDOW_SIZE_TOLERANCE = 8
WINDOW_TITLE = "Avernum"
WINDOW_ID_PATTERN = re.compile(r"^(?:\{([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}|([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))$")
GEOMETRY_KEYS = ("x", "y", "width", "height")
RELATIVE_CANDIDATES = (
    {"action": "select_1024x768", "x": 145, "y": 161},
    {"action": "enable_play_in_window", "x": 104, "y": 441},
    {"action": "enable_always_start", "x": 104, "y": 467},
    {"action": "confirm_ok", "x": 347, "y": 529},
)


@dataclass(frozen=True)
class WindowInfo:
    window_id: str
    title: str
    x: int
    y: int
    width: int
    height: int


def normalize_window_id(value: str | None) -> str | None:
    if value is None:
        return None
    match = WINDOW_ID_PATTERN.fullmatch(value.strip())
    return (match.group(1) or match.group(2)).lower() if match else None


def parse_geometry(output: str) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for key in GEOMETRY_KEYS:
        match = re.search(rf"\b{key}\s*:\s*(-?\d+)\b", output, re.IGNORECASE)
        if not match:
            return None
        values[key] = int(match.group(1))
    if values["width"] <= 0 or values["height"] <= 0:
        return None
    return values


def point_inside_window(x: int, y: int, width: int, height: int) -> bool:
    return width > 0 and height > 0 and 0 <= x < width and 0 <= y < height


def relative_to_screen(window_x: int, window_y: int, relative_x: int, relative_y: int) -> tuple[int, int]:
    return window_x + relative_x, window_y + relative_y


def geometry_matches_reference(width: int, height: int, tolerance: int = WINDOW_SIZE_TOLERANCE) -> bool:
    expected_width, expected_height = EXPECTED_WINDOW_SIZE
    return abs(width - expected_width) <= tolerance and abs(height - expected_height) <= tolerance


def _same_window_id(left: str | None, right: str | None) -> bool:
    normalized_left = normalize_window_id(left)
    normalized_right = normalize_window_id(right)
    return normalized_left is not None and normalized_left == normalized_right


def evaluate_candidate(
    windows: Sequence[WindowInfo],
    active_window_id: str | None,
    *,
    wayland_kde: bool,
) -> dict:
    """Assess a candidate; this function never performs input or focus changes."""
    exact = [window for window in windows if window.title == WINDOW_TITLE]
    reasons: list[str] = []
    if not wayland_kde:
        reasons.append("wayland_kde_not_confirmed")
    if len(exact) != 1:
        reasons.append("no_exact_window" if not exact else "multiple_exact_windows")

    target = exact[0] if len(exact) == 1 else None
    id_format_ok = bool(target and normalize_window_id(target.window_id))
    if target and not id_format_ok:
        reasons.append("window_id_format_unverified")
    active_match = bool(target and _same_window_id(target.window_id, active_window_id))
    if target and not active_match:
        reasons.append("active_window_mismatch")

    geometry_match: bool | None = None
    points_inside: bool | None = None
    if target:
        geometry_match = geometry_matches_reference(target.width, target.height)
        if not geometry_match:
            reasons.append("window_geometry_mismatch")
        points_inside = all(
            point_inside_window(point["x"], point["y"], target.width, target.height)
            for point in RELATIVE_CANDIDATES
        )
        if not points_inside:
            reasons.append("candidate_outside_window")

    # The reference is a single screenshot without OCR/visual confirmation.
    # Therefore no candidate is safe to execute, even when metadata matches.
    reasons.append("settings_content_unverified_without_ocr")
    return {
        "status": "blocked",
        "block_reasons": reasons,
        "exact_title_match": len(exact) == 1,
        "exact_window_count": len(exact),
        "window_id_format_ok": id_format_ok,
        "active_window_match": active_match,
        "geometry_matches_reference": geometry_match,
        "candidate_points_inside_window": points_inside,
        "reference_image_size": {"width": REFERENCE_IMAGE_SIZE[0], "height": REFERENCE_IMAGE_SIZE[1]},
        "reference_dialog_geometry": dict(REFERENCE_DIALOG),
        "candidate_points_window_relative": [dict(point) for point in RELATIVE_CANDIDATES],
        "settings_content_verified": False,
        "ocr_status": "unavailable_unverified",
        "operation_plan_approved": False,
        "input_implemented": False,
        "input_attempted": False,
    }


def _run_readonly(command: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def _parse_search_ids(output: str) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in output.splitlines() if line.strip()))


def probe_current_window(
    kdotool: str,
    runner: Callable = _run_readonly,
) -> tuple[list[WindowInfo], str | None, str]:
    """Read exact-title window metadata only; never return it in the public report."""
    try:
        found = runner([kdotool, "search", "--title", "--case-sensitive", "^Avernum$"])
    except (OSError, subprocess.TimeoutExpired):
        return [], None, "kdotool_unavailable"
    if found.returncode not in (0, 1):
        return [], None, "window_query_failed"
    ids = _parse_search_ids(found.stdout or "")
    windows: list[WindowInfo] = []
    for window_id in ids:
        if normalize_window_id(window_id) is None:
            return [], None, "window_id_unverified"
        try:
            title_result = runner([kdotool, "getwindowname", window_id])
            geometry_result = runner([kdotool, "getwindowgeometry", window_id])
        except (OSError, subprocess.TimeoutExpired):
            return [], None, "window_query_failed"
        if title_result.returncode != 0 or geometry_result.returncode != 0:
            return [], None, "window_query_failed"
        geometry = parse_geometry(geometry_result.stdout or "")
        if geometry is None:
            return [], None, "geometry_unverified"
        windows.append(WindowInfo(window_id, title_result.stdout.strip(), **geometry))
    try:
        active_result = runner([kdotool, "getactivewindow"])
    except (OSError, subprocess.TimeoutExpired):
        return windows, None, "active_window_query_failed"
    if active_result.returncode != 0:
        return windows, None, "active_window_query_failed"
    active_lines = [line.strip() for line in (active_result.stdout or "").splitlines() if line.strip()]
    active_id = active_lines[0] if len(active_lines) == 1 and normalize_window_id(active_lines[0]) else None
    if active_id is None:
        return windows, None, "active_window_unverified"
    return windows, active_id, "ok"


def _wayland_kde() -> bool:
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    desktops = {part.lower() for part in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")}
    return session_type == "wayland" and bool(desktops & {"kde", "plasma"})


def make_report(*, runner: Callable = _run_readonly) -> dict:
    kdotool = shutil.which("kdotool")
    if not _wayland_kde():
        windows, active_id, query_status = [], None, "wayland_kde_not_confirmed"
    elif not kdotool:
        windows, active_id, query_status = [], None, "kdotool_missing"
    else:
        windows, active_id, query_status = probe_current_window(kdotool, runner)
    report = evaluate_candidate(windows, active_id, wayland_kde=_wayland_kde())
    if query_status != "ok":
        report["block_reasons"].append(query_status)
    report["kdotool_available"] = kdotool is not None
    report["metadata_query_status"] = query_status
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="既定。読み取り専用で計画を表示")
    mode.add_argument("--plan", action="store_true", help="--dry-runと同じ読み取り専用計画")
    parser.parse_args(argv)
    print(json.dumps(make_report(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
