from __future__ import annotations

import hashlib
from contextlib import contextmanager
from collections.abc import Iterator
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import time
import tomllib
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MODES = ('coding', 'research', 'study', 'report', 'game')
EXCLUDED = {'.git', '.adhd', '.omx', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build'}


def home() -> Path:
    return Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().resolve()


def _has_native_state(root: Path) -> bool:
    sessions = root / 'native'
    if not sessions.is_dir() or sessions.is_symlink():
        return False
    try:
        for session in sessions.iterdir():
            if (not session.is_dir() or session.is_symlink()
                    or not re.fullmatch(r'[0-9a-f]{24}', session.name)):
                continue
            state_path = session / 'state.json'
            if not state_path.is_file() or state_path.is_symlink():
                continue
            try:
                state = json.loads(state_path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                continue
            if (isinstance(state, dict) and state.get('key') == session.name
                    and isinstance(state.get('workspace'), str)):
                return True
    except OSError:
        return False
    return False


def store() -> Path:
    configured = os.environ.get('ADHD_HOME')
    if configured:
        return Path(configured).expanduser().resolve()
    preferred = home() / 'adhd'
    if _has_native_state(preferred):
        return preferred.resolve()

    # Keep using a prior state directory only when its session records match the
    # native state schema. This avoids guessing a renamed path or moving user data.
    candidates: set[Path] = set()
    codex_home = home()
    try:
        for child in codex_home.iterdir():
            if child.is_symlink() or (hasattr(child, 'is_junction') and child.is_junction()):
                continue
            try:
                resolved = child.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved != preferred.resolve() and _has_native_state(child):
                candidates.add(resolved)
    except (OSError, RuntimeError):
        pass
    for name, value in os.environ.items():
        if name.endswith('_HOME') and value:
            candidate = Path(value).expanduser()
            try:
                candidate = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            if candidate != preferred.resolve() and _has_native_state(candidate):
                candidates.add(candidate)
    return next(iter(candidates)) if len(candidates) == 1 else preferred.resolve()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(value, encoding='utf-8')
    os.replace(tmp, path)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\x00' in relative:
        raise ValueError('Invalid relative path')
    # Treat Windows separators as separators even on Linux.
    relative = relative.replace('\\', '/')
    if re.match(r'^[A-Za-z]:', relative):
        raise ValueError('Absolute Windows paths are not artifact paths')
    p = (root / relative).resolve()
    if p == root.resolve() or not p.is_relative_to(root.resolve()):
        raise ValueError(f'Path leaves workspace: {relative}')
    return p


def select_mode(goal: str) -> str:
    text = goal.lower()
    for mode, words in [('game', ['게임', 'game', 'godot', 'unity']),
                        ('report', ['보고서', '초록', '논문 작성', '발표자료', 'report', 'docx', 'pptx']),
                        ('study', ['학습 자료', '해설', '공부', '문제 풀', '시험', 'teach', 'study']),
                        ('research', ['연구', '실험', '논문 비교', 'research', 'experiment', 'hypothesis'])]:
        if any(w in text for w in words):
            return mode
    return 'coding'


def environment_signature(workspace: Path) -> dict[str, Any]:
    files = ['pyproject.toml', 'uv.lock', 'requirements.txt', 'requirements.lock',
             'package.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
             'Cargo.toml', 'Cargo.lock', 'project.godot']
    return {'platform': platform.system(), 'python_major_minor': platform.python_version_tuple()[:2],
            'dependencies': {n: file_hash(workspace / n) for n in files if (workspace / n).is_file()}}


def workspace_fingerprint(workspace: Path) -> str:
    """Bounded progress heuristic, NOT a correctness or tamper-proof proof."""
    records = []
    for root, dirs, files in os.walk(workspace, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (Path(root) / d).is_symlink())
        for name in sorted(files):
            p = Path(root) / name
            if p.is_symlink():
                continue
            try:
                st = p.stat()
                # Hash small sources; avoid rereading multi-gigabyte research data every round.
                records.append((str(p.relative_to(workspace)), st.st_size,
                                file_hash(p) if st.st_size < 1024 * 1024 else st.st_mtime_ns))
            except OSError:
                continue
            if len(records) >= 20000:
                return digest(records)
    return digest(records)


def bounded(text: str, limit: int = 3500) -> str:
    return text if len(text) <= limit else text[:limit] + '\n[truncated; see local log]'


def redact(text: str) -> str:
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]+', r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}', '[REDACTED]', text)
    return re.sub(r'(?i)((?:api[_-]?key|password|secret|token)\s*[:=]\s*)[^\s,;]+', r'\1[REDACTED]', text)


