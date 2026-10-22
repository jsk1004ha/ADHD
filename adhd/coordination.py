"""Transactional, bounded project coordination; never a filesystem sandbox."""
from __future__ import annotations

from contextlib import contextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import math
import secrets

from .core import store

MAX_CARD = 2048
MAX_MESSAGE = 1024


def _publication_ready(proof, consumer):
    """Same-run staging is provisional; cross-run reuse needs current validation."""
    publication=proof.get('publication')
    if not publication:return True  # Explicit non-large publisher owns its completion contract.
    if consumer.get('run_id')==publication['run_id']:return True
    try:
        path=Path(publication['state_db'])
        if not path.is_file() or any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in (path,*path.parents)):
            return False
        with closing(sqlite3.connect(path,timeout=2)) as db:
            value=db.execute('SELECT value FROM state WHERE id=1').fetchone()[0]
        if len(value.encode())>2*1024**2:return False
        state=json.loads(value);row=state['tasks'][publication['task_id']]
        validation=state.get('validation') or {};frozen=state.get('frozen') or {}
        report=Path(validation['report'])
        if (row['state'] not in {'verified','accepted'} or state.get('pending_intent')
                or state['spec']['contract_hash']!=publication['contract_hash']
                or row.get('coordination',{}).get('generation')!=proof['generation']
                or validation.get('snapshot_digest')!=frozen.get('snapshot_digest')
                or not report.is_file()):return False
        with report.open('rb') as source:
            return hashlib.file_digest(source,'sha256').hexdigest()==validation['report_sha256']
    except (OSError,sqlite3.Error,KeyError,TypeError,ValueError):
        return False


