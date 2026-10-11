"""Loops are steered toward a new strategy; an autonomous goal run is never ended by them."""
from __future__ import annotations

import unittest
import json
from copy import deepcopy

from adhd.loop_guard import CHURN_GRACE, observe_tool_loop, repeated_review_rejection
from adhd.native import DEFAULTS, bridge, mark_verified_progress
from tests import test_native as fixtures
from tests import test_native_goal


class LoopGuardTests(unittest.TestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown
    event = fixtures.NativeTests.event
    state = fixtures.NativeTests.state
    save = fixtures.NativeTests.save
    request = fixtures.NativeTests.request
    candidate = fixtures.NativeTests.candidate
    spawn = fixtures.NativeTests.spawn
    review = fixtures.NativeTests.review
    begin = test_native_goal.NativeGoalTests.begin

    def failing(self, n, command='pytest -q'):
        return self.event('PostToolUse', tool_name='Bash', tool_input={'command': command},
                          tool_response='FAILED test_x - AssertionError at 12:00:%02d' % n)

    def settled(self):
        # The first Stop records the plan as real progress; stagnation counts from here.
        self.begin()
        self.event('Stop')
        self.assertEqual(self.state()['stagnation'], 0)

    def replan(self, approach):
        return self.request('plan', {'objective': 'Meet the requested outcome', 'approach': approach,
            'verification': 'Run actual checks', 'steps': [{'id': 'S1', 'action': 'Implement and verify',
                                                            'requirements': ['R1'], 'depends_on': []}]})

    def test_ladder_forces_changed_plan_without_ending_the_run(self):
        self.begin()
        cap = DEFAULTS['max_stagnation']
        for _ in range(cap * 3 + 2):
            result = self.event('Stop')
            self.assertEqual(result['decision'], 'block')
            self.assertNotIn('continue', result)
        state = self.state()
        self.assertEqual(state['status'], 'working')
        self.assertGreaterEqual(state['ladder_stage'], 3)
        self.assertTrue(state['needs_replan'])
        self.assertIn('changed plan', state['feedback'])
        note = (bridge(self.ws, self.key) / 'stuck.md').read_text(encoding='utf-8')
        self.assertIn('Required before the next attempt', note)

    def test_identical_plan_is_rejected_and_a_changed_plan_resumes_work(self):
        self.begin()
        for _ in range(DEFAULTS['max_stagnation'] + 1):
            self.event('Stop')
        self.assertTrue(self.state()['needs_replan'])
        self.assertFalse(self.replan('Implement and check')['ok'])
        self.assertFalse(self.candidate()['ok'])
        self.assertTrue(self.replan('Reduce to the smallest failing check first')['ok'])
        self.assertFalse(self.state()['needs_replan'])

    def test_soft_reset_names_the_architect_only_while_budget_remains(self):
        self.settled()
        state = self.state()
        state['stagnation'] = DEFAULTS['max_stagnation'] * 2 - 1
        self.save(state)
        self.event('Stop')
        self.assertIn('adhd-architect', self.state()['feedback'])
        state = self.state()
        state['astra_calls'] = state['policy']['max_astra_calls']
        state['stagnation'] = DEFAULTS['max_stagnation'] * 3 - 1
        self.save(state)
        self.event('Stop')
        self.assertNotIn('adhd-architect', self.state()['feedback'])

    def test_verified_progress_resets_the_ladder(self):
        self.settled()
        state = self.state()
        state['stagnation'] = DEFAULTS['max_stagnation'] - 1
        self.save(state)
        self.event('Stop')
        self.assertEqual(self.state()['ladder_stage'], 1)
        state = self.state()
        mark_verified_progress(state, 'plan_step', 'S1')
        self.save(state)
        self.event('Stop')
        state = self.state()
        self.assertEqual(state['stagnation'], 0)
        self.assertEqual(state['ladder_stage'], 0)

    def test_edit_churn_stops_resetting_stagnation_after_grace(self):
        self.begin()
        self.event('Stop')
        for i in range(CHURN_GRACE):
            (self.ws / 'answer.txt').write_text('variant %d' % i, encoding='utf-8')
            self.event('Stop')
            self.assertEqual(self.state()['stagnation'], 0)
        for i in range(3):
            (self.ws / 'answer.txt').write_text('later variant %d' % i, encoding='utf-8')
            self.event('Stop')
        self.assertEqual(self.state()['stagnation'], 3)

    def test_artifact_grace_preserves_the_next_stop_loop_warning(self):
        self.settled()
        state = self.state(); state['stagnation'] = 2; self.save(state)
        (self.ws / 'answer.txt').write_text('weak artifact edit', encoding='utf-8')
        for n in range(3): self.failing(n)
        self.event('Stop', turn_id='combined-stop')
        state = self.state()
        self.assertEqual(state['churn'], 1)
        self.assertEqual(state['stagnation'], 3)
        self.assertEqual(state.get('loop_hits', 0), 0)
        self.assertTrue(state['needs_replan'])
        self.event('Stop', turn_id='combined-stop')
        self.assertEqual(self.state()['stagnation'], 3)  # No second bonus on redelivery.

    def test_verified_outcome_wins_over_both_artifact_churn_and_warning(self):
        self.settled()
        state = self.state(); state.update(stagnation=2, loop_hits=1, churn=2)
        mark_verified_progress(state, 'plan_step', 'S1'); self.save(state)
        (self.ws / 'answer.txt').write_text('verified artifact edit', encoding='utf-8')
        self.event('Stop')
        state = self.state()
        self.assertEqual(state['stagnation'], 0)
        self.assertEqual(state['churn'], 0)
        self.assertEqual(state.get('loop_hits', 0), 0)
        self.assertEqual(state['ladder_stage'], 0)

    def test_warning_before_the_first_simple_stop_is_counted(self):
        self.assertTrue(self.request('begin', {'mode': 'coding', 'execution_profile': 'simple',
            'criteria': [{'id': 'R1', 'text': 'Behavior passes', 'kind': 'behavior'}],
            'artifacts': ['answer.txt']})['ok'])
        self.assertFalse(self.state()['progress_initialized'])
        for n in range(3): self.failing(n)
        self.event('Stop', turn_id='first-loop-stop')
        self.assertEqual(self.state()['stagnation'], 1)
        self.assertEqual(self.state().get('loop_hits', 0), 0)
        self.event('Stop', turn_id='first-loop-stop')
        self.assertEqual(self.state()['stagnation'], 1)

    def test_stagnation_signal_matrix(self):
        from adhd.native import update_stagnation
        from unittest.mock import patch
        cases = [
            # initialized, artifact edit, prior churn, verified outcome, warning, expected count
            (False, False, 0, False, 0, 0),
            (False, False, 0, False, 1, 1),
            (True, False, 0, False, 0, 3),
            (True, False, 0, False, 1, 4),
            (True, True, 0, False, 0, 0),
            (True, True, 0, False, 1, 3),
            (True, True, CHURN_GRACE, False, 0, 3),
            (True, True, CHURN_GRACE, False, 1, 4),
            (False, False, 0, True, 1, 0),
            (True, True, 2, True, 0, 0),
            (True, True, 2, True, 1, 0),
        ]
        for initialized, edit, churn, verified, warning, expected in cases:
            with self.subTest(case=(initialized, edit, churn, verified, warning)):
                state = {'status': 'working', 'children': {}, 'progress_initialized': initialized,
                         'stagnation': 2, 'churn': churn, 'loop_hits': warning,
                         'seen_target_digests': {'answer.txt': ['old']}}
                with patch('adhd.native.progress_markers', return_value={'verified:test'} if verified else set()), \
                     patch('adhd.native.target_manifest', return_value={'answer.txt': 'new' if edit else 'old'}):
                    update_stagnation(state)
                self.assertEqual(state['stagnation'], expected)
                self.assertEqual(state.get('loop_hits', 0), 0)

    def test_identical_failing_tool_result_warns_inside_the_turn(self):
        self.settled()
        self.assertEqual(self.failing(1), {})
        self.assertEqual(self.failing(2), {})
        out = self.failing(3)  # timestamps differ; the failure is the same
        self.assertIn('Loop', out['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.failing(4), {})  # once per level, not per call
        self.assertIn('keeps failing', self.failing(5)['hookSpecificOutput']['additionalContext'])
        before = self.state()['stagnation']
        self.event('Stop')
        self.assertGreaterEqual(self.state()['stagnation'], before + 2)

    def test_success_clears_the_loop_evidence_for_that_command(self):
        self.begin()
        self.failing(1)
        self.failing(2)
        self.event('PostToolUse', tool_name='Bash', tool_input={'command': 'pytest -q'},
                   tool_response='12 passed')
        self.assertEqual(self.failing(3), {})
        self.assertEqual(self.state().get('loop_hits', 0), 0)

    def test_different_failures_and_read_only_tools_do_not_warn(self):
        self.begin()
        for n in range(6):
            self.assertEqual(self.failing(n, command='pytest tests/test_%d.py' % (n * 7)), {})
        for _ in range(4):
            self.assertEqual(self.event('PostToolUse', tool_name='wait', tool_response='error'), {})
        self.assertEqual(self.state().get('loop_hits', 0), 0)

    def test_repeated_reviewer_finding_forces_a_changed_plan(self):
        self.begin()
        for attempt in (1, 2):
            self.assertTrue(self.candidate()['ok'])
            self.spawn(aid='review-%d' % attempt)
            self.review(False, aid='review-%d' % attempt)
            self.assertEqual(self.state()['status'], 'revising')
            self.assertEqual(self.state()['needs_replan'], attempt == 2)
            if attempt == 1:
                self.event('Stop')
        self.assertIn('repeated its previous finding', self.state()['feedback'])
        self.assertFalse(self.candidate()['ok'])

    def test_a_different_reviewer_finding_is_ordinary_repair(self):
        self.begin()
        self.assertTrue(self.candidate()['ok'])
        self.spawn(aid='first')
        self.review(False, aid='first')
        self.event('Stop')
        self.assertTrue(self.candidate()['ok'])
        self.spawn(aid='second')
        self.review(False, aid='second', findings=['Layout overlaps the footer on narrow screens'])
        self.assertFalse(self.state()['needs_replan'])

    def test_similarity_uses_words_not_exact_text(self):
        state = {}
        self.assertFalse(repeated_review_rejection(state, 'Reviewer rejected: The real test is missing for parser'))
        self.assertTrue(repeated_review_rejection(state, 'reviewer rejected: real test missing for the parser'))
        self.assertFalse(repeated_review_rejection(state, 'Reviewer rejected: footer overlaps on mobile screens'))

    def test_structured_success_wins_over_failure_words(self):
        state = {'status': 'working'}
        for _ in range(6):
            self.assertIsNone(observe_tool_loop(state, {'tool_name': 'exec_command',
                'tool_input': {'cmd': 'pytest'},
                'tool_response': {'exit_code': 0, 'output': '0 failed, 12 passed; error cases covered'}}))

    def test_success_rearms_warnings_after_recurrence(self):
        state = {'status': 'working'}
        ev = {'tool_name': 'exec_command', 'tool_input': {'cmd': 'pytest'},
              'tool_response': {'exit_code': 1, 'output': 'FAILED test_x'}}
        for _ in range(2):
            warnings = [bool(observe_tool_loop(state, ev)) for _ in range(5)]
            self.assertEqual(warnings, [False, False, True, False, True])
            self.assertIsNone(observe_tool_loop(state, {**ev, 'tool_response': {'exit_code': 0}}))

    def test_unknown_and_running_results_do_not_clear_failures(self):
        state = {'status': 'working'}
        ev = {'tool_name': 'exec_command', 'tool_input': {'cmd': 'pytest'},
              'tool_response': {'exit_code': 2, 'output': 'invalid input'}}
        observe_tool_loop(state, ev); observe_tool_loop(state, ev)
        self.assertIsNone(observe_tool_loop(state, {**ev, 'tool_response': {'session_id': 123, 'output': 'error'}}))
        self.assertIsNone(observe_tool_loop(state, {**ev, 'tool_response': 'read source containing error'}))
        self.assertIsNotNone(observe_tool_loop(state, ev))

    def test_duplicate_host_event_and_long_command_identity(self):
        state = {'status': 'working'}
        ev = {'tool_name': 'exec_command', 'tool_use_id': 'same',
              'tool_input': {'cmd': 'pytest'}, 'tool_response': {'exit_code': 1, 'output': 'bad'}}
        for _ in range(5): self.assertIsNone(observe_tool_loop(state, ev))
        for i in range(6):
            self.assertIsNone(observe_tool_loop(state, {**ev, 'tool_use_id': str(i),
                'tool_input': {'cmd': 'x' * 1600 + str(i)}}))

    def test_structured_failure_envelope_noise_is_ignored(self):
        state = {'status': 'working'}
        warnings = []
        for i in range(3):
            warnings.append(bool(observe_tool_loop(state, {'tool_name': 'exec_command',
                'tool_input': {'cmd': 'pytest'}, 'tool_response': {'exit_code': 1,
                    'output': 'FAILED test_1', 'chunk_id': str(i), 'wall_time_seconds': i}})))
        self.assertEqual(warnings, [False, False, True])

    def assert_repeated_finding(self, finding):
        self.settled()
        for attempt in range(2):
            self.assertTrue(self.candidate()['ok'])
            self.spawn(aid='r' + str(attempt))
            self.review(False, aid='r' + str(attempt), findings=[finding])
        self.assertTrue(self.state()['needs_replan'])

    def test_review_test_failure_repetition(self):
        self.assert_repeated_finding('test failure in parser')

    def test_review_layout_repetition(self):
        self.assert_repeated_finding('layout overlap in footer')

    def test_review_hash_repetition(self):
        self.assert_repeated_finding('hash changed unexpectedly')

    def test_wait_does_not_bank_stagnation_or_consume_loop_hits(self):
        self.settled()
        state = self.state()
        state.update(stagnation=2, churn=2, ladder_stage=0, loop_hits=1)
        state['children']['busy'] = {'status': 'running', 'role': 'adhd-scout'}
        self.save(state)
        for _ in range(2): self.event('Stop')
        after = self.state()
        for key in ('stagnation', 'churn', 'ladder_stage', 'loop_hits'):
            self.assertEqual(after[key], state[key])
        after['children']['busy']['status'] = 'finished'; self.save(after)
        self.event('Stop')
        self.assertEqual(self.state()['stagnation'], 4)

    def test_new_run_and_resume_reset_transient_loop_state(self):
        self.settled()
        state = self.state()
        state.update(ladder_stage=3, churn=9, loop_hits=1, fp_ring=[['a', 'b']], loop_warned={'b': 2})
        self.save(state)
        self.request('pause', {})
        self.event('UserPromptSubmit', prompt='resume')
        after = self.state()
        for key in ('ladder_stage', 'churn', 'loop_hits'): self.assertEqual(after.get(key, 0), 0)

    def test_stuck_note_redacts_secrets(self):
        self.settled()
        state = self.state(); state['stagnation'] = 2
        state['failure_history'] = [{'category': 'test_failure', 'detail': 'token=secretvalue failed'}]
        self.save(state); self.event('Stop')
        note = (bridge(self.ws, self.key) / 'stuck.md').read_text(encoding='utf-8')
        self.assertNotIn('secretvalue', note)

    def test_command_credentials_are_redacted_before_diagnostics_are_stored(self):
        commands = [('check --token plain_dummy_value', 'plain_dummy_value'),
                    ('check --client-secret "quoted dummy value"', 'quoted dummy value'),
                    ('check --api-key=equal_dummy_value', 'equal_dummy_value'),
                    ("check --password 'safe''ps_single_tail'", 'ps_single_tail'),
                    ('check --token "safe""ps_double_tail"', 'ps_double_tail'),
                    ('check --token "safe`"ps_escaped_tail"', 'ps_escaped_tail'),
                    ('check --token safe` ps_unquoted_tail', 'ps_unquoted_tail')]
        for command, secret in commands:
            with self.subTest(command=command):
                state = {'status': 'working'}
                event = {'tool_name': 'exec_command',
                         'tool_input': {'cmd': command, 'access_token': 'json_dummy_value'},
                         'tool_response': {'session_id': 123}}
                observe_tool_loop(state, event)
                self.assertNotIn(secret, json.dumps(state))  # Pending commands are persisted too.
                for _ in range(3):
                    observe_tool_loop(state, {**event, 'tool_response': {'exit_code': 1, 'output': 'failed check'}})
                stored = json.dumps(state)
                for value in (secret, 'json_dummy_value'):
                    self.assertNotIn(value, stored)
                self.assertIn('REDACTED', stored)

    def test_stuck_note_redacts_separated_flags_in_all_diagnostic_sources(self):
        self.settled()
        state = self.state(); state['stagnation'] = 2
        state['failure_history'] = [{'category': 'test_failure', 'detail': "check --password 'failure dummy value' --token 'safe''history_tail'"}]
        state['loop_notes'] = [{'tool': 'exec_command', 'n': 3, 'cmd': 'check --token loop_dummy_value --secret "safe`"note_tail"'}]
        state['review_findings'] = ['retry --client-secret=review_dummy_value --password safe` review_tail']
        self.save(state); self.event('Stop')
        note = (bridge(self.ws, self.key) / 'stuck.md').read_text(encoding='utf-8')
        for secret in ('failure dummy value', 'loop_dummy_value', 'review_dummy_value',
                       'history_tail', 'note_tail', 'review_tail'):
            self.assertNotIn(secret, note)
        self.assertIn('REDACTED', note)

    def test_background_completion_is_attributed_to_original_command(self):
        state = {'status': 'working'}
        for sid in range(3):
            observe_tool_loop(state, {'tool_name': 'exec_command', 'tool_input': {'cmd': 'pytest'},
                'tool_response': {'session_id': sid, 'output': ''}})
            out = observe_tool_loop(state, {'tool_name': 'write_stdin',
                'tool_input': {'session_id': sid, 'chars': ''},
                'tool_response': {'exit_code': 1, 'output': 'FAILED test_x'}})
        self.assertIsNotNone(out)

    def test_different_paths_and_contract_revisions_do_not_repeat(self):
        state = {'run_id': 'run', 'intent_version': 1}
        self.assertFalse(repeated_review_rejection(state, 'src/one.py is missing a parser boundary test'))
        self.assertFalse(repeated_review_rejection(state, 'src/two.py is missing a parser boundary test'))
        state['intent_version'] = 2
        self.assertFalse(repeated_review_rejection(state, 'src/two.py is missing a parser boundary test'))

    def test_malformed_review_is_not_a_finding(self):
        self.settled(); self.assertTrue(self.candidate()['ok']); self.spawn()
        self.event('SubagentStop', agent_type='adhd-verifier', agent_id='child', last_assistant_message='{bad json')
        self.assertFalse(self.state().get('review_findings'))

    def test_reject_without_required_results_never_records_repetition(self):
        self.settled()
        for attempt in range(2):
            self.assertTrue(self.candidate()['ok']); self.spawn(aid=str(attempt))
            state = self.state()
            verdict = {'verdict': 'reject', 'reviewed_digest': state['candidate']['digest'],
                       'reviewed_contract_hash': state['contract_hash'],
                       'reviewed_turn_ids': [p['turn_id'] for p in state['prompts']],
                       'intent_alignment': True, 'findings': ['Parser lacks a boundary check']}
            self.event('SubagentStop', agent_type='adhd-verifier', agent_id=str(attempt),
                       last_assistant_message=json.dumps(verdict))
            self.assertFalse(self.state().get('review_findings'))
            self.assertFalse(self.state()['needs_replan'])

    def test_reject_with_changed_evidence_bindings_is_not_a_finding(self):
        self.settled()
        for field, value in (('evidence_ids', []), ('observation_ids', ['invented'])):
            with self.subTest(field=field):
                for attempt in range(2):
                    self.assertTrue(self.candidate()['ok']); self.spawn(aid=field + str(attempt))
                    rows = deepcopy(self.state()['candidate']['criterion_results'])
                    rows[0]['pass'] = False; rows[0][field] = value
                    self.review(False, aid=field + str(attempt), criterion_results=rows)
                    self.assertFalse(self.state().get('review_findings'))
                    self.assertFalse(self.state()['needs_replan'])

    def test_reject_requires_complete_boolean_results_and_concrete_evidence(self):
        self.settled()
        for defect in ('missing', 'boolean', 'evidence', 'unknown'):
            with self.subTest(defect=defect):
                self.assertTrue(self.candidate()['ok']); self.spawn(aid=defect)
                rows = deepcopy(self.state()['candidate']['criterion_results'])
                rows[0]['pass'] = False
                if defect == 'missing': rows = []
                elif defect == 'boolean': rows[0]['pass'] = 0
                elif defect == 'evidence': rows[0]['evidence'] = ''
                else: rows[0]['id'] = 'unbound'
                self.review(False, aid=defect, criterion_results=rows)
                self.assertFalse(self.state().get('review_findings'))
                self.assertFalse(self.state()['needs_replan'])

    def test_valid_false_result_rejection_can_request_replanning(self):
        self.settled()
        for attempt in range(2):
            self.assertTrue(self.candidate()['ok']); self.spawn(aid=str(attempt))
            rows = deepcopy(self.state()['candidate']['criterion_results']); rows[0]['pass'] = False
            self.review(False, aid=str(attempt), criterion_results=rows)
        self.assertTrue(self.state()['needs_replan'])

    def test_reviewer_wait_freezes_accounting(self):
        self.settled(); self.assertTrue(self.candidate()['ok'])
        state = self.state(); state.update(stagnation=2, churn=2, loop_hits=1, ladder_stage=0)
        self.save(state)
        for _ in range(3): self.event('Stop')
        after = self.state()
        for key in ('stagnation', 'churn', 'loop_hits', 'ladder_stage'):
            self.assertEqual(after[key], state[key])

    def test_stuck_write_failure_keeps_hook_alive(self):
        from unittest.mock import patch
        self.settled(); state = self.state(); state['stagnation'] = 2; self.save(state)
        with patch('adhd.loop_guard.atomic_text', side_effect=OSError('read only')):
            result = self.event('Stop')
        self.assertEqual(result['decision'], 'block')
        self.assertTrue(self.state()['needs_replan'])
        self.assertNotIn('stuck.md', result['reason'])

    def test_new_run_does_not_inherit_loop_diagnostics(self):
        self.settled(); state = self.state()
        state.update(loop_hits=1, ladder_stage=3, churn=8, fp_ring=[['a', 'b']],
                     review_findings=[{'text': 'old finding', 'revision': ['old', 1], 'keys': []}])
        self.save(state); self.request('pause', {})
        self.event('UserPromptSubmit', prompt='Create a new independent task with different requirements')
        turn = str(self.counter)
        result = self.request('sync-intent', {'source_turn_id': turn,
            'base_revision': self.state()['intent_version'], 'classification': 'new_task'})
        self.assertTrue(result['ok'], result)
        self.begin()
        state = self.state()
        for key in ('loop_hits', 'ladder_stage', 'churn'): self.assertEqual(state.get(key, 0), 0)
        self.assertFalse(state.get('review_findings')); self.assertFalse(state.get('fp_ring'))

    def test_structured_error_and_rg_no_match(self):
        state = {'status': 'working'}
        for _ in range(4):
            self.assertIsNone(observe_tool_loop(state, {'tool_name': 'exec_command',
                'tool_input': {'cmd': 'rg absent file.txt'}, 'tool_response': {'exit_code': 1, 'output': ''}}))
        for _ in range(3):
            out = observe_tool_loop(state, {'tool_name': 'apply_patch', 'tool_input': {'patch': 'same'},
                'tool_response': {'isError': True, 'content': [{'type': 'text', 'text': 'hunk mismatch'}]}})
        self.assertIsNotNone(out)

    def test_benign_rg_and_non_string_command_inputs_do_not_crash(self):
        state = {'status': 'working'}
        for output in (None, [], ''):
            for _ in range(4):
                self.assertIsNone(observe_tool_loop(state, {'tool_name': 'exec_command',
                    'tool_input': {'cmd': 'rg absent file.txt'},
                    'tool_output': {'exitCode': 1, 'output': output}}))
        self.assertFalse(state.get('fp_ring'))
        self.assertIsNone(observe_tool_loop(state, {'tool_name': 'exec_command',
            'tool_input': {'cmd': None}, 'tool_response': {'exit_code': 1, 'output': []}}))


if __name__ == '__main__':
    unittest.main()
