"""Immutable reader inputs. Host-enforced concurrent execution is opt-in, never assumed."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from .core import atomic_json, file_hash, read_json, store
from . import leases

MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 10000
FORBIDDEN = {'.git', '.adhd', '.omx', '.codex', '.agents', 'node_modules', '__pycache__'}
SECRET = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?i:aws_secret_access_key\s*=|(?:api_key|access_token|password)\s*[:=]\s*[\x22\x27][^\x22\x27\r\n]{12,})')


class _Blobs:
    """One bounded, deadline-limited Git reader per scan, rather than one process per file."""
    def __init__(self, workspace):
        self.errors=tempfile.TemporaryFile()
        self.proc=subprocess.Popen(['git','-C',str(workspace),'cat-file','--batch'],
                                   stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.errors)
        self.timer=threading.Timer(30,self.proc.kill)
        self.timer.daemon=True
        self.timer.start()

    def get(self, oid):
        if not re.fullmatch(r'[0-9a-f]{40,64}',oid):
            raise ValueError('Invalid object identity')
        self.proc.stdin.write(oid.encode('ascii')+b'\n')
        self.proc.stdin.flush()
        header=self.proc.stdout.readline(256).split()
        if len(header)!=3 or header[1]!=b'blob' or not header[2].isdigit():
            raise ValueError('Git blob unavailable or reader deadline expired')
        size=int(header[2])
        if size>MAX_FILE:
            raise ValueError('Snapshot file exceeds byte budget')
        data=self.proc.stdout.read(size)
        if len(data)!=size or self.proc.stdout.read(1)!=b'\n':
            raise ValueError('Incomplete Git blob')
        return data

    def close(self):
        self.timer.cancel()
        self.proc.stdin.close()
        self.proc.stdout.close()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        self.errors.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def _git(workspace: Path, *args: str, maximum: int = 8 * 1024 * 1024) -> bytes:
    # stdout is disk-backed, so a malformed repository cannot create an unbounded capture.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(['git', '-C', str(workspace), *args], stdout=output,
                                stderr=errors, timeout=30, check=False)
        if result.returncode:
            raise ValueError('Git snapshot input unavailable')
        if output.tell() > maximum:
            raise ValueError('Snapshot input exceeds byte budget')
        output.seek(0)
        return output.read(maximum + 1)


def _safe(root: Path, relative: str) -> Path:
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('Snapshot root crosses a link')
    rel = PurePosixPath(relative.replace('\\', '/'))
    if not relative or rel.is_absolute() or any(p in {'', '.', '..'} or ':' in p for p in rel.parts):
        raise ValueError('Unsafe snapshot path')
    path = root.joinpath(*rel.parts)
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise ValueError('Snapshot path crosses a link')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Snapshot path escapes root')
    return path


def _excluded(relative: str) -> str | None:
    parts = PurePosixPath(relative).parts
    name = parts[-1].lower()
    if any(part.lower() in FORBIDDEN for part in parts):
        return 'runtime_or_generated'
    if name.startswith('.env') or name in {'auth.json', 'credentials', 'credentials.json', 'secrets.json', '.npmrc', '.pypirc', 'id_rsa', 'id_ed25519'} or name.endswith(('.p12', '.pfx', '.key', '.pem')):
        return 'credential_path'
    return None


def _layout(workspace: Path, scratch: Path) -> Path:
    lexical = scratch.absolute()
    cursor = lexical
    while cursor != cursor.parent:
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise ValueError('Scratch crosses a link')
        cursor = cursor.parent
    workspace, scratch, active = workspace.resolve(), scratch.resolve(), store().resolve()
    for protected in (workspace, active):
        if scratch.is_relative_to(protected) or protected.is_relative_to(scratch):
            raise ValueError('Scratch must be outside source and active ADHD state')
    cursor = scratch
    while cursor != cursor.parent:
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise ValueError('Scratch crosses a link')
        cursor = cursor.parent
    scratch.mkdir(parents=True, exist_ok=True)
    target = scratch / ('snapshot-' + uuid.uuid4().hex)
    target.mkdir()
    return target


def _entries(workspace: Path, commit: str) -> list[tuple[str, str, str]]:
    rows = []
    for row in _git(workspace, 'ls-tree', '-rz', commit).split(b'\0'):
        if not row:
            continue
        meta, raw_path = row.split(b'\t', 1)
        mode, kind, oid = meta.decode('ascii').split()
        path = raw_path.decode('utf-8', errors='strict')
        _safe(workspace, path)
        rows.append((path, mode, oid if kind == 'blob' else ''))
        if len(rows) > MAX_FILES:
            raise ValueError('Snapshot file count exceeds budget')
    return rows


def _source(workspace: Path, commit: str, dirty: bool, untracked: list[str]) -> tuple[dict, dict, list]:
    with _Blobs(workspace) as blobs:
        return _source_with_blobs(workspace,commit,dirty,untracked,blobs)


def _source_with_blobs(workspace, commit, dirty, untracked, blobs):
    files, staged, excluded = {}, {}, []
    entries = _entries(workspace, commit)
    index = {}
    if dirty:
        for row in _git(workspace, 'ls-files', '--stage', '-z').split(b'\0'):
            if not row:
                continue
            meta, raw_path = row.split(b'\t', 1)
            mode, oid, stage = meta.decode('ascii').split()
            if stage != '0':
                raise ValueError('Resolve merge conflicts before taking a dirty snapshot')
            path = raw_path.decode('utf-8', errors='strict')
            index[path] = (mode, oid)
        entries = [(p, m, oid) for p, (m, oid) in index.items()]
        tracked = set(index)
        deleted_from_index = {p for p, _, _ in _entries(workspace, commit)} - tracked
        for path in untracked:
            if path in tracked:
                raise ValueError('Explicit untracked input is tracked')
            entries.append((path, '100644', ''))
    total = 0
    for path, mode, oid in entries:
        original = _safe(workspace, path)
        reason = _excluded(path)
        if mode not in {'100644', '100755'}:
            reason = 'link_or_submodule'
        if reason:
            excluded.append({'path': path, 'reason': reason})
            continue
        if dirty and not original.exists():
            files[path] = {'deleted': True}
        else:
            if dirty:
                if not original.is_file() or original.stat().st_size > MAX_FILE:
                    raise ValueError('Snapshot file is unavailable or too large')
                with original.open('rb') as stream:
                    data = stream.read(MAX_FILE + 1)
            else:
                data = blobs.get(oid)
            if len(data) > MAX_FILE:
                raise ValueError('Snapshot file exceeds budget')
            total += len(data)
            if total > MAX_TOTAL:
                raise ValueError('Snapshot exceeds total byte budget')
            if SECRET.search(data):
                excluded.append({'path': path, 'reason': 'secret_content'})
                continue
            files[path] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'mode': mode}
        if dirty and oid:
            staged_data = blobs.get(oid)
            total += len(staged_data)
            if total > MAX_TOTAL:
                raise ValueError('Snapshot exceeds total byte budget')
            if SECRET.search(staged_data):
                files.pop(path, None)
                excluded.append({'path': path, 'reason': 'secret_in_index'})
                continue
            staged[path] = {'sha256': hashlib.sha256(staged_data).hexdigest(), 'bytes': len(staged_data), 'oid': oid}
    if len(files) > MAX_FILES:
        raise ValueError('Snapshot file count exceeds budget')
    if dirty:
        for path in deleted_from_index:
            if not _excluded(path):
                files[path]={'deleted':True,'index_deleted':True}
    return files, staged, excluded


def _copy_inputs(workspace, dirty, before, staged, content, object_ids):
    with _Blobs(workspace) as blobs:
        for path,item in before.items():
            if item.get('deleted'):continue
            destination=_safe(content/'worktree',path)
            destination.parent.mkdir(parents=True,exist_ok=True)
            if dirty:
                with _safe(workspace,path).open('rb') as stream:data=stream.read(MAX_FILE+1)
            else:data=blobs.get(object_ids[path])
            if len(data)>MAX_FILE or SECRET.search(data):
                raise ValueError('Source changed or secret detected during snapshot copy')
            destination.write_bytes(data)
        for path,item in staged.items():
            destination=_safe(content/'index',path)
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(blobs.get(item['oid']))


def _discard(target: Path, parent: Path):
    if not target.exists():return
    if target.is_symlink() or (hasattr(target,'is_junction') and target.is_junction()):
        raise ValueError('Refuse cleanup across a changed scratch link')
    resolved=target.resolve()
    if resolved.parent!=parent.resolve() or not re.fullmatch(r'snapshot-[0-9a-f]{32}|attempt-[0-2]',target.name):
        raise ValueError('Refuse cleanup outside the owned snapshot directory')
    # Only generated copies beneath this verified absolute root are removed.
    shutil.rmtree(resolved)


def prepare(workspace: Path, *, commit: str = 'HEAD', dirty: bool = False,
            untracked: list[str] | None = None, scratch: Path | None = None,
            require_point_in_time: bool = False) -> dict:
    workspace = workspace.resolve()
    untracked = untracked or []
    if not isinstance(untracked, list) or len(untracked) > 500 or any(not isinstance(p, str) for p in untracked):
        raise ValueError('Specify at most 500 explicit untracked paths')
    head = _git(workspace, 'rev-parse', '--verify', commit + '^{commit}').decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise ValueError('Commit did not resolve')
    fallback = False
    if dirty and require_point_in_time:
        dirty, fallback = False, True
    target = _layout(workspace, scratch or Path(tempfile.gettempdir()) / 'adhd-reader-inputs')
    try:
        return _prepare_attempts(workspace,head,dirty,untracked,scratch,target,fallback)
    except BaseException:
        _discard(target,target.parent)
        raise


def _prepare_attempts(workspace, head, dirty, untracked, scratch, target, fallback):
    object_ids = {p: oid for p, _, oid in _entries(workspace, head)}
    attempts = 3 if dirty else 1
    for attempt in range(attempts):
        before, staged, excluded = _source(workspace, head, dirty, untracked)
        # Each attempt gets its own subtree; failed copies are never published.
        content = target / ('attempt-' + str(attempt))
        content.mkdir()
        _copy_inputs(workspace,dirty,before,staged,content,object_ids)
        copied = {p: (dict(item) if item.get('deleted') else {**item,
                  'sha256': file_hash(_safe(content / 'worktree', p)),
                  'bytes': _safe(content / 'worktree', p).stat().st_size}) for p, item in before.items()}
        staged_valid = all(file_hash(_safe(content / 'index', p)) == item['sha256'] for p, item in staged.items())
        after, staged_after, excluded_after = _source(workspace, head, dirty, untracked)
        current_head=_git(workspace,'rev-parse','HEAD').decode('ascii').strip() if dirty else head
        if before == copied == after and staged == staged_after and staged_valid and excluded == excluded_after and current_head==head:
            manifest = {'schema_version': 1, 'id': target.name, 'workspace': str(workspace),
                        'commit': head, 'dirty': dirty, 'untracked': untracked if dirty else [],
                        'files': before, 'staged': staged, 'excluded': excluded,
                        'point_in_time_consistent': not dirty, 'writer_checkpoint_required': dirty,
                        'fixed_commit_fallback': fallback, 'attempts': attempt + 1,
                        'content': str(content), 'created_at': time.time(),
                        'concurrent_reader': concurrent_support()}
            atomic_json(target / 'snapshot.json', manifest)
            return {'status': 'ready', 'snapshot': str(target / 'snapshot.json'),
                    'manifest_hash': file_hash(target / 'snapshot.json'), **manifest}
        _discard(content,target)
    # Fixed input is the safe recovery; the failed dirty copies are not used.
    _discard(target,target.parent)
    result = prepare(workspace, commit=head, scratch=scratch, require_point_in_time=True)
    result['fixed_commit_fallback'] = True
    result['dirty_attempts'] = attempts
    path=Path(result['snapshot'])
    manifest=read_json(path,{})
    manifest.update(fixed_commit_fallback=True,dirty_attempts=attempts)
    atomic_json(path,manifest)
    result['manifest_hash']=file_hash(path)
    return result


def concurrent_support() -> dict:
    return {'enabled': False, 'reason': 'No verified host adapter blocks writes to source, configuration and external tools',
            'serial_snapshot_analysis': True, 'authority': 'host_enforcement_required'}


def read(snapshot: Path, expected_hash: str, path: str, *, start_line: int = 1,
         end_line: int | None = None, max_chars: int = 6500, start_char: int = 0) -> dict:
    if not snapshot.is_file() or file_hash(snapshot) != expected_hash:
        return {'status': 'stale', 'reason': 'snapshot_manifest_changed'}
    manifest = read_json(snapshot, {})
    item = manifest.get('files', {}).get(path, {})
    if not item or item.get('deleted'):
        return {'status': 'unavailable', 'reason': 'input_excluded_or_deleted'}
    content = Path(manifest['content']) / 'worktree'
    if not content.resolve().is_relative_to(snapshot.parent.resolve()):
        raise ValueError('Snapshot content escaped its directory')
    original = _safe(content, path)
    if not original.is_file() or file_hash(original) != item['sha256']:
        return {'status': 'stale', 'reason': 'snapshot_content_changed'}
    if (type(start_line) is not int or type(start_char) is not int or start_line < 1
            or not 0 <= start_char <= 32 * 1024 * 1024
            or end_line is not None and (type(end_line) is not int or end_line < start_line)
            or type(max_chars) is not int or not 1 <= max_chars <= 128000):
        raise ValueError('Invalid snapshot read range')
    selected, used, last, continuation = [], 0, start_line - 1, None
    with original.open('r', encoding='utf-8', errors='replace') as stream:
        number, offset = 1, 0
        while True:
            if end_line is not None and number > end_line:
                break
            chunk = stream.readline(65536)
            if not chunk:
                break
            skip = max(0, start_char - offset) if number == start_line else 0
            fragment = chunk[skip:] if number >= start_line else ''
            if fragment:
                take = min(max_chars - used, len(fragment))
                selected.append(fragment[:take]); used += take; last = number
                if used == max_chars:
                    next_line, next_char = number, offset + skip + take
                    if take == len(fragment) and chunk.endswith('\n'):
                        next_line, next_char = number + 1, 0
                    more = take < len(fragment) or bool(stream.read(1))
                    if more and (end_line is None or next_line <= end_line):
                        continuation = {'start_line': next_line, 'start_char': next_char}
                        if end_line is not None:
                            continuation['end_line'] = end_line
                    break
            offset += len(chunk)
            if chunk.endswith('\n'):
                number += 1; offset = 0
    return {'status': 'ok', 'text': ''.join(selected), 'truncated': continuation is not None,
            'ref': {'snapshot': manifest['id'], 'manifest_hash': expected_hash,
                    'commit': manifest['commit'], 'path': path, 'hash': item['sha256'],
                    'start_line': start_line, 'start_char': start_char, 'end_line': last},
            'continuation': continuation}


def claim_writer(snapshot: Path, expected_hash: str, *, run_id: str, owner: str) -> dict:
    if not snapshot.is_file() or file_hash(snapshot) != expected_hash:
        raise ValueError('Snapshot manifest changed')
    manifest = read_json(snapshot, {})
    workspace = Path(manifest['workspace'])
    started = time.monotonic()
    try:
        generation = leases.acquire(workspace, run_id, owner=owner)
    except ValueError as exc:
        return {'status': 'conflict', 'wait_seconds': time.monotonic() - started,
                'reason': str(exc), 'lease_acquired': False}
    failure=None
    try:
        current_head = _git(workspace, 'rev-parse', 'HEAD').decode('ascii').strip()
        current, staged, excluded = _source(workspace, manifest['commit'], True, manifest.get('untracked', []))
        expected = manifest['files']
        unchanged = current_head == manifest['commit'] and current == expected
        if manifest.get('dirty'):
            unchanged = unchanged and staged == manifest.get('staged') and excluded == manifest.get('excluded')
        else:
            unchanged=unchanged and set(staged)==set(expected) and all(staged[p]['sha256']==item.get('sha256') for p,item in expected.items())
    except (OSError,ValueError,subprocess.TimeoutExpired) as error:
        unchanged=False
        failure=type(error).__name__
    if not unchanged:
        # Never release a native lease while a registered child is still running.
        parts = run_id.split(':')
        state = read_json(store() / 'native' / parts[1] / 'state.json', {}) if owner == 'native' and len(parts) == 3 else {}
        children_running = any(c.get('status') == 'running' for c in state.get('children', {}).values())
        owner_unknown=owner=='native' and (not state or 'children' not in state)
        released = False if children_running or owner_unknown else leases.release(workspace, run_id, generation)
        return {'status': 'blocked' if failure else 'stale', 'reason': failure or 'source_changed_since_snapshot', 'generation': generation,
                'lease_acquired': not released, 'children_running': children_running,
                'owner_unknown':owner_unknown,
                'wait_seconds': time.monotonic() - started}
    return {'status': 'writer_acquired', 'generation': generation, 'lease_acquired': True,
            'wait_seconds': time.monotonic() - started, 'independent_acceptance_required': True}
