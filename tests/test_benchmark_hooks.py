"""Measured diagnostics must not fall back to setup records."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import benchmark_hooks


class BenchmarkHookTests(unittest.TestCase):
    def run_sample(self, *, measured_diagnostic, rotate=False):
        fixture = {'prompt': 'Original benchmark intent'}
        scenario = {'initial': 'idle', 'event': {'hook_event_name': 'PostToolUse'}}
        calls = []

        def invoke(root, environment, event):
            calls.append(event['turn_id'])
            session = Path(environment['ADHD_HOME']) / 'native/session'
            session.mkdir(parents=True, exist_ok=True)
            (session / 'state.json').write_text(json.dumps({
                'status': 'idle', 'prompts': [{'text': fixture['prompt']}]}), encoding='utf-8')
            ledger = session / 'ledger.jsonl'
            measured = event['turn_id'] == 'measured'
            if measured and rotate:
                ledger.replace(session / 'ledger.previous.jsonl')
            row = {'event': 'hook_diagnostics', 'stdout_json_bytes': 2,
                   'phase': event['turn_id']}
            if measured and not measured_diagnostic:
                row = {'event': 'other_measured_record'}
            with ledger.open('a', encoding='utf-8') as output:
                output.write(json.dumps(row) + '\n')
            # Setup and measurement deliberately have identical output byte counts.
            return 100, {}, 2

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(benchmark_hooks, 'invoke', side_effect=invoke):
                result = benchmark_hooks.sample(Path(temporary), True, fixture, scenario, temporary)
        self.assertEqual(calls, ['setup-session', 'setup-prompt', 'measured'])
        self.assertTrue(result['processing_verified'])
        self.assertEqual(result['stdout_json_bytes'], 2)
        return result

    def test_missing_measured_diagnostic_with_matching_setup_bytes_remains_null(self):
        result = self.run_sample(measured_diagnostic=False)
        self.assertIsNone(result['diagnostic'])

    def test_selects_only_new_measured_diagnostic(self):
        result = self.run_sample(measured_diagnostic=True)
        self.assertEqual(result['diagnostic']['phase'], 'measured')

    def test_rotated_ledger_selects_new_measured_diagnostic(self):
        result = self.run_sample(measured_diagnostic=True, rotate=True)
        self.assertEqual(result['diagnostic']['phase'], 'measured')
