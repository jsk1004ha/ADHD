"""Entry point run by the bundled Windows interpreter after safe extraction."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from .core import ROOT, atomic_json
from .native_install import audit_native, upgrade_native, _find_managed_installation


def install_from_bundle(codex_home: Path) -> dict:
    runtime = ROOT / 'runtime' / 'python.exe'
    path_file = ROOT / 'runtime' / 'python313._pth'
    if not runtime.is_file() or not path_file.is_file():
        raise ValueError('The installer payload is missing its Python runtime')
    paths = {line.strip().replace('/', chr(92)) for line in path_file.read_text(encoding='utf-8-sig').splitlines()
             if line.strip() and not line.startswith('#')}
    if not {'python313.zip', '.', '..', '..' + chr(92) + 'third_party'} <= paths:
        raise ValueError('The bundled Python module paths are incomplete')
    target = codex_home.expanduser().resolve()
    if not target.is_dir():
        raise ValueError('Codex home does not exist: ' + str(target))
    outcome = upgrade_native(target)
    managed = _find_managed_installation(target)
    if managed is None:
        raise RuntimeError('The managed installation record was not created')
    record = managed[2]
    release = Path(record['release'])
    persistent = release / 'runtime' / 'python.exe'
    if not persistent.is_file():
        raise RuntimeError('The persistent Python runtime was not installed')
    hooks = json.loads((target / 'hooks.json').read_text(encoding='utf-8-sig'))
    commands = [hook['command'] for groups in hooks.get('hooks', {}).values()
                for group in groups for hook in group.get('hooks', [])
                if hook.get('type') == 'command' and str(release / 'hook.py') in hook.get('command', '')]
    if not commands or any(str(persistent) not in command for command in commands):
        raise RuntimeError('Managed hooks do not use the persistent runtime')
    doctor = audit_native(target)
    if doctor['installation_drift']:
        raise RuntimeError('Post-installation audit found managed file drift')
    return {'ok': True, 'status': outcome.get('status', 'installed'),
            'codex_home': str(target), 'release': str(release),
            'python': str(persistent), 'managed_hook_commands': len(commands),
            'installation_drift': doctor['installation_drift'],
            'next': 'Restart Codex, then review and trust the new commands when prompted.'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Install ADHD from a bundled Windows setup file')
    parser.add_argument('--codex-home', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        result = install_from_bundle(args.codex_home)
    except Exception as error:
        result = {'ok': False, 'error': str(error)}
    if args.report:
        atomic_json(args.report.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False))
    if not result['ok']:
        print(result['error'], file=sys.stderr)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
