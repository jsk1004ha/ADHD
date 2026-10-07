"""Preview/apply/recover explicit vault edits without changing existing IDs."""
from __future__ import annotations

from datetime import date
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from urllib.parse import urlencode

from filelock import FileLock
from .. import obsidian
from ..core import atomic_json, file_hash

ASSETS = Path(__file__).parent


def read_note(workspace: Path, note_id: str, section: str|None=None,
              revision: str|None=None, max_chars: int=6500) -> dict:
    """Reserve packet framing before expanding a large section of source text."""
    obsidian._budget(1,max_chars)
    if not isinstance(note_id,str) or not note_id or len(note_id)>1000:raise ValueError('note-id-required')
    if section is not None and (not isinstance(section,str) or not section.strip() or len(section)>500):
        raise ValueError('invalid-section')
    def build(packet,policy,engine,db):
        notes,mapping,warnings=obsidian._notes(db,policy,engine);packet['warnings']+=warnings
        denied=db.execute("SELECT 1 FROM tombstones WHERE kind='id' AND value=?",
            (obsidian.index.canonical_id(policy['vault_id'],note_id),)).fetchone()
        if denied:packet['warnings'].append('note-not-found');return
        direct=[row['note_id'] for note in notes if str(note.metadata['id'])==note_id
                for row in [mapping[note.relative_path]]]
        identity,error=(direct[0],None) if len(direct)==1 else obsidian.index.resolve_id(db,note_id,list(mapping.values()))
        if error:packet['warnings'].append(error);return
        note=next(n for n in notes if mapping[n.relative_path]['note_id']==identity)
        row=mapping[note.relative_path]
        if revision is not None and revision!=row['digest']:packet['warnings'].append('stale-revision');return
        text,resolved=obsidian._section_text(note.body,section)
        if text is None:packet['warnings'].append('section-not-found');return
        if not obsidian._add_card(packet,row,note,'',resolved,policy,engine):return
        room=max(0,max(2048,max_chars)-len(json.dumps(packet,ensure_ascii=False))-128)
        redacted=engine.redact_private_text(text,redact_long_tokens=False)
        packet['cards'][-1]['excerpt']=redacted[:room]
        packet['budget']['used']=len(redacted[:room])
        if len(redacted)>room:packet['warnings'].append('excerpt-truncated')
    return obsidian._run(workspace,max_chars,build)


def forget_note(workspace: Path, note_id: str) -> dict:
    """Deny an identity and its exclusive aliases, without denying its citations."""
    policy=_policy(workspace);engine=obsidian._engine(policy)
    db,generation,_=obsidian.index.refresh(workspace.resolve(),policy,engine)
    try:
        notes,mapping,_=obsidian._notes(db,policy,engine)
        direct=[mapping[n.relative_path]['note_id'] for n in notes if str(n.metadata['id'])==note_id]
        identity,error=(direct[0],None) if len(direct)==1 else obsidian.index.resolve_id(db,note_id,list(mapping.values()))
        if error:return {'status':'ambiguous' if error=='ambiguous-alias' else 'not_found','id':note_id}
        with db:
            db.execute("INSERT OR IGNORE INTO tombstones VALUES ('id',?)",(identity,))
            for row in db.execute('SELECT alias FROM aliases WHERE note_id=?',(identity,)):
                count=db.execute('SELECT COUNT(DISTINCT note_id) FROM aliases WHERE alias=?',(row[0],)).fetchone()[0]
                if count==1:db.execute("INSERT OR IGNORE INTO tombstones VALUES ('alias',?)",(row[0],))
            for row in db.execute('SELECT digest FROM revisions WHERE note_id=?',(identity,)):
                db.execute("INSERT OR IGNORE INTO tombstones VALUES ('revision',?)",(row[0],))
            generation+=1
            db.execute("INSERT OR REPLACE INTO meta VALUES ('generation',?)",(str(generation),))
        return {'status':'forgotten','id':identity,'index_generation':generation,'vault_file_deleted':False}
    finally:db.close()


