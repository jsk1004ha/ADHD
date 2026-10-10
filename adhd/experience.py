"""Selective, permission-scoped experience records.

Events are structured observations, not instructions or proof of success.  The
local ledger is the only place candidate() writes; apply_candidate() is the
separate, configured vault-write boundary.  This module makes no model calls.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import date
from typing import Any

from filelock import FileLock

from .core import file_hash


_KINDS = {"feedback", "decision", "success", "failure"}
_PHASES = {"capture", "retrieval", "execution", "child", "verification",
           "candidate", "curation", "retry", "procedure", "other"}
_POLARITIES = {"positive", "negative", "unknown", "mixed"}
_DIMENSIONS = {"accuracy", "requirements", "usability", "expression",
               "efficiency", "autonomy", "unknown"}
_MAX_EVENT = 32_000
_MAX_NOTE = 128_000


def _hash(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded(value: Any, label: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise ValueError(f"{label} must be text of at most {limit} characters")
    return value.strip()


def _reject_links(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise ValueError("Paths cannot cross links or junctions")


def _root(path: Path | str) -> Path:
    raw = Path(path).expanduser().absolute()
    _reject_links(raw)
    return raw.resolve()


def _child(root: Path, relative: str, *, allow_absolute: bool = False) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("Invalid path")
    path = Path(relative.replace("\\", "/"))
    if path.is_absolute():
        if not allow_absolute:
            raise ValueError("Path must be relative to its root")
        lexical = path.absolute()
    else:
        if re.match(r"^[A-Za-z]:", relative) or any(p in {"..", ""} for p in path.parts):
            raise ValueError("Path leaves its root")
        lexical = root / path
    _reject_links(lexical)
    resolved = lexical.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("Path leaves its root")
    return resolved


def _atomic(path: Path, data: bytes, *, create: bool = False) -> None:
    """Replace a checked file atomically; exclusive link prevents create races."""
    fd, name = tempfile.mkstemp(prefix=".adhd-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if create:
            os.link(temp, path)  # Fails if another writer created the target.
        else:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _ledger_dir(workspace: Path) -> Path:
    root = _root(workspace)
    directory = _child(root, ".adhd/experience")
    directory.mkdir(parents=True, exist_ok=True)
    _reject_links(directory)
    return directory


def _load(directory: Path) -> dict:
    path = directory / "ledger.json"
    if not path.exists():
        return {"schema_version": 1, "traces": {}, "candidates": {}, "usage": {}}
    if path.stat().st_size > 16_000_000:
        raise ValueError("Experience ledger exceeds its bound")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Invalid experience ledger")
    for key in ("traces", "candidates", "usage"):
        if not isinstance(value.get(key), dict):
            raise ValueError("Invalid experience ledger")
    return value


def _save(directory: Path, value: dict) -> None:
    _atomic(directory / "ledger.json", (json.dumps(value, ensure_ascii=False,
            sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _load_config(workspace: Path) -> Any:
    # Import lazily so ordinary ADHD use has no Obsidian dependency or side effect.
    try:
        from .obsidian import load_config
    except ModuleNotFoundError as exc:
        if exc.name == "adhd.obsidian":
            return None
        raise
    return load_config(workspace)


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def _config(workspace: Path, *, for_write: bool = False) -> dict | None:
    raw = _mapping(_load_config(workspace))
    if not raw or raw.get("enabled") is not True:
        return None
    vault_data = _mapping(raw.get("vault"))
    vault_id = raw.get("vault_id", vault_data.get("id"))
    vault_path = raw.get("vault") if isinstance(raw.get("vault"), str) else None
    vault_path = vault_path or raw.get("vault_root", raw.get("vault_path", vault_data.get("root")))
    if not isinstance(vault_id, str) or not vault_id.strip() or not vault_path:
        raise ValueError("Enabled Obsidian config needs vault_id and vault_root")
    vault = _root(vault_path)
    if not vault.is_dir():
        raise ValueError("Configured vault is unavailable")
    roots = raw.get("write_roots", [])
    if not isinstance(roots, list) or any(not isinstance(item, str) for item in roots):
        raise ValueError("Invalid write_roots")
    grants = [_child(vault, item, allow_absolute=True) for item in roots]
    if for_write and (raw.get("write_enabled") is not True or not grants):
        raise ValueError("Vault writes are not enabled or granted")
    return {"vault_id": vault_id, "vault": vault, "write_enabled": raw.get("write_enabled") is True,
            "write_roots": grants}


def _grant(config: dict, target: Path) -> Path:
    for root in config["write_roots"]:
        if target.is_relative_to(root) and (root.is_dir() or not root.exists()):
            return root
    raise ValueError("Target is outside granted write_roots")


def _frontmatter_id(text: str) -> str:
    """Guard only the target's identity/policy; retrieval owns general parsing."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("Target has no valid frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("Unclosed target frontmatter") from exc
    fields: dict[str, str] = {}
    sequence = False
    for line in lines[1:end]:
        match = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
        if match:
            key, value = match.groups()
            if key in fields:
                raise ValueError("Duplicate target metadata key")
            if value.startswith('"'):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError) as exc:
                    raise ValueError("Invalid target metadata") from exc
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if not isinstance(value, str):
                raise ValueError("Invalid target metadata")
            fields[key] = value
            sequence = key in {"tags", "source_ids", "topics", "projects", "seed_records"} and not value
        elif sequence and re.fullmatch(r"  - .+", line):
            continue
        else:
            raise ValueError("Invalid target metadata")
    if (not fields.get("id") or fields.get("type") != "artifact"
            or fields.get("layer") != "record" or fields.get("privacy") not in {"private", "public"}
            or fields.get("status") in {None, "excluded", "deleted", "forgotten"}):
        raise ValueError("Target metadata/policy is invalid")
    return fields["id"]