def _identity(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def project_identity(workspace: Path) -> tuple[str, str]:
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError('Workspace must be a directory')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    try:
        result = subprocess.run(['git', '-C', str(workspace), 'rev-parse', '--path-format=absolute',
                                 '--git-common-dir'], capture_output=True, env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    common = Path(os.fsdecode(result.stdout).strip()) if result and not result.returncode else workspace
    return _hash(_identity(common)), _hash(_identity(workspace))


def _path(value: str) -> str:
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or value.startswith('/') or any(p in {'', '.', '..'} for p in value.split('/'))
            or value.split('/')[0] in {'.git', '.adhd', '.omx'}):
        raise ValueError('Invalid coordinated relative path')
    if '*' in value and not value.endswith('/**'):
        raise ValueError('Only directory/** patterns are supported')
    return value.casefold() if os.name == 'nt' else value


def _overlap(a: str, b: str) -> bool:
    a = a.removesuffix('/**').rstrip('/')
    b = b.removesuffix('/**').rstrip('/')
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


def _conflicts(a: dict, b: dict) -> list[str]:
    result = set(a.get('resources', [])) & set(b.get('resources', []))
    if (a.get('checkout_id') == b.get('checkout_id') and a['write_paths'] and b['write_paths']
            and not (a.get('isolated') and b.get('isolated'))):
        result.add('checkout:writer')
    for left, right in ((a['write_paths'], b['write_paths']),
                        (a['write_paths'], [] if b.get('snapshot_revision') else b['read_paths']),
                        ([] if a.get('snapshot_revision') else a['read_paths'], b['write_paths'])):
        for x in left:
            for y in right:
                if _overlap(x, y):
                    result.add('path:' + min(x, y))
    return sorted(result)


class Coordinator:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve(strict=True)
        self.project_id, self.checkout_id = project_identity(self.workspace)
        self._owned = {}
        self.path = store() / 'coordination.sqlite3'
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
               for p in (self.path, *self.path.parents)):
            raise ValueError('Unsafe coordination store')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS coordination_tasks(
                  project TEXT NOT NULL, task TEXT NOT NULL, value TEXT NOT NULL,
                  PRIMARY KEY(project,task));
                CREATE TABLE IF NOT EXISTS coordination_decisions(
                  project TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
                  PRIMARY KEY(project,id));
                CREATE TABLE IF NOT EXISTS coordination_outputs(
                  project TEXT NOT NULL, path TEXT NOT NULL, value TEXT NOT NULL,
                  PRIMARY KEY(project,path));
            ''')
        with self._transaction() as db:
            columns={r[1] for r in db.execute('PRAGMA table_info(coordination_tasks)')}
            if 'state_status' not in columns:
                db.execute('ALTER TABLE coordination_tasks ADD COLUMN state_status TEXT')
                for project,task,value in db.execute('SELECT project,task,value FROM coordination_tasks').fetchall():
                    db.execute('UPDATE coordination_tasks SET state_status=? WHERE project=? AND task=?',
                               (json.loads(value)['status'],project,task))
            db.execute('CREATE INDEX IF NOT EXISTS coordination_active ON coordination_tasks(project,state_status)')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA busy_timeout=10000')
        return db

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _rows(self, db):
        rows={row[0]:json.loads(row[1]) for row in db.execute(
            "SELECT task,value FROM coordination_tasks WHERE project=? AND state_status IN ('queued','waiting','running','cleanup_pending') LIMIT 513",
            (self.project_id,))}
        if len(rows)>512:
            raise ValueError('Active coordination task bound exceeded')
        pending=list(rows.values())
        while pending:
            for dep in pending.pop()['depends_on']:
                if dep not in rows:
                    found=self._get(db,dep)
                    if found:
                        rows[dep]=found;pending.append(found)
                        if len(rows)>1024:
                            raise ValueError('Coordination dependency bound exceeded')
        return rows

    def _get(self,db,task_id):
        row=db.execute('SELECT value FROM coordination_tasks WHERE project=? AND task=?',
                       (self.project_id,task_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, db, row):
        db.execute('INSERT INTO coordination_tasks(project,task,value,state_status) VALUES(?,?,?,?) '
                   'ON CONFLICT(project,task) DO UPDATE SET value=excluded.value,state_status=excluded.state_status',
                   (self.project_id, row['task_id'], json.dumps(row, ensure_ascii=False),row['status']))

    def register(self, card: dict) -> dict:
        if not isinstance(card, dict) or len(json.dumps(card, ensure_ascii=False).encode()) > MAX_CARD:
            raise ValueError('Task card must be bounded to 2048 UTF-8 bytes')
        allowed = {'task_id', 'contract_revision', 'requirements', 'read_paths', 'write_paths',
                   'resources', 'base_revision', 'snapshot_revision', 'depends_on', 'priority', 'run_id'}
        if set(card) - allowed:
            raise ValueError('Unknown coordination card field')
        name = card.get('task_id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', name):
            raise ValueError('Invalid coordinated task ID')
        revision = card.get('contract_revision')
        if type(revision) is not int or revision < 1:
            raise ValueError('Positive contract revision required')
        if not isinstance(card.get('base_revision'), str) or not card['base_revision']:
            raise ValueError('Input revision required')
        value = dict(card)
        for field in ('requirements', 'read_paths', 'write_paths', 'resources', 'depends_on'):
            items = value.setdefault(field, [])
            if not isinstance(items, list) or any(not isinstance(x, str) or not x for x in items):
                raise ValueError('Invalid coordination ' + field)
            if len(items) != len(set(items)):
                raise ValueError('Duplicate coordination ' + field)
        if not value['requirements']:
            raise ValueError('Original requirement IDs required')
        value['read_paths'] = [_path(x) for x in value['read_paths']]
        value['write_paths'] = [_path(x) for x in value['write_paths']]
        if type(value.get('priority', 0)) is not int:
            raise ValueError('Invalid priority')
        if 'snapshot_revision' in value and (not isinstance(value['snapshot_revision'], str)
                                              or not value['snapshot_revision']):
            raise ValueError('Verified snapshot revision required')
        value['checkout_id'] = self.checkout_id
        value['fingerprint'] = _hash(value)
        with self._transaction() as db:
            rows = self._rows(db)
            old = self._get(db,name)
            if old:rows[name]=old
            for dep in value['depends_on']:
                found=self._get(db,dep)
                if found:rows[dep]=found
            if old and old['fingerprint'] == value['fingerprint']:
                return old
            if old and old['status'] == 'running':
                raise ValueError('Cannot amend active coordination scope')
            if old and revision <= old['contract_revision']:
                raise ValueError('Changed card needs a newer revision')
            if any(dep not in rows or dep == name for dep in value['depends_on']):
                raise ValueError('Unknown dependency or cycle')
            graph = {k: r['depends_on'] for k, r in rows.items()}
            graph[name] = value['depends_on']
            visited = set()
            def visit(node, stack):
                if node in stack:
                    raise ValueError('Coordination dependency cycle')
                if node in visited:
                    return
                if node not in graph:
                    ancestor = self._get(db, node)
                    if ancestor is None:
                        raise ValueError('Unknown coordination dependency')
                    graph[node] = ancestor['depends_on']
                for dep in graph[node]:
                    visit(dep, stack | {node})
                visited.add(node)
            visit(name, set())
            value.update(status='queued', generation=old['generation'] if old else 0,
                         queued_at=old['queued_at'] if old else time.time())
            self._save(db, value)
            return value

    def acquire(self, task_id: str, *, isolated: bool = False, owner: str | None = None) -> dict:
        """Internal dispatchers may attest separate assigned workspaces.

        Public arbitrary commands always use isolated=False; this flag cannot
        confer host filesystem isolation or permission.
        """
        with self._transaction() as db:
            rows = self._rows(db)
            row = self._get(db,task_id)
            if row is None:
                raise ValueError('Unknown coordinated task')
            if row['status'] == 'running':
                if owner is None or _hash(owner) != row.get('owner_hash'):
                    return {'task_id': task_id, 'status': 'waiting', 'reason': 'already_owned'}
                return {**{k:v for k,v in row.items() if k!='owner_hash'}, 'owner': owner}
            if row['status'] == 'complete':
                raise ValueError('Completed task needs a newer revision to run again')
            row = {**row, 'isolated': isolated}
            waiting_for = [dep for dep in row['depends_on'] if rows[dep]['status'] != 'complete']
            predecessors=[]
            if not row.get('snapshot_revision'):
                for name,value in db.execute('SELECT path,value FROM coordination_outputs WHERE project=?',(self.project_id,)):
                    proof=json.loads(value)
                    if (proof['task_id']!=task_id and proof['base_revision']==row['base_revision']
                            and any(_overlap(name,p) for p in row['read_paths']+row['write_paths'])):
                        predecessors.append(proof['task_id'])
                        producer=self._get(db,proof['task_id'])
                        current=(producer and producer['status']=='complete'
                                 and producer['generation']==proof['generation']
                                 and producer['fingerprint']==proof['fingerprint'])
                        if not current or not _publication_ready(proof,row):
                            waiting_for.append(proof['task_id'])
                            rows[proof['task_id']]=producer
            occupied, resources = [], set()
            for other in rows.values():
                if other['task_id'] == task_id or other['status'] != 'running':
                    continue
                common = _conflicts(row, other)
                if common:
                    occupied.append(other['task_id'])
                    resources.update(common)
            # Fairness among ready queued tasks; no partial resource acquisition.
            order = lambda item: (-item.get('priority', 0), item['queued_at'], item['task_id'])
            if not occupied and not waiting_for:
                for other in rows.values():
                    if (other['task_id'] != task_id and other['status'] in {'queued', 'waiting'}
                            and not any(rows[x]['status'] != 'complete' for x in other['depends_on'])
                            and order(other) < order(row)
                            and _conflicts(row, {**other, 'isolated': other.get('isolated', isolated)})):
                        if not any(active['status'] == 'running' and _conflicts(other, active)
                                   for active in rows.values()):
                            occupied.append(other['task_id'])
            if occupied or waiting_for:
                participants = sorted(set([task_id, *occupied, *waiting_for]))
                key = _hash([(name, rows[name]['fingerprint']) for name in participants])
                decision = {'id': key, 'participants': {n: rows[n]['fingerprint'] for n in participants},
                            'resources': sorted(resources), 'action': 'sequence', 'messages': [], 'tokens': None}
                db.execute('INSERT OR IGNORE INTO coordination_decisions VALUES(?,?,?)',
                           (self.project_id, key, json.dumps(decision)))
                row.update(status='waiting', waiting_for=sorted(set(occupied + waiting_for)), conflict_id=key)
            else:
                owner = secrets.token_hex(32)
                inherited=set(predecessors)
                row.update(status='running', generation=row['generation'] + 1,
                           started_at=time.time(), waiting_for=[], owner_hash=_hash(owner),
                           inherited_from=sorted(inherited))
                row.pop('producer_pid', None)
                row.pop('conflict_id', None)
            self._save(db, row)
            result = {k:v for k,v in row.items() if k!='owner_hash'}
            if row['status']=='running':
                self._owned[(task_id,row['generation'])] = owner
                result['owner'] = owner
            return result

    def _owner(self, db, task_id, generation, owner=None):
        row = self._get(db,task_id)
        if not row or row['status'] != 'running' or row['generation'] != generation:
            raise ValueError('Coordination owner/generation changed')
        owner = owner or self._owned.get((task_id,generation))
        if owner is None or _hash(owner) != row.get('owner_hash'):
            raise ValueError('Coordination owner capability mismatch')
        return row

    def attach_process(self, task_id: str, generation: int, pid: int, *, owner=None) -> None:
        if type(pid) is not int or pid <= 0:
            raise ValueError('Real producer PID required')
        with self._transaction() as db:
            row = self._owner(db, task_id, generation, owner)
            if row.get('producer_pid'):
                raise ValueError('Producer already attached')
            row['producer_pid'] = pid
            self._save(db, row)

    def assert_owner(self, task_id: str, generation: int, *, owner=None) -> dict:
        with self._transaction() as db:
            return self._owner(db, task_id, generation, owner)

    def finish(self, task_id: str, generation: int, *, success: bool = True,
               outputs: dict[str, Path | None] | None = None, owner=None, _publication=None) -> dict:
        # Caller must observe its producer exit before releasing ownership.
        with self._transaction() as db:
            previous = self._get(db,task_id)
            capability = owner or self._owned.get((task_id,generation))
            if (previous and previous['status'] in {'complete','failed'}
                    and previous['generation']==generation and capability
                    and _hash(capability)==previous.get('owner_hash')):
                return {k:v for k,v in previous.items() if k!='owner_hash'}
            row = self._owner(db, task_id, generation, owner)
            if row.get('producer_pid'):
                from .large_tasks import LargeTaskStore
                if LargeTaskStore._pid_alive(row['producer_pid']):
                    raise ValueError('Producer termination has not been observed')
            for relative, path in (outputs or {}).items():
                relative = _path(relative)
                if not any(_overlap(relative, p) for p in row['write_paths']):
                    raise ValueError('Published path outside coordinated scope')
                sha = None
                if path is not None:
                    path = Path(path)
                    if not path.is_file() or any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in (path,*path.parents)):
                        raise ValueError('Published output must be a regular file')
                    with path.open('rb') as f:
                        sha = hashlib.file_digest(f, 'sha256').hexdigest()
                proof = {'path': str(path.resolve()) if path is not None else None,
                         'deleted': path is None, 'sha256': sha, 'task_id': task_id,
                         'generation': generation, 'fingerprint': row['fingerprint'],
                         'contract_revision': row['contract_revision'], 'base_revision': row['base_revision']}
                if _publication:proof['publication']=_publication
                db.execute('INSERT INTO coordination_outputs VALUES(?,?,?) '
                           'ON CONFLICT(project,path) DO UPDATE SET value=excluded.value',
                           (self.project_id, relative, json.dumps(proof)))
            row.update(status='complete' if success else 'failed', completed_at=time.time())
            self._save(db, row)
            return row

    def inputs(self, task_id: str, generation: int, *, owner=None) -> dict:
        with self._transaction() as db:
            row = self._owner(db, task_id, generation, owner)
            results = {}
            for name, value in db.execute('SELECT path,value FROM coordination_outputs WHERE project=?', (self.project_id,)):
                proof = json.loads(value)
                if proof['task_id'] not in row['depends_on']+row.get('inherited_from',[]) or not any(_overlap(name,p) for p in row['read_paths']+row['write_paths']):
                    continue
                producer = self._get(db,proof['task_id'])
                path = Path(proof['path']) if proof['path'] is not None else None
                if (not producer or producer['status']!='complete' or producer['fingerprint']!=proof['fingerprint']
                        or producer['generation']!=proof['generation']
                        or proof['base_revision']!=row['base_revision']
                        or not _publication_ready(proof,row)
                        or (path is not None and (not path.is_file() or any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in (path,*path.parents))))):
                    raise ValueError('Shared output provenance changed')
                if path is not None:
                    with path.open('rb') as source:
                        if hashlib.file_digest(source,'sha256').hexdigest()!=proof['sha256']:
                            raise ValueError('Shared output bytes changed')
                results[name]=proof
            return results

    def negotiate(self, conflict_id: str, message: dict) -> dict:
        """Consume a bounded optional reply; it cannot expand scope or steal a writer."""
        if not isinstance(message, dict) or len(json.dumps(message, ensure_ascii=False).encode()) > MAX_MESSAGE:
            raise ValueError('Negotiation message must be bounded to 1024 UTF-8 bytes')
        if set(message) - {'action', 'first'} or message.get('action') != 'sequence':
            raise ValueError('Scope-changing proposals require new verified task cards')
        with self._transaction() as db:
            found = db.execute('SELECT value FROM coordination_decisions WHERE project=? AND id=?',
                               (self.project_id, conflict_id)).fetchone()
            if not found:
                raise ValueError('Unknown conflict')
            decision = json.loads(found[0])
            rows = self._rows(db)
            for name in decision['participants']:
                found=self._get(db,name)
                if found:rows[name]=found
            if any(name not in rows or rows[name]['fingerprint'] != fingerprint
                   for name, fingerprint in decision['participants'].items()):
                raise ValueError('stale negotiation revision')
            if len(decision['messages']) >= 2:
                raise ValueError('At most two negotiation messages')
            if message.get('first') not in decision['participants']:
                raise ValueError('Negotiation participant mismatch')
            decision['messages'].append(message)
            decision['status']='proposed'
            if len(decision['messages'])==2:
                agreed=decision['messages'][0]==decision['messages'][1]
                first=message['first']
                active=[n for n in decision['participants'] if rows[n]['status']=='running']
                eligible=(not active or active==[first]) and not rows[first]['depends_on']
                decision['status']='confirmed_sequence' if agreed and eligible else 'automatic_sequence'
                if agreed and eligible and not active:
                    # Reordering needs matching replies and equal explicit user
                    # priorities. Existing requirements/resources are untouched.
                    participants=[rows[n] for n in decision['participants']]
                    if len({r.get('priority',0) for r in participants})==1:
                        chosen=rows[first]
                        chosen.setdefault('first_queued_at',chosen['queued_at'])
                        chosen['queued_at']=min(r['queued_at'] for r in participants)-.000001
                        self._save(db,chosen)
            db.execute('UPDATE coordination_decisions SET value=? WHERE project=? AND id=?',
                       (json.dumps(decision), self.project_id, conflict_id))
            return decision

    def status(self) -> dict:
        with closing(self._connect()) as db:
            rows=self._rows(db)
            for task,value in db.execute("SELECT task,value FROM coordination_tasks WHERE project=? AND state_status IN ('complete','failed') ORDER BY rowid DESC LIMIT 20",(self.project_id,)):
                rows.setdefault(task,json.loads(value))
            return {'project_id': self.project_id, 'checkout_id': self.checkout_id,
                    'tasks': [{k:v for k,v in row.items() if k!='owner_hash'} for row in rows.values()], 'model_calls': 0,
                    'host_parallel_writer_enforcement': 'isolated assigned execution only'}

    def run(self, card: dict, argv: list[str], *, wait_seconds: float = 10,
            timeout: float = 900) -> dict:
        """Run one explicitly requested command behind the checkout writer gate."""
        if (not isinstance(argv, list) or not argv or len(argv) > 64
                or any(not isinstance(x, str) or '\x00' in x for x in argv)
                or len(json.dumps(argv).encode()) > 32768):
            raise ValueError('A bounded explicit argv array is required')
        for number in (wait_seconds, timeout):
            if type(number) not in {int, float} or not math.isfinite(number) or number < 0:
                raise ValueError('Finite execution limits required')
        if wait_seconds > 60 or not 0 < timeout <= 86400:
            raise ValueError('Execution limit exceeds supported bounds')
        from .core import read_json
        from .leases import lease_path, _terminal_owner
        legacy=read_json(lease_path(self.workspace))
        if legacy and not _terminal_owner(legacy):
            return {'status':'waiting','reason':'native_or_legacy_workspace_owner',
                    'task_id':card.get('task_id')}
        card = dict(card)
        resource = 'checkout-writer:' + self.checkout_id
        card['resources'] = sorted(set([*card.get('resources',[]),resource]))
        self.register(card)
        deadline = time.monotonic() + wait_seconds
        while True:
            token = self.acquire(card['task_id'])
            if token['status'] == 'running':
                break
            if time.monotonic() >= deadline:
                return token
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        success = False
        try:
            from .workspace_storage import WorkspaceStorage
            from .workspace_commands import run_in_scratch
            result = run_in_scratch(WorkspaceStorage(self.workspace),
                {'argv': argv, 'timeout': timeout, 'owner': 'coordinated-command'},
                _on_process=lambda proc: self.attach_process(token['task_id'], token['generation'], proc.pid))
            success = result['status'] == 'complete'
            return result
        finally:
            self.finish(token['task_id'], token['generation'], owner=token['owner'], success=success)
