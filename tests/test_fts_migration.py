from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from adhd.fts_migration import proposal, cutover, rollback
from adhd.memory import Memory


class FtsProposalTests(unittest.TestCase):
    def test_shadow_does_not_change_old_default_or_live_db(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.sqlite3"
            with Memory(path) as memory:
                memory.put(scope="global", kind="fact", key="한글 판단", content="근거 자료",
                           source="fixture", locator="line 1")
                memory.put(scope="global", kind="fact", key="x1", content="short identifier",
                           source="fixture", locator="line 2")
                before = memory.recall("한글", "global")
                result = proposal(path, ["한글", "x1", "missing"])
                after = memory.recall("한글", "global")
            self.assertEqual(result["status"], "proposal_only")
            self.assertFalse(result["can_cutover"])
            self.assertTrue(result["source_unchanged"])
            self.assertEqual(before["records"], after["records"])
            with self.assertRaisesRegex(RuntimeError, "opt-in"):
                cutover(path, ["한글"])

    def test_opt_in_cutover_postwrite_forget_and_current_record_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.sqlite3"
            with Memory(path, journal_mode="DELETE") as memory:
                old = memory.put(scope="global", kind="fact", key="한글 원본", content="근거 자료",
                                 source="fixture", locator="line 1")["id"]
                before_forget = memory.put(scope="global", kind="fact", key="삭제 전용", content="지울 본문",
                                           source="fixture", locator="line 0")["id"]
                memory.forget(before_forget)
            stale = Memory(path, journal_mode="DELETE")
            try:
                estimate = proposal(path, ["한글", "원본", "missing"])
                if estimate["estimated_net_bytes"] <= 0:
                    unchanged = cutover(path, ["한글", "원본", "missing"], allow_cutover=True)
                    self.assertEqual(unchanged["status"], "unchanged")
                migrated = cutover(path, ["한글", "원본", "missing"],
                                   allow_cutover=True, require_savings=False)
                with self.assertRaisesRegex(RuntimeError, "reopen"):
                    stale.put(scope="global", kind="fact", key="stale", content="writer",
                              source="fixture", locator="line 3")
            finally:
                stale.close()
            self.assertEqual(migrated["status"], "cutover")
            self.assertTrue(Path(migrated["backup"]).is_file())
            with Memory(path) as memory:
                self.assertTrue(memory.external_fts)
                self.assertNotIn(before_forget, [r["id"] for r in memory.recall("삭제", "global")["records"]])
                with self.assertRaises(sqlite3.IntegrityError):
                    with memory.db:
                        memory.db.execute("UPDATE records SET content='changed' WHERE id=?", (old,))
                new = memory.put(scope="global", kind="fact", key="새로운 용어", content="한글 후속 기록",
                                 source="fixture", locator="line 2")["id"]
                self.assertIn(new, [r["id"] for r in memory.recall("후속", "global")["records"]])
                memory.forget(old)
                self.assertNotIn(old, [r["id"] for r in memory.recall("원본", "global")["records"]])
            result = rollback(path)
            self.assertEqual(result["status"], "rolled_back")
            with Memory(path, journal_mode="DELETE") as memory:
                self.assertFalse(memory.external_fts)
                self.assertIn(new, [r["id"] for r in memory.recall("후속", "global")["records"]])
                self.assertNotIn(old, [r["id"] for r in memory.recall("원본", "global")["records"]])

    def test_cutover_waits_for_other_process_writer_and_includes_its_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.sqlite3"
            ready = Path(temporary) / "writer-ready"
            with Memory(path, journal_mode="DELETE") as memory:
                memory.put(scope="global", kind="fact", key="baseline", content="existing",
                           source="fixture", locator="line 1")
            script = ("import sys,time; from pathlib import Path; "
                      "from adhd import dependencies as _dependencies; from adhd.memory import Memory; "
                      "db=Memory(Path(sys.argv[1]),journal_mode='DELETE'); "
                      "lock=db._write_lock; lock.acquire(); "
                      "db.put(scope='global',kind='fact',key='cross process',content='fresh evidence',"
                      "source='fixture',locator='line 2'); "
                      "Path(sys.argv[2]).write_text('holding'); time.sleep(1.5); "
                      "lock.release(); db.close()")
            child = subprocess.Popen([sys.executable, "-S", "-c", script, str(path), str(ready)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 20
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "child writer did not reach locked checkpoint")
                started = time.monotonic()
                result = cutover(path, ["cross", "baseline"], allow_cutover=True,
                                 require_savings=False)
                self.assertGreater(time.monotonic() - started, 0.3)
                self.assertEqual(result["status"], "cutover")
                self.assertEqual(child.wait(timeout=10), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
            with Memory(path) as memory:
                self.assertTrue(memory.recall("cross", "global")["records"])


if __name__ == "__main__":
    unittest.main()
