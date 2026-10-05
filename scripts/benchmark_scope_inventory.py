"""Alternate a saved baseline and current inventory on identical warm inputs.

Before editing: copy adhd/coding_scope.py to an ignored evidence directory.
Then run from the repository root:
python -m scripts.benchmark_scope_inventory --baseline-file PATH --out PATH
The supplied baseline is trusted local Python source; only _path/_tree are loaded.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

from adhd import coding_scope


def baseline_tree(path: Path):
    source = path.read_bytes()
    parsed = ast.parse(source, filename=str(path))
    functions = [node for node in parsed.body
                 if isinstance(node, ast.FunctionDef) and node.name in {'_path', '_tree'}]
    if {node.name for node in functions} != {'_path', '_tree'}:
        raise ValueError('Baseline must define _path and _tree')
    namespace = vars(coding_scope).copy()
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['_tree'], hashlib.sha256(source).hexdigest()


def measure(name, root, excluded, baseline, rounds):
    expected = baseline(root, excluded)
    if coding_scope._tree(root, excluded) != expected:
        raise AssertionError('Inventory differs from baseline: ' + name)
    samples = {'baseline': [], 'optimized': []}
    functions = {'baseline': baseline, 'optimized': coding_scope._tree}
    for index in range(rounds):
        order = ['baseline', 'optimized'] if index % 2 == 0 else ['optimized', 'baseline']
        for label in order:
            started = time.perf_counter()
            actual = functions[label](root, excluded)
            samples[label].append(time.perf_counter() - started)
            if actual != expected:
                raise AssertionError('Inventory changed during measurement: ' + name)
    medians = {label: statistics.median(values) for label, values in samples.items()}
    return {'workload': name, 'files': len(expected), 'exclusions': len(excluded),
            'inventory_equal': True, 'samples_seconds': samples,
            'median_seconds': medians,
            'speedup': medians['baseline'] / medians['optimized'],
            'latency_reduction_percent': 100 * (1 - medians['optimized'] / medians['baseline'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-file', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=7)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    args = parser.parse_args()
    if not 3 <= args.rounds <= 30:
        parser.error('--rounds must be 3..30')
    baseline, baseline_hash = baseline_tree(args.baseline_file)
    repository = args.repository.resolve()
    excluded = sorted(set(['.git/', '.adhd/', '.omx/'] + coding_scope._names(
        coding_scope._git(repository, 'ls-files', '--others', '--ignored',
                          '--exclude-standard', '--directory', '-z'))))
    results = [measure('repository', repository, excluded, baseline, args.rounds)]
    with tempfile.TemporaryDirectory(prefix='adhd-inventory-benchmark-') as folder:
        root = Path(folder).resolve()
        for label, files, depth, ignores in [('wide', 2000, 1, 0),
                                             ('deep', 640, 8, 0),
                                             ('ignored-heavy', 800, 2, 400)]:
            work = root / label
            work.mkdir()
            for index in range(files):
                directory = work / f'group-{index % 16}'
                for level in range(depth - 1):
                    directory /= f'level-{level}'
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f'file-{index}.py').write_bytes(b'')
            excluded = []
            for index in range(ignores):
                directory = work / f'ignored-{index}'
                directory.mkdir()
                (directory / 'hidden.py').write_bytes(b'')
                excluded.append(directory.name + '/')
            results.append(measure(label, work, excluded, baseline, args.rounds))
    report = {'schema_version': 1, 'environment': {'python': sys.version,
              'platform': platform.platform()}, 'conditions': {
              'cache': 'one warm-up per implementation; OS cache not flushed',
              'rounds': args.rounds, 'concurrency': 1, 'order': 'alternating',
              'scope': '_tree inventory only; not total agent/task time'},
              'baseline_source_sha256': baseline_hash,
              'optimized_source_sha256': hashlib.sha256(
                  Path(coding_scope.__file__).read_bytes()).hexdigest(),
              'results': results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'out': str(args.out), 'results': [
        {k: row[k] for k in ('workload', 'files', 'exclusions', 'median_seconds',
                             'speedup', 'inventory_equal')} for row in results]}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
