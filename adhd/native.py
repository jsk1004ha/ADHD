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
from time import perf_counter_ns
import tomllib
import uuid
from typing import Any
from . import DISPLAY_NAME, __version__
from . import dependencies as _dependencies
from filelock import FileLock
from .core import (ROOT, MODES, atomic_json, digest, file_hash, home, read_json, safe_path,
                   store, recipes, save_recipe, quarantine, select_mode)
from .models import ROLES, route, execution_profile
from .request_routing import (classify_request, is_brief_approval, is_status_followup,
                              is_substantive_request_source)
from .gates import (validate_plan, validate_document_contract, document_candidate,
                    review_documents, validate_learning_check)
from .host_capabilities import (normalize_event, observe_event, completion_capability,
                                observed_thread_goal)
from .intent import apply_intent_patch
from .delivery_policy import validate_contract_metadata, deadline_guidance, delivery_status
from .execution_decisions import (register_action, authorize_action, action_decision,
                                  classify_denial, record_action_outcome, next_wait,
                                  matched_host_action, observe_action_host_result)
from .evidence import (validate_execution, observe_host_tool, validate_tool_observations,
                       receipt_test_failures)
from .recovery import classify_failure, record_failure, verified_failure_signature
from .memory import validate_procedure_bundle
from . import leases
from .vendor.smolagents_context import truncate_content

ACTIVE = {'working', 'reviewing', 'revising'}
PREFIX = '[ADHD_CONTINUE:'
MAX_MESSAGE = 160_000
MAX_FILE = 128 * 1024 * 1024
def hook_output_sizes(out: dict) -> dict:
    """Count decoded strings sent to model-facing context/block/error fields.

    The JSON envelope, escaped text, event names and decision enums are excluded.
    Codex can further truncate/ignore these strings; this measures hook emission.
    """
    fields = {}
    for name in ('reason', 'systemMessage', 'stopReason'):
        if isinstance(out.get(name), str):
            fields[name] = out[name]
    specific = out.get('hookSpecificOutput', {})
    if isinstance(specific, dict):
        for name in ('additionalContext', 'permissionDecisionReason'):
            if isinstance(specific.get(name), str):
                fields['hookSpecificOutput.' + name] = specific[name]
    return {'emitted_context_chars': sum(len(value) for value in fields.values()),
            'emitted_context_bytes': sum(len(value.encode('utf-8')) for value in fields.values()),
            'emitted_fields': list(fields)}


class HookDiagnostics:
    def __init__(self, *, enabled=None, started_ns=None):
        self.enabled = os.environ.get('ADHD_HOOK_DIAGNOSTICS') == '1' if enabled is None else enabled
        self.started_ns = perf_counter_ns() if started_ns is None else started_ns
        self.phases_ns = {}
        self.key = None
        self.event_name = None
        self.host_usage = None

    @contextmanager
    def phase(self, name):
        if not self.enabled:
            yield
            return
        started = perf_counter_ns()
        try:
            yield
        finally:
            self.phases_ns[name] = self.phases_ns.get(name, 0) + perf_counter_ns() - started

    def record(self, out, serialized):
        return {'schema_version': 1, 'event_name': self.event_name,
                'internal_elapsed_ns': perf_counter_ns() - self.started_ns,
                'phases_ns': dict(self.phases_ns), **hook_output_sizes(out),
                'stdout_json_bytes': len((serialized + os.linesep).encode('utf-8')),
                'host_usage_observed': self.host_usage is not None,
                'host_usage': self.host_usage}


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
    match = re.match(r'^\s*(?:/goal|\$adhd-goal|\[\$adhd-goal\]\((?:skill://adhd-goal|[^\s)]*[\\/]adhd-goal(?:[\\/]SKILL\.md)?)\))(?=\s|$)\s*([\s\S]*)$', prompt)
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
        state['progress_last_gained_at']=time.time()
        state.pop('stagnation_guidance_issued',None)
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
def locked(key: str, diagnostics=None):
    d = folder(key); d.mkdir(parents=True, exist_ok=True)
    # Reuse filelock's OS-specific locks instead of writing a lock primitive.
    started = time.perf_counter_ns() if diagnostics and diagnostics.enabled else None
    with FileLock(str(d / 'state.lock'), timeout=4):
        if started is not None:
            diagnostics.phases_ns['lock_wait'] = time.perf_counter_ns() - started
        yield d


def write_hook_diagnostics(diagnostics, out: dict, serialized: str) -> None:
    """Append using the existing ledger/lock; only new diagnostics are best effort.

    Called after stdout emission, including the hook's normal error output. It
    never encloses request validation or state saving in its exception boundary.
    """
    if not diagnostics.enabled or diagnostics.key is None:
        return
    try:
        started = time.perf_counter_ns()
        with locked(diagnostics.key) as d:
            diagnostics.phases_ns['ledger_lock_wait'] = time.perf_counter_ns() - started
            append_event(d, 'hook_diagnostics', diagnostics.record(out, serialized))
    except Exception:
        # A lost observation cannot change an already determined hook result.
        pass

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


_NEW_CONTRACT_FIELDS = frozenset({'preserve_conditions', 'delivery_target',
                                  'runtime_context', 'deadline'})


