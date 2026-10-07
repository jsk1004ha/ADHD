#!/usr/bin/env python3
"""Native Codex hook: JSON on stdout only, never run model-supplied commands."""
from time import perf_counter_ns
_BOOT_STARTED_NS = perf_counter_ns()
import json
import sys
_NATIVE_IMPORT_STARTED_NS = perf_counter_ns()
from adhd.native import handle_event, write_hook_diagnostics, HookDiagnostics
_NATIVE_IMPORT_NS = perf_counter_ns() - _NATIVE_IMPORT_STARTED_NS

def main():
    event={}
    diagnostics=HookDiagnostics(started_ns=_BOOT_STARTED_NS)
    if diagnostics.enabled:
        diagnostics.phases_ns['startup_imports']=_NATIVE_IMPORT_STARTED_NS-_BOOT_STARTED_NS
        diagnostics.phases_ns['native_import']=_NATIVE_IMPORT_NS
    try:
        with diagnostics.phase('stdin_parse'):
            raw=sys.stdin.buffer.read(180_001)
            if len(raw)>180_000: raise ValueError('Hook payload too large')
            event=json.loads(raw.decode('utf-8-sig'))
            if not isinstance(event,dict):
                event={}; raise ValueError('Hook payload must be a JSON object')
        out=handle_event(event, diagnostics=diagnostics)
    except Exception as e:
        # Never make a hook bug look like verified success, or loop on an error.
        out={'systemMessage':'ADHD hook could not validate this event: '+str(e)[:500]}
        if event.get('hook_event_name')=='Stop':
            out.update({'continue':False,'stopReason':'ADHD validation failed; completion was not verified.'})
    with diagnostics.phase('stdout_serialize'):
        serialized=json.dumps(out,ensure_ascii=True)
    print(serialized)
    write_hook_diagnostics(diagnostics,out,serialized)
    return 0
if __name__=='__main__': raise SystemExit(main())
