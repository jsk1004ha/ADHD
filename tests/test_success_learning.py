"""A completed, current task becomes one reusable project procedure."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.core import atomic_json, digest, discover_skills, file_hash, learn_success, recipes, skill_search, store
from adhd.evidence import run_check
from adhd.experience_procedures import _completion
from adhd.memory import Memory
from adhd.snapshots import build_snapshot


class SuccessLearningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        env = patch.dict(os.environ, {'ADHD_HOME': str(self.root / 'controller'),
                                   'CODEX_HOME': str(self.root / 'codex')})
        env.start()
        self.addCleanup(env.stop)
        self.key = uuid.uuid4().hex[:24]
        self.run_id = uuid.uuid4().hex
        self.directory = store() / 'native' / self.key
        (self.workspace / 'result.txt').write_text('result', encoding='utf-8')
        check = run_check({'run_id': self.run_id, 'contract_revision': 1,
                           'subject_paths': ['result.txt'],
                           'argv': [sys.executable, '-c', 'print("result verified")']}, self.workspace)
        self.receipt = check['receipt']
        criteria = [{'id': 'R1', 'kind': 'test', 'text': 'Result has the requested content'}]
        contract = {'criteria': criteria, 'artifacts': ['result.txt']}
        candidate = {'files': build_snapshot(self.workspace, ['result.txt']), 'snapshot_schema': 1,
                     'intent_version': 1, 'contract_hash': digest(contract), 'evidence_schema': 1,
                     'criterion_results': [{'id': 'R1', 'pass': True,
                                            'evidence': 'Observed successful result check',
                                            'evidence_ids': [self.receipt]}],
                     'execution_receipts': {self.receipt: file_hash(self.workspace / self.receipt)},
                     'procedure': []}
        candidate['digest'] = digest(candidate)
        completion = {'method': 'checked', 'digest': candidate['digest'],
                      'contract_hash': digest(contract), 'time': 1.0}
        self.state = {'workspace': str(self.workspace), 'key': self.key, 'run_id': self.run_id,
                      'status': 'complete', 'contract': contract, 'contract_hash': digest(contract),
                      'intent_version': 1, 'mode': 'coding', 'candidate': candidate,
                      'execution_profile': {'name': 'auto', 'review_depth': 'checked'},
                      'completion_receipt': completion, 'children': {}, 'pending_turn_ids': [],
                      'prompts': [{'turn_id': 'turn-1', 'text': 'Produce a result.txt file'}]}
        atomic_json(self.directory / 'state.json', self.state)
        atomic_json(self.directory / ('completion-' + self.run_id + '.json'), completion)

    def test_checked_completion_learns_and_discovers_one_local_skill(self):
        result = learn_success(self.state, self.directory)
        self.assertEqual(result['learned_digest'], self.state['candidate']['digest'])
        self.assertEqual(result['learning']['method'], 'checked')
        rid = result['recipe_saved']
        self.assertTrue(rid)
        found = discover_skills(self.workspace)
        self.assertIn(str(store() / 'recipes' / rid / 'SKILL.md'), [row['path'] for row in found])
        recalled = recipes(self.workspace, 'coding', 'Produce a result.txt file')
        self.assertEqual(recalled[0]['id'], rid)
        self.assertIn('result.txt', ' '.join(recalled[0]['steps']))
        self.assertIn(self.receipt, ' '.join(recalled[0]['steps']))
        self.assertEqual(recalled[0]['verified_runs'], 1)
        self.assertIn(str(store() / 'recipes' / rid / 'SKILL.md'),
                      [row['path'] for row in skill_search('Produce a result.txt file', workspace=self.workspace)])
        again = learn_success(self.state, self.directory)
        self.assertEqual(again['recipe_saved'], rid)
        self.assertEqual(recipes(self.workspace, 'coding', 'Produce a result.txt file')[0]['verified_runs'], 1)

    def test_tampered_checked_completion_cannot_learn(self):
        receipt = self.directory / ('completion-' + self.run_id + '.json')
        original = json.loads(receipt.read_text(encoding='utf-8'))
        for altered in ({**original, 'digest': '0' * 64},
                        {**original, 'contract_hash': '0' * 64}):
            atomic_json(receipt, altered)
            with self.assertRaises(ValueError):
                learn_success(self.state, self.directory)
        atomic_json(receipt, original)
        (self.workspace / 'result.txt').write_text('changed', encoding='utf-8')
        with self.assertRaises(ValueError):
            learn_success(self.state, self.directory)
        self.assertFalse((store() / 'memory-v2.sqlite3').exists())

    def test_strict_profile_still_requires_independent_completion(self):
        self.state['execution_profile'] = {'name': 'standard', 'review_depth': 'independent'}
        atomic_json(self.directory / 'state.json', self.state)
        with self.assertRaises(ValueError):
            _completion(self.workspace, {'session_key': self.key, 'run_id': self.run_id}, current=True)
        with self.assertRaises(ValueError):
            learn_success(self.state, self.directory)

    def test_independent_completion_learns_without_claiming_checked(self):
        candidate = self.state['candidate']
        candidate['procedure'] = ['Create result.txt', 'Run the recorded result check']
        candidate['digest'] = digest({key: value for key, value in candidate.items() if key != 'digest'})
        contract_hash = self.state['contract_hash']
        verdict = {'verdict': 'approve', 'reviewed_digest': candidate['digest'],
                   'reviewed_contract_hash': contract_hash, 'reviewed_turn_ids': ['turn-1'],
                   'intent_alignment': True, 'criterion_results': candidate['criterion_results']}
        review = {'digest': candidate['digest'], 'verdict': verdict}
        self.state['execution_profile'] = {'name': 'standard', 'review_depth': 'independent'}
        self.state.pop('completion_receipt')
        self.state['review_receipt'] = review
        atomic_json(self.directory / 'state.json', self.state)
        atomic_json(self.directory / ('completion-' + self.run_id + '.json'), review)
        result = learn_success(self.state, self.directory)
        self.assertEqual(result['learning']['method'], 'independent')
        self.assertEqual(recipes(self.workspace, 'coding', 'Produce a result.txt file')[0]['steps'],
                         candidate['procedure'])

    def test_project_environment_and_lifecycle_limit_discovery(self):
        rid = learn_success(self.state, self.directory)['recipe_saved']
        other = self.root / 'other'
        other.mkdir()
        self.assertNotIn('recipe-' + rid, [row['name'] for row in discover_skills(other)])
        (self.workspace / 'package.json').write_text('{"version":"2"}', encoding='utf-8')
        self.assertNotIn('recipe-' + rid, [row['name'] for row in discover_skills(self.workspace)])
        (self.workspace / 'package.json').unlink()
        with Memory() as memory:
            memory.quarantine([rid])
        self.assertEqual(recipes(self.workspace, 'coding', 'Produce a result.txt file'), [])
        with self.assertRaises(ValueError):
            learn_success(self.state, self.directory)
        self.assertNotIn(str(store() / 'recipes' / rid / 'SKILL.md'),
                         [row['path'] for row in discover_skills(self.workspace)])

    def test_forgotten_procedure_is_not_relearned(self):
        rid = learn_success(self.state, self.directory)['recipe_saved']
        with Memory() as memory:
            memory.forget(rid)
        with self.assertRaises(ValueError):
            learn_success(self.state, self.directory)
        self.assertNotIn(str(store() / 'recipes' / rid / 'SKILL.md'),
                         [row['path'] for row in discover_skills(self.workspace)])


if __name__ == '__main__':
    unittest.main()
