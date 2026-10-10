"""Execution-count and freshness regressions for bounded verification reuse."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from adhd import native, validation_batch as batch
from adhd.core import file_hash
from adhd.evidence import run_check, validate_execution


class VerificationEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name)
        for i in range(8):
            (self.ws / f'f{i}.txt').write_text(str(i), encoding='utf-8')

    def check(self, i, **extra):
        return {'id': f'c{i}', 'argv': [sys.executable, '-c', 'print("ok")'],
                'subject_paths': [f'f{i}.txt'], 'requirements': ['R1'],
                'impact_complete': True, 'environment_complete': True,
                'dependency_paths': [], 'fixture_paths': [], 'environment_vars': [], **extra}

    def spec(self, checks):
        return {'run_id': 'count-test', 'contract_revision': 1, 'contract_hash': 'abc',
                'requirements': ['R1'], 'mandatory_checks': ['c0'],
                'process_slots': 2, 'checks': checks}

    def build_fixture(self, *, cover_inputs=True):
        (self.ws / 'artifact.zip').write_bytes(b'build')
        manifest = {'artifact': 'artifact.zip', 'flags': [], 'environment_hash': 'build-env'}
        for group, name in [('source_hashes', 'f0.txt'), ('toolchain_hashes', 'f1.txt'),
                            ('dependency_hashes', 'f2.txt')]:
            manifest[group] = {name: file_hash(self.ws / name)}
        receipt = run_check({'run_id': 'count-test', 'contract_revision': 1,
                             'argv': [sys.executable, '-c',
                                      'from pathlib import Path; Path("artifact.zip").write_bytes(b"build")'],
                             'subject_paths': ['artifact.zip'] +
                                              (['f0.txt', 'f1.txt', 'f2.txt'] if cover_inputs else [])},
                            self.ws)['receipt']
        previous = {**batch.build_decision(self.ws, manifest), 'receipt': receipt,
                    'run_id': 'count-test', 'contract_revision': 1}
        return manifest, previous

    def test_build_reuse_requires_current_observed_source_toolchain_and_dependencies(self):
        manifest, previous = self.build_fixture()
        self.assertTrue(batch.build_decision(self.ws, manifest, previous)['reuse'])
        for name in ('f0.txt', 'f1.txt', 'f2.txt'):
            with self.subTest(input=name):
                path = self.ws / name
                original = path.read_bytes()
                path.write_bytes(b'changed build input')
                self.assertFalse(batch.build_decision(self.ws, manifest, previous)['reuse'])
                path.write_bytes(original)
                self.assertTrue(batch.build_decision(self.ws, manifest, previous)['reuse'])

    def test_output_only_build_receipt_cannot_authorize_stale_manifest_reuse(self):
        manifest, previous = self.build_fixture(cover_inputs=False)
        (self.ws / 'f0.txt').write_bytes(b'changed without updating manifest')
        result = batch.build_decision(self.ws, manifest, previous)
        self.assertFalse(result['reuse'])
        self.assertEqual(result['evidence_refs'], [])

    def test_build_hash_identifiers_without_observed_paths_fail_closed(self):
        manifest, previous = self.build_fixture()
        manifest['source_hashes'] = {'unobserved-source-id': file_hash(self.ws / 'f0.txt')}
        previous.update(batch.build_decision(self.ws, manifest))
        self.assertFalse(batch.build_decision(self.ws, manifest, previous)['reuse'])

    def test_eight_then_two_then_zero_processes_preserve_dependency_checks(self):
        spec = self.spec([self.check(0), self.check(1, depends_on=['c0']),
                          *[self.check(i) for i in range(2, 8)]])
        counts = []
        for iteration in range(3):
            if iteration == 1:
                (self.ws / 'f0.txt').write_text('repair', encoding='utf-8')
            with patch.object(batch, 'run_check', wraps=run_check) as executions:
                result = batch.run_batch(spec, self.ws)
                counts.append(executions.call_count)
            report = batch.load_report(self.ws, result['report'])
            self.assertEqual(report['usage']['check_processes'], counts[-1])
            self.assertEqual(report['verification']['round'], iteration + 1)
            spec['previous_report'] = result['report']
        self.assertEqual(counts, [8, 2, 0])
        self.assertEqual(result['verification']['reused_checks'], 8)

    def test_adding_a_check_does_not_invalidate_unchanged_mandatory_check(self):
        spec = self.spec([self.check(0)])
        first = batch.run_batch(spec, self.ws)
        spec['checks'].append(self.check(1))
        spec['mandatory_checks'].append('c1')
        spec['previous_report'] = first['report']
        second = batch.run_batch(spec, self.ws)
        report = batch.load_report(self.ws, second['report'])
        self.assertEqual([r['status'] for r in report['results']], ['reused', 'passed'])
        self.assertEqual(report['results'][1]['invalidation_reason'], 'new_check')

    def test_environment_change_invalidates_only_the_declaring_check(self):
        spec = self.spec([self.check(0, environment_vars=['ADHD_COUNT_A']),
                          self.check(1, environment_vars=['ADHD_COUNT_B'])])
        with patch.dict(os.environ, {'ADHD_COUNT_A': 'a', 'ADHD_COUNT_B': 'b'}):
            first = batch.run_batch(spec, self.ws)
            os.environ['ADHD_COUNT_B'] = 'changed'
            spec['previous_report'] = first['report']
            second = batch.run_batch(spec, self.ws)
            report = batch.load_report(self.ws, second['report'])
        self.assertEqual([r['status'] for r in report['results']], ['reused', 'passed'])
        self.assertEqual(report['results'][1]['invalidation_reason'], 'environment_changed')

    def test_fixture_change_reruns_its_check_without_invalidating_others(self):
        spec = self.spec([self.check(0, fixture_paths=['f7.txt']), self.check(1)])
        first = batch.run_batch(spec, self.ws)
        (self.ws / 'f7.txt').write_text('changed fixture', encoding='utf-8')
        spec['previous_report'] = first['report']
        report = batch.load_report(self.ws, batch.run_batch(spec, self.ws)['report'])
        self.assertEqual([r['status'] for r in report['results']], ['passed', 'reused'])

    def test_tampered_log_cannot_be_reused(self):
        spec = self.spec([self.check(0)])
        first = batch.run_batch(spec, self.ws)
        report = batch.load_report(self.ws, first['report'])
        logs = self.ws / Path(report['results'][0]['receipt']).parent / 'check.stderr.log'
        logs.write_text('tampered', encoding='utf-8')
        spec['previous_report'] = first['report']
        second = batch.run_batch(spec, self.ws)
        row = batch.load_report(self.ws, second['report'])['results'][0]
        self.assertEqual(row['status'], 'passed')
        self.assertEqual(row['invalidation_reason'], 'receipt_invalid')
        self.assertNotEqual(row['receipt'], report['results'][0]['receipt'])

    def test_contract_change_and_unknown_impact_still_force_execution(self):
        spec = self.spec([self.check(0), self.check(1, impact_complete=False)])
        first = batch.run_batch(spec, self.ws)
        spec.update(previous_report=first['report'], contract_hash='new-contract')
        second = batch.run_batch(spec, self.ws)
        report = batch.load_report(self.ws, second['report'])
        self.assertEqual([r['status'] for r in report['results']], ['passed', 'passed'])

    def test_held_failure_is_not_reported_as_a_new_check_or_success(self):
        spec = self.spec([self.check(0, argv=[sys.executable, '-c', 'raise AssertionError("broken")'])])
        first = batch.run_batch(spec, self.ws)
        spec['previous_report'] = first['report']
        second = batch.run_batch(spec, self.ws)
        report = batch.load_report(self.ws, second['report'], require_current=False)
        self.assertEqual(report['status'], 'needs_repair')
        self.assertEqual(report['verification']['new_checks'], 0)
        self.assertEqual(report['verification']['held_checks'], 1)
        self.assertEqual(report['usage']['check_processes'], 0)
        self.assertEqual(report['verification']['invalidation_reasons'], {})

    def test_failed_setup_is_an_unobserved_attempt_not_a_process_receipt(self):
        result = batch.run_batch(self.spec([self.check(0, cwd='missing')]), self.ws)
        report = batch.load_report(self.ws, result['report'], require_current=False)
        self.assertEqual(report['usage']['check_processes'], 0)
        self.assertEqual(report['usage']['unobserved_execution_attempts'], 1)
        self.assertEqual(report['verification']['new_checks'], 1)

    def test_shared_receipt_is_checked_once_per_call_and_freshly_on_next_call(self):
        receipt = run_check({'run_id': 'count-test', 'contract_revision': 1,
                             'argv': [sys.executable, '-c', 'print("ok")'],
                             'subject_paths': ['f0.txt']}, self.ws)['receipt']
        state = {'workspace': str(self.ws), 'run_id': 'count-test', 'intent_version': 1,
                 'contract': {'criteria': [{'id': f'R{i}', 'kind': 'test'} for i in range(1, 9)]}}
        rows = [{'id': f'R{i}', 'pass': True, 'evidence': 'actual shared check',
                 'evidence_ids': [receipt]} for i in range(1, 9)]
        with patch.object(native, 'validate_execution', wraps=validate_execution) as validations:
            native.result_valid(state, rows, require_execution=True)
            self.assertEqual(validations.call_count, 1)
            native.result_valid(state, rows, require_execution=True)
            self.assertEqual(validations.call_count, 2)
            (self.ws / 'f0.txt').write_text('changed after review', encoding='utf-8')
            with self.assertRaises(ValueError):
                native.result_valid(state, rows, require_execution=True)
            self.assertEqual(validations.call_count, 3)

    def test_executable_hash_is_deduplicated_only_inside_each_fresh_phase(self):
        checks = [self.check(i) for i in range(8)]
        executable = Path(sys.executable).resolve()
        with patch.object(batch, 'file_hash', wraps=batch.file_hash) as hashes:
            snapshot = batch.freeze(self.ws, [f'f{i}.txt' for i in range(8)],
                                    run_id='count-test', revision=1, checks=checks)
            self.assertEqual(sum(Path(c.args[0]).resolve() == executable for c in hashes.call_args_list), 1)
            hashes.reset_mock()
            self.assertTrue(batch._tools_current(snapshot))
            self.assertEqual(sum(Path(c.args[0]).resolve() == executable for c in hashes.call_args_list), 1)
        with patch.object(batch, 'file_hash', return_value='changed executable'):
            self.assertFalse(batch._tools_current(snapshot))

    def test_diagnostics_do_not_silently_rewrite_critical_or_scope(self):
        checks = [self.check(i, subject_paths=['f0.txt'], critical=True,
                             environment_complete=False) for i in range(3)]
        spec = self.spec(checks)
        original = copy.deepcopy(spec)
        diagnostic = batch.diagnose_plan(spec)
        self.assertEqual(spec, original)
        codes = {r['code'] for r in diagnostic['warnings']}
        self.assertTrue({'all_checks_critical', 'shared_input_scope', 'incomplete_environment'} <= codes)
        self.assertFalse(diagnostic['reuse_ready'])

    def test_diagnostic_cli_does_not_execute_checks(self):
        from adhd.cli import parser
        from adhd.large_cli import execute
        plan = self.ws / 'plan.json'
        plan.write_text(json.dumps(self.spec([self.check(0)])), encoding='utf-8')
        args = parser().parse_args(['batch', 'diagnose', '--workspace', str(self.ws), '--spec-file', str(plan)])
        with patch.object(batch, 'run_check', side_effect=AssertionError('diagnostic executed tests')):
            result = execute(args)
        self.assertTrue(result['reuse_ready'])


if __name__ == '__main__':
    unittest.main()