def _request_needs_current_reader(op: str, payload: dict) -> bool:
    if op == 'begin':
        return bool(_NEW_CONTRACT_FIELDS & payload.keys())
    if op == 'sync-intent':
        return any(isinstance(row, dict) and str(row.get('target', '')).split('/')[0] in
                   _NEW_CONTRACT_FIELDS for row in payload.get('operations', []))
    return op == 'candidate' and 'delivery_evidence' in payload


def _current_session_reader(state: dict) -> bool:
    reader = state.get('session_reader') or {}
    return (reader.get('release_root') == str(ROOT.resolve())
            and reader.get('source_sha256') == file_hash(Path(__file__).resolve())
            and reader.get('max_state_version') == 2)

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
    if any(child.get('status') != 'finished' for child in state.get('children', {}).values()):
        raise ValueError('Cannot release writer while a child has unconfirmed termination')
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
    if any(child.get('status') != 'finished' for child in state.get('children', {}).values()):
        state['status'] = 'interrupt_pending'
        state['pending_terminal'] = terminal
    else:
        state['status'] = terminal
        state.pop('pending_terminal', None)
        release_writer(state)

def export_view(state: dict) -> None:
    b = bridge(Path(state['workspace']), state['key']); b.mkdir(parents=True, exist_ok=True)
    # This is a model-readable copy; never trust it as authoritative state.
    public = {k:v for k,v in state.items() if k not in {'processed','request_acks','reservations','seen_stops','host_goal_cursor','guidance_signature','last_assistant_message'}}
    public['completion_capability']=completion_capability(state.get('host_capabilities',{}))
    public['pending_intent']=bool(state.get('pending_turn_ids'))
    public['artifact_status']='candidate' if state.get('candidate') else 'in_progress' if state.get('contract') else 'unknown'
    public['acceptance_status']='accepted' if state['status']=='complete' else 'pending'
    target=(state.get('contract') or {}).get('delivery_target')
    delivery=(state.get('candidate') or {}).get('delivery_evidence')
    public['delivery_status']=(('complete' if state['status']=='complete' else 'pending')
                               if not target or target['kind']=='local_artifact' else
                               ('verified_candidate' if delivery and state['status'] in {'reviewing','complete'} else
                                delivery_status(target,delivery)['status']))
    from .run_metrics import summarize_run_metrics
    usage_rows=[{'id':'parent-unobserved','kind':'usage','actor':'parent',
                 'input_tokens':None,'cached_input_tokens':None,'output_tokens':None}]
    for index, call in enumerate(state.get('model_calls', [])):
        usage=call.get('usage') or {}
        usage_rows.append({'id':'child:'+str(call.get('agent_id',index))+':'+str(index),
            'kind':'usage','actor':'child:'+str(call.get('agent_id',index)),
            **{name:usage.get(name) for name in ('input_tokens','cached_input_tokens','output_tokens')}})
    public['observed_accounting']=summarize_run_metrics(usage_rows)
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
    version = state.get('schema_version', 1)
    minimum = state.get('min_reader_version', 1)
    if (type(version) is not int or type(minimum) is not int or version < 1
            or minimum < 1 or version > 2 or minimum > 2):
        raise ValueError('Native state requires a newer compatible reader; preserve it without mutation')
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
        state.setdefault('request_acks',{})
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
    validated_receipts=set()
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
                if ref not in validated_receipts:
                    validate_execution(Path(state['workspace']), ref, run_id=state['run_id'],
                                       revision=state['intent_version'])
                    validated_receipts.add(ref)
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
            from .snapshots import validate_snapshot
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
            from .coding_scope import native_evidence
            proof, _ = native_evidence(state, c.get('coding_scope_evidence'), checked_path)
            if proof != c.get('coding_scope_proof'):
                return False
        if state.get('large_task'):
            from .large_native import validate_admission
            validate_admission(state)
        if c.get('batch_report'):
            from .validation_batch import load_report
            batch=load_report(ws,c['batch_report'])
            if (batch['snapshot']['run_id']!=state['run_id'] or
                    batch['snapshot']['contract_revision']!=state['intent_version'] or
                    batch['snapshot']['contract_hash']!=state['contract_hash'] or
                    c.get('batch_report_digest')!=batch['report_digest']):
                return False
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
    if any(child.get('status')!='finished' for child in state.get('children',{}).values()):
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
        metadata=validate_contract_metadata(payload,criteria)
        if metadata and not _current_session_reader(state):
            raise ValueError('New contract metadata requires a current SessionStart reader; reopen the managed session first')
        if any(row['source_turn_id'] not in {p['turn_id'] for p in state['prompts']}
               for row in [*metadata.get('preserve_conditions',[]),
                           *(metadata[name] for name in ('delivery_target','runtime_context','deadline') if name in metadata)]):
            raise ValueError('Contract metadata needs a recorded user source turn')
        routed=state.get('request_route') or {}
        profile=execution_profile(payload.get('execution_profile', routed.get('suggested_profile', 'standard')))
        effective_prompt=routed.get('effective_prompt') or state['prompts'][-1]['text']
        mode=payload.get('mode',select_mode(effective_prompt))
        if mode not in MODES:
            raise ValueError('Invalid task mode')
        artifacts=payload.get('artifacts')
        if not isinstance(artifacts,list) or not 1 <= len(artifacts) <= 60:
            raise ValueError('Declare 1..60 output artifacts; for text tasks use answer.md')
        if len(set(artifacts))!=len(artifacts):
            raise ValueError('Duplicate artifact')
        for p in artifacts: checked_path(ws,p)
        scope = None
        if mode == 'coding':
            from .coding_scope import validate_baseline, scope_repositories
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
            'intent_version':state['intent_version'], 'documents':documents, 'protected_inputs':protected_hashes,
            **metadata}
        state['efficiency_policy_version']=1
        state['actions']={};state['check_decisions']=[];state['wait_observations']=[]
        state['delivery_milestones']=[];state['context_refs']={}
        state['batch_required']=bool(metadata) and mode=='coding'
        if scope:
            state['contract']['coding_scope'] = scope
        if 'plan' in payload:
            state['plan']={'content':validate_profile_plan(payload['plan'],criteria,profile['name']),
                           'intent_version':state['intent_version']}
            state['plan']['sha256']=digest(state['plan']['content'])
        state['contract_hash']=digest(state['contract'])
        state['seen_target_digests']={rel:[sha] for rel,sha in target_manifest(state).items()}
        state['recipes']=recipes(ws,mode,effective_prompt,limit=2)
        claim_writer(state)
        atomic_json(d/('contract-'+state['run_id']+'-v'+str(state['intent_version'])+'.json'),state['contract'])
        if profile['plan_depth']=='optional':
            return 'Simple run armed. Execute directly or use one bounded child; verify before independent review.'
        return 'Run armed. Read relevant memory/skills, then submit a requirement-covered plan before implementation. Do not wait for user approval of reversible decisions.'
    if op=='status': return 'Status exported to view.json.'
    if op=='record-action':
        if state['status'] not in ACTIVE:
            raise ValueError('Action needs an active run')
        if payload.get('source_turn_id') not in {p['turn_id'] for p in state['prompts']}:
            raise ValueError('Action needs an observed user turn')
        row=register_action(state,payload)
        return 'Action '+row['action_id']+' recorded; user authorization and host capability are separate.'
    if op=='authorize-action':
        turn=payload.get('source_turn_id')
        prompt=next((p['text'] for p in reversed(state['prompts']) if p['turn_id']==turn),None)
        row=authorize_action(state,payload.get('action_id'),turn_id=turn,prompt=prompt)
        return 'Scoped user authorization recorded for '+row['action_id']+'; host permission remains unknown.'
    if op=='action-decision':
        row=state.get('actions',{}).get(payload.get('action_id'))
        if not row:
            raise ValueError('Unknown action')
        decision=action_decision(state,row,state.get('host_policy_version'))
        state['last_action_decision']=decision
        return json.dumps(decision,ensure_ascii=False)
    if op=='action-outcome':
        row=state.get('actions',{}).get(payload.get('action_id'))
        if not row:
            raise ValueError('Unknown action')
        ref=payload.get('evidence_id')
        receipt=validate_execution(ws,ref,run_id=state['run_id'],revision=state['intent_version'],expect_failure=True)
        stderr=checked_path(ws,str((Path(ref).parent/'check.stderr.log').as_posix()),existing=True)
        stdout=checked_path(ws,str((Path(ref).parent/'check.stdout.jsonl').as_posix()),existing=True)
        output=(stderr.read_text(encoding='utf-8',errors='replace')[-8192:]
                + '\n' + stdout.read_text(encoding='utf-8',errors='replace')[-8192:])
        observed=classify_denial(output)
        if payload.get('outcome')!=observed:
            raise ValueError('Action outcome differs from observed failure output')
        record_action_outcome(state,row,observed,ref,None)
        return 'Action outcome recorded from failed execution receipt; identical denied retry held.'
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
        if _request_needs_current_reader(op,payload) and not _current_session_reader(state):
            raise ValueError('New contract metadata needs a current SessionStart reader before amendment')
        turn=payload['source_turn_id']
        if changed==['new_task']:
            archive_for_new_task(state,d,turn)
            return 'Prior run preserved; new task waits for existing children.' if state['status']=='handoff_pending' else 'Prior run archived; submit a new begin contract.'
        state['pending_turn_ids'].remove(turn)
        if not changed:
            return 'Status-only turn reconciled; contract, plan and candidate unchanged.'
        criteria_valid(updated['criteria'])
        if any(row['source_turn_id'] not in {p['turn_id'] for p in state['prompts']}
               for row in [*updated.get('preserve_conditions',[]),
                           *(updated[name] for name in ('delivery_target','runtime_context','deadline') if name in updated)]):
            raise ValueError('Amended metadata needs a recorded user source turn')
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
        state.pop('batch_report',None)
        state.pop('batch_report_digest',None)
        state['check_decisions']=[]
        from .large_prompts import apply_context_delta
        deltas={}
        for aid,child in state.get('children',{}).items():
            ref=child.get('context_packet')
            if child.get('status')!='running' or not ref:
                continue
            original=bounded_json(Path(ref),8*1024*1024)
            amendment=next((p['text'] for p in state['prompts'] if p['turn_id']==turn),'')
            delta={'base_revision':old_version,'revision':updated['intent_version'],
                   'changed':{'requirement_ids':[row['id'] for row in updated['criteria']],
                              'acceptance':[row['text'] for row in updated['criteria']],
                              'append_verbatim_excerpts':[amendment]}}
            revised=apply_context_delta(original,delta)
            destination=Path(ref).with_name(Path(ref).stem+'-r'+str(updated['intent_version'])+'.json')
            atomic_json(destination,revised)
            child['context_packet']=str(destination)
            deltas[aid]={'delta':delta,'recovery_packet':str(destination)}
        state['pending_context_deltas']=deltas
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
        return ('User amendment applied as contract revision '+str(state['intent_version'])+
                ('; forward pending_context_deltas from view.json to running children.' if deltas else ''))
    if state['status'] not in ACTIVE:
        raise ValueError('No active native run')
    if digest(state['contract']) != state['contract_hash']:
        raise ValueError('Authoritative contract changed unexpectedly')
    if op=='read-observation':
        from .large_prompts import read_decision
        if not isinstance(payload.get('sections'),list) or any(not isinstance(v,str) for v in payload['sections']):
            raise ValueError('Read observation needs sections')
        for key in ('question','scope','source_hash'):
            if not isinstance(payload.get(key),str) or not payload[key] or len(payload[key])>500:
                raise ValueError('Read observation needs bounded '+key)
        refs=state.setdefault('context_refs',{})
        decision=read_decision(refs,question=payload['question'],scope=payload['scope'],
                               source_hash=payload['source_hash'],sections=payload['sections'],
                               independent_review=payload.get('independent_review') is True)
        if decision['read'] and len(refs)<100:
            refs[decision['key']]={'revision':state['intent_version'],'scope':payload['scope']}
        return json.dumps(decision,ensure_ascii=False)
    if op=='wait-observation':
        fingerprint=payload.get('status_digest')
        if not isinstance(fingerprint,str) or not re.fullmatch(r'[0-9a-f]{64}',fingerprint):
            raise ValueError('Wait observation needs a status digest')
        previous=state.setdefault('wait_observations',[])
        unchanged=0
        for row in reversed(previous):
            if row['status_digest']!=fingerprint:
                break
            unchanged+=1
        decision=next_wait({'unchanged_count':unchanged,'changed':unchanged==0,
                            'alive':payload.get('alive') is True})
        state['wait_observations']=(previous+[{'status_digest':fingerprint,
                                              'seconds':decision['seconds']}])[-30:]
        return json.dumps(decision,ensure_ascii=False)
    if op == 'attach-large':
        if not state.get('plan') or state['plan']['intent_version'] != state['intent_version']:
            raise ValueError('Current deep plan is required before attaching large execution')
        from .large_native import attach
        state['large_task'] = attach(state, payload)
        state['candidate'] = None
        return 'Large graph attached. Use isolated CLI workers; App multi-writer correlation remains unavailable.'
    if op=='attach-batch':
        from .validation_batch import load_report
        ref=payload.get('report')
        if not isinstance(ref,str):
            raise ValueError('Batch attachment needs report path')
        report=load_report(ws,ref)
        batch_snapshot=report['snapshot']
        if (batch_snapshot['run_id']!=state['run_id'] or batch_snapshot['contract_revision']!=state['intent_version']
                or batch_snapshot['contract_hash']!=state['contract_hash']):
            raise ValueError('Batch belongs to another run, revision or contract')
        state['batch_report']=ref
        state['batch_report_digest']=report['report_digest']
        state['batch_plan_sha256']=(state.get('plan') or {}).get('sha256')
        state['check_decisions']=[{'id':row['id'],'decision_class':row.get('decision_class','required_fresh'),
                                   'receipt':row['receipt']} for row in report['results']][-200:]
        return 'Current passed validation batch attached: '+ref
    if op=='plan':
        content=validate_profile_plan(payload,state['contract']['criteria'],
                                      state.get('execution_profile', execution_profile())['name'])
        new_sha=digest(content)
        if state.get('needs_replan') and new_sha==state.get('plan',{}).get('sha256'):
            raise ValueError('Repeated failure needs a changed plan and hypothesis')
        if new_sha != (state.get('plan') or {}).get('sha256'):
            state['completed_steps']=[]
        state['plan']={'content':content,'intent_version':state['intent_version'],'sha256':new_sha}
        if new_sha!=state.get('batch_plan_sha256'):
            state.pop('batch_report',None)
            state.pop('batch_report_digest',None)
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
        if state.get('batch_required'):
            from .validation_batch import load_report
            ref=state.get('batch_report')
            if not ref:
                raise ValueError('New coding delivery contract needs an attached current validation batch')
            report=load_report(ws,ref)
            batch_snapshot=report['snapshot']
            if (batch_snapshot['run_id']!=state['run_id'] or batch_snapshot['contract_revision']!=state['intent_version']
                    or batch_snapshot['contract_hash']!=state['contract_hash'] or
                    report['report_digest']!=state.get('batch_report_digest') or
                    state.get('batch_plan_sha256')!=(state.get('plan') or {}).get('sha256')):
                raise ValueError('Validation batch no longer matches the current contract')
        result_valid(state,payload.get('criterion_results'),require_execution=True)
        target=state['contract'].get('delivery_target')
        if target and target['kind'] in {'release','live_deployment'}:
            delivery=payload.get('delivery_evidence')
            if not isinstance(delivery,dict) or delivery_status(target,delivery)['status']!='evidence_pending':
                raise ValueError('Requested release/live delivery needs publish and verification evidence; push alone is insufficient')
            if delivery['publish_ref']==delivery['verification_ref']:
                raise ValueError('Publish and delivery verification need separate observations')
            observed_delivery={}
            for label,ref in (('publish',delivery['publish_ref']),('verification',delivery['verification_ref'])):
                if not isinstance(ref,str) or current_receipt(state,ref) is None:
                    raise ValueError('Delivery evidence needs current successful execution receipts')
                observed_delivery[label]=current_receipt(state,ref)
            publish_files=observed_delivery['publish']['subject_files']
            verification_files=observed_delivery['verification']['subject_files']
            verified_output=checked_path(ws,str((Path(delivery['verification_ref']).parent/'check.stdout.jsonl').as_posix()),existing=True).read_text(encoding='utf-8',errors='replace')
            if target['kind']=='release':
                asset_paths=[rel for rel,sha in target_manifest(state).items() if sha==delivery['asset_sha256']]
                if (not re.fullmatch(r'[0-9a-f]{64}',delivery['asset_sha256']) or
                        not asset_paths or not set(asset_paths)&set(publish_files) or
                        not set(asset_paths)&set(verification_files) or
                        delivery['asset_sha256'] not in verified_output):
                    raise ValueError('Release publish must bind current asset bytes; verification must observe its hash')
            else:
                paths=delivery.get('source_paths')
                if (not isinstance(paths,list) or not paths or any(not isinstance(p,str) for p in paths)
                        or not set(paths)<=set(publish_files)
                        or not set(paths)<=set(verification_files)):
                    raise ValueError('Live publish and verification receipts must cover declared source paths')
                expected=delivery.get('expected_sha')
                if (not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{7,64}',expected) or
                        delivery.get('live_sha')!=expected or expected not in verified_output or
                        'healthy' not in verified_output.lower()):
                    raise ValueError('Live verification must observe matching SHA and healthy service')
            state['delivery_milestones']=(state.get('delivery_milestones',[])+[{
                'target':target['kind'],'publish_ref':delivery['publish_ref'],
                'verification_ref':delivery['verification_ref'],'intent_version':state['intent_version']}])[-10:]
        large_proof, large_files = None, []
        if state.get('large_task'):
            from .large_native import candidate_evidence
            large_proof, large_files = candidate_evidence(state, payload)
        scope_proof = None; scope_files = []
        if state['contract'].get('coding_scope'):
            from .coding_scope import native_evidence
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
            from .provenance import validate_provenance
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
        from .snapshots import build_snapshot
        snap=build_snapshot(ws,descriptors)
        execution_refs={ref:file_hash(checked_path(ws,ref,existing=True))
                        for row in payload['criterion_results']
                        for ref in row.get('evidence_ids',[])}
        if target and target['kind'] in {'release','live_deployment'}:
            for ref in (delivery['publish_ref'],delivery['verification_ref']):
                execution_refs[ref]=file_hash(checked_path(ws,ref,existing=True))
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
        if state.get('batch_report'):
            c['batch_report']=state['batch_report']
            c['batch_report_digest']=state['batch_report_digest']
        if target and target['kind'] in {'release','live_deployment'}:
            c['delivery_evidence']=delivery
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
        if p.stem in state['processed']:
            out=checked_path(ws,str((b/'outbox'/(p.stem+'.json')).relative_to(ws)))
            cached=state.get('request_acks',{}).get(p.stem)
            if not out.is_file():
                atomic_json(out,cached or {'ok':False,'status':state['status'],
                    'message':'Prior request state was committed but its result is unavailable; inspect view.json.'})
            archive=checked_path(ws,str((b/'processed'/p.name).relative_to(ws)))
            archive.parent.mkdir(parents=True,exist_ok=True)
            if not archive.exists():os.replace(p,archive)
            else:p.unlink()
            reports.append(json.dumps(read_json(out),ensure_ascii=False))
            continue
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
        acks=state.setdefault('request_acks',{})
        acks[p.stem]=ack
        if len(acks)>200:
            keep=set(state['processed'])
            state['request_acks']={key:value for key,value in acks.items() if key in keep}
        # Commit the transition and its response together before exporting the
        # acknowledgment. A missing ack can then be reconstructed on replay.
        persist(d,state)
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
    if state.get('contract'):
        from .large_prompts import plan_context_packet
        contract=state['contract'];ws=Path(state['workspace'])
        packet=plan_context_packet({'task_id':str(aid),'revision':state['intent_version'],
            'requirement_ids':[row['id'] for row in contract['criteria']],
            'verbatim_excerpts':[row['text'] for row in state['prompts']],
            'owned_paths':contract['artifacts'],
            'dependencies':[], 'acceptance':[row['text'] for row in contract['criteria']],
            'evidence_refs':[row['id'] for row in state.get('checkpoint_evidence',[])]},
            {'contract_hash':state['contract_hash'],
             'contract_ref':str(folder(state['key'])/('contract-'+state['run_id']+'-v'+str(state['intent_version'])+'.json'))})
        packets=bridge(ws,state['key'])/'packets';packets.mkdir(parents=True,exist_ok=True)
        packet_path=checked_path(ws,str((packets/(digest(str(aid))+'.json')).relative_to(ws)))
        atomic_json(packet_path,packet)
        state['children'][str(aid)]['context_packet']=str(packet_path)
        instruction+=' Read exact task packet '+str(packet_path)+'; original excerpts and source pointers are preserved there.'
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
    call={'agent_id':str(ev.get('agent_id')),'role':child['role'],'selected_model':child.get('selected_model'),
          'selected_effort':child.get('selected_effort'),
          'selection_reason':child.get('selection_reason'),
           'observed_model':child.get('model'),'observed_effort':child.get('observed_effort'),
           'configured_effort':child.get('configured_effort'),'effort_source':child.get('effort_source'),
          'observed_source':'SubagentStart' if child.get('model') else None,
          'usage_observed':measured is not None,'usage':measured,
          'retry':sum(c['role']==child['role'] for c in state.get('model_calls',[])),
          'outcome':'completion_unverified','finished_at':time.time()}
    state['model_calls']=(state.get('model_calls',[])+[call])[-100:]
    if state['status']=='handoff_pending' and all(c.get('status')=='finished' for c in state['children'].values()):
        archive_for_new_task(state,d,state['handoff_turn_id'])
        return message('SubagentStop','Previous run archived; a new begin contract may be submitted.')
    if state['status']=='interrupt_pending' and all(c.get('status')=='finished' for c in state['children'].values()):
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


