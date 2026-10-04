"""One post-assembly check batch, disk-backed evidence and conservative repair reuse.

Runs explicit argv through normal CLI tools, never from a host hook. Logs are
complete on disk; summaries are bounded. A suspected failure group is not a
proof that its members share a root cause.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
import json
import os
import re
import sys
import time
import uuid

from .core import atomic_json, digest, environment_signature, file_hash
from .evidence import _path, _argv, run_check, subject_manifest, validate_execution


def validate_plan(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError('Batch plan must be an object')
    checks = spec.get('checks')
    if not isinstance(checks, list) or not 1 <= len(checks) <= 200:
        raise ValueError('Batch requires 1..200 declared checks')
    known, covered = set(), set()
    for row in checks:
        if not isinstance(row, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(row.get('id', ''))):
            raise ValueError('Invalid check id')
        if row['id'] in known:
            raise ValueError('Duplicate check id')
        dependencies = row.get('depends_on', [])
        if not isinstance(dependencies, list) or not set(dependencies) <= known:
            raise ValueError('Check dependencies must refer to earlier checks')
        argv, subjects = row.get('argv'), row.get('subject_paths')
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
            raise ValueError('Check needs explicit argv')
        if not isinstance(subjects, list) or not subjects or len(subjects) != len(set(subjects)):
            raise ValueError('Check needs unique subject paths')
        for field in ('requirements', 'resources'):
            values = row.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError('Invalid check ' + field)
        known.add(row['id'])
        covered.update(row.get('requirements', []))
    required = spec.get('requirements', [])
    mandatory = spec.get('mandatory_checks', [])
    if not isinstance(required, list) or not set(required) <= covered:
        raise ValueError('Every required outcome needs a declared check')
    if not isinstance(mandatory, list) or not set(mandatory) <= known:
        raise ValueError('A mandatory check cannot be omitted')
    slots = spec.get('process_slots', 2)
    if type(slots) is not int or not 1 <= slots <= 16:
        raise ValueError('process_slots must be 1..16 (separate from model slots)')
    return spec


def freeze(workspace: Path, paths: list[str], *, run_id: str, revision: int,
           contract_hash: str = '') -> dict:
    files = _snapshot_manifest(workspace, paths)
    value = {'run_id': run_id, 'contract_revision': revision,
             'contract_hash': contract_hash, 'files': files,
             'environment': digest([environment_signature(workspace), sys.version, os.name])}
    return {**value, 'digest': digest(value)}


def _snapshot_manifest(workspace: Path, paths: list[str]) -> dict:
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20000 or len(paths) != len(set(paths)):
        raise ValueError('Frozen assembly needs 1..20000 distinct files')
    return {rel: file_hash(_path(workspace, rel)) for rel in sorted(paths)}


def _current(workspace: Path, snapshot: dict) -> bool:
    try:
        return (_snapshot_manifest(workspace, list(snapshot['files'])) == snapshot['files']
                and digest([environment_signature(workspace), sys.version, os.name]) == snapshot['environment'])
    except (ValueError, OSError):
        return False


def _excerpt(workspace: Path, receipt: str | None, limit: int = 2500) -> dict:
    if not receipt:
        return {'text': '', 'truncated': False}
    parent = _path(workspace, receipt).parent
    chunks, size = [], 0
    for name in ('check.stderr.log', 'check.stdout.jsonl'):
        path = parent / name
        size += path.stat().st_size
        with path.open('rb') as stream:
            if path.stat().st_size > limit:
                stream.seek(-limit, 2)
            chunks.append(stream.read(limit).decode('utf-8', errors='replace'))
    text = '\n'.join(chunks)
    return {'text': text[-limit:], 'truncated': size > limit,
            'full_logs': str(parent.relative_to(workspace)).replace('\\', '/')}


def failure_groups(results: list[dict]) -> list[dict]:
    groups = {}
    for row in results:
        if row['status'] not in {'failed', 'timeout', 'error', 'stale'}:
            continue
        text = row.get('excerpt', {}).get('text', '') or row.get('detail', '')
        lines = [v.strip() for v in text.splitlines() if v.strip()]
        message = next((v for v in reversed(lines) if re.search(r'Error|FAIL|Assertion|Exception', v)),
                       lines[-1] if lines else row['status'])
        location = next((v.strip() for v in reversed(lines) if re.search(r'File .+line \d+|[\w./\\]+:\d+', v)), '')
        normalized = re.sub(r'0x[0-9a-fA-F]+', '<address>', message)[:600]
        # Owner and location deliberately separate identical messages in different parts.
        key = digest([row.get('owner'), row['status'], normalized, location])
        if key not in groups:
            groups[key] = {'id': 'F' + str(len(groups) + 1), 'suspected_cause': normalized,
                           'location': location[:400], 'owner_package': row.get('owner'),
                           'affected_checks': [], 'evidence_refs': [],
                           'confidence': 'needs_confirmation'}
        groups[key]['affected_checks'].append(row['id'])
        groups[key]['evidence_refs'].append(row.get('receipt') or row.get('error_ref'))
    return list(groups.values())


def _reuse(workspace: Path, check: dict, previous: dict | None,
           snapshot: dict, plan_digest: str, changed: set[str], dependency_changed: bool) -> dict | None:
    if not previous or dependency_changed or check.get('critical'):
        return None
    if (previous.get('plan_digest') != plan_digest
            or previous.get('snapshot', {}).get('contract_revision') != snapshot['contract_revision']
            or previous.get('snapshot', {}).get('contract_hash') != snapshot['contract_hash']
            or previous.get('snapshot', {}).get('environment') != snapshot['environment']):
        return None
    if changed and (not check.get('impact_complete') or changed & set(check['subject_paths'])):
        return None
    old = next((r for r in previous.get('results', []) if r['id'] == check['id']), None)
    if not old or old['status'] not in {'passed', 'reused'}:
        return None
    try:
        validate_execution(workspace, old['receipt'], run_id=snapshot['run_id'],
                           revision=snapshot['contract_revision'])
    except (OSError, ValueError, TypeError):
        return None
    return {**old, 'status': 'reused', 'reused_from_snapshot': previous['snapshot']['digest'],
            'reuse_basis': 'Unchanged declared inputs/environment/plan; complete impact declaration; current receipt rechecked'}


def run_batch(spec: dict, workspace: Path) -> dict:
    """Execute all runnable checks, including independent checks after a failure."""
    validate_plan(spec)
    workspace = workspace.resolve()
    run_id, revision = spec.get('run_id'), spec.get('contract_revision')
    if not isinstance(run_id, str) or not run_id or type(revision) is not int or revision < 1:
        raise ValueError('Batch requires current run_id and contract_revision')
    paths = sorted({p for row in spec['checks'] for p in row['subject_paths']})
    paths = sorted(set(paths) | set(spec.get('snapshot_paths', [])))
    snapshot = freeze(workspace, paths, run_id=run_id, revision=revision,
                      contract_hash=spec.get('contract_hash', ''))
    if spec.get('expected_snapshot_digest') and snapshot['digest'] != spec['expected_snapshot_digest']:
        raise ValueError('Assembly changed before batch started')
    batch_id = uuid.uuid4().hex
    root = _path(workspace, '.adhd/batches/' + batch_id + '/report.json', existing=False).parent
    root.mkdir(parents=True, exist_ok=False)
    plan_value = {k: spec[k] for k in ('checks', 'requirements', 'mandatory_checks') if k in spec}
    plan_digest = digest(plan_value)
    atomic_json(root / 'plan.json', plan_value)
    atomic_json(root / 'snapshot.json', snapshot)
    previous = None
    if spec.get('previous_report'):
        previous = load_report(workspace, spec['previous_report'], require_current=False)
        if previous['snapshot']['run_id'] != run_id:
            raise ValueError('Previous batch belongs to another run')
        old_plan = json.loads(_path(workspace, previous['plan_ref']).read_text(encoding='utf-8'))
        if previous['snapshot']['contract_hash'] == snapshot['contract_hash']:
            new_checks = {c['id']: c for c in spec['checks']}
            if (any(new_checks.get(c['id']) != c for c in old_plan['checks'])
                    or not set(old_plan.get('mandatory_checks', [])) <= set(spec.get('mandatory_checks', []))
                    or not set(old_plan.get('requirements', [])) <= set(spec.get('requirements', []))):
                raise ValueError('Repair cannot remove or weaken the declared validation plan')
    old_files = previous.get('snapshot', {}).get('files', {}) if previous else {}
    changed = {p for p in set(old_files) | set(snapshot['files']) if old_files.get(p) != snapshot['files'].get(p)}
    pending = list(spec['checks'])
    completed, futures, busy = {}, {}, set()
    slots = spec.get('process_slots', 2)
    started = time.time()

    def execute(check):
        row = {'id': check['id'], 'owner': check.get('owner'),
               'requirements': check.get('requirements', []), 'status': 'error'}
        try:
            receipt = run_check({'run_id': run_id, 'contract_revision': revision,
                                 'argv': check['argv'], 'subject_paths': check['subject_paths'],
                                 'cwd': check.get('cwd', '.'), 'timeout': check.get('timeout', 300)}, workspace)
            row.update(receipt)
            row['status'] = ('stale' if not receipt['inputs_unchanged'] else
                             'timeout' if receipt.get('status') == 'timeout' else
                             'passed' if receipt['exit_code'] == 0 else 'failed')
            row['excerpt'] = _excerpt(workspace, receipt['receipt']) if row['status'] != 'passed' else {'text': '', 'truncated': False}
        except Exception as error:
            # Persist errors; never manufacture a successful execution receipt.
            row['status'] = 'timeout' if error.__class__.__name__ == 'Halt' else 'error'
            row['detail'] = str(error)[:2000]
            error_path = root / (check['id'] + '.error.json')
            atomic_json(error_path, {'type': error.__class__.__name__, 'detail': str(error)})
            row['error_ref'] = error_path.relative_to(workspace).as_posix()
        return row

    with ThreadPoolExecutor(max_workers=slots) as pool:
        while pending or futures:
            for check in list(pending):
                deps = check.get('depends_on', [])
                if not all(d in completed for d in deps):
                    continue
                if any(completed[d]['status'] not in {'passed', 'reused'} for d in deps):
                    completed[check['id']] = {'id': check['id'], 'status': 'blocked',
                                             'blocked_by': deps, 'owner': check.get('owner')}
                    pending.remove(check)
                    continue
                if len(futures) >= slots or busy & set(check.get('resources', [])):
                    continue
                reused = _reuse(workspace, check, previous, snapshot, plan_digest, changed,
                                any(completed[d]['status'] != 'reused' for d in deps))
                pending.remove(check)
                if reused:
                    completed[check['id']] = reused
                else:
                    future = pool.submit(execute, check)
                    futures[future] = check
                    busy.update(check.get('resources', []))
            if futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    check = futures.pop(future)
                    completed[check['id']] = future.result()
                    busy.difference_update(check.get('resources', []))
            elif pending:
                raise ValueError('Batch has unresolved dependencies')
    results = [completed[row['id']] for row in spec['checks']]
    current = _current(workspace, snapshot)
    status = ('stale' if not current else 'passed' if all(r['status'] in {'passed', 'reused'} for r in results) else 'needs_repair')
    report = {'schema_version': 1, 'batch_id': batch_id, 'snapshot': snapshot,
              'assembly_digest': spec.get('assembly_digest'),
              'plan_digest': plan_digest, 'plan_ref': (root / 'plan.json').relative_to(workspace).as_posix(),
              'status': status, 'inputs_unchanged': current, 'results': results,
              'groups': failure_groups(results), 'blocked_checks': [r['id'] for r in results if r['status'] == 'blocked'],
              'omitted_checks_with_reason': [], 'started_at': started, 'finished_at': time.time(),
              'usage': {'tokens': None, 'source': 'unmeasured', 'check_processes': sum(r['status'] not in {'blocked', 'reused'} for r in results)}}
    report['report_digest'] = digest(report)
    atomic_json(root / 'report.json', report)
    return {'report': (root / 'report.json').relative_to(workspace).as_posix(),
            'status': status, 'snapshot_digest': snapshot['digest'],
            'counts': {s: sum(r['status'] == s for r in results) for s in sorted({r['status'] for r in results})},
            'groups': report['groups'], 'blocked_checks': report['blocked_checks'],
            'tokens': None, 'logs': 'Full logs retained in the referenced execution receipts'}


def load_report(workspace: Path, relative: str, *, require_current: bool = True) -> dict:
    path = _path(workspace, relative)
    if not re.fullmatch(r'\.adhd/batches/[0-9a-f]{32}/report\.json', path.relative_to(workspace).as_posix()):
        raise ValueError('Report must be in the batch evidence store')
    report = json.loads(path.read_text(encoding='utf-8'))
    value = {k: v for k, v in report.items() if k != 'report_digest'}
    if digest(value) != report.get('report_digest'):
        raise ValueError('Batch report changed')
    plan = json.loads(_path(workspace, report['plan_ref']).read_text(encoding='utf-8'))
    if digest(plan) != report['plan_digest']:
        raise ValueError('Batch plan changed')
    snapshot_path = path.parent / 'snapshot.json'
    if json.loads(snapshot_path.read_text(encoding='utf-8')) != report['snapshot']:
        raise ValueError('Batch snapshot changed')
    if require_current:
        if report['status'] != 'passed' or not _current(workspace, report['snapshot']):
            raise ValueError('Batch failed or frozen inputs changed')
        expected = {r['id']: r for r in plan['checks']}
        if len(report['results']) != len(expected) or {r['id'] for r in report['results']} != set(expected):
            raise ValueError('Batch check coverage changed')
        for row in report['results']:
            if row['status'] not in {'passed', 'reused'}:
                raise ValueError('Batch contains an unverified check')
            receipt = validate_execution(workspace, row['receipt'], run_id=report['snapshot']['run_id'],
                                         revision=report['snapshot']['contract_revision'])
            check = expected[row['id']]
            if set(receipt['subject_files']) != set(check['subject_paths']):
                raise ValueError('Check subjects differ from the declared plan')
            if receipt['invocation']['argv'] != _argv(check):
                raise ValueError('Check invocation differs from the declared plan')
            expected_cwd = workspace if check.get('cwd', '.') == '.' else workspace / check['cwd']
            if Path(receipt['invocation']['cwd']).resolve() != expected_cwd.resolve():
                raise ValueError('Check working directory differs from the declared plan')
    return report


def repair_plan(workspace: Path, relative: str) -> dict:
    report = load_report(workspace, relative, require_current=False)
    return {'snapshot_digest': report['snapshot']['digest'], 'groups': report['groups'],
            'blocked_checks': report['blocked_checks'],
            'rule': 'QA confirms suspected roots and assigns one owner per shared cause; rerun failed, impacted and critical checks after repair.'}
