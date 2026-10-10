from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from build import package_adhd
from adhd import __version__


class PublicPackageTests(unittest.TestCase):
    def test_delivery_runtime_and_checks_are_in_public_allowlist(self):
        names = {path.relative_to(package_adhd.ROOT).as_posix()
                 for path in package_adhd.public_files()}
        required = {'adhd/delivery_policy.py', 'adhd/execution_decisions.py',
                    'adhd/run_metrics.py', 'docs/delivery-efficiency.md',
                    'schemas/native-delivery.json', 'scripts/benchmark_delivery_efficiency.py',
                    'scripts/compare_delivery_efficiency.py',
                    'tests/test_delivery_efficiency.py'}
        self.assertTrue(required <= names)

    def test_ignored_private_files_never_enter_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            (root / 'build').mkdir(parents=True)
            (root / 'README.md').write_text('public\n', encoding='utf-8')
            (root / 'auth.json').write_text('{"secret":"fixture"}\n', encoding='utf-8')
            public_list = root / 'build' / 'public-files.txt'
            public_list.write_text('README.md\nbuild/public-files.txt\n', encoding='utf-8')
            output = Path(temp) / 'public.zip'
            with patch.object(package_adhd, 'ROOT', root), patch.object(
                package_adhd, 'PUBLIC_FILE_LIST', public_list
            ), patch.object(package_adhd, 'OUTPUT', output):
                package_adhd.main()
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
            self.assertEqual(package_adhd.ARCHIVE_ROOT, f'ADHD-v{__version__}')
            self.assertIn(f'{package_adhd.ARCHIVE_ROOT}/README.md', names)
            self.assertNotIn(f'{package_adhd.ARCHIVE_ROOT}/auth.json', names)

    def test_path_traversal_in_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            public_list = root / 'public-files.txt'
            public_list.write_text('../private\nbuild/public-files.txt\n', encoding='utf-8')
            with patch.object(package_adhd, 'ROOT', root), patch.object(
                package_adhd, 'PUBLIC_FILE_LIST', public_list
            ):
                with self.assertRaisesRegex(ValueError, 'Unsafe public path'):
                    package_adhd.public_files()


if __name__ == '__main__':
    unittest.main()
