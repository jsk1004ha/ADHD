from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from adhd import experience
from adhd.evidence import run_check


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.workspace = base / "project"
        self.vault = base / "vault"
        self.workspace.mkdir()
        (self.vault / "10 기록" / "아티팩트").mkdir(parents=True)
        self.config = {"enabled": True, "vault_id": "vault-one", "vault_root": str(self.vault),
                       "write_enabled": True, "write_roots": ["10 기록/아티팩트"]}
        self.config_patch = patch.object(experience, "_load_config", side_effect=lambda _ws: self.config)
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.temp.cleanup()

    def event(self, **changes):
        result = {"task_id": "task-a", "artifact_revision": "rev-a", "kind": "failure",
                  "project": "project-a", "title": "Repeated build failure",
                  "summary": "Build failed at package resolution", "conditions": "offline build"}
        result.update(changes)
        return result

    def feedback(self, **changes):
        result = self.event(kind="feedback", feedback=[{
            "polarity": "positive", "dimension": "expression", "scope": "task",
            "quote": "The explanation was concise", "source": "turn-12"}])
        result.update(changes)
        return result

    def ledger(self):
        return json.loads((self.workspace / ".adhd" / "experience" / "ledger.json").read_text(encoding="utf-8"))

    def test_disabled_integration_has_no_trace_or_vault_side_effect(self):
        self.config["enabled"] = False
        result = experience.candidate(self.workspace, self.feedback())
        self.assertEqual(result["status"], "disabled")
        self.assertFalse((self.workspace / ".adhd").exists())
        self.assertEqual(list(self.vault.rglob("*.md")), [])

    def test_actual_adapter_config_and_flat_list_provenance(self):
        self.config["vault"] = self.config.pop("vault_root")
        selected = experience.candidate(self.workspace, self.feedback())
        result = experience.apply_candidate(self.workspace, selected["candidate_id"])
        self.assertEqual(result["status"], "applied")
        note = next(self.vault.rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn('source_ids:\n  - "task:task-a"', note)
        self.assertIn('projects:\n  - "project-a"', note)
        self.assertNotIn('source_ids: ["', note)

    def test_trivial_and_duplicate_event_do_not_create_notes(self):
        event = self.event(summary="One-off path lookup")
        first = experience.candidate(self.workspace, event)
        again = experience.candidate(self.workspace, event)
        self.assertEqual(first["status"], "skipped")
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.ledger()["traces"]), 1)
        self.assertEqual(experience.list_candidates(self.workspace), [])
        self.assertEqual(list(self.vault.rglob("*.md")), [])
        with self.assertRaisesRegex(ValueError, "Conflicting experience"):
            experience.candidate(self.workspace, {**event, "summary": "Different finding"})

    def test_recurrence_keeps_distinct_task_evidence(self):
        first = experience.candidate(self.workspace, self.event(task_id="one"))
        second = experience.candidate(self.workspace, self.event(task_id="two"))
        self.assertEqual(first["status"], "skipped")
        self.assertEqual(second["status"], "selected")
        traces = list(self.ledger()["traces"].values())
        self.assertEqual(len(traces), 2)
        self.assertEqual(traces[0]["content_hash"], traces[1]["content_hash"])
        self.assertEqual({row["task_id"] for row in traces}, {"one", "two"})

    def test_feedback_axes_are_separate_and_scopes_remain_local(self):
        event = self.feedback(feedback=[
            {"polarity": "positive", "dimension": "expression", "scope": "task",
             "quote": "The explanation was concise", "source": "turn-12"},
            {"polarity": "negative", "dimension": "requirements", "scope": "project",
             "quote": "The old feature broke", "source": "turn-12"},
            {"polarity": "unknown", "quote": "Interesting", "source": "turn-13"},
        ], verified=True)
        result = experience.candidate(self.workspace, event)
        record = self.ledger()["candidates"][result["candidate_id"]]["record"]
        self.assertEqual(record["technical_status"], "unverified")
        self.assertEqual([row["polarity"] for row in record["feedback"]],
                         ["positive", "negative", "unknown"])
        self.assertEqual([row["scope_id"] for row in record["feedback"]],
                         ["task-a", "project-a", "task-a"])
        self.assertEqual(record["feedback"][2]["dimension"], "unknown")
        self.assertNotIn("global", json.dumps(record))

    def test_forged_receipt_and_dangling_document_cannot_verify_success(self):
        forged = self.workspace / ".adhd" / "checks" / ("a" * 32) / "receipt.json"
        forged.parent.mkdir(parents=True)
        forged.write_text('{"success": true}', encoding="utf-8")
        doc = self.workspace / "missing.pdf"
        event = self.event(kind="success", reusable=True, verified=True,
                           receipts=[{"kind": "receipt", "path": str(forged.relative_to(self.workspace)),
                                      "run_id": "run", "contract_revision": 1}],
                           artifacts=[{"kind": "document", "path": doc.name,
                                       "sha256": "0" * 64, "requirements": ["R3"]}])
        result = experience.candidate(self.workspace, event)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(next(iter(self.ledger()["traces"].values()))["technical_status"], "unverified")
        self.assertEqual(experience.audit_artifact({"workspace": str(self.workspace), "kind": "document",
                                                   "path": doc.name, "sha256": "0" * 64,
                                                   "requirements": ["R3"]})["status"], "unverified")

    def test_real_receipt_and_artifact_pointer_select_verified_example(self):
        artifact = self.workspace / "artifact.txt"
        artifact.write_text("verified content", encoding="utf-8")
        receipt = run_check({"argv": [sys.executable, "-c", "print('ok')"],
                             "subject_paths": [artifact.name], "run_id": "run",
                             "contract_revision": 1}, self.workspace)
        result = experience.candidate(self.workspace, self.event(
            kind="success", reusable=True,
            receipts=[{"kind": "receipt", "path": receipt["receipt"],
                       "run_id": "run", "contract_revision": 1}],
            artifacts=[{"kind": "document", "path": artifact.name,
                        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        "requirements": ["R3"]}]))
        self.assertEqual(result["status"], "selected")
        self.assertEqual(self.ledger()["candidates"][result["candidate_id"]]["record"]["technical_status"],
                         "passed")

    def test_write_grant_revision_and_user_edits(self):
        first = experience.candidate(self.workspace, self.feedback())
        note = self.vault / first["target"]["path"]
        self.assertFalse(note.exists())  # Candidate creation cannot write the vault.
        self.assertEqual(experience.apply_candidate(self.workspace, first["candidate_id"])["status"], "applied")
        before = note.read_text(encoding="utf-8")
        self.assertIn('source_ids:\n  - "task:task-a"', before)
        self.assertEqual(experience.apply_candidate(self.workspace, first["candidate_id"])["status"],
                         "already_applied")
        second = experience.candidate(self.workspace, self.feedback(
            task_id="task-b", target={"path": first["target"]["path"],
                                      "note_id": first["target"]["note_id"],
                                      "expected_revision": hashlib.sha256(note.read_bytes()).hexdigest()}))
        note.write_text(before + "\nUser's independent edit\n", encoding="utf-8")
        self.assertEqual(experience.apply_candidate(self.workspace, second["candidate_id"])["status"],
                         "revision_conflict")
        self.assertIn("User's independent edit", note.read_text(encoding="utf-8"))
        self.config["write_enabled"] = False
        with self.assertRaisesRegex(ValueError, "not enabled"):
            experience.apply_candidate(self.workspace, second["candidate_id"])

    def test_crash_between_note_and_status_recovers_without_overwrite(self):
        item = experience.candidate(self.workspace, self.feedback())
        original = experience._save
        calls = 0

        def fail_second(directory, state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated crash after note write")
            return original(directory, state)

        with patch.object(experience, "_save", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                experience.apply_candidate(self.workspace, item["candidate_id"])
        note = self.vault / item["target"]["path"]
        self.assertTrue(note.is_file())
        self.assertEqual(self.ledger()["candidates"][item["candidate_id"]]["status"], "applying")
        self.assertEqual(experience.apply_candidate(self.workspace, item["candidate_id"])["status"],
                         "already_applied")
        self.assertEqual(len(list(self.vault.rglob("ADHD-*.md"))), 1)

    def test_concurrent_apply_and_linked_target_fail_closed(self):
        item = experience.candidate(self.workspace, self.feedback())
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(experience.apply_candidate, self.workspace, item["candidate_id"])
                       for _ in range(2)]
        self.assertEqual({future.result()["status"] for future in futures},
                         {"applied", "already_applied"})
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("keep", encoding="utf-8")
        linked = self.vault / "10 기록" / "아티팩트" / "linked.md"
        try:
            linked.symlink_to(outside)
        except OSError:
            return
        with self.assertRaisesRegex(ValueError, "links or junctions"):
            experience.candidate(self.workspace, self.feedback(task_id="linked",
                target={"path": "10 기록/아티팩트/linked.md", "note_id": "artifact:linked"}))
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_usage_keeps_partial_and_discarded_costs_without_false_zero(self):
        observed = {"call_id": "a", "task_id": "task-a", "phase": "child", "role": "executor",
                    "retry": 1, "input_tokens": 100, "output_tokens": 20,
                    "cached_input_tokens": 30}
        partial = {"call_id": "b", "task_id": "task-a", "phase": "curation",
                   "role": "reviewer", "input_tokens": None, "output_tokens": 7}
        missing = {"call_id": "c", "task_id": "task-a", "phase": "candidate",
                   "role": "selector"}
        for event in (observed, partial, missing):
            experience.record_usage(self.workspace, event)
        self.assertEqual(experience.record_usage(self.workspace, observed)["status"], "duplicate")
        with self.assertRaisesRegex(ValueError, "Conflicting usage"):
            experience.record_usage(self.workspace, {**observed, "output_tokens": 21})
        report = experience.usage_report(self.workspace)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["calls"], 3)
        self.assertEqual(report["totals"]["input_tokens"],
                         {"known": 100, "missing_calls": 2, "observed_calls": 1})
        self.assertEqual(report["phases"]["curation"], 1)
        self.assertEqual(report["phases"]["candidate"], 1)
        self.assertEqual(report["retry_calls"], 1)


if __name__ == "__main__":
    unittest.main()
