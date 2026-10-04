"""Bounded, streaming snapshots for ordinary artifacts and render bundles."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from .core import digest


@dataclass(frozen=True)
class SnapshotPolicy:
    max_artifacts: int = 150
    max_bundle_files: int = 220
    max_task_bundle_files: int = 20000
    max_task_bundle_bytes: int = 128 * 1024 * 1024 * 1024
    max_task_file_bytes: int = 64 * 1024 * 1024 * 1024
    max_task_manifest_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_file_bytes: int = 512 * 1024 * 1024
    max_large_file_bytes: int = 64 * 1024 * 1024 * 1024
    max_large_total_bytes: int = 128 * 1024 * 1024 * 1024
    max_manifest_bytes: int = 2 * 1024 * 1024
    max_control_json_bytes: int = 2 * 1024 * 1024
    max_validation_seconds: float = 120.0


DEFAULT_POLICY = SnapshotPolicy()


def _policy(value: SnapshotPolicy | dict[str, Any] | None) -> SnapshotPolicy:
    if value is None:return DEFAULT_POLICY
    if isinstance(value,SnapshotPolicy):return value
    if not isinstance(value,dict):raise ValueError('Snapshot policy must be a SnapshotPolicy or mapping')
    allowed=set(SnapshotPolicy.__dataclass_fields__)
    if set(value)-allowed:raise ValueError('Unknown snapshot policy field')
    result=SnapshotPolicy(**value)
    for name,number in asdict(result).items():
        if not isinstance(number,(int,float)) or number<=0:raise ValueError('Snapshot policy limits must be positive')
    return result


def _deadline(start: float, policy: SnapshotPolicy) -> None:
    if time.monotonic()-start>policy.max_validation_seconds:raise TimeoutError('Snapshot validation time limit exceeded; result is unverified')


def _stream_record(path: Path, relative: str, policy: SnapshotPolicy, start: float, *, control_json: bool=False, byte_limit: int|None=None, allow_empty: bool=False) -> dict:
    before=path.stat()
    limit=byte_limit if byte_limit is not None else (policy.max_control_json_bytes if control_json else policy.max_file_bytes)
    if before.st_size==0 and not allow_empty:raise ValueError('Snapshot file is empty: '+relative)
    if before.st_size>limit:raise ValueError('Snapshot file exceeds its configured size limit: '+relative)
    h=hashlib.sha256();data=bytearray() if control_json else None
    with path.open('rb') as handle:
        while chunk:=handle.read(1024*1024):
            h.update(chunk)
            if data is not None:data.extend(chunk)
            _deadline(start,policy)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('File changed during snapshot: '+relative)
    record={'path':relative,'size':after.st_size,'sha256':h.hexdigest()}
    if data is not None:
        try:json.loads(data.decode('utf-8-sig'))
        except (UnicodeDecodeError,json.JSONDecodeError) as e:raise ValueError('Invalid bounded control JSON: '+relative) from e
        record['control_json_validated']=True
    return record


def _relative_file(workspace: Path, value: str) -> tuple[Path,str]:
    if not isinstance(value,str) or not value or '\x00' in value:raise ValueError('Invalid snapshot relative path')
    relative=Path(value.replace('\\','/'))
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('Snapshot path must stay relative to the workspace')
    cursor=workspace
    for part in relative.parts:
        cursor=cursor/part
        if cursor.is_symlink() or (hasattr(cursor,'is_junction') and cursor.is_junction()):raise ValueError('Snapshot paths cannot traverse links or junctions')
    path=cursor.resolve()
    if not path.is_relative_to(workspace) or not path.is_file():raise ValueError('Snapshot leaf must be a regular workspace file: '+str(value))
    return path,path.relative_to(workspace).as_posix()


def _reject_path_links(path: Path, message: str) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        if part in {'', '.'}:
            continue
        if part == '..':
            cursor = cursor.parent
            continue
        cursor = cursor / part
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise ValueError(message)


def _manifest_leaf(workspace: Path, value: Any, label: str) -> tuple[Path,str]:
    if not isinstance(value,str) or not value or not Path(value).is_absolute():raise ValueError(label+' must use the absolute render-manifest path format')
    path = Path(value)
    if not path.is_file():raise ValueError(label+' leaves the workspace or is missing')
    _reject_path_links(path, label+' cannot traverse links or junctions')
    try:relative=path.resolve().relative_to(workspace)
    except ValueError:raise ValueError(label+' leaves the workspace or is missing')
    return _relative_file(workspace,relative.as_posix())


def _render_bundle(workspace: Path, relative_manifest: str, policy: SnapshotPolicy, start: float) -> dict:
    manifest_path,manifest_rel=_relative_file(workspace,relative_manifest)
    if manifest_path.stat().st_size>policy.max_manifest_bytes:raise ValueError('Render manifest exceeds snapshot limit')
    manifest_record=_stream_record(manifest_path,manifest_rel,policy,start,control_json=True,byte_limit=policy.max_manifest_bytes)
    manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    pages=manifest.get('pages')
    if not isinstance(pages,list) or not pages or manifest.get('page_count')!=len(pages):raise ValueError('Render bundle has incomplete page metadata')
    if [row.get('page') for row in pages]!=list(range(1,len(pages)+1)):raise ValueError('Render bundle pages must be complete and ordered')
    values=[('pdf',manifest.get('pdf'))]+[(f'page {row.get("page")}',row.get('image')) for row in pages]
    if len(values)>policy.max_bundle_files:raise ValueError('Render bundle exceeds leaf-file limit')
    resolved=[];seen={manifest_rel}
    for label,value in values:
        path,relative=_manifest_leaf(workspace,value,label)
        if relative in seen:raise ValueError('Duplicate render bundle leaf: '+relative)
        seen.add(relative)
        resolved.append((label,path,relative))
    source,_=_manifest_leaf(workspace,manifest.get('source'),'render source')
    from .documents import validate_render
    render_validation=validate_render(manifest,source)
    if not render_validation['derivation_verified']:raise ValueError('Render bundle needs a current derivation-verified manifest')
    leaves=[]
    for label,path,relative in resolved:
        record=_stream_record(path,relative,policy,start)
        expected=manifest.get('pdf_sha256') if label=='pdf' else pages[len(leaves)-1].get('sha256')
        if record['sha256']!=expected:raise ValueError('Render bundle leaf hash differs from manifest: '+relative)
        leaves.append(record)
    return {'kind':'render_bundle','manifest':manifest_record,'page_count':render_validation['actual_page_count'],'leaves':leaves,
            'bundle_digest':digest({'manifest':manifest_record,'leaves':leaves})}


def _task_bundle(workspace: Path, relative: str, policy: SnapshotPolicy, start: float) -> dict:
    path, rel = _relative_file(workspace, relative)
    manifest_record = _stream_record(path, rel, policy, start, control_json=True, byte_limit=policy.max_task_manifest_bytes)
    value = json.loads(path.read_text(encoding='utf-8'))
    if digest({k: v for k, v in value.items() if k != 'snapshot_digest'}) != value.get('snapshot_digest'):
        raise ValueError('Task bundle has an invalid frozen digest')
    stage = Path(value.get('staging_workspace', ''))
    if not stage.is_absolute() or not stage.is_dir():
        raise ValueError('Task bundle workspace escapes snapshot workspace')
    _reject_path_links(stage, 'Task bundle workspace cannot traverse links or junctions')
    stage = stage.resolve()
    if not stage.is_relative_to(workspace):
        raise ValueError('Task bundle workspace escapes snapshot workspace')
    stage_relative = stage.relative_to(workspace)
    files = value.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= policy.max_task_bundle_files:
        raise ValueError('Task bundle file count exceeds policy')
    leaves, task_bytes = [], 0
    for name, sha in sorted(files.items()):
        if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name.replace('\\', '/')).parts:
            raise ValueError('Task bundle leaf escapes staging workspace')
        source, leaf_rel = _relative_file(workspace, (stage_relative / name).as_posix())
        record = _stream_record(source, leaf_rel, policy, start, allow_empty=True, byte_limit=policy.max_task_file_bytes)
        task_bytes += record['size']
        if task_bytes > policy.max_task_bundle_bytes:
            raise ValueError('Task bundle exceeds its byte budget')
        if record['sha256'] != sha:
            raise ValueError('Task bundle file changed')
        leaves.append(record)
    return {'kind': 'task_bundle', 'manifest': manifest_record, 'leaves': leaves,
            'snapshot_digest': value['snapshot_digest'], 'task_bytes': task_bytes}


def build_snapshot(workspace: Path, artifacts: list[Any], *, policy: SnapshotPolicy | dict[str,Any] | None=None) -> dict:
    workspace=Path(workspace).resolve();rules=_policy(policy);start=time.monotonic()
    if not isinstance(artifacts,list) or not artifacts or len(artifacts)>rules.max_artifacts:raise ValueError('Snapshot needs 1..max_artifacts descriptors')
    rows=[];claimed=set();total=0;large_total=0
    for artifact in artifacts:
        _deadline(start,rules)
        if isinstance(artifact,str):
            path,relative=_relative_file(workspace,artifact)
            if relative in claimed:raise ValueError('Duplicate snapshot artifact: '+relative)
            claimed.add(relative);record=_stream_record(path,relative,rules,start,control_json=path.suffix.lower()=='.json')
            row={'kind':'file',**record}
        elif isinstance(artifact,dict) and set(artifact)=={'kind','manifest'} and artifact.get('kind')=='render_bundle':
            row=_render_bundle(workspace,artifact['manifest'],rules,start)
            bundle_paths={row['manifest']['path'],*(leaf['path'] for leaf in row['leaves'])}
            if claimed&bundle_paths:raise ValueError('Duplicate file across snapshot descriptors')
            claimed.update(bundle_paths)
        elif isinstance(artifact,dict) and set(artifact)=={'kind','manifest'} and artifact.get('kind')=='task_bundle':
            row = _task_bundle(workspace, artifact['manifest'], rules, start)
            bundle_paths = {row['manifest']['path'], *(leaf['path'] for leaf in row['leaves'])}
            if claimed & bundle_paths:raise ValueError('Duplicate file across task bundle descriptors')
            claimed.update(bundle_paths)
        elif isinstance(artifact,dict) and set(artifact)=={'kind','path'} and artifact.get('kind')=='large_artifact':
            path,relative=_relative_file(workspace,artifact['path'])
            if relative in claimed:raise ValueError('Duplicate snapshot artifact: '+relative)
            claimed.add(relative)
            row={'kind':'large_artifact',**_stream_record(path,relative,rules,start,byte_limit=rules.max_large_file_bytes)}
            large_total+=row['size']
            if large_total>rules.max_large_total_bytes:raise ValueError('Large artifacts exceed their byte budget')
        else:raise ValueError('Snapshot artifact must be a relative path or render_bundle descriptor')
        if row['kind']=='task_bundle':
            large_total += row['task_bytes']
            if large_total>rules.max_large_total_bytes:raise ValueError('Task bundles exceed the large byte budget')
            total += row['manifest']['size']
        elif row['kind']!='large_artifact':
            total+=row.get('size',0)+sum(leaf['size'] for leaf in row.get('leaves',[]))+row.get('manifest',{}).get('size',0)
        if total>rules.max_total_bytes:raise ValueError('Snapshot exceeds total byte budget')
        rows.append(row)
    body={'schema':1,'workspace':str(workspace),'policy':asdict(rules),'artifacts':rows,
          'total_bytes':total,'large_total_bytes':large_total}
    body['digest']=digest(body)
    return body


def validate_snapshot(workspace: Path, manifest: dict, *, policy: SnapshotPolicy | dict[str,Any] | None=None) -> dict:
    if not isinstance(manifest,dict) or manifest.get('schema')!=1 or not isinstance(manifest.get('artifacts'),list):raise ValueError('Invalid snapshot manifest')
    workspace=Path(workspace).resolve()
    if manifest.get('workspace')!=str(workspace):raise ValueError('Snapshot workspace mismatch')
    descriptors=[]
    for row in manifest['artifacts']:
        if row.get('kind')=='file':descriptors.append(row.get('path'))
        elif row.get('kind') in {'render_bundle', 'task_bundle'}:descriptors.append({'kind':row['kind'],'manifest':row.get('manifest',{}).get('path')})
        elif row.get('kind')=='large_artifact':descriptors.append({'kind':'large_artifact','path':row.get('path')})
        else:raise ValueError('Unknown snapshot artifact kind')
    current=build_snapshot(workspace,descriptors,policy=policy or manifest.get('policy'))
    if current.get('digest')!=manifest.get('digest'):raise ValueError('Snapshot artifact changed')
    return {'valid':True,'digest':current['digest'],'leaf_count':sum(1+len(r.get('leaves',[])) for r in current['artifacts']),
            'total_bytes':current['total_bytes'],'large_total_bytes':current['large_total_bytes']}


def preflight_snapshot(artifacts: list[dict[str,int]], *, policy: SnapshotPolicy | dict[str,Any] | None=None) -> dict:
    """Check planned counts and byte estimates before expensive artifacts are created."""
    rules=_policy(policy)
    if not isinstance(artifacts,list) or not artifacts or len(artifacts)>rules.max_artifacts:raise ValueError('Planned snapshot descriptor count exceeds policy')
    total=0;large_total=0
    for row in artifacts:
        if not isinstance(row,dict) or type(row.get('files')) is not int or type(row.get('bytes')) is not int or row['files']<1 or row['bytes']<0:
            raise ValueError('Preflight rows need nonnegative bytes and positive file counts')
        file_limit = rules.max_task_bundle_files if row.get('kind') == 'task_bundle' else rules.max_bundle_files
        if row['files']>file_limit:raise ValueError('Planned bundle file count exceeds policy')
        if row.get('kind')=='large_artifact':
            if row['files']!=1 or row['bytes']>rules.max_large_file_bytes:
                raise ValueError('Planned large artifact exceeds per-file budget')
            large_total+=row['bytes']
        else:total+=row['bytes']
    if total>rules.max_total_bytes:raise ValueError('Planned snapshot byte total exceeds policy')
    if large_total>rules.max_large_total_bytes:raise ValueError('Planned large artifacts exceed policy')
    return {'fits':True,'planned_bytes':total,'remaining_bytes':rules.max_total_bytes-total,
            'planned_large_bytes':large_total,'remaining_large_bytes':rules.max_large_total_bytes-large_total}
