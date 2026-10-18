from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from adhd.context_index import CodeIndex


class CodeIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "repo"
        self.workspace.mkdir()
        self.home = patch.dict(os.environ, {"ADHD_HOME": str(self.base / "state")})
        self.home.start()
        self.addCleanup(self.home.stop)
        self.run_git("init", "-q")
        self.source = self.workspace / "module.py"
        self.source.write_text("def target():\n    return 1\n", encoding="utf-8")
        self.run_git("add", "module.py")
        self.run_git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "initial")

    def run_git(self, *args):
        subprocess.run(("git", "-C", str(self.workspace), *args), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def test_symbol_ref_stales_on_same_mtime_content_change(self):
        with CodeIndex(self.workspace) as index:
            first = index.refresh()
            self.assertTrue(first["complete"])
            found = index.locate("target", complete=True)
            self.assertEqual(found["cards"][0]["symbol"], "target")
            ref = found["cards"][0]["ref"]
            self.assertEqual(index.read(ref, ref["revision"])["status"], "ok")
            before = self.source.stat()
            self.source.write_text("def target():\n    return 2\n", encoding="utf-8")
            os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(index.read(ref, ref["revision"])["status"], "stale")
            self.assertEqual(index.refresh()["changed"], 1)
            changed = index.locate("target", complete=True)["cards"][0]["ref"]
            self.assertNotEqual(ref["hash"], changed["hash"])

    def test_incremental_parse_rename_delete_and_branch_invalidation(self):
        with CodeIndex(self.workspace) as index:
            self.assertEqual(index.refresh()["changed"], 1)
            with patch("adhd.context_index._symbols", wraps=__import__(
                    "adhd.context_index", fromlist=["_symbols"])._symbols) as parser:
                self.assertEqual(index.refresh()["changed"], 0)
                self.assertEqual(parser.call_count, 0)
            original = index.locate("target", complete=True)["cards"][0]["ref"]
            self.run_git("switch", "-q", "-c", "other")
            self.assertEqual(index.read(original, original["revision"])["status"], "stale")
            self.assertEqual(index.refresh()["changed"], 1)
            moved = self.source.with_name("renamed.py")
            self.source.rename(moved)
            result = index.refresh()
            self.assertGreaterEqual(result["changed"], 2)
            self.assertEqual(index.locate("target", complete=True)["cards"][0]["path"], "renamed.py")
            moved.unlink()
            self.assertEqual(index.refresh()["changed"], 1)
            self.assertEqual(index.locate("target", complete=True)["cards"], [])

    def test_ref_cannot_escape_scope_or_access_secret_file(self):
        with CodeIndex(self.workspace) as index:
            index.refresh()
            ref = index.locate("target", complete=True)["cards"][0]["ref"]
            outside = {**ref, "path": "../outside.py"}
            self.assertEqual(index.read(outside, outside["revision"])["status"], "denied")
            hidden = {**ref, "path": ".env.production"}
            self.assertEqual(index.read(hidden, hidden["revision"])["status"], "denied")

    def test_refresh_byte_exhaustion_preserves_index_and_releases_queue(self):
        from adhd.maintenance_budget import BudgetExhausted, MaintenanceBudget
        with CodeIndex(self.workspace) as index:
            second = self.workspace / "z.py"
            second.write_text("def z():\n    return 1\n", encoding="utf-8")
            index.refresh()
            original = index.db.execute("SELECT digest FROM files WHERE path='module.py'").fetchone()[0]
            self.source.write_text("def target():\n    return 222\n", encoding="utf-8")
            second.write_text("def z():\n    return 222\n", encoding="utf-8")
            with self.assertRaises(BudgetExhausted):
                index.refresh(paths=["module.py", "z.py"],
                              budget=MaintenanceBudget(read_bytes=len(self.source.read_bytes()) + 1))
            self.assertEqual(index.db.execute(
                "SELECT digest FROM files WHERE path='module.py'").fetchone()[0], original)
            self.assertFalse(index.locate("target", complete=True)["complete"])
            self.assertEqual(index.refresh()["changed"], 2)

    def test_maintenance_cpu_is_measured_on_the_calling_thread(self):
        from adhd.maintenance_budget import BudgetExhausted, MaintenanceBudget
        ticks = iter((1.0, 1.0, 1.3))
        budget = MaintenanceBudget(cpu_seconds=.2, clock=lambda: next(ticks))
        budget.consume(read=1)
        with self.assertRaisesRegex(BudgetExhausted, "maintenance-cpu-budget-exhausted"):
            budget.consume(read=1)

    def test_refresh_checks_cpu_inside_source_hash_loop(self):
        from adhd import context_index
        from adhd.maintenance_budget import BudgetExhausted, MaintenanceBudget
        inside = {"value": False}
        original = context_index._source_bytes
        def read_source(*args, **kwargs):
            inside["value"] = True
            return original(*args, **kwargs)
        budget = MaintenanceBudget(cpu_seconds=.1,
                                   clock=lambda: 1.0 if inside["value"] else 0.0)
        with CodeIndex(self.workspace) as index:
            with patch("adhd.context_index._source_bytes", side_effect=read_source):
                with self.assertRaisesRegex(BudgetExhausted, "maintenance-cpu-budget-exhausted"):
                    index.refresh(paths=["module.py"], budget=budget)
            self.assertIsNone(index.db.execute("SELECT digest FROM files WHERE path='module.py'").fetchone())
            self.assertEqual(index.refresh()["changed"], 1)

    def test_invalid_maintenance_limits_cannot_disable_bounds(self):
        from adhd.maintenance_budget import MaintenanceBudget
        for value in (float("nan"), float("inf"), 0, -1, True):
            with self.subTest(cpu=value), self.assertRaises(ValueError):
                MaintenanceBudget(cpu_seconds=value)
        for value in (True, 1.5, -1):
            with self.subTest(bytes=value), self.assertRaises(ValueError):
                MaintenanceBudget(read_bytes=value)
            with self.subTest(write=value), self.assertRaises(ValueError):
                MaintenanceBudget(write_bytes=value)
        budget = MaintenanceBudget()
        for value in (True, 1.5, -1):
            with self.subTest(consumed=value), self.assertRaises(ValueError):
                budget.consume(read=value)
            with self.subTest(ensured=value), self.assertRaises(ValueError):
                budget.ensure(write=value)


if __name__ == "__main__":
    unittest.main()
