"""Reach the physical recovery protocol through the public CLI after restart."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from resourcelease.backends.android import AndroidBackend
from resourcelease.backends.android_placement import AndroidPlacement
from resourcelease.backends.android_maintenance import AndroidMaintenance
from resourcelease.backends.android_state import PersistentAndroidState
from resourcelease.cli import main, write_private_receipt
from resourcelease.model import Config, QuarantineError
from resourcelease.operations import Operations
from resourcelease.service import Guard, Service
from resourcelease.store import Store, private_directory
from test_resource_lease_android import free_console_port, make_fake_android_sdk
from test_resource_lease_service import until


class SimulatedProcessExit(BaseException):
    pass


class MaintenanceRecoveryTests(unittest.TestCase):
    def make_case(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        sdk = make_fake_android_sdk(root / "sdk")
        options = sdk.options(free_console_port())
        options.update(state_mode="persistent", state_directory=str(root / "state"))
        config = Config.parse({
            "schema": 1, "slots": [
                {"id": "one", "backend": "android", "capabilities": ["test"],
                 "options": options},
            ],
        })
        store = Store.initialize(root / "pool", config)
        backend = AndroidBackend(placement=AndroidPlacement(root / "guards"))
        slot = config.slots[0]
        maintenance = backend.maintenance(slot)
        state = PersistentAndroidState.prepare(
            maintenance.directory, placement=backend.placement, **maintenance.profile,
        )
        state.avd_path.mkdir(mode=0o700)
        (state.avd_path / "config.ini").write_text("AvdId=test\n")
        (state.avd_home / f"{state.avd_name}.ini").write_text(f"path={state.avd_path}\n")
        state.mark_complete()
        guards = private_directory(root / "guards")
        epoch = store.service_start()
        with Guard(guards, backend.keys(slot), store.pool_id, "fixture-inspect",
                   maintenance_key=backend.maintenance_key(slot)) as guard:
            snapshot = maintenance.inspect()
            guard.clear()
        return root, store, backend, maintenance, guards, snapshot, epoch

    def start_service(self, store, backend, guards):
        service = Service(store, {"android": backend}, guard_root=guards,
                          maintenance_backends={"android": backend})
        thread = Thread(target=service.serve)

        def cleanup():
            service.request_shutdown()
            if thread.ident is not None:
                thread.join(10)
                self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        thread.start()
        until(lambda: store.status()["service"]["available"]
              and store.status()["service"]["epoch"] == service.epoch)
        return service

    def call_cli(self, store, backend, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(
                ["--root", str(store.root), *map(str, args)],
                backends={"android": backend}, maintenance_backends={"android": backend},
            )
        return code, json.loads(output.getvalue()) if output.getvalue() else None, errors.getvalue()

    def seed_interrupted_archive(self, *, after_rename, action="reset", committed=False):
        root, store, backend, maintenance, guards, snapshot, epoch = self.make_case()
        slot = store.config.slots[0]
        custody = {"pool_id": store.pool_id, "lease_id": None}
        backup_id = None
        if action in ("restore", "retire"):
            with Guard(guards, backend.keys(slot), store.pool_id, "setup-backup",
                       maintenance_key=backend.maintenance_key(slot)) as guard:
                snapshot = maintenance.archive("setup-backup", snapshot["revision"], custody)
                backup_id = snapshot["backup_id"]
                guard.clear()
        operations = Operations(store)
        receipt = operations.new_receipt(action, "one", 0, snapshot["revision"], backup_id)
        operations.submit(receipt)
        operation = operations.pending("one")
        operations.begin(
            receipt["id"], execution_epoch=epoch,
            execution_attempt=operation["execution_attempt"],
        )
        original = AndroidMaintenance._rename_directory

        def interrupt(instance, *args):
            if after_rename:
                original(instance, *args)
            raise SimulatedProcessExit()

        def perform():
            if action == "reset":
                return maintenance.archive(receipt["id"], snapshot["revision"], custody)
            if action == "restore":
                return maintenance.restore(
                    receipt["id"], backup_id, snapshot["revision"], custody,
                )
            return maintenance.retire(receipt["id"], backup_id, snapshot["revision"], custody)

        with Guard(guards, backend.keys(slot), store.pool_id, receipt["id"],
                   maintenance_key=backend.maintenance_key(slot)) as guard:
            if committed:
                result = perform()
                operations.set_phase(
                    receipt["id"], "physical_committed", result, execution_epoch=epoch,
                    execution_attempt=operation["execution_attempt"],
                )
                guard.clear()  # Crash after clear, before SQLite final completion.
            elif action == "retire":
                unlink = os.unlink

                def interrupt_deletion(*args, **kwargs):
                    unlink(*args, **kwargs)
                    raise SimulatedProcessExit()

                with patch("resourcelease.backends.android_maintenance.os.unlink",
                           side_effect=interrupt_deletion):
                    with self.assertRaisesRegex(QuarantineError, "retirement is incomplete") as raised:
                        perform()
                    self.assertIsInstance(raised.exception.__cause__, SimulatedProcessExit)
            else:
                with patch.object(AndroidMaintenance, "_rename_directory", interrupt):
                    with self.assertRaises(SimulatedProcessExit):
                        perform()
        path = root / "operation.json"
        write_private_receipt(path, receipt)
        return store, backend, maintenance, guards, receipt, path

    def prepare_reset_for_process_crash(self):
        root, store, backend, maintenance, guards, snapshot, epoch = self.make_case()
        receipt = Operations(store).new_receipt(
            "reset", "one", 0, snapshot["revision"],
        )
        operations = Operations(store)
        operations.submit(receipt)
        operation = operations.pending("one")
        operations.begin(
            receipt["id"], execution_epoch=epoch,
            execution_attempt=operation["execution_attempt"],
        )
        path = root / "operation.json"
        write_private_receipt(path, receipt)
        return store, backend, maintenance, guards, receipt, path

    @staticmethod
    def reap_child(child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def test_cli_recovers_restore_and_partial_retirement(self):
        for action in ("restore", "retire"):
            with self.subTest(action=action):
                store, backend, maintenance, guards, _receipt, path = (
                    self.seed_interrupted_archive(after_rename=True, action=action)
                )
                self.start_service(store, backend, guards)
                with patch.object(backend, "start", side_effect=AssertionError("SDK forbidden")):
                    code, result, error = self.call_cli(
                        store, backend, "operation", "recover", "--operation", path, "--wait", 5,
                    )
                self.assertEqual(code, 0, error)
                self.assertEqual(result["state"], "completed")
                self.assertEqual(maintenance.directory.exists(), action == "restore")
                self.assertEqual(store.status()["slots"][0]["state"], "idle")
                self.assertTrue(all(item.stat().st_size == 0 for item in guards.iterdir()))

    def test_committed_recovery_with_cleared_markers_does_not_repeat_archive(self):
        store, backend, maintenance, guards, _receipt, path = (
            self.seed_interrupted_archive(after_rename=True, committed=True)
        )
        self.start_service(store, backend, guards)
        with patch.object(AndroidMaintenance, "archive",
                          side_effect=AssertionError("archive must not repeat")):
            code, result, error = self.call_cli(
                store, backend, "operation", "recover", "--operation", path, "--wait", 5,
            )
        self.assertEqual(code, 0, error)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(maintenance.directory.exists())

    def test_cli_recovers_archive_before_or_after_rename_without_starting_sdk(self):
        for after_rename in (False, True):
            with self.subTest(after_rename=after_rename):
                store, backend, maintenance, guards, receipt, path = (
                    self.seed_interrupted_archive(after_rename=after_rename)
                )
                self.start_service(store, backend, guards)
                self.assertEqual(store.status()["slots"][0]["state"], "quarantined")
                with patch.object(backend, "start", side_effect=AssertionError("SDK start forbidden")):
                    code, result, error = self.call_cli(
                        store, backend, "operation", "recover", "--operation", path, "--wait", 5,
                    )
                self.assertEqual(code, 0, error)
                self.assertEqual(result["state"], "completed")
                self.assertEqual(store.status()["slots"][0]["state"], "idle")
                self.assertFalse(maintenance.directory.exists())
                self.assertTrue(all(item.stat().st_size == 0 for item in guards.iterdir()))
                original_result = Operations(store).inspect(receipt)
                code, repeated, error = self.call_cli(
                    store, backend, "operation", "recover", "--operation", path, "--wait", 5,
                )
                self.assertEqual(code, 0, error)
                self.assertEqual(repeated["id"], original_result["id"])
                self.assertEqual(repeated["execution_attempt"], original_result["execution_attempt"])
                self.assertEqual(repeated["result"], original_result["result"])

    def test_real_process_crash_recovers_archive_before_and_after_rename(self):
        script = r"""
