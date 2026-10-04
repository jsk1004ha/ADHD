"""Reviewed extension staging, immutable local pins, activation and rollback.

No remote download/install scripts run here. Codex may obtain a requested package
inside its own approvals, then stage the exact local files. Optional MCP probing
uses the official Python SDK. A registered server is not called 'working'.
"""
from __future__ import annotations
import asyncio, importlib.util, json, os, re, shutil, subprocess, sys, time
from pathlib import Path
from typing import Any
from . import dependencies as _dependencies
from filelock import FileLock
from .core import ROOT, store, home, atomic_json, file_hash, read_json, digest
from .change_journal import apply_toml_entry, rollback as rollback_change
from .memory import SECRET


def _tree(root: Path) -> dict:
    if not root.is_dir() or root.is_symlink(): raise ValueError('Expected a real local skill directory')
    entries:dict[str,str]={}; total=0
    for p in sorted(root.rglob('*')):
        if p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()): raise ValueError('Skill links/junctions are not accepted')
        if p.is_file():
            total+=p.stat().st_size
            if total>8*1024*1024 or len(entries)>=256: raise ValueError('Extension exceeds 256-file/8-MiB review budget')
            entries[p.relative_to(root).as_posix()]=file_hash(p)
    if 'SKILL.md' not in entries: raise ValueError('SKILL.md missing')
    return entries


def _paths():
    root=store()/'extensions';root.mkdir(parents=True,exist_ok=True);return root


def _id(value):
    if not isinstance(value,str) or not re.fullmatch('[a-z][a-z0-9-]{1,63}',value):raise ValueError('Invalid extension id')
    return value


def _real_file(value, label='file') -> Path:
    p=Path(value or '').expanduser()
    if not p.is_absolute() or not p.is_file() or _linked_path(p):
        raise ValueError(f'Pin a real absolute {label}')
    return p.resolve()


def _linked_path(path:Path)->bool:
    path=path.absolute();parts=path.parts
    current=Path(parts[0])
    for part in parts[1:]:
        current=current/part
        if current.is_symlink() or (hasattr(current,'is_junction') and current.is_junction()):return True
    return False


def _pin_files(values, *, limit=512) -> dict[str,str]:
    if not isinstance(values,list) or len(values)>limit:raise ValueError(f'At most {limit} pinned runtime files')
    result={}
    for value in values:
        p=_real_file(value,'runtime file')
        result[str(p)]=file_hash(p)
    return result


def _runtime_tree(root:Path, *, limit=512, max_bytes=64*1024*1024)->dict[str,str]:
    """Hash the complete regular-file closure beneath a reviewed runtime root."""
    if not root.is_dir() or _linked_path(root):
        raise ValueError('runtime_root must be a real absolute directory')
    result:dict[str,str]={};total=0
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
            raise ValueError('Runtime links/junctions are not accepted')
        if path.is_file():
            total+=path.stat().st_size
            if len(result)>=limit or total>max_bytes:
                raise ValueError('Runtime closure exceeds 512-file/64-MiB review budget')
            result[str(path.resolve())]=file_hash(path)
    return result


def _script_closure(execution:dict, entry:Path, values)->tuple[Path,dict[str,str]]:
    root=Path(execution.get('runtime_root','')).expanduser()
    if not root.is_absolute() or not root.is_dir() or _linked_path(root):
        raise ValueError('script execution requires a real absolute runtime_root')
    root=root.resolve();declared=_pin_files(values)
    if not declared:
        raise ValueError('script execution requires the reviewed runtime_files closure')
    if not entry.is_relative_to(root) or str(entry) not in declared:
        raise ValueError('Script entrypoint must be inside runtime_root and listed in runtime_files')
    actual=_runtime_tree(root)
    if actual!=declared:
        raise ValueError('runtime_files must declare the complete local runtime_root closure')
    return root,declared


