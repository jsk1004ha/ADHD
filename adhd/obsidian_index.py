"""Rebuildable, source-native index for configured Obsidian Markdown notes.

The vault remains the source of truth.  This cache never grants authority or
privacy access; callers recheck both the current policy and source revision.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Callable

from filelock import FileLock


INDEX_VERSION = 1
PRIVACY = frozenset({"public", "private", "restricted", "sensitive"})
RETRIEVAL = frozenset({"on_demand", "pinned", "excluded"})
ID_RE = re.compile(r"^[^\s\x00-\x1f]{1,200}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
LIST_KEYS = frozenset({"aliases", "projects", "topics", "source_ids", "seed_records", "supersedes", "contradicts"})
SCALAR_KEYS = frozenset({"id", "type", "layer", "privacy", "status", "retrieval", "title",
                         "valid_from", "valid_until", "verified_at",
                         "evidence_strength", "created", "updated", "schema_version",
                         "knowledge_kind", "artifact_kind", "source_uri", "source_kind"})
BOOL_KEYS = frozenset({"template", "search_body", "context_scope"})
MAX_NOTE_BYTES = 4 * 1024 * 1024


def _date(value: str) -> date:
    return date.fromisoformat(value[:10]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", value) else date.fromisoformat(value)


def _reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def safe_root(root: Path | str) -> Path:
    base = Path(root).absolute()
    if not base.is_dir():
        raise ValueError("unsafe-root")
    for component in (base, *base.parents):
        if _reparse(component):
            raise ValueError("path-reparse-point")
    return base


def safe_path(root: Path | str, candidate: Path | str, *, allow_missing: bool = False) -> Path:
    """Validate lexical and resolved containment, including Windows reparse points."""
    base = safe_root(root)
    path = Path(candidate)
    path = path.absolute() if path.is_absolute() else base / path
    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise ValueError("path-outside-root") from exc
    if any(part in {"..", "."} for part in relative.parts):
        raise ValueError("path-traversal")
    current = base
    for part in relative.parts:
        current = current / part
        if _reparse(current):
            raise ValueError("path-reparse-point")
    try:
        resolved = path.resolve(strict=not allow_missing)
    except (OSError, RuntimeError) as exc:
        raise ValueError("path-unavailable") from exc
    if os.path.commonpath((os.path.normcase(str(base.resolve())), os.path.normcase(str(resolved)))) != os.path.normcase(str(base.resolve())):
        raise ValueError("path-outside-root")
    if not allow_missing and not path.exists():
        raise ValueError("path-unavailable")
    return path


def canonical_id(vault_id: str, note_id: str) -> str:
    return f"obsidian:{vault_id}:{note_id}"


def _strict_metadata(text: str, engine: Any) -> tuple[dict[str, Any], str]:
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("missing-frontmatter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError("unclosed-frontmatter")
    lines = normalized[4:end].splitlines()
    keys: set[str] = set()
    current_list: str | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace() and stripped.startswith("- ") and current_list:
            if not stripped[2:].strip() or stripped[2:].lstrip().startswith(("{", "[")):
                raise ValueError("malformed-list")
            continue
        if line[:1].isspace() or ":" not in line:
            raise ValueError("malformed-frontmatter")
        key, value = line.split(":", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key) or key in keys:
            raise ValueError("malformed-key")
        keys.add(key)
        current_list = key if not value.strip() else None
        if current_list and key in {"id", "type", "privacy", "retrieval"}:
            raise ValueError("empty-field")
        if value.strip().startswith(("{", "[")) and value.strip() != "[]":
            raise ValueError("unsupported-frontmatter-value")
    metadata, body = engine.parse_frontmatter(normalized)
    if not isinstance(metadata, dict) or not isinstance(body, str):
        raise ValueError("invalid-parser-result")
    for key in SCALAR_KEYS - {"id", "type", "privacy", "retrieval"}:
        if metadata.get(key) in ([], "", "null", "~"):
            metadata.pop(key, None)
    if metadata.get("verification_scope") == []:
        metadata.pop("verification_scope", None)
    for key in ("id", "type", "privacy"):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f"missing-{key}")
    if not ID_RE.fullmatch(metadata["id"]):
        raise ValueError("invalid-id")
    if metadata["privacy"] not in PRIVACY:
        raise ValueError("invalid-privacy")
    if metadata.get("retrieval", "on_demand") not in RETRIEVAL:
        raise ValueError("invalid-retrieval")
    for key in LIST_KEYS:
        value = metadata.get(key, [])
        if not isinstance(value, (str, list)) or (isinstance(value, list) and any(not isinstance(item, str) or not item.strip() for item in value)):
            raise ValueError("invalid-list")
    for key in SCALAR_KEYS & metadata.keys():
        if not isinstance(metadata[key], str):
            raise ValueError("invalid-scalar")
    for key in BOOL_KEYS & metadata.keys():
        if not isinstance(metadata[key], bool):
            raise ValueError("invalid-boolean")
    scope = metadata.get("verification_scope")
    if scope is not None and not (isinstance(scope, str) or isinstance(scope, list)
                                  and all(isinstance(value, str) for value in scope)):
        raise ValueError("invalid-verification-scope")
    for key in ("valid_from", "valid_until", "verified_at"):
        if key in metadata:
            try:
                _date(metadata[key])
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid-date") from exc
    if metadata.get("valid_from") and metadata.get("valid_until") and _date(metadata["valid_from"]) > _date(metadata["valid_until"]):
        raise ValueError("invalid-validity-range")
    return metadata, body


def _open_index(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    with db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, digest TEXT,
                note_id TEXT, state TEXT NOT NULL, title TEXT, body TEXT, metadata TEXT,
                reason TEXT
            );
            CREATE INDEX IF NOT EXISTS files_note_id ON files(note_id);
            CREATE TABLE IF NOT EXISTS aliases (alias TEXT NOT NULL, note_id TEXT NOT NULL,
                PRIMARY KEY(alias, note_id));
            CREATE TABLE IF NOT EXISTS revisions (note_id TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(note_id, digest));
            CREATE TABLE IF NOT EXISTS tombstones (kind TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(kind, value));
        """)
    return db