def _feedback(items: Any, revision: str, task_id: str, project: str) -> list[dict]:
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 8:
        raise ValueError("feedback must be a bounded list")
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get("polarity") not in _POLARITIES:
            raise ValueError("Invalid explicit feedback polarity")
        dimension = item.get("dimension", "unknown")
        scope = item.get("scope", "task")
        if dimension not in _DIMENSIONS or scope not in {"task", "project"}:
            raise ValueError("Invalid feedback dimension or scope")
        if scope == "project" and not project:
            raise ValueError("Project-scoped feedback needs a project")
        result.append({"polarity": item["polarity"], "dimension": dimension,
                       "scope": scope, "scope_id": project if scope == "project" else task_id,
                       "quote": _bounded(item.get("quote"), "feedback quote", 1000, required=True),
                       "source": _bounded(item.get("source"), "feedback source", 240, required=True),
                       "target_revision": _bounded(item.get("target_revision", revision),
                                                   "feedback target revision", 160, required=True)})
    return result


def _short_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 6:
        raise ValueError(f"{label} must be a bounded list")
    return [_bounded(item, label, 400, required=True) for item in value]


def audit_artifact(reference: dict) -> dict:
    """Check a local canonical pointer; user-supplied success flags are ignored."""
    if not isinstance(reference, dict) or not reference.get("workspace"):
        raise ValueError("Artifact reference needs a workspace")
    workspace = _root(reference["workspace"])
    kind = reference.get("kind")
    if kind == "receipt":
        try:
            from .evidence import validate_execution
            path = _child(workspace, reference["path"])
            relative = path.relative_to(workspace).as_posix()
            if not re.fullmatch(r"\.adhd/checks/[0-9a-f]{32}/receipt\.json", relative):
                raise ValueError("Receipt path is not a check receipt")
            args = {"run_id": reference["run_id"], "revision": reference["contract_revision"]}
            try:
                receipt = validate_execution(workspace, relative, **args)
                status = "passed"
            except ValueError:
                receipt = validate_execution(workspace, relative, expect_failure=True, **args)
                status = "failed"
            return {"status": status, "kind": kind, "path": relative,
                    "sha256": file_hash(path), "exit_code": receipt["result"]["exit_code"],
                    "verified_at": receipt["result"]["finished_at"],
                    "run_id": reference["run_id"],
                    "contract_revision": reference["contract_revision"]}
        except (KeyError, ValueError, OSError, TypeError) as exc:
            return {"status": "unverified", "kind": kind, "reason": str(exc)}
    if kind == "document":
        try:
            path = _child(workspace, reference["path"])
            expected = reference["sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", expected) or not path.is_file():
                raise ValueError("Missing document or SHA-256")
            if not isinstance(reference.get("requirements"), list) or not reference["requirements"]:
                raise ValueError("Document pointer needs requirement IDs")
            actual = file_hash(path)
            if actual != expected:
                raise ValueError("Document hash changed")
            return {"status": "observed", "kind": kind,
                    "path": path.relative_to(workspace).as_posix(), "sha256": actual,
                    "requirements": reference.get("requirements", [])}
        except (KeyError, ValueError, OSError, TypeError) as exc:
            return {"status": "unverified", "kind": kind, "reason": str(exc)}
    if kind == "code":
        try:
            repo = _child(workspace, reference.get("repository", ".")) if reference.get("repository", ".") != "." else workspace
            commit = reference["commit"]
            if not re.fullmatch(r"[0-9a-f]{40}", commit) or not (repo / ".git").exists():
                raise ValueError("Code reference needs a reachable full commit")
            if not reference.get("conditions") or not reference.get("check"):
                raise ValueError("Code pointer needs conditions and a check")
            relative = reference["path"]
            _child(repo, relative)  # Reject traversal and linked working-tree aliases.
            if ":" in relative:
                raise ValueError("Invalid Git path")
            obj = subprocess.run(["git", "-C", str(repo), "cat-file", "-t", commit],
                                 capture_output=True, timeout=10)
            if obj.returncode or obj.stdout.strip() != b"commit":
                raise ValueError("Commit is unreachable")
            git_path = relative.replace("\\", "/")
            data = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{git_path}"],
                                  capture_output=True, timeout=10)
            if data.returncode or len(data.stdout) > 8_000_000:
                raise ValueError("Committed file is unavailable or oversized")
            actual = _hash(data.stdout)
            if reference.get("sha256") and reference["sha256"] != actual:
                raise ValueError("Committed file hash changed")
            return {"status": "observed", "kind": kind, "repository": str(repo),
                    "commit": commit, "path": relative, "sha256": actual,
                    "conditions": reference.get("conditions", ""), "check": reference.get("check", "")}
        except (KeyError, ValueError, OSError, TypeError, subprocess.TimeoutExpired) as exc:
            return {"status": "unverified", "kind": kind, "reason": str(exc)}
    raise ValueError("Unknown artifact kind")


