"""Shared same-user physical exclusion namespace."""
import os
from pathlib import Path


def home() -> Path:
    configured = os.environ.get("RESOURCE_LEASE_HOME")
    return Path(configured).expanduser().absolute() if configured else (
        Path.home() / ".local" / "state" / "resource-lease")


def default_guard_root() -> Path:
    return home() / "guards"
