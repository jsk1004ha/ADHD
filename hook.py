#!/usr/bin/env python3
"""Native Codex hook: JSON on stdout only, never run model-supplied commands."""
import json
import sys
from apzn.native import handle_event

def main():
    event={}
    try:
        raw=sys.stdin.buffer.read(180_001)
        if len(raw)>180_000: raise ValueError('Hook payload too large')
        event=json.loads(raw.decode('utf-8-sig'))
        if not isinstance(event,dict):
            event={}; raise ValueError('Hook payload must be a JSON object')
        out=handle_event(event)
    except Exception as e:
        # Never make a hook bug look like verified success, or loop on an error.
        out={'systemMessage':'ADHD hook could not validate this event: '+str(e)[:500]}
        if event.get('hook_event_name')=='Stop':
            out.update({'continue':False,'stopReason':'ADHD validation failed; completion was not verified.'})
        print(json.dumps(out,ensure_ascii=True));return 0
    print(json.dumps(out,ensure_ascii=True));return 0
if __name__=='__main__': raise SystemExit(main())
