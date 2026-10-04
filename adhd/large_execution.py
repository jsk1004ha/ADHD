"""Observed Codex CLI workers in assigned workspaces; no competing child loops.

Only the controller owns processes/commits/staging. Host hooks never launch
processes. The CLI sandbox is the host boundary; semantic/path submission
checks are additional post-execution guards, not an OS sandbox.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import time
import uuid

from .core import atomic_json, atomic_text
from .large_prompts import worker_prompt
from .large_tasks import _covers
from .models import route
from .runner import executable_command, terminate_tree, usage_from_log
from filelock import FileLock


def preflight(codex: str) -> list[str]:
    binary = executable_command(codex)
    check = subprocess.run(binary + ['exec', '--help'], capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=20)
    advertised = check.stdout + check.stderr
    if check.returncode or any(flag not in advertised for flag in ('--json', '--sandbox', '--output-last-message', '--cd')):
        raise ValueError('CLI does not advertise required worker flags; no model request made')
    return binary


def _git(workspace: Path, *argv: str) -> str:
    result = subprocess.run(['git', '-C', str(workspace), *argv], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=30)
    if result.returncode:
        raise ValueError('Git worker operation failed: ' + result.stderr[:1800])
    return result.stdout.strip()


def _owned(path: str, scopes: list[str]) -> bool:
    return any(_covers(scope, path) for scope in scopes)


def commit_owned(card: dict) -> str:
    workspace = Path(card['workspace'])
    # Rename pairs complicate status parsing. The final submitted commit guard
    # still checks both paths, but inspect with no renames before staging.
    raw = subprocess.run(['git', '-C', str(workspace), '-c', 'core.quotepath=false',
                          'status', '--porcelain=v1', '-z', '--no-renames'], capture_output=True,
                         timeout=30, check=True).stdout.decode('utf-8').split('\x00')
    changed = [v[3:] for v in raw if v]
    if any(not _owned(path, card['write_paths']) or _owned(path, card.get('forbidden_paths', [])) for path in changed):
        raise ValueError('Worker changed an unassigned path')
    if changed:
        _git(workspace, 'add', '-A', '--', *changed)
        _git(workspace, '-c', 'user.name=ADHD Worker', '-c', 'user.email=adhd@localhost',
             '-c', 'commit.gpgsign=false', 'commit', '-m', 'ADHD task ' + card['task_id'])
    return _git(workspace, 'rev-parse', 'HEAD')


def run_workers(store, **kwargs) -> dict:
    with FileLock(str(store.directory / 'executor.lock'), timeout=1):
        return _run_workers(store, **kwargs)


def _run_workers(store, *, codex: str = 'codex', limits: dict | None = None,
                worker_timeout: int = 900, max_seconds: int = 3600) -> dict:
    binary = preflight(codex)
    if not 1 <= worker_timeout <= 1800 or not 1 <= max_seconds <= 86400:
        raise ValueError('Invalid worker/process wall time limit')
    owner = 'cli-director-' + uuid.uuid4().hex
    active, outcomes = {}, []
    started = time.monotonic()
    choice = route('adhd-implementer')
    process_root = store.directory / 'processes'
    process_root.mkdir(parents=True, exist_ok=True)
    recovery = store.reconcile()

    def launch(card):
        task_id, generation = card['task_id'], card['generation']
        prefix = process_root / (task_id + '-' + str(generation))
        prompt = worker_prompt(card)
        atomic_text(prefix.with_suffix('.prompt.txt'), prompt)
        stdin = prefix.with_suffix('.prompt.txt').open('rb')
        stdout = prefix.with_suffix('.stdout.jsonl').open('wb')
        stderr = prefix.with_suffix('.stderr.log').open('wb')
        argv = [*binary, '-a', 'never', '-m', choice['model'], '-c',
                'model_reasoning_effort=' + json.dumps(choice['effort']),
                'exec', '--sandbox', 'workspace-write', '--json',
                '--skip-git-repo-check', '-C', card['workspace'],
                '-o', str(prefix.with_suffix('.answer.txt')), '-']
        kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        nonce = uuid.uuid4().hex
        proc = None
        try:
            # Existing installed releases also recognize this exclusion marker.
            # It excludes child ADHD loops; this is not a legacy run owner.
            proc = subprocess.Popen(argv, cwd=card['workspace'], stdin=stdin, stdout=stdout, stderr=stderr,
                                    env={**os.environ, 'ADHD_EXEC_OWNER': 'legacy', 'ADHD_LARGE_WORKER': '1'}, **kwargs)
            store.attach_process(task_id, generation, proc.pid, nonce)
            atomic_json(prefix.with_suffix('.process.json'), {'pid': proc.pid, 'nonce': nonce,
                        'argv': argv, 'workspace': card['workspace'], 'task_id': task_id,
                        'generation': generation, 'started_at': time.time()})
            return {'proc': proc, 'card': card, 'nonce': nonce, 'streams': (stdin, stdout, stderr),
                    'prefix': prefix, 'started': time.monotonic()}
        except BaseException:
            if proc is not None:
                terminate_tree(proc)
            for stream in (stdin, stdout, stderr):
                stream.close()
            raise

    def finish(item, *, cancelled=False):
        proc, card = item['proc'], item['card']
        if proc.poll() is None:
            raise ValueError('Cannot release a live worker')
        for stream in item['streams']:
            stream.close()
        store.record_termination(card['task_id'], card['generation'], item['nonce'], proc.returncode)
        usage, measured = usage_from_log(item['prefix'].with_suffix('.stdout.jsonl'))
        row = {'task_id': card['task_id'], 'generation': card['generation'],
               'exit_code': proc.returncode, 'logs': str(item['prefix']),
               'usage': usage if measured else None, 'usage_source': 'observed' if measured else 'unmeasured'}
        if cancelled or proc.returncode != 0:
            store.request_cancel(card['task_id'])
            store.confirm_cancel(card['task_id'], card['generation'], {'nonce': item['nonce'], 'exit_code': proc.returncode})
            row['status'] = 'cancelled' if cancelled else 'failed'
        else:
            try:
                commit = commit_owned(card) if store.status()['mode'] == 'git' else None
                unchanged = (store.status()['mode'] == 'git' and commit == card['base_commit'])
                store.submit(card['task_id'], card['generation'], commit, owner=card['owner'],
                             no_op_reason='Requested behavior already present; verify in central batch' if unchanged else None)
                store.stage(card['task_id'])
                row.update(status='provisionally_staged', commit=commit)
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                row.update(status='needs_repair', detail=str(error)[:1800])
                # Scope/integration faults remain visible; never auto-resolve them.
        outcomes.append(row)
        atomic_json(item['prefix'].with_suffix('.result.json'), row)

    try:
        while True:
            current = store.status()
            tasks = current['tasks']
            for task_id, item in list(active.items()):
                timed_out = time.monotonic() - item['started'] >= worker_timeout
                cancelled = tasks.get(task_id, {}).get('state') == 'cancel_requested'
                exhausted = time.monotonic() - started >= max_seconds
                if timed_out or cancelled or exhausted:
                    store.request_cancel(task_id)
                    terminate_tree(item['proc'])
                if item['proc'].poll() is not None:
                    finish(item, cancelled=timed_out or cancelled or exhausted)
                    del active[task_id]
            if time.monotonic() - started >= max_seconds:
                break
            actual_limits = dict(limits or {})
            current = store.status()
            session = current['spec'].get('native_session')
            if session:
                from .native import folder, bounded_json
                native = bounded_json(folder(session) / 'state.json', 8 * 1024 * 1024)
                if (native.get('run_id') != store.run_id or native.get('workspace') != str(store.workspace)
                        or native['contract_hash'] != current['spec']['contract_hash']
                        or native['status'] not in {'working', 'revising'}):
                    for task_id, item in active.items():
                        store.request_cancel(task_id)
                        terminate_tree(item['proc'])
                    break
                readers = sum(r['status'] == 'running' for r in native['children'].values())
                capacity = max(0, native['policy']['max_children'] - readers)
                calls = max(0, native['policy']['max_total_children'] - native['total_children']
                            - native['policy']['max_review_children'] - current['dispatch_count'])
                if native.get('pending_turn_ids'):
                    capacity = 0
                for key, cap in [('host_slots', capacity), ('policy_slots', capacity), ('budget_slots', calls)]:
                    actual_limits[key] = min(actual_limits.get(key, 1), cap)
            cards = store.dispatch(owner, actual_limits)
            for card in cards:
                try:
                    active[card['task_id']] = launch(card)
                except Exception as error:
                    # No process was successfully admitted. Retain a visible lease
                    # for reconciliation rather than fabricate termination evidence.
                    outcomes.append({'task_id': card['task_id'], 'status': 'blocked', 'detail': str(error)[:1800]})
                    row = store.status()['tasks'][card['task_id']]
                    if not row.get('process'):
                        store.abandon_dispatch(card['task_id'], card['generation'], str(error)[:1800])
            if not active:
                break
            time.sleep(.15)
    finally:
        for task_id, item in list(active.items()):
            store.request_cancel(task_id)
            terminate_tree(item['proc'])
            if item['proc'].poll() is not None:
                finish(item, cancelled=True)
    final = store.status()
    assembled = all(r['state'] in {'provisionally_staged', 'validation_pending', 'verified', 'accepted'} for r in final['tasks'].values())
    status = ('provisionally_staged' if assembled else 'needs_repair' if
              any(r['state'] == 'needs_repair' for r in final['tasks'].values()) else 'blocked')
    return {'run_id': store.run_id, 'status': status, 'outcomes': outcomes, 'state': final, 'recovery': recovery,
            'elapsed_seconds': time.monotonic() - started,
            'completion': 'Provisional assembly only; batch validation and independent native acceptance still required'}
