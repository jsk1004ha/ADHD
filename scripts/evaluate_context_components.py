"""Reproducible component observations, explicitly not real agent pilot/holdout trials."""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
from statistics import median
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'third_party'))
from adhd.context_search import locate
from adhd.context_session import ContextSession
from adhd.memory import Memory,namespace
from adhd.resource_metrics import ProcessTreeMonitor
from adhd.trial_registry import TrialRegistry,compare_trials
from adhd.core import atomic_json,file_hash


def run(out: Path, *, cases_per_type=6):
    out.mkdir(parents=True,exist_ok=False)
    if (out/'results.json').exists():raise ValueError('Use a fresh output directory; do not overwrite trials')
    registry=TrialRegistry(out/'registry')
    expected=[]
    source_files={p.relative_to(ROOT).as_posix():file_hash(p)
                  for p in sorted((ROOT/'adhd').glob('*.py'))}
    source_files['scripts/evaluate_context_components.py']=file_hash(Path(__file__))
    source_snapshot=hashlib.sha256(json.dumps(source_files,sort_keys=True).encode()).hexdigest()
    tool_versions={}
    for name,flag in (('git','--version'),('rg','--version')):
        try:
            observed=subprocess.run([name,flag],capture_output=True,timeout=10,check=False)
            tool_versions[name]=observed.stdout[:512].decode('utf-8',errors='replace') if observed.returncode==0 else None
        except (OSError,subprocess.TimeoutExpired):tool_versions[name]=None
    with tempfile.TemporaryDirectory(prefix='adhd-components-') as temporary:
        root=Path(temporary)
        previous=os.environ.get('CODEX_HOME')
        previous_store=os.environ.pop('ADHD_HOME',None)
        os.environ['CODEX_HOME']=str(root/'isolated-codex')
        try:
            ws=root/'source'
            ws.mkdir()
            sources=[]
            for n in range(cases_per_type):
                p=ws/f'component_{n}.py'
                p.write_bytes((f'def target_{n}():\n    return "필수 정답 {n}"\n'+'# unrelated context\n'*3000).encode('utf-8'))
                sources.append(p)
            with Memory(root/'memory.sqlite3') as memory:
                for n in range(240):
                    memory.put(scope=namespace(ws),kind='fact',key=f'기억{n}',content=f'한국어 탐색 근거 항목{n}',source='component_fixture',locator='generated corpus')
                for typ in ('code','korean_memory','long_log','context_delivery'):
                    for number in range(cases_per_type):
                        case=f'{typ}-{number}'
                        expected.append(case)
                        query=f'target_{number}' if typ=='code' else f'항목{number}'
                        log=root/f'log-{number}.txt'
                        log.write_bytes((('noise\n'*10000)+f'FINAL required-{number}\n').encode())
                        input_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sources)+query.encode()+log.read_bytes()).hexdigest()
                        conditions=dict(model='not_invoked_component_only',effort='not_invoked',input_snapshot=input_hash,
                            source_snapshot=source_snapshot,policy='component-v1',
                            environment=sys.version+'-'+os.name+'-sqlite-'+sqlite3.sqlite_version,
                            tools=json.dumps(tool_versions,sort_keys=True),
                            permissions='temporary_local_fixture',delivery='utf8_json',
                            cache=('code_index_cold_parser_cold_os_unknown' if typ=='code' and number==0 else
                                   'code_index_warm_parser_warm_os_unknown' if typ=='code' else
                                   'generated_fixture_os_unknown'))
                        variants=('baseline','candidate') if number%2==0 else ('candidate','baseline')
                        for variant in variants:
                            trial=registry.start(case,typ,variant,conditions)
                            began=time.perf_counter()
                            try:
                                with ProcessTreeMonitor() as monitor:
                                    if typ=='code':
                                        if variant=='baseline':
                                            result={'text':b''.join(p.read_bytes() for p in sources).decode()}
                                            assert query in result['text']
                                        else:
                                            result=locate(ws,query,sources=('code',),complete=True)
                                            assert any(c['ref']['path']==sources[number].name for c in result['cards'])
                                    elif typ=='korean_memory':
                                        result=memory.recall(query,namespace(ws),complete=variant=='baseline')
                                        assert any(query in r['content'] for r in result['records'])
                                    elif typ=='long_log':
                                        if variant=='baseline':result={'text':log.read_text()}
                                        else:
                                            tail=deque(maxlen=8)
                                            digest=hashlib.sha256()
                                            with log.open('rb') as stream:
                                                for line in stream:
                                                    digest.update(line)
                                                    tail.append(line)
                                            assert digest.hexdigest()==file_hash(log)
                                            result={'text':b''.join(tail).decode(),'full_ref':log.name,'sha256':digest.hexdigest(),'excerpt':True}
                                        assert f'required-{number}' in result['text']
                                    else:
                                        text=('selected context\n'*150)+str(number)
                                        ref={'source':'component_fixture','revision':input_hash,'policy_version':'component-v1'}
                                        if variant=='baseline':result={'excerpts':[text,text]}
                                        else:
                                            with ContextSession(ws,case,root=root/'sessions') as ledger:
                                                one=ledger.deliver(ref,text)
                                                two=ledger.deliver(ref,text)
                                                assert one['excerpt']==text and two['duplicate'] and not two['excerpt']
                                                assert ledger.deliver(ref,text,epoch='after-compaction')['excerpt']==text
                                                result={'first':one,'repeat':two}
                                encoded=json.dumps(result,ensure_ascii=False).encode('utf-8')
                                registry.finish(trial,'success',metrics={'verified_ms':(time.perf_counter()-began)*1000,
                                    'context_bytes':len(encoded),'peak_private_bytes':monitor.report()['peak_private_bytes']},
                                    resources=monitor.report(),quality={'requirements':True,'preservation':True,
                                        'security':True,'independent':False})
                            except Exception as error:
                                registry.finish(trial,'failed',reason=type(error).__name__)
            raw_pairs={}
            for row in registry.trials():raw_pairs.setdefault(row['case_id'],{})[row['variant']]=row
            descriptive={}
            for typ in ('code','korean_memory','long_log','context_delivery'):
                pairs=[p for key,p in raw_pairs.items() if key.startswith(typ+'-') and set(p)=={'baseline','candidate'} and all(r['status']=='success' for r in p.values())]
                descriptive[typ]={'paired_cases':len(pairs),'metrics':{metric:{
                    variant:median(p[variant]['metrics'][metric] for p in pairs if p[variant]['metrics'].get(metric) is not None)
                    if any(p[variant]['metrics'].get(metric) is not None for p in pairs) else None
                    for variant in ('baseline','candidate')}
                    for metric in ('verified_ms','context_bytes','peak_private_bytes')}}
            source_stable=all(file_hash(ROOT/name)==sha for name,sha in source_files.items())
            comparison=compare_trials(registry.trials(),expected_cases=expected)
            if not source_stable:
                comparison={'comparable':False,'reason':'source_changed_during_evaluation',
                            'default_changed':False,'comparison_before_source_check':comparison}
            report={'schema_version':1,'kind':'component_fixture_observations','baseline_definition':
                'Explicit broad fixture source/log reads, exhaustive memory recall and repeated excerpt delivery; not a previous production build',
                'source_files':source_files,'source_snapshot':source_snapshot,'tool_versions':tool_versions,
                'source_stable_after_evaluation':source_stable,
                'all_starts':registry.summary(),'comparison':comparison,
                'descriptive_observations_not_acceptance':descriptive,
                'real_agent_pilot_executed':False,'real_agent_holdout_executed':False,'tokens_observed':False,
                'default_changed':False,'limitations':['No model task or independent acceptance was run.','Sampling may miss short-lived children.','OS caches were not reset or measured; the first code candidate builds its index and later candidates reuse it.','The module source manifest and tool versions are recorded; the complete host/tool policy environment is not attested.','Fixture byte reductions do not establish production savings.']}
            atomic_json(out/'results.json',report)
            return report
        finally:
            if previous is None:os.environ.pop('CODEX_HOME',None)
            else:os.environ['CODEX_HOME']=previous
            if previous_store is not None:os.environ['ADHD_HOME']=previous_store


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(run(args.out),ensure_ascii=False,indent=2))
