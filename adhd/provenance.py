"""Validate declared research claim lineage against current local artifacts.

This checks hashes, graph links and declared numbers. It cannot discover every
claim in an arbitrary document or establish that an analysis was scientifically
sound; those remain independent review tasks.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any

from .core import file_hash
from .snapshots import _relative_file, _reject_path_links


ROLES = {'raw', 'external', 'analysis', 'result', 'visual', 'report'}
CATEGORIES = {'measured', 'calculated', 'interpreted', 'external', 'unverified'}
ANALYSIS_SUFFIXES = {'.py', '.r', '.jl', '.m', '.ipynb'}
NUMBERS = re.compile(r'(?<![\w.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?(?![\w.])')


def _stable_hash(path: Path) -> str:
    before = path.stat()
    result = file_hash(path)
    after = path.stat()
    if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
        raise ValueError('Provenance file changed during hashing: ' + str(path))
    return result


def _list(value: Any, limit: int, label: str) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f'{label} must be a bounded array')
    return value


def _pointer(value: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 300:
        raise ValueError('A result claim needs a JSON pointer')
    for token in pointer[1:].split('/'):
        token = token.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdecimal() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ValueError('Result JSON pointer does not exist')
    return value


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError('Claim/result value must be numeric')
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('Invalid numeric claim/result') from error
    if not number.is_finite():
        raise ValueError('Nonfinite numeric claims are not supported')
    return number


def _text(path: Path, role: str) -> str:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Claim text extraction exceeds the 32 MiB limit')
    if path.suffix.lower() in {'.txt', '.md', '.csv', '.tsv', '.html', '.htm', '.svg'}:
        return path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() in {'.docx', '.pptx', '.pdf', '.hwpx'}:
        from .documents import inspect_document
        result = inspect_document(path)
        if 'text' not in result:
            raise ValueError('No extractable claim text in presentation')
        return result['text']
    raise ValueError(f'Unsupported {role} text format')


def validate_provenance(workspace: Path, manifest_path: Path | str) -> dict:
    workspace = Path(workspace).resolve()
    manifest_file = Path(manifest_path)
    if manifest_file.is_absolute():
        if not manifest_file.is_file():
            raise ValueError('Provenance manifest leaves workspace')
        _reject_path_links(manifest_file, 'Provenance manifest cannot traverse links or junctions')
        manifest_file = manifest_file.resolve()
        try:
            relative = manifest_file.relative_to(workspace).as_posix()
        except ValueError as error:
            raise ValueError('Provenance manifest leaves workspace') from error
    else:
        relative = manifest_file.as_posix()
    manifest_file, relative = _relative_file(workspace, relative)
    if manifest_file.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Provenance manifest exceeds 2 MiB')
    manifest = json.loads(manifest_file.read_text(encoding='utf-8-sig'))
    if not isinstance(manifest, dict) or manifest.get('schema') != 1:
        raise ValueError('Unsupported provenance manifest schema')
    rows = _list(manifest.get('nodes'), 100, 'nodes')
    claims = _list(manifest.get('claims'), 200, 'claims')
    if not rows or not claims:
        raise ValueError('Provenance needs nodes and claims')
    nodes: dict[str, dict] = {}
    paths: dict[str, Path] = {}
    used_paths: set[Path] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('role') not in ROLES:
            raise ValueError('Invalid provenance node')
        node_id = row.get('id')
        if not isinstance(node_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', node_id) or node_id in nodes:
            raise ValueError('Duplicate/invalid provenance node ID')
        inputs = _list(row.get('inputs'), 30, 'node inputs')
        if len(set(inputs)) != len(inputs) or any(x not in nodes for x in inputs):
            raise ValueError('Provenance inputs must name earlier nodes')
        if row['role'] in {'raw', 'external'} and inputs:
            raise ValueError('A source node cannot depend on derived artifacts')
        if row['role'] == 'analysis' and not any(nodes[x]['role'] == 'raw' for x in inputs):
            raise ValueError('Analysis node needs a raw input')
        if row['role'] == 'result' and not any(nodes[x]['role'] == 'analysis' for x in inputs):
            raise ValueError('Result node needs analysis code')
        if row['role'] == 'visual' and not any(nodes[x]['role'] == 'result' for x in inputs):
            raise ValueError('Visual node needs a result input')
        if row['role'] == 'report' and not inputs:
            raise ValueError('Report node needs a source/result input')
        rel_path=row.get('path')
        if not isinstance(rel_path,str):
            raise ValueError('Provenance node needs a file path')
        path, _ = _relative_file(workspace, rel_path)
        if path in used_paths:
            raise ValueError('Provenance roles must use distinct artifact paths')
        if row['role'] == 'analysis' and (path.suffix.lower() not in ANALYSIS_SUFFIXES or path.stat().st_size == 0):
            raise ValueError('Analysis node needs a nonempty code artifact')
        expected = row.get('sha256')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected) or _stable_hash(path) != expected:
            raise ValueError('Provenance artifact hash changed: ' + node_id)
        nodes[node_id] = row
        paths[node_id] = path
        used_paths.add(path)

    def ancestors(node_id: str) -> set[str]:
        result = set(nodes[node_id]['inputs'])
        for parent in nodes[node_id]['inputs']:
            result.update(ancestors(parent))
        return result

    verified: list[str] = []
    declared_only: list[str] = []
    seen: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict) or claim.get('category') not in CATEGORIES:
            raise ValueError('Invalid claim category')
        cid = claim.get('id')
        if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', cid) or cid in seen:
            raise ValueError('Duplicate/invalid claim ID')
        seen.add(cid)
        output_id = claim.get('output')
        if output_id not in nodes or nodes[output_id]['role'] != 'report':
            raise ValueError('Claim output must name a report node')
        literal = claim.get('literal')
        if not isinstance(literal, str) or not literal or len(literal) > 1000 or literal not in _text(paths[output_id], 'report'):
            raise ValueError('Declared claim text is absent from current report: ' + cid)
        category = claim['category']
        if category in {'measured', 'calculated'}:
            result_id = claim.get('result')
            if result_id not in nodes or nodes[result_id]['role'] != 'result' or result_id not in ancestors(output_id):
                raise ValueError('Numeric claim lacks result-to-report lineage: ' + cid)
            lineage = ancestors(result_id)
            if not any(nodes[x]['role'] == 'analysis' for x in lineage):
                raise ValueError('Numeric claim needs analysis code: ' + cid)
            if not any(nodes[x]['role'] == 'raw' for x in lineage):
                raise ValueError('Measured/calculated claim needs raw data: ' + cid)
            result_path = paths[result_id]
            if result_path.suffix.lower() != '.json' or result_path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('Numeric result must be bounded JSON')
            result_data = json.loads(result_path.read_text(encoding='utf-8-sig'),
                                     parse_float=Decimal,parse_int=Decimal)
            pointer=claim.get('json_pointer')
            if not isinstance(pointer,str):
                raise ValueError('Numeric claim needs a result JSON pointer')
            source_value = _decimal(_pointer(result_data, pointer))
            claimed_value = _decimal(claim.get('value'))
            if source_value != claimed_value or claimed_value not in {_decimal(n) for n in NUMBERS.findall(literal)}:
                raise ValueError('Numeric claim differs from result data or rendered text: ' + cid)
            unit = claim.get('unit')
            if unit is not None and (not isinstance(unit, str) or not unit or unit not in literal):
                raise ValueError('Declared claim unit is missing from text: ' + cid)
            verified.append(cid)
        else:
            if category == 'external' and not any(nodes[x]['role'] == 'external' for x in ancestors(output_id)):
                raise ValueError('External claim needs a captured external source: ' + cid)
            declared_only.append(cid)
    return {'valid': True, 'manifest': relative, 'manifest_sha256': _stable_hash(manifest_file),
            'verified_claims': verified, 'declared_only_claims': declared_only,
            'node_count': len(nodes), 'node_paths': [node['path'] for node in rows],
            'analysis_paths': [node['path'] for node in rows if node['role'] == 'analysis'],
            'coverage': 'declared_claims_only',
            'limitation': 'Hash and number consistency do not prove scientific correctness, complete claim coverage or visual layout.'}
