"""Execute one isolated failed-check replay against two source trees.

This is a deterministic failure-policy and prompt-payload microbenchmark, not
a successful task, deployment, provider-token measurement, or causal time claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter_ns


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _worker(tree: Path, isolated: Path) -> dict:
    sys.path.insert(0, str(tree))
    from adhd import dependencies as _dependencies  # Load the selected tree's shipped libraries.
    from adhd.core import digest
    from adhd.evidence import validate_execution
    from adhd.large_prompts import worker_prompt
    from adhd.validation_batch import load_report, run_batch
    import adhd.validation_batch as batch_module
    import adhd.large_prompts as prompt_module

    if (not Path(batch_module.__file__).resolve().is_relative_to(tree)
            or not Path(prompt_module.__file__).resolve().is_relative_to(tree)):
        raise ValueError('Worker imported a module outside the selected source tree')
    workspace = isolated / 'workspace'
    workspace.mkdir(parents=True)
    (workspace / 'subject.txt').write_text('unchanged input\n', encoding='utf-8')
    code = ('from pathlib import Path;import sys;'
            'p=Path("attempts.log");'
            'p.open("a",encoding="utf-8").write("started\\n");'
            'print("AssertionError: fixed failing fixture",file=sys.stderr);sys.exit(1)')
    spec = {'run_id': 'fixed-failure-replay', 'contract_revision': 1,
            'contract_hash': digest('unchanged contract'), 'requirements': ['R1'],
            'mandatory_checks': ['failure'], 'process_slots': 1,
            'checks': [{'id': 'failure', 'argv': [sys.executable, '-c', code],
                        'subject_paths': ['subject.txt'], 'requirements': ['R1'],
                        'impact_complete': True, 'environment_complete': True}]}
    runs, receipts = [], []
    first_snapshot = first_plan = first_subject = None
    marker = workspace / 'attempts.log'
    for index in range(5):
        before = len(marker.read_text(encoding='utf-8').splitlines()) if marker.exists() else 0
        started = perf_counter_ns()
        result = run_batch(spec, workspace)
        wall_ms = (perf_counter_ns() - started) / 1_000_000
        report = load_report(workspace, result['report'], require_current=False)
        row = report['results'][0]
        snapshot = report['snapshot']['digest']
        subject = report['snapshot']['files']['subject.txt']
        if index == 0:
            first_snapshot, first_plan, first_subject = snapshot, report['plan_digest'], subject
        after = len(marker.read_text(encoding='utf-8').splitlines()) if marker.exists() else 0
        validate_execution(workspace, row['receipt'], run_id=spec['run_id'],
                           revision=1, expect_failure=True)
        if (report['status'] != 'needs_repair' or row['status'] != 'failed'
                or report['snapshot']['contract_hash'] != spec['contract_hash']
                or snapshot != first_snapshot or report['plan_digest'] != first_plan
                or subject != first_subject
                or after - before != report['usage']['check_processes']):
            raise ValueError('Replay outcome or observed process accounting changed')
        receipts.append(row['receipt'])
        runs.append({'index': index + 1, 'status': report['status'],
                     'check_status': row['status'],
                     'decision_class': row.get('decision_class'),
                     'new_processes': after - before,
                     'reported_check_processes': report['usage']['check_processes'],
                     'receipt_sha256': _sha(workspace / row['receipt']),
                     'wall_ms': wall_ms})
        spec['previous_report'] = result['report']
    starts = len(marker.read_text(encoding='utf-8').splitlines())
    if starts != len(set(receipts)) or starts != sum(row['new_processes'] for row in runs):
        raise ValueError('Process markers and distinct verified receipts disagree')

    original = ('기존 CPU/RAM 그래프를 유지하고 원래 실행 방법을 보존해 주세요. ' * 24).strip()
    amendment = '갱신 간격을 줄이되 기존 그래프와 Windows 실행 경로를 그대로 유지해 주세요.'
    card = {'id': 'T1', 'task_id': 'T1', 'package': 'fixture',
            'objective': 'Preserve behavior while improving refresh', 'owner': 'fixture',
            'generation': 1, 'workspace': 'isolated', 'base_commit': 'fixed',
            'contract_hash': digest('contract-v1'), 'contract_revision': 1,
            'requirements': [{'id': 'R1', 'text': original, 'kind': 'behavior'}],
            'verbatim_excerpts': [original], 'write_paths': ['widget.py'],
            'depends_on': [], 'inputs': ['widget.py'], 'outputs': ['widget.exe']}
    initial_bytes = len(worker_prompt(card).encode('utf-8'))
    amended = {**card, 'contract_revision': 2, 'contract_hash': digest('contract-v2'),
               'requirements': card['requirements'] + [
                   {'id': 'R2', 'text': amendment, 'kind': 'behavior'}],
               'verbatim_excerpts': [original, amendment]}
    resend_bytes = len(worker_prompt(amended).encode('utf-8'))
    prompt = {'initial_bytes': initial_bytes, 'full_resend_bytes': resend_bytes,
              'delta_bytes': None, 'original_excerpts_intact': original in worker_prompt(amended)}
    if hasattr(prompt_module, 'plan_context_packet'):
        packet = prompt_module.plan_context_packet({
            'task_id': card['task_id'], 'revision': 1, 'requirement_ids': ['R1'],
            'verbatim_excerpts': card['verbatim_excerpts'],
            'owned_paths': card['write_paths'], 'dependencies': [],
            'acceptance': ['R1'], 'evidence_refs': []},
            {'contract_hash': card['contract_hash'], 'recovery_path': 'packet-v1.json'})
        (workspace / 'packet-v1.json').write_bytes(_encoded(packet))
        delta = {'base_revision': 1, 'revision': 2,
                 'changed': {'requirement_ids': ['R1', 'R2'],
                             'acceptance': ['R1', 'R2'],
                             'append_verbatim_excerpts': [amendment],
                             'inputs': {'contract_hash': amended['contract_hash'],
                                        'recovery_path': 'packet-v1.json'}}}
        updated = prompt_module.apply_context_delta(packet, delta)
        recovered = json.loads((workspace / 'packet-v1.json').read_text(encoding='utf-8'))
        intact = (updated['verbatim_excerpts'] == amended['verbatim_excerpts']
                  and recovered['packet_digest'] == packet['packet_digest']
                  and updated['inputs']['recovery_path'] == 'packet-v1.json')
        if not intact:
            raise ValueError('Context delta lost original excerpts or recovery reference')
        prompt.update(delta_bytes=len(_encoded(delta)), original_excerpts_intact=True)
    return {'source_tree': str(tree), 'validation_batch_sha256': _sha(tree / 'adhd/validation_batch.py'),
            'large_prompts_sha256': _sha(tree / 'adhd/large_prompts.py'),
            'plan_digest': first_plan, 'subject_sha256': first_subject,
            'runs': runs, 'observed_process_starts': starts,
            'distinct_receipts': len(set(receipts)), 'prompt': prompt}


def _run_tree(tree: Path, isolated: Path) -> dict:
    if not (tree / 'adhd/validation_batch.py').is_file():
        raise ValueError('Source tree lacks validation_batch.py: ' + str(tree))
    environment = {**os.environ, 'CODEX_HOME': str(isolated / 'codex'),
                   'ADHD_HOME': str(isolated / 'codex/adhd'), 'ADHD_EXEC_OWNER': '',
                   'PYTHONPATH': str(tree), 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONUTF8': '1'}
    run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker',
                          str(tree), str(isolated)], cwd=tree, env=environment,
                         capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise ValueError('Isolated benchmark worker failed: ' + run.stderr[-4000:])
    return json.loads(run.stdout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-tree', required=True, type=Path)
    parser.add_argument('--candidate-tree', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    roots = [args.baseline_tree.resolve(), args.candidate_tree.resolve()]
    with tempfile.TemporaryDirectory(prefix='adhd-delivery-benchmark-') as temporary:
        base = Path(temporary)
        baseline = _run_tree(roots[0], base / 'baseline')
        candidate = _run_tree(roots[1], base / 'candidate')
    same_outcome = all(row['status'] == 'needs_repair' and row['check_status'] == 'failed'
                       for arm in (baseline, candidate) for row in arm['runs'])
    if (baseline['plan_digest'] != candidate['plan_digest']
            or baseline['subject_sha256'] != candidate['subject_sha256']):
        raise ValueError('Benchmark arms did not receive the same plan and source bytes')
    report = {'schema_version': 1,
              'workload': {'case_id': 'same-input-failed-check', 'attempts_per_arm': 5,
                           'outcome': 'intentional_failure',
                           'same_benign_input_and_plan': True},
              'python_executable': sys.executable,
              'baseline': baseline, 'candidate': candidate,
              'quality_gate': {'same_non_success_outcome': same_outcome,
                               'no_false_complete': same_outcome},
              'failure_policy_processes_avoided': baseline['observed_process_starts']
                  - candidate['observed_process_starts'] if same_outcome else None,
              'prompt_payload': {
                  'baseline_initial_bytes': baseline['prompt']['initial_bytes'],
                  'baseline_full_resend_bytes': baseline['prompt']['full_resend_bytes'],
                  'candidate_initial_bytes': candidate['prompt']['initial_bytes'],
                  'candidate_full_resend_bytes': candidate['prompt']['full_resend_bytes'],
                  'candidate_delta_bytes': candidate['prompt']['delta_bytes'],
                  'original_excerpts_intact': bool(candidate['prompt']['original_excerpts_intact'])},
              'live_model_tokens': None, 'actual_delivery_ms': None,
              'limits': 'Failed-check policy replay only; wall durations are raw local subprocess observations, not successful task or live-model savings.'}
    if not same_outcome:
        raise ValueError('Benchmark arms did not retain the same non-success outcome')
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        if args.output.exists():
            raise ValueError('Refusing to overwrite benchmark report')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == '__main__':
    try:
        if len(sys.argv) == 4 and sys.argv[1] == '--worker':
            print(json.dumps(_worker(Path(sys.argv[2]).resolve(),
                                     Path(sys.argv[3]).resolve()), ensure_ascii=False))
        else:
            raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