def _verification(workspace: Path, event: dict) -> tuple[str, list[dict]]:
    refs = event.get("receipts", [])
    if not isinstance(refs, list) or len(refs) > 8:
        raise ValueError("receipts must be a bounded list")
    checked = [audit_artifact({**ref, "workspace": str(workspace)}) for ref in refs]
    if any(row["status"] == "failed" for row in checked):
        return "failed", checked
    if checked and all(row["status"] == "passed" for row in checked):
        return "passed", checked
    return "unverified", checked


def _note(candidate: dict, today: str) -> str:
    event = candidate["record"]
    def line(value: str) -> str:
        return value.replace("\r", " ").replace("\n", " ").strip()
    feedback = event["feedback"]
    feedback_lines = [f"- {row['polarity']} / {row['dimension']} / {row['scope']}:{line(row['scope_id'])}: "
                      f"{line(row['quote'])} (source: {line(row['source'])}; revision: {line(row['target_revision'])})"
                      for row in feedback]
    def list_field(key: str, values: list[str]) -> list[str]:
        return ([key + ":"] + ["  - " + json.dumps(value, ensure_ascii=False) for value in values]
                if values else [key + ": []"])
    verified_at = next((row["verified_at"][:10] for row in event["receipts"]
                        if row["status"] == "passed"), "") if event["technical_status"] == "passed" else ""
    strength = "direct" if verified_at else "unverified"
    metadata = ["---", "type: artifact", "domain: artifact", "layer: record", "privacy: private",
                "status: inbox", f"id: {json.dumps(candidate['target']['note_id'], ensure_ascii=False)}",
                f"artifact_kind: {'decision' if event['kind'] == 'decision' else 'run_report'}",
                f"created: {json.dumps(today)}", f"updated: {json.dumps(today)}",
                *list_field("source_ids", event["source_ids"] or ["task:" + event["task_id"]]), "topics: []",
                *list_field("projects", [event["project"]] if event["project"] else []),
                'source_root: ""', "source_kind: file", 'source_uri: ""',
                f"verified_at: {json.dumps(verified_at)}",
                f"verification_scope: {json.dumps(event['verification_scope'], ensure_ascii=False)}",
                f"evidence_strength: {strength}", "tags:", "  - record", "  - artifact", "---"]
    headings = [f"# {line(event['title'])}", "", "## 상황과 적용 범위",
                line(event["conditions"] or event["project"] or event["task_id"]), "",
                "## 관찰과 결과", event["summary"],
                *(["선택 이유: " + event["decision_reason"]] if event["decision_reason"] else []),
                *("대안: " + value for value in event["alternatives"]),
                *(["재검토 조건: " + event["revisit_when"]] if event["revisit_when"] else []),
                *("실패한 가설: " + value for value in event["failed_hypotheses"]),
                "", "## 사용자 평가",
                *(feedback_lines or ["미수집"]), "", "## 검증과 한계",
                f"기술 검증: {event['technical_status']} / {line(event['verification_scope']) or '범위 미확인'}",
                *(f"- {row['kind']}: {row['status']} ({row.get('path', row.get('reason', ''))})"
                  for row in event["receipts"]),
                *(f"- 정본 {row['kind']}: {row['status']} ({row.get('path', row.get('reason', ''))})"
                  for row in event["artifacts"]), ""]
    headings.extend("- 미해결: " + value for value in event["unresolved"])
    if event["next_action"]:
        headings += ["## 다음 행동", event["next_action"], ""]
    headings += ["## 근거", f"task: {event['task_id']} / revision: {event['artifact_revision']}",
                 f"content_sha256: {candidate['content_hash']}",
                 f"<!-- adhd-candidate:{candidate['candidate_id']} -->", ""]
    return "\n".join(metadata + headings)


