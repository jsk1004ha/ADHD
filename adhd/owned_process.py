"""Own a command and its descendants until they have stopped.

Windows uses a Job Object; Linux uses a new process group. This is lifecycle
ownership, not a sandbox: a Linux child that deliberately leaves its group is
outside this helper's ownership.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                    ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimit),
                    ('IoInfo', ctypes.c_uint64 * 6),
                    ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _Accounting(ctypes.Structure):
        _fields_ = [('TotalUserTime', ctypes.c_int64),
                    ('TotalKernelTime', ctypes.c_int64),
                    ('ThisPeriodTotalUserTime', ctypes.c_int64),
                    ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                    ('TotalPageFaultCount', wintypes.DWORD),
                    ('TotalProcesses', wintypes.DWORD),
                    ('ActiveProcesses', wintypes.DWORD),
                    ('TotalTerminatedProcesses', wintypes.DWORD)]

    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel.OpenJobObjectW.restype = wintypes.HANDLE
    _kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD]
    _kernel.SetInformationJobObject.restype = wintypes.BOOL
    _kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD,
                                                   ctypes.c_void_p]
    _kernel.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel.TerminateJobObject.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL

    def _win_check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _active(job):
        info = _Accounting()
        _win_check(_kernel.QueryInformationJobObject(job, 1, ctypes.byref(info),
                                                      ctypes.sizeof(info), None))
        return info.ActiveProcesses


def _linux_group_live(pgid: int) -> bool:
    # Keep the root as a zombie until this scan is complete, pinning its PID/PGID.
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(') ', 1)[1].split()
        except FileNotFoundError:
            continue
        if int(fields[2]) == pgid and fields[0] not in {'Z', 'X', 'x'}:
            return True
    return False


class OwnedProcess:
    """Popen-like owner; call ``close`` after ``wait`` or on cancellation."""

    def __init__(self, argv: list[str], *, cwd: Path, env: dict | None = None,
                 stdin=None, stdout=None, stderr=None):
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError('argv must be a nonempty list of strings')
        cwd = Path(cwd).resolve(strict=True)
        self._closed = False
        self._completed_code = None
        self._stopping_at = None
        self._job = None
        self._temp = None
        if os.name == 'nt':
            self._start_windows(argv, cwd, env, stdin, stdout, stderr)
        elif sys.platform.startswith('linux'):
            self._process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin,
                                             stdout=stdout, stderr=stderr,
                                             start_new_session=True)
            self.pid = self._process.pid
        else:
            raise NotImplementedError('OwnedProcess supports Windows and Linux')
        self.stdin, self.stdout, self.stderr = (self._process.stdin, self._process.stdout,
                                                self._process.stderr)

    def _start_windows(self, argv, cwd, env, stdin, stdout, stderr):
        self._temp = tempfile.TemporaryDirectory(prefix='adhd-owned-')
        folder = Path(self._temp.name)
        name = 'Local\\adhd-owned-' + uuid.uuid4().hex
        spec = folder / 'launch.json'
        ready = folder / 'ready'
        try:
            ctypes.set_last_error(0)
            self._job = _kernel.CreateJobObjectW(None, name)
            _win_check(self._job)
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                raise RuntimeError('Owned Job Object name collision')
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            _win_check(_kernel.SetInformationJobObject(self._job, 9, ctypes.byref(limits),
                                                        ctypes.sizeof(limits)))
            payload = json.dumps({'argv': argv, 'cwd': str(cwd), 'env': env,
                                  'job': name, 'ready': str(ready)}).encode('utf-8')
            if len(payload) > 1_048_576:
                raise ValueError('Owned process launch specification is too large')
            spec.write_bytes(payload)
            self._process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                              '--owned-process-launcher', str(spec)],
                                             cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr)
            until = time.monotonic() + 10
            while not ready.exists():
                if self._process.poll() is not None or time.monotonic() >= until:
                    raise RuntimeError('Owned command could not join its Job Object or start')
                time.sleep(.01)
            marker = ready.read_text()
            if not marker.isdecimal():
                raise RuntimeError('Owned command could not join its Job Object or start')
            self.pid = int(marker)
        except BaseException:
            if self._job:
                _kernel.TerminateJobObject(self._job, 1)
                _kernel.CloseHandle(self._job)
                self._job = None
            if hasattr(self, '_process'):
                self._process.kill()
                self._process.wait()
                for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                    if stream is not None:
                        stream.close()
            self._temp.cleanup()
            raise
        finally:
            spec.unlink(missing_ok=True)
            ready.unlink(missing_ok=True)

    def _root_finished(self):
        if os.name == 'nt':
            return self._process.poll() is not None
        return os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None

    def _signal_group(self, sig):
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass

    def poll(self):
        if self._completed_code is not None:
            return self._completed_code
        if not self._root_finished():
            if self._stopping_at is not None and time.monotonic() - self._stopping_at >= .5:
                if os.name == 'nt':
                    _win_check(_kernel.TerminateJobObject(self._job, 1))
                else:
                    self._signal_group(signal.SIGKILL)
            return None
        if os.name == 'nt':
            if _active(self._job):
                _win_check(_kernel.TerminateJobObject(self._job, 1))
                return None
        else:
            if self._stopping_at is None:
                self._stopping_at = time.monotonic()
                self._signal_group(signal.SIGTERM)
            if _linux_group_live(self.pid):
                if time.monotonic() - self._stopping_at >= .5:
                    self._signal_group(signal.SIGKILL)
                return None
        self._completed_code = self._process.wait()
        return self._completed_code

    @property
    def returncode(self):
        return self.poll()

    def wait(self, timeout=None):
        until = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self.poll()
            if result is not None:
                return result
            if until is not None and time.monotonic() >= until:
                raise subprocess.TimeoutExpired([self.pid], timeout)
            time.sleep(.02)

    def terminate(self):
        if self._completed_code is not None:
            return
        self._stopping_at = time.monotonic()
        if os.name == 'nt':
            _win_check(_kernel.TerminateJobObject(self._job, 1))
        else:
            self._signal_group(signal.SIGTERM)

    def kill(self):
        if self._completed_code is not None:
            return
        self._stopping_at = time.monotonic()
        if os.name == 'nt':
            _win_check(_kernel.TerminateJobObject(self._job, 1))
        else:
            self._signal_group(signal.SIGKILL)

    def close(self):
        if self._closed:
            return
        try:
            running = self.poll() is None
        except OSError:
            self.kill()
            raise  # Keep ownership if termination cannot be confirmed.
        if running:
            self.kill()
            self.wait(timeout=10)
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                stream.close()
        if self._job:
            _win_check(_kernel.CloseHandle(self._job))
            self._job = None
        if self._temp:
            self._temp.cleanup()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _launch_windows(spec_path: Path) -> int:
    """Standalone entry point: assign this launcher before creating the command."""
    spec = json.loads(spec_path.read_bytes())
    job = _kernel.OpenJobObjectW(0x0001, False, spec['job'])  # ASSIGN_PROCESS
    if not job:
        return 125
    try:
        if not _kernel.AssignProcessToJobObject(job, _kernel.GetCurrentProcess()):
            return 125
    finally:
        _kernel.CloseHandle(job)
    try:
        command = subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=spec['env'],
                                   stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        ready = Path(spec['ready'])
        marker = ready.with_name('ready.tmp')
        marker.write_text(str(command.pid))
        os.replace(marker, ready)
        return command.wait()
    except (OSError, ValueError):
        return 125


if __name__ == '__main__' and os.name == 'nt':
    if len(sys.argv) == 3 and sys.argv[1] == '--owned-process-launcher':
        sys.exit(_launch_windows(Path(sys.argv[2])))
