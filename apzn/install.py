from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import time
import tomllib
from typing import Any

from .core import ROOT, atomic_json, atomic_text, file_hash, read_json
from . import __version__

BEGIN = '<!-- APZN:BEGIN -->'
END = '<!-- APZN:END -->'
ADDENDUM = '''<!-- APZN:BEGIN -->
## ADHD — Autonomous Delegation Harness Director task routing
For substantial research, study, report-writing, coding or game tasks, read
`~/.codex/skills/apzn/SKILL.md` (use CODEX_HOME if configured). Preserve the user's
exact goal, constraints, existing capabilities, and acceptance evidence.
Use ONE loop owner: the active OMX/LazyCodex runtime OR `adhd.py run`, never both.
Explicit user-selected skills and repository instructions take precedence.
Reuse verified project-local procedures only after checking current applicability.
<!-- APZN:END -->'''


def tree_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): file_hash(p) for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def install(target: Path, compact: bool = False) -> dict[str, Any]:
    """Add skill + local runner. Never import sanitized runtime config, caches, or secrets."""
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    parent = target / 'apzn'
    current = parent / 'current'
    parent.mkdir(exist_ok=True)
    record = parent / 'installation.json'
    if record.exists():
        raise ValueError('Already installed. Keep the current package, or uninstall the managed overlay before reinstalling. User config is untouched.')
    if (target / 'skills' / 'apzn').exists():
        raise ValueError('A skill named apzn already exists; refusing to overwrite it.')
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid())
    stamp += '-' + str(time.time_ns() % 1000000)
    if current.exists() and ROOT.resolve() != current.resolve():
        current = parent / 'releases' / stamp
    backup = parent / 'backups' / stamp
    backup.mkdir(parents=True)
    agents = target / 'AGENTS.md'
    original = agents.read_bytes() if agents.exists() else None
    original_text = original.decode('utf-8-sig') if original is not None else ''
    if BEGIN in original_text:
        raise ValueError('An unmanaged APZN block already exists. Resolve it before installing.')
    if original is not None:
        (backup / 'AGENTS.md').write_bytes(original)
    cfg_path = target / 'config.toml'
    hooks_path = target / 'hooks.json'
    stable = {str(p.relative_to(target)): file_hash(p) for p in (cfg_path, hooks_path) if p.is_file()}
    try:
        # Copy only the generated package, not any of the user's original files or credentials.
        if ROOT.resolve() != current.resolve():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(ROOT, current, dirs_exist_ok=False,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git', 'test-results*'))
        shutil.copytree(ROOT / 'skills' / 'apzn', target / 'skills' / 'apzn')
        installed_skill = target / 'skills' / 'apzn' / 'SKILL.md'
        text = installed_skill.read_text(encoding='utf-8')
        text = text.replace('APZN_RUNNER_PATH', str(current / 'adhd.py'))
        atomic_text(installed_skill, text)
        if compact:
            legacy = parent / 'legacy' / 'AGENTS.original.md'
            if original is not None:
                legacy.parent.mkdir(exist_ok=True)
                legacy.write_bytes(original)
            compact_text = (ROOT / 'config' / 'AGENTS.compact.md').read_text(encoding='utf-8')
            compact_text = compact_text.replace('APZN_LEGACY_PATH', str(legacy))
            atomic_text(agents, compact_text)
        else:
            # Original bytes are an exact prefix, including original newline convention/BOM.
            agents.write_bytes((original or b'') + b'\n\n' + ADDENDUM.encode('utf-8') + b'\n')
        launcher_dir = parent / 'bin'
        launcher_dir.mkdir(exist_ok=True)
        if os.name == 'nt':
            # The user controls this local launcher. Prompts are still passed to Codex via stdin.
            command='@echo off\r\n"' + sys.executable + '" "' + str(current / 'adhd.py') + '" %*\r\n'
            atomic_text(launcher_dir / 'adhd.cmd', command)
            atomic_text(launcher_dir / 'apzn.cmd', command)
        else:
            import shlex
            command='#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(current / 'adhd.py')) + ' "$@"\n'
            for name in ('adhd','apzn'):
                launcher=launcher_dir/name
                atomic_text(launcher,command)
                launcher.chmod(0o755)
        unchanged = {rel: file_hash(target / rel) == h for rel, h in stable.items()}
        if not all(unchanged.values()):
            raise RuntimeError('Unexpected concurrent config change; check the installation report.')
        result = {'version': __version__, 'target': str(target), 'backup': str(backup), 'compact': compact,
                  'original_agents_existed': original is not None, 'installed_agents_sha256': file_hash(agents),
                  'original_agents_sha256': file_hash(backup / 'AGENTS.md') if original is not None else None,
                  'unchanged_config_and_hooks': unchanged,
                  'launcher': str(launcher_dir / ('adhd.cmd' if os.name == 'nt' else 'adhd')),
                  'runner': str(current / 'adhd.py')}
        atomic_json(record, result)
        return result
    except BaseException:
        if original is not None:
            agents.write_bytes(original)
        elif agents.exists():
            agents.unlink()
        # Only our newly created skill is removed; keep backup/runner for diagnosis.
        shutil.rmtree(target / 'skills' / 'apzn', ignore_errors=True)
        raise


def uninstall(target: Path) -> dict:
    target = target.expanduser().resolve()
    path = target / 'apzn' / 'installation.json'
    record = read_json(path)
    if not record:
        raise ValueError('No managed installation record found')
    agents = target / 'AGENTS.md'
    if not agents.exists() or file_hash(agents) != record['installed_agents_sha256']:
        raise ValueError('AGENTS.md changed after installation. Refusing to overwrite newer guidance. Manually merge from the recorded backup.')
    backup = Path(record['backup']) / 'AGENTS.md'
    if record['original_agents_existed']:
        if not backup.is_file() or file_hash(backup) != record['original_agents_sha256']:
            raise ValueError('Recorded original backup is missing or changed; refusing unsafe restoration.')
        agents.write_bytes(backup.read_bytes())
    else:
        agents.unlink()
    # Preserve learned recipes, logs, runner and backups. Rename the installed skill instead of deleting user additions.
    skill = target / 'skills' / 'apzn'
    if skill.exists():
        destination = Path(record['backup']) / 'uninstalled-apzn-skill'
        shutil.move(str(skill), str(destination))
    path.rename(path.with_name('installation.uninstalled.' + str(int(time.time())) + '.json'))
    return {'restored': str(agents), 'preserved': str(target / 'apzn')}


def audit(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    problems = []
    cfg = {}
    try:
        p = target / 'config.toml'
        cfg = tomllib.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}
    except (ValueError, OSError) as e:
        problems.append('Invalid config.toml: ' + str(e))
    skills = list((target / 'skills').glob('*/SKILL.md'))
    agents = list((target / 'agents').glob('*.toml'))
    roles = []
    for p in agents:
        try:
            d = tomllib.loads(p.read_text(encoding='utf-8-sig'))
            roles.append({'role': p.stem, 'model': d.get('model', '(inherit)'),
                          'reasoning': d.get('model_reasoning_effort', '(inherit)')})
        except (ValueError, OSError) as e:
            problems.append(p.name + ': ' + str(e))
    pin_mismatch = [r for r in roles if r['model'] != '(inherit)' and r['model'] != cfg.get('model')]
    if pin_mismatch:
        problems.append(f'{len(pin_mismatch)} role model pins differ from the root model. Different is not necessarily invalid; verify runtime availability.')
    raw_files = [target / 'config.toml', target / 'hooks.json', target / 'AGENTS.md']
    placeholders = [str(p.relative_to(target)) for p in raw_files if p.exists() and
                    any(s in p.read_text(encoding='utf-8-sig', errors='replace') for s in ('<USER_HOME>', '<REDACTED_', 'PROJECT_001'))]
    if placeholders:
        problems.append('Sanitized placeholders found: do not restore this export over a working installation.')
    executables = {name: shutil.which(name) for name in ['codex', 'omx', 'node', 'git', 'uv', 'tmux', 'psmux', 'code-review-graph']}
    if not executables['codex']:
        problems.append('Codex CLI not on this shell PATH; no authenticated end-to-end test has been run.')
    policy = cfg.get('features', {}).get('multi_agent_v2')
    return {'target': str(target), 'model': cfg.get('model'), 'reasoning': cfg.get('model_reasoning_effort'),
            'global_agents_bytes': (target / 'AGENTS.md').stat().st_size if (target / 'AGENTS.md').exists() else 0,
            'skill_count': len(skills), 'agent_count': len(agents),
            'prompt_count': len(list((target / 'prompts').glob('*.md'))),
            'configured_mcp': sorted(cfg.get('mcp_servers', {})),
            'explicit_plugins': sorted(cfg.get('plugins', {})), 'multi_agent_v2': policy,
            'roles': roles, 'executables': executables, 'warnings': problems,
            'runtime_status': 'NOT_TESTED (presence/configuration is not proof of functionality)'}
