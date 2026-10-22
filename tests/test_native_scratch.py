"""Native termination recovers registered scratch, never legacy diagnostics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adhd import native
from adhd.core import read_json
from adhd.workspace_storage import WorkspaceStorage
from adhd.workspace_commands import run_in_scratch
import sys


class NativeScratchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.ws = root / 'workspace'
        self.ws.mkdir()
        env = patch.dict(os.environ, {'CODEX_HOME': str(root / 'codex'),
            'ADHD_HOME': str(root / 'state'), 'ADHD_EXEC_OWNER': ''})
        env.start()
        self.addCleanup(env.stop)
        self.manager = WorkspaceStorage(self.ws)

    def pending(self):
        with patch.object(self.manager, '_clean', return_value=False):
            with self.assertRaisesRegex(OSError, 'cleanup pending'):
                with self.manager.scratch('diagnostic', reserved_bytes=32, minimum_free=0) as path:
                    (path / 'generated').write_text('temporary')
        return path

    def event(self, name):
        return native.handle_event({'hook_event_name': name, 'session_id': 'scratch-tests',
                                    'cwd': str(self.ws)})

    def test_terminal_hooks_queue_and_next_owned_command_recovers_scratch(self):
        original = self.ws / 'original.hwpx'
        original.write_bytes(b'original')
        report = self.ws / '.adhd/results/required-report.json'
        report.parent.mkdir(parents=True)
        report.write_text('{"verified":true}')
        unknown = self.ws / '.adhd/legacy-diagnostic'
        unknown.mkdir()
        (unknown / 'generated.json').write_text('unregistered: preserve')
        for event in ('Stop', 'SessionEnd', 'Interrupt'):
            with self.subTest(event=event):
                pending = self.pending()
                with (patch.object(WorkspaceStorage, 'sweep', side_effect=AssertionError('Hook must not sweep')),
                      patch('adhd.workspace_storage.shutil.rmtree', side_effect=AssertionError('Hook must not delete'))):
                    self.event(event)
                self.assertTrue(pending.exists())
                state = read_json(native.folder(native.session_key('scratch-tests')) / 'state.json')
                self.assertIn(pending.name, [row['id'] for row in state['scratch_cleanup']['entries']])
                run_in_scratch(self.manager, {'argv': [sys.executable, '-B', '-c', 'pass'],
                    'reserved_bytes': 4096, 'minimum_free': 0})
                self.assertFalse(pending.exists())
        self.assertEqual(original.read_bytes(), b'original')
        self.assertEqual(json.loads(report.read_text()), {'verified': True})
        self.assertEqual((unknown / 'generated.json').read_text(), 'unregistered: preserve')

    def test_live_and_referenced_scratch_survive_hooks(self):
        with self.manager.scratch('live', reserved_bytes=32, minimum_free=0) as live:
            self.manager.add_reference(live, 'required-consumer')
            self.event('Stop')
            self.assertTrue(live.exists())
        self.event('Stop')
        self.assertTrue(live.exists())
        self.manager.remove_reference(live, 'required-consumer')
        run_in_scratch(self.manager, {'argv': [sys.executable, '-B', '-c', 'pass'],
            'reserved_bytes': 4096, 'minimum_free': 0})
        self.assertFalse(live.exists())

    def test_hooks_without_catalog_do_not_create_storage_database(self):
        self.event('Stop')
        self.assertFalse(self.manager.catalog.exists())

    def test_busy_catalog_is_reported_without_touching_scratch(self):
        from filelock import FileLock
        pending = self.pending()
        with FileLock(str(self.manager.catalog) + '.lock'):
            self.event('Stop')
        state = read_json(native.folder(native.session_key('scratch-tests')) / 'state.json')
        self.assertEqual(state['scratch_cleanup'], {'status': 'cleanup_pending', 'error': 'Timeout'})
        self.assertTrue(pending.exists())

    def test_unconfirmed_native_child_defers_cleanup(self):
        pending = self.pending()
        self.event('Stop')
        state_path = native.folder(native.session_key('scratch-tests')) / 'state.json'
        state = read_json(state_path)
        # Construct the native guard boundary without starting a competing run.
        state['status'] = 'interrupt_pending'
        state['children'] = {'child': {'status': 'running'}}
        pending = self.pending()
        native.persist(native.folder(state['key']), state)
        self.event('Stop')
        self.assertTrue(pending.exists())


if __name__ == '__main__':
    unittest.main()
