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
from typing import Any
from . import DISPLAY_NAME, __version__
from . import dependencies as _dependencies
import tomlkit
from filelock import FileLock
from .core import ROOT, atomic_json, atomic_text, file_hash, read_json, home
from .models import MODELS, migrate_role
from .install import audit as legacy_audit

START='<!-- APZN-NATIVE:BEGIN -->'
END='<!-- APZN-NATIVE:END -->'
NATIVE_SCHEMA_VERSION=3
INSTALL_SUBDIR='adhd'
LEGACY_INSTALL_SUBDIR='apzn'
RELEASE_DIRS=('apzn','skills','native','schemas','third_party','tests')
RELEASE_FILES=('adhd.py','apzn.py','hook.py','LICENSE','LICENSE-RAIBIT-MIT','THIRD_PARTY_NOTICES.md','requirements-documents.txt','README.md','README.ko.md','APZN_PROVENANCE.md')

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
    for capability in ['apzn-native','apzn-memory','apzn-documents','apzn-extensions']:
        base=release/'skills'/capability
        if not base.is_dir():continue
        for source in sorted(base.rglob('*')):
            if not source.is_file():continue
            content=source.read_text(encoding='utf-8')
            content=content.replace('APZN_ROOT',str(final_release)).replace('APZN_PYTHON',str(sys.executable))
            # A release can invoke its own upgrade command. Rewrite references
            # embedded by that prior installation to the new immutable release.
            content=content.replace(str(source_root),str(final_release))
            source.write_text(content,encoding='utf-8')

def _validate_release_record(record:dict)->dict:
    release=Path(record.get('release',''))
    expected=record.get('release_identity') or {}
    if not release.is_dir():
        raise ValueError('Installed APZN release is missing: '+str(release))
    if not isinstance(expected.get('files'),list):
        raise ValueError('Installed release has no exact per-file manifest; reinstall is required')
    actual=_compute_release_identity(release)
    if actual.get('code_digest')!=expected.get('code_digest') or actual.get('files')!=expected.get('files'):
        raise ValueError('Installed APZN release bytes were modified: '+str(release))
    return actual

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
    if kind=='json_hook_groups':
        hooks=json.loads((read_bytes(p) or b'{}').decode('utf-8-sig'))
        if any(not _hook_group_present(hooks,x['event'],x['group']) for x in row['selector']['groups']):
            raise ValueError('Later edits detected in APZN hook group: '+str(p))
    elif kind=='marker':
        data=read_bytes(p) or b'';managed=row['selector']['content'].encode()
        if data.count(managed)!=1:raise ValueError('Later edits detected in APZN guidance marker: '+str(p))
    elif not p.is_file() or file_hash(p)!=row['installed_sha256']:
        raise ValueError('Later edits detected; nothing restored. Merge recorded backup for '+str(p))

def _restore_managed_row(row:dict)->None:
    p=Path(row['path']);kind=row.get('selector',{}).get('kind','file')
    if kind=='json_hook_groups':
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

def _validate_install_record(record:dict, *, legacy_migration:bool=False)->None:
    identity=record.get('release_identity') or {}
    if isinstance(identity.get('files'),list):
        _validate_release_record(record)
    elif not (legacy_migration and record.get('version') in {'0.1.0','0.1.1','0.1.2'}
              and identity.get('native_schema_version')==2
              and Path(record.get('release','')).is_dir()
              and Path(record['release']).resolve().is_relative_to(Path(record['target']).resolve()/'apzn'/'releases')):
        raise ValueError('Installed APZN release has no exact file manifest; explicit legacy migration is required')
    for row in record.get('files',[]):
        _validate_managed_row(row)
        if row['existed'] and (not Path(row['backup']).is_file() or file_hash(Path(row['backup']))!=row['old_sha256']):
            raise ValueError('Backup missing or modified; nothing restored')

