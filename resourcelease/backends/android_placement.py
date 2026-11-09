"""Shared crash-safe placement claims for persistent Android state."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import stat
from threading import Lock, RLock, get_ident
import time
from typing import Callable, Iterator
from weakref import WeakValueDictionary

from resourcelease.model import LeaseError, QuarantineError


PLACEMENT_SCHEMA = "resource-lease-android-placement-v1"
CLAIM_PREFIX = ".resource-lease-android-name-"
MAINTENANCE_DIRECTORY_PREFIX = ".resource-lease-android-maintenance-"
MANIFEST_NAME = ".resource-lease-android-state.json"
GATE_NAME = "android-placement.lock"
MAX_CLAIM_BYTES = 8 * 1024
MAX_TREE_ENTRIES = 250_000
MAX_TREE_DEPTH = 128


class MissingPlacementReservation(QuarantineError):
    """No claim exists; validated legacy active data may be registered."""


class LegacyPlacementRegistration(LeaseError):
    """Existing active state needs validation before first reservation publication."""


_LOCAL_LOCKS_GUARD = Lock()
_LOCAL_LOCKS: WeakValueDictionary[Path, Lock] = WeakValueDictionary()


def _local_lock(path: Path) -> Lock:
    key = path.resolve(strict=False)
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _inode(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _path_info(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LeaseError("cannot safely inspect Android placement path") from error


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short placement claim write")
        view = view[written:]


class AndroidPlacement:
    """Serialize path ownership publication and retain exact sibling claims.

    Claims are immutable and intentionally survive state archive, restore, and
    retirement. The OS resolves case and normalization aliases for claim names.
    """

    def __init__(self, guard_root: str | Path, *, lock_timeout: float = 30.0):
        if (isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float))
                or not math.isfinite(lock_timeout) or lock_timeout < 0):
            raise LeaseError("Android placement lock_timeout must be a finite nonnegative number")
        self.guard_root = Path(guard_root).expanduser().absolute()
        self.lock_timeout = float(lock_timeout)
        self._instance_lock = RLock()
        self._process_lock = _local_lock(self.guard_root / GATE_NAME)
        self._owner_thread: int | None = None
        self._depth = 0
        self._gate_fd: int | None = None
        self._outer_read_only = False

    @contextmanager
    def transaction(self, *, read_only: bool = False) -> Iterator[None]:
        """Hold the shared gate for at most lock_timeout while waiting.

        Same-instance, same-thread nesting is reentrant; other threads and
        processes serialize on the same owner-private flock file.
        """
        deadline = time.monotonic() + self.lock_timeout
        if not self._instance_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise LeaseError("Android placement gate is busy")
        try:
            current_thread = get_ident()
            if self._depth:
                if self._owner_thread != current_thread:
                    raise LeaseError("Android placement transaction ownership differs")
                if self._outer_read_only and not read_only:
                    raise LeaseError(
                        "cannot publish Android placement claims in a read-only transaction"
                    )
            else:
                if not self._process_lock.acquire(
                    timeout=max(0.0, deadline - time.monotonic()),
                ):
                    raise LeaseError("Android placement gate is busy")
                try:
                    self._gate_fd = self._acquire_gate(deadline, read_only=read_only)
                except BaseException:
                    self._process_lock.release()
                    raise
                self._owner_thread = current_thread
                self._outer_read_only = read_only
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    descriptor = self._gate_fd
                    self._gate_fd = None
                    self._owner_thread = None
                    self._outer_read_only = False
                    try:
                        if descriptor is not None:
                            try:
                                fcntl.flock(descriptor, fcntl.LOCK_UN)
                            finally:
                                os.close(descriptor)
                    finally:
                        self._process_lock.release()
        finally:
            self._instance_lock.release()

    def reserve(
        self,
        directory: str | Path,
        identity: str,
        maintenance_directory: str | Path,
        *,
        legacy_validator: Callable[[], object] | None = None,
    ) -> None:
        """Publish or verify both exact sibling claims before a managed boundary.

        ``legacy_validator`` is called under the gate only when an existing
        active root has no claims. It never permits adoption of an absent
        active root with unbound maintenance metadata.
        """
        with self.transaction():
            active, maintenance, record = self._expected(directory, identity, maintenance_directory)
            claim_paths = self._claim_paths(active, maintenance)
            self._check_ancestor_claims(active, record)

            existing = [self._read_claim(path) if _path_info(path) is not None else None
                        for path in claim_paths]
            if any(item is not None for item in existing):
                if not all(item is not None for item in existing):
                    raise QuarantineError(
                        "Android placement reservation is partial; preserving all claims"
                    )
                if any(item != record for item in existing):
                    raise LeaseError(
                        "Android state path conflicts with a different placement reservation"
                    )
                return

            active_info = _path_info(active)
            maintenance_info = _path_info(maintenance)
            if active_info is not None:
                if legacy_validator is None:
                    raise LegacyPlacementRegistration(
                        "existing Android state must be validated before placement registration"
                    )
                legacy_validator()
                self._require_directory(active, active_info, "legacy Android state")
            elif maintenance_info is not None:
                raise QuarantineError(
                    "legacy Android maintenance lineage has no provable placement reservation"
                )

            if active_info is not None:
                self._check_descendant_boundaries(active, allow_missing=False)
            if maintenance_info is not None:
                self._check_descendant_boundaries(maintenance, allow_missing=False)
            for path in claim_paths:
                self._create_claim(path, record)

    def require(
        self,
        directory: str | Path,
        identity: str,
        maintenance_directory: str | Path,
    ) -> None:
        """Read-only verification of the exact existing reservation pair."""
        with self.transaction(read_only=True):
            active, maintenance, record = self._expected(directory, identity, maintenance_directory)
            self._check_ancestor_claims(active, record)
            claim_paths = self._claim_paths(active, maintenance)
            present = [_path_info(path) is not None for path in claim_paths]
            if not any(present):
                raise MissingPlacementReservation(
                    "Android placement reservation is missing; preserving state"
                )
            if not all(present):
                raise QuarantineError(
                    "Android placement reservation is partial; preserving all claims"
                )
            records = []
            for path in claim_paths:
                records.append(self._read_claim(path))
            if any(item != record for item in records):
                raise QuarantineError(
                    "Android placement reservation differs from this state path"
                )
            self._check_descendant_boundaries(active, allow_missing=True)
            self._check_descendant_boundaries(maintenance, allow_missing=True)

    def check_ancestors(
        self,
        directory: str | Path,
        identity: str,
        maintenance_directory: str | Path,
        *,
        target: str | Path | None = None,
    ) -> None:
        """Reject a target beneath another state's active or maintenance claim."""
        with self.transaction(read_only=True):
            active, _, record = self._expected(directory, identity, maintenance_directory)
            target_path = self._canonical_target(target) if target is not None else active
            self._check_ancestor_claims(target_path, record)

    def _acquire_gate(self, deadline: float, *, read_only: bool) -> int:
        try:
            info = self.guard_root.lstat()
        except FileNotFoundError:
            if read_only:
                raise QuarantineError("Android placement guard directory is missing")
            try:
                self.guard_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                info = self.guard_root.lstat()
            except OSError as error:
                raise LeaseError("cannot create Android placement guard directory") from error
        except OSError as error:
            raise LeaseError("cannot inspect Android placement guard directory") from error
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_mode & 0o7000):
            raise LeaseError("Android placement guard directory is unsafe")

        gate = self.guard_root / GATE_NAME
        created = False
        try:
            if read_only:
                descriptor = os.open(
                    gate, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
            else:
                try:
                    descriptor = os.open(
                        gate,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                    )
                    created = True
                except FileExistsError:
                    descriptor = os.open(
                        gate, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                    )
        except FileNotFoundError as error:
            raise QuarantineError("Android placement gate is missing") from error
        except OSError as error:
            raise LeaseError("cannot safely open Android placement gate") from error

        try:
            opened = os.fstat(descriptor)
            linked = gate.lstat()
            if (_inode(opened) != _inode(linked) or not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.getuid() or opened.st_mode & 0o077
                    or opened.st_mode & 0o7000 or opened.st_nlink != 1
                    or opened.st_size != 0):
                raise LeaseError("Android placement gate is unsafe")
            if created:
                _sync_directory(self.guard_root)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                        raise LeaseError("cannot acquire Android placement gate") from error
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LeaseError("Android placement gate is busy") from error
                    time.sleep(min(0.01, remaining))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _expected(
        directory: str | Path,
        identity: str,
        maintenance_directory: str | Path | None,
    ) -> tuple[Path, Path | None, dict[str, str]]:
        if (not isinstance(identity, str) or len(identity) != 64
                or any(character not in "0123456789abcdef" for character in identity)):
            raise LeaseError("Android placement identity is invalid")
        active = AndroidPlacement._canonical_target(directory)
        if active == Path("/") or not active.name:
            raise LeaseError("Android state path cannot be the filesystem root")

        if maintenance_directory is None:
            maintenance = None
        else:
            maintenance = AndroidPlacement._canonical_target(maintenance_directory)
            if (maintenance.parent != active.parent
                    or not maintenance.name.startswith(MAINTENANCE_DIRECTORY_PREFIX)):
                raise LeaseError("Android maintenance boundary is not a sibling of its state")
            if active.name == maintenance.name:
                raise LeaseError("Android state and maintenance boundaries collide")

        record = {
            "schema": PLACEMENT_SCHEMA,
            "state_identity": identity,
            "active_path": str(active),
            "maintenance_path": str(maintenance) if maintenance is not None else "",
        }
        return active, maintenance, record

    @staticmethod
    def _canonical_target(value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() or "\x00" in os.fspath(candidate):
            raise LeaseError("Android placement path must be absolute")
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            try:
                parent = candidate.parent.resolve(strict=True)
                parent_info = parent.stat()
            except (OSError, RuntimeError) as error:
                raise LeaseError("Android placement parent is unavailable") from error
            if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid():
                raise LeaseError("Android placement parent is unsafe")
            result = parent / candidate.name
        except (OSError, RuntimeError) as error:
            raise LeaseError("cannot inspect Android placement path") from error
        else:
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise LeaseError("Android placement boundary is not an owned directory")
            result = candidate.resolve(strict=True)
        if not result.name or result.name in {".", ".."}:
            raise LeaseError("Android placement boundary name is invalid")
        return result

    @staticmethod
    def _claim_paths(active: Path, maintenance: Path | None) -> tuple[Path, ...]:
        boundaries = [active]
        if maintenance is not None:
            boundaries.append(maintenance)
        paths = []
        try:
            maximum = os.pathconf(active.parent, "PC_NAME_MAX")
        except (OSError, ValueError) as error:
            raise LeaseError("cannot determine Android placement reservation name limit") from error
        for boundary in boundaries:
            name = CLAIM_PREFIX + boundary.name
            if len(os.fsencode(name)) > maximum:
                raise LeaseError("Android placement reservation name exceeds filesystem limit")
            paths.append(boundary.parent / name)
        if len({path.name for path in paths}) != len(paths):
            raise LeaseError("Android placement reservation names collide")
        return tuple(paths)

    def _check_ancestor_claims(self, active: Path, expected: dict[str, str]) -> None:
        chain = [active, *active.parents]
        for boundary in chain:
            if boundary != active:
                if self._is_maintenance_boundary(boundary):
                    own_maintenance = expected["maintenance_path"] and self._same_path(
                        boundary, Path(expected["maintenance_path"]),
                    )
                    if not own_maintenance:
                        raise LeaseError(
                            "Android state cannot be placed inside a maintenance boundary"
                        )
                if _path_info(boundary / MANIFEST_NAME) is not None:
                    raise LeaseError(
                        "Android state cannot be placed inside another managed state"
                    )
            claim = boundary.parent / (CLAIM_PREFIX + boundary.name)
            if _path_info(claim) is None:
                continue
            record = self._read_claim(claim)
            owner_boundary = self._claim_boundary(claim, record)
            matching_owner = (
                record["schema"] == expected["schema"]
                and record["state_identity"] == expected["state_identity"]
                and record["active_path"] == expected["active_path"]
                and (
                    not expected["maintenance_path"]
                    or record["maintenance_path"] == expected["maintenance_path"]
                )
            )
            if matching_owner and boundary == owner_boundary:
                continue
            if boundary == active:
                raise QuarantineError(
                    "Android path spelling conflicts with a placement reservation"
                )
            raise LeaseError(
                "Android state path conflicts with or is nested inside a placement reservation"
            )

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        if left == right:
            return True
        try:
            return os.path.samefile(left, right)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise QuarantineError(
                "cannot safely compare Android placement boundary aliases"
            ) from error

    @classmethod
    def _is_maintenance_boundary(cls, boundary: Path) -> bool:
        prefix = MAINTENANCE_DIRECTORY_PREFIX
        if boundary.name.startswith(prefix):
            return True
        if boundary.name[:len(prefix)].lower() != prefix.lower():
            return False
        canonical_spelling = boundary.parent / (
            prefix + boundary.name[len(prefix):]
        )
        return cls._same_path(boundary, canonical_spelling)

    @classmethod
    def _claim_boundary(cls, claim: Path, record: dict[str, str]) -> Path:
        boundaries = [
            Path(record["active_path"]),
            Path(record["maintenance_path"]),
        ]
        for boundary in boundaries:
            expected_claim = boundary.parent / (CLAIM_PREFIX + boundary.name)
            if cls._same_path(claim, expected_claim):
                return boundary
        raise QuarantineError(
            "Android placement claim no longer identifies its recorded boundary"
        )

    def _read_claim(self, path: Path) -> dict[str, str]:
        try:
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_mode & 0o7000
                    or before.st_nlink != 1 or before.st_size > MAX_CLAIM_BYTES):
                raise QuarantineError("Android placement claim is unsafe; preserving it")
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except (OSError, QuarantineError) as error:
            if isinstance(error, QuarantineError):
                raise
            raise QuarantineError("Android placement claim is unavailable; preserving it") from error
        try:
            opened = os.fstat(descriptor)
            if (_inode(opened) != _inode(before) or not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.getuid() or opened.st_nlink != 1
                    or opened.st_mode & 0o077 or opened.st_size > MAX_CLAIM_BYTES):
                raise QuarantineError("Android placement claim changed during inspection")
            chunks = []
            remaining = MAX_CLAIM_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(4096, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            linked = path.lstat()
            content = b"".join(chunks)
            if (_inode(after) != _inode(opened) or _inode(linked) != _inode(opened)
                    or len(content) > MAX_CLAIM_BYTES or after.st_size != len(content)):
                raise QuarantineError("Android placement claim changed during inspection")
        except OSError as error:
            raise QuarantineError("cannot read Android placement claim; preserving it") from error
        finally:
            os.close(descriptor)

        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise QuarantineError("Android placement claim is malformed; preserving it") from error
        fields = {"schema", "state_identity", "active_path", "maintenance_path"}
        if (not isinstance(value, dict) or set(value) != fields
                or any(not isinstance(value.get(field), str) for field in fields)
                or value["schema"] != PLACEMENT_SCHEMA
                or len(value["state_identity"]) != 64
                or any(ch not in "0123456789abcdef" for ch in value["state_identity"])
                or not Path(value["active_path"]).is_absolute()
                or value["maintenance_path"]
                and not Path(value["maintenance_path"]).is_absolute()):
            raise QuarantineError("Android placement claim is incompatible; preserving it")
        active = Path(value["active_path"])
        maintenance = Path(value["maintenance_path"]) if value["maintenance_path"] else None
        if maintenance is None or (
            active.parent != maintenance.parent
            or not maintenance.name.startswith(MAINTENANCE_DIRECTORY_PREFIX)
        ):
            raise QuarantineError("Android placement claim boundaries are inconsistent")
        expected_paths = (
            active.parent / (CLAIM_PREFIX + active.name),
            maintenance.parent / (CLAIM_PREFIX + maintenance.name),
        )
        try:
            belongs = any(os.path.samefile(path, expected) for expected in expected_paths)
        except OSError as error:
            raise QuarantineError("cannot verify Android placement claim name") from error
        if not belongs:
            raise QuarantineError("Android placement claim name differs from its record")
        return value

    def _create_claim(self, path: Path, record: dict[str, str]) -> None:
        content = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(content) > MAX_CLAIM_BYTES:
            raise LeaseError("Android placement reservation record exceeds its size limit")
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except OSError as error:
            raise QuarantineError(
                "Android placement claim publication is uncertain; preserving all state"
            ) from error
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except OSError as error:
            raise QuarantineError(
                "Android placement claim write is uncertain; preserving the partial claim"
            ) from error
        finally:
            os.close(descriptor)
        try:
            _sync_directory(path.parent)
        except OSError as error:
            raise QuarantineError(
                "Android placement claim durability is uncertain; preserving it"
            ) from error

    @staticmethod
    def _require_directory(path: Path, info: os.stat_result, label: str) -> None:
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_mode & 0o7000):
            raise LeaseError(f"{label} is unsafe")

    @staticmethod
    def _check_descendant_boundaries(directory: Path, *, allow_missing: bool) -> None:
        info = _path_info(directory)
        if info is None:
            if allow_missing:
                return
            raise LeaseError("Android placement root disappeared")
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise LeaseError("Android placement root is unsafe")
        root_fd = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(root_fd)
        if _inode(opened) != _inode(info):
            os.close(root_fd)
            raise LeaseError("Android placement root changed during inspection")
        count = [1]

        def walk(descriptor: int, depth: int, components: tuple[str, ...]) -> None:
            if depth > MAX_TREE_DEPTH:
                raise LeaseError("Android placement tree exceeds the traversal depth bound")
            try:
                names = os.listdir(descriptor)
            except OSError as error:
                raise LeaseError("cannot safely inspect Android placement descendants") from error
            for name in names:
                count[0] += 1
                if count[0] > MAX_TREE_ENTRIES:
                    raise LeaseError("Android placement tree exceeds the traversal entry bound")
                try:
                    child_info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except OSError as error:
                    raise LeaseError("Android placement tree changed during inspection") from error
                if child_info.st_uid != os.getuid() or child_info.st_dev != opened.st_dev:
                    raise LeaseError("Android placement tree contains foreign or cross-device data")
                path_components = components + (name,)
                if (name.startswith(CLAIM_PREFIX)
                        or name.startswith(MAINTENANCE_DIRECTORY_PREFIX)
                        and stat.S_ISDIR(child_info.st_mode)):
                    raise LeaseError("Android placement contains a nested managed boundary")
                is_managed_manifest = name == MANIFEST_NAME
                if is_managed_manifest:
                    backup_root_manifest = (
                        len(path_components) == 2
                        and path_components[0].startswith("backup-")
                    )
                    if depth > 0 and not backup_root_manifest:
                        raise LeaseError(
                            "Android placement contains a nested managed-state manifest"
                        )
                if stat.S_ISDIR(child_info.st_mode):
                    try:
                        child = os.open(
                            name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=descriptor,
                        )
                    except OSError as error:
                        raise LeaseError("Android placement directory changed during inspection") from error
                    try:
                        child_opened = os.fstat(child)
                        if _inode(child_opened) != _inode(child_info):
                            raise LeaseError(
                                "Android placement directory changed during inspection"
                            )
                        walk(child, depth + 1, path_components)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(child_info.st_mode):
                    if child_info.st_nlink != 1:
                        raise LeaseError("Android placement tree contains a hard-linked file")
                else:
                    raise LeaseError("Android placement tree contains a symlink or special file")

        try:
            walk(root_fd, 0, ())
        finally:
            os.close(root_fd)
