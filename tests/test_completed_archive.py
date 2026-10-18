from __future__ import annotations

import json
from pathlib import Path
import unittest

from adhd.completed_archive import capture_completed
from adhd.immutable_gc import NATIVE_STATE_FIELDS
from adhd.immutable_store import ImmutableStore


class CompletedArchiveTests(unittest.TestCase):
    def test_native_api_completed_receipt_capture_read_gc_and_next_run(self):
        # Uses the native begin/candidate/reviewer/finish API fixture, not a forged state JSON.
        from test_native import NativeTests
        native = NativeTests("test_native_success")
        native.setUp()
        self.addCleanup(native.tearDown)
        (native.ws / ".adhd").mkdir(exist_ok=True)
        (native.ws / ".adhd" / "immutable-policy.json").write_text(json.dumps({
            "schema_version": 1, "policy_revision": "policy-1", "allowed_privacy": ["private"],
            "allowed_actions": ["capture", "read", "export", "restore", "forget"]}), encoding="utf-8")
        native.event("SessionStart")
        native.begin()
        native.event("Stop", last_assistant_message="Work in progress")
        self.assertTrue(native.candidate()["ok"])
        native.spawn()
        native.review()
        self.assertEqual(native.state()["status"], "complete")
        native.event("Stop", last_assistant_message="Verified completed result")
        self.assertEqual(set(native.state()["session_helper_profiles"]), {"adhd-scout", "adhd-light"})
        self.assertIn("session_reader", native.state())
        self.assertIn("view_hint_sig", native.state())
        self.assertIn("last_assistant_message", native.state())
        self.assertEqual(set(native.state()) - NATIVE_STATE_FIELDS, set())
        source = native.ch / "adhd" / "native" / native.key / ("completion-" + native.state()["run_id"] + ".json")
        captured = capture_completed(native.ws, native.key, "policy-1")
        access = {"workspace": native.ws, "policy_revision": "policy-1", "allowed_privacy": ["private"]}
        with ImmutableStore(native.ch / "adhd" / "immutable-v2", native_root=native.ch / "adhd" / "native") as archive:
            self.assertEqual(archive.read(captured["ref_id"], access), source.read_bytes())
            archive.forget(captured["ref_id"], access)
            preview = archive.gc()
            self.assertEqual(preview["status"], "planned", preview)
            self.assertEqual(len(preview["candidates"]), 1)
            self.assertEqual(archive.gc_apply(preview["operation_id"])["status"], "complete")
        self.assertTrue(source.exists())
        past = capture_completed(native.ws, native.key, "policy-1")
        native.event("UserPromptSubmit", prompt="A different task with a new deliverable")
        pending = native.state()["pending_turn_ids"][0]
        self.assertTrue(native.request("sync-intent", {"source_turn_id": pending,
            "base_revision": native.state()["intent_version"], "classification": "new_task"})["ok"])
        native.begin()
        with ImmutableStore(native.ch / "adhd" / "immutable-v2", native_root=native.ch / "adhd" / "native") as archive:
            self.assertEqual(archive.read(past["ref_id"], access), source.read_bytes())
            exported = native.base / "old-run.zip"
            archive.export(past["ref_id"], access, exported)
            self.assertTrue(exported.exists())
            archive.forget(past["ref_id"], access)
            self.assertEqual(archive.gc()["status"], "blocked")
        self.assertTrue(native.candidate()["ok"])
        native.spawn(aid="second-child")
        native.review(aid="second-child")
        self.assertEqual(native.state()["status"], "complete")
        second = capture_completed(native.ws, native.key, "policy-1")
        with ImmutableStore(native.ch / "adhd" / "immutable-v2", native_root=native.ch / "adhd" / "native") as archive:
            self.assertEqual(archive.read(second["ref_id"], access),
                             (native.ch / "adhd" / "native" / native.key /
                              ("completion-" + second["owner_run"] + ".json")).read_bytes())
            self.assertNotEqual(past["owner_run"], second["owner_run"])
            self.assertEqual(archive.db.execute("SELECT count(*) FROM gc_roots").fetchone()[0], 2)
            self.assertEqual(archive.gc()["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
