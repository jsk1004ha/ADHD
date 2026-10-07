"""Goal execution continues until verified acceptance, with explicit limits intact."""
from __future__ import annotations

import json
import unittest

from adhd.core import atomic_json, store
from adhd.native import DEFAULTS, lease_path
from tests import test_native as fixtures


class NativeGoalTests(unittest.TestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown
    event = fixtures.NativeTests.event
    state = fixtures.NativeTests.state
    save = fixtures.NativeTests.save
    request = fixtures.NativeTests.request
    candidate = fixtures.NativeTests.candidate
    spawn = fixtures.NativeTests.spawn
    review = fixtures.NativeTests.review
    installed_verifier_profile = fixtures.NativeTests.installed_verifier_profile
    observed_verifier_start = fixtures.NativeTests.observed_verifier_start

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