def validate_execution_spec(manifest:dict)->dict:
    execution=manifest.get('execution')
    if execution is None:
        # v1 compatibility is an explicitly bounded binary record.
        execution={'kind':'binary','command':manifest.get('command'),'args':manifest.get('args',[])}
    if not isinstance(execution,dict) or execution.get('kind') not in {'binary','script','module'}:
        raise ValueError('execution.kind must be binary, script or module')
    kind=execution['kind'];command=_real_file(execution.get('command'),'executable')
    if command.suffix.lower() in {'.cmd','.bat','.ps1','.sh'}:raise ValueError('Shell wrappers are not supported')
    args=execution.get('args',[])
    if not isinstance(args,list) or len(args)>32 or any(not isinstance(x,str) or len(x)>4000 for x in args):raise ValueError('Invalid args')
    if any(x in {'-c','-Command','--eval','-e'} for x in args):raise ValueError('Inline shell/eval programs are not supported')
    cwd=Path(execution.get('cwd') or command.parent).expanduser()
    if not cwd.is_absolute() or not cwd.is_dir() or _linked_path(cwd):
        raise ValueError('execution.cwd must be a real absolute directory')
    result={'kind':kind,'command':str(command),'command_sha256':file_hash(command),'args':args,'cwd':str(cwd.resolve())}
    runtime_files=execution.get('runtime_files',manifest.get('local_files',[]))
    if kind=='script':
        if command!=Path(sys.executable).resolve():
            raise ValueError('Script MCP execution currently supports the pinned Python interpreter only')
        entry=_real_file(execution.get('entrypoint'),'script entrypoint')
        if entry.suffix.lower()!='.py':
            raise ValueError('Script MCP entrypoint must be a Python file')
        runtime_root,runtime_pins=_script_closure(execution,entry,runtime_files)
        lock=execution.get('lock_file');lock_pin=None
        if lock:
            lp=_real_file(lock,'dependency lock');lock_pin={'path':str(lp),'sha256':file_hash(lp)}
        result.update(entrypoint=str(entry),entrypoint_sha256=file_hash(entry),
                      runtime_root=str(runtime_root),runtime_files=runtime_pins,lock=lock_pin)
    elif kind=='module':
        if command!=Path(sys.executable).resolve():
            raise ValueError('Module MCP execution needs the pinned Python interpreter')
        module=execution.get('module');root=Path(execution.get('package_root','')).expanduser()
        if not isinstance(module,str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*',module):raise ValueError('Invalid module name')
        if not root.is_absolute() or not root.is_dir() or _linked_path(root):raise ValueError('package_root must be a real absolute directory')
        root=root.resolve(); files=_pin_files(execution.get('installed_files',[]))
        if not files:raise ValueError('module execution requires the reviewed installed_files closure')
        if any(not Path(p).is_relative_to(root) for p in files):raise ValueError('Installed module file leaves package_root')
        if files!=_runtime_tree(root):raise ValueError('installed_files must cover the complete package_root closure')
        if cwd.resolve()!=root:raise ValueError('Module cwd must equal the reviewed package_root')
        lock=execution.get('lock_file');lock_pin=None
        if lock:
            lp=_real_file(lock,'dependency lock');lock_pin={'path':str(lp),'sha256':file_hash(lp)}
        result.update(module=module,package_root=str(root),installed_files=files,lock=lock_pin,
                      runtime_files=_pin_files(runtime_files))
    else:
        result['runtime_files']=_pin_files(runtime_files)
    return result


def validate_probe_call(value:dict|None)->dict|None:
    if value is None:return None
    if (not isinstance(value,dict) or set(value)!={'tool','arguments','read_only','purpose'} or
            not isinstance(value['tool'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',value['tool']) or
            not isinstance(value['arguments'],dict) or value['read_only'] is not True or
            not isinstance(value['purpose'],str) or not value['purpose'].strip()):
        raise ValueError('Probe call needs a named reviewed read-only tool, arguments and purpose')
    encoded=json.dumps(value['arguments'],ensure_ascii=False)
    if len(encoded)>4000 or SECRET.search(encoded) or re.search(r'(?i)"(?:password|secret|token|api[_-]?key)"\s*:',encoded):
        raise ValueError('Probe arguments are oversized or may contain a secret')
    if not re.match(r'(?i)^(?:read|get|list|search|inspect|describe|status|ping|health|query|fetch)(?:[_\-.]|$)',value['tool']):
        raise ValueError('Only clearly read-oriented tool names may be probed automatically')
    def mutating_keys(item):
        if isinstance(item,dict):
            return any(re.search(r'(?i)(?:write|delete|remove|update|create|send|post|publish|execute|command|shell)',str(key))
                       or mutating_keys(child) for key,child in item.items())
        return isinstance(item,list) and any(mutating_keys(child) for child in item)
    if mutating_keys(value['arguments']):
        raise ValueError('Probe arguments contain a potentially mutating field')
    return value


def stage(manifest: dict) -> dict:
    if not isinstance(manifest,dict) or manifest.get('kind') not in {'skill','mcp-stdio'}:raise ValueError('kind must be skill or mcp-stdio')
    eid=_id(manifest.get('id')); root=_paths(); dest=root/eid
    if dest.exists():raise ValueError('This extension id is already staged; rollback/archive it rather than silently updating')
    if not isinstance(manifest.get('source'),str) or not manifest['source'].strip():raise ValueError('Record upstream source/version/license in source')
    if SECRET.search(json.dumps(manifest)):raise ValueError('Do not put secrets in manifests; name required environment variables instead')
    record={'schema_version':2,'id':eid,'kind':manifest['kind'],'source':manifest['source'],'created':time.time(),'status':'staged','runtime_verified':False}
    if record['kind']=='skill':
        local=Path(manifest.get('local_path','')).expanduser().resolve();pins=_tree(local)
        text=(local/'SKILL.md').read_text(encoding='utf-8-sig')
        if not text.startswith('---') or not re.search(r'(?m)^name:\s*'+re.escape(eid)+r'\s*$',text):raise ValueError('Skill frontmatter name must match extension id')
        record.update(local_path=str(local),pins=pins)
    else:
        execution=validate_execution_spec(manifest);env=manifest.get('env_vars',[])
        if not isinstance(env,list) or any(not re.fullmatch('[A-Z_][A-Z0-9_]*',str(x)) for x in env):raise ValueError('env_vars must contain names only')
        if any(name.upper().startswith('PYTHON') for name in env):
            raise ValueError('Python interpreter environment variables cannot be inherited by an MCP extension')
        record.update(execution=execution,env_vars=env,
                      probe_call=validate_probe_call(manifest.get('probe_call')))
    dest.mkdir()
    if record['kind']=='skill':shutil.copytree(record['local_path'],dest/'payload')
    atomic_json(dest/'record.json',record)
    return {**record,'next':'Inspect source, permissions and scripts. Then apply --reviewed. MCP stays disabled until a real initialize/tools-list probe passes.'}


def validate_pins(record,root):
    if record['kind']=='skill':
        if _tree(root/'payload')!=record['pins']:raise ValueError('Staged skill changed after review')
    else:
        execution=record['execution']
        if file_hash(_real_file(execution['command'],'executable'))!=execution['command_sha256']:raise ValueError('MCP executable changed')
        if execution['kind']=='script':
            root=Path(execution['runtime_root'])
            if _runtime_tree(root)!=execution['runtime_files']:
                raise ValueError('Reviewed script runtime closure changed')
        if execution['kind']=='module' and _runtime_tree(Path(execution['package_root']))!=execution['installed_files']:
            raise ValueError('Reviewed module package closure changed')
        for field in ['runtime_files','installed_files']:
            for p,sha in execution.get(field,{}).items():
                if file_hash(_real_file(p,'runtime file'))!=sha:raise ValueError('Pinned MCP runtime file changed')
        if execution['kind']=='script' and file_hash(_real_file(execution['entrypoint'],'script entrypoint'))!=execution['entrypoint_sha256']:
            raise ValueError('Pinned MCP script changed')
        if execution.get('lock') and file_hash(_real_file(execution['lock']['path'],'dependency lock'))!=execution['lock']['sha256']:
            raise ValueError('Pinned dependency lock changed')


_pinned=validate_pins


def _launcher_entry(eid:str,codex_home:Path)->tuple[dict,dict]:
    from .native_install import release_identity
    launcher=(ROOT/'adhd.py').resolve()
    entry={'command':str(Path(sys.executable).resolve()),
           'args':[str(launcher),'extension','launch','--id',eid,'--codex-home',str(codex_home.resolve())],
           'env_vars':[],'enabled':False}
    pins={'python':file_hash(Path(sys.executable).resolve()),'launcher':str(launcher),
          'launcher_sha256':file_hash(launcher),'code_digest':release_identity(ROOT)['code_digest']}
    return entry,pins


def _owned_operations(root:Path,eid:str)->list[dict]:
    rows=[]
    for path in (root/'journal').glob('*.json'):
        row=read_json(path)
        if row and row.get('owner')=='extension:'+eid and row.get('phase')=='committed':rows.append(row)
    return sorted(rows,key=lambda row:(row.get('created_at',0),row['operation_id']))


def apply(eid:str,*,reviewed:bool=False,agents_home:Path|None=None,codex_home:Path|None=None)->dict:
    root=_paths()/_id(eid)
    with FileLock(str(_paths()/'registry.lock'),timeout=5):
        r=read_json(root/'record.json')
        if not r or r['status']!='staged':raise ValueError('No staged extension')
        if not reviewed:raise ValueError('Source/permission review required; --reviewed records the caller assertion, not a security audit')
        validate_pins(r,root)
        if r['kind']=='skill':
            dest=(agents_home or Path.home()/'.agents')/'skills'/eid
            if dest.exists():raise ValueError('Existing skill preserved; choose a new id or explicit migration')
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(root/'payload',dest)
            r.update(target=str(dest),status='installed_reload_required')
        else:
            codex_home=(codex_home or home()).expanduser().resolve();cfgp=codex_home/'config.toml';cfgp.parent.mkdir(parents=True,exist_ok=True)
            if cfgp.is_symlink():raise ValueError('Config symlink refused')
            entry,launcher_pins=_launcher_entry(eid,codex_home)
            journal_root=root/'journal'
            existing=_owned_operations(root,eid)
            if existing and existing[-1].get('after',{}).get('value')==entry:
                op=existing[-1]
            else:
                op=apply_toml_entry(cfgp,['mcp_servers',eid],entry,owner='extension:'+eid,
                                    journal_root=journal_root,require_absent=True)
            r.update(config=str(cfgp),entry=entry,launcher=launcher_pins,journal=[op['operation_id']],status='registered_disabled')
        r['reviewed_at']=time.time();atomic_json(root/'record.json',r);return r


def _target_argv(r:dict)->list[str]:
    execution=r['execution'];argv=[execution['command']]
    if execution['kind']=='script':argv.extend(['-S',execution['entrypoint']])
    elif execution['kind']=='module':argv.extend(['-S','-m',execution['module']])
    return argv+execution['args']


def _runtime_env(r:dict)->dict[str,str]:
    keys={'PATH','HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP','SYSTEMROOT'}|set(r['env_vars'])
    env={k:os.environ[k] for k in keys if k in os.environ and not k.upper().startswith('PYTHON')}
    if r['execution']['kind']=='module':
        env['PYTHONNOUSERSITE']='1';env['PYTHONPATH']=r['execution']['package_root']
        env['PYTHONDONTWRITEBYTECODE']='1'
    elif r['execution']['kind']=='script':
        env['PYTHONNOUSERSITE']='1';env['PYTHONDONTWRITEBYTECODE']='1'
    return env


def launch(eid:str,*,probe_mode:bool=False)->int:
    root=_paths()/_id(eid);r=read_json(root/'record.json')
    allowed={'active_reload_required'}|({'registered_disabled','tool_call_required',
                                      'probe_failed','auth_required','sdk_missing'} if probe_mode else set())
    if not r or r.get('kind')!='mcp-stdio' or r.get('status') not in allowed:raise ValueError('MCP is not active')
    validate_pins(r,root)
    launcher=r.get('launcher',{})
    if launcher:
        from .native_install import release_identity
        if (file_hash(Path(sys.executable).resolve())!=launcher['python'] or
            file_hash(Path(launcher['launcher']))!=launcher['launcher_sha256'] or
            release_identity(ROOT)['code_digest']!=launcher.get('code_digest')):
            raise ValueError('ADHD extension launcher changed; review and reapply the extension')
    missing=[k for k in r['env_vars'] if k not in os.environ]
    if missing:raise ValueError('Required MCP environment names are missing: '+', '.join(missing))
    # Inherited streams keep stdout exclusively available for MCP JSON-RPC.
    return subprocess.Popen(_target_argv(r),cwd=r['execution']['cwd'],env=_runtime_env(r),
                            stdin=None,stdout=None,stderr=None,shell=False).wait()


async def probe_session(session, call:dict|None=None)->dict:
    """Observe one reviewed read; support the official SDK's v1/v2 attributes."""
    call=validate_probe_call(call)
    init=await session.initialize(); tools=await session.list_tools()
    info=getattr(init,'server_info',getattr(init,'serverInfo',None))
    if info is None:raise ValueError('Live MCP initialize returned no server identity')
    result={'server':info.model_dump(),'tools':[t.name for t in tools.tools],
            'connected':True,'tool_call':None}
    if call is not None:
        tool=next((t for t in tools.tools if t.name==call['tool']),None)
        if tool is None:raise ValueError('Reviewed probe tool is absent from live server')
        annotations=getattr(tool,'annotations',None)
        hint=getattr(annotations,'read_only_hint',getattr(annotations,'readOnlyHint',None))
        if hint is not True:raise ValueError('Live MCP tool does not declare readOnlyHint=true')
        response=await session.call_tool(call['tool'],arguments=call['arguments'])
        if getattr(response,'is_error',getattr(response,'isError',False)):
            raise ValueError('Live MCP probe tool returned isError')
        serialized=response.model_dump(mode='json')
        if 'structured_content' in serialized and 'structuredContent' not in serialized:
            serialized['structuredContent']=serialized.pop('structured_content')
        if not serialized.get('content') and not serialized.get('structuredContent'):
            raise ValueError('Live MCP probe returned no observable result')
        result['tool_call']={'tool':call['tool'],'request_sha256':digest(call['arguments']),
            'result_sha256':digest(serialized),'read_only_hint_observed':True,
            'status':'succeeded','observed_at':time.time()}
    return result


async def _probe_stdio(r):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    # Pass only named credentials plus minimal runtime environment, not all secrets.
    params=StdioServerParameters(command=str(Path(sys.executable).resolve()),
        args=[str((ROOT/'adhd.py').resolve()),'extension','launch','--id',r['id'],'--codex-home',str(Path(r['config']).parent),'--probe-launch'],
        env=_runtime_env(r))
    async with stdio_client(params) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            return await probe_session(session,r.get('probe_call'))


def probe(eid:str,*,timeout:int=25)->dict:
    root=_paths()/_id(eid);r=read_json(root/'record.json')
    if not r or r['kind']!='mcp-stdio' or r['status'] not in {'registered_disabled','tool_call_required','probe_failed','auth_required','sdk_missing'}:raise ValueError('Register the reviewed MCP first')
    validate_pins(r,root)
    missing=[k for k in r['env_vars'] if k not in os.environ]
    if missing:
        r.update(status='auth_required',missing_environment_names=missing,runtime_verified=False)
        atomic_json(root/'record.json',r)
        return r
    if not importlib.util.find_spec('mcp'):
        r.update(status='sdk_missing',runtime_verified=False,
                 next='Install the official mcp Python SDK through the existing Codex approval flow, then repeat the probe')
        atomic_json(root/'record.json',r)
        return r
    if not 1<=timeout<=120:raise ValueError('Probe timeout 1..120 seconds')
    try:
        result=asyncio.run(asyncio.wait_for(_probe_stdio(r),timeout=timeout))
    except (ValueError,RuntimeError,TimeoutError,OSError) as error:
        with FileLock(str(_paths()/'registry.lock'),timeout=5):
            r.update(status='probe_failed',runtime_verified=False,tool_call_verified=False,
                     probe_error={'type':type(error).__name__,'detail':str(error)[:500]},probed_at=time.time())
            atomic_json(root/'record.json',r)
        return {**r,'limitation':'MCP probe failed; registration remains disabled.'}
    with FileLock(str(_paths()/'registry.lock'),timeout=5):
        for stale in ('missing_environment_names','next','probe_error'):
            r.pop(stale,None)
        if not result.get('tool_call'):
            r.update(status='tool_call_required',runtime_verified=False,tool_call_verified=False,
                     probe=result,probed_at=time.time())
            atomic_json(root/'record.json',r)
            return {**r,'limitation':'Initialize/tools-list succeeded, but no reviewed read-only tool call was observed. MCP remains disabled.'}
        call=result['tool_call']
        if (not r.get('probe_call') or call.get('tool')!=r['probe_call']['tool'] or
                call.get('request_sha256')!=digest(r['probe_call']['arguments']) or
                call.get('status')!='succeeded' or call.get('read_only_hint_observed') is not True or
                not re.fullmatch('[0-9a-f]{64}',str(call.get('result_sha256','')))):
            raise ValueError('MCP call receipt does not match the reviewed probe')
        cfgp=Path(r['config']);entry={**r['entry'],'enabled':True}
        op=apply_toml_entry(cfgp,['mcp_servers',eid],entry,owner='extension:'+eid,
                            journal_root=root/'journal')
        r['journal'].append(op['operation_id']);r['entry']=entry
        r.update(status='active_reload_required',runtime_verified=True,tool_call_verified=True,
                 probe=result,probed_at=time.time())
        atomic_json(root/'record.json',r)
    return {**r,'limitation':'A reviewed read-only tool call succeeded; only that call is verified. Reload Codex tool discovery for host use.'}


def rollback(eid:str)->dict:
    root=_paths()/_id(eid)
    with FileLock(str(_paths()/'registry.lock'),timeout=5):
        r=read_json(root/'record.json')
        if not r:raise ValueError('Unknown extension')
        if r['kind']=='skill' and 'target' in r:
            dest=Path(r['target'])
            if _tree(dest)!=r['pins']:raise ValueError('Later skill edits detected; refusing removal')
            shutil.rmtree(dest)
        elif r['kind']=='mcp-stdio' and 'config' in r:
            operations=list(dict.fromkeys(r.get('journal',[])+[x['operation_id'] for x in _owned_operations(root,eid)]))
            for operation in reversed(operations):
                rollback_change(operation,journal_root=root/'journal')
        r.update(status='rolled_back',runtime_verified=False);atomic_json(root/'record.json',r);return r


def status(eid:str|None=None):
    return read_json(_paths()/_id(eid)/'record.json') if eid else [read_json(p) for p in _paths().glob('*/record.json')]
