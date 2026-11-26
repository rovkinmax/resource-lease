from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from resourcelease.admission import Admission
from resourcelease.android_cli import main
from test_resource_lease_android import make_fake_android_sdk


PACKAGE = "system-images;android-36.1;google_apis_playstore;arm64-v8a"


class AndroidCLITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.sdk = make_fake_android_sdk(self.home / "fixture")
        path = self.sdk.sdk_root.joinpath(*PACKAGE.split(";"))
        path.mkdir(parents=True)
        (path / "system.img").write_bytes(b"exact 36.1 image")
        (path / "package.xml").write_text(
            f'<repository><localPackage path="{PACKAGE}"><revision><major>4</major>'
            "</revision></localPackage></repository>",
        )
        self.root = self.home / ".local/state/resource-lease/android-host"
        self.assertEqual(main([
            "configure", "--sdk", str(self.sdk.sdk_root), "--java-home", str(self.sdk.java_home),
        ], home=self.home), 0)

    def test_list_and_two_disposable_runs_never_reuse_avd_or_caller_adb(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["list"], home=self.home), 0)
        self.assertEqual(output.getvalue().splitlines(), [PACKAGE])
        self.assertTrue(not self.sdk.calls())

        result = self.home / "payload.json"
        payload = (
            "import json, os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps("
            "{k: os.environ.get(k) for k in ['ADB_SERVER_SOCKET','ANDROID_SERIAL','ADBHOST']}"
            ")); raise SystemExit(3)"
        )
        with patch.dict("os.environ", {
            "ADB_SERVER_SOCKET": "tcp:127.0.0.1:5037", "ADBHOST": "invalid",
            "ANDROID_SERIAL": "foreign", "RESOURCE_LEASE_HOME": "/not-the-authority",
        }):
            for _ in range(2):
                self.assertEqual(main([
                    "run", "--image", PACKAGE, "--", sys.executable, "-c", payload, str(result),
                ], home=self.home), 3)
                endpoint = json.loads(result.read_text())
                self.assertNotEqual(endpoint["ADB_SERVER_SOCKET"], "tcp:127.0.0.1:5037")
                self.assertRegex(endpoint["ANDROID_SERIAL"], r"^emulator-")
                self.assertIsNone(endpoint["ADBHOST"])
                self.assertEqual(list((self.root / "generations").iterdir()), [])
                self.assertEqual(list((self.sdk.root / "running").iterdir()), [])

        launches = [call["args"] for call in self.sdk.calls("avdmanager")]
        self.assertEqual(len(launches), 2)
        self.assertNotEqual(launches[0][3], launches[1][3])
        self.assertEqual([args[args.index("-k") + 1] for args in launches], [PACKAGE, PACKAGE])
        self.assertTrue(all("-port" in call["args"] and "-ports" not in call["args"]
                            for call in self.sdk.calls("emulator")))

    def test_missing_image_starts_nothing(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(main(["run", "--image", PACKAGE.replace("36.1", "36"), "--", "true"],
                                  home=self.home), 1)
        self.assertIn("not installed", errors.getvalue())
        self.assertEqual(self.sdk.calls(), [])

    def spawn(self, name):
        ready = self.home / (name + ".ready")
        release = self.home / (name + ".release")
        subprocess_code = (
            "import sys; from pathlib import Path; from resourcelease.android_cli import main;"
            "raise SystemExit(main(sys.argv[2:], home=Path(sys.argv[1])))"
        )
        job = """
from pathlib import Path
import sys, time
Path(sys.argv[1]).touch()
while not Path(sys.argv[2]).exists():
    time.sleep(0.025)
"""
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", subprocess_code, str(self.home), "run", "--image",
             PACKAGE, "--", sys.executable, "-c", job, str(ready), str(release)],
            env={**os.environ, "HOME": str(self.home / name),
                 "RESOURCE_LEASE_HOME": str(self.home / (name + "-not-authority"))},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.settle, process)
        return ready, release, process

    @staticmethod
    def settle(process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=20)

    def await_result(self, predicate, description):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        self.fail(description)

    def counts(self):
        with Admission(self.root)._transaction(write=False) as connection:
            return dict(connection.execute("SELECT state,count(*) FROM requests GROUP BY state"))

    def test_processes_share_limit_and_cancel_waiting_owner_without_start(self):
        owners = [self.spawn(f"runner-{i}") for i in range(3)]
        self.await_result(lambda: all(ready.exists() for ready, _, _ in owners),
                          "first three fake emulators did not become ready")
        first = self.spawn("first-waiter")
        self.await_result(lambda: self.counts() == {"admitted": 3, "waiting": 1},
                          "fourth request did not wait")
        second = self.spawn("second-waiter")
        self.await_result(lambda: self.counts() == {"admitted": 3, "waiting": 2},
                          "second waiter did not join the queue")
        self.assertEqual(len(self.sdk.calls("emulator")), 3)
        self.assertFalse(first[0].exists() or second[0].exists())

        second[2].send_signal(signal.SIGINT)
        self.assertEqual(second[2].wait(timeout=5), 130)
        self.assertEqual(self.counts(), {"admitted": 3, "waiting": 1})
        owners[0][1].touch()
        self.assertEqual(owners[0][2].wait(timeout=8), 0)
        self.await_result(first[0].exists, "oldest waiter was not promoted")
        self.assertEqual(self.counts(), {"admitted": 3})
        self.assertEqual(len(self.sdk.calls("emulator")), 4)
        self.assertFalse(second[0].exists())
        for _, release, process in owners[1:] + [first]:
            release.touch()
            self.assertEqual(process.wait(timeout=8), 0)
        self.assertEqual(self.counts(), {})
        self.assertEqual(list((self.root / "generations").iterdir()), [])
