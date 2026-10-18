"""Closed, opt-in GC for CAS objects born after a native root was registered.

Only the fixed native state and local pin-manifest contract are understood here.
Anything outside that contract prevents collection; source archives are untouched.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from filelock import FileLock

from .core import digest as core_digest
from .immutable_store import (CHUNK, DIGEST, KEY, RUN, _access, _hash_file,
                              _link, _safe_path)
from .models import EFFORT_OVERRIDABLE


MAX_ROOTS = 64
MAX_OBJECTS = 4096
MAX_REFS = 256
BATCH = 8
MAX_MANIFEST = 65536
NATIVE_STATE_FIELDS = set("""actions astra_calls batch_required candidate check_decisions checkpoints
children context_refs contract contract_hash delivery_milestones efficiency_policy_version epoch
execution_profile explicit_policy_keys failure_counts failure_history feedback goal_request
guidance_signature halt_emitted host_capabilities intent_history intent_version key
last_progress last_stop_decision lease_generation lifetime_children lifetime_rounds loop_mode
mode model_calls needs_replan pending_turn_ids plan plan_required policy processed
progress_initialized prompt_turns prompts recipe_saved recipes request_acks request_route
reservations review_children review_receipt rounds run_id schema_version seen_progress
seen_stops seen_target_digests stagnation started status test_outcomes tool_observations
total_children usage verified_failure_counts verified_progress version wait_observations
workspace completed_at session_helper_profiles session_verifier_profile session_reader
last_assistant_message view_hint_sig host_policy_version last_action_decision
 progress_last_gained_at churn ladder_stage time_budget""".split())


def _generation(store) -> int:
    return store.db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]


def _contract(store) -> None:
    if store.db.execute("SELECT value FROM meta WHERE key='gc_contract'").fetchone()[0] != 1:
        raise ValueError("legacy-store-has-no-closed-gc-contract")


def _roots(store) -> list:
    rows = store.db.execute("SELECT * FROM gc_roots ORDER BY owner_key,id LIMIT ?", (MAX_ROOTS + 1,)).fetchall()
    if not rows or len(rows) > MAX_ROOTS:
        raise ValueError("missing-or-unbounded-registered-roots")
    return rows


def _lease(store, workspace: Path) -> Path:
    return store.native_root.parent / "locks" / (core_digest(str(workspace.resolve()))[:24] + ".json")


@contextmanager
def _guards(store, roots):
    """Native state, then lease guards, then CAS writer: native's lock order."""
    with ExitStack() as stack:
        state_locks = sorted({str(store.native_root / row["owner_key"] / "state.lock") for row in roots})
        lease_locks = sorted({str(_lease(store, Path(row["workspace"]))) + ".guard" for row in roots})
        for path in state_locks:
            _safe_path(Path(path), store.native_root, existing=False)
            stack.enter_context(FileLock(path, timeout=10))
        for path in lease_locks:
            _safe_path(Path(path), store.native_root.parent, existing=False)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(FileLock(path, timeout=10))
        stack.enter_context(store.lock)
        yield