def guidance_signature(state: dict) -> str:
    """Output bookkeeping only; prompt collection and event handling always run."""
    routed=state.get('request_route') or {}
    default_profile=state.get('execution_profile', {}).get('name', 'standard')
    return digest([state['status'], state.get('execution_profile'), state.get('contract_hash'),
                   state.get('pending_turn_ids'), state.get('feedback'), state.get('goal_request'),
                   state.get('plan', {}).get('sha256') if state.get('plan') else None,
                   state.get('candidate', {}).get('digest') if state.get('candidate') else None,
                   state.get('needs_replan'), state.get('host_goal_scan_pending'),
                   routed.get('path', 'inspect'),
                   routed.get('suggested_profile', default_profile),
                   routed.get('reason','').startswith('Brief approval')])


def context(state: dict, *, bootstrap: bool = True, restore: bool = False) -> str:
    b=bridge(Path(state['workspace']),state['key'])
    routed=state.get('request_route') or {}
    direct_turn=state['status']=='complete' and routed.get('path')=='direct' and not state.get('pending_turn_ids')
    profile=execution_profile(routed.get('suggested_profile', state.get('execution_profile', {}).get('name', 'standard'))
                              if state['status']=='idle' or direct_turn
                              else state.get('execution_profile', {}).get('name', 'standard'))
    planning={'optional':'Plan is optional; execute directly or use limited delegation.',
              'brief':'Submit a brief requirement-covered plan before implementation.',
              'deep':'Submit a deep plan covering every requirement before implementation.'}
    if state.get('pending_turn_ids'):
        next_action='Classify '+str(len(state['pending_turn_ids']))+' pending user turn(s) via native sync-intent before review.'
    elif state.get('plan_required') and (not state.get('plan') or state['plan']['intent_version']!=state['intent_version']):
        next_action='Submit native plan for the current requirements.'
    elif state['status']=='reviewing':
        next_action='Independent adhd-verifier must review the current candidate digest.'
    elif state['status'] in ACTIVE:
        next_action='Implement/check the current requirements, then submit candidate and independent review.'
    elif direct_turn:
        next_action='Answer the new direct request with appropriate checking. The completed contract stays accepted.'
    elif state['status']=='complete':
        next_action='Report verified deliverables and limits.'
    elif state['status']=='idle':
        path=routed.get('path','inspect')
        if path=='direct':
            next_action='Answer directly with appropriate checking. No native begin or durable acceptance loop is needed; escalate with begin if scope grows.'
        elif path=='inspect':
            next_action='Inspect the request and relevant context briefly; if substantive, use native begin with an appropriate profile.'
        else:
            next_action='Use native begin with suggested '+profile['name']+' profile; explicit profile choices take precedence.'
    else:
        next_action='Resolve the recorded blocker; resume only with real user/host authorization.'
    required=['ADHD Status='+state['status']+'; Profile='+profile['name']+'; plan='+profile['plan_depth']+'.',
              'SESSION='+state['key']+'; view='+str(b/'view.json'),
              'Next: '+next_action]
    lines=(['Direct reply: no native plan or acceptance loop.']
           if (state['status']=='idle' or direct_turn) and routed.get('path')=='direct'
           else [planning[profile['plan_depth']]])
    if (state['status']=='idle' or direct_turn) and routed:
        lines.insert(0,'Initial route='+routed['path']+'; '+routed['reason'])
        if routed.get('effective_prompt') and routed['effective_prompt']!=state['prompts'][-1]['text']:
            lines.append('Brief approval refers to: '+truncate_content(routed['effective_prompt'],100))
    if state.get('goal_request'):
        lines.append('Goal execution requested: '+truncate_content(state['goal_request']['outcome'],90)+
                     '. Continue until independent acceptance or explicit stop/limit/blocker.')
    if restore:
        contract=state.get('contract', {})
        if state['status']=='idle' or direct_turn:
            objective=routed.get('effective_prompt') or (state['prompts'][-1]['text'] if state.get('prompts') else None)
            if (state['status']=='idle' and state.get('prompts') and
                    is_brief_approval(state['prompts'][-1]['text']) and
                    objective==state['prompts'][-1]['text']):
                objective=None
        else:
            objective=(state.get('plan') or {}).get('content', {}).get('objective')
            if not objective and state.get('prompts'):
                objective=state['prompts'][0]['text']
        if objective:
            lines.append('Objective: '+truncate_content(objective,140))
        unmet=[row['id'] for row in contract.get('criteria', [])] if state['status']!='complete' else []
        if unmet:
            lines.append('Unmet acceptance: '+truncate_content(', '.join(unmet),120)+'. Full contract: view.json.')
        if state.get('mode')=='study':
            lines.append('Study: explain and give a short self-check/answer key; full solution when explicitly requested.')
        elif state.get('mode')=='research':
            lines.append('Research: separate measured/calculated/interpreted/unverified claims and bind provenance.')
    if state.get('feedback'):
        lines.append('Feedback: '+truncate_content(state['feedback'],160))
    deadline=(state.get('contract') or {}).get('deadline')
    if deadline:
        risk=deadline_guidance(deadline,datetime.now(timezone.utc).isoformat())
        if risk['level']!='normal':
            lines.append('Explicit deadline '+risk['level']+'; '+str(risk['remaining_seconds'])+
                         ' seconds remain. Preserve every mandatory requirement and report delivery evidence.')
    if state.get('goal_request'):
        lines.append('Read '+str(ROOT/'skills/adhd-goal/SKILL.md')+'.')
    if bootstrap:
        lines.append('ADHD v'+__version__+'. Read '+str(ROOT/'skills/adhd-native/SKILL.md')+'.')
        if state.get('contract'):
            lines.append('Preserve model/effort and original intent; verify independently. No competing owner. Tokens unknown.')
        else:
            lines.append('Small questions: answer directly. For substantive work use '+str(ROOT/'adhd.py')+
                         ' native begin. Read inputs/skills/memory. Preserve parent model/effort and original intent; '
                         'verify independently. If another owner is active, do not begin ADHD. Native tokens are unknown.')
    # Our guidance budget is in characters; the host's additionalContextLimit
    # is an approximate token threshold. Never cut mandatory recovery fields,
    # even when an unusually long view path alone exceeds the character budget.
    text='\n'.join(required)
    remaining=1200-len(text)-1
    if remaining>0:
        explanation='\n'.join(lines)
        if len(explanation)>remaining:
            explanation=explanation[:max(0,remaining-3)]+'.'*min(3,remaining)
        text+='\n'+explanation
    return text