def _install_native(target: Path, agents_home: Path|None=None, compact: bool=False,
                   migrate_models: bool=True, fixture_mode: bool=False) -> dict:
    target=target.expanduser().resolve()
    # This host discovers user skills from CODEX_HOME/skills. Keep an explicit
    # agents_home override for isolated tests and custom installations.
    agents_home=(agents_home or target).expanduser().resolve()
    parent=target/INSTALL_SUBDIR; parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(parent/'install-native.lock'),timeout=5):
        record_path=parent/'native-installation.json'
        old_record=read_json(record_path)
        if old_record:
            raise ValueError('Native overlay already installed. Use rollback-native first; it refuses to erase later user edits.')
        if INSTALL_SUBDIR!=LEGACY_INSTALL_SUBDIR and (target/LEGACY_INSTALL_SUBDIR/'native-installation.json').exists():
            raise ValueError('Existing APZN installation found; use upgrade to migrate the verified installation.')
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
        installed_skill=agents_home/'skills'/'apzn-native'/'SKILL.md'
        if installed_skill.exists(): raise ValueError('Unmanaged apzn-native skill already exists')
        for capability in ['apzn-native','apzn-memory','apzn-documents','apzn-extensions']:
            base=ROOT/'skills'/capability
            for source in sorted(base.rglob('*')):
                if not source.is_file() or (capability=='apzn-native' and source.name=='SKILL.md'):continue
                dest=agents_home/'skills'/capability/source.relative_to(base)
                if dest.exists():raise ValueError('Unmanaged capability already exists: '+str(dest))
        for p in (ROOT/'native'/'agents').glob('*.toml'):
            target_agent=target/'agents'/p.name
            if target_agent.exists():raise ValueError('Unmanaged native role already exists: '+str(target_agent))
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
        skill=(release/'skills'/'apzn-native'/'SKILL.md').read_text(encoding='utf-8')
        changes[installed_skill]=skill.encode()
        for capability in ['apzn-native','apzn-memory','apzn-documents','apzn-extensions']:
            base=release/'skills'/capability
            for source in sorted(base.rglob('*')):
                if not source.is_file() or (capability=='apzn-native' and source.name=='SKILL.md'): continue
                dest=agents_home/'skills'/capability/source.relative_to(base)
                content=source.read_text(encoding='utf-8')
                changes[dest]=content.encode()
        for p in (release/'native'/'agents').glob('*.toml'):
            target_agent=target/'agents'/p.name
            text=p.read_text(encoding='utf-8').replace('APZN_ROOT',str(release))
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
          'For substantial research, study, reports, coding and games, read `'+str(installed_skill)+'` when the APZN owner is active. '
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
                temp=p.with_name(p.name+'.apzn-install.tmp');temp.write_bytes(content);os.replace(temp,p)
                row['installed_sha256']=file_hash(p);changed.append(row)
            result={'version':__version__,'display_version':DISPLAY_NAME+' v'+__version__,'release_identity':identity,
                'source_identity':source_identity,
                'target':str(target),'agents_home':str(agents_home),
                'release':str(release),'backup':str(backup),'files':rows,'migrated_roles':migrated,
                'compact':compact,'native_hook_trust':'CHECK_IN_CODEX; no bypass used',
                 'models':'Existing model settings preserved; APZN role pins require a live availability probe',
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
            if release_created and release.exists():shutil.rmtree(release)
            raise

def _rollback_native(target: Path, *, record_subdir:str|None=None, legacy_migration:bool=False) -> dict:
    target=target.expanduser().resolve()
    if record_subdir is None:
        record_subdir=(INSTALL_SUBDIR if (target/INSTALL_SUBDIR/'native-installation.json').exists()
                       else LEGACY_INSTALL_SUBDIR)
    parent=target/record_subdir;parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(parent/'install-native.lock'),timeout=5):
        p=parent/'native-installation.json';record=read_json(p)
        if not record: raise ValueError('No native installation record')
        # Validate ALL originals and current files before making any changes.
        _validate_install_record(record,legacy_migration=legacy_migration)
        for row in reversed(record['files']):
            _restore_managed_row(row)
        p.rename(parent/('native-uninstalled-'+uuid.uuid4().hex[:8]+'.json'))
        return {'restored_files':len(record['files']),'memory_and_backups_preserved':str(parent)}

