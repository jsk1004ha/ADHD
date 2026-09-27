from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apzn.evaluation import run_evaluation


class PersonalEvaluationTests(unittest.TestCase):
    def test_scorecard_separates_fixture_pass_from_live_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'scorecard.json'
            with patch('apzn.evaluation.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=b'', stderr=b'OK')) as process:
                result = run_evaluation(output, root=root,
                    scenarios=(('fixture','tests.test_v012.ProvenanceTests.test_mismatched_report_number_rejected'),))
            self.assertEqual(process.call_count,1)
            self.assertEqual(result['overall'],'passed_fixture_suite')
            self.assertIsNone(result['metrics']['human_review_minutes'])
            self.assertEqual(result['live_integrations']['browser_user_flow'],'not_run')
            self.assertTrue(output.is_file())


if __name__ == '__main__':
    unittest.main()
