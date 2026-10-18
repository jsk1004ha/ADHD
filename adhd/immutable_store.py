"""Opt-in local sharing of completed, immutable textual run evidence.

Existing raw/gzip archives remain authoritative and are never changed here.
Authority comes from the current workspace policy, never request metadata.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile

from filelock import FileLock

from .core import store
from .memory import SECRET, namespace
from .immutable_policy import authorize
from .maintenance_budget import MaintenanceBudget


SCHEMA = 2
CHUNK = 1024 * 1024
MAX_OBJECT = 64 * CHUNK
PRIVACY = frozenset({"public", "private", "restricted", "sensitive"})
KINDS = frozenset({"log", "tool_result"})
RETENTION = frozenset({"normal", "protected", "ephemeral"})
TEXT_SUFFIXES = frozenset({".txt", ".log", ".json", ".jsonl"})
DENIED_NAMES = frozenset({"state.json", "auth.json", "config.toml", "credentials.json",
                          "secrets.json", ".env", ".npmrc", ".pypirc"})
RUN = re.compile(r"^[0-9a-f]{32}$")
KEY = re.compile(r"^[0-9a-f]{24}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                              getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _contained(path: Path, root: Path) -> bool:
    if ".." in path.parts or ".." in root.parts:
        return False
    try:
        path_value = os.path.normcase(os.path.normpath(str(path.absolute())))
        root_value = os.path.normcase(os.path.normpath(str(root.absolute())))
        return os.path.commonpath((path_value, root_value)) == root_value
    except ValueError:
        return False


def _safe_path(path: Path, root: Path, *, existing: bool = True) -> Path:
    path = path.expanduser().absolute()
    root = root.expanduser().absolute()
    if not _contained(path, root):
        raise ValueError("path-outside-authorized-root")
    for component in (path, *path.parents):
        if _link(component):
            raise ValueError("link-or-reparse-point-excluded")
    if not _contained(path.resolve(strict=False), root.resolve(strict=False)):
        raise ValueError("path-outside-authorized-root")
    if existing and not path.is_file():
        raise ValueError("source-file-required")
    return path


def _safe_destination(path: Path) -> Path:
    path = path.expanduser().absolute()
    if not path.parent.is_dir() or any(_link(component) for component in (path, *path.parents)):
        raise ValueError("destination-link-or-parent-excluded")
    return path


def _hash_file(path: Path, budget: MaintenanceBudget | None = None) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            sha.update(chunk)
            size += len(chunk)
            if budget is not None:
                budget.consume(read=len(chunk))
    return sha.hexdigest(), size


def _access(metadata: dict, access: dict, *, action: str = "read") -> None:
    if not isinstance(access, dict) or not isinstance(access.get("workspace"), Path):
        raise ValueError("trusted-current-workspace-required")
    workspace = access["workspace"].expanduser().resolve(strict=True)
    if (str(workspace) != metadata["workspace"] or namespace(workspace) != metadata["scope"]
            or access.get("policy_revision") != metadata["policy_revision"]
            or not isinstance(access.get("allowed_privacy"), (list, tuple))):
        raise PermissionError("reference-outside-current-scope-or-policy")
    authorize(workspace, action=action, expected_revision=metadata["policy_revision"],
              privacy=metadata["privacy"], requested_privacy=access["allowed_privacy"])


class ImmutableStore:
    def __init__(self, root: Path | None = None, *, native_root: Path | None = None):
        self.root = (root or store() / "immutable-v2").expanduser().absolute()
        self.native_root = (native_root or store() / "native").expanduser().absolute()
        for component in (self.root, *self.root.parents):
            if _link(component):
                raise ValueError("unsafe-store-root")
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("objects", "tmp", "quarantine"):
            child = self.root / name
            if _link(child):
                raise ValueError("unsafe-store-directory")
            child.mkdir(exist_ok=True)
        self.path = self.root / "refs.sqlite3"
        if _link(self.path) or _link(self.root / "writer.lock"):
            raise ValueError("unsafe-store-metadata-or-lock")
        fresh = not self.path.exists()
        self.lock = FileLock(str(self.root / "writer.lock"), timeout=10)
        self.db = sqlite3.connect(self.path, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=2000")
        self.db.execute("PRAGMA cache_size=-8192")
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS refs (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, size INTEGER NOT NULL,
                    metadata TEXT NOT NULL, forgotten INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS refs_digest ON refs(digest);
                INSERT OR IGNORE INTO meta VALUES ('generation',0);
                CREATE TABLE IF NOT EXISTS gc_roots (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_key TEXT NOT NULL,
                    owner_run TEXT NOT NULL, state_hash TEXT NOT NULL,
                    UNIQUE(workspace,owner_key,owner_run));
                CREATE TABLE IF NOT EXISTS gc_births (
                    digest TEXT PRIMARY KEY, root_id TEXT, managed INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS gc_pins (
                    id TEXT PRIMARY KEY, ref_id TEXT NOT NULL, digest TEXT NOT NULL,
                    kind TEXT NOT NULL, manifest_path TEXT, confirmed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS gc_runs (
                    id TEXT PRIMARY KEY, generation INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS gc_candidates (
                    run_id TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
                    root_id TEXT NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY(run_id,digest));
            """)
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('gc_contract',?)", (1 if fresh else 0,))

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> ImmutableStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _owner(self, metadata: dict) -> tuple[Path, Path]:
        key, run = metadata.get("owner_key"), metadata.get("owner_run")
        if not isinstance(key, str) or not KEY.fullmatch(key) or not isinstance(run, str) or not RUN.fullmatch(run):
            raise ValueError("native-owner-key-and-run-required")
        path = _safe_path(self.native_root / key / "state.json", self.native_root)
        if path.stat().st_size > 2 * CHUNK:
            raise ValueError("owner-state-too-large")
        state = json.loads(path.read_text(encoding="utf-8"))
        workspace = Path(metadata["workspace"]).expanduser().resolve(strict=True)
        if (state.get("schema_version") != 2 or state.get("key") != key
                or state.get("run_id") != run or state.get("status") != "complete"
                or Path(state.get("workspace", "")).expanduser().resolve() != workspace
                or state.get("pending_turn_ids")
                or any(child.get("status") != "finished" for child in state.get("children", {}).values())):
            raise ValueError("authoritative-owner-not-complete")
        return workspace, path.parent

    def _metadata(self, metadata: dict) -> tuple[dict, Path]:
        required = {"workspace", "scope", "owner_key", "owner_run", "kind", "privacy",
                    "policy_revision", "provenance", "retention"}
        if not isinstance(metadata, dict) or set(metadata) != required:
            raise ValueError("invalid-reference-metadata")
        if (metadata["kind"] not in KINDS or metadata["privacy"] not in PRIVACY
                or metadata["retention"] not in RETENTION
                or not isinstance(metadata["policy_revision"], str) or not metadata["policy_revision"]
                or not isinstance(metadata["provenance"], dict)):
            raise ValueError("invalid-reference-policy-or-provenance")
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        if len(encoded) > 4096 or SECRET.search(encoded):
            raise ValueError("metadata-secret-or-size-rejected")
        workspace, owner_dir = self._owner(metadata)
        if metadata["scope"] != namespace(workspace):
            raise ValueError("owner-scope-mismatch")
        authorize(workspace, action="capture", expected_revision=metadata["policy_revision"],
                  privacy=metadata["privacy"])
        return metadata, owner_dir

    def _blob(self, digest: str) -> Path:
        if not DIGEST.fullmatch(digest):
            raise ValueError("invalid-blob-digest")
        path = self.root / "objects" / digest[:2] / digest
        _safe_path(path, self.root, existing=False)
        return path

    def put(self, source: Path, metadata: dict, *, budget: MaintenanceBudget | None = None) -> dict:
        metadata, owner_dir = self._metadata(metadata)
        budget = budget if budget is not None else MaintenanceBudget()
        workspace = Path(metadata["workspace"])
        source = Path(source).expanduser().absolute()
        run_dir = workspace / ".adhd" / metadata["owner_run"]
        workspace_source = source.parent == run_dir and _contained(source, run_dir)
        native_source = (source.parent == owner_dir and
                         source.name == "completion-" + metadata["owner_run"] + ".json")
        if not (workspace_source or native_source):
            raise ValueError("source-outside-completed-run")
        _safe_path(source, workspace if workspace_source else self.native_root)
        name = source.name.casefold()
        if (name in DENIED_NAMES or name.startswith(".env.") or any(value in name for value in ("secret", "token", "credential", "password", "auth"))
                or source.suffix.casefold() not in TEXT_SUFFIXES):
            raise ValueError("source-name-or-type-excluded")
        before = source.stat()
        if before.st_size > MAX_OBJECT:
            raise ValueError("source-too-large")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root / "tmp", prefix="pending-", delete=False) as output:
                temporary = Path(output.name)
                sha = hashlib.sha256()
                size = 0
                tail = ""
                decoder = codecs.getincrementaldecoder("utf-8")()
                with source.open("rb") as input_file:
                    while chunk := input_file.read(CHUNK):
                        size += len(chunk)
                        if size > MAX_OBJECT or b"\x00" in chunk:
                            raise ValueError("nontext-or-oversized-source")
                        current = tail + decoder.decode(chunk)
                        if SECRET.search(current) or re.search(r"Authorization:\s*Bearer\s+\S+", current, re.I):
                            raise ValueError("known-secret-rejected")
                        tail = current[-1024:]
                        sha.update(chunk)
                        output.write(chunk)
                        budget.consume(read=len(chunk), write=len(chunk))
                    decoder.decode(b"", final=True)
                    budget.check()
                output.flush()
                os.fsync(output.fileno())
            after = source.stat()
            digest = sha.hexdigest()
            if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
                    (after.st_size, after.st_mtime_ns, after.st_ino)
                    or _hash_file(source, budget) != (digest, size)
                    or _hash_file(temporary, budget) != (digest, size)):
                raise ValueError("source-changed-or-copy-incomplete")
            from .immutable_gc import _guards
            owner = {"owner_key": metadata["owner_key"], "workspace": metadata["workspace"]}
            with _guards(self, [owner]):
                self._owner(metadata)  # Terminal state can change while the source is copied.
                authorize(workspace, action="capture", expected_revision=metadata["policy_revision"],
                          privacy=metadata["privacy"])
                state_path = owner_dir / "state.json"
                state_hash = _hash_file(state_path, budget)[0]
                self._owner(metadata)
                if _hash_file(state_path, budget)[0] != state_hash:
                    raise ValueError("native-owner-changed-during-registration")
                root = self.db.execute(
                    "SELECT id FROM gc_roots WHERE workspace=? AND owner_key=? AND owner_run=?",
                    (metadata["workspace"], metadata["owner_key"], metadata["owner_run"])).fetchone()
                managed_root = root["id"] if root and self.db.execute(
                    "SELECT value FROM meta WHERE key='gc_contract'").fetchone()[0] == 1 else None
                blob = self._blob(digest)
                blob.parent.mkdir(exist_ok=True)
                created_blob = False
                if blob.exists():
                    if _hash_file(blob, budget) != (digest, size):
                        raise ValueError("existing-blob-corrupt")
                    if self.db.execute("SELECT 1 FROM gc_births WHERE digest=?",
                                       (digest,)).fetchone() is None:
                        raise ValueError("existing-blob-ownership-unclassified")
                else:
                    os.replace(temporary, blob)
                    temporary = None
                    created_blob = True
                ref_id = uuid.uuid4().hex
                stored = {**metadata, "source_name": source.name, "source_hash": digest}
                stored["owner_receipt"] = {"schema_version": 1, "owner_key": metadata["owner_key"],
                                           "owner_run": metadata["owner_run"],
                                           "workspace": metadata["workspace"], "state_hash": state_hash,
                                           "terminal_status": "complete"}
                if managed_root:
                    stored["gc_root_id"] = managed_root
                    stored["source_origin"] = "workspace" if _contained(source, workspace) else "native"
                    stored["source_relative"] = str(source.relative_to(
                        workspace if stored["source_origin"] == "workspace" else self.native_root))
                try:
                    with self.db:
                        if created_blob:
                            self.db.execute("INSERT INTO gc_births VALUES (?,?,?)",
                                            (digest, managed_root, int(bool(managed_root))))
                        self.db.execute("INSERT INTO refs VALUES (?,?,?,?,0,?)",
                                        (ref_id, digest, size, json.dumps(stored, ensure_ascii=False), time.time()))
                        self.db.execute("UPDATE meta SET value=value+1 WHERE key='generation'")
                except sqlite3.Error:
                    # The CAS guard still excludes other publishers. Only remove
                    # this call's new blob after rollback proves no owner exists.
                    # Ambiguous storage failures stay protected for recovery.
                    if created_blob:
                        try:
                            self.db.rollback()
                            referenced = self.db.execute(
                                "SELECT 1 FROM refs WHERE digest=? LIMIT 1", (digest,)).fetchone()
                            born = self.db.execute(
                                "SELECT 1 FROM gc_births WHERE digest=?", (digest,)).fetchone()
                            if not referenced and not born and _hash_file(blob) == (digest, size):
                                blob.unlink()
                        except (sqlite3.Error, OSError, ValueError):
                            pass
                    raise
                return {"ref_id": ref_id, "hash": digest, "bytes": size, "schema_version": SCHEMA,
                        "original_preserved": True}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _reference(self, ref_id: str, access: dict, *, action: str = "read",
                   budget: MaintenanceBudget | None = None) -> tuple[sqlite3.Row, dict, Path]:
        if not isinstance(ref_id, str) or not RUN.fullmatch(ref_id):
            raise ValueError("reference-id-required")
        row = self.db.execute("SELECT * FROM refs WHERE id=? AND forgotten=0", (ref_id,)).fetchone()
        if row is None:
            raise ValueError("reference-unavailable")
        metadata = json.loads(row["metadata"])
        _access(metadata, access, action=action)
        self._check_owner_receipt(metadata)
        blob = self._blob(row["digest"])
        if not blob.is_file() or _hash_file(blob, budget) != (row["digest"], row["size"]):
            raise ValueError("blob-missing-or-corrupt")
        return row, metadata, blob

    def _check_owner_receipt(self, metadata: dict) -> None:
        receipt = metadata.get("owner_receipt")
        if receipt is None:
            self._owner(metadata)  # Previously stored refs retain their original read rule.
            return
        if (not isinstance(receipt, dict) or set(receipt) != {"schema_version", "owner_key",
                "owner_run", "workspace", "state_hash", "terminal_status"}
                or receipt["schema_version"] != 1 or receipt["terminal_status"] != "complete"
                or receipt["owner_key"] != metadata["owner_key"]
                or receipt["owner_run"] != metadata["owner_run"]
                or receipt["workspace"] != metadata["workspace"]
                or not isinstance(receipt["state_hash"], str)
                or not DIGEST.fullmatch(receipt["state_hash"])):
            raise ValueError("invalid-stored-owner-receipt")

    def read(self, ref_id: str, access: dict, *, offset: int = 0, limit: int = CHUNK,
             budget: MaintenanceBudget | None = None) -> bytes:
        if not 0 <= offset or not 1 <= limit <= CHUNK:
            raise ValueError("invalid-read-range")
        budget = budget if budget is not None else MaintenanceBudget()
        with self.lock:
            row, metadata, blob = self._reference(ref_id, access, budget=budget)
            if offset >= row["size"]:
                return b""
            with blob.open("rb") as source:
                source.seek(offset)
                data = source.read(limit)
            budget.consume(read=len(data))
            _access(metadata, access)
            return data

    def forget(self, ref_id: str, access: dict, *, budget: MaintenanceBudget | None = None) -> dict:
        budget = budget if budget is not None else MaintenanceBudget()
        with self.lock:
            row, metadata, _ = self._reference(ref_id, access, action="forget", budget=budget)
            _access(metadata, access, action="forget")
            with self.db:
                self.db.execute("UPDATE refs SET forgotten=1 WHERE id=?", (ref_id,))
                self.db.execute("UPDATE meta SET value=value+1 WHERE key='generation'")
        return {"ref_id": ref_id, "forgotten": True, "blob_preserved": True}

    def export(self, ref_id: str, access: dict, destination: Path,
               *, budget: MaintenanceBudget | None = None) -> dict:
        budget = budget if budget is not None else MaintenanceBudget()
        with self.lock:
            row, metadata, blob = self._reference(ref_id, access, action="export", budget=budget)
            destination = _safe_destination(Path(destination))
            if destination.exists() or _contained(destination, self.root):
                raise ValueError("export-destination-unsafe-or-exists")
            manifest = {"schema_version": SCHEMA, "ref_id": ref_id, "hash": row["digest"],
                        "bytes": row["size"], "metadata": metadata}
            manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("manifest.json", manifest_bytes)
                    budget.consume(write=len(manifest_bytes))
                    with blob.open("rb") as source, archive.open("content", "w") as output:
                        while chunk := source.read(CHUNK):
                            output.write(chunk)
                            budget.consume(read=len(chunk), write=len(chunk))
                with zipfile.ZipFile(temporary) as archive:
                    if archive.read("manifest.json") != manifest_bytes:
                        raise ValueError("export-verification-failed")
                    budget.consume(read=len(manifest_bytes))
                    sha = hashlib.sha256()
                    size = 0
                    with archive.open("content") as source:
                        while chunk := source.read(CHUNK):
                            sha.update(chunk)
                            size += len(chunk)
                            budget.consume(read=len(chunk))
                    if (sha.hexdigest(), size) != (row["digest"], row["size"]):
                        raise ValueError("export-verification-failed")
                _access(metadata, access, action="export")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return {"path": str(destination), "ref_id": ref_id, "hash": row["digest"], "bytes": row["size"]}

    @staticmethod
    def restore(export_path: Path, destination: Path, access: dict,
                *, budget: MaintenanceBudget | None = None) -> dict:
        """Restore exact bytes from a standalone export even if the store is disabled."""
        budget = budget if budget is not None else MaintenanceBudget()
        destination = _safe_destination(Path(destination))
        if destination.exists():
            raise ValueError("restore-will-not-overwrite")
        if Path(export_path).stat().st_size > MAX_OBJECT + CHUNK:
            raise ValueError("export-archive-too-large")
        with zipfile.ZipFile(export_path) as archive:
            if set(archive.namelist()) != {"manifest.json", "content"}:
                raise ValueError("invalid-export-members")
            if (archive.getinfo("manifest.json").file_size > 65536
                    or archive.getinfo("content").file_size > MAX_OBJECT):
                raise ValueError("export-member-too-large")
            with archive.open("manifest.json") as source:
                raw_manifest = source.read(65537)
            budget.consume(read=len(raw_manifest))
            if len(raw_manifest) > 65536:
                raise ValueError("export-manifest-too-large")
            manifest = json.loads(raw_manifest)
            if manifest.get("schema_version") != SCHEMA or not DIGEST.fullmatch(manifest.get("hash", "")):
                raise ValueError("invalid-export-manifest")
            metadata = manifest["metadata"]
            _access(metadata, access, action="restore")
            if destination.name != metadata["source_name"]:
                raise ValueError("original-name-required")
            temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with archive.open("content") as source, temporary.open("xb") as output:
                    sha = hashlib.sha256()
                    size = 0
                    while chunk := source.read(CHUNK):
                        size += len(chunk)
                        if size > MAX_OBJECT:
                            raise ValueError("export-content-too-large")
                        sha.update(chunk)
                        output.write(chunk)
                        budget.consume(read=len(chunk), write=len(chunk))
                    output.flush()
                    os.fsync(output.fileno())
                if (sha.hexdigest(), size) != (manifest["hash"], manifest["bytes"]):
                    raise ValueError("export-content-corrupt")
                _access(metadata, access, action="restore")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return {"path": str(destination), "hash": manifest["hash"], "bytes": manifest["bytes"]}

    def audit(self) -> dict:
        try:
            return self._audit()
        except OSError:
            raise ValueError("object-store-filesystem-unavailable") from None

    def _audit(self) -> dict:
        refs, forgotten, logical = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(forgotten),0),"
            "COALESCE(SUM(CASE WHEN forgotten=0 THEN size ELSE 0 END),0) FROM refs"
        ).fetchone()
        object_count = 0
        object_bytes = 0
        orphan_count = 0
        for folder in (self.root / "objects").iterdir():
            if _link(folder) or not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", folder.name):
                raise ValueError("unknown-object-directory")
            for path in folder.iterdir():
                if _link(path) or not path.is_file() or not DIGEST.fullmatch(path.name):
                    raise ValueError("unknown-object-entry")
                object_count += 1
                object_bytes += path.stat().st_size
                if self.db.execute("SELECT 1 FROM refs WHERE digest=? AND forgotten=0 LIMIT 1",
                                   (path.name,)).fetchone() is None:
                    orphan_count += 1
        physical = object_bytes + self.path.stat().st_size
        generation = self.db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        return {"schema_version": SCHEMA, "generation": generation,
                "refs": refs, "forgotten_refs": forgotten,
                "objects": object_count, "orphan_count": orphan_count,
                "orphan_hashes": [], "orphan_list_truncated": orphan_count > 0,
                "logical_bytes": logical, "physical_bytes_including_metadata": physical,
                "pending_objects": len(tuple((self.root / "tmp").iterdir())),
                "net_logical_savings_bytes": logical - physical, "reclaimed_disk_bytes": None}

    def gc(self, *, dry_run: bool = True) -> dict:
        from .immutable_gc import plan
        if not dry_run:
            return {**self.audit(), "dry_run": False, "status": "blocked",
                    "reason": "gc_apply-requires-frozen-operation-id", "removed": []}
        return plan(self)

    def register_root(self, workspace: Path, owner_key: str, owner_run: str) -> str:
        from .immutable_gc import register_root
        return register_root(self, workspace, owner_key, owner_run)

    def pin(self, ref_id: str, access: dict, kind: str = "local_manifest") -> dict:
        from .immutable_gc import pin
        return pin(self, ref_id, access, kind)

    def confirm_pin(self, pin_id: str, access: dict) -> dict:
        from .immutable_gc import confirm_pin
        return confirm_pin(self, pin_id, access)

    def release_pin(self, pin_id: str, access: dict) -> dict:
        from .immutable_gc import release_pin
        return release_pin(self, pin_id, access)

    def gc_apply(self, operation_id: str) -> dict:
        from .immutable_gc import apply
        try:
            return apply(self, operation_id)
        except OSError:
            raise ValueError("gc-filesystem-unavailable") from None

    def gc_recover(self) -> dict:
        from .immutable_gc import recover
        try:
            return recover(self)
        except OSError:
            raise ValueError("gc-filesystem-unavailable") from None
