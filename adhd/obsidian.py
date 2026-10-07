"""Scoped, read-only Obsidian retrieval for ADHD workspaces.

Markdown is untrusted source data.  No text or metadata from this module is a
controller instruction, proof of verification, or an installed skill.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Any

from filelock import FileLock

from . import obsidian_index as index


PACKET_SCHEMA_VERSION = 1
safe_path = index.safe_path
_DEFAULT_PRIVACY = ["public"]


def _workspace(value: Path | str) -> Path:
    return index.safe_root(value)


def _config_path(workspace: Path) -> Path:
    return safe_path(workspace, ".adhd/obsidian.json", allow_missing=True)


def load_config(workspace: Path | str, required: bool = False) -> dict[str, Any] | None:
    """Return current workspace policy; missing configuration leaves retrieval off."""
    path = _config_path(_workspace(workspace))
    if not path.is_file():
        if required:
            raise ValueError("obsidian-not-configured")
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(config, dict) or config.get("schema_version") != 1
                or type(config.get("policy_epoch")) is not int or config["policy_epoch"] < 1
                or type(config.get("enabled")) is not bool or type(config.get("write_enabled")) is not bool
                or not isinstance(config.get("vault_id"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", config["vault_id"])):
            raise ValueError("invalid-obsidian-config")
        vault = Path(config["vault"])
        engine = safe_path(vault, config["engine"])
        if engine.name != "wiki_context.py":
            raise ValueError("invalid-obsidian-config")
        for key in ("read_roots", "write_roots"):
            if not isinstance(config[key], list) or any(not isinstance(root, str) or not root for root in config[key]):
                raise ValueError("invalid-obsidian-config")
            for root in config[key]:
                safe_path(vault, root, allow_missing=True)
        if (not isinstance(config["allowed_privacy"], list)
                or not all(isinstance(value, str) for value in config["allowed_privacy"])
                or not set(config["allowed_privacy"]).issubset(index.PRIVACY)):
            raise ValueError("invalid-obsidian-config")
        return config
    except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid-obsidian-config") from exc


@lru_cache(maxsize=4)
def _import_engine(path: str, mtime_ns: int, size: int, source_sha256: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"adhd_configured_wiki_{uuid.uuid5(uuid.NAMESPACE_URL, path).hex}", path)
    if spec is None or spec.loader is None:
        raise ValueError("wiki-engine-unavailable")
    import sys
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    for name in ("Note", "parse_frontmatter", "note_title", "rank_notes", "exact_project_matches",
                  "scoped_project_matches", "project_identifiers", "canonical_key", "list_values",
                  "stable_note_source_id", "redact_private_text"):
        if not hasattr(module, name):
            raise ValueError(f"wiki-engine-missing:{name}")
    return module


def _engine(config: dict[str, Any]) -> Any:
    path = safe_path(config["vault"], config["engine"])
    if path.name != "wiki_context.py":
        raise ValueError("wiki-engine-must-be-wiki_context.py")
    info = path.stat()
    return _import_engine(str(path), info.st_mtime_ns, info.st_size, index._read(path)[1])


def configure(workspace: Path | str, payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically install or revise a workspace-local vault policy."""
    ws = _workspace(workspace)
    if not isinstance(payload, dict):
        raise ValueError("invalid-obsidian-payload")
    unknown = set(payload) - {"vault", "engine", "enabled", "read_roots", "allowed_privacy",
                              "write_roots", "write_enabled", "vault_id"}
    if unknown:
        raise ValueError("unknown-obsidian-config-fields")
    if not isinstance(payload.get("vault"), str) or not isinstance(payload.get("engine"), str):
        raise ValueError("vault-and-engine-required")
    vault = index.safe_root(payload["vault"])
    engine_path = safe_path(vault, payload["engine"])
    if engine_path.name != "wiki_context.py":
        raise ValueError("wiki-engine-must-be-wiki_context.py")
    for key in ("enabled", "write_enabled"):
        if key in payload and not isinstance(payload[key], bool):
            raise ValueError("invalid-boolean-config")
    read_roots = payload.get("read_roots", ["홈.md", "10 기록", "20 지식", "30 인덱스", "99 시스템/운영"])
    write_roots = payload.get("write_roots", [])
    allowed = payload.get("allowed_privacy", _DEFAULT_PRIVACY)
    if (not all(isinstance(group, list) for group in (read_roots, write_roots, allowed))
            or any(not isinstance(value, str) or not value for group in (read_roots, write_roots, allowed) for value in group)
            or not set(allowed).issubset(index.PRIVACY)):
        raise ValueError("invalid-policy-list")
    for root in read_roots + write_roots:
        safe_path(vault, root, allow_missing=True)
    if payload.get("write_enabled", False) and not write_roots:
        raise ValueError("write-roots-required")
    _import_engine(str(engine_path), engine_path.stat().st_mtime_ns, engine_path.stat().st_size,
                   index._read(engine_path)[1])
    state = safe_path(ws, ".adhd", allow_missing=True)
    state.mkdir(exist_ok=True)
    path = _config_path(ws)
    with FileLock(str(path) + ".lock", timeout=30):
        previous = load_config(ws)
        if previous and os.path.normcase(str(Path(previous["vault"]))) != os.path.normcase(str(vault)):
            raise ValueError("vault-root-change-requires-new-configuration")
        vault_id = previous["vault_id"] if previous else payload.get(
            "vault_id", uuid.uuid5(uuid.NAMESPACE_URL, "adhd-obsidian:" + os.path.normcase(str(vault))).hex)
        if previous and "vault_id" in payload and payload["vault_id"] != vault_id:
            raise ValueError("vault-id-immutable")
        if not isinstance(vault_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", vault_id):
            raise ValueError("invalid-vault-id")
        config = {"schema_version": 1, "vault_id": vault_id, "policy_epoch": previous["policy_epoch"] + 1 if previous else 1,
                  "vault": str(vault), "engine": str(engine_path),
                  "enabled": payload.get("enabled", False), "read_roots": read_roots,
                  "allowed_privacy": allowed, "write_roots": write_roots,
                  "write_enabled": payload.get("write_enabled", False)}
        temp = safe_path(ws, path.with_name(f"obsidian.{uuid.uuid4().hex}.tmp"), allow_missing=True)
        try:
            with temp.open("x", encoding="utf-8") as output:
                json.dump(config, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return config


def _packet(config: dict[str, Any] | None, generation: int = 0, warnings: list[str] | None = None,
            max_chars: int = 6500) -> dict[str, Any]:
    return {"schema_version": PACKET_SCHEMA_VERSION, "packet_schema_version": PACKET_SCHEMA_VERSION,
            "policy_epoch": config["policy_epoch"] if config else 0, "index_generation": generation,
            "cards": [], "warnings": warnings or [], "expansion_pointers": [], "conflicts": [],
            "budget": {"unit": "characters", "scope": "excerpts", "limit": max_chars, "used": 0, "tokens": None}}


def _budget(limit: int, max_chars: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("invalid-result-limit")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 100_000:
        raise ValueError("invalid-character-budget")


def _snapshot(workspace: Path) -> tuple[dict[str, Any] | None, Any, sqlite3.Connection | None, int, list[str]]:
    try:
        config = load_config(workspace)
    except ValueError:
        return None, None, None, 0, ["obsidian-invalid-config"]
    if config is None or not config["enabled"]:
        return config, None, None, 0, ["obsidian-disabled"]
    try:
        engine = _engine(config)
    except (OSError, ValueError):
        return config, None, None, 0, ["obsidian-engine-unavailable"]
    db, generation, warnings = index.refresh(workspace, config, engine)
    return config, engine, db, generation, warnings


def _notes(db: sqlite3.Connection, config: dict[str, Any], engine: Any) -> tuple[list[Any], dict[str, sqlite3.Row], list[str]]:
    rows, warnings = index.live_rows(db, config)
    notes = []
    mapping: dict[str, sqlite3.Row] = {}
    for row in rows:
        try:
            path = safe_path(config["vault"], row["path"])
        except ValueError:
            warnings.append("unsafe-note-path")
            continue
        metadata = json.loads(row["metadata"])
        notes.append(engine.Note(path, row["path"], row["title"], row["body"], metadata))
        mapping[row["path"]] = row
    return notes, mapping, warnings


def _date_value(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _current(notes: list[Any], as_of: str | None, engine: Any) -> list[Any]:
    when = _date_value(as_of) or date.today()
    valid = [note for note in notes if (not note.metadata.get("valid_from") or _date_value(note.metadata["valid_from"]) <= when)
             and (not note.metadata.get("valid_until") or _date_value(note.metadata["valid_until"]) >= when)]
    replaced = {old for note in valid for old in engine.list_values(note.metadata, "supersedes")}
    return [note for note in valid if str(note.metadata["id"]) not in replaced]


def _source_ok(config: dict[str, Any], row: sqlite3.Row) -> bool:
    try:
        path = safe_path(config["vault"], row["path"])
        _, digest = index._read(path)
        return digest == row["digest"]
    except (OSError, UnicodeError, ValueError):
        return False


def _section_text(body: str, section: str | None) -> tuple[str | None, str | None]:
    if not section:
        return body, None
    lines = body.splitlines()
    target = section.lstrip("#").strip().casefold()
    for offset, line in enumerate(lines):
        if line.strip().casefold().endswith("^" + target):
            return line, section
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match or match.group(2).casefold() != target:
            continue
        level = len(match.group(1))
        end = next((idx for idx in range(offset + 1, len(lines))
                    if (next_match := re.match(r"^(#{1,6})\s+", lines[idx])) and len(next_match.group(1)) <= level), len(lines))
        return "\n".join(lines[offset:end]), match.group(2)
    return None, None


def _add_card(packet: dict[str, Any], row: sqlite3.Row, note: Any, text: str, section: str | None,
              config: dict[str, Any], engine: Any) -> bool:
    remaining = packet["budget"]["limit"] - packet["budget"]["used"]
    if remaining <= 0 or not _source_ok(config, row):
        packet["warnings"].append("source-changed-or-unavailable" if remaining > 0 else "character-budget-exhausted")
        return False
    excerpt = engine.redact_private_text(text, redact_long_tokens=False)[:remaining]
    if len(text) > remaining:
        packet["warnings"].append("excerpt-truncated")
    metadata = note.metadata
    projects = [value for value in engine.list_values(metadata, "projects") if len(value) <= 160][:8]
    all_sources = engine.list_values(metadata, "source_ids")
    sources = [value for value in all_sources if len(value) <= 256]
    scope = metadata.get("verification_scope")
    if isinstance(scope, list):
        scope = "; ".join(scope)
    if len(sources) > 16 or len(sources) != len(all_sources):
        packet["warnings"].append("source-pointers-truncated")
    card = {"id": row["note_id"], "source_namespace": "obsidian", "authority": "source_data",
            "revision": row["digest"], "section": section, "project": projects[0] if projects else None,
            "projects": projects, "privacy": metadata["privacy"], "kind": metadata["type"],
            "title": engine.redact_private_text(note.title, redact_long_tokens=False)[:200],
            "verification_scope": engine.redact_private_text(str(scope or ""), redact_long_tokens=False)[:500] or None,
            "verified_at": str(metadata["verified_at"])[:40] if metadata.get("verified_at") else None,
            "evidence_basis": str(metadata.get("evidence_strength", "unverified"))[:64],
            "valid_from": metadata.get("valid_from"), "valid_until": metadata.get("valid_until"),
            "supersedes": [value for value in engine.list_values(metadata, "supersedes") if len(value) <= 256][:16],
            "source_ids": sources[:16],
            "locator": {"vault_id": config["vault_id"], "relative_path": row["path"], "section": section},
            "excerpt": excerpt}
    packet["cards"].append(card)
    packet["budget"]["used"] += len(excerpt)
    packet["expansion_pointers"].append({"id": row["note_id"], "revision": row["digest"], "section": section})
    return True


def _finish(workspace: Path, config: dict[str, Any], db: sqlite3.Connection, packet: dict[str, Any]) -> bool:
    try:
        current = load_config(workspace)
    except ValueError:
        return False
    if current is None or not current["enabled"] or current["policy_epoch"] != config["policy_epoch"]:
        return False
    generation = int((db.execute("SELECT value FROM meta WHERE key='generation'").fetchone() or [0])[0])
    if generation != packet["index_generation"]:
        return False
    valid, _ = index.live_rows(db, current)
    valid_ids = {row["note_id"]: row["digest"] for row in valid}
    return all(valid_ids.get(card["id"]) == card["revision"] and _source_ok(current, next(row for row in valid if row["note_id"] == card["id"]))
               for card in packet["cards"])


def _bound_packet(packet: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Bound metadata as well as excerpts; JSON framing has a 2048-char floor."""
    cap = max(2048, max_chars)
    packet["budget"]["packet_limit"] = cap
    while len(json.dumps(packet, ensure_ascii=False)) > cap and packet["cards"]:
        packet["cards"].pop()
        packet["warnings"] = sorted(set(packet["warnings"] + ["packet-budget-exhausted"]))
        visible = {card["id"] for card in packet["cards"]}
        packet["expansion_pointers"] = [p for p in packet["expansion_pointers"] if p["id"] in visible]
        packet["conflicts"] = [p for p in packet["conflicts"] if p["id"] in visible]
    packet["budget"]["used"] = sum(len(card["excerpt"]) for card in packet["cards"])
    return packet


def _run(workspace: Path | str, max_chars: int, build: Any) -> dict[str, Any]:
    ws = _workspace(workspace)
    for _ in range(2):
        config, engine, db, generation, warnings = _snapshot(ws)
        packet = _packet(config, generation, warnings, max_chars)
        if db is None:
            return _bound_packet(packet, max_chars)
        try:
            build(packet, config, engine, db)
            if _finish(ws, config, db, packet):
                packet["warnings"] = sorted(set(packet["warnings"]))
                return _bound_packet(packet, max_chars)
        finally:
            db.close()
    try:
        latest = load_config(ws)
    except ValueError:
        latest = None
    return _bound_packet(_packet(latest, warnings=["policy-or-source-changed-during-query"],
                                 max_chars=max_chars), max_chars)


def context(workspace: Path | str, query: str, limit: int = 6, max_chars: int = 6500,
            project: str | None = None, as_of: str | None = None, expand_links: bool = False) -> dict[str, Any]:
    _budget(limit, max_chars)
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("query-required")
    if project is not None and (not isinstance(project, str) or not project.strip() or len(project) > 500):
        raise ValueError("invalid-project")
    if as_of:
        _date_value(as_of)

    def build(packet: dict[str, Any], config: dict[str, Any], engine: Any, db: sqlite3.Connection) -> None:
        notes, mapping, warnings = _notes(db, config, engine)
        packet["warnings"].extend(warnings)
        notes = _current(notes, as_of, engine)
        if project:
            matches = engine.exact_project_matches(notes, project)
            if len(matches) != 1:
                packet["warnings"].append("project-ambiguous" if matches else "project-not-found")
                return
            keys = engine.project_identifiers(matches[0])
            notes = [note for note in notes if note == matches[0] or keys.intersection(
                     engine.canonical_key(value) for value in engine.list_values(note.metadata, "projects"))]
        elif len((matches := engine.scoped_project_matches(notes, query))) == 1:
            keys = engine.project_identifiers(matches[0])
            notes = [note for note in notes if note == matches[0] or keys.intersection(
                     engine.canonical_key(value) for value in engine.list_values(note.metadata, "projects"))]
        exact: list[Any] = []
        q = query.strip().casefold()
        alias_id, _ = index.resolve_id(db, query.strip(), list(mapping.values()))
        for note in notes:
            row = mapping[note.relative_path]
            values = [row["note_id"], str(note.metadata["id"]), note.title, note.relative_path,
                      *engine.list_values(note.metadata, "aliases"), *engine.list_values(note.metadata, "source_ids")]
            if (row["note_id"] == alias_id or any(q == value.casefold() for value in values)
                    or re.search(r"(?<!\w)" + re.escape(query.strip()) + r"(?!\w)", note.body, re.IGNORECASE)):
                exact.append(note)
        ranked = [hit.note for hit in engine.rank_notes(notes, query, limit=max(50, limit * 4))]
        selected: list[Any] = []
        for note in exact + ranked:
            if note not in selected:
                selected.append(note)
        for note in selected[:limit]:
            row = mapping[note.relative_path]
            snippet = next((hit.snippet for hit in engine.rank_notes([note], query, 1)), note.body[:600])
            _add_card(packet, row, note, snippet[:900], None, config, engine)
        if not packet["cards"]:
            packet["warnings"].append("no-results")
        visible_by_id = {str(note.metadata["id"]): note for note in notes}
        for card in list(packet["cards"]):
            note = visible_by_id.get(card["id"].split(":", 2)[-1])
            if note is None:
                continue
            for target in engine.list_values(note.metadata, "contradicts"):
                if target in visible_by_id:
                    packet["conflicts"].append({"id": card["id"], "with": mapping[visible_by_id[target].relative_path]["note_id"]})
        if expand_links:
            original = list(packet["cards"])
            for card in original:
                note = visible_by_id.get(card["id"].split(":", 2)[-1])
                if note is None:
                    continue
                links = (engine.list_values(note.metadata, "source_ids") +
                         engine.list_values(note.metadata, "supersedes") +
                         engine.list_values(note.metadata, "contradicts"))
                for target in links:
                    linked = visible_by_id.get(target)
                    if linked and len(packet["cards"]) < limit + 2 and all(c["id"] != mapping[linked.relative_path]["note_id"] for c in packet["cards"]):
                        _add_card(packet, mapping[linked.relative_path], linked, linked.body[:500], None, config, engine)

    return _run(workspace, max_chars, build)


def project(workspace: Path | str, name: str, **budgets: Any) -> dict[str, Any]:
    return context(workspace, name, project=name, **budgets)


def read(workspace: Path | str, note_id: str, section: str | None = None,
         revision: str | None = None, max_chars: int = 6500) -> dict[str, Any]:
    _budget(1, max_chars)
    if not isinstance(note_id, str) or not note_id or len(note_id) > 1000:
        raise ValueError("note-id-required")
    if section is not None and (not isinstance(section, str) or not section.strip() or len(section) > 500):
        raise ValueError("invalid-section")

    def build(packet: dict[str, Any], config: dict[str, Any], engine: Any, db: sqlite3.Connection) -> None:
        notes, mapping, warnings = _notes(db, config, engine)
        packet["warnings"].extend(warnings)
        rows = [mapping[note.relative_path] for note in notes]
        canonical, error = index.resolve_id(db, note_id, rows)
        if error:
            packet["warnings"].append(error)
            return
        note = next(note for note in notes if mapping[note.relative_path]["note_id"] == canonical)
        row = mapping[note.relative_path]
        if revision is not None and revision != row["digest"]:
            packet["warnings"].append("stale-revision")
            return
        text, resolved_section = _section_text(note.body, section)
        if text is None:
            packet["warnings"].append("section-not-found")
            return
        _add_card(packet, row, note, text, resolved_section, config, engine)

    return _run(workspace, max_chars, build)


def forget(workspace: Path | str, note_id: str) -> dict[str, Any]:
    ws = _workspace(workspace)
    config = load_config(ws, required=True)
    return index.forget_index(ws, config, note_id, _engine(config))
