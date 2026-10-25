import os
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_packaged_public_cli_owns_fake_sdk_lifecycle_and_expiry(self):
        from test_resource_lease_android import make_fake_android_sdk

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = make_fake_android_sdk(root / "sdk-fixture")
            port = None
            for candidate in range(5600, 5682, 2):
                with socket.socket() as console, socket.socket() as adb:
                    try:
                        console.bind(("127.0.0.1", candidate))
                        adb.bind(("127.0.0.1", candidate + 1))
                        port = candidate
                        break
                    except OSError:
                        continue
            self.assertIsNotNone(port, "no unused fake-SDK test port pair")
            package = root / "lease.pyz"
            built = subprocess.run(
                [sys.executable, str(ROOT / "scripts/build-resource-lease-package"),
                 "--output", str(package)], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stderr)
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema": 1, "lease_seconds": 3,
                "slots": [{"id": "sdk", "backend": "android", "capabilities": ["test"],
                           "options": fixture.options(port)}],
            }))
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("KENT") and key != "PYTHONPATH"}
            environment["RESOURCE_LEASE_HOME"] = str(root / "shared")
            command = [sys.executable, "-I", str(package), "--root", str(root / "pool")]

            def call(*args, timeout=20):
                result = subprocess.run([*command, *map(str, args)], cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=timeout)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            call("init", "--config", config)
            service = subprocess.Popen([*command, "serve"], cwd=root, env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            clients = []
            try:
                deadline = time.monotonic() + 10
                while not call("status")["service"]["available"]:
                    self.assertLess(time.monotonic(), deadline, "service did not become healthy")
                    time.sleep(0.05)
                a = root / "a.json"
                first = call("acquire", "--owner", "a", "--capability", "test",
                             "--lease", a, "--wait", "10")
                self.assertEqual(first["endpoint"]["adb_serial"], f"emulator-{port}")
                b = root / "b.json"
                second = subprocess.Popen(
                    [*command, "acquire", "--owner", "b", "--capability", "test",
                     "--lease", str(b), "--wait", "10"], cwd=root, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                clients.append(second)
                deadline = time.monotonic() + 5
                while not any(row["owner"] == "b" for row in call("status")["requests"]):
                    self.assertLess(time.monotonic(), deadline)
                self.assertIsNone(second.poll())
                call("release", "--owner", "a", "--lease", a, "--wait", "10")
                out, err = second.communicate(timeout=15)
                self.assertEqual(second.returncode, 0, err)
                self.assertEqual(json.loads(out)["generation"], 2)
                c = root / "c.json"
                third = call("acquire", "--owner", "c", "--capability", "test",
                             "--lease", c, "--wait", "10")
                self.assertEqual(third["generation"], 3)
                self.assertEqual(call("inspect", "--owner", "b", "--lease", b)["state"], "expired")
                call("release", "--owner", "c", "--lease", c, "--wait", "10")
                state = call("shutdown", "--wait", "10")
                self.assertEqual(state["service"]["state"], "stopped")
                self.assertEqual(state["slots"][0]["state"], "idle")
                out, err = service.communicate(timeout=10)
                self.assertEqual(service.returncode, 0, err)
                self.assertFalse(list((root / "pool" / "generations").rglob("userdata-qemu.img")))
            finally:
                for client in clients:
                    if client.poll() is None:
                        client.terminate()
                    client.communicate(timeout=15)
                if service.poll() is None:
                    service.terminate()
                service.communicate(timeout=30)

    def test_actual_zipapp_runs_without_checkout_kent_or_pythonpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "resource-lease.pyz"
            built = subprocess.run(
                [sys.executable, str(ROOT / "scripts/build-resource-lease-package"),
                 "--output", str(package)], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stderr)
            with zipfile.ZipFile(package) as archive:
                names = archive.namelist()
                self.assertIn("resourcelease/cli.py", names)
                self.assertIn("resourcelease/backends/android.py", names)
                self.assertFalse(any("workflowkit" in name or "__pycache__" in name for name in names))
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("KENT") and key != "PYTHONPATH"}
            environment["PATH"] = "/usr/bin:/bin"
            result = subprocess.run([sys.executable, "-I", str(package), "--help"],
                                    cwd=root, env=environment, capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("acquire", result.stdout)
            state = root / "absent"
            result = subprocess.run(
                [sys.executable, "-I", str(package), "--root", str(state), "status"],
                cwd=root, env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(state.exists())
            before = package.read_bytes()
            repeated = subprocess.run(
                [sys.executable, str(ROOT / "scripts/build-resource-lease-package"),
                 "--output", str(package)], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(package.read_bytes(), before)
