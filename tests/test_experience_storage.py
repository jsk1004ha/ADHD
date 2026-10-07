from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from adhd.core import atomic_json, file_hash
from adhd import experience_storage as storage


class ExperienceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()

    def test_inventory_separates_categories_and_reports_duplicates_without_deletion(self):
        vault = self.workspace / 'vault'
        vault.mkdir()
        (vault / 'first.md').write_bytes(b'same note')
        (vault / 'second.md').write_bytes(b'same note')
        state = self.workspace / '.adhd'
        state.mkdir()
        (state / 'checks').mkdir()
        (state / 'checks' / 'check.stderr.log').write_bytes(b'log')
        (state / 'index.sqlite3').write_bytes(b'index')
        (state / 'backups').mkdir()
        (state / 'backups' / 'prior.zip').write_bytes(b'backup')
        # Category accounting is independent of the policy/index adapter; the
        # integrated tests exercise the real configured vault visibility.
        with patch.object(storage, '_visible_notes', return_value=([vault / 'first.md', vault / 'second.md'], [])):
            result = storage.inventory(self.workspace)
        self.assertEqual(result['categories']['canonical_notes']['files'], 2)
        self.assertEqual(result['categories']['logs']['files'], 1)
        self.assertEqual(result['categories']['indexes']['files'], 1)
        self.assertEqual(result['categories']['backups']['files'], 1)
        self.assertEqual(len(result['duplicates']), 1)
        self.assertFalse(result['deletion_performed'])
        self.assertIsNone(result['reclaimed_disk_bytes'])
        self.assertTrue((vault / 'first.md').exists())

    def test_document_and_commit_reachability_audit(self):
        document = self.workspace / 'report.md'
        document.write_text('canonical document', encoding='utf-8')
        valid = {'kind': 'document', 'path': 'report.md', 'sha256': file_hash(document)}
        self.assertTrue(storage.audit_references(self.workspace, [valid])['valid'])
        document.unlink()
        self.assertEqual(storage.audit_references(self.workspace, [valid])['missing'][0]['status'], 'missing')

        repo = self.workspace / 'source'
        repo.mkdir()
        commands = [
            ['git', 'init', '-q', str(repo)],
            ['git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid'],
            ['git', '-C', str(repo), 'config', 'user.name', 'Test'],
        ]
        for command in commands:
            subprocess.run(command, check=True, capture_output=True)
        code = repo / 'main.py'
        code.write_text('print("ok")\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', 'main.py'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'initial'], check=True, capture_output=True)
        commit = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                                capture_output=True, text=True).stdout.strip()
        reference = {'kind': 'code', 'repository': 'source', 'commit': commit,
                     'path': 'source/main.py', 'sha256': hashlib.sha256(subprocess.check_output(
                         ['git', '-C', str(repo), 'show', commit + ':main.py'])).hexdigest(),
                     'worktree_sha256': file_hash(code)}
        self.assertTrue(storage.audit_references(self.workspace, [reference])['valid'])
        wrong = {**reference, 'sha256': '0' * 64}
        self.assertEqual(storage.audit_references(self.workspace, [wrong])['stale'][0]['status'], 'stale')

    def test_reference_and_archive_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            storage.audit_references(self.workspace, [{'kind': 'document', 'path': '../escape.md',
                                                       'sha256': '0' * 64}])
        with self.assertRaises(ValueError):
            storage.archive(self.workspace, {'run_id': uuid.uuid4().hex,
                                             'receipts': ['../receipt.json']})
        outside = self.base / 'outside.md'
        outside.write_text('external', encoding='utf-8')
        linked = self.workspace / 'linked.md'
        try:
            linked.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable to this test process')
        with self.assertRaises(ValueError):
            storage.audit_references(self.workspace, [{'kind': 'document', 'path': 'linked.md',
                                                       'sha256': file_hash(outside)}])

    def _receipt_and_manifest(self):
        receipt = '.adhd/checks/' + uuid.uuid4().hex + '/receipt.json'
        path = self.workspace / receipt
        atomic_json(path, {'test': 'backend validates the actual receipt'})
        manifest = '.adhd/evidence-archives/' + uuid.uuid4().hex + '/manifest.json'
        atomic_json(self.workspace / manifest, {'test': 'backend validates the actual manifest'})
        return receipt, manifest

    def test_archive_requires_backend_and_preserves_originals_by_default(self):
        receipt, manifest = self._receipt_and_manifest()
        run_id = uuid.uuid4().hex
        raw = self.workspace / '.adhd/checks' / Path(receipt).parent.name / 'check.stdout.jsonl'
        raw.write_bytes(b'original bytes')
        with patch.object(storage, '_archive_engine', side_effect=RuntimeError('backend unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'backend unavailable'):
                storage.archive(self.workspace, {'run_id': run_id, 'receipts': [receipt]})
        self.assertEqual(raw.read_bytes(), b'original bytes')

        engine = Mock()
        engine.archive_receipts.return_value = {'manifest': manifest, 'original_bytes': len(raw.read_bytes()),
                                                'stored_bytes': 6}
        engine.verify_archive.return_value = {'verified': True, 'run_id': run_id, 'logs': 1,
                                              'raw_present': 1}
        with patch.object(storage, '_archive_engine', return_value=engine):
            result = storage.archive(self.workspace, {'run_id': run_id, 'receipts': [receipt]})
        engine.archive_receipts.assert_called_once_with(self.workspace, run_id, [receipt], release=False)
        engine.release_raw.assert_not_called()
        self.assertTrue(result['originals_retained'])
        self.assertIsNone(result['reclaimed_disk_bytes'])
        self.assertEqual(raw.read_bytes(), b'original bytes')

    def test_tampered_manifest_and_restore_postcheck_fail_closed(self):
        _, manifest = self._receipt_and_manifest()
        engine = Mock()
        engine.verify_archive.return_value = {'verified': False}
        with patch.object(storage, '_archive_engine', return_value=engine):
            with self.assertRaisesRegex(ValueError, 'not verified'):
                storage.restore(self.workspace, {'manifest': manifest})
        engine.restore_raw.assert_not_called()

        raw = self.workspace / 'raw.log'
        expected = b'byte-exact restored log\r\n'
        expected_hash = hashlib.sha256(expected).hexdigest()
        engine = Mock()
        def restore_raw(*_):
            raw.write_bytes(expected)
            return {'restored': 1}
        engine.restore_raw.side_effect = restore_raw
        engine.verify_archive.side_effect = lambda *_: {
            'verified': True, 'logs': 1,
            'raw_present': int(raw.exists() and hashlib.sha256(raw.read_bytes()).hexdigest() == expected_hash)}
        with patch.object(storage, '_archive_engine', return_value=engine):
            result = storage.restore(self.workspace, {'manifest': manifest})
        self.assertEqual(raw.read_bytes(), expected)
        self.assertTrue(result['verified'])


if __name__ == '__main__':
    unittest.main()
