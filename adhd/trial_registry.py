"""Append-only all-start trial accounting; observations are not acceptance receipts.

No prompts, command output, credentials or subscription-price estimates belong
here. The registry deliberately retains failed, interrupted and unobserved runs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
from statistics import median
import tempfile
import time
import uuid

from filelock import FileLock

from .core import atomic_json, digest, store
from .memory import SECRET
from .run_metrics import summarize_run_metrics


CONDITIONS = frozenset({'model', 'effort', 'input_snapshot', 'source_snapshot', 'policy',
                        'environment', 'tools', 'permissions', 'delivery', 'cache'})
MATCH_CONDITIONS = CONDITIONS - {'source_snapshot'}
STATUSES = frozenset({'success', 'failed', 'blocked', 'timeout', 'cancelled', 'missing'})
METRICS = ('verified_ms', 'context_bytes', 'peak_private_bytes', 'stored_bytes',
           'first_usable_ms', 'input_tokens', 'cached_input_tokens', 'output_tokens',
           'human_corrections', 'rework', 'false_completion', 'required_recall',
           'stale_results', 'returned_bytes', 'duplicate_reads', 'recovery_ms')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _post_task_observation(value):
    numbers = {'remaining_count', 'remaining_count_lower_bound', 'remaining_cpu_delta_seconds',
               'window_elapsed_ms', 'unavailable_pid_count', 'reused_pid_count',
               'tracked_identity_count', 'remaining_identity_count', 'root_pid', 'observer_pid'}
    flags = {'remaining_idle_cpu_observed', 'sampler_stopped',
             'remaining_identities_truncated', 'observer_in_remaining'}
    lists = {'unavailable_pids', 'reused_pids'}
    labels = {'remaining_coverage', 'cpu_coverage', 'scope'}
    required = {'remaining_count', 'remaining_cpu_delta_seconds', 'window_elapsed_ms',
                'sampler_stopped', 'scope'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - numbers - flags - lists - labels - {'remaining_identities'}):
        raise ValueError('Invalid post-task observation schema')
    if any(v is not None and not _number(v) for k, v in value.items() if k in numbers):
        raise ValueError('Invalid post-task numeric observation')
    if any(v is not None and type(v) is not bool for k, v in value.items() if k in flags):
        raise ValueError('Invalid post-task observation flag')
    for key in lists:
        rows = value.get(key, [])
        if not isinstance(rows, list) or len(rows) > 64 or any(type(v) is not int or v < 0 for v in rows):
            raise ValueError('Invalid post-task PID list')
    identities = value.get('remaining_identities', [])
    if (not isinstance(identities, list) or len(identities) > 32
            or any(not isinstance(row, dict) or set(row) != {'pid', 'birth'}
                   or any(type(v) is not int or v < 0 for v in row.values()) for row in identities)):
        raise ValueError('Invalid post-task process identity')
    if any(not isinstance(v, str) or len(v) > 500 for k, v in value.items() if k in labels):
        raise ValueError('Invalid post-task scope or coverage')


class TrialRegistry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else store() / 'trials-v1'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'trials.jsonl'
        self.lock = FileLock(str(self.path) + '.lock', timeout=8)

    def _events(self):
        if not self.path.exists():
            return
        with self.path.open('rb') as stream:
            while raw := stream.readline(65537):
                if len(raw) > 65536 or not raw.endswith(b'\n'):
                    raise ValueError('Registry contains an oversized or incomplete event; preserve and repair it explicitly')
                event = json.loads(raw)
                if event.get('schema_version') != 1:
                    raise ValueError('Unsupported trial event version')
                yield event

    def _append(self, event):
        raw = (json.dumps({'schema_version': 1, 'at_ns': time.time_ns(), **event},
                          ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(raw) > 65536:
            raise ValueError('Trial event exceeds metadata budget')
        with self.path.open('ab') as stream:
            stream.write(raw)
            stream.flush()
            import os
            os.fsync(stream.fileno())

    def start(self, case_id: str, task_type: str, variant: str, conditions: dict,
              *, phase='component', usable_scenario: str | None = None) -> str:
        if set(conditions) != CONDITIONS or any(not isinstance(v, str) or not v or len(v) > 500
                                               for v in conditions.values()):
            raise ValueError('Trial requires bounded, explicit comparison conditions')
        labels = [case_id, task_type, variant, phase, usable_scenario or '', *conditions.values()]
        if any(not isinstance(v, str) or len(v) > 500 for v in labels) or SECRET.search(' '.join(labels)):
            raise ValueError('Invalid or secret-bearing trial metadata')
        if not case_id or not task_type or variant not in {'baseline', 'candidate'} or phase not in {'component', 'pilot', 'holdout'}:
            raise ValueError('Invalid trial identity')
        trial_id = uuid.uuid4().hex
        with self.lock:
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open('rb') as stream:
                    stream.seek(-1, 2)
                    if stream.read(1) != b'\n':
                        raise ValueError('Registry contains an incomplete event')
            self._append(dict(kind='start', id=trial_id, case_id=case_id, task_type=task_type,
                              variant=variant, phase=phase, conditions=dict(conditions),
                              usable_scenario=usable_scenario, started_ns=time.time_ns()))
        return trial_id

    def _find(self, trial_id):
        result = None
        for event in self._events():
            if event['id'] != trial_id:
                continue
            if event['kind'] == 'start':
                result = dict(event, status='unfinished', first_usable_ms=None)
            elif result is not None:
                result.update({key: value for key, value in event.items() if key not in {'kind', 'at_ns', 'schema_version'}})
        if result is None:
            raise ValueError('Unknown trial ID')
        return result

    def usable(self, trial_id: str, receipt: Path):
        raw = Path(receipt).read_bytes()
        if len(raw) > 65536:
            raise ValueError('Scenario receipt exceeds budget')
        evidence = json.loads(raw)
        with self.lock:
            row = self._find(trial_id)
            if (row['status'] != 'unfinished' or not row.get('usable_scenario')
                    or evidence.get('scenario_id') != row['usable_scenario'] or evidence.get('passed') is not True):
                raise ValueError('First usable requires the preregistered scenario passing before trial finish')
            if row.get('first_usable_ms') is not None:
                return row['first_usable_ms']
            elapsed = (time.time_ns() - row['started_ns']) / 1_000_000
            self._append(dict(kind='usable', id=trial_id, first_usable_ms=elapsed,
                              first_usable_assurance='scenario_report_not_independent_acceptance',
                              usable_receipt={'path': str(receipt), 'sha256': hashlib.sha256(raw).hexdigest()}))
            return elapsed

    def finish(self, trial_id: str, status: str, *, metrics=None, quality=None, usage_events=None,
               resources=None, reason: str = ''):
        metrics, quality = metrics or {}, quality or {}
        resources=resources or {}
        numeric={'cpu_seconds','read_bytes','write_bytes','peak_private_bytes','peak_working_set_bytes',
                 'observed_processes','sample_count','interval_ms','elapsed_ms','missing_pid_count'}
        if not isinstance(resources,dict) or set(resources)-numeric-{'coverage','missing_pids','errors','sampler_stopped','note','post_task'}:
            raise ValueError('Resource observations require the sampler metadata schema')
        if 'post_task' in resources:
            _post_task_observation(resources['post_task'])
        if any(resources.get(k) is not None and not _number(resources[k]) for k in numeric if k in resources):
            raise ValueError('Invalid resource observation')
        if len(json.dumps(resources))>8000 or SECRET.search(json.dumps(resources)):
            raise ValueError('Oversized or secret-bearing resource observation')
        if status not in STATUSES or set(metrics) - set(METRICS) or any(v is not None and not _number(v) for v in metrics.values()):
            raise ValueError('Invalid terminal status or observed metrics')
        if set(quality) - {'requirements', 'preservation', 'security', 'independent'} or any(type(v) is not bool for v in quality.values()):
            raise ValueError('Quality fields require explicit booleans')
        if len(reason) > 1000 or SECRET.search(reason):
            raise ValueError('Keep only a bounded nonsecret failure reason')
        usage = summarize_run_metrics(usage_events or [])
        with self.lock:
            row = self._find(trial_id)
            if row['status'] != 'unfinished':
                raise ValueError('A finished trial is immutable; register a separate repeat')
            self._append(dict(kind='finish', id=trial_id, status=status, metrics=metrics, quality=quality,
                              usage=usage, resources=resources or {}, reason=reason,
                              elapsed_ms=(time.time_ns() - row['started_ns']) / 1_000_000))

    def trials(self):
        # A disposable disk-backed projection keeps both memory and scan cost
        # bounded. It never replaces the append-only evidence source.
        with tempfile.TemporaryDirectory(prefix='adhd-trials-') as scratch:
            db = sqlite3.connect(str(Path(scratch) / 'projection.sqlite3'))
            try:
                db.execute('PRAGMA cache_size=-8192')
                db.execute('CREATE TABLE trials(id TEXT PRIMARY KEY,payload TEXT)')
                with self.lock, db:
                    for event in self._events():
                        if event['kind'] == 'start':
                            row = dict(event, status='unfinished', first_usable_ms=None)
                        else:
                            prior = db.execute('SELECT payload FROM trials WHERE id=?', (event['id'],)).fetchone()
                            if prior is None:
                                raise ValueError('Trial event has no start')
                            row = json.loads(prior[0])
                            row.update({k: v for k, v in event.items() if k not in {'kind', 'at_ns', 'schema_version'}})
                        db.execute('INSERT OR REPLACE INTO trials VALUES (?,?)', (event['id'], json.dumps(row)))
                for row in db.execute('SELECT payload FROM trials ORDER BY rowid'):
                    yield json.loads(row[0])
            finally:
                db.close()

    def summary(self):
        statuses = {}
        for row in self.trials():
            statuses[row['status']] = statuses.get(row['status'], 0) + 1
        total = sum(statuses.values())
        return dict(started=total, succeeded=statuses.get('success', 0),
                    unfinished=statuses.get('unfinished', 0), statuses=statuses,
                    completion_rate=statuses.get('success', 0) / total if total else None)

    def preregister(self, spec: dict):
        cases = spec.get('cases', [])
        if (not isinstance(cases, list) or not cases or len(cases) > 1000
                or any(not isinstance(c,dict) or set(c) != {'id', 'task_type', 'phase', 'usable_scenario', 'input_sha256'} for c in cases)
                or any(not all(isinstance(v,str) and 0<len(v)<=500 for v in c.values()) for c in cases)
                or any(not re.fullmatch(r'[0-9a-f]{64}',c['input_sha256']) for c in cases)
                or len({c['id'] for c in cases}) != len(cases)
                or any(c['phase'] not in {'pilot', 'holdout', 'component'} for c in cases)):
            raise ValueError('Preregister distinct cases, phases, usable scenarios and immutable input digests')
        if SECRET.search(json.dumps(spec)) or len(json.dumps(spec)) > 200000:
            raise ValueError('Invalid experiment metadata')
        path = self.root / ('experiment-' + digest(spec) + '.json')
        with self.lock:
            if not path.exists():
                atomic_json(path, spec)
        return {'path': str(path), 'sha256': digest(spec)}


def compare_trials(trials, *, expected_cases=(), minimum_per_type=6):
    """Predeclared engineering gates. Repeats never increase independent case N."""
    grouped = {}
    started = succeeded = 0
    for row in trials:
        started += 1
        succeeded += row.get('status') == 'success'
        grouped.setdefault(row['case_id'], {}).setdefault(row['variant'], []).append(row)
    missing = sorted(set(expected_cases) - set(grouped))
    exclusions, values, types = [], {key: [] for key in METRICS}, {}
    quality_passed = not missing
    for case, variants in grouped.items():
        if set(variants) != {'baseline', 'candidate'} or any(len(v) != 1 for v in variants.values()):
            exclusions.append({'case_id': case, 'reason': 'unpaired_or_repeated_case'})
            quality_passed = False
            continue
        before, after = variants['baseline'][0], variants['candidate'][0]
        if (before.get('task_type') != after.get('task_type') or before.get('phase') != after.get('phase')
                or any(before['conditions'].get(key) != after['conditions'].get(key) for key in MATCH_CONDITIONS)):
            exclusions.append({'case_id': case, 'reason': 'comparison_conditions_differ'})
            quality_passed = False
            continue
        good = all(row.get('status') == 'success' and all(row.get('quality', {}).get(key) is True
                   for key in ('requirements', 'preservation', 'security', 'independent')) for row in (before, after))
        if not good:
            quality_passed = False
            exclusions.append({'case_id': case, 'reason': 'failed_or_unobserved_quality'})
        else:
            types[before['task_type']] = types.get(before['task_type'], 0) + 1
        for key in METRICS:
            if key.endswith('_tokens'):
                pair = [row.get('usage', {}).get('tokens', {}).get(key, {}).get('known')
                        if row.get('usage', {}).get('coverage') == 'complete' else None for row in (before, after)]
            else:
                pair = [row.get('metrics', {}).get(key, row.get(key)) for row in (before, after)]
            if key=='first_usable_ms' and any(row.get('first_usable_assurance')!='verified_execution_and_independent_scenario' for row in (before,after)):
                pair=[None,None]
            if good and all(_number(v) for v in pair) and pair[0] > 0:
                values[key].append(1 - pair[1] / pair[0])
    metrics = {key: {'paired': len(vals), 'median_savings': median(vals) if vals else None,
                     'excluded': len(grouped) - len(vals)} for key, vals in values.items()}
    primary = any(metrics[key]['median_savings'] is not None and metrics[key]['median_savings'] >= threshold
                  for key, threshold in [('verified_ms', .10), ('context_bytes', .20),
                                         ('peak_private_bytes', .20), ('stored_bytes', .20)])
    auxiliary = bool(grouped) and all(metrics[key]['paired'] == len(grouped) and
                    metrics[key]['median_savings'] >= -.10 for key in ('verified_ms', 'peak_private_bytes'))
    n_pass = bool(types) and all(n >= minimum_per_type for n in types.values())
    return {'started': started, 'completion_rate': succeeded / started if started else None,
            'independent_cases': len(grouped), 'by_task_type': types, 'missing_cases': missing,
            'exclusions': exclusions, 'metrics': metrics, 'quality_gate': quality_passed,
            'meets_component_gate': bool(quality_passed and primary and auxiliary and n_pass),
            'adopt': False,
            'claim': 'observed_pairs_only_engineering_gate_not_statistical_proof'}


def rollout_gate(spec: dict, trials) -> dict:
    """Require the separate 4x3 pilot and 4x6 holdout, never synthetic repeats."""
    cases = spec.get('cases', [])
    expected_types = {'windows_code', 'ui_preservation', 'document_format', 'cited_research'}
    if (not cases or len({case['id'] for case in cases}) != len(cases)
            or any(case['task_type'] not in expected_types for case in cases)):
        raise ValueError('Declare distinct preregistered cases for all four task types')
    rows = list(trials)
    reports = {}
    for phase, minimum in [('pilot', 3), ('holdout', 6)]:
        planned = [case for case in cases if case['phase'] == phase]
        planned_counts = {typ: sum(case['task_type'] == typ for case in planned) for typ in expected_types}
        chosen = [row for row in rows if row.get('phase') == phase]
        by_id = {case['id']: case for case in planned}
        for row in chosen:
            case = by_id.get(row['case_id'])
            if (case is None or row['task_type'] != case['task_type']
                    or row.get('usable_scenario') != case['usable_scenario']
                    or row['conditions']['input_snapshot'] != case['input_sha256']):
                raise ValueError('Observed trial differs from preregistered input/phase/scenario')
        report = compare_trials(chosen, expected_cases=by_id, minimum_per_type=minimum)
        report['design_complete'] = all(count >= minimum for count in planned_counts.values())
        report['all_types_observed'] = set(report['by_task_type']) == expected_types
        reports[phase] = report
    return {'schema_version': 1, 'experiment_sha256': digest(spec), 'phases': reports,
            'declared_metrics_gate': all(row['meets_component_gate'] and row['design_complete'] and row['all_types_observed']
                                        for row in reports.values()),
            'adopt': False, 'independent_acceptance': 'not_attested_by_trial_metadata',
            'default_changed': False, 'policy': 'missing_or_failed_gate_retains_existing_default'}
