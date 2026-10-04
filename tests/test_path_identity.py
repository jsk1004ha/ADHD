from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from adhd.core import digest, file_hash
from adhd.provenance import validate_provenance
from adhd.snapshots import _manifest_leaf, build_snapshot, validate_snapshot
from adhd.validation_batch import load_report, run_batch


class PathIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / 'anchor').mkdir()
        self.alias = self.workspace / 'anchor' / '..'

    def tearDown(self):
        self.temp.cleanup()

    def _task_manifest(self, stage: Path) -> str:
        part = stage.resolve() / 'part.txt'
        frozen = {'staging_workspace': str(stage), 'files': {'part.txt': file_hash(part)}}
        frozen['snapshot_digest'] = digest(frozen)
        path = self.workspace / 'frozen.json'
        path.write_text(json.dumps(frozen), encoding='utf-8')
        return path.name

    def _provenance_manifest(self, path: Path) -> None:
        for name, content in {
            'raw.csv': 'value\n2\n',
            'analysis.py': 'print(2)\n',
            'result.json': '{"value": 2}\n',
            'report.md': 'Measured value: 2 kg.\n',
        }.items():
            (self.workspace / name).write_text(content, encoding='utf-8')
        nodes = [
            {'id': 'raw', 'role': 'raw', 'path': 'raw.csv', 'inputs': []},
            {'id': 'code', 'role': 'analysis', 'path': 'analysis.py', 'inputs': ['raw']},
            {'id': 'result', 'role': 'result', 'path': 'result.json', 'inputs': ['raw', 'code']},
            {'id': 'report', 'role': 'report', 'path': 'report.md', 'inputs': ['result']},
        ]
        for node in nodes:
            node['sha256'] = file_hash(self.workspace / node['path'])
        value = {'schema': 1, 'nodes': nodes, 'claims': [
            {'id': 'value', 'category': 'calculated', 'result': 'result',
             'json_pointer': '/value', 'output': 'report', 'literal': '2 kg',
             'value': '2', 'unit': 'kg'}]}
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_task_bundle_accepts_real_lexical_alias_and_keeps_stale_guard(self):
        stage = self.workspace / 'stage'
        stage.mkdir()
        (stage / 'part.txt').write_text('current', encoding='utf-8')
        alias_stage = self.alias / 'stage'
        snapshot = build_snapshot(self.workspace, [
            {'kind': 'task_bundle', 'manifest': self._task_manifest(alias_stage)}])
        self.assertTrue(validate_snapshot(self.workspace, snapshot)['valid'])
        (stage / 'part.txt').write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'changed'):
            validate_snapshot(self.workspace, snapshot)

    def test_task_bundle_rejects_outside_and_linked_staging_workspace(self):
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            (outside / 'part.txt').write_text('outside', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'escapes'):
                build_snapshot(self.workspace, [
                    {'kind': 'task_bundle', 'manifest': self._task_manifest(outside)}])
        stage = self.workspace / 'stage'
        stage.mkdir()
        (stage / 'part.txt').write_text('current', encoding='utf-8')
        linked = self.workspace / 'linked-stage'
        try:
            linked.symlink_to(stage, target_is_directory=True)
        except OSError:
            return
        with self.assertRaisesRegex(ValueError, 'links or junctions'):
            build_snapshot(self.workspace, [
                {'kind': 'task_bundle', 'manifest': self._task_manifest(linked)}])

    def test_render_manifest_leaf_accepts_alias_without_erasing_link_checks(self):
        leaf = self.workspace / 'page.png'
        leaf.write_bytes(b'page')
        resolved, relative = _manifest_leaf(self.workspace, str(self.alias / leaf.name), 'page')
        self.assertEqual(resolved, leaf.resolve())
        self.assertEqual(relative, leaf.name)
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp) / 'page.png'
            outside.write_bytes(b'outside')
            with self.assertRaisesRegex(ValueError, 'leaves the workspace'):
                _manifest_leaf(self.workspace, str(outside), 'page')
        linked = self.workspace / 'linked-page.png'
        try:
            linked.symlink_to(leaf)
        except OSError:
            return
        with self.assertRaisesRegex(ValueError, 'links or junctions'):
            _manifest_leaf(self.workspace, str(linked), 'page')

    def test_provenance_accepts_real_absolute_alias_and_rejects_escape_or_link(self):
        manifest = self.workspace / 'provenance.json'
        self._provenance_manifest(manifest)
        result = validate_provenance(self.workspace, self.alias / manifest.name)
        self.assertEqual(result['verified_claims'], ['value'])
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp) / 'provenance.json'
            outside.write_text(manifest.read_text(encoding='utf-8'), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'leaves workspace'):
                validate_provenance(self.workspace, outside)
        linked = self.workspace / 'linked-provenance.json'
        try:
            linked.symlink_to(manifest)
        except OSError:
            return
        with self.assertRaisesRegex(ValueError, 'links or junctions'):
            validate_provenance(self.workspace, linked)

    def test_batch_report_uses_canonical_workspace_identity_and_stays_current(self):
        subject = self.workspace / 'subject.txt'
        subject.write_text('current', encoding='utf-8')
        spec = {'run_id': 'path-identity', 'contract_revision': 1,
                'requirements': ['R1'], 'mandatory_checks': ['check'], 'checks': [
                    {'id': 'check', 'argv': [sys.executable, '-c', 'print("ok")'],
                     'subject_paths': ['subject.txt'], 'requirements': ['R1']}]}
        result = run_batch(spec, self.workspace)
        self.assertEqual(load_report(self.alias, result['report'])['status'], 'passed')
        subject.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'frozen inputs changed'):
            load_report(self.alias, result['report'])
        with self.assertRaises(ValueError):
            load_report(self.workspace, '../report.json')


if __name__ == '__main__':
    unittest.main()
