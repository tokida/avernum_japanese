#!/usr/bin/env python3
"""Safe, read-only KDE/Wayland automation preflight; never launches the game."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Mapping

REQUIRED_COMMANDS = ("python3", "git", "kdotool", "ydotool")
_SAFE_VALUE = re.compile(r"[^A-Za-z0-9._:-]+")

def safe_label(value: str | None) -> str | None:
    """Keep environment labels, excluding paths or other unexpected content."""
    if not value:
        return None
    value = _SAFE_VALUE.sub("", value)[:120]
    return value or None

def collect_report(
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict:
    env = os.environ if environ is None else environ
    return {
        "xdg_session_type": safe_label(env.get("XDG_SESSION_TYPE")),
        "xdg_current_desktop": safe_label(env.get("XDG_CURRENT_DESKTOP")),
        "required_commands": {name: which(name) is not None for name in REQUIRED_COMMANDS},
        "wayland_display_present": bool(env.get("WAYLAND_DISPLAY")),
    }

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".relaydeck/phase22_13_preflight.json"),
        help="JSON output path (default: .relaydeck/phase22_13_preflight.json)",
    )
    args = parser.parse_args(argv)
    report = collect_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
