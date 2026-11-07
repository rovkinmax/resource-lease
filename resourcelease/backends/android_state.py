"""Ownership and validation for opt-in persistent Android AVD state."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from typing import Mapping

from resourcelease.model import LeaseError


MANIFEST_NAME = ".resource-lease-android-state.json"
MANIFEST_SCHEMA = "resource-lease-android-state-v1"
MAX_MANIFEST_BYTES = 16 * 1024
MAX_AVD_INI_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 1024 * 1024


def canonical_state_directory(value: object) -> Path:
    """Resolve an absolute state path without creating or adopting it."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise LeaseError("Android state_directory must be a nonempty absolute path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise LeaseError("Android state_directory must be an absolute path")

    try:
        info = candidate.lstat()
    except FileNotFoundError:
        try:
            parent = candidate.parent.resolve(strict=True)
            parent_info = parent.stat()
        except (OSError, RuntimeError) as error:
            raise LeaseError(
                "Android state_directory parent must already exist"
            ) from error
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid():
            raise LeaseError("Android state_directory parent must be an owned directory")
        canonical = parent / candidate.name
    except (OSError, RuntimeError) as error:
        raise LeaseError("cannot inspect Android state_directory") from error
    else:
        if not stat.S_ISDIR(info.st_mode):
            raise LeaseError(
                "Android state_directory must be a real directory, not a file or symlink"
            )
        canonical = candidate.resolve(strict=True)
        _require_private_directory(canonical, "Android state_directory")

    if canonical == Path("/") or not canonical.name:
        raise LeaseError("Android state_directory cannot be the filesystem root")
    return canonical


def state_identity(directory: Path) -> str:
    """Return a stable, non-path-bearing identity for the canonical directory."""
    return hashlib.sha256(os.fsencode(str(directory))).hexdigest()


def _require_private_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise LeaseError(f"{label} is unavailable") from error
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_mode & 0o7000):
        raise LeaseError(f"{label} must be an owner-private directory")
    return info


def _require_owned_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise LeaseError(f"{label} is unavailable; preserving persistent state") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LeaseError(f"{label} is unsafe; preserving persistent state")
    return info


def _regular_file(
    path: Path, label: str, maximum: int, *, private: bool = False,
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise LeaseError(f"{label} is unavailable; preserving persistent state") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_size > maximum
            or (private and (info.st_mode & 0o077 or info.st_mode & 0o7000))):
        raise LeaseError(f"{label} is unsafe or oversized; preserving persistent state")
    return info


