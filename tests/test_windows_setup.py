"""Bundled Windows setup checks that do not require a global Codex install."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from adhd import native_install, windows_setup
from build import build_windows_installer as builder


class RuntimePayloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime_zip = self.root / 'runtime.zip'

    def write_zip(self, names):
        with zipfile.ZipFile(self.runtime_zip, 'w') as archive:
            for name in names:
                archive.writestr(name, b'fixture')

    def test_runtime_hash_rejects_unknown_archive(self):
        self.write_zip(['python.exe'])
        with self.assertRaisesRegex(ValueError, 'digest'):
            builder.runtime_files(self.runtime_zip)

    def test_runtime_archive_rejects_unsafe_path_even_when_hash_is_supplied(self):
        self.write_zip(['../python.exe'])
        digest = hashlib.sha256(self.runtime_zip.read_bytes()).hexdigest()
        with patch.object(builder, 'PYTHON_ZIP_SHA256', digest):
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                builder.runtime_files(self.runtime_zip)

    def test_runtime_rewrites_only_module_path_and_keeps_license(self):
        names = ['python.exe', 'python313.dll', 'python313.zip', 'python313._pth',
                 '_ssl.pyd', '_sqlite3.pyd', 'sqlite3.dll', 'LICENSE.txt']
        self.write_zip(names)
        digest = hashlib.sha256(self.runtime_zip.read_bytes()).hexdigest()
        with patch.object(builder, 'PYTHON_ZIP_SHA256', digest):
            files = builder.runtime_files(self.runtime_zip)
        self.assertEqual(files['LICENSE.txt'], b'fixture')
        self.assertIn(b'..\\third_party', files['python313._pth'])
        self.assertIn(b'Original ZIP SHA-256', files['RUNTIME-CHANGES.txt'])


class PersistentRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_rendered_skill_references_immutable_runtime(self):
        staging = self.root / 'staging'
        final = self.root / 'codex' / 'adhd' / 'releases' / 'new'
        skill = staging / 'skills' / 'adhd-native' / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('ADHD_PYTHON ADHD_ROOT', encoding='utf-8')
        runtime = staging / 'runtime' / 'python.exe'
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b'fixture')
        native_install._render_release(staging, final, self.root / 'source')
        text = skill.read_text(encoding='utf-8')
        self.assertIn(str(final / 'runtime' / 'python.exe'), text)
        self.assertIn(str(final), text)
        self.assertNotIn(str(staging), text)

    def test_bundle_setup_rejects_missing_runtime_before_changes(self):
        with patch.object(windows_setup, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, 'missing its Python runtime'):
                windows_setup.install_from_bundle(self.root)

    def test_bundle_setup_checks_persistent_hook_and_audit(self):
        source = self.root / 'source'
        runtime = source / 'runtime'
        runtime.mkdir(parents=True)
        (runtime / 'python.exe').write_bytes(b'fixture')
        (runtime / 'python313._pth').write_bytes(builder.PTH)
        home = self.root / 'codex'
        home.mkdir()
        release = home / 'adhd' / 'releases' / 'new'
        persistent = release / 'runtime' / 'python.exe'
        persistent.parent.mkdir(parents=True)
        persistent.write_bytes(b'fixture')
        (home / 'hooks.json').write_text(json.dumps({'hooks': {'UserPromptSubmit': [
            {'hooks': [{'type': 'command', 'command': f'"{persistent}" "{release / "hook.py"}"'}]}]}}),
            encoding='utf-8')
        record = {'release': str(release)}
        with (patch.object(windows_setup, 'ROOT', source),
              patch.object(windows_setup, 'upgrade_native', return_value={'status': 'already_installed'}),
              patch.object(windows_setup, '_find_managed_installation', return_value=(home, home / 'record.json', record)),
              patch.object(windows_setup, 'audit_native', return_value={'installation_drift': []})):
            result = windows_setup.install_from_bundle(home)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'already_installed')
        self.assertEqual(result['python'], str(persistent))


if __name__ == '__main__':
    unittest.main()
