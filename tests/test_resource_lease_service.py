from pathlib import Path
from threading import Event, Thread
import tempfile
import time
import unittest
import subprocess
import sys

from resourcelease.backends.android import AndroidBackend
from resourcelease.model import Config, LeaseError, QuarantineError
from resourcelease.service import Busy, Guard, Service
from resourcelease.store import Store, private_directory
from test_resource_lease_android import free_console_port, make_fake_android_sdk
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

    def android_pool(self, label, options, ttl=120):
        config = Config.parse({
            "schema": 1,
            "lease_seconds": ttl,
            "slots": [{
                "id": f"android-{label}",
                "capabilities": ["android-api-34"],
                "backend": "android",
                "options": options,
            }],
        })
        store = Store.initialize(self.root / label, config)
        service = Service(store, {"android": AndroidBackend()}, guard_root=self.guard_root)
        thread = Thread(target=service.serve)
        self.services.append((service, thread))
        thread.start()
        until(lambda: store.status()["service"]["available"])
        return store

    def android_request(self, pool, owner):
        value = pool.new_receipt(owner, "android-api-34")
        pool.register(value, owner)
        return value

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

    def test_persistent_android_state_is_guarded_across_pools_and_crashes(self):
        sdk = make_fake_android_sdk(self.root / "android-sdk")
        try:
            port_one = free_console_port()
            port_two = free_console_port(start=port_one + 2)
            port_three = free_console_port(start=port_two + 2)
        except RuntimeError as error:
            self.skipTest(str(error))
        state_directory = self.root / "shared-persistent-avd"

        options_one = sdk.options(port_one, boot_timeout=5)
        options_one.update(state_mode="persistent", state_directory=str(state_directory))
        options_two = sdk.options(port_two, boot_timeout=5)
        options_two.update(state_mode="persistent", state_directory=str(state_directory))
        first_pool = self.android_pool("android-one", options_one)
        first_receipt = self.android_request(first_pool, "android-owner-one")
        first = self.active(first_pool, first_receipt, "android-owner-one")
        self.assertEqual(len(sdk.calls("avdmanager")), 1)
        avd_path = Path(sdk.calls("avdmanager")[0]["args"][-1])
        user_data = avd_path / "userdata-qemu.img"
        user_data.write_bytes(b"retained across pools")

        second_pool = self.android_pool("android-two", options_two)
        second_receipt = self.android_request(second_pool, "android-owner-two")
        time.sleep(0.8)
        self.assertNotEqual(
            second_pool.lease(second_receipt, "android-owner-two")["state"],
            "active",
            "a different port must not permit simultaneous use of one persistent AVD",
        )
        self.assertEqual(len(sdk.calls("avdmanager")), 1)
        self.assertEqual(len(sdk.calls("emulator")), 1)

        first_pool.release(first_receipt, "android-owner-one")
        self.assertTrue(user_data.exists())
        self.assertEqual(first["generation"], 1)
        second = self.active(second_pool, second_receipt, "android-owner-two")
        self.assertGreaterEqual(second["generation"], 1)
        self.assertEqual(len(sdk.calls("avdmanager")), 1)
        self.assertEqual(len(sdk.calls("emulator")), 2)
        self.assertEqual(user_data.read_bytes(), b"retained across pools")
        second_pool.release(second_receipt, "android-owner-two")

        def second_released():
            lease = second_pool.lease(second_receipt, "android-owner-two")
            return lease["state"] == "completed" and not lease["cleanup_pending"]

        until(second_released)

        keys = AndroidBackend().keys(first_pool.config.slots[0])
        crash_script = (
            "from pathlib import Path; from resourcelease.service import Guard; import sys; "
            f"guard = Guard(Path(sys.argv[1]), {keys!r}, 'crashed-pool', 'lease'); "
            "guard.__enter__(); print('marked', flush=True); sys.stdin.read()"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", crash_script, str(self.guard_root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            ready = child.stdout.readline().strip()
            if ready != "marked":
                _, error = child.communicate(timeout=5)
                self.fail(f"guard child failed before marking persistent identity: {error}")
            child.kill()
            child.communicate(timeout=5)
        finally:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)

        options_three = sdk.options(port_three, boot_timeout=5)
        options_three.update(state_mode="persistent", state_directory=str(state_directory))
        third_pool = self.android_pool("android-three", options_three)
        third_receipt = self.android_request(third_pool, "android-owner-three")
        until(lambda: third_pool.status()["slots"][0]["state"] == "quarantined")
        self.assertEqual(third_pool.lease(third_receipt, "android-owner-three")["state"], "failed")
        self.assertEqual(len(sdk.calls("avdmanager")), 1)
        self.assertEqual(len(sdk.calls("emulator")), 2)
        self.assertTrue(user_data.exists())

    def test_persistent_android_state_survives_expiry(self):
        sdk = make_fake_android_sdk(self.root / "expiry-android-sdk")
        sdk.configure(shutdown_delay=1.5)
        try:
            port = free_console_port()
        except RuntimeError as error:
            self.skipTest(str(error))
        state_directory = self.root / "expiring-persistent-avd"
        options = sdk.options(port, boot_timeout=5)
        options.update(state_mode="persistent", state_directory=str(state_directory))
        pool = self.android_pool("android-expiry", options, ttl=3)
        first_receipt = self.android_request(pool, "expiring-owner")
        self.active(pool, first_receipt, "expiring-owner")
        avd_path = Path(sdk.calls("avdmanager")[0]["args"][-1])
        user_data = avd_path / "userdata-qemu.img"
        user_data.write_bytes(b"retained after expiry")

        def first_expired_and_cleaned():
            lease = pool.lease(first_receipt, "expiring-owner")
            slot = pool.status()["slots"][0]
            return (
                lease["state"] == "expired"
                and not lease["cleanup_pending"]
                and slot["state"] == "idle"
            )

        until(first_expired_and_cleaned, timeout=10)
        self.assertEqual(pool.lease(first_receipt, "expiring-owner")["state"], "expired")
        second_receipt = self.android_request(pool, "waiting-owner")
        second = self.active(pool, second_receipt, "waiting-owner")
        self.assertEqual(second["generation"], 2)
        self.assertEqual(len(sdk.calls("avdmanager")), 1)
        self.assertEqual(len(sdk.calls("emulator")), 2)
        self.assertEqual(user_data.read_bytes(), b"retained after expiry")
        pool.release(second_receipt, "waiting-owner")

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