def _read_private_file(
    path: Path, label: str, maximum: int, *, private: bool = True,
) -> bytes:
    before = _regular_file(path, label, maximum, private=private)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise LeaseError(f"cannot open {label}; preserving persistent state") from error
    try:
        opened = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_nlink != 1 or opened.st_size > maximum
                or (private and (opened.st_mode & 0o077 or opened.st_mode & 0o7000))):
            raise LeaseError(f"{label} changed during inspection; preserving persistent state")
        chunks = []
        size = 0
        while size <= maximum:
            chunk = os.read(descriptor, min(8192, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        linked = path.lstat()
        if ((after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
                or after.st_size != opened.st_size or size != opened.st_size
                or size > maximum):
            raise LeaseError(f"{label} changed during inspection; preserving persistent state")
        return b"".join(chunks)
    except OSError as error:
        raise LeaseError(f"cannot read {label}; preserving persistent state") from error
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class PersistentAndroidState:
    directory: Path
    private_home: Path
    avd_home: Path
    avd_name: str
    identity: str
    profile: Mapping[str, object]
    manifest: dict[str, object]

    @property
    def avd_path(self) -> Path:
        return self.avd_home / f"{self.avd_name}.avd"

    @property
    def complete(self) -> bool:
        return self.manifest["status"] == "complete"

    @classmethod
    def prepare(
        cls,
        directory: Path,
        *,
        system_image: str,
        image_identity: Mapping[str, object],
        memory_mb: int,
    ) -> "PersistentAndroidState":
        identity = state_identity(directory)
        avd_name = f"resource_lease_{identity[:16]}"
        profile = {
            "system_image": system_image,
            "image_identity": dict(image_identity),
            "memory_mb": memory_mb,
        }
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            return cls._open_existing(directory, identity, avd_name, profile)
        except OSError as error:
            raise LeaseError(
                f"cannot create Android state_directory: {directory}"
            ) from error

        try:
            _require_private_directory(directory, "Android state_directory")
            state = cls(
                directory=directory,
                private_home=directory / "android-home",
                avd_home=directory / "android-home" / "avd",
                avd_name=avd_name,
                identity=identity,
                profile=profile,
                manifest={
                    "schema": MANIFEST_SCHEMA,
                    "owner": "resource-lease",
                    "state_identity": identity,
                    "profile": profile,
                    "avd_name": avd_name,
                    "status": "initializing",
                },
            )
            state._write_new_manifest()
            state.private_home.mkdir(mode=0o700, exist_ok=False)
            _require_private_directory(state.private_home, "persistent Android home")
            state.avd_home.mkdir(mode=0o700, exist_ok=False)
            _require_private_directory(state.avd_home, "persistent Android AVD home")
            _sync_directory(directory)
            return state
        except LeaseError:
            raise
        except OSError as error:
            raise LeaseError(
                "persistent Android state initialization failed; partial data was preserved"
            ) from error

    @classmethod
    def _open_existing(
        cls,
        directory: Path,
        identity: str,
        avd_name: str,
        profile: Mapping[str, object],
    ) -> "PersistentAndroidState":
        _require_private_directory(directory, "Android state_directory")
        try:
            manifest = json.loads(_read_private_file(
                directory / MANIFEST_NAME, "persistent Android manifest", MAX_MANIFEST_BYTES,
            ))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LeaseError(
                "persistent Android manifest is malformed; preserving state"
            ) from error
        expected_fields = {
            "schema", "owner", "state_identity", "profile", "avd_name", "status",
        }
        status = manifest.get("status") if isinstance(manifest, dict) else None
        if (not isinstance(manifest, dict) or set(manifest) != expected_fields
                or manifest.get("schema") != MANIFEST_SCHEMA
                or manifest.get("owner") != "resource-lease"
                or manifest.get("state_identity") != identity
                or manifest.get("avd_name") != avd_name
                or not isinstance(status, str)
                or status not in {"initializing", "complete"}):
            raise LeaseError(
                "persistent Android state is unmarked or incompatible; preserving state"
            )
        if manifest["profile"] != dict(profile):
            raise LeaseError(
                "persistent Android state profile differs; preserving state"
            )
        if manifest["status"] != "complete":
            raise LeaseError(
                "persistent Android state initialization is incomplete; preserving state"
            )

        state = cls(
            directory=directory,
            private_home=directory / "android-home",
            avd_home=directory / "android-home" / "avd",
            avd_name=avd_name,
            identity=identity,
            profile=profile,
            manifest=manifest,
        )
        state.verify_avd()
        return state

    def _write_new_manifest(self) -> None:
        path = self.directory / MANIFEST_NAME
        content = (
            json.dumps(self.manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(self.directory)

    def _replace_manifest(self, replacement: dict[str, object]) -> None:
        path = self.directory / MANIFEST_NAME
        if self._read_manifest() != self.manifest:
            raise LeaseError(
                "persistent Android manifest changed during use; preserving state"
            )
        temporary = self.directory / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
        content = (json.dumps(replacement, sort_keys=True, separators=(",", ":")) + "\n")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            _write_all(descriptor, content.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _sync_directory(self.directory)

    def _read_manifest(self) -> dict[str, object]:
        try:
            manifest = json.loads(_read_private_file(
                self.directory / MANIFEST_NAME,
                "persistent Android manifest",
                MAX_MANIFEST_BYTES,
            ))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LeaseError(
                "persistent Android manifest is malformed; preserving state"
            ) from error
        if not isinstance(manifest, dict):
            raise LeaseError("persistent Android manifest is invalid; preserving state")
        return manifest

    def verify_avd(self) -> None:
        _require_private_directory(self.directory, "Android state_directory")
        _require_private_directory(self.private_home, "persistent Android home")
        _require_private_directory(self.avd_home, "persistent Android AVD home")
        if self._read_manifest() != self.manifest:
            raise LeaseError(
                "persistent Android manifest changed during use; preserving state"
            )
        try:
            entries = set()
            for index, entry in enumerate(self.directory.iterdir()):
                if index >= 64:
                    raise LeaseError(
                        "persistent Android state has too many entries; preserving state"
                    )
                entries.add(entry.name)
        except OSError as error:
            raise LeaseError("cannot inspect persistent Android state; preserving it") from error
        if entries != {MANIFEST_NAME, "android-home"}:
            raise LeaseError(
                "persistent Android state contains unknown entries; preserving state"
            )

        _require_owned_directory(self.avd_path, "persistent Android AVD")
        _regular_file(
            self.avd_path / "config.ini", "persistent Android AVD config", MAX_CONFIG_BYTES,
        )
        locator = self.avd_home / f"{self.avd_name}.ini"
        data = _read_private_file(
            locator, "persistent Android AVD locator", MAX_AVD_INI_BYTES, private=False,
        )
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise LeaseError("persistent Android AVD locator is invalid; preserving state") from error
        locations = [
            line.split("=", 1)[1].strip()
            for line in lines
            if "=" in line and line.split("=", 1)[0].strip() == "path"
        ]
        if len(locations) != 1:
            raise LeaseError("persistent Android AVD locator is incomplete; preserving state")
        try:
            located_path = Path(locations[0]).resolve(strict=True)
            avd_path = self.avd_path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise LeaseError("persistent Android AVD path is unavailable; preserving state") from error
        if located_path != avd_path:
            raise LeaseError(
                "persistent Android AVD locator points elsewhere; preserving state"
            )

    def mark_complete(self) -> None:
        if self.complete:
            self.verify_avd()
            return
        self.verify_avd()
        replacement = dict(self.manifest)
        replacement["status"] = "complete"
        self._replace_manifest(replacement)
        self.manifest = replacement
