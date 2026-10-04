"""Read-only artifact admission in native hooks; no Git or process execution."""
from __future__ import annotations

import json
from pathlib import Path

from .core import digest, file_hash
from .large_tasks import LargeTaskStore
from .evidence import _path


def _store(state):
    ws = Path(state['workspace'])
    run_id = state['run_id']
    db = ws / '.adhd' / 'large' / run_id / 'state.sqlite3'
    if not db.is_file() or db.is_symlink():
        raise ValueError('Initialize the actual large-task store through the normal CLI first')
    return LargeTaskStore(ws, run_id)


def attach(state: dict, payload: dict) -> dict:
    if payload.get('run_id') != state['run_id']:
        raise ValueError('Large graph belongs to another native run')
    store = _store(state)
    spec = store.status()['spec']
    if spec['contract_hash'] != state['contract_hash'] or spec['contract_revision'] != state['intent_version']:
        raise ValueError('Large graph contract is stale')
    if spec['original_turns'] != [{'id': r['turn_id'], 'text': r['text']} for r in state['prompts']]:
        raise ValueError('Large graph original intent differs from observed native turns')
    if {r['id'] for r in spec['requirements']} != {r['id'] for r in state['contract']['criteria']}:
        raise ValueError('Large graph omits a native requirement')
    return {'run_id': state['run_id'], 'spec_digest': digest(spec), 'adapter': 'observed_cli_workspaces',
            'app_multiwriter_admission': 'unavailable_without_task_cwd_correlation'}


def candidate_evidence(state: dict, payload: dict) -> tuple[dict, list[str]]:
    from .validation_batch import load_report
    evidence = payload.get('large_task_evidence')
    if not isinstance(evidence, dict) or evidence.get('run_id') != state['run_id']:
        raise ValueError('Attached large task needs current frozen batch evidence')
    store = _store(state)
    row = store.status()
    if row['spec']['contract_hash'] != state['contract_hash'] or row['spec']['contract_revision'] != state['intent_version']:
        raise ValueError('Reconcile large graph after native intent amendment')
    if any(t['state'] not in {'verified', 'accepted'} for t in row['tasks'].values()):
        raise ValueError('All required parts need current batch verification')
    frozen_ref = row.get('frozen')
    if not frozen_ref or frozen_ref['snapshot_digest'] != evidence.get('snapshot_digest'):
        raise ValueError('Large assembly snapshot is stale')
    if file_hash(store.frozen_path) != frozen_ref['sha256']:
        raise ValueError('Frozen assembly manifest changed')
    frozen = store.assert_snapshot_no_git(frozen_ref['snapshot_digest'])
    if digest({k: v for k, v in frozen.items() if k != 'snapshot_digest'}) != frozen['snapshot_digest']:
        raise ValueError('Invalid frozen manifest')
    ws = Path(state['workspace'])
    staging = Path(frozen['staging_workspace'])
    if not staging.resolve().is_relative_to(store.directory.resolve()):
        raise ValueError('Staging workspace leaves assigned run')
    files, hashes = [], {}
    for rel, sha in frozen['files'].items():
        path = _path(staging, rel)
        if file_hash(path) != sha:
            raise ValueError('Frozen assembly file changed: ' + rel)
    report_rel = evidence.get('batch_report')
    report = load_report(staging, report_rel)
    if (report['assembly_digest'] != frozen['snapshot_digest']
            or report['snapshot']['contract_hash'] != state['contract_hash']
            or report['snapshot']['contract_revision'] != state['intent_version']
            or not set(frozen['files']) <= set(report['snapshot']['files'])):
        raise ValueError('Batch evidence belongs to another assembly')
    plan = json.loads(_path(staging, report['plan_ref']).read_text(encoding='utf-8'))
    if set(plan.get('requirements', [])) != {r['id'] for r in state['contract']['criteria']}:
        raise ValueError('Batch does not cover all original native requirements')
    for rel in [report_rel, report['plan_ref'],
                str(Path(report_rel).parent / 'snapshot.json')]:
        path = _path(staging, rel)
        files.append(path.relative_to(ws).as_posix())
        hashes[path.relative_to(ws).as_posix()] = file_hash(path)
    for check in report['results']:
        receipt_path = _path(staging, check['receipt'])
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        for path in [receipt_path, receipt_path.parent / 'check.stdout.jsonl',
                     receipt_path.parent / 'check.stderr.log', _path(staging, receipt['subject_manifest'])]:
            # Empty logs stay receipt-hash-bound; native snapshots require nonempty leaves.
            hashes[path.relative_to(ws).as_posix()] = file_hash(path)
    result = {'run_id': state['run_id'], 'snapshot_digest': frozen['snapshot_digest'],
              'batch_report': report_rel, 'staging_workspace': str(staging),
              'frozen_sha256': frozen_ref['sha256'], 'report_sha256': file_hash(_path(staging, report_rel)),
              'snapshot_manifest': store.frozen_path.relative_to(ws).as_posix(),
              'evidence_hashes': hashes}
    return result, list(dict.fromkeys(files))


def validate_admission(state: dict) -> None:
    candidate = state.get('candidate') or {}
    if state.get('large_task'):
        expected = candidate.get('large_task_evidence')
        observed, _ = candidate_evidence(state, {'large_task_evidence': expected})
        if observed != expected:
            raise ValueError('Large-task batch admission changed after review')


def invalidate_intent(state: dict) -> None:
    _store(state).invalidate_intent(state['contract_hash'], state['intent_version'])


def accept_verified(state: dict) -> None:
    validate_admission(state)
    proof = state['candidate']['large_task_evidence']
    _store(state).accept_verified(proof['snapshot_digest'], state['review_receipt'])
