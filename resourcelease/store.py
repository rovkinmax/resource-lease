"""Transactional lease state. SDK effects belong exclusively to the service."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from typing import Iterator
import uuid

from .model import (
    Config, LeaseError, REGISTRATION_WINDOW, SERVICE_STALE_AFTER,
    TERMINAL, name,
)


LIVE = ("queued", "starting", "active", "resetting", "held_unavailable")
SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE slots(
 id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'idle',
 lease_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
 endpoint TEXT, error TEXT, operation_id TEXT
);
CREATE TABLE requests(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, capability TEXT NOT NULL,
 secret_hash TEXT NOT NULL, epoch TEXT NOT NULL, issued REAL NOT NULL,
 expires REAL NOT NULL, state TEXT NOT NULL,
 slot_id TEXT REFERENCES slots(id), target TEXT, completed REAL, error TEXT,
 artifacts INTEGER NOT NULL DEFAULT 0, quarantined INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE operations(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE, secret_hash TEXT NOT NULL,
 action TEXT NOT NULL, slot_id TEXT NOT NULL REFERENCES slots(id),
 lease_id TEXT, owner TEXT, epoch TEXT NOT NULL, execution_epoch TEXT NOT NULL,
 execution_attempt INTEGER NOT NULL DEFAULT 0,
 execution_kind TEXT NOT NULL, previous_error TEXT,
 issued REAL NOT NULL,
 expected_generation INTEGER NOT NULL, expected_revision TEXT, backup_id TEXT,
 state TEXT NOT NULL, phase TEXT, result TEXT, error TEXT,
 generation_advanced INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL, completed REAL
);
CREATE INDEX operation_slot_state ON operations(slot_id,state,sequence);
CREATE UNIQUE INDEX one_slot_owner ON requests(slot_id)
 WHERE state IN ('starting', 'active', 'resetting', 'held_unavailable', 'releasing');
PRAGMA user_version=2;
"""


def private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LeaseError(f"state directory must be private and owned by you: {path}")
    return path.resolve()


def private_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise LeaseError(f"expected a private regular file: {path}")


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _meta(connection, key):
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    if row is None:
        raise LeaseError("incomplete lease database")
    return row[0]


def _set_meta(connection, **values):
    connection.executemany("UPDATE metadata SET value=? WHERE key=?",
                           [(str(value), key) for key, value in values.items()])


def _public_operation(row):
    return {
        "id": row["id"], "action": row["action"], "slot_id": row["slot_id"],
        "lease_id": row["lease_id"], "epoch": row["epoch"],
        "execution_epoch": row["execution_epoch"],
        "execution_attempt": row["execution_attempt"],
        "execution_kind": row["execution_kind"],
        "previous_error": row["previous_error"],
        "expected_generation": row["expected_generation"],
        "expected_revision": row["expected_revision"], "backup_id": row["backup_id"],
        "state": row["state"], "phase": row["phase"],
        "result": json.loads(row["result"]) if row["result"] is not None else None,
        "error": row["error"], "generation_advanced": bool(row["generation_advanced"]),
    }


