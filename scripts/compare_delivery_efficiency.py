"""Compare fixed baseline/candidate observations without claiming live savings.

Usage: python scripts/compare_delivery_efficiency.py --baseline base.json --candidate new.json
Each input is an array of records with a unique case_id. Replays and actual
provider-usage runs should be kept in separate input files and reports.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adhd.run_metrics import compare_paired_runs


def _rows(path: Path) -> dict[str, dict]:
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Comparison input must be an existing bounded JSON file')
    values = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(values, list) or not values:
        raise ValueError('Comparison input must contain observed run rows')
    result = {}
    for row in values:
        if not isinstance(row, dict) or not isinstance(row.get('case_id'), str) or not row['case_id']:
            raise ValueError('Every run needs a case_id')
        if row['case_id'] in result:
            raise ValueError('Duplicate case_id: ' + row['case_id'])
        result[row['case_id']] = row
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Deterministic observed paired-run comparison')
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    baseline, candidate = _rows(args.baseline), _rows(args.candidate)
    if set(baseline) != set(candidate):
        raise ValueError('Baseline/candidate case IDs differ')
    pairs = [{'baseline': baseline[key], 'candidate': candidate[key]} for key in sorted(baseline)]
    result = compare_paired_runs(pairs)
    result['case_ids'] = sorted(baseline)
    result['limitations'] = ('Observed fields only; deterministic fixture results do not measure '
                             'live model tokens, wall time or causal savings.')
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        if args.output.exists():
            raise ValueError('Refusing to overwrite a comparison report')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
