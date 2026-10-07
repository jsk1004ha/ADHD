from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.core import atomic_json, digest, file_hash, store
from adhd.evidence import run_check
from adhd.experience_procedures import (
    _controller_evidence, adopt, list_procedures, propose, rollback,
)
from adhd.snapshots import build_snapshot


class ProcedureLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        self.environment = patch.dict(os.environ, {'ADHD_HOME': str(self.base / 'controller'),
                                                 'CODEX_HOME': str(self.base / 'codex')})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.artifact = self.workspace / 'artifact.md'
        self.artifact.write_text('verified document bytes', encoding='utf-8')
        self.cost = self.workspace / 'cost.json'
        atomic_json(self.cost, {'baseline': 10, 'candidate': 7, 'unit': 'seconds', 'basis': 'measured'})
        self.steps = ['Open the project artifact', 'Run the matching validation']

    def completion(self, *, replay: bool, steps: list[str] | None = None) -> tuple[dict, dict]:
        steps = steps or self.steps
        key, run_id = uuid.uuid4().hex[:24], uuid.uuid4().hex
        ids = ('replay', 'regression', 'cost', 'rollback') if replay else ('example',)
        contract = {'criteria': [{'id': name, 'kind': 'test'} for name in ids],
                    'artifacts': ['artifact.md']}
        contract_hash = digest(contract)
        results, receipts = [], {}
        for name in ids:
            subject = ['cost.json'] if name == 'cost' else ['artifact.md']
            row = run_check({'run_id': run_id, 'contract_revision': 1,
                             'subject_paths': subject,
                             'argv': [sys.executable, '-c', 'print("observed")']}, self.workspace)
            receipts[row['receipt']] = file_hash(self.workspace / row['receipt'])
            results.append({'id': name, 'pass': True, 'evidence': 'Observed local check',
                            'evidence_ids': [row['receipt']]})
        files = ['artifact.md', 'cost.json'] if replay else ['artifact.md']
        candidate = {'files': build_snapshot(self.workspace, files), 'snapshot_schema': 1,
                     'intent_version': 1, 'contract_hash': contract_hash, 'evidence_schema': 1,
                     'criterion_results': results, 'execution_receipts': receipts,
                     'procedure': steps}
        candidate['digest'] = digest(candidate)
        review = {'digest': candidate['digest'], 'verdict': {
            'verdict': 'approve', 'reviewed_digest': candidate['digest'],
            'reviewed_contract_hash': contract_hash, 'reviewed_turn_ids': ['turn-1'],
            'intent_alignment': True, 'criterion_results': results}}
        state = {'workspace': str(self.workspace), 'key': key, 'run_id': run_id,
                 'status': 'complete', 'contract': contract, 'contract_hash': contract_hash,
                 'intent_version': 1, 'candidate': candidate, 'review_receipt': review,
                 'children': {}, 'pending_turn_ids': [], 'prompts': [{'turn_id': 'turn-1'}]}
        directory = store() / 'native' / key
        atomic_json(directory / 'state.json', state)
        atomic_json(directory / ('completion-' + run_id + '.json'), review)
        return {'session_key': key, 'run_id': run_id}, results

    def payload(self, source: dict, replay: dict | None = None, *, steps: list[str] | None = None) -> dict:
        value = {'mode': 'coding', 'goal': 'Repeat verified artifact validation',
                 'scope': 'project:demo', 'input_conditions': ['Same input format'],
                 'steps': steps or self.steps, 'verifier': ['Check result and evidence'],
                 'recovery': ['Restore previous version'],
                 'prohibited_conditions': ['Input format differs'],
                 'artifact': {'kind': 'document', 'path': 'artifact.md',
                              'sha256': file_hash(self.artifact)},
                 'source_receipt': source}
        if replay:
            value['replay_receipt'] = replay
            value['evaluation'] = {
                name: {'criterion_id': name, **({'report': 'cost.json'} if name == 'cost' else {})}
                for name in ('replay', 'regression', 'cost', 'rollback')}
        return value

    def test_example_candidate_adoption_and_previous_version_rollback(self):
        source, _ = self.completion(replay=False)
        example = propose(self.workspace, self.payload(source))
        self.assertEqual(example['stage'], 'example')
        self.assertFalse((store() / 'memory-v2.sqlite3').exists())
        with self.assertRaisesRegex(ValueError, 'controller'):
            adopt(self.workspace, example['proposal_id'], {'session_key': source['session_key']})

        replay, _ = self.completion(replay=True)
        candidate = propose(self.workspace, self.payload(source, replay))
        self.assertEqual(candidate['stage'], 'candidate')
        with self.assertRaisesRegex(ValueError, 'controller'):
            adopt(self.workspace, candidate['proposal_id'], 'approved')
        first = adopt(self.workspace, candidate['proposal_id'],
                      _controller_evidence(self.workspace, **replay))
        self.assertEqual(first['version'], 1)

        newer_steps = ['Open the project artifact', 'Run validation and inspect recovery']
        source2, _ = self.completion(replay=False, steps=newer_steps)
        replay2, _ = self.completion(replay=True, steps=newer_steps)
        second = propose(self.workspace, self.payload(source2, replay2, steps=newer_steps))
        adopted = adopt(self.workspace, second['proposal_id'],
                        _controller_evidence(self.workspace, **replay2))
        self.assertEqual(adopted['version'], 2)
        self.assertEqual(adopted['previous_version'], 1)
        rolled = rollback(self.workspace, first['procedure_id'])
        self.assertEqual(rolled['active_version'], 1)
        record = list_procedures(self.workspace)['procedures'][0]
        self.assertEqual(len(record['versions']), 2)
        self.assertEqual(record['active_version'], 1)

    def test_forged_or_stale_controller_completion_cannot_adopt(self):
        source, _ = self.completion(replay=False)
        replay, _ = self.completion(replay=True)
        candidate = propose(self.workspace, self.payload(source, replay))
        capability = _controller_evidence(self.workspace, **replay)
        receipt = store() / 'native' / replay['session_key'] / ('completion-' + replay['run_id'] + '.json')
        receipt.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'completion'):
            adopt(self.workspace, candidate['proposal_id'], capability)

    def test_replay_and_cost_gates_reject_wrong_evidence(self):
        source, _ = self.completion(replay=False)
        with self.assertRaisesRegex(ValueError, 'distinct'):
            propose(self.workspace, self.payload(source, source))
        replay, _ = self.completion(replay=True)
        wrong = self.payload(source, replay)
        wrong['evaluation']['regression']['criterion_id'] = 'replay'
        with self.assertRaisesRegex(ValueError, 'distinct'):
            propose(self.workspace, wrong)
        atomic_json(self.cost, {'baseline': 5, 'candidate': 7, 'unit': 'seconds', 'basis': 'measured'})
        with self.assertRaisesRegex(ValueError, 'stale|changed'):
            propose(self.workspace, self.payload(source, replay))

    def test_artifact_path_escape_and_missing_reference_rejected(self):
        source, _ = self.completion(replay=False)
        bad = self.payload(source)
        bad['artifact']['path'] = '../artifact.md'
        with self.assertRaises(ValueError):
            propose(self.workspace, bad)
        bad['artifact']['path'] = 'gone.md'
        with self.assertRaises(ValueError):
            propose(self.workspace, bad)


if __name__ == '__main__':
    unittest.main()
