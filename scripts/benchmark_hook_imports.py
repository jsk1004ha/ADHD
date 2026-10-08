"""Alternate frozen versions; measure whole hooks and first optional-validator use.

All state is temporary. Setup is excluded. No installation, network or model calls.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile

from scripts.benchmark_hooks import FIXTURES, invoke, materialize, sample, sizes


def distribution(values):
    q1, _, q3 = statistics.quantiles(values, n=4, method='inclusive') if len(values) > 1 else [values[0]] * 3
    return {'n': len(values), 'median': statistics.median(values), 'q1': q1, 'q3': q3, 'iqr': q3 - q1}


def cli(root, environment, *arguments):
    process = subprocess.run([sys.executable, str(root / 'adhd.py'), *arguments], cwd=root,
                             env=environment, capture_output=True, timeout=30)
    if process.returncode:
        raise RuntimeError(process.stderr.decode('utf-8', errors='replace'))
    return json.loads(process.stdout)


def first_candidate(root, enabled, fixture, scenario, temporary):
    with tempfile.TemporaryDirectory(dir=temporary, prefix='case-') as directory:
        base = Path(directory)
        workspace = base / 'workspace'; workspace.mkdir()
        environment = {**os.environ, 'CODEX_HOME': str(base / 'codex'),
                       'ADHD_HOME': str(base / 'codex/adhd'), 'ADHD_EXEC_OWNER': '',
                       'ADHD_HOOK_DIAGNOSTICS': '1' if enabled else '0',
                       'PYTHONUTF8': '1', 'PYTHONPATH': str(root)}
        event = dict(session_id='hook-benchmark', cwd=str(workspace), turn_id='setup-session',
                     hook_event_name='SessionStart', source='startup', model='gpt-6-sol',
                     permission_mode='default', transcript_path=None)
        invoke(root, environment, event)
        invoke(root, environment, {**event, 'turn_id': 'setup-prompt',
                                  'hook_event_name': 'UserPromptSubmit', 'prompt': fixture['prompt']})
        key = hashlib.sha256(json.dumps('hook-benchmark', ensure_ascii=False,
                         sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
        provenance = scenario['name'] == 'first_provenance_candidate'
        begin = copy.deepcopy(fixture['begin'])
        begin['mode'] = 'research' if provenance else 'report'
        begin['criteria'][0]['kind'] = 'provenance' if provenance else 'behavior'
        begin['artifacts'] = ['report.md' if provenance else 'result.txt']
        (workspace / 'begin.json').write_text(json.dumps(begin), encoding='utf-8')
        queued_begin = cli(root, environment, 'native', 'begin', '--session', key,
                           '--workspace', str(workspace), '--payload-file', str(workspace / 'begin.json'))
        invoke(root, environment, {**event, 'turn_id': 'setup-begin',
                                  'hook_event_name': 'PostToolUse', 'tool_name': 'exec_command'})
        assert json.loads(Path(queued_begin['receipt']).read_text())['ok']
        state_path = base / 'codex/adhd/native' / key / 'state.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        candidate = {'files': begin['artifacts'],
                     'criterion_results': [{'id': 'R1', 'pass': True, 'evidence': 'Fixture verified'}]}
        if provenance:
            files = {'raw.csv': '1,2\n', 'result.json': '{"mean":1.5}\n', 'report.md': 'Mean 1.5 kg\n',
                     'analysis.py': 'import json\nfrom pathlib import Path\nraw=[float(x) for x in Path("raw.csv").read_text().strip().split(",")]\nassert json.loads(Path("result.json").read_text())["mean"]==sum(raw)/len(raw)\n'}
            for name, text in files.items():
                (workspace / name).write_text(text, encoding='utf-8')
            nodes = [(name, role, inputs) for name, role, inputs in (
                ('raw.csv', 'raw', []), ('analysis.py', 'analysis', ['raw']),
                ('result.json', 'result', ['raw', 'code']), ('report.md', 'report', ['result']))]
            manifest = {'schema': 1, 'nodes': [dict(id=node_id, role=role, path=name,
                sha256=hashlib.sha256((workspace / name).read_bytes()).hexdigest(), inputs=inputs)
                for node_id, (name, role, inputs) in zip(('raw', 'code', 'result', 'report'), nodes)],
                'claims': [dict(id='mean', category='calculated', result='result',
                   json_pointer='/mean', output='report', literal='1.5 kg', value='1.5', unit='kg')]}
            (workspace / 'provenance.json').write_text(json.dumps(manifest), encoding='utf-8')
            spec = dict(run_id=state['run_id'], contract_revision=state['intent_version'],
                        subject_paths=list(files), argv=[sys.executable, str(workspace / 'analysis.py')])
            (workspace / 'check.json').write_text(json.dumps(spec), encoding='utf-8')
            check = cli(root, environment, 'check', '--workspace', str(workspace),
                        '--spec-file', str(workspace / 'check.json'))
            assert check['exit_code'] == 0 and check['inputs_unchanged']
            candidate['criterion_results'][0]['evidence_ids'] = [check['receipt']]
            candidate['provenance_manifests'] = [dict(criterion_id='R1', manifest='provenance.json')]
        else:
            (workspace / 'result.txt').write_text('verified fixture result', encoding='utf-8')
        (workspace / 'candidate.json').write_text(json.dumps(candidate), encoding='utf-8')
        queued = cli(root, environment, 'native', 'candidate', '--session', key,
                     '--workspace', str(workspace), '--payload-file', str(workspace / 'candidate.json'))
        ledger_path = state_path.parent / 'ledger.jsonl'
        position = ledger_path.stat()
        elapsed, out, byte_count = invoke(root, environment, {**event, 'turn_id': 'measured',
                                            'hook_event_name': 'PostToolUse', 'tool_name': 'exec_command'})
        state = json.loads(state_path.read_text(encoding='utf-8'))
        assert state['status'] == 'reviewing' and state['prompts'][0]['text'] == fixture['prompt']
        assert json.loads(Path(queued['receipt']).read_text())['ok']
        assert queued['queued'] in state['processed']
        assert (workspace / '.adhd/bridge' / key / 'processed' / (queued['queued'] + '.json')).is_file()
        if provenance:
            assert state['candidate']['provenance_evidence'][0]['verified_claims'] == ['mean']
        diagnostic = None
        if enabled:
            with ledger_path.open('rb') as ledger:
                if os.fstat(ledger.fileno()).st_ino == position.st_ino:
                    ledger.seek(position.st_size)
                observations = [json.loads(line) for line in ledger]
            diagnostic = next((row for row in reversed(observations)
                               if row['event'] == 'hook_diagnostics'), None)
            assert diagnostic and diagnostic['stdout_json_bytes'] == byte_count
        return {'process_elapsed_ns': elapsed, **sizes(out), 'stdout_json_bytes': byte_count,
                'processing_verified': True, 'first_use_verified': True, 'diagnostic': diagnostic}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', action='append', required=True, metavar='LABEL=COMMIT')
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--warmups', type=int, default=2)
    parser.add_argument('--scenario', action='append')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.rounds <= 100 or not 0 <= args.warmups <= 10:
        parser.error('rounds 1..100; warmups 0..10')
    fixture = json.loads(FIXTURES.read_text(encoding='utf-8'))
    scenarios = [dict(name='idle_startup', initial='idle', event=dict(hook_event_name='SessionStart', source='startup')),
                 *fixture['scenarios'], dict(name='first_snapshot_candidate'), dict(name='first_provenance_candidate')]
    scenarios = [row for row in scenarios if not args.scenario or row['name'] in args.scenario]
    if not scenarios:
        parser.error('No scenarios selected')
    with tempfile.TemporaryDirectory(prefix='adhd-lazy-benchmark-') as directory:
        temporary = Path(directory)
        sources = []
        for item in args.ref:
            label, ref = item.split('=', 1)
            if ref == 'WORKTREE':
                parser.error('Use a frozen commit, not WORKTREE')
            root = temporary / ('source-' + str(len(sources)).zfill(2))
            identity = materialize(ref, root)
            sources.append((label, root, identity))
        if len(set(label for label, _, _ in sources)) != len(sources):
            parser.error('Duplicate label')
        versions = [dict(label=label, identity=identity,
            sha256={name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
                    ('hook.py', 'adhd/native.py', 'scripts/benchmark_hooks.py')})
                    for label, root, identity in sources]
        rows, order_log = [], []
        for scenario in scenarios:
            for iteration in range(args.warmups + args.rounds):
                order = sources if iteration % 2 == 0 else list(reversed(sources))
                for label, root, _ in order:
                    for enabled in ((False, True) if iteration % 2 == 0 else (True, False)):
                        function = first_candidate if scenario['name'].startswith('first_') else sample
                        observed = function(root, enabled, fixture, scenario, temporary)
                        entry = dict(version=label, diagnostics_requested=enabled,
                                     scenario=scenario['name'], iteration=iteration,
                                     warmup=iteration < args.warmups)
                        order_log.append(entry)
                        if not entry['warmup']:
                            rows.append({**entry, **observed})
            print('finished', scenario['name'], flush=True)
        results = []
        for scenario in scenarios:
            for label, _, _ in sources:
                for enabled in (False, True):
                    group = [row for row in rows if row['scenario'] == scenario['name'] and
                             row['version'] == label and row['diagnostics_requested'] == enabled]
                    assert len(group) == args.rounds
                    metrics = {name: distribution([row[name] for row in group]) for name in
                               ('process_elapsed_ns', 'emitted_context_chars', 'stdout_json_bytes')}
                    if enabled:
                        assert all(row['diagnostic'] is not None for row in group)
                        for phase in ('native_import', 'inbox', 'dispatch'):
                            values = [row['diagnostic']['phases_ns'].get(phase) for row in group]
                            if all(value is not None for value in values):
                                metrics[phase + '_ns'] = distribution(values)
                    results.append(dict(version=label, scenario=scenario['name'], diagnostics_requested=enabled,
                                        diagnostics_observed=all(row['diagnostic'] is not None for row in group), metrics=metrics))
        report = dict(schema=1, environment=dict(os=platform.platform(), python=sys.version,
            executable=sys.executable, timer='perf_counter_ns'), argv=sys.argv,
            rounds=args.rounds, warmups=args.warmups, versions=versions,
            fixture_sha256=hashlib.sha256(FIXTURES.read_bytes()).hexdigest(),
            benchmark_driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            quartiles='statistics.quantiles(n=4, method=inclusive); IQR=Q3-Q1',
            conditions='Fresh temp state and hook process per sample; OS/source caches warmed by two excluded rounds. Setup CLI/hooks excluded; deferred first-use work is inside the measured PostToolUse. Versions and on/off order alternate per iteration. No models or remote calls.',
            source_order=order_log, results=results, samples=rows)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'out': str(args.out), 'samples': len(rows), 'conditions': len(results)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