def _state(store, row) -> dict:
    workspace = Path(row["workspace"]).resolve(strict=True)
    path = _safe_path(store.native_root / row["owner_key"] / "state.json", store.native_root)
    if path.stat().st_size > 2 * CHUNK or _hash_file(path)[0] != row["state_hash"]:
        raise ValueError("native-root-changed-since-registration")
    state = json.loads(path.read_text(encoding="utf-8"))
    if (state.get("schema_version") != 2 or state.get("key") != row["owner_key"]
            or state.get("run_id") != row["owner_run"]
            or Path(state.get("workspace", "")).resolve() != workspace
            or state.get("status") != "complete"):
        raise ValueError("native-root-not-complete")
    children = state.get("children", {})
    if (not isinstance(children, dict) or any(not isinstance(child, dict) or child.get("status") != "finished"
                                               for child in children.values())
            or state.get("pending_turn_ids") or state.get("reservations") or state.get("context_refs")
            or any(state.get(name) for name in ("batch_report", "pending_terminal",
                                                  "host_resume_pending", "worktree", "backup",
                                                  "archived_runs", "pending_context_deltas"))):
        raise ValueError("native-root-has-protected-work")
    if set(state) - NATIVE_STATE_FIELDS:
        raise ValueError("native-root-has-unclassified-fields")
    if "session_helper_profiles" in state or "session_verifier_profile" in state or "session_reader" in state:
        helpers = state.get("session_helper_profiles")
        verifier = state.get("session_verifier_profile")
        reader = state.get("session_reader")
        if (not isinstance(helpers, dict) or set(helpers) != EFFORT_OVERRIDABLE
                or any(value is not None and (not isinstance(value, str) or not DIGEST.fullmatch(value))
                       for value in helpers.values())
                or not isinstance(verifier, dict)
                or set(verifier) != {"release_root", "profile_hash"}
                or not isinstance(verifier["release_root"], str)
                or not Path(verifier["release_root"]).is_absolute()
                or (verifier["profile_hash"] is not None
                    and (not isinstance(verifier["profile_hash"], str)
                         or not DIGEST.fullmatch(verifier["profile_hash"])))
                or not isinstance(reader, dict)
                or set(reader) != {"release_root", "source_sha256", "max_state_version", "observed_at"}
                or reader["release_root"] != verifier["release_root"]
                or not isinstance(reader["source_sha256"], str)
                or not DIGEST.fullmatch(reader["source_sha256"])
                or reader["max_state_version"] != 2
                or isinstance(reader["observed_at"], bool)
                or not isinstance(reader["observed_at"], (int, float))
                or not math.isfinite(reader["observed_at"])):
            raise ValueError("native-session-attestation-unclassified")
    assistant = state.get("last_assistant_message")
    if assistant is not None and (
            not isinstance(assistant, dict)
            or set(assistant) != {"text", "sha256", "observed_at"}
            or not isinstance(assistant["text"], str) or len(assistant["text"]) > 4000
            or not isinstance(assistant["sha256"], str) or not DIGEST.fullmatch(assistant["sha256"])
            or isinstance(assistant["observed_at"], bool)
            or not isinstance(assistant["observed_at"], (int, float))
            or not math.isfinite(assistant["observed_at"])):
        raise ValueError("native-assistant-observation-unclassified")
    if "view_hint_sig" in state and (not isinstance(state["view_hint_sig"], str)
                                     or not DIGEST.fullmatch(state["view_hint_sig"])):
        raise ValueError("native-view-hint-unclassified")
    policy_version = state.get("host_policy_version")
    if policy_version is not None and (
            not isinstance(policy_version, str) or not 1 <= len(policy_version) <= 128
            or any(ord(char) < 32 for char in policy_version)):
        raise ValueError("native-host-policy-unclassified")
    progress_time = state.get("progress_last_gained_at")
    if progress_time is not None and (
            isinstance(progress_time, bool) or not isinstance(progress_time, (int, float))
            or not math.isfinite(progress_time) or progress_time <= 0):
        raise ValueError("native-progress-time-unclassified")
    decision = state.get("last_action_decision")
    if decision is not None and (
            not isinstance(decision, dict)
            or set(decision) != {"retry_allowed", "reason", "evidence_refs", "limits_of_observation"}
            or not isinstance(decision["retry_allowed"], bool)
            or not isinstance(decision["reason"], str)
            or decision["reason"] not in {"needs_scoped_user_authorization",
                                          "same_denial_without_policy_change",
                                          "no_identical_observed_denial"}
            or not isinstance(decision["evidence_refs"], list)
            or len(decision["evidence_refs"]) > 5
            or any(not isinstance(ref, str) or not 1 <= len(ref) <= 256
                   for ref in decision["evidence_refs"])
            or not isinstance(decision["limits_of_observation"], str)
            or len(decision["limits_of_observation"]) > 256):
        raise ValueError("native-action-decision-unclassified")
    for name in ("churn", "ladder_stage"):
        if name in state and (type(state[name]) is not int or not 0 <= state[name] <= 1_000_000):
            raise ValueError("native-loop-accounting-unclassified")
    candidate = state.get("candidate")
    if candidate is not None:
        if (not isinstance(candidate, dict) or candidate.get("batch_report")
                or (state.get("contract") or {}).get("coding_scope")
                or not isinstance(state.get("review_receipt"), dict)
                or state["review_receipt"].get("digest") != candidate.get("digest")):
            raise ValueError("native-candidate-or-batch-unclassified")
        from .native import is_fresh
        if not is_fresh(state):
            raise ValueError("native-candidate-evidence-not-current")
    return state


def _manifest(path: Path) -> dict:
    if _link(path) or not path.is_file() or path.stat().st_size > MAX_MANIFEST:
        raise ValueError("pin-manifest-missing-or-unsafe")
    with path.open("rb") as source:
        raw = source.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ValueError("pin-manifest-too-large")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "ref_ids", "hashes"}
            or value["schema_version"] != 1 or not isinstance(value["ref_ids"], list)
            or not isinstance(value["hashes"], dict) or len(value["ref_ids"]) > MAX_REFS
            or len(set(value["ref_ids"])) != len(value["ref_ids"])):
        raise ValueError("unsupported-pin-manifest")
    for ref_id in value["ref_ids"]:
        if not isinstance(ref_id, str) or not RUN.fullmatch(ref_id) or not DIGEST.fullmatch(value["hashes"].get(ref_id, "")):
            raise ValueError("unsupported-pin-manifest")
    if set(value["hashes"]) != set(value["ref_ids"]):
        raise ValueError("unsupported-pin-manifest")
    return value


