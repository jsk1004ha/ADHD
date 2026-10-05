"""Transactional overlay; preserve user providers, plugins, skills and permission policy."""
from __future__ import annotations
import json
import os
from contextlib import ExitStack
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import PurePosixPath
from typing import Any
from . import DISPLAY_NAME, __version__
from . import dependencies as _dependencies
import tomlkit
from filelock import FileLock
from .core import ROOT, atomic_json, atomic_text, file_hash, read_json, home
from .models import MODELS, migrate_role
from .install import audit as legacy_audit
from .builtin import (skill_manifest, mcp_catalog, skill_changes, prepare_mcp_config,
                      validate_mcp_entries, restore_mcp_entries, prune_empty_skill_dirs)

START='<!-- ADHD-NATIVE:BEGIN -->'
END='<!-- ADHD-NATIVE:END -->'
NATIVE_SCHEMA_VERSION=3
INSTALL_SUBDIR='adhd'
RELEASE_DIRS=('adhd','skills','native','schemas','third_party','tests','config','bundled')
RELEASE_FILES=('adhd.py','hook.py','LICENSE','LICENSE-RAIBIT-MIT','THIRD_PARTY_NOTICES.md','requirements-documents.txt','README.md','README.ko.md','ADHD_PROVENANCE.md',
               'docs/large-tasks.md','docs/releases/0.1.4.md','docs/releases/0.1.5.md',
               'examples/large-task.json','examples/large-limits.json','examples/batch-checks.json')
WORKFLOW_SKILLS=('adhd-shape','adhd-challenge','adhd-decide','adhd-steer',
                 'adhd-unblock','adhd-retro','adhd-optimize')
NATIVE_CAPABILITIES=('adhd-native','adhd-memory','adhd-documents','adhd-extensions')+WORKFLOW_SKILLS

def _release_files(root:Path):
    for name in RELEASE_FILES:
        p=root/name
        if p.is_file():yield p
    for name in RELEASE_DIRS:
        base=root/name
        if not base.is_dir():continue
        for p in sorted(base.rglob('*')):
            if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'}:
                yield p

def _release_manifest(root:Path)->list[dict]:
    return [{'path':p.relative_to(root).as_posix(),'sha256':file_hash(p),'bytes':p.stat().st_size}
            for p in _release_files(root)]

def _compute_release_identity(root:Path)->dict:
    files=_release_manifest(root)
    raw=json.dumps([(row['path'],row['sha256']) for row in files],ensure_ascii=False,separators=(',',':')).encode()
    import hashlib
    code_digest=hashlib.sha256(raw).hexdigest()
    return {'code_digest':code_digest,'native_schema_version':NATIVE_SCHEMA_VERSION,
            'build_id':__version__+'-'+code_digest[:12],'files':files}

def release_identity(root:Path=ROOT)->dict:
    return _compute_release_identity(root)

def _copy_release(source:Path,dest:Path)->None:
    dest.mkdir(parents=True,exist_ok=False)
    for p in _release_files(source):
        out=dest/p.relative_to(source);out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,out)

def _render_release(release:Path,final_release:Path,source_root:Path)->None:
    """Expand install-time placeholders before the release identity is recorded."""
    for capability in NATIVE_CAPABILITIES:
        base=release/'skills'/capability
        if not base.is_dir():continue
        for source in sorted(base.rglob('*')):
            if not source.is_file():continue
            content=source.read_text(encoding='utf-8')
            content=content.replace('ADHD_ROOT',str(final_release)).replace('ADHD_PYTHON',str(sys.executable))
            # A release can invoke its own upgrade command. Rewrite references
            # embedded by that prior installation to the new immutable release.
            content=content.replace(str(source_root),str(final_release))
            source.write_text(content,encoding='utf-8')

