import fcntl
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from resourcelease.model import Config, LeaseError
from resourcelease.operations import Operations
from resourcelease.store import Store
from test_resource_lease_model import configuration


def persistent_configuration(**changes):
    value = configuration(**changes)
    value["slots"][0]["options"] = {"state_mode": "persistent"}
    return value


def operation_call(operations, method, operation, *args, **kwargs):
    kwargs["execution_epoch"] = operation["execution_epoch"]
    kwargs["execution_attempt"] = operation["execution_attempt"]
    return getattr(operations, method)(operation["id"], *args, **kwargs)


def make_schema1_pool(root, *, service_state="stopped", request_state=None):
    root.mkdir(mode=0o700)
    connection = sqlite3.connect(root / "leases.sqlite3")
    connection.executescript("""
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE slots(
         id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'idle',
         lease_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
         endpoint TEXT, error TEXT
        );
        CREATE TABLE requests(
         sequence INTEGER PRIMARY KEY AUTOINCREMENT,
         id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, capability TEXT NOT NULL,
         secret_hash TEXT NOT NULL, epoch TEXT NOT NULL, issued REAL NOT NULL,
         expires REAL NOT NULL, state TEXT NOT NULL,
         slot_id TEXT REFERENCES slots(id), target TEXT, completed REAL, error TEXT,
         artifacts INTEGER NOT NULL DEFAULT 0, quarantined INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX one_slot_owner ON requests(slot_id)
         WHERE state IN ('starting', 'active', 'releasing');
        PRAGMA user_version=1;
    """)
    pool_id = "pool-before-migration"
    config = Config.parse(persistent_configuration()).document()
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES (?,?)",
        [("pool_id", pool_id), ("config", json.dumps(config, sort_keys=True,
         separators=(",", ":"))), ("epoch", ""), ("service_seen", "0"),
         ("service_state", service_state), ("shutdown", "0")],
    )
    connection.execute("INSERT INTO slots(id) VALUES ('one')")
    if request_state is not None:
        connection.execute(
            "INSERT INTO requests(id,owner,capability,secret_hash,epoch,issued,expires,state,completed) "
            "VALUES ('old-request','alice','test','hash','',1,1,?,?)",
            (request_state, 2 if request_state in ("completed", "cancelled", "expired", "failed")
             else None),
        )
    connection.commit()
    connection.close()
    os.chmod(root / "leases.sqlite3", 0o600)
    return pool_id


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "pool"
        self.store = Store.initialize(
            self.root, Config.parse(persistent_configuration()),
        )
        self.epoch = self.store.service_start()
        self.operations = Operations(self.store)

    def active_lease(self):
        receipt = self.store.new_receipt("alice", "test")
        self.store.register(receipt, "alice")
        self.assertEqual(self.store.assign("one"), receipt["id"])
        self.assertTrue(self.store.publish(receipt["id"], {"serial": "fake"}))
        return receipt

    def receipt(self, action="reset", *, lease=None, owner=None, generation=None,
                revision="revision-one", backup_id=None):
        if generation is None:
            generation = self.store.status()["slots"][0]["generation"]
        return self.operations.new_receipt(
            action, "one", generation, revision, backup_id,
            lease=lease, owner=owner,
        )

    @staticmethod
    def finish_idle_operation(store, operations, action, revision, *,
                              backup_id=None, result=None, error=None):
        generation = store.status()["slots"][0]["generation"]
        receipt = operations.new_receipt(
            action, "one", generation, revision, backup_id,
        )
        operation = operations.submit(receipt)
        operation_call(operations, "begin", operation)
        if result is not None and error is None:
            operation_call(operations, "set_phase", operation, "physical_committed", result)
        return operation_call(
            operations, "complete", operation, result, error=error,
        ), receipt

    def test_idle_submission_reserves_slot_and_retry_is_idempotent(self):
        receipt = self.receipt()
        first = self.operations.submit(receipt)
        second = self.operations.submit(receipt)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["epoch"], receipt["epoch"])
        self.assertEqual(first["execution_epoch"], receipt["epoch"])
        self.assertEqual(first["execution_attempt"], 0)
        self.assertEqual(first["execution_kind"], "normal")
        self.assertIsNone(first["previous_error"])
        self.assertIsNone(self.store.assign("one"))
        status = self.store.status()
        self.assertEqual(status["slots"][0]["operation_id"], receipt["id"])
        self.assertNotIn(receipt["secret"], json.dumps(status))
        self.assertEqual(self.operations.inspect(receipt)["id"], receipt["id"])

    def test_completion_cannot_settle_before_fenced_begin(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        with self.assertRaisesRegex(LeaseError, "not running"):
            operation_call(
                self.operations, "complete", operation, {"outcome": "premature"},
            )
        self.assertEqual(self.operations.inspect(receipt)["state"], "pending")
        self.assertEqual(
            self.store.status()["slots"][0]["operation_id"], operation["id"],
        )

    def test_receipt_binding_and_stale_generation_are_rejected(self):
        receipt = self.receipt()
        self.operations.submit(receipt)
        altered = dict(receipt, expected_revision="different-revision")
        with self.assertRaises(LeaseError):
            self.operations.submit(altered)
        with self.assertRaises(LeaseError):
            self.operations.submit(self.receipt(generation=1))
        self.assertEqual(self.store.status()["slots"][0]["state"], "maintenance")
        self.assertEqual(self.store.status()["slots"][0]["operation_id"], receipt["id"])

    def test_generation_input_is_bounded_to_sqlite_integer_range(self):
        with self.assertRaisesRegex(LeaseError, "generation"):
            self.operations.new_receipt("reset", "one", 2**63, "revision-one")

    def test_idle_admission_races_assignment_atomically(self):
        lease = self.store.new_receipt("alice", "test")
        self.store.register(lease, "alice")
        operation_receipt = self.receipt()

        def submit():
            try:
                return self.operations.submit(operation_receipt)
            except LeaseError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            assigned = executor.submit(self.store.assign, "one")
            submitted = executor.submit(submit)
            lease_id, operation = assigned.result(), submitted.result()
        if operation is not None:
            self.assertEqual(operation["state"], "pending")
            self.assertIsNone(lease_id)
            self.assertEqual(self.store.status()["slots"][0]["state"], "maintenance")
        else:
            self.assertEqual(lease_id, lease["id"])
            self.assertEqual(self.store.status()["slots"][0]["state"], "starting")

    def test_owned_reset_withdraws_endpoint_and_preserves_renewable_lease(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        current = self.store.lease(lease, "alice")
        self.assertEqual(current["state"], "resetting")
        self.assertIsNone(current["endpoint"])
        self.assertFalse(current["cleanup_pending"])
        self.assertFalse(current["resource_available"])
        self.assertFalse(self.store.status()["slots"][0]["resource_available"])
        self.assertEqual(self.operations.pending("one"), None)
        self.assertEqual(self.operations.pending("one", lease["id"])["id"], operation["id"])
        self.assertEqual(self.store.renew(lease, "alice")["state"], "resetting")
        self.assertIsNone(self.store.assign("one"))

    def test_owned_operation_receipt_requires_matching_lease_and_owner(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        with self.assertRaises(LeaseError):
            self.operations.submit(receipt, lease=lease, owner="mallory")
        changed_owner = dict(receipt, owner="mallory")
        with self.assertRaises(LeaseError):
            self.operations.validate_receipt(
                changed_owner, action="reset", slot_id="one",
                expected_generation=receipt["expected_generation"],
                expected_revision="revision-one", lease=lease, owner="alice",
            )
        self.assertEqual(self.store.lease(lease, "alice")["state"], "active")

    def test_owned_reset_generation_fences_publication_and_keeps_lease_receipt(self):
        lease = self.active_lease()
        before = self.store.lease(lease, "alice")
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        operation_call(self.operations, "begin", operation)
        generation = operation_call(self.operations, "advance_generation", operation)
        self.assertGreater(generation, before["generation"])
        self.assertFalse(self.store.publish(
            lease["id"], {"serial": "stale"}, operation_id=operation["id"],
            generation=generation - 1,
        ))
        self.assertFalse(self.store.publish(
            lease["id"], {"serial": "wrong"}, operation_id="wrong-operation",
            generation=generation,
        ))
        self.assertTrue(self.store.publish(
            lease["id"], {"serial": "replacement"}, operation_id=operation["id"],
            generation=generation,
        ))
        self.assertTrue(self.store.publish(
            lease["id"], {"serial": "replacement"}, operation_id=operation["id"],
            generation=generation,
        ))
        self.assertFalse(self.store.publish(
            lease["id"], {"serial": "changed"}, operation_id=operation["id"],
            generation=generation,
        ))
        operation_call(
            self.operations, "complete", operation, {"backup_id": "backup-one"},
        )
        after = self.store.lease(lease, "alice")
        self.assertEqual(after["state"], "active")
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["generation"], generation)
        self.assertEqual(after["endpoint"], {"serial": "replacement"})
        self.assertEqual(self.operations.inspect(receipt)["state"], "completed")
        self.assertEqual(self.operations.submit(
            receipt, lease=lease, owner="alice",
        )["state"], "completed")

    def test_expiry_during_reset_prevents_publication_and_lease_revival(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        operation_call(self.operations, "begin", operation)
        generation = operation_call(self.operations, "advance_generation", operation)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE requests SET expires=? WHERE id=?",
                (time.monotonic() - 1, lease["id"]),
            )
        self.store.service_tick(self.epoch)
        self.assertFalse(self.store.publish(
            lease["id"], {"serial": "late"}, operation_id=operation["id"],
            generation=generation,
        ))
        operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("lease expired"),
        )
        self.store.stopping(lease["id"])
        self.store.finish(lease["id"])
        self.assertEqual(self.store.lease(lease, "alice")["state"], "expired")
        with self.assertRaises(LeaseError):
            self.store.renew(lease, "alice")

    def test_restart_fences_an_owned_reset_without_reviving_its_receipt(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        replacement_store = Store(self.store.root)
        replacement_store.service_start()
        self.assertEqual(self.operations.inspect(receipt)["state"], "failed")
        self.assertEqual(replacement_store.status()["slots"][0]["state"], "quarantined")
        self.assertEqual(replacement_store.status()["slots"][0]["operation_id"], operation["id"])
        with self.assertRaises(LeaseError):
            self.store.renew(lease, "alice")

    def test_release_during_reset_is_not_revived_by_completion(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        operation_call(self.operations, "begin", operation)
        with self.assertRaises(LeaseError):
            self.store.finish(lease["id"])
        self.store.release(lease, "alice")
        self.store.stopping(lease["id"])
        operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("released"),
        )
        self.store.finish(lease["id"])
        self.assertEqual(self.store.lease(lease, "alice")["state"], "completed")

    def test_failed_reset_holds_unavailable_lease_but_never_revives_expiry(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        operation_call(self.operations, "begin", operation)
        operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("boot failed"),
        )
        unavailable = self.store.lease(lease, "alice")
        self.assertEqual(unavailable["state"], "held_unavailable")
        self.assertIsNone(unavailable["endpoint"])
        self.assertFalse(unavailable["cleanup_pending"])
        self.assertFalse(unavailable["resource_available"])
        self.assertEqual(self.store.renew(lease, "alice")["state"], "held_unavailable")

    def test_failed_completion_after_publication_holds_unavailable_and_withdraws_endpoint(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        operation_call(self.operations, "begin", operation)
        generation = operation_call(self.operations, "advance_generation", operation)
        self.assertTrue(self.store.publish(
            lease["id"], {"serial": "replacement"}, operation_id=operation["id"],
            generation=generation,
        ))
        failed = operation_call(
            self.operations, "complete", operation,
            {"backup_id": "backup-after-publish"},
            error=LeaseError("replacement failed after endpoint publication"),
        )
        current = self.store.lease(lease, "alice")
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(current["state"], "held_unavailable")
        self.assertIsNone(current["endpoint"])
        self.assertFalse(current["resource_available"])
        self.assertEqual(self.store.renew(lease, "alice")["state"], "held_unavailable")

    def test_reset_completion_without_fenced_publication_fails_closed(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        operation_call(self.operations, "begin", operation)
        operation_call(self.operations, "advance_generation", operation)
        result = operation_call(
            self.operations, "complete", operation, {"backup_id": "backup-unpublished"},
        )
        current = self.store.lease(lease, "alice")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(current["state"], "held_unavailable")
        self.assertIsNone(current["endpoint"])
        self.assertFalse(current["resource_available"])

    def test_failed_completion_after_publication_does_not_revive_expired_lease(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        operation_call(self.operations, "begin", operation)
        generation = operation_call(self.operations, "advance_generation", operation)
        self.assertTrue(self.store.publish(
            lease["id"], {"serial": "replacement"}, operation_id=operation["id"],
            generation=generation,
        ))
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE requests SET expires=? WHERE id=?",
                (time.monotonic() - 1, lease["id"]),
            )
        self.store.service_tick(self.epoch)
        operation_call(
            self.operations, "complete", operation, {"backup_id": "backup-expired"},
            error=LeaseError("replacement failed after expiry"),
        )
        current = self.store.lease(lease, "alice")
        self.assertEqual(current["state"], "expired")
        self.assertIsNone(current["endpoint"])
        self.assertFalse(current["resource_available"])
        with self.assertRaises(LeaseError):
            self.store.renew(lease, "alice")
        self.store.stopping(lease["id"])
        self.store.finish(lease["id"])

    def test_idle_quarantine_and_restart_fail_closed(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        operation_call(self.operations, "begin", operation)
        operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("unknown archive outcome"),
            quarantine=True,
        )
        self.assertEqual(self.store.status()["slots"][0]["state"], "quarantined")

        another = Store.initialize(
            Path(self.temporary.name) / "restart",
            Config.parse(persistent_configuration()),
        )
        another.service_start()
        restart_ops = Operations(another)
        pending_receipt = restart_ops.new_receipt("reset", "one", 0, "revision-one")
        restart_ops.submit(pending_receipt)
        another.service_start()
        self.assertEqual(restart_ops.inspect(pending_receipt)["state"], "failed")
        self.assertEqual(another.status()["slots"][0]["state"], "quarantined")
        self.assertIsNone(restart_ops.pending("one"))

    def test_quarantined_operation_binding_pins_aged_history(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        operation_call(self.operations, "begin", operation)
        operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("unknown archive outcome"),
            quarantine=True,
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE operations SET completed=?,issued=? WHERE id=?",
                (time.monotonic() - 20_000, time.monotonic() - 20_000, operation["id"]),
            )
            self.operations._prune(connection, time.monotonic() + 20_000)
        self.assertIn(
            operation["id"],
            {item["id"] for item in self.store.status()["operations"]},
        )
        self.assertEqual(
            self.store.status()["slots"][0]["operation_id"], operation["id"],
        )

    def test_idle_recovery_requeues_same_receipt_and_deduplicates_attempt(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        operation_call(self.operations, "begin", operation)
        operation_call(
            self.operations, "set_phase", operation,
            "archive_intent", {"archive_id": "backup-recovery"},
        )
        operation_call(
            self.operations, "complete", operation,
            {"archive_id": "backup-recovery"},
            error=LeaseError("service restarted during archive"),
            quarantine=True,
        )
        replacement_store = Store(self.store.root)
        new_epoch = replacement_store.service_start()
        recovered_operations = Operations(replacement_store)

        barrier = Barrier(2)

        def recover():
            barrier.wait()
            return recovered_operations.recover(receipt)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(recover)
            second = executor.submit(recover)
            recovered, repeated = first.result(), second.result()
        self.assertEqual(recovered["id"], receipt["id"])
        self.assertEqual(recovered["state"], "pending")
        self.assertEqual(recovered["phase"], "archive_intent")
        self.assertEqual(recovered["result"], {"archive_id": "backup-recovery"})
        self.assertEqual(recovered["execution_epoch"], new_epoch)
        self.assertEqual(recovered["execution_attempt"], 1)
        self.assertEqual(recovered["execution_kind"], "recovery")
        self.assertEqual(recovered["previous_error"], "service restarted during archive")
        self.assertEqual(recovered["epoch"], receipt["epoch"])
        self.assertEqual(recovered["expected_generation"], receipt["expected_generation"])
        self.assertEqual(recovered["expected_revision"], receipt["expected_revision"])
        self.assertEqual(recovered["backup_id"], receipt["backup_id"])
        self.assertEqual(recovered["lease_id"], receipt["lease_id"])
        self.assertEqual(repeated["execution_epoch"], new_epoch)
        self.assertEqual(repeated["execution_attempt"], 1)
        begun = operation_call(recovered_operations, "begin", recovered)
        repeated_running = recovered_operations.recover(receipt)
        self.assertEqual(begun["phase"], "archive_intent")
        self.assertEqual(begun["result"], {"archive_id": "backup-recovery"})
        self.assertEqual(repeated_running["state"], "running")
        self.assertEqual(repeated_running["execution_epoch"], new_epoch)
        self.assertEqual(repeated_running["execution_attempt"], 1)
        slot = replacement_store.status()["slots"][0]
        self.assertEqual(slot["state"], "quarantined")
        self.assertEqual(slot["operation_id"], receipt["id"])

    def test_stale_attempt_cannot_mutate_recovered_operation(self):
        receipt = self.receipt()
        original = self.operations.submit(receipt)
        operation_call(self.operations, "begin", original)
        operation_call(
            self.operations, "complete", original, None,
            error=LeaseError("uncertain archive"),
            quarantine=True,
        )
        replacement_store = Store(self.store.root)
        replacement_store.service_start()
        recovered_operations = Operations(replacement_store)
        recovered = recovered_operations.recover(receipt)
        stale = {
            "execution_epoch": original["execution_epoch"],
            "execution_attempt": original["execution_attempt"],
        }
        stale_attempt = {
            "execution_epoch": recovered["execution_epoch"],
            "execution_attempt": original["execution_attempt"],
        }
        for token in (stale, stale_attempt):
            for mutate in (
                lambda token=token: recovered_operations.begin(
                    original["id"], **token,
                ),
                lambda token=token: recovered_operations.set_phase(
                    original["id"], "stale", **token,
                ),
                lambda token=token: recovered_operations.advance_generation(
                    original["id"], **token,
                ),
                lambda token=token: recovered_operations.complete(
                    original["id"], {"outcome": "stale"}, **token,
                ),
            ):
                with self.subTest(token=token, mutate=mutate), self.assertRaises(LeaseError):
                    mutate()
        current = recovered_operations.inspect(receipt)
        self.assertEqual(current["state"], "pending")
        self.assertEqual(current["execution_epoch"], recovered["execution_epoch"])
        self.assertEqual(current["execution_attempt"], recovered["execution_attempt"])
        self.assertEqual(
            replacement_store.status()["slots"][0]["operation_id"], receipt["id"],
        )

    def test_stale_attempt_cannot_fail_current_owned_operation(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        with self.assertRaises(LeaseError):
            self.operations.fail_for_lease(
                lease["id"], "stale failure", execution_epoch=self.epoch,
                execution_attempt=operation["execution_attempt"] + 1,
            )
        self.assertEqual(self.operations.inspect(receipt)["state"], "pending")
        self.assertEqual(
            self.store.status()["slots"][0]["operation_id"], operation["id"],
        )

    def test_completed_idle_recovery_returns_existing_outcome(self):
        receipt = self.receipt()
        original = self.operations.submit(receipt)
        operation_call(self.operations, "begin", original)
        operation_call(
            self.operations, "complete", original, None,
            error=LeaseError("uncertain archive"), quarantine=True,
        )
        replacement_store = Store(self.store.root)
        replacement_store.service_start()
        recovered_operations = Operations(replacement_store)
        recovered = recovered_operations.recover(receipt)
        operation_call(recovered_operations, "begin", recovered)
        outcome = {"backup_id": "backup-recovered"}
        completed = operation_call(
            recovered_operations, "complete", recovered, outcome,
        )
        repeated_complete = operation_call(
            recovered_operations, "complete", recovered, outcome,
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(repeated_complete, completed)
        self.assertEqual(recovered_operations.recover(receipt), completed)
        with self.assertRaises(LeaseError):
            operation_call(recovered_operations, "complete", original, outcome)
        self.assertEqual(replacement_store.status()["slots"][0]["state"], "idle")

    def test_positive_no_mutation_recovery_settles_and_rejects_replay(self):
        receipt = self.receipt()
        original = self.operations.submit(receipt)
        operation_call(self.operations, "begin", original)
        operation_call(
            self.operations, "complete", original, None,
            error=LeaseError("uncertain archive"), quarantine=True,
        )
        replacement_store = Store(self.store.root)
        replacement_store.service_start()
        recovered_operations = Operations(replacement_store)
        recovered = recovered_operations.recover(receipt)
        operation_call(recovered_operations, "begin", recovered)
        settled = operation_call(
            recovered_operations, "complete", recovered, None,
            error=LeaseError("adapter proved no mutation"), quarantine=False,
        )
        self.assertEqual(settled["state"], "failed")
        self.assertEqual(replacement_store.status()["slots"][0]["state"], "idle")
        self.assertIsNone(replacement_store.status()["slots"][0]["operation_id"])
        with self.assertRaisesRegex(LeaseError, "exact quarantined"):
            recovered_operations.recover(receipt)

    def test_recovery_admission_rejects_service_shutdown(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        operation_call(self.operations, "begin", operation)
        operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("uncertain archive"), quarantine=True,
        )
        self.store.shutdown()
        with self.assertRaisesRegex(LeaseError, "shutting down"):
            self.operations.recover(receipt)
        self.assertEqual(self.operations.inspect(receipt)["state"], "failed")
        self.assertEqual(
            self.store.status()["slots"][0]["operation_id"], receipt["id"],
        )

    def test_recovery_rejects_owned_lease_operation(self):
        lease = self.active_lease()
        receipt = self.receipt(lease=lease, owner="alice")
        operation = self.operations.submit(receipt, lease=lease, owner="alice")
        operation_call(self.operations, "begin", operation)
        operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("uncertain owned reset"), quarantine=True,
        )
        replacement_store = Store(self.store.root)
        replacement_store.service_start()
        with self.assertRaisesRegex(LeaseError, "only idle"):
            Operations(replacement_store).recover(receipt)

    def test_shutdown_keeps_pending_recovery_quarantined(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        operation_call(self.operations, "begin", operation)
        operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("uncertain archive"),
            quarantine=True,
        )
        replacement_store = Store(self.store.root)
        replacement_store.service_start()
        recovered_operations = Operations(replacement_store)
        recovery = recovered_operations.recover(receipt)
        replacement_store.shutdown()
        replacement_store.service_tick(recovery["execution_epoch"])

        failed = recovered_operations.inspect(receipt)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["execution_attempt"], recovery["execution_attempt"])
        slot = replacement_store.status()["slots"][0]
        self.assertEqual(slot["state"], "quarantined")
        self.assertEqual(slot["operation_id"], receipt["id"])
        with self.assertRaises(LeaseError):
            replacement_store.assign("one")

    def test_shutdown_settles_pending_idle_operation_without_quarantining(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        self.store.shutdown()
        self.store.service_tick(self.epoch)
        result = self.operations.inspect(receipt)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["phase"], "not_started")
        self.assertIsNone(self.operations.pending("one"))
        slot = self.store.status()["slots"][0]
        self.assertEqual(slot["state"], "idle")
        self.assertIsNone(slot["operation_id"])
        with self.assertRaises(LeaseError):
            operation_call(self.operations, "begin", operation)

    def test_operation_phase_result_and_error_storage_are_bounded(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        operation_call(self.operations, "begin", operation)
        with self.assertRaisesRegex(LeaseError, "record limit"):
            operation_call(
                self.operations, "set_phase", operation,
                "oversized", {"diagnostic": "x" * 9000},
            )
        failed = operation_call(
            self.operations, "complete", operation,
            {"outcome": "failed"}, error="e" * 3000,
        )
        self.assertEqual(len(failed["error"]), 2048)
        self.assertEqual(len(failed["result"]["outcome"]), len("failed"))

    def test_committed_operation_evidence_cannot_be_overwritten(self):
        receipt = self.receipt()
        operation = self.operations.submit(receipt)
        operation_call(self.operations, "begin", operation)
        evidence = {"backup_id": "backup-one", "outcome": "archived"}
        operation_call(
            self.operations, "set_phase", operation, "physical_committed", evidence,
        )
        with self.assertRaisesRegex(LeaseError, "cannot be replaced"):
            operation_call(
                self.operations, "set_phase", operation,
                "physical_committed", {"outcome": "different"},
            )
        result = operation_call(
            self.operations, "complete", operation, None,
            error=LeaseError("replacement startup failed"),
        )
        self.assertEqual(result["result"], evidence)
        with self.assertRaisesRegex(LeaseError, "differs from its receipt"):
            operation_call(
                self.operations, "complete", operation, {"outcome": "different"},
            )

    def test_operation_capacity_does_not_prune_backup_evidence_or_block_release(self):
        config = Config.parse(persistent_configuration(max_requests=16))
        store = Store.initialize(Path(self.temporary.name) / "bounded", config)
        store.service_start()
        operations = Operations(store)
        for index in range(config.max_requests):
            receipt = operations.new_receipt("reset", "one", 0, f"revision-{index}")
            operation = operations.submit(receipt)
            operation_call(operations, "begin", operation)
            operation_call(
                operations, "complete", operation, {"backup_id": f"backup-{index}"},
            )
        future = time.monotonic() + 10_000
        with store.transaction() as connection:
            operations._prune(connection, future)
        self.assertEqual(len(store.status()["operations"]), config.max_requests)
        next_receipt = operations.new_receipt("reset", "one", 0, "last-revision")
        with self.assertRaisesRegex(LeaseError, "capacity"):
            operations.submit(next_receipt)
        self.assertEqual(len(store.status()["operations"]), config.max_requests)

        lease = store.new_receipt("alice", "test")
        store.register(lease, "alice")
        store.release(lease, "alice", cancel=True)
        self.assertEqual(store.lease(lease, "alice")["state"], "cancelled")

    def test_successful_same_pool_retire_or_restore_releases_aged_backup_history(self):
        for action in ("retire", "restore"):
            with self.subTest(action=action):
                pool = Store.initialize(
                    Path(self.temporary.name) / f"{action}-pool",
                    Config.parse(persistent_configuration()),
                )
                pool.service_start()
                operations = Operations(pool)
                archived, archive_receipt = self.finish_idle_operation(
                    pool, operations, "reset", "revision-before",
                    result={"backup_id": f"backup-{action}"},
                )
                resolver, resolver_receipt = self.finish_idle_operation(
                    pool, operations, action, "revision-after",
                    backup_id=f"backup-{action}", result={"outcome": action},
                )
                future = time.monotonic() + 10_000
                with pool.transaction() as connection:
                    operations._prune(connection, future)
                retained = {item["id"] for item in pool.status()["operations"]}
                self.assertNotIn(archive_receipt["id"], retained, archived)
                self.assertNotIn(resolver_receipt["id"], retained, resolver)

    def test_unmatched_or_cross_pool_backup_resolution_does_not_prune_history(self):
        _, archive_receipt = self.finish_idle_operation(
            self.store, self.operations, "reset", "revision-before",
            result={"backup_id": "backup-cross-pool"},
        )
        self.finish_idle_operation(
            self.store, self.operations, "retire", "revision-other",
            backup_id="different-backup", result={"outcome": "retired"},
        )

        other_pool = Store.initialize(
            Path(self.temporary.name) / "other-pool",
            Config.parse(persistent_configuration()),
        )
        other_pool.service_start()
        other_operations = Operations(other_pool)
        self.finish_idle_operation(
            other_pool, other_operations, "retire", "revision-other",
            backup_id="backup-cross-pool", result={"outcome": "retired"},
        )

        future = time.monotonic() + 10_000
        with self.store.transaction() as connection:
            self.operations._prune(connection, future)
        retained = {item["id"] for item in self.store.status()["operations"]}
        self.assertIn(archive_receipt["id"], retained)

    def test_uncommitted_resolver_cannot_prune_matching_backup_history(self):
        _, archive_receipt = self.finish_idle_operation(
            self.store, self.operations, "reset", "revision-before",
            result={"backup_id": "backup-uncommitted"},
        )
        resolver_receipt = self.receipt(
            "retire", revision="revision-after", backup_id="backup-uncommitted",
        )
        resolver = self.operations.submit(resolver_receipt)
        operation_call(self.operations, "begin", resolver)
        # A completed row is not proof of physical retirement unless its
        # irreversible commit phase was durably recorded first.
        operation_call(
            self.operations, "complete", resolver, {"outcome": "retired"},
        )

        future = time.monotonic() + 10_000
        with self.store.transaction() as connection:
            self.operations._prune(connection, future)
        retained = {item["id"] for item in self.store.status()["operations"]}
        self.assertIn(archive_receipt["id"], retained)
        self.assertIn(resolver_receipt["id"], retained)

    def test_unknown_backup_evidence_keeps_same_pool_resolution_proof(self):
        _, archive_receipt = self.finish_idle_operation(
            self.store, self.operations, "reset", "revision-before",
            result={"backup_id": ["unparseable"]},
        )
        _, retire_receipt = self.finish_idle_operation(
            self.store, self.operations, "retire", "revision-after",
            backup_id="backup-unknown", result={"outcome": "retired"},
        )

        future = time.monotonic() + 10_000
        with self.store.transaction() as connection:
            self.operations._prune(connection, future)
        retained = {item["id"] for item in self.store.status()["operations"]}
        self.assertIn(archive_receipt["id"], retained)
        self.assertIn(retire_receipt["id"], retained)

    def test_migrate_schema1_preserves_backup_and_existing_records(self):
        schema1_root = Path(self.temporary.name) / "schema1"
        pool_id = make_schema1_pool(schema1_root, request_state="cancelled")
        with self.assertRaisesRegex(LeaseError, "migrate"):
            Store(schema1_root)
        migrated = Store.migrate(schema1_root)
        self.assertEqual(migrated.pool_id, pool_id)
        self.assertEqual(migrated.status()["schema"], 2)
        backup = schema1_root / "leases.sqlite3.schema1.backup"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT value FROM metadata WHERE key='pool_id'").fetchone()[0],
                pool_id,
            )
            self.assertEqual(
                connection.execute("SELECT state FROM requests WHERE id='old-request'").fetchone()[0],
                "cancelled",
            )
        with closing(sqlite3.connect(schema1_root / "leases.sqlite3")) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            columns = [
                row[1] for row in connection.execute("PRAGMA table_info(operations)")
            ]
            self.assertTrue({
                "epoch", "execution_epoch", "execution_attempt", "execution_kind",
                "previous_error",
            }.issubset(columns))
            self.assertEqual(
                connection.execute("SELECT state FROM requests WHERE id='old-request'").fetchone()[0],
                "cancelled",
            )
        with closing(sqlite3.connect(self.store.path)) as connection:
            initialized_columns = [
                row[1] for row in connection.execute("PRAGMA table_info(operations)")
            ]
        self.assertEqual(columns, initialized_columns)
        with self.assertRaisesRegex(LeaseError, "schema"):
            Store.migrate(schema1_root)

    def test_existing_unverifiable_schema1_backup_blocks_without_migration(self):
        schema1_root = Path(self.temporary.name) / "bad-backup"
        make_schema1_pool(schema1_root)
        backup = schema1_root / "leases.sqlite3.schema1.backup"
        backup.write_text("not a sqlite database", encoding="utf-8")
        backup.chmod(0o600)
        with self.assertRaisesRegex(LeaseError, "backup"):
            Store.migrate(schema1_root)
        with closing(sqlite3.connect(schema1_root / "leases.sqlite3")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_migration_requires_stopped_settled_pool_and_available_lock(self):
        running = Path(self.temporary.name) / "running"
        make_schema1_pool(running, service_state="running")
        with self.assertRaises(LeaseError):
            Store.migrate(running)
        self.assertFalse((running / "leases.sqlite3.schema1.backup").exists())

        unsettled = Path(self.temporary.name) / "unsettled"
        make_schema1_pool(unsettled, request_state="queued")
        with self.assertRaises(LeaseError):
            Store.migrate(unsettled)
        self.assertFalse((unsettled / "leases.sqlite3.schema1.backup").exists())

        locked = Path(self.temporary.name) / "locked"
        make_schema1_pool(locked)
        lock_path = locked / "service.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(LeaseError, "service"):
                Store.migrate(locked)
        finally:
            os.close(descriptor)
        self.assertFalse((locked / "leases.sqlite3.schema1.backup").exists())


if __name__ == "__main__":
    unittest.main()
