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
from resourcelease.model import Config, LeaseError, QuarantineError, Slot
from resourcelease.process import (
    CommandResult, CommandTimeout, ManagedProcess, launch_child, run_command,
)
from resourcelease.store import Store


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
        "tmpdir": os.environ.get("TMPDIR"),
        "android_tmp": os.environ.get("ANDROID_TMP"),
        "feature_override": os.environ.get("ANDROID_EMULATOR_FEATURES"),
        "netsim_overrides": sorted(key for key in os.environ if key.startswith("NETSIM_")),
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

if tool in ("emulator", "netsimd"):
    if control().get(tool) == "exit":
        print(f"fixture {tool} startup failure", file=sys.stderr)
        raise SystemExit(17)
    if tool == "emulator":
        console_port, adb_port = (int(value) for value in args[args.index("-ports") + 1].split(","))
        ports = (console_port, adb_port)
    else:
        console_port = int(args[args.index("--grpc-port") + 1])
        ports = (console_port, int(args[args.index("--hci-port") + 1]))
        if control().get("netsimd") == "no-endpoint":
            ports = ()
    listeners = []
    marker_dir = root / "running"
    marker_dir.mkdir(exist_ok=True)
    marker = marker_dir / str(console_port)
    try:
        for port in ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            listeners.append(listener)
        marker.write_text(str(os.getpid()), encoding="ascii")
        (root / f"{tool}-started").write_text(str(os.getpid()), encoding="ascii")
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
            if control().get(tool) == "die":
                break
            if (tool == "netsimd" and control().get("netsimd") == "no-clients"
                    and (root / "trigger-netsim-timeout").exists()):
                retained = "--no-shutdown" in args
                (root / "netsim-timeout-result").write_text(
                    "retained" if retained else "expired", encoding="ascii",
                )
                if not retained:
                    break
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
    if args[2:5] == ["shell", "sh", "-c"] and args[-4] == "resource-lease-ready":
        settings = control()
        (root / "readiness-started").touch()
        if settings.get("readiness") == "hang":
            while True:
                time.sleep(0.02)
        counter = root / "readiness-checks"
        checks = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(checks))
        if not settings.get("package_manager", True):
            print("Package Manager not ready")
            raise SystemExit(1)
        if not settings.get("storage_mounted", True) or checks <= settings.get("storage_delay", 0):
            print("Primary storage not mounted")
            raise SystemExit(1)
        if args[-2] == "1" and not settings.get("download_ready", True):
            print("Download not ready")
            raise SystemExit(1)
        if not settings.get("storage_io", True):
            print("Storage write/read/delete probe failed")
            raise SystemExit(1)
        raise SystemExit(0)
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
    netsimd = sdk_root / "emulator" / "netsimd"
    adb = sdk_root / "platform-tools" / "adb"
    image = sdk_root / "system-images" / "android-34" / "google_apis" / "arm64-v8a" / "system.img"
    for path in (avdmanager, emulator, adb, java_home / "bin" / "java"):
        path.parent.mkdir(parents=True, exist_ok=True)
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"deterministic fake installed system image")
    (image.parent / "source.properties").write_text(
        "Pkg.Revision=1.0.0\n", encoding="utf-8",
    )
    control_path = root / "control.json"
    control_path.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    trace_path = root / "calls.jsonl"
    driver_path = root / "fake_android_tool.py"
    driver_path.write_text(
        _FAKE_TOOL.replace("__FAKE_ROOT__", repr(str(root))),
        encoding="utf-8",
    )
    for executable in (avdmanager, emulator, adb, netsimd):
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


