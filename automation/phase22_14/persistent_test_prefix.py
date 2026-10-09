#!/usr/bin/env python3
"""Prepare and optionally run the Phase 22.13 pilot with a private persistent prefix."""
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
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

SOURCE_PACKAGE = Path.home() / "ダウンロード" / "avernum_jp_phase22_13_auto_pilot"
PRIVATE_ROOT = Path.home() / ".cache" / "avernum-jp-smoke" / "phase22_14"
PRIVATE_PILOT = PRIVATE_ROOT / "persistent_pilot"
PRIVATE_COMPAT = PRIVATE_ROOT / "persistent_compatdata"
MANIFEST_NAME = "MANIFEST_SHA256.txt"
PREPARED_NAME = "prepared_manifest.json"
RUNNER_RELATIVE = Path("base/run_proton_isolated.sh")
PILOT_RELATIVE = Path("auto_pilot.py")
AUTO_RUN_RELATIVE = Path("run_auto.sh")
RESULTS_ZIP = Path.home() / "Downloads" / "avernum_jp_phase22_13_results.zip"
EXPECTED_EXE_SHA256 = "76d23df5f57ab68a4d8fcf7594ad7f2b3a59481c5b7a451b21c3d775c73d89e8"
EXPECTED_DLL_SHA256 = "4832f13fc973401319b37cfabd4049942c84adf902ec20ea68c5bffc89f323f1"
OLD_COMPAT_LINE = 'compat="$stage/compatdata"'
NEW_COMPAT_LINE = 'compat="${AVERNUM_JP_TEST_COMPAT_PATH:-$stage/compatdata}"'
MANIFEST_LINE = re.compile(r"^([0-9a-fA-F]{64})  (.+)$")
RUN_WAIT_SECONDS = 180


class SafetyError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_manifest(root: Path) -> dict[str, str]:
    manifest = root / MANIFEST_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise SafetyError("manifest_missing")
    records: dict[str, str] = {}
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise SafetyError("manifest_unreadable") from None
    for line in lines:
        match = MANIFEST_LINE.fullmatch(line)
        if not match:
            raise SafetyError("manifest_format_invalid")
        digest, name = match.groups()
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or name in records:
            raise SafetyError("manifest_path_invalid")
        records[name] = digest.lower()
    if not records:
        raise SafetyError("manifest_empty")
    return records


def _regular_files_without_symlinks(root: Path) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        raise SafetyError("directory_invalid")
    found: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise SafetyError("symlink_rejected")
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise SafetyError("nonregular_file_rejected")
            found.add(path.relative_to(root).as_posix())
    return found


def _verify_manifest_files(root: Path, records: dict[str, str], *, prepared: bool) -> None:
    actual = _regular_files_without_symlinks(root)
    expected_files = set(records)
    extras = {MANIFEST_NAME}
    if prepared:
        extras.add(PREPARED_NAME)
    if actual != expected_files | extras:
        raise SafetyError("package_file_set_mismatch")
    for name, expected in records.items():
        path = root / PurePosixPath(name)
        if not path.is_file():
            raise SafetyError("manifest_file_missing")
        actual_sha = sha256_file(path)
        if prepared and name == RUNNER_RELATIVE.as_posix():
            continue
        if actual_sha != expected:
            raise SafetyError("manifest_hash_mismatch")


