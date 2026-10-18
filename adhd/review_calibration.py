"""Measure observed independent reviewer decisions against fixed calibration cases."""
from __future__ import annotations

from .core import file_hash
from .obsidian_index import safe_path


def calibrate(workspace, cases: list[dict], reviews: list[dict]) -> dict:
    if not isinstance(cases, list) or not 1 <= len(cases) <= 500 or not isinstance(reviews, list) or len(reviews) > 1000:
        raise ValueError('Calibration requires bounded preregistered cases and observed reports')
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Duplicate calibration case')
    by_id = {}
    for review in reviews:
        by_id.setdefault(review['case_id'], []).append(review)
    false_accepts = false_rejects = accepts = rejects = 0
    missing = []
    for case in cases:
        if case.get('expected') not in {'accept', 'reject'}:
            raise ValueError('Declare both acceptable and faulty cases before review')
        path = safe_path(workspace, case['artifact'])
        rows = by_id.get(case['id'], [])
        if (not path.is_file() or file_hash(path) != case.get('sha256') or len(rows) != 1):
            missing.append({'case_id': case['id'], 'reason': 'missing_stale_or_repeated_case'})
            continue
        review = rows[0]
        if (review.get('artifact_sha256') != case['sha256'] or not review.get('reviewer_id')
                or review.get('reviewer_id') == case.get('writer_id')
                or review.get('decision') not in {'accept', 'reject'}):
            missing.append({'case_id': case['id'], 'reason': 'unbound_or_nonindependent_review'})
            continue
        report = safe_path(workspace, review['report'])
        if not report.is_file() or file_hash(report) != review.get('report_sha256'):
            missing.append({'case_id': case['id'], 'reason': 'missing_or_stale_report'})
            continue
        if case['expected'] == 'accept':
            accepts += 1
            false_rejects += review['decision'] == 'reject'
        else:
            rejects += 1
            false_accepts += review['decision'] == 'accept'
    return {'schema_version': 1, 'acceptable_cases': accepts, 'faulty_cases': rejects,
            'false_accepts': false_accepts, 'false_rejects': false_rejects,
            'false_accept_rate': false_accepts / rejects if rejects else None,
            'false_reject_rate': false_rejects / accepts if accepts else None,
            'missing': missing, 'complete': bool(accepts and rejects and not missing),
            'native_acceptance_changed': False, 'claim': 'observed_review_reports_only'}
