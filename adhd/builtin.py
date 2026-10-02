"""Reviewed default skills and MCP connection definitions for Codex homes.

The bundle contains public skill files. MCP registrations are launch recipes;
external engines, accounts, and a successful server handshake are separate.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Any

from . import dependencies as _dependencies
import tomlkit
from filelock import FileLock

from .core import ROOT, atomic_json, file_hash, read_json


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _plain(value: Any) -> Any:
    return value.unwrap() if hasattr(value, 'unwrap') else value


def _runtime_cache(path: Path) -> bool:
    return '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}


def skill_manifest(root: Path = ROOT, *, verify: bool = True) -> dict:
    manifest = json.loads((root / 'config' / 'builtin-skills.json').read_text(encoding='utf-8'))
    rows = manifest.get('skills', [])
    if manifest.get('schema_version') != 1 or manifest.get('count') != 50 or len(rows) != 50:
        raise ValueError('Built-in skill manifest must contain exactly 50 reviewed skills')
    names = set()
    for row in rows:
        name = row['name']
        if not name or name in names or Path(name).name != name or name in {'.', '..'}:
            raise ValueError('Unsafe or duplicate built-in skill name')
        names.add(name)
        base = root / 'bundled' / 'skills' / name
        if row['path'] != f'bundled/skills/{name}' or not (base / 'SKILL.md').is_file():
            raise ValueError('Built-in skill missing: ' + name)
        if verify:
            actual = set()
            for item in row['files']:
                rel = item['path']
                if not rel or '\\' in rel or Path(rel).is_absolute() or '..' in Path(rel).parts:
                    raise ValueError('Unsafe bundled skill file path')
                p = base / rel
                if p.is_symlink() or not p.is_file() or file_hash(p) != item['sha256'] or p.stat().st_size != item['bytes']:
                    raise ValueError('Bundled skill file differs from manifest: ' + name + '/' + rel)
                actual.add(rel.replace('\\', '/'))
            if actual != {p.relative_to(base).as_posix() for p in base.rglob('*')
                          if p.is_file() and not _runtime_cache(p.relative_to(base))}:
                raise ValueError('Bundled skill file inventory differs: ' + name)
            if not (base / row['license_file']).is_file():
                raise ValueError('Bundled skill license missing: ' + name)
    return manifest


def mcp_catalog(root: Path = ROOT) -> list[dict]:
    data = json.loads((root / 'config' / 'mcp-selection.json').read_text(encoding='utf-8'))
    rows = data.get('servers', [])
    ids = {row.get('id') for row in rows}
    if len(rows) != 16 or ids != {f'M{i:02d}' for i in range(1, 17)}:
        raise ValueError('Built-in MCP catalog must include M01 through M16')
    for row in rows:
        recipe = row.get('builtin')
        if not isinstance(recipe, dict) or recipe.get('id') != row['id'] or not recipe.get('key'):
            raise ValueError('Missing reviewed MCP recipe: ' + str(row.get('id')))
        if recipe.get('status') != 'unsupported' and not (recipe.get('url') or (recipe.get('command') and recipe.get('args'))):
            raise ValueError('Incomplete MCP connection recipe: ' + row['id'])
    return rows


def _program_available(name: str, environment: dict[str, str]) -> bool:
    if name == 'aside' and _aside_executable(environment):
        return True
    if name == 'godot' and environment.get('GODOT_PATH'):
        return Path(environment['GODOT_PATH']).is_file()
    if shutil.which(name, path=environment.get('PATH', '')):
        return True
    if name == 'chrome' and os.name == 'nt':
        locations = [environment.get('PROGRAMFILES'), environment.get('PROGRAMFILES(X86)'),
                     environment.get('LOCALAPPDATA')]
        return any((Path(base) / 'Google/Chrome/Application/chrome.exe').is_file()
                   for base in locations if base)
    return False


def _aside_executable(environment: dict[str, str]) -> str | None:
    found = shutil.which('aside', path=environment.get('PATH', ''))
    if found:
        return found
    if os.name == 'nt' and environment.get('LOCALAPPDATA'):
        candidate = Path(environment['LOCALAPPDATA']) / 'Aside/CLI/current/aside.exe'
        if candidate.is_file():
            return str(candidate)
    return None


def _supported_node(recipe: dict, environment: dict[str, str]) -> bool:
    minimum = recipe.get('node_runtime')
    if not minimum:
        return True
    try:
        executable = shutil.which('node', path=environment.get('PATH', ''))
        if not executable:
            return False
        result = subprocess.run([executable, '--version'], capture_output=True, text=True,
                                timeout=3, check=False, env=environment)
        parts = result.stdout.strip().lstrip('v').split('.')
        version = tuple(int(part) for part in parts[:3])
        if result.returncode or len(version) != 3:
            return False
        return any(version >= tuple(bound) and (ceiling is None or version < tuple(ceiling))
                   for bound, ceiling in minimum)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def readiness(recipe: dict, environment: dict[str, str] | None = None) -> dict:
    environment = dict(os.environ if environment is None else environment)
    if recipe.get('status') == 'unsupported':
        return {'status': 'unsupported', 'ready': False, 'missing': [],
                'reason': recipe.get('reason', 'No verified connection contract')}
    missing = []
    command = recipe.get('command')
    if command and not _program_available(command, environment):
        missing.append(command)
    if command == 'npx' and not _supported_node(recipe, environment):
        missing.append('supported Node.js runtime')
    engine = recipe.get('requires_program')
    if engine and not _program_available(engine, environment):
        missing.append(engine)
    if recipe.get('engine_integration_required'):
        missing.append(f'{engine} MCP integration/connection')
    if missing:
        return {'status': 'dependency_required', 'ready': False, 'missing': missing}
    required_env = [name for name in recipe.get('required_env', []) if not environment.get(name)]
    bearer = recipe.get('bearer_token_env_var')
    if bearer and not environment.get(bearer):
        required_env.append(bearer)
    if required_env:
        return {'status': 'auth_required', 'ready': False, 'missing': required_env}
    if recipe.get('oauth_required'):
        return {'status': 'auth_required', 'ready': False, 'missing': ['OAuth authorization']}
    if recipe.get('auth_required') or bearer or recipe.get('required_env'):
        return {'status': 'auth_available', 'ready': True, 'missing': []}
    return {'status': 'ready', 'ready': True, 'missing': []}


def _mcp_value(recipe: dict, enabled: bool, environment: dict[str, str]) -> dict:
    value: dict[str, Any] = {}
    if 'url' in recipe:
        value['url'] = recipe['url']
    else:
        value['command'] = (_aside_executable(environment) or recipe['command']) \
            if recipe['command'] == 'aside' else recipe['command']
        value['args'] = recipe['args']
        if recipe.get('env_vars'):
            value['env_vars'] = recipe['env_vars']
    if recipe.get('bearer_token_env_var'):
        value['bearer_token_env_var'] = recipe['bearer_token_env_var']
    value['enabled'] = enabled
    return value


def prepare_mcp_config(original: bytes, root: Path = ROOT,
                       environment: dict[str, str] | None = None) -> tuple[bytes, list[dict], list[dict]]:
    """Append only absent reviewed entries. Return selector records for rollback."""
    environment = dict(os.environ if environment is None else environment)
    document = tomlkit.parse(original.decode('utf-8-sig'))
    servers = document.get('mcp_servers')
    if servers is None:
        servers = tomlkit.table()
        document['mcp_servers'] = servers
    if not isinstance(servers, dict):
        raise ValueError('Existing mcp_servers is not a TOML table')
    entries: list[dict] = []
    statuses: list[dict] = []
    existing_urls = {str(value.get('url', '')).rstrip('/'): str(key)
                     for key, value in servers.items() if isinstance(value, dict) and value.get('url')}
    for row in mcp_catalog(root):
        recipe = row['builtin']
        status = readiness(recipe, environment)
        item = {'id': row['id'], 'key': recipe['key'], **status}
        if status['status'] == 'unsupported':
            item['registration'] = 'unsupported'
        elif recipe['key'] in servers:
            item['registration'] = 'preserved_existing'
        elif recipe.get('url', '').rstrip('/') in existing_urls and recipe.get('url'):
            item['registration'] = 'preserved_url_alias'
            item['existing_key'] = existing_urls[recipe['url'].rstrip('/')]
        else:
            # Credentialed services stay disabled by default. Explicit enable
            # requires a separate readiness check; no token is stored here.
            enabled = bool(recipe.get('anonymous') and status['ready']) or bool(
                not recipe.get('url') and status['status'] == 'ready')
            value = _mcp_value(recipe, enabled, environment)
            servers[recipe['key']] = value
            entries.append({'key': recipe['key'], 'digest': _digest(value)})
            item['registration'] = 'added'
            item['enabled'] = enabled
        statuses.append(item)
    return (tomlkit.dumps(document).encode('utf-8') if entries else original, entries, statuses)


def _managed_mcp_document(path: Path, entries: list[dict]):
    document = tomlkit.parse(path.read_text(encoding='utf-8-sig') if path.exists() else '')
    servers = document.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('Managed MCP table was replaced: ' + str(path))
    for row in entries:
        key = row['key']
        if key not in servers or _digest(_plain(servers[key])) != row['digest']:
            raise ValueError('Later edits detected in ADHD MCP entry: ' + key)
    return document


def validate_mcp_entries(path: Path, entries: list[dict]) -> None:
    _managed_mcp_document(path, entries)


def restore_mcp_entries(path: Path, entries: list[dict], backup: Path | None = None,
                        installed_sha256: str | None = None) -> None:
    document = _managed_mcp_document(path, entries)
    if backup and installed_sha256 and path.is_file() and file_hash(path) == installed_sha256:
        path.write_bytes(backup.read_bytes())
        return
    if backup is None and installed_sha256 and path.is_file() and file_hash(path) == installed_sha256:
        path.unlink()
        return
    servers = document['mcp_servers']
    for row in reversed(entries):
        del servers[row['key']]
    temp = path.with_name(path.name + '.adhd-builtin.tmp')
    temp.write_text(tomlkit.dumps(document), encoding='utf-8')
    os.replace(temp, path)


def skill_changes(agents_home: Path, source_root: Path = ROOT) -> tuple[dict[Path, bytes], list[dict]]:
    manifest = skill_manifest(source_root)
    changes: dict[Path, bytes] = {}
    statuses: list[dict] = []
    for row in manifest['skills']:
        base = agents_home / 'skills' / row['name']
        if base.exists():
            statuses.append({'name': row['name'], 'status': 'preserved_existing',
                             'fallback': str(source_root / row['path'] / 'SKILL.md')})
            continue
        for item in row['files']:
            changes[base / item['path']] = (source_root / row['path'] / item['path']).read_bytes()
        statuses.append({'name': row['name'], 'status': 'added', 'path': str(base / 'SKILL.md')})
    return changes, statuses


def prune_empty_skill_dirs(agents_home: Path, statuses: list[dict]) -> None:
    """Remove only empty directories made for added skills; keep user additions."""
    skills_root = agents_home.expanduser().resolve() / 'skills'
    for row in statuses:
        if row.get('status') != 'added':
            continue
        base = skills_root / row['name']
        if not base.is_dir() or base.is_symlink():
            continue
        for directory in sorted((p for p in base.rglob('*') if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            base.rmdir()
        except OSError:
            pass


def _validate_record(record: dict) -> None:
    for row in record['files']:
        path = Path(row['path'])
        if row.get('selector', {}).get('kind') == 'toml_mcp_entries':
            validate_mcp_entries(path, row['selector']['entries'])
        elif not path.is_file() or file_hash(path) != row['installed_sha256']:
            raise ValueError('Later edits detected in bundled skill: ' + str(path))
        if row['existed'] and file_hash(Path(row['backup'])) != row['old_sha256']:
            raise ValueError('Built-in backup missing or changed: ' + str(path))


def apply_builtin(target: Path, agents_home: Path | None = None, source_root: Path = ROOT) -> dict:
    """Apply only bundled skills and MCP definitions to an existing Codex home."""
    target = target.expanduser().resolve()
    agents_home = (agents_home or target).expanduser().resolve()
    parent = target / 'adhd'
    parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(parent / 'builtin-operation.lock'), timeout=10):
        record_path = parent / 'builtin-installation.json'
        existing = read_json(record_path)
        if existing:
            _validate_record(existing)
            return {'status': 'already_installed', 'skills': existing['skills'], 'mcp': existing['mcp']}
        manifest = skill_manifest(source_root)
        catalog = mcp_catalog(source_root)
        stamp = _digest({'skills': file_hash(source_root / 'config' / 'builtin-skills.json'),
                         'mcp': file_hash(source_root / 'config' / 'mcp-selection.json')})[:16]
        release = parent / 'builtin-releases' / stamp
        if not release.exists():
            staging = release.with_name('.staging-' + uuid.uuid4().hex)
            try:
                for skill in manifest['skills']:
                    for item in skill['files']:
                        source = source_root / skill['path'] / item['path']
                        destination = staging / skill['path'] / item['path']
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                (staging / 'config').mkdir(parents=True)
                for name in ('builtin-skills.json', 'mcp-selection.json'):
                    shutil.copy2(source_root / 'config' / name, staging / 'config' / name)
                skill_manifest(staging)
                mcp_catalog(staging)
                release.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, release)
            except BaseException:
                if staging.exists():
                    shutil.rmtree(staging)
                raise
        elif (file_hash(release / 'config' / 'builtin-skills.json') !=
              file_hash(source_root / 'config' / 'builtin-skills.json') or
              file_hash(release / 'config' / 'mcp-selection.json') !=
              file_hash(source_root / 'config' / 'mcp-selection.json')):
            raise ValueError('Existing immutable built-in release differs from source')
        skill_files, skills = skill_changes(agents_home, release)
        cfg_path = target / 'config.toml'
        original_cfg = cfg_path.read_bytes() if cfg_path.exists() else b''
        new_cfg, entries, mcp = prepare_mcp_config(original_cfg, release)
        changes = dict(skill_files)
        if entries:
            changes[cfg_path] = new_cfg
        backup = parent / 'backups' / ('builtin-' + uuid.uuid4().hex)
        backup.mkdir(parents=True)
        rows = []
        for i, (path, content) in enumerate(changes.items()):
            old = path.read_bytes() if path.exists() else None
            bp = backup / f'{i:04d}.original'
            if old is not None:
                bp.write_bytes(old)
            rows.append({'path': str(path), 'existed': old is not None, 'backup': str(bp),
                         'old_sha256': file_hash(bp) if old is not None else None,
                         'installed_sha256': hashlib.sha256(content).hexdigest(),
                         'selector': {'kind': 'toml_mcp_entries', 'entries': entries}
                         if path == cfg_path else {'kind': 'file'}})
        written = []
        try:
            for row, content in zip(rows, changes.values()):
                path = Path(row['path'])
                path.parent.mkdir(parents=True, exist_ok=True)
                expected = Path(row['backup']).read_bytes() if row['existed'] else None
                if (path.read_bytes() if path.exists() else None) != expected:
                    raise ValueError('Concurrent user edit during built-in install: ' + str(path))
                temp = path.with_name(path.name + '.adhd-builtin.tmp')
                temp.write_bytes(content)
                os.replace(temp, path)
                written.append(row)
            result = {'version': 1, 'target': str(target), 'agents_home': str(agents_home),
                      'release': str(release), 'backup': str(backup), 'files': rows,
                      'skills': skills, 'mcp': mcp, 'catalog_count': len(catalog)}
            atomic_json(record_path, result)
            return result
        except BaseException:
            for row in reversed(written):
                path = Path(row['path'])
                if path.is_file() and file_hash(path) == row['installed_sha256']:
                    if row['existed']:
                        path.write_bytes(Path(row['backup']).read_bytes())
                    else:
                        path.unlink()
            prune_empty_skill_dirs(agents_home, skills)
            raise


def rollback_builtin(target: Path) -> dict:
    parent = target.expanduser().resolve() / 'adhd'
    with FileLock(str(parent / 'builtin-operation.lock'), timeout=10):
        record_path = parent / 'builtin-installation.json'
        record = read_json(record_path)
        if not record:
            raise ValueError('No built-in installation record')
        _validate_record(record)
        for row in reversed(record['files']):
            path = Path(row['path'])
            if row['selector']['kind'] == 'toml_mcp_entries':
                restore_mcp_entries(path, row['selector']['entries'],
                                    Path(row['backup']) if row['existed'] else None,
                                    row['installed_sha256'])
            elif row['existed']:
                path.write_bytes(Path(row['backup']).read_bytes())
            else:
                path.unlink()
        prune_empty_skill_dirs(Path(record['agents_home']), record['skills'])
        record_path.rename(parent / ('builtin-uninstalled-' + uuid.uuid4().hex[:8] + '.json'))
        return {'restored_files': len(record['files']), 'release_preserved': record['release']}


def builtin_status(target: Path, agents_home: Path | None = None) -> dict:
    target = target.expanduser().resolve()
    agents_home = (agents_home or target).expanduser().resolve()
    config_path = target / 'config.toml'
    document = tomlkit.parse(config_path.read_text(encoding='utf-8-sig') if config_path.exists() else '')
    servers = document.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('Existing mcp_servers is not a TOML table')
    existing_urls = {str(value.get('url', '')).rstrip('/'): str(name)
                     for name, value in servers.items() if isinstance(value, dict) and value.get('url')}
    mcp = []
    for row in mcp_catalog():
        recipe = row['builtin']
        state = readiness(recipe)
        existing_key = recipe['key'] if recipe['key'] in servers else existing_urls.get(
            recipe.get('url', '').rstrip('/')) if recipe.get('url') else None
        installed = servers.get(existing_key) if existing_key else None
        mcp.append({'id': row['id'], 'key': recipe['key'], **state,
                    'existing_key': existing_key,
                    'registered': installed is not None,
                    'enabled': bool(installed.get('enabled', True)) if isinstance(installed, dict) else False})
    skills = [{'name': row['name'], 'installed': (agents_home / 'skills' / row['name'] / 'SKILL.md').is_file()}
              for row in skill_manifest(verify=False)['skills']]
    return {'skills_installed': sum(row['installed'] for row in skills), 'skills_total': len(skills),
            'skills': skills, 'mcp': mcp,
            'standalone_record': (target / 'adhd' / 'builtin-installation.json').is_file()}


def enable_builtin(target: Path, key: str) -> dict:
    """Enable a managed disabled entry only after its current prerequisites pass."""
    target = target.expanduser().resolve()
    parent = target / 'adhd'
    with FileLock(str(parent / 'builtin-operation.lock'), timeout=10):
        record_path = parent / 'builtin-installation.json'
        record = read_json(record_path)
        if not record:
            from .native_install import _find_managed_installation
            managed = _find_managed_installation(target)
            if not managed:
                raise ValueError('No managed built-in installation')
            record_path, record = managed[1], managed[2]
        recipe = next((row['builtin'] for row in mcp_catalog()
                       if row['builtin']['key'] == key), None)
        if recipe is None:
            raise ValueError('Unknown built-in MCP key: ' + key)
        state = readiness(recipe)
        if not state['ready']:
            raise ValueError(f'{key} is {state["status"]}: {state["missing"]}')
        cfg_path = target / 'config.toml'
        row = next((item for item in record['files'] if item['path'] == str(cfg_path)
                    and item.get('selector', {}).get('kind') == 'toml_mcp_entries'), None)
        if row is None or key not in {item['key'] for item in row['selector']['entries']}:
            raise ValueError('MCP entry is not managed by ADHD: ' + key)
        _validate_record(record) if 'catalog_count' in record else None
        document = _managed_mcp_document(cfg_path, row['selector']['entries'])
        current = document['mcp_servers'][key]
        if current.get('enabled', True):
            return {'key': key, 'status': 'already_enabled'}
        before = cfg_path.read_bytes()
        current['enabled'] = True
        temp = cfg_path.with_name(cfg_path.name + '.adhd-builtin.tmp')
        try:
            temp.write_text(tomlkit.dumps(document), encoding='utf-8')
            if cfg_path.read_bytes() != before:
                raise ValueError('Concurrent user edit during MCP enable')
            os.replace(temp, cfg_path)
            for entry in row['selector']['entries']:
                if entry['key'] == key:
                    entry['digest'] = _digest(_plain(current))
            row['installed_sha256'] = file_hash(cfg_path)
            atomic_json(record_path, record)
            return {'key': key, 'status': 'enabled', 'readiness': state}
        except BaseException:
            if cfg_path.is_file() and cfg_path.read_bytes() != before:
                cfg_path.write_bytes(before)
            raise
        finally:
            if temp.exists():
                temp.unlink()
