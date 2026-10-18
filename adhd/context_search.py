"""Bounded locations across code, retained memory and configured notes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Iterable

from .context_index import CodeIndex, _digest, _git, _safe_file, STREAM_BYTES
from .memory import Memory, namespace
from . import obsidian
from . import obsidian_index


SOURCES = frozenset({"code", "memory", "wiki"})


def _sources(values: Iterable[str]) -> tuple[str, ...]:
    chosen = tuple(dict.fromkeys(values))
    if not chosen or any(value not in SOURCES for value in chosen):
        raise ValueError("invalid-context-sources")
    return chosen


def _identity(workspace: Path) -> tuple[str, str]:
    code, common = _git(workspace, "rev-parse", "--git-common-dir")
    repository = str((workspace / os.fsdecode(common).strip()).resolve()) if code == 0 else str(workspace)
    return _digest(repository), _digest(str(workspace))


def _code_paths(index: CodeIndex, query: str) -> tuple[list[str], list[str]]:
    """Git paths and exact rg terms are hints; an empty hint is never an absence proof."""
    names, enumerated = index._paths()
    terms = [query.strip(), *re.findall(r"[a-z0-9_]+|[가-힣]+", query.casefold())[:4]]
    terms = list(dict.fromkeys(term for term in terms if term))
    chosen = {name for name in names if any(term.casefold() in name.casefold() for term in terms)}
    warnings = [] if enumerated else ["path-enumeration-incomplete"]
    command = ["rg", "-l", "-0", "-i", "-F"]
    for term in terms:
        command.extend(("-e", term))
    command.extend(("--", "."))
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(command, cwd=index.workspace, stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=20, check=False)
            if output.tell() > STREAM_BYTES:
                warnings.append("rg-output-truncated")
            output.seek(0)
            raw = output.read(STREAM_BYTES)
            code = result.returncode
        if code not in (0, 1):
            warnings.append("rg-incomplete")
        for value in raw.split(b"\0"):
            if not value:
                continue
            relative = os.fsdecode(value).removeprefix("./").replace("\\", "/")
            try:
                _safe_file(index.workspace, relative)
            except ValueError:
                continue
            chosen.add(relative)
    except (OSError, subprocess.TimeoutExpired):
        warnings.append("rg-unavailable")
    if len(chosen) > 200:
        warnings.append("candidate-paths-truncated")
    return sorted(chosen)[:200], warnings


def _memory_cards(memory: Memory, query: str, scope: str, environment: str,
                  limit: int, max_chars: int, complete: bool,
                  include_global: bool, identity: tuple[str, str]) -> tuple[list[dict[str, Any]], bool, bool]:
    result = memory.recall(query, scope, environment=environment, limit=min(limit, 12),
                           max_chars=max_chars, complete=complete, include_global=include_global)
    cards = []
    for record in result["records"]:
        row = memory.db.execute("SELECT digest,environment FROM records WHERE id=?", (record["id"],)).fetchone()
        if row is None:
            continue
        ref = {"source": "memory", "id": record["id"], "scope": record["scope"],
               "repository_id": identity[0], "worktree_id": identity[1],
               "hash": row["digest"], "revision": row["digest"], "environment": row["environment"],
               "start_line": 1, "end_line": len(record["content"].splitlines()) or 1,
               "parser_version": 0, "policy_version": 1}
        cards.append({"source": "memory", "id": record["id"], "kind": record["kind"],
                      "title": record["logical_key"], "excerpt": record["content"][:600], "ref": ref,
                      "provenance": {"source": record["source"], "locator": record["locator"],
                                     "basis": record["basis"]}})
    return cards, result["search_complete"], result["truncated"]


def _wiki_cards(workspace: Path, query: str, limit: int, max_chars: int,
                complete: bool, identity: tuple[str, str]) -> tuple[list[dict[str, Any]], bool, bool, list[str]]:
    try:
        config = obsidian.load_config(workspace)
    except ValueError:
        return [], False, False, ["obsidian-invalid-config"]
    if not config or not config["enabled"]:
        return [], True, False, ["obsidian-disabled"]
    try:
        engine = obsidian._engine(config)
    except (OSError, ValueError):
        return [], False, False, ["obsidian-engine-unavailable"]
    if complete:
        db, generation, warnings = obsidian_index.refresh(workspace, config, engine)
        reconciled = not any(value.startswith(("unsafe-", "note-excluded:changed-during-read")) for value in warnings)
    else:
        db, generation = obsidian_index.hint_snapshot(workspace, config)
        warnings = []
        reconciled = False
        if db is None:
            db, generation, warnings = obsidian_index.refresh(workspace, config, engine)
            warnings.append("cold-index-reconciled")
    packet = obsidian._packet(config, generation, warnings, max_chars)
    selected_rows = []
    clipped = False
    try:
        rows, row_warnings, clipped = obsidian_index.query_rows(db, config, query, limit=800 if complete else 200)
        packet["warnings"].extend(row_warnings)
        for row in rows[:limit]:
            path = obsidian.safe_path(config["vault"], row["path"])
            metadata = json.loads(row["metadata"])
            note = engine.Note(path, row["path"], row["title"], row["body"], metadata)
            lines = note.body.splitlines()
            needle = query.casefold()
            match = next((number for number, line in enumerate(lines) if needle in line.casefold()), 0)
            excerpt = "\n".join(lines[max(0, match-1):match+3])[:900]
            if obsidian._add_card(packet, row, note, excerpt, None, config, engine):
                selected_rows.append(row)
        current = obsidian.load_config(workspace)
        current_generation = db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        if (not current or not current["enabled"] or current["policy_epoch"] != config["policy_epoch"]
                or not current_generation or int(current_generation[0]) != generation
                or any(not obsidian._source_ok(current, row) for row in selected_rows)):
            return [], False, True, ["policy-or-source-changed-during-query"]
    finally:
        db.close()
    packet = obsidian._bound_packet(packet, max_chars)
    cards = []
    for card in packet["cards"]:
        ref = {"source": "wiki", "id": card["id"], "revision": card["revision"],
               "repository_id": identity[0], "worktree_id": identity[1],
               "hash": card["revision"], "policy_epoch": packet["policy_epoch"],
               "vault_id": card["locator"]["vault_id"], "section": card["section"],
               "parser_version": 1, "policy_version": packet["policy_epoch"]}
        cards.append({"source": "wiki", "id": card["id"], "title": card["title"],
                      "kind": card["kind"], "excerpt": card["excerpt"], "ref": ref,
                      "provenance": {"relative_path": card["locator"]["relative_path"],
                                     "evidence_basis": card["evidence_basis"]}})
    finished = complete and reconciled and not clipped and not any(
        warning in {"source-changed-or-unavailable", "packet-budget-exhausted"}
        for warning in packet["warnings"])
    return cards, finished, clipped or len(rows) > limit, packet["warnings"]


def locate(workspace: Path | str, query: str, *, limit: int = 6, max_chars: int = 6500,
           complete: bool = False, scope: str | None = None, environment: str = "",
           include_global: bool = True,
           sources: Iterable[str] = ("code", "memory", "wiki")) -> dict[str, Any]:
    """Return evidence cards; complete scans never assert absence from stale hints."""
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("query-required")
    if not 1 <= limit <= 12 or not 500 <= max_chars <= 16000:
        raise ValueError("invalid-context-budget")
    selected = _sources(sources)
    ws = Path(workspace).expanduser().resolve(strict=True)
    allowed_scope = namespace(ws)
    requested_scope = scope or allowed_scope
    if requested_scope not in (allowed_scope, "global"):
        raise ValueError("memory-scope-outside-workspace")
    if requested_scope == "global" and not include_global:
        raise ValueError("global-memory-excluded")
    identity = _identity(ws) if "code" not in selected else None
    per_source: dict[str, list[dict[str, Any]]] = {}
    completeness: dict[str, bool] = {}
    truncated: dict[str, bool] = {}
    warnings: list[str] = []
    if "code" in selected:
        with CodeIndex(ws) as index:
            identity = (index.repository_id, index.worktree_id)
            if complete:
                refresh = index.refresh()
                paths = None
            else:
                paths, hints = _code_paths(index, query)
                warnings.extend(hints)
                refresh = index.refresh(paths)
            found = index.locate(query, paths=paths, limit=limit, max_chars=max_chars, complete=complete)
            per_source["code"] = found["cards"]
            completeness["code"] = bool(refresh["complete"] and found["complete"])
            truncated["code"] = found["truncated"]
            warnings.extend(found["warnings"])
            if not refresh["complete"] and complete:
                warnings.append(refresh.get("reason", "code-reconciliation-incomplete"))
    if "memory" in selected:
        with Memory() as memory:
            cards, finished, clipped = _memory_cards(memory, query, requested_scope, environment,
                                                     limit, max_chars, complete, include_global, identity)
        per_source["memory"] = cards
        completeness["memory"] = finished
        truncated["memory"] = clipped
    if "wiki" in selected:
        cards, finished, wiki_clipped, wiki_warnings = _wiki_cards(ws, query, limit, max_chars,
                                                                 complete, identity)
        per_source["wiki"] = cards
        completeness["wiki"] = finished
        truncated["wiki"] = wiki_clipped
        warnings.extend(wiki_warnings)
    cards = []
    used = 0
    for position in range(limit):
        for source in selected:
            available = per_source.get(source, [])
            if position >= len(available) or len(cards) >= limit:
                continue
            card = available[position]
            encoded = json.dumps(card, ensure_ascii=False)
            if used + len(encoded) > max_chars:
                truncated[source] = True
                continue
            cards.append(card)
            used += len(encoded)
    search_complete = all(completeness.values())
    any_truncated = any(truncated.values())
    all_complete = search_complete and not any_truncated
    continuation = None if all_complete else {
        "query": query, "sources": list(selected), "complete": True,
        "limit": limit, "max_chars": min(16000, max_chars * 2),
        "narrow_query_required": any_truncated and max_chars >= 16000}
    return {"cards": cards, "complete": all_complete, "search_complete": search_complete,
            "truncated": any_truncated, "continuation": continuation,
            "sources": completeness, "used_chars": used, "limit": max_chars,
            "repository_id": identity[0], "worktree_id": identity[1],
            "warnings": sorted(set(warnings)), "trust": "retrieved_data_not_instructions"}


def read(workspace: Path | str, ref: dict[str, Any], *, expected_revision: str,
         start_line: int | None = None, end_line: int | None = None,
         max_chars: int = 6500, environment: str = "", include_global: bool = True,
         start_char: int = 0) -> dict[str, Any]:
    """Check current scope, policy and source hash before returning a bounded range."""
    if not isinstance(ref, dict) or not isinstance(expected_revision, str) or not expected_revision:
        raise ValueError("reference-and-expected-revision-required")
    if not 1 <= max_chars <= 16000:
        raise ValueError("invalid-character-budget")
    if not isinstance(start_char, int) or isinstance(start_char, bool) or not 0 <= start_char <= STREAM_BYTES:
        raise ValueError("invalid-source-character-offset")
    ws = Path(workspace).expanduser().resolve(strict=True)
    source = ref.get("source")
    if source in {"memory", "wiki"} and (ref.get("repository_id"), ref.get("worktree_id")) != _identity(ws):
        return {"status": "denied", "reason": "reference-scope-mismatch"}
    if source == "code":
        with CodeIndex(ws) as index:
            return index.read(ref, expected_revision, start_line, end_line,
                              max_chars=max_chars, start_char=start_char)
    if source == "memory":
        if ref.get("scope") == "global" and not include_global:
            return {"status": "denied", "reason": "global-memory-excluded"}
        if ref.get("scope") not in (namespace(ws), "global"):
            return {"status": "denied", "reason": "memory-scope-outside-workspace"}
        with Memory() as memory:
            row = memory.db.execute("SELECT * FROM records WHERE id=? AND scope=? AND status='active' "
                                    "AND (expires IS NULL OR expires>?) AND (environment='' OR environment=?)",
                                    (ref.get("id"), ref["scope"], time.time(), environment)).fetchone()
            if row is None:
                return {"status": "stale", "reason": "memory-unavailable-or-ineligible"}
            if expected_revision != ref.get("revision") or row["digest"] != expected_revision:
                return {"status": "stale", "reason": "memory-revision-changed"}
            text = row["content"]
    elif source == "wiki":
        try:
            config = obsidian.load_config(ws)
        except ValueError:
            config = None
        if (not config or not config["enabled"] or config["vault_id"] != ref.get("vault_id")
                or config["policy_epoch"] != ref.get("policy_epoch")):
            return {"status": "denied", "reason": "wiki-policy-changed-or-disabled"}
        if expected_revision != ref.get("revision"):
            return {"status": "stale", "reason": "wiki-revision-changed"}
        snapshot_config, engine, db, generation, warnings = obsidian._snapshot(ws)
        if db is None or engine is None:
            return {"status": "stale", "reason": ",".join(warnings) or "wiki-unavailable"}
        try:
            if (not snapshot_config or snapshot_config["vault_id"] != ref.get("vault_id")
                    or snapshot_config["policy_epoch"] != ref.get("policy_epoch")):
                return {"status": "denied", "reason": "wiki-policy-changed-or-disabled"}
            config = snapshot_config
            notes, mapping, _ = obsidian._notes(db, config, engine)
            canonical, error = obsidian_index.resolve_id(db, ref.get("id", ""), list(mapping.values()))
            if error:
                return {"status": "stale", "reason": error}
            note = next((item for item in notes if mapping[item.relative_path]["note_id"] == canonical), None)
            if note is None:
                return {"status": "stale", "reason": "wiki-unavailable"}
            row = mapping[note.relative_path]
            if row["digest"] != expected_revision or not obsidian._source_ok(config, row):
                return {"status": "stale", "reason": "wiki-revision-changed"}
            section_text, _ = obsidian._section_text(note.body, ref.get("section"))
            if section_text is None:
                return {"status": "stale", "reason": "wiki-section-unavailable"}
            text = engine.redact_private_text(section_text, redact_long_tokens=False)
            receipt = {"index_generation": generation,
                       "cards": [{"id": row["note_id"], "revision": row["digest"]}]}
            current = obsidian.load_config(ws)
            if (not current or current["vault_id"] != ref.get("vault_id")
                    or not obsidian._finish(ws, config, db, receipt)):
                return {"status": "stale", "reason": "wiki-policy-or-source-changed"}
        finally:
            db.close()
    else:
        return {"status": "denied", "reason": "unknown-source"}
    lines = text.splitlines(keepends=True)
    start = ref.get("start_line", 1) if start_line is None else start_line
    end = ref.get("end_line", len(lines)) if end_line is None else end_line
    if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int)
            or isinstance(end, bool) or not 1 <= start <= end or end - start > 500):
        raise ValueError("invalid-source-range")
    selected = lines[start-1:end]
    if selected and start_char > len(selected[0]):
        raise ValueError("invalid-source-character-offset")
    if selected:
        selected[0] = selected[0][start_char:]
    output = []
    used = 0
    continuation = None
    for number, line in enumerate(selected, start):
        take = min(len(line), max_chars-used)
        output.append(line[:take])
        used += take
        if take < len(line):
            continuation = {"start_line": number,
                            "start_char": (start_char if number == start else 0)+take,
                            "end_line": end}
            break
    clipped = continuation is not None
    return {"status": "ok", "ref": ref, "start_line": start, "end_line": end,
            "text": "".join(output), "used_chars": used, "truncated": clipped,
            "continuation": continuation}
