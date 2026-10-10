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
import shutil
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
        for field in ('requirements', 'resources', 'dependency_paths', 'fixture_paths', 'environment_vars'):
            values = row.get(field, [])
            if not isinstance(values, list) or len(values) > 500 or len(values) != len(set(values)) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError('Invalid check ' + field)
        for field in ('critical', 'impact_complete', 'environment_complete', 'external_state', 'time_sensitive'):
            if field in row and type(row[field]) is not bool:
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


def diagnose_plan(spec: dict) -> dict:
    """Explain expensive plan choices without rewriting required checks."""
    validate_plan(spec)
    checks = spec['checks']
    warnings = []
    if len(checks) > 1 and all(row.get('critical') for row in checks):
        warnings.append({'code': 'all_checks_critical',
                         'checks': [row['id'] for row in checks],
                         'message': 'Every check requires fresh execution; mandatory evidence alone does not require critical=true.'})
    scopes = {}
    for row in checks:
        scope = tuple(sorted(set(row['subject_paths'] + row.get('dependency_paths', []) + row.get('fixture_paths', []))))
        scopes.setdefault(scope, []).append(row['id'])
    shared = [ids for ids in scopes.values() if len(ids) > 1]
    if shared:
        warnings.append({'code': 'shared_input_scope', 'check_groups': shared,
                         'message': 'These checks share all declared inputs; confirm that each needs the full scope.'})
    for flag, code in [('impact_complete', 'incomplete_impact'), ('environment_complete', 'incomplete_environment')]:
        ids = [row['id'] for row in checks if not row.get(flag)]
        if ids:
            warnings.append({'code': code, 'checks': ids,
                             'message': 'Incomplete declarations require execution; confirm coverage before enabling reuse.'})
    eligible = [row['id'] for row in checks if row.get('impact_complete') and row.get('environment_complete')
                and not any(row.get(flag) for flag in ('critical', 'external_state', 'time_sensitive'))]
    return {'checks': len(checks), 'mandatory_checks': spec.get('mandatory_checks', []),
            'reusable_checks': eligible, 'reuse_ready': bool(eligible), 'warnings': warnings}


def freeze(workspace: Path, paths: list[str], *, run_id: str, revision: int,
           contract_hash: str = '', checks: list[dict] | None = None) -> dict:
    files = _snapshot_manifest(workspace, paths)
    checks = checks or []
    variables = sorted({name for row in checks for name in row.get('environment_vars', [])})
    declared_environment = {name: digest(os.environ[name]) if name in os.environ else None for name in variables}
    tools, hashes = {}, {}
    for row in checks:
        executable = shutil.which(row['argv'][0]) or row['argv'][0]
        path = Path(executable).resolve()
        if path not in hashes:
            hashes[path] = file_hash(path) if path.is_file() else None
        tools[row['id']] = {'argv0': row['argv'][0], 'path': str(path), 'sha256': hashes[path]}
    value = {'run_id': run_id, 'contract_revision': revision,
             'contract_hash': contract_hash, 'files': files,
             'environment': digest([environment_signature(workspace), sys.version, os.name]),
             'declared_environment': declared_environment, 'tools': tools}
    # Each key binds only that check and its dependency definitions. Adding an
    # unrelated check cannot invalidate existing evidence. File bytes are still
    # checked separately, including at every final approval boundary.
    keys = {}
    for row in checks:
        keys[row['id']] = digest({'check': row, 'run_id': run_id, 'revision': revision,
                                 'contract_hash': contract_hash, 'environment': value['environment'],
                                 'declared_environment': {name: declared_environment[name]
                                                          for name in row.get('environment_vars', [])},
                                 'tool': tools[row['id']],
                                 'dependencies': {key: keys[key] for key in row.get('depends_on', [])}})
    value['check_keys'] = keys
    return {**value, 'digest': digest(value)}


def _snapshot_manifest(workspace: Path, paths: list[str]) -> dict:
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20000 or len(paths) != len(set(paths)):
        raise ValueError('Frozen assembly needs 1..20000 distinct files')
    return {rel: file_hash(_path(workspace, rel)) for rel in sorted(paths)}


def _current(workspace: Path, snapshot: dict) -> bool:
    try:
        return (_snapshot_manifest(workspace, list(snapshot['files'])) == snapshot['files']
                and digest([environment_signature(workspace), sys.version, os.name]) == snapshot['environment']
                and {name: digest(os.environ[name]) if name in os.environ else None
                     for name in snapshot.get('declared_environment', {})} == snapshot.get('declared_environment', {})
                and _tools_current(snapshot))
    except (ValueError, OSError):
        return False


