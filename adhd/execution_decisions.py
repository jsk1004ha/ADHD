"""Bounded action, denial and wait decisions for the existing native controller."""
from __future__ import annotations

import re
from .core import digest


DENIALS = {'host_policy_denied', 'os_permission_denied', 'credential_missing',
           'harness_capability_missing', 'unknown_denial'}


def action_key(action: dict) -> str:
    operation, targets = action.get('operation'), action.get('canonical_targets')
    if (not isinstance(operation, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{1,63}', operation)
            or not isinstance(targets, list) or not targets or len(targets) > 30
            or any(not isinstance(p, str) or not p or len(p) > 500 for p in targets)
            or not isinstance(action.get('scope_digest'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', action['scope_digest'])):
        raise ValueError('Action needs operation, canonical targets and scope digest')
    return digest([operation, sorted(targets), action['scope_digest']])


def register_action(state: dict, action: dict) -> dict:
    if (not isinstance(action.get('source_turn_id'), str) or not action['source_turn_id']
            or not isinstance(action.get('constraints', []), list)
            or not isinstance(action.get('authorization_excerpt'), str)
            or not 4 <= len(action['authorization_excerpt']) <= 1000):
        raise ValueError('Action needs source turn and constraints')
    key = action_key(action)
    tool_name = action.get('tool_name')
    request_sha = action.get('request_sha256')
    if tool_name is not None or request_sha is not None:
        if (not isinstance(tool_name, str) or not tool_name or
                not isinstance(request_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', request_sha)):
            raise ValueError('Host action correlation needs tool name and exact request hash')
    proposal_excerpt = action.get('proposal_excerpt')
    proposal = state.get('last_assistant_message') or {}
    proposal_ref = None
    if proposal_excerpt is not None:
        if (not isinstance(proposal_excerpt, str) or not 4 <= len(proposal_excerpt) <= 1000
                or proposal_excerpt not in proposal.get('text', '')):
            raise ValueError('Action proposal needs an observed assistant excerpt')
        proposal_ref = proposal.get('sha256')
    actions = state.setdefault('actions', {})
    existing = actions.get(key)
    if existing:
        return existing
    if len(actions) >= 100:
        raise ValueError('Action ledger is full; archive this run before adding more')
    row = {k: action[k] for k in ('operation', 'canonical_targets', 'scope_digest', 'source_turn_id')}
    row.update(action_id=key, constraints=action.get('constraints', []),
               authorization_excerpt=action['authorization_excerpt'],
               tool_name=tool_name, request_sha256=request_sha,
               proposal_ref=proposal_ref,
               authorized=False, status='needs_user_authorization', revoked_by=None,
               attempts=[])
    actions[key] = row
    return row


def authorize_action(state: dict, key: str, *, turn_id: str, prompt: str) -> dict:
    from .request_routing import is_brief_approval
    action = state.get('actions', {}).get(key)
    if not action or action.get('revoked_by'):
        raise ValueError('Unknown or revoked action')
    prompts = state.get('prompts', [])
    if not prompts or prompts[-1].get('turn_id') != turn_id or prompts[-1].get('text') != prompt:
        raise ValueError('Authorization needs the latest observed user turn')
    if is_brief_approval(prompt):
        # This is scoped to the sole pending action. The host still owns permission.
        pending = [row for row in state['actions'].values() if not row.get('authorized') and not row.get('revoked_by')]
        observed = state.get('last_assistant_message') or {}
        if (len(pending) != 1 or pending[0]['action_id'] != key or
                not action.get('proposal_ref') or action['proposal_ref'] != observed.get('sha256') or
                observed.get('observed_at', float('inf')) > prompts[-1].get('time', 0)):
            raise ValueError('Brief approval is ambiguous without one specific pending action')
    elif (turn_id != action['source_turn_id'] or
          action['authorization_excerpt'] not in prompt):
        raise ValueError('Action needs its exact observed user instruction excerpt')
    action['authorized'] = True
    action['authorized_by'] = turn_id
    action['status'] = 'capability_unknown'
    return action


def classify_denial(response: object) -> str:
    if isinstance(response, dict):
        code = str(response.get('code', '')).lower()
        if response.get('host_policy_denied') is True or code in {'host_policy_denied', 'policy_denied'}:
            return 'host_policy_denied'
        response = response.get('message', response.get('error', response))
    text = str(response).lower()
    if any(word in text for word in ('credential missing', 'authentication required', 'login required')):
        return 'credential_missing'
    if any(word in text for word in ('permission denied', 'access denied', 'eacces', 'eperm')):
        return 'os_permission_denied'
    if any(word in text for word in ('unsupported capability', 'unknown agent_type', 'tool unavailable')):
        return 'harness_capability_missing'
    return 'unknown_denial'


def record_action_outcome(state: dict, action: dict, outcome: str,
                          evidence_ref: str, policy_version: str | None) -> dict:
    if outcome not in DENIALS | {'succeeded', 'failed'}:
        raise ValueError('Invalid action outcome')
    key = action_key(action)
    row = state.get('actions', {}).get(key)
    if not row or not isinstance(evidence_ref, str) or not evidence_ref:
        raise ValueError('Action outcome needs a registered action and evidence')
    observation = {'outcome': outcome, 'evidence_ref': evidence_ref,
                   'policy_version': policy_version if isinstance(policy_version, str) and policy_version else None}
    existing = next((item for item in row.setdefault('attempts', [])
                     if item['evidence_ref'] == evidence_ref), None)
    if existing:
        if any(existing[name] != observation[name] for name in observation):
            raise ValueError('Conflicting action observation for host/tool ID')
    else:
        row['attempts'] = (row['attempts'] + [observation])[-10:]
    row['status'] = 'blocked' if outcome in DENIALS else outcome
    return row


def action_decision(state: dict, action: dict, policy_version: str | None) -> dict:
    row = state.get('actions', {}).get(action_key(action))
    if not row or not row.get('authorized') or row.get('revoked_by'):
        return {'retry_allowed': False, 'reason': 'needs_scoped_user_authorization', 'evidence_refs': [],
                'limits_of_observation': 'Conversation authorization cannot grant host permission'}
    for prior in reversed(row.get('attempts', [])):
        if prior['outcome'] in DENIALS and (not policy_version or not prior['policy_version']
                                           or prior['policy_version'] == policy_version):
            return {'retry_allowed': False, 'reason': 'same_denial_without_policy_change',
                    'evidence_refs': [prior['evidence_ref']],
                    'limits_of_observation': 'Only a new observed host capability or policy version permits reevaluation'}
    return {'retry_allowed': True, 'reason': 'no_identical_observed_denial',
            'evidence_refs': [], 'limits_of_observation': 'Preflight does not guarantee execution permission'}


def matched_host_action(state: dict, event: dict) -> dict | None:
    name, request = event.get('tool_name'), event.get('tool_input')
    if not isinstance(name, str) or not isinstance(request, dict):
        return None
    request_sha = digest(request)
    rows = [row for row in state.get('actions', {}).values()
            if row.get('tool_name') == name and row.get('request_sha256') == request_sha]
    return rows[0] if len(rows) == 1 else None


def observe_action_host_result(state: dict, event: dict) -> dict | None:
    """Correlate a real host result; absent PostToolUse stays unobserved."""
    row = matched_host_action(state, event)
    tool_id = event.get('tool_use_id')
    response = event.get('tool_response', event.get('tool_output'))
    if not row or not row.get('authorized') or not isinstance(tool_id, str) or not tool_id or response is None:
        return None
    if isinstance(response, dict):
        is_error = response.get('isError') is True or response.get('status') in {'denied', 'blocked', 'error'}
        is_error = is_error or response.get('permissionDecision') == 'deny'
    else:
        text = str(response).lower()
        is_error = any(term in text for term in ('blocked by policy', 'permission denied',
                                                 'access denied', 'authentication required'))
    outcome = classify_denial(response) if is_error else 'succeeded'
    policy = event.get('policy_version')
    if not isinstance(policy, str) or not policy:
        policy = None
    evidence_ref = 'host:' + tool_id
    observed = record_action_outcome(state, row, outcome, evidence_ref, policy)
    attempt = next(item for item in observed['attempts'] if item['evidence_ref'] == evidence_ref)
    attempt.update({'tool_name': event['tool_name'],
        'request_sha256': row['request_sha256'], 'response_sha256': digest(response),
        'assurance': 'host_PostToolUse_exact_request'})
    if policy:
        state['host_policy_version'] = policy
    return attempt


def next_wait(observation: dict) -> dict:
    if observation.get('changed') or observation.get('user_input'):
        step = 0
    else:
        step = max(0, int(observation.get('unchanged_count', 0)))
    return {'seconds': (5, 15, 30, 60)[min(step, 3)], 'failed': False,
            'reason': 'event_change' if step == 0 else 'unchanged_poll_backoff',
            'evidence_refs': observation.get('evidence_refs', [])[:5],
            'limits_of_observation': 'Alive work is not a failure or a wall-time deadline'}
