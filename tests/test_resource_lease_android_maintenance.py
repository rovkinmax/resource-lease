"""Deterministic tests for persistent Android state maintenance."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import uuid
from unittest import mock

from resourcelease.backends import android_maintenance as maintenance_module
from resourcelease.backends.android_maintenance import AndroidMaintenance
from resourcelease.backends.android_placement import AndroidPlacement
from resourcelease.backends.android_state import PersistentAndroidState
from resourcelease.model import LeaseError, QuarantineError


SYSTEM_IMAGE = "system-images;android-34;google_apis;arm64-v8a"
PROFILE = {
    "system_image": SYSTEM_IMAGE,
    "image_identity": {"package_id": SYSTEM_IMAGE, "revision": "14"},
    "memory_mb": 2048,
}


class AndroidMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.new_case()

    def new_case(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.placement = AndroidPlacement(self.root / "guards")
        self.state_directory = self.root / "persistent-state"
        self.maintenance = AndroidMaintenance(
            self.state_directory,
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=PROFILE["memory_mb"],
            placement=self.placement,
        )
        self.state_directory = self.maintenance.directory
        self.custody = {"pool_id": "pool-a", "lease_id": "lease-a"}

    def create_complete_state(self) -> PersistentAndroidState:
        state = PersistentAndroidState.prepare(
            self.state_directory,
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=PROFILE["memory_mb"],
            placement=self.placement,
        )
        state.avd_path.mkdir(mode=0o700)
        (state.avd_path / "config.ini").write_text(
            "AvdId=resource-lease-test\n", encoding="utf-8",
        )
        locator = state.avd_home / f"{state.avd_name}.ini"
        locator.write_text(f"path={state.avd_path}\n", encoding="utf-8")
        state.mark_complete()
        return state

    def create_managed_state(self, directory: Path) -> tuple[PersistentAndroidState, AndroidMaintenance]:
        directory = maintenance_module.canonical_state_directory(os.fspath(directory))
        state = PersistentAndroidState.prepare(
            directory,
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=PROFILE["memory_mb"],
            placement=self.placement,
        )
        state.avd_path.mkdir(mode=0o700)
        (state.avd_path / "config.ini").write_text(
            "AvdId=resource-lease-nested-test\n", encoding="utf-8",
        )
        (state.avd_home / f"{state.avd_name}.ini").write_text(
            f"path={state.avd_path}\n", encoding="utf-8",
        )
        (state.avd_path / "user-data").write_bytes(b"nested resource data")
        state.mark_complete()
        maintenance = AndroidMaintenance(
            directory,
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=PROFILE["memory_mb"],
            placement=self.placement,
        )
        maintenance.inspect()
        return state, maintenance

    def leave_archive_intent_after_move(self) -> dict[str, object]:
        self.create_complete_state()
        before = self.maintenance.inspect()
        real_sync = maintenance_module._sync_directory
        failed = False

        def fail_source_sync(path: Path):
            nonlocal failed
            if path == self.state_directory.parent and not failed:
                failed = True
                raise OSError(errno.EIO, "injected archive recovery boundary")
            return real_sync(path)

        with mock.patch.object(
            maintenance_module, "_sync_directory", side_effect=fail_source_sync,
        ):
            with self.assertRaises(QuarantineError):
                self.maintenance.archive(
                    "case-alias-reset", before["revision"], self.custody,
                )
        self.assertTrue(failed)
        self.assertFalse(self.state_directory.exists())
        return before

    def assert_case_insensitive_temporary_filesystem(self) -> None:
        mixed = self.root / "CaseSensitivityProbe"
        mixed.mkdir()
        if not (self.root / "casesensitivityprobe").is_dir():
            self.skipTest("temporary filesystem is case-sensitive")

    def maintenance_directory(self) -> Path:
        matches = list(self.root.glob(".resource-lease-android-maintenance-*"))
        self.assertEqual(len(matches), 1)
        return matches[0]

    def backup_directory(self, backup_id: str) -> Path:
        return self.maintenance_directory() / f"backup-{backup_id}"

    def filesystem_snapshot(self) -> list[tuple[object, ...]]:
        snapshot = []
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            data = path.read_bytes() if os.path.isfile(path) and not path.is_symlink() else None
            snapshot.append((
                path.relative_to(self.root).as_posix(),
                stat.S_IFMT(info.st_mode),
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                data,
            ))
        return snapshot

    def test_inspect_bootstraps_valid_legacy_state_without_changing_avd_bytes(self):
        state = self.create_complete_state()
        locator = state.avd_home / f"{state.avd_name}.ini"
        before = locator.read_bytes()

        snapshot = self.maintenance.inspect()

        self.assertEqual(snapshot["state"], "complete")
        self.assertIsNone(snapshot["backup_id"])
        self.assertEqual(locator.read_bytes(), before)
        self.assertEqual(
            json.loads(
                (self.state_directory / ".resource-lease-android-state.json").read_text()
            )["status"],
            "complete",
        )

    def test_inspect_registers_valid_legacy_state_without_changing_v1_manifest(self):
        with mock.patch.object(AndroidPlacement, "reserve"):
            self.create_complete_state()
        manifest_before = (
            self.state_directory / ".resource-lease-android-state.json"
        ).read_bytes()

        snapshot = self.maintenance.inspect()

        self.assertEqual(snapshot["state"], "complete")
        self.assertEqual(
            (self.state_directory / ".resource-lease-android-state.json").read_bytes(),
            manifest_before,
        )
        self.assertEqual(
            len(list(self.root.glob(f"{maintenance_module.PLACEMENT_CLAIM_PREFIX}*"))),
            2,
        )

    def test_absent_legacy_lineage_without_claim_fails_closed(self):
        self.maintenance._maintenance_directory.mkdir(mode=0o700)
        sentinel = self.maintenance._maintenance_directory / "operator-data"
        sentinel.write_bytes(b"preserve")
        with self.placement.transaction():
            pass
        before = self.filesystem_snapshot()

        with self.assertRaises(QuarantineError):
            self.maintenance.inspect()

        self.assertEqual(self.filesystem_snapshot(), before)

    def test_inspect_rejects_unknown_or_incomplete_legacy_state_without_adopting_it(self):
        self.state_directory.mkdir(mode=0o700)
        sentinel = self.state_directory / "operator-data"
        sentinel.write_bytes(b"keep")

        with self.assertRaisesRegex(LeaseError, "persistent Android manifest"):
            self.maintenance.inspect()

        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob(".resource-lease-android-maintenance-*")), [])

        sentinel.unlink()
        self.state_directory.rmdir()
        self.create_complete_state()
        PersistentAndroidState._open_existing(
            self.state_directory,
            maintenance_module.state_identity(self.state_directory),
            f"resource_lease_{maintenance_module.state_identity(self.state_directory)[:16]}",
            PROFILE,
        )
        manifest_path = self.state_directory / ".resource-lease-android-state.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "initializing"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(LeaseError, "initialization is incomplete"):
            self.maintenance.inspect()
        self.assertEqual(list(self.root.glob(".resource-lease-android-maintenance-*")), [])

    def test_check_start_bootstraps_legacy_complete_lineage_once(self):
        self.create_complete_state()

        self.assertIsNone(self.maintenance.check_start())
        first = self.maintenance.inspect()
        self.assertEqual(self.maintenance.check_start(), None)
        self.assertEqual(self.maintenance.inspect(), first)

    def test_parent_symlink_and_dotdot_aliases_share_canonical_lineage(self):
        self.create_complete_state()
        original = self.maintenance.inspect()
        parent_alias = self.root / "parent-alias"
        parent_alias.symlink_to(self.state_directory.parent, target_is_directory=True)
        alias = AndroidMaintenance(
            parent_alias / self.state_directory.name,
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=PROFILE["memory_mb"],
        )
        dotdot = self.root / "intermediate" / ".." / self.state_directory.name
        (self.root / "intermediate").mkdir()
        normalized = AndroidMaintenance(
            dotdot,
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=PROFILE["memory_mb"],
        )

        self.assertEqual(alias.identity, self.maintenance.identity)
        self.assertEqual(normalized.identity, self.maintenance.identity)
        self.assertEqual(alias.inspect(), original)
        self.assertEqual(normalized.inspect(), original)

    def test_case_alias_cannot_bypass_unresolved_archive_on_check_start(self):
        self.assert_case_insensitive_temporary_filesystem()
        self.leave_archive_intent_after_move()
        alias = AndroidMaintenance(
            self.root / self.state_directory.name.upper(),
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=PROFILE["memory_mb"],
        )
        filesystem_before = self.filesystem_snapshot()

        with self.assertRaises(QuarantineError):
            alias.check_start()

        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_case_alias_inspect_cannot_create_a_second_lineage(self):
        self.assert_case_insensitive_temporary_filesystem()
        self.leave_archive_intent_after_move()
        alias = AndroidMaintenance(
            self.root / self.state_directory.name.upper(),
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=PROFILE["memory_mb"],
        )
        filesystem_before = self.filesystem_snapshot()

        with self.assertRaises(QuarantineError):
            alias.inspect()

        self.assertEqual(self.filesystem_snapshot(), filesystem_before)
        self.assertEqual(
            len(list(self.root.glob(".resource-lease-android-maintenance-*"))), 1,
        )

    def test_archive_moves_opaque_absolute_locator_and_same_operation_is_idempotent(self):
        state = self.create_complete_state()
        locator = state.avd_home / f"{state.avd_name}.ini"
        original_locator = locator.read_bytes()
        original_manifest = (self.state_directory / ".resource-lease-android-state.json").read_bytes()
        before = self.maintenance.inspect()

        archived = self.maintenance.archive(
            "reset-1", before["revision"], self.custody,
        )

        self.assertEqual(archived["state"], "empty")
        self.assertIsNotNone(archived["backup_id"])
        backup = self.backup_directory(archived["backup_id"])
        self.assertTrue(backup.is_dir())
        self.assertFalse(self.state_directory.exists())
        self.assertEqual(
            (backup / "android-home" / "avd" / f"{state.avd_name}.ini").read_bytes(),
            original_locator,
        )
        self.assertEqual(
            (backup / ".resource-lease-android-state.json").read_bytes(),
            original_manifest,
        )

        retried = self.maintenance.archive(
            "reset-1", before["revision"], self.custody,
        )
        self.assertEqual(retried, archived)
        self.assertEqual(len(list(self.maintenance_directory().glob("backup-*"))), 1)

    def test_archive_refuses_nested_managed_state_before_moving_parent(self):
        parent_state = self.create_complete_state()
        before = self.maintenance.inspect()
        with (mock.patch.object(AndroidPlacement, "reserve"),
              mock.patch.object(AndroidMaintenance, "_reject_managed_ancestor")):
            child_state, child_maintenance = self.create_managed_state(
                parent_state.avd_home / "nested-state",
            )
        child_sentinel = child_state.avd_path / "user-data"
        child_bytes = child_sentinel.read_bytes()

        try:
            self.maintenance.archive(
                "archive-parent-with-child", before["revision"], self.custody,
            )
        except LeaseError:
            pass
        else:
            self.fail(
                "archive moved a nested managed state without its guard; "
                f"child exists at original path={child_state.directory.exists()}, "
                f"child journal={child_maintenance._maintenance_directory.exists()}"
            )

        self.assertTrue(self.state_directory.is_dir())
        self.assertEqual(child_sentinel.read_bytes(), child_bytes)
        self.assertTrue(child_maintenance._maintenance_directory.is_dir())

    def test_retire_refuses_backup_containing_nested_managed_state(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive(
            "archive-parent", before["revision"], self.custody,
        )
        with (mock.patch.object(AndroidPlacement, "reserve"),
              mock.patch.object(AndroidMaintenance, "_reject_managed_ancestor")):
            child_state, child_maintenance = self.create_managed_state(
                self.backup_directory(archived["backup_id"])
                / "android-home" / "avd" / "nested-state",
            )
        child_sentinel = child_state.avd_path / "user-data"
        child_bytes = child_sentinel.read_bytes()

        try:
            self.maintenance.retire(
                "retire-parent", archived["backup_id"], archived["revision"],
                self.custody,
            )
        except LeaseError:
            pass
        else:
            self.fail(
                "retire deleted a nested managed state without its guard; "
                f"child exists={child_state.directory.exists()}, "
                f"child journal={child_maintenance._maintenance_directory.exists()}"
            )

        self.assertEqual(child_sentinel.read_bytes(), child_bytes)
        self.assertTrue(child_maintenance._maintenance_directory.is_dir())
        self.assertTrue(self.backup_directory(archived["backup_id"]).is_dir())

    def test_classify_committed_archive_is_read_only_and_needs_no_state_marker(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive(
            "recover-archive", before["revision"], self.custody,
        )
        filesystem_before = self.filesystem_snapshot()

        with (
            mock.patch.object(
                self.maintenance, "_bootstrap_journal",
                side_effect=AssertionError("recovery must not bootstrap"),
            ),
            mock.patch.object(
                self.maintenance, "_write_journal",
                side_effect=AssertionError("recovery must not write the journal"),
            ),
            mock.patch.object(
                maintenance_module, "_rename_no_replace",
                side_effect=AssertionError("recovery classification must not rename"),
            ),
        ):
            classified = self.maintenance.classify_recovery(
                "recover-archive", "reset", before["revision"], None, self.custody,
            )

        self.assertEqual(classified, {
            "outcome": "completed",
            "result": archived,
            "error": None,
            "requires_state_marker": False,
        })
        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_classify_recovery_never_bootstraps_and_rejects_foreign_or_superseded_proof(self):
        with self.placement.transaction():
            pass
        missing_before = self.filesystem_snapshot()
        with self.assertRaises(QuarantineError):
            self.maintenance.classify_recovery(
                "missing-operation", "reset", str(uuid.uuid4()), None, self.custody,
            )
        self.assertEqual(self.filesystem_snapshot(), missing_before)

        partial = self.maintenance._maintenance_directory
        partial.mkdir(mode=0o700)
        (partial / "operator-data").write_bytes(b"preserve")
        partial_before = self.filesystem_snapshot()
        with self.assertRaises(QuarantineError):
            self.maintenance.classify_recovery(
                "partial-operation", "reset", str(uuid.uuid4()), None, self.custody,
            )
        self.assertEqual(self.filesystem_snapshot(), partial_before)

        self.new_case()
        self.create_complete_state()
        initial = self.maintenance.inspect()
        archived = self.maintenance.archive(
            "old-reset", initial["revision"], self.custody,
        )
        foreign_before = self.filesystem_snapshot()
        with self.assertRaises(QuarantineError):
            self.maintenance.classify_recovery(
                "old-reset", "reset", initial["revision"], None,
                {"pool_id": "pool-foreign", "lease_id": "lease-a"},
            )
        self.assertEqual(self.filesystem_snapshot(), foreign_before)

        self.maintenance.retire(
            "new-retire", archived["backup_id"], archived["revision"], self.custody,
        )
        superseded_before = self.filesystem_snapshot()
        with self.assertRaises(QuarantineError):
            self.maintenance.classify_recovery(
                "old-reset", "reset", initial["revision"], None, self.custody,
            )
        self.assertEqual(self.filesystem_snapshot(), superseded_before)

    def test_placement_claims_survive_archive_restore_and_retirement(self):
        self.create_complete_state()
        self.maintenance.inspect()
        claims = sorted(self.root.glob(f"{maintenance_module.PLACEMENT_CLAIM_PREFIX}*"))
        claim_bytes = {path: path.read_bytes() for path in claims}
        before = self.maintenance.inspect()

        archived = self.maintenance.archive("claim-archive", before["revision"], self.custody)
        self.assertEqual({path: path.read_bytes() for path in claims}, claim_bytes)
        restored = self.maintenance.restore(
            "claim-restore", archived["backup_id"], archived["revision"], self.custody,
        )
        self.assertEqual({path: path.read_bytes() for path in claims}, claim_bytes)
        archived_again = self.maintenance.archive(
            "claim-archive-again", restored["revision"], self.custody,
        )
        self.maintenance.retire(
            "claim-retire", archived_again["backup_id"],
            archived_again["revision"], self.custody,
        )
        self.assertEqual({path: path.read_bytes() for path in claims}, claim_bytes)

    def test_classify_recovery_rejects_malformed_journal_without_rewriting_it(self):
        self.maintenance.inspect()
        journal_path = self.maintenance._maintenance_directory / maintenance_module.JOURNAL_NAME
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["state"] = []
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        filesystem_before = self.filesystem_snapshot()

        with self.assertRaises(QuarantineError):
            self.maintenance.classify_recovery(
                "malformed-operation", "reset", str(uuid.uuid4()), None, self.custody,
            )

        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_classify_archive_intent_after_rename_is_read_only_and_requires_marker(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        real_sync = maintenance_module._sync_directory
        failed = False

        def fail_source_sync(path: Path):
            nonlocal failed
            if path == self.state_directory.parent and not failed:
                failed = True
                raise OSError(errno.EIO, "injected archive recovery boundary")
            return real_sync(path)

        with mock.patch.object(
            maintenance_module, "_sync_directory", side_effect=fail_source_sync,
        ):
            with self.assertRaises(QuarantineError):
                self.maintenance.archive(
                    "recover-intent", before["revision"], self.custody,
                )
        self.assertTrue(failed)
        journal = json.loads(
            (self.maintenance._maintenance_directory / maintenance_module.JOURNAL_NAME)
            .read_text(encoding="utf-8")
        )
        self.assertEqual(journal["operation"]["phase"], "ARCHIVE_INTENT")
        filesystem_before = self.filesystem_snapshot()

        with (
            mock.patch.object(
                self.maintenance, "_write_journal",
                side_effect=AssertionError("recovery must not write the journal"),
            ),
            mock.patch.object(
                maintenance_module, "_rename_no_replace",
                side_effect=AssertionError("recovery must not rename"),
            ),
        ):
            classified = self.maintenance.classify_recovery(
                "recover-intent", "reset", before["revision"], None, self.custody,
            )

        self.assertEqual(classified, {
            "outcome": "resume",
            "result": None,
            "error": None,
            "requires_state_marker": True,
        })
        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_classify_archive_intent_before_rename_requires_marker(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        with mock.patch.object(
            maintenance_module,
            "_rename_no_replace",
            side_effect=SystemExit("simulated interruption before rename"),
        ):
            with self.assertRaises(SystemExit):
                self.maintenance.archive(
                    "recover-before-rename", before["revision"], self.custody,
                )
        self.assertTrue(self.state_directory.is_dir())
        self.assertEqual(
            list(self.maintenance._maintenance_directory.glob("backup-*")), [],
        )
        filesystem_before = self.filesystem_snapshot()

        classified = self.maintenance.classify_recovery(
            "recover-before-rename", "reset", before["revision"], None, self.custody,
        )

        self.assertEqual(classified["outcome"], "resume")
        self.assertTrue(classified["requires_state_marker"])
        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_classify_archive_no_mutation_as_failed_terminal_evidence(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        with mock.patch.object(
            maintenance_module,
            "_rename_no_replace",
            side_effect=OSError(errno.EIO, "injected no-mutation failure"),
        ):
            with self.assertRaises(LeaseError):
                self.maintenance.archive(
                    "recover-no-mutation", before["revision"], self.custody,
                )
        filesystem_before = self.filesystem_snapshot()

        classified = self.maintenance.classify_recovery(
            "recover-no-mutation", "reset", before["revision"], None, self.custody,
        )

        self.assertEqual(classified["outcome"], "failed")
        self.assertIsNone(classified["result"])
        self.assertIsNotNone(classified["error"])
        self.assertFalse(classified["requires_state_marker"])
        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_classify_empty_archive_returns_original_noop_result(self):
        empty = self.maintenance.inspect()
        result = self.maintenance.archive(
            "recover-empty", empty["revision"], self.custody,
        )
        filesystem_before = self.filesystem_snapshot()

        classified = self.maintenance.classify_recovery(
            "recover-empty", "reset", empty["revision"], None, self.custody,
        )

        self.assertEqual(classified["outcome"], "completed")
        self.assertEqual(classified["result"], result)
        self.assertFalse(classified["requires_state_marker"])
        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_classify_restore_intent_after_rename_requires_marker(self):
        self.create_complete_state()
        initial = self.maintenance.inspect()
        archived = self.maintenance.archive(
            "reset-for-restore", initial["revision"], self.custody,
        )
        real_sync = maintenance_module._sync_directory
        failed = False

        def fail_destination_sync(path: Path):
            nonlocal failed
            if path == self.state_directory.parent and not failed:
                failed = True
                raise OSError(errno.EIO, "injected restore recovery boundary")
            return real_sync(path)

        with mock.patch.object(
            maintenance_module, "_sync_directory", side_effect=fail_destination_sync,
        ):
            with self.assertRaises(QuarantineError):
                self.maintenance.restore(
                    "recover-restore", archived["backup_id"], archived["revision"],
                    self.custody,
                )
        self.assertTrue(failed)
        self.assertFalse(self.backup_directory(archived["backup_id"]).exists())
        filesystem_before = self.filesystem_snapshot()

        classified = self.maintenance.classify_recovery(
            "recover-restore", "restore", archived["revision"], archived["backup_id"],
            self.custody,
        )

        self.assertEqual(classified["outcome"], "resume")
        self.assertTrue(classified["requires_state_marker"])
        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_classify_restore_no_mutation_as_failed_terminal_evidence(self):
        self.create_complete_state()
        initial = self.maintenance.inspect()
        archived = self.maintenance.archive(
            "reset-for-restore", initial["revision"], self.custody,
        )
        with mock.patch.object(
            maintenance_module,
            "_rename_no_replace",
            side_effect=OSError(errno.EIO, "injected no-mutation failure"),
        ):
            with self.assertRaises(LeaseError):
                self.maintenance.restore(
                    "restore-no-mutation", archived["backup_id"], archived["revision"],
                    self.custody,
                )
        filesystem_before = self.filesystem_snapshot()

        classified = self.maintenance.classify_recovery(
            "restore-no-mutation", "restore", archived["revision"],
            archived["backup_id"], self.custody,
        )

        self.assertEqual(classified["outcome"], "failed")
        self.assertIsNone(classified["result"])
        self.assertFalse(classified["requires_state_marker"])
        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_classify_published_archive_and_restore_or_retire_commits(self):
        self.create_complete_state()
        initial = self.maintenance.inspect()
        archived = self.maintenance.archive(
            "published-reset", initial["revision"], self.custody,
        )
        self.create_complete_state()
        self.maintenance.check_start()
        published = self.maintenance.classify_recovery(
            "published-reset", "reset", initial["revision"], None, self.custody,
        )
        self.assertEqual(published["outcome"], "completed")
        self.assertEqual(published["result"], archived)
        self.assertFalse(published["requires_state_marker"])

        for action in ("restore", "retire"):
            with self.subTest(action=action):
                self.new_case()
                self.create_complete_state()
                current = self.maintenance.inspect()
                archived = self.maintenance.archive(
                    f"archive-{action}", current["revision"], self.custody,
                )
                operation_id = f"resolve-{action}"
                if action == "restore":
                    result = self.maintenance.restore(
                        operation_id, archived["backup_id"], archived["revision"],
                        self.custody,
                    )
                else:
                    result = self.maintenance.retire(
                        operation_id, archived["backup_id"], archived["revision"],
                        self.custody,
                    )
                filesystem_before = self.filesystem_snapshot()
                classified = self.maintenance.classify_recovery(
                    operation_id, action, archived["revision"],
                    archived["backup_id"], self.custody,
                )
                self.assertEqual(classified["outcome"], "completed")
                self.assertEqual(classified["result"], result)
                self.assertFalse(classified["requires_state_marker"])
                self.assertEqual(self.filesystem_snapshot(), filesystem_before)

    def test_archive_is_bound_to_revision_operation_and_custody(self):
        self.create_complete_state()
        before = self.maintenance.inspect()

        with self.assertRaisesRegex(LeaseError, "revision"):
            self.maintenance.archive("reset-1", "stale-revision", self.custody)
        archived = self.maintenance.archive(
            "reset-1", before["revision"], self.custody,
        )
        with self.assertRaisesRegex(LeaseError, "custody"):
            self.maintenance.archive(
                "reset-1", before["revision"], {"pool_id": "pool-b", "lease_id": "lease-a"},
            )
        with self.assertRaisesRegex(LeaseError, "backup"):
            self.maintenance.archive(
                "reset-2", archived["revision"], self.custody,
            )
        self.assertTrue(self.backup_directory(archived["backup_id"]).is_dir())

    def test_empty_archive_is_a_stable_noop(self):
        empty = self.maintenance.inspect()
        first = self.maintenance.archive("reset-empty", empty["revision"], self.custody)
        second = self.maintenance.archive("reset-empty", empty["revision"], self.custody)

        self.assertEqual(first, second)
        self.assertEqual(first["state"], "empty")
        self.assertIsNone(first["backup_id"])
        self.assertEqual(first["outcome"], "already_empty")
        self.assertEqual(first["revision"], empty["revision"])
        self.assertEqual(list(self.root.glob("backup-*")), [])

    def test_archive_rejects_destination_collision_without_removing_either_tree(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        backup_id = str(uuid.uuid4())
        collision = self.backup_directory(backup_id)
        collision.mkdir(mode=0o700)
        sentinel = collision / "operator-data"
        sentinel.write_bytes(b"untouched")
        generated = uuid.UUID(backup_id)

        with mock.patch.object(maintenance_module.uuid, "uuid4", return_value=generated):
            with self.assertRaises(QuarantineError):
                self.maintenance.archive("reset-collision", before["revision"], self.custody)

        self.assertTrue(self.state_directory.is_dir())
        self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_archive_rename_exdev_and_enospc_are_settled_without_mutation(self):
        for error_number in (errno.EXDEV, errno.ENOSPC, errno.EACCES):
            with self.subTest(error=error_number):
                self.new_case()
                self.create_complete_state()
                before = self.maintenance.inspect()
                with mock.patch.object(
                    maintenance_module,
                    "_rename_no_replace",
                    side_effect=OSError(error_number, os.strerror(error_number)),
                ):
                    with self.assertRaises(LeaseError):
                        self.maintenance.archive(
                            f"reset-{error_number}", before["revision"], self.custody,
                        )
                self.assertTrue(self.state_directory.is_dir())
                self.assertEqual(
                    list(self.maintenance_directory().glob("backup-*")), [],
                )

    def test_archive_parent_fsync_failure_quarantines_and_exact_retry_settles(self):
        for failed_name in ("source", "destination"):
            with self.subTest(parent=failed_name):
                if failed_name == "destination":
                    self.new_case()
                self.create_complete_state()
                before = self.maintenance.inspect()
                failed_parent = (
                    self.state_directory.parent if failed_name == "source"
                    else self.maintenance._maintenance_directory
                )
                real_sync = maintenance_module._sync_directory
                target_calls = 0
                failure_call = 1 if failed_name == "source" else 2
                failed = False

                def fail_after_rename(path: Path):
                    nonlocal failed, target_calls
                    if path == failed_parent:
                        target_calls += 1
                    if path == failed_parent and target_calls == failure_call:
                        failed = True
                        raise OSError(errno.EIO, "injected parent fsync failure")
                    return real_sync(path)

                with mock.patch.object(
                    maintenance_module, "_sync_directory", side_effect=fail_after_rename,
                ):
                    with self.assertRaises(QuarantineError):
                        self.maintenance.archive(
                            "reset-fsync", before["revision"], self.custody,
                        )

                self.assertTrue(failed)
                with self.assertRaises(QuarantineError):
                    self.maintenance.check_start()
                self.assertFalse(self.state_directory.exists())
                backup_matches = list(self.maintenance_directory().glob("backup-*"))
                self.assertEqual(len(backup_matches), 1)

                settled = self.maintenance.archive(
                    "reset-fsync", before["revision"], self.custody,
                )
                self.assertEqual(settled["state"], "empty")
                self.assertEqual(settled["backup_id"], backup_matches[0].name.removeprefix("backup-"))

    def test_archive_detects_symlink_swap_and_destination_collision_races(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        original_directory = self.root / "raced-original"
        foreign = self.root / "foreign"
        foreign.mkdir()
        foreign_marker = foreign / "operator-data"
        foreign_marker.write_bytes(b"keep")
        real_rename = maintenance_module._rename_no_replace
        raced = False

        def swap_for_symlink(source_fd, source_name, destination_fd, destination_name):
            nonlocal raced
            if source_name == self.state_directory.name and not raced:
                raced = True
                real_rename(
                    source_fd,
                    source_name,
                    source_fd,
                    original_directory.name,
                )
                os.symlink(foreign, source_name, dir_fd=source_fd)
            return real_rename(
                source_fd, source_name, destination_fd, destination_name,
            )

        with mock.patch.object(
            maintenance_module, "_rename_no_replace", side_effect=swap_for_symlink,
        ):
            with self.assertRaises(QuarantineError):
                self.maintenance.archive("reset-symlink-race", before["revision"], self.custody)
        self.assertTrue(raced)
        self.assertEqual(foreign_marker.read_bytes(), b"keep")
        self.assertTrue((original_directory / ".resource-lease-android-state.json").is_file())
        raced_backup = next(self.maintenance_directory().glob("backup-*"))
        self.assertTrue(raced_backup.is_symlink())

        self.new_case()
        self.create_complete_state()
        before = self.maintenance.inspect()
        real_rename = maintenance_module._rename_no_replace
        collision_created = False

        def create_destination_race(source_fd, source_name, destination_fd, destination_name):
            nonlocal collision_created
            if source_name == self.state_directory.name and not collision_created:
                collision_created = True
                os.mkdir(destination_name, mode=0o700, dir_fd=destination_fd)
                created_fd = os.open(
                    destination_name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    dir_fd=destination_fd,
                )
                self.addCleanup(os.close, created_fd)
                descriptor = os.open(
                    "operator-data",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=created_fd,
                )
                try:
                    os.write(descriptor, b"keep")
                finally:
                    os.close(descriptor)
            return real_rename(
                source_fd, source_name, destination_fd, destination_name,
            )

        with mock.patch.object(
            maintenance_module, "_rename_no_replace", side_effect=create_destination_race,
        ):
            with self.assertRaises(QuarantineError):
                self.maintenance.archive("reset-destination-race", before["revision"], self.custody)
        self.assertTrue(collision_created)
        self.assertTrue(
            self.state_directory.is_dir(),
            f"active path changed; parent={list(self.state_directory.parent.iterdir())!r}",
        )
        collision = next(self.maintenance_directory().glob("backup-*"))
        self.assertEqual((collision / "operator-data").read_bytes(), b"keep")

    def test_profile_mismatch_preserves_journal_and_active_state(self):
        self.create_complete_state()
        self.maintenance.inspect()
        journal_path = self.maintenance_directory() / maintenance_module.JOURNAL_NAME
        journal_before = journal_path.read_bytes()
        state_before = sorted(
            (path.relative_to(self.state_directory).as_posix(), path.read_bytes())
            for path in self.state_directory.rglob("*")
            if path.is_file()
        )
        incompatible = AndroidMaintenance(
            self.state_directory,
            system_image=SYSTEM_IMAGE,
            image_identity=PROFILE["image_identity"],
            memory_mb=3072,
        )

        with self.assertRaisesRegex(LeaseError, "profile differs"):
            incompatible.check_start()

        state_after = sorted(
            (path.relative_to(self.state_directory).as_posix(), path.read_bytes())
            for path in self.state_directory.rglob("*")
            if path.is_file()
        )
        self.assertEqual(state_after, state_before)
        self.assertEqual(journal_path.read_bytes(), journal_before)

    def test_tree_and_journal_bounds_reject_without_moving_state(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        with mock.patch.object(maintenance_module, "MAX_TREE_ENTRIES", 2):
            with self.assertRaisesRegex(LeaseError, "entry bound"):
                self.maintenance.archive("reset-tree-bound", before["revision"], self.custody)
        self.assertTrue(self.state_directory.is_dir())
        self.assertEqual(list(self.maintenance_directory().glob("backup-*")), [])

        journal_path = self.maintenance_directory() / maintenance_module.JOURNAL_NAME
        limit = len(journal_path.read_bytes())
        with mock.patch.object(maintenance_module, "MAX_JOURNAL_BYTES", limit):
            with self.assertRaisesRegex(LeaseError, "size bound"):
                self.maintenance.archive(
                    "reset-journal-bound", before["revision"], self.custody,
                )
        self.assertTrue(self.state_directory.is_dir())
        self.assertEqual(list(self.maintenance_directory().glob("backup-*")), [])

    def test_archive_refuses_symlink_hardlink_and_special_entries_before_mutation(self):
        cases = ("symlink", "hardlink", "special")
        for case in cases:
            with self.subTest(case=case):
                self.new_case()
                state = self.create_complete_state()
                before = self.maintenance.inspect()
                payload = state.avd_path / "user-data"
                if case == "symlink":
                    payload.symlink_to(self.root / "outside")
                elif case == "hardlink":
                    payload.write_bytes(b"shared")
                    os.link(payload, self.root / "outside-hardlink")
                else:
                    if not hasattr(os, "mkfifo"):
                        self.skipTest("FIFO creation is unavailable")
                    os.mkfifo(payload)
                with self.assertRaises(LeaseError):
                    self.maintenance.archive(
                        f"reset-{case}", before["revision"], self.custody,
                    )

                self.assertTrue(self.state_directory.is_dir())
                self.assertEqual(
                    list(self.maintenance_directory().glob("backup-*")), [],
                )

    def test_check_start_blocks_partial_maintenance_directory(self):
        directory = self.maintenance._maintenance_directory
        directory.mkdir(mode=0o700)
        (directory / "operator-data").write_text("partial", encoding="utf-8")

        with self.assertRaises(QuarantineError):
            self.maintenance.check_start()

    def test_check_start_quarantines_journal_with_unhashable_state(self):
        self.maintenance.inspect()
        journal_path = self.maintenance._maintenance_directory / maintenance_module.JOURNAL_NAME
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["state"] = []
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        corrupt_bytes = journal_path.read_bytes()

        with self.assertRaisesRegex(QuarantineError, "journal is incompatible"):
            self.maintenance.check_start()

        self.assertEqual(journal_path.read_bytes(), corrupt_bytes)

    def test_restore_requires_absent_original_and_preserves_replacement(self):
        state = self.create_complete_state()
        locator = state.avd_home / f"{state.avd_name}.ini"
        original_locator = locator.read_bytes()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive("reset-restore", before["revision"], self.custody)
        backup = self.backup_directory(archived["backup_id"])

        self.state_directory.mkdir(mode=0o700)
        sentinel = self.state_directory / "replacement"
        sentinel.write_bytes(b"replacement")
        with self.assertRaisesRegex(LeaseError, "already exists"):
            self.maintenance.restore(
                "restore-1", archived["backup_id"], archived["revision"], self.custody,
            )
        self.assertEqual(sentinel.read_bytes(), b"replacement")
        self.assertTrue(backup.is_dir())

        sentinel.unlink()
        self.state_directory.rmdir()
        restored = self.maintenance.restore(
            "restore-1", archived["backup_id"], archived["revision"], self.custody,
        )
        self.assertEqual(restored["state"], "complete")
        self.assertIsNone(restored["backup_id"])
        self.assertEqual(locator.read_bytes(), original_locator)
        self.assertFalse(backup.exists())
        self.assertIsNone(self.maintenance.check_start())

    def test_restore_rejects_wrong_backup_and_is_idempotent(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive("reset-restore", before["revision"], self.custody)

        with self.assertRaisesRegex(LeaseError, "backup"):
            self.maintenance.restore(
                "restore-1", str(uuid.uuid4()), archived["revision"], self.custody,
            )
        restored = self.maintenance.restore(
            "restore-1", archived["backup_id"], archived["revision"], self.custody,
        )
        retried = self.maintenance.restore(
            "restore-1", archived["backup_id"], archived["revision"], self.custody,
        )
        self.assertEqual(retried, restored)

    def test_restore_rename_failure_is_retryable_only_for_exact_operation(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive("reset-archive", before["revision"], self.custody)
        real_rename = maintenance_module._rename_no_replace
        failed = False

        def fail_once(source_fd, source_name, destination_fd, destination_name):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError(errno.EIO, "injected rename failure")
            return real_rename(source_fd, source_name, destination_fd, destination_name)

        with mock.patch.object(maintenance_module, "_rename_no_replace", side_effect=fail_once):
            with self.assertRaises(LeaseError):
                self.maintenance.restore(
                    "restore-exact", archived["backup_id"], archived["revision"], self.custody,
                )

        self.assertTrue(self.backup_directory(archived["backup_id"]).is_dir())
        self.assertIsNone(self.maintenance.check_start())
        with self.assertRaisesRegex(LeaseError, "no mutation"):
            self.maintenance.restore(
                "restore-exact", archived["backup_id"], archived["revision"], self.custody,
            )
        restored = self.maintenance.restore(
            "restore-retry", archived["backup_id"], archived["revision"], self.custody,
        )
        self.assertEqual(restored["state"], "complete")

    def test_restore_parent_fsync_failure_blocks_start_until_exact_retry(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive("reset-archive", before["revision"], self.custody)
        real_sync = maintenance_module._sync_directory
        failed = False

        def fail_destination_parent(path: Path):
            nonlocal failed
            if path == self.state_directory.parent and not failed:
                failed = True
                raise OSError(errno.EIO, "injected restore parent fsync failure")
            return real_sync(path)

        with mock.patch.object(
            maintenance_module, "_sync_directory", side_effect=fail_destination_parent,
        ):
            with self.assertRaises(QuarantineError):
                self.maintenance.restore(
                    "restore-fsync", archived["backup_id"], archived["revision"], self.custody,
                )

        self.assertTrue(failed)
        with self.assertRaises(QuarantineError):
            self.maintenance.check_start()
        restored = self.maintenance.restore(
            "restore-fsync", archived["backup_id"], archived["revision"], self.custody,
        )
        self.assertEqual(restored["state"], "complete")

    def test_retire_requires_exact_backup_and_retains_lineage(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive("reset-archive", before["revision"], self.custody)

        with self.assertRaisesRegex(LeaseError, "backup"):
            self.maintenance.retire(
                "retire-wrong", str(uuid.uuid4()), archived["revision"], self.custody,
            )
        retired = self.maintenance.retire(
            "retire-1", archived["backup_id"], archived["revision"], self.custody,
        )

        self.assertEqual(retired["state"], "empty")
        self.assertIsNone(retired["backup_id"])
        self.assertNotEqual(retired["revision"], archived["revision"])
        self.assertFalse(self.backup_directory(archived["backup_id"]).exists())
        self.assertEqual(self.maintenance.inspect(), retired)

    def test_retire_releases_namespace_gate_during_backup_deletion(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive("retire-gate-archive", before["revision"], self.custody)
        competing_placement = AndroidPlacement(self.placement.guard_root, lock_timeout=0.2)
        real_delete = self.maintenance._delete_backup_tree
        observed = []

        def verify_gate_released(path, expected_inode):
            with competing_placement.transaction():
                journal = self.maintenance._read_journal()
                operation = journal["operation"]
                observed.append(operation["phase"])
                self.assertEqual(operation["phase"], "RETIRE_INTENT")
            return real_delete(path, expected_inode)

        with mock.patch.object(
            self.maintenance, "_delete_backup_tree", side_effect=verify_gate_released,
        ):
            retired = self.maintenance.retire(
                "retire-gate", archived["backup_id"], archived["revision"], self.custody,
            )

        self.assertEqual(observed, ["RETIRE_INTENT"])
        self.assertIsNone(retired["backup_id"])

    def test_retire_blocks_case_alias_placement_after_backup_manifest_removal(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive(
            "retire-alias-race-archive", before["revision"], self.custody,
        )
        backup = self.backup_directory(archived["backup_id"])
        backup_info = backup.stat()
        alias_maintenance = backup.parent.parent / (
            maintenance_module.MAINTENANCE_DIRECTORY_PREFIX.upper() + self.maintenance.identity
        )
        if not alias_maintenance.is_dir():
            self.skipTest("temporary filesystem is case-sensitive")

        nested_directory = (
            alias_maintenance / backup.name / "android-home" / "avd" / "raced-state"
        )
        real_listdir = maintenance_module.os.listdir
        real_unlink = maintenance_module.os.unlink
        attempts = []

        def manifest_first(descriptor):
            names = real_listdir(descriptor)
            try:
                info = os.fstat(descriptor)
            except TypeError:
                return names
            if (info.st_dev, info.st_ino) == (backup_info.st_dev, backup_info.st_ino):
                names.sort(key=lambda item: item != maintenance_module.MANIFEST_NAME)
            return names

        def place_after_manifest_unlink(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if path == maintenance_module.MANIFEST_NAME:
                try:
                    nested = PersistentAndroidState.prepare(
                        nested_directory,
                        system_image=SYSTEM_IMAGE,
                        image_identity=PROFILE["image_identity"],
                        memory_mb=PROFILE["memory_mb"],
                        placement=self.placement,
                    )
                except LeaseError:
                    attempts.append("blocked")
                else:
                    (nested.directory / "operator-data").write_bytes(b"preserve")
                    attempts.append("created")
            return result

        with (
            mock.patch.object(maintenance_module.os, "listdir", side_effect=manifest_first),
            mock.patch.object(
                maintenance_module.os, "unlink", side_effect=place_after_manifest_unlink,
            ),
        ):
            retired = self.maintenance.retire(
                "retire-alias-race", archived["backup_id"],
                archived["revision"], self.custody,
            )

        self.assertEqual(attempts, ["blocked"])
        self.assertIsNone(retired["backup_id"])
        self.assertFalse(backup.exists())

    def test_retire_preserves_intent_when_final_journal_differs(self):
        self.create_complete_state()
        before = self.maintenance.inspect()
        archived = self.maintenance.archive(
            "retire-revalidation-archive", before["revision"], self.custody,
        )
        backup = self.backup_directory(archived["backup_id"])
        real_delete = self.maintenance._delete_backup_tree
        real_read_journal = self.maintenance._read_journal
        deleted = False

        def delete_then_mark_complete(path, expected_inode):
            nonlocal deleted
            real_delete(path, expected_inode)
            deleted = True

        def changed_journal():
            current = real_read_journal()
            if deleted:
                changed = dict(current)
                changed["backup"] = None
                return changed
            return current

        with (
            mock.patch.object(
                self.maintenance, "_delete_backup_tree", side_effect=delete_then_mark_complete,
            ),
            mock.patch.object(
                self.maintenance, "_read_journal", side_effect=changed_journal,
            ),
            self.assertRaises(QuarantineError),
        ):
            self.maintenance.retire(
                "retire-revalidation", archived["backup_id"], archived["revision"],
                self.custody,
            )

        self.assertFalse(backup.exists())
        journal = real_read_journal()
        self.assertEqual(journal["operation"]["phase"], "RETIRE_INTENT")
        self.assertEqual(journal["backup"]["id"], archived["backup_id"])

    def test_interrupted_retirement_is_unrestorable_and_exact_retry_completes(self):
        state = self.create_complete_state()
        (state.avd_path / "user-data").write_bytes(b"retire")
        before = self.maintenance.inspect()
        archived = self.maintenance.archive("reset-archive", before["revision"], self.custody)
        backup = self.backup_directory(archived["backup_id"])
        entry_count = len(list(backup.rglob("*")))
        real_unlink = maintenance_module.os.unlink
        removed = 0

        def fail_after_one_unlink(*args, **kwargs):
            nonlocal removed
            if removed == 1:
                raise OSError(errno.EIO, "injected partial delete failure")
            removed += 1
            return real_unlink(*args, **kwargs)

        with mock.patch.object(maintenance_module.os, "unlink", side_effect=fail_after_one_unlink):
            with self.assertRaises(QuarantineError):
                self.maintenance.retire(
                    "retire-exact", archived["backup_id"], archived["revision"], self.custody,
                )
        self.assertEqual(removed, 1)
        self.assertLess(len(list(backup.rglob("*"))), entry_count)
        filesystem_before = self.filesystem_snapshot()
        classified = self.maintenance.classify_recovery(
            "retire-exact", "retire", archived["revision"], archived["backup_id"],
            self.custody,
        )
        self.assertEqual(classified["outcome"], "resume")
        self.assertTrue(classified["requires_state_marker"])
        self.assertEqual(self.filesystem_snapshot(), filesystem_before)

        with self.assertRaisesRegex(LeaseError, "retiring"):
            self.maintenance.restore(
                "restore-blocked", archived["backup_id"], archived["revision"], self.custody,
            )
        with self.assertRaises(QuarantineError):
            self.maintenance.check_start()
        retired = self.maintenance.retire(
            "retire-exact", archived["backup_id"], archived["revision"], self.custody,
        )
        self.assertIsNone(retired["backup_id"])
        self.assertFalse(self.backup_directory(archived["backup_id"]).exists())

    def test_publish_complete_advances_lineage_and_archive_result_remains_idempotent(self):
        empty = self.maintenance.inspect()
        archived = self.maintenance.archive("reset-start", empty["revision"], self.custody)
        self.maintenance.check_start()
        state = self.create_complete_state()

        complete = self.maintenance.publish_complete()

        self.assertEqual(complete["state"], "complete")
        self.assertEqual(complete["backup_id"], archived["backup_id"])
        self.assertNotEqual(complete["revision"], archived["revision"])
        archive_retry = self.maintenance.archive(
            "reset-start", empty["revision"], self.custody,
        )
        self.assertEqual(archive_retry, archived)
        self.assertTrue(state.avd_path.is_dir())


if __name__ == "__main__":
    unittest.main()
