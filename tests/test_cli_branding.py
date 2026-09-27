from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BRAND = 'ADHD — Autonomous Delegation Harness Director'


class CliBrandingTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows console encoding regression')
    def test_primary_and_compatibility_help_work_with_cp949_stdout(self):
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'cp949'
        for entry in ('adhd.py', 'apzn.py'):
            with self.subTest(entry=entry):
                result = subprocess.run(
                    [sys.executable, str(ROOT / entry), '--help'],
                    cwd=ROOT, env=env, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
                self.assertIn(BRAND.encode('utf-8'), result.stdout)

    def test_primary_entrypoint_install_and_uninstall_use_native_adhd_record(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / 'codex'
            codex_home.mkdir()
            (codex_home / 'config.toml').write_text('model="gpt-6-sol"\n', encoding='utf-8')
            (codex_home / 'AGENTS.md').write_text('keep existing guidance\n', encoding='utf-8')
            (codex_home / 'hooks.json').write_text('{"hooks":{}}\n', encoding='utf-8')
            argv = [sys.executable, str(ROOT / 'adhd.py')]
            install = subprocess.run(argv + ['install', '--codex-home', str(codex_home)],
                                     cwd=ROOT, capture_output=True, check=False)
            self.assertEqual(install.returncode, 0, install.stderr.decode('utf-8', errors='replace'))
            self.assertTrue((codex_home / 'adhd/native-installation.json').is_file())
            remove = subprocess.run(argv + ['uninstall', '--codex-home', str(codex_home)],
                                    cwd=ROOT, capture_output=True, check=False)
            self.assertEqual(remove.returncode, 0, remove.stderr.decode('utf-8', errors='replace'))
            self.assertFalse((codex_home / 'adhd/native-installation.json').exists())
            self.assertEqual((codex_home / 'AGENTS.md').read_text(encoding='utf-8'),
                             'keep existing guidance\n')


if __name__ == '__main__':
    unittest.main()
