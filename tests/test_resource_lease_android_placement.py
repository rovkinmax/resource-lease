"""Deterministic tests for Android persistent-state placement claims."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from threading import Event, Thread
import unittest
from unittest import mock

from resourcelease.backends import android_placement as placement_module
from resourcelease.backends.android_placement import AndroidPlacement
from resourcelease.backends.android_state import state_identity
from resourcelease.model import LeaseError, QuarantineError


class AndroidPlacementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.guard_root = self.root / "guards"
        self.placement = AndroidPlacement(self.guard_root)

    def paths(self, directory: Path):
        directory = self.placement._canonical_target(directory)
        identity = state_identity(directory)
        maintenance = directory.parent / (
            placement_module.MAINTENANCE_DIRECTORY_PREFIX + identity
        )
        return identity, maintenance

    def test_reservation_precedes_root_creation_and_is_private_durable_metadata(self):
        directory = self.placement._canonical_target(self.root / "state")
        identity, maintenance = self.paths(directory)

        self.placement.reserve(directory, identity, maintenance)

        self.assertFalse(directory.exists())
        self.assertFalse(maintenance.exists())
        records = list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*"))
        self.assertEqual(len(records), 2)
        for record_path in records:
            info = record_path.lstat()
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(info.st_uid, os.getuid())
            self.assertEqual(info.st_mode & 0o777, 0o600)
            self.assertEqual(info.st_nlink, 1)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["state_identity"], identity)
            self.assertEqual(record["active_path"], str(directory))
            self.assertEqual(record["maintenance_path"], str(maintenance))
        with mock.patch.object(
            AndroidPlacement, "_check_descendant_boundaries",
            side_effect=AssertionError("existing claims must avoid a recursive tree scan"),
        ):
            self.placement.reserve(directory, identity, maintenance)

    def test_nested_reservation_is_rejected_before_creating_child_claims(self):
        parent = self.root / "parent"
        parent_identity, parent_maintenance = self.paths(parent)
        self.placement.reserve(parent, parent_identity, parent_maintenance)
        parent.mkdir()

        child = parent / "child"
        child_identity, child_maintenance = self.paths(child)
        with self.assertRaises(LeaseError):
            self.placement.reserve(child, child_identity, child_maintenance)

        self.assertFalse(child.exists())
        self.assertFalse(child_maintenance.exists())
        self.assertEqual(len(list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*"))), 2)

    def test_parent_reservation_is_rejected_when_descendant_claim_exists(self):
        parent = self.root / "parent"
        parent.mkdir()
        child = parent / "child"
        child_identity, child_maintenance = self.paths(child)
        self.placement.reserve(child, child_identity, child_maintenance)

        parent_identity, parent_maintenance = self.paths(parent)
        with self.assertRaises(LeaseError):
            self.placement.reserve(
                parent, parent_identity, parent_maintenance,
                legacy_validator=lambda: None,
            )

        self.assertFalse(parent_maintenance.exists())
        self.assertEqual(len(list(parent.rglob(f"{placement_module.CLAIM_PREFIX}*"))), 2)

    def test_case_alias_of_maintenance_prefix_rejects_nested_state(self):
        parent = self.root / "parent"
        parent_identity, parent_maintenance = self.paths(parent)
        self.placement.reserve(parent, parent_identity, parent_maintenance)
        parent_maintenance.mkdir(mode=0o700)
        alias_maintenance = parent.parent / (
            placement_module.MAINTENANCE_DIRECTORY_PREFIX.upper() + parent_identity
        )
        if not alias_maintenance.is_dir():
            self.skipTest("temporary filesystem is case-sensitive")

        nested = alias_maintenance / "nested"
        nested_identity, nested_maintenance = self.paths(nested)
        with self.assertRaises(LeaseError):
            self.placement.reserve(nested, nested_identity, nested_maintenance)

        self.assertFalse(nested.exists())
        self.assertFalse(nested_maintenance.exists())
        self.assertEqual(len(list(self.root.rglob(f"{placement_module.CLAIM_PREFIX}*"))), 2)

    def test_case_alias_resolves_existing_reservation_or_remains_independent(self):
        probe = self.root / "CaseProbe"
        probe.mkdir()
        case_sensitive = not (self.root / "caseprobe").is_dir()

        original = self.root / "Persistent-State"
        identity, maintenance = self.paths(original)
        self.placement.reserve(original, identity, maintenance)

        alias = self.root / "persistent-state"
        alias_identity, alias_maintenance = self.paths(alias)
        if case_sensitive:
            self.assertNotEqual(alias_identity, identity)
            self.placement.reserve(alias, alias_identity, alias_maintenance)
            self.placement.require(alias, alias_identity, alias_maintenance)
            self.assertEqual(
                len(list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*"))), 4,
            )
        else:
            with self.assertRaises((LeaseError, QuarantineError)):
                self.placement.reserve(alias, alias_identity, alias_maintenance)

        if case_sensitive:
            self.assertEqual(len(list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*"))), 4)
        else:
            self.assertEqual(len(list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*"))), 2)
        self.placement.require(original, identity, maintenance)

    def test_partial_claim_is_preserved_and_never_repaired_or_overwritten(self):
        directory = self.root / "state"
        identity, maintenance = self.paths(directory)
        partial = self.root / f"{placement_module.CLAIM_PREFIX}{directory.name}"
        partial.write_bytes(b'{"schema":')
        before = partial.read_bytes()

        with self.assertRaises(QuarantineError):
            self.placement.reserve(directory, identity, maintenance)

        self.assertEqual(partial.read_bytes(), before)
        self.assertFalse(directory.exists())
        self.assertFalse(maintenance.exists())

    def test_existing_legacy_root_requires_a_validator_before_claim_publication(self):
        directory = self.root / "legacy"
        directory.mkdir(mode=0o700)
        identity, maintenance = self.paths(directory)

        with self.assertRaises(placement_module.LegacyPlacementRegistration):
            self.placement.reserve(directory, identity, maintenance)
        self.assertEqual(list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*")), [])

        called = []
        self.placement.reserve(
            directory,
            identity,
            maintenance,
            legacy_validator=lambda: called.append("validated"),
        )

        self.assertEqual(called, ["validated"])
        self.assertEqual(len(list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*"))), 2)

    def test_overlong_raw_basename_claim_is_rejected_without_hash_fallback(self):
        directory = self.root / ("s" * 100)
        identity, maintenance = self.paths(directory)
        with mock.patch.object(placement_module.os, "pathconf", return_value=64):
            with self.assertRaisesRegex(LeaseError, "reservation name"):
                self.placement.reserve(directory, identity, maintenance)

        self.assertEqual(list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*")), [])

    def test_transaction_is_reentrant_and_serializes_other_instances(self):
        other = AndroidPlacement(self.guard_root)
        entered = Event()
        release = Event()
        second_entered = Event()

        with self.placement.transaction():
            with self.placement.transaction():
                pass

        def hold_gate():
            with self.placement.transaction():
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release placement gate")

        def wait_for_gate():
            with other.transaction():
                second_entered.set()

        first = Thread(target=hold_gate)
        second = Thread(target=wait_for_gate)
        first.start()
        self.addCleanup(first.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))

        second.start()
        self.addCleanup(second.join, 10)
        self.assertFalse(second_entered.wait(0.1))
        release.set()
        first.join(10)
        second.join(10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())

    def test_gate_serializes_across_processes(self):
        release = self.root / "release-child-gate"
        script = """
