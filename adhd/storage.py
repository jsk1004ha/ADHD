"""Explicit cleanup of exact, unreferenced immutable native releases."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tomllib
import uuid

from . import dependencies as _dependencies  # Activate the shipped offline libraries first.
from filelock import FileLock

from .core import ROOT, file_hash, home, store
from .native_install import _find_managed_installation, _validate_release_record

RELEASE_NAME = re.compile(r'^(\d+\.\d+\.\d+)-(\d{8}-\d{6})-[0-9a-f]{8}$')
MAX_REFERENCE_BYTES = 2 * 1024 * 1024


def _link(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        return True
    try:
        attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _read(path: Path) -> str:
    if any(_link(part) for part in (path, *path.parents)):
        raise ValueError('Unsafe storage reference: ' + str(path))
    if path.stat().st_size > MAX_REFERENCE_BYTES:
        raise ValueError('Storage reference exceeds bounded scan: ' + str(path))
    return path.read_text(encoding='utf-8-sig')


def _records(codex: Path, parent: Path, base: Path) -> dict[str, dict]:
    records = {}
    paths = [parent / 'native-installation.json', *sorted(parent.glob('native-uninstalled-*.json'))]
    for path in paths:
        row = json.loads(_read(path))
        if (not isinstance(row, dict) or not isinstance(row.get('release'), str)
                or not isinstance(row.get('target'), str)
                or not isinstance(row.get('release_identity'), dict)):
            raise ValueError('Invalid release ownership record: ' + str(path))
        release = Path(row.get('release', ''))
        match = RELEASE_NAME.fullmatch(release.name)
        if (not match or row.get('version') != match[1]
                or Path(row.get('target', '')).resolve() != codex
                or release.absolute().parent != base
                or not isinstance((row.get('release_identity') or {}).get('files'), list)):
            continue
        previous = records.get(release.name)
        if previous and previous['release_identity'] != row['release_identity']:
            raise ValueError('Conflicting release ownership records: ' + release.name)
        records[release.name] = row
    return records


def _references(codex: Path, active: dict, names: set[str]) -> set[str]:
    paths = {codex / name for name in ('config.toml', 'hooks.json', 'AGENTS.md')}
    paths.add(codex / 'adhd' / 'native-installation.json')
    paths.update((codex / 'agents').glob('*.toml'))
    paths.update((codex / 'skills').glob('*/SKILL.md'))
    paths.update(Path(row['path']) for row in active.get('files', [])
                 if Path(row['path']).suffix in {'.md', '.toml', '.json', '.py', '.sh', '.ps1', '.js'})
    state_dirs = {child / 'native' for child in codex.iterdir()
                  if child.is_dir() and not _link(child) and (child / 'native').exists()}
    if codex == home():
        state_dirs.add(store() / 'native')
    for directory in state_dirs:
        if not directory.exists():
            continue
        if _link(directory):
            raise ValueError('Unsafe native state directory: ' + str(directory))
        paths.update(directory.glob('*/state.json'))
    referenced = set()
    for path in sorted(paths):
        if not path.exists() and not path.is_symlink():
            continue
        text = _read(path)
        if path.suffix == '.json':
            json.loads(text)
        elif path.suffix == '.toml':
            tomllib.loads(text)
        text = text.replace('\\\\', '\\').replace('\\', '/').casefold()
        referenced.update(name for name in names if name.casefold() in text)
    if ROOT.resolve().parent == codex / 'adhd' / 'releases':
        referenced.add(ROOT.name)
    return referenced


def _checked_tree(path: Path, base: Path, record: dict) -> dict:
    """Reject unrecorded data too, including files hidden in cache directories."""
    if (_link(base) or _link(base.parent) or _link(path)
            or path.absolute().parent != base or path.resolve().parent != base
            or not path.is_dir()):
        raise ValueError('Unsafe release cleanup path: ' + str(path))
    expected = {row['path'] for row in record['release_identity']['files']}
    directories = {parent.as_posix() for name in expected
                   for parent in Path(name).parents if parent.as_posix() != '.'}
    rows = []
    def fail(error):
        raise error
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=fail):
        for name in [*dirs, *files]:
            entry = Path(directory) / name
            relative = entry.relative_to(path)
            if _link(entry) or entry.resolve().is_relative_to(path) is False:
                raise ValueError('Release contains a link/junction or escape: ' + str(relative))
            if entry.is_dir():
                if (relative.as_posix() not in directories
                        and not (relative.name == '__pycache__'
                                 and (relative.parent.as_posix() == '.'
                                      or relative.parent.as_posix() in directories))):
                    raise ValueError('Unrecorded release directory: ' + str(relative))
                continue
            name = relative.as_posix()
            source = (relative.parent.parent / (relative.name.split('.')[0] + '.py')).as_posix()
            bytecode = relative.parent.name == '__pycache__' and relative.suffix in {'.pyc', '.pyo'} and source in expected
            if name not in expected and not bytecode:
                raise ValueError('Unrecorded release file: ' + name)
            rows.append((name, entry.stat().st_size, file_hash(entry)))
    rows.sort()
    _validate_release_record({**record, 'release': str(path)})
    return {'bytes': sum(row[1] for row in rows), 'files': len(rows),
            'tree_sha256': hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()}


def _scan(codex: Path, keep: int) -> tuple[dict, dict[str, dict]]:
    if type(keep) is not int or keep < 2 or keep > 100:
        raise ValueError('keep_releases must be 2..100 (current plus rollback)')
    managed = _find_managed_installation(codex)
    parent = codex / 'adhd'
    base = parent / 'releases'
    if not managed or managed[0] != parent:
        raise ValueError('An ADHD managed installation is required; upgrade legacy layouts first')
    if _link(parent) or _link(base):
        raise ValueError('Unsafe managed release collection')
    active = managed[2]
    _validate_release_record(active)
    records = _records(codex, parent, base)
    existing = sorted(base.iterdir())
    known = [path for path in existing if path.name in records]
    current = Path(active['release']).name
    recent = sorted((path.name for path in known if path.name != current),
                    key=lambda name: RELEASE_NAME.fullmatch(name)[2], reverse=True)[:keep - 1]
    protected = {current: 'current', **dict.fromkeys(recent, 'rollback')}
    for name in _references(codex, active, set(records)):
        protected.setdefault(name, 'known_reference')
    candidates, retained, skipped = [], [], []
    for path in existing:
        row = {'name': path.name, 'path': str(path)}
        if path.name in protected:
            retained.append({**row, 'reason': protected[path.name]})
        elif path.name not in records:
            skipped.append({**row, 'reason': 'unknown_or_unmanifested'})
        else:
            try:
                proof = _checked_tree(path, base, records[path.name])
                candidates.append({**row, **proof})
            except (ValueError, OSError, RuntimeError) as error:
                skipped.append({**row, 'reason': str(error)})
    return ({'dry_run': True, 'keep_releases': keep, 'candidates': candidates,
             'protected': retained, 'skipped': skipped,
             'reclaimable_bytes': sum(row['bytes'] for row in candidates),
             'reclaimed_bytes': 0, 'removed': []}, records)


def storage(codex_home: Path, *, apply: bool = False, keep_releases: int = 2) -> dict:
    codex = codex_home.expanduser().resolve()
    if not apply:
        return _scan(codex, keep_releases)[0]
    parent = codex / 'adhd'
    if _link(parent) or _link(parent / 'releases'):
        raise ValueError('Unsafe managed release collection')
    with (FileLock(str(parent / 'native-operation.lock'), timeout=10),
          FileLock(str(parent / 'install-native.lock'), timeout=5)):
        result, records = _scan(codex, keep_releases)
        result['dry_run'] = False
        base = parent / 'releases'
        for candidate in result['candidates']:
            path = base / candidate['name']
            # Refresh references and bytes after planning, before moving anything.
            refreshed, _ = _scan(codex, keep_releases)
            fresh = next((row for row in refreshed['candidates'] if row['name'] == path.name), None)
            if fresh != candidate:
                result['skipped'].append({'name': path.name, 'reason': 'changed_since_scan'})
                continue
            quarantine = base / ('.prune-' + uuid.uuid4().hex)
            os.replace(path, quarantine)
            try:
                proof = _checked_tree(quarantine, base, records[path.name])
                active = _find_managed_installation(codex)[2]
                if (proof != {key: candidate[key] for key in proof}
                        or path.name in _references(codex, active, set(records))):
                    raise ValueError('Release bytes or references changed during cleanup')
                if _link(quarantine) or quarantine.resolve().parent != base:
                    raise ValueError('Cleanup target changed before deletion')
                # Recheck the resolved absolute target before recursive deletion.
                shutil.rmtree(quarantine)
            except BaseException:
                if quarantine.exists() and not path.exists():
                    os.replace(quarantine, path)
                raise
            result['removed'].append(path.name)
            result['reclaimed_bytes'] += candidate['bytes']
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['scan', 'prune'], nargs='?', default='scan')
    parser.add_argument('--codex-home', type=Path, default=home())
    parser.add_argument('--keep-releases', type=int, default=2)
    parser.add_argument('--apply', action='store_true', help='Apply prune; preview is the default')
    args = parser.parse_args(argv)
    from .cli import output
    try:
        if args.apply and args.action != 'prune':
            raise ValueError('--apply requires prune')
        output(storage(args.codex_home, apply=args.apply, keep_releases=args.keep_releases))
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        output({'error': str(error), 'reclaimed_bytes': 0})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
