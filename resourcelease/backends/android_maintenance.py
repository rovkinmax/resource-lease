"""Guarded, crash-aware maintenance for persistent Android AVD state."""
from __future__ import annotations

import errno
import ctypes
from functools import wraps
import json
import os
from pathlib import Path
import stat
import sys
from typing import Mapping
import uuid

from resourcelease.backends.android_state import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    MAX_AVD_INI_BYTES,
    MAX_CONFIG_BYTES,
    MAX_MANIFEST_BYTES,
    PersistentAndroidState,
    _read_private_file,
    _regular_file,
    _require_owned_directory,
    _require_private_directory,
    _sync_directory,
    _write_all,
    canonical_state_directory,
    state_identity,
)
from resourcelease.backends.android_placement import (
    CLAIM_PREFIX as PLACEMENT_CLAIM_PREFIX,
    AndroidPlacement,
)
from resourcelease.model import LeaseError, QuarantineError, name
from resourcelease.namespace import default_guard_root


MAINTENANCE_SCHEMA = "resource-lease-android-maintenance-v1"
MAINTENANCE_DIRECTORY_PREFIX = ".resource-lease-android-maintenance-"
JOURNAL_NAME = "journal.json"
MAX_JOURNAL_BYTES = 32 * 1024
# V1 bounds metadata traversal independently of the large opaque AVD image files.
MAX_TREE_ENTRIES = 250_000
MAX_TREE_DEPTH = 128

_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_UNRESOLVED_PHASES = frozenset({
    "ARCHIVE_INTENT", "RESTORE_INTENT", "RETIRE_INTENT",
})


def _inode(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino]


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _path_stat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LeaseError(f"cannot safely inspect Android state path {path.name}") from error


def _rename_no_replace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename one entry without ever replacing a destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        flag = 0x00000004  # RENAME_EXCL
        unavailable = "renameatx_np exclusive rename is unavailable"
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        flag = 0x00000001  # RENAME_NOREPLACE
        unavailable = "renameat2 exclusive rename is unavailable"
    else:
        raise OSError(errno.ENOTSUP, "exclusive directory rename is unsupported")
    if function is None:
        raise OSError(errno.ENOSYS, unavailable)

    function.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _open_directory(path: Path, expected: list[int] | None = None) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise LeaseError(f"cannot safely open Android maintenance directory {path.name}") from error
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid()
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or (expected is not None and _inode(opened) != expected)):
            raise LeaseError(f"Android maintenance directory {path.name} changed during inspection")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _read_json_file(path: Path, label: str, maximum: int) -> dict[str, object]:
    try:
        content = _read_private_file(path, label, maximum)
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LeaseError(f"{label} is malformed; preserving Android state") from error
    if not isinstance(value, dict):
        raise LeaseError(f"{label} is invalid; preserving Android state")
    return value


def _placement_guarded(*, read_only: bool = False):
    def decorate(method):
        @wraps(method)
        def guarded(self, *args, **kwargs):
            with self.placement.transaction(read_only=read_only):
                if read_only:
                    self._require_placement()
                else:
                    self._ensure_placement()
                return method(self, *args, **kwargs)
        return guarded
    return decorate