def _record(workspace: Path, event: dict) -> dict:
    task_id = _bounded(event.get("task_id"), "task_id", 160, required=True)
    revision = _bounded(event.get("artifact_revision"), "artifact_revision", 160, required=True)
    kind = event.get("kind")
    if kind not in _KINDS:
        raise ValueError("Unsupported experience event kind")
    project = _bounded(event.get("project", ""), "project", 160)
    feedback = _feedback(event.get("feedback"), revision, task_id, project)
    artifacts = event.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 8:
        raise ValueError("artifacts must be a bounded list")
    checked_artifacts = [audit_artifact({**ref, "workspace": str(workspace)}) for ref in artifacts]
    technical, receipts = _verification(workspace, event)
    if technical == "passed" and any(row["status"] != "observed" for row in checked_artifacts):
        technical = "unverified"
    source_ids = event.get("source_ids", [])
    if not isinstance(source_ids, list) or len(source_ids) > 16 or any(
            not isinstance(item, str) or not item or len(item) > 160 for item in source_ids):
        raise ValueError("source_ids must be bounded explicit IDs")
    return {"task_id": task_id, "artifact_revision": revision, "kind": kind,
            "project": project, "title": _bounded(event.get("title", ""), "title", 160) or
            f"{kind}: {task_id}",
            "summary": _bounded(event.get("summary", ""), "summary", 2400),
            "conditions": _bounded(event.get("conditions", ""), "conditions", 600),
            "next_action": _bounded(event.get("next_action", ""), "next_action", 400),
            "decision_reason": _bounded(event.get("decision_reason", ""), "decision_reason", 800),
            "alternatives": _short_list(event.get("alternatives"), "alternatives"),
            "revisit_when": _bounded(event.get("revisit_when", ""), "revisit_when", 400),
            "failed_hypotheses": _short_list(event.get("failed_hypotheses"), "failed_hypotheses"),
            "unresolved": _short_list(event.get("unresolved"), "unresolved"),
            "verification_scope": _bounded(event.get("verification_scope", ""),
                                            "verification_scope", 400),
            "feedback": feedback, "technical_status": technical, "receipts": receipts,
            "artifacts": checked_artifacts, "source_ids": source_ids,
            "failure_key": _bounded(event.get("failure_key", ""), "failure_key", 160) or
                           (_hash(str(event.get("summary", "")).strip().casefold())[:20]
                            if kind == "failure" and str(event.get("summary", "")).strip() else ""),
            "consequential": event.get("consequential") is True,
            "reusable": event.get("reusable") is True,
            "material": event.get("material") is True}


