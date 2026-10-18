from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from adhd import context_search, obsidian
from adhd.memory import Memory, namespace


class ContextSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.home = patch.dict(os.environ, {"ADHD_HOME": str(self.base / "state")})
        self.home.start()
        self.addCleanup(self.home.stop)
        (self.workspace / "logic.py").write_text("def 검증():\n    return 'evidence'\n", encoding="utf-8")
        (self.workspace / "screen.ts").write_text("const x1 = 'hello';\n", encoding="utf-8")

    def test_complete_code_search_and_bounded_stale_read(self):
        found = context_search.locate(self.workspace, "검증", sources=("code",), complete=True)
        self.assertTrue(found["complete"])
        self.assertEqual(found["cards"][0]["symbol"], "검증")
        ref = found["cards"][0]["ref"]
        self.assertEqual(context_search.read(self.workspace, ref, expected_revision=ref["revision"],
                                             max_chars=50)["status"], "ok")
        (self.workspace / "logic.py").write_text("def 검증():\n    return 'changed'\n", encoding="utf-8")
        self.assertEqual(context_search.read(self.workspace, ref, expected_revision=ref["revision"])["status"], "stale")
        negative = context_search.locate(self.workspace, "absent-key", sources=("code",), complete=True)
        self.assertTrue(negative["complete"])
        self.assertEqual(negative["cards"], [])

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required for non-Git complete enumeration")
    def test_non_git_complete_search_probes_git_only_once(self):
        for number in range(4):
            (self.workspace / f"extra-{number}.txt").write_text("other evidence\n", encoding="utf-8")
        from adhd import context_index
        with patch("adhd.context_index._git", wraps=context_index._git) as git:
            found = context_search.locate(self.workspace, "검증", sources=("code",), complete=True)
        self.assertTrue(found["complete"])
        self.assertEqual(found["cards"][0]["symbol"], "검증")
        self.assertEqual(git.call_count, 1)

    def test_text_fallback_and_memory_scope(self):
        text = context_search.locate(self.workspace, "x1", sources=("code",), complete=True)
        self.assertEqual(text["cards"][0]["path"], "screen.ts")
        self.assertIsNone(text["cards"][0]["symbol"])
        with Memory() as memory:
            memory.put(scope=namespace(self.workspace), kind="fact", key="한글 키",
                       content="실제 근거", source="fixture", locator="line 1")
        found = context_search.locate(self.workspace, "한글", sources=("memory",), complete=True)
        self.assertTrue(found["complete"])
        ref = found["cards"][0]["ref"]
        self.assertIn("실제 근거", context_search.read(self.workspace, ref,
                      expected_revision=ref["revision"])["text"])
        with self.assertRaisesRegex(ValueError, "scope"):
            context_search.locate(self.workspace, "한글", scope=namespace(self.base / "other"), sources=("memory",))

    def test_no_global_applies_to_reference_read_and_locate(self):
        with Memory() as memory:
            memory.put(scope="global", kind="fact", key="globalmarker",
                       content="shared evidence", source="fixture", locator="line 1")
        found = context_search.locate(self.workspace, "globalmarker", sources=("memory",), complete=True)
        ref = found["cards"][0]["ref"]
        self.assertEqual(ref["scope"], "global")
        self.assertEqual(context_search.read(self.workspace, ref, expected_revision=ref["revision"],
                                             include_global=False)["reason"], "global-memory-excluded")
        excluded = context_search.locate(self.workspace, "globalmarker", sources=("memory",),
                                         complete=True, include_global=False)
        self.assertEqual(excluded["cards"], [])

    def test_budget_truncation_does_not_claim_complete_result(self):
        result = context_search.locate(self.workspace, "검증", sources=("code",),
                                       complete=True, max_chars=500)
        self.assertTrue(result["search_complete"])
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["continuation"]["query"], "검증")

    def test_long_line_read_continues_by_character_offset(self):
        (self.workspace / "long.txt").write_text("needle " + "x" * 700 + "\n", encoding="utf-8")
        found = context_search.locate(self.workspace, "needle", sources=("code",), complete=True)
        ref = found["cards"][0]["ref"]
        first = context_search.read(self.workspace, ref, expected_revision=ref["revision"], max_chars=80)
        self.assertTrue(first["truncated"])
        second = context_search.read(self.workspace, ref, expected_revision=ref["revision"],
                                     max_chars=80, **first["continuation"])
        self.assertEqual(first["text"] + second["text"], ("needle " + "x" * 700)[:160])
        with Memory() as memory:
            memory.put(scope=namespace(self.workspace), kind="fact", key="memoryneedle",
                       content="y" * 400, source="fixture", locator="line 1")
        memory_card = context_search.locate(self.workspace, "memoryneedle", sources=("memory",),
                                            complete=True)["cards"][0]
        memory_ref = memory_card["ref"]
        first = context_search.read(self.workspace, memory_ref,
                                    expected_revision=memory_ref["revision"], max_chars=60)
        second = context_search.read(self.workspace, memory_ref,
                                     expected_revision=memory_ref["revision"], max_chars=60,
                                     **first["continuation"])
        self.assertEqual(first["text"] + second["text"], "y" * 120)


