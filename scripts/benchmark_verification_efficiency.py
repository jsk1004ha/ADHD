"""Compare observed check counts on one fixed, local successful workload.

The baseline directory must contain the pre-change validation_batch.py and
native.py under adhd/. No installation, provider calls or network are involved.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adhd import native, validation_batch
from adhd.core import digest, file_hash
from adhd.evidence import run_check, validate_execution


def load_before(root, name):
    spec = importlib.util.spec_from_file_location('adhd._before_' + name, root / 'adhd' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workload(batch, controller):
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ADHD_COUNT_BENCH': 'first'}):
        ws = Path(directory)
        for i in range(9):
            (ws / f'f{i}.txt').write_text(str(i), encoding='utf-8')
        checks = [{'id': f'c{i}', 'argv': [sys.executable, '-c', 'print("ok")'],
                   'subject_paths': [f'f{i}.txt'], 'requirements': ['R1'],
                   'impact_complete': True, 'environment_complete': True,
                   'environment_vars': ['ADHD_COUNT_BENCH'] if i == 2 else [],
                   'depends_on': ['c0'] if i == 1 else []} for i in range(9)]
        spec = {'run_id': 'count-benchmark', 'contract_revision': 1, 'contract_hash': 'abc',
                'requirements': ['R1'], 'mandatory_checks': ['c0'], 'process_slots': 2, 'checks': checks[:8]}
        phases = []
        for phase in ('initial', 'repair_leaf', 'add_check', 'environment_change', 'unchanged'):
            if phase == 'repair_leaf':
                (ws / 'f0.txt').write_text('repair', encoding='utf-8')
            elif phase == 'add_check':
                spec['checks'].append(checks[8])
                spec['mandatory_checks'].append('c8')
            elif phase == 'environment_change':
                os.environ['ADHD_COUNT_BENCH'] = 'second'
            started = perf_counter()
            with patch.object(batch, 'run_check', wraps=run_check) as executions:
                result = batch.run_batch(spec, ws)
                count = executions.call_count
            report = batch.load_report(ws, result['report'])
            if report['status'] != 'passed' or report['usage']['check_processes'] != count:
                raise ValueError('Workload outcome or observed execution count changed')
            phases.append({'phase': phase, 'processes': count, 'status': report['status'],
                           'reused': sum(r['status'] == 'reused' for r in report['results']),
                           'wall_seconds': perf_counter() - started})
            spec['previous_report'] = result['report']
        receipt = report['results'][0]['receipt']
        state = {'workspace': str(ws), 'run_id': spec['run_id'], 'intent_version': 1,
                 'contract': {'criteria': [{'id': f'R{i}', 'kind': 'test'} for i in range(1, 9)]}}
        rows = [{'id': f'R{i}', 'pass': True, 'evidence': 'actual workload result',
                 'evidence_ids': [receipt]} for i in range(1, 9)]
        with patch.object(controller, 'validate_execution', wraps=validate_execution) as validations:
            controller.result_valid(state, rows, require_execution=True)
            shared_validations = validations.call_count
        return {'phases': phases, 'check_processes': sum(r['processes'] for r in phases),
                'shared_receipt_validations': shared_validations,
                'wall_seconds': sum(r['wall_seconds'] for r in phases), 'tokens': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-tree', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    before = args.baseline_tree.resolve()
    baseline = workload(load_before(before, 'validation_batch'), load_before(before, 'native'))
    candidate = workload(validation_batch, native)
    payload = {'kind': 'deterministic_verification_count_fixture', 'baseline': baseline,
               'candidate': candidate, 'all_outcomes_passed': True,
               'baseline_sha256': {name: file_hash(before / 'adhd' / (name + '.py'))
                                   for name in ('validation_batch', 'native')},
               'candidate_sha256': {name: file_hash(ROOT / 'adhd' / (name + '.py'))
                                    for name in ('validation_batch', 'native')},
               'workload_sha256': file_hash(Path(__file__)),
               'environment': digest([sys.version, os.name, sys.executable]),
               'limits': ['One local fixture pair; wall times are observations, not a reliable speedup estimate.',
                          'Provider tokens and end-to-end coding task savings are unmeasured.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: {'check_processes': value['check_processes'],
                           'shared_receipt_validations': value['shared_receipt_validations']}
                      for key, value in [('baseline', baseline), ('candidate', candidate)]}))


if __name__ == '__main__':
    main()