def candidate(workspace: Path | str, event: dict) -> dict:
    """Record one event and return a selected, bounded review candidate if useful.

    Required: task_id, artifact_revision, kind.  User feedback is a list of
    explicit quote/source/polarity entries.  Receipts and artifacts are pointers,
    never copied blobs.  Reusing a task/revision/kind with different content is
    an error; independent tasks remain distinct even if content hashes match.
    """
    workspace = _root(workspace)
    if not isinstance(event, dict) or len(_stable(event).encode("utf-8")) > _MAX_EVENT:
        raise ValueError("Experience event must be a bounded object")
    config = _config(workspace)
    if config is None:
        return {"status": "disabled", "candidate_id": None}
    record = _record(workspace, event)
    content_hash = _hash(_stable({
        "record": {key: value for key, value in record.items()
                   if key not in {"task_id", "artifact_revision"}},
        "target": event.get("target", {})}))
    key = _hash(_stable([record["task_id"], record["artifact_revision"], record["kind"]]))
    directory = _ledger_dir(workspace)
    with FileLock(str(directory / "ledger.lock"), timeout=10):
        state = _load(directory)
        old = state["traces"].get(key)
        if old:
            if old["content_hash"] != content_hash:
                raise ValueError("Conflicting experience event for the same idempotency key")
            return {"status": old["selection"], "candidate_id": old.get("candidate_id"),
                    "idempotency_key": key, "duplicate": True}
        recurring = bool(record["failure_key"] and any(
            row.get("failure_key") == record["failure_key"] and row.get("task_id") != record["task_id"]
            for row in state["traces"].values()))
        selected = (record["kind"] == "feedback" and bool(record["feedback"])) or (
            record["kind"] == "decision" and record["consequential"] and
            bool(record["summary"]) and bool(record["decision_reason"])) or (
            record["kind"] == "success" and record["reusable"] and
            record["technical_status"] == "passed" and bool(record["artifacts"])) or (
            record["kind"] == "failure" and bool(record["summary"]) and
            (record["material"] or recurring))
        selection = "selected" if selected else "skipped"
        trace = {"task_id": record["task_id"], "artifact_revision": record["artifact_revision"],
                 "kind": record["kind"], "content_hash": content_hash,
                 "failure_key": record["failure_key"], "selection": selection,
                 "technical_status": record["technical_status"]}
        result = {"status": selection, "candidate_id": None, "idempotency_key": key,
                  "duplicate": False}
        if selected:
            candidate_id = _hash("candidate:" + key)[:32]
            target_spec = event.get("target", {})
            if not isinstance(target_spec, dict):
                raise ValueError("target must be an object")
            target_rel = target_spec.get("path", f"10 기록/아티팩트/ADHD-{candidate_id}.md")
            target = _child(config["vault"], target_rel)
            note_id = target_spec.get("note_id", f"artifact:adhd:{candidate_id}")
            note_id = _bounded(note_id, "target note_id", 160, required=True)
            if any(c in note_id for c in "\r\n"):
                raise ValueError("Invalid target note_id")
            if target.exists():
                if not target.is_file() or target.stat().st_size > _MAX_NOTE:
                    raise ValueError("Target is unavailable or oversized")
                old_text = target.read_text(encoding="utf-8-sig")
                if _frontmatter_id(old_text) != note_id:
                    raise ValueError("Target canonical ID differs")
                expected = file_hash(target)
            else:
                old_text = ""
                expected = None
            if target_spec.get("expected_revision", expected) != expected:
                raise ValueError("Target expected revision differs")
            candidate_data = {"candidate_id": candidate_id, "idempotency_key": key,
                              "content_hash": content_hash, "record": record,
                              "status": "pending", "target": {"vault_id": config["vault_id"],
                              "note_id": note_id, "path": target.relative_to(config["vault"]).as_posix(),
                              "expected_revision": expected,
                              "section": _bounded(target_spec.get("section", ""), "target section", 160)}}
            today = date.today().isoformat()
            note = _note(candidate_data, today)
            if expected is not None:
                note = "\n\n" + note.split("---\n", 2)[-1]
                # Existing RECORDs keep their metadata and content.  Add one
                # self-identifying section, guarded by the entire source hash.
                note = "\n\n## ADHD experience " + candidate_id + "\n" + note.lstrip()
            new_text = old_text + note if expected is not None else note
            if len(new_text.encode("utf-8")) > _MAX_NOTE:
                raise ValueError("Candidate note exceeds its bound")
            if expected is None and _frontmatter_id(new_text) != note_id:
                raise ValueError("Generated note identity is invalid")
            diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                                fromfile="expected", tofile="candidate"))
            candidate_data.update({"diff": diff, "new_sha256": _hash(new_text),
                                   "consent_scope": {
                                       "vault_id": config["vault_id"],
                                       "target": candidate_data["target"]["path"],
                                       "operation": "update" if expected else "create",
                                       "diff_sha256": _hash(diff)}})
            if expected is None:
                candidate_data["new_text"] = new_text
            else:
                candidate_data["addition"] = note
            state["candidates"][candidate_id] = candidate_data
            trace["candidate_id"] = candidate_id
            result["candidate_id"] = candidate_id
            result["target"] = candidate_data["target"]
            result["diff"] = diff
        state["traces"][key] = trace
        _save(directory, state)
        return result