from pathlib import Path
import sys
import time
from resourcelease.backends.android_placement import AndroidPlacement

root = Path(sys.argv[1])
with AndroidPlacement(root, lock_timeout=5).transaction():
    print("held", flush=True)
    release = Path(sys.argv[2])
    deadline = time.monotonic() + 5
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.guard_root), str(release)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def clean_child():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(clean_child)
        self.assertEqual(process.stdout.readline().strip(), "held")
        attempting = Event()
        entered = Event()

        def acquire_in_parent():
            attempting.set()
            with self.placement.transaction():
                entered.set()

        waiter = Thread(target=acquire_in_parent)
        waiter.start()
        self.addCleanup(waiter.join, 10)
        self.addCleanup(release.touch)
        self.assertTrue(attempting.wait(5))
        self.assertFalse(entered.wait(0.1))
        release.touch()
        waiter.join(10)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(entered.is_set())
        self.assertEqual(process.wait(timeout=5), 0)

    def test_process_exit_after_first_claim_leaves_fail_closed_partial_record(self):
        directory = self.root / "state"
        identity, maintenance = self.paths(directory)
        script = """
import os
from pathlib import Path
import sys
from resourcelease.backends.android_placement import AndroidPlacement
from resourcelease.backends.android_state import state_identity

root = Path(sys.argv[1])
directory = root.resolve() / "state"
identity = state_identity(directory)
maintenance = directory.parent / (
    ".resource-lease-android-maintenance-" + identity
)
placement = AndroidPlacement(root / "guards")
create = placement._create_claim
def create_then_exit(path, record):
    create(path, record)
    os._exit(73)
placement._create_claim = create_then_exit
placement.reserve(directory, identity, maintenance)
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 73)

        claims = list(self.root.glob(f"{placement_module.CLAIM_PREFIX}*"))
        self.assertEqual(len(claims), 1)
        original = claims[0].read_bytes()
        with self.assertRaises(QuarantineError):
            self.placement.reserve(directory, identity, maintenance)
        self.assertEqual(claims[0].read_bytes(), original)
        self.assertFalse(directory.exists())

    def test_gate_contention_fails_within_configured_wait_bound(self):
        impatient = AndroidPlacement(self.guard_root, lock_timeout=0.05)
        started = Event()
        release = Event()

        def hold_gate():
            with self.placement.transaction():
                started.set()
                release.wait(5)

        holder = Thread(target=hold_gate)
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(5))

        began = time.monotonic()
        try:
            with self.assertRaisesRegex(LeaseError, "gate is busy"):
                with impatient.transaction():
                    pass
        finally:
            release.set()
        elapsed = time.monotonic() - began
        holder.join(10)
        self.assertFalse(holder.is_alive())
        self.assertLess(elapsed, 1)
