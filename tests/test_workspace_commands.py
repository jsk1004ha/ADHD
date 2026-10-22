from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from adhd.workspace_commands import run_in_scratch
from adhd.workspace_storage import WorkspaceStorage


class WorkspaceCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name).resolve() / 'project'
        self.ws.mkdir()
        guard = patch.dict(os.environ, {'ADHD_HOME': str(self.ws.parent / 'state')})
        guard.start()
        self.addCleanup(guard.stop)
        self.manager = WorkspaceStorage(self.ws)

    def run_code(self, code, budget=4096):
        return run_in_scratch(self.manager, {'argv': [sys.executable, '-B', '-c', code],
                            'reserved_bytes': budget, 'minimum_free': 0, 'timeout': 5})

    def test_real_command_cleanup_and_results_preserved(self):
        source = self.ws / 'original.txt'
        source.write_text('original')
        for _ in range(3):
            result = self.run_code("import os,pathlib;pathlib.Path(os.environ['ADHD_SCRATCH'],'copy').write_bytes(b'proof');print('verified')")
            self.assertEqual(result['status'], 'complete')
            output = self.ws / result['results']
            self.assertEqual((output / 'stdout.log').read_text().strip(), 'verified')
            self.assertTrue((output / 'report.json').is_file())
            self.assertFalse((self.manager.scratch_root / result['scratch_id']).exists())
        self.assertEqual(source.read_text(), 'original')

    def test_failing_command_still_cleans_own_data(self):
        result = self.run_code("import os,pathlib,sys;pathlib.Path(os.environ['ADHD_SCRATCH'],'copy').write_bytes(b'x');sys.exit(7)")
        self.assertEqual((result['status'], result['exit_code']), ('failed', 7))
        self.assertFalse((self.manager.scratch_root / result['scratch_id']).exists())

    def test_observed_budget_overrun_is_failure(self):
        result = self.run_code("import os,pathlib;pathlib.Path(os.environ['ADHD_SCRATCH'],'copy').write_bytes(b'x'*8192)")
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'scratch_budget_exceeded')
        self.assertFalse((self.manager.scratch_root / result['scratch_id']).exists())

    def test_short_lived_command_log_overflow_is_failure_and_bounded(self):
        result=self.run_code("import sys;sys.stdout.buffer.write(b'x'*(11*1024**2))")
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reason'],'log_budget_exceeded')
        self.assertEqual((self.ws/result['stdout']).stat().st_size,10*1024**2)

    def test_reservation_includes_retained_log_payload(self):
        original=self.manager.reserve
        with patch.object(self.manager,'reserve',wraps=original) as reserve:
            self.run_code('pass',budget=4096)
        self.assertEqual(reserve.call_args.args[1],4096+20*1024**2)

    def test_latest_logs_are_retained_but_prior_reports_and_pinned_logs_stay(self):
        first=self.run_code("print('old')")
        pinned=run_in_scratch(self.manager,{'argv':[sys.executable,'-c',"print('pin')"],
                    'reserved_bytes':4096,'minimum_free':0,'retain_logs':True})
        latest=self.run_code("print('latest')")
        self.assertFalse((self.ws/first['stdout']).exists())
        self.assertTrue((self.ws/first['results']/'report.json').is_file())
        self.assertTrue((self.ws/pinned['stdout']).is_file())
        self.assertEqual((self.ws/latest['stdout']).read_text().strip(),'latest')


if __name__ == '__main__':
    unittest.main()
