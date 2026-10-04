"""Execution profiles change workflow gates without changing role models."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.core import atomic_json, read_json, store
from adhd.evidence import run_check
from adhd.models import execution_profile, route
from adhd.native import folder, handle_event, session_key, submit_request


class ExecutionProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ws = Path(temp.name) / 'workspace'
        self.ws.mkdir()
        codex_home = Path(temp.name) / 'codex'
        codex_home.mkdir()
        env = patch.dict(os.environ, {'CODEX_HOME': str(codex_home), 'ADHD_EXEC_OWNER': ''})
        env.start()
        self.addCleanup(env.stop)
        self.sid = uuid.uuid4().hex
        self.key = session_key(self.sid)
        self.turn = 0
        self.event('UserPromptSubmit', prompt='Implement result.txt and verify it')
        self.brief = {'objective': 'Deliver result', 'approach': 'Write and check',
                      'verification': 'Run test',
                      'steps': [{'id': 'S1', 'action': 'Implement and test',
                                 'requirements': ['R1'], 'depends_on': []}]}

    def event(self, name, **kw):
        self.turn += 1
        return handle_event({'hook_event_name': name, 'session_id': self.sid,
                             'cwd': str(self.ws), 'turn_id': str(self.turn), **kw})

    def request(self, op, payload):
        receipt = submit_request(self.key, self.ws, op, payload)
        self.event('PostToolUse', tool_name='Bash')
        return read_json(Path(receipt['receipt']))

    def state(self):
        return read_json(folder(self.key) / 'state.json')

    def begin(self, profile, plan=None):
        payload = {'mode': 'coding', 'execution_profile': profile,
                   'criteria': [{'id': 'R1', 'text': 'Result verified', 'kind': 'test'}],
                   'artifacts': ['result.txt']}
        if plan is not None:
            payload['plan'] = plan
        return self.request('begin', payload)

    def successful_check(self):
        (self.ws / 'result.txt').write_text('working', encoding='utf-8')
        return run_check({'run_id': self.state()['run_id'],
                          'contract_revision': self.state()['intent_version'],
                          'subject_paths': ['result.txt'],
                          'argv': [sys.executable, '-c', 'print("PASS")']}, self.ws)

    def candidate(self, receipt):
        return self.request('candidate', {'files': ['result.txt'],
            'criterion_results': [{'id': 'R1', 'pass': True,
                                   'evidence': 'Observed local process pass',
                                   'evidence_ids': [receipt['receipt']]}]})

    def review(self, *, include_deep):
        review_id = 'reviewer-' + str(self.turn)
        tool_id = 'review-tool-' + str(self.turn)
        self.event('PreToolUse', tool_name='spawn_agent', tool_use_id=tool_id,
                   tool_input={'agent_type': 'adhd-verifier', 'message': 'Inspect actual candidate.'})
        self.event('SubagentStart', agent_type='adhd-verifier', agent_id=review_id,
                   model=route('adhd-verifier')['model'], tool_use_id=tool_id)
        state = self.state()
        verdict = {'verdict': 'approve', 'reviewed_digest': state['candidate']['digest'],
                   'reviewed_contract_hash': state['contract_hash'],
                   'reviewed_turn_ids': [p['turn_id'] for p in state['prompts']],
                   'intent_alignment': True,
                   'criterion_results': state['candidate']['criterion_results'], 'findings': []}
        if include_deep:
            verdict['evidence_review'] = {
                'target_covering_receipts': state['candidate']['deep_evidence']['target_covering_receipts'],
                'findings': ['Checked current receipt against result.txt and observed a passing process.']}
        self.event('SubagentStop', agent_type='adhd-verifier', agent_id=review_id,
                   last_assistant_message=json.dumps(verdict))

    def test_simple_direct_or_one_child_and_independent_review(self):
        self.assertTrue(self.begin('simple')['ok'])
        self.assertEqual(self.state()['execution_profile'], execution_profile('simple'))
        self.assertFalse(self.state()['plan_required'])
        self.assertEqual(self.state()['policy']['max_children'], 1)
        check = self.successful_check()
        self.assertTrue(self.candidate(check)['ok'])
        self.assertEqual(self.state()['status'], 'reviewing')
        self.review(include_deep=False)
        self.assertEqual(self.state()['status'], 'complete')
        self.assertEqual(route('adhd-verifier')['model'], 'gpt-6-sol')

    def test_standard_accepts_brief_plan_and_requires_it_for_candidate(self):
        self.assertTrue(self.begin('standard')['ok'])
        self.assertEqual(self.state()['execution_profile']['plan_depth'], 'brief')
        check = self.successful_check()
        self.assertFalse(self.candidate(check)['ok'])
        self.assertTrue(self.request('plan', self.brief)['ok'])
        self.assertTrue(self.candidate(check)['ok'])
        self.review(include_deep=False)
        self.assertEqual(self.state()['status'], 'complete')

    def test_deep_needs_full_plan_current_target_check_and_evidence_review(self):
        self.assertFalse(self.begin('deep', self.brief)['ok'])
        full = {**self.brief, 'preflight': ['Inspect inputs'],
                'risks': ['Regression'], 'alternatives': ['Smaller patch']}
        self.assertTrue(self.begin('deep', full)['ok'])
        self.assertEqual(self.state()['execution_profile']['review_depth'], 'strengthened')
        (self.ws / 'result.txt').write_text('working', encoding='utf-8')
        stale = run_check({'run_id': self.state()['run_id'],
                           'contract_revision': self.state()['intent_version'],
                           'subject_paths': ['result.txt'],
                           'argv': [sys.executable, '-c', 'print("PASS")']}, self.ws)
        (self.ws / 'result.txt').write_text('changed', encoding='utf-8')
        self.assertFalse(self.candidate(stale)['ok'])
        fresh = self.successful_check()
        self.assertTrue(self.candidate(fresh)['ok'])
        self.review(include_deep=False)
        self.assertEqual(self.state()['status'], 'revising')
        self.assertTrue(self.candidate(fresh)['ok'])
        self.review(include_deep=True)
        self.assertEqual(self.state()['status'], 'complete')

    def test_explicit_policy_precedes_simple_profile_defaults(self):
        policy = store() / 'native-policy.json'
        policy.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(policy, {'max_children': 2, 'max_total_children': 8})
        self.assertTrue(self.begin('simple')['ok'])
        self.assertEqual(self.state()['policy']['max_children'], 2)
        self.assertEqual(self.state()['policy']['max_total_children'], 8)
