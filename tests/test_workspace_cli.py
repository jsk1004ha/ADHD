"""Actual diagnostic CLI producers clean temporary output without a second command."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceCliTests(unittest.TestCase):
    def test_direct_argv_generates_temp_and_retains_final_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ws = base / 'workspace'
            ws.mkdir()
            original = ws / 'original.txt'
            original.write_text('keep')
            env = {**os.environ, 'CODEX_HOME': str(base / 'codex'), 'ADHD_HOME': str(base / 'state')}
            process = subprocess.run([sys.executable, '-B', str(ROOT / 'adhd.py'),
                'workspace-storage', 'run', '--workspace', str(ws), '--max-bytes', '4096',
                '--timeout', '5', '--owner', 'diagnostic', '--argv', sys.executable, '-B', '-c',
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('temp'); "
                "pathlib.Path(sys.argv[2]).write_text('verified')",
                '{scratch}/generated.json', '{results}/proof.txt'],
                capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result['cleanup']['status'], 'cleaned')
            self.assertEqual(list((ws / '.adhd/scratch').iterdir()), [])
            self.assertEqual((ws / result['results'] / 'proof.txt').read_text(), 'verified')
            self.assertEqual(original.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
