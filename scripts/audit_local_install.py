"""Read-only fingerprints for an ADHD overlay upgrade; never print file contents."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str | None:
    return sha(path.read_bytes()) if path.is_file() else None


def tree(root: Path, *, exclude_adhd: bool = False) -> dict[str, str]:
    if not root.is_dir():
        return {}
    rows = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink() or '__pycache__' in path.parts:
            continue
        rel = path.relative_to(root)
        if exclude_adhd and rel.parts[0].startswith('adhd-'):
            continue
        rows[rel.as_posix()] = file_sha(path)
    return rows


def stable_digest(value) -> str:
    return sha(json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8'))


def unmanaged_hooks(root: Path) -> dict:
    path = root / 'hooks.json'
    if not path.is_file():
        return {}
    hooks = json.loads(path.read_text(encoding='utf-8-sig'))
    filtered = {}
    for event, groups in hooks.get('hooks', {}).items():
        kept = [row for row in groups if not any(
            f'/{name}/releases/' in str(hook.get('command', '')).replace('\\', '/')
            for hook in row.get('hooks', []) for name in ('adhd',))]
        if kept:
            filtered[event] = kept
    return {**{k: v for k, v in hooks.items() if k != 'hooks'}, 'hooks': filtered}


def unmanaged_agents(root: Path) -> str | None:
    path = root / 'AGENTS.md'
    if not path.is_file():
        return None
    text = path.read_text(encoding='utf-8-sig')
    start, end = '<!-- ADHD-NATIVE:BEGIN -->', '<!-- ADHD-NATIVE:END -->'
    if start in text:
        before, tail = text.split(start, 1)
        if end not in tail:
            raise ValueError('Unclosed ADHD guidance block')
        text = before.rstrip('\n') + tail.split(end, 1)[1]
    return sha(text.encode('utf-8'))


def snapshot(root: Path) -> dict:
    return {
        'config_sha256': file_sha(root / 'config.toml'),
        'auth_sha256': file_sha(root / 'auth.json'),
        'unmanaged_hooks_digest': stable_digest(unmanaged_hooks(root)),
        'unmanaged_guidance_sha256': unmanaged_agents(root),
        'unmanaged_agents_digest': stable_digest(tree(root / 'agents', exclude_adhd=True)),
        'unmanaged_skills_digest': stable_digest(tree(root / 'skills', exclude_adhd=True)),
        'plugins_digest': stable_digest(tree(root / 'plugins')),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex-home', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.codex_home.resolve()), ensure_ascii=False, indent=2))
