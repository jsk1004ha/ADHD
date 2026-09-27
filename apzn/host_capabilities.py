"""Describe only host capabilities observed in actual hook events."""
from __future__ import annotations

from typing import Any

EVENTS = frozenset({'SessionStart', 'UserPromptSubmit', 'PostToolUse',
                    'PreToolUse', 'Stop', 'SubagentStart', 'SubagentStop',
                    'PostCompact', 'SessionEnd', 'Interrupt'})


def normalize_event(raw: dict[str, Any], *, host_profile: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError('Hook payload must be an object')
    name = raw.get('hook_event_name', '')
    if name not in EVENTS:
        return {'name': name, 'supported': False}
    result = {'name': name, 'supported': True, 'session_id': raw.get('session_id'),
              'cwd': raw.get('cwd'), 'turn_id': raw.get('turn_id'),
              'tool_use_id': raw.get('tool_use_id'), 'agent_id': raw.get('agent_id'),
              'agent_type': raw.get('agent_type'), 'model': raw.get('model')}
    if host_profile is not None:
        result['profile'] = host_profile.get('host', 'unidentified')
    return result


def correlate_spawn(reservation: dict, start_event: dict, stop_event: dict) -> bool:
    """Never infer correlation from a role name or nearby timestamp alone."""
    return bool(reservation.get('tool_use_id') and
                reservation['tool_use_id'] == start_event.get('tool_use_id') and
                start_event.get('agent_id') and
                start_event['agent_id'] == stop_event.get('agent_id') and
                reservation.get('role') == start_event.get('agent_type'))


def completion_capability(profile: dict) -> str:
    if profile.get('verifier_correlated') is True:
        return 'supported'
    if profile.get('observed_events'):
        return 'partial'
    return 'unavailable'


def observe_event(profile: dict, event: dict) -> None:
    """Record event names, not prompt text or auth payloads."""
    if event.get('supported'):
        profile['observed_events'] = sorted(set(profile.get('observed_events', [])) | {event['name']})