class AndroidMaintenance:
    """Archive, restore, or retire one persistent AVD under caller-held guards.

    The caller must retain the Android state and required port guards from
    ``check_start`` through normal startup and ``publish_complete``. This class
    never acquires guards, starts processes, or adopts process identifiers.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        system_image: str,
        image_identity: Mapping[str, object],
        memory_mb: int,
        placement: AndroidPlacement | None = None,
    ) -> None:
        self.directory = canonical_state_directory(os.fspath(directory))
        self.identity = state_identity(self.directory)
        self.placement = placement or AndroidPlacement(default_guard_root())
        if not isinstance(system_image, str) or not system_image or len(system_image) > 512:
            raise LeaseError("Android maintenance system_image is invalid")
        if (isinstance(memory_mb, bool) or not isinstance(memory_mb, int)
                or not 512 <= memory_mb <= 8192):
            raise LeaseError("Android maintenance memory_mb is invalid")
        if not isinstance(image_identity, Mapping):
            raise LeaseError("Android maintenance image_identity is invalid")
        self.profile: dict[str, object] = {
            "system_image": system_image,
            "image_identity": dict(image_identity),
            "memory_mb": memory_mb,
        }
        try:
            encoded_profile = json.dumps(
                self.profile, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise LeaseError("Android maintenance image_identity is not JSON-compatible") from error
        if len(encoded_profile) > 8 * 1024:
            raise LeaseError("Android maintenance profile is oversized")

        self._maintenance_directory = self.directory.parent / (
            MAINTENANCE_DIRECTORY_PREFIX + self.identity
        )
        self._checked = False
        self._checked_revision: str | None = None
        self._checked_had_journal = False

    @property
    def _avd_name(self) -> str:
        return f"resource_lease_{self.identity[:16]}"

    def _ensure_placement(self) -> None:
        self.placement.reserve(
            self.directory,
            self.identity,
            self._maintenance_directory,
            legacy_validator=self._open_active,
        )

    def _require_placement(self) -> None:
        self.placement.require(
            self.directory, self.identity, self._maintenance_directory,
        )

    @_placement_guarded()
    def inspect(self) -> dict[str, object]:
        """Return current lineage, bootstrapping a validated legacy state."""
        journal = self._ensure_journal()
        journal = self._require_settled(journal, publish_completed=True)
        self._validate_backup(journal)
        return self._snapshot(journal)

    @_placement_guarded()
    def check_start(self) -> None:
        """Fail closed on unresolved maintenance before ordinary AVD startup."""
        self._checked = False
        self._checked_revision = None
        self._checked_had_journal = False

        if _path_stat(self._maintenance_directory) is None:
            if _path_stat(self.directory) is not None:
                journal = self._bootstrap_journal()
                journal = self._require_settled(journal, publish_completed=True)
                self._validate_backup(journal)
                self._remember_check(journal)
            else:
                self._checked = True
            return None

        journal = self._read_journal()
        if self._is_unresolved(journal):
            raise QuarantineError("persistent Android maintenance operation is unresolved")
        journal = self._require_settled(journal, publish_completed=True)
        self._validate_backup(journal)
        self._remember_check(journal)
        return None

    @_placement_guarded()
    def publish_complete(self) -> dict[str, object]:
        """Publish successful ordinary initialization on this checked instance."""
        if not self._checked:
            raise LeaseError("Android maintenance startup was not checked under its guard")
        self._checked = False
        if _path_stat(self.directory) is None:
            raise LeaseError("persistent Android state is absent after successful startup")
        self._open_active()

        if _path_stat(self._maintenance_directory) is None:
            if self._checked_had_journal:
                raise QuarantineError("persistent Android lineage disappeared during startup")
            journal = self._bootstrap_journal()
            if journal["state"] != "complete":
                raise QuarantineError("persistent Android initialization was not published")
            self._remember_check(journal)
            return self._snapshot(journal)

        journal = self._read_journal()
        if self._is_unresolved(journal):
            raise QuarantineError("persistent Android maintenance operation is unresolved")
        if journal["revision"] != self._checked_revision:
            raise QuarantineError("persistent Android lineage changed during startup")
        if journal["state"] == "complete":
            self._validate_backup(journal)
            return self._snapshot(journal)
        if journal["state"] != "empty":
            raise QuarantineError("persistent Android lineage has an invalid state")

        published = self._publish_complete_journal(journal)
        return self._snapshot(published)

    @_placement_guarded()
    def archive(
        self,
        operation_id: str,
        expected_revision: str,
        custody: Mapping[str, object],
    ) -> dict[str, object]:
        """Move a complete AVD to the one retained backup, idempotently."""
        operation_id = name(operation_id, "operation_id")
        custody_record = self._custody(custody)
        journal = self._ensure_journal()
        prior = journal.get("operation")
        if isinstance(prior, dict) and prior.get("id") == operation_id:
            self._require_same_operation(
                prior, "archive", expected_revision, custody_record, backup_id=None,
            )
            if prior["phase"] in {"ARCHIVE_COMMITTED", "ARCHIVE_PUBLISHED", "ARCHIVE_EMPTY",
                                  "ARCHIVE_EMPTY_PUBLISHED"}:
                return dict(prior["archive_result"])
            if prior["phase"] == "ARCHIVE_NO_MUTATION":
                raise LeaseError("the prior archive attempt settled with no mutation")
            if prior["phase"] == "ARCHIVE_INTENT":
                return self._resume_archive(journal)
            raise LeaseError("operation_id is already bound to another maintenance operation")

        journal = self._require_settled(journal, publish_completed=True)
        self._validate_backup(journal)
        if journal["revision"] != expected_revision:
            raise LeaseError("persistent Android state revision differs")
        if journal["backup"] is not None:
            raise LeaseError("a retained Android backup must be explicitly retired first")

        active_info = _path_stat(self.directory)
        if active_info is None:
            if journal["state"] != "empty":
                raise QuarantineError("complete persistent Android state disappeared")
            result = self._snapshot(journal, outcome="already_empty")
            operation = {
                "id": operation_id,
                "kind": "archive",
                "phase": "ARCHIVE_EMPTY",
                "expected_revision": expected_revision,
                "result_revision": journal["revision"],
                "custody": custody_record,
                "backup_id": None,
                "archive_result": result,
            }
            replacement = dict(journal)
            replacement["operation"] = operation
            return self._snapshot(self._write_journal(journal, replacement), outcome="already_empty")

        self._open_active()
        if journal["state"] != "complete":
            raise QuarantineError("persistent Android state and lineage disagree")
        self._scan_tree(self.directory, expected_inode=_inode(active_info))

        backup_id = str(uuid.uuid4())
        destination = self._backup_path(backup_id)
        if _path_stat(destination) is not None:
            raise LeaseError("Android backup destination already exists")
        result_revision = str(uuid.uuid4())
        result = {
            "revision": result_revision,
            "state": "empty",
            "backup_id": backup_id,
        }
        operation = {
            "id": operation_id,
            "kind": "archive",
            "phase": "ARCHIVE_INTENT",
            "expected_revision": expected_revision,
            "result_revision": result_revision,
            "custody": custody_record,
            "backup_id": backup_id,
            "inode": _inode(active_info),
            "archive_result": result,
        }
        replacement = dict(journal)
        replacement["operation"] = operation
        journal = self._write_journal(journal, replacement)
        return self._resume_archive(journal)

    @_placement_guarded()
    def restore(
        self,
        operation_id: str,
        backup_id: str,
        expected_revision: str,
        custody: Mapping[str, object],
    ) -> dict[str, object]:
        """Restore the named complete backup only when the original path is absent."""
        operation_id = name(operation_id, "operation_id")
        backup_id = self._validate_backup_id(backup_id)
        custody_record = self._custody(custody)
        journal = self._ensure_journal()
        prior = journal.get("operation")
        if isinstance(prior, dict) and prior.get("id") == operation_id:
            self._require_same_operation(
                prior, "restore", expected_revision, custody_record, backup_id=backup_id,
            )
            if prior["phase"] == "RESTORE_COMMITTED":
                return dict(prior["result_snapshot"])
            if prior["phase"] == "RESTORE_NO_MUTATION":
                raise LeaseError("the prior restore attempt settled with no mutation")
            if prior["phase"] == "RESTORE_INTENT":
                return self._resume_restore(journal)
            raise LeaseError("operation_id is already bound to another maintenance operation")
        if isinstance(prior, dict) and prior.get("phase") == "RETIRE_INTENT":
            raise LeaseError("Android backup is retiring and cannot be restored")
        if _path_stat(self.directory) is not None:
            raise LeaseError("persistent Android state path already exists; preserving replacement")

        journal = self._require_settled(journal, publish_completed=True)
        if journal["revision"] != expected_revision:
            raise LeaseError("persistent Android state revision differs")
        backup = journal["backup"]
        if not isinstance(backup, dict) or backup["id"] != backup_id:
            raise LeaseError("the requested Android backup is not retained")
        self._validate_backup(journal)
        operation = {
            "id": operation_id,
            "kind": "restore",
            "phase": "RESTORE_INTENT",
            "expected_revision": expected_revision,
            "result_revision": str(uuid.uuid4()),
            "custody": custody_record,
            "backup_id": backup_id,
            "inode": backup["inode"],
        }
        replacement = dict(journal)
        replacement["operation"] = operation
        journal = self._write_journal(journal, replacement)
        return self._resume_restore(journal)

    def retire(
        self,
        operation_id: str,
        backup_id: str,
        expected_revision: str,
        custody: Mapping[str, object],
    ) -> dict[str, object]:
        """Irreversibly remove only the explicitly identified retained backup."""
        operation_id = name(operation_id, "operation_id")
        backup_id = self._validate_backup_id(backup_id)
        custody_record = self._custody(custody)
        with self.placement.transaction():
            self._ensure_placement()
            journal = self._ensure_journal()
            prior = journal.get("operation")
            if isinstance(prior, dict) and prior.get("id") == operation_id:
                self._require_same_operation(
                    prior, "retire", expected_revision, custody_record, backup_id=backup_id,
                )
                if prior["phase"] == "RETIRE_COMMITTED":
                    return dict(prior["result_snapshot"])
                if prior["phase"] != "RETIRE_INTENT":
                    raise LeaseError(
                        "operation_id is already bound to another maintenance operation"
                    )
            else:
                journal = self._require_settled(journal, publish_completed=True)
                if journal["revision"] != expected_revision:
                    raise LeaseError("persistent Android state revision differs")
                backup = journal["backup"]
                if not isinstance(backup, dict) or backup["id"] != backup_id:
                    raise LeaseError("the requested Android backup is not retained")
                self._validate_backup(journal)
                operation = {
                    "id": operation_id,
                    "kind": "retire",
                    "phase": "RETIRE_INTENT",
                    "expected_revision": expected_revision,
                    "result_revision": str(uuid.uuid4()),
                    "custody": custody_record,
                    "backup_id": backup_id,
                    "inode": backup["inode"],
                }
                replacement = dict(journal)
                replacement["operation"] = operation
                journal = self._write_journal(journal, replacement)
        return self._resume_retire(journal)

    @_placement_guarded(read_only=True)
    def classify_recovery(
        self,
        operation_id: str,
        action: str,
        expected_revision: str,
        backup_id: str | None,
        custody: Mapping[str, object],
    ) -> dict[str, object]:
        """Classify an exact journaled operation without changing filesystem state."""
        try:
            operation_id = name(operation_id, "operation_id")
            if action not in {"reset", "restore", "retire"}:
                raise LeaseError("operation action cannot be recovered by Android maintenance")
            if not _is_uuid(expected_revision):
                raise LeaseError("expected state revision is invalid")
            custody_record = self._custody(custody)
            kind = "archive" if action == "reset" else action
            if action == "reset":
                if backup_id is not None:
                    raise LeaseError("reset recovery must not name a backup")
                expected_backup_id = None
            else:
                expected_backup_id = self._validate_backup_id(backup_id)

            # Read the existing journal directly. Recovery classification must never
            # bootstrap absent lineage or publish any result.
            journal = self._read_journal()
            operation = journal.get("operation")
            if (not isinstance(operation, dict)
                    or operation.get("id") != operation_id
                    or operation.get("kind") != kind
                    or operation.get("expected_revision") != expected_revision
                    or operation.get("custody") != custody_record):
                raise QuarantineError(
                    "Android recovery does not match the current journaled operation"
                )
            if action != "reset" and operation.get("backup_id") != expected_backup_id:
                raise QuarantineError("Android recovery backup identity differs")

            phase = operation.get("phase")
            self._validate_recovery_operation(journal, operation, phase)
            self._validate_recovery_topology(journal, operation, phase)

            if phase in _UNRESOLVED_PHASES:
                return {
                    "outcome": "resume",
                    "result": None,
                    "error": None,
                    "requires_state_marker": True,
                }
            if phase in {"ARCHIVE_NO_MUTATION", "RESTORE_NO_MUTATION"}:
                label = "archive" if phase == "ARCHIVE_NO_MUTATION" else "restore"
                return {
                    "outcome": "failed",
                    "result": None,
                    "error": f"Android {label} settled without mutation",
                    "requires_state_marker": False,
                }
            if phase in {
                "ARCHIVE_COMMITTED", "ARCHIVE_PUBLISHED",
                "ARCHIVE_EMPTY", "ARCHIVE_EMPTY_PUBLISHED",
            }:
                result = dict(operation["archive_result"])
            else:
                result = dict(operation["result_snapshot"])
            return {
                "outcome": "completed",
                "result": result,
                "error": None,
                "requires_state_marker": False,
            }
        except QuarantineError:
            raise
        except (LeaseError, OSError, TypeError, ValueError, KeyError, AttributeError) as error:
            raise QuarantineError(
                "Android recovery evidence is invalid or incomplete; preserving state"
            ) from error

    def _validate_recovery_operation(
        self,
        journal: Mapping[str, object],
        operation: Mapping[str, object],
        phase: object,
    ) -> None:
        if not isinstance(phase, str):
            raise QuarantineError("Android recovery phase is invalid")
        common = {
            "id", "kind", "phase", "expected_revision",
            "result_revision", "custody", "backup_id",
        }
        kind = operation["kind"]
        if kind == "archive":
            fields_by_phase = {
                "ARCHIVE_INTENT": common | {"inode", "archive_result"},
                "ARCHIVE_COMMITTED": common | {"inode", "archive_result"},
                "ARCHIVE_PUBLISHED": common | {
                    "inode", "archive_result", "published_result",
                },
                "ARCHIVE_EMPTY": common | {"archive_result"},
                "ARCHIVE_EMPTY_PUBLISHED": common | {
                    "archive_result", "published_result",
                },
                "ARCHIVE_NO_MUTATION": common | {"inode", "archive_result"},
            }
        elif kind == "restore":
            fields_by_phase = {
                "RESTORE_INTENT": common | {"inode"},
                "RESTORE_NO_MUTATION": common | {"inode"},
                "RESTORE_COMMITTED": common | {"inode", "result_snapshot"},
            }
        elif kind == "retire":
            fields_by_phase = {
                "RETIRE_INTENT": common | {"inode"},
                "RETIRE_COMMITTED": common | {"inode", "result_snapshot"},
            }
        else:
            raise QuarantineError("Android recovery operation kind is unsupported")
        fields = fields_by_phase.get(phase)
        if fields is None or set(operation) != fields:
            raise QuarantineError("Android recovery operation fields or phase differ")
        if (not _is_uuid(operation.get("result_revision"))
                or operation.get("expected_revision") != journal.get("revision")
                and phase in _UNRESOLVED_PHASES | {
                    "ARCHIVE_NO_MUTATION", "RESTORE_NO_MUTATION",
                }):
            raise QuarantineError("Android recovery revision binding is inconsistent")

        empty_archive = phase in {
            "ARCHIVE_EMPTY", "ARCHIVE_EMPTY_PUBLISHED", "ARCHIVE_NO_MUTATION",
        }
        if empty_archive:
            if operation.get("backup_id") is not None:
                raise QuarantineError("Android recovery unexpectedly names a backup")
        elif not _is_uuid(operation.get("backup_id")):
            raise QuarantineError("Android recovery backup identifier is invalid")
        if phase in _UNRESOLVED_PHASES | {
            "ARCHIVE_COMMITTED", "ARCHIVE_PUBLISHED", "ARCHIVE_NO_MUTATION",
            "RESTORE_NO_MUTATION", "RESTORE_COMMITTED",
            "RETIRE_INTENT", "RETIRE_COMMITTED",
        } and not self._valid_inode(operation.get("inode")):
            raise QuarantineError("Android recovery inode binding is invalid")

        if kind == "archive":
            result = operation.get("archive_result")
            expected_backup = (
                result.get("backup_id")
                if phase == "ARCHIVE_NO_MUTATION" and isinstance(result, dict)
                else operation.get("backup_id")
            )
            self._validate_recovery_snapshot(
                result,
                state="empty",
                backup_id=expected_backup,
                revision=operation["result_revision"],
                allow_outcome=phase in {"ARCHIVE_EMPTY", "ARCHIVE_EMPTY_PUBLISHED"},
            )
            if phase in {"ARCHIVE_EMPTY", "ARCHIVE_EMPTY_PUBLISHED"}:
                if (operation["result_revision"] != operation["expected_revision"]
                        or result.get("outcome") != "already_empty"):
                    raise QuarantineError("empty archive result is inconsistent")
            elif "outcome" in result:
                raise QuarantineError("non-empty archive result has an unexpected outcome")
            if (phase == "ARCHIVE_NO_MUTATION"
                    and not _is_uuid(result.get("backup_id"))):
                raise QuarantineError("settled archive attempt result is invalid")
            if phase in {"ARCHIVE_PUBLISHED", "ARCHIVE_EMPTY_PUBLISHED"}:
                self._validate_recovery_snapshot(
                    operation.get("published_result"),
                    state="complete",
                    backup_id=operation.get("backup_id"),
                    revision=journal.get("revision"),
                    allow_outcome=False,
                )
        elif phase == "RESTORE_COMMITTED":
            self._validate_recovery_snapshot(
                operation.get("result_snapshot"),
                state="complete",
                backup_id=None,
                revision=operation["result_revision"],
                allow_outcome=False,
            )
        elif phase == "RETIRE_COMMITTED":
            self._validate_recovery_snapshot(
                operation.get("result_snapshot"),
                state=journal.get("state"),
                backup_id=None,
                revision=operation["result_revision"],
                allow_outcome=False,
            )

    @staticmethod
    def _validate_recovery_snapshot(
        value: object,
        *,
        state: object,
        backup_id: object,
        revision: object,
        allow_outcome: bool,
    ) -> None:
        if not isinstance(value, dict):
            raise QuarantineError("Android recovery result snapshot is invalid")
        expected_fields = {"revision", "state", "backup_id"}
        if "outcome" in value and allow_outcome:
            expected_fields.add("outcome")
        if (set(value) != expected_fields
                or value.get("state") != state
                or value.get("backup_id") != backup_id
                or value.get("revision") != revision
                or not _is_uuid(value.get("revision"))
                or (backup_id is not None and not _is_uuid(backup_id))):
            raise QuarantineError("Android recovery result snapshot differs from its journal")

    def _validate_recovery_topology(
        self,
        journal: Mapping[str, object],
        operation: Mapping[str, object],
        phase: object,
    ) -> None:
        active = self._path_stat_for_recovery(self.directory, "active Android state")
        operation_backup_id = operation.get("backup_id")
        backup_path = (
            self._backup_path(operation_backup_id)
            if isinstance(operation_backup_id, str) else None
        )
        backup = (
            self._path_stat_for_recovery(backup_path, "Android backup")
            if backup_path is not None else None
        )

        if phase in {"ARCHIVE_INTENT", "ARCHIVE_NO_MUTATION"}:
            if (journal["state"] != "complete"
                    or journal["revision"] != operation["expected_revision"]
                    or journal["backup"] is not None):
                raise QuarantineError("Android archive intent lineage differs")
            inode = operation["inode"]
            if phase == "ARCHIVE_NO_MUTATION":
                if active is None or backup is not None:
                    raise QuarantineError("settled archive no-mutation topology differs")
                self._validate_recovery_active(inode)
                return
            if active is not None and backup is None:
                self._validate_recovery_active(inode)
                return
            if active is None and backup is not None:
                self._validate_recovery_archived(backup_path, inode)
                return
            raise QuarantineError("Android archive intent paths do not match its inode")

        if phase in {"ARCHIVE_COMMITTED", "ARCHIVE_PUBLISHED"}:
            if phase == "ARCHIVE_COMMITTED":
                if (journal["state"] != "empty"
                        or journal["revision"] != operation["result_revision"]
                        or active is not None):
                    raise QuarantineError("committed Android archive topology differs")
            elif (journal["state"] != "complete" or active is None):
                raise QuarantineError("published Android archive topology differs")
            self._validate_recovery_backup_record(journal, operation)
            self._validate_recovery_archived(backup_path, operation["inode"])
            if phase == "ARCHIVE_PUBLISHED":
                self._validate_recovery_active(None)
            return

        if phase in {"ARCHIVE_EMPTY", "ARCHIVE_EMPTY_PUBLISHED"}:
            if journal["backup"] is not None:
                raise QuarantineError("empty archive unexpectedly retains a backup")
            if phase == "ARCHIVE_EMPTY":
                if (journal["state"] != "empty" or active is not None
                        or journal["revision"] != operation["expected_revision"]):
                    raise QuarantineError("empty archive topology differs")
            elif journal["state"] != "complete" or active is None:
                raise QuarantineError("published empty archive topology differs")
            if phase == "ARCHIVE_EMPTY_PUBLISHED":
                self._validate_recovery_active(None)
            return

        if phase in {"RESTORE_INTENT", "RESTORE_NO_MUTATION"}:
            if (journal["state"] != "empty"
                    or journal["revision"] != operation["expected_revision"]):
                raise QuarantineError("Android restore intent lineage differs")
            self._validate_recovery_backup_record(journal, operation)
            inode = operation["inode"]
            if phase == "RESTORE_NO_MUTATION":
                if active is not None or backup is None:
                    raise QuarantineError("settled restore no-mutation topology differs")
                self._validate_recovery_archived(backup_path, inode)
            elif backup is not None and active is None:
                self._validate_recovery_archived(backup_path, inode)
            elif backup is None and active is not None:
                self._validate_recovery_active(inode)
            else:
                raise QuarantineError("Android restore intent paths do not match its inode")
            return

        if phase == "RESTORE_COMMITTED":
            if (journal["state"] != "complete"
                    or journal["revision"] != operation["result_revision"]
                    or journal["backup"] is not None
                    or backup is not None or active is None):
                raise QuarantineError("committed Android restore topology differs")
            self._validate_recovery_active(operation["inode"])
            return

        if phase == "RETIRE_INTENT":
            if (journal["revision"] != operation["expected_revision"]
                    or active is not None and journal["state"] != "complete"
                    or active is None and journal["state"] != "empty"):
                raise QuarantineError("Android retirement intent lineage differs")
            self._validate_recovery_backup_record(journal, operation)
            if active is not None:
                self._validate_recovery_active(None)
            if backup is not None:
                self._validate_recovery_retiring_tree(backup_path, operation["inode"])
            return

        if phase == "RETIRE_COMMITTED":
            if (journal["revision"] != operation["result_revision"]
                    or journal["backup"] is not None or backup is not None):
                raise QuarantineError("committed Android retirement topology differs")
            if journal["state"] == "complete":
                if active is None:
                    raise QuarantineError("retired Android state is unexpectedly absent")
                self._validate_recovery_active(None)
            elif active is not None:
                raise QuarantineError("retired empty Android state unexpectedly exists")
            return

        raise QuarantineError("Android recovery phase is unsupported")

    def _validate_recovery_backup_record(
        self,
        journal: Mapping[str, object],
        operation: Mapping[str, object],
    ) -> None:
        backup = journal.get("backup")
        if (not isinstance(backup, dict)
                or backup.get("id") != operation.get("backup_id")
                or backup.get("directory") != f"backup-{operation.get('backup_id')}"
                or backup.get("inode") != operation.get("inode")
                or not isinstance(backup.get("archive_operation_id"), str)):
            raise QuarantineError("Android recovery backup journal binding differs")
        try:
            name(backup["archive_operation_id"], "archive_operation_id")
        except LeaseError as error:
            raise QuarantineError("Android archive operation identity is invalid") from error
        if operation["kind"] == "archive" and (
            backup.get("archive_operation_id") != operation.get("id")
            or backup.get("custody") != operation.get("custody")
        ):
            raise QuarantineError("Android archive custody differs from its backup")

    def _path_stat_for_recovery(self, path: Path, label: str) -> os.stat_result | None:
        try:
            return _path_stat(path)
        except LeaseError as error:
            raise QuarantineError(f"cannot safely inspect {label}") from error

    def _validate_recovery_active(self, expected_inode: list[int] | None) -> None:
        info = self._path_stat_for_recovery(self.directory, "active Android state")
        if (info is None or not stat.S_ISDIR(info.st_mode)
                or expected_inode is not None and _inode(info) != expected_inode):
            raise QuarantineError("active Android state does not match recovery evidence")
        try:
            self._open_active()
            self._scan_tree(self.directory, expected_inode=_inode(info))
        except (LeaseError, OSError) as error:
            raise QuarantineError("active Android state is not valid recovery evidence") from error

    def _validate_recovery_archived(
        self,
        path: Path,
        expected_inode: list[int],
    ) -> None:
        info = self._path_stat_for_recovery(path, "archived Android state")
        if (info is None or not stat.S_ISDIR(info.st_mode)
                or _inode(info) != expected_inode):
            raise QuarantineError("archived Android state does not match recovery evidence")
        try:
            self._validate_archived_payload(path)
            self._scan_tree(path, expected_inode=expected_inode)
        except (LeaseError, OSError) as error:
            raise QuarantineError("archived Android state is not valid recovery evidence") from error

    def _validate_recovery_retiring_tree(
        self,
        path: Path,
        expected_inode: list[int],
    ) -> None:
        info = self._path_stat_for_recovery(path, "retiring Android backup")
        if (info is None or not stat.S_ISDIR(info.st_mode)
                or _inode(info) != expected_inode):
            raise QuarantineError("retiring Android backup inode differs")
        try:
            _require_private_directory(path, "retiring Android backup")
            self._scan_tree(path, expected_inode=expected_inode)
        except (LeaseError, OSError) as error:
            raise QuarantineError("retiring Android backup is unsafe") from error

    def _custody(self, value: Mapping[str, object]) -> dict[str, str | None]:
        if not isinstance(value, Mapping) or set(value) - {"pool_id", "lease_id"}:
            raise LeaseError("maintenance custody must contain pool_id and optional lease_id")
        pool_id = value.get("pool_id")
        lease_id = value.get("lease_id")
        if not isinstance(pool_id, str) or not pool_id or len(pool_id) > 128 or "\x00" in pool_id:
            raise LeaseError("maintenance custody pool_id is invalid")
        if (lease_id is not None
                and (not isinstance(lease_id, str) or not lease_id or len(lease_id) > 128
                     or "\x00" in lease_id)):
            raise LeaseError("maintenance custody lease_id is invalid")
        return {"pool_id": pool_id, "lease_id": lease_id}

    @staticmethod
    def _validate_backup_id(value: str) -> str:
        if not _is_uuid(value):
            raise LeaseError("backup_id must be an exact retained backup UUID")
        return value

    def _backup_path(self, backup_id: str) -> Path:
        return self._maintenance_directory / f"backup-{backup_id}"

    def _snapshot(
        self,
        journal: Mapping[str, object],
        *,
        outcome: str | None = None,
    ) -> dict[str, object]:
        backup = journal["backup"]
        result: dict[str, object] = {
            "revision": journal["revision"],
            "state": journal["state"],
            "backup_id": backup["id"] if isinstance(backup, dict) else None,
        }
        if outcome is not None:
            result["outcome"] = outcome
        return result

    def _ensure_journal(self) -> dict[str, object]:
        if _path_stat(self._maintenance_directory) is None:
            return self._bootstrap_journal()
        return self._read_journal()

    def _bootstrap_journal(self) -> dict[str, object]:
        active = _path_stat(self.directory)
        if active is None:
            state = "empty"
        else:
            self._open_active()
            self._scan_tree(self.directory, expected_inode=_inode(active))
            state = "complete"

        try:
            self._maintenance_directory.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            return self._read_journal()
        except OSError as error:
            raise LeaseError("cannot create private Android maintenance directory") from error
        try:
            _require_private_directory(
                self._maintenance_directory, "Android maintenance directory",
            )
            _sync_directory(self.directory.parent)
            journal: dict[str, object] = {
                "schema": MAINTENANCE_SCHEMA,
                "owner": "resource-lease",
                "state_identity": self.identity,
                "profile": self.profile,
                "revision": str(uuid.uuid4()),
                "state": state,
                "backup": None,
                "operation": None,
            }
            self._create_journal(journal)
            return journal
        except QuarantineError:
            raise
        except BaseException as error:
            raise QuarantineError(
                "Android maintenance bootstrap is incomplete; preserving all state"
            ) from error

    def _create_journal(self, journal: dict[str, object]) -> None:
        content = self._encode_journal(journal)
        path = self._maintenance_directory / JOURNAL_NAME
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except OSError as error:
            raise QuarantineError("cannot exclusively create Android maintenance journal") from error
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except OSError as error:
            raise QuarantineError("Android maintenance journal bootstrap is uncertain") from error
        finally:
            os.close(descriptor)
        try:
            _sync_directory(self._maintenance_directory)
        except OSError as error:
            raise QuarantineError("Android maintenance journal publication is uncertain") from error

    def _read_journal(self) -> dict[str, object]:
        try:
            root_info = _require_private_directory(
                self._maintenance_directory, "Android maintenance directory",
            )
            entries = set(os.listdir(self._maintenance_directory))
            journal = _read_json_file(
                self._maintenance_directory / JOURNAL_NAME,
                "Android maintenance journal",
                MAX_JOURNAL_BYTES,
            )
        except (OSError, LeaseError) as error:
            raise QuarantineError(
                "Android maintenance metadata is incomplete or unsafe; preserving state"
            ) from error
        if root_info.st_dev != self.directory.parent.stat().st_dev:
            raise QuarantineError("Android maintenance area is not on the state filesystem")
        self._validate_journal(journal)

        expected_entries = {JOURNAL_NAME}
        backup = journal["backup"]
        operation = journal["operation"]
        expected_backup_id = backup["id"] if isinstance(backup, dict) else None
        if isinstance(operation, dict) and operation["phase"] in {
            "ARCHIVE_INTENT", "RESTORE_INTENT", "RETIRE_INTENT",
        }:
            expected_backup_id = operation["backup_id"]
        if expected_backup_id is not None:
            backup_path = self._backup_path(expected_backup_id)
            if _path_stat(backup_path) is not None:
                expected_entries.add(backup_path.name)
        if entries != expected_entries:
            raise QuarantineError(
                "Android maintenance area contains unknown or partial entries; preserving them"
            )
        return journal

    def _validate_journal(self, journal: dict[str, object]) -> None:
        if set(journal) != {
            "schema", "owner", "state_identity", "profile", "revision",
            "state", "backup", "operation",
        }:
            raise QuarantineError("Android maintenance journal fields are incompatible")
        if (journal.get("schema") != MAINTENANCE_SCHEMA
                or journal.get("owner") != "resource-lease"
                or journal.get("state_identity") != self.identity
                or not _is_uuid(journal.get("revision"))
                or not isinstance(journal.get("state"), str)
                or journal["state"] not in {"empty", "complete"}):
            raise QuarantineError("Android maintenance journal is incompatible")
        if journal.get("profile") != self.profile:
            raise LeaseError("persistent Android state profile differs; preserving state")
        backup = journal.get("backup")
        if backup is not None:
            if (not isinstance(backup, dict)
                    or set(backup) != {
                        "id", "directory", "inode", "archive_operation_id", "custody",
                    }
                    or not _is_uuid(backup.get("id"))
                    or backup.get("directory") != f"backup-{backup.get('id')}"
                    or not self._valid_inode(backup.get("inode"))
                    or not isinstance(backup.get("archive_operation_id"), str)):
                raise QuarantineError("Android maintenance backup metadata is invalid")
            try:
                if self._custody(backup.get("custody")) != backup.get("custody"):
                    raise LeaseError("custody changed")
            except (LeaseError, TypeError) as error:
                raise QuarantineError("Android maintenance backup custody is invalid") from error

        operation = journal.get("operation")
        if operation is not None:
            if (not isinstance(operation, dict)
                    or not isinstance(operation.get("id"), str)
                    or not isinstance(operation.get("kind"), str)
                    or not isinstance(operation.get("phase"), str)
                    or operation["kind"] not in {"archive", "restore", "retire", "initialize"}):
                raise QuarantineError("Android maintenance operation journal is invalid")
            allowed_phases = {
                "archive": {
                    "ARCHIVE_INTENT", "ARCHIVE_COMMITTED", "ARCHIVE_PUBLISHED",
                    "ARCHIVE_EMPTY", "ARCHIVE_EMPTY_PUBLISHED", "ARCHIVE_NO_MUTATION",
                },
                "restore": {"RESTORE_INTENT", "RESTORE_COMMITTED", "RESTORE_NO_MUTATION"},
                "retire": {"RETIRE_INTENT", "RETIRE_COMMITTED"},
                "initialize": {"INITIALIZE_COMMITTED"},
            }[operation["kind"]]
            if (operation["phase"] not in allowed_phases
                    or operation["phase"] in _UNRESOLVED_PHASES
                    and operation["kind"] not in {"archive", "restore", "retire"}):
                raise QuarantineError("Android maintenance operation phase is invalid")
            if operation.get("kind") != "initialize":
                try:
                    name(operation["id"], "operation_id")
                    self._custody(operation.get("custody"))
                except (LeaseError, KeyError, TypeError) as error:
                    raise QuarantineError("Android maintenance operation binding is invalid") from error
                if not isinstance(operation.get("expected_revision"), str):
                    raise QuarantineError("Android maintenance expected revision is invalid")
            if operation["kind"] in {"archive", "restore", "retire"}:
                backup_id = operation.get("backup_id")
                if operation["kind"] == "archive" and operation["phase"] in {
                    "ARCHIVE_EMPTY", "ARCHIVE_EMPTY_PUBLISHED", "ARCHIVE_NO_MUTATION",
                }:
                    if backup_id is not None:
                        raise QuarantineError("empty archive unexpectedly names a backup")
                elif not _is_uuid(backup_id):
                    raise QuarantineError("Android maintenance operation backup ID is invalid")
            if operation["phase"] in _UNRESOLVED_PHASES and not self._valid_inode(
                operation.get("inode"),
            ):
                raise QuarantineError("unresolved Android maintenance inode is invalid")

    @staticmethod
    def _valid_inode(value: object) -> bool:
        return (
            isinstance(value, list) and len(value) == 2
            and all(type(item) is int and item >= 0 for item in value)
        )

    @staticmethod
    def _is_unresolved(journal: Mapping[str, object]) -> bool:
        operation = journal.get("operation")
        return isinstance(operation, dict) and operation.get("phase") in _UNRESOLVED_PHASES

    def _require_settled(
        self,
        journal: dict[str, object],
        *,
        publish_completed: bool,
    ) -> dict[str, object]:
        if self._is_unresolved(journal):
            raise QuarantineError("persistent Android maintenance operation is unresolved")
        active = _path_stat(self.directory)
        if active is None:
            if journal["state"] == "complete":
                raise QuarantineError("complete persistent Android state disappeared")
            return journal
        self._open_active()
        self._scan_tree(self.directory, expected_inode=_inode(active))
        if journal["state"] == "complete":
            return journal
        if not publish_completed:
            raise QuarantineError("complete Android state has not been published to lineage")
        return self._publish_complete_journal(journal)

    def _remember_check(self, journal: Mapping[str, object]) -> None:
        self._checked = True
        self._checked_revision = str(journal["revision"])
        self._checked_had_journal = True

    def _publish_complete_journal(self, journal: dict[str, object]) -> dict[str, object]:
        if journal["state"] == "complete":
            return journal
        active = _path_stat(self.directory)
        if active is None:
            raise LeaseError("persistent Android state is absent")
        self._open_active()
        self._scan_tree(self.directory, expected_inode=_inode(active))

        result_revision = str(uuid.uuid4())
        replacement = dict(journal)
        replacement["revision"] = result_revision
        replacement["state"] = "complete"
        prior = journal.get("operation")
        result = {
            "revision": result_revision,
            "state": "complete",
            "backup_id": (
                journal["backup"]["id"] if isinstance(journal["backup"], dict) else None
            ),
        }
        if isinstance(prior, dict) and prior.get("kind") == "archive":
            prior = dict(prior)
            if prior["phase"] in {"ARCHIVE_COMMITTED", "ARCHIVE_EMPTY"}:
                prior["phase"] = (
                    "ARCHIVE_PUBLISHED" if prior["phase"] == "ARCHIVE_COMMITTED"
                    else "ARCHIVE_EMPTY_PUBLISHED"
                )
            prior["published_result"] = result
            replacement["operation"] = prior
        else:
            replacement["operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "initialize",
                "phase": "INITIALIZE_COMMITTED",
                "expected_revision": journal["revision"],
                "result_revision": result_revision,
                "result_snapshot": result,
            }
        try:
            return self._write_journal(journal, replacement)
        except LeaseError as error:
            raise QuarantineError(
                "complete persistent state could not be durably published"
            ) from error

    def _validate_backup(self, journal: Mapping[str, object]) -> None:
        backup = journal["backup"]
        if not isinstance(backup, dict):
            return
        operation = journal.get("operation")
        if isinstance(operation, dict) and operation.get("phase") == "RETIRE_INTENT":
            return
        path = self._backup_path(backup["id"])
        info = _path_stat(path)
        if info is None or _inode(info) != backup["inode"]:
            raise QuarantineError("retained Android backup is missing or changed")
        self._validate_archived_payload(path)
        self._scan_tree(path, expected_inode=backup["inode"])

    def _validate_archived_payload(self, directory: Path) -> None:
        try:
            _require_private_directory(directory, "archived persistent Android state")
            manifest = _read_json_file(
                directory / MANIFEST_NAME, "archived Android manifest", MAX_MANIFEST_BYTES,
            )
            if (set(manifest) != {
                    "schema", "owner", "state_identity", "profile", "avd_name", "status",
                }
                    or manifest.get("schema") != MANIFEST_SCHEMA
                    or manifest.get("owner") != "resource-lease"
                    or manifest.get("state_identity") != self.identity
                    or manifest.get("profile") != self.profile
                    or manifest.get("avd_name") != self._avd_name
                    or manifest.get("status") != "complete"):
                raise LeaseError("archived persistent Android manifest differs")

            private_home = directory / "android-home"
            avd_home = private_home / "avd"
            avd_path = avd_home / f"{self._avd_name}.avd"
            _require_private_directory(private_home, "archived persistent Android home")
            _require_private_directory(avd_home, "archived persistent Android AVD home")
            _require_owned_directory(avd_path, "archived persistent Android AVD")
            _regular_file(
                avd_path / "config.ini", "archived persistent Android AVD config",
                MAX_CONFIG_BYTES,
            )
            locator = avd_home / f"{self._avd_name}.ini"
            data = _read_private_file(
                locator, "archived persistent Android AVD locator",
                MAX_AVD_INI_BYTES, private=False,
            )
            try:
                lines = data.decode("utf-8").splitlines()
            except UnicodeDecodeError as error:
                raise LeaseError("archived Android AVD locator is invalid") from error
            locations = [
                line.split("=", 1)[1].strip()
                for line in lines
                if "=" in line and line.split("=", 1)[0].strip() == "path"
            ]
            if len(locations) != 1 or not Path(locations[0]).is_absolute():
                raise LeaseError("archived Android AVD locator is incomplete")
            expected = self.directory / "android-home" / "avd" / f"{self._avd_name}.avd"
            if Path(locations[0]).resolve(strict=False) != expected:
                raise LeaseError("archived Android AVD locator no longer names the original path")
        except (OSError, LeaseError) as error:
            raise QuarantineError(
                "retained Android backup is incomplete or incompatible; preserving it"
            ) from error

    def _open_active(self) -> PersistentAndroidState:
        try:
            return PersistentAndroidState._open_existing(
                self.directory, self.identity, self._avd_name, self.profile,
            )
        except LeaseError:
            raise
        except OSError as error:
            raise LeaseError("cannot validate persistent Android state") from error

    def _scan_tree(
        self,
        directory: Path,
        *,
        expected_inode: list[int] | None = None,
    ) -> int:
        self._reject_managed_ancestor(directory)
        root_info = _path_stat(directory)
        if (root_info is None or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.getuid()
                or (expected_inode is not None and _inode(root_info) != expected_inode)):
            raise LeaseError("Android state tree root is unsafe or changed")
        root_fd, opened = _open_directory(directory, expected_inode)
        count = 1

        def walk(descriptor: int, depth: int) -> None:
            nonlocal count
            if depth > MAX_TREE_DEPTH:
                raise LeaseError("Android state tree exceeds the traversal depth bound")
            try:
                entries = os.listdir(descriptor)
            except OSError as error:
                raise LeaseError("cannot safely traverse Android state tree") from error
            for entry_name in entries:
                count += 1
                if count > MAX_TREE_ENTRIES:
                    raise LeaseError("Android state tree exceeds the traversal entry bound")
                try:
                    info = os.stat(entry_name, dir_fd=descriptor, follow_symlinks=False)
                except OSError as error:
                    raise LeaseError("Android state tree changed during traversal") from error
                if info.st_uid != os.getuid() or info.st_dev != opened.st_dev:
                    raise LeaseError("Android state tree contains foreign or cross-device data")
                if (
                    depth > 0 and entry_name == MANIFEST_NAME
                    or entry_name.startswith(PLACEMENT_CLAIM_PREFIX)
                    or entry_name.startswith(MAINTENANCE_DIRECTORY_PREFIX)
                    and stat.S_ISDIR(info.st_mode)
                ):
                    raise LeaseError(
                        "Android state tree contains a nested managed-state boundary"
                    )
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child = os.open(entry_name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    except OSError as error:
                        raise LeaseError("Android state tree directory changed during traversal") from error
                    try:
                        opened_child = os.fstat(child)
                        if (opened_child.st_dev, opened_child.st_ino) != (info.st_dev, info.st_ino):
                            raise LeaseError("Android state tree directory changed during traversal")
                        walk(child, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise LeaseError("Android state tree contains a hard-linked file")
                else:
                    raise LeaseError("Android state tree contains a symlink or special file")

        try:
            walk(root_fd, 0)
        finally:
            os.close(root_fd)
        return count

    def _reject_managed_ancestor(self, directory: Path) -> None:
        self.placement.check_ancestors(
            self.directory,
            self.identity,
            self._maintenance_directory,
            target=directory,
        )
        for ancestor in directory.parents:
            if ancestor == self._maintenance_directory:
                continue
            if ancestor.name.startswith(MAINTENANCE_DIRECTORY_PREFIX):
                raise LeaseError("Android state tree is nested inside a maintenance boundary")
            if _path_stat(ancestor / MANIFEST_NAME) is not None:
                raise LeaseError("Android state tree is nested inside another managed state")

    def _backup_preflight(self, backup_id: str) -> tuple[Path, list[int]]:
        path = self._backup_path(backup_id)
        info = _path_stat(path)
        if info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise QuarantineError("Android backup path is missing or unsafe")
        return path, _inode(info)

    def _require_same_operation(
        self,
        operation: Mapping[str, object],
        kind: str,
        expected_revision: str,
        custody: Mapping[str, object],
        *,
        backup_id: str | None,
    ) -> None:
        if (operation.get("kind") != kind
                or operation.get("expected_revision") != expected_revision
                or operation.get("custody") != custody
                or backup_id is not None and operation.get("backup_id") != backup_id):
            raise LeaseError("operation_id is already bound to different operation custody")

    def _resume_archive(self, journal: dict[str, object]) -> dict[str, object]:
        operation = journal["operation"]
        if not isinstance(operation, dict) or operation.get("phase") != "ARCHIVE_INTENT":
            raise QuarantineError("Android archive journal is not resumable")
        backup_id = operation["backup_id"]
        source_info = _path_stat(self.directory)
        target = self._backup_path(backup_id)
        target_info = _path_stat(target)
        expected_inode = operation["inode"]
        if source_info is not None and _inode(source_info) == expected_inode and target_info is None:
            try:
                self._scan_tree(self.directory, expected_inode=expected_inode)
                self._rename_directory(self.directory, target, expected_inode)
            except QuarantineError:
                raise
            except (OSError, LeaseError) as error:
                source_now = _path_stat(self.directory)
                target_now = _path_stat(target)
                if (source_now is not None and _inode(source_now) == expected_inode
                        and target_now is None):
                    self._settle_no_mutation(journal, "ARCHIVE_NO_MUTATION")
                    raise LeaseError("Android archive failed without mutation") from error
                raise QuarantineError("Android archive rename outcome is uncertain") from error
            source_info = None
            target_info = _path_stat(target)
        if (source_info is not None or target_info is None
                or _inode(target_info) != expected_inode):
            raise QuarantineError("Android archive paths do not match the recorded intent")
        self._sync_rename_parents(self.directory.parent, self._maintenance_directory)

        backup = {
            "id": backup_id,
            "directory": target.name,
            "inode": expected_inode,
            "archive_operation_id": operation["id"],
            "custody": operation["custody"],
        }
        result = dict(operation["archive_result"])
        replacement = dict(journal)
        replacement.update({
            "revision": operation["result_revision"],
            "state": "empty",
            "backup": backup,
        })
        completed_operation = dict(operation)
        completed_operation["phase"] = "ARCHIVE_COMMITTED"
        completed_operation["archive_result"] = result
        replacement["operation"] = completed_operation
        try:
            committed = self._write_journal(journal, replacement)
        except (LeaseError, QuarantineError) as error:
            raise QuarantineError(
                "Android archive moved state but its outcome was not durably committed"
            ) from error
        self._validate_backup(committed)
        return dict(result)

    def _resume_restore(self, journal: dict[str, object]) -> dict[str, object]:
        operation = journal["operation"]
        if not isinstance(operation, dict) or operation.get("phase") != "RESTORE_INTENT":
            raise QuarantineError("Android restore journal is not resumable")
        backup_id = operation["backup_id"]
        source = self._backup_path(backup_id)
        source_info = _path_stat(source)
        target_info = _path_stat(self.directory)
        expected_inode = operation["inode"]
        if source_info is not None and _inode(source_info) == expected_inode and target_info is None:
            try:
                self._validate_archived_payload(source)
                self._scan_tree(source, expected_inode=expected_inode)
                self._rename_directory(source, self.directory, expected_inode)
            except QuarantineError:
                raise
            except (OSError, LeaseError) as error:
                source_now = _path_stat(source)
                target_now = _path_stat(self.directory)
                if (source_now is not None and _inode(source_now) == expected_inode
                        and target_now is None):
                    self._settle_no_mutation(journal, "RESTORE_NO_MUTATION")
                    raise LeaseError("Android restore failed without mutation") from error
                raise QuarantineError("Android restore rename outcome is uncertain") from error
            source_info = None
            target_info = _path_stat(self.directory)
        if (source_info is not None or target_info is None
                or _inode(target_info) != expected_inode):
            raise QuarantineError("Android restore paths do not match the recorded intent")
        self._sync_rename_parents(self._maintenance_directory, self.directory.parent)
        try:
            self._open_active()
            self._scan_tree(self.directory, expected_inode=expected_inode)
        except (LeaseError, OSError) as error:
            raise QuarantineError(
                "restored Android state is not valid at its original path"
            ) from error

        result = {
            "revision": operation["result_revision"],
            "state": "complete",
            "backup_id": None,
        }
        replacement = dict(journal)
        replacement.update({
            "revision": operation["result_revision"],
            "state": "complete",
            "backup": None,
        })
        completed_operation = dict(operation)
        completed_operation.update({
            "phase": "RESTORE_COMMITTED",
            "result_snapshot": result,
        })
        replacement["operation"] = completed_operation
        try:
            self._write_journal(journal, replacement)
        except (LeaseError, QuarantineError) as error:
            raise QuarantineError(
                "Android restore moved state but its outcome was not durably committed"
            ) from error
        return result

    def _resume_retire(self, journal: dict[str, object]) -> dict[str, object]:
        operation = journal["operation"]
        if not isinstance(operation, dict) or operation.get("phase") != "RETIRE_INTENT":
            raise QuarantineError("Android retirement journal is not resumable")
        backup_id = operation["backup_id"]
        path = self._backup_path(backup_id)
        info = _path_stat(path)
        expected_inode = operation["inode"]
        if info is not None:
            if _inode(info) != expected_inode:
                raise QuarantineError("Android backup changed during retirement")
            try:
                self._delete_backup_tree(path, expected_inode)
            except BaseException as error:
                raise QuarantineError(
                    "Android backup retirement is incomplete; retry only this operation"
                ) from error
        with self.placement.transaction():
            self._require_placement()
            current = self._read_journal()
            current_operation = current.get("operation")
            if (current != journal
                    or not isinstance(current_operation, dict)
                    or current_operation.get("id") != operation.get("id")
                    or current_operation.get("phase") != "RETIRE_INTENT"
                    or current_operation != operation
                    or current["revision"] != journal["revision"]
                    or current["state"] != journal["state"]):
                raise QuarantineError(
                    "Android retirement journal changed while its backup was removed"
                )
            try:
                _sync_directory(self._maintenance_directory)
            except OSError as error:
                raise QuarantineError(
                    "Android backup retirement directory sync failed"
                ) from error
            if _path_stat(path) is not None:
                raise QuarantineError("Android backup remains after retirement")

            result = {
                "revision": operation["result_revision"],
                "state": current["state"],
                "backup_id": None,
            }
            replacement = dict(current)
            replacement.update({
                "revision": operation["result_revision"],
                "backup": None,
            })
            completed_operation = dict(operation)
            completed_operation.update({
                "phase": "RETIRE_COMMITTED",
                "result_snapshot": result,
            })
            replacement["operation"] = completed_operation
            try:
                self._write_journal(current, replacement)
            except (LeaseError, QuarantineError) as error:
                raise QuarantineError(
                    "Android backup was removed but retirement was not durably committed"
                ) from error
            return result

    def _settle_no_mutation(
        self,
        journal: dict[str, object],
        phase: str,
    ) -> None:
        replacement = dict(journal)
        operation = dict(journal["operation"])
        operation["phase"] = phase
        if phase == "ARCHIVE_NO_MUTATION":
            operation["backup_id"] = None
        replacement["operation"] = operation
        try:
            self._write_journal(journal, replacement)
        except (LeaseError, QuarantineError) as error:
            raise QuarantineError(
                "Android maintenance no-mutation outcome could not be settled"
            ) from error

    def _sync_rename_parents(self, source_parent: Path, destination_parent: Path) -> None:
        try:
            _sync_directory(source_parent)
            _sync_directory(destination_parent)
        except OSError as error:
            raise QuarantineError(
                "Android maintenance rename durability is uncertain; preserving journal"
            ) from error

    def _rename_directory(
        self,
        source: Path,
        destination: Path,
        expected_inode: list[int],
    ) -> None:
        source_fd, source_parent_info = _open_directory(source.parent)
        try:
            destination_fd, destination_parent_info = _open_directory(destination.parent)
        except BaseException:
            os.close(source_fd)
            raise
        try:
            if source_parent_info.st_dev != destination_parent_info.st_dev:
                raise OSError(errno.EXDEV, "Android maintenance paths are on different filesystems")
            try:
                current_source = os.stat(
                    source.name, dir_fd=source_fd, follow_symlinks=False,
                )
            except OSError as error:
                raise QuarantineError("Android maintenance source changed before rename") from error
            if (not stat.S_ISDIR(current_source.st_mode)
                    or _inode(current_source) != expected_inode
                    or current_source.st_uid != os.getuid()):
                raise QuarantineError("Android maintenance source inode differs")
            try:
                os.stat(destination.name, dir_fd=destination_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise QuarantineError("cannot verify Android maintenance destination") from error
            else:
                raise QuarantineError("Android maintenance destination appeared after intent")
            _rename_no_replace(
                source_fd, source.name, destination_fd, destination.name,
            )
            try:
                moved = os.stat(destination.name, dir_fd=destination_fd, follow_symlinks=False)
            except OSError as error:
                raise QuarantineError("Android maintenance rename destination is missing") from error
            if (not stat.S_ISDIR(moved.st_mode) or _inode(moved) != expected_inode
                    or moved.st_uid != os.getuid()):
                raise QuarantineError("Android maintenance rename moved an unexpected inode")
            try:
                os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise QuarantineError("Android maintenance source path is uncertain") from error
            else:
                raise QuarantineError("Android maintenance source still exists after rename")
        finally:
            os.close(destination_fd)
            os.close(source_fd)
        self._sync_rename_parents(source.parent, destination.parent)

    def _delete_backup_tree(self, path: Path, expected_inode: list[int]) -> None:
        self._scan_tree(path, expected_inode=expected_inode)
        parent_fd, _ = _open_directory(path.parent)
        try:
            root_info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if _inode(root_info) != expected_inode or not stat.S_ISDIR(root_info.st_mode):
                raise LeaseError("Android backup root changed during retirement")
            root_fd = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            try:
                opened = os.fstat(root_fd)
                if _inode(opened) != expected_inode:
                    raise LeaseError("Android backup root changed during retirement")
                self._delete_directory_contents(root_fd, opened.st_dev, 0, [0])
            finally:
                os.close(root_fd)
            final = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if _inode(final) != expected_inode or not stat.S_ISDIR(final.st_mode):
                raise LeaseError("Android backup root changed before removal")
            os.rmdir(path.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    def _delete_directory_contents(
        self,
        descriptor: int,
        device: int,
        depth: int,
        count: list[int],
    ) -> None:
        if depth > MAX_TREE_DEPTH:
            raise LeaseError("Android backup exceeds the deletion depth bound")
        for entry_name in os.listdir(descriptor):
            count[0] += 1
            if count[0] > MAX_TREE_ENTRIES:
                raise LeaseError("Android backup exceeds the deletion entry bound")
            info = os.stat(entry_name, dir_fd=descriptor, follow_symlinks=False)
            if info.st_uid != os.getuid() or info.st_dev != device:
                raise LeaseError("Android backup contains foreign or cross-device data")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(entry_name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if _inode(opened) != _inode(info):
                        raise LeaseError("Android backup directory changed before deletion")
                    self._delete_directory_contents(child, device, depth + 1, count)
                finally:
                    os.close(child)
                current = os.stat(entry_name, dir_fd=descriptor, follow_symlinks=False)
                if _inode(current) != _inode(info) or not stat.S_ISDIR(current.st_mode):
                    raise LeaseError("Android backup directory changed before removal")
                os.rmdir(entry_name, dir_fd=descriptor)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                current = os.stat(entry_name, dir_fd=descriptor, follow_symlinks=False)
                if (_inode(current) != _inode(info) or not stat.S_ISREG(current.st_mode)
                        or current.st_nlink != 1 or current.st_uid != os.getuid()):
                    raise LeaseError("Android backup file changed before deletion")
                os.unlink(entry_name, dir_fd=descriptor)
            else:
                raise LeaseError("Android backup contains a symlink or special file")

    def _write_journal(
        self,
        current: dict[str, object],
        replacement: dict[str, object],
    ) -> dict[str, object]:
        if self._read_journal() != current:
            raise QuarantineError("Android maintenance journal changed during operation")
        content = self._encode_journal(replacement)
        temporary = self._maintenance_directory / (
            f".{JOURNAL_NAME}.{uuid.uuid4().hex}.tmp"
        )
        descriptor: int | None = None
        replaced = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self._maintenance_directory / JOURNAL_NAME)
            replaced = True
            _sync_directory(self._maintenance_directory)
            return replacement
        except BaseException as error:
            if descriptor is not None:
                os.close(descriptor)
            if replaced:
                raise QuarantineError("Android maintenance journal durability is uncertain") from error
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                raise QuarantineError(
                    "Android maintenance temporary journal could not be settled"
                ) from cleanup_error
            try:
                _sync_directory(self._maintenance_directory)
            except OSError as sync_error:
                raise QuarantineError(
                    "Android maintenance temporary journal cleanup is uncertain"
                ) from sync_error
            if isinstance(error, (LeaseError, QuarantineError)):
                raise
            raise LeaseError("Android maintenance journal update failed before commit") from error

    @staticmethod
    def _encode_journal(journal: Mapping[str, object]) -> bytes:
        try:
            content = (
                json.dumps(
                    journal, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise LeaseError("Android maintenance journal is not JSON-compatible") from error
        if len(content) > MAX_JOURNAL_BYTES:
            raise LeaseError("Android maintenance journal exceeds its size bound")
        return content