def _native_files(store, row) -> list:
    owner = store.native_root / row["owner_key"]
    run = row["owner_run"]
    entries = []
    for path in owner.iterdir():
        name = path.name
        supported = (name in {"state.json", "state.lock", "ledger.jsonl", "ledger.previous.jsonl",
                              f"completion-{run}.json", f"plan-{run}.json"}
                     or re.fullmatch(r"contract-" + run + r"-v[1-9][0-9]*\.json", name))
        if not supported or not path.is_file() or _link(path) or path.stat().st_size > 2 * CHUNK:
            raise ValueError("unknown-or-unbounded-native-artifact")
        entries.append((name, "held-lock" if name == "state.lock" else _hash_file(
            _safe_path(path, store.native_root))[0]))
    return sorted(entries)


def _derived_status_sidecars(workspace: Path, key: str, run: str, state: dict) -> set[str]:
    names = {'context-status.json', 'context-status.md'}
    root = workspace / '.adhd'
    if not any((root / name).exists() for name in names):
        return set()
    paths = {name: _safe_path(root / name, workspace) for name in names}
    if any(path.stat().st_size > MAX_MANIFEST for path in paths.values()):
        raise ValueError('unbounded-derived-status-sidecar')
    value = json.loads(paths['context-status.json'].read_text(encoding='utf-8'))
    fields = {'schema_version', 'authority', 'session', 'source', 'run_id', 'intent_version',
              'goal', 'phase', 'verification', 'failure', 'blocker', 'next_action',
              'candidate_digest', 'completed_steps', 'truncated_fields', 'projection_sha256',
              'changes', 'markdown'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['intent_version']) is not int
            or value['authority'] != 'derived_not_acceptance' or value['session'] != key
            or value['run_id'] != run or value['intent_version'] != state.get('intent_version')
            or value['phase'] != state.get('status')
            or value['source'] != {'path': '.adhd/bridge/' + key + '/view.json'}
            or value['candidate_digest'] != (state.get('candidate') or {}).get('digest')
            or not isinstance(value['projection_sha256'], str)
            or DIGEST.fullmatch(value['projection_sha256']) is None
            or not isinstance(value['markdown'], str)):
        raise ValueError('unrecognized-derived-status-sidecar')
    from .context_session import _status_markdown
    if (_status_markdown(value) != value['markdown']
            or paths['context-status.md'].read_text(encoding='utf-8') != value['markdown']):
        raise ValueError('derived-status-content-mismatch')
    return names


def _workspace_files(workspace: Path, key: str, run: str, state: dict,
                     known_run: set[str], pin_paths: list[Path]) -> list:
    root = workspace / ".adhd"
    if not root.is_dir() or _link(root):
        raise ValueError("native-workspace-artifacts-missing")
    policy_file = _safe_path(root / "immutable-policy.json", workspace)
    if policy_file.stat().st_size > 8192:
        raise ValueError("immutable-policy-too-large")
    policy_hash = _hash_file(policy_file)[0]
    from .immutable_policy import load_policy
    load_policy(workspace)
    if _hash_file(policy_file)[0] != policy_hash:
        raise ValueError("immutable-policy-changed-during-scan")
    receipt_ids = set()
    for relative in (state.get("candidate") or {}).get("execution_receipts", {}):
        parts = Path(relative).parts
        if len(parts) == 4 and parts[:2] == (".adhd", "checks") and parts[3] == "receipt.json" and RUN.fullmatch(parts[2]):
            receipt_ids.add(parts[2])
    allowed_dirs = {"bridge", f"bridge/{key}", f"bridge/{key}/inbox",
                    f"bridge/{key}/outbox", f"bridge/{key}/processed", f"bridge/{key}/packets",
                    "checks", run, f"{run}/cas-pins"}
    allowed_dirs |= {f"checks/{receipt}" for receipt in receipt_ids}
    pin_names = {path.name for path in pin_paths if path.exists()}
    status_sidecars = _derived_status_sidecars(workspace, key, run, state)
    files = []
    count = 0
    for current, directories, names in os.walk(root, followlinks=False):
        for name in directories:
            item = Path(current) / name
            relative = item.relative_to(root).as_posix()
            if relative not in allowed_dirs or _link(item):
                raise ValueError("unknown-native-workspace-directory")
        for name in names:
            item = _safe_path(Path(current) / name, workspace)
            relative = item.relative_to(root).as_posix()
            parts = relative.split("/")
            accepted = (len(parts) == 2 and parts[0] == run and parts[1] in known_run)
            accepted |= relative == "immutable-policy.json"
            accepted |= relative in status_sidecars
            accepted |= len(parts) == 3 and parts[:2] == [run, "cas-pins"] and parts[2] in pin_names
            accepted |= (len(parts) == 3 and parts[:2] == ["bridge", key] and parts[2] == "view.json")
            accepted |= (len(parts) == 4 and parts[:2] == ["bridge", key]
                         and parts[2] in {"outbox", "processed"} and RUN.fullmatch(parts[3][:-5]) is not None
                         and parts[3].endswith(".json"))
            accepted |= (len(parts) == 4 and parts[:2] == ["bridge", key]
                         and parts[2] == "packets" and DIGEST.fullmatch(parts[3][:-5]) is not None
                         and parts[3].endswith(".json"))
            accepted |= (len(parts) == 3 and parts[0] == "checks" and parts[1] in receipt_ids
                         and parts[2] in {"receipt.json", "subject.json", "check.input.txt",
                                          "check.stderr.log", "check.stdout.jsonl"})
            if not accepted or item.stat().st_size > 64 * CHUNK:
                raise ValueError("unknown-or-unbounded-native-workspace-file")
            count += 1
            if count > 512:
                raise ValueError("too-many-native-workspace-files")
            files.append((relative, _hash_file(item)[0]))
    view = root / "bridge" / key / "view.json"
    if (root / "bridge").exists():
        if not view.is_file() or view.stat().st_size > 2 * CHUNK:
            raise ValueError("native-bridge-view-missing")
        public = json.loads(view.read_text(encoding="utf-8"))
        if (public.get("schema_version") != 2 or public.get("key") != key
                or public.get("run_id") != run or public.get("status") != "complete"):
            raise ValueError("native-bridge-view-mismatch")
    return sorted(files)