import json
import os
from pathlib import Path
import sys

from resourcelease.backends.android import AndroidBackend
from resourcelease.backends.android_placement import AndroidPlacement
from resourcelease.backends.android_maintenance import AndroidMaintenance
from resourcelease.service import Guard
from resourcelease.store import Store

pool_root = Path(sys.argv[1])
guard_root = Path(sys.argv[2])
receipt = json.loads(Path(sys.argv[3]).read_text())
crash_point = sys.argv[4]
store = Store(pool_root)
backend = AndroidBackend(placement=AndroidPlacement(guard_root))
slot = store.config.slots[0]
maintenance = backend.maintenance(slot)
original_rename = AndroidMaintenance._rename_directory

def crash_at_archive_window(instance, *args):
    if crash_point == "before_rename":
        os._exit(71)
    original_rename(instance, *args)
    if crash_point == "after_rename":
        os._exit(72)

AndroidMaintenance._rename_directory = crash_at_archive_window
with Guard(
    guard_root,
    backend.keys(slot),
    store.pool_id,
    receipt["id"],
    maintenance_key=backend.maintenance_key(slot),
):
    maintenance.archive(
        receipt["id"],
        receipt["expected_revision"],
        {"pool_id": store.pool_id, "lease_id": None},
    )
os._exit(73)
"""
        root_directory = Path(__file__).resolve().parents[1]
        for crash_point, expected_exit in (("before_rename", 71), ("after_rename", 72)):
            with self.subTest(crash_point=crash_point):
                store, backend, maintenance, guards, receipt, path = (
                    self.prepare_reset_for_process_crash()
                )
                child = subprocess.Popen(
                    [
                        sys.executable, "-c", script, str(store.root), str(guards),
                        str(path), crash_point,
                    ],
                    cwd=root_directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(self.reap_child, child)
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(
                    child.returncode, expected_exit,
                    f"child did not exit at {crash_point}: stdout={stdout!r}, stderr={stderr!r}",
                )

                journal = maintenance._read_journal()
                operation = journal["operation"]
                self.assertEqual(operation["phase"], "ARCHIVE_INTENT")
                if crash_point == "before_rename":
                    self.assertTrue(maintenance.directory.exists())
                    self.assertFalse(
                        (maintenance._maintenance_directory
                         / f"backup-{operation['backup_id']}").exists()
                    )
                else:
                    self.assertFalse(maintenance.directory.exists())
                    self.assertTrue(
                        (maintenance._maintenance_directory
                         / f"backup-{operation['backup_id']}").exists()
                    )

                service = self.start_service(store, backend, guards)
                self.assertNotEqual(service.epoch, receipt["epoch"])
                self.assertEqual(store.status()["slots"][0]["state"], "quarantined")
                with patch.object(
                    backend, "start", side_effect=AssertionError("SDK start forbidden"),
                ):
                    code, result, error = self.call_cli(
                        store, backend, "operation", "recover", "--operation", path,
                        "--wait", 5,
                    )
                self.assertEqual(code, 0, error)
                self.assertEqual(result["id"], receipt["id"])
                self.assertEqual(result["state"], "completed")
                self.assertEqual(store.status()["slots"][0]["state"], "idle")
                self.assertFalse(maintenance.directory.exists())
                self.assertTrue(
                    (maintenance._maintenance_directory
                     / f"backup-{operation['backup_id']}").exists()
                )
                self.assertTrue(all(item.stat().st_size == 0 for item in guards.iterdir()))

    def test_foreign_marker_recovery_keeps_quarantine_and_original_binding(self):
        store, backend, _maintenance, guards, receipt, path = (
            self.seed_interrupted_archive(after_rename=False)
        )
        foreign = next(guards.iterdir())
        foreign.write_bytes(b'{"schema":1,"pool":"foreign","lease":"do-not-touch"}\n')
        before = {item.name: item.read_bytes() for item in guards.iterdir()}
        self.start_service(store, backend, guards)
        code, _result, error = self.call_cli(
            store, backend, "operation", "recover", "--operation", path, "--wait", 5,
        )
        self.assertEqual(code, 1)
        self.assertIn("marker", error)
        slot = store.status()["slots"][0]
        self.assertEqual(slot["state"], "quarantined")
        self.assertEqual(slot["operation_id"], receipt["id"])
        self.assertEqual(before, {item.name: item.read_bytes() for item in guards.iterdir()})


if __name__ == "__main__":
    unittest.main()
