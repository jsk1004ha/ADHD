"""Prefer the user's installed route-v7; small deterministic fallback otherwise."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
from .core import home, store, read_json, atomic_json, file_hash, discover_skills, preferred_skill_entries
from .memory import tokens

WIKI_ROUTE_TIMEOUT_SECONDS = 90


def configure_router(script:Path) -> dict:
    script=script.expanduser().resolve()
    if script.name!='skill_wiki.py' or not script.is_file():raise ValueError('Select the existing skill_wiki.py, not a ZIP or install script')
    # Pin local Python sources, not whole catalog contents. This is explicit local trust.
    sources={str(p):file_hash(p) for p in script.parent.glob('*.py') if p.is_file() and not p.is_symlink()}
    record={'script':str(script),'source_hashes':sources,'python':sys.executable,
            'note':'Runs existing local routing code only on an explicit route CLI call, never in a host hook'}
    atomic_json(store()/'wiki-router.json',record);return record


def route_skills(query:str,limit:int=4,*,workspace:Path|None=None) -> dict:
    if not query.strip() or len(query)>20000:raise ValueError('Provide a bounded task description')
    workspace=(workspace or Path.cwd()).expanduser().resolve()
    warnings=[];config=read_json(store()/'wiki-router.json',{})
    if config:
        for name,h in config['source_hashes'].items():
            if not Path(name).is_file() or file_hash(Path(name))!=h:
                raise ValueError('Installed wiki router changed; inspect and run route-config again: '+name)
        # route-v7 owns freshness, set-cover, Korean action semantics, owner aliases.
        argv=[config['python'],config['script'],'route',query,'--codex-home',str(home()),
              '--refresh-stale','--observe','--caller','codex','--compact']
        try:
            result=subprocess.run(argv,capture_output=True,text=True,encoding='utf-8',
                                  timeout=WIKI_ROUTE_TIMEOUT_SECONDS,cwd=workspace)
            if result.stdout:
                payload=json.loads(result.stdout)
                if not isinstance(payload,dict):raise ValueError('Router returned a non-object')
                if len(result.stdout)>35000:raise ValueError('Unexpectedly large router output; do not dump full catalogs')
                stale=payload.get('status')=='stale' or any('stale' in str(x).lower() for x in payload.get('warnings',[]))
                if result.returncode==0 or stale:
                    return {'engine':'existing-wiki-route-v7','result':payload,
                            'status':'stale' if stale else payload.get('status','selected'),
                            'warnings':(['Wiki index remains stale after its bounded refresh; inspect router diagnostics before relying on coverage'] if stale else []),
                            'caution':'Read actual selected SKILL.md files. Selection is not proof a MCP/tool is authenticated.'}
            warnings.append('Existing router failed; exit='+str(result.returncode)+' '+result.stderr[-600:])
        except (subprocess.SubprocessError,ValueError,OSError) as exc:
            warnings.append('Existing router unavailable: '+str(exc)[:800])
    q=tokens(query);explicit={w[1:] for w in query.split() if w.startswith('$')}
    ranked=[];seen=set()
    for entry in preferred_skill_entries(discover_skills(workspace)):
        identity=entry['name'].lower()
        path_identity=entry['path'].lower()
        if path_identity in seen:continue
        score=6*len(q&tokens(entry['name']))+len(q&tokens(entry.get('description','')))
        if identity in explicit:score+=1000
        if score:
            seen.add(path_identity)
            ranked.append({**entry,'description':entry.get('description','')[:350],'score':score})
    ranked=sorted(ranked,key=lambda x:(-x['score'],x['name']))[:max(1,min(4,limit))]
    return {'engine':'local-metadata-fallback','primary':ranked[:1],'supporting':ranked[1:],
            'warnings':warnings+['No live tool/authentication verification implied by skill discovery'],
            'body_loading':'Read only selected skills; full catalog is never returned',
            'status':'selected' if ranked else 'no_match'}
