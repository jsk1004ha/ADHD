"""Conservative project inventory, reference audit and evidence archive adapter.

Archive bytes and authority checks belong to adhd.evidence_archive. This module
never interprets a reported compression ratio as reclaimed filesystem space.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import types

from .core import file_hash
from .experience_procedures import _link, _path, _root


_SHA = re.compile(r'[0-9a-f]{64}\Z')
_COMMIT = re.compile(r'[0-9a-f]{40}\Z')
_RECEIPT = re.compile(r'\.adhd/checks/[0-9a-f]{32}/receipt\.json\Z')
_MANIFEST = re.compile(r'\.adhd/evidence-archives/[0-9a-f]{32}/manifest\.json\Z')
_RUN = re.compile(r'[0-9a-f]{32}\Z')
_BACKEND_ROOT = Path(__file__).with_name('obsidian_assets') / 'evidence'


def _git(repository: Path, *args: str, bytes_output: bool = False) -> str | bytes:
    result = subprocess.run(['git', '-C', str(repository), *args],
                            capture_output=True, check=False, timeout=20)
    if result.returncode:
        raise ValueError('Git reference is unavailable: ' + result.stderr.decode('utf-8', 'replace')[:240])
    return result.stdout if bytes_output else result.stdout.decode('utf-8', 'replace').strip()


def _reference(workspace: Path, value: dict) -> dict:
    if not isinstance(value, dict) or value.get('kind') not in {'code', 'document'}:
        raise ValueError('Reference needs code or document kind')
    path = value.get('path')
    target = _path(workspace, path)
    sha = value.get('sha256')
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ValueError('Reference needs an exact SHA-256')
    if value['kind'] == 'document':
        if set(value) != {'kind', 'path', 'sha256'}:
            raise ValueError('Document reference has unknown fields')
        if not target.is_file():
            return {'reference': value, 'status': 'missing'}
        return {'reference': value, 'status': 'valid' if file_hash(target) == sha else 'stale'}
    required = {'kind', 'path', 'sha256', 'repository', 'commit'}
    if set(value) not in (required, required | {'worktree_sha256'}):
        raise ValueError('Code reference has unknown fields')
    commit = value['commit']
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ValueError('Code reference needs a full commit hash')
    repository = workspace if value['repository'] == '.' else _path(workspace, value['repository'])
    if not repository.is_dir():
        return {'reference': value, 'status': 'missing'}
    try:
        actual_root = Path(_git(repository, 'rev-parse', '--show-toplevel')).resolve()
        if actual_root != repository.resolve():
            raise ValueError('Repository reference does not name its Git root')
        member = target.relative_to(repository).as_posix()
        if member.startswith('.git/'):
            raise ValueError('Git internals are not reusable code')
        if not _git(repository, 'branch', '-a', '--contains', commit, '--format=%(refname)'):
            return {'reference': value, 'status': 'missing', 'reason': 'commit is unreachable'}
        size = int(_git(repository, 'cat-file', '-s', f'{commit}:{member}'))
        if size > 128 * 1024 * 1024:
            raise ValueError('Code reference exceeds the audit size limit')
        committed = _git(repository, 'show', f'{commit}:{member}', bytes_output=True)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        return {'reference': value, 'status': 'missing', 'reason': str(error)}
    if hashlib.sha256(committed).hexdigest() != sha:
        return {'reference': value, 'status': 'stale', 'reason': 'committed bytes differ from declared hash'}
    if 'worktree_sha256' in value:
        working = value['worktree_sha256']
        if not isinstance(working, str) or not _SHA.fullmatch(working) or not target.is_file() or file_hash(target) != working:
            return {'reference': value, 'status': 'stale', 'reason': 'approved working bytes changed'}
        normalized = _git(repository, 'hash-object', '--path=' + member, str(target))
        if normalized != _git(repository, 'rev-parse', f'{commit}:{member}'):
            return {'reference': value, 'status': 'stale', 'reason': 'approved working bytes do not match Git content'}
    return {'reference': value, 'status': 'valid', 'committed_bytes': size}


def audit_references(workspace: Path, references: list[dict] | None = None) -> dict:
    """Audit immutable code commits and document bytes; never repair silently."""
    workspace = _root(workspace)
    if references is None:
        path = _path(workspace, '.adhd/experience/procedures.json')
        if path.is_file():
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('Procedure ledger exceeds audit limit')
            ledger = json.loads(path.read_text(encoding='utf-8-sig'))
            if ledger.get('workspace') != str(workspace) or not isinstance(ledger.get('proposals'), dict):
                raise ValueError('Invalid procedure ledger')
            references = [row['artifact'] for row in ledger['proposals'].values()]
        else:
            references = []
    if not isinstance(references, list) or len(references) > 500:
        raise ValueError('Reference audit needs at most 500 entries')
    rows = [_reference(workspace, value) for value in references]
    return {'valid': all(row['status'] == 'valid' for row in rows), 'checked': len(rows),
            'rows': rows, 'missing': [row for row in rows if row['status'] == 'missing'],
            'stale': [row for row in rows if row['status'] == 'stale']}


def _visible_notes(workspace: Path) -> tuple[list[Path], list[str]]:
    config = _path(workspace, '.adhd/obsidian.json')
    if not config.is_file():
        return [], []
    if config.stat().st_size > 32_000:
        raise ValueError('Obsidian configuration is oversized')
    data = json.loads(config.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('enabled') is not True:
        return [], []
    try:
        from .obsidian import load_config
        from .obsidian_index import live_rows, safe_path
    except ImportError as error:
        raise RuntimeError('Obsidian policy/index module is required for canonical note counts') from error
    policy = load_config(workspace, required=True)
    if not policy['enabled']:
        return [], []
    index = _path(workspace, '.adhd/obsidian-index.sqlite3')
    if not index.is_file():
        return [], ['obsidian-index-missing']
    vault = Path(policy['vault'])
    with sqlite3.connect(index.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows, warnings = live_rows(db, policy)
    paths = []
    for row in rows:
        path = safe_path(vault, row['path'])
        if file_hash(path) == row['digest']:
            paths.append(path)
        else:
            warnings.append('obsidian-index-stale:' + row['path'])
    return paths, sorted(set(warnings))


def _walk(root: Path):
    if _link(root):
        raise ValueError('Inventory root is a link or reparse point')
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in [*dirs, *files]:
            if _link(base / name):
                raise ValueError('Inventory contains a link or reparse point')
        dirs[:] = [name for name in dirs if name not in {'.git', '.obsidian', 'node_modules', '.venv'}]
        for name in files:
            path = base / name
            if not path.is_file():
                raise ValueError('Inventory contains a non-file entry')
            count += 1
            if count > 50_000:
                raise ValueError('Inventory exceeds its file limit')
            yield path


def inventory(workspace: Path) -> dict:
    """Count logical file bytes by class, with hash duplicates but no deletion."""
    workspace = _root(workspace)
    categories = {name: {'files': 0, 'bytes': 0} for name in
                  ('canonical_notes', 'logs', 'indexes', 'backups', 'other_evidence')}
    hashes: dict[str, list[str]] = {}

    def add(path: Path, category: str, label: str) -> None:
        stat_result = path.stat()
        categories[category]['files'] += 1
        categories[category]['bytes'] += stat_result.st_size
        hashes.setdefault(file_hash(path), []).append(label)

    notes, warnings = _visible_notes(workspace)
    for path in notes:
        add(path, 'canonical_notes', 'vault:' + str(path))
    state_root = _path(workspace, '.adhd')
    if state_root.is_dir():
        for path in _walk(state_root):
            relative = path.relative_to(workspace).as_posix()
            parts = path.relative_to(state_root).parts
            if 'backup' in parts or 'backups' in parts or path.suffix.lower() in {'.bak', '.zip'}:
                category = 'backups'
            elif path.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.fts'}:
                category = 'indexes'
            elif path.suffix.lower() in {'.log', '.jsonl'} or 'checks' in parts:
                category = 'logs'
            else:
                category = 'other_evidence'
            add(path, category, relative)
    duplicates = [{'sha256': sha, 'paths': paths, 'logical_duplicate_bytes':
                   sum((Path(p[6:]) if p.startswith('vault:') else workspace / p).stat().st_size
                       for p in paths[1:])}
                  for sha, paths in hashes.items() if len(paths) > 1]
    return {'categories': categories, 'duplicates': duplicates,
            'logical_bytes': sum(row['bytes'] for row in categories.values()),
            'reclaimed_disk_bytes': None, 'deletion_performed': False,
            'warnings': warnings}


def _archive_engine():
    try:
        from . import evidence_archive
    except ImportError:
        # Reuse the previously verified archive/codec sources without replacing
        # the active native schema or the user's concurrent core changes.
        root = _BACKEND_ROOT.resolve()
        for component in (_BACKEND_ROOT, *_BACKEND_ROOT.parents):
            if _link(component):
                raise ValueError('Evidence backend is a reparse point')
        manifest = root / 'sources.json'
        if not manifest.is_file():
            raise RuntimeError('Reviewed evidence archive assets are missing')
        sources = json.loads(manifest.read_text(encoding='utf-8'))
        if set(sources.get('files', {})) != {'evidence_codec.py', 'evidence_archive.py'}:
            raise ValueError('Invalid evidence backend source manifest')
        for name, expected in sources['files'].items():
            source = root / name
            if _link(source) or file_hash(source) != expected:
                raise ValueError('Evidence backend source changed')
        package_name = 'adhd._obsidian_evidence_' + hashlib.sha256(str(root).encode()).hexdigest()[:12]
        package = types.ModuleType(package_name)
        package.__path__ = [str(root)]
        sys.modules[package_name] = package
        for name in ('core', 'evidence', 'leases', 'native', 'validation_batch'):
            sys.modules[package_name + '.' + name] = importlib.import_module('adhd.' + name)
        guard = types.ModuleType(package_name + '.storage')
        def no_reparse(path):
            for component in (Path(path), *Path(path).parents):
                if _link(component):
                    raise ValueError('Reparse point is not managed storage')
        guard._no_reparse = no_reparse
        sys.modules[guard.__name__] = guard
        # Drop only our private cached modules so a changed source cannot reuse
        # bytecode from a different reviewed manifest.
        for name in ('evidence_codec', 'evidence_archive'):
            sys.modules.pop(package_name + '.' + name, None)
        evidence_archive = importlib.import_module(package_name + '.evidence_archive')
    for name in ('archive_receipts', 'verify_archive', 'release_raw', 'restore_raw'):
        if not callable(getattr(evidence_archive, name, None)):
            raise RuntimeError('adhd.evidence_archive backend lacks ' + name)
    return evidence_archive


def archive(workspace: Path, payload: dict) -> dict:
    """Archive explicit closed-run check receipts using the existing engine."""
    workspace = _root(workspace)
    if not isinstance(payload, dict) or set(payload) - {'run_id', 'receipts', 'release'}:
        raise ValueError('Archive needs run_id, receipts and optional release')
    run_id, receipts = payload.get('run_id'), payload.get('receipts')
    if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
        raise ValueError('Archive needs a native run id')
    if (not isinstance(receipts, list) or not 1 <= len(receipts) <= 200
            or len(receipts) != len(set(receipts))):
        raise ValueError('Archive needs 1..200 distinct receipts')
    for receipt in receipts:
        if not isinstance(receipt, str) or not _RECEIPT.fullmatch(receipt):
            raise ValueError('Archive accepts only workspace check receipts')
        _path(workspace, receipt, file=True)
    if type(payload.get('release', False)) is not bool:
        raise ValueError('release must be an explicit boolean')
    engine = _archive_engine()
    result = engine.archive_receipts(workspace, run_id, receipts, release=False)
    manifest = result['manifest']
    if not isinstance(manifest, str) or not _MANIFEST.fullmatch(manifest):
        raise ValueError('Archive engine returned an unsafe manifest')
    _path(workspace, manifest, file=True)
    verified = engine.verify_archive(workspace, manifest)
    if verified.get('verified') is not True or verified.get('run_id') != run_id:
        raise ValueError('Archive verification failed')
    released = engine.release_raw(workspace, manifest) if payload.get('release', False) else None
    return {**result, 'verified': True, 'release': released,
            'reclaimed_disk_bytes': None, 'originals_retained': released is None}


def restore(workspace: Path, payload: dict) -> dict:
    """Restore exact raw paths/bytes through the existing verified manifest."""
    workspace = _root(workspace)
    if not isinstance(payload, dict) or set(payload) != {'manifest'}:
        raise ValueError('Restore needs only an explicit manifest')
    manifest = payload['manifest']
    if not isinstance(manifest, str) or not _MANIFEST.fullmatch(manifest):
        raise ValueError('Invalid archive manifest')
    _path(workspace, manifest, file=True)
    engine = _archive_engine()
    before = engine.verify_archive(workspace, manifest)
    if before.get('verified') is not True:
        raise ValueError('Archive is not verified')
    result = engine.restore_raw(workspace, manifest)
    after = engine.verify_archive(workspace, manifest)
    if after.get('verified') is not True or after.get('raw_present') != after.get('logs'):
        raise ValueError('Restored raw paths or bytes failed verification')
    return {**result, 'verified': True, 'reclaimed_disk_bytes': None}