def handle_event(ev: dict, *, diagnostics=None) -> dict:
    if os.environ.get('ADHD_EXEC_OWNER') in {'legacy','native-check'}: return {}
    if not isinstance(ev,dict): raise ValueError('Hook input must be an object')
    name=ev.get('hook_event_name','')
    normalized=normalize_event(ev)
    if not normalized['supported']: return {}
    key=session_key(ev.get('session_id','')); ws=Path(ev.get('cwd','')).expanduser().resolve()
    if not ws.is_dir() or not ev.get('cwd'): raise ValueError('Missing workspace')
    diagnostics = diagnostics or HookDiagnostics(enabled=False)
    diagnostics.key = key
    diagnostics.event_name = name
    with locked(key, diagnostics) as d:
        with diagnostics.phase('state_read'):
            state=read_json(d/'state.json') or initial(key,ws)
        if state['workspace']!=str(ws): raise ValueError('Session workspace changed; start a new session')
        with diagnostics.phase('state_prepare'):
            migrate_state(state,d)
            observe_event(state['host_capabilities'],normalized)
        with diagnostics.phase('goal_check'):
            if name in {'SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop'}:
                observe_thread_goal(state, ev, d)
        dispatch_started = time.perf_counter_ns() if diagnostics.enabled else None
        out={}
        if name=='UserPromptSubmit':
            restore=False
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
                    restore=True
                else:
                    state['prompts'].append({'text':prompt,'turn_id':turn,'time':time.time()})
                    outcome = goal_command(prompt)
                    if outcome is not None:
                        state['goal_request']={'source_turn_id':turn, 'outcome':outcome}
                    if state.get('contract') and state['status']!='complete':
                        state['pending_turn_ids'].append(turn)
                        state['feedback']='Classify the latest user turn as no_change, amend or new_task before final review.'
                        state['request_route']=classify_request(prompt,active_contract=True)
                    elif state.get('contract') and state['status']=='complete':
                        # A standalone progress question refers to the completed
                        # run until sync-intent says otherwise. Other direct
                        # questions can be answered without reopening it.
                        state['request_route']=classify_request(prompt, active_contract=is_status_followup(prompt))
                        if state['request_route']['path']!='direct':
                            state['pending_turn_ids'].append(turn)
                            state['feedback']='Classify the latest user turn as new_task or no_change before beginning another run.'
                    else:
                        previous=next((p['text'] for p in reversed(state['prompts'][:-1])
                                       if is_substantive_request_source(p['text'])),None)
                        state['request_route']=classify_request(prompt,previous=previous)
                        state['intent_version']+=1
                append_event(d,'user_prompt',{'turn_id':turn,'intent_version':state['intent_version'],'status':state['status']})
            signature=guidance_signature(state)
            if not state.get('guidance_signature'):
                out=message(name,context(state,restore=bool(state.get('contract') or state.get('goal_request'))))
            elif restore or signature!=state['guidance_signature']:
                out=message(name,context(state,bootstrap=False,restore=True))
            state['guidance_signature']=signature
        elif name=='SessionStart':
            # Codex loads custom agent roles with the session. Disk changes during
            # a hot upgrade cannot attest what an already-running session loaded.
            state['session_verifier_profile']={'release_root':str(ROOT.resolve()),
                'profile_hash':verifier_profile_hash()}
            state['session_reader']={'release_root':str(ROOT.resolve()),
                'source_sha256':file_hash(Path(__file__).resolve()),
                'max_state_version':2,'observed_at':time.time()}
            out=message(name,context(state,restore=True))
            state['guidance_signature']=guidance_signature(state)
        elif name=='PostCompact':
            out=message(name,context(state))
        elif name=='PostToolUse':
            observe_action_host_result(state,ev)
            observe_host_tool(state,ev)
            with diagnostics.phase('inbox'):
                reports=process_inbox(state,d)
            if reports: out=message(name,'\n'.join(reports)+'\nRead bridge/view.json for authoritative exported status.')
        elif name=='PreToolUse':
            matched=matched_host_action(state,ev)
            if matched:
                if isinstance(ev.get('policy_version'),str) and ev['policy_version']:
                    state['host_policy_version']=ev['policy_version']
                decision=action_decision(state,matched,state.get('host_policy_version'))
                if not decision['retry_allowed']:
                    out=deny('ADHD scoped action held: '+decision['reason']+'; evidence '+
                             ', '.join(decision['evidence_refs']))
            if not out:
                out=spawn_guard(state,ev)
        elif name=='SubagentStart': out=start_child(state,ev)
        elif name=='SubagentStop':
            child=state.get('children',{}).get(str(ev.get('agent_id','')))
            was_running=child and child.get('status')=='running'
            out=stop_child(state,ev,d)
            if diagnostics.enabled and was_running and state.get('model_calls'):
                diagnostics.host_usage=state['model_calls'][-1].get('usage')
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
            last_message=ev.get('last_assistant_message')
            if isinstance(last_message,str) and last_message:
                state['last_assistant_message']={'text':last_message[-4000:],
                    'sha256':digest(last_message),'observed_at':time.time()}
            with diagnostics.phase('inbox'):
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
                    deadline=(state.get('contract') or {}).get('deadline')
                    if deadline:
                        risk=deadline_guidance(deadline,datetime.now(timezone.utc).isoformat(),
                                               last_level=state.get('deadline_alert_level'))
                        if risk['changed']:
                            state['deadline_alert_level']=risk['level']
                            if risk['level']!='normal':
                                state['feedback']=('Explicit deadline '+risk['level']+': '+str(risk['remaining_seconds'])+
                                    ' seconds left. Check remaining mandatory work, blockers and delivery evidence.')
                    if (time.time()-state.get('progress_last_gained_at',state['started'])>=600 and
                            not state.get('stagnation_guidance_issued') and
                            not any(child.get('status')!='finished' for child in state.get('children',{}).values())):
                        state['stagnation_guidance_issued']=True
                        state['feedback']='Progress check: no fresh requirement/test/artifact progress for 10 minutes. Identify delay, remaining mandatory work, current execution value, and one changed next action.'
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
        if dispatch_started is not None:
            diagnostics.phases_ns['event_dispatch'] = time.perf_counter_ns() - dispatch_started
        with diagnostics.phase('persist'):
            persist(d,state)
        return out


