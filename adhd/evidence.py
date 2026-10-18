"""Bounded local process observations for native candidate evidence.

These receipts describe a process this CLI executed. They are not signatures and
cannot defend against a user with write access to both the receipt and files.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
from typing import Any

from .core import atomic_json, digest, file_hash, safe_path
from .runner import Halt, execute_native_check
from .time_budget import TimeBudgetExpired, validate_seconds


def recognized_test_failures(stdout: str, stderr: str, exit_code: int) -> int | None:
    """Count failures only from unittest or pytest summaries, never arbitrary prose."""
    output = (stderr + '\n' + stdout)[-128_000:]
    if re.search(r'(?m)^Ran \d+ tests? in [\d.]+s\s*$', output):
        summaries = re.findall(r'(?m)^FAILED \(([^\n]+)\)\s*$', output)
        if summaries:
            fields = dict((name, int(count)) for name, count in
                          re.findall(r'(failures|errors)=(\d+)', summaries[-1]))
            return sum(fields.values()) if fields else None
        if exit_code == 0 and re.search(r'(?m)^OK(?: \(skipped=\d+\))?\s*$', output):
            return 0
    summaries = re.findall(r'(?m)^=+\s*(.+?)\s*=+\s*$', output)
    for summary in reversed(summaries):
        counts = re.findall(r'(\d+)\s+(failed|error|errors|passed|skipped|xfailed|xpassed)\b', summary)
        if counts:
            failed = sum(int(n) for n, kind in counts if kind in {'failed', 'error', 'errors'})
            if failed or exit_code == 0 and any(kind == 'passed' for _, kind in counts):
                return failed
    return None


def receipt_test_failures(workspace: Path, receipt_rel: str, receipt: dict) -> int | None:
    parent = _path(workspace, receipt_rel).parent
    def tail(path: Path) -> str:
        with path.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 128_000))
            return stream.read(128_000).decode('utf-8', errors='replace')
    stdout = tail(parent / 'check.stdout.jsonl')
    stderr = tail(parent / 'check.stderr.log')
    return recognized_test_failures(stdout, stderr, receipt['result']['exit_code'])


def _path(workspace: Path, relative: str, *, existing: bool = True) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('Evidence paths must be relative to the workspace')
    root = workspace.resolve()
    path = safe_path(root, relative)
    cursor = root
    for part in Path(relative.replace('\\', '/')).parts:
        cursor = cursor / part
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise ValueError('Evidence paths cannot cross a link or junction')
    if existing and not path.is_file():
        raise ValueError('Missing evidence file: ' + relative)
    return path


def subject_manifest(workspace: Path, paths: list[str]) -> dict[str, str]:
    if not isinstance(paths, list) or not 1 <= len(paths) <= 500 or len(set(paths)) != len(paths):
        raise ValueError('Specify 1..500 distinct test inputs')
    return {rel: file_hash(_path(workspace, rel)) for rel in sorted(paths)}


def _argv(spec: dict) -> list[str]:
    argv = spec.get('argv')
    if not isinstance(argv, list) or not 1 <= len(argv) <= 32 or any(
        not isinstance(x, str) or not x or len(x) > 4000 for x in argv
    ):
        raise ValueError('Check argv must be a bounded string array')
    executable = shutil.which(argv[0]) or (argv[0] if Path(argv[0]).is_file() else '')
    if not executable:
        raise ValueError('Check executable not found')
    binary = Path(executable).resolve()
    if not binary.is_file() or binary.suffix.lower() in {'.cmd', '.bat', '.ps1', '.sh'}:
        raise ValueError('Check needs a real executable, not a shell wrapper')
    return [str(binary), *argv[1:]]


def run_check(spec: dict, workspace: Path, *, deadline_monotonic: float | None = None) -> dict:
    """Execute through Codex's normal CLI tool call, never from a host hook."""
    if not isinstance(spec, dict):
        raise ValueError('Check spec must be an object')
    began = time.monotonic()
    if deadline_monotonic is not None and (type(deadline_monotonic) not in {int, float}
                                           or not math.isfinite(deadline_monotonic)):
        raise ValueError('Check deadline must be a finite monotonic instant')
    if 'time_limit_seconds' in spec:
        own_deadline = began + validate_seconds(spec['time_limit_seconds'], 'time_limit_seconds')
        deadline_monotonic = min(deadline_monotonic, own_deadline) if deadline_monotonic is not None else own_deadline
    if deadline_monotonic is not None and began >= deadline_monotonic:
        raise TimeBudgetExpired('Check was not started: time budget exhausted')
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError('Missing check workspace')
    argv = _argv(spec)
    cwd_rel = spec.get('cwd', '.')
    cwd = workspace if cwd_rel == '.' else safe_path(workspace, cwd_rel)
    if not cwd.is_dir():
        raise ValueError('Missing check cwd')
    paths = spec.get('subject_paths')
    if not isinstance(paths,list):
        raise ValueError('Check needs subject_paths')
    before = subject_manifest(workspace, paths)
    run_id = spec.get('run_id')
    revision = spec.get('contract_revision')
    if not isinstance(run_id, str) or not run_id or type(revision) is not int or revision < 0:
        raise ValueError('Check needs a run_id and contract_revision')
    timeout = spec.get('timeout', 300)
    if type(timeout) not in {int, float} or not 1 <= timeout <= 1800:
        raise ValueError('Check timeout must be 1..1800 seconds')
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise TimeBudgetExpired('Check was not started: time budget exhausted while preparing inputs')
    check_id = uuid.uuid4().hex
    out = _path(workspace, '.adhd/checks/' + check_id + '/receipt.json', existing=False)
    out.parent.mkdir(parents=True, exist_ok=False)
    prefix = out.parent / 'check'
    subject_path = out.parent / 'subject.json'
    atomic_json(subject_path, {'schema_version': 2, 'files': before,
                               'digest': digest(before)})
    started = datetime.now(timezone.utc).isoformat()
    execution_status = 'exited'
    trial_id, registry, monitor = None, None, None
    if spec.get('trial') is not None:
        from .trial_registry import TrialRegistry
        from .resource_metrics import ProcessTreeMonitor
        metadata = dict(spec['trial'])
        registry_relative = metadata.pop('registry', '.adhd/trials-v1')
        registry = TrialRegistry(_path(workspace, registry_relative, existing=False))
        trial_id = registry.start(**metadata)
        monitor = ProcessTreeMonitor()
        monitor.__enter__()
    try:
        process_timeout = timeout
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeBudgetExpired('Check was not started: time budget exhausted before launch')
            process_timeout = min(timeout, remaining)
        exit_code = execute_native_check(argv, cwd, prefix, timeout=process_timeout)
    except TimeBudgetExpired:
        if registry is not None:
            registry.finish(trial_id, 'timeout', reason='Time budget expired before process launch')
        raise
    except Halt as error:
        execution_status = 'timeout' if error.status == 'budget_exhausted' else 'cancelled'
        exit_code = 124 if execution_status == 'timeout' else 130
    except BaseException as error:
        if registry is not None:
            registry.finish(trial_id, 'failed', reason=type(error).__name__)
        raise
    finally:
        if monitor is not None:
            monitor.__exit__()
    finished = datetime.now(timezone.utc).isoformat()
    after = subject_manifest(workspace, paths)
    receipt = {
        'schema_version': 2, 'evidence_id': check_id, 'kind': 'test_run',
        'assurance': 'local_process_observed', 'run_id': run_id,
        'contract_revision': revision, 'tool_use_id': None,
        'subject_manifest': str(subject_path.relative_to(workspace)).replace('\\', '/'),
        'subject_manifest_sha256': file_hash(subject_path),
        'subject_files': before, 'subject_digest': digest(before),
        'invocation': {'argv': argv, 'cwd': str(cwd),
                       'executable_sha256': file_hash(Path(argv[0])),
                       'entrypoint_sha256': None,
                       'environment_fingerprint': digest([sys.version, os.name])},
        'result': {'started_at': started, 'finished_at': finished,
                   'status': execution_status,
                   'exit_code': exit_code, 'inputs_unchanged': before == after,
                   'stdout_sha256': file_hash(prefix.with_suffix('.stdout.jsonl')),
                   'stderr_sha256': file_hash(prefix.with_suffix('.stderr.log'))},
    }
    if deadline_monotonic is not None:
        receipt['invocation']['timeout_seconds'] = process_timeout
        receipt['result']['budget_limited'] = execution_status == 'timeout' and process_timeout < timeout
    atomic_json(out, receipt)
    if registry is not None:
        resources = monitor.report()
        post_task = monitor.observe_post_task()
        resources['post_task'] = post_task
        registry.finish(trial_id, 'success' if exit_code == 0 and before == after else
                        'timeout' if execution_status == 'timeout' else
                        'cancelled' if execution_status == 'cancelled' else 'failed',
                        resources=resources, metrics={'peak_private_bytes': resources['peak_private_bytes']})
        receipt['trial_observation'] = {'id': trial_id, 'resources': resources,
                                       'quality_acceptance': 'not_inferred_from_check_exit'}
        atomic_json(out, receipt)
    return {'receipt': str(out.relative_to(workspace)).replace('\\', '/'),
            'evidence_id': check_id, 'exit_code': exit_code,
            'inputs_unchanged': before == after, 'status': execution_status}


