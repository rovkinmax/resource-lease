"""Bounded subprocess helpers that retain live direct-child handles only."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import stat
import subprocess
from threading import Event, Thread
import time
from typing import Mapping, Sequence

from .model import LeaseError, QuarantineError


MAX_COMMAND_OUTPUT = 32 * 1024
MAX_LOG_BYTES = 128 * 1024
MAX_COMMAND_INPUT = 4096
MAX_COMMAND_TIMEOUT = 3600.0
POLL_INTERVAL = 0.05


class CommandCancelled(LeaseError):
    """A bounded command was cancelled and its direct child exited."""


class CommandTimeout(LeaseError):
    """A bounded command timed out and its direct child exited."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str
    log_path: Path


class _OutputCollector:
    """Drain a child pipe while retaining only bounded output and log bytes."""

    def __init__(
        self,
        stream,
        log_path: Path,
        *,
        log_limit: int,
        capture_limit: int,
        append: bool,
    ):
        self.stream = stream
        self.log_path = log_path
        self.log_limit = log_limit
        self.capture_limit = capture_limit
        self.append = append
        self.captured = bytearray()
        self.error: OSError | None = None
        self._thread = Thread(target=self._drain, name="resource-lease-output", daemon=True)

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self.append:
            flags |= os.O_APPEND
        else:
            flags |= os.O_EXCL
        try:
            descriptor = os.open(self.log_path, flags, 0o600)
        except OSError as error:
            self.stream.close()
            raise LeaseError(f"cannot open bounded process log: {self.log_path}") from error
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            os.close(descriptor)
            self.stream.close()
            raise LeaseError(f"process log is not a private regular file: {self.log_path}")
        if info.st_size > self.log_limit:
            os.close(descriptor)
            self.stream.close()
            raise LeaseError(f"existing process log exceeds its bounded size: {self.log_path}")
        self._remaining = max(0, self.log_limit - info.st_size)
        self._log = os.fdopen(descriptor, "ab" if self.append else "wb")
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    break
                capture = self.capture_limit - len(self.captured)
                if capture > 0:
                    self.captured.extend(chunk[:capture])
                if self._remaining:
                    written = min(len(chunk), self._remaining)
                    try:
                        self._log.write(chunk[:written])
                        self._remaining -= written
                    except OSError as error:
                        self.error = error
                        self._remaining = 0
        except OSError as error:
            self.error = error
        finally:
            try:
                self._log.flush()
                self._log.close()
            except OSError as error:
                self.error = self.error or error
            try:
                self.stream.close()
            except OSError as error:
                self.error = self.error or error

    def join(self, timeout: float = 2.0) -> None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise QuarantineError(
                "direct child exited but its output pipe remains open; cleanup is uncertain"
            )

    def close_if_not_started(self) -> None:
        if self._thread.ident is not None:
            return
        try:
            self.stream.close()
        except OSError:
            pass
        log = getattr(self, "_log", None)
        if log is not None:
            try:
                log.close()
            except OSError:
                pass

    @property
    def output(self) -> str:
        return self.captured.decode("utf-8", errors="replace")


class ManagedProcess:
    """A running child represented by its live ``Popen`` handle, never a PID."""

    def __init__(self, process: subprocess.Popen, collector: _OutputCollector):
        self.process = process
        self._collector = collector
        self.log_path = collector.log_path

    def poll(self) -> int | None:
        return self.process.poll()

    @property
    def output(self) -> str:
        return self._collector.output

    @property
    def log_error(self) -> OSError | None:
        return self._collector.error

    def wait(self, timeout: float | None = None) -> int:
        try:
            result = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise
        self._collector.join()
        return result

    def stop(self, *, grace: float = 5.0, kill_timeout: float = 2.0) -> int:
        """Stop this direct child, escalating only through its live Popen handle."""
        if (isinstance(grace, bool) or not isinstance(grace, (int, float))
                or isinstance(kill_timeout, bool) or not isinstance(kill_timeout, (int, float))
                or not math.isfinite(grace) or not math.isfinite(kill_timeout)
                or grace < 0 or kill_timeout < 0):
            raise ValueError("process wait bounds must be finite and nonnegative")
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError as error:
                if self.process.poll() is None:
                    raise QuarantineError("cannot signal owned direct child") from error
        try:
            result = self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                self.process.kill()
            except OSError as error:
                if self.process.poll() is None:
                    raise QuarantineError("cannot kill owned direct child") from error
            try:
                result = self.process.wait(timeout=kill_timeout)
            except subprocess.TimeoutExpired as error:
                raise QuarantineError("owned direct child did not exit after kill") from error
        self._collector.join(timeout=max(2.0, kill_timeout))
        return result


