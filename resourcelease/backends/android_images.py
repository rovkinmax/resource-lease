"""Exact, read-only inventory of installed Android SDK system images."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import stat
import xml.etree.ElementTree as ET

from resourcelease.model import LeaseError

from .android import SYSTEM_IMAGE


MAX_METADATA_BYTES = 256 * 1024


@dataclass(frozen=True)
class InstalledImage:
    package: str
    revision: tuple[int, ...]
    signature: tuple[int, int, int, int, str]


def _direct_child(xml: ET.Element, name: str) -> ET.Element:
    children = [child for child in xml if child.tag.rpartition("}")[2] == name]
    if len(children) != 1:
        raise LeaseError(f"expected a single {name} in Android SDK metadata")
    return children[0]


def installed_image(sdk_root: Path, package: str) -> InstalledImage:
    if not SYSTEM_IMAGE.fullmatch(package):
        raise LeaseError("expected an Android SDK system-image package ID")
    path = sdk_root.joinpath(*package.split(";"))
    try:
        # Installed catalog paths are canonical; never read redirected metadata,
        # directory aliases, or arbitrary caller-supplied image paths.
        for asset in (path / "package.xml", path / "system.img"):
            if asset.resolve(strict=True) != asset:
                raise LeaseError(f"noncanonical installed Android system image: {package}")
        package_file = path / "package.xml"
        if not stat.S_ISREG(package_file.lstat().st_mode):
            raise LeaseError(f"invalid SDK package declaration: {package}")
        with package_file.open("rb") as stream:
            metadata = stream.read(MAX_METADATA_BYTES + 1)
        image = (path / "system.img").lstat()
    except (OSError, RuntimeError) as error:
        raise LeaseError(f"Android system image is not installed: {package}") from error
    if len(metadata) > MAX_METADATA_BYTES or not stat.S_ISREG(image.st_mode):
        raise LeaseError(f"invalid installed Android system image: {package}")
    if b"<!DOCTYPE" in metadata or b"<!ENTITY" in metadata or b"\0" in metadata:
        raise LeaseError(f"unsupported SDK package declaration: {package}")
    try:
        declaration = _direct_child(ET.fromstring(metadata), "localPackage")
        if declaration.get("path") != package:
            raise LeaseError(f"installed SDK declaration does not match {package}")
        version = _direct_child(declaration, "revision")
        components = []
        for name in ("major", "minor", "micro"):
            values = [element for element in version if element.tag.rpartition("}")[2] == name]
            if len(values) > 1 or (name == "major" and not values):
                raise ValueError("invalid revision fields")
            value = int(values[0].text or "") if values else 0
            if not 0 <= value < 2**31:
                raise ValueError("invalid revision")
            components.append(value)
        revision = tuple(components)
    except LeaseError:
        raise
    except (ET.ParseError, ValueError) as error:
        raise LeaseError(f"invalid SDK package declaration: {package}") from error
    return InstalledImage(
        package, revision, (
            image.st_dev, image.st_ino, image.st_size, image.st_mtime_ns,
            hashlib.sha256(metadata).hexdigest(),
        ),
    )


def list_images(sdk_root: Path) -> list[InstalledImage]:
    result = []
    for declaration in sorted((sdk_root / "system-images").glob("*/*/*/package.xml")):
        package = ";".join(declaration.parent.relative_to(sdk_root).parts)
        try:
            result.append(installed_image(sdk_root, package))
        except LeaseError:
            # Malformed or partially removed images are not launch candidates.
            continue
    return result
