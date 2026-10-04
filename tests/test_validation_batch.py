from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

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
        checks = [self.check('a', impact_complete=True), self.check('b', paths=['b.txt'], impact_complete=True),
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


if __name__ == '__main__':
    unittest.main()
