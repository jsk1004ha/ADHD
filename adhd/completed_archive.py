"""Explicit opt-in capture of a native completed run's textual evidence."""
from __future__ import annotations

import json
from pathlib import Path

from .immutable_store import ImmutableStore, KEY, _safe_path
from .memory import namespace


def capture_completed(workspace: Path, owner_key: str, policy_revision: str, *,
                      privacy: str = "private", source: Path | None = None,
                      retention: str = "normal", root: Path | None = None,
                      native_root: Path | None = None) -> dict:
    """Capture a native-authorized completed file; no hook or existing archive is changed."""
    if not isinstance(owner_key, str) or not KEY.fullmatch(owner_key):
        raise ValueError("native-owner-key-required")
    workspace = Path(workspace).expanduser().resolve(strict=True)
    with ImmutableStore(root, native_root=native_root) as store:
        state_file = _safe_path(store.native_root / owner_key / "state.json", store.native_root)
        if state_file.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("native-state-too-large")
        state = json.loads(state_file.read_text(encoding="utf-8"))
        run = state.get("run_id")
        metadata = {"workspace": str(workspace), "scope": namespace(workspace),
                    "owner_key": owner_key, "owner_run": run, "kind": "tool_result",
                    "privacy": privacy, "policy_revision": policy_revision,
                    "provenance": {"kind": "native-completed-run", "owner_run": run},
                    "retention": retention}
        store._metadata(metadata)  # The authoritative native state, not caller JSON, grants completion.
        store.register_root(workspace, owner_key, run)
        selected = (store.native_root / owner_key / ("completion-" + run + ".json")) if source is None else Path(source)
        result = store.put(selected, metadata)
        return {**result, "owner_key": owner_key, "owner_run": run, "source": str(selected)}
