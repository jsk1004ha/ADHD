"""Isolated subprocess benchmark; never installs hooks or changes a checkout.

Example (new worktree files must be staged so WORKTREE includes them):
python -m scripts.benchmark_hooks --ref baseline=2d5c443 --ref A=COMMIT \
    --ref B=WORKTREE --rounds 20 --warmups 2 --out .adhd/hooks.json
Or supply already prepared trusted source trees with --tree LABEL=PATH.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
from time import perf_counter_ns

REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures/hooks/events.json'
TEXT_FIELDS = ('reason', 'systemMessage', 'stopReason')


def sizes(out):
    texts = [out[name] for name in TEXT_FIELDS if isinstance(out.get(name), str)]
    specific = out.get('hookSpecificOutput', {})
    texts += [specific[name] for name in ('additionalContext', 'permissionDecisionReason')
              if isinstance(specific.get(name), str)]
    return {'emitted_context_chars': sum(map(len, texts)),
            'emitted_context_bytes': sum(len(value.encode('utf-8')) for value in texts)}


def summary(values):
    ordered = sorted(values)
    return {'median': statistics.median(ordered),
            'p95': ordered[math.ceil(len(ordered) * .95) - 1]}


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPOSITORY), *args])


def materialize(ref, target):
    target.mkdir()
    if ref == 'WORKTREE':
        # Only tracked/staged source; existing untracked user outputs stay private.
        paths = git('ls-files', '-z').decode('utf-8').split('\0')
        for name in filter(None, paths):
            source = REPOSITORY / name
            if source.is_file():
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        return 'WORKTREE at ' + git('rev-parse', 'HEAD').decode().strip()
    commit = git('rev-parse', ref + '^{commit}').decode().strip()
    with tarfile.open(fileobj=io.BytesIO(git('archive', commit))) as archive:
        archive.extractall(target, filter='data')
    return commit


def invoke(root, environment, event):
    command = [sys.executable, str(root / 'hook.py')]
    started = perf_counter_ns()
    process = subprocess.run(command, input=json.dumps(event).encode('utf-8'),
                             capture_output=True, cwd=root, env=environment, timeout=30)
    elapsed = perf_counter_ns() - started
    if process.returncode or process.stderr:
        raise RuntimeError('Hook failed: ' + process.stderr.decode('utf-8', errors='replace'))
    output = json.loads(process.stdout)
    if 'systemMessage' in output:
        raise RuntimeError('Unexpected hook validation error: ' + output['systemMessage'])
    return elapsed, output, len(process.stdout)


def sample(root, enabled, fixture, scenario, temporary):
    # Each measurement starts from fresh isolated state; setup is not timed.
    with tempfile.TemporaryDirectory(dir=temporary, prefix='case-') as directory:
        base = Path(directory)
        workspace = base / 'workspace'
        workspace.mkdir()
        environment = {**os.environ, 'CODEX_HOME': str(base / 'codex'),
                       'ADHD_HOME': str(base / 'codex/adhd'), 'ADHD_EXEC_OWNER': '',
                       'ADHD_HOOK_DIAGNOSTICS': '1' if enabled else '0',
                       'PYTHONUTF8': '1', 'PYTHONPATH': str(root)}
        event = {'session_id': 'hook-benchmark', 'cwd': str(workspace), 'turn_id': 'setup-session',
                 'hook_event_name': 'SessionStart', 'source': 'startup',
                 'model': 'gpt-6-sol', 'permission_mode': 'default', 'transcript_path': None}
        invoke(root, environment, event)
        invoke(root, environment, {**event, 'hook_event_name': 'UserPromptSubmit',
                                  'turn_id': 'setup-prompt', 'prompt': fixture['prompt']})
        if scenario['initial'] == 'active' or scenario.get('queue_begin'):
            (workspace / 'begin.json').write_text(json.dumps(fixture['begin']), encoding='utf-8')
            queued = subprocess.run([sys.executable, str(root / 'adhd.py'), 'native', 'begin',
                '--session', hashlib.sha256(json.dumps('hook-benchmark', ensure_ascii=False,
                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24],
                '--workspace', str(workspace), '--payload-file', str(workspace / 'begin.json')],
                capture_output=True, env=environment, cwd=root, timeout=30)
            if queued.returncode:
                raise RuntimeError(queued.stderr.decode('utf-8', errors='replace'))
            if scenario['initial'] == 'active':
                invoke(root, environment, {**event, 'hook_event_name': 'PostToolUse',
                                          'turn_id': 'setup-begin', 'tool_name': 'exec_command'})
        ledger_root = base / 'codex/adhd/native'
        ledger_positions = {}
        if enabled:
            for path in ledger_root.glob('*/ledger.jsonl'):
                info = path.stat()
                ledger_positions[path] = (info.st_ino, info.st_size)
        elapsed, out, byte_count = invoke(root, environment,
                                         {**event, 'turn_id': 'measured', **scenario['event']})
        state_files = list((base / 'codex/adhd/native').glob('*/state.json'))
        if len(state_files) != 1:
            raise AssertionError('Missing/ambiguous isolated state')
        state = json.loads(state_files[0].read_text(encoding='utf-8'))
        expected_status = 'working' if scenario['initial'] == 'active' or scenario.get('queue_begin') else 'idle'
        if state['status'] != expected_status or state['prompts'][0]['text'] != fixture['prompt']:
            raise AssertionError('Processing/original intent differs from fixture')
        if scenario.get('queue_begin'):
            acknowledgements = list((workspace / '.adhd/bridge').glob('*/outbox/*.json'))
            if len(acknowledgements) != 1 or not json.loads(acknowledgements[0].read_text())['ok']:
                raise AssertionError('CLI begin was not acknowledged successfully')
            if not state['processed'] or not list((workspace / '.adhd/bridge').glob('*/processed/*.json')):
                raise AssertionError('CLI begin processing record missing')
        observations = []
        if enabled:
            for path in ledger_root.glob('*/ledger.jsonl'):
                with path.open('rb') as ledger:
                    previous = ledger_positions.get(path)
                    # A rotated/replaced ledger contains new records from offset zero.
                    if previous and os.fstat(ledger.fileno()).st_ino == previous[0]:
                        ledger.seek(previous[1])
                    observations.extend(json.loads(line) for line in ledger)
        diagnostic = next((row for row in reversed(observations)
                           if row['event'] == 'hook_diagnostics'), None) if enabled else None
        if diagnostic and diagnostic['stdout_json_bytes'] != byte_count:
            raise AssertionError('Internal/external output byte count mismatch')
        return {'process_elapsed_ns': elapsed, **sizes(out), 'stdout_json_bytes': byte_count,
                'processing_verified': True, 'diagnostic': diagnostic}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', action='append', default=[], metavar='LABEL=REF')
    parser.add_argument('--tree', action='append', default=[], metavar='LABEL=PATH')
    parser.add_argument('--fixture', type=Path, default=FIXTURES)
    parser.add_argument('--scenario', action='append')
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--warmups', type=int, default=2)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.ref and not args.tree:
        parser.error('Provide --ref or --tree')
    if not 1 <= args.rounds <= 100 or not 0 <= args.warmups <= 10:
        parser.error('rounds must be 1..100 and warmups 0..10')
    fixture = json.loads(args.fixture.read_text(encoding='utf-8'))
    scenarios = [row for row in fixture['scenarios']
                 if not args.scenario or row['name'] in args.scenario]
    if not scenarios:
        parser.error('No matching scenarios')
    with tempfile.TemporaryDirectory(prefix='adhd-hook-benchmark-') as directory:
        temporary = Path(directory)
        sources = []
        for item in args.ref:
            label, ref = item.split('=', 1)
            root = temporary / ('source-' + str(len(sources)).zfill(2))
            identity = materialize(ref, root)
            sources.append((label, root, identity))
        for item in args.tree:
            label, path = item.split('=', 1)
            sources.append((label, Path(path).resolve(), 'provided trusted source tree'))
        labels = [row[0] for row in sources]
        if len(set(labels)) != len(labels):
            parser.error('Labels must be unique')
        versions = [{ 'label': label, 'identity': identity,
                      'sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                 for name in ('hook.py', 'adhd/native.py', 'adhd/hook_diagnostics.py')
                                 if (root / name).is_file()}}
                    for label, root, identity in sources]
        samples = {(label, enabled, row['name']): [] for label in labels
                   for enabled in (False, True) for row in scenarios}
        for scenario in scenarios:
            for iteration in range(args.warmups + args.rounds):
                # Alternate source and mode order to avoid one-sided warming bias.
                order = sources if iteration % 2 == 0 else list(reversed(sources))
                for label, root, _ in order:
                    for enabled in ((False, True) if iteration % 2 == 0 else (True, False)):
                        observed = sample(root, enabled, fixture, scenario, temporary)
                        if iteration >= args.warmups:
                            samples[label, enabled, scenario['name']].append(observed)
        results = []
        for (label, enabled, name), rows in samples.items():
            metrics = {metric: summary([row[metric] for row in rows]) for metric in
                       ('process_elapsed_ns', 'emitted_context_chars', 'emitted_context_bytes', 'stdout_json_bytes')}
            results.append({'version': label, 'diagnostics_requested': enabled,
                            'diagnostics_observed': all(row['diagnostic'] is not None for row in rows),
                            'scenario': name, 'metrics': metrics, 'samples': rows})
        # Resolve vendored version in the actual source tree, without importing it in this process.
        dependency = subprocess.check_output([sys.executable, '-c',
            'from adhd import dependencies; import filelock; print(filelock.__version__)'],
            cwd=sources[-1][1], env={**os.environ, 'PYTHONPATH': str(sources[-1][1])}).decode().strip()
        report = {'schema_version': 1, 'environment': {'os': platform.platform(),
                  'python': sys.version, 'python_executable': sys.executable,
                  'filelock': dependency, 'timer': 'time.perf_counter_ns',
                  'command': [sys.executable, '-m', 'scripts.benchmark_hooks', *(argv or sys.argv[1:])]},
                  'rounds': args.rounds, 'warmups': args.warmups,
                  'initial_conditions': 'Fresh temp CODEX_HOME/ADHD_HOME/workspace per sample; startup+original prompt, optional CLI begin/PostToolUse. Setup excluded; subprocess imports warm after warmups.',
                  'process_scope': 'Before subprocess.run to after process exit/output collection; includes interpreter, imports, native processing, stdout and diagnostic ledger append.',
                  'p95_method': 'nearest rank ceil(.95*n)', 'model_usage': None,
                  'fixture_sha256': hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
                  'versions': versions, 'results': results}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        for row in results:
            print(row['version'], 'on' if row['diagnostics_requested'] else 'off', row['scenario'],
                  'median_ms=' + str(round(row['metrics']['process_elapsed_ns']['median'] / 1e6, 3)),
                  'chars=' + str(row['metrics']['emitted_context_chars']['median']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
