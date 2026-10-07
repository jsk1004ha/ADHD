"""Paired diagnostic retrieval benchmark, with explicit unmeasured model cost."""
from __future__ import annotations
import argparse
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from adhd import obsidian
from adhd.obsidian_assets.bridge import _flat, read_note


def fixture_vault(base: Path, engine: Path, fixture: dict) -> tuple[Path,Path]:
    workspace=base/'workspace';vault=base/'vault';workspace.mkdir();vault.mkdir()
    tool=vault/'99 시스템/도구/wiki_context.py';tool.parent.mkdir(parents=True);shutil.copyfile(engine,tool)
    for entry in fixture['notes']:
        path=vault/entry['path'];path.parent.mkdir(parents=True,exist_ok=True)
        metadata={**entry['metadata'],'id':entry['id']};malformed=metadata.pop('malformed',False)
        if metadata['type']=='project':metadata['source_uri']=workspace.as_uri()
        data=_flat(metadata,entry['body'])
        if malformed:data=data.replace(b'\n---\n',b'\n',1)
        path.write_bytes(data)
    obsidian.configure(workspace,{'vault':str(vault),'engine':'99 시스템/도구/wiki_context.py','enabled':True,
                                 'read_roots':['10 기록'],'allowed_privacy':['public']})
    return workspace,vault


def _ids_legacy(value: object, fixture: dict) -> set[str]:
    identities={e['id'] for e in fixture['notes']}
    names={Path(e['path']).name:e['id'] for e in fixture['notes']}
    found=set()
    def walk(node):
        if isinstance(node,dict):
            for key,item in node.items():
                if key in {'id','note_id'} and isinstance(item,str) and item in identities:found.add(item)
                if key in {'path','relative_path'} and isinstance(item,str) and Path(item).name in names:
                    found.add(names[Path(item).name])
                if key in {'metadata','sources','read_order','records','knowledge','project','hits','items'} or isinstance(item,(dict,list)):
                    walk(item)
        elif isinstance(node,list):
            for item in node:walk(item)
    walk(value);return found


def _legacy(engine, vault, case):
    query=case['query'] or case['args'].get('note_id','')
    function=engine.project_payload if case['operation']=='project' else engine.context_payload
    parameters=inspect.signature(function).parameters
    kwargs={'limit':6} if 'limit' in parameters else {}
    return function(engine.collect_notes(vault).notes,query,**kwargs) or {'warnings':['no-results']}


def _adapter(workspace,case):
    args=dict(case['args'])
    if case['operation']=='read':return read_note(workspace,args.pop('note_id'),**args)
    if case['operation']=='project':return obsidian.project(workspace,case['query'],**args)
    return obsidian.context(workspace,case['query'],**args)


def _score(ids,expected):
    required=set(expected['required_ids']);forbidden=set(expected['forbidden_ids'])
    coverage=len(ids&required)/len(required) if required else None
    passed=required<=ids and not ids&forbidden and (not expected['abstain'] or not ids)
    return {'passed':passed,'source_coverage':coverage,'forbidden_hits':sorted(ids&forbidden),
            'abstention_correct':not ids if expected['abstain'] else None}


def benchmark(engine_path: Path, fixture_path: Path, *, split='dev', repetitions=3) -> dict:
    if repetitions<3:raise ValueError('At least three interleaved repetitions are required')
    fixture=json.loads(fixture_path.read_text(encoding='utf-8'))
    cases=[c for c in fixture['cases'] if split=='all' or c['split']==split]
    rows=[];priming=[]
    with tempfile.TemporaryDirectory() as directory:
        workspace,vault=fixture_vault(Path(directory),engine_path,fixture)
        engine=obsidian._engine(obsidian.load_config(workspace))
        for number,case in enumerate(cases):
            for repetition in range(repetitions):
                order=['A','B','C'];offset=(number+repetition)%3;order=order[offset:]+order[:offset]
                for variant in order:
                    if variant=='B':
                        index=obsidian.safe_path(workspace,'.adhd/obsidian-index.sqlite3',allow_missing=True)
                        if index.exists():index.unlink()  # This is only the isolated rebuildable fixture cache.
                    if variant=='C':
                        start=time.perf_counter();_adapter(workspace,case)
                        priming.append((time.perf_counter()-start)*1000)
                    start=time.perf_counter()
                    value=_legacy(engine,vault,case) if variant=='A' else _adapter(workspace,case)
                    elapsed=(time.perf_counter()-start)*1000
                    ids=_ids_legacy(value,fixture) if variant=='A' else {c['id'].split(':',2)[2] for c in value['cards']}
                    rows.append({'case_id':case['id'],'category':case['category'],'split':case['split'],
                                 'variant':variant,'repetition':repetition,'ms':elapsed,
                                 'bytes':len(json.dumps(value,ensure_ascii=False).encode()),'ids':sorted(ids),
                                 'warnings':value.get('warnings',[]),**_score(ids,case['expected'])})
    summary={}
    for variant in ['A','B','C']:
        data=[r for r in rows if r['variant']==variant];times=sorted(r['ms'] for r in data)
        coverage=[r['source_coverage'] for r in data if r['source_coverage'] is not None]
        summary[variant]={'checks':len(data),'passed':sum(r['passed'] for r in data),
                          'p50_ms':statistics.median(times),'p95_ms':times[max(0,int(.95*len(times))-1)],
                          'median_bytes':statistics.median(r['bytes'] for r in data),
                          'mean_source_coverage':statistics.mean(coverage) if coverage else None}
    return {'schema_version':1,'scope':fixture['scope'],'split':split,'queries':len(cases),'repetitions':repetitions,
            'source_engine_sha256':hashlib.sha256(engine_path.read_bytes()).hexdigest(),
            'fixture_sha256':hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            'variants':{'A':'existing wiki context/project engine','B':'adapter with cold index','C':'adapter warm index'},
            'cache_scope':'process/engine and OS cache uncontrolled; B resets only fixture SQLite index',
            'summary':summary,'rows':rows,'priming':{'calls':len(priming),'total_ms':sum(priming)},
            'total_model_tokens':None,'token_savings':None,'human_correction_time':None,
            'holdout_status':'diagnostic, evaluated after implementation; invalidate on retrieval tuning' if split!='dev' else 'not evaluated',
            'adoption':{'embeddings':'not adopted: no measured end-to-end gain or user failure evidence',
                        'links':'optional explicit one-hop source links, disabled by default',
                        'obsidian_uri':'optional policy-resolved link; no automatic launch or UI benefit claim'}}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--engine',type=Path,required=True)
    parser.add_argument('--fixture',type=Path,default=ROOT/'tests/fixtures/obsidian/evaluation.json')
    parser.add_argument('--split',choices=['dev','holdout','all'],default='dev')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();result=benchmark(args.engine,args.fixture,split=args.split)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'queries':result['queries'],'summary':result['summary'],'total_model_tokens':None},ensure_ascii=False))
    return 0 if all(r['passed'] for r in result['rows'] if r['variant'] in {'B','C'}) else 1

if __name__=='__main__':raise SystemExit(main())
