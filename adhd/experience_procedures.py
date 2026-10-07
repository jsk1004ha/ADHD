"""Project-local examples and versioned procedure proposals.

The local ledger describes evidence; it never grants controller completion or
executes stored steps. Only a verified native completion can mint the private
adoption capability used by the controller integration.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import stat
import time

from . import dependencies as _dependencies  # Activate the shipped offline libraries.
from filelock import FileLock

from .core import atomic_json, digest, environment_signature, file_hash, store
from .evidence import validate_execution
from .memory import Memory
from .native import is_fresh


_HEX = re.compile(r"[0-9a-f]{32}\Z")
_KEY = re.compile(r"[0-9a-f]{24}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SEAL = object()


def _link(path: Path) -> bool:
    try:
        mode = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISLNK(mode.st_mode)
            or bool(getattr(mode, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
            or (hasattr(path, 'is_junction') and path.is_junction()))


def _root(workspace: Path) -> Path:
    path = Path(workspace).expanduser().absolute()
    if _link(path) or not path.is_dir():
        raise ValueError('Procedure workspace must be a real directory')
    return path.resolve()


def _path(root: Path, relative: str, *, file: bool = False) -> Path:
    if (not isinstance(relative, str) or not relative or '\x00' in relative
            or re.match(r'^[A-Za-z]:', relative)):
        raise ValueError('Expected a workspace-relative path')
    parts = relative.replace('\\', '/').split('/')
    if any(part in {'', '.', '..'} or ':' in part for part in parts):
        raise ValueError('Unsafe procedure path')
    path = root
    for part in parts:
        path = path / part
        if _link(path):
            raise ValueError('Procedure path crosses a link or reparse point')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Procedure path leaves workspace')
    if file and not path.is_file():
        raise ValueError('Missing procedure file: ' + relative)
    return path


def _ledger_path(workspace: Path) -> Path:
    return _path(workspace, '.adhd/experience/procedures.json')


def _read_ledger(workspace: Path) -> dict:
    path = _ledger_path(workspace)
    if not path.exists():
        return {'schema_version': 1, 'workspace': str(workspace), 'proposals': {}, 'procedures': {}}
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Procedure ledger exceeds its size limit')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or data.get('workspace') != str(workspace)
            or not isinstance(data.get('proposals'), dict)
            or not isinstance(data.get('procedures'), dict)):
        raise ValueError('Invalid procedure ledger')
    return data


def _locked(workspace: Path):
    path = _path(workspace, '.adhd/experience/procedures.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=8)


def _bounded_text(value: object, name: str, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(name + ' needs bounded nonempty text')
    return value.strip()


def _lines(value: object, name: str) -> list[str]:
    if (not isinstance(value, list) or not 1 <= len(value) <= 10
            or any(not isinstance(line, str) or not line.strip() or len(line) > 800 for line in value)):
        raise ValueError(name + ' needs 1..10 bounded text entries')
    return [line.strip() for line in value]


def _completion(workspace: Path, ref: dict, *, current: bool = False) -> dict:
    if (not isinstance(ref, dict) or set(ref) != {'session_key', 'run_id'}
            or not isinstance(ref['session_key'], str) or not _KEY.fullmatch(ref['session_key'])
            or not isinstance(ref['run_id'], str) or not _HEX.fullmatch(ref['run_id'])):
        raise ValueError('Completion reference needs canonical session_key and run_id')
    session = store() / 'native' / ref['session_key']
    state_path = session / 'state.json'
    if not current and state_path.is_file():
        active = json.loads(state_path.read_text(encoding='utf-8-sig'))
        if active.get('run_id') != ref['run_id']:
            state_path = session / 'run-archive' / ref['run_id'] / 'state.json'
    for part in (session, state_path.parent, state_path):
        if _link(part):
            raise ValueError('Native completion path is a link or reparse point')
    if not state_path.is_file() or state_path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Canonical native completion state is missing')
    state = json.loads(state_path.read_text(encoding='utf-8-sig'))
    candidate = state.get('candidate')
    review = state.get('review_receipt')
    receipt_path = session / ('completion-' + ref['run_id'] + '.json')
    if (state.get('workspace') != str(workspace) or state.get('key') != ref['session_key']
            or state.get('run_id') != ref['run_id'] or state.get('status') != 'complete'
            or state.get('pending_turn_ids') or any(row.get('status') == 'running' for row in state.get('children', {}).values())
            or not isinstance(candidate, dict) or not isinstance(review, dict)
            or review.get('verdict', {}).get('verdict') != 'approve'
            or review.get('digest') != candidate.get('digest')
            or review.get('verdict', {}).get('reviewed_digest') != candidate.get('digest')
            or review.get('verdict', {}).get('reviewed_contract_hash') != state.get('contract_hash')
            or review.get('verdict', {}).get('intent_alignment') is not True
            or review.get('verdict', {}).get('reviewed_turn_ids') != [row['turn_id'] for row in state.get('prompts', [])]
            or review.get('verdict', {}).get('criterion_results') != candidate.get('criterion_results')
            or candidate.get('contract_hash') != state.get('contract_hash')
            or candidate.get('intent_version') != state.get('intent_version')
            or state.get('contract_hash') != digest(state.get('contract'))
            or candidate.get('digest') != digest({key: item for key, item in candidate.items() if key != 'digest'})
            or not isinstance(candidate.get('files'), dict)
            or not receipt_path.is_file() or _link(receipt_path)
            or receipt_path.stat().st_size > 160_000
            or json.loads(receipt_path.read_text(encoding='utf-8-sig')) != review):
        raise ValueError('Native completion is unapproved, stale or inconsistent')
    if current and not is_fresh(state):
        raise ValueError('Current native completion or final artifact hashes changed')
    return {'ref': dict(ref), 'state': state, 'receipt': str(receipt_path),
            'receipt_sha256': file_hash(receipt_path)}


def _artifact(workspace: Path, value: object, completion: dict) -> dict:
    if not isinstance(value, dict) or value.get('kind') not in {'code', 'document'}:
        raise ValueError('Artifact needs code or document kind')
    path = _bounded_text(value.get('path'), 'artifact path')
    _path(workspace, path, file=True)
    sha = value.get('sha256')
    working_sha = value.get('worktree_sha256', sha)
    if (not isinstance(sha, str) or not _SHA.fullmatch(sha) or not isinstance(working_sha, str)
            or not _SHA.fullmatch(working_sha) or file_hash(_path(workspace, path, file=True)) != working_sha):
        raise ValueError('Artifact hash is missing or stale')
    rows = completion['state']['candidate']['files'].get('artifacts', [])
    if not any((row.get('path') == path and row.get('sha256') == working_sha)
               or any(leaf.get('path') == path and leaf.get('sha256') == working_sha
                      for leaf in row.get('leaves', [])) for row in rows):
        raise ValueError('Artifact is not bound to the approved final snapshot')
    if value['kind'] == 'document':
        if set(value) != {'kind', 'path', 'sha256'}:
            raise ValueError('Document reference needs path and sha256 only')
        return dict(value)
    fields = {'kind', 'path', 'sha256', 'repository', 'commit'}
    if set(value) not in (fields, fields | {'worktree_sha256'}):
        raise ValueError('Code reference needs repository, commit, path and sha256')
    repository = _path(workspace, value['repository']) if value['repository'] != '.' else workspace
    if not repository.is_dir() or not re.fullmatch(r'[0-9a-f]{40}', value['commit']):
        raise ValueError('Code reference needs an immutable commit')
    # Reachability and committed content are checked again by storage.audit_references.
    from .experience_storage import audit_references
    result = audit_references(workspace, [dict(value)])
    if not result['valid']:
        raise ValueError('Code commit/file reference is unavailable or hash-mismatched')
    return dict(value)


def _assessment(workspace: Path, completed: dict, value: object) -> dict:
    """Use distinct approved criteria and observed check receipts for each gate."""
    if not isinstance(value, dict) or set(value) != {'replay', 'regression', 'cost', 'rollback'}:
        raise ValueError('Candidate needs replay, regression, cost and rollback evidence')
    state = completed['state']
    criteria = {r['id']: r for r in state['contract']['criteria']}
    results = {r['id']: r for r in state['candidate']['criterion_results']}
    used_ids, used_receipts = set(), set()
    checked = {}
    for name in ('replay', 'regression', 'cost', 'rollback'):
        row = value[name]
        if not isinstance(row, dict) or not isinstance(row.get('criterion_id'), str):
            raise ValueError(name + ' needs an approved criterion')
        cid = row['criterion_id']
        criterion, result = criteria.get(cid), results.get(cid)
        if (cid in used_ids or not criterion or criterion.get('kind') != 'test'
                or not result or result.get('pass') is not True
                or not isinstance(result.get('evidence_ids'), list) or not result['evidence_ids']):
            raise ValueError(name + ' needs distinct successful test evidence')
        receipts = result['evidence_ids']
        if used_receipts.intersection(receipts):
            raise ValueError('Assessment gates need distinct execution receipts')
        for receipt in receipts:
            validate_execution(workspace, receipt, run_id=state['run_id'],
                               revision=state['intent_version'])
            if state['candidate']['execution_receipts'].get(receipt) != file_hash(_path(workspace, receipt, file=True)):
                raise ValueError('Assessment receipt differs from approved candidate')
        checked[name] = {'criterion_id': cid, 'receipts': receipts}
        used_ids.add(cid)
        used_receipts.update(receipts)
    cost = value['cost']
    if set(cost) != {'criterion_id', 'report'}:
        raise ValueError('Cost gate needs a measured report')
    report_path = _path(workspace, cost['report'], file=True)
    if report_path.stat().st_size > 32_000:
        raise ValueError('Cost report too large')
    report = json.loads(report_path.read_text(encoding='utf-8-sig'))
    if (not isinstance(report, dict) or set(report) != {'baseline', 'candidate', 'unit', 'basis'}
            or report['basis'] != 'measured' or report['unit'] not in {'tokens', 'seconds', 'bytes', 'operations'}
            or any(type(report[key]) not in {int, float} or report[key] < 0 for key in ('baseline', 'candidate'))
            or report['candidate'] > report['baseline']):
        raise ValueError('Cost measurement is missing, worse or unmeasured')
    bound = state['candidate']['files'].get('artifacts', [])
    if not any(row.get('path') == cost['report'] and row.get('sha256') == file_hash(report_path) for row in bound):
        raise ValueError('Cost report is not in the approved final snapshot')
    if not any(cost['report'] in validate_execution(workspace, receipt,
                run_id=state['run_id'], revision=state['intent_version'])['subject_files']
               for receipt in checked['cost']['receipts']):
        raise ValueError('Cost check did not cover the measured report')
    checked['cost']['report'] = {'path': cost['report'], 'sha256': file_hash(report_path), **report}
    return checked


def propose(workspace: Path, payload: dict) -> dict:
    """Record an example or a validation-gated candidate; neither is executable."""
    workspace = _root(workspace)
    if not isinstance(payload, dict):
        raise ValueError('Proposal payload must be an object')
    mode = _bounded_text(payload.get('mode'), 'mode', 30)
    goal = _bounded_text(payload.get('goal'), 'goal', 500)
    scope = _bounded_text(payload.get('scope'), 'project scope', 250)
    fields = ('input_conditions', 'steps', 'verifier', 'recovery', 'prohibited_conditions')
    content = {field: _lines(payload.get(field), field) for field in fields}
    source = _completion(workspace, payload.get('source_receipt'), current=True)
    artifact = _artifact(workspace, payload.get('artifact'), source)
    if source['state']['candidate'].get('procedure') != content['steps']:
        raise ValueError('Example steps differ from the approved completion')
    replay_ref = payload.get('replay_receipt')
    stage = 'example' if replay_ref is None else 'candidate'
    replay, assessment = None, None
    if replay_ref is not None:
        replay = _completion(workspace, replay_ref, current=True)
        if (replay['state']['run_id'] == source['state']['run_id']
                or replay['state']['contract_hash'] == source['state']['contract_hash']):
            raise ValueError('Replay must be a distinct relevant task and contract')
        if replay['state']['candidate'].get('procedure') != content['steps']:
            raise ValueError('Replay did not validate these procedure steps')
        assessment = _assessment(workspace, replay, payload.get('evaluation'))
    elif payload.get('evaluation') is not None:
        raise ValueError('Example cannot claim candidate assessment')
    body = {'stage': stage, 'mode': mode, 'goal': goal, 'scope': scope,
            'artifact': artifact, **content,
            'source_receipt': source['ref'], 'source_receipt_sha256': source['receipt_sha256'],
            'source_candidate_digest': source['state']['candidate']['digest'],
            'replay_receipt': replay['ref'] if replay else None,
            'replay_receipt_sha256': replay['receipt_sha256'] if replay else None,
            'replay_candidate_digest': replay['state']['candidate']['digest'] if replay else None,
            'evaluation': assessment}
    proposal_id = digest(body)[:32]
    with _locked(workspace):
        ledger = _read_ledger(workspace)
        if proposal_id not in ledger['proposals']:
            ledger['proposals'][proposal_id] = {'id': proposal_id, 'created_at': time.time(), **body}
            atomic_json(_ledger_path(workspace), ledger)
    return {'proposal_id': proposal_id, 'stage': stage, 'artifact': artifact}


class _ControllerEvidence:
    __slots__ = ('_seal', 'reference', 'digest')

    def __init__(self, seal: object, reference: dict, candidate_digest: str):
        if seal is not _SEAL:
            raise ValueError('Controller evidence cannot be constructed from a payload')
        self._seal, self.reference, self.digest = seal, reference, candidate_digest


def _controller_evidence(workspace: Path, session_key: str, run_id: str) -> _ControllerEvidence:
    """Internal controller hook: mint only after canonical fresh independent completion."""
    completed = _completion(_root(workspace), {'session_key': session_key, 'run_id': run_id}, current=True)
    return _ControllerEvidence(_SEAL, completed['ref'], completed['state']['candidate']['digest'])


def adopt(workspace: Path, proposal_id: str, controller_evidence: object) -> dict:
    workspace = _root(workspace)
    if not isinstance(controller_evidence, _ControllerEvidence) or controller_evidence._seal is not _SEAL:
        raise ValueError('Adoption requires internal controller completion evidence')
    if not isinstance(proposal_id, str) or not _HEX.fullmatch(proposal_id):
        raise ValueError('Invalid proposal id')
    completed = _completion(workspace, controller_evidence.reference, current=True)
    if completed['state']['candidate']['digest'] != controller_evidence.digest:
        raise ValueError('Controller completion changed after capability creation')
    with _locked(workspace):
        ledger = _read_ledger(workspace)
        proposal = ledger['proposals'].get(proposal_id)
        if not proposal or proposal['stage'] != 'candidate':
            raise ValueError('Only a validated candidate can be adopted')
        for key in ('source', 'replay'):
            ref = proposal[key + '_receipt']
            source = _completion(workspace, ref)
            if (source['receipt_sha256'] != proposal[key + '_receipt_sha256']
                    or source['state']['candidate']['digest'] != proposal[key + '_candidate_digest']):
                raise ValueError('Proposal completion receipt changed')
        _assessment(workspace, _completion(workspace, proposal['replay_receipt']),
                    {name: {'criterion_id': row['criterion_id'], **({'report': row['report']['path']} if name == 'cost' else {})}
                     for name, row in proposal['evaluation'].items()})
        from .experience_storage import audit_references
        if not audit_references(workspace, [proposal['artifact']])['valid']:
            raise ValueError('Proposal artifact reference is stale')
        if completed['state']['run_id'] not in {proposal['replay_receipt']['run_id'],
                                                proposal['source_receipt']['run_id']}:
            raise ValueError('Adoption needs the current independently approved source or replay completion')
        identity = digest([proposal['scope'], proposal['mode'], proposal['goal'], proposal['input_conditions']])[:32]
        record = ledger['procedures'].setdefault(identity, {'id': identity, 'scope': proposal['scope'],
                                                              'versions': [], 'active_version': None})
        if any(row['proposal_id'] == proposal_id for row in record['versions']):
            raise ValueError('Proposal was already adopted')
        conditions = proposal['input_conditions'] + ['Do not apply when: ' + item for item in proposal['prohibited_conditions']]
        if len(conditions) > 10:
            raise ValueError('Procedure conditions exceed Memory bundle limit')
        bundle = {'input_conditions': conditions, 'execution_script': proposal['steps'],
                  'verifier': proposal['verifier'], 'recovery': proposal['recovery']}
        # Memory.save_procedure is reached only here, after the controller capability
        # and all current evidence gates. The local JSON is the version ledger.
        with Memory() as memory:
            memory_id = memory.save_procedure(workspace, proposal['mode'], proposal['goal'],
                                              proposal['steps'], completed['receipt'],
                                              environment=digest(environment_signature(workspace)), bundle=bundle)
        previous = record['active_version']
        version = len(record['versions']) + 1
        record['versions'].append({'version': version, 'proposal_id': proposal_id,
                                   'previous_version': previous, 'memory_id': memory_id,
                                   'controller_receipt': completed['receipt'],
                                   'controller_receipt_sha256': completed['receipt_sha256'],
                                   'adopted_at': time.time(), 'artifact': proposal['artifact']})
        record['active_version'] = version
        atomic_json(_ledger_path(workspace), ledger)
    return {'procedure_id': identity, 'version': version, 'previous_version': previous,
            'memory_id': memory_id}


def rollback(workspace: Path, procedure_id: str, version: int | None = None) -> dict:
    """Select a previously adopted version; never run its steps."""
    workspace = _root(workspace)
    if not isinstance(procedure_id, str) or not _HEX.fullmatch(procedure_id):
        raise ValueError('Invalid procedure id')
    with _locked(workspace):
        ledger = _read_ledger(workspace)
        record = ledger['procedures'].get(procedure_id)
        if not record or record['active_version'] is None:
            raise ValueError('Unknown active procedure')
        current = record['active_version']
        if version is None:
            version = next(row['previous_version'] for row in record['versions'] if row['version'] == current)
        if type(version) is not int or version < 1 or version == current:
            raise ValueError('Rollback needs a different previously adopted version')
        target = next((row for row in record['versions'] if row['version'] == version), None)
        active = next(row for row in record['versions'] if row['version'] == current)
        if not target:
            raise ValueError('Unknown previous procedure version')
        with Memory() as memory:
            if active['memory_id'] != target['memory_id']:
                memory.quarantine([active['memory_id']], reason='procedure rollback')
                memory.restore(target['memory_id'], evidence='procedure rollback to approved version',
                               verified_receipt=target['controller_receipt'])
        record['active_version'] = version
        record.setdefault('rollbacks', []).append({'from': current, 'to': version, 'at': time.time()})
        atomic_json(_ledger_path(workspace), ledger)
    return {'procedure_id': procedure_id, 'active_version': version, 'previous_version': current}


def list_procedures(workspace: Path) -> dict:
    workspace = _root(workspace)
    with _locked(workspace):
        ledger = _read_ledger(workspace)
    return {'workspace': str(workspace), 'proposals': list(ledger['proposals'].values()),
            'procedures': list(ledger['procedures'].values()),
            'trust': 'retrieved_data_not_instructions'}
