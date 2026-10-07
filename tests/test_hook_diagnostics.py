"""Opt-in hook observation must not alter native processing or error contracts."""
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
from adhd.hook_diagnostics import HookDiagnostics, output_sizes
from adhd.native import folder, handle_event, session_key, write_hook_diagnostics

ROOT = Path(__file__).resolve().parents[1]


class HookDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ws = Path(temporary.name) / 'workspace'
        self.ws.mkdir()
        env = patch.dict(os.environ, {'CODEX_HOME': str(Path(temporary.name) / 'codex'),
                                     'ADHD_EXEC_OWNER': '', 'ADHD_HOOK_DIAGNOSTICS': ''})
        env.start()
        self.addCleanup(env.stop)
        self.event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'diagnostic-session',
                      'cwd': str(self.ws), 'turn_id': 'request', 'prompt': 'Preserve 원문'}

    def invoke(self, enabled):
        environment = {**os.environ, 'ADHD_HOOK_DIAGNOSTICS': '1' if enabled else '0'}
        result = subprocess.run([sys.executable, str(ROOT / 'hook.py')],
                                input=json.dumps(self.event).encode('utf-8'),
                                capture_output=True, env=environment, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        return json.loads(result.stdout), result.stdout.decode('utf-8')

    def records(self):
        path = folder(session_key(self.event['session_id'])) / 'ledger.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_disabled_has_no_performance_records(self):
        self.invoke(False)
        self.assertFalse(any(row['event'] == 'hook_diagnostics' for row in self.records()))

    def test_output_sizes_count_only_explicit_model_text_fields(self):
        out = {'reason': '차단', 'systemMessage': 'error', 'stopReason': 'stop',
               'hookSpecificOutput': {'additionalContext': '안내',
                                      'permissionDecisionReason': 'denied',
                                      'hookEventName': 'PreToolUse'}, 'decision': 'block'}
        sizes = output_sizes(out)
        texts = ['차단', 'error', 'stop', '안내', 'denied']
        self.assertEqual(sizes['emitted_context_chars'], sum(map(len, texts)))
        self.assertEqual(sizes['emitted_context_bytes'], sum(len(t.encode('utf-8')) for t in texts))
        self.assertEqual(len(sizes['emitted_fields']), 5)
        self.assertEqual(output_sizes({})['emitted_context_chars'], 0)

    def test_enabled_preserves_output_state_and_view_and_measures_import(self):
        before, _ = self.invoke(False)
        state_path = folder(session_key(self.event['session_id'])) / 'state.json'
        state = read_json(state_path)
        after, stdout = self.invoke(True)
        self.assertEqual(before, after)
        self.assertEqual(state, read_json(state_path))
        view = read_json(self.ws / '.adhd/bridge' / session_key(self.event['session_id']) / 'view.json')
        self.assertNotIn('phases_ns', view)
        diagnostic = [r for r in self.records() if r['event'] == 'hook_diagnostics'][-1]
        self.assertGreater(diagnostic['phases_ns']['native_import'], 0)
        for phase in ['lock_wait', 'state_read', 'state_prepare', 'goal_check', 'event_dispatch', 'persist']:
            self.assertIn(phase, diagnostic['phases_ns'])
        self.assertEqual(diagnostic['stdout_json_bytes'], len(stdout.encode('utf-8')))
        self.assertIsNone(diagnostic['host_usage'])
        self.assertFalse(diagnostic['host_usage_observed'])
        serialized = json.dumps(diagnostic, ensure_ascii=False)
        self.assertNotIn('Preserve', serialized)
        self.assertNotIn(str(self.ws), serialized)

    def test_record_failure_is_best_effort_only(self):
        diagnostics = HookDiagnostics(enabled=True)
        output = handle_event(self.event, diagnostics=diagnostics)
        with patch('adhd.native.append_event', side_effect=OSError('diagnostic disk failure')):
            write_hook_diagnostics(diagnostics, output, json.dumps(output))
        self.assertEqual(read_json(folder(session_key(self.event['session_id'])) / 'state.json')['status'], 'idle')

    def test_original_save_failure_is_not_swallowed(self):
        with patch('adhd.native.persist', side_effect=OSError('real state failure')):
            with self.assertRaisesRegex(OSError, 'real state failure'):
                handle_event(self.event, diagnostics=HookDiagnostics(enabled=True))

    def test_original_validation_failure_is_not_swallowed(self):
        with self.assertRaisesRegex(ValueError, 'Invalid/oversized'):
            handle_event({**self.event, 'prompt': 12}, diagnostics=HookDiagnostics(enabled=True))

    def test_error_stdout_is_measured_without_changing_error_schema(self):
        self.event['prompt'] = 12
        out, stdout = self.invoke(True)
        self.assertIn('systemMessage', out)
        diagnostic = [r for r in self.records() if r['event'] == 'hook_diagnostics'][-1]
        self.assertEqual(diagnostic['emitted_context_chars'], len(out['systemMessage']))
        self.assertEqual(diagnostic['stdout_json_bytes'], len(stdout.encode('utf-8')))

    def test_unknown_event_remains_empty_with_no_state(self):
        self.event['hook_event_name'] = 'UnsupportedEvent'
        self.assertEqual(self.invoke(True)[0], {})
        self.assertFalse((folder(session_key(self.event['session_id'])) / 'state.json').exists())

    def test_only_explicit_one_enables_observation(self):
        for value in ['', '0', 'false', 'yes']:
            with patch.dict(os.environ, {'ADHD_HOOK_DIAGNOSTICS': value}):
                self.assertFalse(HookDiagnostics().enabled)

    def test_existing_host_usage_is_reused_only_for_observed_child_stop(self):
        diagnostics = HookDiagnostics(enabled=True)
        handle_event(self.event, diagnostics=diagnostics)
        self.assertIsNone(diagnostics.host_usage)
        state_path = folder(session_key(self.event['session_id'])) / 'state.json'
        state = read_json(state_path)
        state['children']['observed'] = {'status': 'running', 'role': 'adhd-scout'}
        from adhd.core import atomic_json
        atomic_json(state_path, state)
        diagnostics = HookDiagnostics(enabled=True)
        handle_event({**self.event, 'hook_event_name': 'SubagentStop', 'agent_id': 'observed',
                      'usage': {'input_tokens': 17, 'output_tokens': 3}}, diagnostics=diagnostics)
        self.assertEqual(diagnostics.host_usage, {'input_tokens': 17, 'output_tokens': 3})
        diagnostics = HookDiagnostics(enabled=True)
        handle_event({**self.event, 'hook_event_name': 'SubagentStop', 'agent_id': 'observed'}, diagnostics=diagnostics)
        self.assertIsNone(diagnostics.host_usage)
