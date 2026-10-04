"""A native writer remains owned until every observed child has stopped."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.core import atomic_json, read_json
from adhd import leases
from adhd.models import route
from adhd.native import folder, handle_event, session_key, submit_request


class NativeTerminationTests(unittest.TestCase):
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
        self.counter = 0
        self.event('UserPromptSubmit', prompt='Implement and verify result.txt')
        plan = {'objective': 'Implement result', 'approach': 'Inspect, edit, check',
                'verification': 'Run checks', 'preflight': ['Inspect'],
                'risks': ['Regression'], 'alternatives': ['Small patch'],
                'steps': [{'id': 'S1', 'action': 'Implement and check',
                           'requirements': ['R1'], 'depends_on': []}]}
        result = self.request('begin', {'mode': 'coding', 'criteria': [
            {'id': 'R1', 'text': 'Working result', 'kind': 'test'}],
            'artifacts': ['result.txt'], 'plan': plan})
        self.assertTrue(result['ok'], result)

    def event(self, name, **kw):
        self.counter += 1
        return handle_event({'hook_event_name': name, 'session_id': self.sid,
                             'cwd': str(self.ws), 'turn_id': str(self.counter), **kw})

    def request(self, op, payload):
        receipt = submit_request(self.key, self.ws, op, payload)
        self.event('PostToolUse', tool_name='Bash')
        return read_json(Path(receipt['receipt']))

    def state(self):
        return read_json(folder(self.key) / 'state.json')

    def save(self, state):
        atomic_json(folder(self.key) / 'state.json', state)

    def child(self, role, aid):
        self.event('PreToolUse', tool_name='spawn_agent', tool_use_id=aid,
                   tool_input={'agent_type': role, 'message': 'Complete the assigned slice.'})
        self.event('SubagentStart', agent_type=role, agent_id=aid,
                   model=route(role)['model'], tool_use_id=aid)

    def stop_child(self, role, aid):
        self.event('SubagentStop', agent_type=role, agent_id=aid,
                   last_assistant_message='Done')

    def assert_retained(self, target):
        state = self.state()
        self.assertEqual(state['status'], 'interrupt_pending')
        self.assertEqual(state['pending_terminal'], target)
        self.assertTrue(leases.lease_path(self.ws).exists())
        with self.assertRaisesRegex(ValueError, 'owns this workspace'):
            leases.acquire(self.ws, 'native:other:run', owner='native')

    def test_budget_drains_all_children_before_release(self):
        self.child('adhd-implementer', 'writer')
        self.child('adhd-scout', 'reader')
        state = self.state()
        state['policy']['max_rounds'] = 0
        self.save(state)
        self.assertFalse(self.event('Stop')['continue'])
        self.assert_retained('budget_exhausted')
        self.stop_child('adhd-scout', 'reader')
        self.assert_retained('budget_exhausted')
        self.stop_child('adhd-implementer', 'writer')
        self.assertEqual(self.state()['status'], 'budget_exhausted')
        self.assertFalse(leases.lease_path(self.ws).exists())
        generation = leases.acquire(self.ws, 'native:other:run', owner='native')
        self.stop_child('adhd-implementer', 'writer')  # late duplicate old event
        self.assertEqual(read_json(leases.lease_path(self.ws))['generation'], generation)

    def test_pause_cancel_interrupt_session_end_and_blocked_drain(self):
        for event in ('pause', 'cancel', 'Interrupt', 'SessionEnd', 'blocked'):
            with self.subTest(event=event):
                # Each case gets a fresh run and lease.
                if event != 'pause':
                    self.tearDown()
                    self.setUp()
                self.child('adhd-implementer', 'writer')
                if event == 'pause':
                    self.assertTrue(self.request('pause', {})['ok'])
                    terminal = 'paused'
                elif event == 'cancel':
                    self.event('UserPromptSubmit', prompt='cancel')
                    terminal = 'cancelled'
                elif event == 'blocked':
                    self.assertTrue(self.request('blocked', {'reason': 'Missing capability'})['ok'])
                    terminal = 'blocked'
                else:
                    self.event(event)
                    terminal = 'paused'
                self.assert_retained(terminal)
                self.stop_child('adhd-implementer', 'writer')
                self.assertEqual(self.state()['status'], terminal)
                self.assertFalse(leases.lease_path(self.ws).exists())

    def test_terminal_state_with_running_child_is_not_reclaimable(self):
        self.child('adhd-implementer', 'writer')
        state = self.state()
        state['status'] = 'budget_exhausted'  # persisted state from an older release
        self.save(state)
        with self.assertRaisesRegex(ValueError, 'owns this workspace'):
            leases.acquire(self.ws, 'native:other:run', owner='native')
