"""Bounded, receipt-authenticated maintenance operations in the SQLite authority."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import time
import uuid

from .model import (
    LeaseError, REGISTRATION_WINDOW, SERVICE_STALE_AFTER, name,
)
from .store import _meta, _public_operation, canonical


_ACTIONS = frozenset({"inspect", "reset", "restore", "retire"})
_RECEIPT_FIELDS = frozenset({
    "schema", "pool", "id", "action", "slot_id", "expected_generation",
    "expected_revision", "backup_id", "lease_id", "owner", "epoch", "issued", "secret",
})
_MAX_RESULT_BYTES = 8192
_MAX_ERROR_CHARS = 2048
_MAX_PHASE_CHARS = 64


def _error_message(error) -> str | None:
    return str(error)[:_MAX_ERROR_CHARS] if error is not None else None


def _validate_revision(value) -> None:
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(char) < 0x20 for char in value)):
        raise LeaseError("expected state revision must be a non-empty bounded string")


def _json_result(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LeaseError("operation result must be a JSON object")
    try:
        payload = canonical(value)
    except (TypeError, ValueError) as error:
        raise LeaseError("operation result is not bounded JSON data") from error
    if len(payload.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise LeaseError("operation result exceeds the maintenance record limit")
    return payload


def _public(row):
    return _public_operation(row)


def _execution_token(execution_epoch, execution_attempt) -> None:
    try:
        parsed = uuid.UUID(execution_epoch)
        if str(parsed) != execution_epoch or parsed.version != 4:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise LeaseError("execution epoch must be a canonical UUID4") from error
    if (isinstance(execution_attempt, bool) or not isinstance(execution_attempt, int)
            or not 0 <= execution_attempt <= 2**63 - 1):
        raise LeaseError("execution attempt must fit a nonnegative SQLite integer")


class Operations:
    """Own maintenance admission, operation receipts, and fenced outcomes."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _arguments(action, slot_id, expected_generation, expected_revision, backup_id):
        if not isinstance(action, str) or action not in _ACTIONS:
            raise LeaseError("unsupported maintenance action")
        name(slot_id, "slot")
        if (isinstance(expected_generation, bool) or not isinstance(expected_generation, int)
                or not 0 <= expected_generation <= 2**63 - 1):
            raise LeaseError("expected generation must fit a nonnegative SQLite integer")
        if action == "inspect":
            if expected_revision is not None:
                _validate_revision(expected_revision)
        else:
            _validate_revision(expected_revision)
        if action in ("restore", "retire"):
            name(backup_id, "backup id")
        elif backup_id is not None:
            raise LeaseError("backup id is only valid for restore or retire")

    def new_receipt(self, action, slot_id, expected_generation, expected_revision,
                    backup_id=None, *, lease=None, owner=None) -> dict:
        self._arguments(action, slot_id, expected_generation, expected_revision, backup_id)
        if (lease is None) != (owner is None):
            raise LeaseError("lease and owner must be supplied together")
        if lease is not None:
            self.store._validate_receipt(lease, owner)
            if action not in ("inspect", "reset"):
                raise LeaseError("only inspection and reset are available under an owned lease")
        slot = next((item for item in self.store.config.slots if item.id == slot_id), None)
        if slot is None:
            raise LeaseError("unknown slot")

        with self.store.transaction(write=False) as connection:
            epoch = self.store._live_service(connection)
            now = time.monotonic()
            if lease is not None:
                row = self.store._authenticated(connection, lease, owner)
                current = connection.execute(
                    "SELECT * FROM slots WHERE id=? AND lease_id=?",
                    (slot_id, row["id"]),
                ).fetchone()
                if (row["state"] != "active" or row["expires"] <= now
                        or current is None or current["state"] != "leased"
                        or current["endpoint"] is None
                        or current["generation"] != expected_generation):
                    raise LeaseError("owned maintenance requires the current active generation")
            return {
                "schema": 1,
                "pool": self.store.pool_id,
                "id": str(uuid.uuid4()),
                "action": action,
                "slot_id": slot_id,
                "expected_generation": expected_generation,
                "expected_revision": expected_revision,
                "backup_id": backup_id,
                "lease_id": lease["id"] if lease is not None else None,
                "owner": owner,
                "epoch": epoch,
                "issued": now,
                "secret": secrets.token_hex(32),
            }

    def _validate_receipt(self, receipt) -> None:
        if (not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS
                or type(receipt.get("schema")) is not int or receipt["schema"] != 1
                or receipt.get("pool") != self.store.pool_id):
            raise LeaseError("operation receipt does not belong to this pool")
        try:
            parsed = uuid.UUID(receipt["id"])
            epoch = uuid.UUID(receipt["epoch"])
            if (str(parsed) != receipt["id"] or parsed.version != 4
                    or str(epoch) != receipt["epoch"] or epoch.version != 4):
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as error:
            raise LeaseError("operation receipt identifiers must be canonical UUID4 values") from error
        if (not isinstance(receipt["secret"], str) or len(receipt["secret"]) != 64
                or any(char not in "0123456789abcdef" for char in receipt["secret"])):
            raise LeaseError("invalid operation credential")
        if (not isinstance(receipt["issued"], (int, float))
                or isinstance(receipt["issued"], bool) or not math.isfinite(receipt["issued"])):
            raise LeaseError("invalid operation receipt creation time")
        self._arguments(
            receipt["action"], receipt["slot_id"], receipt["expected_generation"],
            receipt["expected_revision"], receipt["backup_id"],
        )
        lease_id, owner = receipt["lease_id"], receipt["owner"]
        if lease_id is None:
            if owner is not None:
                raise LeaseError("idle operation receipt cannot name an owner")
        else:
            try:
                parsed = uuid.UUID(lease_id)
                if str(parsed) != lease_id or parsed.version != 4:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError) as error:
                raise LeaseError("operation lease id must be a canonical UUID4") from error
            name(owner, "owner")

    def validate_receipt(self, receipt, *, action, slot_id, expected_generation,
                         expected_revision, backup_id=None, lease=None, owner=None) -> dict:
        """Validate a saved receipt against requested arguments, without requiring a live service."""
        self._validate_receipt(receipt)
        self._arguments(action, slot_id, expected_generation, expected_revision, backup_id)
        if (receipt["action"] != action or receipt["slot_id"] != slot_id
                or receipt["expected_generation"] != expected_generation
                or receipt["expected_revision"] != expected_revision
                or receipt["backup_id"] != backup_id):
            raise LeaseError("operation receipt arguments differ from this command")
        if (lease is None) != (owner is None):
            raise LeaseError("lease and owner must be supplied together")
        if lease is None:
            if receipt["lease_id"] is not None or receipt["owner"] is not None:
                raise LeaseError("operation receipt belongs to a different lease")
        else:
            self.store._validate_receipt(lease, owner)
            if receipt["lease_id"] != lease["id"] or receipt["owner"] != owner:
                raise LeaseError("operation receipt belongs to a different lease or owner")
        return receipt

    def _authenticate_operation(self, row, receipt) -> None:
        expected_hash = hashlib.sha256(receipt["secret"].encode()).hexdigest()
        if (not hmac.compare_digest(row["secret_hash"], expected_hash)
                or row["action"] != receipt["action"]
                or row["slot_id"] != receipt["slot_id"]
                or row["lease_id"] != receipt["lease_id"]
                or row["owner"] != receipt["owner"]
                or row["epoch"] != receipt["epoch"]
                or row["issued"] != receipt["issued"]
                or row["expected_generation"] != receipt["expected_generation"]
                or row["expected_revision"] != receipt["expected_revision"]
                or row["backup_id"] != receipt["backup_id"]):
            raise LeaseError("operation identity or immutable parameters differ")

    def _current_execution(self, connection, execution_epoch, execution_attempt,
                           now) -> None:
        _execution_token(execution_epoch, execution_attempt)
        try:
            seen = float(_meta(connection, "service_seen"))
        except (TypeError, ValueError) as error:
            raise LeaseError("service heartbeat is invalid") from error
        if (_meta(connection, "epoch") != execution_epoch
                or _meta(connection, "service_state") not in ("running", "stopping")
                or not 0 <= now - seen <= SERVICE_STALE_AFTER):
            raise LeaseError("maintenance execution is stale or service is unavailable")

    def _fenced_operation(self, connection, operation_id, execution_epoch,
                         execution_attempt, now, *, require_binding=True):
        self._current_execution(
            connection, execution_epoch, execution_attempt, now,
        )
        row = connection.execute(
            "SELECT * FROM operations WHERE id=?", (operation_id,),
        ).fetchone()
        if row is None:
            raise LeaseError("operation not found")
        if (row["execution_epoch"] != execution_epoch
                or row["execution_attempt"] != execution_attempt):
            raise LeaseError("maintenance execution attempt is stale")
        slot = connection.execute(
            "SELECT * FROM slots WHERE id=?", (row["slot_id"],),
        ).fetchone()
        if require_binding and not self._binding_matches(row, slot):
            raise LeaseError("maintenance slot binding changed")
        return row, slot

    @staticmethod
    def _binding_matches(operation, slot) -> bool:
        if (slot is None or slot["operation_id"] != operation["id"]
                or slot["lease_id"] != operation["lease_id"]):
            return False
        if operation["lease_id"] is not None:
            expected_states = ("leased", "stopping")
            generation_advanced = (
                operation["action"] == "reset" and operation["generation_advanced"]
            )
        elif operation["execution_kind"] == "recovery":
            expected_states = ("quarantined",)
            generation_advanced = False
        else:
            expected_states = ("maintenance",)
            generation_advanced = False
        expected_generation = operation["expected_generation"] + int(generation_advanced)
        return (slot["state"] in expected_states
                and slot["generation"] == expected_generation)

    def submit(self, receipt, *, lease=None, owner=None) -> dict:
        self._validate_receipt(receipt)
        if (lease is None) != (owner is None):
            raise LeaseError("lease and owner must be supplied together")
        if lease is None:
            if receipt["lease_id"] is not None or receipt["owner"] is not None:
                raise LeaseError("owned operation requires its lease receipt and owner")
        else:
            self.store._validate_receipt(lease, owner)
            if receipt["lease_id"] != lease["id"] or receipt["owner"] != owner:
                raise LeaseError("operation receipt belongs to a different lease or owner")

        with self.store.transaction() as connection:
            now = time.monotonic()
            existing = connection.execute(
                "SELECT * FROM operations WHERE id=?", (receipt["id"],),
            ).fetchone()
            if existing is not None:
                self._authenticate_operation(existing, receipt)
                return _public(existing)

            execution_epoch = self.store._live_service(connection, receipt["epoch"])
            if not 0 <= now - receipt["issued"] < REGISTRATION_WINDOW:
                raise LeaseError("operation registration window expired; use a new operation receipt")
            slot = connection.execute(
                "SELECT * FROM slots WHERE id=?", (receipt["slot_id"],),
            ).fetchone()
            if slot is None:
                raise LeaseError("unknown slot")
            if slot["generation"] != receipt["expected_generation"]:
                raise LeaseError("slot generation changed; inspect current status")
            if slot["operation_id"] is not None:
                raise LeaseError("slot already has a maintenance operation")

            lease_row = None
            if receipt["lease_id"] is None:
                if slot["state"] != "idle" or slot["lease_id"] is not None:
                    raise LeaseError("idle maintenance requires an idle, unleased slot")
            else:
                if receipt["action"] not in ("inspect", "reset"):
                    raise LeaseError("only inspection and reset are available under an owned lease")
                lease_row = self.store._authenticated(connection, lease, owner)
                if (lease_row["id"] != receipt["lease_id"] or lease_row["state"] != "active"
                        or lease_row["expires"] <= now or slot["state"] != "leased"
                        or slot["lease_id"] != lease_row["id"] or slot["endpoint"] is None):
                    raise LeaseError("owned maintenance requires the current active lease")

            self._prune(connection, now)
            if connection.execute("SELECT count(*) FROM operations").fetchone()[0] >= (
                self.store.config.max_requests
            ):
                raise LeaseError("maintenance operation capacity reached")

            connection.execute(
                "INSERT INTO operations("
                "id,secret_hash,action,slot_id,lease_id,owner,epoch,"
                "execution_epoch,execution_attempt,execution_kind,previous_error,issued,"
                "expected_generation,expected_revision,backup_id,state,phase,created"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending','reserved',?)",
                (
                    receipt["id"],
                    hashlib.sha256(receipt["secret"].encode()).hexdigest(),
                    receipt["action"], receipt["slot_id"], receipt["lease_id"],
                    receipt["owner"], receipt["epoch"], execution_epoch, 0, "normal", None,
                    receipt["issued"],
                    receipt["expected_generation"], receipt["expected_revision"],
                    receipt["backup_id"], now,
                ),
            )
            if receipt["lease_id"] is None:
                connection.execute(
                    "UPDATE slots SET state='maintenance',operation_id=?,endpoint=NULL,error=NULL "
                    "WHERE id=? AND state='idle' AND operation_id IS NULL",
                    (receipt["id"], receipt["slot_id"]),
                )
            else:
                if receipt["action"] == "reset":
                    connection.execute(
                        "UPDATE requests SET state='resetting' WHERE id=? AND state='active'",
                        (receipt["lease_id"],),
                    )
                    connection.execute(
                        "UPDATE slots SET operation_id=?,endpoint=NULL WHERE id=? AND lease_id=?",
                        (receipt["id"], receipt["slot_id"], receipt["lease_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE slots SET operation_id=? WHERE id=? AND lease_id=?",
                        (receipt["id"], receipt["slot_id"], receipt["lease_id"]),
                    )
            row = connection.execute(
                "SELECT * FROM operations WHERE id=?", (receipt["id"],),
            ).fetchone()
            return _public(row)

    def recover(self, receipt) -> dict:
        """Requeue only an existing, exact quarantined idle maintenance operation."""
        self._validate_receipt(receipt)
        with self.store.transaction() as connection:
            operation = connection.execute(
                "SELECT * FROM operations WHERE id=?", (receipt["id"],),
            ).fetchone()
            if operation is None:
                raise LeaseError("operation not found")
            self._authenticate_operation(operation, receipt)
            if (operation["lease_id"] is not None or operation["owner"] is not None
                    or operation["action"] not in ("reset", "restore", "retire")):
                raise LeaseError("only idle reset, restore, or retire operations can recover")
            if operation["state"] == "completed":
                return _public(operation)

            execution_epoch = self.store._live_service(connection)
            slot = connection.execute(
                "SELECT * FROM slots WHERE id=?", (operation["slot_id"],),
            ).fetchone()
            if (slot is None or slot["operation_id"] != operation["id"]
                    or slot["lease_id"] is not None or slot["endpoint"] is not None
                    or slot["generation"] != operation["expected_generation"]
                    or slot["state"] != "quarantined"):
                raise LeaseError("operation no longer has its exact quarantined idle slot binding")

            if operation["state"] in ("pending", "running"):
                if (operation["execution_kind"] != "recovery"
                        or operation["execution_epoch"] != execution_epoch):
                    raise LeaseError("operation is not an active recovery attempt")
                return _public(operation)
            if operation["state"] != "failed":
                raise LeaseError("operation is not recoverable")
            if _meta(connection, "shutdown") != "0":
                raise LeaseError("service is shutting down")
            if operation["execution_attempt"] >= 2**63 - 1:
                raise LeaseError("maintenance execution attempt capacity reached")

            previous_error = operation["error"] or operation["previous_error"]
            if previous_error is not None:
                previous_error = previous_error[:_MAX_ERROR_CHARS]
            connection.execute(
                "UPDATE operations SET state='pending',execution_epoch=?,"
                "execution_attempt=execution_attempt+1,execution_kind='recovery',"
                "previous_error=?,error=NULL,completed=NULL "
                "WHERE id=? AND state='failed' AND execution_attempt=?",
                (
                    execution_epoch, previous_error, operation["id"],
                    operation["execution_attempt"],
                ),
            )
            return _public(connection.execute(
                "SELECT * FROM operations WHERE id=?", (operation["id"],),
            ).fetchone())

    def inspect(self, receipt) -> dict:
        self._validate_receipt(receipt)
        with self.store.transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id=?", (receipt["id"],),
            ).fetchone()
            if row is None:
                raise LeaseError("operation not found")
            self._authenticate_operation(row, receipt)
            return _public(row)

    def pending(self, slot_id, lease_id=None) -> dict | None:
        name(slot_id, "slot")
        with self.store.transaction(write=False) as connection:
            if lease_id is None:
                row = connection.execute(
                    "SELECT * FROM operations WHERE slot_id=? AND lease_id IS NULL "
                    "AND state IN ('pending','running') ORDER BY sequence LIMIT 1",
                    (slot_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM operations WHERE slot_id=? AND lease_id=? "
                    "AND state IN ('pending','running') ORDER BY sequence LIMIT 1",
                    (slot_id, lease_id),
                ).fetchone()
            return _public(row) if row is not None else None

    def begin(self, operation_id, *, execution_epoch, execution_attempt) -> dict:
        with self.store.transaction() as connection:
            now = time.monotonic()
            row, slot = self._fenced_operation(
                connection, operation_id, execution_epoch, execution_attempt, now,
            )
            if row["state"] == "running":
                return _public(row)
            if row["state"] != "pending":
                raise LeaseError("operation is no longer pending")
            desired = False
            if row["lease_id"] is None:
                expected_slot_state = (
                    "quarantined" if row["execution_kind"] == "recovery" else "maintenance"
                )
                desired = slot["state"] == expected_slot_state
            else:
                request = connection.execute(
                    "SELECT * FROM requests WHERE id=?", (row["lease_id"],),
                ).fetchone()
                desired = (
                    request is not None and request["state"] in ("active", "resetting")
                    and request["expires"] > now and slot is not None
                    and slot["operation_id"] == operation_id and slot["lease_id"] == row["lease_id"]
                )
            if not desired or _meta(connection, "shutdown") != "0":
                raise LeaseError("operation is no longer desired")
            connection.execute(
                "UPDATE operations SET state='running',"
                "phase=CASE WHEN execution_kind='recovery' OR phase='physical_committed' "
                "THEN phase ELSE 'started' END "
                "WHERE id=? AND state='pending'", (operation_id,),
            )
            return _public(connection.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,),
            ).fetchone())

    def set_phase(self, operation_id, phase, result=None, *,
                  execution_epoch, execution_attempt) -> dict:
        if (not isinstance(phase, str) or not 1 <= len(phase) <= _MAX_PHASE_CHARS
                or any(not (char.isalnum() or char in "_-") for char in phase)):
            raise LeaseError("operation phase is invalid")
        payload = _json_result(result)
        with self.store.transaction() as connection:
            now = time.monotonic()
            row, _ = self._fenced_operation(
                connection, operation_id, execution_epoch, execution_attempt, now,
            )
            if row["state"] != "running":
                raise LeaseError("operation is not running")
            if row["phase"] == "physical_committed":
                if row["phase"] != phase or row["result"] != payload:
                    raise LeaseError("committed maintenance evidence cannot be replaced")
                return _public(row)
            if row["result"] is not None and payload is not None and row["result"] != payload:
                raise LeaseError("operation result conflicts with recorded evidence")
            payload = payload if payload is not None else row["result"]
            connection.execute(
                "UPDATE operations SET phase=?,result=? WHERE id=? AND state='running'",
                (phase, payload, operation_id),
            )
            return _public(connection.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,),
            ).fetchone())

    def advance_generation(self, operation_id, *,
                           execution_epoch, execution_attempt) -> int:
        with self.store.transaction() as connection:
            now = time.monotonic()
            row, slot = self._fenced_operation(
                connection, operation_id, execution_epoch, execution_attempt, now,
            )
            if row["state"] != "running" or row["action"] != "reset":
                raise LeaseError("only a running reset can advance generation")
            if _meta(connection, "shutdown") != "0":
                raise LeaseError("service is shutting down")
            request = connection.execute(
                "SELECT * FROM requests WHERE id=?", (row["lease_id"],),
            ).fetchone() if row["lease_id"] else None
            if (request is None or request["state"] != "resetting"
                    or request["expires"] <= now or slot is None
                    or slot["lease_id"] != row["lease_id"]
                    or slot["operation_id"] != operation_id):
                raise LeaseError("reset lease is no longer desired")
            if row["generation_advanced"]:
                return slot["generation"]
            if slot["generation"] >= 2**63 - 1:
                raise LeaseError("slot generation capacity reached")
            generation = slot["generation"] + 1
            connection.execute(
                "UPDATE slots SET generation=? WHERE id=? AND operation_id=?",
                (generation, row["slot_id"], operation_id),
            )
            connection.execute(
                "UPDATE operations SET generation_advanced=1 WHERE id=? AND state='running'",
                (operation_id,),
            )
            return generation

    def complete(self, operation_id, result, error=None, quarantine=False, *,
                 execution_epoch, execution_attempt) -> dict:
        payload = _json_result(result)
        message = _error_message(error)
        if type(quarantine) is not bool:
            raise LeaseError("quarantine must be a boolean")
        with self.store.transaction() as connection:
            now = time.monotonic()
            operation, slot = self._fenced_operation(
                connection, operation_id, execution_epoch, execution_attempt, now,
                require_binding=False,
            )
            if operation["state"] in ("completed", "failed"):
                if (payload is not None and operation["result"] is not None
                        and payload != operation["result"]):
                    raise LeaseError("completed operation result differs from its receipt")
                if message is not None and message != operation["error"]:
                    raise LeaseError("completed operation error differs from its receipt")
                return _public(operation)
            if operation["state"] != "running":
                raise LeaseError("operation is not running")
            if not self._binding_matches(operation, slot):
                raise LeaseError("maintenance slot binding changed")
            if operation["result"] is not None and payload is not None and payload != operation["result"]:
                raise LeaseError("operation result conflicts with recorded evidence")
            payload = payload if payload is not None else operation["result"]
            final_error = message
            state = "failed" if message is not None else "completed"
            if quarantine and final_error is None:
                final_error = "maintenance cleanup is uncertain"
                state = "failed"
            if operation["lease_id"] is None:
                if slot is not None and slot["operation_id"] == operation_id:
                    if quarantine:
                        connection.execute(
                            "UPDATE slots SET state='quarantined',endpoint=NULL,error=?,"
                            "operation_id=? WHERE id=? AND operation_id=?",
                            (final_error or "maintenance cleanup is uncertain",
                             operation_id, operation["slot_id"], operation_id),
                        )
                    else:
                        connection.execute(
                            "UPDATE slots SET state='idle',lease_id=NULL,endpoint=NULL,"
                            "error=NULL,operation_id=NULL WHERE id=? AND operation_id=?",
                            (operation["slot_id"], operation_id),
                        )
            else:
                request = connection.execute(
                    "SELECT * FROM requests WHERE id=?", (operation["lease_id"],),
                ).fetchone()
                if slot is not None and slot["operation_id"] == operation_id:
                    if quarantine:
                        connection.execute(
                            "UPDATE slots SET state='quarantined',endpoint=NULL,error=?,"
                            "operation_id=? WHERE id=? AND operation_id=?",
                            (final_error or "maintenance cleanup is uncertain",
                             operation_id, operation["slot_id"], operation_id),
                        )
                    elif (message is not None and operation["action"] == "reset"
                          and request is not None
                          and request["state"] in ("resetting", "active")
                          and request["expires"] > now and _meta(connection, "shutdown") == "0"):
                        final_error = message
                        connection.execute(
                            "UPDATE requests SET state='held_unavailable',error=? "
                            "WHERE id=? AND state IN ('resetting','active')",
                            (message, operation["lease_id"]),
                        )
                        connection.execute(
                            "UPDATE slots SET state='leased',endpoint=NULL,error=?,"
                            "operation_id=NULL WHERE id=? AND operation_id=?",
                            (message, operation["slot_id"], operation_id),
                        )
                    else:
                        published_reset = (
                            operation["action"] == "reset" and message is None
                            and request is not None and request["state"] == "active"
                            and request["expires"] > now and slot["state"] == "leased"
                            and operation["generation_advanced"]
                            and slot["generation"] > operation["expected_generation"]
                            and slot["endpoint"] is not None
                            and _meta(connection, "shutdown") == "0"
                        )
                        if operation["action"] == "reset" and message is None and not published_reset:
                            final_error = "reset replacement was not published for the current lease"
                            state = "failed"
                            if (request is not None and request["state"] == "resetting"
                                    and request["expires"] > now
                                    and _meta(connection, "shutdown") == "0"):
                                connection.execute(
                                    "UPDATE requests SET state='held_unavailable',error=? "
                                    "WHERE id=? AND state='resetting'",
                                    (final_error, operation["lease_id"]),
                                )
                                connection.execute(
                                    "UPDATE slots SET state='leased',error=? "
                                    "WHERE id=? AND operation_id=?",
                                    (final_error, operation["slot_id"], operation_id),
                                )
                        preserve_endpoint = (
                            request is not None and request["state"] == "active"
                            and request["expires"] > now and _meta(connection, "shutdown") == "0"
                            and (operation["action"] == "inspect" or published_reset)
                        )
                        connection.execute(
                            "UPDATE slots SET endpoint=CASE WHEN ? THEN endpoint ELSE NULL END,"
                            "operation_id=NULL,error=CASE WHEN ? THEN error ELSE ? END "
                            "WHERE id=? AND operation_id=?",
                            (
                                int(preserve_endpoint), int(preserve_endpoint), final_error,
                                operation["slot_id"], operation_id,
                            ),
                        )

            connection.execute(
                "UPDATE operations SET state=?,result=?,error=?,completed=? "
                "WHERE id=? AND state IN ('pending','running')",
                (state, payload, final_error, now, operation_id),
            )
            return _public(connection.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,),
            ).fetchone())

    def fail_for_lease(self, lease_id, error, quarantine=False, *,
                       execution_epoch, execution_attempt) -> dict | None:
        message = _error_message(error) or "lease ended before maintenance completed"
        if type(quarantine) is not bool:
            raise LeaseError("quarantine must be a boolean")
        with self.store.transaction() as connection:
            now = time.monotonic()
            self._current_execution(
                connection, execution_epoch, execution_attempt, now,
            )
            operation = connection.execute(
                "SELECT * FROM operations WHERE lease_id=? AND state IN ('pending','running') "
                "ORDER BY sequence LIMIT 1", (lease_id,),
            ).fetchone()
            if operation is None:
                return None
            operation, _ = self._fenced_operation(
                connection, operation["id"], execution_epoch, execution_attempt, now,
            )
            if quarantine:
                connection.execute(
                    "UPDATE slots SET state='quarantined',endpoint=NULL,error=?,operation_id=? "
                    "WHERE id=? AND operation_id=?",
                    (message, operation["id"], operation["slot_id"], operation["id"]),
                )
            else:
                connection.execute(
                    "UPDATE slots SET endpoint=NULL,operation_id=NULL,error=? "
                    "WHERE id=? AND operation_id=?",
                    (message, operation["slot_id"], operation["id"]),
                )
            connection.execute(
                "UPDATE operations SET state='failed',error=?,completed=? "
                "WHERE id=? AND state IN ('pending','running')",
                (message, now, operation["id"]),
            )
            return _public(connection.execute(
                "SELECT * FROM operations WHERE id=?", (operation["id"],),
            ).fetchone())

    def _prune(self, connection, now) -> None:
        cutoff = now - max(self.store.config.history_seconds, REGISTRATION_WINDOW)
        rows = connection.execute(
            "SELECT id,action,slot_id,backup_id,state,phase,error,result,completed,issued "
            "FROM operations ORDER BY sequence"
        ).fetchall()
        links = {}
        resolved = set()
        for row in rows:
            backup_keys = {(row["slot_id"], row["backup_id"])} if row["backup_id"] else set()
            unknown_link = False
            try:
                result = json.loads(row["result"]) if row["result"] is not None else None
            except (TypeError, ValueError):
                continue
            if result is not None and not isinstance(result, dict):
                continue
            result_backup = result.get("backup_id") if result is not None else None
            if result_backup is not None:
                if isinstance(result_backup, str) and result_backup:
                    backup_keys.add((row["slot_id"], result_backup))
                else:
                    unknown_link = True
            links[row["id"]] = (backup_keys, unknown_link)
            if (row["action"] in ("retire", "restore") and row["state"] == "completed"
                    and row["phase"] == "physical_committed" and row["error"] is None
                    and row["backup_id"]):
                resolved.add((row["slot_id"], row["backup_id"]))

        aged = {
            row["id"] for row in rows
            if row["state"] in ("completed", "failed")
            and row["completed"] is not None and row["completed"] < cutoff
            and row["issued"] < now - REGISTRATION_WINDOW
        }
        pinned = {
            row[0] for row in connection.execute(
                "SELECT operation_id FROM slots WHERE operation_id IS NOT NULL",
            )
        }
        candidates = set()
        resolvers = {
            row["id"] for row in rows
            if (row["action"] in ("retire", "restore") and row["state"] == "completed"
                and row["phase"] == "physical_committed" and row["error"] is None
                and row["backup_id"])
        }
        for row in rows:
            if row["id"] not in aged or row["id"] in pinned:
                continue
            backup_keys, unknown_link = links.get(row["id"], (set(), True))
            if (not unknown_link and
                    all(key in resolved for key in backup_keys)):
                candidates.add(row["id"])

        # Keep a successful resolver as proof until every older local record it
        # resolves can age out too. Cross-pool evidence is intentionally unknown.
        for resolver_id in resolvers & candidates:
            resolver_links = links.get(resolver_id, (set(), True))[0]
            resolver_slots = {key[0] for key in resolver_links}
            linked_records = {
                row["id"] for row in rows
                if row["id"] != resolver_id and row["slot_id"] in resolver_slots
                and (
                    row["id"] not in links
                    or links[row["id"]][1]
                    or links[row["id"]][0] & resolver_links
                )
            }
            if not linked_records.issubset(candidates):
                candidates.remove(resolver_id)
        if candidates:
            connection.executemany(
                "DELETE FROM operations WHERE id=?",
                [(operation_id,) for operation_id in candidates],
            )
