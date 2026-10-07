"""Describe only host capabilities observed in actual hook events."""
from __future__ import annotations

from typing import Any
import json
from pathlib import Path

from .core import digest, home

GOAL_TRANSCRIPT_TAIL = 2 * 1024 * 1024


def observed_thread_goal(event: dict, cursor: dict | None = None) -> dict | None:
    """Read bounded increments of this root session's host-supplied transcript."""
    session = event.get('session_id')
    transcript = event.get('transcript_path')
    if (event.get('agent_type') or not isinstance(session, str) or not session
            or not isinstance(transcript, str) or not transcript):
        return None
    try:
        path = Path(transcript).resolve()
        if not path.is_relative_to((home() / 'sessions').resolve()):
            return None
        with path.open('rb') as stream:
            header = json.loads(stream.readline(GOAL_TRANSCRIPT_TAIL))
            meta = header.get('payload', {})
            if (header.get('type') != 'session_meta' or meta.get('id') != session
                    or Path(meta.get('cwd', '')).resolve() != Path(event.get('cwd', '')).resolve()):
                return None
            header_end = stream.tell()
            stat = path.stat()
            source = digest([str(path).casefold(), stat.st_ino])
            cursor = cursor or {}
            offset = cursor.get('offset', header_end)
            if (cursor.get('source') != source or type(offset) is not int
                    or not header_end <= offset <= stat.st_size):
                offset = header_end
                cursor = {}
            stream.seek(offset)
            tail = stream.read(GOAL_TRANSCRIPT_TAIL)
        start = 0
        skipping = cursor.get('skip_line') is True
        if skipping:
            newline = tail.find(b'\n')
            start = len(tail) if newline < 0 else newline + 1
            skipping = newline < 0
        end = tail.rfind(b'\n') + 1
        if end < start:
            end = start
        if end == start and len(tail) == GOAL_TRANSCRIPT_TAIL:
            # An oversized non-goal row may span reads. Never parse its suffix.
            end = len(tail)
            skipping = True
        latest = None
        for line in tail[start:end].splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
                continue
            payload = row['payload']
            if (row.get('type') != 'event_msg' or payload.get('type') != 'thread_goal_updated'
                    or payload.get('threadId') != session or 'goal' not in payload):
                continue
            goal = payload.get('goal')
            if goal is not None and (not isinstance(goal, dict)
                    or goal.get('threadId') != session
                    or not isinstance(goal.get('objective'), str)
                    or not goal['objective'].strip() or len(goal['objective']) > 160000
                    or not isinstance(goal.get('status'), str) or not 0 < len(goal['status']) <= 64):
                continue
            if goal is not None:
                created = goal.get('createdAt')
                if created is not None and (type(created) is not int or created < 0):
                    continue
                goal = {key: goal[key] for key in ('threadId', 'objective', 'status')}
                if created is not None:
                    goal['createdAt'] = created
            latest = {'goal': goal, 'event_sha256': digest(goal)}
        next_cursor = {'source': source, 'offset': offset + end, 'skip_line': skipping}
        return {'observed': latest, 'cursor': next_cursor,
                'pending': next_cursor['offset'] < stat.st_size}
    except (OSError, ValueError, TypeError, AttributeError):
        return None

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
