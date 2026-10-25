from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
import sys
from unittest.mock import patch

from resourcelease.model import Config, LeaseError
from resourcelease.store import Store
from resourcelease import store as store_module
from test_resource_lease_model import configuration


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store.initialize(Path(self.temporary.name) / "pool", Config.parse(configuration()))
        self.epoch = self.store.service_start()

    def request(self, owner="alice", capability="test"):
        value = self.store.new_receipt(owner, capability)
        self.store.register(value, owner)
        return value

    def test_initialize_never_overwrites(self):
        with self.assertRaises(LeaseError):
            Store.initialize(self.store.root, self.store.config)
        self.assertEqual(self.store.pool_id, Store(self.store.root).pool_id)

    def test_registration_samples_time_after_transaction_lock(self):
        value = self.store.new_receipt("alice", "test")
        original = self.store.transaction
        before = time.monotonic()
        clock = [before]

        @contextmanager
        def delayed_transaction(*, write=True):
            # Another service tick commits while this client waits for the lock.
            clock[0] = before + 0.1
            with original(write=write) as connection:
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='service_seen'", (str(clock[0]),))
                yield connection

        with patch.object(self.store, "transaction", delayed_transaction):
            with patch("resourcelease.store.time.monotonic", side_effect=lambda: clock[0]):
                self.store.register(value, "alice")

        self.assertEqual(self.store.lease(value, "alice")["state"], "queued")

    def test_read_heartbeat_uses_time_after_snapshot_is_established(self):
        original = store_module._meta
        clock = [time.monotonic()]

        def metadata(connection, key):
            if key == "service_seen":
                clock[0] += 0.1
                return str(clock[0])
            return original(connection, key)

        with patch("resourcelease.store._meta", side_effect=metadata):
            with patch("resourcelease.store.time.monotonic", side_effect=lambda: clock[0]):
                value = self.store.new_receipt("alice", "test")
                self.assertEqual(value["epoch"], self.epoch)
                self.assertTrue(self.store.status()["service"]["available"])

    def test_waiting_for_transaction_cannot_renew_an_expired_lease(self):
        value = self.request()
        original = self.store.transaction
        clock = [time.monotonic()]

        @contextmanager
        def delayed_transaction(*, write=True):
            clock[0] += self.store.config.lease_seconds + 1
            with original(write=write) as connection:
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='service_seen'", (str(clock[0]),))
                yield connection

        with patch.object(self.store, "transaction", delayed_transaction):
            with patch("resourcelease.store.time.monotonic", side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(LeaseError, "no longer renewable"):
                    self.store.renew(value, "alice")

    def test_concurrent_assign_has_exactly_one_winner(self):
        receipts = [self.request(f"user-{i}") for i in range(12)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            assigned = list(executor.map(lambda _: self.store.assign("one"), range(12)))
        self.assertEqual([value for value in assigned if value], [receipts[0]["id"]])
        self.assertEqual(self.store.lease(receipts[1], "user-1")["state"], "queued")

    def test_multiprocess_assignment_is_atomic(self):
        first = self.request()
        self.request("bob")
        script = ("from pathlib import Path; from resourcelease.store import Store; "
                  "import sys; print(Store(Path(sys.argv[1])).assign('one'))")
        children = [subprocess.Popen([sys.executable, "-c", script, str(self.store.root)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(6)]
        outputs = []
        for child in children:
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err)
            outputs.append(out.strip())
        self.assertEqual([value for value in outputs if value != "None"], [first["id"]])

    def test_request_retry_and_payload_conflict(self):
        receipt = self.request()
        self.store.register(receipt, "alice")
        self.assertEqual(len(self.store.status()["requests"]), 1)
        changed = dict(receipt, capability="other")
        with self.assertRaises(LeaseError):
            self.store.register(changed, "alice")

    def test_credential_and_owner_are_checked(self):
        receipt = self.request()
        for value, owner in ((dict(receipt, secret="0" * 64), "alice"), (receipt, "mallory")):
            with self.subTest(owner=owner), self.assertRaises(LeaseError):
                self.store.release(value, owner)
        self.assertNotIn(receipt["secret"], json.dumps(self.store.status()))

    def test_request_ids_cannot_be_paths(self):
        value = self.store.new_receipt("alice", "test")
        with self.assertRaises(LeaseError):
            self.store.register(dict(value, id="../not-a-generation"), "alice")

    def test_renewal_cannot_revive_expired_lease_before_service_tick(self):
        receipt = self.request()
        later = time.monotonic() + self.store.config.lease_seconds + 1
        with patch("resourcelease.store.time.monotonic", return_value=later):
            self.store.service_tick(self.epoch)
            with self.assertRaises(LeaseError):
                self.store.renew(receipt, "alice")

    def test_atomic_expiry_without_timer_processing(self):
        receipt = self.request()
        with self.store.transaction() as connection:
            connection.execute("UPDATE requests SET expires=? WHERE id=?",
                               (time.monotonic() - 1, receipt["id"]))
        with self.assertRaises(LeaseError):
            self.store.renew(receipt, "alice")
        self.assertIsNone(self.store.assign("one"))

    def test_release_does_not_relabel_an_already_expired_lease(self):
        receipt = self.request()
        self.store.assign("one")
        self.store.publish(receipt["id"], {"serial": "fake"})
        with self.store.transaction() as connection:
            connection.execute("UPDATE requests SET expires=?", (time.monotonic() - 1,))
        self.store.release(receipt, "alice")
        self.store.finish(receipt["id"])
        self.assertEqual(self.store.lease(receipt, "alice")["state"], "expired")

    def test_status_never_advertises_an_expired_or_unavailable_endpoint(self):
        receipt = self.request()
        self.store.assign("one")
        self.store.publish(receipt["id"], {"serial": "fake"})
        self.store.service_stop(self.epoch)
        self.assertIsNone(self.store.status()["requests"][0]["endpoint"])
        with self.store.transaction() as connection:
            connection.execute("UPDATE requests SET expires=?", (time.monotonic() - 1,))
        result = self.store.lease(receipt, "alice")
        self.assertEqual(result["state"], "expired")
        self.assertTrue(result["cleanup_pending"])

    def test_cancellation_fences_late_boot(self):
        receipt = self.request()
        self.store.assign("one")
        self.store.release(receipt, "alice", cancel=True)
        self.assertFalse(self.store.publish(receipt["id"], {"serial": "fake"}))
        self.assertTrue(self.store.lease(receipt, "alice")["cleanup_pending"])
        self.assertIsNone(self.store.assign("one"))
        self.store.finish(receipt["id"])
        self.assertEqual(self.store.lease(receipt, "alice")["state"], "cancelled")

    def test_release_is_idempotent_after_successor_assignment(self):
        first, second = self.request(), self.request("bob")
        self.store.assign("one")
        self.store.publish(first["id"], {"serial": "fake"})
        self.store.release(first, "alice")
        self.store.finish(first["id"])
        self.assertEqual(self.store.assign("one"), second["id"])
        self.store.release(first, "alice")
        self.assertEqual(self.store.lease(second, "bob")["state"], "starting")

    def test_restart_fences_old_receipts_and_quarantines_custody(self):
        first, waiting = self.request(), self.request("bob")
        self.store.assign("one")
        self.store.service_start()
        self.assertEqual(self.store.status()["slots"][0]["state"], "quarantined")
        for value, owner in ((first, "alice"), (waiting, "bob")):
            with self.assertRaises(LeaseError):
                self.store.renew(value, owner)
            self.assertEqual(self.store.lease(value, owner)["state"], "failed")

    def test_history_pruning_never_resurrects_old_request(self):
        value = self.request()
        self.store.release(value, "alice", cancel=True)
        future = time.monotonic() + 4000
        with patch("resourcelease.store.time.monotonic", return_value=future):
            self.store.service_tick(self.epoch)
            self.assertEqual(self.store.status()["requests"], [])
            with self.assertRaisesRegex(LeaseError, "window expired"):
                self.store.register(value, "alice")

    def test_recent_tombstones_survive_capacity_pressure(self):
        small = Store.initialize(self.store.root.parent / "small",
                                 Config.parse(configuration(max_requests=16)))
        small.service_start()
        for i in range(16):
            value = small.new_receipt(f"user-{i}", "test")
            small.register(value, f"user-{i}")
            small.release(value, f"user-{i}", cancel=True)
        value = small.new_receipt("full", "test")
        with self.assertRaisesRegex(LeaseError, "capacity"):
            small.register(value, "full")
        self.assertEqual(len(small.status()["requests"]), 16)

    def test_release_remains_available_when_service_is_down(self):
        value = self.request()
        self.store.service_stop(self.epoch)
        with self.assertRaises(LeaseError):
            self.store.renew(value, "alice")
        self.assertEqual(self.store.release(value, "alice")["state"], "cancelled")

    def test_incompatible_older_request_does_not_block(self):
        config = configuration()
        config["slots"].append({"id": "two", "backend": "fake",
                                 "capabilities": ["other"], "options": {}})
        pool = Store.initialize(self.store.root.parent / "compatible", Config.parse(config))
        pool.service_start()
        a = pool.new_receipt("a", "other")
        b = pool.new_receipt("b", "test")
        pool.register(a, "a")
        pool.register(b, "b")
        self.assertEqual(pool.assign("one"), b["id"])
        self.assertEqual(pool.assign("two"), a["id"])
