from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adhd.native_install import audit_native, install_native, release_identity, rollback_native, upgrade_native


class NativeInstallIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex = self.root / 'codex'
        self.agents = self.root / 'agents'
        self.codex.mkdir()
        (self.codex / 'config.toml').write_text('model="gpt-6-sol"\n', encoding='utf-8')
        (self.codex / 'AGENTS.md').write_text('keep user guidance\n', encoding='utf-8')
        (self.codex / 'hooks.json').write_text(json.dumps({'hooks': {}}), encoding='utf-8')
        self.original = {p.relative_to(self.codex).as_posix(): p.read_bytes()
                         for p in self.codex.rglob('*') if p.is_file()}

    def tearDown(self):
        self.temp.cleanup()

    def test_record_hashes_exact_rendered_release_bytes(self):
        record = install_native(self.codex, self.agents, fixture_mode=True)
        release = Path(record['release'])
        self.assertEqual(record['release_identity'], release_identity(release))
        self.assertTrue(record['release_identity']['files'])
        self.assertNotEqual(record['release_identity']['code_digest'], record['source_identity']['code_digest'])
        self.assertNotIn('ADHD_ROOT', (release / 'skills/adhd-native/SKILL.md').read_text(encoding='utf-8'))
        rollback_native(self.codex)
        for relative, content in self.original.items():
            self.assertEqual((self.codex / relative).read_bytes(), content)

    def test_doctor_reports_installed_release_tamper(self):
        record = install_native(self.codex, self.agents, fixture_mode=True)
        hook = Path(record['release']) / 'hook.py'
        hook.write_bytes(hook.read_bytes() + b'\n# tampered\n')
        report = audit_native(self.codex)
        self.assertIn(record['release'], report['installation_drift'])

    def test_upgrade_refuses_noop_when_installed_release_is_tampered(self):
        record = install_native(self.codex, self.agents, fixture_mode=True)
        installed_config = (self.codex / 'config.toml').read_bytes()
        hook = Path(record['release']) / 'hook.py'
        hook.write_bytes(hook.read_bytes() + b'\n# tampered\n')
        with self.assertRaisesRegex(ValueError, r'^Installed release file size changed: hook\.py$'):
            upgrade_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual((self.codex / 'config.toml').read_bytes(), installed_config)
        self.assertEqual((self.codex / 'AGENTS.md').read_bytes().split(b'\n\n')[0] + b'\n', self.original['AGENTS.md'])

    def test_destination_conflict_is_rejected_before_release_creation(self):
        conflict = self.agents / 'skills/adhd-native/SKILL.md'
        conflict.parent.mkdir(parents=True)
        conflict.write_text('user owned', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unmanaged adhd-native skill'):
            install_native(self.codex, self.agents, fixture_mode=True)
        releases = self.codex / 'adhd/releases'
        self.assertFalse(releases.exists() and any(releases.iterdir()))
        self.assertEqual(conflict.read_text(encoding='utf-8'), 'user owned')


if __name__ == '__main__':
    unittest.main()