def free_console_port(start: int = 5554) -> int:
    for port in range(start, 5683, 2):
        sockets = []
        try:
            for candidate in (port, port + 1, port + 10000, port + 20000):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(probe)
                probe.bind(("127.0.0.1", candidate))
            if socket.has_ipv6:
                for candidate in (port, port + 1, port + 10000, port + 20000):
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

    def persistent_slot(
        self,
        state_directory: Path,
        *,
        port: int | None = None,
        **overrides,
    ) -> Slot:
        options = self.sdk.options(port or self.port, **overrides)
        options.update(
            state_mode="persistent",
            state_directory=str(state_directory),
        )
        return Slot("android-test", ("android-api-34",), "android", options)

    def generation(self) -> Path:
        generation = self.root / "generation"
        generation.mkdir(mode=0o700)
        return generation

    def assert_private_home_removed(self, generation: Path) -> None:
        self.assertFalse((generation / "android-home").exists())

    def test_default_capabilities_use_owned_transport_without_feature_overrides(self):
        resource = self.backend.start(self.slot(), self.generation(), Event())
        self.addCleanup(resource.stop)
        arguments = self.sdk.calls("emulator")[0]["args"]
        self.assertNotIn("-feature", arguments)
        self.assertNotIn("-no-audio", arguments)
        self.assertIn("-no-boot-anim", arguments)
        self.assertEqual(len(self.sdk.calls("netsimd")), 1)
        self.assertEqual(
            arguments[arguments.index("-packet-streamer-endpoint") + 1],
            f"127.0.0.1:{self.port + 10000}",
        )

    def test_boot_complete_does_not_publish_unready_storage(self):
        for index, settings in enumerate((
            {"package_manager": False},
            {"storage_mounted": False},
            {"download_ready": False},
            {"storage_io": False},
        )):
            with self.subTest(settings=settings):
                self.sdk.configure(
                    package_manager=True, storage_mounted=True,
                    download_ready=True, storage_io=True,
                )
                self.sdk.configure(**settings)
                generation = self.root / f"unready-{index}"
                generation.mkdir(mode=0o700)
                resource = None
                try:
                    with self.assertRaisesRegex(LeaseError, "boot|ready"):
                        resource = self.backend.start(
                            self.slot(boot_timeout=1), generation, Event(),
                        )
                        self.addCleanup(resource.stop)
                finally:
                    if resource is not None:
                        resource.stop()
                self.assert_private_home_removed(generation)

    def test_start_waits_for_storage_without_an_additional_deadline(self):
        self.sdk.configure(storage_delay=2)
        resource = self.backend.start(self.slot(), self.generation(), Event())
        self.addCleanup(resource.stop)
        self.assertEqual((self.sdk.root / "readiness-checks").read_text(), "3")
        checks = [
            call for call in self.sdk.calls("adb")
            if call["args"][2:5] == ["shell", "sh", "-c"]
        ]
        self.assertEqual(len(checks), 3)
        self.assertEqual({call["args"][1] for call in checks}, {f"emulator-{self.port}"})
        self.assertEqual({call["args"][-2] for call in checks}, {"1"})
        self.assertEqual(len({call["args"][-1] for call in checks}), 1)

    def test_persistent_acquisition_does_not_require_download(self):
        self.sdk.configure(download_ready=False)
        resource = self.backend.start(
            self.persistent_slot(self.root / "persistent-state"),
            self.generation(), Event(),
        )
        self.addCleanup(resource.stop)
        checks = [
            call for call in self.sdk.calls("adb")
            if call["args"][2:5] == ["shell", "sh", "-c"]
        ]
        self.assertTrue(checks, "Acquisition must check storage even in persistent mode")
        self.assertEqual({call["args"][-2] for call in checks}, {"0"})

    def test_download_is_not_an_ongoing_health_requirement(self):
        resource = self.backend.start(self.slot(), self.generation(), Event())
        self.addCleanup(resource.stop)
        checks = (self.sdk.root / "readiness-checks").read_text()
        self.sdk.configure(download_ready=False, storage_io=False)
        self.assertTrue(resource.healthy())
        self.assertEqual((self.sdk.root / "readiness-checks").read_text(), checks)

    def test_storage_probe_timeout_exhausts_original_boot_deadline(self):
        options = android_adapter._parse_options(self.slot(boot_timeout=1))
        child = mock.Mock()
        child.poll.return_value = None
        transport = mock.Mock()
        transport.poll.return_value = None
        log = self.root / "adb.log"
        now = [0.0]
        clock = mock.Mock(wraps=time)
        clock.monotonic.side_effect = lambda: now[0]
        timeouts = []

        def observe(arguments, **kwargs):
            timeouts.append(kwargs["timeout"])
            if arguments[3:] == ["get-state"]:
                now[0] = 0.25
                return CommandResult(0, "device", log)
            if arguments[3:] == ["shell", "getprop", "sys.boot_completed"]:
                now[0] = 0.5
                return CommandResult(0, "1", log)
            self.assertEqual(arguments[3:6], ["shell", "sh", "-c"])
            now[0] = 1.0
            raise CommandTimeout("injected storage command timeout")

        # Advance the adapter clock at each command boundary. Slow fake-SDK
        # interpreter startup must not consume the budget before this test's
        # storage phase; real command reaping and startup cleanup are tested separately.
        with mock.patch.object(android_adapter, "time", clock):
            with mock.patch.object(android_adapter, "run_command", side_effect=observe):
                with self.assertRaisesRegex(LeaseError, "did not boot within 1s"):
                    self.backend._wait_ready(options, child, {}, log, Event(), transport, 1.0)
        self.assertEqual(timeouts, [1.0, 0.75, 0.5])

    def test_storage_probe_cancellation_preserves_persistent_initialization_and_reuse(self):
        for mode in ("clean", "persistent-new", "persistent-reused"):
            with self.subTest(mode=mode):
                self.sdk.configure(readiness="ready")
                state = self.root / mode
                slot = (
                    self.slot(boot_timeout=10) if mode == "clean"
                    else self.persistent_slot(state, boot_timeout=10)
                )
                manifest = state / ".resource-lease-android-state.json"
                manifest_before = None
                sentinel = None
                if mode == "persistent-reused":
                    initial = self.root / "initial-generation"
                    initial.mkdir(mode=0o700)
                    resource = self.backend.start(slot, initial, Event())
                    self.addCleanup(resource.stop)
                    call = self.sdk.calls("avdmanager")[-1]["args"]
                    sentinel = Path(call[call.index("-p") + 1]) / "userdata-qemu.img"
                    sentinel.write_bytes(b"keep persistent data")
                    resource.stop()
                    manifest_before = manifest.read_bytes()
                marker = self.sdk.root / "readiness-started"
                marker.unlink(missing_ok=True)
                self.sdk.configure(readiness="hang")
                cancelled = Event()

                def cancel_during_probe():
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        if cancelled.wait(0.01):
                            return
                    cancelled.set()

                thread = Thread(target=cancel_during_probe)
                thread.start()
                self.addCleanup(thread.join, 5)
                self.addCleanup(cancelled.set)
                generation = self.root / f"{mode}-generation"
                generation.mkdir(mode=0o700)
                try:
                    with self.assertRaisesRegex(LeaseError, "cancelled"):
                        self.backend.start(slot, generation, cancelled)
                finally:
                    cancelled.set()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(marker.exists())
                self.assert_private_home_removed(generation)
                self.assertEqual(list((self.sdk.root / "running").iterdir()), [])
                if mode == "persistent-new":
                    self.assertEqual(json.loads(manifest.read_text())["status"], "initializing")
                elif mode == "persistent-reused":
                    self.assertEqual(manifest.read_bytes(), manifest_before)
                    self.assertEqual(sentinel.read_bytes(), b"keep persistent data")

    def test_transport_environment_and_launch_order(self):
        with mock.patch.dict(os.environ, {
            "ANDROID_EMULATOR_FEATURES": "-BluetoothEmulation",
            "NETSIM_GRPC_PORT": "12345", "NETSIM_INSTANCE": "999",
        }):
            resource = self.backend.start(self.slot(), self.generation(), Event())
        self.addCleanup(resource.stop)
        calls = [call for call in self.sdk.calls() if call["tool"] != "adb"]
        self.assertEqual([call["tool"] for call in calls], ["avdmanager", "netsimd", "emulator"])
        for call in calls[1:3]:
            self.assertIsNone(call["feature_override"])
            self.assertEqual(call["netsim_overrides"], [])
            self.assertEqual(call["tmpdir"], call["android_tmp"])
            self.assertEqual(
                Path(call["tmpdir"]), (self.root / "generation/android-home/netsim-tmp").resolve(),
            )
        self.assertEqual(calls[0]["feature_override"], "-BluetoothEmulation")
        self.assertEqual(calls[1]["args"], [
            "--instance", str(self.port),
            "--grpc-port", str(self.port + 10000), "--hci-port", str(self.port + 20000),
            "--logtostderr", "--no-cli-ui", "--no-web-ui", "--no-test-beacons",
            "--no-shutdown",
        ])

    def test_missing_transport_is_rejected_before_any_child_launch(self):
        (self.sdk.sdk_root / "emulator/netsimd").unlink()
        with self.assertRaisesRegex(LeaseError, "netsimd"):
            self.backend.start(self.slot(), self.generation(), Event())
        self.assertEqual(self.sdk.calls(), [])

    def test_foreign_transport_ports_are_not_adopted(self):
        generation = self.generation()
        for port in (self.port + 10000, self.port + 20000):
            with self.subTest(port=port), socket.socket() as listener:
                listener.bind(("127.0.0.1", port))
                listener.listen()
                with self.assertRaisesRegex(LeaseError, "already in use"):
                    self.backend.start(self.slot(), generation, Event())
                self.assertNotEqual(listener.fileno(), -1)
        self.assertEqual(self.sdk.calls(), [])

    def test_transport_and_emulator_start_failures_reap_every_child(self):
        for tool in ("netsimd", "emulator"):
            with self.subTest(tool=tool):
                generation = self.root / f"failure-{tool}"
                generation.mkdir()
                self.sdk.configure(netsimd="ready", emulator="ready")
                self.sdk.configure(**{tool: "exit"})
                with self.assertRaises(LeaseError):
                    self.backend.start(self.slot(), generation, Event())
                self.assert_private_home_removed(generation)
                self.assertTrue(all(android_adapter._port_bindable(port) for port in
                                    (self.port, self.port + 1, self.port + 10000, self.port + 20000)))

    def test_transport_timeout_prevents_emulator_launch_and_cleans_runtime(self):
        self.sdk.configure(netsimd="no-endpoint")
        generation = self.generation()
        with self.assertRaisesRegex(LeaseError, "netsimd endpoint startup timed out"):
            self.backend.start(self.slot(boot_timeout=1), generation, Event())
        self.assertEqual(self.sdk.calls("emulator"), [])
        self.assert_private_home_removed(generation)

    def test_transport_start_cancellation_reaps_child(self):
        self.sdk.configure(netsimd="no-endpoint")
        cancelled = Event()
        generation = self.generation()
        original_wait = self.backend._wait_transport

        def cancel(options, child, deadline, event):
            self.addCleanup(child.stop)
            event.set()
            return original_wait(options, child, deadline, event)

        with mock.patch.object(self.backend, "_wait_transport", side_effect=cancel):
            with self.assertRaisesRegex(LeaseError, "cancelled"):
                self.backend.start(self.slot(), generation, cancelled)
        self.assertEqual(self.sdk.calls("emulator"), [])
        self.assert_private_home_removed(generation)

    def test_boot_uses_the_transport_deadline_without_reset(self):
        generation = self.generation()
        original_wait = self.backend._wait_transport
        deadlines = []

        def wait(options, child, deadline, cancelled):
            deadlines.append(deadline)
            return original_wait(options, child, deadline, cancelled)

        original_ready = self.backend._wait_ready

        def ready(*args):
            self.assertEqual(args[-1], deadlines[0])
            return original_ready(*args)

        with mock.patch.object(self.backend, "_wait_transport", side_effect=wait), \
                mock.patch.object(self.backend, "_wait_ready", side_effect=ready):
            resource = self.backend.start(self.slot(), generation, Event())
        self.addCleanup(resource.stop)

    def test_transport_death_during_boot_cleans_both_children(self):
        generation = self.generation()
        original_ready = self.backend._wait_ready

        def ready(*args):
            args[-2].stop()
            return original_ready(*args)

        with mock.patch.object(self.backend, "_wait_ready", side_effect=ready):
            with self.assertRaisesRegex(LeaseError, "netsimd exited"):
                self.backend.start(self.slot(), generation, Event())
        self.assert_private_home_removed(generation)

    def test_transport_death_invalidates_cached_health(self):
        resource = self.backend.start(self.slot(), self.generation(), Event())
        self.addCleanup(resource.stop)
        self.assertTrue(resource.healthy())
        resource._netsimd.stop()
        self.assertFalse(resource.healthy())

    def test_unused_transport_lifetime_is_owned_by_lease_not_sdk_timer(self):
        self.sdk.configure(netsimd="no-clients")
        generation = self.generation()
        resource = self.backend.start(self.slot(), generation, Event())
        self.addCleanup(resource.stop)
        (self.sdk.root / "trigger-netsim-timeout").write_text("expire", encoding="ascii")
        result = self.sdk.root / "netsim-timeout-result"
        deadline = time.monotonic() + 3
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(result.exists(), "Fake SDK did not observe its timeout")
        self.assertEqual(result.read_text(encoding="ascii"), "retained")
        self.assertTrue(resource.healthy())
        resource.stop()
        self.assertIsNotNone(resource._netsimd.poll())
        self.assertIsNotNone(resource._child.poll())
        self.assert_private_home_removed(generation)

    def test_failed_emulator_stop_still_stops_transport_and_preserves_runtime(self):
        generation = self.generation()
        resource = self.backend.start(self.slot(), generation, Event())
        self.addCleanup(resource.stop)
        with mock.patch.object(resource._child, "stop", side_effect=QuarantineError("injected stop")):
            with self.assertRaises(QuarantineError):
                resource.stop()
        self.assertIsNotNone(resource._netsimd.poll())
        self.assertTrue((generation / "android-home").exists())
        resource.stop()
        self.assert_private_home_removed(generation)

    def test_transport_port_remaining_after_children_stop_prevents_cleanup(self):
        generation = self.generation()
        resource = self.backend.start(self.slot(), generation, Event())
        self.addCleanup(resource.stop)
        resource._netsimd.stop()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.port + 20000))
            listener.listen()
            with mock.patch.object(android_adapter, "PORT_RELEASE_TIMEOUT", 0):
                with self.assertRaisesRegex(QuarantineError, "ports remain occupied"):
                    resource.stop()
            self.assertTrue((generation / "android-home").exists())
        resource.stop()
        self.assert_private_home_removed(generation)

    def test_keys_reserve_both_explicit_ports(self):
        self.assertEqual(
            self.backend.keys(self.slot()),
            tuple(f"tcp-port:{port}" for port in
                  (self.port, self.port + 1, self.port + 10000, self.port + 20000)),
        )

    def test_persistent_mode_uses_canonical_state_guard_without_creating_state(self):
        self.assertEqual(
            self.backend.keys(self.slot()),
            tuple(f"tcp-port:{port}" for port in
                  (self.port, self.port + 1, self.port + 10000, self.port + 20000)),
        )
        parent = self.root / "persistent-parent"
        parent.mkdir(mode=0o700)
        state_directory = parent / "android-state"
        options = self.sdk.options(self.port)
        options.update(state_mode="persistent", state_directory=str(state_directory))
        slot = Slot("android-persistent", ("android-api-34",), "android", options)

        keys = self.backend.keys(slot)
        state_keys = tuple(key for key in keys if key.startswith("android-state:"))
        self.assertEqual(len(state_keys), 1)
        self.assertFalse(state_directory.exists(), "key discovery must not create persistent state")

        alias = self.root / "persistent-parent-alias"
        alias.symlink_to(parent, target_is_directory=True)
        alias_options = self.sdk.options(self.port)
        alias_options.update(
            state_mode="persistent",
            state_directory=str(alias / "android-state"),
        )
        alias_slot = Slot("android-persistent-alias", ("android-api-34",), "android",
                          alias_options)
        alias_keys = self.backend.keys(alias_slot)
        self.assertEqual(
            state_keys,
            tuple(key for key in alias_keys if key.startswith("android-state:")),
            "different path spellings must guard one canonical AVD state",
        )

    def test_persistent_mode_configuration_is_explicit_and_closed(self):
        options = self.sdk.options(self.port)
        options["state_mode"] = "persistent"
        with self.assertRaisesRegex(LeaseError, "state_directory"):
            self.backend.keys(Slot("android-test", ("test",), "android", options))

        options["state_directory"] = str(self.root / "persistent")
        options["state_mode"] = "other"
        with self.assertRaisesRegex(LeaseError, "state_mode"):
            self.backend.keys(Slot("android-test", ("test",), "android", options))

        options["state_mode"] = "clean"
        with self.assertRaisesRegex(LeaseError, "only valid"):
            self.backend.keys(Slot("android-test", ("test",), "android", options))

    def test_persistent_avd_is_reused_and_survives_release(self):
        state_directory = self.root / "persistent-state"
        first_generation = self.generation()
        first = self.backend.start(
            self.persistent_slot(state_directory), first_generation, Event(),
        )
        self.addCleanup(first.stop)
        avd_call = self.sdk.calls("avdmanager")[0]
        avd_args = avd_call["args"]
        avd_name = avd_args[avd_args.index("-n") + 1]
        avd_path = Path(avd_args[avd_args.index("-p") + 1])
        marker = avd_path / "userdata-qemu.img"
        marker.write_bytes(b"persistent fake user data")

        manifest_path = state_directory / ".resource-lease-android-state.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["owner"], "resource-lease")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["profile"]["system_image"], SYSTEM_IMAGE)
        self.assertEqual(manifest["profile"]["memory_mb"], 2048)
        self.assertTrue(state_directory.is_dir())
        self.assertFalse(state_directory.is_relative_to(first_generation))

        first.stop()
        self.assertTrue(marker.exists(), "release deleted persistent AVD data")
        self.assertFalse((first_generation / "android-home").exists())

        second_generation = self.root / "generation-two"
        second_generation.mkdir(mode=0o700)
        second = self.backend.start(
            self.persistent_slot(state_directory), second_generation, Event(),
        )
        self.addCleanup(second.stop)
        self.assertEqual(len(self.sdk.calls("avdmanager")), 1)
        emulator_calls = self.sdk.calls("emulator")
        self.assertEqual(len(emulator_calls), 2)
        second_args = emulator_calls[1]["args"]
        self.assertEqual(second_args[second_args.index("-avd") + 1], avd_name)
        self.assertEqual(marker.read_bytes(), b"persistent fake user data")
        second.stop()
        self.assertTrue(marker.exists(), "later release deleted persistent AVD data")

    def test_unknown_persistent_directory_is_never_adopted_or_deleted(self):
        state_directory = self.root / "unmarked-state"
        state_directory.mkdir(mode=0o700)
        sentinel = state_directory / "operator-data"
        sentinel.write_text("not owned by this backend", encoding="utf-8")

        with self.assertRaisesRegex(LeaseError, "persistent Android manifest"):
            self.backend.start(
                self.persistent_slot(state_directory), self.generation(), Event(),
            )
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "not owned by this backend")
        self.assertEqual(self.sdk.calls(), [])

    def test_partial_persistent_initialization_is_retained_and_not_retried(self):
        state_directory = self.root / "partial-state"
        slot = self.persistent_slot(state_directory)
        self.sdk.configure(avdmanager="create-fail")
        with self.assertRaisesRegex(LeaseError, "fixture avdmanager failure"):
            self.backend.start(slot, self.generation(), Event())

        manifest_path = state_directory / ".resource-lease-android-state.json"
        manifest_before = manifest_path.read_bytes()
        self.assertEqual(json.loads(manifest_before)["status"], "initializing")
        avd_files_before = sorted(
            path.relative_to(state_directory).as_posix()
            for path in state_directory.rglob("*")
        )
        self.assertTrue(any(path.endswith("config.ini") for path in avd_files_before))
        self.assertEqual(len(self.sdk.calls("avdmanager")), 1)

        second_generation = self.root / "generation-two"
        second_generation.mkdir(mode=0o700)
        with self.assertRaisesRegex(LeaseError, "initialization is incomplete"):
            self.backend.start(slot, second_generation, Event())
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(
            sorted(path.relative_to(state_directory).as_posix()
                   for path in state_directory.rglob("*")),
            avd_files_before,
        )
        self.assertEqual(len(self.sdk.calls("avdmanager")), 1)

    def test_incompatible_persistent_image_and_memory_profiles_are_preserved(self):
        state_directory = self.root / "incompatible-state"
        first = self.backend.start(
            self.persistent_slot(state_directory), self.generation(), Event(),
        )
        self.addCleanup(first.stop)
        avd_path = Path(self.sdk.calls("avdmanager")[0]["args"][-1])
        marker = avd_path / "userdata-qemu.img"
        marker.write_bytes(b"must survive profile mismatch")
        first.stop()
        manifest_path = state_directory / ".resource-lease-android-state.json"
        manifest_before = manifest_path.read_bytes()

        properties = (
            self.sdk.sdk_root / "system-images" / "android-34" / "google_apis"
            / "arm64-v8a" / "source.properties"
        )
        properties.write_text("Pkg.Revision=2.0.0\n", encoding="utf-8")
        generation_two = self.root / "generation-two"
        generation_two.mkdir(mode=0o700)
        with self.assertRaisesRegex(LeaseError, "profile differs"):
            self.backend.start(
                self.persistent_slot(state_directory),
                generation_two,
                Event(),
            )
        properties.write_text("Pkg.Revision=1.0.0\n", encoding="utf-8")

        generation_three = self.root / "generation-three"
        generation_three.mkdir(mode=0o700)
        with self.assertRaisesRegex(LeaseError, "profile differs"):
            self.backend.start(
                self.persistent_slot(state_directory, memory_mb=1024),
                generation_three,
                Event(),
            )
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(marker.read_bytes(), b"must survive profile mismatch")
        self.assertEqual(len(self.sdk.calls("avdmanager")), 1)
        self.assertEqual(len(self.sdk.calls("emulator")), 1)

    def test_persistent_state_outside_generation_pruning_tree(self):
        pool_root = self.root / "pool"
        generations = pool_root / "generations"
        generations.mkdir(parents=True, mode=0o700)
        generation = generations / "lease"
        generation.mkdir(mode=0o700)
        state_directory = generations / "persistent-state"
        slot = self.persistent_slot(state_directory)
        with self.assertRaisesRegex(LeaseError, "outside generations"):
            self.backend.start(slot, generation, Event())
        self.assertFalse(state_directory.exists())
        self.assertEqual(self.sdk.calls(), [])

    def test_persistent_state_rejects_another_pools_generation(self):
        other_pool = Store.initialize(self.root / "other-pool", Config.parse({
            "schema": 1,
            "slots": [{
                "id": "other", "backend": "android", "capabilities": ["android-api-34"],
                "options": self.sdk.options(self.port),
            }],
        }))
        completed_generation = other_pool.root / "generations" / "completed-request"
        completed_generation.mkdir(parents=True, mode=0o700)
        state_directory = completed_generation / "persistent-state"
        with self.assertRaisesRegex(LeaseError, "outside generations"):
            self.backend.keys(self.persistent_slot(state_directory))
        self.assertFalse(state_directory.exists())
        self.assertEqual(self.sdk.calls(), [])

    def test_new_pool_generation_namespace_is_reserved_before_first_request(self):
        pool = Store.initialize(self.root / "unused-pool", Config.parse({
            "schema": 1,
            "slots": [{
                "id": "other", "backend": "android", "capabilities": ["android-api-34"],
                "options": self.sdk.options(self.port),
            }],
        }))
        for name in ("generations", "GENERATIONS", "GeNeRaTiOnS"):
            state = pool.root / name
            with self.subTest(name=name), self.assertRaisesRegex(LeaseError, "outside generations"):
                self.backend.keys(self.persistent_slot(state))
            self.assertFalse(state.exists())
        self.assertEqual(self.sdk.calls(), [])

    def test_foreign_generation_rejection_preserves_existing_state_and_resolves_aliases(self):
        pool = self.root / "other-pool"
        pool.mkdir(mode=0o700)
        (pool / "generations").mkdir(mode=0o700)
        generation = pool / "generations" / "completed-request"
        generation.mkdir(mode=0o700)
        # Even a damaged database must not make a pool's disposable tree safe.
        (pool / "leases.sqlite3").write_bytes(b"damaged database")
        state = generation / "persistent-state"
        state.mkdir(mode=0o700)
        sentinel = state / "userdata.img"
        sentinel.write_bytes(b"preserve existing state")
        alias = self.root / "generation-alias"
        alias.symlink_to(generation, target_is_directory=True)
        for path in (state, alias / state.name, generation.parent):
            with self.subTest(path=path), self.assertRaisesRegex(LeaseError, "outside generations"):
                self.backend.keys(self.persistent_slot(path))
        self.assertEqual(sentinel.read_bytes(), b"preserve existing state")
        self.assertEqual(self.sdk.calls(), [])

    def test_generation_name_without_pool_marker_is_not_reserved(self):
        parent = self.root / "generations"
        parent.mkdir(mode=0o700)
        self.assertTrue(self.backend.keys(self.persistent_slot(parent / "state")))

    def test_foreign_generation_case_alias_is_rejected(self):
        pool = self.root / "other-pool"
        generation = pool / "generations" / "completed-request"
        generation.mkdir(parents=True, mode=0o700)
        (pool / "leases.sqlite3").write_bytes(b"pool marker")
        alias = pool / "GENERATIONS" / generation.name
        if not alias.is_dir() or not alias.samefile(generation):
            self.skipTest("Requires a case-insensitive filesystem")
        state = alias / "state"
        with self.assertRaisesRegex(LeaseError, "outside generations"):
            self.backend.keys(self.persistent_slot(state))
        self.assertFalse(state.exists())
        self.assertEqual(self.sdk.calls(), [])

    def test_unreadable_pool_marker_does_not_allow_persistent_state(self):
        parent = self.root / "other-pool" / "generations"
        parent.mkdir(parents=True, mode=0o700)
        marker = (parent.parent / "leases.sqlite3").resolve()
        original_lstat = Path.lstat

        def unreadable_marker(path, *args, **kwargs):
            if path == marker:
                raise PermissionError("injected marker access failure")
            return original_lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", unreadable_marker):
            with self.assertRaisesRegex(LeaseError, "cannot verify"):
                self.backend.keys(self.persistent_slot(parent / "state"))
        self.assertFalse((parent / "state").exists())
        self.assertEqual(self.sdk.calls(), [])

    def test_persistent_state_cannot_be_nested_in_a_direct_backend_generation(self):
        generation = self.generation()
        slot = self.persistent_slot(generation / "state")
        with self.assertRaisesRegex(LeaseError, "outside generations"):
            resource = self.backend.start(slot, generation, Event())
            self.addCleanup(resource.stop)
        self.assertFalse((generation / "state").exists())
        self.assertEqual(self.sdk.calls(), [])

    def test_direct_backend_generation_case_alias_is_rejected(self):
        generation = self.generation()
        alias = generation.with_name(generation.name.upper())
        if not alias.is_dir() or not alias.samefile(generation):
            self.skipTest("Requires a case-insensitive filesystem")
        state = alias / "state"
        with self.assertRaisesRegex(LeaseError, "outside generations"):
            resource = self.backend.start(self.persistent_slot(state), generation, Event())
            self.addCleanup(resource.stop)
        self.assertFalse(state.exists())
        self.assertEqual(self.sdk.calls(), [])

    def test_persistent_startup_quarantine_preserves_partial_state(self):
        state_directory = self.root / "quarantined-state"
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
            raise QuarantineError("injected persistent startup uncertainty")

        with mock.patch.object(self.backend, "_wait_ready", side_effect=quarantine_after_spawn):
            with self.assertRaisesRegex(QuarantineError, "persistent startup uncertainty"):
                self.backend.start(
                    self.persistent_slot(state_directory), generation, Event(),
                )

        self.assertEqual(len(captured), 1)
        self.assertIsNotNone(captured[0].poll())
        manifest = json.loads(
            (state_directory / ".resource-lease-android-state.json").read_text()
        )
        self.assertEqual(manifest["status"], "initializing")
        self.assertTrue((state_directory / "android-home" / "avd").is_dir())
        self.assertTrue(all(self._port_free(port) for port in (self.port, self.port + 1)))

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
        original_launch = android_adapter.launch_child

        def track_launch(*args, **kwargs):
            child = original_launch(*args, **kwargs)
            self.addCleanup(lambda: original_stop(child))
            return child

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
        ), mock.patch.object(android_adapter, "launch_child", side_effect=track_launch):
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
            "-no-window", "-no-snapshot", "-no-boot-anim",
            "-no-metrics", "-crash-report-mode",
        ):
            self.assertIn(flag, emulator_args)
        self.assertNotIn("-feature", emulator_args)
        self.assertNotIn("-no-audio", emulator_args)

        adb_calls = self.sdk.calls("adb")
        self.assertTrue(adb_calls)
        self.assertTrue(all(call["args"][:2] == ["-s", f"emulator-{self.port}"]
                            for call in adb_calls))
        resource.stop()
        self.assertFalse(resource.healthy())
        self.assert_private_home_removed(generation)
        self.assertEqual(
            {path.name for path in generation.iterdir()},
            {"android-avdmanager.log", "android-emulator.log", "android-adb.log", "android-netsimd.log"},
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
