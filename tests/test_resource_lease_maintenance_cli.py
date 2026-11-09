from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from resourcelease.cli import main, parser, write_private_receipt
from resourcelease.model import Config, LeaseError
from resourcelease.operations import Operations
from resourcelease.store import Store


class MaintenanceBackend:
    def maintenance_key(self, slot):
        return slot.options["key"]


class MaintenanceCliTests(unittest.TestCase):
    invalid_waits = ("-1", "nan", "86401")

    def make_store(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = Config.parse({
            "schema": 1,
            "slots": [
                {"id": "leased", "backend": "fake", "capabilities": ["test"],
                 "options": {"key": "leased-key"}},
                {"id": "idle", "backend": "fake", "capabilities": ["test"],
                 "options": {"key": "idle-key"}},
            ],
        })
        store = Store.initialize(root / "pool", config)
        store.service_start()
        return root, store

    @staticmethod
    def call_cli(store, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(
                ["--root", str(store.root), *map(str, args)],
                backends={"fake": object()},
                maintenance_backends={"fake": MaintenanceBackend()},
            )
        return code, errors.getvalue()

    def make_active_lease(self, store, root):
        lease = store.new_receipt("alice", "test")
        store.register(lease, "alice")
        self.assertEqual(store.assign("leased"), lease["id"])
        endpoint = {"resource": "owned"}
        self.assertTrue(store.publish(lease["id"], endpoint))
        path = root / "lease.json"
        write_private_receipt(path, lease)
        return lease, path, endpoint


    def test_reset_targets_are_exclusive(self):
        with self.assertRaises(SystemExit):
            parser().parse_args([
                "--root", "/pool", "reset", "--slot", "one", "--lease", "/lease",
                "--operation", "/operation", "--expect-generation", "1",
                "--expect-state-revision", "revision",
            ])

    def test_owned_reset_parses_original_receipt_and_revision(self):
        args = parser().parse_args([
            "--root", "/pool", "reset", "--lease", "/lease", "--owner", "alice",
            "--operation", "/operation", "--expect-generation", "1",
            "--expect-state-revision", "revision",
        ])
        self.assertEqual(args.lease, Path("/lease"))
        self.assertEqual(args.expect_generation, 1)
        self.assertEqual(args.expect_state_revision, "revision")

    def test_operation_inspection_has_no_new_request_arguments(self):
        args = parser().parse_args([
            "--root", "/pool", "operation", "inspect", "--operation", "/operation",
        ])
        self.assertEqual(args.action, "operation")
        self.assertEqual(args.operation_action, "inspect")

    def test_recovery_uses_only_original_receipt(self):
        args = parser().parse_args([
            "--root", "/pool", "operation", "recover",
            "--operation", "/original.operation.json", "--wait", "7",
        ])
        self.assertEqual(args.operation_action, "recover")
        self.assertEqual(args.operation, Path("/original.operation.json"))
        self.assertEqual(args.wait, 7)

    def test_private_receipt_never_replaces_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "operation.json"
            write_private_receipt(path, {"secret": "private"})
            before = path.read_bytes()
            with self.assertRaises((LeaseError, FileExistsError)):
                write_private_receipt(path, {"secret": "replacement"})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_wait_does_not_submit_reset_or_state_operations(self):
        state_actions = (
            ("inspect", None),
            ("restore", "backup-one"),
            ("retire", "backup-one"),
        )
        for wait in self.invalid_waits:
            with self.subTest(action="reset", wait=wait):
                root, store = self.make_store()
                lease, lease_path, endpoint = self.make_active_lease(store, root)
                operation_path = root / "reset.operation.json"
                code, error = self.call_cli(
                    store, "reset", "--lease", lease_path, "--owner", "alice",
                    "--operation", operation_path, "--expect-generation", 1,
                    "--expect-state-revision", "revision-one", "--wait", wait,
                )
                self.assertEqual(code, 1)
                self.assertIn("wait timeout", error)
                self.assertFalse(operation_path.exists())
                self.assertEqual(store.status()["operations"], [])
                self.assertEqual(store.lease(lease, "alice")["endpoint"], endpoint)

            for action, backup_id in state_actions:
                with self.subTest(action=f"state {action}", wait=wait):
                    root, store = self.make_store()
                    operation_path = root / "state.operation.json"
                    args = [
                        "state", action, "--slot", "idle", "--operation", operation_path,
                        "--expect-generation", 0, "--wait", wait,
                    ]
                    if action != "inspect":
                        args.extend([
                            "--expect-state-revision", "revision-one",
                            "--backup-id", backup_id,
                        ])
                    code, error = self.call_cli(store, *args)
                    self.assertEqual(code, 1)
                    self.assertIn("wait timeout", error)
                    self.assertFalse(operation_path.exists())
                    self.assertEqual(store.status()["operations"], [])
                    slot = next(item for item in store.status()["slots"]
                                if item["id"] == "idle")
                    self.assertEqual(slot["state"], "idle")
                    self.assertIsNone(slot["operation_id"])

    def test_invalid_wait_does_not_start_or_advance_recovery(self):
        for wait in self.invalid_waits:
            with self.subTest(wait=wait):
                root, store = self.make_store()
                operations = Operations(store)
                receipt = operations.new_receipt("reset", "idle", 0, "revision-one")
                operations.submit(receipt)
                pending = operations.pending("idle")
                operations.begin(
                    receipt["id"], execution_epoch=pending["execution_epoch"],
                    execution_attempt=pending["execution_attempt"],
                )
                operations.complete(
                    receipt["id"], None, error=LeaseError("simulated interrupted operation"),
                    quarantine=True, execution_epoch=pending["execution_epoch"],
                    execution_attempt=pending["execution_attempt"],
                )
                before = operations.inspect(receipt)
                operation_path = root / "original.operation.json"
                write_private_receipt(operation_path, receipt)

                code, error = self.call_cli(
                    store, "operation", "recover", "--operation", operation_path,
                    "--wait", wait,
                )
                self.assertEqual(code, 1)
                self.assertIn("wait timeout", error)
                after = operations.inspect(receipt)
                self.assertEqual(after["state"], before["state"])
                self.assertEqual(after["execution_attempt"], before["execution_attempt"])
                slot = next(item for item in store.status()["slots"]
                            if item["id"] == "idle")
                self.assertEqual(slot["state"], "quarantined")
                self.assertEqual(slot["operation_id"], receipt["id"])


if __name__ == "__main__":
    unittest.main()
