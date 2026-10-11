"""Bounded failure-loop observations and nonterminal strategy steering."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .core import atomic_text, digest, redact, safe_path
from .recovery import verified_failure_signature

CHURN_GRACE = 3
RING_SIZE = 12
LOOP_TOOLS = frozenset({'Bash', 'exec', 'exec_command', 'apply_patch', 'write_stdin'})
LOOP_STATUSES = frozenset({'working', 'reviewing', 'revising'})
TRANSIENT = ('fp_ring', 'loop_warned', 'loop_notes', 'loop_seen_ids', 'loop_hits',
             'loop_sessions', 'churn', 'ladder_stage', 'view_hint_sig')
SECRET_NAME = r'(?:[a-z0-9]+[-_])*(?:api[-_]?key|password|secret|token)'
# Shell escapes and PowerShell doubled quotes must not leave a value's tail visible.
SECRET_FLAG = re.compile(
    rf'''(?i)(--{SECRET_NAME}(?:\s+|=))("(?:\\.|`.|""|[^"\\`])*"|'(?:\\.|''|[^'\\])*'|(?:\\.|`.|[^\s,;])+)''')


def reset_loop(state: dict, *, reviews: bool = False) -> None:
    for key in TRANSIENT:
        state.pop(key, None)
    if reviews:
        state.pop('review_findings', None)


def _text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _redact_diagnostic(text: str) -> str:
    return redact(SECRET_FLAG.sub(r'\1[REDACTED]', text))


def _redact_arguments(value: object) -> object:
    # Redact strings before JSON encoding adds escapes around quoted CLI values.
    if isinstance(value, dict):
        return {key: '[REDACTED]' if re.fullmatch(SECRET_NAME, str(key), re.I)
                else _redact_arguments(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_arguments(item) for item in value]
    return _redact_diagnostic(value) if isinstance(value, str) else value


def _result(value: object) -> tuple[str, str]:
    """Known terminal fields beat textual clues; missing/in-progress is unknown."""
    if isinstance(value, dict):
        output = _text(value.get('output', value.get('content', '')))
        if value.get('stderr'):
            output += '\n' + _text(value['stderr'])
        code = value.get('exit_code', value.get('exitCode'))
        if type(code) is int:
            return ('success' if code == 0 else 'failure'), f'exit_code={code}\n' + output
        if value.get('isError') is True:
            return 'failure', output
        if value.get('session_id') is not None:
            return 'unknown', output
        return 'unknown', output
    if isinstance(value, str):
        # Native exec output envelopes (not arbitrary JSON/string contents).
        code = re.search(r'(?im)^Process exited with code (-?\d+)\s*$', value)
        if code:
            return ('success' if int(code[1]) == 0 else 'failure'), f'exit_code={code[1]}\n' + value.split('Final output:', 1)[-1]
        if re.search(r'(?im)^Process running with session ID ', value):
            return 'unknown', value
        # Legacy Bash payloads used by supported CLI hooks.
        if re.search(r'(?im)^\s*(?:FAILED(?:\s+\S+|\s*\()|Traceback \(most recent call last\):|AssertionError\b)', value):
            return 'failure', value
        if re.search(r'(?m)^\s*\d+ passed(?:\b|,)', value) or value.strip() == 'Success. Updated the following files:':
            return 'success', value
    return 'unknown', _text(value)


def observe_tool_loop(state: dict, event: dict) -> str | None:
    name = event.get('tool_name')
    if state.get('status') not in LOOP_STATUSES or name not in LOOP_TOOLS or 'tool_input' not in event:
        return None
    identity = event.get('tool_use_id')
    if isinstance(identity, str) and identity:
        seen = state.setdefault('loop_seen_ids', [])
        if identity in seen:
            return None
        seen.append(identity)
        del seen[:-100]
    response = event.get('tool_response', event.get('tool_output'))
    outcome, output = _result(response)
    args = event['tool_input']
    command_id = digest([name, args, event.get('cwd', state.get('workspace'))])
    display = _text(_redact_arguments(args))[:120]
    sessions = state.setdefault('loop_sessions', {})
    session_id = args.get('session_id') if isinstance(args, dict) and name == 'write_stdin' else None
    origin = sessions.get(str(session_id)) if session_id is not None else None
    if origin:
        command_id, display = origin['command_id'], origin['cmd']
        name = origin['tool']
    if outcome == 'unknown':
        pending = response.get('session_id') if isinstance(response, dict) else None
        if isinstance(response, str):
            match = re.search(r'Process running with session ID (\d+)', response)
            if match: pending = match[1]
        if pending is not None and name in {'exec', 'exec_command', 'Bash'}:
            sessions[str(pending)] = {'command_id': command_id, 'cmd': display, 'tool': name}
            for key in list(sessions)[:-30]: del sessions[key]
        return None
    if session_id is not None:
        sessions.pop(str(session_id), None)
    ring = state.setdefault('fp_ring', [])
    warned = state.setdefault('loop_warned', {})
    notes = state.setdefault('loop_notes', [])
    if outcome == 'success':
        state['fp_ring'] = [row for row in ring if row[0] != command_id]
        for fp, row in list(warned.items()):
            if isinstance(row, dict) and row.get('command_id') == command_id:
                del warned[fp]
        state['loop_notes'] = [row for row in notes if row.get('command_id') != command_id]
        return None
    # rg exit 1 means no match, not a failed execution worth strategy steering.
    command = args.get('cmd', args.get('command', '')) if isinstance(args, dict) else ''
    if isinstance(command, str) and re.match(r'^\s*rg(?:\s|$)', command) and isinstance(response, dict):
        if (response.get('exit_code', response.get('exitCode')) == 1
                and not response.get('output') and not response.get('content') and not response.get('stderr')):
            return None
    fingerprint = digest([command_id, verified_failure_signature(output)])
    state['fp_ring'] = ring = (ring + [[command_id, fingerprint]])[-RING_SIZE:]
    count = sum(row[1] == fingerprint for row in ring)
    note = next((row for row in notes if row['fp'] == fingerprint), None)
    if note is None:
        note = {'fp': fingerprint, 'command_id': command_id, 'tool': name,
                'cmd': display, 'n': 0}
        notes.append(note)
        del notes[:-6]
    note['n'] = count
    if count < 3:
        return None
    level = 2 if count >= 5 else 1
    prior = warned.get(fingerprint, {})
    if isinstance(prior, dict) and prior.get('level', 0) >= level:
        return None
    warned[fingerprint] = {'level': level, 'command_id': command_id}
    for fp in list(warned)[:-30]:
        del warned[fp]
    state['loop_hits'] = 1
    if level == 2:
        return f'Loop ({count}x): this command keeps failing identically. Change the hypothesis and test the smallest failing check before retrying.'
    return f'Loop: the same command produced the same failure {count} times. Read the evidence and change the hypothesis before retrying.'


def _words(text: str) -> set[str]:
    words = set(re.findall(r'[a-z0-9가-힣_./-]{3,}', text[:2000].lower()))
    return words - {'reviewer', 'rejected', 'candidate', 'independent', 'the', 'for', 'and'}


def repeated_review_rejection(state: dict, finding: str, *, keys: list[str] | None = None) -> bool:
    history = state.setdefault('review_findings', [])
    revision = [state.get('run_id'), state.get('intent_version')]
    finding = _redact_diagnostic(finding)[:2000]
    current = _words(finding)
    previous = history[-1] if history else None
    if isinstance(previous, str):  # A pre-upgrade diagnostic has no contract binding.
        previous = None
    repeated = False
    if previous and previous['revision'] == revision and current:
        old = _words(previous['text'])
        union = old | current
        compatible = not keys or not previous['keys'] or keys == previous['keys']
        paths = lambda text: set(re.findall(r'\b[\w./-]+\.(?:py|js|ts|tsx|md|json|pdf|docx|toml)\b', text.lower()))
        current_paths, old_paths = paths(finding), paths(previous['text'])
        if current_paths and old_paths and current_paths.isdisjoint(old_paths):
            compatible = False
        repeated = compatible and bool(union) and len(old & current) / len(union) >= 0.6
    history.append({'text': finding, 'revision': revision, 'keys': keys or []})
    del history[:-3]
    return repeated


def failed_approaches(state: dict, limit: int = 360) -> str:
    items, seen = [], set()
    for row in reversed(state.get('failure_history', [])[-8:]):
        key = row.get('verified_signature') or digest([row.get('category'), row.get('detail')])
        if key in seen:
            continue
        seen.add(key)
        items.append(row.get('category', 'unknown') + ': ' + _redact_diagnostic(row.get('detail', ''))[:80])
    for row in state.get('loop_notes', [])[-3:]:
        items.append(f"{row['tool']} x{row['n']}: {_redact_diagnostic(row['cmd'])}")
    return ('; '.join(items) or 'none recorded')[:limit]


def _write_stuck_note(state: dict, bridge_dir: Path, stage: int) -> str:
    lines = [f'# Stuck note (stage {stage}, stagnation {state.get("stagnation", 0)})', '',
             '## Failed approaches']
    for row in state.get('failure_history', [])[-10:]:
        lines.append('- ' + row.get('category', 'unknown') + ': ' + _redact_diagnostic(row.get('detail', ''))[:300].replace('\n', ' '))
    for row in state.get('loop_notes', []):
        lines.append(f"- {row['tool']} x{row['n']}: {_redact_diagnostic(row['cmd'])}")
    for row in state.get('review_findings', []):
        lines.append('- reviewer: ' + _redact_diagnostic(row if isinstance(row, str) else row['text'])[:300].replace('\n', ' '))
    lines += ['', '## Required before the next attempt',
              '1. Cite the failing evidence and identify the assumption it falsifies.',
              '2. Pick the single smallest failing check; change one assumption.',
              '3. Put the changed assumption in plan.approach and the check in plan.verification.',
              '4. Submit a changed plan before another candidate.']
    try:
        raw = bridge_dir / 'stuck.md'
        for node in (raw, *raw.parents):
            if node == Path(state['workspace']): break
            if node.is_symlink() or hasattr(node, 'is_junction') and node.is_junction():
                raise ValueError('Unsafe stuck note path')
        path = safe_path(Path(state['workspace']), str(raw.relative_to(state['workspace'])))
        atomic_text(path, '\n'.join(lines) + '\n')
        return str(path)
    except (OSError, ValueError):
        return ''


def steer_if_stuck(state: dict, bridge_dir: Path) -> str | None:
    stage = state.get('stagnation', 0) // state['policy']['max_stagnation']
    if stage == 0:
        state['ladder_stage'] = 0
        return None
    if stage <= state.get('ladder_stage', 0):
        return None
    state['ladder_stage'] = stage
    state['needs_replan'] = True
    note = _write_stuck_note(state, bridge_dir, stage)
    text = f'No verified progress for {state["stagnation"]} rounds. A changed plan is required.'
    if note:
        text += ' Details: ' + note + '.'
    if stage >= 2:
        text += ' Soft reset: solve the smallest failing check first.'
        if state['policy'].get('max_astra_calls', 0) > state.get('astra_calls', 0):
            text += ' If an unresolved design judgment remains, consult adhd-architect once.'
    if stage >= 3:
        text += ' Switch to a materially different implementation strategy.'
    return text + ' Already failed: ' + failed_approaches(state) + '.'
