from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from adhd.immutable_store import ImmutableStore
from adhd.memory import namespace


class ImmutableStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.run = "a" * 32
        self.key = "b" * 24
        self.native = self.base / "native"
        state = self.native / self.key
        state.mkdir(parents=True)
        self.state = state / "state.json"
        self.state.write_text(json.dumps({"schema_version": 2, "key": self.key,
            "run_id": self.run, "workspace": str(self.workspace), "status": "complete",
            "children": {}, "pending_turn_ids": []}), encoding="utf-8")
        output = self.workspace / ".adhd" / self.run
        output.mkdir(parents=True)
        self.policy_path = self.workspace / ".adhd" / "immutable-policy.json"
        self.policy = {"schema_version": 1, "policy_revision": "policy-1",
                       "allowed_privacy": ["private"],
                       "allowed_actions": ["capture", "read", "export", "restore", "forget"]}
        self.policy_path.write_text(json.dumps(self.policy), encoding="utf-8")
        self.source = output / "result.log"
        self.source.write_bytes("한글 completed result\n".encode())
        self.metadata = {"workspace": str(self.workspace), "scope": namespace(self.workspace),
            "owner_key": self.key, "owner_run": self.run, "kind": "log", "privacy": "private",
            "policy_revision": "policy-1", "provenance": {"kind": "check", "receipt": "test"},
            "retention": "normal"}
        self.access = {"workspace": self.workspace, "allowed_privacy": ["private"],
                       "policy_revision": "policy-1"}
        self.store = ImmutableStore(self.base / "store", native_root=self.native)
        self.addCleanup(self.store.close)

    def test_requests_cannot_grant_privacy_or_bypass_current_policy(self):
        with self.assertRaises(PermissionError):
            self.store.put(self.source, {**self.metadata, "privacy": "sensitive"})
        ref = self.store.put(self.source, self.metadata)["ref_id"]
        with self.assertRaises(PermissionError):
            self.store.read(ref, {**self.access, "allowed_privacy": ["private", "sensitive"]})
        self.policy_path.write_text(json.dumps({**self.policy, "policy_revision": "policy-2"}),
                                    encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.store.read(ref, self.access)

    def test_capture_and_compression_budget_exhaustion_preserve_originals(self):
        from adhd.maintenance_budget import BudgetExhausted, MaintenanceBudget
        original = self.source.read_bytes()
        with self.assertRaises(BudgetExhausted):
            self.store.put(self.source, self.metadata, budget=MaintenanceBudget(read_bytes=1))
        self.assertEqual(self.store.audit()["refs"], 0)
        self.assertEqual(self.store.audit()["objects"], 0)
        self.assertEqual(list((self.base / "store" / "tmp").iterdir()), [])
        self.assertEqual(self.source.read_bytes(), original)
        ref = self.store.put(self.source, self.metadata)["ref_id"]
        destination = self.base / "bounded.zip"
        with self.assertRaises(BudgetExhausted):
            self.store.export(ref, self.access, destination,
                              budget=MaintenanceBudget(read_bytes=len(original) + 1))
        self.assertFalse(destination.exists())
        self.assertEqual(self.source.read_bytes(), original)

    def test_restore_budget_exhaustion_leaves_destination_absent(self):
        from adhd.maintenance_budget import BudgetExhausted, MaintenanceBudget
        ref = self.store.put(self.source, self.metadata)["ref_id"]
        archive = self.base / "portable.zip"
        self.store.export(ref, self.access, archive)
        destination = self.base / "restored" / self.source.name
        destination.parent.mkdir()
        with self.assertRaises(BudgetExhausted):
            ImmutableStore.restore(archive, destination, self.access,
                                   budget=MaintenanceBudget(write_bytes=1))
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".*.tmp")), [])
        self.policy_path.unlink()
        with self.assertRaises(PermissionError):
            self.store.read(ref, self.access)

    def test_capture_can_be_disabled_while_existing_read_and_restore_work(self):
        ref = self.store.put(self.source, self.metadata)["ref_id"]
        exported = self.base / "portable.zip"
        self.store.export(ref, self.access, exported)
        self.policy_path.write_text(json.dumps({**self.policy,
            "allowed_actions": ["read", "restore"]}), encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.store.put(self.source, self.metadata)
        self.assertEqual(self.store.read(ref, self.access), self.source.read_bytes())
        for action in (lambda: self.store.export(ref, self.access, self.base / "denied.zip"),
                       lambda: self.store.forget(ref, self.access)):
            with self.assertRaises(PermissionError):
                action()
        restored = self.base / "restored" / self.source.name
        restored.parent.mkdir()
        ImmutableStore.restore(exported, restored, self.access)
        self.assertEqual(restored.read_bytes(), self.source.read_bytes())
        self.policy_path.write_text(json.dumps({**self.policy, "allowed_actions": ["read"]}),
                                    encoding="utf-8")
        restored.unlink()
        with self.assertRaises(PermissionError):
            ImmutableStore.restore(exported, restored, self.access)
        self.assertFalse(restored.exists())

    def test_policy_rechecked_before_read_returns_bytes(self):
        from adhd.immutable_store import _hash_file
        ref = self.store.put(self.source, self.metadata)["ref_id"]

        def revoke_after_hash(path, budget=None):
            result = _hash_file(path, budget)
            self.policy_path.write_text(json.dumps({**self.policy, "allowed_actions": []}),
                                        encoding="utf-8")
            return result

        with patch("adhd.immutable_store._hash_file", side_effect=revoke_after_hash):
            with self.assertRaises(PermissionError):
                self.store.read(ref, self.access)

    def test_db_registration_failure_rolls_back_own_blob_and_retry_is_classified(self):
        self.store.register_root(self.workspace, self.key, self.run)
        self.store.db.execute("CREATE TRIGGER reject_ref BEFORE INSERT ON refs "
                              "BEGIN SELECT RAISE(ABORT, 'simulated disk full'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.put(self.source, self.metadata)
        self.assertEqual(self.store.audit()["objects"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM gc_births").fetchone()[0], 0)
        self.assertEqual(self.source.read_bytes(), "한글 completed result\n".encode())
        self.store.db.execute("DROP TRIGGER reject_ref")
        ref = self.store.put(self.source, self.metadata)
        self.assertEqual(self.store.db.execute("SELECT managed FROM gc_births WHERE digest=?",
                                              (ref["hash"],)).fetchone()[0], 1)
        self.assertEqual(self.store.audit()["orphan_count"], 0)

    def test_ambiguous_cleanup_failure_cannot_be_silently_adopted_on_retry(self):
        self.store.register_root(self.workspace, self.key, self.run)
        self.store.db.execute("CREATE TRIGGER reject_ref BEFORE INSERT ON refs "
                              "BEGIN SELECT RAISE(ABORT, 'simulated disk full'); END")
        with patch("pathlib.Path.unlink", side_effect=PermissionError("sharing violation")):
            with self.assertRaises(sqlite3.Error):
                self.store.put(self.source, self.metadata)
        self.store.db.execute("DROP TRIGGER reject_ref")
        self.assertEqual(self.store.audit()["orphan_count"], 1)
        with self.assertRaisesRegex(ValueError, "ownership-unclassified"):
            self.store.put(self.source, self.metadata)
        self.assertEqual(self.store.audit()["refs"], 0)
        self.assertEqual(self.store.gc()["status"], "blocked")
        self.assertTrue(self.source.exists())

    def test_shared_bytes_forget_one_ref_and_portable_restore(self):
        first = self.store.put(self.source, self.metadata)
        second = self.store.put(self.source, self.metadata)
        self.assertEqual(first["hash"], second["hash"])
        self.assertEqual(self.store.audit()["objects"], 1)
        self.assertEqual(self.store.read(first["ref_id"], self.access), self.source.read_bytes())
        self.store.forget(first["ref_id"], self.access)
        with self.assertRaisesRegex(ValueError, "reference-unavailable"):
            self.store.read(first["ref_id"], self.access)
        self.assertEqual(self.store.read(second["ref_id"], self.access), self.source.read_bytes())
        exported = self.base / "export.zip"
        self.store.export(second["ref_id"], self.access, exported)
        restored = self.base / "restored" / self.source.name
        restored.parent.mkdir()
        self.assertEqual(ImmutableStore.restore(exported, restored, self.access)["hash"], second["hash"])
        self.assertEqual(restored.read_bytes(), self.source.read_bytes())
        self.assertTrue(self.source.exists())

    def test_current_scope_policy_privacy_and_terminal_owner(self):
        ref = self.store.put(self.source, self.metadata)["ref_id"]
        for change in ({"allowed_privacy": ["public"]}, {"policy_revision": "policy-2"},
                       {"workspace": self.base}):
            with self.assertRaises((PermissionError, ValueError)):
                self.store.read(ref, {**self.access, **change})
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["status"] = "working"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "authoritative-owner-not-complete"):
            self.store.put(self.source, self.metadata)

    def test_secret_corruption_and_failed_atomic_publish_preserve_original(self):
        self.source.write_text("api_key=longsecretvalue12345", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "known-secret"):
            self.store.put(self.source, self.metadata)
        self.source.write_text("clean evidence", encoding="utf-8")
        with patch("adhd.immutable_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.put(self.source, self.metadata)
        self.assertEqual(self.store.audit()["refs"], 0)
        self.assertEqual(self.source.read_text(encoding="utf-8"), "clean evidence")
        ref = self.store.put(self.source, self.metadata)
        blob = self.store._blob(ref["hash"])
        blob.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "blob-missing-or-corrupt"):
            self.store.read(ref["ref_id"], self.access)

    def test_gc_is_dry_run_and_unknown_closure_blocks_apply(self):
        content = b"orphan from interrupted registration"
        digest = hashlib.sha256(content).hexdigest()
        orphan = self.store._blob(digest)
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(content)
        preview = self.store.gc()
        self.assertEqual(preview["orphan_count"], 1)
        self.assertEqual(preview["orphan_hashes"], [])
        self.assertIsNone(preview["reclaimed_disk_bytes"])
        blocked = self.store.gc(dry_run=False)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(orphan.exists())

    def test_restore_rejects_oversized_manifest_before_loading_it(self):
        exported = self.base / "oversized.zip"
        with zipfile.ZipFile(exported, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", "x" * 65537)
            archive.writestr("content", b"safe")
        with self.assertRaisesRegex(ValueError, "export-member-too-large"):
            ImmutableStore.restore(exported, self.base / "result.log", self.access)
        self.assertFalse((self.base / "result.log").exists())

    def test_completed_ref_survives_next_native_run_with_current_policy_enforced(self):
        ref = self.store.put(self.source, self.metadata)["ref_id"]
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["run_id"] = "c" * 32
        state["status"] = "working"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.store.read(ref, self.access), self.source.read_bytes())
        with self.assertRaises(PermissionError):
            self.store.read(ref, {**self.access, "policy_revision": "policy-2"})
        self.store.export(ref, self.access, self.base / "past-run.zip")
        self.store.forget(ref, self.access)
        with self.assertRaisesRegex(ValueError, "reference-unavailable"):
            self.store.read(ref, self.access)

    def test_source_traversal_and_bridge_name_cannot_mint_run_ref(self):
        outside = self.workspace / "outside.log"
        outside.write_text("unrelated", encoding="utf-8")
        through_parent = self.workspace / ".adhd" / self.run / ".." / ".." / "outside.log"
        with self.assertRaisesRegex(ValueError, "source-outside-completed-run"):
            self.store.put(through_parent, self.metadata)
        bridge = self.workspace / ".adhd" / "bridge"
        bridge.mkdir()
        forged = bridge / ("view-" + self.run + ".log")
        forged.write_text("unrelated", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source-outside-completed-run"):
            self.store.put(forged, self.metadata)


if __name__ == "__main__":
    unittest.main()
