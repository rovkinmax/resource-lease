"""User-owned receipts and JSON CLI; lease mutations are short transactions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .model import Config, LeaseError, MAX_RECORD, TERMINAL, number
from .service import Service
from .store import Store, canonical, private_file


def read_json(path: Path, *, private=False):
    if private:
        private_file(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECORD + 1)
    if len(raw) > MAX_RECORD:
        raise LeaseError("input exceeds 64 KiB")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise LeaseError(f"invalid JSON: {path}") from error


def receipt(store, path, owner, capability):
    path = path.expanduser().absolute()
    if path.exists() or path.is_symlink():
        result = read_json(path, private=True)
        store._validate_receipt(result, owner)
        if result.get("capability") != capability:
            raise LeaseError("existing lease file was created for another capability")
        return result
    result = store.new_receipt(owner, capability)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(canonical(result) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return result


def parser():
    result = argparse.ArgumentParser(description="Standalone cooperative resource leasing")
    result.add_argument("--root", type=Path, required=True, help="private pool state directory")
    commands = result.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="initialize a new pool without launching resources")
    init.add_argument("--config", type=Path, required=True)
    for action in ("serve", "start", "status"):
        commands.add_parser(action)
    shutdown = commands.add_parser("shutdown", help="revoke leases, stop owned resources and service")
    shutdown.add_argument("--wait", type=float, default=120)
    for action in ("acquire", "renew", "release", "cancel", "inspect"):
        command = commands.add_parser(action)
        command.add_argument("--owner", required=True)
        command.add_argument("--lease", type=Path, required=True,
                             help="private caller receipt; never put its secret in argv")
        if action == "acquire":
            command.add_argument("--capability", required=True)
            command.add_argument("--wait", type=float, default=300,
                                 help="seconds to await readiness; timeout cancels this request")
        elif action in ("release", "cancel"):
            command.add_argument("--wait", type=float, default=120,
                                 help="seconds to await teardown; timeout never transfers the resource")
    return result


def _wait_result(store, value, owner, timeout, *, acquiring):
    number(timeout, "wait timeout", 0, 86400)
    deadline = time.monotonic() + timeout
    next_renew = time.monotonic()
    try:
        while True:
            status = store.lease(value, owner)
            if acquiring and status["state"] == "active" and status["endpoint"]:
                return status
            if status["state"] in TERMINAL and not status["cleanup_pending"]:
                if acquiring or status["state"] == "failed":
                    raise LeaseError(f"lease {status['state']}: {status['error'] or 'request ended'}")
                return status
            if not acquiring and not status["cleanup_pending"] and status["state"] != "releasing":
                return status
            now = time.monotonic()
            if now >= deadline:
                if acquiring:
                    store.release(value, owner, cancel=True)
                raise LeaseError("wait timed out; inspect the lease for cleanup status")
            if acquiring:
                if not status["service_available"]:
                    raise LeaseError("service stopped while acquiring; inspect before retry")
                if now >= next_renew:
                    store.renew(value, owner)
                    next_renew = now + min(10, store.config.lease_seconds / 3)
            time.sleep(min(0.2, max(0, deadline - now)))
    except KeyboardInterrupt:
        if acquiring:
            store.release(value, owner, cancel=True)
        raise


def start_service(store, backends):
    if store.status()["service"]["available"]:
        return store.status()
    # Validate backend configuration before creating a background process.
    Service(store, backends)
    log_path = store.root / "service.log"
    if log_path.exists():
        private_file(log_path)
        if log_path.stat().st_size > 1024 * 1024:
            previous = store.root / "service.previous.log"
            if previous.exists():
                private_file(previous)
            log_path.replace(previous)
    descriptor = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    spec = getattr(sys.modules["__main__"], "__spec__", None)
    executable = (["-m", "resourcelease"]
                  if spec is not None and spec.name == "resourcelease.__main__"
                  else [str(Path(sys.argv[0]).resolve())])
    command = [sys.executable, *executable, "--root", str(store.root), "serve"]
    with os.fdopen(descriptor, "ab") as log:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=log, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = store.status()
        if status["service"]["available"]:
            return status
        if child.poll() is not None:
            raise LeaseError(f"service startup failed; see {log_path}")
        time.sleep(0.1)
    raise LeaseError(f"service startup timed out; inspect {log_path} before starting again")


def main(argv=None, *, backends=None):
    args = parser().parse_args(argv)
    try:
        if backends is None:
            raise LeaseError("no backend assembly provided by this executable")
        if args.action == "init":
            config = Config.parse(read_json(args.config))
            # Pure validation before state creation. Physical exclusion is checked
            # by Service before effects, including duplicate keys across slots.
            for slot in config.slots:
                if slot.backend not in backends:
                    raise LeaseError(f"unsupported backend: {slot.backend}")
                backends[slot.backend].keys(slot)
            output = Store.initialize(args.root, config).status()
        else:
            store = Store(args.root)
            if args.action == "status":
                output = store.status()
            elif args.action == "serve":
                Service(store, backends).serve(install_signals=True)
                output = store.status()
            elif args.action == "start":
                output = start_service(store, backends)
            elif args.action == "shutdown":
                number(args.wait, "wait timeout", 0, 86400)
                output = store.shutdown()
                deadline = time.monotonic() + args.wait
                while output["service"]["state"] != "stopped":
                    if time.monotonic() >= deadline:
                        raise LeaseError("shutdown pending; inspect status; no resource was force-released")
                    time.sleep(0.2)
                    output = store.status()
            elif args.action == "acquire":
                number(args.wait, "wait timeout", 0, 86400)
                value = receipt(store, args.lease, args.owner, args.capability)
                store.register(value, args.owner)
                output = _wait_result(store, value, args.owner, args.wait, acquiring=True)
            else:
                value = read_json(args.lease.expanduser(), private=True)
                if args.action == "inspect":
                    output = store.lease(value, args.owner)
                elif args.action == "renew":
                    output = store.renew(value, args.owner)
                else:
                    number(args.wait, "wait timeout", 0, 86400)
                    store.release(value, args.owner, cancel=args.action == "cancel")
                    output = _wait_result(store, value, args.owner, args.wait, acquiring=False)
        print(canonical(output))
        return 0
    except (LeaseError, OSError, ValueError) as error:
        print(f"resource-lease: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("resource-lease: interrupted; inspect lease cleanup status", file=sys.stderr)
        return 130
