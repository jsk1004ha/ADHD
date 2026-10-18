from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from adhd.source_cache import SourceCache
from build import package_adhd


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.remote = "https://example.invalid/pinned.git"
        self.git("remote", "add", "origin", self.remote)
        (self.repo / "main.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "main.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "initial")
        self.commit = subprocess.check_output(("git", "-C", str(self.repo), "rev-parse", "HEAD")).decode().strip()

    def git(self, *args):
        subprocess.run(("git", "-C", str(self.repo), *args), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def test_reuse_pinned_bytes_and_reject_wrong_identity(self):
        cache = SourceCache(self.root / "cache")
        first = cache.snapshot(self.repo, self.remote, self.commit, ["main.py"])
        self.assertFalse(first["reused"])
        (self.repo / "main.py").write_text("value = 2\n", encoding="utf-8")
        second = cache.snapshot(self.repo, self.remote, self.commit, ["main.py"])
        self.assertTrue(second["reused"])
        self.assertEqual((Path(second["path"]) / "files" / "main.py").read_text(), "value = 1\n")
        with self.assertRaises(ValueError):
            cache.snapshot(self.repo, self.remote, "0" * 40, ["main.py"])
        with self.assertRaises(ValueError):
            cache.snapshot(self.repo, self.remote, self.commit, [".env.production"])
        with self.assertRaisesRegex(ValueError, "credential-bearing-remote"):
            cache.snapshot(self.repo, "https://user:password@example.invalid/repo.git",
                           self.commit, ["main.py"])
        cached_file = Path(second["path"]) / "files" / "main.py"
        with patch("adhd.source_cache._link", side_effect=lambda path: path == cached_file):
            with self.assertRaisesRegex(ValueError, "cached-source-link"):
                cache.snapshot(self.repo, self.remote, self.commit, ["main.py"])
        manifest = Path(second["path"]) / "manifest.json"
        original = manifest.read_bytes()
        manifest.write_bytes(b"x" * 65537)
        with self.assertRaisesRegex(ValueError, "cached-source-corrupt"):
            cache.snapshot(self.repo, self.remote, self.commit, ["main.py"])
        manifest.write_bytes(original)
        (Path(second["path"]) / "files" / "unexpected.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cached-source-unexpected-entry"):
            cache.snapshot(self.repo, self.remote, self.commit, ["main.py"])

    def test_package_rejects_cache_and_auth_path_even_if_allowlisted(self):
        root = self.root / "package"
        (root / "build").mkdir(parents=True)
        (root / "cache").mkdir()
        (root / "cache" / "entry.py").write_text("x", encoding="utf-8")
        allowlist = root / "build" / "public-files.txt"
        allowlist.write_text("build/public-files.txt\ncache/entry.py\n", encoding="utf-8")
        with patch.object(package_adhd, "ROOT", root), patch.object(package_adhd, "PUBLIC_FILE_LIST", allowlist):
            with self.assertRaisesRegex(ValueError, "Private or generated"):
                package_adhd.public_files()
            (root / "cache" / "entry.py").unlink()
            (root / "auth.json").write_text("{}", encoding="utf-8")
            allowlist.write_text("auth.json\nbuild/public-files.txt\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Private or generated"):
                package_adhd.public_files()

    def test_committed_symbolic_link_is_not_copied(self):
        digest = subprocess.check_output(("git", "-C", str(self.repo), "hash-object", "-w", "--stdin"),
                                         input=b"main.py").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{digest},link")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "link")
        commit = subprocess.check_output(("git", "-C", str(self.repo), "rev-parse", "HEAD")).decode().strip()
        with self.assertRaisesRegex(ValueError, "source-not-regular"):
            SourceCache(self.root / "cache").snapshot(self.repo, self.remote, commit, ["link"])

    def test_cache_root_rejects_linked_ancestor(self):
        with patch("adhd.source_cache._link", side_effect=lambda path: path == self.root):
            with self.assertRaisesRegex(ValueError, "unsafe-cache-root"):
                SourceCache(self.root / "cache")

    def test_copy_and_reuse_exhaustion_never_publish_partial_cache(self):
        from adhd.maintenance_budget import BudgetExhausted, MaintenanceBudget
        cache = SourceCache(self.root / "cache")
        committed_bytes = (self.repo / "main.py").stat().st_size
        with self.assertRaises(BudgetExhausted):
            cache.snapshot(self.repo, self.remote, self.commit, ["main.py"],
                           budget=MaintenanceBudget(write_bytes=committed_bytes))
        self.assertEqual(list((self.root / "cache").glob("*/manifest.json")), [])
        self.assertEqual(list((self.root / "cache").glob("pending-*")), [])
        first = cache.snapshot(self.repo, self.remote, self.commit, ["main.py"])
        manifest_bytes = (Path(first["path"]) / "manifest.json").stat().st_size
        with self.assertRaises(BudgetExhausted):
            cache.snapshot(self.repo, self.remote, self.commit, ["main.py"],
                           budget=MaintenanceBudget(read_bytes=manifest_bytes + 1))
        self.assertTrue((Path(first["path"]) / "files" / "main.py").is_file())


if __name__ == "__main__":
    unittest.main()