def _stop_after_setup_failure(
    process: subprocess.Popen,
    *,
    grace: float = 2.0,
    kill_timeout: float = 2.0,
) -> None:
    """Reap a direct child started before supervision setup completed."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError as error:
            if process.poll() is None:
                raise QuarantineError("cannot signal child after process setup failure") from error
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError as error:
            if process.poll() is None:
                raise QuarantineError("cannot kill child after process setup failure") from error
        try:
            process.wait(timeout=kill_timeout)
        except subprocess.TimeoutExpired as error:
            raise QuarantineError("child did not exit after process setup failure") from error


def launch_child(
    argv: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str],
    log_path: Path,
    log_limit: int = MAX_LOG_BYTES,
) -> ManagedProcess:
    """Launch one owned child with bounded diagnostics and no shell expansion."""
    if (isinstance(argv, (str, bytes)) or not argv
            or any(not isinstance(value, (str, os.PathLike)) or not os.fspath(value)
                        for value in argv)):
        raise LeaseError("child command must be a nonempty argument vector")
    _validate_log_limit(log_limit)
    collector = None
    try:
        process = subprocess.Popen(
            [os.fspath(value) for value in argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(env),
            close_fds=True,
            bufsize=0,
        )
        collector = _OutputCollector(
            process.stdout, Path(log_path), log_limit=log_limit,
            capture_limit=MAX_COMMAND_OUTPUT, append=False,
        )
        collector.start()
        return ManagedProcess(process, collector)
    except BaseException as error:
        if "process" in locals():
            _stop_after_setup_failure(process)
            if collector is not None:
                if collector._thread.ident is not None:
                    collector.join()
                else:
                    collector.close_if_not_started()
        if isinstance(error, (LeaseError, KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, OSError):
            raise LeaseError(f"cannot launch direct child: {os.fspath(argv[0])}") from error
        raise LeaseError("cannot initialize bounded direct-child supervision") from error


def _validate_log_limit(log_limit: int) -> None:
    if (isinstance(log_limit, bool) or not isinstance(log_limit, int)
            or not 0 <= log_limit <= MAX_LOG_BYTES):
        raise LeaseError(f"log limit must be an integer in 0..{MAX_LOG_BYTES}")


def run_command(
    argv: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str],
    timeout: float,
    log_path: Path,
    input_data: bytes | None = None,
    cancelled: Event | None = None,
    log_limit: int = MAX_LOG_BYTES,
    append_log: bool = False,
    capture_limit: int = MAX_COMMAND_OUTPUT,
) -> CommandResult:
    """Run one direct child with bounded time, stdin, memory and disk output."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= MAX_COMMAND_TIMEOUT):
        raise LeaseError(
            f"command timeout must be finite and between 0 and {MAX_COMMAND_TIMEOUT:g}s"
        )
    if input_data is not None and not isinstance(input_data, bytes):
        raise LeaseError("command input must be bytes")
    if input_data is not None and len(input_data) > MAX_COMMAND_INPUT:
        raise LeaseError("command input exceeds the bounded 4 KiB limit")
    _validate_log_limit(log_limit)
    if (isinstance(capture_limit, bool) or not isinstance(capture_limit, int)
            or not 0 <= capture_limit <= MAX_COMMAND_OUTPUT):
        raise LeaseError("command capture limit is outside the bounded range")
    if (isinstance(argv, (str, bytes)) or not argv
            or any(not isinstance(value, (str, os.PathLike)) or not os.fspath(value)
                        for value in argv)):
        raise LeaseError("command must be a nonempty argument vector")
    try:
        process = subprocess.Popen(
            [os.fspath(value) for value in argv],
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(env),
            close_fds=True,
            bufsize=0,
        )
    except OSError as error:
        raise LeaseError(f"cannot run SDK command: {os.fspath(argv[0])}") from error

    collector = _OutputCollector(
        process.stdout, Path(log_path), log_limit=log_limit,
        capture_limit=capture_limit, append=append_log,
    )
    try:
        collector.start()
    except BaseException:
        _stop_after_setup_failure(process)
        collector.close_if_not_started()
        raise

    if input_data is not None and process.stdin is not None:
        input_error = None
        try:
            process.stdin.write(input_data)
            process.stdin.flush()
        except BrokenPipeError:
            pass
        except OSError as error:
            input_error = error
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            except OSError as error:
                input_error = input_error or error
        if input_error is not None:
            child = ManagedProcess(process, collector)
            child.stop()
            raise LeaseError("failed to send bounded input to direct child") from input_error

    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled is not None and cancelled.is_set():
                child = ManagedProcess(process, collector)
                child.stop()
                details = child.output.strip()[:1200]
                suffix = f": {details}" if details else ""
                raise CommandCancelled(f"SDK command cancelled{suffix}; bounded log: {log_path}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                child = ManagedProcess(process, collector)
                child.stop()
                details = child.output.strip()[:1200]
                suffix = f": {details}" if details else ""
                raise CommandTimeout(
                    f"SDK command timed out after {timeout:g}s{suffix}; bounded log: {log_path}"
                )
            try:
                returncode = process.wait(timeout=min(POLL_INTERVAL, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        collector.join()
    except (CommandCancelled, CommandTimeout):
        raise
    except BaseException:
        child = ManagedProcess(process, collector)
        if child.poll() is None:
            child.stop()
        raise

    if collector.error is not None:
        raise LeaseError(f"failed while recording bounded command output: {log_path}") from collector.error
    return CommandResult(returncode, collector.output, Path(log_path))
