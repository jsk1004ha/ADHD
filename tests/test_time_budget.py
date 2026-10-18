"""Focused validation of opt-in wall-time inputs shared by native and batch work."""

import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adhd.core import ROOT, atomic_json
from adhd.native import bridge, submit_request
from adhd.time_budget import TimeBudgetExpired, validate_seconds


class TimeBudgetTests(unittest.TestCase):
    def test_old_managed_reader_cannot_silently_ignore_new_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp)
            session = 'b' * 24
            directory = bridge(ws, session)
            directory.mkdir(parents=True)
            atomic_json(directory / 'view.json', {'session_reader': {
                'release_root': str(ROOT.resolve()), 'source_sha256': '0' * 64,
                'max_state_version': 2}})
            with patch('adhd.native_install._find_managed_installation',
                       return_value=(None, None, {'release': str(ROOT.resolve())})):
                with self.assertRaisesRegex(ValueError, 'request was not queued'):
                    submit_request(session, ws, 'begin', {'time_limit_seconds': 60})
            self.assertFalse(list(directory.glob('inbox/*.json')))

    def test_positive_fractional_limit_and_zero_reserve(self):
        self.assertEqual(validate_seconds(0.25, 'time_limit_seconds'), 0.25)
        self.assertEqual(validate_seconds(0, 'review_reserve_seconds', allow_zero=True), 0.0)
        self.assertTrue(issubclass(TimeBudgetExpired, RuntimeError))

    def test_invalid_or_nonfinite_values_are_rejected(self):
        for value in (True, False, None, '3', -1, 0, math.inf, -math.inf, math.nan, 86401):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_seconds(value, 'time_limit_seconds')
        with self.assertRaises(ValueError):
            validate_seconds(-0.01, 'review_reserve_seconds', allow_zero=True)


if __name__ == '__main__':
    unittest.main()