def config() -> dict[str, Any]:
    p = home() / 'config.toml'
    return tomllib.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}


def _project_skill_roots(workspace: Path) -> list[tuple[Path,str,str]]:
    workspace=workspace.expanduser().resolve();chain=[];current=workspace
    while True:
        chain.append(current)
        if (current/'.git').exists() or current.parent==current: break
        # Without a repository marker, the supplied workspace is the only project scope.
        current=current.parent
    if not any((p/'.git').exists() for p in chain): chain=chain[:1]
    roots=[]
    for p in chain:
        roots.extend([(p/'.agents'/'skills','project-agents',str(p)),
                      (p/'.codex'/'skills','project-codex',str(p))])
    return roots


def _skill_config_paths() -> tuple[set[str],list[Path]]:
    cfg=config().get('skills',{});rows=cfg.get('config',[]) if isinstance(cfg,dict) else []
    if isinstance(rows,dict): rows=[rows]
    disabled:set[str]=set();enabled=[]
    for row in rows if isinstance(rows,list) else []:
        if not isinstance(row,dict) or not isinstance(row.get('path'),str): continue
        p=Path(row['path']).expanduser()
        if not p.is_absolute(): p=home()/p
        p=p.resolve();skill=p if p.name=='SKILL.md' else p/'SKILL.md'
        if row.get('enabled',True) is False: disabled.add(str(skill))
        else: enabled.append(skill)
    return disabled,enabled


def discover_skills(workspace: Path|None=None, *, host_catalog=None) -> list[dict[str, Any]]:
    workspace=(workspace or Path.cwd()).expanduser().resolve()
    if host_catalog is not None:
        result=[];seen=set()
        for raw in host_catalog:
            if not isinstance(raw,dict) or raw.get('enabled',True) is False: continue
            path=Path(str(raw.get('path',''))).expanduser().resolve()
            if path.name!='SKILL.md' or str(path) in seen: continue
            seen.add(str(path));result.append({'name':str(raw.get('name') or path.parent.name),
                'path':str(path),'description':str(raw.get('description',''))[:1800],
                'origin':str(raw.get('origin','host-catalog')),'enabled':True,
                'scope':str(raw.get('scope','host'))})
        return sorted(result,key=lambda d:(d['name'],d['path']))
    disabled,explicit=_skill_config_paths()
    roots=_project_skill_roots(workspace)+[(home()/'skills','codex-home','user'),
        (Path.home()/'.agents'/'skills','user-agents','user'),
        (home()/'plugins'/'cache','plugin-cache','plugin'),
        (ROOT/'skills','adhd-release','harness')]
    candidates:list[tuple[Path,str,str]]=[]
    for root,origin,scope in roots:
        if not root.exists(): continue
        pattern='*/SKILL.md' if origin!='plugin-cache' else '**/SKILL.md'
        candidates.extend((p,origin,scope) for p in root.glob(pattern))
    candidates.extend((p,'skills-config','configured') for p in explicit if p.is_file())
    # A pre-existing user skill owns its folder. Keep the bundled copy in the
    # immutable release visible as a separate fallback to local routing.
    managed_root = home() / 'adhd'
    for record_name, field in [('builtin-installation.json', 'skills'),
                               ('native-installation.json', 'builtin_skills'),
                               ('native-installation.json', 'workflow_skills')]:
        record = read_json(managed_root / record_name, {})
        for row in record.get(field, []) if isinstance(record, dict) else []:
            if row.get('status') != 'preserved_existing':
                continue
            fallback = Path(str(row.get('fallback', ''))).expanduser().resolve()
            if fallback.is_relative_to(managed_root.resolve()) and fallback.is_file():
                candidates.append((fallback, 'adhd-bundled-fallback', 'user'))
    result=[];seen:set[str]=set()
    for p,origin,scope in candidates[:20000]:
        try:
            key=str(p.resolve(strict=True))
        except (OSError,RuntimeError):
            continue
        if key in seen or key in disabled or not p.is_file(): continue
        seen.add(key)
        text=p.read_text(encoding='utf-8-sig',errors='replace')
        front=text.split('---',2)[1] if text.startswith('---') and text.count('---')>=2 else ''
        name_match=re.search(r'^name:\s*(.*)',front,re.M)
        m=re.search(r'^description:\s*(.*)',front,re.M)
        desc=m.group(1).strip(' "\'') if m else ''
        if desc in ('>','|','>-','|-'):
            desc=' '.join(line.strip() for line in front[m.end():].splitlines() if line.startswith((' ','\t')))
        name=name_match.group(1).strip(' "\'') if name_match else p.parent.name
        result.append({'name':name,'path':key,'description':desc[:1800],
                       'origin':origin,'enabled':True,'scope':scope})
    return sorted(result,key=lambda d:(d['name'],d['path']))


