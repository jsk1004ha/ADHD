from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from adhd.native_install import audit_native, install_native, rollback_native, upgrade_native


class RoleEffortInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.codex = self.root / 'codex'
        self.agents = self.root / 'agent-skills'
        self.codex.mkdir()
        (self.codex / 'config.toml').write_text('model = "gpt-6-sol"\n', encoding='utf-8')
        (self.codex / 'AGENTS.md').write_text('user guidance\n', encoding='utf-8')
        (self.codex / 'hooks.json').write_text('{"hooks": {}}\n', encoding='utf-8')

    def policy(self, value):
        path = self.codex / 'adhd' / 'native-policy.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'role_effort': value}), encoding='utf-8')

    def effort(self, role):
        path = self.codex / 'agents' / (role + '.toml')
        return tomllib.loads(path.read_text(encoding='utf-8'))['model_reasoning_effort']

    def test_default_and_explicit_default_are_noops(self):
        installed = install_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual(self.effort('adhd-scout'), 'max')
        self.assertEqual(self.effort('adhd-light'), 'max')
        report = audit_native(self.codex)
        self.assertIsNone(report['role_effort_policy_error'])
        self.assertEqual(report['role_effort_drift'], [])
        self.assertEqual(report['role_effort_missing_profiles'], [])
        self.assertEqual(report['role_effort_invalid_profiles'], [])
        with patch('adhd.native_install.release_identity', return_value=installed['source_identity']):
            self.assertEqual(upgrade_native(self.codex, self.agents, fixture_mode=True)['status'], 'already_installed')
            self.policy({'adhd-scout': 'max'})
            self.assertEqual(upgrade_native(self.codex, self.agents, fixture_mode=True)['status'], 'already_installed')
        rollback_native(self.codex)

    def test_policy_only_upgrade_and_self_upgrade_restore_profiles(self):
        installed = install_native(self.codex, self.agents, fixture_mode=True)
        self.policy({'adhd-scout': 'low', 'adhd-light': 'low'})
        self.assertEqual(audit_native(self.codex)['role_effort_drift'], ['adhd-light', 'adhd-scout'])
        upgraded = upgrade_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual(upgraded['upgraded_from'], installed['version'])
        self.assertNotEqual(upgraded['role_effort_fingerprint'], installed['role_effort_fingerprint'])
        self.assertEqual(self.effort('adhd-scout'), 'low')
        self.assertEqual(self.effort('adhd-light'), 'low')
        self.assertEqual(audit_native(self.codex)['role_effort_drift'], [])

        self.policy({})
        with patch('adhd.native_install.ROOT', Path(upgraded['release'])):
            restored = upgrade_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual(restored['upgraded_from'], installed['version'])
        self.assertEqual(self.effort('adhd-scout'), 'max')
        self.assertEqual(self.effort('adhd-light'), 'max')
        self.assertEqual(audit_native(self.codex)['role_effort_drift'], [])
        rollback_native(self.codex)

    def test_invalid_policy_is_diagnostic_and_never_installed(self):
        for value in (None, [], {'adhd-scout': []}, {'adhd-light': {}}, {'adhd-verifier': 'low'}):
            with self.subTest(value=value):
                self.policy(value)
                with self.assertRaisesRegex(ValueError, 'role_effort'):
                    install_native(self.codex, self.agents, fixture_mode=True)
                self.assertFalse((self.codex / 'adhd' / 'native-installation.json').exists())
                self.assertFalse((self.codex / 'agents' / 'adhd-scout.toml').exists())
                self.assertIsNotNone(audit_native(self.codex)['role_effort_policy_error'])

    def test_doctor_distinguishes_policy_missing_invalid_and_drift(self):
        installed = install_native(self.codex, self.agents, fixture_mode=True)
        scout = self.codex / 'agents' / 'adhd-scout.toml'
        light = self.codex / 'agents' / 'adhd-light.toml'
        scout_bytes = scout.read_bytes()
        light_bytes = light.read_bytes()
        scout.unlink()
        report = audit_native(self.codex)
        self.assertEqual(report['role_effort_missing_profiles'], ['adhd-scout'])
        self.assertEqual(report['role_effort_invalid_profiles'], [])
        scout.write_bytes(scout_bytes)
        light.write_text('invalid = [', encoding='utf-8')
        report = audit_native(self.codex)
        self.assertEqual(report['role_effort_invalid_profiles'], ['adhd-light'])
        for value in ('["low"]', '{level = "low"}'):
            with self.subTest(profile_effort=value):
                light.write_text(light_bytes.decode('utf-8').replace('model_reasoning_effort = "max"',
                                 'model_reasoning_effort = ' + value), encoding='utf-8')
                self.assertEqual(audit_native(self.codex)['role_effort_invalid_profiles'], ['adhd-light'])
        light.write_bytes(light_bytes)
        light.write_text(light_bytes.decode('utf-8').replace('model_reasoning_effort = "max"',
                                                         'model_reasoning_effort = "low"'), encoding='utf-8')
        report = audit_native(self.codex)
        self.assertEqual(report['role_effort_drift'], ['adhd-light'])
        light.write_bytes(light_bytes)
        self.policy({'adhd-scout': None})
        report = audit_native(self.codex)
        self.assertIsNotNone(report['role_effort_policy_error'])
        self.assertEqual(report['role_effort_drift'], [])
        self.policy({})
        rollback_native(self.codex)

    def test_foreign_target_policy_ignores_ambient_adhd_home(self):
        ambient = self.root / 'ambient-state'
        ambient.mkdir()
        (ambient / 'native-policy.json').write_text('{"role_effort": {"adhd-scout": "low"}}', encoding='utf-8')
        with patch.dict(os.environ, {'ADHD_HOME': str(ambient)}):
            install_native(self.codex, self.agents, fixture_mode=True)
            self.assertEqual(self.effort('adhd-scout'), 'max')
            rollback_native(self.codex)
            self.policy({'adhd-scout': 'low'})
            install_native(self.codex, self.agents, fixture_mode=True)
            self.assertEqual(self.effort('adhd-scout'), 'low')
            self.assertEqual(audit_native(self.codex)['role_effort_drift'], [])
            rollback_native(self.codex)

    def test_policy_only_upgrade_failure_restores_old_install(self):
        installed = install_native(self.codex, self.agents, fixture_mode=True)
        record_path = self.codex / 'adhd' / 'native-installation.json'
        before_record = record_path.read_bytes()
        before_scout = (self.codex / 'agents' / 'adhd-scout.toml').read_bytes()
        self.policy({'adhd-scout': 'low'})
        with patch('adhd.native_install._install_native', side_effect=RuntimeError('injected install failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected install failure'):
                upgrade_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual(record_path.read_bytes(), before_record)
        self.assertEqual((self.codex / 'agents' / 'adhd-scout.toml').read_bytes(), before_scout)
        self.assertEqual(audit_native(self.codex)['role_effort_drift'], ['adhd-scout'])
        self.policy({})
        rollback_native(self.codex)


if __name__ == '__main__':
    unittest.main()
