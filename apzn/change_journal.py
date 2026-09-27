"""Conditional, owner-scoped configuration changes.

The journal records only the selector APZN owns.  Rollback compares that
selector, so unrelated edits in the same TOML file survive while edits to the
managed value become an explicit conflict.
"""
from __future__ import annotations

import copy
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import dependencies as _dependencies
import tomlkit
from filelock import FileLock

from .core import atomic_json, digest, read_json, store


def _root(journal_root: Path | None = None) -> Path:
    path = (journal_root or (store() / "change-journal")).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plain(value: Any) -> Any:
    if hasattr(value, "unwrap"):
        return value.unwrap()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _value_digest(exists: bool, value: Any) -> str | None:
    return digest(_plain(value)) if exists else None


def _lookup(document: Any, key_path: list[str]) -> tuple[bool, Any]:
    node = document
    for key in key_path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, _plain(node)


def _set(document: Any, key_path: list[str], exists: bool, value: Any = None) -> None:
    node = document
    for key in key_path[:-1]:
        if key not in node:
            node[key] = tomlkit.table()
        if not isinstance(node[key], dict):
            raise ValueError("Managed TOML path crosses a non-table value")
        node = node[key]
    leaf = key_path[-1]
    if exists:
        node[leaf] = copy.deepcopy(value)
    elif leaf in node:
        del node[leaf]


def _write_toml(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".apzn-journal.tmp")
    temp.write_text(tomlkit.dumps(document), encoding="utf-8")
    os.replace(temp, path)


def apply_toml_entry(target: Path, key_path: list[str], value: Any, *, owner: str,
                     journal_root: Path | None = None, require_absent: bool = False) -> dict:
    if not owner or not key_path or any(not isinstance(k, str) or not k for k in key_path):
        raise ValueError("Owner and nonempty TOML key path are required")
    target = target.expanduser().resolve()
    root = _root(journal_root)
    with FileLock(str(root / "operations.lock"), timeout=10):
        text = target.read_text(encoding="utf-8-sig") if target.exists() else ""
        document = tomlkit.parse(text)
        before_exists, before_value = _lookup(document, key_path)
        if require_absent and before_exists:
            raise ValueError("Managed TOML entry already exists")
        operation_id = uuid.uuid4().hex
        record = {
            "schema_version": 1,
            "operation_id": operation_id,
            "owner": owner,
            "target": str(target),
            "selector": {"kind": "toml_entry", "key_path": key_path},
            "before": {"exists": before_exists, "value": before_value,
                       "value_digest": _value_digest(before_exists, before_value)},
            "after": {"exists": True, "value": _plain(value),
                      "value_digest": _value_digest(True, value)},
            "phase": "prepared",
            "created_at": time.time(),
        }
        path = root / f"{operation_id}.json"
        atomic_json(path, record)
        # Re-read immediately before replacement. External writers do not obey
        # this lock, so selector CAS is still required.
        current = tomlkit.parse(target.read_text(encoding="utf-8-sig") if target.exists() else "")
        current_exists, current_value = _lookup(current, key_path)
        if (current_exists, _value_digest(current_exists, current_value)) != (
                before_exists, record["before"]["value_digest"]):
            record["phase"] = "conflict"; atomic_json(path, record)
            raise ValueError("Managed TOML entry changed before apply")
        _set(current, key_path, True, value)
        _write_toml(target, current)
        record["phase"] = "applied"; atomic_json(path, record)
        verify = tomlkit.parse(target.read_text(encoding="utf-8-sig"))
        exists, observed = _lookup(verify, key_path)
        if not exists or _value_digest(True, observed) != record["after"]["value_digest"]:
            record["phase"] = "conflict"; atomic_json(path, record)
            raise ValueError("Managed TOML entry was not applied exactly")
        record["phase"] = "committed"; atomic_json(path, record)
        return record


def rollback(operation: str | dict, *, journal_root: Path | None = None) -> dict:
    root = _root(journal_root)
    record = read_json(root / f"{operation}.json") if isinstance(operation, str) else copy.deepcopy(operation)
    if not record or record.get("selector", {}).get("kind") != "toml_entry":
        raise ValueError("Unknown or unsupported journal operation")
    path = root / f"{record['operation_id']}.json"
    with FileLock(str(root / "operations.lock"), timeout=10):
        target = Path(record["target"])
        document = tomlkit.parse(target.read_text(encoding="utf-8-sig") if target.exists() else "")
        key_path = record["selector"]["key_path"]
        exists, value = _lookup(document, key_path)
        after = record["after"]
        if (exists, _value_digest(exists, value)) != (after["exists"], after["value_digest"]):
            record["phase"] = "conflict"; atomic_json(path, record)
            raise ValueError("Managed TOML entry has later edits; rollback refused")
        before = record["before"]
        _set(document, key_path, before["exists"], before.get("value"))
        _write_toml(target, document)
        record["phase"] = "rolled_back"; record["rolled_back_at"] = time.time()
        atomic_json(path, record)
        return record


def recover(journal_root: Path | None = None, *, rollback_incomplete: bool = True) -> list[dict]:
    """Recover interrupted selector writes conservatively.

    A prepared row still at ``before`` made no change and is closed as rolled
    back.  A prepared/applied row at ``after`` is conditionally rolled back.
    Any third value is a conflict and is never overwritten.
    """
    root = _root(journal_root)
    result=[]
    for path in sorted(root.glob("*.json")):
        record=read_json(path)
        if not record or record.get("phase") in {"committed","rolled_back"}:continue
        if not rollback_incomplete or record.get("phase")=='conflict':result.append(record);continue
        target=Path(record['target']);document=tomlkit.parse(target.read_text(encoding='utf-8-sig') if target.exists() else '')
        exists,value=_lookup(document,record['selector']['key_path']);observed=(exists,_value_digest(exists,value))
        before=(record['before']['exists'],record['before']['value_digest']);after=(record['after']['exists'],record['after']['value_digest'])
        if observed==before:
            record['phase']='rolled_back';record['recovered_at']=time.time();atomic_json(path,record)
        elif observed==after:
            record=rollback(record,journal_root=root);record['recovered_at']=time.time();atomic_json(path,record)
        else:
            record['phase']='conflict';atomic_json(path,record);result.append(record)
    return result
