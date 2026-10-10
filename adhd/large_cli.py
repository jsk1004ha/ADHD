"""Normal CLI tools for durable work packages; commands never run in hooks."""
from __future__ import annotations

import json
from pathlib import Path

from .core import atomic_json
from .large_tasks import LargeTaskStore
from .native import bounded_json, folder


def add_parsers(sub):
    q = sub.add_parser('large', help='Durable large-task DAG, isolated workers and batch assembly')
    q.add_argument('action', choices=['init', 'status', 'events', 'dispatch', 'run-workers',
                                     'interface', 'submit', 'stage', 'freeze', 'validate',
                                     'repairs', 'cancel', 'confirm-cancel', 'amend', 'reconcile', 'publish'])
    q.add_argument('--workspace', type=Path, default=Path.cwd())
    q.add_argument('--run-id', required=True)
    q.add_argument('--session')
    q.add_argument('--payload-file', type=Path)
    q.add_argument('--task-id')
    q.add_argument('--generation', type=int)
    q.add_argument('--owner', default='director')
    q.add_argument('--codex', default='codex')
    q.add_argument('--worker-timeout', type=int, default=900)
    q.add_argument('--max-seconds', type=int, default=3600)
    q.add_argument('--out', help='New workspace-relative JSON output path')
    q = sub.add_parser('batch', help='One shared post-assembly validation batch for ordinary/large tasks')
    q.add_argument('action', choices=['run', 'diagnose', 'verify', 'repairs', 'build-decision'])
    q.add_argument('--workspace', type=Path, default=Path.cwd())
    q.add_argument('--spec-file', type=Path)
    q.add_argument('--report')
    q.add_argument('--previous-build', type=Path)


def _payload(args):
    if not args.payload_file:
        return {}
    value = bounded_json(args.payload_file, 2 * 1024 * 1024)
    if not isinstance(value, dict):
        raise ValueError('Payload must be an object')
    return value


def _native_spec(args, spec):
    if not args.session:
        return spec
    state = bounded_json(folder(args.session) / 'state.json', 2 * 1024 * 1024)
    if (state['workspace'] != str(args.workspace.resolve()) or state['run_id'] != args.run_id
            or state['status'] not in {'working', 'revising'} or state.get('pending_turn_ids')
            or not state.get('plan') or state['plan']['intent_version'] != state['intent_version']):
        raise ValueError('Large task requires current native intent and deep plan')
    spec = {**spec, 'run_id': state['run_id'], 'contract_revision': state['intent_version'],
            'contract_hash': state['contract_hash'],
            'original_turns': [{'id': r['turn_id'], 'text': r['text']} for r in state['prompts']],
            'native_session': args.session}
    policy = dict(spec.get('execution_policy', {}))
    policy['max_workers'] = min(policy.get('max_workers', 6), state['policy']['max_children'])
    policy['max_worker_calls'] = min(policy.get('max_worker_calls', 36),
                                     state['policy']['max_total_children'] - state['total_children'])
    policy['review_reserve'] = max(policy.get('review_reserve', 3), state['policy']['max_review_children'])
    spec['execution_policy'] = policy
    if ({r['id'] for r in spec.get('requirements', [])} != {r['id'] for r in state['contract']['criteria']}):
        raise ValueError('Large graph must cover every native requirement')
    return spec


