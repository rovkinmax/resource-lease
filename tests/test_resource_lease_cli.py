from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from resourcelease.cli import main, receipt
from resourcelease.model import Config
from resourcelease.service import Service
from resourcelease.store import Store, private_directory
from test_resource_lease_model import configuration
from test_resource_lease_service import FakeBackend, until


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.backend = FakeBackend()
        self.pool = self.root / "pool"
        self.services = []
        self.addCleanup(self.stop_services)

    def stop_services(self):
        for service, thread in self.services:
            service.cancelled.set()
            self.backend.gate.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

    def call(self, *arguments):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--root", str(self.pool), *map(str, arguments)],
                        backends={"fake": self.backend})
        return code, out.getvalue(), err.getvalue()

    def start(self):
        store = Store.initialize(self.pool, Config.parse(configuration(lease_seconds=3)))
        service = Service(store, {"fake": self.backend},
                          guard_root=private_directory(self.root / "guards"))
        thread = Thread(target=service.serve)
        self.services.append((service, thread))
        thread.start()
        until(lambda: store.status()["service"]["available"])
        return store

    def test_status_does_not_initialize(self):
        code, out, err = self.call("status")
        self.assertEqual(code, 1)
        self.assertFalse(out)
        self.assertFalse(self.pool.exists())

    def test_invalid_owner_creates_no_receipt(self):
        self.start()
        path = self.root / "invalid.lease"
        code, out, err = self.call("acquire", "--owner", "", "--capability", "test",
                                   "--lease", path)
        self.assertEqual(code, 1)
        self.assertFalse(path.exists())

    def test_full_public_lifecycle_and_receipt_privacy(self):
        self.start()
        path = self.root / "alice.lease"
        code, out, err = self.call("acquire", "--owner", "alice", "--capability", "test",
                                   "--lease", path, "--wait", "5")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["state"], "active")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        secret = json.loads(path.read_text())["secret"]
        self.assertNotIn(secret, out + err)
        for command in ("inspect", "renew", "release", "release"):
            code, out, err = self.call(command, "--owner", "alice", "--lease", path)
            self.assertEqual(code, 0, err)
            self.assertNotIn(secret, out + err)
        self.assertEqual(self.backend.starts, 1)
        self.assertEqual(self.backend.stops, 1)
        code, out, err = self.call("acquire", "--owner", "alice", "--capability", "test",
                                   "--lease", path, "--wait", "1")
        self.assertEqual(code, 1)
        self.assertEqual(self.backend.starts, 1)

    def test_wait_timeout_cancels_and_stops_boot(self):
        self.backend.gate.clear()
        store = self.start()
        path = self.root / "alice.lease"
        code, out, err = self.call("acquire", "--owner", "alice", "--capability", "test",
                                   "--lease", path, "--wait", "0.4")
        self.assertEqual(code, 1)
        value = json.loads(path.read_text())
        until(lambda: not store.lease(value, "alice")["cleanup_pending"])
        self.assertEqual(store.lease(value, "alice")["state"], "cancelled")
        self.assertEqual(store.status()["slots"][0]["state"], "idle")

    def test_lost_response_reuses_persisted_receipt(self):
        store = self.start()
        path = self.root / "alice.lease"
        value = receipt(store, path, "alice", "test")
        store.register(value, "alice")
        code, out, err = self.call("acquire", "--owner", "alice", "--capability", "test",
                                   "--lease", path, "--wait", "5")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["id"], value["id"])
        self.assertEqual(self.backend.starts, 1)

    def test_shutdown_is_graceful(self):
        self.start()
        path = self.root / "alice.lease"
        self.call("acquire", "--owner", "alice", "--capability", "test", "--lease", path)
        code, out, err = self.call("shutdown", "--wait", "5")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["service"]["state"], "stopped")
        self.assertEqual(self.backend.stops, 1)

    def test_release_cleanup_failure_is_not_success(self):
        self.start()
        path = self.root / "alice.lease"
        self.call("acquire", "--owner", "alice", "--capability", "test", "--lease", path)
        self.backend.fail_stop = True
        code, out, err = self.call("release", "--owner", "alice", "--lease", path, "--wait", "5")
        self.assertEqual(code, 1)
        self.assertIn("cannot prove stop", err)
