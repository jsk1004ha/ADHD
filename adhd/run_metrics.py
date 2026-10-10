"""Observed run accounting. Missing provider fields remain unknown."""
from __future__ import annotations

from statistics import median


FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens')


def summarize_run_metrics(events: list[dict]) -> dict:
    if not isinstance(events, list):
        raise ValueError('Metric events must be a list')
    seen = {}
    totals = {field: {'known': 0, 'missing_events': 0, 'observed_events': 0} for field in FIELDS}
    last = {}
    segments = {}
    spans = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get('id'), str):
            raise ValueError('Metric event needs a stable ID')
        if event['id'] in seen:
            if seen[event['id']] != event:
                raise ValueError('Conflicting metric event ID')
            continue
        seen[event['id']] = event
        if event.get('kind') == 'run':
            start, finish = event.get('started_ms'), event.get('finished_ms')
            if not all(type(v) in (int, float) for v in (start, finish)) or finish < start:
                raise ValueError('Invalid run span')
            spans.append((start, finish))
            continue
        if event.get('kind') != 'usage':
            continue
        actor = event.get('actor')
        if not isinstance(actor, str) or not actor:
            raise ValueError('Usage event needs parent or child actor')
        # Some hosts expose child usage inside a parent aggregate. Keep the
        # observation but do not add it again to the account total.
        countable = not event.get('included_in_parent', False)
        reset = bool(event.get('cumulative') and any(
            event.get(field) is not None and last.get((actor, field)) is not None
            and event[field] < last[(actor, field)] for field in FIELDS))
        if countable and reset:
            segments[actor] = segments.get(actor, 1) + 1
        for field in FIELDS:
            value = event.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('Token fields need nonnegative integers or null')
            if not countable:
                continue
            if value is None:
                totals[field]['missing_events'] += 1
                continue
            if event.get('cumulative'):
                prior = last.get((actor, field))
                if prior is None or value < prior:
                    delta = value
                else:
                    delta = value - prior
                last[(actor, field)] = value
            else:
                delta = value
            totals[field]['known'] += delta
            totals[field]['observed_events'] += 1
    for value in totals.values():
        if not value['observed_events']:
            value['known'] = None
    wall = max(finish for _, finish in spans) - min(start for start, _ in spans) if spans else None
    return {'tokens': totals, 'wall_ms': wall, 'reset_segments': segments,
            'event_count': len(seen), 'coverage': 'complete' if all(
                item['missing_events'] == 0 and item['observed_events'] > 0 for item in totals.values())
                else 'partial' if any(item['observed_events'] for item in totals.values()) else 'unknown'}


def compare_paired_runs(pairs: list[dict]) -> dict:
    """Deterministic comparison; a missing or failed gate never becomes savings."""
    def accepted(row: dict) -> bool:
        gate = row.get('acceptance')
        required = ('requirements_passed', 'preservation_passed',
                    'security_passed', 'independent_review_passed')
        if not isinstance(gate, dict) or not all(gate.get(key) is True for key in required):
            return False
        return row['delivery_target'] == 'local_artifact' or gate.get('delivery_passed') is True

    by_type = {}
    for pair in pairs:
        if (not isinstance(pair, dict) or not isinstance(pair.get('baseline'), dict)
                or not isinstance(pair.get('candidate'), dict)
                or not pair['baseline'].get('task_type')
                or pair['baseline']['task_type'] != pair['candidate'].get('task_type')):
            raise ValueError('Pair needs matching task type')
        base, candidate = dict(pair['baseline']), dict(pair['candidate'])
        if not base.get('case_id') or base['case_id'] != candidate.get('case_id'):
            raise ValueError('Paired run case IDs are missing or differ')
        if (base.get('delivery_target') not in {'local_artifact', 'release', 'live_deployment'}
                or base['delivery_target'] != candidate.get('delivery_target')):
            raise ValueError('Paired run delivery targets are missing or differ')
        for key in ('model', 'effort', 'scope_hash', 'environment_hash'):
            if not base.get(key) or base[key] != candidate.get(key):
                raise ValueError('Paired run conditions differ: ' + key)
        typ = base['task_type']
        row = {'gate': accepted(base) and accepted(candidate)}
        for record in (base, candidate):
            input_count, cached = record.get('input_tokens'), record.get('cached_input_tokens')
            record['uncached_input_tokens'] = (input_count - cached if type(input_count) is int
                and type(cached) is int and 0 <= cached <= input_count else None)
        for key in ('elapsed_wall_ms', 'first_usable_ms', 'delivery_ms', 'input_tokens',
                    'cached_input_tokens', 'uncached_input_tokens', 'output_tokens',
                    'duplicate_checks', 'unchanged_polls'):
            before, after = base.get(key), candidate.get(key)
            row[key] = None if (not row['gate'] or type(before) not in (int,float)
                                or type(after) not in (int,float) or before <= 0 or after < 0) else 1 - after / before
        by_type.setdefault(typ, []).append(row)
    result = {}
    for typ, rows in by_type.items():
        metrics = {}
        for key in ('elapsed_wall_ms', 'first_usable_ms', 'delivery_ms', 'input_tokens',
                    'cached_input_tokens', 'uncached_input_tokens', 'output_tokens',
                    'duplicate_checks', 'unchanged_polls'):
            values = [row[key] for row in rows if row[key] is not None]
            metrics[key] = median(values) if len(values) == len(rows) and values else None
        result[typ] = {'pairs': len(rows), 'quality_gate': all(row['gate'] for row in rows),
                       'paired_median_savings': metrics,
                       'disposition': 'inconclusive' if len(rows) < 3 or not all(row['gate'] for row in rows)
                       or any(metrics[key] is None for key in ('first_usable_ms','input_tokens','uncached_input_tokens'))
                       else 'measured_exploratory'}
    return {'by_task_type': result, 'claim': 'observed_pair_values_only'}