def _inventory(store, roots, *, quarantined: dict[str, Path] | None = None) -> dict:
    """Only known native/workspace/pin files and managed objects form a closure."""
    if {item.name for item in store.root.iterdir()} - {"objects", "tmp", "quarantine", "refs.sqlite3", "writer.lock"}:
        raise ValueError("unknown-store-root-artifact")
    registered_keys = {row["owner_key"] for row in roots}
    native_names = set()
    if store.native_root.exists():
        native_names = {item.name for item in store.native_root.iterdir()}
    if len(native_names) > MAX_ROOTS or native_names != registered_keys:
        raise ValueError("unregistered-or-unknown-native-owner")
    known_leases = {_lease(store, Path(row["workspace"])) for row in roots}
    lease_dir = store.native_root.parent / "locks"
    lease_evidence = []
    if lease_dir.exists():
        for item in lease_dir.iterdir():
            generations = {path.with_suffix(".generation.json") for path in known_leases}
            if item in generations:
                safe = _safe_path(item, store.native_root.parent)
                if safe.stat().st_size > MAX_MANIFEST:
                    raise ValueError("lease-generation-unbounded")
                generation = json.loads(safe.read_text(encoding="utf-8"))
                if (not isinstance(generation, dict) or set(generation) != {"generation"}
                        or not isinstance(generation["generation"], int) or generation["generation"] < 1):
                    raise ValueError("lease-generation-unknown")
                lease_evidence.append((item.name, _hash_file(safe)[0]))
            elif item not in known_leases and item not in {Path(str(path) + ".guard") for path in known_leases}:
                raise ValueError("unknown-writer-lease-or-lock")
    pin_evidence = []
    native_evidence = []
    workspace_evidence = []
    for row in roots:
        workspace = Path(row["workspace"]).resolve(strict=True)
        native_dir = _safe_path(store.native_root / row["owner_key"], store.native_root, existing=False)
        if not native_dir.is_dir() or _link(native_dir):
            raise ValueError("native-owner-directory-unsafe")
        state = _state(store, row)
        lease = _lease(store, workspace)
        _safe_path(lease, store.native_root.parent, existing=False)
        if lease.exists():
            raise ValueError("writer-lease-protects-root")
        run_dir = _safe_path(workspace / ".adhd" / row["owner_run"], workspace, existing=False)
        if run_dir.exists() and (not run_dir.is_dir() or _link(run_dir)):
            raise ValueError("registered-run-directory-unsafe")
        known_run = set()
        refs = store.db.execute("SELECT metadata FROM refs WHERE json_extract(metadata,'$.gc_root_id')=?", (row["id"],))
        count = 0
        for ref in refs:
            count += 1
            if count > MAX_REFS:
                raise ValueError("too-many-root-references")
            meta = json.loads(ref["metadata"])
            relative = meta.get("source_relative")
            if not isinstance(relative, str):
                raise ValueError("unclassified-reference")
            origin = meta.get("source_origin")
            base = workspace if origin == "workspace" else store.native_root if origin == "native" else None
            if base is None:
                raise ValueError("unclassified-reference")
            source = _safe_path(base / relative, base)
            if origin == "workspace":
                if source.parent != run_dir:
                    raise ValueError("unsupported-nested-workspace-source")
                known_run.add(source.name)
            else:
                if source.parent != native_dir:
                    raise ValueError("unsupported-nested-native-source")
        pins = store.db.execute("SELECT * FROM gc_pins WHERE manifest_path LIKE ?", (str(run_dir) + "%",))
        pin_paths = []
        for pin in pins:
            path = _safe_path(Path(pin["manifest_path"]), workspace, existing=False)
            if path.parent != run_dir / "cas-pins":
                raise ValueError("unclassified-pin")
            pin_paths.append(path)
            if path.exists():
                data = _manifest(path)
                pin_evidence.append((str(path), _hash_file(path)[0]))
                if data["ref_ids"] != [pin["ref_id"]] or data["hashes"].get(pin["ref_id"]) != pin["digest"]:
                    raise ValueError("pin-manifest-changed")
        native_evidence.append((row["id"], _native_files(store, row)))
        pin_dir = run_dir / "cas-pins"
        if pin_dir.exists():
            if _link(pin_dir) or not pin_dir.is_dir() or {item.name for item in pin_dir.iterdir()} != {p.name for p in pin_paths if p.exists()}:
                raise ValueError("unknown-pin-artifact")
        elif pin_paths:
            raise ValueError("pin-directory-missing")
        workspace_evidence.append((row["id"], _workspace_files(workspace, row["owner_key"],
                                                                 row["owner_run"], state, known_run, pin_paths)))
    quarantined = quarantined or {}
    expected_quarantine = {path.name for path in quarantined.values()} | {path.name + ".restore" for path in quarantined.values()}
    if any((store.root / "tmp").iterdir()) or {path.name for path in (store.root / "quarantine").iterdir()} - expected_quarantine:
        raise ValueError("pending-or-quarantined-object")
    found = set()
    for folder in (store.root / "objects").iterdir():
        if _link(folder) or not folder.is_dir() or len(folder.name) != 2:
            raise ValueError("unknown-object-directory")
        for item in folder.iterdir():
            if _link(item) or not item.is_file() or not DIGEST.fullmatch(item.name) or item.name[:2] != folder.name:
                raise ValueError("unknown-object-entry")
            found.add(item.name)
            if len(found) > MAX_OBJECTS:
                raise ValueError("too-many-objects-for-closed-scan")
            birth = store.db.execute("SELECT managed FROM gc_births WHERE digest=?", (item.name,)).fetchone()
            if birth is None or birth["managed"] != 1:
                raise ValueError("legacy-or-unclassified-object")
    for digest, path in quarantined.items():
        if path.parent != store.root / "quarantine" or not path.is_file() or _link(path):
            raise ValueError("missing-or-unsafe-quarantined-object")
        found.add(digest)
    managed_rows = store.db.execute("SELECT digest FROM gc_births WHERE managed=1 LIMIT ?", (MAX_OBJECTS + 1,)).fetchall()
    if len(managed_rows) > MAX_OBJECTS:
        raise ValueError("too-many-managed-objects")
    managed = {row["digest"] for row in managed_rows}
    if found != managed:
        raise ValueError("missing-or-unregistered-object")
    return json.loads(json.dumps({"roots": [{"id": row["id"], "state_hash": row["state_hash"]} for row in roots],
                                  "objects": sorted(found), "pin_manifests": sorted(pin_evidence),
                                  "native_files": native_evidence, "workspace_files": workspace_evidence,
                                  "lease_generations": sorted(lease_evidence)}, sort_keys=True))


