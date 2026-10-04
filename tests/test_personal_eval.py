from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adhd.evaluation import run_evaluation


class PersonalEvaluationTests(unittest.TestCase):
    def test_scorecard_separates_fixture_pass_from_live_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'scorecard.json'
            with patch('adhd.evaluation.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=b'', stderr=b'OK')) as process:
                result = run_evaluation(output, root=root,
                    scenarios=(('fixture','tests.test_v012.ProvenanceTests.test_mismatched_report_number_rejected'),))
            self.assertEqual(process.call_count,1)
            self.assertEqual(result['overall'],'passed_fixture_suite')
            self.assertIsNone(result['metrics']['human_review_minutes'])
            self.assertEqual(result['live_integrations']['browser_user_flow'],'not_run')
            self.assertEqual(result['execution_profile']['name'], 'standard')
            self.assertIn('not measured', result['profile_evaluation'])
            self.assertTrue(output.is_file())

    def test_explicit_profile_is_recorded_without_cost_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('adhd.evaluation.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=b'', stderr=b'OK')):
                result = run_evaluation(root / 'scorecard.json', root=root,
                    scenarios=(('fixture', 'tests.test_personal_eval.PersonalEvaluationTests.test_scorecard_separates_fixture_pass_from_live_unknowns'),),
                    execution_profile='deep')
            self.assertEqual(result['execution_profile']['name'], 'deep')
            self.assertIsNone(result['metrics']['price_per_verified_task'])


if __name__ == '__main__':
    unittest.main()
