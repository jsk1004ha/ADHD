"""Requirement-bound preservation, explicit deadlines and delivery evidence."""
from __future__ import annotations

from datetime import datetime
import re


def validate_contract_metadata(value: dict, criteria: list[dict]) -> dict:
    """Optional v2 contract fields; absence means unknown, never satisfied."""
    ids = {row['id'] for row in criteria}
    output = {}
    preserve = value.get('preserve_conditions', [])
    if not isinstance(preserve, list) or len(preserve) > 50:
        raise ValueError('preserve_conditions needs at most 50 rows')
    seen = set()
    for row in preserve:
        if not isinstance(row, dict) or set(row) - {'id', 'text', 'requirement_id', 'source_turn_id', 'basis', 'evidence_ref'}:
            raise ValueError('Invalid preservation row')
        if (not isinstance(row.get('id'), str) or not re.fullmatch(r'P[1-9][0-9]{0,2}', row['id'])
                or row['id'] in seen or not isinstance(row.get('text'), str)
                or not 0 < len(row['text'].strip()) <= 4000):
            raise ValueError('Preservation needs distinct ID and text')
        seen.add(row['id'])
        _source(row, ids)
    if preserve:
        output['preserve_conditions'] = preserve
    for name in ('delivery_target', 'runtime_context', 'deadline'):
        row = value.get(name)
        if row is None:
            continue
        if not isinstance(row, dict):
            raise ValueError(name + ' must be an object')
        _source(row, ids)
        if name == 'delivery_target':
            if set(row) - {'kind', 'requirement_id', 'source_turn_id', 'basis', 'evidence_ref'} or row.get('kind') not in {'local_artifact', 'release', 'live_deployment'}:
                raise ValueError('Invalid delivery_target')
        elif name == 'runtime_context':
            if set(row) - {'os', 'entrypoint', 'branch', 'environment_evidence', 'requirement_id', 'source_turn_id', 'basis', 'evidence_ref'}:
                raise ValueError('Invalid runtime_context')
            if not any(row.get(key) for key in ('os', 'entrypoint', 'branch', 'environment_evidence')):
                raise ValueError('Runtime context needs an observed or user-specified detail')
            for key in ('os', 'entrypoint', 'branch', 'environment_evidence'):
                if key in row and (not isinstance(row[key], str) or not 0 < len(row[key]) <= 500):
                    raise ValueError('Invalid runtime context ' + key)
        else:
            if set(row) - {'due_at', 'timezone', 'observed_at', 'requirement_id', 'source_turn_id', 'basis', 'evidence_ref'}:
                raise ValueError('Invalid deadline')
            if not isinstance(row.get('timezone'), str) or not row['timezone']:
                raise ValueError('Deadline needs a timezone')
            due = _instant(row.get('due_at'))
            start = _instant(row.get('observed_at'))
            if due <= start:
                raise ValueError('Deadline must follow its first observed instruction')
        output[name] = row
    return output


def _source(row: dict, ids: set[str]) -> None:
    if (row.get('requirement_id') not in ids or row.get('basis') not in {'user', 'observed', 'assumed'}
            or not isinstance(row.get('source_turn_id'), str) or not row['source_turn_id']):
        raise ValueError('Contract metadata needs a known requirement and source turn')
    ref = row.get('evidence_ref')
    if ref is not None and (not isinstance(ref, str) or len(ref) > 1000):
        raise ValueError('Invalid metadata evidence reference')


def _instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError('Deadline needs ISO timestamps')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError('Deadline needs ISO timestamps') from error
    if parsed.tzinfo is None:
        raise ValueError('Deadline timestamp needs an offset')
    return parsed


def merge_deadline(current: dict | None, incoming: dict, *, explicit_change: bool = False) -> dict:
    if current is None or explicit_change:
        return incoming
    if current['due_at'] == incoming['due_at']:
        return current
    raise ValueError('Changing a deadline needs an explicit replacement')


def deadline_guidance(deadline: dict, now: str, *, last_level: str | None = None) -> dict:
    due = _instant(deadline['due_at'])
    start = _instant(deadline['observed_at'])
    fraction = ( _instant(now) - start ).total_seconds() / (due - start).total_seconds()
    level = 'overdue' if fraction >= 1 else 'high' if fraction >= .8 else 'watch' if fraction >= .5 else 'normal'
    return {'level': level, 'changed': level != last_level,
            'remaining_seconds': max(0, int((due - _instant(now)).total_seconds())),
            'reason': 'explicit_user_deadline', 'source_turn_id': deadline['source_turn_id']}


def delivery_status(target: dict | None, evidence: dict | None) -> dict:
    kind = (target or {}).get('kind', 'local_artifact')
    evidence = evidence or {}
    if kind == 'local_artifact':
        return {'status': 'local_candidate', 'reason': 'Artifact acceptance remains at the normal candidate gate'}
    if kind == 'release':
        complete = all(evidence.get(key) for key in ('publish_ref', 'verification_ref', 'asset_sha256'))
    elif kind == 'live_deployment':
        complete = all(evidence.get(key) for key in ('publish_ref', 'verification_ref', 'expected_sha', 'live_sha', 'health', 'source_paths'))
        complete = complete and evidence.get('health') == 'healthy'
    else:
        raise ValueError('Unknown delivery target')
    return {'status': 'evidence_pending' if complete else 'incomplete',
            'reason': 'Publish and independent release/live observations require validation' if complete
                      else 'Push or CI alone does not prove requested delivery'}
