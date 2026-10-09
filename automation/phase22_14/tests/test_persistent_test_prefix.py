import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

MODULE_PATH = Path(__file__).parents[1] / "persistent_test_prefix.py"
spec = importlib.util.spec_from_file_location("phase22_14_persistent_test_prefix", MODULE_PATH)
prefix = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = prefix
spec.loader.exec_module(prefix)


def safe_pilot_source():
    return '''
def ensure_active():
    window.cmd("windowactivate")
def click_ydotool(x, y):
    pass
def main():
    parser.add_argument("--detect-only", dest="click_choice", action="store_false")
    parser.set_defaults(click_choice=True)
    if args.click_choice:
        ensure_active()
        click_ydotool(1, 2)
'''


def create_package(root: Path):
    (root / "base").mkdir(parents=True)
    dll_data = b"fixture-dll"
    dll_sha = hashlib.sha256(dll_data).hexdigest()
    exe_sha = "e" * 64
    runner = (
        f"EXPECTED_EXE='{exe_sha}'\nEXPECTED_DLL=\"{dll_sha}\"\n"
        + prefix.OLD_COMPAT_LINE + "\n"
    )
    files = {
        "base/run_proton_isolated.sh": runner.encode(),
        "base/avernum_jp_unicode.dll": dll_data,
        "auto_pilot.py": safe_pilot_source().encode(),
        "run_auto.sh": b"#!/usr/bin/env bash\nexec python3 auto_pilot.py \"$@\"\n",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(files.items())
    )
    (root / prefix.MANIFEST_NAME).write_text(manifest, encoding="utf-8")
    return exe_sha, dll_sha, files


class PersistentTestPrefixTests(unittest.TestCase):
    def test_runner_patch_changes_exactly_one_compat_line(self):
        source = "before\n" + prefix.OLD_COMPAT_LINE + "\nafter\n"
        patched = prefix.patch_runner_text(source)
        self.assertEqual(patched.count(prefix.NEW_COMPAT_LINE), 1)
        self.assertNotIn(prefix.OLD_COMPAT_LINE, patched)

    def test_double_patch_or_missing_source_line_is_rejected(self):
        with self.assertRaises(prefix.SafetyError):
            prefix.patch_runner_text(prefix.OLD_COMPAT_LINE + "\n" + prefix.OLD_COMPAT_LINE)
        with self.assertRaises(prefix.SafetyError):
            prefix.patch_runner_text(prefix.NEW_COMPAT_LINE + "\n")

    def test_prepare_copies_and_patches_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source = temp / "distribution"
            source.mkdir()
            exe_sha, dll_sha, files = create_package(source)
            original_runner = (source / prefix.RUNNER_RELATIVE).read_bytes()
            private_root = temp / "private"
            private_root.mkdir(mode=0o700)
            result = prefix.prepare_copy(source, private_root, expected_exe=exe_sha, expected_dll=dll_sha)
            self.assertEqual(result["status"], "ready")
            prepared = private_root / "persistent_pilot"
            self.assertEqual((source / prefix.RUNNER_RELATIVE).read_bytes(), original_runner)
            self.assertIn(prefix.NEW_COMPAT_LINE, (prepared / prefix.RUNNER_RELATIVE).read_text())
            for relative, contents in files.items():
                if relative != prefix.RUNNER_RELATIVE.as_posix():
                    self.assertEqual((prepared / relative).read_bytes(), contents)
            metadata = json.loads((prepared / prefix.PREPARED_NAME).read_text())
            self.assertEqual(set(metadata), {"source_runner_sha256", "patched_runner_sha256", "copy_created"})
            self.assertTrue(metadata["copy_created"])

    def test_only_the_designated_private_compat_path_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            self.assertTrue(prefix.private_compat_path_is_allowed(root / "persistent_compatdata", root))
            self.assertFalse(prefix.private_compat_path_is_allowed(Path(temporary) / "steam_compat", root))
            self.assertFalse(prefix.private_compat_path_is_allowed(root / "nested" / "persistent_compatdata", root))

    def test_invalid_existing_result_zip_blocks_run_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source = temp / "distribution"
            source.mkdir()
            exe_sha, dll_sha, _ = create_package(source)
            private_root = temp / "private"
            private_root.mkdir(mode=0o700)
            prepared = prefix.prepare_copy(source, private_root, expected_exe=exe_sha, expected_dll=dll_sha)
            self.assertEqual(prepared["status"], "ready")
            result_zip = temp / "avernum_jp_phase22_13_results.zip"
            result_zip.write_bytes(b"not-a-zip")
            launcher = Mock()
            report = prefix.run_pilot(
                private_root,
                result_zip,
                popen_factory=launcher,
                expected_exe=exe_sha,
                expected_dll=dll_sha,
            )
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["error"], "result_backup_failed")
            launcher.assert_not_called()

    def test_status_is_read_only_when_unprepared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "not-created"
            report = prefix.status_report(root)
            self.assertEqual(report["status"], "unprepared")
            self.assertFalse(report["prepared"])
            self.assertFalse(root.exists())

    def test_zip_backup_requires_integrity_and_hash_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source = temp / "source.zip"
            target = temp / "backup.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("result.txt", "private test fixture")
            status, source_hash = prefix.backup_result_zip(source, target)
            self.assertEqual(status, "verified")
            self.assertEqual(prefix.sha256_file(target), source_hash)
            self.assertTrue(prefix._valid_zip(target))


if __name__ == "__main__":
    unittest.main(verbosity=2)
