"""Selective archive ingestion: no extraction/execution, no git/db/history import."""
from __future__ import annotations
import hashlib
from pathlib import Path, PurePosixPath
import re
import zipfile
from .memory import Memory, namespace


def scan_zip(path:Path) -> dict:
    with zipfile.ZipFile(path) as z:
        infos=z.infolist();md=[i for i in infos if i.filename.endswith('.md')]
        return {'archive':str(path.resolve()),'entries':len(infos),
          'uncompressed_bytes':sum(i.file_size for i in infos),'markdown_files':len(md),
          'router_paths':[i.filename for i in infos if i.filename.endswith('/skill_wiki.py')],
          'extracted':False,'executed':False,'note':'Inventory is a dated snapshot, not proof of currently installed/usable skills'}


def import_zip(path:Path,workspace:Path,*,include_records:bool=False,max_files:int=240,
               max_bytes:int=8*1024*1024,db:Path|None=None) -> dict:
    if not 1<=max_files<=1000 or not 1024<=max_bytes<=32*1024*1024:raise ValueError('Invalid import budget')
    inserted=[];skipped=[];consumed=0;scope=namespace(workspace)
    # Archive digest streams; never loads the multi-GB expansion.
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    archive_digest=h.hexdigest()
    with zipfile.ZipFile(path) as z,Memory(db) as memory:
        for info in sorted(z.infolist(),key=lambda i:i.filename):
            name=info.filename.replace('\\','/');parts=PurePosixPath(name).parts
            if name.startswith('/') or '..' in parts or re.match(r'^[A-Za-z]:',name):
                raise ValueError('Unsafe archive member')
            if any(p.startswith('.') or p in {'__pycache__','node_modules'} for p in parts):continue
            if not name.endswith('.md') or info.file_size>600_000:continue
            knowledge='/20 지식/' in '/'+name or '/40 스킬 위키/' in '/'+name
            record='/10 기록/' in '/'+name
            if not knowledge and not (include_records and record):continue
            if any(x in name for x in ['사용자 프로필','credentials','secrets']):continue
            if len(inserted)>=max_files or consumed+info.file_size>max_bytes:
                skipped.append('budget:'+name);continue
            raw=z.read(info);consumed+=len(raw)
            text=raw.decode('utf-8-sig',errors='strict')
            # Source excerpt is intentionally not an AI-generated "fact" or instruction.
            lines=text.splitlines();excerpt='\n'.join(lines[:45])[:2500]
            if not excerpt.strip():continue
            # Identity is scoped content, while archive/member paths are aliases. Renaming
            # a source therefore cannot bypass forget/quarantine state.
            content_identity=hashlib.sha256(excerpt.encode('utf-8')).hexdigest()
            key='wiki:'+content_identity[:32]
            source_alias='zip:'+str(path.resolve())+'!/'+name
            try:
                result=memory.put(scope=scope,kind='document',key=key,content=excerpt,
                    source=source_alias,
                    locator='lines 1..'+str(min(45,len(lines)))+'; archive-sha256='+archive_digest,
                    basis='source_read',ttl_days=60,content_identity=content_identity,
                    source_alias=source_alias)
                inserted.append({'member':name,**result})
            except ValueError as exc:skipped.append(str(exc)+':'+name)
    return {'read_bytes':consumed,'selected':len(inserted),'records':inserted,
      'skipped':skipped,'private_history_included':include_records,
      'warning':'Only bounded excerpts indexed; inspect exact source for complete evidence. No source code executed.'}
