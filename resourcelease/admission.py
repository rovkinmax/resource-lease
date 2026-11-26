"""Pool-free FIFO admission for cooperating, process-owned leases."""
from __future__ import annotations

from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator
import uuid

from .model import LeaseError
from .store import private_directory, private_file


CAPACITY = 3
WAIT_HEARTBEAT = 15.0
MAX_WAITERS = 1024
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE requests (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('waiting', 'admitted', 'quarantined')),
    touched REAL NOT NULL
);
CREATE INDEX request_state_sequence ON requests(state, sequence);
PRAGMA user_version=1;
"""


class Admission:
    """One authority shared by all new callers, independent of their working roots.

    Waiting callers must renew while they are alive. Admitted callers never
    expire on a clock: they might have started a child before disappearing.
    """

    def __init__(self, root: Path):
        self.path = root / "admission.sqlite3"
        private_file(self.path)
        with self._transaction(write=False) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise LeaseError("unsupported or incomplete host admission database")

    @classmethod
    def initialize(cls, root: Path) -> Admission:
        """Explicit empty-authority initialization; never recreates on request."""
        path = private_directory(root) / "admission.sqlite3"
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
        )
        os.close(descriptor)
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as connection:
            connection.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\nCOMMIT;")
        return cls(path.parent)

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        private_file(self.path)
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5.0)
        try:
            # SDK and child work always happens after this transaction commits.
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _expire_waiters(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM requests WHERE state='waiting' AND (touched<=? OR touched>?)",
            (now - WAIT_HEARTBEAT, now),
        )

    def enqueue(self) -> str:
        request = uuid.uuid4().hex
        with self._transaction() as connection:
            now = time.monotonic()
            self._expire_waiters(connection, now)
            waiting = connection.execute(
                "SELECT count(*) FROM requests WHERE state='waiting'",
            ).fetchone()[0]
            if waiting >= MAX_WAITERS:
                raise LeaseError("host admission queue is full")
            connection.execute(
                "INSERT INTO requests (id, state, touched) VALUES (?, 'waiting', ?)",
                (request, now),
            )
        return request

    def try_admit(self, request: str) -> bool:
        with self._transaction() as connection:
            now = time.monotonic()
            self._expire_waiters(connection, now)
            updated = connection.execute(
                "UPDATE requests SET touched=? WHERE id=? AND state='waiting'",
                (now, request),
            )
            if updated.rowcount != 1:
                raise LeaseError("admission request is no longer waiting")
            admitted = connection.execute(
                "SELECT count(*) FROM requests WHERE state!='waiting'",
            ).fetchone()[0]
            if admitted >= CAPACITY:
                return False
            head = connection.execute(
                "SELECT id FROM requests WHERE state='waiting' ORDER BY sequence LIMIT 1",
            ).fetchone()[0]
            if head != request:
                return False
            connection.execute("UPDATE requests SET state='admitted' WHERE id=?", (request,))
            return True

    def cancel_waiting(self, request: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM requests WHERE id=? AND state='waiting'", (request,))

    def finish(self, request: str, *, cleanup_confirmed: bool) -> None:
        """Only the caller holding direct ownership may attest its cleanup."""
        with self._transaction() as connection:
            if cleanup_confirmed:
                changed = connection.execute(
                    "DELETE FROM requests WHERE id=? AND state='admitted'", (request,),
                )
            else:
                changed = connection.execute(
                    "UPDATE requests SET state='quarantined' WHERE id=? AND state='admitted'",
                    (request,),
                )
            if changed.rowcount != 1:
                raise LeaseError("cannot finish a request without an admitted lease")
