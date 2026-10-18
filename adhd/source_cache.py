"""Opt-in, local-only reuse of pinned committed source bytes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

from filelock import FileLock

from .core import store
from .maintenance_budget import MaintenanceBudget


MAX_BLOB = 64 * 1024 * 1024
CHUNK = 1024 * 1024
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
DENIED = frozenset({".git", ".adhd", ".env", "__pycache__", "node_modules", "auth.json",
                    "credentials.json", "secrets.json", "id_rsa", "id_ed25519"})


def _git(repo: Path, *args: str, max_output: int = 4096) -> bytes:
    env = os.environ.copy()
    env.update({"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.allow",
                "GIT_CONFIG_VALUE_0": "never"})
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(["git", "-C", str(repo), *args], stdout=output,
                                    stderr=subprocess.DEVNULL, env=env, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("pinned-local-git-object-unavailable") from error
        if result.returncode:
            raise ValueError("pinned-local-git-object-unavailable")
        if output.tell() > max_output:
            raise ValueError("git-output-too-large")
        output.seek(0)
        return output.read(max_output)


def _path(value: str) -> str:
    path = Path(value)
    if (not value or path.is_absolute() or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} or part.casefold() in DENIED or part.casefold().startswith(".env.")
                   for part in value.split("/"))
            or any(word in path.name.casefold() for word in ("secret", "credential", "password", "token", "auth"))
            or path.suffix.casefold() in {".pem", ".key", ".p12"}):
        raise ValueError("source-path-excluded")
    return value


def _hash(path: Path, budget: MaintenanceBudget | None = None) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            sha.update(chunk)
            if budget is not None:
                budget.consume(read=len(chunk))
    return sha.hexdigest()


def _link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                              getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _no_links(path: Path, root: Path) -> None:
    for component in (path, *path.parents):
        if _link(component):
            raise ValueError("cached-source-link-excluded")
        if component == root:
            return
    raise ValueError("cached-source-outside-root")


class SourceCache:
    def __init__(self, root: Path | None = None):
        self.root = (root or store() / "source-cache-v1").expanduser().absolute()
        if any(_link(component) for component in (self.root, *self.root.parents)):
            raise ValueError("unsafe-cache-root")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.root / "writer.lock"), timeout=10)

    def snapshot(self, repository: Path, remote: str, commit: str, paths: list[str],
                 *, budget: MaintenanceBudget | None = None) -> dict:
        budget = budget if budget is not None else MaintenanceBudget()
        repository = Path(repository).expanduser().resolve(strict=True)
        if (not isinstance(remote, str) or not remote.strip() or len(remote) > 2048
                or not COMMIT.fullmatch(commit) or not isinstance(paths, list)
                or not 1 <= len(paths) <= 100 or len(set(paths)) != len(paths)):
            raise ValueError("pinned-remote-commit-and-paths-required")
        if "://" in remote:
            parsed_remote = urlsplit(remote)
            if (parsed_remote.query or parsed_remote.fragment or parsed_remote.password is not None
                    or parsed_remote.scheme in {"http", "https"} and parsed_remote.username is not None):
                raise ValueError("credential-bearing-remote-excluded")
        paths = sorted(_path(path) for path in paths)
        if Path(os.fsdecode(_git(repository, "rev-parse", "--show-toplevel")).strip()).resolve() != repository:
            raise ValueError("repository-root-required")
        if os.fsdecode(_git(repository, "remote", "get-url", "origin")).strip() != remote:
            raise ValueError("remote-identity-mismatch")
        if os.fsdecode(_git(repository, "rev-parse", "--verify", commit + "^{commit}")).strip() != commit:
            raise ValueError("commit-not-pinned-or-unavailable")
        # These are independent Git features, recorded separately.
        try:
            partial_filter = os.fsdecode(_git(repository, "config", "--get", "remote.origin.partialclonefilter")).strip()
        except ValueError:
            partial_filter = ""
        try:
            sparse_checkout = os.fsdecode(_git(repository, "config", "--get", "core.sparseCheckout")).strip().lower() == "true"
        except ValueError:
            sparse_checkout = False
        identity = {"remote": remote, "commit": commit, "paths": paths,
                    "partial_clone_filter": partial_filter or None, "sparse_checkout": sparse_checkout}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        target = self.root / key
        with self.lock:
            budget.check()
            _no_links(target, self.root)
            if target.exists():
                manifest_path = target / "manifest.json"
                _no_links(manifest_path, self.root)
                if not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
                    raise ValueError("cached-source-corrupt")
                with manifest_path.open("rb") as source:
                    raw_manifest = source.read(65537)
                budget.consume(read=len(raw_manifest))
                if len(raw_manifest) > 65536:
                    raise ValueError("cached-source-corrupt")
                manifest = json.loads(raw_manifest.decode("utf-8"))
                if (set(manifest) != {"schema_version", "identity", "hashes"}
                        or manifest["schema_version"] != 1 or manifest["identity"] != identity
                        or not isinstance(manifest["hashes"], dict)
                        or set(manifest["hashes"]) != set(paths)):
                    raise ValueError("cached-source-corrupt")
                expected_dirs = set()
                for path in paths:
                    parts = Path(path).parts
                    expected_dirs.update(Path(*parts[:index]).as_posix()
                                         for index in range(1, len(parts)))
                files_root = target / "files"
                _no_links(files_root, self.root)
                if not files_root.is_dir():
                    raise ValueError("cached-source-corrupt")
                pending = [files_root]
                actual = set()
                while pending:
                    for entry in pending.pop().iterdir():
                        _no_links(entry, self.root)
                        relative = entry.relative_to(files_root).as_posix()
                        if entry.is_dir() and relative in expected_dirs:
                            pending.append(entry)
                        elif entry.is_file() and relative in paths:
                            actual.add(relative)
                        else:
                            raise ValueError("cached-source-unexpected-entry")
                if actual != set(paths):
                    raise ValueError("cached-source-corrupt")
                for path in paths:
                    budget.check()
                    cached = target / "files" / path
                    _no_links(cached, self.root)
                    if (not cached.is_file() or cached.stat().st_size > MAX_BLOB
                            or not isinstance(manifest["hashes"][path], str)
                            or not DIGEST.fullmatch(manifest["hashes"][path])
                            or _hash(cached, budget) != manifest["hashes"][path]):
                        raise ValueError("cached-source-corrupt")
                return {"path": str(target), "reused": True, **manifest}
            temporary = Path(tempfile.mkdtemp(prefix="pending-", dir=self.root))
            try:
                hashes = {}
                for path in paths:
                    budget.check()
                    entry = _git(repository, "ls-tree", "-z", commit, "--", path)
                    if not entry.startswith((b"100644 blob ", b"100755 blob ")) or not entry.endswith(b"\t" + os.fsencode(path) + b"\0"):
                        raise ValueError("source-not-regular-committed-file")
                    output = temporary / "files" / path
                    output.parent.mkdir(parents=True, exist_ok=True)
                    size = int(_git(repository, "cat-file", "-s", commit + ":" + path).strip())
                    if size > MAX_BLOB:
                        raise ValueError("source-blob-too-large")
                    budget.ensure(read=size, write=size)
                    env = os.environ.copy()
                    env.update({"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
                                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.allow",
                                "GIT_CONFIG_VALUE_0": "never"})
                    try:
                        with output.open("xb") as destination:
                            result = subprocess.run(["git", "-C", str(repository), "show", commit + ":" + path],
                                                    stdout=destination, stderr=subprocess.DEVNULL, env=env,
                                                    timeout=20, check=False)
                        if result.returncode or output.stat().st_size != size:
                            raise ValueError("source-blob-unavailable")
                    except (OSError, subprocess.TimeoutExpired) as error:
                        raise ValueError("source-blob-unavailable") from error
                    budget.consume(write=output.stat().st_size)
                    hashes[path] = _hash(output, budget)
                manifest = {"schema_version": 1, "identity": identity, "hashes": hashes}
                manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
                (temporary / "manifest.json").write_bytes(manifest_bytes)
                budget.consume(write=len(manifest_bytes))
                os.replace(temporary, target)
                return {"path": str(target), "reused": False, **manifest}
            finally:
                if temporary.exists() and temporary.parent == self.root and not temporary.is_symlink():
                    shutil.rmtree(temporary)
