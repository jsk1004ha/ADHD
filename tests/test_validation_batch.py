from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adhd.validation_batch import run_batch, load_report, repair_plan, validate_plan


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name)
        (self.ws / 'a.txt').write_text('first', encoding='utf-8')
        (self.ws / 'b.txt').write_text('second', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def check(self, name, code='print("ok")', paths=None, **extra):
        return {'id': name, 'argv': [sys.executable, '-c', code],
                'subject_paths': paths or ['a.txt'], 'requirements': ['R1'], **extra}

    def spec(self, checks):
        return {'run_id': 'test-run', 'contract_revision': 1, 'contract_hash': 'abc',
                'requirements': ['R1'], 'mandatory_checks': [checks[0]['id']],
                'process_slots': 2, 'checks': checks}

    def test_independent_checks_continue_and_dependent_is_blocked(self):
        spec = self.spec([self.check('bad', 'raise AssertionError("broken contract")', owner='api'),
                          self.check('dependent', depends_on=['bad']), self.check('other', paths=['b.txt'])])
        result = run_batch(spec, self.ws)
        self.assertEqual(result['status'], 'needs_repair')
        report = load_report(self.ws, result['report'], require_current=False)
        self.assertEqual([r['status'] for r in report['results']], ['failed', 'blocked', 'passed'])
        self.assertEqual(report['groups'][0]['affected_checks'], ['bad'])
        self.assertTrue((self.ws / report['results'][0]['receipt']).is_file())
        self.assertEqual(repair_plan(self.ws, result['report'])['blocked_checks'], ['dependent'])
        with self.assertRaises(ValueError):
            load_report(self.ws, result['report'])

    def test_passed_report_is_bound_to_actual_inputs_and_logs(self):
        result = run_batch(self.spec([self.check('ok')]), self.ws)
        report = load_report(self.ws, result['report'])
        self.assertEqual(report['status'], 'passed')
        logs = self.ws / Path(report['results'][0]['receipt']).parent / 'check.stdout.jsonl'
        logs.write_text('edited', encoding='utf-8')
        with self.assertRaises(ValueError):
            load_report(self.ws, result['report'])

    def test_changed_input_invalidates_pass(self):
        result = run_batch(self.spec([self.check('ok')]), self.ws)
        (self.ws / 'a.txt').write_text('changed', encoding='utf-8')
        with self.assertRaises(ValueError):
            load_report(self.ws, result['report'])

    def test_repair_reuses_only_proven_unaffected_inputs_and_reruns_critical(self):
        complete = {'impact_complete': True, 'environment_complete': True,
                    'dependency_paths': [], 'fixture_paths': [], 'environment_vars': []}
        checks = [self.check('a', **complete),
                  self.check('b', paths=['b.txt'], **complete),
                  self.check('critical', paths=['b.txt'], critical=True)]
        spec = self.spec(checks)
        first = run_batch(spec, self.ws)
        (self.ws / 'a.txt').write_text('changed', encoding='utf-8')
        spec['previous_report'] = first['report']
        second = run_batch(spec, self.ws)
        report = load_report(self.ws, second['report'])
        self.assertEqual([r['status'] for r in report['results']], ['passed', 'reused', 'passed'])
        self.assertEqual(report['results'][1]['reused_from_snapshot'], first['snapshot_digest'])

    def test_unknown_impact_reruns_all(self):
        spec = self.spec([self.check('a'), self.check('b', paths=['b.txt'])])
        first = run_batch(spec, self.ws)
        (self.ws / 'a.txt').write_text('changed', encoding='utf-8')
        spec['previous_report'] = first['report']
        second = run_batch(spec, self.ws)
        self.assertNotIn('reused', second['counts'])

    def test_unchanged_inputs_still_rerun_when_impact_scope_is_unknown(self):
        spec = self.spec([self.check('a', environment_complete=True)])
        first = run_batch(spec, self.ws)
        first_row = load_report(self.ws, first['report'])['results'][0]
        spec['previous_report'] = first['report']
        second = run_batch(spec, self.ws)
        report = load_report(self.ws, second['report'])
        self.assertEqual(report['results'][0]['status'], 'passed')
        self.assertNotEqual(report['results'][0]['receipt'], first_row['receipt'])
        self.assertEqual(report['usage']['check_processes'], 1)

    def test_failed_check_is_not_held_when_impact_scope_is_unknown(self):
        spec = self.spec([self.check('failure', 'import sys;sys.exit(1)',
                                     environment_complete=True)])
        first = run_batch(spec, self.ws)
        first_row = load_report(self.ws, first['report'], require_current=False)['results'][0]
        spec['previous_report'] = first['report']
        second = run_batch(spec, self.ws)
        report = load_report(self.ws, second['report'], require_current=False)
        self.assertEqual(report['results'][0]['status'], 'failed')
        self.assertNotEqual(report['results'][0]['receipt'], first_row['receipt'])
        self.assertEqual(report['usage']['check_processes'], 1)

    def test_repair_cannot_drop_or_weaken_existing_checks(self):
        spec = self.spec([self.check('a'), self.check('b', paths=['b.txt'])])
        first = run_batch(spec, self.ws)
        repaired = copy.deepcopy(spec)
        repaired['previous_report'] = first['report']
        repaired['checks'] = repaired['checks'][:1]
        with self.assertRaises(ValueError):
            run_batch(repaired, self.ws)
        repaired = copy.deepcopy(spec)
        repaired['previous_report'] = first['report']
        repaired['checks'][0]['argv'] = [sys.executable, '-c', 'pass']
        with self.assertRaises(ValueError):
            run_batch(repaired, self.ws)

    def test_plan_coverage_and_mandatory_checks_cannot_be_omitted(self):
        spec = self.spec([self.check('ok')])
        spec['mandatory_checks'] = ['missing']
        with self.assertRaises(ValueError):
            validate_plan(spec)
        spec['mandatory_checks'] = ['ok']
        spec['requirements'] = ['R2']
        with self.assertRaises(ValueError):
            validate_plan(spec)

    def test_mutation_during_batch_is_stale(self):
        code = 'from pathlib import Path; Path("a.txt").write_text("changed")'
        result = run_batch(self.spec([self.check('mutator', code)]), self.ws)
        self.assertEqual(result['status'], 'stale')

    def test_identical_messages_different_owners_remain_separate(self):
        checks = [self.check('api', 'raise ValueError("wrong")', owner='api'),
                  self.check('ui', 'raise ValueError("wrong")', owner='ui')]
        result = run_batch(self.spec(checks), self.ws)
        self.assertEqual(len(result['groups']), 2)


class BatchTimeBudgetTests(unittest.TestCase):
    setUp = BatchTests.setUp
    tearDown = BatchTests.tearDown
    check = BatchTests.check
    spec = BatchTests.spec

    def test_invalid_controls_fail_before_any_process(self):
        invalid = [('time_limit_seconds', True), ('time_limit_seconds', float('nan')),
                   ('time_limit_seconds', 0), ('fail_fast', 1), ('scheduling', 'skip_required')]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                spec = self.spec([self.check('ok')])
                spec[field] = value
                with patch('adhd.validation_batch.run_check') as observed:
                    with self.assertRaises(ValueError):
                        run_batch(spec, self.ws)
                    observed.assert_not_called()

    def test_estimates_reject_nonfinite_and_boolean_values(self):
        for value in (-1, True, float('inf'), 'fast'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_plan(self.spec([self.check('a', estimated_seconds=value)]))

    def test_shared_budget_bounds_running_work_and_does_not_launch_next_check(self):
        checks = [self.check('slow', 'import time; time.sleep(20)'),
                  self.check('next', 'from pathlib import Path;Path("unexpected").write_text("ran")')]
        spec = dict(self.spec(checks), process_slots=1, time_limit_seconds=1)
        result = run_batch(spec, self.ws)
        self.assertEqual(result['status'], 'budget_exhausted')
        report = load_report(self.ws, result['report'], require_current=False)
        self.assertEqual(report['results'][0]['status'], 'timeout')
        self.assertEqual(report['results'][1]['status'], 'blocked')
        self.assertEqual(report['results'][1]['reason'], 'batch_time_budget')
        self.assertNotIn('receipt', report['results'][1])
        self.assertEqual(report['usage']['check_processes'], 1)
        self.assertFalse((self.ws / 'unexpected').exists())
        with self.assertRaises(ValueError):
            load_report(self.ws, result['report'])

    def test_early_failure_is_fast_feedback_not_missing_mandatory_acceptance(self):
        slow = self.check('slow', 'from pathlib import Path;Path("unexpected").write_text("ran")', estimated_seconds=10)
        fast = self.check('failure', 'import sys;sys.exit(3)', estimated_seconds=.01)
        spec = dict(self.spec([slow, fast]), process_slots=1, fail_fast=True, scheduling='shortest_first')
        result = run_batch(spec, self.ws)
        report = load_report(self.ws, result['report'], require_current=False)
        self.assertEqual(result['status'], 'needs_repair')
        self.assertEqual([r['status'] for r in report['results']], ['blocked', 'failed'])
        self.assertEqual(report['results'][0]['reason'], 'fail_fast')
        self.assertEqual(report['usage']['check_processes'], 1)
        self.assertEqual(report['omitted_checks_with_reason'], [{'id': 'slow', 'reason': 'fail_fast'}])
        self.assertFalse((self.ws / 'unexpected').exists())
        with self.assertRaises(ValueError):
            load_report(self.ws, result['report'])

    def test_estimate_order_still_respects_dependencies_and_resources(self):
        def write(letter):
            return f'from pathlib import Path;p=Path("order");p.write_text((p.read_text() if p.exists() else "")+"{letter}")'
        checks = [self.check('a', write('a'), estimated_seconds=1, resources=['shared']),
                  self.check('b', write('b'), estimated_seconds=.01, depends_on=['a'], resources=['shared']),
                  self.check('c', write('c'), estimated_seconds=2, resources=['shared'])]
        result = run_batch(dict(self.spec(checks), scheduling='shortest_first'), self.ws)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual((self.ws / 'order').read_text(), 'abc')

    def test_diagnosis_does_not_invent_unknown_durations(self):
        from adhd.validation_batch import diagnose_plan
        spec = dict(self.spec([self.check('known', estimated_seconds=60), self.check('unknown')]),
                    time_limit_seconds=5)
        result = diagnose_plan(spec)
        self.assertEqual(result['timing']['unknown_checks'], ['unknown'])
        self.assertIsNone(result['timing']['estimated_total_seconds'])
        self.assertTrue(any(row['code'] == 'estimated_work_exceeds_budget' for row in result['warnings']))


if __name__ == '__main__':
    unittest.main()