def validate_execution(workspace: Path, receipt_rel: str, *, run_id: str,
                       revision: int, expect_failure: bool = False) -> dict:
    """Recheck receipt, logs, executable and declared test inputs."""
    receipt_path = _path(workspace, receipt_rel)
    if not re.fullmatch(r'\.adhd/checks/[0-9a-f]{32}/receipt\.json',
                        receipt_path.relative_to(workspace.resolve()).as_posix()):
        raise ValueError('Execution receipt is outside the ADHD check store')
    if receipt_path.stat().st_size > 160_000:
        raise ValueError('Oversized execution receipt')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
    if (not isinstance(receipt, dict) or receipt.get('schema_version') != 2
            or receipt.get('assurance') != 'local_process_observed'
            or receipt.get('run_id') != run_id or receipt.get('contract_revision') != revision):
        raise ValueError('Execution receipt belongs to another run or revision')
    if receipt.get('evidence_id') != receipt_path.parent.name or receipt.get('kind') != 'test_run':
        raise ValueError('Execution identity mismatch')
    result = receipt.get('result', {})
    exit_code = result.get('exit_code')
    valid_exit = (type(exit_code) is int and exit_code != 0) if expect_failure else exit_code == 0
    if (result.get('status') != 'exited' or not valid_exit
            or result.get('inputs_unchanged') is not True):
        raise ValueError('Failed or stale test execution')
    try:
        started=datetime.fromisoformat(result['started_at'])
        finished=datetime.fromisoformat(result['finished_at'])
        if started.tzinfo is None or finished.tzinfo is None or finished < started:
            raise ValueError('Invalid check times')
    except (KeyError, TypeError) as error:
        raise ValueError('Missing check times') from error
    invocation = receipt.get('invocation', {})
    argv = invocation.get('argv', [])
    if not argv or not Path(argv[0]).is_file() or file_hash(Path(argv[0])) != invocation.get('executable_sha256'):
        raise ValueError('Execution program changed')
    for suffix, field in [('.stdout.jsonl', 'stdout_sha256'), ('.stderr.log', 'stderr_sha256')]:
        path = receipt_path.parent / ('check' + suffix)
        if not path.is_file() or file_hash(path) != result.get(field):
            raise ValueError('Execution log changed')
    files = receipt.get('subject_files')
    if not isinstance(files, dict) or not files or subject_manifest(workspace, list(files)) != files:
        raise ValueError('Checked inputs changed')
    subject_rel=receipt.get('subject_manifest')
    if not isinstance(subject_rel,str) or not subject_rel.startswith('.adhd/checks/'):
        raise ValueError('Missing check subject manifest')
    subject_path=_path(workspace,subject_rel)
    if subject_path.parent != receipt_path.parent or file_hash(subject_path) != receipt.get('subject_manifest_sha256'):
        raise ValueError('Check subject manifest changed')
    subject=json.loads(subject_path.read_text(encoding='utf-8-sig'))
    if subject != {'schema_version':2,'files':files,'digest':digest(files)}:
        raise ValueError('Check subject manifest mismatch')
    if digest(files) != receipt.get('subject_digest'):
        raise ValueError('Execution subject digest changed')
    return receipt