def preferred_skill_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude shipped same-name fallbacks from selection, preserving discovery."""
    fallback_origins = {'adhd-release', 'adhd-bundled-fallback'}
    overrides = {entry['name'].lower() for entry in entries
                 if entry.get('origin') not in fallback_origins}
    return [entry for entry in entries
            if entry.get('origin') not in fallback_origins
            or entry['name'].lower() not in overrides]


def words(text: str) -> set[str]:
    out = set(re.findall(r'[\w-]+', text.lower()))
    # CJK substrings help search when Korean suffixes vary. No embeddings/API needed.
    for part in re.findall(r'[가-힣]{2,}', text):
        out.update(part[i:i+2] for i in range(len(part)-1))
    return out


def skill_search(query: str, limit: int = 5, *, workspace: Path|None=None) -> list[dict[str, Any]]:
    q = words(query)
    ranked = []
    for d in preferred_skill_entries(discover_skills(workspace)):
        score = len(q & words(d['name'])) * 4 + len(q & words(d['description']))
        if score:
            ranked.append(dict(d, score=score, description=d['description'][:260]))
    return sorted(ranked, key=lambda d: (-d['score'], d['name']))[:max(1, min(10, limit))]


@contextmanager
def memory_db() -> Iterator[sqlite3.Connection]:
    p = store() / 'memory.sqlite3'
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.execute('''CREATE TABLE IF NOT EXISTS recipes (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, mode TEXT NOT NULL,
        goal TEXT NOT NULL, environment TEXT NOT NULL, steps TEXT NOT NULL,
        evidence TEXT NOT NULL, successes INTEGER NOT NULL, failures INTEGER NOT NULL,
        updated REAL NOT NULL)''')
    conn.commit()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def recipes(workspace: Path, mode: str, goal: str, limit: int = 3) -> list[dict[str, Any]]:
    env = digest(environment_signature(workspace))
    from .memory import Memory
    with Memory() as memory:
        memory.migrate_legacy_recipes(store()/'memory.sqlite3')
        return memory.procedure_recipes(workspace,mode,goal,env,limit)


def save_recipe(workspace: Path, mode: str, goal: str, steps: list[str], evidence: str,
                bundle: dict|None=None) -> str | None:
    clean = [redact(s)[:800] for s in steps if isinstance(s, str) and s.strip()][:10]
    if not clean:
        return None
    clean_bundle={key:[redact(value)[:800] for value in values]
                  for key,values in bundle.items()} if bundle is not None else None
    env=digest(environment_signature(workspace))
    from .memory import Memory
    with Memory() as memory:
        memory.migrate_legacy_recipes(store()/'memory.sqlite3')
        rid=memory.save_procedure(workspace,mode,redact(goal),clean,evidence,env,bundle=clean_bundle)
    folder = store() / 'recipes' / rid
    atomic_text(folder / 'SKILL.md', '---\nname: recipe-' + rid + '\ndescription: Verified project-local procedure; check environment and current request before reuse.\n---\n\n' +
                '\n'.join(f'{i+1}. {s}' for i, s in enumerate(clean)) +
                ('\n\nProcedure bundle:\n'+json.dumps(clean_bundle,ensure_ascii=False,indent=2) if clean_bundle else '')+
                '\n\nEvidence: ' + evidence + '\n')
    return rid


def quarantine(recipe_ids: list[str]) -> None:
    from .memory import Memory
    with Memory() as memory:
        memory.migrate_legacy_recipes(store()/'memory.sqlite3')
        memory.quarantine(recipe_ids)