def submit_request(key: str, workspace: Path, op: str, payload: dict,
                   request_id: str | None = None) -> dict:
    """Sandbox-side transport only. A trusted host PostToolUse/Stop hook applies it."""
    workspace=workspace.expanduser().resolve(); b=bridge(workspace,key)
    view_path=b/'view.json'
    view=bounded_json(view_path, 8*1024*1024) if view_path.is_file() else {}
    if not isinstance(view,dict):
        raise ValueError('Native bridge view is invalid')
    contract=view.get('contract') or {}
    if not isinstance(contract,dict):
        raise ValueError('Native contract view is invalid')
    required_reader = (_request_needs_current_reader(op,payload)
                       or (op != 'status' and bool(_NEW_CONTRACT_FIELDS & contract.keys())))
    if required_reader:
        from .native_install import _find_managed_installation
        managed=_find_managed_installation(home())
        if (not _current_session_reader(view)
                or not managed or Path(managed[2].get('release','')).resolve()!=ROOT.resolve()):
            raise ValueError('Required contract semantics need the current managed hook and SessionStart reader; request was not queued')
    rid=request_id or uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}',rid):
        raise ValueError('Request ID must be 32 lowercase hexadecimal characters')
    value={'id':rid,'session':key,'op':op,'payload':payload}
    if len(json.dumps(value,ensure_ascii=False).encode())>MAX_MESSAGE: raise ValueError('Request too large')
    p=checked_path(workspace,str((b/'inbox'/(rid+'.json')).relative_to(workspace)))
    processed=checked_path(workspace,str((b/'processed'/(rid+'.json')).relative_to(workspace)))
    prior=p if p.exists() else processed if processed.exists() else None
    if prior:
        if bounded_json(prior)!=value:
            raise ValueError('Request ID was used for different content')
        return {'queued':rid,'receipt':str(b/'outbox'/(rid+'.json')),
                'note':'Existing request reused; inspect ack or wait for host processing.'}
    atomic_json(p,value)
    return {'queued':rid,'receipt':str(b/'outbox'/(rid+'.json')),
            'note':'The native Codex hook must process this request; queued is not success.'}