def _candidates(vault: Path, roots: list[str]) -> tuple[set[str], list[str]]:
    found: set[str] = set()
    warnings: list[str] = []
    for root in roots:
        try:
            target = safe_path(vault, root, allow_missing=True)
        except ValueError as exc:
            warnings.append("unsafe-read-root")
            continue
        if target.is_file() and target.suffix.lower() == ".md":
            found.add(target.relative_to(vault).as_posix())
        elif target.is_dir():
            for directory, subdirs, files in os.walk(target, followlinks=False):
                base = Path(directory)
                subdirs[:] = [name for name in subdirs if not _reparse(base / name)]
                for name in files:
                    if name.lower().endswith(".md"):
                        path = base / name
                        try:
                            safe_path(vault, path)
                        except ValueError:
                            warnings.append("unsafe-note-path")
                            continue
                        found.add(path.relative_to(vault).as_posix())
    return found, warnings


def _read(path: Path) -> tuple[bytes, str]:
    before = path.stat()
    if before.st_size > MAX_NOTE_BYTES:
        raise ValueError("oversized-note")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("changed-during-read")
    return raw, hashlib.sha256(raw).hexdigest()


def refresh(workspace: Path, config: dict[str, Any], engine: Any) -> tuple[sqlite3.Connection, int, list[str]]:
    """Reconcile every path/hash, parsing only changed bytes, in one generation."""
    state = safe_path(workspace, ".adhd", allow_missing=True)
    state.mkdir(exist_ok=True)
    index = safe_path(workspace, state / "obsidian-index.sqlite3", allow_missing=True)
    with FileLock(str(index) + ".lock", timeout=30):
        db = _open_index(index)
        try:
            vault = Path(config["vault"])
            found, warnings = _candidates(vault, config["read_roots"])
            with db:
                version = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                if version and int(version[0]) != INDEX_VERSION:
                    db.execute("DELETE FROM files")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('version',?)", (str(INDEX_VERSION),))
                old = {row["path"]: row for row in db.execute("SELECT * FROM files")}
                changed = bool(set(old) - found)
                for gone in set(old) - found:
                    db.execute("DELETE FROM files WHERE path=?", (gone,))
                for relative in sorted(found):
                    digest = None
                    info = None
                    try:
                        path = safe_path(vault, relative)
                        raw, digest = _read(path)
                        info = path.stat()
                        previous = old.get(relative)
                        if previous and previous["digest"] == digest:
                            if previous["state"] == "invalid":
                                warnings.append(f"note-excluded:{previous['reason']}")
                                continue
                            if previous["state"] == "ok":
                                if (previous["mtime_ns"], previous["size"]) != (info.st_mtime_ns, info.st_size):
                                    db.execute("UPDATE files SET mtime_ns=?,size=? WHERE path=?", (info.st_mtime_ns, info.st_size, relative))
                                continue
                        metadata, body = _strict_metadata(raw.decode("utf-8-sig"), engine)
                        if (metadata.get("template") is True or metadata.get("retrieval") == "excluded"
                                or metadata.get("status") in {"excluded", "deleted", "forgotten"}):
                            raise ValueError("excluded-note")
                        note_id = canonical_id(config["vault_id"], metadata["id"])
                        title = engine.note_title(path, body)
                        row = (relative, info.st_mtime_ns, info.st_size, digest, note_id, "ok", title,
                               body, json.dumps(metadata, ensure_ascii=False), "")
                        db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)", row)
                        db.execute("INSERT OR IGNORE INTO revisions VALUES (?,?)", (note_id, digest))
                        aliases = {note_id, metadata["id"], relative, engine.stable_note_source_id(path),
                                   digest, "sha256:" + digest}
                        aliases.update(engine.list_values(metadata, "aliases"))
                        aliases.update(engine.list_values(metadata, "source_ids"))
                        for alias in aliases:
                            if alias:
                                db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (alias, note_id))
                        changed = True
                    except (OSError, UnicodeError, ValueError) as exc:
                        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                        warnings.append(f"note-excluded:{reason}")
                        previous = old.get(relative)
                        if not previous or previous["state"] != "invalid" or previous["reason"] != reason or previous["digest"] != digest:
                            changed = True
                        db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (relative, info.st_mtime_ns if info else None, info.st_size if info else None,
                                    digest, None, "invalid", None, None, None, reason))
                generation = int((db.execute("SELECT value FROM meta WHERE key='generation'").fetchone() or [0])[0])
                if changed or not generation:
                    generation += 1
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('generation',?)", (str(generation),))
            return db, generation, warnings
        except BaseException:
            db.close()
            raise


