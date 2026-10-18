from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from adhd.immutable_store import ImmutableStore
from adhd.memory import namespace


def _concurrent_operation(root: str, native: str, operation: str, ref_id: str,
                          access: dict, source: str, metadata: dict, export_path: str, queue) -> None:
    with ImmutableStore(Path(root), native_root=Path(native)) as store:
        try:
            if operation == "read":
                queue.put(store.read(ref_id, access))
            elif operation == "export":
                queue.put(store.export(ref_id, access, Path(export_path))["ref_id"])
            else:
                queue.put(store.put(Path(source), metadata)["ref_id"])
        except Exception as exc:
            queue.put(type(exc).__name__)


class ImmutableGCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".adhd").mkdir()
        (self.workspace / ".adhd" / "immutable-policy.json").write_text(json.dumps({
            "schema_version": 1, "policy_revision": "policy-1", "allowed_privacy": ["private"],
            "allowed_actions": ["capture", "read", "export", "restore", "forget"]}), encoding="utf-8")
        self.run = "a" * 32
        self.key = "b" * 24
        self.native = self.base / "native"
        owner = self.native / self.key
        owner.mkdir(parents=True)
        self.state = owner / "state.json"
        self._state({"schema_version": 2, "key": self.key, "run_id": self.run,
                     "workspace": str(self.workspace), "status": "complete",
                     "children": {}, "pending_turn_ids": []})
        self.source = self.workspace / ".adhd" / self.run / "result.log"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes("completed 원문\n".encode())
        self.metadata = {"workspace": str(self.workspace), "scope": namespace(self.workspace),
                         "owner_key": self.key, "owner_run": self.run, "kind": "log",
                         "privacy": "private", "policy_revision": "policy-1",
                         "provenance": {"kind": "test"}, "retention": "normal"}
        self.access = {"workspace": self.workspace, "allowed_privacy": ["private"],
                       "policy_revision": "policy-1"}
        self.store = ImmutableStore(self.base / "store", native_root=self.native)
        self.addCleanup(self.store.close)
        self.store.register_root(self.workspace, self.key, self.run)

    def _state(self, value):
        self.state.write_text(json.dumps(value), encoding="utf-8")

    def _refs(self):
        first = self.store.put(self.source, self.metadata)
        second = self.store.put(self.source, self.metadata)
        return first, second

    def test_shared_ref_forget_and_gc_preserves_original_and_portable_export(self):
        first, second = self._refs()
        self.store.forget(first["ref_id"], self.access)
        self.assertEqual(self.store.gc()["candidates"], [])
        export = self.base / "portable.zip"
        self.store.export(second["ref_id"], self.access, export)
        self.store.forget(second["ref_id"], self.access)
        preview = self.store.gc()
        self.assertEqual(len(preview["candidates"]), 1)
        self.assertNotIn("hash", preview["candidates"][0])
        result = self.store.gc_apply(preview["operation_id"])
        self.assertEqual(result["removed_count"], 1)
        self.assertFalse(self.store._blob(first["hash"]).exists())
        self.assertEqual(self.source.read_bytes(), "completed 원문\n".encode())
        destination = self.base / "restored" / self.source.name
        destination.parent.mkdir()
        ImmutableStore.restore(export, destination, self.access)
        self.assertEqual(destination.read_bytes(), self.source.read_bytes())

    def test_pin_requires_published_manifest_disposal_before_release(self):
        ref = self.store.put(self.source, self.metadata)
        pin = self.store.pin(ref["ref_id"], self.access)
        with self.assertRaisesRegex(ValueError, "unconfirmed-pin"):
            self.store.release_pin(pin["pin_id"], self.access)
        manifest = Path(pin["manifest_path"])
        manifest.write_text(json.dumps(pin["manifest"]), encoding="utf-8")
        self.store.confirm_pin(pin["pin_id"], self.access)
        self.store.forget(ref["ref_id"], self.access)
        self.assertEqual(self.store.gc()["candidates"], [])
        with self.assertRaisesRegex(ValueError, "still-references"):
            self.store.release_pin(pin["pin_id"], self.access)
        manifest.unlink()
        self.store.release_pin(pin["pin_id"], self.access)
        self.assertEqual(len(self.store.gc()["candidates"]), 1)

    def test_generation_owner_and_unknown_artifact_block_stale_plan(self):
        ref = self.store.put(self.source, self.metadata)
        self.store.forget(ref["ref_id"], self.access)
        preview = self.store.gc()
        self.store.put(self.source, self.metadata)
        with self.assertRaisesRegex(ValueError, "gc-plan-stale"):
            self.store.gc_apply(preview["operation_id"])
        self.store.forget(self.store.db.execute("SELECT id FROM refs WHERE forgotten=0").fetchone()[0], self.access)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["candidate"] = {"unexpected": True}
        self._state(state)
        self.assertEqual(self.store.gc()["status"], "blocked")
        self.assertTrue(self.store._blob(ref["hash"]).exists())

    def test_active_lease_unknown_owner_and_legacy_store_block(self):
        ref = self.store.put(self.source, self.metadata)
        self.store.forget(ref["ref_id"], self.access)
        from adhd.immutable_gc import _lease
        lease = _lease(self.store, self.workspace)
        lease.parent.mkdir(exist_ok=True)
        lease.write_text("{}", encoding="utf-8")
        self.assertEqual(self.store.gc()["status"], "blocked")
        lease.unlink()
        (self.native / ("c" * 24)).mkdir()
        self.assertEqual(self.store.gc()["status"], "blocked")
        (self.native / ("c" * 24)).rmdir()
        with ImmutableStore(self.base / "legacy", native_root=self.native) as legacy:
            legacy.db.execute("UPDATE meta SET value=0 WHERE key='gc_contract'")
            legacy.db.commit()
            self.assertEqual(legacy.gc()["status"], "blocked")

    def test_quarantine_verification_fault_restores_blob_and_recover(self):
        ref = self.store.put(self.source, self.metadata)
        self.store.forget(ref["ref_id"], self.access)
        preview = self.store.gc()
        with patch("adhd.immutable_gc._verify_quarantine", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "^gc-filesystem-unavailable$"):
                self.store.gc_apply(preview["operation_id"])
        self.assertTrue(self.store._blob(ref["hash"]).exists())
        self.assertEqual(self.store.gc()["status"], "blocked")
        self.assertEqual(self.store.gc_recover()["status"], "recovered")

    def test_public_filesystem_failures_do_not_disclose_object_identity(self):
        ref = self.store.put(self.source, self.metadata)
        self.store.forget(ref["ref_id"], self.access)
        blob = self.store._blob(ref["hash"])
        denied = PermissionError(13, "access denied", str(blob))
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == blob:
                raise denied
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", stat):
            with self.assertRaisesRegex(ValueError, "^object-store-filesystem-unavailable$") as failure:
                self.store.audit()
            self.assertNotIn(ref["hash"], str(failure.exception))
        preview = self.store.gc()
        with patch("adhd.immutable_gc._hash_file", side_effect=denied):
            blocked = self.store.gc()
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["reason"], "gc-filesystem-unavailable")
            with self.assertRaisesRegex(ValueError, "^gc-filesystem-unavailable$"):
                self.store.gc_apply(preview["operation_id"])
        original_iterdir = Path.iterdir

        def iterdir(path):
            if path == self.store.root / "quarantine":
                raise denied
            return original_iterdir(path)

        with patch.object(Path, "iterdir", iterdir):
            with self.assertRaisesRegex(ValueError, "^gc-filesystem-unavailable$"):
                self.store.gc_recover()
        self.assertTrue(blob.is_file())

    def test_read_export_and_put_wait_for_writer_lock_across_processes(self):
        ref = self.store.put(self.source, self.metadata)
        for operation in ("read", "export", "put"):
            with self.subTest(operation=operation):
                queue = multiprocessing.get_context("spawn").Queue()
                export_path = self.base / (operation + ".zip")
                process = multiprocessing.get_context("spawn").Process(
                    target=_concurrent_operation,
                    args=(str(self.store.root), str(self.native), operation, ref["ref_id"],
                          self.access, str(self.source), self.metadata, str(export_path), queue))
                with self.store.lock:
                    process.start()
                    self.assertTrue(queue.empty())
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
                result = queue.get(timeout=2)
                if operation == "read":
                    self.assertEqual(result, self.source.read_bytes())
                elif operation == "export":
                    self.assertEqual(result, ref["ref_id"])
                    self.assertTrue(export_path.is_file())
                else:
                    self.assertRegex(result, "^[0-9a-f]{32}$")

    def test_two_candidates_in_one_frozen_batch(self):
        first = self.store.put(self.source, self.metadata)
        other = self.source.with_name("other.log")
        other.write_bytes(b"other completed result")
        second = self.store.put(other, self.metadata)
        self.store.forget(first["ref_id"], self.access)
        self.store.forget(second["ref_id"], self.access)
        preview = self.store.gc()
        self.assertEqual(len(preview["candidates"]), 2)
        self.assertEqual(self.store.gc_apply(preview["operation_id"])["removed_count"], 2)
        self.assertEqual(self.source.read_bytes(), "completed 원문\n".encode())
        self.assertEqual(other.read_bytes(), b"other completed result")

    def test_interrupted_quarantine_restores_original_cas_location(self):
        ref = self.store.put(self.source, self.metadata)
        self.store.forget(ref["ref_id"], self.access)
        op = self.store.gc()["operation_id"]
        quarantined = self.store.root / "quarantine" / (op + "-" + ref["hash"])
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE gc_runs SET status='applying' WHERE id=?", (op,))
            self.store.db.execute("UPDATE gc_candidates SET status='quarantined' WHERE run_id=?", (op,))
            os.replace(self.store._blob(ref["hash"]), quarantined)
        self.assertEqual(self.store.gc_recover()["restored_count"], 1)
        self.assertTrue(self.store._blob(ref["hash"]).is_file())
        self.assertFalse(quarantined.exists())

    def test_delete_ready_crash_restores_verified_copy(self):
        ref = self.store.put(self.source, self.metadata)
        self.store.forget(ref["ref_id"], self.access)
        op = self.store.gc()["operation_id"]
        blob = self.store._blob(ref["hash"])
        backup = self.store.root / "quarantine" / (op + "-" + ref["hash"] + ".restore")
        shutil.copyfile(blob, backup)
        blob.unlink()
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE gc_runs SET status='applying' WHERE id=?", (op,))
            self.store.db.execute("UPDATE gc_candidates SET status='delete_ready' WHERE run_id=?", (op,))
        self.assertEqual(self.store.gc_recover()["restored_count"], 1)
        self.assertTrue(blob.exists())
        self.assertFalse(backup.exists())

    def test_child_pending_protected_and_unknown_object_are_not_collected(self):
        protected = self.store.put(self.source, {**self.metadata, "retention": "protected"})
        self.store.forget(protected["ref_id"], self.access)
        self.assertEqual(self.store.gc()["candidates"], [])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        for field, value in (("children", {"child": {"status": "running"}}),
                             ("pending_turn_ids", ["turn-1"]),
                             ("session_reader", {"unknown": True})):
            changed = {**state, field: value}
            self._state(changed)
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE gc_roots SET state_hash=?", (hashlib.sha256(self.state.read_bytes()).hexdigest(),))
            self.assertEqual(self.store.gc()["status"], "blocked")
        self._state(state)
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE gc_roots SET state_hash=?", (hashlib.sha256(self.state.read_bytes()).hexdigest(),))
        policy = self.workspace / ".adhd" / "immutable-policy.json"
        original_policy = policy.read_text(encoding="utf-8")
        policy.write_text(original_policy[:-1] + ', "unknown": true}', encoding="utf-8")
        self.assertEqual(self.store.gc()["status"], "blocked")
        policy.write_text(original_policy, encoding="utf-8")
        unknown = self.store._blob(hashlib.sha256(b"unregistered").hexdigest())
        unknown.parent.mkdir(exist_ok=True)
        unknown.write_bytes(b"unregistered")
        self.assertEqual(self.store.gc()["status"], "blocked")
        self.assertTrue(unknown.exists())


if __name__ == "__main__":
    unittest.main()
