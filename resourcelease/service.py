"""Independent resource supervision with one direct-child owner per slot."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import shutil
import stat
from threading import Event, Thread
import time
from typing import Mapping

from .model import Backend, LeaseError, QuarantineError, Slot
from .store import Store, canonical, private_directory


class Busy(LeaseError):
    """A live service holds the physical resource."""


def home() -> Path:
    """One per-user exclusion namespace, independent of an individual pool."""
    configured = os.environ.get("RESOURCE_LEASE_HOME")
    return Path(configured).expanduser().absolute() if configured else (
        Path.home() / ".local" / "state" / "resource-lease")


@contextmanager
def locked_file(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise LeaseError("unsafe lock file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Busy("resource is held by another running service") from error
        yield descriptor
    finally:
        os.close(descriptor)


class Guard:
    """An OS lock plus a crash interlock, not a second lease scheduler."""

    def __init__(self, root: Path, keys: tuple[str, ...], pool_id: str, lease_id: str):
        self.root = root
        self.keys = tuple(sorted(keys))
        self.marker = (canonical({"schema": 1, "pool": pool_id, "lease": lease_id}) + "\n").encode()
        self.contexts = []
        self.descriptors = []
        self.marked = False

    def __enter__(self):
        try:
            for key in self.keys:
                context = locked_file(self.root / (hashlib.sha256(key.encode()).hexdigest() + ".lock"))
                descriptor = context.__enter__()
                self.contexts.append(context)
                self.descriptors.append(descriptor)
            if any(os.fstat(fd).st_size for fd in self.descriptors):
                raise QuarantineError("physical resource has an unresolved crash marker; do not reset it")
            for descriptor in self.descriptors:
                os.write(descriptor, self.marker)
                os.fsync(descriptor)
            self.marked = True
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def clear(self):
        # Check every marker before clearing any: never remove another owner's record.
        for descriptor in self.descriptors:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.read(descriptor, len(self.marker) + 1) != self.marker:
                raise QuarantineError("physical interlock changed during resource use")
        for descriptor in self.descriptors:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
        self.marked = False

    def __exit__(self, *_):
        for context in reversed(self.contexts):
            context.__exit__(None, None, None)
        self.contexts.clear()
        self.descriptors.clear()


class Service:
    def __init__(self, store: Store, backends: Mapping[str, Backend], *, guard_root=None):
        self.store = store
        self.backends = backends
        self.cancelled = Event()
        self.jobs: dict[str, tuple[Thread, Event]] = {}
        self.keys = {}
        occupied = set()
        for slot in store.config.slots:
            if slot.backend not in backends:
                raise LeaseError(f"unsupported backend: {slot.backend}")
            keys = backends[slot.backend].keys(slot)
            if (not keys or len(keys) > 16 or any(not isinstance(key, str) or not 1 <= len(key) <= 256
                                                for key in keys)
                    or len(set(keys)) != len(keys) or occupied.intersection(keys)):
                raise LeaseError("slots require nonoverlapping canonical physical resource keys")
            occupied.update(keys)
            self.keys[slot.id] = keys
        self.guard_root = private_directory(guard_root or home() / "guards")

    def request_shutdown(self, *_):
        self.cancelled.set()

    def serve(self, *, install_signals=False):
        with locked_file(self.store.root / "service.lock"):
            epoch = self.store.service_start()
            previous = {}
            if install_signals:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    previous[sig] = signal.signal(sig, self.request_shutdown)
            try:
                while True:
                    if self.cancelled.is_set():
                        self.store.shutdown()
                    self._prune_artifacts()
                    stopping = self.store.service_tick(epoch)
                    for slot_id, (thread, _) in list(self.jobs.items()):
                        if not thread.is_alive():
                            thread.join()
                            del self.jobs[slot_id]
                    if stopping:
                        for _, cancellation in self.jobs.values():
                            cancellation.set()
                        if not self.jobs:
                            break
                    else:
                        for slot in self.store.config.slots:
                            if slot.id not in self.jobs:
                                lease_id = self.store.assign(slot.id)
                                if lease_id:
                                    cancellation = Event()
                                    thread = Thread(target=self._job, args=(slot, lease_id, cancellation),
                                                    name=f"lease-{slot.id}", daemon=False)
                                    self.jobs[slot.id] = (thread, cancellation)
                                    thread.start()
                    self.cancelled.wait(0.2) if not stopping else time.sleep(0.2)
            finally:
                for _, cancellation in self.jobs.values():
                    cancellation.set()
                for thread, _ in self.jobs.values():
                    thread.join()
                self.store.service_stop(epoch)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)

    def _prune_artifacts(self):
        for lease_id in self.store.old_artifacts():
            directory = self.store.root / "generations" / lease_id
            if directory.exists():
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077):
                    raise LeaseError("refusing to clean an unsafe completed generation directory")
                # IDs originate from this database; only completed, nonquarantined
                # generations reach here. rmtree does not follow child symlinks.
                shutil.rmtree(directory)
            self.store.artifacts_removed(lease_id)

    def _job(self, slot: Slot, lease_id: str, cancelled: Event):
        guard = Guard(self.guard_root, self.keys[slot.id], self.store.pool_id, lease_id)
        resource = None
        error = None
        quarantine = False
        busy = False
        # Start observes cancellation independently of potentially slow SDK work.
        monitoring_done = Event()

        def monitor():
            while not monitoring_done.wait(0.1):
                try:
                    if not self.store.desired(lease_id):
                        cancelled.set()
                        return
                except Exception:
                    cancelled.set()
                    return

        observer = Thread(target=monitor, name=f"lease-cancellation-{slot.id}", daemon=True)
        observer.start()
        try:
            with guard:
                generations = private_directory(self.store.root / "generations")
                directory = generations / lease_id
                directory.mkdir(mode=0o700)  # Never adopt an existing generation directory.
                try:
                    if cancelled.is_set() or not self.store.desired(lease_id):
                        raise LeaseError("lease cancelled before resource start")
                    resource = self.backends[slot.backend].start(slot, directory, cancelled)
                    if self.store.publish(lease_id, resource.endpoint):
                        while not cancelled.wait(0.2):
                            if not resource.healthy():
                                raise LeaseError("owned resource exited unexpectedly")
                    self.store.stopping(lease_id)
                except BaseException as failure:
                    error = failure
                    # Ordinary backend failures promise completed cleanup; unexpected
                    # failures without a handle cannot make that promise.
                    if resource is None and not isinstance(failure, LeaseError):
                        quarantine = True
                    if isinstance(failure, QuarantineError):
                        quarantine = True
                finally:
                    if resource is not None:
                        try:
                            resource.stop()
                        except BaseException as failure:
                            error = failure
                            quarantine = True
                if not quarantine:
                    guard.clear()
        except Busy as failure:
            error, busy = failure, True
            # Do not spin a new boot worker for every scheduler tick under contention.
            cancelled.wait(0.5)
        except BaseException as failure:
            error, quarantine = failure, True
        finally:
            monitoring_done.set()
            observer.join()
            self.store.finish(lease_id, error=error, quarantine=quarantine, busy=busy)
