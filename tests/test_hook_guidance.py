"""Output can be quiet while intent, inbox and restoration keep working."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from adhd.core import read_json
from adhd.models import execution_profile, route
from adhd.native import context, folder, handle_event, session_key, submit_request

ROOT = Path(__file__).resolve().parents[1]


class HookGuidanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ws = Path(temporary.name) / 'workspace'
        self.ws.mkdir()
        env = patch.dict(os.environ, {'CODEX_HOME': str(Path(temporary.name) / 'codex'),
                                     'ADHD_HOME': str(Path(temporary.name) / 'codex/adhd'),
                                     'ADHD_EXEC_OWNER': '', 'ADHD_HOOK_DIAGNOSTICS': ''})
        env.start()
        self.addCleanup(env.stop)
        self.sid = 'guidance-session'
        self.key = session_key(self.sid)
        self.turn = 0

    def event(self, name, **fields):
        self.turn += 1
        return handle_event({'hook_event_name': name, 'session_id': self.sid,
                             'cwd': str(self.ws), 'turn_id': str(self.turn), **fields})

    def state(self):
        return read_json(folder(self.key) / 'state.json')

    def text(self, out):
        return out.get('hookSpecificOutput', {}).get('additionalContext', '')

    def begin(self, profile='standard', plan=True):
        self.event('UserPromptSubmit', prompt='Implement result.txt and verify its contents.')
        payload = {'mode': 'coding', 'execution_profile': profile,
                   'criteria': [{'id': 'R1', 'text': 'Result must be verified', 'kind': 'test'}],
                   'artifacts': ['result.txt']}
        if plan:
            payload['plan'] = {'objective': 'Preserve result behavior', 'approach': 'Minimal edit',
                'verification': 'Run regression tests', 'alternatives': ['Keep existing processing'],
                'risks': ['Lost intent'], 'preflight': ['Read the source'],
                'steps': [{'id': 'S1', 'action': 'Implement and test', 'requirements': ['R1'], 'depends_on': []}]}
        request = submit_request(self.key, self.ws, 'begin', payload)
        self.event('PostToolUse', tool_name='exec_command')
        self.assertTrue(read_json(Path(request['receipt']))['ok'])

    def test_all_profiles_match_existing_dict_depth_and_role_pins(self):
        self.event('SessionStart', source='startup')
        for profile, expected in [('simple', 'optional'), ('standard', 'brief'), ('deep', 'deep')]:
            with self.subTest(profile=profile):
                state = self.state()
                state['execution_profile'] = execution_profile(profile)
                text = context(state)
                self.assertIn('Profile=' + profile, text)
                self.assertIn('plan=' + expected, text)
                if profile != 'deep':
                    self.assertNotIn('submit a deep plan', text)
        self.assertEqual(route('adhd-verifier')['model'], 'gpt-6-sol')
        self.assertEqual(route('adhd-verifier')['effort'], 'max')

    def test_startup_bootstrap_contains_rules_entrypoint_and_standard_brief(self):
        text = self.text(self.event('SessionStart', source='startup'))
        self.assertIn('adhd.py native begin', text)
        self.assertIn('view=', text)
        self.assertIn('plan=brief', text)
        self.assertIn('Preserve', text)
        self.assertLess(len(text), 1200)

    def test_ordinary_idle_prompts_are_quiet_but_preserved(self):
        self.event('SessionStart', source='startup')
        self.assertEqual(self.event('UserPromptSubmit', prompt='First original request.'), {})
        self.assertEqual(self.event('UserPromptSubmit', prompt='A substantive multi-step task.'), {})
        self.assertEqual([row['text'] for row in self.state()['prompts']],
                         ['First original request.', 'A substantive multi-step task.'])
        self.assertEqual(self.state()['intent_version'], 2)

    def test_missing_session_start_bootstraps_once_without_classifying_idle(self):
        first = self.text(self.event('UserPromptSubmit', prompt='Build a substantial complete system.'))
        self.assertIn('substantive', first)
        self.assertIn('adhd.py native begin', first)
        self.assertEqual(self.event('UserPromptSubmit', prompt='Include tests.'), {})
        self.assertEqual(len(self.state()['prompts']), 2)

    def test_idle_cli_begin_is_processed_after_quiet_prompt(self):
        self.event('SessionStart', source='startup')
        self.assertEqual(self.event('UserPromptSubmit', prompt='Implement a verified result.'), {})
        payload = {'mode': 'coding', 'execution_profile': 'simple',
                   'criteria': [{'id': 'R1', 'text': 'Verified result', 'kind': 'test'}],
                   'artifacts': ['result.txt']}
        (self.ws / 'begin.json').write_text(json.dumps(payload), encoding='utf-8')
        command = subprocess.run([sys.executable, str(ROOT / 'adhd.py'), 'native', 'begin',
            '--session', self.key, '--workspace', str(self.ws), '--payload-file', str(self.ws / 'begin.json')],
            capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(command.returncode, 0, command.stderr)
        queued = json.loads(command.stdout)
        self.assertEqual(self.state()['status'], 'idle')
        output = self.event('PostToolUse', tool_name='exec_command', tool_input={'cmd': 'native begin'})
        self.assertIn('Simple run armed', self.text(output))
        self.assertTrue(read_json(Path(queued['receipt']))['ok'])
        self.assertIn(queued['queued'], self.state()['processed'])
        self.assertTrue((self.ws / '.adhd/bridge' / self.key / 'processed' / (queued['queued'] + '.json')).exists())
        self.assertEqual(self.state()['status'], 'working')

    def test_amendment_announces_delta_and_retains_original_contract(self):
        self.begin()
        original = self.state()
        text = self.text(self.event('UserPromptSubmit', prompt='Also preserve the format.'))
        state = self.state()
        self.assertIn('sync-intent', text)
        self.assertNotIn('For small questions', text)
        self.assertEqual(state['contract_hash'], original['contract_hash'])
        self.assertEqual(state['prompts'][0], original['prompts'][0])
        self.assertEqual(state['prompts'][-1]['text'], 'Also preserve the format.')
        self.assertEqual(state['pending_turn_ids'], [str(self.turn)])
        self.assertLess(len(text), 850)

    def test_continuation_without_change_is_quiet_but_still_saves(self):
        self.begin()
        self.event('SessionStart', source='compact')
        with patch('adhd.native.persist', wraps=__import__('adhd.native', fromlist=['persist']).persist) as save:
            output = self.event('UserPromptSubmit', prompt='[ADHD_CONTINUE:' + self.key + '] continue')
        self.assertEqual(output, {})
        save.assert_called_once()
        self.assertEqual(self.state()['status'], 'working')

    def test_compact_resume_restore_current_objective_unmet_and_next_action(self):
        self.begin('deep')
        before = self.state()
        for source in ('compact', 'resume'):
            out = self.event('SessionStart', source=source)
            self.assertEqual(set(out), {'hookSpecificOutput'})
            self.assertEqual(out['hookSpecificOutput']['hookEventName'], 'SessionStart')
            text = self.text(out)
            for expected in ('Objective: Preserve result behavior', 'Profile=deep', 'plan=deep', 'R1', 'Next:'):
                self.assertIn(expected, text)
            self.assertEqual(self.state()['run_id'], before['run_id'])
            self.assertEqual(self.state()['contract_hash'], before['contract_hash'])

    def test_stop_resume_restore_without_losing_requirements_or_policy(self):
        self.begin()
        before = self.state()
        self.assertIn('cancelled', self.text(self.event('UserPromptSubmit', prompt='중단')))
        self.assertEqual(self.state()['status'], 'cancelled')
        text = self.text(self.event('UserPromptSubmit', prompt='재개'))
        self.assertIn('Objective:', text)
        self.assertIn('R1', text)
        self.assertEqual(self.state()['status'], 'working')
        self.assertEqual(self.state()['contract_hash'], before['contract_hash'])
        self.assertEqual(self.state()['policy'], before['policy'])

    def test_pause_session_resume_does_not_auto_resume_the_run(self):
        self.begin()
        self.event('SessionEnd')
        self.assertEqual(self.state()['status'], 'paused')
        self.assertIn('paused', self.text(self.event('SessionStart', source='resume')))
        self.assertEqual(self.state()['status'], 'paused')

    def test_explicit_goal_and_goal_restoration_survive_quiet_output(self):
        self.event('SessionStart', source='startup')
        text = self.text(self.event('UserPromptSubmit', prompt='/goal Deliver a verified result'))
        self.assertIn('Goal', text)
        self.assertEqual(self.state()['goal_request']['outcome'], 'Deliver a verified result')
        self.assertIn('Deliver a verified result', self.text(self.event('SessionStart', source='compact')))

    def test_unsupported_context_events_keep_existing_state_processing(self):
        self.begin()
        before = self.state()
        self.assertEqual(self.event('PostCompact', trigger='auto'), {})
        self.assertEqual(self.state()['run_id'], before['run_id'])
        self.assertIn('PostCompact', self.state()['host_capabilities']['observed_events'])
        self.assertEqual(self.event('SubagentStop', agent_id='missing'), {})
        self.assertEqual(self.event('UnsupportedEvent'), {})

    def test_direct_protocol_instructions_describe_profile_sized_plan(self):
        protocol = (ROOT / 'skills/adhd-native/references/protocol.md').read_text(encoding='utf-8')
        self.assertIn('SessionStart', protocol)
        self.assertIn('compact', protocol)
        self.assertIn('Simple plans are optional', protocol)

    def test_plan_missing_and_pending_intent_are_restored(self):
        self.begin(plan=False)
        text = self.text(self.event('SessionStart', source='compact'))
        self.assertIn('native plan', text)
        self.event('UserPromptSubmit', prompt='Keep an extra format constraint.')
        text = self.text(self.event('SessionStart', source='resume'))
        self.assertIn('sync-intent', text)

    def test_long_restoration_keeps_action_and_profile_before_common_rules(self):
        self.begin('deep')
        state = self.state()
        state['plan']['content']['objective'] = 'Objective details ' * 200
        state['contract']['criteria'] = [{'id': 'R' + str(i), 'text': 'Required'} for i in range(40)]
        state['feedback'] = 'Observed blocker details ' * 300
        state['goal_request'] = {'outcome': 'Long original goal ' * 200}
        text = context(state, restore=True)
        self.assertLessEqual(len(text), 1200)
        for expected in ('Profile=deep', 'plan=deep', 'Goal execution requested', 'Objective:', 'Unmet acceptance:', 'Next:', 'view='):
            self.assertIn(expected, text)
        self.assertIn(str(self.ws / '.adhd/bridge' / self.key / 'view.json'), text)
