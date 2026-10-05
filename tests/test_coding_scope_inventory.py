from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from adhd.coding_scope import _tree, audit, capture, validate_report
from tests.test_coding_scope import GitFixture


class ScopeInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'')
        return path

    def test_sorted_nested_unicode_and_empty_files(self):
        expected = ['z.py', 'src/b.py', 'src/a.py', '한글/빈 파일.py']
        for relative in expected:
            self.write(relative)
        (self.root / 'empty').mkdir()
        self.assertEqual(_tree(self.root, []), sorted(expected))

    def test_exclusions_preserve_exact_files_and_directory_boundaries(self):
        for relative in ['skip.txt', 'skip.txt.more', 'cache/a.py', 'cache2/a.py',
                         'src/cache/a.py', 'src/cache2/a.py', 'kept.py']:
            self.write(relative)
        self.assertEqual(_tree(self.root, ['skip.txt', 'cache/', 'src/cache/']),
                         ['cache2/a.py', 'kept.py', 'skip.txt.more', 'src/cache2/a.py'])

    def test_directory_without_trailing_slash_is_not_a_subtree_exclusion(self):
        self.write('cache/kept.py')
        self.assertEqual(_tree(self.root, ['cache']), ['cache/kept.py'])

    def test_directory_exclusion_also_excludes_same_named_file(self):
        self.write('cache')
        self.write('cache2')
        self.assertEqual(_tree(self.root, ['cache/']), ['cache2'])

    def test_relative_root_is_supported(self):
        self.write('src/file.py')
        relative = Path(os.path.relpath(self.root, Path.cwd()))
        self.assertEqual(_tree(relative, []), ['src/file.py'])

    def test_inventory_is_fresh_after_added_and_removed_files(self):
        old = self.write('old.py')
        self.assertEqual(_tree(self.root, []), ['old.py'])
        old.unlink()
        self.write('new.py')
        self.assertEqual(_tree(self.root, []), ['new.py'])

    def test_file_limit_accepts_20000_and_rejects_20001(self):
        # Avoid creating thousands of files just to exercise the bounded inventory.
        for count in (20_000, 20_001):
            files = [f'f{i}.py' for i in range(count)]
            with self.subTest(count=count), patch('adhd.coding_scope.os.walk',
                    return_value=[(str(self.root), [], files)]), patch('adhd.coding_scope._path'):
                if count == 20_000:
                    self.assertEqual(_tree(self.root, []), sorted(files))
                else:
                    with self.assertRaisesRegex(ValueError, 'exceeds 20000 files'):
                        _tree(self.root, [])

    def test_all_ancestors_are_rechecked_for_each_entry(self):
        self.write('src/nested/a.py')
        self.write('src/nested/b.py')
        nested = self.root / 'src/nested'
        real_is_symlink = Path.is_symlink
        calls = 0

        def changes_after_first_leaf(path):
            nonlocal calls
            if path == nested:
                calls += 1
                if calls >= 3:
                    return True
            return real_is_symlink(path)

        with patch.object(Path, 'is_symlink', changes_after_first_leaf):
            with self.assertRaisesRegex(ValueError, 'link or junction'):
                _tree(self.root, [])
        self.assertGreaterEqual(calls, 3)

    def test_resolved_leaf_escape_is_rejected(self):
        self.write('file.py')
        real_resolve = Path.resolve

        def escaped(path, *args, **kwargs):
            if path == self.root / 'file.py':
                return self.root.parent / 'escaped.py'
            return real_resolve(path, *args, **kwargs)

        with patch.object(Path, 'resolve', escaped):
            with self.assertRaisesRegex(ValueError, 'Path leaves workspace'):
                _tree(self.root, [])

    def test_file_directory_and_broken_symlinks_are_rejected(self):
        target = self.write('target/file.py')
        for name, destination, directory in [
                ('file-link', target, False),
                ('directory-link', target.parent, True),
                ('broken-link', self.root / 'missing', False)]:
            link = self.root / name
            try:
                link.symlink_to(destination, target_is_directory=directory)
            except OSError:
                self.skipTest('This account cannot create symlinks')
            try:
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'link or junction'):
                    _tree(self.root, [])
            finally:
                link.unlink()

    def test_excluded_symlink_is_not_followed_or_validated(self):
        link = self.root / 'ignored'
        try:
            link.symlink_to(self.root / 'missing', target_is_directory=True)
        except OSError:
            self.skipTest('This account cannot create symlinks')
        self.assertEqual(_tree(self.root, ['ignored/']), [])

    @unittest.skipUnless(os.name == 'nt' and hasattr(Path, 'is_junction'),
                         'Windows junction detection requires Python 3.12+')
    def test_windows_junction_is_rejected_and_excluded_junction_is_skipped(self):
        target = self.root / 'target'
        target.mkdir()
        self.write('target/file.py')
        link = self.root / 'junction'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                capture_output=True, check=False)
        if result.returncode:
            self.skipTest('This account cannot create a junction')
        self.addCleanup(os.rmdir, link)
        with self.assertRaisesRegex(ValueError, 'link or junction'):
            _tree(self.root, [])
        self.assertEqual(_tree(self.root, ['junction/']), ['target/file.py'])

    @unittest.skipIf(os.name == 'nt', 'Backslash and colon filenames require POSIX')
    def test_backslash_and_windows_drive_names_keep_safe_path_semantics(self):
        self.write('literal\\name.py')
        self.assertEqual(_tree(self.root, []), ['literal\\name.py'])
        self.write('..\\escape.py')
        with self.assertRaisesRegex(ValueError, 'Path leaves workspace'):
            _tree(self.root, [])
        (self.root / '..\\escape.py').unlink()
        self.write('C:drive.py')
        with self.assertRaisesRegex(ValueError, 'Absolute Windows paths'):
            _tree(self.root, [])


class ScopeInventoryReportTests(GitFixture):
    def test_same_size_content_with_restored_mtime_is_still_rejected(self):
        baseline = capture(self.root, '.', ['a.py'])
        report = audit(self.root, baseline)
        path = self.root / 'a.py'
        before = path.stat()
        path.write_bytes(b'changed!\n')  # Same size as original\n.
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with patch('adhd.coding_scope._git', side_effect=AssertionError('hook ran Git')):
            with self.assertRaisesRegex(ValueError, 'source content changed'):
                validate_report(self.root, baseline, report)

    def test_empty_file_removal_and_new_ignored_files_keep_inventory_rules(self):
        self.git('add', '.gitignore')
        baseline = capture(self.root, '.', ['empty.py'])
        (self.root / 'empty.py').write_bytes(b'')
        report = audit(self.root, baseline)
        ignored = self.root / '.adhd' / 'later.json'
        ignored.parent.mkdir()
        ignored.write_text('{}', encoding='utf-8')
        with patch('adhd.coding_scope._git', side_effect=AssertionError('hook ran Git')):
            validate_report(self.root, baseline, report)
            (self.root / 'empty.py').unlink()
            with self.assertRaisesRegex(ValueError, 'source content changed'):
                validate_report(self.root, baseline, report)