def storage_inventory(workspace: Path) -> dict:
    from .. import experience_storage
    config,engine,db,generation,warnings=obsidian._snapshot(workspace.resolve())
    categories={key:{'files':0,'bytes':0} for key in ['canonical_notes','logs','indexes','backups','other_evidence']}
    hashes={};sizes={}
    def add(path,category,label):
        categories[category]['files']+=1;categories[category]['bytes']+=path.stat().st_size
        hashes.setdefault(file_hash(path),[]).append(label)
        sizes[label]=path.stat().st_size
    try:
        if db is not None:
            rows,current_warnings=obsidian.index.live_rows(db,config);warnings+=current_warnings
            for row in rows:
                path=obsidian.safe_path(config['vault'],row['path'])
                if file_hash(path)==row['digest']:add(path,'canonical_notes','vault:'+row['path'])
                else:warnings.append('obsidian-index-stale:'+row['path'])
    finally:
        if db is not None:db.close()
    root=experience_storage._path(workspace,'.adhd')
    if root.is_dir():
        for path in experience_storage._walk(root):
            relative=path.relative_to(root).as_posix()
            category=('logs' if path.suffix in {'.log','.jsonl'} else 'indexes' if path.suffix in {'.db','.sqlite','.sqlite3'}
                else 'backups' if any(part in {'backups','originals','evidence-archives','provision'} for part in path.parts)
                else 'other_evidence')
            add(path,category,'workspace:.adhd/'+relative)
    return {'schema_version':1,'categories':categories,
        'duplicates':[{'sha256':key,'paths':paths,'files':len(paths),'logical_duplicate_bytes':sum(sizes[p] for p in paths[1:])}
            for key,paths in sorted(hashes.items()) if len(paths)>1],
        'warnings':sorted(set(warnings)),'index_generation':generation,'deletion_performed':False,
        'logical_bytes':sum(row['bytes'] for row in categories.values()),'reclaimed_disk_bytes':None}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _flat(fields: dict, body: str) -> bytes:
    lines=['---']
    for key,value in fields.items():
        if isinstance(value,list):
            lines += [key+':']+["  - "+json.dumps(item,ensure_ascii=False) for item in value] if value else [key+': []']
        else:lines.append(key+': '+json.dumps(value,ensure_ascii=False))
    return ('\n'.join(lines+['---',body,''])).encode('utf-8')


def _policy(workspace: Path) -> dict:
    policy=obsidian.load_config(workspace,required=True)
    if not policy['enabled']:raise ValueError('Obsidian integration is disabled')
    return policy


def _grant(policy: dict, path: Path) -> None:
    if not policy['write_enabled']:raise ValueError('Vault writes are not enabled')
    roots=[obsidian.safe_path(policy['vault'],p,allow_missing=True) for p in policy['write_roots']]
    if not any(path.is_relative_to(root) for root in roots):raise ValueError('Vault target is outside granted roots')


