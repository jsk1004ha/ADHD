import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adhd.host_capabilities import capability_inventory
from adhd.review_calibration import calibrate
from adhd.validation_batch import suggest_inputs


class DiagnosticTests(unittest.TestCase):
    def test_configured_exposed_and_success_are_separate_from_auth(self):
        entries = capability_inventory(['tool', 'role'], ['tool'], [
            {'name': 'tool', 'status': 'auth_required', 'call_id': 'one', 'evidence_ref': 'denial.json'}])['capabilities']
        tool = next(row for row in entries if row['name'] == 'tool')
        self.assertEqual('exposed', tool['state'])
        self.assertFalse(tool['observed_success'])
        self.assertEqual('auth_required', tool['blocker'])
        observed = capability_inventory(['tool'], ['tool'], [
            {'name': 'tool', 'status': 'success', 'call_id': 'two', 'evidence_ref': 'receipt.json'}])
        self.assertTrue(observed['capabilities'][0]['observed_success'])
        self.assertEqual(0, observed['capabilities'][0]['additional_probe_calls'])

    def test_static_hints_do_not_claim_dynamic_impact_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'check.py').write_text('import importlib, os\nimportlib.import_module(os.getenv("DRIVER"))\nopen("fixture.json")\n')
            spec = {'checks': [{'id': 'a', 'argv': ['python', 'check.py'], 'subject_paths': ['check.py']} ]}
            result = suggest_inputs(root, spec)
            self.assertIn('dynamic_dependency', result['checks'][0]['uncertainty'])
            self.assertIn('DRIVER', result['checks'][0]['environment_vars'])
            self.assertFalse(result['checks'][0]['impact_complete'])
            self.assertFalse(result['manifest_rewritten'])

    def test_normal_and_faulty_reviews_measure_both_error_directions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cases, reviews = Path(tmp), [], []
            for name, expected, decision in [('good', 'accept', 'reject'), ('broken', 'reject', 'accept')]:
                (root / name).write_text(name)
                (root / (name + '.review')).write_text(decision)
                sha = lambda path: hashlib.sha256((root / path).read_bytes()).hexdigest()
                cases.append(dict(id=name, expected=expected, artifact=name, sha256=sha(name), writer_id='writer'))
                reviews.append(dict(case_id=name, decision=decision, artifact_sha256=sha(name),
                                    reviewer_id='independent', report=name + '.review', report_sha256=sha(name + '.review')))
            result = calibrate(root, cases, reviews)
            self.assertEqual(1, result['false_accepts'])
            self.assertEqual(1, result['false_rejects'])
            self.assertTrue(result['complete'])
            self.assertFalse(result['native_acceptance_changed'])
            (root / 'good').write_text('changed after review')
            self.assertFalse(calibrate(root, cases, reviews)['complete'])

    def test_manifest_input_budget_marks_uninspected_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            fixture=root/'check.py';fixture.write_text('value=1\n')
            spec={'checks':[{'id':'large','argv':['python','check.py'],
                             'subject_paths':[f'input-{i}.py' for i in range(501)]}]}
            with patch('adhd.obsidian_index.safe_path',return_value=fixture):
                row=suggest_inputs(root,spec)['checks'][0]
            self.assertIn('input_budget_exceeded',row['uncertainty'])
            self.assertTrue(row['requires_fresh'])
