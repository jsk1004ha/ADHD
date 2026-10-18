"""Measure optional early-failure feedback on a fixed local workload; no model calls."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adhd import validation_batch
from adhd.core import file_hash


def load_baseline(tree):
    source = tree / 'adhd/validation_batch.py'
    spec = importlib.util.spec_from_file_location('adhd._time_baseline', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workload(batch, fast):
    with tempfile.TemporaryDirectory() as directory:
        ws = Path(directory)
        (ws / 'input.txt').write_text('same fixed input\n', encoding='utf-8')
        (ws / 'fast.py').write_text('import sys;sys.exit(3)\n', encoding='utf-8')
        checks = [
            {'id': 'slow1', 'argv': [sys.executable, '-c', 'import time;time.sleep(.18)'],
             'subject_paths': ['input.txt'], 'estimated_seconds': 1, 'requirements': ['R1']},
            {'id': 'slow2', 'argv': [sys.executable, '-c', 'import time;time.sleep(.18)'],
             'subject_paths': ['input.txt'], 'estimated_seconds': 1, 'requirements': ['R1']},
            {'id': 'fast', 'argv': [sys.executable, 'fast.py'], 'subject_paths': ['fast.py'],
             'estimated_seconds': .01, 'requirements': ['R1']},
        ]
        spec = {'run_id': 'fixed-time-fixture', 'contract_revision': 1, 'contract_hash': 'fixed',
                'requirements': ['R1'], 'mandatory_checks': [row['id'] for row in checks],
                'process_slots': 1, 'checks': checks}
        if fast:
            spec.update(fail_fast=True, scheduling='shortest_first')
        observations = []
        for phase in ('diagnose_failure', 'after_real_fix'):
            if phase == 'after_real_fix':
                (ws / 'fast.py').write_text('print("fixed")\n', encoding='utf-8')
            started = time.perf_counter()
            result = batch.run_batch(spec, ws)
            report = batch.load_report(ws, result['report'], require_current=phase == 'after_real_fix')
            elapsed = time.perf_counter() - started
            expected = 'needs_repair' if phase == 'diagnose_failure' else 'passed'
            if result['status'] != expected:
                raise ValueError('Unexpected actual workload outcome: ' + result['status'])
            processes = report['usage']['check_processes']
            if phase == 'diagnose_failure' and processes != (1 if fast else 3):
                raise ValueError('Unexpected observed process count')
            if phase == 'after_real_fix' and (processes != 3 or len(report['results']) != 3):
                raise ValueError('Required verification was omitted after repair')
            observations.append({'phase': phase, 'status': result['status'],
                                 'seconds': elapsed, 'processes': processes,
                                 'counts': report['counts']})
            spec['previous_report'] = result['report']
        return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-tree', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 5 or args.output.exists():
        raise ValueError('Use 1..5 repetitions and a new output file')
    before = load_baseline(args.baseline_tree.resolve())
    rows = []
    for i in range(args.repetitions):
        values = {}
        # Alternate ordering to expose the individual runs rather than hide cache effects.
        for name in (('baseline', 'candidate') if i % 2 == 0 else ('candidate', 'baseline')):
            values[name] = workload(before if name == 'baseline' else validation_batch, name == 'candidate')
        rows.append(values)
    median = {name: statistics.median(row[name][0]['seconds'] for row in rows)
              for name in ('baseline', 'candidate')}
    result = {'kind': 'controlled_local_failure_feedback_not_model_benchmark',
              'pairs': rows, 'failure_feedback_median_seconds': median,
              'median_feedback_reduction_fraction': 1 - median['candidate'] / median['baseline'],
              'failure_processes_per_pair': {'baseline': 3, 'candidate': 1},
              'after_fix_required_processes': {'baseline': 3, 'candidate': 3},
              'required_evidence_preserved': True, 'model_calls': 0, 'tokens': None,
              'source_sha256': {'baseline_validation_batch': file_hash(args.baseline_tree / 'adhd/validation_batch.py'),
                                'candidate_validation_batch': file_hash(ROOT / 'adhd/validation_batch.py'),
                                'shared_current_evidence_runner': file_hash(ROOT / 'adhd/evidence.py'),
                                'workload': file_hash(Path(__file__))},
              'environment': {'python': sys.version, 'executable': sys.executable},
              'limits': ['Three short serial checks in disposable workspaces, with one deliberate failure.',
                         'Fail-fast defers mandatory work only in the failed iteration; repair runs all three checks.',
                         'Same current process/evidence runner for both arms; only batch policy is compared.',
                         'Cache and OS load are not controlled; individual alternating runs are retained.',
                         'No general task, token, memory or production speedup is established.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('kind', 'failure_feedback_median_seconds',
                      'median_feedback_reduction_fraction', 'required_evidence_preserved', 'model_calls')}))


if __name__ == '__main__':
    main()
