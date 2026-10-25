from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event, Thread
import tempfile
import time
import unittest
from unittest import mock

from resourcelease.backends import android as android_adapter
from resourcelease.backends.android import AndroidBackend
from resourcelease.model import LeaseError, QuarantineError, Slot
from resourcelease.process import CommandTimeout, ManagedProcess, launch_child, run_command


SYSTEM_IMAGE = "system-images;android-34;google_apis;arm64-v8a"
MAX_LOG_BYTES = 128 * 1024


_FAKE_TOOL = r'''from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

root = Path(__FAKE_ROOT__)
control_path = root / "control.json"
trace_path = root / "calls.jsonl"

def control():
    return json.loads(control_path.read_text(encoding="utf-8"))

def record(tool, args):
    item = {
        "tool": tool,
        "args": args,
        "home": os.environ.get("HOME"),
        "android_user_home": os.environ.get("ANDROID_USER_HOME"),
        "android_avd_home": os.environ.get("ANDROID_AVD_HOME"),
    }
    with trace_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, sort_keys=True) + "\n")

tool = os.environ.get("FAKE_ANDROID_TOOL_NAME", "")
args = sys.argv[1:]
record(tool, args)

if tool == "avdmanager":
    if args[:2] != ["create", "avd"]:
        print("unexpected avdmanager command", file=sys.stderr)
        raise SystemExit(64)
    name = args[args.index("-n") + 1]
    path = Path(args[args.index("-p") + 1])
    path.mkdir(parents=True)
    (path / "config.ini").write_text("avd.ini.encoding=UTF-8\n", encoding="utf-8")
    avd_home = Path(os.environ["ANDROID_AVD_HOME"])
    (avd_home / f"{name}.ini").write_text(f"path={path}\n", encoding="utf-8")
    mode = control().get("avdmanager", "ready")
    if mode == "create-fail":
        print("fixture avdmanager failure", file=sys.stderr)
        raise SystemExit(23)
    if mode == "create-hang":
        (root / "avdmanager-started").write_text(str(os.getpid()), encoding="ascii")
        while True:
            time.sleep(0.05)
    print(f"Created AVD {name}")
    raise SystemExit(0)

if tool == "emulator":
    if control().get("emulator") == "exit":
        print("fixture emulator startup failure", file=sys.stderr)
        raise SystemExit(17)
    if "-ports" not in args:
        print("emulator requires explicit console and adb ports", file=sys.stderr)
        raise SystemExit(64)
    console_port, adb_port = (int(value) for value in args[args.index("-ports") + 1].split(","))
    listeners = []
    marker_dir = root / "running"
    marker_dir.mkdir(exist_ok=True)
    marker = marker_dir / str(console_port)
    try:
        for port in (console_port, adb_port):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            listeners.append(listener)
        marker.write_text(str(os.getpid()), encoding="ascii")
        (root / "emulator-started").write_text(str(os.getpid()), encoding="ascii")
        active = True

        def stop(_signal, _frame):
            global active
            active = False
            for listener in listeners:
                listener.close()
            if marker.exists():
                marker.unlink()
            delay = float(control().get("shutdown_delay", 0))
            if delay:
                time.sleep(delay)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while active:
            time.sleep(0.02)
    except OSError as error:
        print(f"fixture emulator port failure: {error}", file=sys.stderr)
        raise SystemExit(18)
    finally:
        for listener in listeners:
            try:
                listener.close()
            except OSError:
                pass
        if marker.exists():
            marker.unlink()
    raise SystemExit(0)

if tool == "adb":
    if len(args) < 3 or args[0] != "-s":
        print("adb must target one exact serial", file=sys.stderr)
        raise SystemExit(64)
    serial = args[1]
    match = __import__("re").fullmatch(r"emulator-([0-9]+)", serial)
    if not match:
        print("invalid fake emulator serial", file=sys.stderr)
        raise SystemExit(64)
    running = (root / "running" / match.group(1)).exists()
    if args[2:] == ["get-state"]:
        if running:
            print("device")
            raise SystemExit(0)
        print("offline")
        raise SystemExit(1)
    if args[2:] == ["shell", "getprop", "sys.boot_completed"]:
        print("1" if running and control().get("boot_completed", True) else "0")
        raise SystemExit(0 if running else 1)
    print("unsupported fake adb command", file=sys.stderr)
    raise SystemExit(64)

print(f"unexpected fake SDK tool: {tool}", file=sys.stderr)
raise SystemExit(64)
'''


