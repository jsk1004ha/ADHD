from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from adhd import storage as cleanup


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.codex = self.root / 'codex'
        self.parent = self.codex / 'adhd'
        self.base = self.parent / 'releases'
        self.base.mkdir(parents=True)
        self.releases = []
        for index in range(1, 5):
            release = self.base / f'0.1.4-2026100{index}-120000-{index:08x}'
            source = release / 'adhd' / 'example.py'
            source.parent.mkdir(parents=True)
            source.write_text(f'value = {index}\n', encoding='utf-8')
            content = source.read_bytes()
            manifest = [{'path': 'adhd/example.py', 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}]
            digest = hashlib.sha256(json.dumps([(row['path'], row['sha256']) for row in manifest],
                                               ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            record = {'version': '0.1.4', 'target': str(self.codex), 'release': str(release),
                      'release_identity': {'files': manifest, 'code_digest': digest}, 'files': []}
            filename = 'native-installation.json' if index == 4 else f'native-uninstalled-{index:08x}.json'
            (self.parent / filename).write_text(json.dumps(record), encoding='utf-8')
            self.releases.append(release)

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_is_read_only_and_keeps_current_plus_rollback(self):
        before = sorted(path.relative_to(self.codex).as_posix() for path in self.codex.rglob('*'))
        report = cleanup.storage(self.codex)
        self.assertTrue(report['dry_run'])
        self.assertEqual([row['name'] for row in report['candidates']], [path.name for path in self.releases[:2]])
        self.assertEqual({row['name'] for row in report['protected']}, {path.name for path in self.releases[2:]})
        self.assertEqual(report['reclaimed_bytes'], 0)
        self.assertEqual(before, sorted(path.relative_to(self.codex).as_posix() for path in self.codex.rglob('*')))

    def test_apply_reclaims_exact_bytes_and_is_idempotent(self):
        preview = cleanup.storage(self.codex)
        backup = self.parent / 'backups' / 'keep'
        backup.parent.mkdir()
        backup.write_bytes(b'original settings')
        report = cleanup.storage(self.codex, apply=True)
        self.assertEqual(report['reclaimed_bytes'], preview['reclaimable_bytes'])
        self.assertEqual(report['removed'], [path.name for path in self.releases[:2]])
        self.assertEqual(backup.read_bytes(), b'original settings')
        self.assertFalse(any(path.name.startswith('.prune-') for path in self.base.iterdir()))
        self.assertEqual(cleanup.storage(self.codex, apply=True)['removed'], [])

    def test_native_state_preserves_terminal_old_session_and_bytes(self):
        state = self.codex / 'prior-harness' / 'native' / ('a' * 24) / 'state.json'
        state.parent.mkdir(parents=True)
        original = json.dumps({'status': 'complete', 'session_verifier_profile': {'release_root': str(self.releases[0])}}).encode()
        state.write_bytes(original)
        report = cleanup.storage(self.codex, apply=True)
        self.assertNotIn(self.releases[0].name, report['removed'])
        self.assertTrue(self.releases[0].exists())
        self.assertEqual(state.read_bytes(), original)

    def test_registered_config_and_custom_role_and_skill_references_preserved(self):
        (self.codex / 'config.toml').write_text('command = ' + json.dumps(str(self.releases[0] / 'hook.py')), encoding='utf-8')
        role = self.codex / 'agents' / 'user.toml'
        role.parent.mkdir()
        role.write_text('instructions = ' + json.dumps(str(self.releases[1])), encoding='utf-8')
        skill = self.codex / 'skills' / 'custom' / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text(str(self.releases[0]), encoding='utf-8')
        self.assertEqual(cleanup.storage(self.codex, apply=True)['removed'], [])

    def test_executing_old_release_is_preserved(self):
        with patch.object(cleanup, 'ROOT', self.releases[0]):
            self.assertNotIn(self.releases[0].name, cleanup.storage(self.codex, apply=True)['removed'])

    def test_modified_release_is_skipped(self):
        source = self.releases[0] / 'adhd/example.py'
        source.write_text('edited by user', encoding='utf-8')
        report = cleanup.storage(self.codex, apply=True)
        self.assertNotIn(self.releases[0].name, report['removed'])
        self.assertEqual(source.read_text(), 'edited by user')
        self.assertTrue(any(row['name'] == self.releases[0].name for row in report['skipped']))

    def test_unknown_release_is_preserved(self):
        unknown = self.base / '0.1.0-20260101-120000-aaaaaaaa'
        unknown.mkdir()
        (unknown / 'keep.txt').write_text('user data')
        cleanup.storage(self.codex, apply=True)
        self.assertEqual((unknown / 'keep.txt').read_text(), 'user data')

    def test_unrecorded_empty_directory_is_preserved(self):
        (self.releases[0] / 'user-directory').mkdir()
        self.assertNotIn(self.releases[0].name, cleanup.storage(self.codex, apply=True)['removed'])

    def test_unrecorded_file_in_pycache_is_preserved(self):
        cache = self.releases[0] / 'adhd/__pycache__'
        cache.mkdir()
        (cache / 'notes.md').write_text('user data')
        self.assertNotIn(self.releases[0].name, cleanup.storage(self.codex, apply=True)['removed'])

    def test_recognized_regenerable_bytecode_is_counted(self):
        cache = self.releases[0] / 'adhd/__pycache__'
        cache.mkdir()
        (cache / 'example.cpython-313.pyc').write_bytes(b'bytecode')
        before = cleanup.storage(self.codex)['reclaimable_bytes']
        self.assertEqual(cleanup.storage(self.codex, apply=True)['reclaimed_bytes'], before)

    def test_corrupt_reference_scan_fails_before_deletion(self):
        state = self.parent / 'native/session/state.json'
        state.parent.mkdir(parents=True)
        state.write_text('{broken json')
        with self.assertRaises(ValueError):
            cleanup.storage(self.codex, apply=True)
        self.assertTrue(all(path.exists() for path in self.releases))

    def test_oversized_reference_fails_before_deletion(self):
        (self.codex / 'AGENTS.md').write_text('x' * (cleanup.MAX_REFERENCE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'bounded scan'):
            cleanup.storage(self.codex, apply=True)
        self.assertTrue(all(path.exists() for path in self.releases))

    def test_retention_cannot_remove_only_rollback(self):
        for keep in (0, 1, 101, True):
            with self.assertRaises(ValueError):
                cleanup.storage(self.codex, apply=True, keep_releases=keep)
        self.assertEqual(cleanup.storage(self.codex, keep_releases=4)['candidates'], [])

    def test_new_reference_between_plan_and_move_prevents_deletion(self):
        original = cleanup._scan
        calls = 0
        def scan(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.codex / 'AGENTS.md').write_text(str(self.releases[0]))
            return original(*args)
        with patch.object(cleanup, '_scan', side_effect=scan):
            report = cleanup.storage(self.codex, apply=True)
        self.assertNotIn(self.releases[0].name, report['removed'])
        self.assertTrue(self.releases[0].exists())

    def test_mutation_after_quarantine_restores_original_tree(self):
        original = cleanup._checked_tree
        def inspect(path, *args):
            if path.name.startswith('.prune-'):
                (path / 'adhd/example.py').write_text('concurrent edit')
            return original(path, *args)
        with patch.object(cleanup, '_checked_tree', side_effect=inspect):
            with self.assertRaises(ValueError):
                cleanup.storage(self.codex, apply=True)
        self.assertEqual((self.releases[0] / 'adhd/example.py').read_text(), 'concurrent edit')
        self.assertFalse(any(path.name.startswith('.prune-') for path in self.base.iterdir()))

    def test_manifest_path_escape_rejected(self):
        record_path = self.parent / 'native-uninstalled-00000001.json'
        record = json.loads(record_path.read_text())
        record['release_identity']['files'][0]['path'] = '../outside.py'
        record_path.write_text(json.dumps(record))
        self.assertNotIn(self.releases[0].name, cleanup.storage(self.codex, apply=True)['removed'])

    def test_new_reference_after_quarantine_restores_original_tree(self):
        original = cleanup._checked_tree
        def inspect(path, *args):
            result = original(path, *args)
            if path.name.startswith('.prune-'):
                (self.codex / 'AGENTS.md').write_text(str(self.releases[0]))
            return result
        with patch.object(cleanup, '_checked_tree', side_effect=inspect):
            with self.assertRaisesRegex(ValueError, 'references changed'):
                cleanup.storage(self.codex, apply=True)
        self.assertTrue(self.releases[0].is_dir())
        self.assertFalse(any(path.name.startswith('.prune-') for path in self.base.iterdir()))

    def test_delete_failure_preserves_remaining_tree(self):
        with patch.object(cleanup.shutil, 'rmtree', side_effect=OSError('read-only filesystem')):
            with self.assertRaises(OSError):
                cleanup.storage(self.codex, apply=True)
        self.assertTrue(all(path.exists() for path in self.releases))

    def test_collection_junction_does_not_touch_outside(self):
        if os.name != 'nt':
            self.skipTest('Windows junction fixture')
        outside = self.root / 'outside-releases'
        os.replace(self.base, outside)
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.base), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises(ValueError):
                cleanup.storage(self.codex, apply=True)
            self.assertEqual(len(list(outside.iterdir())), 4)
        finally:
            os.rmdir(self.base)
            os.replace(outside, self.base)

    def test_member_symlink_is_rejected_without_touching_target(self):
        target = self.root / 'outside'
        target.mkdir()
        (target / 'keep.txt').write_text('keep')
        link = self.releases[0] / 'adhd/link'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks requires available OS permission')
        try:
            self.assertNotIn(self.releases[0].name, cleanup.storage(self.codex, apply=True)['removed'])
            self.assertEqual((target / 'keep.txt').read_text(), 'keep')
        finally:
            link.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction fixture')
    def test_member_junction_is_rejected_without_touching_target(self):
        target = self.root / 'outside'
        target.mkdir()
        (target / 'keep.txt').write_text('keep')
        link = self.releases[0] / 'adhd/link'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.assertNotIn(self.releases[0].name, cleanup.storage(self.codex, apply=True)['removed'])
            self.assertEqual((target / 'keep.txt').read_text(), 'keep')
        finally:
            os.rmdir(link)

    def test_module_cli_defaults_to_preview_and_requires_prune_for_apply(self):
        with patch('adhd.cli.output') as output:
            self.assertEqual(cleanup.main(['--codex-home', str(self.codex)]), 0)
            self.assertTrue(output.call_args.args[0]['dry_run'])
            self.assertEqual(cleanup.main(['scan', '--apply', '--codex-home', str(self.codex)]), 2)
        self.assertTrue(all(path.exists() for path in self.releases))

    def test_module_cli_without_system_or_user_site_packages(self):
        environment = os.environ.copy()
        environment.pop('PYTHONPATH', None)
        environment.pop('PYTHONHOME', None)
        result = subprocess.run([sys.executable, '-S', '-B', '-m', 'adhd.storage', '--help'],
                                cwd=Path(__file__).resolve().parents[1], env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--keep-releases', result.stdout)


if __name__ == '__main__':
    unittest.main()
