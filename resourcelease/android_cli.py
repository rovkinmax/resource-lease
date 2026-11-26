"""The pool-free Android front door; legacy command parsing is separate."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import pwd
import shutil
import signal
import sqlite3
import subprocess
import sys
from threading import Event
from typing import Iterator

from .admission import Admission
from .backends.android_clean import clean_android, payload_environment
from .backends.android_images import installed_image, list_images
from .cli import read_json, write_private_receipt
from .model import LeaseError, QuarantineError
from .store import private_directory


def _host_home() -> Path:
    # HOME and RESOURCE_LEASE_HOME are not authority selectors in this mode.
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resource-lease android")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="list exact IDs of installed Android images")
    run = commands.add_parser("run", help="wait for a fresh emulator, run a command and clean up")
    run.add_argument("--image", required=True, help="exact package ID from android list")
    run.add_argument("--device", help="SDK hardware profile ID when the image needs one")
    run.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND [ARG ...]")
    configure = commands.add_parser("configure", help="initialize this host once")
    configure.add_argument("--sdk", required=True, type=Path)
    configure.add_argument("--java-home", required=True, type=Path)
    return parser


def _configure(root: Path, sdk: Path, java_home: Path) -> None:
    sdk = sdk.expanduser().resolve(strict=True)
    java_home = java_home.expanduser().resolve(strict=True)
    if not (sdk / "system-images").is_dir() or not os.access(java_home / "bin/java", os.X_OK):
        raise LeaseError("configure a local Android SDK with images and an installed JDK")
    private_directory(root)
    write_private_receipt(root / "host.json", {
        "sdk": str(sdk), "java_home": str(java_home),
    })
    Admission.initialize(root)


@contextmanager
def _cancelled() -> Iterator[Event]:
    event = Event()

    def cancel(_signum, _frame):
        event.set()

    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield event
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _stop_payload(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as error:
            if process.poll() is None:
                raise QuarantineError("lease command did not exit; release not confirmed") from error


def _payload(command: list[str], environment: dict[str, str], cancelled: Event, resource) -> int:
    if cancelled.is_set():
        return 130
    try:
        # Foreground TTY and output stay with the caller. Only this actual
        # child is supervised; daemonizing is not supported.
        process = subprocess.Popen(command, env=environment)
    except OSError as error:
        raise LeaseError(f"could not launch lease command: {command[0]}") from error
    try:
        while not cancelled.is_set():
            try:
                result = process.wait(timeout=0.25)
                return result if result >= 0 else 128 - result
            except subprocess.TimeoutExpired:
                if not resource.healthy():
                    raise LeaseError("owned Android emulator stopped while running the command")
        return 130
    finally:
        _stop_payload(process)


def _run(args: argparse.Namespace, root: Path, guards: Path, sdk: Path, java: Path) -> int:
    command = args.command
    if len(command) < 2 or command[0] != "--":
        raise LeaseError("expected -- COMMAND after the run options")
    image = installed_image(sdk, args.image)
    admission = Admission(root)
    with _cancelled() as cancelled:
        request = admission.enqueue()
        admitted = False
        cleaned = True
        generation = None
        try:
            said_waiting = False
            while not cancelled.is_set():
                if admission.try_admit(request):
                    admitted = True
                    break
                if not said_waiting:
                    print("Waiting for an Android lease…", file=sys.stderr, flush=True)
                    said_waiting = True
                cancelled.wait(0.2)
            if cancelled.is_set():
                return 130
            requested_path = private_directory(root / "generations") / request
            requested_path.mkdir(mode=0o700)
            generation = requested_path
            with clean_android(
                sdk_root=sdk, java_home=java, guards=private_directory(guards),
                image=image, device=args.device, generation=generation, request=request,
                cancelled=cancelled,
            ) as (resource, adb):
                return _payload(command[1:], payload_environment(sdk, adb, resource), cancelled, resource)
        except QuarantineError:
            cleaned = False
            raise
        except (LeaseError, OSError):
            if cancelled.is_set():
                return 130
            raise
        except BaseException:
            cleaned = False
            raise
        finally:
            if admitted:
                if cleaned and generation is not None:
                    # This invocation created the exact private generation.
                    try:
                        shutil.rmtree(generation)
                    except OSError:
                        cleaned = False
                        raise
                    finally:
                        admission.finish(request, cleanup_confirmed=cleaned)
                else:
                    admission.finish(request, cleanup_confirmed=cleaned)
            else:
                admission.cancel_waiting(request)


def main(argv: list[str], *, home: Path | None = None) -> int:
    args = _parser().parse_args(argv)
    base = (home or _host_home()) / ".local/state/resource-lease"
    root = base / "android-host"
    try:
        if args.action == "configure":
            _configure(root, args.sdk, args.java_home)
            return 0
        settings = read_json(root / "host.json", private=True)
        if not isinstance(settings, dict) or set(settings) != {"sdk", "java_home"} or not all(
            isinstance(value, str) and Path(value).is_absolute() for value in settings.values()
        ):
            raise LeaseError("invalid Android host configuration")
        sdk = Path(settings["sdk"])
        if args.action == "list":
            for image in list_images(sdk):
                print(image.package)
            return 0
        return _run(args, root, base / "guards", sdk, Path(settings["java_home"]))
    except (LeaseError, OSError, sqlite3.Error) as error:
        print(f"resource-lease android: {error}", file=sys.stderr)
        return 1
