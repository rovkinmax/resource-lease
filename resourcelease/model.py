"""Small public types shared by the scheduler and platform adapters."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from threading import Event
from typing import Any, Mapping, Protocol


class LeaseError(ValueError):
    """A rejected request or a backend failure with completed cleanup."""


class QuarantineError(LeaseError):
    """Resource teardown is uncertain; retain its physical interlock."""


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MAX_RECORD = 64 * 1024
REGISTRATION_WINDOW = 60.0
SERVICE_STALE_AFTER = 5.0
TERMINAL = frozenset({"completed", "cancelled", "expired", "failed"})


def name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise LeaseError(f"{label} must be a 1..128 character identifier")
    return value


def number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise LeaseError(f"{label} must be between {minimum} and {maximum}")
    return float(value)


@dataclass(frozen=True)
class Slot:
    id: str
    capabilities: tuple[str, ...]
    backend: str
    options: Mapping[str, Any]


@dataclass(frozen=True)
class Config:
    slots: tuple[Slot, ...]
    lease_seconds: float = 120.0
    max_requests: int = 1024
    history_seconds: float = 3600.0

    @classmethod
    def parse(cls, value: Any) -> "Config":
        if not isinstance(value, dict) or set(value) - {
            "schema", "slots", "lease_seconds", "max_requests", "history_seconds"
        } or type(value.get("schema")) is not int or value["schema"] != 1:
            raise LeaseError("expected resource lease configuration schema 1")
        raw_slots = value.get("slots")
        if not isinstance(raw_slots, list) or not 1 <= len(raw_slots) <= 16:
            raise LeaseError("configuration requires 1..16 slots")
        slots = []
        for raw in raw_slots:
            if not isinstance(raw, dict) or set(raw) != {
                "id", "capabilities", "backend", "options"
            }:
                raise LeaseError("slot requires id, capabilities, backend and options")
            caps = raw["capabilities"]
            if not isinstance(caps, list) or not 1 <= len(caps) <= 32:
                raise LeaseError("slot requires 1..32 capabilities")
            capabilities = tuple(name(cap, "capability") for cap in caps)
            if len(set(capabilities)) != len(capabilities):
                raise LeaseError("duplicate capability")
            if not isinstance(raw["options"], dict):
                raise LeaseError("backend options must be an object")
            slots.append(Slot(name(raw["id"], "slot"), capabilities,
                              name(raw["backend"], "backend"), raw["options"]))
        if len({slot.id for slot in slots}) != len(slots):
            raise LeaseError("duplicate slot")
        limit = value.get("max_requests", 1024)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 16 <= limit <= 4096:
            raise LeaseError("max_requests must be an integer in 16..4096")
        return cls(tuple(slots),
                   number(value.get("lease_seconds", 120), "lease_seconds", 2, 86400),
                   limit,
                   number(value.get("history_seconds", 3600), "history_seconds", 60, 86400))

    def document(self) -> dict:
        return {
            "schema": 1, "lease_seconds": self.lease_seconds,
            "max_requests": self.max_requests, "history_seconds": self.history_seconds,
            "slots": [{"id": s.id, "capabilities": list(s.capabilities),
                       "backend": s.backend, "options": dict(s.options)} for s in self.slots],
        }


class Resource(Protocol):
    @property
    def endpoint(self) -> Mapping[str, str]: ...

    def healthy(self) -> bool:
        """True while the owned resource is alive."""
        ...

    def stop(self) -> None:
        """Stop owned children, verify teardown, clean disposable generation data.

        Raise QuarantineError if safe reuse cannot be established.
        """
        ...


class Backend(Protocol):
    def keys(self, slot: Slot) -> tuple[str, ...]:
        """Validate options and return canonical physical exclusion keys."""
        ...

    def start(self, slot: Slot, directory: Path, cancelled: Event) -> Resource:
        """Return a ready resource; clean up before an ordinary LeaseError.

        An unknown exception is conservatively treated as uncertain teardown.
        """
        ...