def audit_native(target: Path) -> dict:
    report=legacy_audit(target)
    primary=target/INSTALL_SUBDIR/'native-installation.json'
    legacy=target/LEGACY_INSTALL_SUBDIR/'native-installation.json'
    record_path=primary if primary.exists() else legacy
    report['native_installation']=record_path.exists()
    report['default_models']=MODELS
    report['warnings'].append('Native App path does not require a separate CLI, but Python and trusted supported hooks are required. Existing chats may retain their previous model selection.')
    record=read_json(record_path,{})
    drift=[]
    if primary.exists() and legacy.exists() and primary!=legacy:
        drift.append('Multiple active native installation records')
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
    record_subdir=(INSTALL_SUBDIR if (target/INSTALL_SUBDIR/'native-installation.json').exists()
                   else LEGACY_INSTALL_SUBDIR)
    parent=target/record_subdir;parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(parent/'native-operation.lock'),timeout=10):
        return _rollback_native(target,record_subdir=record_subdir)


def upgrade_native(target:Path,agents_home:Path|None=None,compact:bool=False,migrate_models:bool=False,fixture_mode:bool=False)->dict:
    target=target.expanduser().resolve();parent=target/INSTALL_SUBDIR;parent.mkdir(parents=True,exist_ok=True)
    legacy_parent=target/LEGACY_INSTALL_SUBDIR
    with ExitStack() as locks:
        locks.enter_context(FileLock(str(parent/'native-operation.lock'),timeout=10))
        if legacy_parent!=parent and legacy_parent.exists():
            locks.enter_context(FileLock(str(legacy_parent/'native-operation.lock'),timeout=10))
        rp=parent/'native-installation.json'
        record_subdir=INSTALL_SUBDIR
        if not rp.exists() and (legacy_parent/'native-installation.json').exists():
            rp=legacy_parent/'native-installation.json'
            record_subdir=LEGACY_INSTALL_SUBDIR
        old=read_json(rp)
        if not old:return _install_native(target,agents_home,compact,migrate_models,fixture_mode)
        # Validate the currently installed release before deciding whether an
        # upgrade is a no-op or restoring any managed user-facing files.
        legacy_migration=not isinstance((old.get('release_identity') or {}).get('files'),list)
        _validate_install_record(old,legacy_migration=legacy_migration)
        wanted=release_identity(ROOT);installed_source=old.get('source_identity',{})
        running_installed_release=ROOT.resolve()==Path(old['release']).resolve()
        if running_installed_release and legacy_migration:
            raise ValueError('Legacy release cannot self-upgrade without an exact file manifest; run upgrade from a reviewed source copy')
        if record_subdir==INSTALL_SUBDIR and (running_installed_release or (not legacy_migration and installed_source.get('code_digest')==wanted['code_digest'] and int(installed_source.get('native_schema_version',0))>=NATIVE_SCHEMA_VERSION)):
            return {'status':'already_installed','version':__version__,'release_identity':old['release_identity'],
                    'source_identity':wanted,
                    'next':'Installed content digest and native schema already match'}
        # Check every managed byte and original backup before restoring anything.
        current={}
        for row in old['files']:
            p=Path(row['path']);data=read_bytes(p);_validate_managed_row(row)
            current[str(p)]=data
        record_bytes=rp.read_bytes()
        _rollback_native(target,record_subdir=record_subdir,legacy_migration=legacy_migration)
        rolled_back={str(Path(row['path'])):read_bytes(Path(row['path'])) for row in old['files']}
        try:
            result=_install_native(target,agents_home or Path(old['agents_home']),compact,migrate_models,fixture_mode)
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