def _tools_current(snapshot: dict) -> bool:
    hashes = {}
    for row_id, tool in snapshot.get('tools', {}).items():
        if not isinstance(tool, dict):
            return False
        executable = shutil.which(tool['argv0']) or tool['argv0']
        path = Path(executable).resolve()
        if not path.is_file() or (tool.get('path') is not None and tool['path'] != str(path)):
            return False
        if path not in hashes:
            hashes[path] = file_hash(path)
        if tool['sha256'] is None or hashes[path] != tool['sha256']:
            return False
    return True


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
           snapshot: dict, plan_digest: str, changed: set[str], dependency_changed: bool) -> tuple[dict | None, str]:
    if not previous:
        return None, 'first_execution'
    old = next((r for r in previous.get('results', []) if r['id'] == check['id']), None)
    if old is None:
        return None, 'new_check'
    for flag, reason in [('critical', 'critical'), ('external_state', 'external_state'), ('time_sensitive', 'time_sensitive')]:
        if check.get(flag):
            return None, reason
    if not check.get('impact_complete'):
        return None, 'incomplete_impact'
    if not check.get('environment_complete'):
        return None, 'incomplete_environment'
    if dependency_changed:
        return None, 'dependency_executed'
    prior = previous.get('snapshot', {})
    if (prior.get('contract_revision') != snapshot['contract_revision']
            or prior.get('contract_hash') != snapshot['contract_hash']):
        return None, 'contract_changed'
    if (prior.get('environment') != snapshot['environment']
            or any(prior.get('declared_environment', {}).get(name) != snapshot['declared_environment'].get(name)
                   for name in check.get('environment_vars', []))):
        return None, 'environment_changed'
    old_tool, new_tool = prior.get('tools', {}).get(check['id']), snapshot['tools'].get(check['id'])
    if old_tool is None or new_tool is None or any(old_tool.get(key) != new_tool.get(key) for key in ('argv0', 'sha256')):
        return None, 'tool_changed'
    if old_tool.get('path') is not None and old_tool['path'] != new_tool.get('path'):
        return None, 'tool_changed'
    if 'check_keys' in prior:
        if prior['check_keys'].get(check['id']) != snapshot['check_keys'].get(check['id']):
            return None, 'check_definition_changed'
    elif (previous.get('plan_digest') != plan_digest
          or prior.get('declared_environment') != snapshot['declared_environment']):
        # Legacy reports lack per-check identity: retain the old conservative
        # whole-plan requirement instead of guessing their dependency scope.
        return None, 'legacy_plan_changed'
    affected=set(check['subject_paths']+check.get('dependency_paths',[])+check.get('fixture_paths',[]))
    if changed & affected:
        return None, 'inputs_changed'
    if old['status'] not in {'passed', 'reused'}:
        return None, 'no_successful_receipt'
    try:
        receipt = validate_execution(workspace, old['receipt'], run_id=snapshot['run_id'],
                                     revision=snapshot['contract_revision'])
        _matches_check(workspace, check, receipt)
    except (OSError, ValueError, TypeError, KeyError):
        return None, 'receipt_invalid'
    reused = {**old, 'status': 'reused', 'reused_from_snapshot': prior['digest'],
              'reuse_basis': 'Unchanged per-check inputs/environment/definition; complete impact declaration; current receipt rechecked'}
    reused.pop('invalidation_reason', None)
    return reused, 'unchanged_inputs'


def _matches_check(workspace: Path, check: dict, receipt: dict) -> None:
    if set(receipt['subject_files']) != set(check['subject_paths']):
        raise ValueError('Check subjects differ from the declared plan')
    if receipt['invocation']['argv'] != _argv(check):
        raise ValueError('Check invocation differs from the declared plan')
    expected_cwd = workspace if check.get('cwd', '.') == '.' else workspace / check['cwd']
    if Path(receipt['invocation']['cwd']).resolve() != expected_cwd.resolve():
        raise ValueError('Check working directory differs from the declared plan')


def _same_failed_check(workspace: Path, check: dict, previous: dict | None,
                       snapshot: dict, plan_digest: str) -> dict | None:
    if (not previous or not check.get('environment_complete') or not check.get('impact_complete')
            or check.get('critical')
            or check.get('external_state') or check.get('time_sensitive')
            or previous.get('plan_digest') != plan_digest
            or previous.get('snapshot', {}).get('digest') != snapshot['digest']):
        return None
    old = next((row for row in previous.get('results', []) if row['id'] == check['id']), None)
    if not old or old['status'] != 'failed':
        return None
    try:
        receipt = validate_execution(workspace, old['receipt'], run_id=snapshot['run_id'],
                                     revision=snapshot['contract_revision'], expect_failure=True)
        if (set(receipt['subject_files']) != set(check['subject_paths'])
                or receipt['invocation']['argv'] != _argv(check)):
            return None
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return old