def _validate_release_record(record:dict)->dict:
    release=Path(record.get('release','')).resolve()
    expected=record.get('release_identity') or {}
    if not release.is_dir():
        raise ValueError('Installed release is missing: '+str(release))
    files=expected.get('files')
    if not isinstance(files,list) or not files:
        raise ValueError('Installed release has no exact per-file manifest; reinstall is required')
    seen=set()
    manifest=[]
    for row in files:
        if not isinstance(row,dict) or not isinstance(row.get('path'),str):
            raise ValueError('Installed release manifest contains an invalid path')
        name=row['path']
        relative=PurePosixPath(name)
        if (not name or '\x00' in name or '\\' in name
                or re.match(r'^[A-Za-z]:',name) or relative.is_absolute()
                or name!=relative.as_posix()
                or any(part in {'', '.', '..'} for part in relative.parts)
                or name in seen):
            raise ValueError('Installed release manifest contains an unsafe or repeated path')
        seen.add(name)
        path=release.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(release):
            raise ValueError('Installed release file is missing or unsafe: '+name)
        if type(row.get('bytes')) is not int or path.stat().st_size!=row['bytes']:
            raise ValueError('Installed release file size changed: '+name)
        actual_hash=file_hash(path)
        if actual_hash!=row.get('sha256'):
            raise ValueError('Installed release file hash changed: '+name)
        manifest.append((name,actual_hash))
    actual_paths=set()
    for path in release.rglob('*'):
        if path.is_symlink():
            raise ValueError('Installed release contains a symlink: '+str(path))
        if (path.is_file() and '__pycache__' not in path.parts
                and path.suffix not in {'.pyc','.pyo'}):
            actual_paths.add(path.relative_to(release).as_posix())
    if actual_paths!=seen:
        raise ValueError('Installed release file inventory changed: '+str(release))
    raw=json.dumps(manifest,ensure_ascii=False,separators=(',',':')).encode()
    import hashlib
    code_digest=hashlib.sha256(raw).hexdigest()
    if code_digest!=expected.get('code_digest'):
        raise ValueError('Installed release manifest digest changed: '+str(release))
    return expected


def _managed_installations(target:Path)->list[tuple[Path,Path,dict]]:
    target=target.expanduser().resolve()
    found=[]
    try:
        children=list(target.iterdir())
    except OSError:
        return found
    for parent in children:
        if (not parent.is_dir() or parent.is_symlink()
                or (hasattr(parent,'is_junction') and parent.is_junction())):
            continue
        record_path=parent/'native-installation.json'
        if not record_path.is_file() or record_path.is_symlink():
            continue
        try:
            record=read_json(record_path,{})
        except (OSError,ValueError) as e:
            raise ValueError('Managed installation record is unreadable: '+str(record_path)) from e
        if not isinstance(record,dict):
            continue
        try:
            if Path(record.get('target','')).expanduser().resolve()!=target:
                continue
            release=Path(record.get('release','')).expanduser().resolve()
            if not release.is_relative_to((parent/'releases').resolve()):
                continue
        except (OSError,ValueError,RuntimeError):
            continue
        found.append((parent,record_path,record))
    return found


def _find_managed_installation(target:Path)->tuple[Path,Path,dict]|None:
    records=_managed_installations(target)
    if len(records)>1:
        raise ValueError('Multiple managed installation records found; resolve the duplicate records before continuing.')
    return records[0] if records else None

def command_line(argv: list[str], windows: bool | None=None) -> str:
    if any('\n' in x or '\r' in x or '\x00' in x for x in argv):
        raise ValueError('Newlines/nulls in executable paths are not supported')
    return subprocess.list2cmdline(argv) if (os.name=='nt' if windows is None else windows) else shlex.join(argv)

def read_bytes(p: Path):
    if p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()):
        raise ValueError('Refusing to replace a symlink/junction: '+str(p))
    return p.read_bytes() if p.exists() else None

def _hook_group_present(hooks:dict,event:str,group:dict)->bool:
    return sum(1 for item in hooks.get('hooks',{}).get(event,[]) if item==group)==1

def _validate_managed_row(row:dict)->None:
    p=Path(row['path']);kind=row.get('selector',{}).get('kind','file')
    if kind=='toml_mcp_entries':
        validate_mcp_entries(p,row['selector']['entries'])
    elif kind=='json_hook_groups':
        hooks=json.loads((read_bytes(p) or b'{}').decode('utf-8-sig'))
        if any(not _hook_group_present(hooks,x['event'],x['group']) for x in row['selector']['groups']):
            raise ValueError('Later edits detected in ADHD hook group: '+str(p))
    elif kind=='marker':
        data=read_bytes(p) or b'';managed=row['selector']['content'].encode()
        if data.count(managed)!=1:raise ValueError('Later edits detected in ADHD guidance marker: '+str(p))
    elif not p.is_file() or file_hash(p)!=row['installed_sha256']:
        raise ValueError('Later edits detected; nothing restored. Merge recorded backup for '+str(p))

