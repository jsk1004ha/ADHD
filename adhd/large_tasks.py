"""Durable large-task queue and isolated integration workspaces.

The queue controls ownership and evidence currentness. Git worktrees isolate
files, but they do not sandbox an agent's tools or external side effects.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Iterator

from .core import atomic_json, digest, file_hash


NEEDS = {'contract_ready', 'artifact_ready'}
ACTIVE = {'running', 'cancel_requested'}
STAGED = {'provisionally_staged', 'validation_pending', 'verified', 'accepted'}
REQUEUEABLE = {'planned', 'ready', 'superseded'}
FORBIDDEN = {'.git', '.adhd', '.omx'}
MAX_WORKERS = 6


def _now() -> float:
    return time.time()


def _clean_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', value):
        raise ValueError(f'Invalid {label}')
    return value


def _relative(value: str, *, pattern: bool = False) -> str:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Expected a relative path')
    value = value.replace('\\', '/')
    suffix = pattern and value.endswith('/**')
    stem = value[:-3] if suffix else value
    if (stem.startswith('/') or ':' in stem or any(p in {'', '.', '..'} for p in stem.split('/'))
            or any(c in stem for c in '*?[]') or stem.split('/')[0] in FORBIDDEN):
        raise ValueError('Unsafe owned path: ' + value)
    return value


def _covers(pattern: str, name: str) -> bool:
    return name == pattern[:-3] or name.startswith(pattern[:-3] + '/') if pattern.endswith('/**') else name == pattern


def _overlap(left: str, right: str) -> bool:
    return (_covers(left, right.removesuffix('/**'))
            or _covers(right, left.removesuffix('/**')))


def _safe_member(root: Path, name: str) -> Path:
    _relative(name)
    path = root
    for part in name.split('/'):
        path = path / part
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Workspace member crosses a link: ' + name)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Workspace member escapes workspace')
    return path


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update({'GIT_AUTHOR_NAME': 'ADHD Staging', 'GIT_AUTHOR_EMAIL': 'adhd@local.invalid',
                'GIT_COMMITTER_NAME': 'ADHD Staging', 'GIT_COMMITTER_EMAIL': 'adhd@local.invalid'})
    proc = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                           '-c', 'core.untrackedCache=false', '-C', str(cwd), *args],
                          input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=env, timeout=90, check=False)
    if proc.returncode:
        raise ValueError('Git operation failed: ' + proc.stderr.decode('utf-8', 'replace')[:1200])
    return proc.stdout


def _names(data: bytes) -> list[str]:
    return [p.decode('utf-8', 'surrogateescape').replace('\\', '/')
            for p in data.split(b'\0') if p]


def _manifest(root: Path, *, git: bool) -> dict[str, str]:
    if git:
        names = _names(_git(root, 'ls-files', '-z'))
    else:
        names = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in FORBIDDEN and not (Path(directory) / d).is_symlink())
            names.extend((Path(directory) / f).relative_to(root).as_posix() for f in sorted(files))
            if len(names) > 20_000:
                raise ValueError('Workspace manifest exceeds 20000 files')
    if len(names) > 20_000:
        raise ValueError('Workspace manifest exceeds 20000 files')
    result = {}
    for name in names:
        path = _safe_member(root, name)
        if not path.is_file():
            raise ValueError('Manifest member is not a regular file: ' + name)
        result[name] = file_hash(path)
    return result


def _visible_inventory(root: Path) -> dict[str, str]:
    """Filesystem inventory a hook can recheck without invoking Git."""
    excluded = FORBIDDEN | {'.venv', 'venv', 'node_modules', '__pycache__',
                           '.pytest_cache', '.mypy_cache', 'dist', 'build'}
    names = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        prefix = Path(directory).relative_to(root).as_posix()
        prefix = '' if prefix == '.' else prefix + '/'
        if any((Path(directory) / name).is_symlink() for name in dirs):
            raise ValueError('Visible inventory contains a directory link')
        dirs[:] = sorted(name for name in dirs if name not in excluded)
        names.extend(prefix + name for name in sorted(files) if name not in excluded)
        if len(names) > 20_000:
            raise ValueError('Visible inventory exceeds 20000 files')
    return {name: file_hash(_safe_member(root, name)) for name in sorted(names)}


def _validate_spec(spec: dict[str, Any], run_id: str) -> dict[str, Any]:
    if not isinstance(spec, dict) or spec.get('run_id') != run_id:
        raise ValueError('Large-task spec must bind the run ID')
    if not isinstance(spec.get('contract_revision'), int) or spec['contract_revision'] < 1:
        raise ValueError('Expected a positive contract revision')
    if not isinstance(spec.get('contract_hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', spec['contract_hash']):
        raise ValueError('Expected a SHA-256 contract hash')
    policy = spec.get('execution_policy', {})
    if not isinstance(policy, dict):
        raise ValueError('Execution policy must be a mapping')
    policy_defaults = {'max_workers': MAX_WORKERS, 'max_worker_calls': 36, 'max_seconds': 3600,
                       'max_repair_waves': 3, 'review_reserve': 3}
    for key, default in policy_defaults.items():
        value = policy.get(key, default)
        if not isinstance(value, int) or value < 0 or (key != 'review_reserve' and value == 0):
            raise ValueError('Invalid execution policy: ' + key)
    if policy.get('max_workers', MAX_WORKERS) > MAX_WORKERS:
        raise ValueError('Large-task policy cannot exceed six workers on this host')
    if policy.get('review_reserve', 3) >= policy.get('max_worker_calls', 36):
        raise ValueError('Execution policy must leave worker and review capacity')
    turns, reqs, tasks = spec.get('original_turns'), spec.get('requirements'), spec.get('tasks')
    if not all(isinstance(rows, list) and rows for rows in (turns, reqs, tasks)):
        raise ValueError('Original turns, requirements and tasks are required')
    turn_map = {}
    for row in turns:
        if not isinstance(row, dict):
            raise ValueError('Invalid original turn')
        ident = _clean_id(row.get('id'), 'turn ID')
        if ident in turn_map or not isinstance(row.get('text'), str) or not row['text'].strip():
            raise ValueError('Duplicate or empty original turn')
        turn_map[ident] = row['text']
    req_map = {}
    for row in reqs:
        if not isinstance(row, dict):
            raise ValueError('Invalid requirement')
        ident = _clean_id(row.get('id'), 'requirement ID')
        excerpt = row.get('source_excerpt')
        if (ident in req_map or not isinstance(row.get('text'), str) or not row['text'].strip()
                or row.get('source_turn_id') not in turn_map or not isinstance(excerpt, str)
                or not excerpt.strip() or excerpt not in turn_map[row['source_turn_id']]):
            raise ValueError('Requirement lacks exact original-text provenance: ' + ident)
        req_map[ident] = row
    task_map = {}
    for row in tasks:
        if not isinstance(row, dict):
            raise ValueError('Invalid task')
        ident = _clean_id(row.get('id'), 'task ID')
        if (ident in task_map or not isinstance(row.get('package'), str) or not row['package'].strip()
                or not isinstance(row.get('objective'), str) or not row['objective'].strip()):
            raise ValueError('Duplicate or incomplete task: ' + ident)
        required = row.get('requirements')
        paths = row.get('write_paths')
        if (not isinstance(required, list) or not required or not set(required) <= req_map.keys()
                or not isinstance(paths, list) or not paths or len(paths) != len(set(paths))):
            raise ValueError('Task needs requirement IDs and distinct owned paths: ' + ident)
        for path in paths:
            _relative(path, pattern=True)
        for path in row.get('forbidden_paths', []):
            _relative(path, pattern=True)
            if any(_overlap(path, owned) for owned in paths):
                raise ValueError('Task forbidden path overlaps its ownership: ' + ident)
        resources = row.get('shared_resources', [])
        if not isinstance(resources, list) or any(not isinstance(v, str) or not v.strip() for v in resources):
            raise ValueError('Invalid shared resource list')
        for field in ('priority', 'importance', 'estimated_cost'):
            value = row.get(field, 1 if field == 'estimated_cost' else 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1000:
                raise ValueError('Invalid task scheduling weight: ' + field)
            if field == 'estimated_cost' and value <= 0:
                raise ValueError('Estimated cost must be positive')
        task_map[ident] = row
    if set(req_map) - {r for row in tasks for r in row['requirements']}:
        raise ValueError('Unassigned original requirement')
    def serial_after(later: str, earlier: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if later in seen:
            return False
        seen.add(later)
        for dep in task_map[later].get('depends_on', []):
            source = dep.get('task_id', dep.get('task'))
            if source == earlier and dep.get('needs') in {'artifact_ready', 'accepted'}:
                return True
            if source in task_map and serial_after(source, earlier, set(seen)):
                return True
        return False
    for left_id, left in task_map.items():
        for right_id, right in task_map.items():
            if left_id >= right_id:
                continue
            if (any(_overlap(a, b) for a in left['write_paths'] for b in right['write_paths'])
                    and not serial_after(left_id, right_id) and not serial_after(right_id, left_id)):
                raise ValueError('Task write paths overlap: ' + left_id + ', ' + right_id)
    for ident, row in task_map.items():
        dependencies = row.get('depends_on', [])
        if not isinstance(dependencies, list):
            raise ValueError('Invalid task dependencies')
        seen = set()
        for dep in dependencies:
            if not isinstance(dep, dict):
                raise ValueError('Invalid dependency')
            source = dep.get('task_id', dep.get('task'))
            if dep.get('needs') == 'accepted':
                raise ValueError('accepted is final assembly acceptance, not a same-run dependency; use contract_ready or artifact_ready')
            if source == ident or source not in task_map or source in seen or dep.get('needs') not in NEEDS:
                raise ValueError('Invalid or duplicate dependency for ' + ident)
            seen.add(source)
            if dep.get('interface_id') is not None:
                _clean_id(dep['interface_id'], 'interface ID')
    visiting, visited = set(), set()
    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError('Large-task dependency cycle')
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep in task_map[task_id].get('depends_on', []):
            visit(dep.get('task_id', dep.get('task')))
        visiting.remove(task_id)
        visited.add(task_id)
    for ident in task_map:
        visit(ident)
    return copy.deepcopy(spec)


class LargeTaskStore:
    """One run's transactional task graph and isolated workspaces."""

    def __init__(self, workspace: Path, run_id: str):
        self.workspace = Path(workspace).resolve()
        self.run_id = _clean_id(run_id, 'run ID')
        self.directory = self.workspace / '.adhd' / 'large' / self.run_id
        self.db_path = self.directory / 'state.sqlite3'
        self.worktrees = self.directory / 'worktrees'
        self.frozen_path = self.directory / 'frozen.json'
        if not self.workspace.is_dir() or self.directory.is_symlink():
            raise ValueError('Large-task workspace must be a real directory')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.worktrees.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)')
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def _transaction(self) -> Iterator[tuple[sqlite3.Connection, dict | None]]:
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
            state = json.loads(row[0]) if row else None
            yield db, state
            if state is not None:
                db.execute('INSERT INTO state(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                           (json.dumps(state, ensure_ascii=False, sort_keys=True),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db: sqlite3.Connection, kind: str, detail: dict) -> None:
        db.execute('INSERT INTO events(at,kind,detail) VALUES(?,?,?)',
                   (_now(), kind, json.dumps(detail, ensure_ascii=False, sort_keys=True)))

    @staticmethod
    def _need_state(state: dict | None) -> dict:
        if state is None:
            raise ValueError('Large-task run is not initialized')
        return state

    def _git_mode(self) -> bool:
        try:
            top = Path(_git(self.workspace, 'rev-parse', '--show-toplevel').decode('utf-8', 'replace').strip()).resolve()
        except ValueError:
            return False
        if top != self.workspace:
            raise ValueError('Large-task Git workspace must be the checkout root')
        return True

    def _source_inputs(self, spec: dict) -> dict[str, str]:
        """Hash only declared non-Git inputs, including existing output bases."""
        names = set()
        for row in spec['tasks']:
            for pattern in row.get('read_paths', []) + row['write_paths']:
                _relative(pattern, pattern=True)
                if pattern.endswith('/**'):
                    folder = _safe_member(self.workspace, pattern[:-3])
                    if folder.exists():
                        if not folder.is_dir():
                            raise ValueError('Declared input prefix is not a directory')
                        for path in folder.rglob('*'):
                            if path.is_file():
                                names.add(path.relative_to(self.workspace).as_posix())
                else:
                    path = _safe_member(self.workspace, pattern)
                    if path.is_file():
                        names.add(pattern)
                if len(names) > 20_000:
                    raise ValueError('Declared source inputs exceed 20000 files')
        return {name: file_hash(_safe_member(self.workspace, name)) for name in sorted(names)}

    def _new_task(self, row: dict, contract_hash: str) -> dict:
        return {'id': row['id'], 'spec': copy.deepcopy(row), 'state': 'planned', 'owner': None,
                'generation': 0, 'workspace': None, 'base_commit': None, 'commit': None,
                'contract_hash': contract_hash, 'interfaces': {}, 'dependency_versions': {},
                'process': None, 'termination': None, 'staged_commit': None,
                'superseded_reason': None, 'repair_groups': []}

    def initialize(self, spec: dict) -> dict:
        spec = _validate_spec(spec, self.run_id)
        git = self._git_mode()
        base_commit = _git(self.workspace, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip() if git else None
        if git:
            dirty = _names(_git(self.workspace, 'diff', '--name-only', '-z', 'HEAD', '--'))
            dirty += _names(_git(self.workspace, 'ls-files', '--others', '--exclude-standard', '-z'))
            if any(any(_covers(path, name) for row in spec['tasks'] for path in row['write_paths']) for name in dirty):
                raise ValueError('Existing work overlaps large-task ownership')
        with self._transaction() as (db, state):
            if state is not None:
                if state['spec'] != spec:
                    raise ValueError('Run already initialized; use amend for a changed contract')
                return copy.deepcopy(state)
            new = {'run_id': self.run_id, 'workspace': str(self.workspace), 'mode': 'git' if git else 'files',
                   'spec': spec, 'base_commit': base_commit, 'staging_workspace': None,
                   'source_inputs': self._source_inputs(spec) if not git else None,
                   'staging_commit': base_commit, 'staged_order': [], 'staging_dirty': False,
                   'frozen': None, 'tasks': {r['id']: self._new_task(r, spec['contract_hash']) for r in spec['tasks']},
                   'created_at': _now(), 'updated_at': _now(),
                   'dispatch_count': 0, 'repair_wave_count': 0,
                   'policy_ceiling': {k: spec.get('execution_policy', {}).get(k, default)
                                      for k, default in {'max_workers': MAX_WORKERS,
                                                         'max_worker_calls': 36, 'max_seconds': 3600,
                                                         'max_repair_waves': 3, 'review_reserve': 3}.items()},
                   'validation_plan': None}
            state = new
            self._event(db, 'initialized', {'run_id': self.run_id, 'mode': new['mode'],
                                            'base_commit': base_commit, 'tasks': list(new['tasks'])})
            db.execute('INSERT INTO state(id,value) VALUES(1,?)', (json.dumps(new, ensure_ascii=False, sort_keys=True),))
            return copy.deepcopy(new)

    def status(self) -> dict:
        with closing(self._connect()) as db:
            row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
        return copy.deepcopy(self._need_state(json.loads(row[0]) if row else None))

    def events(self, after_id: int = 0, limit: int = 100) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute('SELECT id,at,kind,detail FROM events WHERE id>? ORDER BY id LIMIT ?',
                              (after_id, min(max(1, limit), 1000))).fetchall()
        return [{'id': ident, 'at': at, 'kind': kind, 'detail': json.loads(detail)}
                for ident, at, kind, detail in rows]

    @staticmethod
    def _plan_preserves_lock(locked: dict, plan: dict) -> None:
        baseline = locked['checks']
        if (plan['checks'][:len(baseline)] != baseline
                or not set(locked['mandatory_checks']) <= set(plan['mandatory_checks'])
                or set(plan['requirements']) != set(locked['requirements'])):
            raise ValueError('Validation plan weakens or rewrites already declared checks')

    def declare_validation_plan(self, plan: dict) -> dict:
        """Lock check commands and required coverage before the first batch.

        A repair batch may append checks and mandatory gates; changing existing
        commands, subjects, dependencies or required outcomes needs a genuine
        contract amendment, which clears this declaration.
        """
        from .validation_batch import validate_plan
        plan = copy.deepcopy(validate_plan(plan))
        plan.setdefault('mandatory_checks', [])
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            if state.get('pending_intent'):
                raise ValueError('Resolve pending user intent before declaring validation')
            spec = state['spec']
            if (plan.get('run_id', self.run_id) != self.run_id
                    or plan.get('contract_revision', spec['contract_revision']) != spec['contract_revision']
                    or plan.get('contract_hash', spec['contract_hash']) != spec['contract_hash']):
                raise ValueError('Validation plan does not match the current contract')
            required = {r['id'] for r in spec['requirements']}
            if set(plan.get('requirements', [])) != required:
                raise ValueError('Validation plan must cover every original requirement')
            declared = {'checks': copy.deepcopy(plan['checks']),
                        'mandatory_checks': list(plan.get('mandatory_checks', [])),
                        'requirements': sorted(required),
                        'contract_hash': spec['contract_hash'],
                        'contract_revision': spec['contract_revision']}
            locked = state.get('validation_plan')
            if locked:
                self._plan_preserves_lock(locked, plan)
                if (len(plan['checks']) > len(locked['checks'])
                        or set(plan['mandatory_checks']) > set(locked['mandatory_checks'])):
                    state['validation_plan'] = declared
                    self._event(db, 'validation_plan_extended', {'checks': len(plan['checks'])})
            else:
                state['validation_plan'] = declared
                self._event(db, 'validation_plan_declared', {'checks': len(plan['checks']),
                                                             'requirements': sorted(required)})
            return {'checks': len(plan['checks']), 'requirements': sorted(required),
                    'mandatory_checks': list(plan.get('mandatory_checks', [])),
                    'digest': digest(state['validation_plan'])}

    def _stage_workspace(self, state: dict) -> Path:
        current = state.get('staging_workspace')
        if current and not state.get('staging_dirty'):
            path = Path(current)
            if not path.is_dir() or not path.resolve().is_relative_to(self.directory.resolve()):
                raise ValueError('Staging workspace is missing or outside run directory')
            if state['mode'] != 'git' or not _git(path, 'status', '--porcelain', '-z', '--untracked-files=all'):
                return path
            state['staging_dirty'] = True
        serial = len([p for p in self.worktrees.iterdir() if p.name.startswith('staging-')]) + 1
        path = self.worktrees / f'staging-{serial}'
        if path.exists():
            raise ValueError('Staging workspace already exists')
        if state['mode'] == 'git':
            _git(self.workspace, 'worktree', 'add', '--detach', str(path), state['base_commit'])
            for task_id in state['staged_order']:
                row = state['tasks'][task_id]
                row['staged_commit'] = self._apply_git_patch(state, path, row)
            state['staging_commit'] = _git(path, 'rev-parse', 'HEAD').decode().strip()
        else:
            path.mkdir(parents=True)
            for row in state['tasks'].values():
                self._copy_files_inputs(state, row, path, include_staging=False)
            for task_id in state['staged_order']:
                row = state['tasks'][task_id]
                row['staged_commit'] = self._apply_files_patch(state, path, row)
            state['staging_commit'] = None
        state['staging_workspace'] = str(path.resolve())
        state['staging_dirty'] = False
        return path

    def partition_invalid_validation_inputs(self) -> dict:
        """Repair only an unexecutable >500-input declaration, preserving coverage.

        Every partition repeats the same command and keeps all original input
        paths. Valid declarations and successful validation are never unlocked.
        No worker budgets, task states, contract or approval are changed.
        """
        with self._transaction() as (db, maybe_state):
            state=self._need_state(maybe_state)
            locked=state.get('validation_plan')
            if not locked or state.get('validation') or state.get('acceptance') or state.get('pending_intent'):
                raise ValueError('Only an unvalidated current declaration can be partitioned')
            if not any(len(c['subject_paths'])>500 for c in locked['checks']):
                raise ValueError('Valid validation declarations remain locked')
            original=copy.deepcopy(locked);checks=[];mandatory=list(locked['mandatory_checks'])
            known={c['id'] for c in original['checks']};expanded={}
            for row in original['checks']:
                parts=[row['subject_paths'][i:i+500] for i in range(0,len(row['subject_paths']),500)]
                identities=[row['id']]
                for number in range(1,len(parts)):
                    ident=row['id']+'_inputs_'+str(number+1)
                    if len(ident)>80 or ident in known:raise ValueError('Partition check ID collision')
                    known.add(ident);identities.append(ident)
                expanded[row['id']]=identities
                for ident,paths in zip(identities,parts):
                    item=copy.deepcopy(row);item.update(id=ident,subject_paths=paths)
                    item['depends_on']=[piece for dep in row.get('depends_on',[]) for piece in expanded[dep]]
                    checks.append(item)
                    if row['id'] in original['mandatory_checks'] and ident not in mandatory:mandatory.append(ident)
            corrected={**locked,'checks':checks,'mandatory_checks':mandatory}
            from .validation_batch import validate_plan
            validate_plan(corrected)
            state['validation_plan']=corrected
            self._event(db,'invalid_validation_inputs_partitioned',{
                'original_plan':original,'original_digest':digest(original),
                'corrected_digest':digest(corrected),'all_input_paths_preserved':True})
            return copy.deepcopy(corrected)

    def _git_metadata(self, stage: Path) -> dict:
        pointer = stage / '.git'
        if not pointer.is_file() or pointer.is_symlink():
            raise ValueError('Staging worktree lacks a regular .git pointer')
        text = pointer.read_text(encoding='utf-8').strip()
        if not text.startswith('gitdir: '):
            raise ValueError('Invalid staging Git pointer')
        target = Path(text[8:])
        if not target.is_absolute():
            target = stage / target
        target = target.resolve(strict=True)
        common_raw = Path(_git(self.workspace, 'rev-parse', '--git-common-dir').decode().strip())
        common = (common_raw if common_raw.is_absolute() else self.workspace / common_raw).resolve(strict=True)
        actual_raw = Path(_git(stage, 'rev-parse', '--absolute-git-dir').decode().strip())
        actual = actual_raw.resolve(strict=True)
        if (target != actual or not target.is_relative_to(common / 'worktrees')
                or not target.is_dir() or not common.is_dir()):
            raise ValueError('Staging Git pointer escapes the source repository worktrees')
        head, index = target / 'HEAD', target / 'index'
        if not head.is_file() or not index.is_file() or head.is_symlink() or index.is_symlink():
            raise ValueError('Staging HEAD and index must be regular files')
        return {'pointer': {'path': str(pointer.resolve()), 'sha256': file_hash(pointer)},
                'gitdir': str(target), 'common_dir': str(common),
                'head': {'path': str(head), 'sha256': file_hash(head)},
                'index': {'path': str(index), 'sha256': file_hash(index)}}

    def _dependency_ready(self, state: dict, row: dict) -> bool:
        for dep in row['spec'].get('depends_on', []):
            upstream = state['tasks'][dep.get('task_id', dep.get('task'))]
            needs = dep['needs']
            if needs == 'contract_ready':
                interface = upstream['interfaces'].get(dep.get('interface_id')) if dep.get('interface_id') else None
                if interface is None and dep.get('interface_id'):
                    return False
                if not upstream['interfaces'] or upstream['state'] in {'superseded', 'cancel_requested', 'needs_repair'}:
                    return False
                if interface and dep.get('interface_revision') and interface['revision'] != dep['interface_revision']:
                    return False
            elif needs == 'artifact_ready':
                if upstream['state'] not in STAGED or not upstream.get('staged_commit'):
                    return False
            elif upstream['state'] != 'accepted':
                return False
        return True

    def _priority(self, state: dict) -> dict[str, tuple]:
        tasks = state['tasks']
        children = {ident: set() for ident in tasks}
        for ident, row in tasks.items():
            for dep in row['spec'].get('depends_on', []):
                children[dep.get('task_id', dep.get('task'))].add(ident)
        lengths, descendants = {}, {}
        def score(ident: str) -> tuple[float, set[str]]:
            if ident in lengths:
                return lengths[ident], descendants[ident]
            own = tasks[ident]['spec'].get('estimated_cost', 1)
            if not isinstance(own, (int, float)) or own <= 0 or own > 1000:
                own = 1
            child_scores = [score(child)[0] for child in children[ident]]
            lengths[ident] = float(own) + max(child_scores, default=0)
            descendants[ident] = set(children[ident])
            for child in children[ident]:
                descendants[ident].update(descendants[child])
            return lengths[ident], descendants[ident]
        for ident in tasks:
            score(ident)
        return {ident: (lengths[ident], len(descendants[ident]),
                        tasks[ident]['spec'].get('priority', 0),
                        tasks[ident]['spec'].get('importance', 0), ident)
                for ident in tasks}

    def _dependency_versions(self, state: dict, row: dict) -> dict:
        result = {}
        for dep in row['spec'].get('depends_on', []):
            ident = dep.get('task_id', dep.get('task'))
            upstream = state['tasks'][ident]
            result[ident] = {'generation': upstream['generation'],
                             'interfaces': {key: value['revision'] for key, value in upstream['interfaces'].items()},
                             'staged_commit': upstream['staged_commit'] if dep['needs'] != 'contract_ready' else None}
        return result

    def dispatch(self, owner: str, limits: dict | None = None) -> list[dict]:
        _clean_id(owner, 'dispatcher owner')
        limits = limits or {}
        if not isinstance(limits, dict):
            raise ValueError('Dispatch limits must be a mapping')
        def cap(name: str, default: int) -> int:
            value = limits.get(name, default)
            if not isinstance(value, int) or value < 0:
                raise ValueError('Invalid dispatch limit: ' + name)
            return value
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            if state['frozen'] is not None or state.get('pending_intent'):
                return []
            policy = state['spec'].get('execution_policy', {})
            if _now() - state['created_at'] >= policy.get('max_seconds', 3600):
                self._event(db, 'dispatch_budget_exhausted', {'kind': 'time'})
                return []
            calls_left = policy.get('max_worker_calls', 36) - policy.get('review_reserve', 3) - state['dispatch_count']
            if calls_left <= 0:
                self._event(db, 'dispatch_budget_exhausted', {'kind': 'worker_calls'})
                return []
            if state['staging_dirty']:
                self._stage_workspace(state)
            if state['mode'] == 'files' and self._source_inputs(state['spec']) != state['source_inputs']:
                raise ValueError('Declared non-Git source inputs changed; amend and revalidate')
            active = [r for r in state['tasks'].values() if r['state'] in ACTIVE]
            other_roles = cap('other_roles', 0)
            slots = max(0, min(MAX_WORKERS, policy.get('max_workers', MAX_WORKERS),
                               cap('host_slots', 1) - other_roles,
                               cap('policy_slots', 1), cap('resource_slots', 1),
                               cap('budget_slots', 1)) - len(active))
            slots = min(slots, calls_left)
            backlog = sum(r['state'] == 'submitted' for r in state['tasks'].values())
            if backlog >= cap('integration_backlog_limit', MAX_WORKERS):
                return []
            slots = min(slots, cap('integration_backlog_limit', MAX_WORKERS) - backlog)
            if slots <= 0:
                return []
            occupied = {resource for row in active for resource in row['spec'].get('shared_resources', [])}
            ready = [row for row in state['tasks'].values()
                     if row['state'] in REQUEUEABLE and self._dependency_ready(state, row)]
            priorities = self._priority(state)
            ready.sort(key=lambda row: (-priorities[row['id']][0], -priorities[row['id']][1],
                                        -priorities[row['id']][2], -priorities[row['id']][3], row['id']))
            cards = []
            for row in ready:
                if len(cards) >= slots:
                    break
                resources = set(row['spec'].get('shared_resources', []))
                if resources & occupied:
                    continue
                if row['state'] == 'superseded':
                    row['interfaces'] = {}
                    row['commit'] = None
                    row['staged_commit'] = None
                row['generation'] += 1
                row['state'] = 'running'
                row['owner'] = f'{owner}.{row["id"]}.{row["generation"]}'
                row['contract_hash'] = state['spec']['contract_hash']
                row['dependency_versions'] = self._dependency_versions(state, row)
                row['process'] = None
                row['termination'] = None
                row['superseded_reason'] = None
                if state['mode'] == 'git':
                    stage = self._stage_workspace(state)
                    base = _git(stage, 'rev-parse', 'HEAD').decode().strip()
                else:
                    self._stage_workspace(state)
                    base = digest(_manifest(Path(state['staging_workspace']), git=False))
                path = self.worktrees / f'{row["id"]}-g{row["generation"]}'
                if path.exists():
                    raise ValueError('Worker workspace path already exists; reconcile before reassigning')
                if state['mode'] == 'git':
                    _git(self.workspace, 'worktree', 'add', '--detach', str(path), base)
                else:
                    path.mkdir(parents=True)
                    self._copy_files_inputs(state, row, path)
                    row['input_manifest'] = _manifest(path, git=False)
                row['workspace'] = str(path.resolve())
                row['base_commit'] = base
                state['dispatch_count'] += 1
                occupied.update(resources)
                card = self._card(state, row)
                cards.append(card)
                self._event(db, 'dispatched', {'task_id': row['id'], 'generation': row['generation'],
                                               'owner': row['owner'], 'workspace': row['workspace'],
                                               'base_commit': base})
            state['updated_at'] = _now()
            return cards

    def _card(self, state: dict, row: dict) -> dict:
        spec = row['spec']
        refs = set(spec['requirements'])
        return {'id': row['id'], 'task_id': row['id'], 'package': spec['package'],
                'objective': spec['objective'], 'owner': row['owner'], 'generation': row['generation'],
                'workspace': row['workspace'], 'base_commit': row['base_commit'],
                'contract_hash': state['spec']['contract_hash'],
                'contract_revision': state['spec']['contract_revision'],
                'requirements': [r for r in state['spec']['requirements'] if r['id'] in refs],
                'write_paths': spec['write_paths'], 'shared_resources': spec.get('shared_resources', []),
                'forbidden_paths': spec.get('forbidden_paths', []), 'depends_on': spec.get('depends_on', []),
                'interfaces': copy.deepcopy(row['dependency_versions']),
                'interface_contracts': {dep_id: copy.deepcopy(state['tasks'][dep_id]['interfaces'])
                                        for dep_id in row['dependency_versions']},
                'global_invariants': state['spec'].get('global_invariants', []),
                'inputs': spec.get('inputs', spec.get('read_paths', [])),
                'outputs': spec.get('outputs', []),
                'verification': spec.get('verification', {'policy': 'batch_after_assembly', 'execution_status': 'not_run'}),
                'allowed_decisions': spec.get('allowed_decisions', []),
                'escalate_if': spec.get('escalate_if', []),
                'budget': {'worker_calls_remaining': state['spec'].get('execution_policy', {}).get('max_worker_calls', 36)
                           - state['dispatch_count'], 'tokens': None, 'source': 'unmeasured'},
                'user_output_language': state['spec'].get('user_output_language', 'ko'),
                'collaboration_language': state['spec'].get('collaboration_language', 'en')}

    def attach_process(self, task_id: str, generation: int, pid: int, nonce: str) -> dict:
        if not isinstance(pid, int) or pid <= 0 or not isinstance(nonce, str) or len(nonce) < 16:
            raise ValueError('Process needs a real PID and unguessable nonce')
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            row = self._current(state, task_id, generation, {'running'})
            if row['process'] is not None:
                raise ValueError('Process already attached')
            row['process'] = {'pid': pid, 'nonce_sha256': hashlib.sha256(nonce.encode()).hexdigest(),
                              'attached_at': _now()}
            self._event(db, 'process_attached', {'task_id': task_id, 'generation': generation, 'pid': pid})
            return copy.deepcopy(row['process'])

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == 'nt':
            # On Windows os.kill(pid, 0) is not a safe liveness probe: it can
            # invoke TerminateProcess. Query the process handle read-only.
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
            kernel.GetExitCodeProcess.restype = ctypes.c_int
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() not in {87, 1168}
            try:
                code = ctypes.c_uint32()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def record_termination(self, task_id: str, generation: int, nonce: str, exit_code: int) -> dict:
        if not isinstance(exit_code, int):
            raise ValueError('Termination needs an observed integer exit code')
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            row = state['tasks'].get(task_id)
            if row is None or row['generation'] != generation or row['state'] not in ACTIVE:
                raise ValueError('Termination generation/state mismatch')
            process = row['process']
            if (not process or hashlib.sha256(nonce.encode()).hexdigest() != process['nonce_sha256']
                    or self._pid_alive(process['pid'])):
                raise ValueError('Attached process has not been confirmed terminated')
            row['termination'] = {'pid': process['pid'], 'exit_code': exit_code, 'observed_at': _now(),
                                  'nonce_sha256': process['nonce_sha256']}
            self._event(db, 'process_terminated', {'task_id': task_id, 'generation': generation,
                                                   'exit_code': exit_code})
            return copy.deepcopy(row['termination'])

    def _current(self, state: dict, task_id: str, generation: int, allowed: set[str]) -> dict:
        row = state['tasks'].get(task_id)
        if not row or row['generation'] != generation or row['state'] not in allowed:
            raise ValueError('Stale task generation or invalid state')
        if row['contract_hash'] != state['spec']['contract_hash']:
            raise ValueError('Task contract is stale')
        for dep_id, saved in row['dependency_versions'].items():
            dep = state['tasks'][dep_id]
            current = {'generation': dep['generation'],
                       'interfaces': {key: value['revision'] for key, value in dep['interfaces'].items()},
                       'staged_commit': dep['staged_commit'] if saved['staged_commit'] is not None else None}
            if current != saved:
                raise ValueError('Task dependency interface or artifact changed')
        return row

    def publish_interface(self, task_id: str, generation: int, interface_id: str,
                          revision: str, contract: dict) -> dict:
        _clean_id(interface_id, 'interface ID')
        if not isinstance(revision, str) or not revision.strip() or not isinstance(contract, dict) or not contract:
            raise ValueError('Interface needs revision and concrete contract')
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            row = self._current(state, task_id, generation, {'running', 'submitted'})
            previous = row['interfaces'].get(interface_id)
            publication = {'revision': revision, 'contract': copy.deepcopy(contract),
                           'hash': digest(contract), 'published_at': _now()}
            if previous and (previous['revision'], previous['hash']) != (revision, publication['hash']):
                if previous['revision'] == revision:
                    raise ValueError('Changed interface content needs a new revision')
                self._invalidate(state, task_id, 'interface revision changed', include_self=False)
            row['interfaces'][interface_id] = publication
            self._event(db, 'interface_published', {'task_id': task_id, 'interface_id': interface_id,
                                                    'revision': revision, 'hash': publication['hash']})
            return copy.deepcopy(publication)

    def _changed_git(self, row: dict, *, allow_empty: bool = False) -> list[str]:
        cwd = Path(row['workspace'])
        if _git(cwd, 'rev-parse', 'HEAD').decode().strip() != row['commit']:
            raise ValueError('Worker HEAD moved after submission')
        if _git(cwd, 'status', '--porcelain', '-z', '--untracked-files=all'):
            raise ValueError('Worker has unsubmitted file changes')
        names = sorted(set(_names(_git(cwd, 'diff', '--no-renames', '--name-only', '-z',
                                       row['base_commit'], row['commit'], '--'))))
        if not names and not allow_empty:
            raise ValueError('Task commit has no changed files')
        for name in names:
            _relative(name)
            if (not any(_covers(p, name) for p in row['spec']['write_paths'])
                    or any(_covers(p, name) for p in row['spec'].get('forbidden_paths', []))):
                raise ValueError('Task commit changes an unowned path: ' + name)
        for entry in _git(cwd, 'ls-tree', '-rz', row['commit'], '--', *names).split(b'\0'):
            if entry and entry.split(b' ', 1)[0] in {b'120000', b'160000'}:
                raise ValueError('Task commit introduces a link or submodule')
        return names

    def _changed_files(self, row: dict, *, allow_empty: bool = False) -> list[str]:
        current = _manifest(Path(row['workspace']), git=False)
        before = row.get('input_manifest', {})
        names = sorted(p for p in current.keys() | before.keys() if current.get(p) != before.get(p))
        if not names and not allow_empty:
            raise ValueError('Task workspace has no changed files')
        for name in names:
            if (not any(_covers(p, name) for p in row['spec']['write_paths'])
                    or any(_covers(p, name) for p in row['spec'].get('forbidden_paths', []))):
                raise ValueError('Task changed an unowned path: ' + name)
        return names

    def submit(self, task_id: str, generation: int, commit: str | None = None,
               *, owner: str | None = None, no_op_reason: str | None = None) -> dict:
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            row = self._current(state, task_id, generation, {'running'})
            if owner is not None and row['owner'] != owner:
                raise ValueError('Task lease owner mismatch')
            if row['process'] and (not row['termination'] or row['termination']['exit_code'] != 0):
                raise ValueError('Attached writer process has not exited successfully')
            if state['mode'] == 'git':
                if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
                    raise ValueError('Git task submission needs a commit')
                cwd = Path(row['workspace'])
                _git(cwd, 'cat-file', '-e', commit + '^{commit}')
                _git(cwd, 'merge-base', '--is-ancestor', row['base_commit'], commit)
                row['commit'] = commit
                names = self._changed_git(row, allow_empty=bool(no_op_reason))
            else:
                row['commit'] = digest(_manifest(Path(row['workspace']), git=False))
                names = self._changed_files(row, allow_empty=bool(no_op_reason))
                row['submitted_manifest'] = _manifest(Path(row['workspace']), git=False)
            if not names:
                if not isinstance(no_op_reason, str) or not no_op_reason.strip():
                    raise ValueError('No-op submission needs a concrete reason')
                row['no_op_reason'] = no_op_reason[:1000]
            elif no_op_reason:
                raise ValueError('No-op reason conflicts with an actual changed artifact')
            row['changed_paths'] = names
            row['state'] = 'submitted'
            row['submitted_at'] = _now()
            self._event(db, 'submitted', {'task_id': task_id, 'generation': generation,
                                          'commit': row['commit'], 'changed_paths': names})
            return copy.deepcopy(row)

    def _apply_git_patch(self, state: dict, stage: Path, row: dict) -> str:
        self._changed_git(row, allow_empty=bool(row.get('no_op_reason')))
        if row.get('no_op_reason'):
            return _git(stage, 'rev-parse', 'HEAD').decode().strip()
        patch = _git(Path(row['workspace']), 'diff', '--binary', '--full-index',
                     row['base_commit'], row['commit'], '--')
        if _git(stage, 'status', '--porcelain', '-z', '--untracked-files=all'):
            raise ValueError('Staging workspace has unexpected changes')
        try:
            _git(stage, 'apply', '--index', '--whitespace=nowarn', '-', input_bytes=patch)
        except ValueError:
            # A failed apply can leave index/worktree changes. Preserve the old
            # staging worktree for inspection; never force an ours/theirs merge.
            state['staging_dirty'] = True
            raise
        # Create only an internal staging commit. Mandatory project checks run
        # later against the frozen integrated tree in the validation batch.
        tree = _git(stage, 'write-tree').decode().strip()
        parent = _git(stage, 'rev-parse', 'HEAD').decode().strip()
        commit = _git(stage, 'commit-tree', tree, '-p', parent, '-m', 'ADHD stage ' + row['id']).decode().strip()
        _git(stage, 'reset', '--hard', commit)
        return commit

    def _copy_files_inputs(self, state: dict, row: dict, target: Path, *, include_staging: bool = True) -> None:
        roots = [self.workspace]
        if include_staging and state.get('staging_workspace'):
            roots.append(Path(state['staging_workspace']))
        named = row['spec'].get('read_paths', []) + row['spec']['write_paths']
        for pattern in named:
            _relative(pattern, pattern=True)
            for root in roots:
                if pattern.endswith('/**'):
                    folder = _safe_member(root, pattern[:-3])
                    if not folder.exists():
                        continue
                    if not folder.is_dir():
                        raise ValueError('Read path prefix is not a directory')
                    files = [p for p in folder.rglob('*') if p.is_file()]
                    if len(files) > 20_000:
                        raise ValueError('File copy input exceeds 20000 files')
                    for source in files:
                        rel = source.relative_to(root).as_posix()
                        _safe_member(root, rel)
                        dest = _safe_member(target, rel)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, dest)
                else:
                    source = _safe_member(root, pattern)
                    if source.is_file():
                        dest = _safe_member(target, pattern)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, dest)

    def _apply_files_patch(self, state: dict, stage: Path, row: dict) -> str:
        if row.get('submitted_manifest') != _manifest(Path(row['workspace']), git=False):
            raise ValueError('Submitted file workspace changed before staging')
        names = self._changed_files(row, allow_empty=bool(row.get('no_op_reason')))
        worker = Path(row['workspace'])
        for name in names:
            source = _safe_member(worker, name)
            dest = _safe_member(stage, name)
            if source.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
            else:
                dest.unlink(missing_ok=True)
        return digest(_manifest(stage, git=False))

    def stage(self, task_id: str) -> dict:
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            row = state['tasks'].get(task_id)
            if row is None or row['state'] != 'submitted':
                raise ValueError('Only a submitted task can be staged')
            self._current(state, task_id, row['generation'], {'submitted'})
            stage = self._stage_workspace(state)
            if state['mode'] == 'git':
                commit = self._apply_git_patch(state, stage, row)
            else:
                commit = self._apply_files_patch(state, stage, row)
            state['staging_commit'] = commit
            state['staged_order'].append(task_id)
            row['staged_commit'] = commit
            row['state'] = 'provisionally_staged'
            state['frozen'] = None
            state['updated_at'] = _now()
            self._event(db, 'provisionally_staged', {'task_id': task_id, 'commit': commit})
            return copy.deepcopy(row)

    def _descendants(self, state: dict, root_id: str) -> set[str]:
        result, frontier = set(), [root_id]
        while frontier:
            current = frontier.pop()
            for ident, row in state['tasks'].items():
                if ident not in result and any(d.get('task_id', d.get('task')) == current
                                               for d in row['spec'].get('depends_on', [])):
                    result.add(ident)
                    frontier.append(ident)
        return result

    def _invalidate(self, state: dict, task_id: str, reason: str, *, include_self: bool = True) -> list[str]:
        affected = self._descendants(state, task_id)
        if include_self:
            affected.add(task_id)
        if state['frozen']:
            state['frozen'] = None
        state['staged_order'] = [x for x in state['staged_order'] if x not in affected]
        state['staging_dirty'] = True
        for ident in affected:
            row = state['tasks'][ident]
            row['superseded_reason'] = reason
            if row['state'] in ACTIVE:
                row['state'] = 'cancel_requested'
                row['cancel_requested_at'] = _now()
            else:
                row['state'] = 'superseded'
                row['staged_commit'] = None
        return sorted(affected)

    def request_cancel(self, task_id: str) -> dict:
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            row = state['tasks'].get(task_id)
            if not row or row['state'] not in ACTIVE:
                raise ValueError('Task is not running')
            row['state'] = 'cancel_requested'
            row['cancel_requested_at'] = _now()
            self._event(db, 'cancel_requested', {'task_id': task_id, 'generation': row['generation']})
            return copy.deepcopy(row)

    def confirm_cancel(self, task_id: str, generation: int, evidence: dict | None = None) -> dict:
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            row = state['tasks'].get(task_id)
            if not row or row['generation'] != generation or row['state'] != 'cancel_requested':
                raise ValueError('Cancellation generation/state mismatch')
            if row['process']:
                if (not row['termination'] or row['termination']['pid'] != row['process']['pid']
                        or self._pid_alive(row['process']['pid'])):
                    raise ValueError('Cannot reassign until attached process termination is observed')
            elif not isinstance(evidence, dict) or evidence.get('kind') != 'not_launched':
                raise ValueError('Unattached worker needs confirmed not-launched evidence')
            row['state'] = 'superseded' if row.get('superseded_reason') else 'needs_repair'
            row['owner'] = None
            row['staged_commit'] = None
            row['cancelled_at'] = _now()
            self._event(db, 'cancel_confirmed', {'task_id': task_id, 'generation': generation,
                                                 'evidence': evidence or row['termination']})
            return copy.deepcopy(row)

    def abandon_dispatch(self, task_id: str, generation: int, reason: str) -> dict:
        """Release a reservation only when the host failed before process launch."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Abandoned dispatch needs a concrete launch failure')
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            row = state['tasks'].get(task_id)
            if (row is None or row['generation'] != generation or row['state'] != 'running'
                    or row['process'] is not None):
                raise ValueError('Only an unlaunched current reservation can be abandoned')
            row['state'] = 'needs_repair'
            row['owner'] = None
            row['superseded_reason'] = 'host launch failed: ' + reason[:300]
            state['dispatch_count'] = max(0, state['dispatch_count'] - 1)
            self._event(db, 'dispatch_abandoned', {'task_id': task_id, 'generation': generation,
                                                   'reason': reason[:300]})
            return copy.deepcopy(row)

    def reconcile(self) -> dict:
        """Observe durable leases and files after restart; never guess a writer exited."""
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            issues = []
            for ident, row in state['tasks'].items():
                if row['state'] not in ACTIVE:
                    continue
                workspace = Path(row['workspace']) if row['workspace'] else None
                if workspace is None or not workspace.is_dir():
                    issues.append({'task_id': ident, 'kind': 'workspace_missing',
                                   'generation': row['generation']})
                    row['state'] = 'cancel_requested'
                    continue
                process = row['process']
                if process is None:
                    issues.append({'task_id': ident, 'kind': 'launch_status_unknown',
                                   'generation': row['generation']})
                    row['state'] = 'cancel_requested'
                elif row['termination'] is None:
                    kind = 'process_alive' if self._pid_alive(process['pid']) else 'exit_unrecorded'
                    issues.append({'task_id': ident, 'kind': kind, 'pid': process['pid'],
                                   'generation': row['generation']})
                    if kind == 'exit_unrecorded':
                        row['state'] = 'cancel_requested'
            if issues:
                self._event(db, 'reconciled', {'issues': issues})
            return {'issues': issues, 'reassignable': [ident for ident, row in state['tasks'].items()
                                                       if row['state'] in REQUEUEABLE]}

    def amend(self, spec: dict) -> dict:
        spec = _validate_spec(spec, self.run_id)
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            old = state['spec']
            if (spec['contract_revision'] <= old['contract_revision']
                    or spec['contract_hash'] == old['contract_hash']):
                raise ValueError('Amendment needs a new contract revision and hash')
            ceiling = state['policy_ceiling']
            proposed_policy = dict(spec.get('execution_policy', {}))
            previous_policy = old.get('execution_policy', {})
            for key in ceiling:
                proposed_policy.setdefault(key, previous_policy.get(key, ceiling[key]))
            spec['execution_policy'] = proposed_policy
            for key in ('max_workers', 'max_worker_calls', 'max_seconds', 'max_repair_waves'):
                if proposed_policy.get(key, ceiling[key]) > ceiling[key]:
                    raise ValueError('Amendment cannot increase the run execution budget: ' + key)
            if proposed_policy.get('review_reserve', ceiling['review_reserve']) < ceiling['review_reserve']:
                raise ValueError('Amendment cannot spend the reserved review capacity')
            pending = state.get('pending_intent')
            if pending and (pending['contract_hash'] != spec['contract_hash']
                            or pending['contract_revision'] != spec['contract_revision']):
                raise ValueError('Amendment differs from pending original-intent update')
            old_reqs = {r['id']: r for r in old['requirements']}
            new_reqs = {r['id']: r for r in spec['requirements']}
            new_tasks = {r['id']: r for r in spec['tasks']}
            removed = set(state['tasks']) - set(new_tasks)
            if any(state['tasks'][ident]['state'] in ACTIVE for ident in removed):
                raise ValueError('Cancel and confirm running tasks before removing them')
            changed = set()
            for ident, new in new_tasks.items():
                old_row = state['tasks'].get(ident)
                if old_row is None:
                    state['tasks'][ident] = self._new_task(new, spec['contract_hash'])
                    changed.add(ident)
                elif (old_row['spec'] != new or any(old_reqs.get(r) != new_reqs.get(r)
                                                   for r in new['requirements'])):
                    old_row['spec'] = copy.deepcopy(new)
                    changed.add(ident)
            for ident in removed:
                if ident in state['staged_order']:
                    state['staged_order'].remove(ident)
                    state['staging_dirty'] = True
                del state['tasks'][ident]
            old_source_inputs = state.get('source_inputs')
            state['spec'] = spec
            if state['mode'] == 'files':
                state['source_inputs'] = self._source_inputs(spec)
                if state['source_inputs'] != old_source_inputs:
                    changed.update(state['tasks'])
            for row in state['tasks'].values():
                if row['id'] not in changed:
                    if row['state'] in ACTIVE:
                        row['state'] = 'cancel_requested'
                        row['cancel_requested_at'] = _now()
                        row['superseded_reason'] = 'contract revision changed during active work'
                    else:
                        row['contract_hash'] = spec['contract_hash']
            affected = set()
            for ident in changed:
                affected.update(self._invalidate(state, ident, 'contract amendment'))
            if pending:
                reusable = []
                for ident in pending['staged_order']:
                    if (ident not in changed and ident not in affected and ident in state['tasks']
                            and not any(dep.get('task_id', dep.get('task')) in changed | affected
                                        for dep in state['tasks'][ident]['spec'].get('depends_on', []))):
                        reusable.append(ident)
                        state['tasks'][ident]['state'] = 'provisionally_staged'
                state['staged_order'] = reusable
                state['staging_dirty'] = True
                state['pending_intent'] = None
            state['frozen'] = None
            state['validation'] = None
            state['acceptance'] = None
            state['validation_plan'] = None
            state['updated_at'] = _now()
            self._event(db, 'contract_amended', {'revision': spec['contract_revision'],
                                                'affected': sorted(affected), 'removed': sorted(removed)})
            return {'revision': spec['contract_revision'], 'affected': sorted(affected),
                    'cancel_required': sorted(ident for ident, row in state['tasks'].items()
                                              if row['state'] == 'cancel_requested')}

    def invalidate_intent(self, new_contract_hash: str, new_revision: int) -> dict:
        """Hook-safe quarantine of old work before a normal CLI amendment."""
        if (not isinstance(new_contract_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', new_contract_hash)
                or not isinstance(new_revision, int) or new_revision < 1):
            raise ValueError('Intent invalidation needs a versioned contract')
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            if (new_revision <= state['spec']['contract_revision']
                    or new_contract_hash == state['spec']['contract_hash']):
                raise ValueError('New intent must advance revision and hash')
            pending = state.get('pending_intent')
            if pending:
                if (pending['contract_hash'], pending['contract_revision']) != (new_contract_hash, new_revision):
                    raise ValueError('A different intent update is already pending')
                return copy.deepcopy(pending)
            pending = {'contract_hash': new_contract_hash, 'contract_revision': new_revision,
                       'staged_order': list(state['staged_order']), 'observed_at': _now()}
            state['pending_intent'] = pending
            state['frozen'] = None
            state['validation'] = None
            state['acceptance'] = None
            state['staging_dirty'] = True
            state['staged_order'] = []
            for row in state['tasks'].values():
                row['superseded_reason'] = 'original intent changed'
                if row['state'] in ACTIVE:
                    row['state'] = 'cancel_requested'
                    row['cancel_requested_at'] = _now()
                else:
                    row['state'] = 'superseded'
                    row['staged_commit'] = None
            self._event(db, 'intent_invalidated', {'revision': new_revision,
                                                    'contract_hash': new_contract_hash})
            return copy.deepcopy(pending)

    def queue_repairs(self, groups: list[dict]) -> dict:
        if not isinstance(groups, list) or not groups:
            raise ValueError('Repair queue needs failure groups')
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            max_waves = state['spec'].get('execution_policy', {}).get('max_repair_waves', 3)
            if state['repair_wave_count'] >= max_waves:
                raise ValueError('Repair wave budget exhausted; keep unresolved failures visible')
            state['repair_wave_count'] += 1
            affected = set()
            for group in groups:
                if (not isinstance(group, dict) or not group.get('id')
                        or not (group.get('evidence_ref') or group.get('evidence_refs'))):
                    raise ValueError('Repair group needs ID and evidence reference')
                task_id = group.get('task_id')
                if not task_id:
                    matches = [ident for ident, row in state['tasks'].items()
                               if row['spec']['package'] == group.get('owner_package')]
                    if len(matches) != 1:
                        raise ValueError('Repair owner is ambiguous; provide task_id')
                    task_id = matches[0]
                if task_id not in state['tasks']:
                    raise ValueError('Unknown repair owner')
                state['tasks'][task_id]['repair_groups'].append(copy.deepcopy(group))
                affected.update(self._invalidate(state, task_id, 'validation failure group ' + group['id']))
            self._event(db, 'repairs_queued', {'groups': [g['id'] for g in groups],
                                              'affected': sorted(affected)})
            return {'affected': sorted(affected), 'cancel_required': sorted(
                ident for ident in affected if state['tasks'][ident]['state'] == 'cancel_requested')}

    def freeze(self) -> dict:
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            if state['mode'] == 'files' and self._source_inputs(state['spec']) != state['source_inputs']:
                raise ValueError('Declared non-Git source inputs changed before freeze')
            pending = [ident for ident, row in state['tasks'].items() if row['state'] not in STAGED]
            if pending:
                raise ValueError('Cannot freeze with unfinished tasks: ' + ', '.join(sorted(pending)))
            if state['staging_dirty']:
                self._stage_workspace(state)
            stage = self._stage_workspace(state)
            git = state['mode'] == 'git'
            if git and _git(stage, 'status', '--porcelain', '-z', '--untracked-files=all'):
                raise ValueError('Staging workspace has uncommitted changes')
            files = _manifest(stage, git=git)
            frozen = {'run_id': self.run_id, 'workspace': str(stage.resolve()),
                      'source_workspace': str(self.workspace),
                      'staging_workspace': str(stage.resolve()), 'mode': state['mode'],
                      'contract_hash': state['spec']['contract_hash'],
                      'contract_revision': state['spec']['contract_revision'],
                      'source_base_commit': state['base_commit'],
                      'integrated_commit': state['staging_commit'],
                      'git_tree': _git(stage, 'rev-parse', 'HEAD^{tree}').decode().strip() if git else None,
                      'git_metadata': self._git_metadata(stage) if git else None,
                      'visible_inventory': _visible_inventory(stage),
                      'files': files, 'included_tasks': [
                          {'id': ident, 'generation': state['tasks'][ident]['generation'],
                           'commit': state['tasks'][ident]['commit'],
                           'staged_commit': state['tasks'][ident]['staged_commit']}
                          for ident in state['staged_order']],
                      'required_tasks': sorted(state['tasks']), 'created_at': _now()}
            frozen['snapshot_digest'] = digest(frozen)
            atomic_json(self.frozen_path, frozen)
            state['frozen'] = {'path': str(self.frozen_path),
                               'sha256': file_hash(self.frozen_path),
                               'snapshot_digest': frozen['snapshot_digest']}
            for row in state['tasks'].values():
                row['state'] = 'validation_pending'
            self._event(db, 'frozen', {'snapshot_digest': frozen['snapshot_digest'],
                                       'tasks': frozen['required_tasks']})
            return copy.deepcopy(frozen)

    def assert_snapshot_no_git(self, snapshot_digest: str) -> dict:
        """Hook-safe frozen admission check: file bytes and inventory only."""
        state = self.status()
        reference = state.get('frozen')
        if not reference or reference['snapshot_digest'] != snapshot_digest:
            raise ValueError('Snapshot is not current')
        if file_hash(self.frozen_path) != reference['sha256']:
            raise ValueError('Frozen manifest changed')
        frozen = json.loads(self.frozen_path.read_text(encoding='utf-8'))
        if (frozen.get('snapshot_digest') != snapshot_digest
                or digest({k: v for k, v in frozen.items() if k != 'snapshot_digest'}) != snapshot_digest
                or frozen['contract_hash'] != state['spec']['contract_hash']
                or frozen['contract_revision'] != state['spec']['contract_revision']):
            raise ValueError('Frozen contract changed')
        stage = Path(frozen['staging_workspace'])
        if not stage.is_dir() or not stage.resolve().is_relative_to(self.directory.resolve()):
            raise ValueError('Frozen staging workspace is unavailable')
        if _visible_inventory(stage) != frozen['visible_inventory']:
            raise ValueError('Frozen visible file inventory changed')
        for name, sha in frozen['files'].items():
            path = _safe_member(stage, name)
            if not path.is_file() or file_hash(path) != sha:
                raise ValueError('Frozen source file changed: ' + name)
        metadata = frozen.get('git_metadata')
        if state['mode'] == 'git':
            if not isinstance(metadata, dict):
                raise ValueError('Frozen Git metadata is missing')
            common = Path(metadata['common_dir']).resolve()
            gitdir = Path(metadata['gitdir']).resolve()
            if not gitdir.is_relative_to(common / 'worktrees'):
                raise ValueError('Frozen Git metadata escaped repository')
            for key in ('pointer', 'head', 'index'):
                entry = metadata[key]
                path = Path(entry['path'])
                if not path.is_file() or path.is_symlink() or file_hash(path) != entry['sha256']:
                    raise ValueError('Frozen Git ' + key + ' changed')
            if Path(metadata['pointer']['path']).resolve() != stage / '.git':
                raise ValueError('Frozen worktree pointer moved')
            if Path(metadata['head']['path']).resolve() != gitdir / 'HEAD' or Path(metadata['index']['path']).resolve() != gitdir / 'index':
                raise ValueError('Frozen Git HEAD/index path moved')
        elif metadata is not None:
            raise ValueError('Non-Git snapshot has unexpected Git metadata')
        return frozen

    def assert_snapshot(self, snapshot_digest: str) -> dict:
        frozen = self.assert_snapshot_no_git(snapshot_digest)
        state = self.status()
        stage = Path(frozen['staging_workspace'])
        if _manifest(stage, git=state['mode'] == 'git') != frozen['files']:
            raise ValueError('Frozen files changed')
        if state['mode'] == 'git':
            if (_git(stage, 'rev-parse', 'HEAD').decode().strip() != frozen['integrated_commit']
                    or _git(stage, 'rev-parse', 'HEAD^{tree}').decode().strip() != frozen['git_tree']
                    or _git(stage, 'status', '--porcelain', '-z', '--untracked-files=all')):
                raise ValueError('Frozen Git snapshot changed')
        return frozen

    def mark_verified(self, snapshot_digest: str, report_path: str | Path) -> dict:
        frozen = self.assert_snapshot(snapshot_digest)
        from .validation_batch import load_report
        stage = Path(frozen['staging_workspace'])
        candidate = Path(report_path)
        if candidate.is_absolute():
            if not candidate.resolve().is_relative_to(stage.resolve()):
                raise ValueError('Batch report is outside frozen staging workspace')
            relative = candidate.resolve().relative_to(stage.resolve()).as_posix()
        else:
            relative = candidate.as_posix()
        report = load_report(stage, relative, require_current=True)
        snapshot = report['snapshot']
        if (report.get('assembly_digest') != snapshot_digest
                or snapshot.get('run_id') != self.run_id
                or snapshot.get('contract_hash') != frozen['contract_hash']
                or snapshot.get('contract_revision') != frozen['contract_revision']
                or report.get('status') != 'passed'):
            raise ValueError('Batch report does not bind the current assembly')
        if any(snapshot['files'].get(name) != sha for name, sha in frozen['files'].items()):
            raise ValueError('Batch did not observe every frozen assembly file')
        plan = json.loads((stage / report['plan_ref']).read_text(encoding='utf-8'))
        current_state = self.status()
        required = {r['id'] for r in current_state['spec']['requirements']}
        if set(plan.get('requirements', [])) != required:
            raise ValueError('Batch plan omits or changes original requirement coverage')
        locked = current_state.get('validation_plan')
        if (not locked or plan.get('checks') != locked['checks']
                or set(plan.get('mandatory_checks', [])) != set(locked['mandatory_checks'])):
            raise ValueError('Batch ran without the current declared validation plan')
        report_file = stage / relative
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            if not state['frozen'] or state['frozen']['snapshot_digest'] != snapshot_digest:
                raise ValueError('Validation snapshot changed')
            for row in state['tasks'].values():
                if row['state'] != 'validation_pending':
                    raise ValueError('Task state changed after freeze')
                row['state'] = 'verified'
            state['validation'] = {'snapshot_digest': snapshot_digest,
                                   'report': str(report_file), 'report_sha256': file_hash(report_file)}
            self._event(db, 'verified', {'snapshot_digest': snapshot_digest,
                                         'report': str(report_file)})
            return copy.deepcopy(state['validation'])

    def accept_verified(self, snapshot_digest: str, review_receipt: dict) -> dict:
        """Record the native hook's already correlated independent approval.

        This is called only after native.stop_child checks the host lifecycle,
        candidate digest, original turns and independent verifier profile.
        It runs no Git commands in the hook path.
        """
        if not isinstance(review_receipt, dict):
            raise ValueError('Native review receipt is required')
        verdict = review_receipt.get('verdict')
        if (not isinstance(verdict, dict) or verdict.get('verdict') != 'approve'
                or verdict.get('intent_alignment') is not True
                or review_receipt.get('digest') != verdict.get('reviewed_digest')
                or not review_receipt.get('agent_id') or not review_receipt.get('correlation')):
            raise ValueError('Native independent review receipt is incomplete')
        with self._transaction() as (db, maybe_state):
            state = self._need_state(maybe_state)
            frozen = state.get('frozen')
            if (not frozen or frozen['snapshot_digest'] != snapshot_digest
                    or file_hash(self.frozen_path) != frozen['sha256']
                    or verdict.get('reviewed_contract_hash') != state['spec']['contract_hash']
                    or verdict.get('reviewed_turn_ids') != [r['id'] for r in state['spec']['original_turns']]):
                raise ValueError('Native review is stale against the frozen contract')
            if (not state.get('validation') or state['validation']['snapshot_digest'] != snapshot_digest
                    or file_hash(Path(state['validation']['report'])) != state['validation']['report_sha256']):
                raise ValueError('Passed validation evidence is missing or changed')
            if any(row['state'] != 'verified' for row in state['tasks'].values()):
                raise ValueError('All tasks must be verified before acceptance')
            for row in state['tasks'].values():
                row['state'] = 'accepted'
            state['acceptance'] = {'snapshot_digest': snapshot_digest,
                                   'native_review_digest': digest(review_receipt),
                                   'agent_id': review_receipt['agent_id']}
            self._event(db, 'accepted', {'snapshot_digest': snapshot_digest,
                                         'agent_id': review_receipt['agent_id']})
            return copy.deepcopy(state['acceptance'])
