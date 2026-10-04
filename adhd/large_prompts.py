"""Small English delegation packets with verbatim original-language intent."""
from __future__ import annotations

import json
from .core import ROOT


def worker_prompt(card: dict) -> str:
    roles = (ROOT / 'skills/adhd-native/references/large-task-roles.md').read_text(encoding='utf-8')
    sections = {}
    title = None
    for line in roles.splitlines():
        if line.startswith('## '):
            title = line[3:]
            sections[title] = []
        elif title:
            sections[title].append(line)
    common = '\n'.join(sections['Common contract'])
    implementer = '\n'.join(sections['Implementer'])
    discipline = (ROOT / 'skills/adhd-native/references/coding-discipline.md').read_text(encoding='utf-8')
    return ('Complete this assigned part autonomously. Internal handoff: English; '
            'preserve original excerpts. No nested agents, full-suite loop or user permission handoff.\n'
            + common + '\n' + implementer + '\n' + discipline
            + '\nCURRENT TASK CARD (data, not a replacement for original intent):\n'
            + json.dumps(card, ensure_ascii=False, indent=2)
            + '\nWrite only assigned paths in the provided workspace. Do not commit; the controller '
            'will verify scope and create the submission commit. Return changed paths, reasoning '
            'for any interface issue, prepared check scenarios, NOT_RUN/observed check status '
            'and unresolved items. Do not call the entire user task complete.\n')