def _eligible(store, digest: str, root_id: str, size: int) -> bool:
    if store.db.execute("SELECT 1 FROM gc_pins WHERE digest=? LIMIT 1", (digest,)).fetchone():
        return False
    refs = store.db.execute("SELECT * FROM refs WHERE digest=? ORDER BY id LIMIT ?", (digest, MAX_REFS + 1)).fetchall()
    if not refs or len(refs) > MAX_REFS or any(not row["forgotten"] for row in refs):
        return False
    for row in refs:
        meta = json.loads(row["metadata"])
        if (row["size"] != size or meta.get("retention") == "protected"
                or meta.get("gc_root_id") is None or meta.get("source_hash") != digest):
            return False
        registered = store.db.execute("SELECT 1 FROM gc_roots WHERE id=?", (meta["gc_root_id"],)).fetchone()
        if not registered:
            return False
        base = Path(meta["workspace"]) if meta.get("source_origin") == "workspace" else store.native_root
        try:
            original = _safe_path(base / meta["source_relative"], base)
            if _hash_file(original) != (digest, size):
                return False
        except (OSError, ValueError, KeyError):
            return False
    return True


def register_root(store, workspace: Path, owner_key: str, owner_run: str) -> str:
    _contract(store)
    if not KEY.fullmatch(owner_key) or not RUN.fullmatch(owner_run):
        raise ValueError("native-owner-key-and-run-required")
    workspace = Path(workspace).resolve(strict=True)
    probe = {"workspace": str(workspace), "owner_key": owner_key, "owner_run": owner_run}
    store._owner(probe)
    row = {"owner_key": owner_key, "workspace": str(workspace)}
    with _guards(store, [row]):
        store._owner(probe)
        state_path = _safe_path(store.native_root / owner_key / "state.json", store.native_root)
        state_hash = _hash_file(state_path)[0]
        existing = store.db.execute("SELECT * FROM gc_roots WHERE workspace=? OR owner_key=? LIMIT ?",
                                    (str(workspace), owner_key, MAX_ROOTS + 1)).fetchall()
        for prior in existing:
            if (prior["workspace"], prior["owner_key"]) != (str(workspace), owner_key):
                raise ValueError("native-root-owner-conflict")
            if prior["owner_run"] == owner_run:
                if prior["state_hash"] != state_hash:
                    raise ValueError("native-root-changed-since-registration")
                return prior["id"]
        if len(existing) >= MAX_ROOTS:
            raise ValueError("too-many-registered-native-runs")
        root_id = uuid.uuid4().hex
        with store.db:
            store.db.execute("INSERT INTO gc_roots VALUES (?,?,?,?,?)", (root_id, str(workspace), owner_key, owner_run, state_hash))
            store.db.execute("UPDATE meta SET value=value+1 WHERE key='generation'")
    return root_id