def detect_only_click_guard(source: str) -> bool:
    """Require the pilot's click and focus-changing calls to sit behind opt-in."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, TypeError):
        return False
    main = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"), None)
    if main is None:
        return False
    has_flag = False
    default_click_enabled = False
    for node in ast.walk(main):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "add_argument" and node.args:
            if isinstance(node.args[0], ast.Constant) and node.args[0].value == "--detect-only":
                keywords = {item.arg: item.value for item in node.keywords}
                has_flag = (
                    isinstance(keywords.get("dest"), ast.Constant)
                    and keywords["dest"].value == "click_choice"
                    and isinstance(keywords.get("action"), ast.Constant)
                    and keywords["action"].value == "store_false"
                )
        if node.func.attr == "set_defaults":
            keywords = {item.arg: item.value for item in node.keywords}
            default_click_enabled = (
                isinstance(keywords.get("click_choice"), ast.Constant)
                and keywords["click_choice"].value is True
            )

    guarded_calls: set[int] = set()
    for node in ast.walk(main):
        test = node.test if isinstance(node, ast.If) else None
        if (
            isinstance(test, ast.Attribute)
            and test.attr == "click_choice"
            and isinstance(test.value, ast.Name)
            and test.value.id == "args"
        ):
            guarded_calls.update(id(child) for child in ast.walk(node) if isinstance(child, ast.Call))

    actions: list[ast.Call] = []
    for node in ast.walk(main):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
        if name in {"click_ydotool", "ensure_active"}:
            actions.append(node)
    activation_guarded = all(id(action) in guarded_calls for action in actions)
    activation_calls_ok = True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "cmd":
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "windowactivate":
            helper = next(
                (function for function in ast.walk(tree)
                 if isinstance(function, ast.FunctionDef) and function.name == "ensure_active" and node in ast.walk(function)),
                None,
            )
            if helper is None:
                activation_calls_ok = False
    return has_flag and default_click_enabled and bool(actions) and activation_guarded and activation_calls_ok


def patch_runner_text(source: str) -> str:
    old_count = source.count(OLD_COMPAT_LINE)
    new_count = source.count(NEW_COMPAT_LINE)
    if old_count != 1 or new_count != 0:
        raise SafetyError("runner_patch_cardinality_invalid")
    patched = source.replace(OLD_COMPAT_LINE, NEW_COMPAT_LINE, 1)
    if patched.count(NEW_COMPAT_LINE) != 1 or OLD_COMPAT_LINE in patched:
        raise SafetyError("runner_patch_verification_failed")
    return patched


def verify_source_package(
    source: Path,
    *,
    expected_exe: str = EXPECTED_EXE_SHA256,
    expected_dll: str = EXPECTED_DLL_SHA256,
) -> dict:
    records = parse_manifest(source)
    _verify_manifest_files(source, records, prepared=False)
    runner_path = source / RUNNER_RELATIVE
    pilot_path = source / PILOT_RELATIVE
    for required in (runner_path, pilot_path, source / AUTO_RUN_RELATIVE):
        if not required.is_file():
            raise SafetyError("required_package_file_missing")
    runner_text = runner_path.read_text(encoding="utf-8")
    if expected_exe not in runner_text or expected_dll not in runner_text:
        raise SafetyError("fixed_binary_sha_guard_missing")
    dll_name = "base/avernum_jp_unicode.dll"
    if records.get(dll_name) != expected_dll:
        raise SafetyError("manifest_dll_sha_guard_mismatch")
    patch_runner_text(runner_text)
    pilot_text = pilot_path.read_text(encoding="utf-8")
    if not detect_only_click_guard(pilot_text):
        raise SafetyError("detect_only_click_guard_missing")
    return {
        "manifest_valid": True,
        "fixed_exe_sha_guard": True,
        "fixed_dll_sha_guard": True,
        "detect_only_click_guard": True,
        "source_runner_sha256": sha256_file(runner_path),
        "manifest_records": records,
    }


def _private_directory_ok(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        info = path.stat()
        return stat.S_IMODE(info.st_mode) & 0o077 == 0 and (not hasattr(os, "getuid") or info.st_uid == os.getuid())
    except OSError:
        return False


def ensure_private_root(private_root: Path = PRIVATE_ROOT) -> bool:
    try:
        private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return False
    return _private_directory_ok(private_root)


def verify_prepared_copy(
    prepared: Path,
    *,
    expected_exe: str = EXPECTED_EXE_SHA256,
    expected_dll: str = EXPECTED_DLL_SHA256,
) -> dict:
    if not _private_directory_ok(prepared):
        raise SafetyError("prepared_copy_missing_or_not_private")
    records = parse_manifest(prepared)
    metadata_path = prepared / PREPARED_NAME
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise SafetyError("prepared_manifest_missing")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SafetyError("prepared_manifest_invalid") from None
    if set(metadata) != {"source_runner_sha256", "patched_runner_sha256", "copy_created"}:
        raise SafetyError("prepared_manifest_invalid")
    if metadata.get("copy_created") is not True:
        raise SafetyError("prepared_copy_not_confirmed")
    runner_path = prepared / RUNNER_RELATIVE
    try:
        runner_bytes = runner_path.read_bytes()
        runner_text = runner_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        raise SafetyError("prepared_runner_invalid") from None
    if sha256_file(runner_path) != metadata.get("patched_runner_sha256"):
        raise SafetyError("prepared_runner_hash_mismatch")
    if runner_text.count(NEW_COMPAT_LINE) != 1 or OLD_COMPAT_LINE in runner_text:
        raise SafetyError("prepared_runner_patch_invalid")
    restored = runner_text.replace(NEW_COMPAT_LINE, OLD_COMPAT_LINE, 1).encode("utf-8")
    original_sha = hashlib.sha256(restored).hexdigest()
    if original_sha != metadata.get("source_runner_sha256"):
        raise SafetyError("prepared_runner_source_hash_mismatch")
    if records.get(RUNNER_RELATIVE.as_posix()) != original_sha:
        raise SafetyError("prepared_manifest_runner_hash_mismatch")
    _verify_manifest_files(prepared, records, prepared=True)
    if sha256_file(prepared / "base/avernum_jp_unicode.dll") != expected_dll:
        raise SafetyError("prepared_dll_hash_mismatch")
    if expected_exe not in runner_text or expected_dll not in runner_text:
        raise SafetyError("prepared_fixed_sha_guard_missing")
    if not detect_only_click_guard((prepared / PILOT_RELATIVE).read_text(encoding="utf-8")):
        raise SafetyError("prepared_detect_only_guard_missing")
    return metadata


def prepare_copy(
    source: Path = SOURCE_PACKAGE,
    private_root: Path = PRIVATE_ROOT,
    *,
    expected_exe: str = EXPECTED_EXE_SHA256,
    expected_dll: str = EXPECTED_DLL_SHA256,
) -> dict:
    report = {"mode": "prepare", "status": "blocked", "prepared_copy": "not_created"}
    try:
        validation = verify_source_package(source, expected_exe=expected_exe, expected_dll=expected_dll)
        report.update({key: validation[key] for key in ("manifest_valid", "fixed_exe_sha_guard", "fixed_dll_sha_guard", "detect_only_click_guard")})
        report["source_runner_sha256"] = validation["source_runner_sha256"]
    except (SafetyError, OSError, UnicodeError) as error:
        report["error"] = error.reason if isinstance(error, SafetyError) else "source_validation_failed"
        return report
    if not ensure_private_root(private_root):
        report["error"] = "private_root_unavailable"
        return report
    destination = private_root / "persistent_pilot"
    if destination.exists() or destination.is_symlink():
        try:
            metadata = verify_prepared_copy(destination, expected_exe=expected_exe, expected_dll=expected_dll)
            if metadata["source_runner_sha256"] != validation["source_runner_sha256"]:
                raise SafetyError("prepared_source_mismatch")
        except (SafetyError, OSError, UnicodeError):
            report.update(error="prepared_copy_collision", prepared_copy="collision")
            return report
        report.update(status="ready", prepared_copy="reused", patched_runner_sha256=metadata["patched_runner_sha256"])
        return report

    temporary: Path | None = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".persistent_pilot.tmp-", dir=private_root))
        shutil.copytree(source, temporary, dirs_exist_ok=True, copy_function=shutil.copy2, symlinks=False)
        os.chmod(temporary, 0o700)
        copied_runner = temporary / RUNNER_RELATIVE
        copied_text = copied_runner.read_text(encoding="utf-8")
        patched_text = patch_runner_text(copied_text)
        copied_runner.write_text(patched_text, encoding="utf-8", newline="")
        patched_sha = sha256_file(copied_runner)
        metadata = {
            "source_runner_sha256": validation["source_runner_sha256"],
            "patched_runner_sha256": patched_sha,
            "copy_created": True,
        }
        metadata_path = temporary / PREPARED_NAME
        with metadata_path.open("x", encoding="utf-8") as output:
            json.dump(metadata, output, sort_keys=True)
            output.write("\n")
        os.chmod(metadata_path, 0o600)
        verify_prepared_copy(temporary, expected_exe=expected_exe, expected_dll=expected_dll)
        if destination.exists() or destination.is_symlink():
            report.update(error="prepared_copy_collision", prepared_copy="collision")
            return report
        os.rename(temporary, destination)
        temporary = None
        verify_prepared_copy(destination, expected_exe=expected_exe, expected_dll=expected_dll)
        report.update(status="ready", prepared_copy="created", patched_runner_sha256=patched_sha)
        return report
    except (SafetyError, OSError, UnicodeError, shutil.Error) as error:
        report["error"] = error.reason if isinstance(error, SafetyError) else "copy_or_verification_failed"
        return report
    finally:
        if temporary is not None and temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary, ignore_errors=True)


def private_compat_path_is_allowed(path: Path, private_root: Path = PRIVATE_ROOT) -> bool:
    expected = private_root / "persistent_compatdata"
    try:
        return (
            path.absolute() == expected.absolute()
            and not private_root.is_symlink()
            and not path.is_symlink()
        )
    except OSError:
        return False


def _valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


def backup_result_zip(source: Path, destination: Path) -> tuple[str, str | None]:
    if not source.exists():
        return "not_needed", None
    if not source.is_file() or destination.exists() or not _valid_zip(source):
        return "failed", None
    try:
        source_sha = sha256_file(source)
        shutil.copy2(source, destination)
        os.chmod(destination, 0o600)
        if sha256_file(destination) != source_sha or not _valid_zip(destination):
            return "failed", None
        return "verified", source_sha
    except OSError:
        return "failed", None


def _new_private_run_dir(private_root: Path) -> Path:
    run_root = private_root / "persistent_runs"
    try:
        run_root.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise SafetyError("private_run_root_unavailable") from None
    if not _private_directory_ok(run_root):
        raise SafetyError("private_run_root_unavailable")
    try:
        return Path(tempfile.mkdtemp(prefix="run_", dir=run_root))
    except OSError:
        raise SafetyError("private_run_directory_unavailable") from None


def status_report(private_root: Path = PRIVATE_ROOT) -> dict:
    report = {"mode": "status", "status": "unprepared", "prepared": False, "private_prefix_present": False}
    prepared = private_root / "persistent_pilot"
    prefix = private_root / "persistent_compatdata"
    report["private_prefix_present"] = prefix.is_dir() and not prefix.is_symlink()
    if not (prepared.exists() or prepared.is_symlink()):
        report["prepared_copy_status"] = "absent"
        return report
    try:
        metadata = verify_prepared_copy(prepared)
    except (SafetyError, OSError, UnicodeError):
        report.update(status="blocked", prepared_copy_status="unverified_collision")
        return report
    report.update(
        status="ready",
        prepared=True,
        prepared_copy_status="verified",
        source_runner_sha256=metadata["source_runner_sha256"],
        patched_runner_sha256=metadata["patched_runner_sha256"],
    )
    return report


def run_pilot(
    private_root: Path = PRIVATE_ROOT,
    results_zip: Path = RESULTS_ZIP,
    *,
    popen_factory: Callable | None = None,
    expected_exe: str = EXPECTED_EXE_SHA256,
    expected_dll: str = EXPECTED_DLL_SHA256,
) -> dict:
    report = {"mode": "run", "status": "blocked", "pilot_started": False, "detect_only": True, "input_attempted": False}
    try:
        metadata = verify_prepared_copy(
            private_root / "persistent_pilot",
            expected_exe=expected_exe,
            expected_dll=expected_dll,
        )
    except (SafetyError, OSError, UnicodeError):
        report["error"] = "prepared_copy_unverified"
        return report
    if not ensure_private_root(private_root):
        report["error"] = "private_root_unavailable"
        return report
    compat = private_root / "persistent_compatdata"
    if not private_compat_path_is_allowed(compat, private_root):
        report["error"] = "compat_path_rejected"
        return report
    try:
        run_dir = _new_private_run_dir(private_root)
    except SafetyError as error:
        report["error"] = error.reason
        return report
    backup_status, old_result_sha = backup_result_zip(results_zip, run_dir / "phase22_13_results_backup.zip")
    report["existing_result_backup"] = backup_status
    if backup_status == "failed":
        report["error"] = "result_backup_failed"
        return report
    try:
        if compat.exists():
            if not _private_directory_ok(compat):
                report["error"] = "compat_directory_not_private"
                return report
        else:
            compat.mkdir(mode=0o700)
        command = ["bash", str(private_root / "persistent_pilot" / AUTO_RUN_RELATIVE), "--detect-only", "--timeout", "90"]
        environment = os.environ.copy()
        environment["AVERNUM_JP_TEST_COMPAT_PATH"] = str(compat)
        log_path = run_dir / "pilot_console.log"
        with log_path.open("x", encoding="utf-8") as output:
            os.chmod(log_path, 0o600)
            launcher = popen_factory or subprocess.Popen
            child = launcher(command, cwd=private_root / "persistent_pilot", env=environment, stdout=output, stderr=subprocess.STDOUT, close_fds=True)
            report["pilot_started"] = True
            try:
                exit_code = child.wait(timeout=RUN_WAIT_SECONDS)
                report["pilot_exit_confirmed"] = True
                report["pilot_exit_code"] = exit_code
                report["status"] = "completed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                report["status"] = "partial"
                report["pilot_exit_confirmed"] = False
                report["error"] = "pilot_exit_unconfirmed"
        if results_zip.is_file():
            snapshot = run_dir / "phase22_13_results_after_run.zip"
            snapshot_status, _ = backup_result_zip(results_zip, snapshot)
            report["result_zip_snapshot"] = snapshot_status
            report["result_zip_changed"] = old_result_sha is None or sha256_file(results_zip) != old_result_sha
        else:
            report["result_zip_snapshot"] = "absent"
            report["result_zip_changed"] = False
        report["compat_prefix_present"] = compat.is_dir()
        report["input_attempted"] = False
        return report
    except (OSError, subprocess.SubprocessError):
        report.update(status="failed", error="pilot_execution_failed")
        report["input_attempted"] = False
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare", action="store_true", help="Validate and atomically prepare a private pilot copy")
    modes.add_argument("--status", action="store_true", help="Read-only preparation status (default)")
    modes.add_argument("--run", action="store_true", help="Explicitly run the prepared pilot in detect-only mode")
    args = parser.parse_args(argv)
    if args.prepare:
        report = prepare_copy()
    elif args.run:
        report = run_pilot()
    else:
        report = status_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") in {"ready", "completed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