ENGINE = Path(os.environ.get("ADHD_TEST_WIKI_CONTEXT", str(
    Path.home() / "Documents" / "wiki" / "99 시스템" / "도구" / "wiki_context.py")))


@unittest.skipUnless(ENGINE.is_file(), "configured existing wiki_context.py is unavailable")
class ContextWikiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.vault = self.base / "vault"
        (self.vault / "99 시스템" / "도구").mkdir(parents=True)
        shutil.copyfile(ENGINE, self.vault / "99 시스템" / "도구" / "wiki_context.py")
        (self.vault / "10 기록").mkdir()
        self.home = patch.dict(os.environ, {"ADHD_HOME": str(self.base / "state")})
        self.home.start()
        self.addCleanup(self.home.stop)
        obsidian.configure(self.workspace, {"vault": str(self.vault),
            "engine": "99 시스템/도구/wiki_context.py", "enabled": True,
            "read_roots": ["10 기록"], "allowed_privacy": ["public"]})

    def note(self, name, note_id, body, privacy="public"):
        path = self.vault / "10 기록" / name
        path.write_text(f"---\nid: {note_id}\ntype: knowledge\nprivacy: {privacy}\n---\n{body}", encoding="utf-8")
        return path

    def test_warm_hint_is_incomplete_and_checks_live_hash_privacy(self):
        public = self.note("public.md", "wiki:public", "# 한글 근거\noriginal evidence")
        self.note("private.md", "wiki:private", "# 한글 비밀", privacy="private")
        exact = context_search.locate(self.workspace, "한글", sources=("wiki",), complete=True)
        self.assertTrue(exact["complete"])
        self.assertEqual(len(exact["cards"]), 1)
        ref = exact["cards"][0]["ref"]
        warm = context_search.locate(self.workspace, "한글", sources=("wiki",))
        self.assertFalse(warm["complete"])
        self.assertEqual(len(warm["cards"]), 1)
        before = public.stat()
        public.write_text(public.read_text(encoding="utf-8").replace("original", "modified"), encoding="utf-8")
        os.utime(public, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(context_search.read(self.workspace, ref,
                         expected_revision=ref["revision"])["status"], "stale")
        stale_hint = context_search.locate(self.workspace, "original", sources=("wiki",))
        self.assertEqual(stale_hint["cards"], [])
        self.assertFalse(stale_hint["complete"])
        current = context_search.locate(self.workspace, "modified", sources=("wiki",), complete=True)
        self.assertEqual(len(current["cards"]), 1)

    def test_long_wiki_line_can_be_read_to_end_with_continuation(self):
        self.note("long.md", "wiki:long", "# Long note\nneedle " + "X" * 18000 + " END")
        located = context_search.locate(self.workspace, "needle", sources=("wiki",), complete=True)
        ref = located["cards"][0]["ref"]
        pages = []
        request = {}
        for _ in range(5):
            page = context_search.read(self.workspace, ref, expected_revision=ref["revision"],
                                       max_chars=6500, **request)
            self.assertEqual(page["status"], "ok")
            pages.append(page["text"])
            if not page["truncated"]:
                break
            self.assertIsNotNone(page["continuation"])
            request = page["continuation"]
        text = "".join(pages)
        self.assertEqual(text.count("X"), 18000)
        self.assertTrue(text.endswith(" END"))
        self.assertFalse(page["truncated"])


if __name__ == "__main__":
    unittest.main()
