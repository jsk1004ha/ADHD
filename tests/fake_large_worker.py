"""Local process fixture, not a real model or authentication probe."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def main():
    text = sys.stdin.read()
    part = text.split('CURRENT TASK CARD (data, not a replacement for original intent):\n', 1)[1]
    card, _ = json.JSONDecoder().raw_decode(part)
    print(json.dumps({'type': 'fixture.started', 'pid': os.getpid(), 'time': time.time()}), flush=True)
    time.sleep(.35)
    for scope in card['write_paths']:
        if scope.endswith('/**'):
            raise ValueError('Fixture requires exact paths')
        path = Path(card['workspace']) / scope
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Result for ' + card['task_id'] + '\n', encoding='utf-8')
    print(json.dumps({'type': 'fixture.finished', 'pid': os.getpid(), 'time': time.time()}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