def _redacted(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return '[depth limit]'
    if isinstance(value, dict):
        return {str(key): ('[redacted]' if re.search(r'(?i)(?:secret|password|token|authorization|api[_-]?key|cookie)',str(key))
                           else _redacted(item, depth + 1)) for key,item in value.items()}
    if isinstance(value, list):
        return [_redacted(item, depth + 1) for item in value[:30]]
    if isinstance(value, str):
        return re.sub(r'(?i)\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}', '[redacted]', value[:4000])
    return value if value is None or isinstance(value, (int,float,bool)) else str(type(value).__name__)


def _response_types(response: Any) -> set[str]:
    if isinstance(response, str):
        return {'text'} if response.strip() else set()
    if isinstance(response, list):
        return set().union(*(_response_types(item) for item in response)) if response else set()
    if isinstance(response, dict):
        if response.get('isError') is True:
            return set()
        content = response.get('content', response)
        if content is not response:
            return _response_types(content)
        if response.get('type') == 'image' or response.get('mimeType','').startswith('image/'):
            return {'image'}
        if isinstance(response.get('text'),str) and response['text'].strip():
            return {'text'}
        return {'structured'} if set(response) - {'isError'} else set()
    return set()


def observe_host_tool(state: dict, event: dict) -> dict | None:
    """Keep a bounded, redacted host PostToolUse browser/MCP observation."""
    name = event.get('tool_name')
    tool_id = event.get('tool_use_id')
    response = event.get('tool_response', event.get('tool_output'))
    if (state.get('status') not in {'working','reviewing','revising'} or
            not isinstance(name,str) or not name.startswith('mcp__') or
            not isinstance(tool_id,str) or not tool_id or response is None or
            'tool_input' not in event):
        return None
    try:
        encoded = json.dumps({'input':event.get('tool_input'), 'response':response},
                             ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError,ValueError):
        return None
    if len(encoded) > 1_000_000:
        return None
    types = _response_types(response)
    if not types:
        return None
    kind = 'browser' if 'browser' in name.lower() or 'cua_repl' in name.lower() else 'mcp'
    request_excerpt=json.dumps(_redacted(event['tool_input']),ensure_ascii=False,sort_keys=True,default=str)[:4000]
    response_excerpt=json.dumps(_redacted(response),ensure_ascii=False,sort_keys=True,default=str)[:4000]
    row = {'id': digest([state.get('run_id'),state.get('intent_version'),tool_id,name]),
           'run_id':state.get('run_id'),'contract_revision':state.get('intent_version'),
           'tool_use_id':tool_id,'tool_name':name,'kind':kind,
           'request_sha256':digest(event['tool_input']),
           'request_excerpt':request_excerpt,'response_excerpt':response_excerpt,
           'response_types':sorted(types),
           'input_result_sha256':hashlib.sha256(encoded.encode('utf-8')).hexdigest(),
           'observed_at':datetime.now(timezone.utc).isoformat(),
           'assurance':'host_PostToolUse_bounded_redacted'}
    # Preserve supported textual/structured results separately from native schema 2.
    # Missing and sensitive outputs stay explicit; hashes are never read authority.
    row['full_output'] = _store_observed_output(state, row['id'], response,tool_name=name)
    existing=state.setdefault('tool_observations',[])
    if not any(value['id']==row['id'] for value in existing):
        state['tool_observations']=(existing+[row])[-100:]
    return row


def _store_observed_output(state: dict, observation_id: str, response: Any, *, tool_name='') -> dict:
    from .memory import SECRET
    workspace=Path(state.get('workspace',''))
    if not state.get('workspace') or not workspace.is_dir():
        return {'status':'uncollected','reason':'workspace_unavailable'}
    try:
        policy_path=_path(workspace,'.adhd/tool-output-policy.json')
        if policy_path.stat().st_size>8000:raise ValueError('Policy exceeds budget')
        policy=json.loads(policy_path.read_text(encoding='utf-8-sig'))
        if (policy.get('schema_version')!=1 or policy.get('enabled') is not True
                or tool_name not in policy.get('allowed_tools',[]) or not policy.get('policy_revision')):
            return {'status':'uncollected','reason':'capture_disabled'}
        policy_hash=file_hash(policy_path)
    except (OSError,ValueError,TypeError):
        return {'status':'uncollected','reason':'capture_disabled'}
    def sensitive(value, depth=0):
        if depth > 16:
            return True
        if isinstance(value, dict):
            if value.get('type') in {'image','audio'} or 'blob' in value:
                return True
            return any(re.search(r'(?i)(?:authorization|cookie|password|secret|api[_-]?key|access_token|private_key|email|address|phone)', str(k))
                       or sensitive(v,depth+1) for k,v in value.items())
        if isinstance(value,list):
            return any(sensitive(v,depth+1) for v in value)
        return isinstance(value,str) and bool(SECRET.search(value))
    if sensitive(response):
        return {'status':'uncollected','reason':'sensitive_or_binary_result'}
    raw=json.dumps(response,ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')
    if len(raw)>1_000_000:
        return {'status':'uncollected','reason':'result_exceeds_capture_budget'}
    relative='.adhd/tool-results/'+observation_id+'.json'
    try:
        path=_path(workspace,relative,existing=False)
        if file_hash(policy_path)!=policy_hash:
            return {'status':'uncollected','reason':'capture_policy_changed'}
        atomic_json(path,response)
        return {'status':'stored','path':relative,'sha256':file_hash(path),'bytes':path.stat().st_size,
                'policy_revision':policy['policy_revision'],'policy_sha256':policy_hash,
                'reported_truncated':response.get('truncated') if isinstance(response,dict) else None,
                'exit_code':response.get('exit_code') if isinstance(response,dict) else None,
                'authority':'source_data_revalidate_before_use'}
    except (OSError,ValueError):
        return {'status':'uncollected','reason':'local_capture_unavailable'}


def validate_tool_observations(state: dict, refs: list[str], kind: str,
                               expected: dict | None = None) -> list[dict]:
    if not isinstance(refs,list) or not 1<=len(refs)<=12 or len(set(refs))!=len(refs):
        raise ValueError('Browser/MCP criterion needs observed host tool IDs')
    observations={row['id']:row for row in state.get('tool_observations',[])}
    results=[]
    for ref in refs:
        row=observations.get(ref)
        if (not row or row['run_id']!=state['run_id'] or
                row['contract_revision']!=state['intent_version'] or
                row['kind']!=kind or row['assurance']!='host_PostToolUse_bounded_redacted'):
            raise ValueError('Unobserved or stale browser/MCP tool result')
        if (not expected or row['tool_name'] != expected['name'] or
                row['request_sha256'] != expected['request_sha256'] or
                expected['result_type'] not in row['response_types'] or
                (expected.get('result_contains') and expected['result_contains'] not in row['response_excerpt'])):
            raise ValueError('Browser/MCP observation differs from its criterion contract')
        results.append(row)
    return results
