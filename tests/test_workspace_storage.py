from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from adhd.workspace_storage import WorkspaceStorage, _process_birth, _process_state, bounded_copy


class WorkspaceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.catalog = self.root / 'catalog'
        self.store_patch = patch('adhd.workspace_storage.store', return_value=self.catalog)
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def test_reservations_are_shared_and_quota_is_enforced(self):
        first = WorkspaceStorage(self.workspace)
        second = WorkspaceStorage(self.workspace)
        with patch('adhd.workspace_storage.shutil.disk_usage', return_value=(100, 60, 40)):
            token = first.reserve('first', 25, minimum_free=10)
            with self.assertRaisesRegex(ValueError, 'Insufficient free space'):
                second.reserve('second', 6, minimum_free=10)
            first.release(token)
            second_token = second.reserve('second', 6, minimum_free=10)
            second.release(second_token)

    def test_competing_reservations_admit_only_one(self):
        barrier = threading.Barrier(2)
        results = []
        def attempt(name):
            manager = WorkspaceStorage(self.workspace)
            barrier.wait()
            try:
                results.append(('reserved', manager.reserve(name, 25, minimum_free=10)))
            except ValueError:
                results.append(('denied', name))
        with patch('adhd.workspace_storage.shutil.disk_usage', return_value=(100, 60, 40)):
            threads = [threading.Thread(target=attempt, args=(str(i),)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual([kind for kind, _ in results].count('reserved'), 1)
        self.assertEqual([kind for kind, _ in results].count('denied'), 1)
        WorkspaceStorage(self.workspace).release(next(value for kind, value in results
                                                      if kind == 'reserved'))

    def test_scratch_twenty_runs_plateau_and_exception_cleanup(self):
        manager = WorkspaceStorage(self.workspace)
        for _ in range(20):
            with manager.scratch('verification', reserved_bytes=1024, minimum_free=0) as path:
                (path / 'copy.bin').write_bytes(b'a' * 1024)
                self.assertTrue((path / '.ownership.json').is_file())
            self.assertEqual(list((self.workspace / '.adhd/scratch').iterdir()), [])
        with self.assertRaisesRegex(RuntimeError, 'producer failed'):
            with manager.scratch('verification', reserved_bytes=1024, minimum_free=0) as path:
                (path / 'copy.bin').write_bytes(b'partial')
                raise RuntimeError('producer failed')
        self.assertEqual(list((self.workspace / '.adhd/scratch').iterdir()), [])

    def test_exited_child_with_open_popen_handle_is_dead(self):
        child = subprocess.Popen([sys.executable, '-B', '-c', 'pass'])
        birth = _process_birth(child.pid)
        self.assertIsNotNone(birth)
        child.wait(timeout=10)
        self.assertEqual(_process_state(child.pid, birth), 'dead')

    def test_sweep_preserves_referenced_and_unknown_scratch(self):
        manager = WorkspaceStorage(self.workspace)
        with manager.scratch('verification', reserved_bytes=1, minimum_free=0) as path:
            manager.add_reference(path, 'final-report')
            self.assertIn(path.name, manager.sweep()['protected'])
        self.assertTrue(path.exists())
        self.assertIn(path.name, manager.sweep()['protected'])
        manager.remove_reference(path, 'final-report')
        self.assertIn(path.name, manager.sweep()['removed'])
        unknown = self.workspace / '.adhd/scratch/unknown'
        unknown.mkdir()
        (unknown / 'keep').write_text('original')
        manager.sweep()
        self.assertEqual((unknown / 'keep').read_text(), 'original')

    def test_cleanup_failure_is_reported_and_retryable(self):
        manager = WorkspaceStorage(self.workspace)
        with patch.object(manager, '_clean', return_value=False):
            with self.assertRaisesRegex(OSError, 'cleanup pending'):
                with manager.scratch('verification', reserved_bytes=1, minimum_free=0) as path:
                    (path / 'copy').write_bytes(b'x')
        self.assertTrue(path.exists())
        self.assertIn(path.name, manager.sweep()['removed'])
        self.assertFalse(path.exists())

    def test_bounded_copy_preflight_and_existing_original(self):
        source = self.workspace / 'input.bin'
        source.write_bytes(b'input')
        target = self.workspace / 'stage'
        target.mkdir()
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            bounded_copy({'input.bin': source}, target, workspace=self.workspace,
                         max_bytes=4, minimum_free=0)
        self.assertFalse((target / 'input.bin').exists())
        report = bounded_copy({'input.bin': source}, target, workspace=self.workspace,
                              max_bytes=5, minimum_free=0)
        self.assertEqual(report, {'bytes': 5, 'files': 1})
        self.assertEqual((target / 'input.bin').read_bytes(), b'input')
        (target / 'input.bin').write_bytes(b'changed original')
        with self.assertRaisesRegex(ValueError, 'Existing destination differs'):
            bounded_copy({'input.bin': source}, target, workspace=self.workspace,
                         max_bytes=5, minimum_free=0)
        self.assertEqual((target / 'input.bin').read_bytes(), b'changed original')

    def test_bounded_copy_rejects_path_escape_and_links(self):
        source = self.workspace / 'input.bin'
        source.write_bytes(b'data')
        target = self.workspace / 'stage'
        target.mkdir()
        with self.assertRaises(ValueError):
            bounded_copy({'../outside': source}, target, workspace=self.workspace,
                         minimum_free=0)
        outside = self.root / 'outside.bin'
        outside.write_bytes(b'original')
        link = self.workspace / 'linked.bin'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(ValueError):
            bounded_copy({'linked.bin': link}, target, workspace=self.workspace,
                         minimum_free=0)
        self.assertEqual(outside.read_bytes(), b'original')

    def test_bounded_copy_detects_growth_and_removes_partial_output(self):
        source = self.workspace / 'input.bin'
        source.write_bytes(b'abcd')
        target = self.workspace / 'stage'
        target.mkdir()
        original_open = Path.open
        grew = False
        def growing_open(path, *args, **kwargs):
            nonlocal grew
            if path == source and args and args[0] == 'rb' and not grew:
                grew = True
                with original_open(source, 'ab') as output:
                    output.write(b'e')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', growing_open):
            with self.assertRaisesRegex(ValueError, 'grew beyond reserved'):
                bounded_copy({'input.bin': source}, target, workspace=self.workspace,
                             max_bytes=8, minimum_free=0)
        self.assertEqual(list(target.iterdir()), [])

    def test_bounded_copy_does_not_remove_a_colliding_temp(self):
        source = self.workspace / 'input.bin'
        source.write_bytes(b'input')
        target = self.workspace / 'stage'
        target.mkdir()
        collision = target / ('.input.bin.adhd-copy-' + 'a' * 32)
        collision.write_bytes(b'keep')
        with patch('adhd.workspace_storage.uuid.uuid4') as identifier:
            identifier.return_value.hex = 'a' * 32
            with self.assertRaises(FileExistsError):
                bounded_copy({'input.bin': source}, target, workspace=self.workspace,
                             minimum_free=0)
        self.assertEqual(collision.read_bytes(), b'keep')

    def test_selected_zip_reuse_invalidates_options_and_rejects_traversal(self):
        archive = self.workspace / 'input.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('chosen.txt', b'chosen')
            output.writestr('other.txt', b'other')
        manager = WorkspaceStorage(self.workspace)
        first = manager.extract_zip_selected(archive, ['chosen.txt'], namespace='sample',
                                             tool_version='1', options={'mode': 'a'},
                                             minimum_free=0)
        self.assertEqual((first / 'chosen.txt').read_bytes(), b'chosen')
        self.assertFalse((first / 'other.txt').exists())
        self.assertEqual(first, manager.extract_zip_selected(archive, ['chosen.txt'],
            namespace='sample', tool_version='1', options={'mode': 'a'}, minimum_free=0))
        second = manager.extract_zip_selected(archive, ['chosen.txt'], namespace='sample',
                                              tool_version='1', options={'mode': 'b'},
                                              minimum_free=0)
        self.assertNotEqual(first, second)
        third = manager.extract_zip_selected(archive, ['chosen.txt'], namespace='sample',
                                             tool_version='2', options={'mode': 'a'},
                                             minimum_free=0)
        self.assertNotEqual(first, third)
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('chosen.txt', b'new bytes')
        changed = manager.extract_zip_selected(archive, ['chosen.txt'], namespace='sample',
                                               tool_version='1', options={'mode': 'a'},
                                               minimum_free=0)
        self.assertNotEqual(first, changed)
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('../escape', b'bad')
        with self.assertRaises(ValueError):
            manager.extract_zip_selected(archive, ['../escape'], namespace='sample',
                                         tool_version='1', minimum_free=0)
        self.assertFalse((self.workspace / 'escape').exists())

    def test_zip_link_member_is_rejected(self):
        archive = self.workspace / 'linked.zip'
        info = zipfile.ZipInfo('linked.txt')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr(info, 'outside')
        with self.assertRaisesRegex(ValueError, 'Unsafe ZIP member'):
            WorkspaceStorage(self.workspace).extract_zip_selected(
                archive, ['linked.txt'], namespace='sample', tool_version='1', minimum_free=0)

    def test_only_released_cache_entries_are_evicted(self):
        archive=self.workspace/'cache.zip'
        with zipfile.ZipFile(archive,'w') as output:output.writestr('x',b'1234')
        manager=WorkspaceStorage(self.workspace)
        first=manager.extract_zip_selected(archive,['x'],namespace='fixture',tool_version='1',
                                          minimum_free=0,max_cache_bytes=4)
        with self.assertRaisesRegex(ValueError,'cache byte'):
            manager.extract_zip_selected(archive,['x'],namespace='fixture',tool_version='2',
                                         minimum_free=0,max_cache_bytes=4)
        manager.release_cache(first)
        with manager.selected_zip(archive,['x'],namespace='fixture',tool_version='2',
                                  minimum_free=0,max_cache_bytes=4) as second:
            self.assertFalse(first.exists())
            self.assertEqual((second/'x').read_bytes(),b'1234')
            with self.assertRaisesRegex(ValueError,'cache byte'):
                manager.extract_zip_selected(archive,['x'],namespace='fixture',tool_version='3',
                                             minimum_free=0,max_cache_bytes=4)

    def test_two_plain_cache_consumers_need_two_releases(self):
        archive=self.workspace/'cache.zip'
        with zipfile.ZipFile(archive,'w') as output:output.writestr('x',b'1234')
        manager=WorkspaceStorage(self.workspace)
        options={'namespace':'fixture','tool_version':'1','minimum_free':0,'max_cache_bytes':4}
        first=manager.extract_zip_selected(archive,['x'],**options)
        self.assertEqual(first,manager.extract_zip_selected(archive,['x'],**options))
        manager.release_cache(first)
        with self.assertRaisesRegex(ValueError,'cache byte'):
            manager.extract_zip_selected(archive,['x'],**{**options,'tool_version':'2'})
        self.assertTrue(first.is_dir())
        manager.release_cache(first)
        manager.extract_zip_selected(archive,['x'],**{**options,'tool_version':'2'})
        self.assertFalse(first.exists())


if __name__ == '__main__':
    unittest.main()
