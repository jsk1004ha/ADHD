"""Ordinary requests continue to their outcome without an explicit goal command."""
from __future__ import annotations

import sys
import unittest

from adhd.core import atomic_json, store
from adhd.evidence import run_check
from adhd.native import DEFAULTS, folder, limit_enabled
from tests import test_outcome_first as fixtures


class OutcomeCompletionTests(unittest.TestCase):
    setUp = fixtures.OutcomeFirstTests.setUp
    event = fixtures.OutcomeFirstTests.event
    request = fixtures.OutcomeFirstTests.request
    state = fixtures.OutcomeFirstTests.state
    begin = fixtures.OutcomeFirstTests.begin
    check = fixtures.OutcomeFirstTests.check
    candidate = fixtures.OutcomeFirstTests.candidate

    def test_invalid_remaining_work_does_not_start_a_run(self):
        result = self.request('begin', {'mode': 'coding',
            'criteria': [{'id': 'R1', 'text': 'Result works', 'kind': 'test'}],
            'artifacts': ['result.txt'], 'remaining_work': 'not a list'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.state()['status'], 'idle')
        self.begin()
        self.assertEqual(self.state()['status'], 'working')

    def test_auto_continues_past_implicit_limits_without_inventing_a_goal(self):
        self.begin()
        state = self.state()
        state['started'] -= DEFAULTS['max_seconds'] + 10
        atomic_json(folder(self.key) / 'state.json', state)
        for _ in range(DEFAULTS['max_lifetime_rounds'] + 2):
            self.assertEqual(self.event('Stop')['decision'], 'block')
        self.event('PostCompact')
        state = self.state()
        self.assertEqual(state['status'], 'working')
        self.assertEqual(state['loop_mode'], 'bounded')
        self.assertIsNone(state['goal_request'])
        self.assertFalse(state['needs_replan'])
        self.assertFalse(limit_enabled(state, 'max_epochs'))
        self.assertTrue(limit_enabled(state, 'max_children'))
        self.assertIn('next action', state['feedback'])

    def test_user_configured_limit_remains_binding(self):
        atomic_json(store() / 'native-policy.json', {'max_rounds': 1})
        self.begin()
        self.assertEqual(self.event('Stop')['decision'], 'block')
        self.assertFalse(self.event('Stop')['continue'])
        self.assertEqual(self.state()['status'], 'budget_exhausted')

    def test_reported_remaining_work_cannot_be_promoted_to_success(self):
        self.begin()
        check = self.check()
        result = self.request('candidate', {'files': ['result.txt'],
            'remaining_work': ['Run the requested application flow'],
            'criterion_results': [{'id': 'R1', 'pass': True, 'evidence': 'File assertion passed',
                                   'evidence_ids': [check['receipt']]}]})
        self.assertTrue(result['ok'])
        self.assertEqual(self.state()['status'], 'working')
        self.assertFalse(self.state().get('recipe_saved'))
        self.assertIn('Run the requested application flow', self.event('Stop')['reason'])
        self.assertTrue(self.request('candidate', {'files': ['result.txt'], 'remaining_work': [],
            'criterion_results': [{'id': 'R1', 'pass': True, 'evidence': 'Requested flow completed',
                                   'evidence_ids': [self.check()['receipt']]}]})['ok'])
        self.assertEqual(self.state()['status'], 'complete')

    def test_checkpoint_remaining_work_survives_compaction_and_approval(self):
        self.begin()
        self.assertTrue(self.request('checkpoint', {'summary': 'Implementation exists',
            'next_action': 'Finish packaging', 'remaining_work': ['Deliver the usable package']})['ok'])
        self.event('PostCompact')
        self.event('UserPromptSubmit', prompt='계속 진행해')
        self.assertEqual(self.state()['pending_turn_ids'], [])
        self.candidate(self.check())
        self.assertEqual(self.state()['status'], 'working')
        self.assertEqual(self.state()['remaining_work'], ['Deliver the usable package'])

    def test_one_unavailable_method_does_not_end_auto_work(self):
        self.begin()
        (self.ws / 'result.txt').write_text('working', encoding='utf-8')
        failed = run_check({'run_id': self.state()['run_id'], 'contract_revision': 1,
            'subject_paths': ['result.txt'], 'argv': [sys.executable, '-c',
            'import sys; sys.stderr.write("tool unavailable"); sys.exit(1)']}, self.ws)
        result = self.request('checkpoint', {'summary': 'Primary tool unavailable',
            'next_action': 'Use the available local equivalent',
            'failure': {'category': 'tool_unavailable', 'detail': 'Tool unavailable',
                        'evidence_id': failed['receipt']}})
        self.assertTrue(result['ok'])
        self.assertEqual(self.state()['status'], 'working')
        self.assertFalse(self.state()['failure_history'][-1]['retry_allowed'])
        self.assertEqual(self.event('Stop')['decision'], 'block')
        self.assertTrue(self.candidate(self.check())['ok'])
        self.assertEqual(self.state()['status'], 'complete')

    def test_explicit_user_stop_still_stops_auto(self):
        self.begin()
        self.event('UserPromptSubmit', prompt='stop')
        self.assertEqual(self.state()['status'], 'cancelled')
        self.assertFalse(self.event('Stop')['continue'])


if __name__ == '__main__':
    unittest.main()