def provision(workspace: Path, payload: dict, *, apply: bool=False) -> dict:
    """Preview stable project notes, templates and Bases; apply only current diffs."""
    workspace=workspace.resolve();policy=_policy(workspace);vault=Path(policy['vault'])
    if not isinstance(payload,dict) or set(payload)-{'project_root','expected_revisions','source_ref'}:
        raise ValueError('Unknown provisioning fields')
    root=Path(payload.get('project_root',workspace)).resolve()
    if not root.is_dir() or root!=workspace:raise ValueError('Project root must be this existing workspace')
    today=date.today().isoformat();source=payload.get('source_ref','configuration:'+policy['vault_id'])
    if not isinstance(source,str) or not source or len(source)>160:raise ValueError('Invalid source reference')
    edits={}
    for directory,destination in [('templates','99 시스템/템플릿/ADHD'),('views','99 시스템/베이스')]:
        for asset in sorted((ASSETS/directory).glob('*')):
            if asset.is_file():edits[destination+'/'+asset.name]=asset.read_bytes()
    personal='10 기록/프로젝트/개인 위키.md'
    target=obsidian.safe_path(vault,personal,allow_missing=True)
    if target.exists():
        engine=obsidian._engine(policy)
        metadata,_=engine.parse_frontmatter(target.read_text(encoding='utf-8-sig'))
        if metadata.get('id')!='project:personal-wiki':raise ValueError('Personal wiki canonical ID differs')
        text=target.read_text(encoding='utf-8-sig')
        marker='<!-- ADHD:OBSIDIAN:INTEGRATION -->'
        if marker not in text:
            text += ('\n\n## ADHD 연결\n'+marker+'\n'
                     '[[ADHD]]와 [[ADHD 위키 운영]]에서 실행·기록·검증 계약을 확인한다.\n'
                     '사용자 평가는 기술 검사와 분리하며, 전체 대화 대신 선별 후보와 정본 참조를 저장한다.\n'
                     '기존 ID·링크·과거 검증 시점은 유지한다. 현재 작업의 검증은 새 receipt를 따른다.\n')
        edits[personal]=text.encode('utf-8')
    project='10 기록/프로젝트/ADHD.md'
    fields={'type':'project','domain':'project','layer':'record','privacy':'private','status':'active',
            'template':False,'id':'project:adhd','created':today,'updated':today,'source_ids':[source],
            'topics':['adhd','context-retrieval','verification'],'projects':['adhd'],
            'source_root':'adhd','source_kind':'repo','source_uri':root.as_uri(),
            'project_state':'local-obsidian-integration','verified_at':'','verification_scope':'미검증 항목은 실행 receipt에서 확인',
            'evidence_strength':'unverified','tags':['record','project'],'aliases':['ADHD harness','ADHD 하네스']}
    body=('# ADHD\n\n## 상황과 적용 범위\nCodex의 실행·검증·복구를 담당하는 로컬 하네스. 위키는 출처 자료다.\n\n'
          '## 관찰과 결과\n`wiki context/project/read`로 필요한 근거를 조회한다. 설정과 모델 권한은 별도다.\n\n'
          '## 사용자 평가\n미수집. 특정 작업의 명시적 평가를 이후 후보에 기록한다.\n\n'
          '## 검증과 한계\n현재 실행의 test receipt와 독립 검증 결과를 참조한다. 토큰 절감 및 Obsidian UI 효과는 미측정이다.\n\n'
          '## 다음 행동\n검색→원문 확인→작업→검증→선별 후보 검토. 검증 전 후보는 실행 가능한 절차가 아니다.\n\n'
          '## 근거\n[[개인 위키]] · [[ADHD 위키 운영]]\n')
    project_path=obsidian.safe_path(vault,project,allow_missing=True)
    if project_path.exists():
        metadata,_=obsidian._engine(policy).parse_frontmatter(project_path.read_text(encoding='utf-8-sig'))
        if metadata.get('id')!='project:adhd':raise ValueError('ADHD project canonical ID differs')
        edits[project]=project_path.read_bytes()
    else:edits[project]=_flat(fields,body)
    knowledge='20 지식/ADHD 위키 운영.md'
    knowledge_path=obsidian.safe_path(vault,knowledge,allow_missing=True)
    if knowledge_path.exists():
        metadata,_=obsidian._engine(policy).parse_frontmatter(knowledge_path.read_text(encoding='utf-8-sig'))
        if metadata.get('id')!='knowledge:adhd-wiki-operation':raise ValueError('Knowledge canonical ID differs')
        edits[knowledge]=knowledge_path.read_bytes()
    else:
        refs=[obsidian._engine(policy).stable_note_source_id(project_path)]
        if target.exists():refs.append(obsidian._engine(policy).stable_note_source_id(target))
        k={'type':'knowledge','domain':'knowledge','layer':'knowledge','privacy':'private','status':'distilled',
           'template':False,'id':'knowledge:adhd-wiki-operation','created':today,'updated':today,'source_ids':refs,
           'seed_records':['[[ADHD]]']+(['[[개인 위키]]'] if target.exists() else []),
           'topics':['context-retrieval','recording','token-efficiency'],'projects':['adhd','personal-wiki'],
           'aliases':['토큰 절약','위키 기록 정책'],'maturity':'seed','confidence':'provisional','tags':['knowledge']}
        kb=('# ADHD 위키 운영\n\n## 상황과 적용 범위\n작업당 검색 한 번, 필요한 ID·section·revision만 확장한다.\n\n'
            '## 관찰과 결과\n모든 도구 출력을 저장하지 않는다. 결정·명시적 평가·재사용 가능한 검증 성공·반복 실패를 후보로 선별한다.\n'
            '같은 이벤트 재전송은 중복 저장하지 않으며, 별개 작업에서 재발한 실패는 근거를 유지한다.\n\n'
            '## 사용자 평가\n긍정·부정·혼합·미수집과 평가 축을 보존한다. 기술 검사 통과와 만족도는 별도다.\n\n'
            '## 검증과 한계\n문자·바이트·지연은 측정할 수 있다. 전체 작업 토큰과 사람의 수정 시간은 미측정으로 남긴다.\n'
            '성공 자료는 Git commit/파일/hash 또는 문서/hash/검사 receipt를 참조한다. 복사본을 정본으로 만들지 않는다.\n\n'
            '## 다음 행동\n재현·회귀·비용·복구 검사와 독립 완료 근거가 갖춰진 절차만 controller가 채택한다.\n\n'
            '## 근거\n[[ADHD]] · [[개인 위키]]\n')
        edits[knowledge]=_flat(k,kb)
    expected=payload.get('expected_revisions',{})
    if not isinstance(expected,dict):raise ValueError('expected_revisions must be an object')
    rows=[]
    for relative,data in edits.items():
        path=obsidian.safe_path(vault,relative,allow_missing=True)
        old=path.read_bytes() if path.exists() else None
        revision=_sha(old) if old is not None else None
        if relative in expected and expected[relative]!=revision:raise ValueError('Provision revision conflict: '+relative)
        if old==data:continue
        rows.append({'path':relative,'expected_revision':revision,'sha256':_sha(data),'data':data,
                     'diff':''.join(difflib.unified_diff((old or b'').decode('utf-8-sig').splitlines(True),
                         data.decode('utf-8').splitlines(True),fromfile='expected',tofile='candidate'))})
    if not apply:return {'status':'preview','vault_id':policy['vault_id'],
                         'changes':[{k:v for k,v in r.items() if k!='data'} for r in rows]}
    for row in rows:_grant(policy,obsidian.safe_path(vault,row['path'],allow_missing=True))
    state=obsidian.safe_path(vault,'.adhd',allow_missing=True);state.mkdir(exist_ok=True)
    with FileLock(str(state/'provision.lock'),timeout=10):
        latest=_policy(workspace)
        if latest['policy_epoch']!=policy['policy_epoch']:raise ValueError('Provision policy changed')
        for row in rows:
            path=obsidian.safe_path(vault,row['path'],allow_missing=True)
            if (file_hash(path) if path.exists() else None)!=row['expected_revision']:raise ValueError('Provision revision conflict')
        manifest_id=uuid.uuid4().hex;backup=obsidian.safe_path(vault,'.adhd/provision/'+manifest_id,allow_missing=True)
        backup.mkdir(parents=True);manifest={'schema_version':1,'vault_id':policy['vault_id'],'policy_epoch':policy['policy_epoch'],'changes':[]}
        for row in rows:
            path=obsidian.safe_path(vault,row['path'],allow_missing=True);path.parent.mkdir(parents=True,exist_ok=True)
            saved=None
            if path.exists():
                saved=backup/'originals'/row['path'];saved.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,saved)
            entry={k:v for k,v in row.items() if k not in {'data','diff'}}
            entry['backup']=saved.relative_to(vault).as_posix() if saved else None
            manifest['changes'].append(entry)
        atomic_json(backup/'manifest.json',manifest)
        for row in rows:
            path=obsidian.safe_path(vault,row['path'],allow_missing=True)
            temporary=obsidian.safe_path(vault,path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp'),allow_missing=True)
            try:temporary.write_bytes(row['data']);os.replace(temporary,path)
            finally:temporary.unlink(missing_ok=True)
        return {'status':'applied','changes':len(rows),'manifest':(backup/'manifest.json').relative_to(vault).as_posix()}


def restore_templates(workspace: Path, payload: dict) -> dict:
    policy=_policy(workspace);vault=Path(policy['vault'])
    if not isinstance(payload,dict) or set(payload)!={'manifest'} or not re.fullmatch(
        r'\.adhd/provision/[0-9a-f]{32}/manifest\.json',payload['manifest']):raise ValueError('Invalid provision manifest')
    path=obsidian.safe_path(vault,payload['manifest']);manifest=json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema_version')!=1 or manifest.get('vault_id')!=policy['vault_id']:raise ValueError('Provision vault differs')
    with FileLock(str(vault/'.adhd/provision.lock'),timeout=10):
        planned=[]
        for row in manifest['changes']:
            target=obsidian.safe_path(vault,row['path'],allow_missing=True);_grant(policy,target)
            actual=file_hash(target) if target.exists() else None
            if actual==row['expected_revision']:continue
            if actual!=row['sha256']:raise ValueError('User edit prevents provision rollback')
            saved=obsidian.safe_path(vault,row['backup']) if row['backup'] else None
            if saved and file_hash(saved)!=row['expected_revision']:raise ValueError('Provision backup changed')
            planned.append((row,target,saved))
        for row,target,saved in planned:
            retained=path.parent/'restored-current'/row['path'];retained.parent.mkdir(parents=True,exist_ok=True)
            if retained.exists():raise ValueError('Provision was already restored')
            shutil.copyfile(target,retained)
            if saved:shutil.copyfile(saved,target)
            else:target.unlink()
        return {'status':'restored','restored':len(planned),'originals_preserved':True,'manifest':payload['manifest']}


def open_link(workspace: Path, note_id: str, section: str|None=None) -> dict:
    policy=_policy(workspace);packet=read_note(workspace,note_id,section=section,max_chars=4000)
    if not packet['cards']:return {'status':'unavailable','warnings':packet['warnings']}
    card=packet['cards'][0];relative=card['locator']['relative_path']
    target=relative.removesuffix('.md')+('#'+section if section else '')
    return {'status':'link','id':card['id'],'uri':'obsidian://open?'+urlencode({'vault':Path(policy['vault']).name,'file':target}),
            'authority':'source_data','launch_performed':False}
