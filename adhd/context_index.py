"""Rebuildable code locations; source files remain the authority for every read."""
from __future__ import annotations

import ast
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
from typing import Any, Iterable

from filelock import FileLock

from .core import store
from .maintenance_budget import MaintenanceBudget


PARSER_VERSION = 1
POLICY_VERSION = 1
INDEX_VERSION = 1
CACHE_BYTES = 8 * 1024 * 1024
PARSER_CACHE_BYTES = 16 * 1024 * 1024
STREAM_BYTES = 1024 * 1024
MAX_PARSE_BYTES = 16 * 1024 * 1024
MAX_PENDING_MAINTENANCE = 8
EXCLUDED_PARTS = frozenset({".git", ".adhd", ".omx", ".venv", "venv", "node_modules",
                            "__pycache__", "dist", "build"})
EXCLUDED_NAMES = frozenset({".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
                            ".npmrc", ".pypirc", "credentials.json", "secrets.json"})
EXCLUDED_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".pyc", ".jsonl", ".log",
                               ".zip", ".gz", ".7z", ".png", ".jpg", ".pdf", ".pem", ".key"})
_cache: OrderedDict[tuple[str, str, int], bytes] = OrderedDict()
_cache_size = 0
_cache_lock = threading.Lock()
_maintenance_slots = threading.BoundedSemaphore(1 + MAX_PENDING_MAINTENANCE)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _git(workspace: Path, *args: str, timeout: int = 10) -> tuple[int, bytes]:
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(["git", "-C", str(workspace), *args], stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=timeout, check=False)
            if output.tell() > STREAM_BYTES:
                return 2, b""
            output.seek(0)
            return result.returncode, output.read(STREAM_BYTES)
    except (OSError, subprocess.TimeoutExpired):
        return 2, b""


def _safe_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if (path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts)
            or any(part.casefold() in EXCLUDED_PARTS for part in path.parts)
            or path.name.casefold() in EXCLUDED_NAMES or path.name.casefold().startswith(".env.")
            or path.suffix.casefold() in EXCLUDED_SUFFIXES):
        raise ValueError("source-excluded")
    current = root
    for part in path.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                              getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("source-link-excluded")
    resolved = current.resolve(strict=False)
    if os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(resolved)))) != os.path.normcase(str(root)):
        raise ValueError("source-outside-workspace")
    return current


