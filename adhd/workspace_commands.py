"""Run explicitly selected verification commands in invocation-owned scratch."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from .core import atomic_json
from .runner import terminate_tree


def _usage(root: Path, limit: int) -> int:
    total = count = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            path = Path(folder) / name
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('Producer created a link in scratch')
        for name in files:
            try:
                total += (Path(folder) / name).stat().st_size
            except FileNotFoundError:
                continue
            count += 1
            if count > 20000 or total > limit:
                return limit + 1
    return total


def run_in_scratch(manager, request: dict, *, _on_process=None) -> dict:
    allowed = {'argv', 'reserved_bytes', 'minimum_free', 'timeout', 'owner', 'retain_logs'}
    if set(request) - allowed:
        raise ValueError('Unknown scratch command field')
    argv = request.get('argv')
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or any(not isinstance(x, str) or '\x00' in x for x in argv)
            or len(json.dumps(argv).encode()) > 32768):
        raise ValueError('A bounded explicit argv array is required')
    budget = request.get('reserved_bytes', 512 * 1024**2)
    if type(budget) is not int or budget <= 0:
        raise ValueError('A positive scratch byte budget is required')
    if type(request.get('retain_logs',False)) is not bool:
        raise ValueError('retain_logs must be a boolean')
    timeout = request.get('timeout', 900)
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError('Invalid scratch command timeout')
    output = manager.workspace / '.adhd' / 'results' / uuid.uuid4().hex
    output.mkdir(parents=True)
    result = {'status': 'running', 'exit_code': None, 'peak_observed_bytes': 0,
              'results': str(output.relative_to(manager.workspace)),
              'stdout': str((output/'stdout.log').relative_to(manager.workspace)),
              'stderr': str((output/'stderr.log').relative_to(manager.workspace)),
              'enforcement': 'observed external writes; not an OS hard quota'}
    log_overflow = threading.Event()
    capture_errors = []
    def capture(pipe, destination):
        remaining = 10 * 1024**2
        try:
            with destination.open('wb') as target:
                while chunk := pipe.read(65536):
                    if len(chunk) > remaining:
                        log_overflow.set()
                    target.write(chunk[:remaining])
                    remaining = max(0, remaining - len(chunk))
        except Exception as error:
            capture_errors.append(type(error).__name__)
        finally:
            pipe.close()
    try:
        manager.sweep()
        # Log payload is retained outside scratch, but still consumes the same volume.
        with manager.scratch(request.get('owner', 'verification'), reserved_bytes=budget+20*1024**2,
                             minimum_free=request.get('minimum_free', 2*1024**3)) as scratch:
            result['scratch_id'] = scratch.name
            env = {**os.environ, 'ADHD_SCRATCH': str(scratch), 'ADHD_RESULTS': str(output)}
            from .owned_process import OwnedProcess
            process = OwnedProcess(argv, cwd=manager.workspace, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
            threads = []
            try:
                manager.register_child(scratch, process.pid)
                if _on_process is not None:
                    _on_process(process)
                for pipe, name in ((process.stdout, 'stdout.log'), (process.stderr, 'stderr.log')):
                    thread = threading.Thread(target=capture, args=(pipe, output/name), daemon=True)
                    thread.start()
                    threads.append(thread)
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    used = _usage(scratch, budget)
                    result['peak_observed_bytes'] = max(result['peak_observed_bytes'], used)
                    reason = ('scratch_budget_exceeded' if used > budget else 'log_budget_exceeded'
                              if log_overflow.is_set() else 'capture_failed' if capture_errors else
                              'timeout' if time.monotonic() >= deadline else None)
                    if reason:
                        result['reason'] = reason
                        terminate_tree(process)
                        break
                    time.sleep(.1)
                result['exit_code'] = process.wait(timeout=15)
                used = _usage(scratch, budget)
                result['peak_observed_bytes'] = max(result['peak_observed_bytes'], used)
                if used > budget:
                    result['reason'] = 'scratch_budget_exceeded'
                result['status'] = 'complete' if result['exit_code'] == 0 and not result.get('reason') else 'failed'
            finally:
                if process.poll() is None:
                    terminate_tree(process)
                    process.wait(timeout=15)
                for thread in threads:
                    thread.join(timeout=5)
                if any(thread.is_alive() for thread in threads):
                    manager.add_reference(scratch, 'unconfirmed-descendant-pipes')
                    result.update(status='cleanup_pending', reason='unconfirmed_descendant_exit')
                process.close()
                if log_overflow.is_set() or capture_errors:
                    result.update(status='failed', reason='capture_failed' if capture_errors else 'log_budget_exceeded')
        if (manager.scratch_root / result['scratch_id']).exists():
            result['status'] = 'cleanup_pending'
    except BaseException as error:
        result.update(status='failed', error=type(error).__name__)
        raise
    finally:
        if result['status'] in {'complete','failed'} and not request.get('retain_logs',False):
            try:
                result['log_retention']=manager.retain_latest_logs(output,request.get('owner','verification'))
            except (OSError,ValueError) as error:
                result['log_retention']={'cleanup_pending':type(error).__name__}
        atomic_json(output / 'report.json', result)
    return result
