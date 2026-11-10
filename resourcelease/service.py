"""Independent resource supervision with one direct-child owner per slot."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
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

from .model import Backend, LeaseError, MaintenanceBackend, QuarantineError, Resource, Slot
from .namespace import default_guard_root
from .operations import Operations
from .store import Store, canonical, private_directory


class Busy(LeaseError):
    """A live service holds the physical resource."""


@dataclass
class _OwnedResource:
    resource: Resource | None = None


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

    def __init__(self, root: Path, keys: tuple[str, ...], pool_id: str, lease_id: str,
                 *, maintenance_key: str | None = None, recovery: bool = False):
        self.root = root
        self.keys = tuple(sorted(keys))
        self.maintenance = maintenance_key is not None
        if recovery and not self.maintenance:
            raise LeaseError("only typed maintenance guards support recovery")
        self.recovery = recovery
        if maintenance_key is not None:
            if maintenance_key not in keys:
                raise LeaseError("maintenance key must be a physical resource key")
            self.keys = (maintenance_key, *(key for key in self.keys if key != maintenance_key))
        marker = (
            {"schema": 2, "kind": "maintenance", "pool": pool_id, "operation": lease_id}
            if self.maintenance else {"schema": 1, "pool": pool_id, "lease": lease_id}
        )
        self.marker = (canonical(marker) + "\n").encode()
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
            if self.recovery:
                self._recovery_markers()
                return self
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

    def _recovery_markers(self) -> list[bool]:
        present = []
        for descriptor in self.descriptors:
            os.lseek(descriptor, 0, os.SEEK_SET)
            value = os.read(descriptor, len(self.marker) + 1)
            if value not in (b"", self.marker):
                raise QuarantineError("recovery found a foreign or malformed physical marker")
            present.append(bool(value))
        return present

    def activate_recovery(self, *, requires_state_marker: bool) -> None:
        """Called only after the adapter positively classified exact physical evidence."""
        if not self.recovery or not self.descriptors:
            raise LeaseError("recovery guard is not held")
        present = self._recovery_markers()
        if requires_state_marker and not present[0]:
            raise QuarantineError("uncommitted recovery requires its original state marker")
        for descriptor, exists in zip(self.descriptors, present, strict=True):
            if not exists:
                os.lseek(descriptor, 0, os.SEEK_SET)
                written = os.write(descriptor, self.marker)
                if written != len(self.marker):
                    raise QuarantineError("maintenance recovery marker write was incomplete")
                os.fsync(descriptor)
        self.marked = True

    def clear(self):
        # Check every marker before clearing any: never remove another owner's record.
        for descriptor in self.descriptors:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.read(descriptor, len(self.marker) + 1) != self.marker:
                raise QuarantineError("physical interlock changed during resource use")
        order = reversed(self.descriptors) if self.maintenance else iter(self.descriptors)
        for descriptor in order:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
        self.marked = False

    def __exit__(self, *_):
        for context in reversed(self.contexts):
            context.__exit__(None, None, None)
        self.contexts.clear()
        self.descriptors.clear()


class Service:
    def __init__(self, store: Store, backends: Mapping[str, Backend], *, guard_root=None,
                 maintenance_backends: Mapping[str, MaintenanceBackend] | None = None):
        self.store = store
        self.backends = backends
        self.maintenance_backends = dict(maintenance_backends or {})
        self.operations = Operations(store)
        self.epoch: str | None = None
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
        self.guard_root = private_directory(guard_root or default_guard_root())

    def request_shutdown(self, *_):
        self.cancelled.set()

    def serve(self, *, install_signals=False):
        with locked_file(self.store.root / "service.lock"):
            epoch = self.store.service_start()
            self.epoch = epoch
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
                                operation = self.operations.pending(slot.id)
                                if operation is not None:
                                    cancellation = Event()
                                    thread = Thread(
                                        target=self._maintenance_job,
                                        args=(slot, operation, cancellation),
                                        name=f"maintenance-{slot.id}", daemon=False,
                                    )
                                    self.jobs[slot.id] = (thread, cancellation)
                                    thread.start()
                                    continue
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

    def _state_operation(self, slot: Slot, operation: Mapping) -> dict:
        backend = self.maintenance_backends.get(slot.backend)
        if backend is None:
            raise LeaseError("backend does not support state maintenance")
        state = backend.maintenance(slot)
        action = operation["action"]
        custody = {"pool_id": self.store.pool_id, "lease_id": operation["lease_id"]}
        if action == "inspect":
            return dict(state.inspect())
        revision = operation["expected_revision"]
        if action == "reset":
            return dict(state.archive(operation["id"], revision, custody))
        if action == "restore":
            return dict(state.restore(
                operation["id"], operation["backup_id"], revision, custody,
            ))
        if action == "retire":
            return dict(state.retire(
                operation["id"], operation["backup_id"], revision, custody,
            ))
        raise LeaseError("unsupported state maintenance action")

    def _execution(self, operation: Mapping) -> dict:
        if operation["execution_epoch"] != self.epoch:
            raise LeaseError("maintenance belongs to another service execution")
        return {"execution_epoch": self.epoch,
                "execution_attempt": operation["execution_attempt"]}

    def _maintenance_job(self, slot: Slot, operation: Mapping, cancelled: Event):
        identifier = operation["id"]
        result = None
        error = None
        quarantine = False
        execution = self._execution(operation)
        recovery = operation["execution_kind"] == "recovery"
        began = False
        try:
            self.operations.begin(identifier, **execution)
            began = True
            backend = self.maintenance_backends.get(slot.backend)
            if backend is None:
                raise LeaseError("backend does not support state maintenance")
            key = backend.maintenance_key(slot)
            with Guard(self.guard_root, self.keys[slot.id], self.store.pool_id,
                       identifier, maintenance_key=key, recovery=recovery) as guard:
                try:
                    if cancelled.is_set():
                        raise LeaseError("maintenance cancelled before state mutation")
                    if recovery:
                        proof = backend.maintenance(slot).classify_recovery(
                            identifier, operation["action"], operation["expected_revision"],
                            operation["backup_id"],
                            {"pool_id": self.store.pool_id, "lease_id": None},
                        )
                        guard.activate_recovery(
                            requires_state_marker=proof["requires_state_marker"],
                        )
                        if proof["outcome"] == "resume":
                            result = self._state_operation(slot, operation)
                        elif proof["outcome"] in ("completed", "failed"):
                            result, error = proof["result"], proof["error"]
                        else:
                            raise QuarantineError("backend returned an invalid recovery outcome")
                    else:
                        result = self._state_operation(slot, operation)
                    self.operations.set_phase(
                        identifier, "physical_committed", result, **execution,
                    )
                except BaseException as failure:
                    error = failure
                    quarantine = recovery or not isinstance(failure, LeaseError) or isinstance(
                        failure, QuarantineError,
                    )
                if not quarantine:
                    guard.clear()
        except Busy as failure:
            error = failure
            quarantine = recovery
        except BaseException as failure:
            error = failure
            quarantine = recovery or not isinstance(failure, LeaseError) or isinstance(
                failure, QuarantineError,
            )
        finally:
            if began:
                self.operations.complete(
                    identifier, result, error=error, quarantine=quarantine, **execution,
                )

    def _owned_operation(self, slot: Slot, lease_id: str, operation: Mapping,
                         owned: _OwnedResource, generations: Path, cancelled: Event):
        identifier = operation["id"]
        execution = self._execution(operation)
        self.operations.begin(identifier, **execution)
        if operation["action"] == "inspect":
            try:
                result = self._state_operation(slot, operation)
            except QuarantineError:
                raise
            except LeaseError as failure:
                self.operations.complete(identifier, None, error=failure, **execution)
            else:
                self.operations.complete(identifier, result, **execution)
            return
        if operation["action"] != "reset":
            raise LeaseError("only inspection and reset are supported under a lease")
        # Only this worker owns the live process handles. They are never adopted
        # by an independent maintenance worker or released between generations.
        if owned.resource is None:
            raise LeaseError("reset requires a ready owned resource")
        try:
            owned.resource.stop()
        except BaseException as failure:
            raise QuarantineError("old resource stop could not be verified") from failure
        owned.resource = None
        result = None
        try:
            if cancelled.is_set() or not self.store.desired(lease_id):
                raise LeaseError("lease ended before state reset")
            result = self._state_operation(slot, operation)
            self.operations.set_phase(identifier, "physical_committed", result, **execution)
            if cancelled.is_set() or not self.store.desired(lease_id):
                raise LeaseError("lease ended before replacement startup")
            generation = self.operations.advance_generation(identifier, **execution)
            directory = generations / lease_id / str(generation)
            directory.mkdir(mode=0o700)
            owned.resource = self.backends[slot.backend].start(slot, directory, cancelled)
            if not self.store.publish(
                lease_id, owned.resource.endpoint, operation_id=identifier, generation=generation,
            ):
                try:
                    owned.resource.stop()
                except BaseException as failure:
                    raise QuarantineError("replacement stop could not be verified") from failure
                owned.resource = None
                raise LeaseError("lease ended before replacement publication")
            self.operations.complete(identifier, result, **execution)
        except QuarantineError:
            raise
        except LeaseError as failure:
            if owned.resource is not None:
                try:
                    owned.resource.stop()
                except BaseException as stop_failure:
                    raise QuarantineError("failed replacement cleanup could not be verified") from stop_failure
                owned.resource = None
            self.operations.complete(identifier, result, error=failure, **execution)

    def _job(self, slot: Slot, lease_id: str, cancelled: Event):
        guard = Guard(self.guard_root, self.keys[slot.id], self.store.pool_id, lease_id)
        owned = _OwnedResource()
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
                    owned.resource = self.backends[slot.backend].start(slot, directory, cancelled)
                    if self.store.publish(lease_id, owned.resource.endpoint):
                        while not cancelled.wait(0.2):
                            operation = self.operations.pending(slot.id, lease_id)
                            if operation is not None:
                                self._owned_operation(
                                    slot, lease_id, operation, owned, generations, cancelled,
                                )
                            if owned.resource is not None and not owned.resource.healthy():
                                raise LeaseError("owned resource exited unexpectedly")
                    self.store.stopping(lease_id)
                except BaseException as failure:
                    error = failure
                    # Ordinary backend failures promise completed cleanup; unexpected
                    # failures without a handle cannot make that promise.
                    if owned.resource is None and not isinstance(failure, LeaseError):
                        quarantine = True
                    if isinstance(failure, QuarantineError):
                        quarantine = True
                finally:
                    if owned.resource is not None:
                        try:
                            owned.resource.stop()
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
            operation = self.operations.pending(slot.id, lease_id)
            if operation is not None:
                self.operations.fail_for_lease(
                    lease_id, error or "lease ended before maintenance completed", quarantine,
                    **self._execution(operation),
                )
            self.store.finish(lease_id, error=error, quarantine=quarantine, busy=busy)