def execute(args):
    from .validation_batch import run_batch, load_report, repair_plan, build_decision, diagnose_plan
    workspace = args.workspace.resolve()
    if args.command == 'batch':
        if args.action == 'build-decision':
            if not args.spec_file:
                raise ValueError('Build decision needs --spec-file')
            previous = bounded_json(args.previous_build, 200000) if args.previous_build else None
            return build_decision(workspace, bounded_json(args.spec_file, 200000), previous)
        if args.action in {'run', 'diagnose'}:
            if not args.spec_file:
                raise ValueError('Batch needs --spec-file')
            spec = bounded_json(args.spec_file, 2 * 1024 * 1024)
            return diagnose_plan(spec) if args.action == 'diagnose' else run_batch(spec, workspace)
        if not args.report:
            raise ValueError('Batch requires --report')
        if args.action == 'repairs':
            return repair_plan(workspace, args.report)
        report = load_report(workspace, args.report)
        return {'verified': True, 'snapshot_digest': report['snapshot']['digest'],
                'checks': len(report['results']), 'tokens': report['usage']['tokens']}
    store = LargeTaskStore(workspace, args.run_id)
    value = _payload(args)
    if args.action == 'init':
        result = store.initialize(_native_spec(args, value))
    elif args.action == 'status':
        result = store.status()
    elif args.action == 'events':
        result = store.events()
    elif args.action == 'dispatch':
        result = store.dispatch(args.owner, value.get('limits', value))
    elif args.action == 'run-workers':
        from .large_execution import run_workers
        limits = dict(value.get('limits', value))
        if args.session:
            native = bounded_json(folder(args.session) / 'state.json', 2 * 1024 * 1024)
            if (native['run_id'] != args.run_id or native['workspace'] != str(workspace)
                    or native.get('pending_turn_ids') or native['status'] not in {'working', 'revising'}):
                raise ValueError('Native coordinator is not current and active')
            readers = sum(r['status'] == 'running' for r in native['children'].values())
            available = max(0, native['policy']['max_children'] - readers)
            remaining = max(0, native['policy']['max_total_children'] - native['total_children'] - native['policy']['max_review_children'])
            for key, cap in [('host_slots', available), ('policy_slots', available), ('budget_slots', remaining)]:
                limits[key] = min(limits.get(key, 1), cap)
        result = run_workers(store, codex=args.codex, limits=limits,
                             worker_timeout=args.worker_timeout, max_seconds=args.max_seconds)
    elif args.action == 'interface':
        result = store.publish_interface(args.task_id, args.generation, value['interface_id'], value['revision'], value['contract'])
    elif args.action == 'submit':
        result = store.submit(args.task_id, args.generation, value.get('commit'), owner=value.get('owner'),
                              no_op_reason=value.get('no_op_reason'))
    elif args.action == 'stage':
        result = store.stage(args.task_id)
    elif args.action == 'freeze':
        result = store.freeze()
    elif args.action == 'cancel':
        result = store.request_cancel(args.task_id)
    elif args.action == 'confirm-cancel':
        result = store.confirm_cancel(args.task_id, args.generation, value)
    elif args.action == 'amend':
        result = store.amend(_native_spec(args, value))
    elif args.action == 'reconcile':
        result = store.reconcile()
    elif args.action == 'validate':
        state = store.status()
        frozen = store.assert_snapshot(state['frozen']['snapshot_digest']) if state.get('frozen') else store.freeze()
        staging = Path(frozen['staging_workspace'])
        spec = {**value, 'run_id': args.run_id, 'contract_revision': frozen['contract_revision'],
                'contract_hash': frozen['contract_hash'], 'assembly_digest': frozen['snapshot_digest'],
                'snapshot_paths': sorted(frozen['files']),
                'requirements': [r['id'] for r in state['spec']['requirements']]}
        store.declare_validation_plan(spec)
        result = run_batch(spec, staging)
        result['staging_workspace'] = str(staging)
        result['assembly_digest'] = frozen['snapshot_digest']
        if result['status'] == 'passed':
            store.mark_verified(frozen['snapshot_digest'], result['report'])
    elif args.action == 'repairs':
        groups = value.get('groups')
        if not isinstance(groups, list) or any(g.get('confidence') != 'confirmed' for g in groups):
            raise ValueError('QA must confirm root causes and owners before queueing repairs')
        result = store.queue_repairs(groups)
    elif args.action == 'publish':
        result = publish(store, value)
    else:
        raise ValueError('Unknown large operation')
    if args.out:
        from .evidence import _path
        target = _path(workspace, args.out, existing=False)
        if target.exists():
            raise ValueError('Refusing to overwrite an output artifact')
        atomic_json(target, result)
    return result


def publish(store, value):
    """Copy verified results to explicit new destinations; preserve existing originals."""
    from .evidence import _path
    import shutil
    state = store.status()
    if any(r['state'] not in {'verified', 'accepted'} for r in state['tasks'].values()):
        raise ValueError('Only validated assembly may be published locally')
    frozen = store.assert_snapshot(state['frozen']['snapshot_digest'])
    files = value.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError('Declare source-to-new-destination local artifact mapping')
    targets = {}
    for source, destination in files.items():
        if source not in frozen['files']:
            raise ValueError('Publication source is outside frozen assembly')
        target = _path(store.workspace, destination, existing=False)
        if target.exists() or str(target) in targets:
            raise ValueError('Publication must preserve existing originals and unique destinations')
        targets[str(target)] = source
    for target_name, source in targets.items():
        target = Path(target_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(frozen['staging_workspace']) / source, target)
    return {'published': {source: target for target, source in targets.items()},
            'snapshot_digest': frozen['snapshot_digest'], 'acceptance': 'pending independent native review'}