def live_rows(db: sqlite3.Connection, config: dict[str, Any]) -> tuple[list[sqlite3.Row], list[str]]:
    rows = db.execute("SELECT * FROM files WHERE state='ok' ORDER BY path").fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["note_id"]] = counts.get(row["note_id"], 0) + 1
    denied = {(row["kind"], row["value"]) for row in db.execute("SELECT kind,value FROM tombstones")}
    aliases: dict[str, set[str]] = {}
    for row in db.execute("SELECT alias,note_id FROM aliases"):
        aliases.setdefault(row["note_id"], set()).add(row["alias"])
    visible: list[sqlite3.Row] = []
    warnings: list[str] = []
    for row in rows:
        note_id = row["note_id"]
        if counts[note_id] > 1:
            warnings.append("duplicate-id-quarantined")
            continue
        metadata = json.loads(row["metadata"])
        if metadata["privacy"] not in config["allowed_privacy"]:
            continue
        if ("id", note_id) in denied or ("revision", row["digest"]) in denied or any(("alias", alias) in denied for alias in aliases.get(note_id, ())):
            continue
        visible.append(row)
    return visible, sorted(set(warnings))


def resolve_id(db: sqlite3.Connection, value: str, rows: list[sqlite3.Row]) -> tuple[str | None, str | None]:
    valid = {row["note_id"] for row in rows}
    if value in valid:
        return value, None
    matches = {row["note_id"] for row in db.execute("SELECT note_id FROM aliases WHERE alias=?", (value,)) if row["note_id"] in valid}
    if len(matches) == 1:
        return next(iter(matches)), None
    return None, "ambiguous-alias" if matches else "note-not-found"


def forget_index(workspace: Path, config: dict[str, Any], note_id: str, engine: Any) -> dict[str, Any]:
    db, generation, _ = refresh(workspace, config, engine)
    try:
        rows = db.execute("SELECT note_id FROM aliases WHERE alias=?", (note_id,)).fetchall()
        ids = {row[0] for row in rows}
        if note_id.startswith(f"obsidian:{config['vault_id']}:"):
            ids.add(note_id)
        if not ids:
            return {"status": "not_found", "id": note_id, "index_generation": generation}
        if len(ids) != 1:
            return {"status": "ambiguous", "id": note_id, "index_generation": generation}
        canonical = next(iter(ids))
        with db:
            db.execute("INSERT OR IGNORE INTO tombstones VALUES ('id',?)", (canonical,))
            for row in db.execute("SELECT alias FROM aliases WHERE note_id=?", (canonical,)):
                db.execute("INSERT OR IGNORE INTO tombstones VALUES ('alias',?)", (row[0],))
            for row in db.execute("SELECT digest FROM revisions WHERE note_id=?", (canonical,)):
                db.execute("INSERT OR IGNORE INTO tombstones VALUES ('revision',?)", (row[0],))
            generation += 1
            db.execute("INSERT OR REPLACE INTO meta VALUES ('generation',?)", (str(generation),))
        return {"status": "forgotten", "id": canonical, "index_generation": generation}
    finally:
        db.close()