def list_candidates(workspace: Path | str) -> list[dict]:
    directory = _ledger_dir(_root(workspace))
    with FileLock(str(directory / "ledger.lock"), timeout=10):
        state = _load(directory)
    return [{key: value for key, value in item.items() if key not in {"new_text", "addition", "record"}}
            for item in state["candidates"].values()]


def apply_candidate(workspace: Path | str, candidate_id: str) -> dict:
    """Apply one exact diff within current configured grant and expected hash."""
    workspace = _root(workspace)
    if not isinstance(candidate_id, str) or not re.fullmatch(r"[0-9a-f]{32}", candidate_id):
        raise ValueError("Invalid candidate ID")
    config = _config(workspace, for_write=True)
    if config is None:
        raise ValueError("Obsidian integration is disabled")
    directory = _ledger_dir(workspace)
    with FileLock(str(directory / "ledger.lock"), timeout=10):
        state = _load(directory)
        item = state["candidates"].get(candidate_id)
        if not item:
            raise ValueError("Unknown candidate")
        target_info = item["target"]
        if config["vault_id"] != target_info["vault_id"]:
            raise ValueError("Configured vault identity changed")
        target = _child(config["vault"], target_info["path"])
        grant = _grant(config, target)
        if item["consent_scope"] != {"vault_id": config["vault_id"],
                                      "target": target_info["path"],
                                      "operation": "update" if target_info["expected_revision"] else "create",
                                      "diff_sha256": _hash(item["diff"])}:
            raise ValueError("Candidate consent scope changed")
        if target_info["expected_revision"] is None and _hash(item["new_text"]) != item["new_sha256"]:
            raise ValueError("Candidate bytes changed")
        if not grant.is_dir():
            raise ValueError("Granted write root is unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        _reject_links(target)
        with FileLock(str(target.parent / ".adhd-experience.lock"), timeout=10):
            if target.exists():
                if not target.is_file() or target.stat().st_size > _MAX_NOTE:
                    raise ValueError("Target is unavailable or oversized")
                actual = file_hash(target)
                if actual == item["new_sha256"]:
                    item["status"] = "applied"
                    _save(directory, state)
                    return {"status": "already_applied", "candidate_id": candidate_id,
                            "revision": actual}
                current = target.read_text(encoding="utf-8-sig")
                if _frontmatter_id(current) != target_info["note_id"]:
                    raise ValueError("Target canonical ID changed")
                if item["status"] == "applying" and f"<!-- adhd-candidate:{candidate_id} -->" in current:
                    item["status"] = "applied_modified"
                    _save(directory, state)
                    return {"status": "applied_modified", "candidate_id": candidate_id,
                            "revision": actual}
                if actual != target_info["expected_revision"]:
                    return {"status": "revision_conflict", "candidate_id": candidate_id,
                            "revision": actual}
            elif target_info["expected_revision"] is not None:
                return {"status": "revision_conflict", "candidate_id": candidate_id,
                        "revision": None}
            item["status"] = "applying"
            _save(directory, state)  # Recovery intent precedes the vault write.
            _reject_links(target)
            if target_info["expected_revision"] is not None and file_hash(target) != target_info["expected_revision"]:
                return {"status": "revision_conflict", "candidate_id": candidate_id,
                        "revision": file_hash(target)}
            planned = (target.read_text(encoding="utf-8-sig") + item["addition"]
                       if target_info["expected_revision"] is not None else item["new_text"])
            if _hash(planned) != item["new_sha256"]:
                raise ValueError("Candidate bytes changed")
            _atomic(target, planned.encode("utf-8"),
                    create=target_info["expected_revision"] is None)
            item["status"] = "applied"
            _save(directory, state)
            return {"status": "applied", "candidate_id": candidate_id,
                    "revision": item["new_sha256"],
                    "canonical_id": [config["vault_id"], target_info["note_id"]]}


def record_usage(workspace: Path | str, event: dict) -> dict:
    """Store one model call's observed token fields; missing means unavailable."""
    if not isinstance(event, dict) or len(_stable(event).encode("utf-8")) > 4000:
        raise ValueError("Usage event must be a bounded object")
    call_id = _bounded(event.get("call_id"), "call_id", 160, required=True)
    task_id = _bounded(event.get("task_id"), "task_id", 160, required=True)
    phase = event.get("phase")
    if phase not in _PHASES:
        raise ValueError("Invalid usage phase")
    role = _bounded(event.get("role", "unknown"), "role", 100, required=True)
    retry = event.get("retry", 0)
    if type(retry) is not int or retry < 0:
        raise ValueError("retry must be a nonnegative integer")
    actor = event.get('actor', 'call:' + call_id)
    if not isinstance(actor, str) or not actor or len(actor) > 160:
        raise ValueError('Usage actor must be a bounded parent/child identifier')
    for flag in ('cumulative', 'included_in_parent'):
        if flag in event and type(event[flag]) is not bool:
            raise ValueError(flag + ' must be boolean')
    fields = {}
    for field in ("input_tokens", "output_tokens", "cached_input_tokens"):
        value = event.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{field} must be a nonnegative integer or null")
        fields[field] = value
    row = {"call_id": call_id, "task_id": task_id, "phase": phase, "role": role,
           "retry": retry, "actor": actor, "cumulative": event.get('cumulative', False),
           "included_in_parent": event.get('included_in_parent', False), **fields}
    directory = _ledger_dir(_root(workspace))
    with FileLock(str(directory / "ledger.lock"), timeout=10):
        state = _load(directory)
        prior = state["usage"].get(call_id)
        if prior:
            normalized = {**prior, 'actor': prior.get('actor', 'call:' + call_id),
                          'cumulative': prior.get('cumulative', False),
                          'included_in_parent': prior.get('included_in_parent', False)}
            normalized.pop('sequence', None)
            if normalized != row:
                raise ValueError("Conflicting usage event for call_id")
            return {"status": "duplicate", "call_id": call_id}
        state["usage"][call_id] = {**row, 'sequence': len(state['usage'])}
        _save(directory, state)
    return {"status": "recorded", "call_id": call_id,
            "measurement": ("observed" if all(value is not None for value in fields.values())
                            else "partial" if any(value is not None for value in fields.values())
                            else "unavailable")}


def usage_report(workspace: Path | str) -> dict:
    directory = _ledger_dir(_root(workspace))
    with FileLock(str(directory / "ledger.lock"), timeout=10):
        state = _load(directory)
    rows = sorted(state["usage"].values(), key=lambda row: row.get('sequence', -1))
    from .run_metrics import summarize_run_metrics
    accounting = summarize_run_metrics([{'id': row['call_id'], 'kind': 'usage',
        'actor': row.get('actor', 'call:' + row['call_id']),
        'cumulative': row.get('cumulative', False),
        'included_in_parent': row.get('included_in_parent', False),
        **{field: row.get(field) for field in ('input_tokens','cached_input_tokens','output_tokens')}}
        for row in rows])
    fields = ("input_tokens", "output_tokens", "cached_input_tokens")
    totals = {field: {"known": accounting["tokens"][field]["known"],
                      "missing_calls": accounting["tokens"][field]["missing_events"],
                      "observed_calls": accounting["tokens"][field]["observed_events"]}
              for field in fields}
    observed = sum(all(row[field] is not None for field in fields) for row in rows)
    partial = sum(any(row[field] is not None for field in fields)
                  and not all(row[field] is not None for field in fields) for row in rows)
    unavailable = len(rows) - observed - partial
    phases = {phase: sum(row["phase"] == phase for row in rows) for phase in sorted(_PHASES)
              if any(row["phase"] == phase for row in rows)}
    return {"status": "observed" if rows and observed == len(rows) else
            "unavailable" if not rows or unavailable == len(rows) else "partial",
            "calls": len(rows), "observed_calls": observed, "partial_calls": partial,
            "unavailable_calls": unavailable, "totals": totals, "phases": phases,
            "roles": {role: sum(row["role"] == role for row in rows)
                      for role in sorted({row["role"] for row in rows})},
            "retry_calls": sum(row["retry"] > 0 for row in rows),
            "observed_accounting": accounting,
            "candidate_counts": {status: sum(row["selection"] == status for row in state["traces"].values())
                                 for status in ("selected", "skipped")}}
