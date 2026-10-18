import json
from pathlib import Path
import tempfile
import unittest

from adhd.trial_registry import TrialRegistry, compare_trials


CONDITIONS = dict(model='selected-model', effort='max', input_snapshot='input-sha',
                  source_snapshot='build-sha', policy='v1', environment='env-sha',
                  tools='tool-sha', permissions='local', delivery='local_artifact', cache='cold')


class TrialTests(unittest.TestCase):
    def test_post_task_observations_are_bounded_typed_and_backward_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry = TrialRegistry(Path(temporary))
            post_task = {'remaining_count': 0, 'remaining_cpu_delta_seconds': 0.0,
                         'window_elapsed_ms': 200.0, 'sampler_stopped': True,
                         'scope': 'tracked PID and birth identities only'}
            for invalid in ({**post_task, 'whole_host_idle_cpu': 0},
                            {**post_task, 'remaining_cpu_delta_seconds': float('nan')},
                            {**post_task, 'remaining_identities': [{'pid': 1, 'birth': 'unknown'}]},
                            {**post_task, 'remaining_identities': [{'pid': 1, 'birth': 2}] * 33}):
                trial = registry.start('invalid', 'code', 'candidate', CONDITIONS)
                with self.assertRaises(ValueError):
                    registry.finish(trial, 'success', resources={'post_task': invalid})
            trial = registry.start('valid', 'code', 'candidate', CONDITIONS)
            registry.finish(trial, 'success', resources={'post_task': post_task})
            old_trial = registry.start('old', 'code', 'baseline', CONDITIONS)
            registry.finish(old_trial, 'failed', resources={'sampler_stopped': True})
            rows = list(registry.trials())
            self.assertEqual(2, sum(row['status'] != 'unfinished' for row in rows))

    def test_failed_and_unfinished_starts_stay_in_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = TrialRegistry(Path(tmp))
            first = registry.start('one', 'code', 'baseline', CONDITIONS)
            registry.finish(first, 'failed', metrics={'verified_ms': None})
            registry.start('two', 'code', 'candidate', CONDITIONS)
            summary = registry.summary()
            self.assertEqual(2, summary['started'])
            self.assertEqual(0, summary['succeeded'])
            self.assertEqual(1, summary['unfinished'])
            self.assertEqual(0, summary['completion_rate'])

    def test_registry_rejects_secrets_conflicting_finish_and_truncated_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = TrialRegistry(Path(tmp))
            with self.assertRaises(ValueError):
                registry.start('bad', 'code', 'baseline', {**CONDITIONS, 'tools': 'sk-' + 'a' * 30})
            run = registry.start('ok', 'code', 'baseline', CONDITIONS)
            registry.finish(run, 'timeout')
            with self.assertRaises(ValueError):
                registry.finish(run, 'success')
            with registry.path.open('ab') as stream:
                stream.write(b'{"partial"')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                registry.summary()

    def test_only_preregistered_passed_scenario_marks_first_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = TrialRegistry(Path(tmp))
            run = registry.start('one', 'code', 'baseline', CONDITIONS, usable_scenario='button-click')
            receipt = Path(tmp) / 'scenario.json'
            receipt.write_text(json.dumps({'scenario_id': 'other', 'passed': True}))
            with self.assertRaises(ValueError):
                registry.usable(run, receipt)
            receipt.write_text(json.dumps({'scenario_id': 'button-click', 'passed': True}))
            registry.usable(run, receipt)
            registry.finish(run, 'success')
            row = list(registry.trials())[0]
            self.assertGreaterEqual(row['first_usable_ms'], 0)
            self.assertEqual(64, len(row['usable_receipt']['sha256']))

    def test_missing_pairs_and_usage_never_become_measured_savings(self):
        rows = []
        for variant in ('baseline', 'candidate'):
            rows.append(dict(id=variant, case_id='one', task_type='code', variant=variant,
                             conditions=CONDITIONS, status='success',
                             quality={key: True for key in ('requirements', 'preservation', 'security', 'independent')},
                             metrics={'verified_ms': 100 if variant == 'baseline' else 70,
                                      'context_bytes': 1000 if variant == 'baseline' else 500},
                             usage={'coverage': 'unknown'}))
        result = compare_trials(rows, expected_cases=['one', 'missing'])
        self.assertFalse(result['adopt'])
        self.assertEqual(['missing'], result['missing_cases'])
        self.assertEqual(0, result['metrics']['input_tokens']['paired'])
        self.assertEqual(1, result['metrics']['verified_ms']['paired'])


    def test_empty_comparison_keeps_observations_unknown_and_gate_closed(self):
        for expected in ([], ['missing']):
            with self.subTest(expected=expected):
                result = compare_trials([], expected_cases=expected)
                self.assertEqual(result['started'], 0)
                self.assertIsNone(result['completion_rate'])
                self.assertFalse(result['meets_component_gate'])
                self.assertFalse(result['adopt'])
                for metric in result['metrics'].values():
                    self.assertEqual(metric['paired'], 0)
                    self.assertIsNone(metric['median_savings'])

    def test_rollout_with_no_starts_returns_missing_evidence(self):
        from adhd.trial_registry import rollout_gate
        types = ('windows_code', 'ui_preservation', 'document_format', 'cited_research')
        cases = [dict(id=f'{kind}-{phase}-{n}', task_type=kind, phase=phase,
                      input_sha256='a' * 64, usable_scenario='required-scenario')
                 for phase, count in [('pilot', 3), ('holdout', 6)]
                 for kind in types for n in range(count)]
        result = rollout_gate({'cases': cases}, [])
        self.assertFalse(result['declared_metrics_gate'])
        self.assertFalse(result['adopt'])
        for phase in ('pilot', 'holdout'):
            row = result['phases'][phase]
            self.assertEqual(row['started'], 0)
            self.assertFalse(row['meets_component_gate'])
            self.assertTrue(row['missing_cases'])
