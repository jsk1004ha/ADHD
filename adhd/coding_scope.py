"""Git audits run in the CLI; hooks only recheck their files and receipts."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

from .core import ROOT, digest, file_hash, read_json, safe_path
from .evidence import validate_execution


def _relative(value: str, *, repository: bool = False) -> str:
    if repository and value == '.':
        return value
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Scope needs a relative source path')
    value = value.replace('\\', '/')
    if (value.startswith('/') or ':' in value or any(c in value for c in '*?[]')
            or any(p in {'', '.', '..'} for p in value.rstrip('/').split('/'))):
        raise ValueError('Unsafe scope path: ' + value)
    return value


def _path(root: Path, relative: str) -> Path:
    if relative == '.':
        return root.resolve()
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Scope cannot cross a link or junction: ' + relative)
    return safe_path(root, relative)


def _sha(root: Path, relative: str) -> str | None:
    path = _path(root, relative)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('Scope source must be a regular file: ' + relative)
    return file_hash(path)


def _git(root: Path, *args: str) -> bytes:
    # Ignore ambient Git overrides and optional index refreshes; never run a shell.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                             '-c', 'core.untrackedCache=false', '-C', str(root), *args],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30, check=False)
    if result.returncode:
        raise ValueError('Git scope audit failed: ' + result.stderr.decode('utf-8', 'replace')[:500])
    return result.stdout


def _names(data: bytes) -> list[str]:
    return [p.decode('utf-8', 'surrogateescape') for p in data.split(b'\0') if p]


def _index(root: Path) -> dict[str, str]:
    result = {}
    for entry in _git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if entry:
            metadata, path = entry.split(b'\t', 1)
            name = path.decode('utf-8', 'surrogateescape')
            if name in result:
                raise ValueError('Resolve unmerged index entries before capturing scope')
            result[name] = metadata.decode('ascii')
    return result


def _changed(root: Path, base: str) -> set[str]:
    return set(_names(_git(root, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                          '--name-only', '-z', base, '--'))
               + _names(_git(root, 'diff', '--cached', '--no-ext-diff', '--no-textconv',
                             '--no-renames', '--name-only', '-z', base, '--'))
               + _names(_git(root, 'ls-files', '--others', '--exclude-standard', '-z')))


def validate_baseline(workspace: Path, baseline: dict) -> Path:
    if not isinstance(baseline, dict) or baseline.get('schema_version') != 1:
        raise ValueError('Invalid coding scope baseline')
    root = _path(workspace.resolve(), _relative(baseline.get('repository'), repository=True))
    allowed = baseline.get('allowed_paths')
    if (not root.is_dir() or not isinstance(allowed, list) or not 1 <= len(allowed) <= 100
            or any(not isinstance(p, str) for p in allowed)
            or len(set(allowed)) != len(allowed)):
        raise ValueError('Scope needs a repository and distinct allowed source paths')
    for path in allowed:
        if _relative(path) != path or path.split('/')[0] in {'.git', '.adhd', '.omx'}:
            raise ValueError('Invalid allowed source path')
    if (not isinstance(baseline.get('base_ref'), str)
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', baseline['base_ref'])
            or not isinstance(baseline.get('dirty_files'), dict)):
        raise ValueError('Scope needs a captured commit and existing-work manifest')
    for name, row in baseline['dirty_files'].items():
        _relative(name)
        if (not isinstance(row, dict) or set(row) != {'sha256', 'index'}
                or row['sha256'] is not None and (not isinstance(row['sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']))
                or row['index'] is not None and not isinstance(row['index'], str)):
            raise ValueError('Invalid existing-work entry')
    return root


def capture(workspace: Path, repository: str, allowed_paths: list[str]) -> dict:
    root = _path(workspace.resolve(), _relative(repository, repository=True))
    if Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != root:
        raise ValueError('Scope repository must be the Git checkout root')
    base = _git(root, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
    index = _index(root)
    baseline = {'schema_version': 1, 'repository': repository.replace('\\', '/'),
                'base_ref': base, 'allowed_paths': [_relative(p) for p in allowed_paths],
                'dirty_files': {p: {'sha256': _sha(root, p), 'index': index.get(p)}
                                for p in sorted(_changed(root, base))}}
    validate_baseline(workspace, baseline)
    return baseline


def _tree(root: Path, excluded: list[str]) -> list[str]:
    def ignored(path: str) -> bool:
        return any(path == p.rstrip('/') or p.endswith('/') and path.startswith(p) for p in excluded)
    names = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        prefix = Path(directory).relative_to(root).as_posix()
        prefix = '' if prefix == '.' else prefix + '/'
        dirs[:] = [d for d in dirs if not ignored(prefix + d + '/')]
        for name in [*dirs, *files]:
            if not ignored(prefix + name):
                _path(root, prefix + name)
        names.extend(prefix + f for f in files if not ignored(prefix + f))
        if len(names) > 20_000:
            raise ValueError('Scope inventory exceeds 20000 files')
    return sorted(names)


def _engine() -> dict[str, str]:
    return {name: file_hash(ROOT / name) for name in ('adhd.py', 'adhd/cli.py', 'adhd/coding_scope.py')}


def audit(workspace: Path, baseline: dict) -> dict:
    root = validate_baseline(workspace, baseline)
    if Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != root:
        raise ValueError('Scope repository changed')
    _git(root, 'cat-file', '-e', baseline['base_ref'] + '^{commit}')
    index = _index(root)
    for path, original in baseline['dirty_files'].items():
        if {'sha256': _sha(root, path), 'index': index.get(path)} != original:
            raise ValueError('Pre-existing work changed: ' + path)
    changes = sorted(_changed(root, baseline['base_ref']) - set(baseline['dirty_files']))
    outside = [p for p in changes if not any(p == a or a.endswith('/') and p.startswith(a)
                                           for a in baseline['allowed_paths'])]
    if outside:
        raise ValueError('Changes outside coding scope: ' + ', '.join(outside))
    visible = set(_names(_git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')))
    visible.update(baseline['dirty_files'])
    excluded = sorted(set(['.git/', '.adhd/', '.omx/'] + _names(
        _git(root, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z'))))
    metadata = {}
    head = _git(root, 'rev-parse', '--symbolic-full-name', 'HEAD').decode().strip()
    for name in dict.fromkeys(['HEAD', 'index', 'packed-refs', head] if head != 'HEAD'
                              else ['HEAD', 'index', 'packed-refs']):
        path = Path(_git(root, 'rev-parse', '--git-path', name).decode().strip())
        path = path if path.is_absolute() else root / path
        metadata[str(path.absolute())] = file_hash(path) if path.is_file() else None
    return {'schema_version': 1, 'baseline_digest': digest(baseline), 'engine': _engine(),
            'files': {p: _sha(root, p) for p in sorted(visible)}, 'changes': changes,
            'excluded_paths': excluded, 'tree': _tree(root, excluded), 'git_metadata': metadata}


def validate_report(workspace: Path, baseline: dict, report: dict) -> None:
    """Hook-safe: compare bytes and names only; never invoke Git here."""
    root = validate_baseline(workspace, baseline)
    if (report.get('schema_version') != 1 or report.get('baseline_digest') != digest(baseline)
            or report.get('engine') != _engine()):
        raise ValueError('Scope baseline or audit engine changed')
    if {p: _sha(root, p) for p in report['files']} != report['files']:
        raise ValueError('Audited source content changed')
    if _tree(root, report['excluded_paths']) != report['tree']:
        raise ValueError('Audited source inventory changed')
    for name, sha in report['git_metadata'].items():
        path = Path(name)
        if (file_hash(path) if path.is_file() else None) != sha:
            raise ValueError('Audited Git metadata changed')


def scope_repositories(workspace: Path, artifacts: list[str]) -> set[Path]:
    found = set()
    for relative in ['.', *artifacts]:
        path = _path(workspace.resolve(), relative)
        for parent in [path, *path.parents]:
            if not parent.is_relative_to(workspace.resolve()):
                break
            if (parent / '.git').exists():
                found.add(parent)
                break
    return found


def native_evidence(state: dict, evidence: dict, checked_path) -> tuple[dict, list[str]]:
    """Accept only an observed CLI verification of the pinned scope report."""
    workspace = Path(state['workspace'])
    scope = state['contract']['coding_scope']
    base_path = checked_path(workspace, scope['baseline'], existing=True)
    if file_hash(base_path) != scope['sha256']:
        raise ValueError('Coding scope baseline changed after begin')
    if not isinstance(evidence, dict) or set(evidence) != {'report', 'receipt', 'change_coverage'}:
        raise ValueError('Candidate needs coding_scope_evidence: report, receipt and change_coverage')
    report_path = checked_path(workspace, evidence['report'], existing=True)
    if report_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Coding scope report exceeds 2 MiB')
    report = read_json(report_path)
    baseline = read_json(base_path)
    root = validate_baseline(workspace, baseline)
    if scope_repositories(workspace, state['contract']['artifacts']) - {root}:
        raise ValueError('Coding artifacts must belong to the scoped repository')
    receipt = validate_execution(workspace, evidence['receipt'], run_id=state['run_id'],
                                 revision=state['intent_version'])
    argv = [str(Path(sys.executable).resolve()), str(ROOT / 'adhd.py'), 'coding-scope', 'verify',
            '--workspace', str(workspace), '--baseline', scope['baseline'], '--report', evidence['report']]
    if receipt['invocation']['argv'] != argv or not {scope['baseline'], evidence['report']} <= set(receipt['subject_files']):
        raise ValueError('Scope receipt must execute the pinned coding-scope verify command and bind its inputs')
    validate_report(workspace, baseline, report)
    coverage = evidence['change_coverage']
    known = {r['id'] for r in state['contract']['criteria']}
    seen = set()
    if not isinstance(coverage, list):
        raise ValueError('Scope change coverage must be an array')
    for row in coverage:
        if (not isinstance(row, dict) or row.get('path') not in report['changes'] or row['path'] in seen
                or not isinstance(row.get('requirements'), list) or not row['requirements']
                or not set(row['requirements']) <= known or not isinstance(row.get('reason'), str)
                or not row['reason'].strip() or len(row['reason']) > 1000):
            raise ValueError('Every scope change needs known requirement IDs and a concrete reason')
        seen.add(row['path'])
    if seen != set(report['changes']):
        raise ValueError('Scope change coverage is incomplete')
    files = [scope['baseline'], evidence['report'], evidence['receipt']]
    files += [(_path(root, p).relative_to(workspace)).as_posix() for p in report['changes']
              if report['files'].get(p) and _path(root, p).stat().st_size]
    proof = {'report_sha256': file_hash(report_path), 'receipt_sha256': file_hash(
        checked_path(workspace, evidence['receipt'], existing=True)), 'coverage': coverage}
    return proof, files