def run_batch(spec: dict, workspace: Path) -> dict:
    """Execute all runnable checks, including independent checks after a failure."""
    validate_plan(spec)
    started = time.time()
    diagnostics = diagnose_plan(spec)
    retry_policy = spec.get('retry_policy', {})
    if (not isinstance(retry_policy, dict)
            or set(retry_policy) - {row['id'] for row in spec['checks']}):
        raise ValueError('Retry policy must name declared checks')
    for policy in retry_policy.values():
        if (not isinstance(policy, dict) or policy.get('kind') != 'bounded_flake_probe'
                or policy.get('max_attempts') != 1 or not isinstance(policy.get('reason'), str)
                or not policy['reason'].strip() or len(policy['reason']) > 500
                or not isinstance(policy.get('evidence_ref'), str)):
            raise ValueError('A same-input retry needs one bounded flake probe and prior evidence')
    workspace = workspace.resolve()
    run_id, revision = spec.get('run_id'), spec.get('contract_revision')
    if not isinstance(run_id, str) or not run_id or type(revision) is not int or revision < 1:
        raise ValueError('Batch requires current run_id and contract_revision')
    paths = sorted({p for row in spec['checks'] for p in
                    row['subject_paths']+row.get('dependency_paths',[])+row.get('fixture_paths',[])})
    paths = sorted(set(paths) | set(spec.get('snapshot_paths', [])))
    snapshot = freeze(workspace, paths, run_id=run_id, revision=revision,
                      contract_hash=spec.get('contract_hash', ''), checks=spec['checks'])
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
                prior_failure = _same_failed_check(workspace, check, previous, snapshot, plan_digest)
                if prior_failure:
                    probe = retry_policy.get(check['id'])
                    if (not probe or probe['evidence_ref'] != prior_failure['receipt']
                            or prior_failure.get('decision_class') == 'bounded_flake_probe'):
                        completed[check['id']] = {**prior_failure,
                            'decision_class': 'unjustified_duplicate_held'}
                        completed[check['id']].pop('invalidation_reason', None)
                        pending.remove(check)
                        continue
                reused, reason = _reuse(workspace, check, previous, snapshot, plan_digest, changed,
                                        any(completed[d]['status'] != 'reused' for d in deps))
                pending.remove(check)
                if reused:
                    completed[check['id']] = {**reused, 'decision_class': 'unchanged_inputs'}
                else:
                    future = pool.submit(execute, check)
                    futures[future] = (check, 'bounded_flake_probe' if prior_failure else None, reason)
                    busy.update(check.get('resources', []))
            if futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    check, decision_class, reason = futures.pop(future)
                    completed[check['id']] = future.result()
                    completed[check['id']]['invalidation_reason'] = reason
                    if decision_class:
                        completed[check['id']]['decision_class'] = decision_class
                    busy.difference_update(check.get('resources', []))
            elif pending:
                raise ValueError('Batch has unresolved dependencies')
    results = [completed[row['id']] for row in spec['checks']]
    current = _current(workspace, snapshot)
    status = ('stale' if not current else 'passed' if all(r['status'] in {'passed', 'reused'} for r in results) else 'needs_repair')
    counts = {s: sum(r['status'] == s for r in results) for s in sorted({r['status'] for r in results})}
    finished = time.time()
    reasons = {}
    for row in results:
        if row.get('invalidation_reason'):
            reason = row['invalidation_reason']
            reasons[reason] = reasons.get(reason, 0) + 1
    held = sum(r.get('decision_class') == 'unjustified_duplicate_held' for r in results)
    processes = sum(bool(r.get('receipt')) and r['status'] != 'reused'
                    and r.get('decision_class') != 'unjustified_duplicate_held' for r in results)
    verification = {'round': (previous.get('verification', {}).get('round', 1) + 1) if previous else 1,
                    'previous_report': spec.get('previous_report'),
                    'new_checks': sum('invalidation_reason' in r for r in results),
                    'reused_checks': counts.get('reused', 0), 'held_checks': held,
                    'blocked_checks': counts.get('blocked', 0), 'observed_processes': processes,
                    'invalidation_reasons': reasons, 'duration_seconds': finished - started}
    report = {'schema_version': 1, 'batch_id': batch_id, 'snapshot': snapshot,
              'assembly_digest': spec.get('assembly_digest'),
              'plan_digest': plan_digest, 'plan_ref': (root / 'plan.json').relative_to(workspace).as_posix(),
              'status': status, 'inputs_unchanged': current, 'results': results,
              'groups': failure_groups(results), 'blocked_checks': [r['id'] for r in results if r['status'] == 'blocked'],
              'omitted_checks_with_reason': [], 'started_at': started, 'finished_at': finished,
              'counts': counts, 'verification': verification, 'diagnostics': diagnostics,
              'usage': {'tokens': None, 'source': 'unmeasured', 'check_processes': processes,
                        'unobserved_execution_attempts': sum(r['status'] == 'error' and not r.get('receipt') for r in results)}}
    report['report_digest'] = digest(report)
    atomic_json(root / 'report.json', report)
    return {'report': (root / 'report.json').relative_to(workspace).as_posix(),
            'status': status, 'snapshot_digest': snapshot['digest'],
            'counts': counts, 'verification': verification, 'diagnostics': diagnostics,
            'groups': report['groups'], 'blocked_checks': report['blocked_checks'],
            'tokens': None, 'logs': 'Full logs retained in the referenced execution receipts'}


