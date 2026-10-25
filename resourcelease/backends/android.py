"""Android Emulator backend for the standalone resource-lease service."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import math
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import time
from threading import Event, Lock
from typing import Mapping
import uuid

from resourcelease.model import LeaseError, QuarantineError, Resource, Slot
from resourcelease.process import (
    CommandCancelled,
    CommandTimeout,
    ManagedProcess,
    launch_child,
    run_command,
)


DEFAULT_MEMORY_MB = 2048
DEFAULT_BOOT_TIMEOUT = 180.0
MIN_EMULATOR_PORT = 5554
MAX_EMULATOR_PORT = 5682
MIN_MEMORY_MB = 512
MAX_MEMORY_MB = 8192
MIN_BOOT_TIMEOUT = 1.0
MAX_BOOT_TIMEOUT = 1800.0
COMMAND_TIMEOUT = 5.0
PORT_RELEASE_TIMEOUT = 5.0
PORT_POLL_INTERVAL = 0.1
SYSTEM_IMAGE = re.compile(
    r"system-images;android-[1-9][0-9]*;[A-Za-z0-9_.+-]+;[A-Za-z0-9_.+-]+\Z"
)
ANDROID_OPTIONS = frozenset({
    "sdk_root", "java_home", "system_image", "console_port", "memory_mb", "boot_timeout",
})
ANDROID_REQUIRED = frozenset({"sdk_root", "java_home", "system_image", "console_port"})
RADIO_FEATURES = ("BluetoothEmulation", "Uwb", "Nfc", "WiFiPacketStream")


@dataclass(frozen=True)
class _Options:
    sdk_root: Path
    java_home: Path
    system_image: str
    console_port: int
    memory_mb: int
    boot_timeout: float
    avdmanager: Path
    emulator: Path
    adb: Path

    @property
    def adb_port(self) -> int:
        return self.console_port + 1

    def environment(self, private_home: Path, avd_home: Path) -> dict[str, str]:
        # Keep the caller's real HOME: SDK launcher scripts and the host runtime
        # may need it. Redirect only Android SDK state into this lease generation.
        environment = os.environ.copy()
        environment.update({
            "ANDROID_HOME": str(self.sdk_root),
            "ANDROID_SDK_ROOT": str(self.sdk_root),
            "JAVA_HOME": str(self.java_home),
            "ANDROID_USER_HOME": str(private_home),
            "ANDROID_EMULATOR_HOME": str(private_home),
            "ANDROID_AVD_HOME": str(avd_home),
        })
        additions = (
            self.java_home / "bin",
            self.avdmanager.parent,
            self.emulator.parent,
            self.adb.parent,
        )
        inherited = environment.get("PATH", "")
        environment["PATH"] = os.pathsep.join(
            [*(str(path) for path in additions), inherited] if inherited else
            [str(path) for path in additions]
        )
        return environment


def _number(value, label: str, minimum: float, maximum: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise LeaseError(f"Android {label} must be between {minimum:g} and {maximum:g}")
    return float(value)


def _path_option(options: Mapping[str, object], key: str) -> Path:
    value = options.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise LeaseError(f"Android option {key} must be a nonempty path")
    try:
        return Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LeaseError(f"Android option {key} does not resolve to an installed path") from error


def _executable(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError) as error:
        raise LeaseError(f"Android {label} is missing: {path}") from error
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise LeaseError(f"Android {label} is not an executable file: {path}")
    return resolved


def _find_avdmanager(sdk_root: Path) -> Path:
    tools = sdk_root / "cmdline-tools"
    candidates = [tools / "latest" / "bin" / "avdmanager"]
    try:
        versions = [
            child for child in tools.iterdir()
            if child.is_dir() and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", child.name)
        ]
    except OSError:
        versions = []
    versions.sort(
        key=lambda path: tuple(int(part) for part in path.name.split(".")),
        reverse=True,
    )
    candidates.extend(path / "bin" / "avdmanager" for path in versions)
    for candidate in candidates:
        try:
            return _executable(candidate, "avdmanager")
        except LeaseError:
            continue
    raise LeaseError(f"Android SDK has no installed command-line tools avdmanager: {tools}")


def _parse_options(slot: Slot) -> _Options:
    options = slot.options
    if not isinstance(options, Mapping):
        raise LeaseError("Android backend options must be an object")
    missing = ANDROID_REQUIRED.difference(options)
    unexpected = set(options).difference(ANDROID_OPTIONS)
    if missing:
        raise LeaseError(f"Android backend options are missing: {', '.join(sorted(missing))}")
    if unexpected:
        raise LeaseError(f"unsupported Android backend options: {', '.join(sorted(unexpected))}")

    sdk_root = _path_option(options, "sdk_root")
    java_home = _path_option(options, "java_home")
    if not sdk_root.is_dir():
        raise LeaseError(f"Android sdk_root is not a directory: {sdk_root}")
    if not java_home.is_dir():
        raise LeaseError(f"Android java_home is not a directory: {java_home}")
    java = _executable(java_home / "bin" / "java", "Java runtime")
    del java  # Validate the executable without invoking or supervising the JVM.

    system_image = options.get("system_image")
    if not isinstance(system_image, str) or not SYSTEM_IMAGE.fullmatch(system_image):
        raise LeaseError("Android system_image must be an installed SDK package ID")
    _, api, flavor, abi = system_image.split(";")
    image_path = sdk_root / "system-images" / api / flavor / abi / "system.img"
    try:
        if not image_path.is_file():
            raise LeaseError(f"Android system image is not installed: {system_image}")
    except OSError as error:
        raise LeaseError(f"cannot inspect Android system image: {system_image}") from error

    port = options.get("console_port")
    if (isinstance(port, bool) or not isinstance(port, int)
            or not MIN_EMULATOR_PORT <= port <= MAX_EMULATOR_PORT or port % 2):
        raise LeaseError(
            f"Android console_port must be an even port in "
            f"{MIN_EMULATOR_PORT}..{MAX_EMULATOR_PORT}"
        )
    memory = options.get("memory_mb", DEFAULT_MEMORY_MB)
    if (isinstance(memory, bool) or not isinstance(memory, int)
            or not MIN_MEMORY_MB <= memory <= MAX_MEMORY_MB):
        raise LeaseError(
            f"Android memory_mb must be an integer in {MIN_MEMORY_MB}..{MAX_MEMORY_MB}"
        )
    timeout = _number(
        options.get("boot_timeout", DEFAULT_BOOT_TIMEOUT),
        "boot_timeout",
        MIN_BOOT_TIMEOUT,
        MAX_BOOT_TIMEOUT,
    )

    avdmanager = _find_avdmanager(sdk_root)
    emulator = _executable(sdk_root / "emulator" / "emulator", "emulator")
    adb = _executable(sdk_root / "platform-tools" / "adb", "adb")
    return _Options(
        sdk_root, java_home, system_image, port, memory, timeout,
        avdmanager, emulator, adb,
    )


def _port_bindable(port: int) -> bool:
    """Return whether this port can be rebound on both local IP stacks."""
    # Check loopback as well as wildcard: SO_REUSEADDR can permit a wildcard
    # probe alongside a listener bound only to loopback on BSD-derived hosts.
    checks = [
        (socket.AF_INET, ("0.0.0.0", port)),
        (socket.AF_INET, ("127.0.0.1", port)),
    ]
    if socket.has_ipv6:
        checks.extend((
            (socket.AF_INET6, ("::", port)),
            (socket.AF_INET6, ("::1", port)),
        ))
    for family, address in checks:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(address)
                probe.listen(1)
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return False
            if family == socket.AF_INET6 and error.errno in {
                errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT,
            }:
                continue
            raise QuarantineError(f"cannot establish whether TCP port {port} is free") from error
    return True


def _assert_ports_free(options: _Options) -> None:
    for port in (options.console_port, options.adb_port):
        if not _port_bindable(port):
            raise LeaseError(f"Android emulator port {port} is already in use")


def _require_generation(directory: Path) -> Path:
    try:
        generation = Path(directory).resolve(strict=True)
        info = generation.stat()
    except (OSError, RuntimeError) as error:
        raise LeaseError("Android generation directory is unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LeaseError("Android generation directory must be an owned directory")
    return generation


def _create_private_home(generation: Path) -> tuple[Path, Path, str]:
    private_home = generation / "android-home"
    created = False
    created_info = None
    try:
        private_home.mkdir(mode=0o700, exist_ok=False)
        created = True
        created_info = private_home.lstat()
        marker = uuid.uuid4().hex
        descriptor = os.open(
            private_home / ".resource-lease-owner",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
        avd_home = private_home / "avd"
        avd_home.mkdir(mode=0o700)
        return private_home, avd_home, marker
    except OSError as error:
        if created:
            try:
                if created_info is None:
                    raise QuarantineError(
                        "cannot verify newly created private Android home; preserving it"
                    )
                current_info = private_home.lstat()
                if (not stat.S_ISDIR(current_info.st_mode)
                        or current_info.st_uid != os.getuid()
                        or (current_info.st_dev, current_info.st_ino)
                        != (created_info.st_dev, created_info.st_ino)):
                    raise QuarantineError(
                        "private Android home changed during partial creation; preserving it"
                    )
                shutil.rmtree(private_home)
            except QuarantineError:
                raise
            except OSError as cleanup_error:
                raise QuarantineError(
                    "could not clean a partial private Android home"
                ) from cleanup_error
        raise LeaseError(f"cannot create private Android home: {private_home}") from error


def _remove_private_home(generation: Path, private_home: Path, marker: str) -> None:
    if not private_home.exists() and not private_home.is_symlink():
        return
    try:
        info = private_home.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise QuarantineError("private Android home changed type or owner; preserving it")
        if private_home.parent.resolve(strict=True) != generation:
            raise QuarantineError("private Android home escaped its lease generation")
        marker_path = private_home / ".resource-lease-owner"
        marker_info = marker_path.lstat()
        if (not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != os.getuid()
                or marker_info.st_size != 32):
            raise QuarantineError("private Android home ownership marker changed; preserving it")
        descriptor = os.open(
            marker_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened_info = os.fstat(descriptor)
            if (not stat.S_ISREG(opened_info.st_mode)
                    or opened_info.st_uid != os.getuid()
                    or opened_info.st_size != 32
                    or (opened_info.st_dev, opened_info.st_ino)
                    != (marker_info.st_dev, marker_info.st_ino)):
                raise QuarantineError(
                    "private Android home ownership marker changed; preserving it"
                )
            marker_contents = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if marker_contents != marker.encode("ascii"):
            raise QuarantineError("private Android home ownership marker changed; preserving it")
        shutil.rmtree(private_home)
        if private_home.exists() or private_home.is_symlink():
            raise QuarantineError("private Android home remains after scoped cleanup")
    except QuarantineError:
        raise
    except OSError as error:
        raise QuarantineError("could not remove the SDK-created private AVD generation") from error


def _command_error(label: str, result, timeout: float | None = None) -> LeaseError:
    output = result.output.strip()
    if len(output) > 1200:
        output = output[:1200] + "…"
    details = f": {output}" if output else ""
    if timeout is None:
        return LeaseError(
            f"{label} exited with status {result.returncode}{details}; "
            f"bounded log: {result.log_path}"
        )
    return LeaseError(
        f"{label} timed out after {timeout:g}s{details}; bounded log: {result.log_path}"
    )


class AndroidBackend:
    """Create a disposable SDK-owned AVD and supervise its direct emulator child."""

    def keys(self, slot: Slot) -> tuple[str, ...]:
        options = _parse_options(slot)
        return (f"tcp-port:{options.console_port}", f"tcp-port:{options.adb_port}")

    def start(self, slot: Slot, directory: Path, cancelled: Event) -> "AndroidResource":
        options = _parse_options(slot)
        generation = _require_generation(directory)
        _assert_ports_free(options)
        private_home, avd_home, marker = _create_private_home(generation)
        avd_name = f"resource_lease_{uuid.uuid4().hex[:16]}"
        avd_path = avd_home / f"{avd_name}.avd"
        environment = options.environment(private_home, avd_home)
        avdmanager_log = generation / "android-avdmanager.log"
        emulator_log = generation / "android-emulator.log"
        adb_log = generation / "android-adb.log"
        child: ManagedProcess | None = None

        try:
            if cancelled.is_set():
                raise LeaseError("Android resource start cancelled before AVD creation")
            created = run_command(
                [
                    options.avdmanager, "create", "avd", "-n", avd_name,
                    "-k", options.system_image, "-p", avd_path,
                ],
                env=environment,
                timeout=60,
                log_path=avdmanager_log,
                input_data=b"no\n",
                cancelled=cancelled,
            )
            if created.returncode:
                raise _command_error("avdmanager create", created)
            config_ini = avd_path / "config.ini"
            if not config_ini.is_file():
                raise LeaseError(
                    f"avdmanager reported success without creating the fresh AVD: {avd_path}"
                )
            if cancelled.is_set():
                raise LeaseError("Android resource start cancelled after AVD creation")

            # A foreign process may have claimed a port while avdmanager ran.
            _assert_ports_free(options)
            child = launch_child(
                [
                    options.emulator,
                    "-avd", avd_name,
                    "-ports", f"{options.console_port},{options.adb_port}",
                    "-memory", str(options.memory_mb),
                    "-no-window",
                    "-no-snapshot",
                    "-no-audio",
                    "-no-boot-anim",
                    "-no-metrics",
                    "-crash-report-mode", "disabled",
                    *[flag for feature in RADIO_FEATURES
                      for flag in ("-feature", f"-{feature}")],
                ],
                env=environment,
                log_path=emulator_log,
            )
            self._wait_ready(options, child, environment, adb_log, cancelled)
            return AndroidResource(
                self, options, generation, private_home, marker, environment,
                avd_name, child, emulator_log, adb_log,
            )
        except BaseException as failure:
            if isinstance(failure, QuarantineError):
                if child is not None:
                    try:
                        child.stop(grace=10, kill_timeout=3)
                    except BaseException as stop_error:
                        if hasattr(failure, "add_note"):
                            failure.add_note(
                                "known emulator child could not be stopped; "
                                f"generation preserved: {stop_error}"
                            )
                raise
            try:
                if child is not None:
                    child.stop(grace=10, kill_timeout=3)
                    self._wait_ports_free(options)
                _remove_private_home(generation, private_home, marker)
            except QuarantineError as cleanup_error:
                raise cleanup_error from failure
            except BaseException as cleanup_error:
                raise QuarantineError(
                    "Android startup failed and cleanup could not be proven"
                ) from cleanup_error
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(failure, LeaseError):
                raise
            raise LeaseError(f"Android resource start failed: {failure}") from failure

    def _wait_ready(
        self,
        options: _Options,
        child: ManagedProcess,
        environment: Mapping[str, str],
        adb_log: Path,
        cancelled: Event,
    ) -> None:
        serial = f"emulator-{options.console_port}"
        deadline = time.monotonic() + options.boot_timeout
        last_error = ""
        while True:
            if cancelled.is_set():
                raise CommandCancelled("Android emulator boot cancelled")
            status = child.poll()
            if status is not None:
                child.wait(timeout=0)
                output = child.output.strip()
                if len(output) > 1200:
                    output = output[:1200] + "…"
                detail = f": {output}" if output else ""
                raise LeaseError(
                    f"Android emulator exited before readiness with status {status}{detail}; "
                    f"bounded log: {child.log_path}"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                suffix = f"; last ADB result: {last_error}" if last_error else ""
                raise LeaseError(
                    f"Android emulator did not boot within {options.boot_timeout:g}s{suffix}; "
                    f"bounded emulator/ADB logs: {child.log_path}, {adb_log}"
                )
            command_timeout = min(COMMAND_TIMEOUT, remaining)
            try:
                state = run_command(
                    [options.adb, "-s", serial, "get-state"],
                    env=environment,
                    timeout=command_timeout,
                    log_path=adb_log,
                    cancelled=cancelled,
                    append_log=True,
                )
                if state.returncode == 0 and state.output.strip() == "device":
                    boot = run_command(
                        [options.adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                        env=environment,
                        timeout=min(COMMAND_TIMEOUT, max(0.05, deadline - time.monotonic())),
                        log_path=adb_log,
                        cancelled=cancelled,
                        append_log=True,
                    )
                    if boot.returncode == 0 and boot.output.strip() == "1":
                        return
                    last_error = boot.output.strip() or f"boot check exited {boot.returncode}"
                else:
                    last_error = state.output.strip() or f"ADB state exited {state.returncode}"
            except CommandCancelled:
                raise
            except QuarantineError:
                raise
            except (CommandTimeout, LeaseError) as error:
                last_error = str(error)
            cancelled.wait(min(0.25, max(0, deadline - time.monotonic())))

    def _check_ready(
        self,
        options: _Options,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> bool:
        serial = f"emulator-{options.console_port}"
        try:
            state = run_command(
                [options.adb, "-s", serial, "get-state"],
                env=environment,
                timeout=COMMAND_TIMEOUT,
                log_path=log_path,
                append_log=True,
            )
            if state.returncode or state.output.strip() != "device":
                return False
            boot = run_command(
                [options.adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                env=environment,
                timeout=COMMAND_TIMEOUT,
                log_path=log_path,
                append_log=True,
            )
            return boot.returncode == 0 and boot.output.strip() == "1"
        except QuarantineError:
            raise
        except LeaseError:
            return False

    def _wait_ports_free(self, options: _Options) -> None:
        deadline = time.monotonic() + PORT_RELEASE_TIMEOUT
        while True:
            if all(_port_bindable(port) for port in (options.console_port, options.adb_port)):
                return
            if time.monotonic() >= deadline:
                raise QuarantineError(
                    "Android emulator exited but its console/ADB ports remain occupied"
                )
            time.sleep(PORT_POLL_INTERVAL)


class AndroidResource(Resource):
    """A ready AVD and the live direct child that owns its emulator process."""

    def __init__(
        self,
        backend: AndroidBackend,
        options: _Options,
        generation: Path,
        private_home: Path,
        marker: str,
        environment: Mapping[str, str],
        avd_name: str,
        child: ManagedProcess,
        emulator_log: Path,
        adb_log: Path,
    ):
        self._backend = backend
        self._options = options
        self._generation = generation
        self._private_home = private_home
        self._marker = marker
        self._environment = dict(environment)
        self._avd_name = avd_name
        self._child = child
        self._emulator_log = emulator_log
        self._adb_log = adb_log
        self._health_lock = Lock()
        self._last_health_at = 0.0
        self._last_health = True
        self._stopped = False
        self._endpoint = {
            "adb_serial": f"emulator-{options.console_port}",
            "console_port": str(options.console_port),
            "adb_port": str(options.adb_port),
        }

    @property
    def endpoint(self) -> Mapping[str, str]:
        return dict(self._endpoint)

    def healthy(self) -> bool:
        if self._stopped or self._child.poll() is not None:
            return False
        now = time.monotonic()
        with self._health_lock:
            if now - self._last_health_at < 1.0:
                return self._last_health
            self._last_health = self._backend._check_ready(
                self._options, self._environment, self._adb_log,
            )
            self._last_health_at = time.monotonic()
            return self._last_health

    def stop(self) -> None:
        if self._stopped:
            return
        self._child.stop(grace=10, kill_timeout=3)
        self._backend._wait_ports_free(self._options)
        _remove_private_home(self._generation, self._private_home, self._marker)
        self._stopped = True
