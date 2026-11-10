from pathlib import Path
import hashlib
import os
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from resourcelease.model import Config, LeaseError, QuarantineError
from resourcelease.service import Guard, Service
from resourcelease.store import Store, private_directory
from test_resource_lease_service import FakeBackend, until


class FakeMaintenance:
    def __init__(self):
        self.revision = "revision-one"
        self.backup = None
        self.calls = []

    def inspect(self):
        return {"revision": self.revision, "state": "complete", "backup_id": self.backup}

    def archive(self, operation_id, expected_revision, custody):
        if expected_revision != self.revision:
            raise LeaseError("state revision changed")
        self.calls.append(("archive", operation_id))
        self.backup = operation_id
        self.revision = "revision-two"
        return self.inspect()


class ResetBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.state = FakeMaintenance()

    def maintenance(self, slot):
        return self.state

    def maintenance_key(self, slot):
        return self.keys(slot)[0]


class MaintenanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.backend = ResetBackend()
        self.store = Store.initialize(root / "pool", Config.parse({
            "schema": 1,
            "lease_seconds": 30,
            "slots": [
                {"id": "one", "backend": "fake", "capabilities": ["test"], "options": {}},
                {"id": "two", "backend": "fake", "capabilities": ["other"],
                 "options": {"key": "physical-two"}},
            ],
        }))
        self.service = Service(
            self.store, {"fake": self.backend},
            guard_root=private_directory(root / "guards"),
            maintenance_backends={"fake": self.backend},
        )
        self.thread = Thread(target=self.service.serve)
        self.addCleanup(self.stop_service)
        self.thread.start()
        until(lambda: self.store.status()["service"]["available"])

    def stop_service(self):
        self.backend.gate.set()
        self.backend.fail_stop = False
        self.service.request_shutdown()
        if self.thread.ident is not None:
            self.thread.join(10)
            self.assertFalse(self.thread.is_alive())

    def operations(self):
        from resourcelease.operations import Operations
        return Operations(self.store)

    def acquire(self):
        receipt = self.store.new_receipt("alice", "test")
        self.store.register(receipt, "alice")
        until(lambda: self.store.lease(receipt, "alice")["state"] == "active")
        return receipt

    def reset(self, lease=None):
        operations = self.operations()
        generation = self.store.status()["slots"][0]["generation"]
        receipt = operations.new_receipt(
            "reset", "one", generation, "revision-one",
            lease=lease, owner="alice" if lease else None,
        )
        operations.submit(receipt, lease=lease, owner="alice" if lease else None)
        return receipt

    def test_idle_reset_archives_without_starting_resource(self):
        operation = self.reset()
        result = until(lambda: (
            state if (state := self.operations().inspect(operation))["state"]
            in ("completed", "failed") else None
        ))
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(self.backend.starts, 0)
        self.assertEqual(len(self.backend.state.calls), 1)
        self.assertEqual(self.store.status()["slots"][0]["state"], "idle")

    def test_owned_reset_keeps_receipt_and_replaces_generation(self):
        lease = self.acquire()
        before = self.store.lease(lease, "alice")
        operation = self.reset(lease)
        result = until(lambda: (
            state if (state := self.operations().inspect(operation))["state"]
            in ("completed", "failed") else None
        ))
        self.assertEqual(result["state"], "completed", result)
        after = self.store.lease(lease, "alice")
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["state"], "active")
        self.assertGreater(after["generation"], before["generation"])
        self.assertEqual(self.backend.starts, 2)
        self.assertEqual(self.backend.stops, 1)
        self.assertIsNotNone(after["endpoint"])

    def test_failed_replacement_keeps_renewable_lease_without_endpoint(self):
        lease = self.acquire()
        self.backend.fail_start = True
        operation = self.reset(lease)
        result = until(lambda: (
            state if (state := self.operations().inspect(operation))["state"] == "failed"
            else None
        ))
        self.assertEqual(result["state"], "failed")
        state = self.store.lease(lease, "alice")
        self.assertEqual(state["state"], "held_unavailable")
        self.assertIsNone(state["endpoint"])
        self.assertFalse(state["cleanup_pending"])
        self.store.renew(lease, "alice")
        self.store.release(lease, "alice")
        until(lambda: self.store.status()["slots"][0]["state"] == "idle")

    def test_release_during_replacement_prevents_publication(self):
        lease = self.acquire()
        self.backend.gate.clear()
        self.reset(lease)
        until(lambda: self.backend.starts == 2)
        self.assertIsNone(self.store.lease(lease, "alice")["endpoint"])
        self.store.release(lease, "alice")
        self.backend.gate.set()
        until(lambda: self.store.status()["slots"][0]["state"] == "idle")
        state = self.store.lease(lease, "alice")
        self.assertEqual(state["state"], "completed")
        self.assertIsNone(state["endpoint"])

    def test_guard_remains_held_during_replacement(self):
        from resourcelease.service import Busy
        lease = self.acquire()
        self.backend.gate.clear()
        self.reset(lease)
        until(lambda: self.backend.starts == 2)
        with self.assertRaises(Busy):
            with Guard(self.service.guard_root, self.backend.keys(self.store.config.slots[0]),
                       "another-pool", "another-lease"):
                self.fail("reset released physical custody")
        self.backend.gate.set()
        until(lambda: self.store.lease(lease, "alice")["state"] == "active")

    def test_unknown_old_stop_quarantines_without_archive(self):
        lease = self.acquire()
        self.backend.fail_stop = True
        operation = self.reset(lease)
        state = until(lambda: (
            value if (value := self.operations().inspect(operation))["state"] == "failed"
            else None
        ))
        self.assertIsNotNone(state["error"])
        self.assertEqual(self.backend.state.calls, [])
        until(lambda: self.store.status()["slots"][0]["state"] == "quarantined")

    def test_idle_reset_does_not_interrupt_other_slot(self):
        other = self.store.new_receipt("bob", "other")
        self.store.register(other, "bob")
        before = until(lambda: (
            value if (value := self.store.lease(other, "bob"))["state"] == "active"
            else None
        ))
        operation = self.reset()
        until(lambda: self.operations().inspect(operation)["state"] == "completed")
        after = self.store.lease(other, "bob")
        self.assertEqual(after["state"], "active")
        self.assertEqual(after["generation"], before["generation"])
        self.assertEqual(after["endpoint"], before["endpoint"])
        self.assertEqual(self.backend.stops, 0)

    def test_expiry_during_replacement_does_not_revive_lease(self):
        lease = self.acquire()
        self.backend.gate.clear()
        self.reset(lease)
        until(lambda: self.backend.starts == 2)
        with self.store.transaction() as connection:
            connection.execute("UPDATE requests SET expires=0 WHERE id=?", (lease["id"],))
        self.backend.gate.set()
        until(lambda: self.store.status()["slots"][0]["state"] == "idle")
        self.assertEqual(self.store.lease(lease, "alice")["state"], "expired")
        self.assertIsNone(self.store.lease(lease, "alice")["endpoint"])


