"""Execute the actual device-side readiness script against isolated host storage."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from resourcelease.backends.android import _READINESS_SCRIPT


class AndroidReadinessScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = self.root / "storage volume"
        self.storage.mkdir()
        self.download = self.storage / "Download"
        self.download.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        commands = {
            "pm": 'printf "%s\\n" "${PM_RESULT-package:/system/framework/framework-res.apk}"',
            "readlink": 'printf "%s\\n" "${PRIMARY_PATH-/storage/emulated/0}"',
            "sm": 'printf "%s\\n" "${VOLUMES-emulated;0 mounted null}"; exit "${SM_EXIT-0}"',
            "cat": (
                'case "${READ_MODE-ok}" in fail) exit 7;; corrupt) echo wrong;; '
                '*) exec /bin/cat "$@";; esac'
            ),
            "rm": '[ "${REMOVE_FAIL-0}" = 0 ] || exit 8; exec /bin/rm "$@"',
        }
        for name, body in commands.items():
            executable = self.tools / name
            executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            executable.chmod(0o700)
        self.token = "ab0123456789"

    def run_probe(self, *, clean=True, prefix="", **environment):
        return subprocess.run(
            [
                "/bin/sh", "-c", prefix + _READINESS_SCRIPT, "resource-lease-ready",
                str(self.storage), "1" if clean else "0", self.token,
            ],
            env={**os.environ, "PATH": f"{self.tools}:{os.environ['PATH']}", **environment},
            capture_output=True, text=True, timeout=5, check=False,
        )

    def assert_no_probe(self):
        self.assertEqual(list(self.storage.rglob(".resource-lease-ready-*")), [])

    def test_clean_round_trip_uses_download_and_removes_only_own_probe(self):
        sentinel = self.download / "keep.pdf"
        sentinel.write_bytes(b"existing data")
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sentinel.read_bytes(), b"existing data")
        self.assert_no_probe()

    def test_mounted_without_download_is_not_ready_and_does_not_create_it(self):
        self.download.rmdir()
        result = self.run_probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Download not ready", result.stderr)
        self.assertFalse(self.download.exists())
        self.assert_no_probe()

    def test_persistent_round_trip_does_not_recreate_download(self):
        self.download.rmdir()
        result = self.run_probe(clean=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.download.exists())
        self.assert_no_probe()

    def test_primary_volume_is_selected_exactly(self):
        for volume in ("emulated mounted null", "emulated;0 mounted null"):
            with self.subTest(volume=volume):
                result = self.run_probe(VOLUMES=volume)
                self.assertEqual(result.returncode, 0, result.stderr)
        for volume in (
            "emulated;0 checking null", "emulated;0 mounted_ro null",
            "emulated;10 mounted null", "",
            "emulated;10 mounted null\nemulated;0 checking null",
            "emulated mounted null\nemulated;0 mounted null",
        ):
            with self.subTest(volume=volume):
                self.assertNotEqual(self.run_probe(VOLUMES=volume).returncode, 0)
        self.assert_no_probe()

    def test_different_primary_user_is_supported(self):
        result = self.run_probe(
            PRIMARY_PATH="/storage/emulated/10",
            VOLUMES="emulated;0 checking null\nemulated;10 mounted null",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_probe()

    def test_package_manager_path_and_storage_manager_errors_fail_closed(self):
        for environment in (
            {"PM_RESULT": ""}, {"PRIMARY_PATH": "/storage/unknown/0"},
            {"PRIMARY_PATH": "/storage/emulated/0/child"}, {"SM_EXIT": "9"},
        ):
            with self.subTest(environment=environment):
                self.assertNotEqual(self.run_probe(**environment).returncode, 0)
                self.assert_no_probe()

    def test_exclusive_create_failure_never_deletes_existing_file(self):
        existing = self.download / f".resource-lease-ready-{self.token}"
        existing.write_bytes(b"not this invocation")
        result = self.run_probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_bytes(), b"not this invocation")

    def test_write_read_and_content_failures_remove_own_probe(self):
        for prefix, environment in (
            ("printf() { return 7; }\n", {}),
            ("", {"READ_MODE": "fail"}), ("", {"READ_MODE": "corrupt"}),
        ):
            with self.subTest(prefix=prefix, environment=environment):
                result = self.run_probe(prefix=prefix, **environment)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_probe()

    def test_failed_removal_is_not_readiness_success(self):
        result = self.run_probe(REMOVE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Storage probe cleanup failed", result.stderr)
        self.assertTrue((self.download / f".resource-lease-ready-{self.token}").is_file())
