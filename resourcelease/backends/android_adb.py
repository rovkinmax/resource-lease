"""An owned loopback ADB server for one disposable Android run."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
from threading import Event
import time
from typing import Mapping

from resourcelease.model import LeaseError
from resourcelease.process import CommandCancelled, ManagedProcess, launch_child, run_command


_OVERRIDES = (
    "ADBHOST", "ADB_SERVER_SOCKET", "ADB_SERVER_PORT", "ADB_SERVER_HOST",
    "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT", "ADB_VENDOR_KEYS",
    "ADB_LOCAL_TRANSPORT_MAX_PORT", "ANDROID_SERIAL",
)


class OwnedAdb:
    def __init__(self, listener: socket.socket, child: ManagedProcess, home: Path):
        self._listener = listener
        self.child = child
        self.home = home
        self.port = listener.getsockname()[1]

    @staticmethod
    def environment(
        environ: Mapping[str, str], port: int, *, serial: str | None = None,
    ) -> dict[str, str]:
        result = {key: value for key, value in environ.items() if key not in _OVERRIDES}
        result.update(
            ADB_SERVER_SOCKET=f"tcp:127.0.0.1:{port}",
            ANDROID_ADB_SERVER_PORT=str(port),
            ADB_USB="0",
            ADB_EMU="0",
            ADB_MDNS="0",
            ADB_REJECT_KILL_SERVER="1",
        )
        if serial is not None:
            result["ANDROID_SERIAL"] = serial
        return result

    @classmethod
    def start(cls, executable: Path, generation: Path, cancelled: Event) -> OwnedAdb:
        if cancelled.is_set():
            raise CommandCancelled("ADB startup cancelled")
        home = generation / "adb-home"
        home.mkdir(mode=0o700)
        keys = home / ".android"
        keys.mkdir(mode=0o700)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            fd = listener.fileno()
            port = listener.getsockname()[1]
            environment = cls.environment(os.environ, port)
            environment.update(HOME=str(home), ANDROID_USER_HOME=str(home))
            generated = run_command(
                [executable, "keygen", keys / "adbkey"], env=environment,
                timeout=10, log_path=generation / "adb-keygen.log", cancelled=cancelled,
            )
            if generated.returncode != 0 or not all(
                (keys / name).is_file() for name in ("adbkey", "adbkey.pub")
            ):
                raise LeaseError("could not generate disposable ADB keys")
            child = launch_child(
                [executable, "-L", f"acceptfd:{fd}", "server", "nodaemon"],
                env=environment, pass_fds=(fd,), log_path=generation / "adb-server.log",
            )
        except BaseException:
            listener.close()
            raise
        owned = cls(listener, child, home)
        try:
            deadline = time.monotonic() + 8.0
            while not cancelled.is_set():
                if child.poll() is not None:
                    raise LeaseError(f"owned ADB server stopped: {child.log_path}")
                try:
                    if owned._query("host:version"):
                        owned.assert_devices(None)
                        return owned
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise LeaseError(f"owned ADB server did not start: {child.log_path}")
                cancelled.wait(0.05)
            raise CommandCancelled("ADB startup cancelled")
        except BaseException:
            owned.stop()
            raise

    def emulator_environment(
        self, environment: Mapping[str, str], emulator_home: Path,
    ) -> dict[str, str]:
        # emu loads the guest's trusted key from its configuration directory;
        # the server loads the same per-run key from its private $HOME/.android.
        for name in ("adbkey", "adbkey.pub"):
            source = self.home / ".android" / name
            with source.open("rb") as reader, (emulator_home / name).open("xb") as writer:
                os.fchmod(writer.fileno(), 0o600)
                shutil.copyfileobj(reader, writer)
        result = self.environment(environment, self.port)
        result["HOME"] = str(self.home)
        return result

    def _query(self, request: str) -> bytes:
        raw = request.encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=0.4) as stream:
            stream.sendall(f"{len(raw):04x}".encode("ascii") + raw)
            with stream.makefile("rb") as reply:
                if reply.read(4) != b"OKAY":
                    raise LeaseError(f"owned ADB server rejected {request}")
                try:
                    length = int(reply.read(4), 16)
                except ValueError as error:
                    raise LeaseError("owned ADB server returned malformed data") from error
                value = reply.read(length)
                if len(value) != length:
                    raise LeaseError("owned ADB server returned incomplete data")
                return value

    def assert_devices(self, serial: str | None) -> None:
        expected = [] if serial is None else [f"{serial}\tdevice".encode("ascii")]
        if self.child.poll() is not None or self._query("host:devices").splitlines() != expected:
            raise LeaseError("isolated ADB server did not contain exactly the expected device")

    def stop(self) -> None:
        try:
            self.child.stop()
        finally:
            self._listener.close()
