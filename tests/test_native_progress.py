"""Progress is based on current outcomes and changed target bytes."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.core import atomic_json, read_json
from adhd.evidence import run_check, recognized_test_failures
from adhd.native import folder, handle_event, progress_markers, session_key, submit_request
from adhd.recovery import record_failure, verified_failure_signature


class NativeProgressTests(unittest.TestCase):
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
        plan = {'objective': 'Implement result', 'approach': 'Inspect and check',
                'verification': 'Run tests', 'preflight': ['Inspect'],
                'risks': ['Regression'], 'alternatives': ['Small patch'],
                'steps': [{'id': 'S1', 'action': 'Implement', 'requirements': ['R1'],
                           'depends_on': []}]}
        response = self.request('begin', {'mode': 'coding', 'criteria': [
            {'id': 'R1', 'text': 'Tests pass', 'kind': 'test'}],
            'artifacts': ['result.txt'], 'plan': plan})
        self.assertTrue(response['ok'], response)

    def event(self, name, **kw):
        self.turn += 1
        return handle_event({'hook_event_name': name, 'session_id': self.sid,
                             'cwd': str(self.ws), 'turn_id': str(self.turn), **kw})

    def request(self, op, payload):
        result = submit_request(self.key, self.ws, op, payload)
        self.event('PostToolUse', tool_name='Bash')
        return read_json(Path(result['receipt']))

    def state(self):
        return read_json(folder(self.key) / 'state.json')

    def save(self, state):
        atomic_json(folder(self.key) / 'state.json', state)

    def check(self, script):
        return run_check({'run_id': self.state()['run_id'],
                          'contract_revision': self.state()['intent_version'],
                          'subject_paths': ['result.txt'],
                          'argv': [sys.executable, '-c', script]}, self.ws)

    def test_timestamp_only_output_does_not_reset_stagnation(self):
        self.event('Stop')  # a valid plan is the initial planning milestone
        (self.ws / 'result.txt').write_text('same', encoding='utf-8')
        script = 'import time;print(time.time_ns())'
        first = self.check(script)
        self.assertTrue(self.request('checkpoint', {'summary': 'Checked', 'next_action': 'Continue',
                                                     'evidence_ids': [first['receipt']]})['ok'])
        self.event('Stop')
        self.assertEqual(self.state()['stagnation'], 0)
        second = self.check(script)
        self.assertTrue(self.request('checkpoint', {'summary': 'Checked again', 'next_action': 'Continue',
                                                     'evidence_ids': [second['receipt']]})['ok'])
        self.event('Stop')
        self.assertEqual(self.state()['stagnation'], 1)

    def test_verified_failure_reduction_and_exact_repeat(self):
        (self.ws / 'result.txt').write_text('3', encoding='utf-8')
        script = ('from pathlib import Path;import sys,time;'
                  'n=int(Path("result.txt").read_text());'
                  'print(time.strftime("%Y-%m-%dT%H:%M:%S"),file=sys.stderr);'
                  'print("Ran 5 tests in 0.001s",file=sys.stderr);'
                  'print(f"FAILED (failures={n})",file=sys.stderr);sys.exit(1)')
        first = self.check(script)
        self.assertTrue(self.request('checkpoint', {'summary': 'Failure observed', 'next_action': 'Fix',
                     'failure': {'category': 'test_failure', 'detail': 'Three failed',
                                 'evidence_id': first['receipt']}})['ok'])
        self.event('Stop')
        (self.ws / 'result.txt').write_text('2', encoding='utf-8')
        second = self.check(script)
        self.assertTrue(self.request('checkpoint', {'summary': 'Fewer failures', 'next_action': 'Fix',
                     'failure': {'category': 'test_failure', 'detail': 'Two failed',
                                 'evidence_id': second['receipt']}})['ok'])
        self.event('Stop')
        state = self.state()
        self.assertEqual(min(state['test_outcomes'].values()), 2)
        self.assertTrue(any(row['kind'] == 'failing_tests_reduced' for row in state['verified_progress']))
        self.assertEqual(state['failure_history'][-1]['verified_attempt'], 1)
        third = self.check(script)
        self.assertTrue(self.request('checkpoint', {'summary': 'Same failure', 'next_action': 'Fix',
                     'failure': {'category': 'test_failure', 'detail': 'Two still failed',
                                 'evidence_id': third['receipt']}})['ok'])
        self.event('Stop')
        self.assertEqual(self.state()['stagnation'], 1)
        self.assertEqual(self.state()['failure_history'][-1]['verified_attempt'], 2)

    def test_current_requirement_and_plan_step_are_counted_then_expire_on_edit(self):
        (self.ws / 'result.txt').write_text('done', encoding='utf-8')
        check = self.check('import sys;print("Ran 1 test in 0.001s",file=sys.stderr);print("OK",file=sys.stderr)')
        ref = check['receipt']
        response = self.request('checkpoint', {
            'summary': 'Requirement and step checked', 'next_action': 'Submit',
            'criterion_results': [{'id': 'R1', 'pass': True, 'evidence': 'Observed test',
                                   'evidence_ids': [ref]}],
            'completed_steps': [{'id': 'S1', 'evidence_ids': [ref]}]})
        self.assertTrue(response['ok'], response)
        markers = progress_markers(self.state())
        self.assertTrue(any(m.startswith('requirement_pass:') for m in markers))
        self.assertTrue(any(m.startswith('plan_step:') for m in markers))
        (self.ws / 'result.txt').write_text('changed after test', encoding='utf-8')
        self.assertFalse(any(m.startswith('requirement_pass:') for m in progress_markers(self.state())))

    def test_old_receipt_cannot_claim_checkpoint_pass(self):
        (self.ws / 'result.txt').write_text('done', encoding='utf-8')
        check = self.check('print("ok")')
        state = self.state()
        state['started'] += 10
        self.save(state)
        response = self.request('checkpoint', {'summary': 'Old check', 'next_action': 'Continue',
            'criterion_results': [{'id': 'R1', 'pass': True, 'evidence': 'Observed',
                                   'evidence_ids': [check['receipt']]}]})
        self.assertFalse(response['ok'])

    def test_identical_verified_failures_have_separate_count_from_category(self):
        self.assertEqual(recognized_test_failures('', 'Ran 3 tests in 0.1s\nFAILED (failures=2, errors=1)', 1), 3)
        self.assertEqual(recognized_test_failures('== 2 failed, 3 passed in 0.1s ==', '', 1), 2)
        a = verified_failure_signature('2026-10-04T04:00:00 AssertionError: one')
        b = verified_failure_signature('2026-10-04T04:00:09 AssertionError: one')
        self.assertEqual(a, b)
        self.assertNotEqual(a, verified_failure_signature('AssertionError: two'))
        state = {}
        self.assertEqual(record_failure(state, 'test_failure', 'one', 'a', verified_signature=a)['verified_attempt'], 1)
        self.assertEqual(record_failure(state, 'test_failure', 'two', 'b', verified_signature=verified_failure_signature('AssertionError: two'))['verified_attempt'], 1)
        self.assertEqual(state['failure_counts']['test_failure'], 2)

    def test_runner_duration_noise_does_not_change_failure_identity(self):
        first = 'AssertionError: same\nRan 3 tests in 0.100s\nFAILED (failures=1)'
        second = 'AssertionError: same\nRan 3 tests in 0.250s\nFAILED (failures=1)'
        self.assertEqual(verified_failure_signature(first), verified_failure_signature(second))

    def test_old_plan_completion_does_not_survive_plan_replacement(self):
        (self.ws / 'result.txt').write_text('done', encoding='utf-8')
        check = self.check('print("ok")')
        self.assertTrue(self.request('checkpoint', {'summary': 'Step checked', 'next_action': 'Continue',
            'completed_steps': [{'id': 'S1', 'evidence_ids': [check['receipt']]}]})['ok'])
        plan = self.state()['plan']['content']
        plan['steps'] = [{'id': 'S2', 'action': 'Different validated plan',
                          'requirements': ['R1'], 'depends_on': []}]
        self.assertTrue(self.request('plan', plan)['ok'])
        self.assertFalse(any(marker.endswith(':S1') for marker in progress_markers(self.state())))

    def test_unrelated_file_does_not_reset_stagnation(self):
        self.event('Stop')
        (self.ws / 'notes.txt').write_text('unrelated', encoding='utf-8')
        self.event('Stop')
        self.assertEqual(self.state()['stagnation'], 1)
