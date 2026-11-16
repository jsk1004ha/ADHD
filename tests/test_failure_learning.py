"""Failures survive task resets and feed the next attempt without a review gate."""
from __future__ import annotations

import json
from pathlib import Path
import uuid
import unittest
from unittest.mock import patch

from tests import test_outcome_first as fixtures


class FailureLearningTests(unittest.TestCase):
    setUp = fixtures.OutcomeFirstTests.setUp
    event = fixtures.OutcomeFirstTests.event
    request = fixtures.OutcomeFirstTests.request
    state = fixtures.OutcomeFirstTests.state
    begin = fixtures.OutcomeFirstTests.begin
    check = fixtures.OutcomeFirstTests.check
    candidate = fixtures.OutcomeFirstTests.candidate

    def command(self, identity, response, command='python check_feature.py'):
        return self.event('PostToolUse', tool_name='exec_command', tool_use_id=identity,
                          tool_input={'cmd': command}, tool_response=response)

    def records(self):
        return [json.loads(p.read_text(encoding='utf-8'))
                for p in (self.ws / '.adhd/failures').glob('*.json')]

    def test_first_failure_is_retained_and_replay_is_not_counted_twice(self):
        self.begin()
        response = {'exit_code': 1, 'output': 'AssertionError: wrong boundary'}
        self.command('failed-1', response)
        rows = self.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['category'], 'test_failure')
        self.assertEqual(rows[0]['occurrences'], 1)
        self.command('failed-1', response)
        self.assertEqual(self.records()[0]['occurrences'], 1)
        self.command('failed-2', response)
        self.assertEqual(self.records()[0]['occurrences'], 2)

    def test_recovery_is_retained_and_next_run_recalls_it(self):
        from adhd.native import session_key
        self.begin()
        self.command('failure', {'exit_code': 1, 'output': 'AssertionError: missing edge'})
        self.command('repaired', {'exit_code': 0, 'output': 'PASS'})
        self.assertEqual(self.records()[0]['status'], 'recovered')
        self.assertTrue(self.candidate(self.check())['ok'])
        self.sid = uuid.uuid4().hex
        self.key = session_key(self.sid)
        self.event('UserPromptSubmit', prompt='Implement another result.txt feature')
        self.begin()
        self.assertEqual(len(self.state()['prior_failures']), 1)
        self.assertEqual(self.state()['prior_failures'][0]['status'], 'recovered')
        self.assertTrue(self.state()['recipes'])

    def test_success_preserves_an_abandoned_failure_with_completion_reference(self):
        self.begin()
        self.command('failure', {'exit_code': 1, 'output': 'SyntaxError: old approach'})
        self.assertTrue(self.candidate(self.check())['ok'])
        row = self.records()[0]
        self.assertEqual(row['status'], 'task_completed')
        self.assertEqual(row['category'], 'syntax_error')
        self.assertTrue(Path(row['completion_receipt']).is_file())

    def test_running_and_empty_search_are_not_failures(self):
        self.begin()
        self.command('running', {'session_id': 5, 'output': 'still running'})
        self.command('unknown', {'output': 'no terminal result'})
        self.command('no-match', {'exit_code': 1, 'output': ''}, 'rg missing src')
        self.assertEqual(self.records(), [])

    def test_failure_is_redacted_before_durable_storage(self):
        self.begin()
        self.command('secret', {'exit_code': 1,
            'output': 'authentication failed --token "private value" password=confidential'},
            'python check_feature.py --token "another private value"')
        self.assertEqual(len(self.records()), 1)
        body = json.dumps(self.records())
        for secret in ('private value', 'confidential', 'another private value'):
            self.assertNotIn(secret, body)

    def test_failure_storage_error_does_not_interrupt_the_task(self):
        self.begin()
        with patch('adhd.recovery.atomic_json', side_effect=OSError('disk is full')):
            self.command('failed', {'exit_code': 1, 'output': 'AssertionError: boundary'})
        self.assertEqual(self.state()['status'], 'working')
        self.assertIn('disk is full', self.state().get('memory_warning', ''))
        self.assertTrue(self.candidate(self.check())['ok'])

    def test_failed_check_is_retained_without_checkpoint(self):
        self.begin()
        result = self.check(failure=True)
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]['evidence_ref'], result['receipt'])
        self.assertTrue(self.candidate(self.check())['ok'])
        self.assertEqual(self.records()[0]['status'], 'task_completed')

    def test_learning_storage_error_does_not_undo_success(self):
        import sqlite3
        self.begin()
        receipt = self.check()
        with patch('adhd.core.learn_success', side_effect=sqlite3.OperationalError('learning db locked')):
            result = self.candidate(receipt)
        self.assertTrue(result['ok'])
        self.assertIn('Success', result['message'])
        self.assertEqual(self.state()['status'], 'complete')
        self.assertIn('learning db locked', self.state()['memory_warning'])


if __name__ == '__main__':
    unittest.main()
