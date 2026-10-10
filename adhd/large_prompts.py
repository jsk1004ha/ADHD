"""Small English delegation packets with verbatim original-language intent."""
from __future__ import annotations

import json
from .core import ROOT, digest


def plan_context_packet(task: dict, available_refs: dict) -> dict:
    """A recoverable task packet. Soft size targets never remove source text."""
    if not isinstance(task, dict) or not isinstance(available_refs, dict):
        raise ValueError('Task packet needs task data and references')
    required = ('task_id', 'revision', 'requirement_ids', 'verbatim_excerpts',
                'owned_paths', 'dependencies', 'acceptance', 'evidence_refs')
    if any(key not in task for key in required):
        raise ValueError('Task packet lacks required intent or ownership fields')
    if (not isinstance(task['task_id'], str) or type(task['revision']) is not int
            or any(not isinstance(task[key], list) for key in required[2:])):
        raise ValueError('Invalid task packet values')
    packet = {key: task[key] for key in required}
    packet['inputs'] = available_refs
    packet['packet_digest'] = digest(packet)
    packet['soft_limit_exceeded'] = len(json.dumps(packet, ensure_ascii=False)) > 6000
    return packet


def apply_context_delta(packet: dict, delta: dict) -> dict:
    if (delta.get('base_revision') != packet.get('revision')
            or type(delta.get('revision')) is not int
            or delta['revision'] <= packet['revision']
            or not isinstance(delta.get('changed'), dict)):
        raise ValueError('Context delta needs the exact previous revision')
    allowed = {'requirement_ids', 'verbatim_excerpts', 'owned_paths', 'dependencies',
               'acceptance', 'evidence_refs', 'inputs', 'append_verbatim_excerpts'}
    if set(delta['changed']) - allowed:
        raise ValueError('Invalid context delta field')
    changes = dict(delta['changed'])
    appended = changes.pop('append_verbatim_excerpts', [])
    if not isinstance(appended, list) or any(not isinstance(text, str) for text in appended):
        raise ValueError('Context delta excerpts must be verbatim strings')
    updated = {**packet, **changes, 'revision': delta['revision']}
    if appended:
        updated['verbatim_excerpts'] = packet['verbatim_excerpts'] + appended
    updated.pop('packet_digest', None)
    updated.pop('soft_limit_exceeded', None)
    updated['packet_digest'] = digest(updated)
    updated['soft_limit_exceeded'] = len(json.dumps(updated, ensure_ascii=False)) > 6000
    return updated


def read_decision(manifest: dict, *, question: str, scope: str,
                  source_hash: str, sections: list[str], independent_review: bool = False) -> dict:
    key = digest([question, scope, source_hash, sections])
    if independent_review:
        return {'read': True, 'reason': 'independent_review', 'key': key}
    return {'read': key not in manifest, 'reason': 'new_or_changed_source' if key not in manifest else 'unchanged_read',
            'key': key}


def worker_prompt(card: dict) -> str:
    plan_context_packet({'task_id': card['task_id'],
        'revision': card['contract_revision'],
        'requirement_ids': [row['id'] for row in card['requirements']],
        'verbatim_excerpts': card.get('verbatim_excerpts', [row['text'] for row in card['requirements']]),
        'owned_paths': card['write_paths'], 'dependencies': card.get('depends_on', []),
        'acceptance': [row['id'] for row in card['requirements']],
        'evidence_refs': []}, {'contract_hash': card['contract_hash'], 'inputs': card['inputs']})
    # The card already carries every field. Keep excerpts once; the packet
    # validation ensures a complete, recoverable first handoff.
    body = card
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
            + json.dumps(body, ensure_ascii=False, indent=2)
            + '\nWrite only assigned paths in the provided workspace. Do not commit; the controller '
            'will verify scope and create the submission commit. Return changed paths, reasoning '
            'for any interface issue, prepared check scenarios, NOT_RUN/observed check status '
            'and unresolved items. Do not call the entire user task complete.\n')