@contextmanager
def _service_lock(root: Path):
    path = root / "service.lock"
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise LeaseError("unsafe service lock file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LeaseError("service must be stopped before schema migration") from error
        yield
    finally:
        os.close(descriptor)


def _sync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Store:
    def __init__(self, root: Path):
        original = root.expanduser().absolute()
        info = original.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise LeaseError("pool root must be an owner-private directory")
        self.root = original.resolve()
        self.path = self.root / "leases.sqlite3"
        private_file(self.path)
        with self.transaction(write=False) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                hint = "; run the explicit migrate command for schema 1" if version == 1 else ""
                raise LeaseError(f"unsupported lease database schema {version}{hint}")
            self.pool_id = _meta(connection, "pool_id")
            self.config = Config.parse(json.loads(_meta(connection, "config")))

    @classmethod
    def initialize(cls, root: Path, config: Config) -> "Store":
        root = private_directory(root)
        path = root / "leases.sqlite3"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise LeaseError("pool already exists; initialize never replaces state") from error
        os.close(descriptor)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA)
            values = {
                "pool_id": str(uuid.uuid4()), "config": canonical(config.document()),
                "epoch": "", "service_seen": "0", "service_state": "stopped",
                "shutdown": "0",
            }
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", values.items())
            connection.executemany("INSERT INTO slots(id) VALUES (?)",
                                   [(slot.id,) for slot in config.slots])
            connection.commit()
        finally:
            connection.close()
        return cls(root)

    @classmethod
    def migrate(cls, root: Path) -> "Store":
        """Explicitly upgrade a stopped, settled schema-1 pool without losing its source."""
        original = root.expanduser().absolute()
        info = original.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise LeaseError("pool root must be an owner-private directory")
        root = original.resolve()
        path = root / "leases.sqlite3"
        private_file(path)
        backup_path = root / "leases.sqlite3.schema1.backup"

        with _service_lock(root):
            connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                         timeout=3, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version != 1:
                    raise LeaseError(f"migration requires schema 1; found schema {version}")
                if _meta(connection, "service_state") != "stopped":
                    raise LeaseError("service must be stopped before schema migration")
                unsettled = connection.execute(
                    "SELECT count(*) FROM requests WHERE completed IS NULL "
                    "OR state NOT IN ('completed','cancelled','expired','failed')"
                ).fetchone()[0]
                if unsettled:
                    raise LeaseError("schema migration requires all requests to be terminal")
                unresolved_slots = connection.execute(
                    "SELECT count(*) FROM slots WHERE state!='idle' OR lease_id IS NOT NULL"
                ).fetchone()[0]
                if unresolved_slots:
                    raise LeaseError("schema migration requires all slots to be settled")
                config = Config.parse(json.loads(_meta(connection, "config")))
                slots = [row[0] for row in connection.execute(
                    "SELECT id FROM slots ORDER BY id")]
                if slots != sorted(slot.id for slot in config.slots):
                    raise LeaseError("lease database slots do not match the stored configuration")

                cls._preserve_schema1_backup(path, backup_path, connection)

                connection.execute("ALTER TABLE slots ADD COLUMN operation_id TEXT")
                connection.execute("DROP INDEX one_slot_owner")
                connection.execute(
                    "CREATE TABLE operations("
                    "sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "id TEXT NOT NULL UNIQUE,secret_hash TEXT NOT NULL,"
                    "action TEXT NOT NULL,slot_id TEXT NOT NULL REFERENCES slots(id),"
                    "lease_id TEXT,owner TEXT,epoch TEXT NOT NULL,"
                    "execution_epoch TEXT NOT NULL,execution_attempt INTEGER NOT NULL DEFAULT 0,"
                    "execution_kind TEXT NOT NULL,previous_error TEXT,issued REAL NOT NULL,"
                    "expected_generation INTEGER NOT NULL,expected_revision TEXT,backup_id TEXT,"
                    "state TEXT NOT NULL,phase TEXT,result TEXT,error TEXT,"
                    "generation_advanced INTEGER NOT NULL DEFAULT 0,"
                    "created REAL NOT NULL,completed REAL)"
                )
                connection.execute(
                    "CREATE INDEX operation_slot_state ON operations(slot_id,state,sequence)"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX one_slot_owner ON requests(slot_id) "
                    "WHERE state IN ('starting','active','resetting','held_unavailable','releasing')"
                )
                connection.execute("PRAGMA user_version=2")
                connection.commit()
            except sqlite3.Error as error:
                connection.rollback()
                raise LeaseError(f"lease database migration failed: {error}") from error
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return cls(root)

    @staticmethod
    def _preserve_schema1_backup(path: Path, backup_path: Path,
                                 source: sqlite3.Connection) -> None:
        if backup_path.exists() or backup_path.is_symlink():
            private_file(backup_path)
            try:
                backup = sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)
                try:
                    version = backup.execute("PRAGMA user_version").fetchone()[0]
                    same = (version == 1
                            and list(source.iterdump()) == list(backup.iterdump()))
                finally:
                    backup.close()
            except sqlite3.Error as error:
                raise LeaseError("existing schema-1 backup cannot be verified") from error
            if not same:
                raise LeaseError("existing schema-1 backup differs from the current database")
            _sync_file(backup_path)
            _sync_directory(path.parent)
            return

        try:
            descriptor = os.open(
                backup_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except FileExistsError as error:
            raise LeaseError("schema-1 backup path already exists") from error
        os.close(descriptor)
        source_copy = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(backup_path)
        try:
            source_copy.backup(destination)
            destination.commit()
        except sqlite3.Error as error:
            raise LeaseError(f"could not preserve schema-1 backup: {error}") from error
        finally:
            destination.close()
            source_copy.close()
        private_file(backup_path)
        try:
            backup = sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)
            try:
                if (backup.execute("PRAGMA user_version").fetchone()[0] != 1
                        or list(source.iterdump()) != list(backup.iterdump())):
                    raise LeaseError("schema-1 backup verification failed")
            finally:
                backup.close()
        except sqlite3.Error as error:
            raise LeaseError("schema-1 backup verification failed") from error
        _sync_file(backup_path)
        _sync_directory(path.parent)

    @contextmanager
    def transaction(self, *, write=True) -> Iterator[sqlite3.Connection]:
        private_file(self.path)
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                     timeout=3, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise LeaseError(f"lease database operation failed: {error}") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _live_service(connection, epoch=None):
        seen = float(_meta(connection, "service_seen"))
        current = _meta(connection, "epoch")
        # The first read establishes a deferred transaction's snapshot.
        now = time.monotonic()
        if (_meta(connection, "service_state") != "running"
                or not 0 <= now - seen <= SERVICE_STALE_AFTER
                or (epoch is not None and epoch != current)):
            raise LeaseError("service is unavailable or has restarted; inspect status")
        return current

    def new_receipt(self, owner: str, capability: str) -> dict:
        name(owner, "owner")
        name(capability, "capability")
        if not any(capability in slot.capabilities for slot in self.config.slots):
            raise LeaseError("no configured resource supports that capability")
        with self.transaction(write=False) as connection:
            epoch = self._live_service(connection)
            now = time.monotonic()
        return {"schema": 1, "pool": self.pool_id, "id": str(uuid.uuid4()),
                "owner": owner, "capability": capability, "epoch": epoch,
                "issued": now, "secret": secrets.token_hex(32)}

    def _validate_receipt(self, receipt, owner):
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema", "pool", "id", "owner", "capability", "epoch", "issued", "secret"
        } or type(receipt["schema"]) is not int or receipt["schema"] != 1 or receipt["pool"] != self.pool_id:
            raise LeaseError("lease receipt does not belong to this pool")
        if receipt["owner"] != name(owner, "owner"):
            raise LeaseError("owner differs from lease receipt")
        for field in ("id", "epoch"):
            try:
                parsed = uuid.UUID(receipt[field])
                if str(parsed) != receipt[field] or parsed.version != 4:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError) as error:
                raise LeaseError(f"receipt {field} must be a canonical UUID4") from error
        if (not isinstance(receipt["secret"], str) or len(receipt["secret"]) != 64
                or any(c not in "0123456789abcdef" for c in receipt["secret"])):
            raise LeaseError("invalid lease credential")
        if (not isinstance(receipt["issued"], (int, float))
                or isinstance(receipt["issued"], bool) or not math.isfinite(receipt["issued"])):
            raise LeaseError("invalid receipt creation time")
        name(receipt["capability"], "capability")

    def _authenticated(self, connection, receipt, owner):
        self._validate_receipt(receipt, owner)
        row = connection.execute("SELECT * FROM requests WHERE id=?", (receipt["id"],)).fetchone()
        if row is None:
            raise LeaseError("lease not found; old receipts cannot create a new request")
        expected = hashlib.sha256(receipt["secret"].encode()).hexdigest()
        if (not hmac.compare_digest(row["secret_hash"], expected)
                or row["owner"] != owner or row["epoch"] != receipt["epoch"]
                or row["capability"] != receipt["capability"]
                or row["issued"] != receipt["issued"]):
            raise LeaseError("lease identity or immutable request parameters differ")
        return row

    def _prune(self, connection, now):
        cutoff = now - max(self.config.history_seconds, REGISTRATION_WINDOW)
        connection.execute(
            "DELETE FROM requests WHERE completed IS NOT NULL AND completed<? AND issued<? "
            "AND artifacts=0 AND quarantined=0",
            (cutoff, now - REGISTRATION_WINDOW))
        count = connection.execute("SELECT count(*) FROM requests").fetchone()[0]
        if count >= self.config.max_requests:
            # Capacity pressure may shorten diagnostic retention, never the replay window.
            connection.execute(
                "DELETE FROM requests WHERE sequence IN "
                "(SELECT sequence FROM requests WHERE completed IS NOT NULL "
                "AND issued<? AND artifacts=0 AND quarantined=0 ORDER BY sequence LIMIT ?)",
                (now - REGISTRATION_WINDOW, max(1, count - self.config.max_requests + 1)))

    def register(self, receipt, owner):
        self._validate_receipt(receipt, owner)
        with self.transaction() as connection:
            now = time.monotonic()
            exists = connection.execute("SELECT id FROM requests WHERE id=?", (receipt["id"],)).fetchone()
            if exists:
                self._authenticated(connection, receipt, owner)
            else:
                self._live_service(connection, receipt["epoch"])
                if not 0 <= now - receipt["issued"] < REGISTRATION_WINDOW:
                    raise LeaseError("request registration window expired; use a new lease file")
                self._prune(connection, now)
                if connection.execute("SELECT count(*) FROM requests").fetchone()[0] >= self.config.max_requests:
                    raise LeaseError("request capacity reached; existing leases can still be released")
                if not any(receipt["capability"] in s.capabilities for s in self.config.slots):
                    raise LeaseError("unsupported capability")
                connection.execute(
                    "INSERT INTO requests(id,owner,capability,secret_hash,epoch,issued,expires,state) "
                    "VALUES(?,?,?,?,?,?,?,'queued')",
                    (receipt["id"], owner, receipt["capability"],
                     hashlib.sha256(receipt["secret"].encode()).hexdigest(),
                     receipt["epoch"], receipt["issued"], now + self.config.lease_seconds))
        return self.lease(receipt, owner)

    @staticmethod
    def _public(connection, row):
        slot = (connection.execute("SELECT * FROM slots WHERE id=?", (row["slot_id"],)).fetchone()
                if row["slot_id"] else None)
        owns_slot = slot is not None and slot["lease_id"] == row["id"]
        now = time.monotonic()
        effective_state = "expired" if row["state"] in LIVE and row["expires"] <= now else row["state"]
        active = effective_state == "active"
        return {
            "id": row["id"], "owner": row["owner"], "capability": row["capability"],
            "state": effective_state, "slot": row["slot_id"],
            "expires_in": max(0, row["expires"] - now),
            "cleanup_pending": bool(owns_slot and slot["state"] not in ("idle", "quarantined")
                                    and effective_state not in (
                                        "active", "resetting", "held_unavailable",
                                    )),
            "endpoint": json.loads(slot["endpoint"]) if owns_slot and active and slot["endpoint"] else None,
            "resource_available": bool(owns_slot and active and slot["endpoint"] is not None),
            "error": row["error"], "quarantined": bool(row["quarantined"]),
            "generation": slot["generation"] if owns_slot else None,
        }

    def lease(self, receipt, owner):
        with self.transaction(write=False) as connection:
            row = self._authenticated(connection, receipt, owner)
            result = self._public(connection, row)
            try:
                self._live_service(connection, receipt["epoch"])
                result["service_available"] = True
            except LeaseError:
                result["service_available"] = False
                result["endpoint"] = None
                result["resource_available"] = False
            return result

    def renew(self, receipt, owner):
        with self.transaction() as connection:
            now = time.monotonic()
            row = self._authenticated(connection, receipt, owner)
            self._live_service(connection, receipt["epoch"])
            if row["state"] not in LIVE or row["expires"] <= now:
                raise LeaseError("lease is no longer renewable")
            connection.execute("UPDATE requests SET expires=? WHERE id=?",
                               (now + self.config.lease_seconds, row["id"]))
        return self.lease(receipt, owner)

    def release(self, receipt, owner, *, cancel=False):
        with self.transaction() as connection:
            row = self._authenticated(connection, receipt, owner)
            if row["state"] not in TERMINAL and row["state"] != "releasing":
                target = ("expired" if row["expires"] <= time.monotonic() else
                          "cancelled" if cancel or row["state"] == "queued" else "completed")
                state = target if row["slot_id"] is None else "releasing"
                connection.execute(
                    "UPDATE requests SET state=?,target=?,completed=? WHERE id=?",
                    (state, target, time.monotonic() if row["slot_id"] is None else None, row["id"]))
        return self.lease(receipt, owner)

    def status(self):
        with self.transaction(write=False) as connection:
            try:
                self._live_service(connection)
                available = True
            except LeaseError:
                available = False
            requests = [self._public(connection, row) for row in connection.execute(
                "SELECT * FROM requests ORDER BY sequence")]
            operations = [_public_operation(row) for row in connection.execute(
                "SELECT * FROM operations ORDER BY sequence")]
            if not available:
                for request in requests:
                    request["endpoint"] = None
                    request["resource_available"] = False
            slots = []
            for row in connection.execute(
                "SELECT id,state,lease_id,generation,error,operation_id,endpoint "
                "FROM slots ORDER BY id"
            ):
                slot = dict(row)
                endpoint = slot.pop("endpoint")
                slot["resource_available"] = bool(
                    available and slot["state"] == "leased" and endpoint is not None
                )
                slots.append(slot)
            return {
                "schema": SCHEMA_VERSION, "pool": self.pool_id,
                "service": {"state": _meta(connection, "service_state"),
                            "available": available, "epoch": _meta(connection, "epoch")},
                "slots": slots,
                "requests": requests,
                "operations": operations,
            }

    def service_start(self):
        epoch = str(uuid.uuid4())
        with self.transaction() as connection:
            now = time.monotonic()
            connection.execute(
                "UPDATE slots SET state='quarantined',error='service restarted with unresolved custody' "
                "WHERE state!='idle'")
            connection.execute(
                "UPDATE operations SET state='failed',completed=?,"
                "error=COALESCE(error,'service restarted before maintenance settled') "
                "WHERE state IN ('pending','running')", (now,))
            connection.execute(
                "UPDATE requests SET state='failed',completed=?,error='service restarted',"
                "quarantined=CASE WHEN slot_id IS NOT NULL THEN 1 ELSE 0 END "
                "WHERE completed IS NULL", (now,))
            _set_meta(connection, epoch=epoch, service_seen=now, service_state="running", shutdown=0)
        return epoch

    def service_tick(self, epoch):
        with self.transaction() as connection:
            now = time.monotonic()
            if _meta(connection, "epoch") != epoch:
                raise LeaseError("service epoch changed")
            stopping = _meta(connection, "shutdown") == "1"
            _set_meta(connection, service_seen=now, service_state="stopping" if stopping else "running")
            connection.execute(
                "UPDATE requests SET state='expired',target='expired',"
                "completed=CASE WHEN slot_id IS NULL THEN ? ELSE NULL END "
                "WHERE state IN ('queued','starting','active','resetting','held_unavailable') "
                "AND expires<=?", (now, now))
            if stopping:
                connection.execute(
                    "UPDATE requests SET state='cancelled',target='cancelled',"
                    "completed=CASE WHEN slot_id IS NULL THEN ? ELSE NULL END "
                    "WHERE state IN ('queued','starting','active','resetting','held_unavailable')",
                    (now,))
                pending_idle = connection.execute(
                    "SELECT id,slot_id,execution_kind FROM operations "
                    "WHERE state='pending' AND lease_id IS NULL"
                ).fetchall()
                for operation in pending_idle:
                    if operation["execution_kind"] == "recovery":
                        connection.execute(
                            "UPDATE operations SET state='failed',"
                            "error='service shutdown before recovery started',completed=? "
                            "WHERE id=? AND state='pending'", (now, operation["id"]))
                    else:
                        connection.execute(
                            "UPDATE operations SET state='failed',phase='not_started',"
                            "error='service shutdown before maintenance started',completed=? "
                            "WHERE id=? AND state='pending'", (now, operation["id"]))
                        connection.execute(
                            "UPDATE slots SET state='idle',lease_id=NULL,endpoint=NULL,error=NULL,"
                            "operation_id=NULL WHERE id=? AND operation_id=? AND state='maintenance'",
                            (operation["slot_id"], operation["id"]))
            self._prune(connection, now)
        return stopping

    def assign(self, slot_id):
        slot = next(slot for slot in self.config.slots if slot.id == slot_id)
        with self.transaction() as connection:
            now = time.monotonic()
            self._live_service(connection)
            slot_state = connection.execute(
                "SELECT state,operation_id FROM slots WHERE id=?", (slot_id,),
            ).fetchone()
            if slot_state is None or slot_state["state"] != "idle" or slot_state["operation_id"] is not None:
                return None
            placeholders = ",".join("?" for _ in slot.capabilities)
            row = connection.execute(
                f"SELECT * FROM requests WHERE state='queued' AND expires>? "
                f"AND capability IN ({placeholders}) ORDER BY sequence LIMIT 1",
                (now, *slot.capabilities)).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE requests SET state='starting',slot_id=?,artifacts=1 WHERE id=?",
                               (slot_id, row["id"]))
            connection.execute(
                "UPDATE slots SET state='starting',lease_id=?,generation=generation+1,"
                "endpoint=NULL,error=NULL WHERE id=?", (row["id"], slot_id))
            return row["id"]

    def desired(self, lease_id):
        with self.transaction(write=False) as connection:
            row = connection.execute("SELECT state,expires FROM requests WHERE id=?", (lease_id,)).fetchone()
            return bool(row and row["state"] in LIVE and row["expires"] > time.monotonic()
                        and _meta(connection, "shutdown") == "0")

    def publish(self, lease_id, endpoint, *, operation_id=None, generation=None):
        payload = canonical(dict(endpoint))
        if len(payload.encode()) > 8192:
            raise LeaseError("backend endpoint is oversized")
        with self.transaction() as connection:
            now = time.monotonic()
            row = connection.execute("SELECT * FROM requests WHERE id=?", (lease_id,)).fetchone()
            if (row is None or row["expires"] <= now
                    or _meta(connection, "shutdown") != "0"):
                return False
            slot = connection.execute(
                "SELECT * FROM slots WHERE id=? AND lease_id=?", (row["slot_id"], lease_id),
            ).fetchone() if row["slot_id"] else None
            if (row["state"] == "starting" and operation_id is None and generation is None
                    and slot is not None and slot["state"] == "starting"
                    and slot["operation_id"] is None):
                connection.execute("UPDATE requests SET state='active' WHERE id=?", (lease_id,))
                connection.execute(
                    "UPDATE slots SET state='leased',endpoint=? WHERE lease_id=?",
                    (payload, lease_id),
                )
            elif (row["state"] == "resetting" and operation_id is not None
                  and type(generation) is int and slot is not None
                  and slot["state"] == "leased" and slot["operation_id"] == operation_id
                  and slot["generation"] == generation):
                operation = connection.execute(
                    "SELECT * FROM operations WHERE id=?", (operation_id,),
                ).fetchone()
                if (operation is None or operation["action"] != "reset"
                        or operation["lease_id"] != lease_id or operation["state"] != "running"
                        or not operation["generation_advanced"]):
                    return False
                connection.execute("UPDATE requests SET state='active' WHERE id=?", (lease_id,))
                connection.execute(
                    "UPDATE slots SET endpoint=? WHERE id=? AND operation_id=?",
                    (payload, row["slot_id"], operation_id),
                )
            elif (row["state"] == "active" and operation_id is not None
                  and type(generation) is int and slot is not None
                  and slot["state"] == "leased" and slot["generation"] == generation
                  and slot["operation_id"] == operation_id and slot["endpoint"] == payload):
                operation = connection.execute(
                    "SELECT * FROM operations WHERE id=?", (operation_id,),
                ).fetchone()
                if (operation is None or operation["action"] != "reset"
                        or operation["lease_id"] != lease_id or operation["state"] != "running"
                        or not operation["generation_advanced"]):
                    return False
            else:
                return False
        return True

    def stopping(self, lease_id):
        with self.transaction() as connection:
            connection.execute("UPDATE slots SET state='stopping',endpoint=NULL WHERE lease_id=?", (lease_id,))

    def finish(self, lease_id, *, error=None, quarantine=False, busy=False):
        message = str(error)[:2048] if error else None
        with self.transaction() as connection:
            now = time.monotonic()
            row = connection.execute("SELECT * FROM requests WHERE id=?", (lease_id,)).fetchone()
            if row is None:
                raise LeaseError("assigned lease disappeared")
            outstanding = connection.execute(
                "SELECT id FROM operations WHERE lease_id=? AND state IN ('pending','running')",
                (lease_id,),
            ).fetchone()
            if outstanding is not None:
                raise LeaseError("maintenance operation must be settled before lease finish")
            if busy and row["state"] == "starting" and row["expires"] > now:
                connection.execute("UPDATE requests SET state='queued',slot_id=NULL,artifacts=0 WHERE id=?",
                                   (lease_id,))
            else:
                target = (row["target"] or ("expired" if row["expires"] <= now else
                          "failed" if error else "completed"))
                if quarantine:
                    target = "failed"
                connection.execute("UPDATE requests SET state=?,completed=?,error=?,quarantined=? WHERE id=?",
                                   (target, now, message, int(quarantine), lease_id))
            connection.execute(
                "UPDATE slots SET state=?,lease_id=NULL,endpoint=NULL,error=? WHERE lease_id=?",
                ("quarantined" if quarantine else "idle", message if quarantine else None, lease_id))

    def shutdown(self):
        with self.transaction() as connection:
            _set_meta(connection, shutdown=1)
        return self.status()

    def service_stop(self, epoch):
        with self.transaction() as connection:
            if _meta(connection, "epoch") == epoch:
                _set_meta(connection, service_state="stopped", service_seen=time.monotonic())

    def old_artifacts(self):
        """Only completed, proven-clean generations may lose diagnostic files."""
        with self.transaction(write=False) as connection:
            return [row[0] for row in connection.execute(
                "SELECT id FROM requests WHERE completed IS NOT NULL AND artifacts=1 "
                "AND quarantined=0 ORDER BY sequence DESC LIMIT -1 OFFSET ?",
                (min(16, self.config.max_requests // 2),))]

    def artifacts_removed(self, lease_id):
        with self.transaction() as connection:
            connection.execute(
                "UPDATE requests SET artifacts=0 WHERE id=? AND completed IS NOT NULL AND quarantined=0",
                (lease_id,))
