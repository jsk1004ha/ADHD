"""Reusable unittest-report CLI and dedicated stdio MCP; Python standard library only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

FIELDS = ('file', 'status', 'tests', 'failures', 'errors', 'skipped', 'seconds')
RUN = re.compile(r'^Ran (\d+) tests? in ([0-9]+(?:\.[0-9]+)?)s$')
END = re.compile(r'^(OK|FAILED)(?: \(([^)]*)\))?$')


def parse_log(data: bytes) -> dict:
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    lines = data.decode(encoding).splitlines()
    last = dict(status='unrecognized', tests=None, failures=0, errors=0, skipped=0, seconds=None)
    for i, line in enumerate(lines):
        match = RUN.fullmatch(line.strip())
        if not match:
            continue
        following = next((s.strip() for s in lines[i + 1:] if s.strip()), '')
        terminal = END.fullmatch(following)
        if not terminal:
            continue
        counts = dict(failures=0, errors=0, skipped=0)
        for name, value in re.findall(r'\b(failures|errors|skipped)=(\d+)\b', terminal[2] or ''):
            counts[name] = int(value)
        last = dict(status='passed' if terminal[1] == 'OK' else 'failed',
                    tests=int(match[1]), **counts, seconds=float(match[2]))
    return last


def build_report(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    input_dir = (manifest_path.parent / 'inputs').resolve()
    rows, seen = [], set()
    for entry in manifest['inputs']:
        filename = entry['file']
        if not isinstance(filename, str) or Path(filename).name != filename or filename in seen:
            raise ValueError('Manifest filenames must be unique basenames')
        seen.add(filename)
        source = (input_dir / filename).resolve()
        if source.parent != input_dir:
            raise ValueError('Input must remain inside inputs directory')
        raw = source.read_bytes()
        if len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError(f'Input identity mismatch: {filename}')
        rows.append(dict(file=filename, **parse_log(raw)))
    rows.sort(key=lambda row: row['file'])
    summary = dict(files=len(rows), passed_runs=sum(r['status'] == 'passed' for r in rows),
                   failed_runs=sum(r['status'] == 'failed' for r in rows),
                   unrecognized_runs=sum(r['status'] == 'unrecognized' for r in rows),
                   test_executions=sum(r['tests'] or 0 for r in rows),
                   **{key: sum(r[key] for r in rows) for key in ('failures', 'errors', 'skipped')})
    return dict(schema_version=1, runs=rows, summary=summary)


def markdown(report: dict) -> str:
    rows, s = report['runs'], report['summary']
    lines = ['# ADHD unittest 검증 로그 보고서', '',
             f"로그 {s['files']}개: 통과 {s['passed_runs']}개, 실패 {s['failed_runs']}개, 인식 불가 {s['unrecognized_runs']}개.",
             f"과거 테스트 실행 {s['test_executions']}회: 실패 {s['failures']}건, 오류 {s['errors']}건, 건너뜀 {s['skipped']}건.", '']
    for status, label in [('failed', '실패 파일'), ('unrecognized', '인식 불가 파일')]:
        names = ', '.join(r['file'] for r in rows if r['status'] == status) or '없음'
        lines.append(f'- {label}: {names}')
    lines += ['', '| 파일 | 상태 | 테스트 | 실패 | 오류 | 건너뜀 | 초 |',
              '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for row in rows:
        cells = [('—' if row[k] is None else str(row[k])).replace('|', '\\|') for k in FIELDS]
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines += ['', '테스트 수는 과거 로그의 실행 횟수 합계이며, 중복 실행이 포함될 수 있습니다. 고유 테스트 수나 이번에 새로 실행한 검사 수가 아닙니다.', '']
    return '\n'.join(lines)


def generate_report(manifest_path: str, output_dir: str) -> dict:
    manifest = Path(manifest_path).resolve(strict=True)
    output = Path(output_dir).resolve()
    inputs = (manifest.parent / 'inputs').resolve()
    if output == inputs or inputs in output.parents:
        raise ValueError('Output directory must be separate from inputs')
    report = build_report(manifest)
    output.mkdir(parents=True, exist_ok=True)
    json_path, md_path = output / 'report.json', output / 'report.md'
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    md_path.write_text(markdown(report), encoding='utf-8')
    # Ship a standalone copy and data-only replay arguments for repeat execution.
    script = output / 'generate_report.py'
    if script.resolve() != Path(__file__).resolve():
        script.write_bytes(Path(__file__).read_bytes())
    (output / 'replay.json').write_text(json.dumps({'manifest_path': str(manifest), 'output_dir': str(output)}), encoding='utf-8')
    return {'summary': report['summary'], 'artifacts': [str(p) for p in (json_path, md_path, script)],
            'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (json_path, md_path)},
            'input_hashes_verified': True}


TOOL = {'name': 'generate_unittest_report',
        'description': 'Parse last complete unittest summaries from hash-verified manifest inputs; save JSON, Korean Markdown and standalone replay generator. Returns compact totals and artifact paths.',
        'inputSchema': {'type': 'object', 'properties': {'manifest_path': {'type': 'string'}, 'output_dir': {'type': 'string'}},
                        'required': ['manifest_path', 'output_dir'], 'additionalProperties': False}}


def serve() -> None:
    """A deliberately narrow MCP server: no arbitrary code or shell tool."""
    initialized = False
    versions = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25', '2026-07-28')
    for raw in sys.stdin:
        request = None
        try:
            request = json.loads(raw)
            method, params = request['method'], request.get('params', {})
            if 'id' not in request:
                if method == 'notifications/initialized':
                    initialized = True
                continue
            if method == 'initialize':
                version = params.get('protocolVersion')
                result = {'protocolVersion': version if version in versions else versions[-1],
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'unittest-report', 'version': '1.0.0'}}
            elif method == 'ping':
                result = {}
            elif not initialized:
                raise ValueError('Initialize the MCP session first')
            elif method == 'tools/list':
                result = {'tools': [TOOL]}
            elif method == 'tools/call':
                if params.get('name') != TOOL['name']:
                    raise ValueError('Unknown tool')
                try:
                    payload = generate_report(**params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
                              'structuredContent': payload, 'isError': False}
                except (OSError, ValueError, KeyError, TypeError) as error:
                    result = {'content': [{'type': 'text', 'text': str(error)}], 'isError': True}
            else:
                raise ValueError('Unsupported method')
            response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except (ValueError, KeyError, TypeError) as error:
            response = {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                        'error': {'code': -32600, 'message': str(error)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdio', action='store_true')
    parser.add_argument('--manifest')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.stdio:
        serve()
        return
    if args.manifest and args.output:
        arguments = {'manifest_path': args.manifest, 'output_dir': args.output}
    elif not args.manifest and not args.output:
        arguments = json.loads(Path(__file__).with_name('replay.json').read_text(encoding='utf-8'))
    else:
        parser.error('--manifest and --output must be supplied together')
    print(json.dumps(generate_report(**arguments), ensure_ascii=False))


if __name__ == '__main__':
    main()