def pin(store, ref_id: str, access: dict, kind: str = "local_manifest") -> dict:
    _contract(store)
    if kind != "local_manifest":
        raise ValueError("unsupported-pin-kind")
    with store.lock:
        row, meta, _ = store._reference(ref_id, access)
        root_id = meta.get("gc_root_id")
        if not root_id or not store.db.execute("SELECT 1 FROM gc_roots WHERE id=?", (root_id,)).fetchone():
            raise ValueError("unregistered-reference-cannot-pin")
        workspace = Path(meta["workspace"])
        directory = _safe_path(workspace / ".adhd" / meta["owner_run"] / "cas-pins", workspace, existing=False)
        directory.mkdir(parents=True, exist_ok=True)
        pin_id = uuid.uuid4().hex
        manifest = directory / (pin_id + ".json")
        with store.db:
            store.db.execute("INSERT INTO gc_pins VALUES (?,?,?,?,?,0)", (pin_id, ref_id, row["digest"], kind, str(manifest)))
            store.db.execute("UPDATE meta SET value=value+1 WHERE key='generation'")
        return {"pin_id": pin_id, "manifest_path": str(manifest),
                "manifest": {"schema_version": 1, "ref_ids": [ref_id], "hashes": {ref_id: row["digest"]}}}


def _pin_row(store, pin_id: str, access: dict):
    if not isinstance(pin_id, str) or not RUN.fullmatch(pin_id):
        raise ValueError("pin-id-required")
    row = store.db.execute("SELECT * FROM gc_pins WHERE id=?", (pin_id,)).fetchone()
    if row is None:
        raise ValueError("pin-unavailable")
    ref = store.db.execute("SELECT * FROM refs WHERE id=?", (row["ref_id"],)).fetchone()
    if ref is None:
        raise ValueError("pinned-reference-missing")
    meta = json.loads(ref["metadata"])
    _access(meta, access)
    store._check_owner_receipt(meta)
    expected = Path(meta["workspace"]) / ".adhd" / meta["owner_run"] / "cas-pins" / (pin_id + ".json")
    if Path(row["manifest_path"]) != expected:
        raise ValueError("pin-path-mismatch")
    return row, _safe_path(expected, Path(meta["workspace"]), existing=False)


def confirm_pin(store, pin_id: str, access: dict) -> dict:
    with store.lock:
        row, path = _pin_row(store, pin_id, access)
        value = _manifest(path)
        if row["ref_id"] not in value["ref_ids"] or value["hashes"][row["ref_id"]] != row["digest"]:
            raise ValueError("pin-manifest-does-not-reference-object")
        with store.db:
            store.db.execute("UPDATE gc_pins SET confirmed=1 WHERE id=?", (pin_id,))
            store.db.execute("UPDATE meta SET value=value+1 WHERE key='generation'")
    return {"pin_id": pin_id, "confirmed": True}


def release_pin(store, pin_id: str, access: dict) -> dict:
    with store.lock:
        row, path = _pin_row(store, pin_id, access)
        if not row["confirmed"]:
            raise ValueError("unconfirmed-pin-stays-protected")
        if path.exists():
            value = _manifest(path)
            if row["ref_id"] in value["ref_ids"]:
                raise ValueError("published-manifest-still-references-pin")
        with store.db:
            store.db.execute("DELETE FROM gc_pins WHERE id=?", (pin_id,))
            store.db.execute("UPDATE meta SET value=value+1 WHERE key='generation'")
        if path.parent.is_dir() and not any(path.parent.iterdir()):
            path.parent.rmdir()
    return {"pin_id": pin_id, "released": True}


