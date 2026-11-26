"""A disposable Android launch that owns its AVD, ports, and ADB server."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from threading import Event
from typing import Iterator

from resourcelease.model import LeaseError, QuarantineError, Slot
from resourcelease.service import Busy, Guard

from .android import (
    AndroidBackend, AndroidResource, MAX_EMULATOR_PORT, _assert_ports_free, _parse_options,
)
from .android_adb import OwnedAdb
from .android_images import InstalledImage, installed_image
from .android_placement import AndroidPlacement


# Beyond the SDK's default shared-server scan (guest ADB ports 5555..5585).
FIRST_PRIVATE_CONSOLE_PORT = 5586


def payload_environment(
    sdk_root: Path, adb: OwnedAdb, resource: AndroidResource,
) -> dict[str, str]:
    environment = adb.environment(os.environ, adb.port, serial=resource.endpoint["adb_serial"])
    environment["ANDROID_HOME"] = environment["ANDROID_SDK_ROOT"] = str(sdk_root)
    environment["PATH"] = os.pathsep.join((
        str(sdk_root / "platform-tools"), environment.get("PATH", os.defpath),
    ))
    return environment


@contextmanager
def clean_android(
    *,
    sdk_root: Path,
    java_home: Path,
    guards: Path,
    image: InstalledImage,
    device: str | None,
    generation: Path,
    request: str,
    cancelled: Event,
) -> Iterator[tuple[AndroidResource, OwnedAdb]]:
    backend = AndroidBackend(placement=AndroidPlacement(guards))
    for port in range(FIRST_PRIVATE_CONSOLE_PORT, MAX_EMULATOR_PORT + 1, 2):
        slot = Slot("clean", ("clean",), "android", {
            "sdk_root": str(sdk_root), "java_home": str(java_home),
            "system_image": image.package, "console_port": port,
            **({"device_profile": device} if device is not None else {}),
        })
        options = _parse_options(slot)
        guard = Guard(
            guards, tuple(f"tcp-port:{p}" for p in options.ports), "android-clean-v1", request,
        )
        try:
            guard.__enter__()
        except (Busy, QuarantineError):
            # These markers belong to a different generation. Never clear them.
            continue
        except OSError as error:
            raise QuarantineError("could not establish physical guard ownership") from error
        adb = None
        resource = None
        unsafe = False
        started = False
        try:
            try:
                _assert_ports_free(options)
                started = True
                if installed_image(sdk_root, image.package) != image:
                    raise LeaseError("selected Android system image changed while waiting")
                adb = OwnedAdb.start(options.adb, generation, cancelled)
                resource = backend.start(slot, generation, cancelled, owned_adb=adb)
                if installed_image(sdk_root, image.package) != image:
                    raise LeaseError("selected Android system image changed during startup")
                adb.assert_devices(resource.endpoint["adb_serial"])
                yield resource, adb
            except QuarantineError:
                unsafe = True
                raise
            except LeaseError:
                if started:
                    raise
            finally:
                failures = []
                for child in (resource, adb):
                    if child is not None:
                        try:
                            child.stop()
                        except BaseException as error:
                            failures.append(error)
                if failures:
                    raise QuarantineError("owned Android run did not clean up") from failures[0]
                if not unsafe:
                    try:
                        guard.clear()
                    except OSError as error:
                        raise QuarantineError("failed to clear owned guard after cleanup") from error
        finally:
            guard.__exit__(None, None, None)
        if started:
            return
    raise LeaseError("no checked Android console/ADB/transport port group is available")
