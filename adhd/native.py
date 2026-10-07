"""Host-side native Codex state machine.

Codex supplies the agent loop, subagents, sandbox and authentication. This module
adds durable intent, small requests, bounded continuation and artifact gates.
Requests are NOT trusted instructions and NEVER run commands in a host hook.
The shell/MCP/GUI still runs only through Codex's own permission boundary.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import tomllib
import uuid
from typing import Any
from . import DISPLAY_NAME, __version__
from . import dependencies as _dependencies
from filelock import FileLock
from .core import (ROOT, MODES, atomic_json, digest, file_hash, home, read_json, safe_path,
                   store, recipes, save_recipe, quarantine, select_mode)
from .models import ROLES, route, execution_profile
from .gates import (validate_plan, validate_document_contract, document_candidate,
                    review_documents, validate_learning_check)
from .host_capabilities import (normalize_event, observe_event, completion_capability,
                                observed_thread_goal)
from .intent import apply_intent_patch
from .evidence import (validate_execution, observe_host_tool, validate_tool_observations,
                       receipt_test_failures)
from .snapshots import build_snapshot, validate_snapshot
from .coding_scope import validate_baseline, scope_repositories, native_evidence
from .provenance import validate_provenance
from .recovery import classify_failure, record_failure, verified_failure_signature
from .memory import validate_procedure_bundle
from . import leases
from .vendor.smolagents_context import truncate_content

ACTIVE = {'working', 'reviewing', 'revising'}
PREFIX = '[ADHD_CONTINUE:'
MAX_MESSAGE = 160_000
MAX_FILE = 128 * 1024 * 1024
DEFAULTS = {'max_rounds': 8, 'max_seconds': 3600, 'max_children': 3,
            'max_astra_calls': 1, 'max_total_children': 12, 'max_stagnation': 3,
            'max_review_children': 3, 'max_epochs': 3,
            'max_lifetime_children': 36, 'max_lifetime_rounds': 24}
MAX_PROGRESS_AGE = 3600
GOAL_IMPLICIT_LIMITS = frozenset({'max_rounds', 'max_seconds', 'max_stagnation',
    'max_total_children', 'max_review_children', 'max_epochs',
    'max_lifetime_children', 'max_lifetime_rounds'})


def goal_command(prompt: str) -> str | None:
    """Only a leading human command selects persistence; mentions are ordinary text."""
    match = re.match(r'^\s*(?:/goal|\$adhd-goal)(?=\s|$)\s*([\s\S]*)$', prompt)
    return match.group(1).strip() if match else None


def limit_enabled(state: dict, name: str) -> bool:
    return (state.get('loop_mode', 'bounded') != 'goal'
            or name not in GOAL_IMPLICIT_LIMITS
            or name in state.get('explicit_policy_keys', []))


def observe_thread_goal(state: dict, event: dict, directory: Path) -> None:
    """Bind a real host /goal without inventing a UserPromptSubmit event."""
    scan = observed_thread_goal(event, state.get('host_goal_cursor'))
    if scan is None:
        return
    state['host_goal_cursor'] = scan['cursor']
    was_pending = state.get('host_goal_scan_pending', False)
    state['host_goal_scan_pending'] = scan['pending']
    previous = state.get('host_goal_observation')
    observed = scan['observed'] or previous
    request = state.get('goal_request') or {}
    if scan['pending']:
        if request.get('origin') == 'host_thread_goal' and state['status'] in ACTIVE:
            end_after_children(state, 'paused',
                'Host goal updates are still being read; preserve unfinished work.')
        # Do not authorize or resume from a partially checked transcript.
        if scan['observed'] is not None:
            state['host_goal_observation'] = observed
        return
    if observed is None or (observed == previous and not was_pending):
        return
    state['host_goal_observation'] = observed
    goal = observed['goal']
    if goal is None or goal['status'] != 'active':
        state.pop('host_resume_pending', None)
        if request.get('origin') == 'host_thread_goal' and state['status'] in ACTIVE:
            end_after_children(state, 'paused',
                'The host goal stopped or paused; native acceptance is not completion.')
        return
    outcome = goal['objective'].strip()
    if request.get('outcome') == outcome:
        reactivated = previous and (previous['goal'] is None or previous['goal']['status'] != 'active')
        if request.get('origin') == 'host_thread_goal' and (reactivated or was_pending):
            if state['status'] == 'interrupt_pending' and state.get('pending_terminal') == 'paused':
                state['host_resume_pending'] = True
            elif state['status'] == 'paused' or (reactivated and state['status'] in {'blocked', 'budget_exhausted', 'cancelled'}):
                resume_native(state, 'Resumed by the host goal; original contract retained.')
        return
    turn = 'host-goal:' + digest([goal['threadId'], goal.get('createdAt'), outcome])
    state['goal_request'] = {'source_turn_id': turn, 'outcome': outcome,
                             'origin': 'host_thread_goal'}
    if not any(prompt['turn_id'] == turn for prompt in state['prompts']):
        state['prompts'].append({'text': outcome, 'turn_id': turn, 'time': time.time(),
                                'source': 'host_thread_goal'})
        if state.get('contract'):
            state['pending_turn_ids'].append(turn)
            state['feedback'] = 'Reconcile the observed host goal before enabling the goal loop.'
        else:
            state['intent_version'] += 1
    append_event(directory, 'host_goal_observed', {'source_id': turn,
                   'event_sha256': observed['event_sha256'], 'status': goal['status']})


def resume_native(state: dict, feedback: str) -> None:
    if (limit_reached(state, 'max_epochs', state.get('epoch', 1)) or
            limit_reached(state, 'max_lifetime_children', state.get('lifetime_children', 0)) or
            limit_reached(state, 'max_lifetime_rounds', state.get('lifetime_rounds', 0))):
        state['feedback'] = 'Lifetime continuation limit reached; preserve the unfinished contract.'
        return
    state['epoch'] = state.get('epoch', 1) + 1
    state['status'] = 'working'
    state['halt_emitted'] = False
    state['started'] = time.time()
    state['rounds'] = 0
    state['stagnation'] = 0
    state['total_children'] = 0
    state['review_children'] = 0
    state['feedback'] = feedback
    claim_writer(state)


def limit_reached(state: dict, name: str, value: int | float, *, inclusive: bool = True) -> bool:
    return limit_enabled(state, name) and (
        value >= state['policy'][name] if inclusive else value > state['policy'][name])


def target_manifest(state: dict) -> dict[str, str | None]:
    """Hash declared output bytes, including outputs that do not exist yet."""
    ws = Path(state['workspace'])
    result = {}
    for rel in state['contract']['artifacts']:
        path = checked_path(ws, rel)
        result[rel] = file_hash(path) if path.is_file() and path.stat().st_size <= MAX_FILE else None
    return result


def current_receipt(state: dict, ref: str, *, expect_failure: bool = False) -> dict | None:
    try:
        ws = Path(state['workspace'])
        receipt = validate_execution(ws, ref, run_id=state['run_id'],
                                     revision=state['intent_version'],
                                     expect_failure=expect_failure)
        started = datetime.fromisoformat(receipt['result']['started_at']).timestamp()
        now = time.time()
        if started < state['started'] - 2 or started > now + 2 or now - started > MAX_PROGRESS_AGE:
            return None
        return receipt
    except (ValueError, OSError, KeyError, TypeError):
        return None


def mark_verified_progress(state: dict, kind: str, identity: str,
                           ref: str | None = None, *, expect_failure: bool = False,
                           observation_id: str | None = None) -> None:
    row = {'marker': kind + ':' + str(state['intent_version']) + ':' + identity,
           'kind': kind, 'run_id': state['run_id'], 'intent_version': state['intent_version'],
           'contract_hash': state['contract_hash'], 'observed_at': time.time()}
    if ref is not None:
        row['evidence_id'] = ref
        row['expect_failure'] = expect_failure
        row['receipt_sha256'] = file_hash(checked_path(Path(state['workspace']), ref, existing=True))
    if observation_id is not None:
        row['observation_id'] = observation_id
    if kind == 'plan_step':
        row['plan_sha256'] = state['plan']['sha256']
        row['marker'] = kind + ':' + str(state['intent_version']) + ':' + row['plan_sha256'] + ':' + identity
    events = state.setdefault('verified_progress', [])
    if not any(old['marker'] == row['marker'] for old in events):
        state['verified_progress'] = (events + [row])[-100:]


def observed_test_outcome(state: dict, ref: str, receipt: dict) -> None:
    if current_receipt(state, ref, expect_failure=receipt['result']['exit_code'] != 0) is None:
        return
    count = receipt_test_failures(Path(state['workspace']), ref, receipt)
    if count is None:
        return
    invocation = receipt['invocation']
    suite = digest([invocation['argv'], invocation['cwd'], sorted(receipt['subject_files'])])
    best = state.setdefault('test_outcomes', {})
    prior = best.get(suite)
    if prior is None or count < prior:
        if prior is not None:
            mark_verified_progress(state, 'failing_tests_reduced', suite + ':' + str(count), ref,
                                   expect_failure=receipt['result']['exit_code'] != 0)
        best[suite] = count


def progress_markers(state: dict) -> set[str]:
    markers = set()
    plan = state.get('plan')
    if (plan and plan.get('intent_version') == state['intent_version']
            and plan.get('sha256') == digest(plan.get('content'))):
        markers.add('plan:' + str(state['intent_version']))
    for row in state.get('verified_progress', []):
        if row.get('kind') == 'plan_step' and (
                not plan or row.get('plan_sha256') != plan.get('sha256')):
            continue
        if (row.get('run_id') != state['run_id'] or
                row.get('intent_version') != state['intent_version'] or
                row.get('contract_hash') != state['contract_hash'] or
                row.get('observed_at', 0) < state['started'] - 2 or
                time.time() - row.get('observed_at', 0) > MAX_PROGRESS_AGE):
            continue
        ref = row.get('evidence_id')
        if ref is not None:
            receipt = current_receipt(state, ref,
                                      expect_failure=row.get('expect_failure', False))
            if receipt is None:
                continue
            try:
                if file_hash(checked_path(Path(state['workspace']), ref, existing=True)) != row['receipt_sha256']:
                    continue
            except (OSError, ValueError, KeyError):
                continue
        observation_id = row.get('observation_id')
        if observation_id is not None:
            observation = next((item for item in state.get('tool_observations', [])
                                if item.get('id') == observation_id), None)
            if not observation or observation.get('run_id') != state['run_id'] or observation.get('contract_revision') != state['intent_version']:
                continue
            try:
                observed_at = datetime.fromisoformat(observation['observed_at']).timestamp()
                if observed_at < state['started'] - 2 or time.time() - observed_at > MAX_PROGRESS_AGE:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
        markers.add(row['marker'])
    candidate = state.get('candidate')
    if (candidate and is_fresh(state) and
            candidate.get('recorded_at', 0) >= state['started'] - 2 and
            time.time() - candidate.get('recorded_at', 0) <= MAX_PROGRESS_AGE):
        for result in candidate.get('criterion_results', []):
            refs = result.get('evidence_ids', [])
            if refs and not all(current_receipt(state, ref) is not None for ref in refs):
                continue
            observations = result.get('observation_ids', [])
            if observations:
                current = {row.get('id'): row for row in state.get('tool_observations', [])}
                try:
                    if any(current[ref]['run_id'] != state['run_id'] or
                           current[ref]['contract_revision'] != state['intent_version'] or
                           datetime.fromisoformat(current[ref]['observed_at']).timestamp() < state['started'] - 2 or
                           time.time() - datetime.fromisoformat(current[ref]['observed_at']).timestamp() > MAX_PROGRESS_AGE
                           for ref in observations):
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
            markers.add('requirement_pass:' + str(state['intent_version']) + ':' + result['id'])
    return markers


def update_stagnation(state: dict) -> None:
    markers = progress_markers(state)
    seen = set(state.setdefault('seen_progress', []))
    try:
        current = target_manifest(state)
    except (OSError, ValueError):
        current = {}
    prior_targets = state.setdefault('seen_target_digests', {})
    for rel, sha in current.items():
        history = prior_targets.setdefault(rel, [])
        if sha not in history:
            markers.add('target_change:' + rel + ':' + str(sha))
            history.append(sha)
            prior_targets[rel] = history[-30:]
    gained = markers - seen
    if gained:
        state['stagnation'] = 0
    elif state.get('progress_initialized'):
        state['stagnation'] = state.get('stagnation', 0) + 1
    else:
        state['stagnation'] = 0
    state['progress_initialized'] = True
    state['seen_progress'] = sorted(seen | markers)[-200:]
    state['last_progress'] = digest(sorted(markers))

def session_key(session: str) -> str:
    if not isinstance(session, str) or not session or len(session) > 1024:
        raise ValueError('Missing/invalid Codex session_id')
    return digest(session)[:24]

def folder(key: str) -> Path:
    if not re.fullmatch('[0-9a-f]{24}', key):
        raise ValueError('Invalid native session key')
    return store() / 'native' / key

@contextmanager
def locked(key: str):
    d = folder(key); d.mkdir(parents=True, exist_ok=True)
    # Reuse filelock's OS-specific locks instead of writing a lock primitive.
    with FileLock(str(d / 'state.lock'), timeout=4):
        yield d

def checked_path(root: Path, relative: str, *, existing: bool = False) -> Path:
    """Reject traversal AND symlinks/junctions (also inside the workspace)."""
    if not isinstance(relative, str) or not relative or '\x00' in relative:
        raise ValueError('Expected a relative path')
    rel = relative.replace('\\', '/')
    if rel.startswith('/') or re.match(r'^[A-Za-z]:', rel) or '..' in Path(rel).parts:
        raise ValueError('Absolute/traversing paths are not allowed')
    root = root.resolve(); p = root
    for part in Path(rel).parts:
        p = p / part
        if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
            raise ValueError('Symlink/junction not allowed: ' + relative)
    target = safe_path(root, rel)
    if existing and not target.is_file():
        raise ValueError('Missing file: ' + relative)
    return target

def bridge(workspace: Path, key: str) -> Path:
    folder(key)  # validate key
    return checked_path(workspace, '.adhd/bridge/' + key)

def bounded_json(path: Path, limit: int = MAX_MESSAGE) -> Any:
    if path.stat().st_size > limit:
        raise ValueError('Request exceeds byte limit')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    return value

def message(event: str, text: str) -> dict:
    # These lifecycle outputs have no additionalContext field in Codex's schema.
    # State and the bridge view are still persisted by handle_event.
    if event in {'PostCompact', 'SubagentStop'}:
        return {}
    return {'hookSpecificOutput': {'hookEventName': event,
                                  'additionalContext': truncate_content(text, 4200)}}

def deny(reason: str) -> dict:
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                  'permissionDecision': 'deny', 'permissionDecisionReason': reason}}

def load_policy(profile_name: str = 'standard') -> dict:
    overrides = read_json(store() / 'native-policy.json', {})
    if not isinstance(overrides, dict):
        raise ValueError('native-policy.json must be an object')
    result = dict(DEFAULTS)
    if profile_name == 'simple':
        result['max_children'] = 1
        result['max_total_children'] = result['max_review_children'] + 1
    ranges = {'max_rounds':(1,50), 'max_seconds':(60,86400), 'max_children':(1,6),
              'max_astra_calls':(0,5), 'max_total_children':(1,100), 'max_stagnation':(2,10),
              'max_review_children':(1,20), 'max_epochs':(1,20),
              'max_lifetime_children':(1,500), 'max_lifetime_rounds':(1,500)}
    for k, value in overrides.items():
        if k not in ranges or type(value) is not int or not ranges[k][0] <= value <= ranges[k][1]:
            raise ValueError('Invalid local policy field: ' + k)
        result[k] = value
    if profile_name == 'simple' and 'max_total_children' not in overrides:
        result['max_total_children'] = result['max_review_children'] + 1
    return result


def validate_profile_plan(plan: dict, criteria: list[dict], profile_name: str) -> dict:
    if profile_name == 'deep':
        return validate_plan(plan, criteria)
    if not isinstance(plan, dict):
        raise ValueError('Brief plan must be an object')
    if all(field in plan for field in ('preflight', 'risks', 'alternatives')):
        return validate_plan(plan, criteria)
    for field in ('objective', 'approach', 'verification'):
        if not isinstance(plan.get(field), str) or not plan[field].strip() or len(plan[field]) > 6000:
            raise ValueError('Brief plan needs bounded ' + field)
    steps = plan.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValueError('Brief plan needs 1..20 steps')
    expected = {row['id'] for row in criteria}
    ids = set()
    covered = set()
    for step in steps:
        if (not isinstance(step, dict) or not isinstance(step.get('id'), str)
                or not step['id'] or step['id'] in ids
                or not isinstance(step.get('action'), str) or not step['action'].strip()):
            raise ValueError('Brief plan step needs a unique id and action')
        deps, reqs = step.get('depends_on', []), step.get('requirements', [])
        if (not isinstance(deps, list) or not set(deps) <= ids
                or not isinstance(reqs, list) or not reqs or not set(reqs) <= expected):
            raise ValueError('Brief plan dependencies and requirements must be known')
        ids.add(step['id'])
        covered.update(reqs)
    if covered != expected:
        raise ValueError('Brief plan must cover every requirement')
    return plan

def lease_path(ws: Path) -> Path:
    return leases.lease_path(ws)

def claim_writer(state: dict) -> None:
    expected = 'native:' + state['key'] + ':' + state['run_id']
    state['lease_generation'] = leases.acquire(Path(state['workspace']), expected, owner='native')

def release_writer(state: dict) -> None:
    if any(child.get('status') == 'running' for child in state.get('children', {}).values()):
        raise ValueError('Cannot release writer while a child is running')
    leases.release(Path(state['workspace']),
                   'native:' + state['key'] + ':' + state.get('run_id', ''),
                   state.get('lease_generation'))


def end_after_children(state: dict, terminal: str, feedback: str | None = None) -> None:
    """Commit a terminal transition only after the last observed child stops."""
    if terminal not in leases.TERMINAL:
        raise ValueError('Invalid terminal state')
    state.pop('host_resume_pending', None)
    if feedback is not None:
        state['feedback'] = feedback
    if any(child.get('status') == 'running' for child in state.get('children', {}).values()):
        state['status'] = 'interrupt_pending'
        state['pending_terminal'] = terminal
    else:
        state['status'] = terminal
        state.pop('pending_terminal', None)
        release_writer(state)

def export_view(state: dict) -> None:
    b = bridge(Path(state['workspace']), state['key']); b.mkdir(parents=True, exist_ok=True)
    # This is a model-readable copy; never trust it as authoritative state.
    public = {k:v for k,v in state.items() if k not in {'processed','reservations','seen_stops','host_goal_cursor'}}
    public['completion_capability']=completion_capability(state.get('host_capabilities',{}))
    public['pending_intent']=bool(state.get('pending_turn_ids'))
    p = checked_path(Path(state['workspace']), str((b/'view.json').relative_to(state['workspace'])))
    atomic_json(p, public)

def persist(d: Path, state: dict) -> None:
    atomic_json(d/'state.json', state)
    export_view(state)

def append_event(d: Path, event: str, data: dict) -> None:
    # Called inside the session lock; entries are for diagnosis, not prompting.
    ledger=d/'ledger.jsonl'
    if ledger.exists() and ledger.stat().st_size>2*1024*1024:
        os.replace(ledger,d/'ledger.previous.jsonl')
    with ledger.open('a',encoding='utf-8') as f:
        f.write(json.dumps({'time':time.time(),'event':event,**data}, ensure_ascii=False)+'\n')

def initial(key: str, ws: Path) -> dict:
    return {'version':__version__,'schema_version':2,'key':key,'workspace':str(ws),'status':'idle',
            'prompts':[], 'prompt_turns':[], 'intent_version':0, 'processed':[],
            'children':{}, 'reservations':{}, 'seen_stops':[], 'rounds':0,
            'astra_calls':0,'total_children':0, 'feedback':'', 'candidate':None,
            'pending_turn_ids':[], 'intent_history':[], 'host_capabilities':{'observed_events':[]},
            'failure_history':[], 'failure_counts':{}, 'needs_replan':False,
            'model_calls':[], 'tool_observations':[],
            'epoch':1, 'review_children':0, 'lifetime_children':0, 'lifetime_rounds':0,
            'loop_mode':'bounded', 'goal_request':None, 'explicit_policy_keys':[],
            'policy':load_policy(), 'usage':{'observed':False,'tokens':None,
            'note':'Native hooks do not expose a stable billable-token counter. Limits below are continuation/spawn limits, not a hard token cap.'}}


def migrate_state(state: dict, d: Path) -> None:
    """Preserve older state while requiring current evidence for completion."""
    state.setdefault('loop_mode', 'bounded')
    state.setdefault('goal_request', None)
    state.setdefault('explicit_policy_keys', [])
    if state.get('schema_version', 1) >= 2:
        state['version']=__version__
        state.setdefault('failure_history',[])
        state.setdefault('failure_counts',{})
        state.setdefault('verified_failure_counts',{})
        state.setdefault('verified_progress',[])
        state.setdefault('seen_progress',[])
        state.setdefault('test_outcomes',{})
        state.setdefault('execution_profile', execution_profile())
        state.setdefault('needs_replan',False)
        state.setdefault('model_calls',[])
        state.setdefault('tool_observations',[])
        return
    backup=d/'state-v0.1.0.backup.json'
    if not backup.exists():
        atomic_json(backup,state)
    state['schema_version']=2
    state['version']=__version__
    state.setdefault('pending_turn_ids',[])
    state.setdefault('intent_history',[])
    state.setdefault('host_capabilities',{'observed_events':[]})
    state.setdefault('epoch',1)
    state.setdefault('review_children',0)
    state.setdefault('lifetime_children',state.get('total_children',0))
    state.setdefault('lifetime_rounds',state.get('rounds',0))
    state['policy']={**DEFAULTS,**state.get('policy',{})}
    if state.get('candidate') and state['candidate'].get('evidence_schema')!=1:
        state['legacy_candidate']=state['candidate']
        state['candidate']=None
        if state['status']=='complete':
            state['status']='blocked'
        elif state['status'] in ACTIVE:
            state['status']='revising'
        state['feedback']='Previous candidate needs current execution evidence and a fresh review.'

def criteria_valid(rows: Any) -> list[dict]:
    if not isinstance(rows,list) or not 1 <= len(rows) <= 50:
        raise ValueError('criteria must contain 1..50 entries')
    ids = set()
    for row in rows:
        if not isinstance(row,dict) or set(row) - {'id','text','kind','tool_contract'}:
            raise ValueError('Each criterion uses id, text, kind and optional tool_contract only')
        if not re.fullmatch(r'R[1-9][0-9]{0,2}',str(row.get('id',''))) or row['id'] in ids:
            raise ValueError('Criteria need unique R1... IDs')
        if not isinstance(row.get('text'),str) or not row['text'].strip() or len(row['text'])>4000:
            raise ValueError('Criterion text is empty/too large')
        if row.get('kind') not in {'artifact','test','visual','source','reasoning','behavior',
                                  'provenance','browser','mcp'}:
            raise ValueError('Invalid criterion kind')
        if row['kind'] in {'browser','mcp'}:
            tool=row.get('tool_contract')
            if (not isinstance(tool,dict) or not {'name','request_sha256','result_type'} <= set(tool) or
                    set(tool)-{'name','request_sha256','result_type','result_contains'} or
                    not isinstance(tool['name'],str) or not re.fullmatch(r'mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+',tool['name']) or
                    not isinstance(tool['request_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',tool['request_sha256']) or
                    tool['result_type'] not in {'text','image','structured'} or
                    (tool.get('result_contains') is not None and
                     (not isinstance(tool['result_contains'],str) or not 1<=len(tool['result_contains'])<=200))):
                raise ValueError('Browser/MCP criterion needs an exact expected tool, request hash and result type')
            browser_name='browser' in tool['name'].lower() or 'cua_repl' in tool['name'].lower()
            if browser_name != (row['kind']=='browser'):
                raise ValueError('Tool contract name and browser/MCP kind disagree')
        elif 'tool_contract' in row:
            raise ValueError('Only browser/MCP criteria use tool_contract')
        ids.add(row['id'])
    return rows

def result_valid(state: dict, rows: Any, *, require_execution: bool = False) -> None:
    if not isinstance(rows,list):
        raise ValueError('criterion_results must be an array')
    expected = {r['id'] for r in state['contract']['criteria']}
    criteria = {r['id']:r for r in state['contract']['criteria']}
    kinds = {key:r['kind'] for key,r in criteria.items()}
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or row.get('id') in seen or row.get('id') not in expected:
            raise ValueError('Duplicate/unknown requirement result')
        if row.get('pass') is not True or not isinstance(row.get('evidence'),str) or not row['evidence'].strip():
            raise ValueError('Every requirement must pass with concrete evidence')
        if len(row['evidence']) > 6000:
            raise ValueError('Evidence too long; reference a local file instead')
        if require_execution and kinds[row['id']] in {'test','provenance'}:
            refs = row.get('evidence_ids')
            if not isinstance(refs, list) or not refs or len(refs) > 12:
                raise ValueError('Test/provenance criterion needs an observed execution receipt')
            for ref in refs:
                if not isinstance(ref, str):
                    raise ValueError('Invalid execution evidence path')
                validate_execution(Path(state['workspace']), ref, run_id=state['run_id'],
                                   revision=state['intent_version'])
        if kinds[row['id']] in {'browser','mcp'}:
            observations=row.get('observation_ids')
            if not isinstance(observations,list):
                raise ValueError('Browser/MCP criterion needs observed host tool IDs')
            validate_tool_observations(state,observations,kinds[row['id']],
                                       criteria[row['id']].get('tool_contract'))
        seen.add(row['id'])
    if seen != expected:
        raise ValueError('Missing requirement result')

def snapshot(ws: Path, paths: list[str]) -> dict[str,str]:
    if not isinstance(paths,list) or not 1 <= len(paths) <= 150 or len(set(paths))!=len(paths):
        raise ValueError('Snapshot needs 1..150 distinct file paths')
    result={}
    for relative in paths:
        p=checked_path(ws,relative,existing=True)
        if not 0 < p.stat().st_size <= MAX_FILE:
            raise ValueError('Empty or oversized snapshot file (128 MiB max): ' + relative)
        if p.suffix.lower()=='.json':
            bounded_json(p, MAX_FILE)
        result[relative]=file_hash(p)
    return result

def is_fresh(state: dict) -> bool:
    c=state.get('candidate')
    if (not c or c.get('evidence_schema') != 1 or
            c['intent_version'] != state['intent_version'] or
            c.get('contract_hash') != state.get('contract_hash') or
            state.get('pending_turn_ids')):
        return False
    try:
        ws=Path(state['workspace'])
        if c.get('snapshot_schema') == 1:
            validate_snapshot(ws, c['files'])
        elif snapshot(ws, list(c['files'])) != c['files']:
            return False
        for ref, sha in c.get('execution_receipts', {}).items():
            if file_hash(checked_path(ws,ref,existing=True)) != sha:
                return False
            validate_execution(ws,ref,run_id=state['run_id'],revision=state['intent_version'])
        current={r['id']:r for r in state.get('tool_observations',[])}
        if any(current.get(row['id'])!=row for row in c.get('tool_observations',[])):
            return False
        if state['contract'].get('coding_scope'):
            proof, _ = native_evidence(state, c.get('coding_scope_evidence'), checked_path)
            if proof != c.get('coding_scope_proof'):
                return False
        if state.get('large_task'):
            from .large_native import validate_admission
            validate_admission(state)
        return True
    except (ValueError,OSError):
        return False

def source_check(state: dict, rows: Any) -> list[str]:
    if not isinstance(rows,list) or len(rows)>100:
        raise ValueError('sources must be an array of at most 100 entries')
    expected={r['id'] for r in state['contract']['criteria'] if r['kind']=='source'}
    covered=set();files=[];ws=Path(state['workspace'])
    for row in rows:
        fields=['claim_id','claim','source','locator','basis','evidence_ref']
        if not isinstance(row,dict) or not all(isinstance(row.get(k),str) and row[k].strip() for k in fields):
            raise ValueError('Each source needs claim_id, claim, source, locator, basis and evidence_ref')
        if row['claim_id'] not in expected:
            raise ValueError('Source claim_id must name a current source criterion')
        if row['basis'] not in {'read','measured','calculated','inferred'}:
            raise ValueError('Source basis must distinguish read/measured/calculated/inferred')
        reference=checked_path(ws,row['evidence_ref'],existing=True)
        if row.get('evidence_sha256') and row['evidence_sha256']!=file_hash(reference):
            raise ValueError('Source evidence hash changed')
        files.append(row['evidence_ref']);covered.add(row['claim_id'])
    if covered!=expected:
        raise ValueError('Each source criterion needs a hash-bound source evidence reference')
    return list(dict.fromkeys(files))


def archive_for_new_task(state: dict, d: Path, turn_id: str) -> None:
    """Keep the prior run inspectable and start no new writer before old children end."""
    if any(child['status']=='running' for child in state.get('children',{}).values()):
        state['status']='handoff_pending'
        state['handoff_turn_id']=turn_id
        return
    new_prompt=next((p for p in state['prompts'] if p['turn_id']==turn_id),None)
    if not new_prompt:
        raise ValueError('New task turn was not recorded')
    previous=json.loads(json.dumps(state))
    if previous.get('status') in ACTIVE|{'handoff_pending'}:
        previous['status']='paused'
        previous['feedback']='Paused for a distinct user task.'
    archive=d/'run-archive'/str(state.get('run_id','unstarted'))
    archive.mkdir(parents=True,exist_ok=True)
    atomic_json(archive/'state.json',previous)
    if state.get('run_id'):
        release_writer(state)
    fresh=initial(state['key'],Path(state['workspace']))
    fresh['prompt_turns']=state['prompt_turns']
    fresh['prompts']=[new_prompt]
    outcome = goal_command(new_prompt['text'])
    if outcome is not None:
        fresh['goal_request']={'source_turn_id':turn_id, 'outcome':outcome}
    fresh['intent_version']=state['intent_version']+1
    fresh['archived_runs']=state.get('archived_runs',[])+[str(archive/'state.json')]
    state.clear();state.update(fresh)


def apply_request(state: dict, op: str, payload: dict, d: Path) -> str:
    ws=Path(state['workspace'])
    if op=='begin':
        if state['status'] != 'idle':
            raise ValueError('Only a new user task in idle state may begin; use checkpoint, sync-intent, or user-authorized resume')
        if not state['prompts']:
            raise ValueError('No original user prompt recorded by Codex hook')
        loop_mode = payload.get('loop_mode', 'goal' if state.get('goal_request') else 'bounded')
        if loop_mode not in {'bounded', 'goal'}:
            raise ValueError('Invalid loop_mode; use bounded or goal')
        if loop_mode == 'goal' and not state.get('goal_request'):
            raise ValueError('Goal mode requires an observed /goal or $adhd-goal user command')
        if state.get('goal_request') and loop_mode != 'goal':
            raise ValueError('An observed goal command cannot be downgraded to bounded execution')
        if store().resolve().is_relative_to(ws.resolve()):
            raise ValueError('Use a project folder that does not contain the Codex host state directory')
        for field in ['assumptions','non_goals']:
            values=payload.get(field,[])
            if not isinstance(values,list) or len(values)>50 or any(not isinstance(v,str) or len(v)>4000 for v in values):
                raise ValueError(field+' must be a bounded list of strings')
        criteria=criteria_valid(payload.get('criteria'))
        profile=execution_profile(payload.get('execution_profile', 'standard'))
        mode=payload.get('mode',select_mode(state['prompts'][-1]['text']))
        if mode not in MODES:
            raise ValueError('Invalid task mode')
        artifacts=payload.get('artifacts')
        if not isinstance(artifacts,list) or not 1 <= len(artifacts) <= 60:
            raise ValueError('Declare 1..60 output artifacts; for text tasks use answer.md')
        if len(set(artifacts))!=len(artifacts):
            raise ValueError('Duplicate artifact')
        for p in artifacts: checked_path(ws,p)
        scope = None
        repositories = scope_repositories(ws, artifacts) if mode == 'coding' else set()
        if any(not repository.is_relative_to(ws.resolve()) for repository in repositories):
            raise ValueError('Git coding scope workspace must include the checkout root')
        if mode == 'coding' and (payload.get('coding_scope') or repositories):
            baseline_rel = payload.get('coding_scope')
            if not isinstance(baseline_rel, str):
                raise ValueError('Git coding tasks need a CLI-captured coding_scope baseline before begin')
            baseline_path = checked_path(ws, baseline_rel, existing=True)
            repository = validate_baseline(ws, bounded_json(baseline_path, 2 * 1024 * 1024))
            if repositories - {repository}:
                raise ValueError('Coding artifacts must belong to the scoped repository')
            scope = {'baseline': baseline_rel, 'sha256': file_hash(baseline_path)}
        elif payload.get('coding_scope'):
            raise ValueError('coding_scope is only used for coding tasks')
        documents=validate_document_contract(payload.get('documents',[]),artifacts,ws,checked_path)
        protected=payload.get('protected_inputs',[])
        if not isinstance(protected,list) or len(protected)>40: raise ValueError('At most 40 protected input files')
        protected_hashes=snapshot(ws,protected) if protected else {}
        state.update(run_id=uuid.uuid4().hex, status='working', started=time.time(),
            mode=mode, rounds=0, astra_calls=0,total_children=0,children={},reservations={},
            seen_stops=[],halt_emitted=False,last_stop_decision={},feedback='',candidate=None,stagnation=0,last_progress=None,checkpoints=[],
            progress_initialized=False,seen_progress=[],verified_progress=[],test_outcomes={},
            policy=load_policy(profile['name']), loop_mode=loop_mode,
            explicit_policy_keys=sorted(read_json(store() / 'native-policy.json', {})), plan=None,
            plan_required=profile['plan_depth']!='optional', execution_profile=profile,
            pending_turn_ids=[],epoch=1,review_children=0,lifetime_children=0,lifetime_rounds=0)
        state['failure_history']=[];state['failure_counts']={};state['verified_failure_counts']={};state['needs_replan']=False
        state['contract']={'criteria':criteria,'artifacts':artifacts,
            'assumptions':payload.get('assumptions',[]), 'non_goals':payload.get('non_goals',[]),
            'intent_version':state['intent_version'], 'documents':documents, 'protected_inputs':protected_hashes}
        if scope:
            state['contract']['coding_scope'] = scope
        if 'plan' in payload:
            state['plan']={'content':validate_profile_plan(payload['plan'],criteria,profile['name']),
                           'intent_version':state['intent_version']}
            state['plan']['sha256']=digest(state['plan']['content'])
        state['contract_hash']=digest(state['contract'])
        state['seen_target_digests']={rel:[sha] for rel,sha in target_manifest(state).items()}
        state['recipes']=recipes(ws,mode,state['prompts'][-1]['text'],limit=2)
        claim_writer(state)
        atomic_json(d/('contract-'+state['run_id']+'-v'+str(state['intent_version'])+'.json'),state['contract'])
        if profile['plan_depth']=='optional':
            return 'Simple run armed. Execute directly or use one bounded child; verify before independent review.'
        return 'Run armed. Read relevant memory/skills, then submit a requirement-covered plan before implementation. Do not wait for user approval of reversible decisions.'
    if op=='status': return 'Status exported to view.json.'
    if op=='goal':
        request = state.get('goal_request')
        if (not request or payload.get('source_turn_id') != request['source_turn_id']
                or state['status'] not in ACTIVE or state.get('pending_turn_ids')):
            raise ValueError('Goal needs an active contract and a reconciled observed goal command')
        state['loop_mode']='goal'
        state['feedback']='Goal loop enabled. Continue until fresh independent acceptance or an explicit stop/limit/blocker.'
        return state['feedback']
    if op=='pause':
        end_after_children(state, 'paused')
        return ('Pause pending while running children finish; writer lease retained.'
                if state['status']=='interrupt_pending' else
                'Paused without discarding work. Only a real user message can authorize resumption.')
    if op=='blocked':
        state['feedback']=str(payload.get('reason','A required capability is unavailable.'))[:4000]
        end_after_children(state, 'blocked')
        return ('Blocked state pending while running children finish; writer lease retained.'
                if state['status']=='interrupt_pending' else
                'Blocked, not complete. Report the actual blocker.')
    if op=='sync-intent':
        if not state.get('pending_turn_ids'):
            raise ValueError('No pending user turn to reconcile')
        if 'additions' in payload and 'classification' not in payload:
            additions=criteria_valid(payload['additions'])
            payload={'base_revision':state['contract']['intent_version'],
                     'source_turn_id':state['pending_turn_ids'][0],
                     'classification':'amend',
                     'operations':[{'op':'add','target':'criteria/'+r['id'],'value':r} for r in additions]}
        current=state.get('contract')
        if not current:
            raise ValueError('No active contract to amend')
        updated,changed=apply_intent_patch(current,payload,state['pending_turn_ids'])
        turn=payload['source_turn_id']
        if changed==['new_task']:
            archive_for_new_task(state,d,turn)
            return 'Prior run preserved; new task waits for existing children.' if state['status']=='handoff_pending' else 'Prior run archived; submit a new begin contract.'
        state['pending_turn_ids'].remove(turn)
        if not changed:
            return 'Status-only turn reconciled; contract, plan and candidate unchanged.'
        criteria_valid(updated['criteria'])
        if len(set(updated['artifacts']))!=len(updated['artifacts']):
            raise ValueError('Duplicate artifacts after amendment')
        for rel in updated['artifacts']:
            checked_path(ws,rel)
        validate_document_contract(updated['documents'],updated['artifacts'],ws,checked_path)
        for rel,sha in updated['protected_inputs'].items():
            if file_hash(checked_path(ws,rel,existing=True))!=sha:
                raise ValueError('Protected input change requires a real current file hash')
        old_version=current['intent_version']
        atomic_json(d/('contract-'+state['run_id']+'-v'+str(old_version)+'.json'),current)
        state['intent_history']=(state.get('intent_history',[])+[
            {'turn_id':turn,'base_revision':old_version,'changed':changed,
             'previous_hash':state['contract_hash']}])[-100:]
        state['contract']=updated
        state['intent_version']=updated['intent_version']
        state['contract_hash']=digest(updated)
        if state.get('large_task'):
            from .large_native import invalidate_intent
            invalidate_intent(state)
        state['reusable_evidence']={r['id']:r for r in state.get('candidate',{}).get('criterion_results',[])
            if not any(t=='criteria/'+r['id'] or t.startswith('documents/') for t in changed)} if state.get('candidate') else {}
        state['candidate']=None
        state['status']='working'
        state['completed_steps']=[]
        state['test_outcomes']={}
        state['feedback']='User contract updated. Recheck affected evidence and plan coverage.'
        if state.get('lease_generation') is None:
            claim_writer(state)
        atomic_json(d/('contract-'+state['run_id']+'-v'+str(state['intent_version'])+'.json'),updated)
        return 'User amendment applied as contract revision '+str(state['intent_version'])
    if state['status'] not in ACTIVE:
        raise ValueError('No active native run')
    if digest(state['contract']) != state['contract_hash']:
        raise ValueError('Authoritative contract changed unexpectedly')
    if op == 'attach-large':
        if not state.get('plan') or state['plan']['intent_version'] != state['intent_version']:
            raise ValueError('Current deep plan is required before attaching large execution')
        from .large_native import attach
        state['large_task'] = attach(state, payload)
        state['candidate'] = None
        return 'Large graph attached. Use isolated CLI workers; App multi-writer correlation remains unavailable.'
    if op=='plan':
        content=validate_profile_plan(payload,state['contract']['criteria'],
                                      state.get('execution_profile', execution_profile())['name'])
        new_sha=digest(content)
        if state.get('needs_replan') and new_sha==state.get('plan',{}).get('sha256'):
            raise ValueError('Repeated failure needs a changed plan and hypothesis')
        if new_sha != (state.get('plan') or {}).get('sha256'):
            state['completed_steps']=[]
        state['plan']={'content':content,'intent_version':state['intent_version'],'sha256':new_sha}
        state['needs_replan']=False
        atomic_json(d/('plan-'+state['run_id']+'.json'),state['plan'])
        state['candidate']=None;state['status']='working'
        return 'Plan recorded. Execute the dependency order, preflight and verification without a needless confirmation round.'
    if op=='checkpoint':
        text=payload.get('summary',''); nxt=payload.get('next_action','')
        if not isinstance(text,str) or not isinstance(nxt,str) or not text.strip():
            raise ValueError('Checkpoint needs summary and next_action text')
        entry={'summary':truncate_content(text,2000),'next_action':truncate_content(nxt,1000)}
        references=payload.get('evidence_ids',[])
        if not isinstance(references,list) or len(references)>12:
            raise ValueError('Checkpoint evidence_ids must be a bounded list')
        verified=[]
        for ref in references:
            if not isinstance(ref,str):raise ValueError('Invalid checkpoint execution reference')
            receipt=validate_execution(ws,ref,run_id=state['run_id'],revision=state['intent_version'])
            verified.append({'id':ref,'subject_digest':receipt['subject_digest'],
                             'stdout_sha256':receipt['result']['stdout_sha256'],
                             'receipt_sha256':file_hash(checked_path(ws,ref,existing=True))})
            observed_test_outcome(state,ref,receipt)
        if verified:
            existing=state.get('checkpoint_evidence',[])
            known={(row['subject_digest'],row.get('stdout_sha256')) for row in existing}
            state['checkpoint_evidence']=(existing+[
                row for row in verified if (row['subject_digest'],row['stdout_sha256']) not in known])[-30:]
        failure=payload.get('failure')
        if failure is not None:
            if state.get('needs_replan'):
                raise ValueError('Change the plan before another failed-check retry')
            if not isinstance(failure,dict) or set(failure)-{'category','detail','evidence_id'}:
                raise ValueError('Checkpoint failure must have category, detail and evidence_id')
            ref=failure.get('evidence_id')
            if not isinstance(ref,str):raise ValueError('Failure needs a check receipt')
            failed_receipt=validate_execution(ws,ref,run_id=state['run_id'],revision=state['intent_version'],expect_failure=True)
            observed_test_outcome(state,ref,failed_receipt)
            stderr=checked_path(ws,str((Path(ref).parent/'check.stderr.log').as_posix()),existing=True)
            stdout=checked_path(ws,str((Path(ref).parent/'check.stdout.jsonl').as_posix()),existing=True)
            with stderr.open('rb') as stream:
                failure_output=stream.read(8192).decode('utf-8',errors='replace')
            with stdout.open('rb') as stream:
                failure_output+=stream.read(8192).decode('utf-8',errors='replace')
            category=classify_failure(failure_output)
            if failure.get('category')!=category:
                raise ValueError('Failure category differs from verified process output')
            detail=failure.get('detail')
            if not isinstance(detail,str):
                raise ValueError('Failure needs a concrete detail')
            row=record_failure(state,category,detail,ref,
                               verified_signature=verified_failure_signature(failure_output))
            if not row['retry_allowed']:
                if category in {'auth_required','permission_denied','tool_unavailable','budget_exhausted'}:
                    end_after_children(state, 'blocked', row['next_action'])
                else:
                    state['needs_replan']=True
        partial=payload.get('criterion_results',[])
        if not isinstance(partial,list) or len(partial)>50:
            raise ValueError('Checkpoint criterion_results must be a bounded list')
        criterion_by_id={row['id']:row for row in state['contract']['criteria']}
        seen_criteria=set()
        for result in partial:
            if not isinstance(result,dict) or result.get('id') not in criterion_by_id or result['id'] in seen_criteria:
                raise ValueError('Checkpoint has duplicate/unknown requirement result')
            seen_criteria.add(result['id'])
            single={**state,'contract':{**state['contract'],'criteria':[criterion_by_id[result['id']]]}}
            result_valid(single,[result],require_execution=True)
            refs=result.get('evidence_ids',[])
            observations=result.get('observation_ids',[])
            if refs:
                if not all(current_receipt(state,ref) is not None for ref in refs):
                    raise ValueError('Checkpoint requirement evidence is old or stale')
                mark_verified_progress(state,'requirement_pass',result['id'],refs[0])
            elif observations:
                mark_verified_progress(state,'requirement_pass',result['id'],
                                       observation_id=observations[0])
            else:
                raise ValueError('Checkpoint requirement pass needs current observed evidence')
        steps=payload.get('completed_steps',[])
        if not isinstance(steps,list) or len(steps)>20:
            raise ValueError('Checkpoint completed_steps must be a bounded list')
        planned={step['id']:step for step in (state.get('plan') or {}).get('content',{}).get('steps',[])}
        completed=set(state.get('completed_steps',[]))
        for item in steps:
            if not isinstance(item,dict) or set(item)!={'id','evidence_ids'} or item.get('id') not in planned:
                raise ValueError('Completed step needs a known id and evidence_ids')
            refs=item['evidence_ids']
            if (not isinstance(refs,list) or not 1<=len(refs)<=12 or
                    any(not isinstance(ref,str) or current_receipt(state,ref) is None for ref in refs)):
                raise ValueError('Completed step needs fresh successful execution evidence')
            if not set(planned[item['id']].get('depends_on',[])) <= completed:
                raise ValueError('Complete plan prerequisites before dependent steps')
            completed.add(item['id'])
            mark_verified_progress(state,'plan_step',item['id'],refs[0])
        state['completed_steps']=sorted(completed)
        state['checkpoints']=(state.get('checkpoints',[])+[entry])[-6:]
        return 'Checkpoint saved.'
    if op=='candidate':
        if state.get('plan_required') and (not state.get('plan') or state['plan']['intent_version']!=state['intent_version']):
            raise ValueError('Create/update the required plan before submitting this task')
        for rel,sha in state['contract'].get('protected_inputs',{}).items():
            if file_hash(checked_path(ws,rel,existing=True))!=sha:raise ValueError('Protected original input changed: '+rel)
        if state['contract']['intent_version'] != state['intent_version']:
            raise ValueError('Reconcile the new user amendment with sync-intent first')
        if state.get('pending_turn_ids'):
            raise ValueError('Resolve the pending user message before final review')
        if state.get('needs_replan'):
            raise ValueError('Change the plan after repeated failed checks before submitting a candidate')
        result_valid(state,payload.get('criterion_results'),require_execution=True)
        large_proof, large_files = None, []
        if state.get('large_task'):
            from .large_native import candidate_evidence
            large_proof, large_files = candidate_evidence(state, payload)
        scope_proof = None; scope_files = []
        if state['contract'].get('coding_scope'):
            scope_proof, scope_files = native_evidence(state, payload.get('coding_scope_evidence'), checked_path)
        sources=payload.get('sources',[]);source_files=source_check(state,sources)
        expected_provenance={r['id'] for r in state['contract']['criteria'] if r['kind']=='provenance'}
        provenance_rows=payload.get('provenance_manifests',[])
        if not isinstance(provenance_rows,list) or len(provenance_rows)>20:
            raise ValueError('provenance_manifests must be a bounded list')
        provenance_evidence=[];provenance_files=[];provenance_ids=set()
        result_by_id={r['id']:r for r in payload['criterion_results']}
        for item in provenance_rows:
            if not isinstance(item,dict) or set(item)!={'criterion_id','manifest'}:
                raise ValueError('Provenance entry needs criterion_id and manifest')
            cid=item['criterion_id'];rel=item['manifest']
            if cid not in expected_provenance or cid in provenance_ids or not isinstance(rel,str):
                raise ValueError('Unknown or duplicate provenance criterion')
            provenance_ids.add(cid)
            checked_path(ws,rel,existing=True)
            record=validate_provenance(ws,rel)
            if not record['verified_claims']:
                raise ValueError('Provenance criterion needs a verified measured/calculated claim')
            if any(Path(rel).suffix.lower()!='.py' for rel in record['analysis_paths']):
                raise ValueError('Native provenance execution currently supports Python .py analysis only')
            needed=set(record['node_paths'])
            executed_analysis=set()
            for ref in result_by_id[cid]['evidence_ids']:
                receipt=validate_execution(ws,ref,run_id=state['run_id'],revision=state['intent_version'])
                if not needed.issubset(set(receipt['subject_files'])):
                    raise ValueError('Provenance execution receipt does not bind all declared nodes')
                argv=receipt['invocation']['argv']
                if len(argv)<2 or argv[1] in {'-c','-e','-m','-Command','--eval'}:
                    raise ValueError('Provenance check must directly execute a declared analysis entrypoint')
                if (Path(argv[0]).resolve()!=Path(sys.executable).resolve() or
                        receipt['invocation']['executable_sha256']!=file_hash(Path(sys.executable).resolve())):
                    raise ValueError('Provenance analysis must run with the pinned harness Python interpreter')
                analysis_entry=Path(argv[1])
                if not analysis_entry.is_absolute():
                    analysis_entry=Path(receipt['invocation']['cwd'])/analysis_entry
                analysis_entry=analysis_entry.resolve()
                for analysis_rel in record['analysis_paths']:
                    if (Path(analysis_rel).suffix.lower()=='.py' and
                            analysis_entry==checked_path(ws,analysis_rel,existing=True).resolve()):
                        executed_analysis.add(analysis_rel)
            if executed_analysis!=set(record['analysis_paths']):
                raise ValueError('Provenance check did not execute every declared analysis entrypoint')
            provenance_evidence.append({'criterion_id':cid,**record})
            provenance_files.extend([rel,*record['node_paths']])
        if provenance_ids!=expected_provenance:
            raise ValueError('Every provenance criterion needs a current manifest')
        files=payload.get('files',state['contract']['artifacts'])
        if not isinstance(files,list) or not set(state['contract']['artifacts']).issubset(files):
            raise ValueError('All declared artifacts must be in candidate files')
        learning_check=validate_learning_check(state['mode'],state['prompts'],
            payload.get('learning_check'),ws,checked_path)
        document_evidence,render_files=document_candidate(state,payload,checked_path)
        render_manifests=[r['render_manifest'] for r in document_evidence]
        bundle_leaves=set(render_files)
        learning_files=[learning_check['explanation_file']] if learning_check and 'explanation_file' in learning_check else []
        ordinary=list(dict.fromkeys(files+source_files+provenance_files+learning_files+scope_files+large_files+
                                 list(state['contract'].get('protected_inputs',{}))))
        ordinary=[rel for rel in ordinary if rel not in bundle_leaves]
        task_bundle = None
        if large_proof:
            task_bundle = large_proof['snapshot_manifest']
            frozen = bounded_json(checked_path(ws, task_bundle, existing=True), 8 * 1024 * 1024)
            prefix = Path(frozen['staging_workspace']).relative_to(ws)
            task_leaves = {(prefix / name).as_posix() for name in frozen['files']}
            ordinary = [rel for rel in ordinary if rel not in task_leaves and rel != task_bundle]
        large=payload.get('large_artifacts',[])
        if not isinstance(large,list) or len(large)>30 or len(set(large))!=len(large) or not set(large)<=set(ordinary):
            raise ValueError('large_artifacts must name unique candidate files')
        descriptors=[{'kind':'large_artifact','path':rel} if rel in large else rel for rel in ordinary]
        descriptors += [{'kind':'render_bundle','manifest':rel} for rel in render_manifests]
        if task_bundle:
            descriptors.append({'kind':'task_bundle','manifest':task_bundle})
        snap=build_snapshot(ws,descriptors)
        execution_refs={ref:file_hash(checked_path(ws,ref,existing=True))
                        for row in payload['criterion_results']
                        for ref in row.get('evidence_ids',[])}
        deep_evidence=None
        if state.get('execution_profile', execution_profile())['review_depth']=='strengthened':
            required_targets=set(state['contract']['artifacts'])
            covering=[]
            for ref in execution_refs:
                receipt=current_receipt(state,ref)
                if receipt is not None and required_targets <= set(receipt['subject_files']):
                    covering.append(ref)
            if not covering:
                raise ValueError('Deep profile needs a fresh successful execution receipt covering every target artifact')
            deep_evidence={'target_covering_receipts':sorted(covering)}
        used_observations=list(dict.fromkeys(ref for row in payload['criterion_results']
                          for ref in row.get('observation_ids',[])))
        observation_by_id={row['id']:row for row in state.get('tool_observations',[])}
        procedure=payload.get('procedure',[])
        if not isinstance(procedure,list) or len(procedure)>10 or not all(isinstance(s,str) and len(s)<=1000 for s in procedure):
            raise ValueError('procedure must contain at most 10 short text steps')
        procedure_bundle=validate_procedure_bundle(payload.get('procedure_bundle'))
        if procedure_bundle is not None and not procedure:
            raise ValueError('A procedure bundle needs ordinary procedure steps')
        c={'files':snap,'snapshot_schema':1,'intent_version':state['intent_version'],
            'large_task_evidence':large_proof,'recorded_at':time.time(),
            'contract_hash':state['contract_hash'],'criterion_results':payload['criterion_results'],
            'sources':sources,'procedure':procedure,'procedure_bundle':procedure_bundle,
            'document_evidence':document_evidence,
            'provenance_evidence':provenance_evidence,'learning_check':learning_check,
            'tool_observations':[observation_by_id[ref] for ref in used_observations],
            'plan_sha256':(state.get('plan') or {}).get('sha256'),
            'execution_receipts':execution_refs,'evidence_schema':1}
        if deep_evidence is not None:
            c['deep_evidence']=deep_evidence
        if scope_proof:
            c['coding_scope_proof'] = scope_proof
            c['coding_scope_evidence'] = payload['coding_scope_evidence']
        c['digest']=digest(c)
        state['candidate']=c; state['status']='reviewing'; state['feedback']=''
        return 'Candidate '+c['digest']+' recorded. Spawn adhd-verifier on this exact digest; do not approve your own work.'
    raise ValueError('Unknown native operation: '+op)


def process_inbox(state: dict, d: Path) -> list[str]:
    ws=Path(state['workspace']); b=bridge(ws,state['key']); inbox=b/'inbox'
    if not inbox.exists(): return []
    checked_path(ws,str(inbox.relative_to(ws)))
    reports=[]
    for p in sorted(inbox.glob('*.json'))[:8]:
        checked_path(ws,str(p.relative_to(ws)),existing=True)
        if not re.fullmatch('[0-9a-f]{32}.json',p.name): continue
        if p.stem in state['processed']: continue
        before=json.loads(json.dumps(state)); obj=None
        try:
            obj=bounded_json(p)
            if not isinstance(obj,dict) or obj.get('session') != state['key'] or obj.get('id') != p.stem:
                raise ValueError('Request/session mismatch')
            if not isinstance(obj.get('payload'),dict): raise ValueError('payload must be an object')
            text=apply_request(state,obj.get('op',''),obj['payload'],d)
            ack={'ok':True,'message':text,'status':state['status']}
        except (ValueError,TypeError,OSError) as e:
            if state.get('run_id') != before.get('run_id') and state.get('run_id'):
                release_writer(state)
            state.clear();state.update(before)
            ack={'ok':False,'message':str(e),'status':state['status']}
        state['processed']=(state['processed']+[p.stem])[-200:]
        out=checked_path(ws,str((b/'outbox'/(p.stem+'.json')).relative_to(ws)))
        atomic_json(out,ack)
        archive=checked_path(ws,str((b/'processed'/p.name).relative_to(ws)))
        archive.parent.mkdir(parents=True,exist_ok=True);os.replace(p,archive)
        append_event(d,'request',{'operation':obj.get('op','') if isinstance(obj,dict) else '?','ack':ack})
        reports.append(json.dumps(ack,ensure_ascii=False))
    return reports


def spawn_guard(state: dict, ev: dict) -> dict:
    if state['status'] not in ACTIVE: return {}
    tool=ev.get('tool_name','')
    if tool not in {'spawn_agent','Agent'}: return {}
    args=ev.get('tool_input',{})
    if not isinstance(args,dict): return deny('Invalid spawn arguments')
    role=args.get('agent_type',args.get('subagent_type',''))
    if not isinstance(role,str) or role not in ROLES: return deny('Use a ADHD role during this run, or pause the native owner before using another harness.')
    if role=='adhd-implementer' and state.get('plan_required') and (not state.get('plan') or state['plan']['intent_version']!=state['intent_version']):
        return deny('Profile plan and prerequisite inspection required before implementation. Use scout/planner or the Sol director first.')
    choice=route(role)
    requested=args.get('model')
    if requested and requested != choice['model']:
        return deny('Role/model mismatch: '+role+' uses '+choice['model'])
    requested_effort=args.get('reasoning_effort')
    if requested_effort is not None and requested_effort != choice['effort']:
        return deny('Role/reasoning effort mismatch: '+role+' uses '+choice['effort'])
    if role=='adhd-verifier' and not is_fresh(state): return deny('Create a fresh candidate before starting the verifier.')
    if ev.get('model') in {'gpt-6-astra','gpt-6-luna'}:
        return deny('Reader/adviser children must not spawn children. Return to the Sol director.')
    # Reserve before spawn so simultaneous calls cannot exceed the quota.
    now=time.time(); state['reservations']={k:v for k,v in state['reservations'].items() if now-v['time']<120}
    tool_use_id=ev.get('tool_use_id')
    if role=='adhd-verifier' and not tool_use_id:
        return deny('Verifier spawn needs an observable tool_use_id for independent correlation.')
    identity=str(tool_use_id or uuid.uuid4().hex)
    if identity in state['reservations']: return {}
    active=[c for c in state['children'].values() if c['status']=='running']
    pending=list(state['reservations'].values())
    if len(active)+len(pending)>=state['policy']['max_children']:
        return deny('Parallel-child limit reached; wait for a running child.')
    work_limit=state['policy']['max_total_children']-state['policy']['max_review_children']
    if (role!='adhd-verifier' and limit_enabled(state, 'max_total_children')
            and state['total_children']-state.get('review_children',0)>=work_limit):
        return deny('Work-child budget reached; preserve the review reserve.')
    if role=='adhd-verifier' and limit_reached(state, 'max_review_children', state.get('review_children',0)):
        return deny('Review-child budget reached; report the unresolved candidate.')
    if limit_reached(state, 'max_total_children', state['total_children']):
        return deny('Task child-call budget reached; integrate current evidence without more children.')
    if limit_reached(state, 'max_lifetime_children', state.get('lifetime_children',0)):
        return deny('Lifetime child-call budget reached; do not reset it through resume.')
    if role=='adhd-implementer' and any(c['role']==role for c in active+pending):
        return deny('Single-writer invariant: another implementation child is active.')
    if choice['kind']=='astra':
        if state['astra_calls']>=state['policy']['max_astra_calls']:
            return deny('Astra consultation budget reached. Sol must implement/integrate the existing decision brief.')
        # Bound the explicit delegation brief; inherited/tool context remains a soft budget.
        if len(str(args.get('message',args.get('prompt',''))))>8000:
            return deny('Astra brief exceeds 8000 characters. Send only the decision, constraints and relevant evidence pointers.')
        state['astra_calls']+=1
    state['total_children']+=1
    state['lifetime_children']=state.get('lifetime_children',0)+1
    if role=='adhd-verifier':state['review_children']=state.get('review_children',0)+1
    state['reservations'][identity]={'role':role,'time':now,'tool_use_id':tool_use_id,
        'selected_model':choice['model'],'selected_effort':choice['effort'],
        'selection_reason':choice['description']}
    return {}


def verifier_profile_hash() -> str | None:
    """The lifecycle-only App path needs the exact installed read-only profile."""
    source=ROOT/'native'/'agents'/'adhd-verifier.toml'
    installed=home()/'agents'/'adhd-verifier.toml'
    if (not source.is_file() or source.is_symlink() or not installed.is_file()
            or installed.is_symlink()):
        return None
    try:
        expected=source.read_text(encoding='utf-8').replace('ADHD_ROOT',str(ROOT))
        actual=installed.read_text(encoding='utf-8')
        if actual!=expected:return None
        profile=tomllib.loads(actual)
        if (profile.get('name')!='adhd-verifier' or
                profile.get('model')!=route('adhd-verifier')['model'] or
                profile.get('model_reasoning_effort')!=route('adhd-verifier')['effort'] or
                profile.get('sandbox_mode')!='read-only'):
            return None
        return file_hash(installed)
    except (OSError,UnicodeError,ValueError):
        return None


def start_child(state: dict, ev: dict) -> dict:
    role=ev.get('agent_type'); aid=ev.get('agent_id')
    if (state['status'] not in ACTIVE or role not in ROLES or
            not isinstance(aid,str) or not 0<len(aid)<=128): return {}
    if str(aid) in state['children']:
        state['children'][str(aid)]['correlated']=False
        state['feedback']='Duplicate subagent ID observed; review cannot attest this child.'
        return {}
    tool_use_id=ev.get('tool_use_id')
    reservation=state['reservations'].get(str(tool_use_id)) if tool_use_id else None
    matched=bool(reservation and reservation['role']==role)
    profile_hash=verifier_profile_hash() if role=='adhd-verifier' and not matched else None
    active_children=sum(child['status']=='running' for child in state['children'].values())
    attestation=state.get('session_verifier_profile') or {}
    lifecycle_bound=bool(role=='adhd-verifier' and not tool_use_id and profile_hash and
        attestation.get('release_root')==str(ROOT.resolve()) and
        attestation.get('profile_hash')==profile_hash and
        ev.get('model')==route(role)['model'] and
        ev.get('reasoning_effort') in {None,route(role)['effort']} and
        is_fresh(state) and
        active_children<state['policy']['max_children'] and
        not limit_reached(state, 'max_total_children', state['total_children']) and
        not limit_reached(state, 'max_lifetime_children', state.get('lifetime_children',0)) and
        not limit_reached(state, 'max_review_children', state.get('review_children',0)))
    if matched: state['reservations'].pop(str(tool_use_id))
    else:
        # Current App lifecycle hooks omit the spawn tool ID. The custom profile,
        # host agent ID, model and fresh snapshot form the alternative admission.
        state['total_children']+=1
        state['lifetime_children']=state.get('lifetime_children',0)+1
        if role=='adhd-verifier': state['review_children']=state.get('review_children',0)+1
        if role=='adhd-architect': state['astra_calls']+=1
        if not lifecycle_bound:
            state['feedback']='A subagent started without an admissible spawn reservation or verified lifecycle profile.'
    state['children'][str(aid)]={'role':role,'status':'running',
       'model':ev.get('model'),'observed_effort':ev.get('reasoning_effort'),
       'configured_effort':route(role)['effort'],
       'effort_source':'pinned_role_profile' if lifecycle_bound else 'spawn_reservation' if matched else 'unverified',
       'tool_use_id':tool_use_id,'correlated':matched or lifecycle_bound,
       'correlation':'tool_use_id' if matched else 'host_agent_lifecycle' if lifecycle_bound else 'unverified',
       'profile_hash':profile_hash,
       'selected_model':route(role)['model'],'selected_effort':route(role)['effort'],
       'selection_reason':reservation.get('selection_reason') if matched and reservation is not None else route(role)['description'],
       'digest':state['candidate']['digest'] if role=='adhd-verifier' and state.get('candidate') else None}
    instruction='Do not spawn children. Return concise findings with exact file/source pointers. Never claim unexecuted checks.'
    if role=='adhd-architect': instruction+=' Provide a decision brief <=1800 output tokens (soft instruction). No coding, browsing sweep or implementation. Resolve hard choices and exit.'
    if role=='adhd-verifier': instruction+=' Compare every observed user prompt with the current contract, then read view.json and real files. Return the strict verifier JSON described in adhd-native/SKILL.md.'
    return message('SubagentStart',instruction)


def stop_child(state: dict, ev: dict, d: Path) -> dict:
    child=state.get('children',{}).get(str(ev.get('agent_id','')))
    if not child or child.get('status') != 'running': return {}
    child['status']='finished'
    usage=ev.get('usage')
    measured=usage if isinstance(usage,dict) and usage and all(
        type(value) is int and value>=0 for value in usage.values()) else None
    call={'role':child['role'],'selected_model':child.get('selected_model'),
          'selected_effort':child.get('selected_effort'),
          'selection_reason':child.get('selection_reason'),
           'observed_model':child.get('model'),'observed_effort':child.get('observed_effort'),
           'configured_effort':child.get('configured_effort'),'effort_source':child.get('effort_source'),
          'observed_source':'SubagentStart' if child.get('model') else None,
          'usage_observed':measured is not None,'usage':measured,
          'retry':sum(c['role']==child['role'] for c in state.get('model_calls',[])),
          'outcome':'completion_unverified','finished_at':time.time()}
    state['model_calls']=(state.get('model_calls',[])+[call])[-100:]
    if state['status']=='handoff_pending' and not any(c['status']=='running' for c in state['children'].values()):
        archive_for_new_task(state,d,state['handoff_turn_id'])
        return message('SubagentStop','Previous run archived; a new begin contract may be submitted.')
    if state['status']=='interrupt_pending' and not any(c['status']=='running' for c in state['children'].values()):
        resume = state.pop('host_resume_pending', False) and state.get('pending_terminal') == 'paused'
        end_after_children(state, state.get('pending_terminal','paused'))
        if resume:
            resume_native(state, 'Resumed by the host goal after all old children stopped.')
            return message('SubagentStop','Host goal resume processed after all old children finished.')
        return message('SubagentStop','Run stopped after all running children finished; writer lease released.')
    if child['role'] != 'adhd-verifier' or state['status'] not in ACTIVE: return {}
    try:
        if not child.get('correlated') or child.get('correlation') not in {'tool_use_id','host_agent_lifecycle'}:
            raise ValueError('Verifier lacked an observed host agent lifecycle or exact spawn correlation')
        if child['correlation']=='tool_use_id' and not child.get('tool_use_id'):
            raise ValueError('Verifier spawn lacked its reserved tool_use_id')
        if child['correlation']=='host_agent_lifecycle' and (
                not child.get('profile_hash') or verifier_profile_hash()!=child['profile_hash']):
            raise ValueError('Verifier profile was missing or changed during review')
        if state.get('pending_turn_ids'):
            raise ValueError('Pending user turn must be reconciled before review')
        if any(other.get('status') == 'running' for other in state['children'].values()):
            raise ValueError('Other children are still running; wait for them before independent completion')
        if child.get('model') != route('adhd-verifier')['model']:
            raise ValueError('Verifier model was missing or mismatched; cannot attest the configured independent review')
        if (child.get('observed_effort') not in {None,route('adhd-verifier')['effort']} or
                ev.get('reasoning_effort') not in {None,route('adhd-verifier')['effort']}):
            raise ValueError('Verifier reasoning effort was mismatched')
        if ev.get('agent_type') not in {None,child['role']} or ev.get('model') not in {None,child['model']}:
            raise ValueError('Verifier stop event role/model did not match its start')
        if not child.get('digest') or not is_fresh(state) or child['digest'] != state['candidate']['digest']:
            raise ValueError('Verifier snapshot is stale; changes require a new candidate and review')
        text=ev.get('last_assistant_message') or ''
        if len(text)>MAX_MESSAGE: raise ValueError('Verifier result too large')
        if text.strip().startswith('```'):
            text=re.sub(r'^```(?:json)?\s*|\s*```$','',text.strip())
        verdict=json.loads(text)
        if not isinstance(verdict,dict) or verdict.get('reviewed_digest')!=child['digest']:
            raise ValueError('Verifier did not return the exact reviewed_digest')
        if (verdict.get('reviewed_contract_hash') != state['contract_hash'] or
                verdict.get('reviewed_turn_ids') != [p['turn_id'] for p in state['prompts']] or
                verdict.get('intent_alignment') is not True):
            raise ValueError('Verifier did not attest the current contract against every original user turn')
        if verdict.get('verdict')!='approve':
            raise ValueError('Independent reviewer rejected candidate: '+truncate_content(str(verdict.get('findings','No reason provided')),2000))
        result_valid(state,verdict.get('criterion_results'))
        candidate_results={row['id']:row for row in state['candidate']['criterion_results']}
        for row in verdict['criterion_results']:
            if row.get('evidence_ids',[]) != candidate_results[row['id']].get('evidence_ids',[]):
                raise ValueError('Reviewer changed the candidate execution evidence references')
            if row.get('observation_ids',[]) != candidate_results[row['id']].get('observation_ids',[]):
                raise ValueError('Reviewer changed the observed host tool references')
        if state['candidate'].get('deep_evidence'):
            review=verdict.get('evidence_review')
            if (not isinstance(review,dict) or
                    review.get('target_covering_receipts') != state['candidate']['deep_evidence']['target_covering_receipts'] or
                    not isinstance(review.get('findings'),list) or
                    not review['findings'] or
                    any(not isinstance(item,str) or not item.strip() or len(item)>2000 for item in review['findings'])):
                raise ValueError('Deep review needs concrete findings about the current target-covering executions')
        review_documents(state,verdict)
        state['host_capabilities']['verifier_correlated']=True
        state['review_receipt']={'agent_id':str(ev['agent_id']),'model':child.get('model'),
                                 'correlation':child['correlation'],'profile_hash':child.get('profile_hash'),
                                 'configured_effort':child.get('configured_effort'),
                                 'observed_effort':child.get('observed_effort'),
                                 'effort_source':child.get('effort_source'),
                                 'time':time.time(),'digest':child['digest'],'verdict':verdict}
        if state.get('large_task'):
            from .large_native import accept_verified
            accept_verified(state)
        state['feedback']='Verified candidate completed.'
        call['outcome']='independent_approval'
        atomic_json(d/('completion-'+state['run_id']+'.json'),state['review_receipt'])
        state['recipe_saved']=None
        try:
            state['recipe_saved']=save_recipe(Path(state['workspace']),state['mode'],state['prompts'][-1]['text'],
                  state['candidate']['procedure'],str(d/('completion-'+state['run_id']+'.json')),
                  bundle=state['candidate'].get('procedure_bundle'))
        except (ValueError,OSError) as error:
            state['memory_warning']=str(error)[:500]
        end_after_children(state, 'complete')
        return message('SubagentStop','ADHD independent gate passed. Report actual deliverables, evidence, and remaining limitations; do not add unverified edits.')
    except (ValueError,TypeError,OSError) as e:
        call['outcome']='review_rejected'
        failure=record_failure(state,classify_failure(str(e)),str(e),
                               'hook:SubagentStop/'+str(ev.get('agent_id','')))
        state['status']='revising'; state['feedback']=failure['next_action']+' '+str(e)
        quarantine([r['id'] for r in state.get('recipes',[])])
        return message('SubagentStop',state['feedback'])


def context(state: dict) -> str:
    b=bridge(Path(state['workspace']),state['key'])
    mode=state.get('mode')
    outcome= ('Study mode: explain the concept, provide a short applicable self-check and answer key; '
              'give a full solution when explicitly requested. '
              if mode=='study' else
              'Research mode: separate measured, calculated, interpreted and unverified claims; '
              'bind numerical claims with provenance. ' if mode=='research' else
              'Deliverable mode: complete and verify the requested output. ')
    goal = ('\nGoal execution requested. Read '+str(ROOT/'skills'/'adhd-goal'/'SKILL.md')+
            '. Preserve the requested outcome and continue work/check/repair until independently accepted. '
            'Reconcile new goal text before enabling native goal on an existing run. '
            if state.get('goal_request') else '')
    return (DISPLAY_NAME+' v'+__version__+' available. Read '+str(ROOT/'skills'/'adhd-native'/'SKILL.md')+
      '\nSESSION='+state['key']+'; workspace='+state['workspace']+'; view='+str(b/'view.json')+
      '\nFor small questions answer directly (optionally adhd-light); do not start a durable loop. '
      'For substantive deliverables/multi-step work, submit begin through the installed adhd.py native bridge. '
      'First read actual inputs, route installed skills and relevant project memory; submit a deep plan covering every requirement before implementation. '
       +outcome+
       'Preserve the selected parent model and effort; pinned ADHD helpers use Sol, Luna or Astra by role. '
       'If OMX or another owner is active, do not begin ADHD. Original user text in view.json outranks generated plans. '
       'Native tokens are unmeasured; do not claim a hard token cap. Status='+state['status']+goal+
      ('; feedback='+truncate_content(state.get('feedback',''),500) if state.get('feedback') else ''))


def handle_event(ev: dict) -> dict:
    if os.environ.get('ADHD_EXEC_OWNER') in {'legacy','native-check'}: return {}
    if not isinstance(ev,dict): raise ValueError('Hook input must be an object')
    name=ev.get('hook_event_name','')
    normalized=normalize_event(ev)
    if not normalized['supported']: return {}
    key=session_key(ev.get('session_id','')); ws=Path(ev.get('cwd','')).expanduser().resolve()
    if not ws.is_dir() or not ev.get('cwd'): raise ValueError('Missing workspace')
    with locked(key) as d:
        state=read_json(d/'state.json') or initial(key,ws)
        if state['workspace']!=str(ws): raise ValueError('Session workspace changed; start a new session')
        migrate_state(state,d)
        observe_event(state['host_capabilities'],normalized)
        if name in {'SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop'}:
            observe_thread_goal(state, ev, d)
        out={}
        if name=='UserPromptSubmit':
            prompt=ev.get('prompt',''); turn=str(ev.get('turn_id',''))
            if not isinstance(prompt,str) or len(prompt)>MAX_MESSAGE: raise ValueError('Invalid/oversized user prompt')
            continuation=prompt.startswith(PREFIX+key+']')
            if not continuation and (not turn or turn not in state['prompt_turns']):
                state['prompt_turns']=(state['prompt_turns']+[turn])[-200:]
                normalized=prompt.strip().lower().rstrip('.!。')
                if normalized in {'stop','cancel','그만','중단','멈춰','그만해','작업 중단'}:
                    state['halt_emitted']=False;state['feedback']='Stopped by explicit user message.'
                    end_after_children(state, 'cancelled')
                elif normalized in {'resume','재개','이어서 계속','계속 진행'} and state.get('contract') and state['status'] in {'paused','blocked','budget_exhausted','cancelled'}:
                    resume_native(state, 'Resumed by explicit user message; original contract retained.')
                else:
                    state['prompts'].append({'text':prompt,'turn_id':turn,'time':time.time()})
                    outcome = goal_command(prompt)
                    if outcome is not None:
                        state['goal_request']={'source_turn_id':turn, 'outcome':outcome}
                    if state.get('contract'):
                        state['pending_turn_ids'].append(turn)
                        state['feedback']='Classify the latest user turn as no_change, amend or new_task before final review.'
                    else:
                        state['intent_version']+=1
                append_event(d,'user_prompt',{'turn_id':turn,'intent_version':state['intent_version'],'status':state['status']})
            out=message(name,context(state))
        elif name=='SessionStart':
            # Codex loads custom agent roles with the session. Disk changes during
            # a hot upgrade cannot attest what an already-running session loaded.
            state['session_verifier_profile']={'release_root':str(ROOT.resolve()),
                'profile_hash':verifier_profile_hash()}
            out=message(name,context(state))
        elif name=='PostCompact':
            out=message(name,context(state))
        elif name=='PostToolUse':
            observe_host_tool(state,ev)
            reports=process_inbox(state,d)
            if reports: out=message(name,'\n'.join(reports)+'\nRead bridge/view.json for authoritative exported status.')
        elif name=='PreToolUse': out=spawn_guard(state,ev)
        elif name=='SubagentStart': out=start_child(state,ev)
        elif name=='SubagentStop': out=stop_child(state,ev,d)
        elif name=='SessionEnd':
            state.pop('host_resume_pending', None)
            if state['status'] in ACTIVE:
                end_after_children(state, 'paused')
        elif name=='Interrupt':
            state.pop('host_resume_pending', None)
            if state['status'] in ACTIVE:
                end_after_children(state, 'paused',
                                   'Waiting for running children to stop before releasing ownership.')
        elif name=='Stop':
            reports=process_inbox(state,d)
            if state['status']=='complete' and not state.get('pending_turn_ids') and not is_fresh(state):
                state['status']='revising';state['feedback']='Files changed after review. Recreate candidate and review.';claim_writer(state)
            if state['status'] in ACTIVE:
                turn=str(ev.get('turn_id') or digest([ev.get('last_assistant_message'),ev.get('stop_hook_active')]))
                if turn in state['seen_stops']:
                    out=state.get('last_stop_decision',{})
                else:
                    state['seen_stops']=(state['seen_stops']+[turn])[-100:]
                    state['rounds']+=1;state['lifetime_rounds']=state.get('lifetime_rounds',0)+1
                    update_stagnation(state)
                    exceeded=(limit_reached(state, 'max_rounds', state['rounds'], inclusive=False) or
                              limit_reached(state, 'max_lifetime_rounds', state['lifetime_rounds'], inclusive=False) or
                              limit_reached(state, 'max_seconds', time.time()-state['started']) or
                              limit_reached(state, 'max_stagnation', state['stagnation']))
                    if exceeded:
                        failure=record_failure(state,'budget_exhausted',
                            'Continuation/time/no-progress limit reached','hook:Stop/'+turn)
                        end_after_children(state, 'budget_exhausted', failure['next_action'])
                        out={'continue':False,'stopReason':state['feedback']}
                    else:
                        action='Resolve feedback, perform the work, then submit a fresh candidate and independently review it.'
                        if state.get('loop_mode') == 'goal':
                            action='Continue toward the original goal: implement, check, repair, submit a fresh candidate and independently review it.'
                            if state['stagnation'] >= state['policy']['max_stagnation']:
                                action+=' No verified progress: change the hypothesis or approach using concrete failure evidence; report blocked if no viable path remains.'
                        if state['status']=='reviewing': action='Wait for the verifier, or spawn adhd-verifier on the candidate digest. Do not self-approve.'
                        out={'decision':'block','reason':PREFIX+key+'] '+action+' Read '+str(bridge(ws,key)/'view.json')+'. '+truncate_content(state.get('feedback',''),800)}
            elif state['status']=='complete' and not state.get('halt_emitted'):
                out={'continue':False,'stopReason':'ADHD task completed with a fresh independent review. Do not run another automatic loop for this task.'}
            elif state['status'] in {'cancelled','paused','budget_exhausted','blocked','handoff_pending','interrupt_pending'} and not state.get('halt_emitted'):
                out={'continue':False,'stopReason':state.get('feedback') or state['status']}
            if out.get('continue') is False: state['halt_emitted']=True
            state['last_stop_decision']=out
        persist(d,state)
        return out


def submit_request(key: str, workspace: Path, op: str, payload: dict) -> dict:
    """Sandbox-side transport only. A trusted host PostToolUse/Stop hook applies it."""
    workspace=workspace.expanduser().resolve(); b=bridge(workspace,key)
    rid=uuid.uuid4().hex
    value={'id':rid,'session':key,'op':op,'payload':payload}
    if len(json.dumps(value,ensure_ascii=False).encode())>MAX_MESSAGE: raise ValueError('Request too large')
    p=checked_path(workspace,str((b/'inbox'/(rid+'.json')).relative_to(workspace)))
    atomic_json(p,value)
    return {'queued':rid,'receipt':str(b/'outbox'/(rid+'.json')),
            'note':'The native Codex hook must process this request; queued is not success.'}
