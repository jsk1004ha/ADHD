"""Compare actual document inspection and retained scratch on bounded fixtures."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import types
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def peak_rss():
    if os.name=='nt':
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD),
                      *[(name,ctypes.c_size_t) for name in ('peak','working','peak_paged','paged',
                         'peak_nonpaged','nonpaged','pagefile','peak_pagefile')]]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        api=ctypes.WinDLL('psapi',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        api.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD]
        row=Counters();row.cb=ctypes.sizeof(row)
        if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(row),row.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return row.peak
    import resource
    value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform=='darwin' else value*1024


def worker(source,baseline):
    if baseline:
        module=types.ModuleType('adhd._baseline_documents');module.__package__='adhd'
        exec(compile(Path(baseline).read_text(encoding='utf-8'),str(baseline),'exec'),module.__dict__)
    else:
        from adhd import documents as module
    began=time.perf_counter()
    result=module.inspect_document(Path(source))
    elapsed=time.perf_counter()-began
    proof=hashlib.sha256(json.dumps({k:v for k,v in result.items() if k!='path'},
                        sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    print(json.dumps({'seconds':elapsed,'peak_rss_bytes':peak_rss(),'result_digest':proof}))


def fixture(path,size):
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('mimetype',b'application/hwp+zip')
        archive.writestr('Contents/section0.xml',b'<section><p><t>verified text</t></p></section>')
        with archive.open('BinData/picture.bin','w') as output:
            chunk=b'picture-data'*4096
            while size:
                body=chunk[:size];output.write(body);size-=len(body)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--worker',type=Path)
    args=parser.parse_args()
    if args.worker:
        worker(args.worker,args.baseline);return
    if not args.baseline or not args.output:parser.error('--baseline and --output required')
    from adhd.workspace_storage import WorkspaceStorage
    report={'fixture_only':True,'provider_tokens':None,'rss_boundary':'single inspection process including imports',
            'disk_boundary':'retained generated payload logical bytes; not physical free-space delta',
            'baseline_source_sha256':hashlib.sha256(args.baseline.read_bytes()).hexdigest(),'cases':{}}
    with tempfile.TemporaryDirectory(prefix='adhd-resource-benchmark-') as temp:
        folder=Path(temp).resolve()
        for label,size in (('small',16*1024),('large',32*1024**2)):
            source=folder/(label+'.hwpx');fixture(source,size)
            observations={'baseline':[],'candidate':[]}
            for index in range(3):
                for arm in (('baseline','candidate') if index%2==0 else ('candidate','baseline')):
                    command=[sys.executable,'-B',str(Path(__file__).resolve()),'--worker',str(source)]
                    if arm=='baseline':command+=['--baseline',str(args.baseline.resolve())]
                    result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=60,check=True)
                    observations[arm].append(json.loads(result.stdout))
            same=len({r['result_digest'] for rows in observations.values() for r in rows})==1
            medians={arm:{key:statistics.median(r[key] for r in rows)
                          for key in ('seconds','peak_rss_bytes')} for arm,rows in observations.items()}
            report['cases'][label]={'bytes':size,'same_evidence':same,'observations':observations,'medians':medians,
                'rss_reduction':1-medians['candidate']['peak_rss_bytes']/medians['baseline']['peak_rss_bytes'],
                'time_ratio':medians['candidate']['seconds']/medians['baseline']['seconds']}
        payload=folder/'source.dat';payload.write_bytes(b'x'*131072)
        baseline_dir=folder/'retained';baseline_dir.mkdir()
        for index in range(20):shutil.copy2(payload,baseline_dir/str(index))
        # Private temporary controller state, never the user's global registry.
        previous=os.environ.get('ADHD_HOME');os.environ['ADHD_HOME']=str(folder/'state')
        try:
            manager=WorkspaceStorage(folder)
            for _ in range(20):
                with manager.scratch('fixture',reserved_bytes=payload.stat().st_size,minimum_free=0) as scratch:
                    shutil.copy2(payload,scratch/'source.dat')
            report['retention']={'runs':20,'baseline_payload_bytes':sum(p.stat().st_size for p in baseline_dir.iterdir()),
                                 'candidate_scratch_bytes':sum(p.stat().st_size for p in manager.scratch_root.rglob('*') if p.is_file()),
                                 'controller_metadata_bytes':sum(p.stat().st_size for p in (folder/'state').rglob('*') if p.is_file())}
        finally:
            if previous is None:os.environ.pop('ADHD_HOME',None)
            else:os.environ['ADHD_HOME']=previous
    report['all_evidence_equal']=all(case['same_evidence'] for case in report['cases'].values())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'all_evidence_equal':report['all_evidence_equal'],
                      'cases':{name:{'rss_reduction':r['rss_reduction'],'time_ratio':r['time_ratio']} for name,r in report['cases'].items()},
                      'retention':report['retention']}))
    if not report['all_evidence_equal']:raise SystemExit(1)


if __name__=='__main__':main()