def _blocked(store, reason: str, *, operation_id: str | None = None) -> dict:
    result = {"dry_run": True, "status": "blocked", "reason": reason, "candidates": [],
              "operation_id": operation_id, "reclaimed_disk_bytes": None}
    try:
        result.update(store.audit())
    except (OSError, ValueError):
        pass
    return result


def plan(store) -> dict:
    try:
        _contract(store)
        roots = _roots(store)
        with _guards(store, roots):
            if [(r["id"], r["state_hash"]) for r in _roots(store)] != [(r["id"], r["state_hash"]) for r in roots]:
                raise ValueError("registered-roots-changed")
            if store.db.execute("SELECT 1 FROM gc_runs WHERE status IN ('applying','interrupted') LIMIT 1").fetchone():
                raise ValueError("interrupted-gc-requires-recovery")
            snapshot = _inventory(store, roots)
            generation = _generation(store)
            candidates = []
            for birth in store.db.execute("SELECT b.digest,b.root_id,r.size FROM gc_births b "
                                          "JOIN refs r ON r.digest=b.digest WHERE b.managed=1 "
                                          "GROUP BY b.digest ORDER BY b.digest"):
                if _eligible(store, birth["digest"], birth["root_id"], birth["size"]):
                    candidates.append({"hash": birth["digest"], "bytes": birth["size"], "root_id": birth["root_id"]})
                    if len(candidates) >= BATCH:
                        break
            op_id = uuid.uuid4().hex
            with store.db:
                store.db.execute("INSERT INTO gc_runs VALUES (?,?,?,?,?)",
                                 (op_id, generation, json.dumps(snapshot, sort_keys=True), "planned", time.time()))
                store.db.executemany("INSERT INTO gc_candidates VALUES (?,?,?,?,?)",
                                     [(op_id, row["hash"], row["bytes"], row["root_id"], "planned") for row in candidates])
            return {**store.audit(), "dry_run": True, "status": "planned", "operation_id": op_id,
                    "candidates": [{"candidate_id": op_id + ":" + str(index), "bytes": row["bytes"]}
                                   for index, row in enumerate(candidates)],
                    "more_candidates_possible": len(candidates) == BATCH,
                    "candidate_bytes": sum(r["bytes"] for r in candidates),
                    "reclaimed_disk_bytes": None}
    except OSError:
        return _blocked(store, "gc-filesystem-unavailable")
    except (ValueError, json.JSONDecodeError) as exc:
        return _blocked(store, str(exc))


def _verify_quarantine(path: Path, digest: str, size: int) -> Path:
    temporary = path.with_name(path.name + ".restore")
    try:
        with path.open("rb") as source, temporary.open("xb") as output:
            while chunk := source.read(CHUNK):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if _hash_file(path) != (digest, size) or _hash_file(temporary) != (digest, size):
            raise ValueError("quarantine-restore-verification-failed")
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def apply(store, operation_id: str) -> dict:
    if not isinstance(operation_id, str) or not RUN.fullmatch(operation_id):
        raise ValueError("operation-id-required")
    _contract(store)
    roots = _roots(store)
    with _guards(store, roots):
        run = store.db.execute("SELECT * FROM gc_runs WHERE id=?", (operation_id,)).fetchone()
        if run is None or run["status"] != "planned":
            raise ValueError("gc-plan-unavailable")
        if run["generation"] != _generation(store) or json.loads(run["snapshot"]) != _inventory(store, roots):
            raise ValueError("gc-plan-stale")
        candidates = store.db.execute("SELECT * FROM gc_candidates WHERE run_id=? ORDER BY digest", (operation_id,)).fetchall()
        for row in candidates:
            if not _eligible(store, row["digest"], row["root_id"], row["size"]):
                raise ValueError("gc-candidate-no-longer-eligible")
        with store.db:
            store.db.execute("UPDATE gc_runs SET status='applying' WHERE id=?", (operation_id,))
        removed = []
        expected_generation = run["generation"]
        for row in candidates:
            digest, size = row["digest"], row["size"]
            blob = store._blob(digest)
            quarantined = store.root / "quarantine" / (operation_id + "-" + digest)
            committed = False
            if _hash_file(blob) != (digest, size):
                raise ValueError("gc-source-blob-corrupt")
            with store.db:
                store.db.execute("UPDATE gc_candidates SET status='intent' WHERE run_id=? AND digest=?", (operation_id, digest))
            try:
                os.replace(blob, quarantined)
                with store.db:
                    store.db.execute("UPDATE gc_candidates SET status='quarantined' WHERE run_id=? AND digest=?", (operation_id, digest))
                restoration = _verify_quarantine(quarantined, digest, size)
                current = _inventory(store, roots, quarantined={digest: quarantined})
                prior = json.loads(run["snapshot"])
                prior["objects"] = sorted(set(prior["objects"]) - set(removed))
                if (expected_generation != _generation(store) or current != prior
                        or not _eligible(store, digest, row["root_id"], size)):
                    raise ValueError("gc-protection-changed-after-quarantine")
                with store.db:
                    store.db.execute("UPDATE gc_candidates SET status='delete_ready' WHERE run_id=? AND digest=?", (operation_id, digest))
                quarantined.unlink()
                with store.db:
                    store.db.execute("UPDATE gc_candidates SET status='removed' WHERE run_id=? AND digest=?", (operation_id, digest))
                    store.db.execute("DELETE FROM gc_births WHERE digest=?", (digest,))
                    store.db.execute("UPDATE meta SET value=value+1 WHERE key='generation'")
                committed = True
                expected_generation += 1
                removed.append(digest)
                restoration.unlink()
            except Exception:
                if not committed and quarantined.exists() and not blob.exists():
                    os.replace(quarantined, blob)
                restoration = quarantined.with_name(quarantined.name + ".restore")
                if not committed and restoration.exists() and not blob.exists() and _hash_file(restoration) == (digest, size):
                    os.replace(restoration, blob)
                with store.db:
                    store.db.execute("UPDATE gc_runs SET status='interrupted' WHERE id=?", (operation_id,))
                raise
        with store.db:
            store.db.execute("UPDATE gc_runs SET status='complete' WHERE id=?", (operation_id,))
        return {"operation_id": operation_id, "status": "complete", "removed_count": len(removed),
                "removed_blob_bytes": sum(row["size"] for row in candidates), "reclaimed_disk_bytes": None}


