"""Common native/legacy workspace ownership with bounded terminal recovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from filelock import FileLock

from .core import atomic_json, digest, read_json, store

TERMINAL = {'complete', 'blocked', 'cancelled', 'budget_exhausted', 'failed', 'paused'}


def lease_path(workspace: Path) -> Path:
    return store() / 'locks' / (digest(str(workspace.resolve()))[:24] + '.json')


def _terminal_owner(row: dict) -> bool:
    owner, run_id = row.get('owner'), row.get('run_id', '')
    if owner == 'native' and run_id.startswith('native:'):
        parts = run_id.split(':')
        if len(parts) != 3:
            return False
        state = read_json(store() / 'native' / parts[1] / 'state.json', {})
        return (state.get('run_id') == parts[2] and state.get('status') in TERMINAL
                and not any(child.get('status') == 'running'
                            for child in state.get('children', {}).values()))
    if owner == 'legacy':
        state = read_json(store() / 'runs' / run_id / 'state.json', {})
        return state.get('id') == run_id and state.get('status') in TERMINAL
    return False


def acquire(workspace: Path, run_id: str, *, owner: str) -> int:
    if owner not in {'native', 'legacy'} or not run_id:
        raise ValueError('Invalid ADHD lease owner')
    path = lease_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.guard', timeout=5):
        previous = read_json(path)
        if previous and previous.get('run_id') == run_id and previous.get('owner') == owner:
            return int(previous.get('generation', 1))
        if previous:
            if not _terminal_owner(previous):
                raise ValueError('Another ADHD run owns this workspace; inspect the live owner before recovery')
            path.unlink()
        epoch_path = path.with_suffix('.generation.json')
        generation = int(read_json(epoch_path, {}).get('generation', 0)) + 1
        atomic_json(epoch_path, {'generation': generation})
        row = {'run_id': run_id, 'owner': owner, 'generation': generation,
               'created': time.time(), 'last_observed_at': time.time(),
               'pid': os.getpid() if owner == 'legacy' else None}
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(row, stream)
        return generation


def renew(workspace: Path, run_id: str, generation: int) -> None:
    path = lease_path(workspace)
    with FileLock(str(path) + '.guard', timeout=5):
        row = read_json(path, {})
        if row.get('run_id') != run_id or row.get('generation') != generation:
            raise ValueError('Lease generation changed')
        row['last_observed_at'] = time.time()
        atomic_json(path, row)


def release(workspace: Path, run_id: str, generation: int | None = None) -> bool:
    path = lease_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.guard', timeout=5):
        row = read_json(path, {})
        if row.get('run_id') != run_id:
            return False
        if generation is not None and int(row.get('generation', 1)) != generation:
            return False
        path.unlink(missing_ok=True)
        return True