def _symbols(raw: bytes, path: str, budget: MaintenanceBudget | None = None) -> list[tuple[str, str, int, int]]:
    if not path.endswith(".py") or len(raw) > MAX_PARSE_BYTES:
        return []
    key = (hashlib.sha256(raw).hexdigest(), path, PARSER_VERSION)
    global _cache_size
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            if budget is not None:
                budget.consume(read=len(cached))
            return [tuple(value) for value in json.loads(cached)]
    tree = ast.parse(raw.decode("utf-8-sig"), filename=path)
    found: list[tuple[str, str, int, int]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.parents: list[str] = []

        def _definition(self, node: Any, kind: str) -> None:
            name = ".".join([*self.parents, node.name])
            found.append((name, kind, node.lineno, getattr(node, "end_lineno", node.lineno)))
            self.parents.append(node.name)
            self.generic_visit(node)
            self.parents.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._definition(node, "class")

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._definition(node, "function")

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._definition(node, "async_function")

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load):
                found.append((node.id, "reference", node.lineno, node.lineno))

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.ctx, ast.Load):
                found.append((node.attr, "reference", node.lineno, node.lineno))
            self.generic_visit(node)

    Visitor().visit(tree)
    if budget is not None:
        budget.check()
    encoded = json.dumps(found, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= PARSER_CACHE_BYTES:
        if budget is not None:
            budget.consume(write=len(encoded))
        with _cache_lock:
            _cache[key] = encoded
            _cache_size += len(encoded)
            while _cache_size > PARSER_CACHE_BYTES:
                _, removed = _cache.popitem(last=False)
                _cache_size -= len(removed)
    return found


def _source_bytes(path: Path, *, parse: bool,
                  budget: MaintenanceBudget | None = None) -> tuple[str, bytes | None, os.stat_result]:
    before = path.stat()
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    buffered = 0
    with path.open("rb") as source:
        while chunk := source.read(STREAM_BYTES):
            digest.update(chunk)
            if budget is not None:
                budget.consume(read=len(chunk))
            if parse and buffered + len(chunk) <= MAX_PARSE_BYTES:
                chunks.append(chunk)
                buffered += len(chunk)
            else:
                parse = False
                chunks.clear()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("source-changed-during-read")
    return digest.hexdigest(), b"".join(chunks) if parse else None, after


class CodeIndex:
    """Synchronous, lock-serialized metadata maintenance; no resident watcher."""

    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace-required")
        code, top = _git(self.workspace, "rev-parse", "--show-toplevel")
        self._git_enabled = code == 0
        self.repository = Path(os.fsdecode(top).strip()).resolve() if code == 0 else self.workspace
        if self.repository != self.workspace:
            raise ValueError("workspace-must-be-repository-root")
        code, common = _git(self.workspace, "rev-parse", "--git-common-dir") if self._git_enabled else (1, b"")
        repo_identity = str((self.workspace / os.fsdecode(common).strip()).resolve()) if code == 0 else str(self.workspace)
        self.repository_id = _digest(repo_identity)
        self.worktree_id = _digest(str(self.workspace))
        folder = store() / "context-index"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"{self.repository_id}-{self.worktree_id}.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=2000")
        self.db.execute("PRAGMA cache_size=-8192")
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, state TEXT NOT NULL,
                    reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS symbols (path TEXT NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
                CREATE INDEX IF NOT EXISTS symbols_path ON symbols(path);
            """)
        self._full_reconciled = False

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> CodeIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _head_branch(self) -> tuple[str, str]:
        if not self._git_enabled:
            return "unversioned", "detached"
        code, raw = _git(self.workspace, "rev-parse", "HEAD")
        head = os.fsdecode(raw).strip() if code == 0 else "unversioned"
        code, raw = _git(self.workspace, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = os.fsdecode(raw).strip() if code == 0 else "detached"
        return head, branch

    def _paths(self, budget: MaintenanceBudget | None = None) -> tuple[list[str], bool]:
        if self._git_enabled:
            code, raw = _git(self.workspace, "ls-files", "--cached", "--others", "--exclude-standard", "-z", timeout=30)
            if code == 0:
                if budget is not None:
                    budget.consume(read=len(raw))
                names = [os.fsdecode(value) for value in raw.split(b"\0") if value]
                return sorted(set(names)), True
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(["rg", "--files", "--hidden", "-0"], cwd=self.workspace,
                                        stdout=output, stderr=subprocess.DEVNULL, timeout=30, check=False)
                if output.tell() > STREAM_BYTES:
                    return [], False
                output.seek(0)
                raw = output.read(STREAM_BYTES)
        except (OSError, subprocess.TimeoutExpired):
            return [], False
        if budget is not None:
            budget.consume(read=len(raw))
        return sorted({os.fsdecode(value) for value in raw.split(b"\0") if value}), result.returncode in (0, 1)

    def refresh(self, paths: Iterable[str] | None = None, *, lost_hints: bool = False,
                budget: MaintenanceBudget | None = None) -> dict[str, Any]:
        if not _maintenance_slots.acquire(blocking=False):
            return {"complete": False, "reason": "maintenance-queue-full", "changed": 0}
        budget = budget if budget is not None else MaintenanceBudget()
        try:
            with FileLock(str(self.path) + ".lock", timeout=10):
                self._full_reconciled = False
                budget.check()
                head, branch = self._head_branch()
                version = json.dumps([INDEX_VERSION, PARSER_VERSION, POLICY_VERSION, branch])
                with self.db:
                    old = self.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                    if old is None or old[0] != version or lost_hints:
                        budget.consume(write=len(version.encode("utf-8")))
                        self.db.execute("DELETE FROM symbols")
                        self.db.execute("DELETE FROM files")
                        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('version',?)", (version,))
                    if paths is None:
                        current, complete = self._paths(budget)
                        previous = {row[0] for row in self.db.execute("SELECT path FROM files")}
                        deleted = previous - set(current) if complete else set()
                    else:
                        current = sorted(set(paths))
                        complete = False
                        deleted = set()
                    budget.check()
                    changed = 0
                    for relative in sorted(deleted):
                        budget.consume(write=len(relative.encode("utf-8")))
                        self.db.execute("DELETE FROM files WHERE path=?", (relative,))
                        self.db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                        changed += 1
                    for relative in current:
                        budget.check()
                        try:
                            path = _safe_file(self.workspace, relative)
                        except ValueError:
                            continue
                        if not path.is_file():
                            existed = self.db.execute("SELECT 1 FROM files WHERE path=?", (relative,)).fetchone()
                            budget.consume(write=len(relative.encode("utf-8")))
                            self.db.execute("DELETE FROM files WHERE path=?", (relative,))
                            self.db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                            changed += int(existed is not None)
                            continue
                        try:
                            digest, raw, info = _source_bytes(path, parse=path.suffix == ".py", budget=budget)
                            _safe_file(self.workspace, relative)
                        except (OSError, ValueError):
                            complete = False
                            continue
                        previous = self.db.execute("SELECT digest FROM files WHERE path=?", (relative,)).fetchone()
                        if previous and previous[0] == digest:
                            continue
                        state, reason, symbols = "ok", "", []
                        if raw is not None:
                            try:
                                symbols = _symbols(raw, relative, budget=budget)
                            except (SyntaxError, UnicodeError, ValueError) as exc:
                                state, reason = "text", type(exc).__name__
                        elif path.suffix == ".py":
                            state, reason = "text", "oversized-python-source"
                        budget.consume(write=len(relative.encode("utf-8")) + len(digest) +
                                       sum(len(str(item).encode("utf-8")) for item in symbols))
                        self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                                        (relative, digest, info.st_mtime_ns, info.st_size, state, reason))
                        self.db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                        self.db.executemany("INSERT INTO symbols VALUES (?,?,?,?,?)",
                                            ((relative, *item) for item in symbols))
                        changed += 1
                    budget.check()
                self._full_reconciled = complete and paths is None
                return {"complete": complete, "changed": changed, "head": head, "branch": branch,
                        "parser_version": PARSER_VERSION, "policy_version": POLICY_VERSION}
        finally:
            _maintenance_slots.release()

    def _ref(self, relative: str, digest: str, start: int, end: int,
             head_branch: tuple[str, str] | None = None) -> dict[str, Any]:
        head, branch = head_branch or self._head_branch()
        if self._git_enabled:
            code, dirty = _git(self.workspace, "status", "--porcelain", "--", relative)
            overlay = f"dirty:{digest}" if code != 0 or dirty else "clean"
        else:
            overlay = f"dirty:{digest}"
        return {"source": "code", "repository_id": self.repository_id, "worktree_id": self.worktree_id,
                "path": relative, "head": head, "branch": branch, "overlay": overlay,
                "hash": digest, "revision": f"{head}:{digest}", "start_line": start, "end_line": end,
                "parser_version": PARSER_VERSION, "policy_version": POLICY_VERSION}

    def read(self, ref: dict[str, Any], expected_revision: str, start_line: int | None = None,
             end_line: int | None = None, *, max_chars: int = 6500,
             start_char: int = 0) -> dict[str, Any]:
        if not isinstance(ref, dict) or ref.get("repository_id") != self.repository_id or ref.get("worktree_id") != self.worktree_id:
            return {"status": "denied", "reason": "reference-scope-mismatch"}
        relative = ref.get("path")
        if not isinstance(relative, str):
            return {"status": "denied", "reason": "invalid-path"}
        try:
            path = _safe_file(self.workspace, relative)
        except ValueError as exc:
            return {"status": "denied", "reason": str(exc)}
        if not path.is_file():
            return {"status": "stale", "reason": "source-missing"}
        head, branch = self._head_branch()
        if (expected_revision != ref.get("revision") or head != ref.get("head") or branch != ref.get("branch")
                or ref.get("parser_version") != PARSER_VERSION or ref.get("policy_version") != POLICY_VERSION):
            return {"status": "stale", "reason": "revision-or-policy-changed"}
        current_ref = self._ref(relative, ref.get("hash", ""), ref.get("start_line", 1), ref.get("end_line", 1),
                                (head, branch))
        if current_ref["overlay"] != ref.get("overlay"):
            return {"status": "stale", "reason": "overlay-changed"}
        start = ref.get("start_line") if start_line is None else start_line
        end = ref.get("end_line") if end_line is None else end_line
        if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int)
                or isinstance(end, bool) or not 1 <= start <= end or end - start > 500
                or not 1 <= max_chars <= 16000 or not isinstance(start_char, int)
                or isinstance(start_char, bool) or not 0 <= start_char <= STREAM_BYTES):
            raise ValueError("invalid-source-range-or-budget")
        before = path.stat()
        digest = hashlib.sha256()
        output: list[str] = []
        size = 0
        truncated = False
        continuation = None
        try:
            with path.open("rb") as source:
                for number, line in enumerate(iter(lambda: source.readline(STREAM_BYTES + 1), b""), 1):
                    if len(line) > STREAM_BYTES:
                        return {"status": "unavailable", "reason": "source-line-too-long"}
                    digest.update(line)
                    if start <= number <= end:
                        decoded = line.decode("utf-8", errors="replace")
                        offset = start_char if number == start else 0
                        if offset > len(decoded):
                            raise ValueError("invalid-source-character-offset")
                        remaining = decoded[offset:]
                        if not truncated:
                            take = min(len(remaining), max_chars - size)
                            output.append(remaining[:take])
                            size += take
                            if take < len(remaining):
                                truncated = True
                                continuation = {"start_line": number,
                                                "start_char": offset + take, "end_line": end}
        except OSError:
            return {"status": "unavailable", "reason": "source-read-failed"}
        after = path.stat()
        try:
            safe_after = _safe_file(self.workspace, relative) == path
        except ValueError:
            safe_after = False
        if ((before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino)
                or digest.hexdigest() != ref.get("hash") or not safe_after):
            return {"status": "stale", "reason": "source-changed"}
        return {"status": "ok", "ref": ref, "start_line": start, "end_line": end,
                "text": "".join(output), "truncated": truncated, "used_chars": size,
                "continuation": continuation}

    def locate(self, query: str, *, paths: Iterable[str] | None = None, limit: int = 6,
               max_chars: int = 6500, complete: bool = False) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("query-required")
        if not 1 <= limit <= 50 or not 500 <= max_chars <= 16000:
            raise ValueError("invalid-search-budget")
        q = query.strip().casefold()
        selected_paths = sorted(set(paths)) if paths is not None else [row[0] for row in self.db.execute("SELECT path FROM files ORDER BY path")]
        candidates: list[tuple[int, str, str | None, int, int, str]] = []
        for relative in selected_paths:
            row = self.db.execute("SELECT digest,state FROM files WHERE path=?", (relative,)).fetchone()
            if row is None:
                continue
            symbol = self.db.execute("SELECT name,kind,start_line,end_line FROM symbols WHERE path=? AND instr(lower(name),?)>0 "
                                     "ORDER BY CASE WHEN lower(name)=? THEN 0 WHEN kind='reference' THEN 2 ELSE 1 END,start_line LIMIT 1",
                                     (relative, q, q)).fetchone()
            if symbol:
                priority = 4 if symbol["name"].casefold() == q else 3
                candidates.append((priority, relative, symbol["name"], symbol["start_line"], symbol["end_line"], row["digest"]))
            elif q in relative.casefold():
                candidates.append((2, relative, None, 1, 1, row["digest"]))
            else:
                try:
                    path = _safe_file(self.workspace, relative)
                    with path.open("r", encoding="utf-8", errors="replace") as source:
                        line = next((number for number, text in enumerate(source, 1) if q in text.casefold()), None)
                except (OSError, ValueError):
                    line = None
                if line is not None:
                    candidates.append((1, relative, None, line, line, row["digest"]))
        candidates.sort(key=lambda item: (-item[0], item[1], item[3]))
        cards=[]
        used=0
        stale=False
        head_branch = self._head_branch() if candidates else None
        for _, relative, symbol, start, end, digest in candidates:
            if max_chars-used < 1:
                break
            ref=self._ref(relative,digest,start,min(end,start+20),head_branch)
            evidence=self.read(ref,ref["revision"],max_chars=min(500,max_chars-used))
            if evidence["status"] != "ok":
                stale=True
                continue
            card={"source": "code", "path": relative, "symbol": symbol,
                  "kind": "python-symbol" if symbol else "text", "excerpt": evidence["text"], "ref": ref}
            encoded=json.dumps(card,ensure_ascii=False)
            if used+len(encoded)>max_chars:
                break
            cards.append(card);used+=len(encoded)
            if len(cards)>=limit:
                break
        finished = complete and self._full_reconciled and not stale
        return {"cards": cards, "complete": finished, "truncated": len(cards)<len(candidates),
                "continuation": None if finished else {"complete": True},
                "used_chars": used, "repository_id": self.repository_id, "worktree_id": self.worktree_id,
                "parser_version": PARSER_VERSION, "policy_version": POLICY_VERSION,
                "warnings": ["source-changed-during-search"] if stale else []}
