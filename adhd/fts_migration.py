"""Opt-in, quiescent migration of memory's Korean-gram FTS to external content."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile

from filelock import FileLock

from .memory import tokens


def _fingerprint(db: sqlite3.Connection) -> str:
    sha = hashlib.sha256()
    for row in db.execute("SELECT * FROM records ORDER BY id"):
        sha.update(repr(tuple(row)).encode("utf-8"))
    for row in db.execute("SELECT rowid,id,text FROM recall_fts ORDER BY rowid"):
        sha.update(repr(tuple(row)).encode("utf-8"))
    sha.update(repr(tuple(db.execute("SELECT COUNT(*),MAX(id) FROM events").fetchone())).encode())
    return sha.hexdigest()


def _expression(query: str) -> str:
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in sorted(tokens(query))[:40])


def _external_mode(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT sql FROM sqlite_master WHERE name='recall_fts'").fetchone()
    if not row:
        raise ValueError("legacy-fts-unavailable")
    return "content='memory_fts_source'" in row[0]


def _content_view(db: sqlite3.Connection) -> None:
    if not any(row[1] == "search_text" for row in db.execute("PRAGMA table_info(records)")):
        db.execute("ALTER TABLE records ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
    for rowid, key, body in db.execute("SELECT rowid,logical_key,content FROM records ORDER BY rowid"):
        db.execute("UPDATE records SET search_text=? WHERE rowid=?",
                   (" ".join(sorted(tokens(key + " " + body))), rowid))
    db.execute("CREATE VIEW memory_fts_source AS SELECT rowid,id,search_text AS text FROM records")


def _external_fts(db: sqlite3.Connection, name: str) -> None:
    if name not in {"recall_fts", "proposed_fts"}:
        raise ValueError("invalid-fts-name")
    db.execute(f"CREATE VIRTUAL TABLE {name} USING fts5(id UNINDEXED,text,"
               "content='memory_fts_source',content_rowid='rowid',tokenize='unicode61')")
    db.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")


def _triggers(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TRIGGER memory_fts_immutable BEFORE UPDATE OF logical_key,content ON records BEGIN
      SELECT RAISE(ABORT,'Memory content is immutable; retain a new record');
    END""")
    db.execute("""CREATE TRIGGER memory_fts_insert AFTER INSERT ON records BEGIN
      INSERT INTO recall_fts(rowid,id,text) VALUES(new.rowid,new.id,new.search_text);
    END""")
    db.execute("""CREATE TRIGGER memory_fts_delete AFTER DELETE ON records BEGIN
      INSERT INTO recall_fts(recall_fts,rowid,id,text)
      VALUES('delete',old.rowid,old.id,old.search_text);
    END""")
    db.execute("""CREATE TRIGGER memory_fts_update AFTER UPDATE OF id,search_text ON records BEGIN
      INSERT INTO recall_fts(recall_fts,rowid,id,text)
      VALUES('delete',old.rowid,old.id,old.search_text);
      INSERT INTO recall_fts(rowid,id,text) VALUES(new.rowid,new.id,new.search_text);
    END""")


def _compare(db: sqlite3.Connection, queries: list[str]) -> list[dict]:
    mismatches = []
    for query in queries:
        expression = _expression(query)
        if not expression:
            continue
        legacy_only = db.execute(
            "SELECT COUNT(*) FROM (SELECT id FROM recall_fts WHERE recall_fts MATCH ? "
            "EXCEPT SELECT id FROM proposed_fts WHERE proposed_fts MATCH ?)",
            (expression, expression)).fetchone()[0]
        proposed_only = db.execute(
            "SELECT COUNT(*) FROM (SELECT id FROM proposed_fts WHERE proposed_fts MATCH ? "
            "EXCEPT SELECT id FROM recall_fts WHERE recall_fts MATCH ?)",
            (expression, expression)).fetchone()[0]
        if legacy_only or proposed_only:
            mismatches.append({"query": query, "legacy_only": legacy_only,
                               "proposed_only": proposed_only})
    return mismatches


def _shadow(source: sqlite3.Connection, queries: list[str]) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shadow.sqlite3"
        shadow = sqlite3.connect(path)
        try:
            source.backup(shadow)
            shadow.execute("VACUUM")
            baseline_size = path.stat().st_size
            duplicates = shadow.execute(
                "SELECT COUNT(*) FROM (SELECT id FROM recall_fts GROUP BY id HAVING COUNT(*)>1)"
            ).fetchone()[0]
            missing = shadow.execute(
                "SELECT COUNT(*) FROM records r WHERE NOT EXISTS "
                "(SELECT 1 FROM recall_fts f WHERE f.id=r.id)"
            ).fetchone()[0]
            _content_view(shadow)
            _external_fts(shadow, "proposed_fts")
            mismatches = _compare(shadow, queries)
            shadow.execute("DROP TABLE proposed_fts")
            shadow.execute("DROP TABLE recall_fts")
            _external_fts(shadow, "recall_fts")
            _triggers(shadow)
            shadow.commit()
            integrity = shadow.execute("PRAGMA integrity_check").fetchone()[0]
            shadow.execute("INSERT INTO recall_fts(recall_fts,rank) VALUES('integrity-check',1)")
            shadow.commit()
            shadow.execute("VACUUM")
            size = path.stat().st_size
        finally:
            shadow.close()
    return {"duplicate_legacy_ids": duplicates, "records_missing_legacy_fts": missing,
            "query_mismatches": mismatches, "shadow_integrity": integrity,
            "baseline_vacuum_bytes": baseline_size,
            "estimated_shadow_bytes": size}


