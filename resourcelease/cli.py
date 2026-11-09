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
from .operations import Operations
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
    write_private_receipt(path, result)
    return result


def write_private_receipt(path: Path, result: dict) -> None:
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


def parser():
    result = argparse.ArgumentParser(description="Standalone cooperative resource leasing")
    result.add_argument("--root", type=Path, required=True, help="private pool state directory")
    commands = result.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="initialize a new pool without launching resources")
    init.add_argument("--config", type=Path, required=True)
    for action in ("serve", "start", "status"):
        commands.add_parser(action)
    commands.add_parser("migrate", help="explicitly upgrade a stopped, settled pool")
    reset = commands.add_parser("reset", help="reset persistent state while preserving custody")
    _maintenance_arguments(reset, "reset")
    state = commands.add_parser("state", help="inspect and manage persistent state backups")
    state_commands = state.add_subparsers(dest="state_action", required=True)
    for action in ("inspect", "restore", "retire"):
        _maintenance_arguments(state_commands.add_parser(action), action)
    operation = commands.add_parser("operation", help="inspect an existing maintenance operation")
    operation_commands = operation.add_subparsers(dest="operation_action", required=True)
    operation_inspect = operation_commands.add_parser("inspect")
    operation_inspect.add_argument("--operation", type=Path, required=True)
    operation_recover = operation_commands.add_parser(
        "recover", help="reconcile an exact interrupted idle maintenance operation",
    )
    operation_recover.add_argument("--operation", type=Path, required=True)
    operation_recover.add_argument("--wait", type=float, default=300)
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


def _maintenance_arguments(command, action):
    command.set_defaults(lease=None, backup_id=None)
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument("--slot")
    if action in ("inspect", "reset"):
        target.add_argument("--lease", type=Path)
    command.add_argument("--owner")
    command.add_argument("--operation", type=Path, required=True)
    command.add_argument("--expect-generation", type=int, required=True)
    command.add_argument("--expect-state-revision", required=action != "inspect")
    if action in ("restore", "retire"):
        command.add_argument("--backup-id", required=True)
    command.add_argument("--wait", type=float, default=300)


def _wait_operation(store, operations, value, lease, owner, timeout):
    number(timeout, "wait timeout", 0, 86400)
    deadline = time.monotonic() + timeout
    next_renew = time.monotonic()
    while True:
        state = operations.inspect(value)
        if state["state"] == "completed":
            return state
        if state["state"] == "failed":
            raise LeaseError(f"maintenance failed: {state['error']}; inspect the operation")
        now = time.monotonic()
        if now >= deadline:
            raise LeaseError("maintenance wait timed out; inspect the original operation")
        if lease is not None and now >= next_renew:
            store.renew(lease, owner)
            next_renew = now + min(10.0, store.config.lease_seconds / 3)
        time.sleep(min(0.1, max(0, deadline - now)))


def _maintenance_command(store, args, maintenance_backends):
    number(args.wait, "wait timeout", 0, 86400)
    action = "reset" if args.action == "reset" else args.state_action
    lease = read_json(args.lease.expanduser(), private=True) if args.lease else None
    if (lease is None) != (args.owner is None):
        raise LeaseError("--owner and --lease must be supplied together")
    slot_id = args.slot
    if lease is not None:
        slot_id = store.lease(lease, args.owner)["slot"]
        if slot_id is None:
            raise LeaseError("maintenance requires an assigned lease")
    operations = Operations(store)
    path = args.operation.expanduser().absolute()
    if path.exists() or path.is_symlink():
        value = read_json(path, private=True)
        operations.validate_receipt(
            value, action=action, slot_id=slot_id,
            expected_generation=args.expect_generation,
            expected_revision=args.expect_state_revision, backup_id=args.backup_id,
            lease=lease, owner=args.owner,
        )
    else:
        slot = next((slot for slot in store.config.slots if slot.id == slot_id), None)
        if slot is None:
            raise LeaseError("unknown slot")
        backend = maintenance_backends.get(slot.backend)
        if backend is None:
            raise LeaseError("backend does not support state maintenance")
        # Pure options/capability validation; filesystem effects remain service-owned.
        backend.maintenance_key(slot)
        value = operations.new_receipt(
            action, slot_id, args.expect_generation, args.expect_state_revision,
            args.backup_id, lease=lease, owner=args.owner,
        )
        write_private_receipt(path, value)
    operations.submit(value, lease=lease, owner=args.owner)
    return _wait_operation(store, operations, value, lease, args.owner, args.wait)


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


def start_service(store, backends, *, guard_root=None):
    if store.status()["service"]["available"]:
        return store.status()
    # Validate backend configuration before creating a background process.
    Service(store, backends, guard_root=guard_root)
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


def main(argv=None, *, backends=None, maintenance_backends=None, guard_root=None):
    args = parser().parse_args(argv)
    try:
        if backends is None:
            raise LeaseError("no backend assembly provided by this executable")
        maintenance_backends = maintenance_backends or {}
        if args.action == "init":
            config = Config.parse(read_json(args.config))
            # Pure validation before state creation. Physical exclusion is checked
            # by Service before effects, including duplicate keys across slots.
            for slot in config.slots:
                if slot.backend not in backends:
                    raise LeaseError(f"unsupported backend: {slot.backend}")
                backends[slot.backend].keys(slot)
            output = Store.initialize(args.root, config).status()
        elif args.action == "migrate":
            output = Store.migrate(args.root).status()
        else:
            store = Store(args.root)
            if args.action == "status":
                output = store.status()
            elif args.action == "serve":
                Service(
                    store, backends, maintenance_backends=maintenance_backends,
                    guard_root=guard_root,
                ).serve(install_signals=True)
                output = store.status()
            elif args.action == "start":
                output = start_service(store, backends, guard_root=guard_root)
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
            elif args.action in ("reset", "state"):
                output = _maintenance_command(store, args, maintenance_backends)
            elif args.action == "operation":
                value = read_json(args.operation.expanduser(), private=True)
                operations = Operations(store)
                if args.operation_action == "recover":
                    number(args.wait, "wait timeout", 0, 86400)
                    operations.recover(value)
                    output = _wait_operation(store, operations, value, None, None, args.wait)
                else:
                    output = operations.inspect(value)
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