def _restore_managed_row(row:dict)->None:
    p=Path(row['path']);kind=row.get('selector',{}).get('kind','file')
    if kind=='toml_mcp_entries':
        restore_mcp_entries(p,row['selector']['entries'],
                            Path(row['backup']) if row['existed'] else None,
                            row.get('installed_sha256'))
    elif kind=='json_hook_groups':
        hooks=json.loads((read_bytes(p) or b'{}').decode('utf-8-sig'))
        for item in reversed(row['selector']['groups']):
            groups=hooks['hooks'][item['event']];groups.remove(item['group'])
            if not groups:del hooks['hooks'][item['event']]
        backup=Path(row['backup']).read_bytes() if row['existed'] else None
        if backup is not None and hooks==json.loads(backup.decode('utf-8-sig')):p.write_bytes(backup)
        else:p.write_text(json.dumps(hooks,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    elif kind=='marker':
        data=read_bytes(p) or b'';managed=row['selector']['content'].encode();p.write_bytes(data.replace(managed,b'',1))
    elif row['existed']:
        p.write_bytes(Path(row['backup']).read_bytes())
    else:p.unlink()

def _validate_install_record(record:dict, *, legacy_migration:bool=False,
                             record_root:Path|None=None)->None:
    identity=record.get('release_identity') or {}
    if isinstance(identity.get('files'),list):
        _validate_release_record(record)
    elif not (legacy_migration and record.get('version') in {'0.1.0','0.1.1','0.1.2'}
              and identity.get('native_schema_version')==2
              and Path(record.get('release','')).is_dir()
              and record_root is not None
              and Path(record['release']).resolve().is_relative_to((record_root/'releases').resolve())):
        raise ValueError('Installed release has no exact file manifest; explicit migration is required')
    for row in record.get('files',[]):
        _validate_managed_row(row)
        if row['existed'] and (not Path(row['backup']).is_file() or file_hash(Path(row['backup']))!=row['old_sha256']):
            raise ValueError('Backup missing or modified; nothing restored')

def _install_native(target: Path, agents_home: Path|None=None, compact: bool=False,
                   migrate_models: bool=True, fixture_mode: bool=False, *,
                   managed_workflows: frozenset[str]=frozenset()) -> dict:
    target=target.expanduser().resolve()
    # This host discovers user skills from CODEX_HOME/skills. Keep an explicit
    # agents_home override for isolated tests and custom installations.
    agents_home=(agents_home or target).expanduser().resolve()
    parent=target/INSTALL_SUBDIR; parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(parent/'install-native.lock'),timeout=5):
        existing=_find_managed_installation(target)
        if existing and existing[0]!=parent:
            raise ValueError('A managed installation exists in another release directory; use upgrade to migrate it.')
        record_path=parent/'native-installation.json'
        old_record=read_json(record_path)
        if old_record:
            raise ValueError('Native overlay already installed. Use rollback-native first; it refuses to erase later user edits.')
        cfg_path=target/'config.toml'; original_cfg=read_bytes(cfg_path) or b''
        cfg_text=original_cfg.decode('utf-8-sig')
        if not fixture_mode and any(x in cfg_text for x in ['<USER_HOME>','<REDACTED_','PROJECT_001']):
            raise ValueError('Sanitized export detected. Install on the real working Codex home, not the exported placeholder configuration.')
        tomlkit.parse(cfg_text)  # validate, then preserve every original byte
        # Do not replace the selected model/effort, providers, router catalog,
        # project trust, permissions, MCPs or plugins.
        # Default parallelism is controlled by this overlay's hooks. Existing global
        # multi_agent_v2 configuration is preserved rather than introducing unknown keys.
        hooks_path=target/'hooks.json'
        hooks_text=read_bytes(hooks_path)
        hooks:dict[str,Any]=json.loads(hooks_text.decode('utf-8-sig')) if hooks_text else {}
        if not isinstance(hooks,dict) or not isinstance(hooks.get('hooks',{}),dict):
            raise ValueError('Existing hooks.json is not an object with hooks map')
        installed_skill=agents_home/'skills'/'adhd-native'/'SKILL.md'
        if installed_skill.exists(): raise ValueError('Unmanaged adhd-native skill already exists')
        for capability in NATIVE_CAPABILITIES:
            if capability in WORKFLOW_SKILLS:
                if capability not in managed_workflows:
                    continue  # Preserve an unmanaged helper folder as a whole.
                destination=agents_home/'skills'/capability
                if (destination.is_symlink()
                        or hasattr(destination,'is_junction') and destination.is_junction()
                        or destination.exists() and not destination.is_dir()):
                    raise ValueError('Managed workflow folder is unsafe: '+str(destination))
            base=ROOT/'skills'/capability
            for source in sorted(base.rglob('*')):
                if not source.is_file() or (capability=='adhd-native' and source.name=='SKILL.md'):continue
                dest=agents_home/'skills'/capability/source.relative_to(base)
                if dest.exists() or capability in managed_workflows and dest.is_symlink():
                    raise ValueError('Unmanaged capability already exists: '+str(dest))
        for p in (ROOT/'native'/'agents').glob('*.toml'):
            target_agent=target/'agents'/p.name
            if target_agent.exists():raise ValueError('Unmanaged native role already exists: '+str(target_agent))
        skill_manifest(ROOT)
        mcp_catalog(ROOT)
        source_identity=_compute_release_identity(ROOT)
        stamp=__version__+'-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        release=parent/'releases'/stamp
        backup=parent/'backups'/('native-'+stamp);backup.mkdir(parents=True)
        release_created=False
        if ROOT.resolve()!=release.resolve():
            staging=parent/'releases'/('.staging-'+uuid.uuid4().hex)
            try:
                _copy_release(ROOT,staging)
                _render_release(staging,release,ROOT)
                release.parent.mkdir(parents=True,exist_ok=True)
                os.replace(staging,release)
                release_created=True
            except BaseException:
                if staging.exists():shutil.rmtree(staging)
                raise
        identity=_compute_release_identity(release)
        cmd=command_line([sys.executable,str(release/'hook.py')]);hook_additions=[]
        for event in ['SessionStart','UserPromptSubmit','Stop','SubagentStart','SubagentStop','PostCompact','SessionEnd']:
            group={'hooks':[{'type':'command','command':cmd,'commandWindows':cmd,
                             'timeout':3 if event=='SessionEnd' else 20,
                             **({'additionalContextLimit':1200} if event in {'SessionStart','UserPromptSubmit','SubagentStart'} else {})}]}
            hooks.setdefault('hooks',{}).setdefault(event,[]).append(group)
            hook_additions.append({'event':event,'group':group})
        for event,matcher in [('PostToolUse','Bash|apply_patch|update_plan|mcp__.*'),('PreToolUse','Agent|spawn_agent')]:
            matched_group={'matcher':matcher,'hooks':[{'type':'command','command':cmd,'commandWindows':cmd,'timeout':20,'additionalContextLimit':1200}]}
            hooks['hooks'].setdefault(event,[]).append(matched_group);hook_additions.append({'event':event,'group':matched_group})
        # No trusted_hash or bypass is manufactured. Observe Codex's actual trust decision.
        changes: dict[Path,bytes]={hooks_path:(json.dumps(hooks,ensure_ascii=False,indent=2)+'\n').encode()}
        skill=(release/'skills'/'adhd-native'/'SKILL.md').read_text(encoding='utf-8')
        changes[installed_skill]=skill.encode()
        workflow_skills=[]
        for capability in NATIVE_CAPABILITIES:
            base=release/'skills'/capability
            if capability in WORKFLOW_SKILLS and (base/'SKILL.md').is_file():
                destination=agents_home/'skills'/capability
                preserved=(destination.exists() or destination.is_symlink()) and capability not in managed_workflows
                workflow_skills.append({'name':capability,
                    'status':'preserved_existing' if preserved else 'added',
                    'fallback':str(base/'SKILL.md')})
                if preserved:continue
            for source in sorted(base.rglob('*')):
                if not source.is_file() or (capability=='adhd-native' and source.name=='SKILL.md'): continue
                dest=agents_home/'skills'/capability/source.relative_to(base)
                content=source.read_text(encoding='utf-8')
                changes[dest]=content.encode()
        bundled_skill_changes,bundled_skills=skill_changes(agents_home,release)
        changes.update(bundled_skill_changes)
        bundled_cfg,bundled_mcp_entries,bundled_mcp=prepare_mcp_config(original_cfg,release)
        if bundled_mcp_entries:
            changes[cfg_path]=bundled_cfg
        for p in (release/'native'/'agents').glob('*.toml'):
            target_agent=target/'agents'/p.name
            text=p.read_text(encoding='utf-8').replace('ADHD_ROOT',str(release))
            tomlkit.parse(text)
            changes[target_agent]=text.encode()
        migrated=[]
        if migrate_models:
            for p in sorted((target/'agents').glob('*.toml')):
                text=(read_bytes(p) or b'').decode('utf-8-sig')
                data=tomlkit.parse(text); old=str(data.get('model',''))
                if re.fullmatch(r'gpt-(?:5(?:\.\d+)?|6)-(?:sol|luna|terra|astra)',old):
                    kind=migrate_role(p.stem);choice=MODELS[kind]
                    data['model']=choice['model'];data['model_reasoning_effort']=choice['effort']
                    if 'developer_instructions' in data:
                        instructions=str(data['developer_instructions'])
                        instructions=re.sub(r'gpt-(?:5(?:\.\d+)?|6)-(?:sol|luna|terra|astra)',choice['model'],instructions)
                        if kind=='astra': instructions+='\nUse only for bounded hard reasoning/design consultation, never routine implementation. Return a concise decision brief.\n'
                        data['developer_instructions']=tomlkit.string(instructions,multiline=True)
                    changes[p]=tomlkit.dumps(data).encode()
                    migrated.append({'file':p.name,'old_model':old,**choice})
        agents_path=target/'AGENTS.md'; original_agents=read_bytes(agents_path) or b''
        if START.encode() in original_agents: raise ValueError('Unmanaged native guidance already present')
        block=(START+'\n## '+DISPLAY_NAME+' v'+__version__+' (ordinary chat, App and CLI)\n'
          'For substantial research, study, reports, coding and games, read `'+str(installed_skill)+'` when the ADHD owner is active. '
          'Follow existing global and project instructions and explicit user-selected workflows. '
          'Select one loop owner; do not start a legacy loop inside an active native run. '
          'Keep the current model, reasoning effort, router, integrations and permissions unless the user changes them. '
          'Use original user text and real artifact evidence, not self-declared success. '\
          '\n'+END+'\n')
        if compact:
            legacy=parent/'legacy'/('AGENTS-'+stamp+'.md')
            changes[legacy]=original_agents
            block+='\nExisting specialized preferences and integrations remain available in `'+str(legacy)+'`. '
            block+='Read the relevant headings before a task that depends on them; explicit legacy-harness requests use that guidance. '
            block+='Compact mode changes when instructions are loaded; do not assume identical behavior to eager loading.\n'
            changes[agents_path]=block.encode()
        else:
            # Byte-preserving existing prefix, except intentional root/role model settings.
            changes[agents_path]=original_agents+b'\n\n'+block.encode()
        rows:list[dict[str,Any]]=[]
        for i,(p,content) in enumerate(changes.items()):
            old=read_bytes(p);bp=backup/f'{i:04d}.original'
            if old is not None: bp.write_bytes(old)
            row={'path':str(p),'existed':old is not None,'backup':str(bp),
                 'old_sha256':file_hash(bp) if old is not None else None}
            if p==hooks_path:row['selector']={'kind':'json_hook_groups','groups':hook_additions}
            elif p==cfg_path and bundled_mcp_entries:
                row['selector']={'kind':'toml_mcp_entries','entries':bundled_mcp_entries}
            elif p==agents_path and not compact:row['selector']={'kind':'marker','content':'\n\n'+block}
            else:row['selector']={'kind':'file'}
            rows.append(row)
        changed=[]
        try:
            for row,content in zip(rows,changes.values()):
                p=Path(row['path']);p.parent.mkdir(parents=True,exist_ok=True)
                current=read_bytes(p)
                expected=Path(row['backup']).read_bytes() if row['existed'] else None
                if current!=expected: raise ValueError('Concurrent user edit during install: '+str(p))
                temp=p.with_name(p.name+'.adhd-install.tmp');temp.write_bytes(content);os.replace(temp,p)
                row['installed_sha256']=file_hash(p);changed.append(row)
            result={'version':__version__,'display_version':DISPLAY_NAME+' v'+__version__,'release_identity':identity,
                'source_identity':source_identity,
                'target':str(target),'agents_home':str(agents_home),
                'release':str(release),'backup':str(backup),'files':rows,'migrated_roles':migrated,
                'builtin_skills':bundled_skills,'workflow_skills':workflow_skills,
                'builtin_mcp':bundled_mcp,
                'compact':compact,'native_hook_trust':'CHECK_IN_CODEX; no bypass used',
                 'models':'Existing model settings preserved; ADHD role pins require a live availability probe',
                'existing_v1_record_preserved':(parent/'installation.json').exists()}
            atomic_json(record_path,result)
            return result
        except BaseException:
            for row in reversed(changed):
                p=Path(row['path'])
                # Do not overwrite a concurrent edit even while rolling back an error.
                if p.exists() and file_hash(p)==row['installed_sha256']:
                    if row['existed']: p.write_bytes(Path(row['backup']).read_bytes())
                    else: p.unlink()
            prune_empty_skill_dirs(agents_home,bundled_skills+workflow_skills)
            if release_created and release.exists():shutil.rmtree(release)
            raise

def _rollback_native(target: Path, *, record_root:Path|None=None,
                     legacy_migration:bool=False) -> dict:
    target=target.expanduser().resolve()
    if record_root is None:
        managed=_find_managed_installation(target)
        if managed is None:raise ValueError('No native installation record')
        record_root=managed[0]
    parent=record_root.expanduser().resolve()
    with FileLock(str(parent/'install-native.lock'),timeout=5):
        p=parent/'native-installation.json';record=read_json(p)
        if not record: raise ValueError('No native installation record')
        # Validate ALL originals and current files before making any changes.
        _validate_install_record(record,legacy_migration=legacy_migration,record_root=parent)
        for row in reversed(record['files']):
            _restore_managed_row(row)
        prune_empty_skill_dirs(Path(record['agents_home']),
                               record.get('builtin_skills',[])+record.get('workflow_skills',[]))
        p.rename(parent/('native-uninstalled-'+uuid.uuid4().hex[:8]+'.json'))
        return {'restored_files':len(record['files']),'memory_and_backups_preserved':str(parent)}

def audit_native(target: Path) -> dict:
    report=legacy_audit(target)
    drift=[]
    try:
        managed=_find_managed_installation(target)
    except ValueError as e:
        managed=None
        drift.append(str(e))
    record_path=managed[1] if managed else target/INSTALL_SUBDIR/'native-installation.json'
    record=managed[2] if managed else {}
    report['native_installation']=managed is not None
    report['default_models']=MODELS
    report['warnings'].append('Native App path does not require a separate CLI, but Python and trusted supported hooks are required. Existing chats may retain their previous model selection.')
    try:
        if record:_validate_release_record(record)
    except (ValueError,OSError,json.JSONDecodeError):
        drift.append(str(record.get('release') or target/INSTALL_SUBDIR/'releases'))
    for row in record.get('files',[]):
        try:_validate_managed_row(row)
        except (ValueError,OSError,json.JSONDecodeError):drift.append(row['path'])
    report['installation_drift']=drift
    report['native_limits']='Native continuation/spawn limits are enforced only on observed hook paths; total token usage is unmeasured.'
    return report


# All managed installation changes share an outer operation lock. An upgrade is a
# checked rollback + install, with recovery, not an OS-wide atomic transaction.
def install_native(target:Path,agents_home:Path|None=None,compact:bool=False,migrate_models:bool=False,fixture_mode:bool=False)->dict:
    parent=target.expanduser().resolve()/INSTALL_SUBDIR;parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(parent/'native-operation.lock'),timeout=10):
        return _install_native(target,agents_home,compact,migrate_models,fixture_mode)


