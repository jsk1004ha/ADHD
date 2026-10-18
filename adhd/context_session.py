"""Source-data context accounting and compact continuation packets.

These sidecars never edit the native contract, grant authority or attest success.
Caller actors/compaction epochs have separate delivery histories.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

from filelock import FileLock

from .core import atomic_json, atomic_text, digest, store
from .memory import SECRET, namespace
from .obsidian_index import safe_path, safe_root


def _label(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value):
        raise ValueError('Invalid ' + name)
    return value


class ContextSession:
    def __init__(self, workspace: Path, session: str, *, root: Path | None = None):
        self.workspace = safe_root(workspace)
        self.session = _label(session, 'session')
        base = root if root is not None else store() / 'context-sessions-v1' / namespace(workspace).replace(':', '-')
        Path(base).mkdir(parents=True, exist_ok=True)
        self.root = safe_root(base)
        self.path = safe_path(self.root, self.session + '.sqlite3', allow_missing=True)
        self.lock = FileLock(str(self.path) + '.lock', timeout=8)
        with self.lock:
            self.db = sqlite3.connect(self.path, timeout=8)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA cache_size=-8192')
            # Serialized rollback-journal transactions avoid enabling new WAL
            # multiprocess writers on an unverified deployed SQLite runtime.
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('CREATE TABLE IF NOT EXISTS deliveries '
                            '(id TEXT PRIMARY KEY,ref TEXT,actor TEXT,epoch TEXT,reason TEXT,'
                            'bytes INTEGER,chars INTEGER,excerpt_hash TEXT,last_used REAL,uses INTEGER)')
            self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def deliver(self, ref: dict, excerpt: str, *, actor='parent', epoch='0', reason='selected_source',
                max_chars=6500, max_bytes=26000):
        _label(actor, 'actor')
        _label(epoch, 'compaction epoch')
        if (not isinstance(ref, dict) or len(json.dumps(ref)) > 10000 or not isinstance(excerpt, str)
                or not 1 <= max_chars <= 100000 or not 1 <= max_bytes <= 400000
                or not isinstance(reason, str) or not reason or len(reason) > 500):
            raise ValueError('Invalid context reference, selection reason or budget')
        if SECRET.search(json.dumps(ref) + reason + excerpt):
            raise ValueError('Secret-bearing context is not recorded or delivered centrally')
        revision = ref.get('revision') or ref.get('hash')
        if not revision or not (ref.get('policy_version') or ref.get('policy_epoch')):
            raise ValueError('A revision and current policy identity are required')
        body = excerpt[:max_chars]
        while len(body.encode('utf-8')) > max_bytes:
            body = body[:max(0, len(body) - max(1, (len(body.encode('utf-8')) - max_bytes) // 4))]
        body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
        key = digest([ref, actor, epoch, body_hash])
        now = time.time()
        with self.lock, self.db:
            previous = self.db.execute('SELECT uses FROM deliveries WHERE id=?', (key,)).fetchone()
            if previous:
                self.db.execute('UPDATE deliveries SET uses=uses+1,last_used=? WHERE id=?', (now, key))
            else:
                self.db.execute('INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?)',
                                (key, json.dumps(ref, ensure_ascii=False), actor, epoch, reason,
                                 len(body.encode('utf-8')), len(body), body_hash, now, 1))
        return {'schema_version': 1, 'authority': 'source_data', 'reference': ref,
                'excerpt': '' if previous else body, 'duplicate': bool(previous),
                'truncated': body != excerpt, 'chars': 0 if previous else len(body),
                'bytes': 0 if previous else len(body.encode('utf-8')), 'trace_id': key,
                'continuation': {'reference': ref, 'next_char': len(body)} if body != excerpt else None,
                'note': 'New actors and compaction epochs require their source excerpt again.'}

    def trace(self, *, limit=100, after=None):
        if not 1 <= limit <= 1000:
            raise ValueError('Trace limit must be 1..1000')
        with self.lock:
            rows = self.db.execute('SELECT * FROM deliveries WHERE id>? ORDER BY id LIMIT ?',
                                   (after or '', limit + 1)).fetchall()
            totals = self.db.execute('SELECT COUNT(*),SUM(bytes),SUM(bytes*(uses-1)),SUM(uses) FROM deliveries').fetchone()
        records = [{**dict(row), 'ref': json.loads(row['ref'])} for row in rows[:limit]]
        return {'schema_version': 1, 'authority': 'observed_context_sidecar', 'records': records,
                'unique_excerpts': totals[0], 'delivered_bytes': totals[1] or 0,
                'avoided_repeat_bytes': totals[2] or 0, 'uses': totals[3] or 0,
                'usage_tokens': None, 'host_unobserved_context': True,
                'continuation': records[-1]['id'] if len(rows) > limit else None}


def _view(workspace, session):
    if not re.fullmatch(r'[a-f0-9]{24}', session):
        raise ValueError('Native session must be a 24-hex bridge key')
    path = safe_path(safe_root(workspace), '.adhd/bridge/' + session + '/view.json')
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Bridge view exceeds budget')
    raw = path.read_bytes()
    value = json.loads(raw)
    if Path(value.get('workspace', '')).resolve() != Path(workspace).resolve() or value.get('key') != session:
        raise ValueError('Bridge scope mismatch')
    return value, {'path': path.relative_to(workspace).as_posix(), 'sha256': hashlib.sha256(raw).hexdigest()}


def _changes(workspace):
    try:
        with tempfile.TemporaryFile() as output:
            run = subprocess.run(['git', '--no-optional-locks', '-C', str(workspace), 'status', '--porcelain=v1', '-z'],
                                 stdout=output,stderr=subprocess.DEVNULL, timeout=10, check=False)
            size=output.tell()
            output.seek(0)
            raw=output.read(1024*1024+1)
        if run.returncode:
            return {'paths': [], 'complete': False, 'reason': 'git_status_unavailable'}
        if size > 1024 * 1024:
            return {'paths': [], 'complete': False, 'reason': 'git_status_exceeds_budget'}
        # Rename records have a second NUL field; retaining it as a path is
        # harmless, but do not pretend this summary is the full diff.
        fields, paths, i = raw.decode('utf-8', 'replace').split('\0'), [], 0
        while i < len(fields) and fields[i]:
            field = fields[i]
            paths.append({'status': field[:2], 'path': field[3:]})
            if 'R' in field[:2] or 'C' in field[:2]:
                i += 1
                if i < len(fields):
                    paths[-1]['old_path'] = fields[i]
            i += 1
        return {'paths': paths[:200], 'complete': len(paths) <= 200,
                'total': len(paths), 'full_diff_command': ['git', 'diff', 'HEAD', '--']}
    except (OSError, subprocess.TimeoutExpired):
        return {'paths': [], 'complete': False, 'reason': 'git_status_unavailable'}


def handoff(workspace: Path, session: str, *, max_chars=16000):
    workspace = safe_root(workspace)
    view, source = _view(workspace, session)
    contract = view.get('contract') or {}
    checkpoints = view.get('checkpoints') or []
    latest = checkpoints[-1] if checkpoints else {}
    packet = {'schema_version': 1, 'authority': 'source_data_not_acceptance', 'source': source,
              'run_id': view.get('run_id'), 'intent_version': view.get('intent_version'),
              'original_requests': view.get('prompts', []), 'requirements': contract.get('criteria', []),
              'preserve_conditions': contract.get('preserve_conditions', []), 'non_goals': contract.get('non_goals', []),
              'assumptions': contract.get('assumptions', []), 'goal': (view.get('goal_request') or {}).get('outcome'),
              'phase': view.get('status'), 'plan': view.get('plan'), 'changes': _changes(workspace),
              'verification': view.get('test_outcomes', {}), 'candidate': view.get('candidate'),
              'failure': (view.get('failure_history') or [])[-3:],
              'blocker': view.get('feedback'), 'next_action': latest.get('next_action'),
              'checks_source': source, 'missing': []}
    if not contract:
        packet['missing'].append('native_contract_not_established')
    if not packet['next_action']:
        packet['missing'].append('next_action_not_observed')
    # Required intent and preservation text are never silently shortened.
    if len(json.dumps(packet, ensure_ascii=False)) > max_chars:
        packet['plan'] = {'reference': source, 'field': 'plan'}
        packet['candidate'] = {'reference': source, 'field': 'candidate'} if packet['candidate'] else None
        packet['changes'] = {'reference': source, 'command': ['git', 'status', '--porcelain=v1', '-z']}
    if len(json.dumps(packet, ensure_ascii=False)) > max_chars:
        return {'schema_version': 1, 'status': 'budget_exceeded', 'source': source,
                'reason': 'Original requirements exceed packet budget; read referenced original fields',
                'required_fields': ['prompts', 'contract', 'checkpoints'], 'complete': False}
    return packet


def _status_markdown(result):
    text = ('# ADHD progress\n\n' + '\n'.join([
        '**Goal:** ' + str(result.get('goal') or 'unobserved'),
        '**Phase:** ' + str(result.get('phase') or 'unobserved'),
        '**Blocker:** ' + str(result.get('blocker') or 'none observed'),
        '**Next:** ' + str(result.get('next_action') or 'unobserved')]) +
        '\n\n```json\n' + json.dumps({key: result[key] for key in ('changes', 'verification', 'failure')},
                                      ensure_ascii=False, indent=2) + '\n```\n')
    return text.replace('\r\n', '\n').replace('\r', '\n')


def refresh_status(view, *, reconcile_changes=False):
    """Export a bounded derived panel on meaningful native state changes.

    Called by native export, without a watcher, model call or recurring scan.
    The diff is a bounded snapshot at this transition; the review panel remains
    the source for the current full diff. These files never grant acceptance.
    """
    workspace = safe_root(Path(view['workspace']))
    session = view['key']
    if not re.fullmatch(r'[a-f0-9]{24}', session):
        raise ValueError('Invalid native status session')
    source = {'path': '.adhd/bridge/' + session + '/view.json'}
    truncated = []

    def reference(field):
        if field == 'changes':
            return {'command': ['git', '--no-optional-locks', 'diff', 'HEAD', '--'], 'complete': False}
        original = {'goal': 'goal_request.outcome or plan.content.objective or prompts',
                    'verification': 'test_outcomes', 'failure': 'failure_history',
                    'blocker': 'feedback', 'next_action': 'checkpoints[-1].next_action'}
        return {'reference': source, 'field': original.get(field, field), 'complete': False}

    def bounded(value, field, limit):
        encoded = json.dumps(value, ensure_ascii=False).encode('utf-8')
        if len(encoded) <= limit:
            return value
        truncated.append(field)
        if isinstance(value, str):
            prefix = value.encode('utf-8')[:limit - 128].decode('utf-8', errors='ignore')
            while len(json.dumps(prefix, ensure_ascii=False).encode('utf-8')) > limit - 128:
                prefix = prefix[:len(prefix) // 2]
            return prefix + ' [read original source field]'
        return reference(field)

    checkpoints = view.get('checkpoints') or []
    latest = checkpoints[-1] if checkpoints else {}
    projection = {
        'schema_version': 1, 'authority': 'derived_not_acceptance', 'session': session,
        'source': source, 'run_id': view.get('run_id'), 'intent_version': view.get('intent_version'),
        'goal': bounded((view.get('goal_request') or {}).get('outcome')
                        or ((view.get('plan') or {}).get('content') or {}).get('objective')
                        or ((view.get('prompts') or [{}])[0]).get('text'), 'goal', 6000),
        'phase': view.get('status'),
        'verification': bounded(view.get('test_outcomes', {}), 'verification', 10000),
        'failure': bounded((view.get('failure_history') or [])[-3:], 'failure', 3000),
        'blocker': bounded(view.get('feedback'), 'blocker', 2000),
        'next_action': bounded(latest.get('next_action'), 'next_action', 2000),
        'candidate_digest': (view.get('candidate') or {}).get('digest'),
        'completed_steps': bounded(view.get('completed_steps', []), 'completed_steps', 2000),
        'truncated_fields': truncated,
    }
    fingerprint = digest(projection)
    descriptor = {'status': 'available', 'file': '.adhd/context-status.md',
                  'json': '.adhd/context-status.json', 'review': {'view': 'unstaged'},
                  'host_open_required': True, 'projection_sha256': fingerprint}
    directory = safe_path(workspace, '.adhd', allow_missing=True)
    directory.mkdir(exist_ok=True)
    json_path = safe_path(workspace, descriptor['json'], allow_missing=True)
    markdown_path = safe_path(workspace, descriptor['file'], allow_missing=True)
    try:
        if json_path.stat().st_size <= 65536 and markdown_path.stat().st_size <= 65536:
            previous = json.loads(json_path.read_text(encoding='utf-8'))
            if (not reconcile_changes and previous.get('projection_sha256') == fingerprint
                    and previous.get('session') == session
                    and markdown_path.read_text(encoding='utf-8') == previous.get('markdown')):
                return descriptor
    except (OSError, ValueError):
        pass
    if (not reconcile_changes and view.get('mode') == 'coding'
            and not (view.get('contract') or {}).get('coding_scope')):
        changes = {'paths': [], 'complete': False, 'reason': 'native_git_scope_not_established',
                   'full_diff_command': ['git', '--no-optional-locks', 'diff', 'HEAD', '--']}
    else:
        changes = _changes(workspace)
    result = {**projection, 'projection_sha256': fingerprint,
              'changes': bounded(changes, 'changes', 10000)}
    result['markdown'] = _status_markdown(result)
    def stored_size():
        payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        return len(payload.replace('\n', os.linesep).encode('utf-8'))
    if stored_size() > 65536:
        for field in ('verification', 'failure', 'changes'):
            result[field] = reference(field)
            if field not in truncated:
                truncated.append(field)
        result['markdown'] = _status_markdown(result)
    if stored_size() > 65536:
        raise ValueError('Native status projection exceeds byte budget')
    atomic_json(json_path, result)
    atomic_text(markdown_path, result['markdown'])
    return descriptor


def status(workspace: Path, session: str, *, write=False):
    if write:
        view, source = _view(workspace, session)
        panels = refresh_status(view, reconcile_changes=True)
        result = json.loads(safe_path(workspace, panels['json']).read_text(encoding='utf-8'))
        result['source'] = source
        result['missing'] = ([] if view.get('contract') else ['native_contract_not_established'])
        if not result.get('next_action'):
            result['missing'].append('next_action_not_observed')
        result['panels'] = panels
        return result
    packet = handoff(workspace, session, max_chars=100000)
    result = {key: packet.get(key) for key in ('schema_version', 'source', 'run_id', 'goal', 'phase',
              'changes', 'verification', 'failure', 'blocker', 'next_action', 'missing')}
    text = _status_markdown(result)
    result['markdown'] = text
    return result