def load_report(workspace: Path, relative: str, *, require_current: bool = True) -> dict:
    workspace = Path(workspace).resolve()
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
        validated = {}
        for row in report['results']:
            if row['status'] not in {'passed', 'reused'}:
                raise ValueError('Batch contains an unverified check')
            ref = row['receipt']
            if ref not in validated:
                validated[ref] = validate_execution(workspace, ref, run_id=report['snapshot']['run_id'],
                                                     revision=report['snapshot']['contract_revision'])
            receipt = validated[ref]
            check = expected[row['id']]
            _matches_check(workspace, check, receipt)
    return report


def repair_plan(workspace: Path, relative: str) -> dict:
    report = load_report(workspace, relative, require_current=False)
    return {'snapshot_digest': report['snapshot']['digest'], 'groups': report['groups'],
            'blocked_checks': report['blocked_checks'],
            'rule': 'QA confirms suspected roots and assigns one owner per shared cause; rerun failed, impacted and critical checks after repair.'}


def build_decision(workspace: Path, manifest: dict, previous: dict | None = None) -> dict:
    """Reuse only an observed, byte-identical immutable build output."""
    required = ('artifact', 'source_hashes', 'toolchain_hashes', 'flags',
                'dependency_hashes', 'environment_hash')
    if not isinstance(manifest, dict) or any(key not in manifest for key in required):
        raise ValueError('Build manifest lacks source, toolchain, flags or environment')
    for key in ('source_hashes', 'toolchain_hashes', 'dependency_hashes'):
        values = manifest[key]
        if not isinstance(values, dict) or not values or any(not isinstance(v, str) or not v for v in values.values()):
            raise ValueError('Build manifest needs complete ' + key)
    if not isinstance(manifest['flags'], list) or any(not isinstance(flag, str) for flag in manifest['flags']):
        raise ValueError('Invalid build flags')
    if not isinstance(manifest['environment_hash'], str) or not manifest['environment_hash']:
        raise ValueError('Missing build environment hash')
    artifact = _path(workspace, manifest['artifact'], existing=False)
    key = digest({field: manifest[field] for field in required})
    current_sha = file_hash(artifact) if artifact.is_file() else None
    receipt_ok = False
    if previous and previous.get('receipt') and previous.get('run_id') and type(previous.get('contract_revision')) is int:
        try:
            receipt = validate_execution(workspace, previous['receipt'],
                                         run_id=previous['run_id'], revision=previous['contract_revision'])
            # The validator already rehashes each subject; require input coverage
            # here rather than trusting caller-declared hashes or hashing twice.
            receipt_ok = (receipt['subject_files'].get(manifest['artifact']) == current_sha and
                          all(receipt['subject_files'].get(path) == sha
                              for group in ('source_hashes', 'toolchain_hashes', 'dependency_hashes')
                              for path, sha in manifest[group].items()))
        except (ValueError, OSError, KeyError, TypeError):
            pass
    reusable = bool(receipt_ok and previous.get('build_key') == key and
                    previous.get('artifact_sha256') == current_sha and current_sha)
    return {'reuse': reusable, 'reason': 'same_source_toolchain_flags_dependencies_environment_and_output'
            if reusable else 'changed_or_unverified_build_input',
            'build_key': key, 'artifact_sha256': current_sha,
            'evidence_refs': [previous['receipt']] if reusable and previous.get('receipt') else [],
            'limits_of_observation': 'Declared input/output bytes are freshly verified; input completeness, flags and custom environment remain caller-declared'}