def _validate_queries(queries: list[str]) -> None:
    if not isinstance(queries, list) or not queries or len(queries) > 100 or any(
            not isinstance(query, str) or not query.strip() or len(query) > 1000
            for query in queries):
        raise ValueError("bounded-representative-queries-required")


def proposal(path: Path, queries: list[str]) -> dict:
    """Create a consistent shadow; report semantic and space results without live writes."""
    _validate_queries(queries)
    path = Path(path).expanduser().resolve(strict=True)
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    source.execute("PRAGMA query_only=ON")
    source.execute("PRAGMA cache_size=-8192")
    try:
        baseline = _fingerprint(source)
        old_size = path.stat().st_size
        if _external_mode(source):
            return {"status": "already_external", "can_cutover": False,
                    "source_unchanged": True}
        result = _shadow(source, queries)
        unchanged = _fingerprint(source) == baseline
        eligible = (unchanged and result["shadow_integrity"] == "ok"
                    and not result["query_mismatches"] and not result["duplicate_legacy_ids"]
                    and not result["records_missing_legacy_fts"])
        return {"status": "proposal_only", "can_cutover": False, "eligible": eligible,
                "source_unchanged": unchanged, "original_bytes": old_size,
                "estimated_net_bytes": result["baseline_vacuum_bytes"] - result["estimated_shadow_bytes"],
                "default_unchanged": True, **result}
    finally:
        source.close()


def _backup(source: sqlite3.Connection, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb"):
        pass
    try:
        backup = sqlite3.connect(destination)
        try:
            source.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup-integrity-failed")
        finally:
            backup.close()
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def cutover(path: Path, queries: list[str], *, allow_cutover: bool = False,
            require_savings: bool = True, backup_path: Path | None = None) -> dict:
    """Migrate only a quiescent DELETE-journal DB under Memory's shared writer lock."""
    if not allow_cutover:
        raise RuntimeError("Explicit opt-in required for FTS cutover")
    _validate_queries(queries)
    path = Path(path).expanduser().resolve(strict=True)
    backup_path = Path(backup_path or path.with_name(path.name + ".pre-external.bak")).absolute()
    if backup_path.exists():
        raise FileExistsError(backup_path)
    with FileLock(str(path) + ".write.lock", timeout=10):
        db = sqlite3.connect(path, timeout=2)
        try:
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise RuntimeError("Quiescent DELETE-journal database required")
            if _external_mode(db):
                raise ValueError("already-external-fts")
            baseline = _fingerprint(db)
            evaluation = _shadow(db, queries)
            if (evaluation["shadow_integrity"] != "ok" or evaluation["query_mismatches"]
                    or evaluation["duplicate_legacy_ids"] or evaluation["records_missing_legacy_fts"]):
                return {"status": "rejected", "reason": "shadow-audit-or-search-mismatch",
                        **evaluation}
            old_size = path.stat().st_size
            if require_savings and evaluation["estimated_shadow_bytes"] >= evaluation["baseline_vacuum_bytes"]:
                return {"status": "unchanged", "reason": "no-measured-space-savings",
                        "original_bytes": old_size, **evaluation}
            if _fingerprint(db) != baseline:
                raise RuntimeError("source-generation-changed")
            _backup(db, backup_path)
            db.execute("BEGIN EXCLUSIVE")
            try:
                if _fingerprint(db) != baseline:
                    raise RuntimeError("source-generation-changed")
                _content_view(db)
                db.execute("DROP TABLE recall_fts")
                _external_fts(db, "recall_fts")
                _triggers(db)
                db.commit()
            except Exception:
                db.rollback()
                raise
            return {"status": "cutover", "backup": str(backup_path),
                    "original_bytes": old_size, **evaluation}
        finally:
            db.close()


def rollback(path: Path) -> dict:
    """Rebuild legacy FTS from current records, including writes since cutover."""
    path = Path(path).expanduser().resolve(strict=True)
    with FileLock(str(path) + ".write.lock", timeout=10):
        db = sqlite3.connect(path, timeout=2)
        try:
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise RuntimeError("Quiescent DELETE-journal database required")
            if not _external_mode(db):
                return {"status": "already_legacy"}
            db.execute("BEGIN EXCLUSIVE")
            try:
                for name in ("memory_fts_insert", "memory_fts_delete", "memory_fts_update",
                             "memory_fts_immutable"):
                    db.execute("DROP TRIGGER " + name)
                db.execute("DROP TABLE recall_fts")
                db.execute("DROP VIEW memory_fts_source")
                db.execute('CREATE VIRTUAL TABLE recall_fts USING fts5(id UNINDEXED,text,tokenize="unicode61")')
                for rid, key, body in db.execute(
                        "SELECT id,logical_key,content FROM records ORDER BY id"):
                    db.execute("INSERT INTO recall_fts VALUES (?,?)",
                               (rid, " ".join(sorted(tokens(key + " " + body)))))
                db.commit()
            except Exception:
                db.rollback()
                raise
            return {"status": "rolled_back", "records": db.execute(
                "SELECT COUNT(*) FROM records").fetchone()[0],
                "post_cutover_writes_preserved": True}
        finally:
            db.close()
