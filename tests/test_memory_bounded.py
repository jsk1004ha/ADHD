from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from adhd.memory import Memory, namespace


class BoundedRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scope = namespace(self.root)
        self.other = namespace(self.root / "other")
        self.memory = Memory(self.root / "memory.sqlite3")
        self.addCleanup(self.memory.close)

    def put(self, key, content, **kwargs):
        return self.memory.put(scope=kwargs.pop("scope", self.scope), kind="fact",
                               key=key, content=content, source="fixture",
                               locator="line 1", **kwargs)["id"]

    def test_filters_and_related_ids_use_same_eligible_scope(self):
        visible = self.put("한글 ID", "한글 검색 근거")
        hidden = self.put("한글 hidden", "한글 검색 근거", scope=self.other)
        expired = self.put("한글 expired", "한글 검색 근거",
                           observed=time.time() - 86400 * 2, ttl_days=1)
        wrong_env = self.put("한글 environment", "한글 검색 근거", environment="other")
        with self.memory.db:
            for target in (hidden, expired, wrong_env):
                self.memory.db.execute("INSERT INTO edges VALUES (?,?,?)", (visible, target, "fixture"))
        found = self.memory.recall("한글", self.scope, environment="current", complete=True)
        self.assertEqual([row["id"] for row in found["records"]], [visible])
        self.assertEqual(found["eligible_records"], 1)
        self.assertEqual(found["related_ids"], [])
        self.assertTrue(found["search_complete"])

    def test_fts_unavailable_streams_korean_and_short_identifiers(self):
        korean = self.put("버튼", "클릭 검증")
        short = self.put("x1", "short identifier")
        self.memory.fts = False
        self.assertEqual(self.memory.recall("버튼", self.scope, complete=True)["records"][0]["id"], korean)
        self.assertEqual(self.memory.recall("x1", self.scope, complete=True)["records"][0]["id"], short)
        self.assertEqual(self.memory.recall("missing", self.scope, complete=True)["records"], [])

    def test_bounded_query_reports_incompleteness_and_complete_scan_finds_late_match(self):
        for number in range(810):
            self.put(f"needle-{number:04d}", f"needle item {number:04d}")
        late = self.put("needle-special", "needle exact critical")
        partial = self.memory.recall("needle", self.scope, complete=False)
        self.assertEqual(partial["eligible_records"], 811)
        self.assertFalse(partial["search_complete"])
        self.assertTrue(partial["continuation"])
        complete = self.memory.recall("critical", self.scope, complete=True)
        self.assertTrue(complete["search_complete"])
        self.assertEqual(complete["records"][0]["id"], late)


if __name__ == "__main__":
    unittest.main()
