from pathlib import Path
from threading import Event, Thread
import tempfile
import time
import unittest
import subprocess
import sys

from resourcelease.model import Config, LeaseError, QuarantineError
from resourcelease.service import Busy, Guard, Service
from resourcelease.store import Store, private_directory
from test_resource_lease_model import configuration


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("condition did not become true")


class FakeResource:
    def __init__(self, backend, identifier):
        self.backend = backend
        self.endpoint = {"id": identifier}
        self.alive = True

    def healthy(self):
        return self.alive

    def stop(self):
        if self.backend.fail_stop:
            raise QuarantineError("cannot prove stop")
        self.alive = False
        self.backend.stops += 1


class FakeBackend:
    def __init__(self):
        self.starts = 0
        self.stops = 0
        self.gate = Event()
        self.gate.set()
        self.fail_stop = False
        self.fail_start = False
        self.unexpected = False

    def keys(self, slot):
        return (slot.options.get("key", "physical-one"),)

    def start(self, slot, directory, cancelled):
        self.starts += 1
        while not self.gate.wait(0.02):
            if cancelled.is_set():
                raise LeaseError("cancelled during start")
        if self.unexpected:
            raise RuntimeError("unknown failure after possible effect")
        if self.fail_start:
            raise LeaseError("start failed with proven cleanup")
        return FakeResource(self, directory.name)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.guard_root = private_directory(self.root / "guards")
        self.backend = FakeBackend()
        self.services = []
        self.addCleanup(self.stop_services)

    def stop_services(self):
        self.backend.gate.set()
        self.backend.fail_stop = False
        for service, thread in self.services:
            service.cancelled.set()
        for service, thread in self.services:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "test leaked a service")

    def pool(self, label="pool", ttl=120):
        store = Store.initialize(self.root / label, Config.parse(configuration(lease_seconds=ttl)))
        service = Service(store, {"fake": self.backend}, guard_root=self.guard_root)
        thread = Thread(target=service.serve)
        self.services.append((service, thread))
        thread.start()
        until(lambda: store.status()["service"]["available"])
        return store

    def request(self, pool, owner):
        value = pool.new_receipt(owner, "test")
        pool.register(value, owner)
        return value

    def active(self, pool, value, owner):
        return until(lambda: (s := pool.lease(value, owner))["state"] == "active" and s)

    def test_lifecycle_fifo_and_fresh_successor(self):
        pool = self.pool()
        first = self.request(pool, "a")
        a = self.active(pool, first, "a")
        second = self.request(pool, "b")
        self.assertEqual(pool.lease(second, "b")["state"], "queued")
        pool.release(first, "a")
        b = self.active(pool, second, "b")
        self.assertEqual(self.backend.stops, 1)
        self.assertNotEqual(a["endpoint"], b["endpoint"])
        self.assertEqual(b["generation"], 2)

    def test_owner_loss_stops_resource_before_successor(self):
        pool = self.pool(ttl=2)
        first = self.request(pool, "a")
        self.active(pool, first, "a")
        second = self.request(pool, "b")
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and pool.lease(second, "b")["state"] != "active":
            pool.renew(second, "b")
            time.sleep(0.1)
        self.assertEqual(pool.lease(first, "a")["state"], "expired")
        self.assertEqual(pool.lease(second, "b")["state"], "active")
        self.assertEqual(self.backend.stops, 1)

    def test_cancel_during_boot_never_publishes(self):
        self.backend.gate.clear()
        pool = self.pool()
        value = self.request(pool, "a")
        until(lambda: self.backend.starts == 1)
        pool.release(value, "a", cancel=True)
        until(lambda: not pool.lease(value, "a")["cleanup_pending"])
        self.assertEqual(pool.lease(value, "a")["state"], "cancelled")
        self.assertEqual(pool.status()["slots"][0]["state"], "idle")
        self.backend.gate.set()
        time.sleep(0.1)
        self.assertIsNone(pool.lease(value, "a")["endpoint"])

    def test_cross_pool_contention_does_not_duplicate_resource(self):
        one, two = self.pool("one"), self.pool("two")
        a = self.request(one, "a")
        self.active(one, a, "a")
        b = self.request(two, "b")
        time.sleep(0.8)
        self.assertEqual(self.backend.starts, 1)
        one.release(a, "a")
        self.active(two, b, "b")
        self.assertEqual(self.backend.starts, 2)
        self.assertEqual(self.backend.stops, 1)

    def test_unresolved_marker_blocks_another_pool(self):
        with Guard(self.guard_root, ("physical-one",), "old-pool", "old-lease"):
            pass  # A crash releases the OS lock without clearing its durable marker.
        pool = self.pool()
        value = self.request(pool, "a")
        until(lambda: pool.status()["slots"][0]["state"] == "quarantined")
        self.assertEqual(self.backend.starts, 0)
        self.assertEqual(pool.lease(value, "a")["state"], "failed")

    def test_partial_key_overlap_conflicts(self):
        with Guard(self.guard_root, ("port:1", "port:2"), "a", "a") as guard:
            with self.assertRaises(Busy):
                with Guard(self.guard_root, ("port:2", "port:3"), "b", "b"):
                    self.fail("overlap was allowed")
            guard.clear()

    def test_real_process_crash_retains_marker(self):
        script = (
            "from pathlib import Path; from resourcelease.service import Guard; import sys; "
            "g=Guard(Path(sys.argv[1]), ('physical-one',), 'crashed-pool', 'lease'); "
            "g.__enter__(); print('marked', flush=True); sys.stdin.read()"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(self.guard_root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "marked")
            child.kill()  # Direct owned test child; no SDK resource has been spawned.
            child.communicate(timeout=5)
            pool = self.pool()
            value = self.request(pool, "a")
            until(lambda: pool.lease(value, "a")["state"] == "failed")
            self.assertEqual(self.backend.starts, 0)
            self.assertEqual(pool.status()["slots"][0]["state"], "quarantined")
        finally:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)

    def test_completed_diagnostics_are_bounded(self):
        pool = self.pool()
        service = self.services[0][0]
        # Populate completed rows through the same store transitions, without SDK effects.
        service.cancelled.set()
        self.services[0][1].join(timeout=5)
        epoch = pool.service_start()
        for index in range(19):
            value = self.request(pool, f"owner-{index}")
            pool.assign("one")
            directory = pool.root / "generations" / value["id"]
            directory.mkdir(parents=True, mode=0o700)
            pool.finish(value["id"])
            service._prune_artifacts()
            pool.service_tick(epoch)
        self.assertLessEqual(len(list((pool.root / "generations").iterdir())), 16)
        pool.service_stop(epoch)

    def test_cleanup_failure_quarantines_only_resource(self):
        pool = self.pool()
        value = self.request(pool, "a")
        self.active(pool, value, "a")
        self.backend.fail_stop = True
        pool.release(value, "a")
        until(lambda: pool.status()["slots"][0]["state"] == "quarantined")
        self.assertEqual(pool.lease(value, "a")["state"], "failed")
        self.assertTrue(any(p.stat().st_size for p in self.guard_root.iterdir()))

    def test_unknown_backend_failure_preserves_marker(self):
        self.backend.unexpected = True
        pool = self.pool()
        value = self.request(pool, "a")
        until(lambda: pool.lease(value, "a")["state"] == "failed")
        self.assertEqual(pool.status()["slots"][0]["state"], "quarantined")

    def test_ordinary_start_failure_cleans_slot(self):
        self.backend.fail_start = True
        pool = self.pool()
        value = self.request(pool, "a")
        until(lambda: pool.lease(value, "a")["state"] == "failed")
        self.assertEqual(pool.status()["slots"][0]["state"], "idle")
        self.assertFalse(any(p.stat().st_size for p in self.guard_root.iterdir()))

    def test_singleton_does_not_change_epoch(self):
        pool = self.pool()
        epoch = pool.status()["service"]["epoch"]
        with self.assertRaises(Busy):
            Service(pool, {"fake": self.backend}, guard_root=self.guard_root).serve()
        self.assertEqual(pool.status()["service"]["epoch"], epoch)