def recover(store) -> dict:
    _contract(store)
    roots = _roots(store)
    restored = []
    blocked = []
    with _guards(store, roots):
        rows = store.db.execute("SELECT c.*,r.status AS run_status FROM gc_candidates c "
                                "JOIN gc_runs r ON r.id=c.run_id WHERE r.status IN ('applying','interrupted') "
                                "ORDER BY c.run_id,c.digest").fetchall()
        known = {row["run_id"] + "-" + row["digest"] for row in rows}
        entries = {path.name for path in (store.root / "quarantine").iterdir()}
        if entries - known - {name + ".restore" for name in known}:
            return {"status": "blocked", "restored_count": 0, "blocked_count": 1,
                    "reason": "unknown-quarantine-object"}
        for row in rows:
            blob = store._blob(row["digest"])
            quarantined = store.root / "quarantine" / (row["run_id"] + "-" + row["digest"])
            backup = quarantined.with_name(quarantined.name + ".restore")
            _safe_path(quarantined, store.root, existing=False)
            _safe_path(backup, store.root, existing=False)
            if row["status"] == "removed":
                if quarantined.exists() or blob.exists():
                    blocked.append(row["digest"])
                    continue
                if backup.exists():
                    if _hash_file(backup) != (row["digest"], row["size"]):
                        blocked.append(row["digest"])
                        continue
                    if store.db.execute("SELECT 1 FROM refs WHERE digest=? AND forgotten=0 LIMIT 1", (row["digest"],)).fetchone():
                        os.replace(backup, blob)
                        blocked.append(row["digest"])
                        continue
                    backup.unlink()
                continue
            if blob.exists() and (quarantined.exists() or backup.exists()):
                blocked.append(row["digest"])
                continue
            source = quarantined if quarantined.exists() else backup if backup.exists() else None
            if source is not None:
                if _hash_file(source) != (row["digest"], row["size"]):
                    blocked.append(row["digest"])
                    continue
                os.replace(source, blob)
                restored.append(row["digest"])
            elif blob.exists():
                if _hash_file(blob) != (row["digest"], row["size"]):
                    blocked.append(row["digest"])
                    continue
            else:
                blocked.append(row["digest"])
                continue
            if backup.exists():
                if _hash_file(backup) != (row["digest"], row["size"]):
                    blocked.append(row["digest"])
                    continue
                backup.unlink()
            with store.db:
                store.db.execute("UPDATE gc_candidates SET status='restored' WHERE run_id=? AND digest=?",
                                 (row["run_id"], row["digest"]))
        for row in store.db.execute("SELECT id FROM gc_runs WHERE status IN ('applying','interrupted')"):
            if not store.db.execute("SELECT 1 FROM gc_candidates WHERE run_id=? AND status NOT IN ('restored','removed') LIMIT 1", (row["id"],)).fetchone():
                with store.db:
                    store.db.execute("UPDATE gc_runs SET status='recovered' WHERE id=?", (row["id"],))
    return {"status": "blocked" if blocked else "recovered",
            "restored_count": len(restored), "blocked_count": len(blocked)}
