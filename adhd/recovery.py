"""Bounded, typed failure records for an unfinished native task."""
from __future__ import annotations

import time
import re
from .core import digest


ACTIONS = {
    'syntax_error': 'Repair the failing syntax and rerun the focused check.',
    'test_failure': 'Inspect the failed assertion and its input; after a repeat, revisit the assumption and design.',
    'source_mismatch': 'Recheck the original source and calculation; leave unsupported claims unverified.',
    'auth_required': 'Record the missing account or credential approval; stop repeated calls.',
    'permission_denied': 'Record the denied permission and wait for authorized access.',
    'dependency_missing': 'Inspect the exact missing dependency and an approved local alternative.',
    'tool_unavailable': 'Record the unavailable tool or model and use an authorized capability if equivalent.',
    'timeout': 'Reduce scope or input size once, then preserve the unresolved state.',
    'visual_failure': 'Repair the affected layout and rerender the relevant pages.',
    'review_rejected': 'Repair the reviewer finding, rerun affected evidence and request a fresh review.',
    'budget_exhausted': 'Preserve verified work and resume only on a real user continuation.',
    'unknown': 'Inspect the concrete failure output before another attempt.',
}


def classify_failure(message: str) -> str:
    text = str(message).lower()
    for category, words in (
        ('auth_required', ('authentication', 'auth required', 'credential', 'login', '계정 승인')),
        ('permission_denied', ('permission denied', 'access denied', '권한 거부')),
        ('dependency_missing', ('modulenotfounderror', 'no module named', 'dependency missing')),
        ('tool_unavailable', ('unknown agent_type', 'model unavailable', 'tool unavailable', 'not supported')),
        ('timeout', ('timed out', 'timeout', '시간 초과')),
        ('syntax_error', ('syntaxerror', 'indentationerror', 'parse error')),
        ('source_mismatch', ('source mismatch', 'provenance', 'hash changed', '원자료')),
        ('visual_failure', ('clipping', 'overlap', 'visual review', 'layout')),
        ('test_failure', ('assertionerror', 'test failure', 'tests failed',
                          'failed (failures=', ' failed,', ' failed in ')),
        ('review_rejected', ('reviewer rejected', 'review rejected')),
    ):
        if any(word in text for word in words):
            return category
    return 'unknown'


def verified_failure_signature(output: str) -> str:
    """Keep error identity while removing common clock noise from verified logs."""
    text = re.sub(r'\x1b\[[0-9;]*m', '', output)
    text = re.sub(r'\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?\b', '<time>', text)
    text = re.sub(r'\b\d\d:\d\d:\d\d(?:[.,]\d+)?\b', '<time>', text)
    text = re.sub(r'(\bRan \d+ tests? in )\d+(?:\.\d+)?s\b', r'\1<duration>', text)
    text = re.sub(r'(\bin )\d+(?:\.\d+)?s(?=\s*(?:=|$))', r'\1<duration>', text)
    return digest(re.sub(r'\s+', ' ', text).strip()[-8192:])


def record_failure(state: dict, category: str, detail: str, evidence_ref: str,
                   *, verified_signature: str | None = None) -> dict:
    if category not in ACTIONS or not isinstance(detail, str) or not detail.strip():
        raise ValueError('Failure needs a known category and concrete detail')
    if not isinstance(evidence_ref, str) or not evidence_ref.strip() or len(evidence_ref) > 1000:
        raise ValueError('Failure needs a bounded evidence reference')
    counts = state.setdefault('failure_counts', {})
    attempt = counts.get(category, 0) + 1
    counts[category] = attempt
    verified_attempt = None
    if verified_signature is not None:
        if not re.fullmatch(r'[0-9a-f]{64}', verified_signature):
            raise ValueError('Invalid verified failure signature')
        exact = state.setdefault('verified_failure_counts', {})
        key = category + ':' + verified_signature
        verified_attempt = exact.get(key, 0) + 1
        exact[key] = verified_attempt
    retry_attempt = verified_attempt if verified_attempt is not None else attempt
    next_action = ACTIONS[category]
    if category == 'test_failure' and retry_attempt >= 2:
        next_action = 'Revisit the assumption, design and input conditions before another test retry.'
    if category in {'auth_required', 'permission_denied', 'tool_unavailable', 'budget_exhausted'}:
        retry_allowed = False
    else:
        retry_allowed = retry_attempt < 2
        if not retry_allowed:
            next_action = 'Revisit the assumption and change the hypothesis before another attempt; preserve the failure evidence.'
    row = {'category': category, 'attempt': attempt,
           'verified_attempt': verified_attempt, 'verified_signature': verified_signature,
           'detail': detail[:2000],
           'evidence_ref': evidence_ref, 'next_action': next_action,
           'retry_allowed': retry_allowed, 'observed_at': time.time()}
    state['failure_history'] = (state.get('failure_history', []) + [row])[-30:]
    return row
