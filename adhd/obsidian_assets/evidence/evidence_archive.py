"""Explicit, recoverable archives of selected closed native schema-2 check logs."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from filelock import FileLock

from . import leases
from .core import digest, file_hash, read_json, store
from .evidence import _path, subject_manifest, validate_execution
from .evidence_codec import store_log, verify_log, write_log
from .storage import _no_reparse


_RECEIPT = re.compile(r'\.adhd/checks/[0-9a-f]{32}/receipt\.json\Z')
_MANIFEST = re.compile(r'\.adhd/evidence-archives/[0-9a-f]{32}/manifest\.json\Z')
_STREAMS = {'stdout': 'check.stdout.jsonl', 'stderr': 'check.stderr.log'}


def _relative(workspace: Path, path: Path) -> str:
    return path.relative_to(workspace).as_posix()


def _check_receipt(workspace: Path, relative: str, run_id: str) -> tuple[Path, dict]:
    path = _path(workspace, relative)
    if not _RECEIPT.fullmatch(_relative(workspace, path)) or path.stat().st_size > 160_000:
        raise ValueError('Archive requires a bounded ADHD execution receipt')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if (not isinstance(value, dict) or not isinstance(value.get('result'), dict)
            or value.get('schema_version') != 2
            or value.get('run_id') != run_id or value.get('evidence_id') != path.parent.name
            or value.get('assurance') != 'local_process_observed'
            or value.get('kind') != 'test_run' or value.get('result', {}).get('status') != 'exited'):
        raise ValueError('Archive requires an observed exited schema-2 receipt from this run')
    return path, value


def _state_references(value: object, receipts: set[str]) -> bool:
    targets = set(receipts)
    for receipt in receipts:
        targets.update((Path(receipt).parent / name).as_posix() for name in _STREAMS.values())
    def referenced(node: object) -> bool:
        if isinstance(node, str):
            normalized = node.replace('\\', '/')
            return any(target in normalized for target in targets)
        if isinstance(node, dict):
            return any(referenced(key) or referenced(item) for key, item in node.items())
        if isinstance(node, list):
            return any(referenced(item) for item in node)
        return False
    return referenced(value)


def _workspace_states(root: Path):
    native_root = root / 'native'
    if not native_root.exists():
        return []
    if native_root.is_symlink() or (hasattr(native_root, 'is_junction') and native_root.is_junction()):
        raise ValueError('Native state directory is a link or junction')
    sessions = []
    for session in sorted(native_root.iterdir()):
        if not re.fullmatch(r'[0-9a-f]{24}', session.name) or not session.is_dir():
            raise ValueError('Unknown native session entry')
        if session.is_symlink() or (hasattr(session, 'is_junction') and session.is_junction()):
            raise ValueError('Native session directory is a link or junction')
        sessions.append(session)
    return sessions


def _read_states(session: Path):
    paths = [session / 'state.json']
    archive_root = session / 'run-archive'
    if archive_root.exists():
        if archive_root.is_symlink() or (hasattr(archive_root, 'is_junction') and archive_root.is_junction()):
            raise ValueError('Native run archive is a link or junction')
        for entry in archive_root.iterdir():
            if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                raise ValueError('Native run archive entry is a link or junction')
            if entry.is_dir():
                paths.append(entry / 'state.json')
    for path in paths:
        if not path.exists():
            raise ValueError('Missing native owner state')
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()) or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('Unsafe or oversized native state')
        value = read_json(path)
        if not isinstance(value, dict) or value.get('key') != session.name or not isinstance(value.get('workspace'), str):
            raise ValueError('Unknown native state ownership')
        if path != session / 'state.json' and path.parent.name != value.get('run_id'):
            raise ValueError('Native run archive path does not match its run id')
        yield path, value


def _legacy_closed(workspace: Path, receipts: set[str]) -> None:
    """The shared lease guard also excludes a legacy resume during this read."""
    root = store() / 'runs'
    _no_reparse(root)
    if not root.exists():
        return
    if not root.is_dir():
        raise ValueError('Unknown legacy run directory')
    for entry in root.iterdir():
        _no_reparse(entry)
        state_path = entry / 'state.json'
        _no_reparse(state_path)
        if (not entry.is_dir() or not state_path.is_file()
                or state_path.stat().st_size > 8 * 1024 * 1024):
            raise ValueError('Missing or unknown legacy owner state')
        state = read_json(state_path)
        if (not isinstance(state, dict) or state.get('id') != entry.name
                or not isinstance(state.get('workspace'), str)):
            raise ValueError('Unknown legacy owner state')
        if _state_references(state, receipts):
            raise ValueError('Legacy evidence references these logs')
        if Path(state['workspace']).resolve() == workspace and state.get('status') != 'complete':
            raise ValueError('Active or resumable legacy owner protects this workspace')


def _batches_closed(workspace: Path, receipts: set[str], run_id: str) -> None:
    root = _path(workspace, '.adhd/batches', existing=False)
    if not root.exists():
        return
    if not root.is_dir():
        raise ValueError('Invalid batch evidence directory')
    for directory in root.iterdir():
        if not directory.is_dir():
            raise ValueError('Unknown batch evidence entry')
        report = _path(workspace, _relative(workspace, directory / 'report.json'), existing=False)
        if not report.is_file():
            raise ValueError('Unfinished batch may reference these receipts')
        if report.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('Oversized batch report')
        value = json.loads(report.read_text(encoding='utf-8-sig'))
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('status') not in {'passed', 'stale', 'needs_repair'}
                or not isinstance(value.get('results'), list)):
            raise ValueError('Unknown batch report')
        from .validation_batch import load_report
        try:
            load_report(workspace, _relative(workspace, report), require_current=False)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Unverified batch report') from error
        used = {row.get('receipt') for row in value['results'] if isinstance(row, dict)} & receipts
        if used and (value.get('status') != 'passed' or value.get('snapshot', {}).get('run_id') != run_id):
            raise ValueError('Active or resumable batch references an archived receipt')


@contextmanager
def _closed_owner(workspace: Path, run_id: str, receipts: set[str], *,
                  expected: dict | None = None, require_fresh: bool = False):
    """Hold native session locks before the common writer lease guard."""
    sessions = _workspace_states(store())
    with ExitStack() as stack:
        for session in sessions:
            _no_reparse(session / 'state.lock')
            stack.enter_context(FileLock(str(session / 'state.lock'), timeout=5))
        lease = leases.lease_path(workspace)
        _no_reparse(lease)
        _no_reparse(Path(str(lease) + '.guard'))
        lease.parent.mkdir(parents=True, exist_ok=True)
        stack.enter_context(FileLock(str(lease) + '.guard', timeout=5))
        if lease.exists():
            raise ValueError('Workspace still has a writer lease')
        _legacy_closed(workspace, receipts)
        owners = []
        for session in sessions:
            records = list(_read_states(session))
            current = next((row for path, row in records if path == session / 'state.json'), None)
            for path, state in records:
                if Path(state['workspace']).resolve() != workspace:
                    if _state_references(state, receipts):
                        raise ValueError('Another workspace references these evidence logs')
                    continue
                status = state.get('status')
                children = state.get('children', {})
                pending = state.get('pending_turn_ids', [])
                if pending and path != session / 'state.json':
                    acknowledged = (isinstance(pending, list) and all(isinstance(turn, str) and turn for turn in pending)
                                    and isinstance(current, dict)
                                    and current.get('status') in {'idle', 'complete'}
                                    and isinstance(current.get('archived_runs'), list)
                                    and isinstance(current.get('prompt_turns'), list)
                                    and isinstance(current.get('pending_turn_ids', []), list)
                                    and all(isinstance(turn, str) for turn in current['prompt_turns'])
                                    and all(isinstance(turn, str) for turn in current.get('pending_turn_ids', []))
                                    and str(path) in current.get('archived_runs', [])
                                    and set(pending) <= set(current.get('prompt_turns', []))
                                    and not set(pending) & set(current.get('pending_turn_ids', []))
                                    and type(current.get('intent_version')) is int
                                    and type(state.get('intent_version')) is int
                                    and current['intent_version'] > state.get('intent_version', -1))
                    if acknowledged:
                        state = {**state, 'pending_turn_ids': []}
                        pending = []
                if (status not in {'complete', 'idle'} or pending
                        or not isinstance(children, dict)
                        or any(not isinstance(child, dict) or child.get('status') != 'finished'
                               for child in children.values())):
                    raise ValueError('Active, resumable or unknown native owner protects this workspace')
                if state.get('run_id') == run_id:
                    if status != 'complete' or not isinstance(state.get('candidate'), dict):
                        raise ValueError('Archive owner is not a completed native run')
                    owners.append(state)
                elif _state_references(state, receipts):
                    raise ValueError('Another native run references an archived receipt')
        if len(owners) != 1:
            raise ValueError('A unique completed native owner was not found')
        owner = owners[0]
        candidate = owner['candidate']
        if (owner.get('schema_version') != 2 or type(owner.get('intent_version')) is not int
                or owner['intent_version'] < 1 or not isinstance(owner.get('contract_hash'), str)
                or not isinstance(owner.get('contract'), dict)
                or owner['contract_hash'] != digest(owner['contract'])
                or candidate.get('digest') != digest({key: item for key, item in candidate.items() if key != 'digest'})
                or candidate.get('evidence_schema') != 1
                or candidate.get('intent_version') != owner['intent_version']
                or candidate.get('contract_hash') != owner['contract_hash']
                or not isinstance(candidate.get('execution_receipts'), dict)
                or not isinstance(candidate.get('criterion_results'), list)):
            raise ValueError('Unknown or incomplete native candidate state')
        if require_fresh:
            from .native import is_fresh
            if not is_fresh(owner):
                raise ValueError('Completed native candidate is no longer fresh')
        binding = {'key': owner['key'], 'candidate_digest': digest(owner['candidate']),
                   'lease_generation': owner.get('lease_generation')}
        if expected is not None and binding != expected:
            raise ValueError('Native owner/candidate identity changed')
        _batches_closed(workspace, receipts, run_id)
        yield binding, owner


def _control_bytes(workspace: Path, receipt: dict) -> None:
    files = receipt.get('subject_files')
    if not isinstance(files, dict) or not files:
        raise ValueError('Invalid checked input manifest')
    subject = _path(workspace, receipt['subject_manifest'])
    if (file_hash(subject) != receipt.get('subject_manifest_sha256')
            or json.loads(subject.read_text(encoding='utf-8-sig')) !=
            {'schema_version': 2, 'files': files, 'digest': digest(files)}):
        raise ValueError('Check subject manifest changed')


def _controls_current(workspace: Path, receipt: dict) -> None:
    _control_bytes(workspace, receipt)
    result = receipt['result']
    argv = receipt.get('invocation', {}).get('argv', [])
    if (result.get('status') != 'exited' or result.get('inputs_unchanged') is not True
            or not argv or not Path(argv[0]).is_file()
            or file_hash(Path(argv[0])) != receipt['invocation'].get('executable_sha256')):
        raise ValueError('Execution controls are no longer current')
    files = receipt['subject_files']
    if subject_manifest(workspace, list(files)) != files:
        raise ValueError('Checked inputs changed')


def _durable_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as sink:
            json.dump(value, sink, ensure_ascii=False, indent=2)
            sink.write('\n')
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _archive_lock(workspace: Path):
    lock = _path(workspace, '.adhd/evidence-archives/operation.lock', existing=False)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock), timeout=5):
        yield


def _existing_archive(workspace: Path, run_id: str, binding: dict,
                      source: list[tuple[Path, dict]]) -> tuple[str, list[dict]] | None:
    root = _path(workspace, '.adhd/evidence-archives', existing=False)
    wanted = {_relative(workspace, path): file_hash(path) for path, _ in source}
    for directory in root.iterdir():
        if not re.fullmatch(r'[0-9a-f]{32}', directory.name):
            continue
        manifest = _path(workspace, _relative(workspace, directory / 'manifest.json'), existing=False)
        if not manifest.is_file():
            continue
        relative = _relative(workspace, manifest)
        _, value = _load_manifest(workspace, relative)
        if (value['run_id'] == run_id and value['owner'] == binding
                and {row['receipt_path']: row['receipt_sha256'] for row in value['receipts']} == wanted):
            verify_archive(workspace, relative)
            return relative, value['receipts']
    return None


def archive_receipts(workspace: Path, run_id: str, receipts: list[str], *, release: bool = False) -> dict:
    workspace = Path(workspace).resolve()
    if (not isinstance(run_id, str) or not run_id or len(run_id) > 256
            or not isinstance(receipts, list) or not 1 <= len(receipts) <= 200
            or len(set(receipts)) != len(receipts)):
        raise ValueError('Archive needs a run id and 1..200 distinct receipts')
    with _archive_lock(workspace):
        with _closed_owner(workspace, run_id, set(receipts), require_fresh=True) as (binding, _):
            source = []
            for relative in receipts:
                receipt_path, receipt = _check_receipt(workspace, relative, run_id)
                validate_execution(workspace, relative, run_id=run_id,
                                   revision=receipt['contract_revision'],
                                   expect_failure=receipt['result']['exit_code'] != 0)
                source.append((receipt_path, receipt))
            existing = _existing_archive(workspace, run_id, binding, source)
            if existing:
                relative, rows = existing
            else:
                archive_dir = _path(workspace, '.adhd/evidence-archives/' + uuid.uuid4().hex + '/manifest.json',
                                    existing=False).parent
                archive_dir.mkdir(parents=True, exist_ok=False)
                rows = []
                for receipt_path, receipt in source:
                    logs = []
                    for stream, name in _STREAMS.items():
                        raw = _path(workspace, _relative(workspace, receipt_path.parent / name))
                        copied = archive_dir / (receipt_path.parent.name + '.' + stream)
                        with raw.open('rb') as original, copied.open('xb') as target:
                            shutil.copyfileobj(original, target, 1024 * 1024)
                            target.flush()
                            os.fsync(target.fileno())
                        descriptor = store_log(archive_dir, copied)
                        verify_log(archive_dir, descriptor)
                        if descriptor['codec'] == 'gzip':
                            copied.unlink()
                        if descriptor['original_sha256'] != receipt['result'][stream + '_sha256']:
                            raise ValueError('Archived log differs from its receipt')
                        logs.append({'stream': stream, 'original_path': _relative(workspace, raw),
                                     'stored_path': _relative(workspace, archive_dir / descriptor['path']),
                                     'codec': descriptor['codec'],
                                     'original_sha256': descriptor['original_sha256'],
                                     'original_bytes': descriptor['original_bytes'],
                                     'stored_sha256': descriptor['stored_sha256'],
                                     'stored_bytes': descriptor['stored_bytes']})
                    rows.append({'receipt_path': _relative(workspace, receipt_path),
                                 'receipt_sha256': file_hash(receipt_path), 'logs': logs})
                manifest = {'schema_version': 1, 'workspace': str(workspace), 'run_id': run_id,
                            'owner': binding, 'receipts': rows}
                manifest['manifest_digest'] = digest(manifest)
                _durable_json(archive_dir / 'manifest.json', manifest)
                relative = _relative(workspace, archive_dir / 'manifest.json')
                verify_archive(workspace, relative)
    result = {'manifest': relative, 'receipts': len(rows),
              'reused': bool(existing),
              'original_bytes': sum(log['original_bytes'] for row in rows for log in row['logs']),
              'stored_bytes': sum(log['stored_bytes'] for row in rows for log in row['logs'])}
    if release:
        result.update(release_raw(workspace, relative))
    return result


def _load_manifest(workspace: Path, relative: str) -> tuple[Path, dict]:
    path = _path(workspace, relative)
    if not _MANIFEST.fullmatch(_relative(workspace, path)) or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Invalid evidence archive manifest path or size')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or value.get('workspace') != str(workspace)
            or not isinstance(value.get('run_id'), str)
            or not isinstance(value.get('owner'), dict)
            or not isinstance(value.get('receipts'), list)
            or not 1 <= len(value['receipts']) <= 200
            or digest({key: item for key, item in value.items() if key != 'manifest_digest'}) != value.get('manifest_digest')):
        raise ValueError('Evidence archive manifest changed or is unsupported')
    return path, value


def _verify_archive(workspace: Path, relative: str) -> tuple[dict, list[tuple[Path, dict, Path]]]:
    manifest_path, value = _load_manifest(workspace, relative)
    archive_dir = manifest_path.parent
    paths = []
    seen = set()
    for row in value['receipts']:
        if not isinstance(row, dict) or not isinstance(row.get('logs'), list) or len(row['logs']) != 2:
            raise ValueError('Invalid evidence archive receipt entry')
        receipt_path, receipt = _check_receipt(workspace, row.get('receipt_path'), value['run_id'])
        if file_hash(receipt_path) != row.get('receipt_sha256'):
            raise ValueError('Archived receipt changed')
        _control_bytes(workspace, receipt)
        if receipt_path in seen:
            raise ValueError('Duplicate archived receipt')
        seen.add(receipt_path)
        for log in row['logs']:
            if not isinstance(log, dict) or log.get('stream') not in _STREAMS:
                raise ValueError('Invalid archived log entry')
            raw = receipt_path.parent / _STREAMS[log['stream']]
            if log.get('original_path') != _relative(workspace, raw):
                raise ValueError('Archived raw path differs from its receipt')
            raw = _path(workspace, log['original_path'], existing=False)
            stored = _path(workspace, log.get('stored_path'), existing=True)
            if stored.parent != archive_dir:
                raise ValueError('Archived log leaves its manifest directory')
            descriptor = {key: log.get(key) for key in ('codec', 'original_sha256', 'original_bytes',
                          'stored_sha256', 'stored_bytes')}
            descriptor['path'] = stored.name
            verify_log(archive_dir, descriptor)
            if log['original_sha256'] != receipt['result'].get(log['stream'] + '_sha256'):
                raise ValueError('Archived log differs from the original receipt')
            if raw.exists() and (not raw.is_file() or file_hash(raw) != log['original_sha256']
                                 or raw.stat().st_size != log['original_bytes']):
                raise ValueError('Raw log conflicts with the archived copy')
            paths.append((raw, descriptor, archive_dir))
    if len({raw for raw, _, _ in paths}) != len(paths):
        raise ValueError('Duplicate archived raw path')
    return value, paths


def verify_archive(workspace: Path, manifest: str) -> dict:
    workspace = Path(workspace).resolve()
    value, paths = _verify_archive(workspace, manifest)
    return {'verified': True, 'run_id': value['run_id'], 'logs': len(paths),
            'raw_present': sum(raw.is_file() for raw, _, _ in paths)}


def release_raw(workspace: Path, manifest: str) -> dict:
    workspace = Path(workspace).resolve()
    with _archive_lock(workspace):
        value, paths = _verify_archive(workspace, manifest)
        receipts = {row['receipt_path'] for row in value['receipts']}
        with _closed_owner(workspace, value['run_id'], receipts, expected=value['owner']) as (_, owner):
            # Recheck every copy and control while both owner and operation locks are held.
            _verify_archive(workspace, manifest)
            present = {raw for raw, _, _ in paths if raw.exists()}
            if not present:
                return {'released': 0, 'manifest': manifest}
            if len(present) != len(paths):
                _restore_missing(paths)
            from .native import is_fresh
            if not is_fresh(owner):
                raise ValueError('Completed native candidate is no longer fresh')
            for row in value['receipts']:
                _, receipt = _check_receipt(workspace, row['receipt_path'], value['run_id'])
                _controls_current(workspace, receipt)
            for raw, _, _ in paths:
                raw.unlink()
    return {'released': len(present), 'manifest': manifest}


def _restore_missing(paths: list[tuple[Path, dict, Path]]) -> int:
    restored = 0
    for raw, descriptor, archive_dir in paths:
        if raw.exists():
            continue
        temporary = raw.with_name('.' + raw.name + '.restore.' + uuid.uuid4().hex + '.tmp')
        try:
            write_log(archive_dir, descriptor, temporary)
            os.link(temporary, raw)
            restored += 1
        except FileExistsError as error:
            raise ValueError('Raw log destination appeared during restore') from error
        finally:
            temporary.unlink(missing_ok=True)
    return restored


def restore_raw(workspace: Path, manifest: str) -> dict:
    workspace = Path(workspace).resolve()
    with _archive_lock(workspace):
        _, paths = _verify_archive(workspace, manifest)
        restored = _restore_missing(paths)
    return {'restored': restored, 'manifest': manifest}
