"""Regressions for native v0.1.1 trust and transition boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.core import read_json
from adhd.evidence import run_check, validate_execution
from adhd import leases
from adhd.native import folder, handle_event, session_key, submit_request


class NativeV011Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ws = self.base / 'workspace'
        self.ws.mkdir()
        codex_home = self.base / 'codex'
        codex_home.mkdir()
        env = patch.dict(os.environ, {'CODEX_HOME': str(codex_home), 'ADHD_EXEC_OWNER': ''})
        env.start()
        self.addCleanup(env.stop)
        self.sid = uuid.uuid4().hex
        self.key = session_key(self.sid)
        self.turn = 0
        self.event('UserPromptSubmit', prompt='Produce result.txt and verify it')

    def event(self, name, **kwargs):
        self.turn += 1
        return handle_event({'hook_event_name': name, 'session_id': self.sid,
                             'cwd': str(self.ws), 'turn_id': str(self.turn), **kwargs})

    def state(self):
        return read_json(folder(self.key) / 'state.json')

    def request(self, operation, payload):
        receipt = submit_request(self.key, self.ws, operation, payload)
        self.event('PostToolUse', tool_name='Bash')
        return read_json(Path(receipt['receipt']))

    def begin(self):
        result = self.request('begin', {
            'mode': 'coding', 'criteria': [{'id': 'R1', 'text': 'Pass', 'kind': 'test'}],
            'artifacts': ['result.txt'],
            'plan': {'objective': 'Produce result', 'approach': 'Write and check',
                     'verification': 'Run a process', 'preflight': ['Inspect workspace'],
                     'risks': ['False test result'], 'alternatives': ['Simpler manual check'],
                     'steps': [{'id': 'S1', 'action': 'Write then check',
                                'requirements': ['R1'], 'depends_on': []}]}})
        self.assertTrue(result['ok'], result)

    def check(self, *, exit_code=0):
        return run_check({'run_id': self.state()['run_id'],
                          'contract_revision': self.state()['intent_version'],
                          'subject_paths': ['result.txt'],
                          'argv': [sys.executable, '-c', f'import sys;sys.exit({exit_code})']}, self.ws)

    def candidate(self, receipt):
        return self.request('candidate', {
            'files': ['result.txt'], 'criterion_results': [
                {'id': 'R1', 'pass': True, 'evidence': 'Observed command succeeded',
                 'evidence_ids': [receipt['receipt']]}]})

    def test_execution_receipt_requires_success_and_current_subject(self):
        self.begin()
        (self.ws / 'result.txt').write_text('one', encoding='utf-8')
        failed = self.check(exit_code=7)
        self.assertEqual(failed['exit_code'], 7)
        self.assertFalse(self.candidate(failed)['ok'])
        passed = self.check()
        self.assertTrue(self.candidate(passed)['ok'])
        (self.ws / 'result.txt').write_text('two', encoding='utf-8')
        with self.assertRaises(ValueError):
            validate_execution(self.ws, passed['receipt'], run_id=self.state()['run_id'],
                               revision=self.state()['intent_version'])
        self.assertEqual(self.event('Stop')['decision'], 'block')

    def test_unmatched_verifier_start_cannot_approve(self):
        self.begin()
        (self.ws / 'result.txt').write_text('one', encoding='utf-8')
        self.assertTrue(self.candidate(self.check())['ok'])
        pre = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='tool-a',
                         tool_input={'agent_type': 'adhd-verifier'})
        self.assertNotEqual(pre.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.event('SubagentStart', agent_type='adhd-verifier', agent_id='agent-a',
                   tool_use_id='wrong-tool', model='gpt-6-sol')
        state=self.state();row = state['candidate']
        self.event('SubagentStop', agent_id='agent-a', last_assistant_message=json.dumps({
            'verdict': 'approve', 'reviewed_digest': row['digest'],
            'reviewed_contract_hash':state['contract_hash'],
            'reviewed_turn_ids':[p['turn_id'] for p in state['prompts']],
            'intent_alignment':True,
            'criterion_results': row['criterion_results'], 'findings': []}))
        self.assertEqual(self.state()['status'], 'revising')
        self.assertFalse(self.state()['host_capabilities'].get('verifier_correlated', False))

    def test_status_turn_preserves_complete_until_reconciled(self):
        self.begin()
        (self.ws / 'result.txt').write_text('one', encoding='utf-8')
        self.assertTrue(self.candidate(self.check())['ok'])
        self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='tool-a',
                   tool_input={'agent_type': 'adhd-verifier'})
        self.event('SubagentStart', agent_type='adhd-verifier', agent_id='agent-a',
                   tool_use_id='tool-a', model='gpt-6-sol')
        state=self.state();candidate = state['candidate']
        self.event('SubagentStop', agent_id='agent-a', last_assistant_message=json.dumps({
            'verdict': 'approve', 'reviewed_digest': candidate['digest'],
            'reviewed_contract_hash':state['contract_hash'],
            'reviewed_turn_ids':[p['turn_id'] for p in state['prompts']],
            'intent_alignment':True,
            'criterion_results': candidate['criterion_results'], 'findings': []}))
        self.assertEqual(self.state()['status'], 'complete')
        self.event('UserPromptSubmit', prompt='What is the status?')
        self.event('Stop')
        self.assertEqual(self.state()['status'], 'complete')
        pending = self.state()['pending_turn_ids'][0]
        result = self.request('sync-intent', {'source_turn_id': pending,
                         'base_revision': self.state()['intent_version'],
                         'classification': 'no_change'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.state()['status'], 'complete')

    def test_interrupt_waits_for_running_child_before_release(self):
        self.begin()
        owner = leases.lease_path(self.ws)
        self.assertTrue(owner.exists())
        self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='tool-a',
                   tool_input={'agent_type': 'adhd-scout'})
        self.event('SubagentStart', agent_type='adhd-scout', agent_id='agent-a',
                   tool_use_id='tool-a', model='gpt-6-luna')
        self.event('Interrupt')
        self.assertEqual(self.state()['status'], 'interrupt_pending')
        self.assertTrue(owner.exists())
        self.event('SubagentStop', agent_id='agent-a')
        self.assertEqual(self.state()['status'], 'paused')
        self.assertFalse(owner.exists())

    def test_user_replacement_and_retraction_are_revisioned(self):
        self.begin()
        self.event('UserPromptSubmit', prompt='Replace the pass condition and rename the output')
        pending = self.state()['pending_turn_ids'][0]
        response = self.request('sync-intent', {
            'source_turn_id': pending, 'base_revision': self.state()['intent_version'],
            'classification': 'amend', 'operations': [
                {'op': 'replace', 'target': 'criteria/R1',
                 'value': {'id': 'R1', 'text': 'New behavior passes', 'kind': 'test'}},
                {'op': 'replace', 'target': 'artifacts/result.txt', 'value': 'renamed.txt'}]})
        self.assertTrue(response['ok'], response)
        current = self.state()
        self.assertEqual(current['contract']['criteria'][0]['text'], 'New behavior passes')
        self.assertEqual(current['contract']['artifacts'], ['renamed.txt'])
        self.assertTrue((folder(self.key) / f"contract-{current['run_id']}-v1.json").exists())
        self.event('UserPromptSubmit', prompt='Remove the renamed output and use result.txt again')
        pending = self.state()['pending_turn_ids'][0]
        response = self.request('sync-intent', {
            'source_turn_id': pending, 'base_revision': self.state()['intent_version'],
            'classification': 'amend', 'operations': [
                {'op': 'retract', 'target': 'artifacts/renamed.txt'},
                {'op': 'add', 'target': 'artifacts/result.txt'}]})
        self.assertTrue(response['ok'], response)
        self.assertEqual(self.state()['contract']['artifacts'], ['result.txt'])

    def test_new_task_waits_for_running_child_then_archives(self):
        self.begin()
        owner = leases.lease_path(self.ws)
        self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='tool-a',
                   tool_input={'agent_type': 'adhd-scout'})
        self.event('SubagentStart', agent_type='adhd-scout', agent_id='agent-a',
                   tool_use_id='tool-a', model='gpt-6-luna')
        self.event('UserPromptSubmit', prompt='Now do a different task')
        pending = self.state()['pending_turn_ids'][0]
        response = self.request('sync-intent', {
            'source_turn_id': pending, 'base_revision': self.state()['intent_version'],
            'classification': 'new_task'})
        self.assertTrue(response['ok'], response)
        self.assertEqual(self.state()['status'], 'handoff_pending')
        self.assertTrue(owner.exists())
        old_run = self.state()['run_id']
        self.event('SubagentStop', agent_id='agent-a')
        self.assertEqual(self.state()['status'], 'idle')
        self.assertFalse(owner.exists())
        archived = folder(self.key) / 'run-archive' / old_run / 'state.json'
        self.assertEqual(read_json(archived)['run_id'], old_run)


class LeaseGenerationTests(unittest.TestCase):
    def test_old_owner_cannot_release_new_generation(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / 'codex').mkdir()
            workspace = base / 'workspace'
            workspace.mkdir()
            with patch.dict(os.environ, {'CODEX_HOME': str(base / 'codex')}):
                first = leases.acquire(workspace, 'legacy-a', owner='legacy')
                self.assertTrue(leases.release(workspace, 'legacy-a', first))
                second = leases.acquire(workspace, 'legacy-b', owner='legacy')
                self.assertGreater(second, first)
                self.assertFalse(leases.release(workspace, 'legacy-a', first))
                self.assertTrue(leases.lease_path(workspace).exists())
                self.assertTrue(leases.release(workspace, 'legacy-b', second))


if __name__ == '__main__':
    unittest.main()
