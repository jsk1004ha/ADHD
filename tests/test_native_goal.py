"""Goal execution continues until verified acceptance, with explicit limits intact."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from adhd.core import atomic_json, read_json, store
from adhd.native import DEFAULTS, bridge, lease_path
from tests import test_native as fixtures


class NativeGoalTests(unittest.TestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown
    event = fixtures.NativeTests.event
    state = fixtures.NativeTests.state
    save = fixtures.NativeTests.save
    candidate = fixtures.NativeTests.candidate
    spawn = fixtures.NativeTests.spawn
    review = fixtures.NativeTests.review
    installed_verifier_profile = fixtures.NativeTests.installed_verifier_profile
    observed_verifier_start = fixtures.NativeTests.observed_verifier_start

    def request(self, op, payload):
        with patch('adhd.native_install._find_managed_installation',
                   return_value=(None, None, {'release': str(fixtures.ROOT.resolve())})):
            return fixtures.NativeTests.request(self, op, payload)

    def host_transcript(self, status='active', objective='Make answer.txt pass the real checks', **overrides):
        path = self.ch / 'sessions' / 'goal.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {'type': 'session_meta', 'payload': {'id': self.sid, 'cwd': str(self.ws)}}
        goal = {'threadId': self.sid, 'objective': objective, 'status': status, 'createdAt': 1}
        payload = {'type': 'thread_goal_updated', 'threadId': self.sid,
                   'goal': goal if status is not None else None}
        payload.update(overrides)
        if not path.exists():
            path.write_text(json.dumps(header) + '\n', encoding='utf-8')
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': payload}) + '\n')
        return str(path)

    def begin_host_goal(self):
        transcript = self.host_transcript()
        out = self.event('SessionStart', transcript_path=transcript)
        self.assertIn('Goal execution requested', out['hookSpecificOutput']['additionalContext'])
        result = self.request('begin', {'mode': 'coding', 'criteria': [
            {'id': 'R1', 'text': 'Requested behavior passes', 'kind': 'test'}],
            'artifacts': ['answer.txt'], 'plan': {'objective': 'Meet host goal',
                'approach': 'Work and check', 'verification': 'Run actual checks',
                'steps': [{'id': 'S1', 'action': 'Implement and verify',
                    'requirements': ['R1'], 'depends_on': []}]}})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.state()['loop_mode'], 'goal')
        return transcript

    def test_builtin_goal_binds_without_a_user_prompt_hook(self):
        state = self.state()
        state['prompts'] = []
        state['host_capabilities']['observed_events'] = []
        self.save(state)
        self.begin_host_goal()
        state = self.state()
        self.assertEqual(state['prompts'][0]['text'], 'Make answer.txt pass the real checks')
        self.assertEqual(state['prompts'][0]['source'], 'host_thread_goal')
        self.assertNotIn('UserPromptSubmit', state['host_capabilities']['observed_events'])

    def test_real_host_header_can_include_long_base_instructions(self):
        from pathlib import Path
        path = Path(self.host_transcript())
        rows = path.read_text(encoding='utf-8').splitlines()
        header = json.loads(rows[0])
        header['payload']['base_instructions'] = {'text': 'x' * 30000}
        path.write_text(json.dumps(header) + '\n' + rows[1] + '\n', encoding='utf-8')
        self.event('SessionStart', transcript_path=str(path))
        self.assertEqual(self.state()['goal_request']['origin'], 'host_thread_goal')

    def test_installed_hooks_route_real_exec_and_wait_tools(self):
        import re
        from adhd.native_install import install_native
        installed = install_native(self.ch, self.ch, fixture_mode=True)
        hooks = json.loads((self.ch / 'hooks.json').read_text(encoding='utf-8'))
        groups = [group for group in hooks['hooks']['PostToolUse']
                  if any(installed['release'] in row.get('command', '') for row in group['hooks'])]
        self.assertEqual(len(groups), 1)
        for tool in ['exec', 'wait', 'functions.exec', 'functions.wait', 'exec_command', 'write_stdin', 'Bash']:
            with self.subTest(tool=tool):
                self.assertIsNotNone(re.search(groups[0]['matcher'], tool))

    def test_repeated_host_observation_does_not_add_turns_or_reset_limits(self):
        transcript = self.begin_host_goal()
        state = self.state()
        for _ in range(3):
            self.event('PostToolUse', tool_name='Bash', transcript_path=transcript)
        after = self.state()
        for key in ['prompts', 'pending_turn_ids', 'epoch', 'started', 'rounds']:
            self.assertEqual(after[key], state[key])

    def test_host_goal_pause_and_resume_keep_contract_and_policy(self):
        self.begin_host_goal()
        before = self.state()
        self.event('Stop', transcript_path=self.host_transcript('paused'))
        self.assertEqual(self.state()['status'], 'paused')
        self.event('PostToolUse', tool_name='Bash', transcript_path=self.host_transcript())
        after = self.state()
        self.assertEqual(after['status'], 'working')
        self.assertEqual(after['contract_hash'], before['contract_hash'])
        self.assertEqual(after['policy'], before['policy'])
        self.assertEqual(after['epoch'], before['epoch'] + 1)

    def test_host_resume_respects_explicit_lifetime_limit(self):
        atomic_json(store() / 'native-policy.json', {'max_epochs': 1})
        self.begin_host_goal()
        self.event('Stop', transcript_path=self.host_transcript('paused'))
        self.event('PostToolUse', tool_name='Bash', transcript_path=self.host_transcript())
        self.assertEqual(self.state()['status'], 'paused')
        self.assertIn('Lifetime', self.state()['feedback'])

    def test_host_resume_waits_for_old_children_and_preserves_signal(self):
        self.begin_host_goal()
        self.spawn(role='adhd-scout', aid='reader')
        epoch = self.state()['epoch']
        self.event('Stop', transcript_path=self.host_transcript('paused'))
        self.event('PostToolUse', tool_name='Bash', transcript_path=self.host_transcript())
        self.assertEqual(self.state()['status'], 'interrupt_pending')
        self.assertEqual(self.state()['epoch'], epoch)
        self.event('SubagentStop', agent_id='reader', agent_type='adhd-scout', last_assistant_message='Stopped')
        self.assertEqual(self.state()['status'], 'working')
        self.assertEqual(self.state()['epoch'], epoch + 1)
        self.assertTrue(lease_path(self.ws).exists())

    def test_human_cancel_overrides_a_pending_host_resume(self):
        self.begin_host_goal()
        self.spawn(role='adhd-scout', aid='reader')
        self.event('Stop', transcript_path=self.host_transcript('paused'))
        self.event('PostToolUse', tool_name='Bash', transcript_path=self.host_transcript())
        self.event('UserPromptSubmit', prompt='중단')
        self.event('SubagentStop', agent_id='reader', agent_type='adhd-scout', last_assistant_message='Stopped')
        self.assertEqual(self.state()['status'], 'cancelled')

    def assert_session_halt_clears_host_resume(self, event):
        self.begin_host_goal()
        self.spawn(role='adhd-scout', aid='reader')
        self.event('Stop', transcript_path=self.host_transcript('paused'))
        self.event('PostToolUse', tool_name='Bash', transcript_path=self.host_transcript())
        self.event(event)
        self.event('SubagentStop', agent_id='reader', agent_type='adhd-scout', last_assistant_message='Stopped')
        self.assertEqual(self.state()['status'], 'paused')
        self.assertFalse(lease_path(self.ws).exists())

    def test_session_end_cancels_a_pending_host_resume(self):
        self.assert_session_halt_clears_host_resume('SessionEnd')

    def test_interrupt_cancels_a_pending_host_resume(self):
        self.assert_session_halt_clears_host_resume('Interrupt')

    def test_long_transcript_cannot_hide_a_pause_event(self):
        from pathlib import Path
        from adhd.host_capabilities import GOAL_TRANSCRIPT_TAIL
        self.begin_host_goal()
        path = Path(self.host_transcript('paused'))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {'text': 'x' * (GOAL_TRANSCRIPT_TAIL + 128)}}) + '\n')
        self.event('Stop', transcript_path=str(path))
        self.assertEqual(self.state()['status'], 'paused')
        self.assertTrue(self.state()['host_goal_scan_pending'])
        for _ in range(3):
            self.event('PostToolUse', tool_name='Bash', transcript_path=str(path))
            self.assertEqual(self.state()['status'], 'paused')
            if not self.state()['host_goal_scan_pending']:
                break
        self.assertFalse(self.state()['host_goal_scan_pending'])
        self.assertEqual(self.state()['host_goal_observation']['goal']['status'], 'paused')

    def test_host_goal_excludes_unneeded_fields_and_public_cursor(self):
        from pathlib import Path
        self.begin_host_goal()
        before = self.state()
        path = Path(self.host_transcript())
        row = json.loads(path.read_text(encoding='utf-8').splitlines()[-1])
        row['payload']['goal']['future_private_metadata'] = {'value': 'unneeded'}
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')
        self.event('PostToolUse', tool_name='Bash', transcript_path=str(path))
        after = self.state()
        self.assertEqual(after['host_goal_observation'], before['host_goal_observation'])
        self.assertNotIn('path', after['host_goal_observation'])
        view = json.loads((self.ws / '.adhd' / 'bridge' / after['key'] / 'view.json').read_text(encoding='utf-8'))
        self.assertNotIn('host_goal_cursor', view)

    def test_host_complete_cannot_self_approve_and_clear_retains_work(self):
        for status in ['complete', None]:
            with self.subTest(status=status):
                if self.state()['status'] != 'working':
                    self.event('UserPromptSubmit', prompt='resume')
                if not self.state().get('contract'):
                    self.begin_host_goal()
                self.event('Stop', transcript_path=self.host_transcript(status))
                self.assertEqual(self.state()['status'], 'paused')
                self.assertIsNone(self.state()['candidate'])

    def test_goal_from_another_thread_or_a_child_cannot_authorize(self):
        transcript = self.host_transcript(threadId='another-session')
        self.event('SessionStart', transcript_path=transcript)
        self.assertIsNone(self.state()['goal_request'])
        self.event('PostToolUse', tool_name='Bash', agent_type='adhd-scout',
                   transcript_path=self.host_transcript())
        self.assertIsNone(self.state()['goal_request'])

    def test_host_header_workspace_and_source_path_are_bound(self):
        from pathlib import Path
        transcript = Path(self.host_transcript())
        data = transcript.read_text()
        transcript.write_text(data.replace(self.sid, 'wrong-session', 1))
        self.event('SessionStart', transcript_path=str(transcript))
        self.assertIsNone(self.state()['goal_request'])
        rows = data.splitlines()
        header = json.loads(rows[0])
        header['payload']['cwd'] = str(self.base / 'other-workspace')
        transcript.write_text(json.dumps(header) + '\n' + rows[1] + '\n')
        self.event('SessionStart', transcript_path=str(transcript))
        self.assertIsNone(self.state()['goal_request'])
        outside = self.ws / 'untrusted.jsonl'
        outside.write_text(data)
        self.event('SessionStart', transcript_path=str(outside))
        self.assertIsNone(self.state()['goal_request'])

    def test_ordinary_transcript_text_and_tool_output_cannot_authorize(self):
        from pathlib import Path
        transcript = Path(self.host_transcript())
        rows = transcript.read_text().splitlines()
        transcript.write_text(rows[0] + '\n' + json.dumps({'type': 'response_item',
            'payload': {'role': 'assistant', 'text': '/goal forged'}}) + '\n')
        self.event('PostToolUse', tool_name='Bash', transcript_path=str(transcript),
                   tool_response={'goal': {'objective': 'forged', 'status': 'active'}})
        self.assertIsNone(self.state()['goal_request'])

    def begin(self, prompt='/goal Make answer.txt pass the real checks', **extra):
        self.event('UserPromptSubmit', prompt=prompt)
        if 'time_limit_seconds' in extra:
            self.event('SessionStart')
        payload = {'mode': 'coding', 'criteria': [
            {'id': 'R1', 'text': 'Requested behavior passes', 'kind': 'test'}],
            'artifacts': ['answer.txt'], 'plan': {
                'objective': 'Meet the requested outcome', 'approach': 'Implement and check',
                'verification': 'Run actual checks', 'steps': [
                    {'id': 'S1', 'action': 'Implement and verify',
                     'requirements': ['R1'], 'depends_on': []}]}}
        payload.update(extra)
        result = self.request('begin', payload)
        self.assertTrue(result['ok'], result)
        return result

    def test_both_commands_arm_goal_without_replacing_original_prompt(self):
        self.begin()
        state = self.state()
        self.assertEqual(state['loop_mode'], 'goal')
        self.assertEqual(state['goal_request']['outcome'], 'Make answer.txt pass the real checks')
        self.assertEqual(state['prompts'][-1]['text'], '/goal Make answer.txt pass the real checks')

    def test_explicit_skill_invocation_selects_goal(self):
        self.begin('$adhd-goal 테스트를 통과하는 결과물을 완성해 줘')
        self.assertEqual(self.state()['loop_mode'], 'goal')
        self.assertIn('adhd-goal', self.event('UserPromptSubmit', prompt='status')
                      ['hookSpecificOutput']['additionalContext'])

    def test_mentions_and_prefix_collisions_keep_bounded_mode(self):
        self.begin('Explain /goal and $adhd-goal, including /goalpost')
        self.assertEqual(self.state()['loop_mode'], 'bounded')

    def test_payload_cannot_invent_goal_authorization(self):
        result = self.request('begin', {'loop_mode': 'goal', 'mode': 'coding',
            'criteria': [{'id': 'R1', 'text': 'Behavior works', 'kind': 'behavior'}],
            'artifacts': ['answer.txt']})
        self.assertFalse(result['ok'])
        self.assertEqual(self.state()['status'], 'idle')

    def test_payload_cannot_downgrade_explicit_goal(self):
        self.event('UserPromptSubmit', prompt='/goal Finish the requested output')
        result = self.request('begin', {'loop_mode': 'bounded'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.state()['status'], 'idle')

    def test_goalpost_is_an_ordinary_prompt(self):
        self.begin('/goalpost Describe the current result')
        self.assertEqual(self.state()['loop_mode'], 'bounded')

    def test_goal_continues_past_implicit_limits_and_after_compaction(self):
        self.begin()
        state = self.state()
        state['started'] -= DEFAULTS['max_seconds'] + 1
        self.save(state)
        for _ in range(DEFAULTS['max_lifetime_rounds'] + 2):
            self.assertEqual(self.event('Stop')['decision'], 'block')
        self.event('PostCompact', trigger='auto')
        self.assertEqual(self.state()['loop_mode'], 'goal')
        self.assertEqual(self.state()['status'], 'working')
        self.assertGreater(self.state()['stagnation'], DEFAULTS['max_stagnation'])
        self.assertIn('hypothesis', self.event('Stop')['reason'])

    def test_explicit_local_round_limit_still_ends_goal(self):
        atomic_json(store() / 'native-policy.json', {'max_rounds': 1})
        self.begin()
        self.assertEqual(self.event('Stop')['decision'], 'block')
        self.assertFalse(self.event('Stop')['continue'])
        self.assertEqual(self.state()['status'], 'budget_exhausted')

    def test_explicit_time_limit_still_ends_goal(self):
        atomic_json(store() / 'native-policy.json', {'max_seconds': 60})
        self.begin()
        state = self.state()
        state['started'] -= 61
        self.save(state)
        self.assertFalse(self.event('Stop')['continue'])

    def test_opt_in_goal_time_limit_expires_without_acceptance(self):
        self.begin(time_limit_seconds=10, review_reserve_seconds=2)
        budget = self.state()['time_budget']
        self.assertAlmostEqual(budget['expires_at'] - budget['began_at'], 10)
        with patch('adhd.native.time.time', return_value=budget['expires_at'] + 1):
            result = self.event('Stop')
        self.assertFalse(result['continue'])
        self.assertEqual(self.state()['status'], 'budget_exhausted')
        self.assertNotIn('review_receipt', self.state())
        self.assertFalse(lease_path(self.ws).exists())

    def test_bounded_budget_denies_new_tool_at_expiry(self):
        self.begin('Make answer.txt pass the real checks', time_limit_seconds=10)
        budget = self.state()['time_budget']
        with patch('adhd.native.time.time', return_value=budget['expires_at'] + 1):
            result = self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'echo late'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.state()['status'], 'budget_exhausted')
        repeated = self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'echo still late'})
        self.assertEqual(repeated['hookSpecificOutput']['permissionDecision'], 'deny')
        cleanup = self.event('PreToolUse', tool_name='interrupt_agent', tool_input={'target': 'late'})
        self.assertNotEqual(cleanup.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        late_spawn = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='too-late',
            tool_input={'agent_type': 'adhd-scout', 'message': 'Start more work'})
        self.assertEqual(late_spawn['hookSpecificOutput']['permissionDecision'], 'deny')
        self.event('UserPromptSubmit', prompt='중단')
        still_denied = self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'echo after cancel'})
        self.assertEqual(still_denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertFalse(self.event('Stop')['continue'])

    def test_explicit_bounded_budget_replaces_only_implicit_seconds(self):
        self.begin('Make answer.txt pass the real checks', time_limit_seconds=7200)
        state = self.state()
        self.assertEqual(state['time_budget']['reserve_seconds'], 300)
        view = read_json(bridge(self.ws, self.key) / 'view.json')
        self.assertIn('phase=work', view['time_budget_status'])
        context = self.event('SessionStart')['hookSpecificOutput']['additionalContext']
        self.assertIn('Time budget:', context)
        state['started'] -= DEFAULTS['max_seconds'] + 1
        self.save(state)
        self.assertEqual(self.event('Stop')['decision'], 'block')
        self.assertEqual(self.state()['status'], 'working')

    def test_new_user_request_can_reconcile_after_expired_contract(self):
        self.begin(time_limit_seconds=10)
        budget = self.state()['time_budget']
        with patch('adhd.native.time.time', return_value=budget['expires_at'] + 1):
            self.event('Stop')
        self.event('UserPromptSubmit', prompt='Create a different requested artifact')
        result = self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'classify new request'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        spawn = self.event('PreToolUse', tool_name='spawn_agent',
            tool_input={'agent_type': 'adhd-scout', 'message': 'More work'})
        self.assertEqual(spawn['hookSpecificOutput']['permissionDecision'], 'deny')
        state = self.state()
        self.assertEqual(state['status'], 'budget_exhausted')
        command = (f'& "{fixtures.sys.executable}" "{fixtures.ROOT / "adhd.py"}" native sync-intent '
            f'--session {self.key} --workspace "{self.ws}" --classification new_task '
            f'--source-turn-id {state["pending_turn_ids"][-1]} --base-revision {state["intent_version"]}')
        reader = state['session_reader']
        allowed = self.event('PreToolUse', tool_name='Bash', tool_input={'command': command})
        self.assertNotEqual(allowed.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        wrapped = 'text(await tools.exec_command(' + json.dumps({'cmd': command}) + '));'
        allowed = self.event('PreToolUse', tool_name='functions.exec', tool_input=wrapped)
        self.assertNotEqual(allowed.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        for unsafe in (command + '; echo work', command + '\n echo work',
                       command.replace('new_task', 'amend'), command.replace(self.key, 'wrong'),
                       command + ' --payload-file arbitrary.json'):
            with self.subTest(command=unsafe):
                denied = self.event('PreToolUse', tool_name='Bash', tool_input={'command': unsafe})
                self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        denied = self.event('PreToolUse', tool_name='functions.exec',
            tool_input=wrapped + ' text(await tools.exec_command({"cmd":"echo work"}));')
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertTrue(self.request('sync-intent', {'source_turn_id': state['pending_turn_ids'][-1],
            'base_revision': state['intent_version'], 'classification': 'new_task', 'operations': []})['ok'])
        self.assertEqual(self.state()['status'], 'idle')
        self.assertNotIn('time_budget', self.state())
        self.assertEqual(self.state()['session_reader'], reader)

    def test_expired_handoff_blocks_work_and_drains_late_child(self):
        self.begin(time_limit_seconds=60)
        self.spawn(role='adhd-scout', aid='old-reader')
        budget = self.state()['time_budget']
        with patch('adhd.native.time.time', return_value=budget['expires_at'] + 1):
            self.event('UserPromptSubmit', prompt='Create a separate new requested artifact')
            state = self.state()
            self.assertTrue(self.request('sync-intent', {'source_turn_id': state['pending_turn_ids'][-1],
                'base_revision': state['intent_version'], 'classification': 'new_task', 'operations': []})['ok'])
            self.assertEqual(self.state()['status'], 'handoff_pending')
            denied = self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'echo before drain'})
            self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
            self.event('SubagentStart', agent_type='adhd-scout', agent_id='late-reader', model='gpt-6-luna')
            self.assertEqual(self.state()['children']['late-reader']['status'], 'running')
            self.event('SubagentStop', agent_type='adhd-scout', agent_id='old-reader')
            self.assertEqual(self.state()['status'], 'handoff_pending')
            self.event('SubagentStop', agent_type='adhd-scout', agent_id='late-reader')
        self.assertEqual(self.state()['status'], 'idle')
        self.assertFalse(lease_path(self.ws).exists())

    def test_verifier_completion_after_expiry_stays_unfinished(self):
        self.begin(time_limit_seconds=60, review_reserve_seconds=12)
        self.assertTrue(self.candidate()['ok'])
        self.spawn()
        budget = self.state()['time_budget']
        with patch('adhd.native.time.time', return_value=budget['expires_at'] + 1):
            self.review()
        self.assertEqual(self.state()['status'], 'budget_exhausted')
        self.assertNotIn('review_receipt', self.state())
        self.assertFalse(lease_path(self.ws).exists())

    def test_late_observed_child_drains_after_expired_status(self):
        self.begin(time_limit_seconds=10)
        budget = self.state()['time_budget']
        with patch('adhd.native.time.time', return_value=budget['expires_at'] + 1):
            self.event('PostCompact')
            self.event('SubagentStart', agent_type='adhd-scout', agent_id='late', model='gpt-6-luna')
        state = self.state()
        self.assertEqual(state['status'], 'interrupt_pending')
        self.assertEqual(state['pending_terminal'], 'budget_exhausted')
        self.assertEqual(state['children']['late']['status'], 'running')
        self.event('SubagentStop', agent_type='adhd-scout', agent_id='late')
        self.assertEqual(self.state()['status'], 'budget_exhausted')
        self.assertFalse(lease_path(self.ws).exists())

    def test_review_reserve_blocks_work_spawn_but_allows_verifier(self):
        self.begin(time_limit_seconds=60, review_reserve_seconds=20)
        self.assertTrue(self.candidate()['ok'])
        budget = self.state()['time_budget']
        with patch('adhd.native.time.time', return_value=budget['expires_at'] - 10):
            work = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='work',
                tool_input={'agent_type': 'adhd-scout', 'message': 'Inspect current source'})
            review = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='review',
                tool_input={'agent_type': 'adhd-verifier', 'message': 'Review the current candidate'})
        self.assertEqual(work['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertNotEqual(review.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.assertNotIn('work', self.state()['reservations'])
        self.assertIn('review', self.state()['reservations'])

    def test_unreserved_child_observed_in_reserve_is_not_admitted(self):
        self.begin(time_limit_seconds=60, review_reserve_seconds=20)
        budget = self.state()['time_budget']
        with patch('adhd.native.time.time', return_value=budget['expires_at'] - 10):
            result = self.event('SubagentStart', agent_type='adhd-scout',
                agent_id='unreserved', model='gpt-6-luna')
        self.assertIn('reserved verification window', result['hookSpecificOutput']['additionalContext'])
        self.assertFalse(self.state()['children']['unreserved']['correlated'])

    def test_resume_keeps_original_expiry_and_cannot_restart_expired_goal(self):
        self.begin(time_limit_seconds=10, review_reserve_seconds=2)
        budget = self.state()['time_budget']
        self.assertTrue(self.request('pause', {})['ok'])
        with patch('adhd.native.time.time', return_value=budget['expires_at'] + 1):
            self.event('UserPromptSubmit', prompt='resume')
        after = self.state()
        self.assertEqual(after['status'], 'budget_exhausted')
        self.assertEqual(after['time_budget']['expires_at'], budget['expires_at'])
        self.assertFalse(lease_path(self.ws).exists())

    def test_backwards_clock_does_not_increase_remaining_time(self):
        self.begin(time_limit_seconds=60)
        budget = self.state()['time_budget']
        with patch('adhd.native.time.time', return_value=budget['began_at'] + 30):
            self.event('PostCompact')
        observed = self.state()['time_budget']['last_observed_at']
        with patch('adhd.native.time.time', return_value=budget['began_at'] + 10):
            self.event('PostCompact')
        self.assertEqual(self.state()['time_budget']['last_observed_at'], observed)

    def test_invalid_budget_inputs_leave_begin_idle(self):
        payload = {'mode': 'coding', 'criteria': [
            {'id': 'R1', 'text': 'Requested behavior passes', 'kind': 'test'}],
            'artifacts': ['answer.txt'], 'plan': {'objective': 'Meet the requested outcome',
                'approach': 'Implement and check', 'verification': 'Run actual checks',
                'steps': [{'id': 'S1', 'action': 'Implement and verify',
                    'requirements': ['R1'], 'depends_on': []}]}}
        self.event('UserPromptSubmit', prompt='/goal Make answer.txt pass the real checks')
        self.event('SessionStart')
        for extra in ({'review_reserve_seconds': 1}, {'time_limit_seconds': True},
                      {'time_limit_seconds': 0}, {'time_limit_seconds': float('inf')},
                      {'time_limit_seconds': 10, 'review_reserve_seconds': 10}):
            with self.subTest(extra=extra):
                self.assertFalse(self.request('begin', {**payload, **extra})['ok'])
                self.assertEqual(self.state()['status'], 'idle')
                self.assertNotIn('time_budget', self.state())

    def test_legacy_goal_has_no_new_budget_and_independent_gate_remains(self):
        self.begin()
        self.assertNotIn('time_budget', self.state())
        self.assertNotIn('time_budget_status', read_json(bridge(self.ws, self.key) / 'view.json'))
        self.assertTrue(self.candidate()['ok'])
        self.spawn()
        self.review()
        self.assertEqual(self.state()['status'], 'complete')

    def test_budgeted_goal_still_requires_independent_review(self):
        self.begin(time_limit_seconds=60, review_reserve_seconds=12)
        self.assertTrue(self.candidate()['ok'])
        self.assertEqual(self.state()['status'], 'reviewing')
        self.spawn()
        self.review()
        self.assertEqual(self.state()['status'], 'complete')

    def test_explicit_stagnation_limit_still_ends_goal(self):
        atomic_json(store() / 'native-policy.json', {'max_stagnation': 2})
        self.begin()
        for _ in range(3):
            result = self.event('Stop')
        self.assertFalse(result['continue'])
        self.assertEqual(self.state()['status'], 'budget_exhausted')

    def test_duplicate_stop_does_not_increment_goal_rounds(self):
        self.begin()
        first = self.event('Stop', turn_id='same-stop')
        second = self.event('Stop', turn_id='same-stop')
        self.assertEqual(first, second)
        self.assertEqual(self.state()['rounds'], 1)

    def test_goal_success_needs_independent_review_and_reject_loops(self):
        self.begin()
        self.assertTrue(self.candidate()['ok'])
        self.assertEqual(self.event('Stop')['decision'], 'block')
        self.assertEqual(self.state()['status'], 'reviewing')
        self.spawn(aid='first-review')
        self.review(False, aid='first-review')
        self.assertEqual(self.state()['status'], 'revising')
        self.assertEqual(self.event('Stop')['decision'], 'block')
        self.assertTrue(self.candidate()['ok'])
        self.spawn(aid='second-review')
        self.review(aid='second-review')
        self.assertEqual(self.state()['status'], 'complete')
        self.assertFalse(self.event('Stop')['continue'])
        self.assertFalse(lease_path(self.ws).exists())

    def test_goal_can_review_beyond_implicit_child_call_limits(self):
        self.begin()
        self.assertTrue(self.candidate()['ok'])
        state = self.state()
        state['total_children'] = DEFAULTS['max_total_children']
        state['review_children'] = DEFAULTS['max_review_children']
        state['lifetime_children'] = DEFAULTS['max_lifetime_children']
        self.save(state)
        self.observed_verifier_start()
        self.review(aid='app-reviewer')
        self.assertEqual(self.state()['status'], 'complete')

    def test_goal_spawn_preserves_parallel_and_single_writer_guards(self):
        self.begin()
        self.spawn(role='adhd-implementer', aid='writer')
        result = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='second-writer',
            tool_input={'agent_type': 'adhd-implementer', 'message': 'Implement'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.spawn(role='adhd-scout', aid='reader-one')
        self.spawn(role='adhd-scout', aid='reader-two')
        result = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='fourth-child',
            tool_input={'agent_type': 'adhd-scout', 'message': 'Read'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_explicit_review_limit_is_preserved(self):
        atomic_json(store() / 'native-policy.json', {'max_review_children': 1})
        self.begin()
        self.assertTrue(self.candidate()['ok'])
        state = self.state()
        state['review_children'] = 1
        self.save(state)
        result = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='over-budget',
            tool_input={'agent_type': 'adhd-verifier', 'message': 'Review'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_cancel_drains_children_and_resume_preserves_goal(self):
        self.begin()
        self.spawn(role='adhd-scout', aid='reader')
        self.event('UserPromptSubmit', prompt='중단')
        self.assertEqual(self.state()['status'], 'interrupt_pending')
        self.assertTrue(lease_path(self.ws).exists())
        self.event('SubagentStop', agent_id='reader', agent_type='adhd-scout',
                   last_assistant_message='Stopped')
        self.assertEqual(self.state()['status'], 'cancelled')
        self.assertFalse(lease_path(self.ws).exists())
        self.event('UserPromptSubmit', prompt='재개')
        self.assertEqual(self.state()['status'], 'working')
        self.assertEqual(self.state()['loop_mode'], 'goal')

    def test_goal_can_activate_on_reconciled_existing_task(self):
        self.begin('Make answer.txt pass the real checks')
        self.event('UserPromptSubmit', prompt='/goal')
        state = self.state()
        turn = state['pending_turn_ids'][0]
        self.assertFalse(self.request('goal', {'source_turn_id': turn})['ok'])
        self.assertTrue(self.request('sync-intent', {'source_turn_id': turn,
            'base_revision': state['intent_version'], 'classification': 'no_change'})['ok'])
        self.assertTrue(self.request('goal', {'source_turn_id': turn})['ok'])
        self.assertEqual(self.state()['loop_mode'], 'goal')

    def test_block_and_session_end_halt_goal(self):
        self.begin()
        self.assertTrue(self.request('blocked', {'reason': 'Required input missing'})['ok'])
        self.assertFalse(self.event('Stop')['continue'])
        self.event('UserPromptSubmit', prompt='재개')
        self.event('SessionEnd')
        self.assertEqual(self.state()['status'], 'paused')


if __name__ == '__main__':
    unittest.main()