@dataclass
class FakeAndroidSDK:
    root: Path
    sdk_root: Path
    java_home: Path
    control_path: Path
    trace_path: Path
    system_image: str = SYSTEM_IMAGE

    def options(
        self,
        console_port: int,
        *,
        memory_mb: int = 2048,
        boot_timeout: float = 5,
    ) -> dict[str, object]:
        return {
            "sdk_root": str(self.sdk_root),
            "java_home": str(self.java_home),
            "system_image": self.system_image,
            "console_port": console_port,
            "memory_mb": memory_mb,
            "boot_timeout": boot_timeout,
        }

    def configure(self, **values: object) -> None:
        settings = json.loads(self.control_path.read_text(encoding="utf-8"))
        settings.update(values)
        self.control_path.write_text(json.dumps(settings), encoding="utf-8")

    def calls(self, tool: str | None = None) -> list[dict[str, object]]:
        if not self.trace_path.exists():
            return []
        records = [json.loads(line) for line in self.trace_path.read_text().splitlines()]
        return [record for record in records if tool is None or record["tool"] == tool]


def make_fake_android_sdk(root: Path, *, mode: str = "ready") -> FakeAndroidSDK:
    """Build a deterministic fake installed SDK usable by direct and CLI tests."""
    root = Path(root).resolve()
    sdk_root = root / "sdk"
    java_home = root / "java"
    avdmanager = sdk_root / "cmdline-tools" / "latest" / "bin" / "avdmanager"
    emulator = sdk_root / "emulator" / "emulator"
    adb = sdk_root / "platform-tools" / "adb"
    image = sdk_root / "system-images" / "android-34" / "google_apis" / "arm64-v8a" / "system.img"
    for path in (avdmanager, emulator, adb, java_home / "bin" / "java"):
        path.parent.mkdir(parents=True, exist_ok=True)
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"deterministic fake installed system image")
    control_path = root / "control.json"
    control_path.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    trace_path = root / "calls.jsonl"
    driver_path = root / "fake_android_tool.py"
    driver_path.write_text(
        _FAKE_TOOL.replace("__FAKE_ROOT__", repr(str(root))),
        encoding="utf-8",
    )
    for executable in (avdmanager, emulator, adb):
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, runpy\n"
            "os.environ['FAKE_ANDROID_TOOL_NAME'] = pathlib.Path(__file__).name\n"
            f"runpy.run_path({str(driver_path)!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
    java = java_home / "bin" / "java"
    java.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    java.chmod(0o700)
    return FakeAndroidSDK(root, sdk_root, java_home, control_path, trace_path)


def free_console_port() -> int:
    for port in range(5554, 5683, 2):
        sockets = []
        try:
            for candidate in (port, port + 1):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(probe)
                probe.bind(("127.0.0.1", candidate))
            if socket.has_ipv6:
                for candidate in (port, port + 1):
                    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                    sockets.append(probe)
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    probe.bind(("::", candidate))
            return port
        except OSError:
            continue
        finally:
            for probe in sockets:
                probe.close()
    raise RuntimeError("no free Android emulator console/ADB port pair")


class ProcessHelperTests(unittest.TestCase):
    def test_real_python_child_output_and_log_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "python.log"
            result = run_command(
                [sys.executable, "-c", "print('x' * 1000000)"],
                env=os.environ.copy(),
                timeout=5,
                log_path=log,
            )
            self.assertEqual(result.returncode, 0)
            self.assertLessEqual(len(result.output.encode()), 32 * 1024)
            self.assertLessEqual(log.stat().st_size, MAX_LOG_BYTES)

    def test_real_python_command_timeout_reaps_its_direct_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "pid"
            log = root / "timeout.log"
            code = (
                "import os,time; "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(30)"
            )
            with self.assertRaises(CommandTimeout):
                run_command(
                    [sys.executable, "-c", code],
                    env=os.environ.copy(),
                    timeout=0.2,
                    log_path=log,
                )
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_real_python_live_direct_child_stops_without_pid_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "pid"
            code = (
                "import os,time; "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(30)"
            )
            child = launch_child(
                [sys.executable, "-c", code],
                env=os.environ.copy(),
                log_path=root / "child.log",
            )
            deadline = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text())
            child.stop(grace=1, kill_timeout=1)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


class AndroidBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = make_fake_android_sdk(self.root / "fake-sdk")
        self.backend = AndroidBackend()
        try:
            self.port = free_console_port()
        except RuntimeError as error:
            self.skipTest(str(error))

    def slot(self, *, port: int | None = None, **overrides) -> Slot:
        options = self.sdk.options(port or self.port, **overrides)
        return Slot("android-test", ("android-api-34",), "android", options)

    def generation(self) -> Path:
        generation = self.root / "generation"
        generation.mkdir(mode=0o700)
        return generation

    def assert_private_home_removed(self, generation: Path) -> None:
        self.assertFalse((generation / "android-home").exists())

    def test_keys_reserve_both_explicit_ports(self):
        self.assertEqual(
            self.backend.keys(self.slot()),
            (f"tcp-port:{self.port}", f"tcp-port:{self.port + 1}"),
        )

    def test_invalid_options_are_rejected_before_start(self):
        cases = [
            {"console_port": self.port + 1},
            {"console_port": True},
            {"memory_mb": 0},
            {"memory_mb": True},
            {"boot_timeout": float("nan")},
            {"system_image": "system-images;../../bad"},
            {"unapproved_option": "value"},
        ]
        for override in cases:
            with self.subTest(override=override):
                options = self.sdk.options(self.port)
                options.update(override)
                slot = Slot("android-test", ("android-api-34",), "android", options)
                with self.assertRaises(LeaseError):
                    self.backend.keys(slot)
        options = self.sdk.options(self.port)
        del options["sdk_root"]
        with self.assertRaises(LeaseError):
            self.backend.keys(Slot("android-test", ("android-api-34",), "android", options))

    def test_missing_installed_tool_or_system_image_is_rejected(self):
        options = self.sdk.options(self.port)
        (self.sdk.sdk_root / "platform-tools" / "adb").unlink()
        with self.assertRaises(LeaseError):
            self.backend.keys(Slot("android-test", ("test",), "android", options))
        self.assertEqual(self.sdk.calls(), [])

    def test_preexisting_private_home_is_preserved_on_collision(self):
        generation = self.generation()
        private_home = generation / "android-home"
        private_home.mkdir(mode=0o700)
        sentinel = private_home / "must-survive.txt"
        sentinel.write_text("foreign generation", encoding="utf-8")
        with self.assertRaises(LeaseError):
            self.backend.start(self.slot(), generation, Event())
        self.assertTrue(sentinel.exists(), "failed exclusive create deleted foreign data")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "foreign generation")
        self.assertEqual(self.sdk.calls(), [])

    def test_start_quarantine_stops_known_child_but_preserves_uncertain_generation(self):
        generation = self.generation()
        captured: list[object] = []

        def quarantine_after_spawn(*args):
            child = args[1]
            captured.append(child)
            self.addCleanup(child.stop)
            marker = self.root / "fake-sdk" / "emulator-started"
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "fake emulator did not reach direct-child state")
            raise QuarantineError("injected uncertainty after emulator launch")

        with mock.patch.object(self.backend, "_wait_ready", side_effect=quarantine_after_spawn):
            with self.assertRaisesRegex(QuarantineError, "injected uncertainty"):
                self.backend.start(self.slot(), generation, Event())
        self.assertTrue(captured)
        self.assertIsNotNone(captured[0].poll())
        self.assertTrue((generation / "android-home").exists())
        self.assertTrue(all(self._port_free(port) for port in (self.port, self.port + 1)))

    def test_start_quarantine_keeps_original_failure_if_child_stop_is_uncertain(self):
        generation = self.generation()
        captured: list[ManagedProcess] = []
        original_stop = ManagedProcess.stop

        def quarantine_after_spawn(*args):
            child = args[1]
            captured.append(child)
            self.addCleanup(lambda child=child: original_stop(child))
            marker = self.root / "fake-sdk" / "emulator-started"
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "fake emulator did not reach direct-child state")
            raise QuarantineError("original readiness uncertainty")

        with mock.patch.object(
            ManagedProcess, "stop",
            side_effect=QuarantineError("injected child-stop uncertainty"),
        ):
            with mock.patch.object(
                self.backend, "_wait_ready", side_effect=quarantine_after_spawn,
            ):
                with self.assertRaisesRegex(QuarantineError, "original readiness uncertainty"):
                    self.backend.start(self.slot(), generation, Event())

        self.assertEqual(len(captured), 1)
        self.assertIsNone(captured[0].poll())
        private_home = generation / "android-home"
        self.assertTrue((private_home / ".resource-lease-owner").exists())
        self.assertTrue(private_home.exists())

    def test_oversized_private_home_marker_is_rejected_without_reading_contents(self):
        generation = self.generation()
        private_home = generation / "android-home"
        private_home.mkdir(mode=0o700)
        marker_path = private_home / ".resource-lease-owner"
        marker_path.write_bytes(b"x" * (1024 * 1024))

        with mock.patch.object(
            Path, "read_text", side_effect=AssertionError("unbounded marker read")
        ) as read_text:
            with self.assertRaisesRegex(QuarantineError, "ownership marker changed"):
                android_adapter._remove_private_home(
                    generation.resolve(strict=True), private_home, "a" * 32,
                )
        read_text.assert_not_called()
        self.assertTrue(marker_path.exists(), "uncertain private data was deleted")

    def test_fresh_avd_is_ready_then_scoped_stop_leaves_only_bounded_logs(self):
        generation = self.generation()
        resource = self.backend.start(self.slot(), generation, Event())
        self.addCleanup(resource.stop)
        self.assertEqual(
            resource.endpoint,
            {
                "adb_serial": f"emulator-{self.port}",
                "console_port": str(self.port),
                "adb_port": str(self.port + 1),
            },
        )
        self.assertTrue(resource.healthy())
        avd_call = self.sdk.calls("avdmanager")[0]
        avd_args = avd_call["args"]
        self.assertEqual(avd_args[:3], ["create", "avd", "-n"])
        self.assertIn("-p", avd_args)
        private_home = Path(str(avd_call["android_user_home"])).resolve()
        self.assertTrue(private_home.is_relative_to(generation.resolve()))
        self.assertEqual(avd_call["home"], str(Path.home()))
        avd_name = avd_args[avd_args.index("-n") + 1]
        avd_path = Path(avd_args[avd_args.index("-p") + 1])
        self.assertTrue((avd_path / "config.ini").is_file())
        self.assertRegex(avd_name, r"^resource_lease_[a-f0-9]{16}$")

        emulator_call = self.sdk.calls("emulator")[0]
        emulator_args = emulator_call["args"]
        self.assertEqual(
            emulator_args[emulator_args.index("-ports") + 1],
            f"{self.port},{self.port + 1}",
        )
        for flag in (
            "-no-window", "-no-snapshot", "-no-audio", "-no-boot-anim",
            "-no-metrics", "-crash-report-mode", "-feature",
        ):
            self.assertIn(flag, emulator_args)
        for feature in ("-BluetoothEmulation", "-Uwb", "-Nfc", "-WiFiPacketStream"):
            self.assertIn(feature, emulator_args)

        adb_calls = self.sdk.calls("adb")
        self.assertTrue(adb_calls)
        self.assertTrue(all(call["args"][:2] == ["-s", f"emulator-{self.port}"]
                            for call in adb_calls))
        resource.stop()
        self.assertFalse(resource.healthy())
        self.assert_private_home_removed(generation)
        self.assertEqual(
            {path.name for path in generation.iterdir()},
            {"android-avdmanager.log", "android-emulator.log", "android-adb.log"},
        )
        for log in generation.iterdir():
            self.assertLessEqual(log.stat().st_size, MAX_LOG_BYTES)
        resource.stop()  # Proven teardown remains idempotent.

    def test_avdmanager_failure_cleans_partial_sdk_generation(self):
        self.sdk.configure(avdmanager="create-fail")
        generation = self.generation()
        with self.assertRaisesRegex(LeaseError, "fixture avdmanager failure"):
            self.backend.start(self.slot(), generation, Event())
        self.assert_private_home_removed(generation)
        self.assertEqual([call["tool"] for call in self.sdk.calls()], ["avdmanager"])
        self.assertEqual({path.name for path in generation.iterdir()},
                         {"android-avdmanager.log"})
        self.assertLessEqual(
            (generation / "android-avdmanager.log").stat().st_size, MAX_LOG_BYTES,
        )

    def test_boot_timeout_stops_owned_child_before_cleaning_avd(self):
        self.sdk.configure(boot_completed=False)
        generation = self.generation()
        with self.assertRaisesRegex(LeaseError, "did not boot"):
            self.backend.start(
                self.slot(boot_timeout=1),
                generation,
                Event(),
            )
        self.assert_private_home_removed(generation)
        self.assertTrue(all(self._port_free(port) for port in (self.port, self.port + 1)))
        self.assertTrue((self.root / "fake-sdk" / "running" / str(self.port)).parent.is_dir())
        self.assertFalse((self.root / "fake-sdk" / "running" / str(self.port)).exists())
        self.assertTrue((generation / "android-emulator.log").is_file())
        self.assertTrue((generation / "android-adb.log").is_file())

    def test_cancellation_during_avd_creation_reaps_create_child_and_cleans(self):
        self.sdk.configure(avdmanager="create-hang")
        generation = self.generation()
        cancelled = Event()
        started = Event()

        def cancel_after_create_starts():
            deadline = time.monotonic() + 3
            marker = self.root / "fake-sdk" / "avdmanager-started"
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.01)
            started.set()
            cancelled.set()

        thread = Thread(target=cancel_after_create_starts)
        thread.start()
        try:
            with self.assertRaisesRegex(LeaseError, "cancelled"):
                self.backend.start(self.slot(), generation, cancelled)
        finally:
            thread.join(timeout=4)
        self.assertTrue(started.is_set())
        pid = int((self.root / "fake-sdk" / "avdmanager-started").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assert_private_home_removed(generation)
        self.assertEqual([call["tool"] for call in self.sdk.calls()], ["avdmanager"])

    def test_cancellation_during_boot_reaps_emulator_and_cleans_generation(self):
        self.sdk.configure(boot_completed=False)
        generation = self.generation()
        cancelled = Event()
        started = Event()

        def cancel_after_emulator_starts():
            deadline = time.monotonic() + 4
            marker = self.root / "fake-sdk" / "emulator-started"
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.01)
            started.set()
            cancelled.set()

        thread = Thread(target=cancel_after_emulator_starts)
        thread.start()
        try:
            with self.assertRaisesRegex(LeaseError, "cancelled"):
                self.backend.start(self.slot(boot_timeout=20), generation, cancelled)
        finally:
            thread.join(timeout=5)
        self.assertTrue(started.is_set())
        pid = int((self.root / "fake-sdk" / "emulator-started").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assert_private_home_removed(generation)
        self.assertTrue(all(self._port_free(port) for port in (self.port, self.port + 1)))

    def test_foreign_port_is_not_stopped_or_adopted(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(1)
        generation = self.generation()
        with self.assertRaisesRegex(LeaseError, "already in use"):
            self.backend.start(self.slot(), generation, Event())
        self.assertTrue(self.sdk.calls() == [])
        self.assertTrue(listener.fileno() >= 0)
        self.assertEqual(list(generation.iterdir()), [])

    def test_time_wait_with_reuse_enabled_can_be_rebound(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        accepted = None
        try:
            # Linux requires reuse on both the old and the new socket.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(1)
            client.connect(("127.0.0.1", self.port))
            accepted, _ = listener.accept()
            accepted.close()
            accepted = None
            self.assertEqual(client.recv(1), b"")
        finally:
            if accepted is not None:
                accepted.close()
            client.close()
            listener.close()
        time.sleep(0.05)
        self.assertTrue(
            android_adapter._port_bindable(self.port),
            "TIME_WAIT was mistaken for a live listener",
        )

    def test_time_wait_without_reuse_respects_kernel_bindability(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.bind(("127.0.0.1", self.port))
            listener.listen(1)
            client.connect(("127.0.0.1", self.port))
            accepted, _ = listener.accept()
            accepted.close()
            self.assertEqual(client.recv(1), b"")
        # BSD may allow reuse here; Linux keeps this non-reusable socket reserved.
        # The backend must never call a kernel-reserved port available.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", self.port))
                probe.listen(1)
            except OSError as error:
                self.assertEqual(error.errno, errno.EADDRINUSE)
                bindable = False
            else:
                bindable = True
        self.assertEqual(android_adapter._port_bindable(self.port), bindable)

    def test_foreign_port_after_owned_child_exit_quarantines_until_free(self):
        self.sdk.configure(shutdown_delay=0.8)
        generation = self.generation()
        resource = self.backend.start(self.slot(), generation, Event())
        self.addCleanup(resource.stop)
        foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        acquired = Event()
        done = Event()

        def take_released_port():
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not done.is_set():
                try:
                    foreign.bind(("127.0.0.1", self.port))
                    foreign.listen(1)
                    acquired.set()
                    done.wait(4)
                    return
                except OSError:
                    time.sleep(0.01)

        thread = Thread(target=take_released_port, daemon=True)
        thread.start()
        try:
            with mock.patch(
                "resourcelease.backends.android.PORT_RELEASE_TIMEOUT", 0.5
            ):
                with self.assertRaises(QuarantineError):
                    resource.stop()
            self.assertTrue(acquired.wait(2), "foreign listener did not claim released port")
            self.assertTrue((generation / "android-home").exists())
        finally:
            done.set()
            foreign.close()
            thread.join(timeout=2)
        resource.stop()
        self.assert_private_home_removed(generation)
        self.assertFalse(resource.healthy())

    @staticmethod
    def _port_free(port: int) -> bool:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            probe.close()


if __name__ == "__main__":
    unittest.main()