def rollback_native(target:Path)->dict:
    target=target.expanduser().resolve()
    managed=_find_managed_installation(target)
    if managed is None:raise ValueError('No native installation record')
    parent=managed[0]
    with FileLock(str(parent/'native-operation.lock'),timeout=10):
        return _rollback_native(target,record_root=parent)


def upgrade_native(target:Path,agents_home:Path|None=None,compact:bool=False,migrate_models:bool=False,fixture_mode:bool=False)->dict:
    target=target.expanduser().resolve();parent=target/INSTALL_SUBDIR;parent.mkdir(parents=True,exist_ok=True)
    managed=_find_managed_installation(target)
    if managed is None:
        with FileLock(str(parent/'native-operation.lock'),timeout=10):
            return _install_native(target,agents_home,compact,migrate_models,fixture_mode)
    record_root,rp,old=managed
    with ExitStack() as locks:
        for lock_root in sorted({parent,record_root},key=lambda path:str(path).casefold()):
            locks.enter_context(FileLock(str(lock_root/'native-operation.lock'),timeout=10))
        current=_find_managed_installation(target)
        if current is None or current[0]!=record_root:
            raise ValueError('Managed installation changed while waiting for its lock.')
        rp=current[1]
        old=read_json(rp)
        if not old:return _install_native(target,agents_home,compact,migrate_models,fixture_mode)
        # Validate the currently installed release before deciding whether an
        # upgrade is a no-op or restoring any managed user-facing files.
        legacy_migration=not isinstance((old.get('release_identity') or {}).get('files'),list)
        _validate_install_record(old,legacy_migration=legacy_migration,record_root=record_root)
        wanted=release_identity(ROOT);installed_source=old.get('source_identity',{})
        running_installed_release=ROOT.resolve()==Path(old['release']).resolve()
        if running_installed_release and legacy_migration:
            raise ValueError('Legacy release cannot self-upgrade without an exact file manifest; run upgrade from a reviewed source copy')
        if record_root==parent and (running_installed_release or (not legacy_migration and installed_source.get('code_digest')==wanted['code_digest'] and int(installed_source.get('native_schema_version',0))>=NATIVE_SCHEMA_VERSION)):
            return {'status':'already_installed','version':__version__,'release_identity':old['release_identity'],
                    'source_identity':wanted,
                    'next':'Installed content digest and native schema already match'}
        # Check every managed byte and original backup before restoring anything.
        current={}
        for row in old['files']:
            p=Path(row['path']);data=read_bytes(p);_validate_managed_row(row)
            current[str(p)]=data
        record_bytes=rp.read_bytes()
        destination_agents=(agents_home or Path(old['agents_home'])).expanduser().resolve()
        managed_workflows=frozenset(row['name'] for row in old.get('workflow_skills',[])
            if row.get('status')=='added' and row.get('name') in WORKFLOW_SKILLS
            and destination_agents==Path(old['agents_home']).expanduser().resolve())
        _rollback_native(target,record_root=record_root,legacy_migration=legacy_migration)
        rolled_back={str(Path(row['path'])):read_bytes(Path(row['path'])) for row in old['files']}
        try:
            result=_install_native(target,destination_agents,compact,migrate_models,fixture_mode,
                                   managed_workflows=managed_workflows)
            result['upgraded_from']=old['version'];return result
        except BaseException:
            # _install_native already rolls back its partial writes. Restore v1.1 only
            # where bytes still match the expected pre-overlay originals.
            for row in old['files']:
                p=Path(row['path']);expected=rolled_back[str(p)]
                if read_bytes(p)!=expected:raise ValueError('Concurrent edit during upgrade recovery; inspect backups instead of overwriting '+str(p))
            for name,data in current.items():
                p=Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            rp.write_bytes(record_bytes)
            raise