class MaintenanceGuardTests(unittest.TestCase):
    def test_recovery_reads_markers_before_explicit_proof_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = private_directory(Path(temporary))
            keys = ("a-port", "z-state")
            with Guard(root, keys, "pool", "operation", maintenance_key="z-state"):
                pass  # Simulate a process exit with durable maintenance interlocks.
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            with Guard(root, keys, "pool", "operation", maintenance_key="z-state",
                       recovery=True) as guard:
                self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
                guard.activate_recovery(requires_state_marker=True)
                guard.clear()
            self.assertTrue(all(path.stat().st_size == 0 for path in root.iterdir()))

    def test_recovery_never_adopts_lease_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = private_directory(Path(temporary))
            keys = ("a-port", "z-state")
            with Guard(root, keys, "pool", "lease"):
                pass
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            with self.assertRaisesRegex(QuarantineError, "marker"):
                with Guard(root, keys, "pool", "operation", maintenance_key="z-state",
                           recovery=True):
                    self.fail("lease marker was adopted")
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})

    def test_uncommitted_recovery_requires_state_marker_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = private_directory(Path(temporary))
            keys = ("a-port", "z-state")
            with Guard(root, keys, "pool", "operation", maintenance_key="z-state",
                       recovery=True) as guard:
                with self.assertRaisesRegex(QuarantineError, "state marker"):
                    guard.activate_recovery(requires_state_marker=True)
                self.assertTrue(all(path.stat().st_size == 0 for path in root.iterdir()))

    def test_state_marker_is_installed_first_and_cleared_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = private_directory(Path(temporary))
            keys = ("a-port", "z-state")
            with Guard(root, keys, "pool", "operation", maintenance_key="z-state") as guard:
                paths = [root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
                         for key in keys]
                self.assertTrue(all(path.stat().st_size for path in paths))
                calls = []
                original = os.ftruncate

                def truncate(descriptor, size):
                    calls.append(descriptor)
                    return original(descriptor, size)

                with patch("resourcelease.service.os.ftruncate", side_effect=truncate):
                    guard.clear()
                self.assertEqual(calls, list(reversed(guard.descriptors)))
                self.assertTrue(all(path.stat().st_size == 0 for path in paths))


if __name__ == "__main__":
    unittest.main()
