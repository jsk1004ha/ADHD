"""Opt-in sampling of an ADHD-managed process tree; no resident monitor.

Sampled peaks are lower bounds between samples. Missing processes/OS counters
remain explicit and must not be used as a complete resource comparison.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import threading
import time


def _linux_processes():
    result = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            result[int(path.name)] = (int(fields[1]), int(fields[19]))
        except (OSError, ValueError, IndexError):
            continue
    return result


def _linux_sample(pid):
    path = Path('/proc') / str(pid)
    fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines() if ':' in line)
    private = None
    try:
        rollup = dict(line.split(':', 1) for line in (path / 'smaps_rollup').read_text().splitlines() if ':' in line)
        private = sum(int(rollup.get(key, '0 kB').split()[0]) * 1024 for key in ('Private_Clean', 'Private_Dirty', 'Private_Hugetlb'))
    except (OSError, ValueError):
        pass
    io = {}
    try:
        io = dict(line.split(':', 1) for line in (path / 'io').read_text().splitlines())
    except OSError:
        pass
    return {'identity': int(fields[19]), 'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
            'working_set_bytes': int(status.get('VmRSS', '0 kB').split()[0]) * 1024,
            'peak_working_set_bytes': int(status.get('VmHWM', '0 kB').split()[0]) * 1024,
            'private_bytes': private,
            'read_bytes': int(io['read_bytes']) if 'read_bytes' in io else None,
            'write_bytes': int(io['write_bytes']) if 'write_bytes' in io else None}


class _Windows:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.psapi = c.WinDLL('psapi', use_last_error=True)
        class Entry(c.Structure):
            _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                        ('th32DefaultHeapID', c.c_size_t), ('th32ModuleID', w.DWORD),
                        ('cntThreads', w.DWORD), ('th32ParentProcessID', w.DWORD),
                        ('pcPriClassBase', w.LONG), ('dwFlags', w.DWORD), ('szExeFile', w.WCHAR * 260)]
        class Memory(c.Structure):
            _fields_ = [('cb', w.DWORD), ('PageFaultCount', w.DWORD)] + [
                (key, c.c_size_t) for key in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage',
                'PeakPagefileUsage', 'PrivateUsage')]
        class IO(c.Structure):
            _fields_ = [(key, c.c_ulonglong) for key in ('ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        self.Entry, self.Memory, self.IO = Entry, Memory, IO
        self.kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        for name in ('Process32FirstW', 'Process32NextW'):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = [w.HANDLE, c.POINTER(Entry)], w.BOOL
        self.kernel.OpenProcess.argtypes, self.kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        self.kernel.CloseHandle.argtypes, self.kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = w.BOOL
        self.kernel.GetProcessIoCounters.argtypes, self.kernel.GetProcessIoCounters.restype = [w.HANDLE, c.POINTER(IO)], w.BOOL
        self.psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.POINTER(Memory), w.DWORD]
        self.psapi.GetProcessMemoryInfo.restype = w.BOOL

    def processes(self):
        c, k = self.c, self.kernel
        handle = k.CreateToolhelp32Snapshot(2, 0)
        if handle == c.c_void_p(-1).value:
            raise OSError('process-snapshot-unavailable')
        result, entry = {}, self.Entry()
        entry.dwSize = c.sizeof(entry)
        try:
            ok = k.Process32FirstW(handle, c.byref(entry))
            while ok:
                result[entry.th32ProcessID] = (entry.th32ParentProcessID, None)
                ok = k.Process32NextW(handle, c.byref(entry))
        finally:
            k.CloseHandle(handle)
        return result

    def sample(self, pid):
        c, w, k = self.c, self.w, self.kernel
        handle = k.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            raise OSError('process-counter-unavailable')
        try:
            times = [w.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(handle, *(c.byref(value) for value in times)):
                raise OSError('process-times-unavailable')
            number = lambda value: (value.dwHighDateTime << 32) | value.dwLowDateTime
            memory, io = self.Memory(), self.IO()
            memory.cb = c.sizeof(memory)
            mem_ok = self.psapi.GetProcessMemoryInfo(handle, c.byref(memory), c.sizeof(memory))
            io_ok = k.GetProcessIoCounters(handle, c.byref(io))
            return dict(identity=number(times[0]), cpu_seconds=(number(times[2]) + number(times[3])) / 10000000,
                        working_set_bytes=memory.WorkingSetSize if mem_ok else None,
                        peak_working_set_bytes=memory.PeakWorkingSetSize if mem_ok else None,
                        private_bytes=memory.PrivateUsage if mem_ok else None,
                        read_bytes=io.ReadTransferCount if io_ok else None,
                        write_bytes=io.WriteTransferCount if io_ok else None)
        finally:
            k.CloseHandle(handle)


class ProcessTreeMonitor:
    def __init__(self, pid=None, *, interval=.2):
        if not .05 <= interval <= 10:
            raise ValueError('Sampling interval must be .05..10 seconds')
        self.pid, self.interval = pid or os.getpid(), interval
        self.stop_event = threading.Event()
        self.samples, self.peaks, self.first, self.last = 0, {}, {}, {}
        self.missing, self.pids = set(), {self.pid}
        self.errors = set()
        self.identities = {}
        self.thread = None
        self.backend = _Windows() if os.name == 'nt' else None
        self.started = None

    def sample(self):
        try:
            processes = self.backend.processes() if self.backend else _linux_processes()
            sampled={}
            def observe(pid):
                if pid in sampled:return sampled[pid]
                row=self.backend.sample(pid) if self.backend else _linux_sample(pid)
                identity=row['identity']
                if pid in self.identities and self.identities[pid]!=identity:
                    self.errors.add('process-identity-changed')
                    sampled[pid]=None
                    return None
                if pid not in self.identities and len(self.identities)>=4096:
                    self.errors.add('process-budget-exhausted')
                    sampled[pid]=None
                    return None
                self.identities[pid]=identity
                sampled[pid]=row
                return row
            descendants=set()
            try:
                if self.pid in processes and observe(self.pid) is not None:descendants.add(self.pid)
            except (OSError,ValueError,IndexError):self.missing.add(self.pid)
            frontier=set(descendants)
            while frontier:
                following=set()
                for pid,(parent,_) in processes.items():
                    if parent in frontier and pid not in descendants:
                        try:
                            if observe(pid) is not None:following.add(pid)
                        except (OSError,ValueError,IndexError):self.missing.add(pid)
                descendants.update(following)
                frontier=following
            active=descendants | (set(self.identities)&set(processes))
            totals = {key: 0 for key in ('working_set_bytes', 'private_bytes')}
            observed = 0
            for pid in active:
                try:
                    row = observe(pid)
                    if row is None:continue
                    identity = (pid, row['identity'])
                    self.first.setdefault(identity, row)
                    self.last[identity] = row
                    observed += 1
                    for key in totals:
                        totals[key] = None if row[key] is None or totals[key] is None else totals[key] + row[key]
                except (OSError, ValueError, IndexError):
                    self.missing.add(pid)
            self.pids.update(active)
            self.samples += 1
            if observed:
                for key, value in totals.items():
                    if value is not None:
                        self.peaks[key] = max(self.peaks.get(key, 0), value)
        except OSError:
            self.errors.add('process-sampling-unavailable')

    def __enter__(self):
        self.started = time.monotonic()
        self.sample()
        def run():
            while not self.stop_event.wait(self.interval):
                self.sample()
        self.thread = threading.Thread(target=run, name='adhd-resource-sampler', daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self._stop_sampler()
        self.sample()

    def _stop_sampler(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()

    def observe_post_task(self, *, window_seconds=.2):
        """Observe only identities already tracked by this monitor after task exit."""
        if type(window_seconds) not in (int, float) or not .05 <= window_seconds <= 5:
            raise ValueError('Post-task window must be .05..5 seconds')
        self._stop_sampler()
        tracked = dict(self.identities)

        def snapshot():
            rows, unavailable, reused = {}, set(), set()
            try:
                processes = self.backend.processes() if self.backend else _linux_processes()
            except OSError:
                return rows, set(tracked), reused
            for pid, birth in tracked.items():
                if pid not in processes:
                    continue
                try:
                    row = self.backend.sample(pid) if self.backend else _linux_sample(pid)
                except (OSError, ValueError, IndexError):
                    unavailable.add(pid)
                    continue
                if row['identity'] != birth:
                    reused.add(pid)
                else:
                    rows[(pid, birth)] = row
            return rows, unavailable, reused

        started = time.monotonic()
        first, first_unavailable, first_reused = snapshot()
        time.sleep(window_seconds)
        last, last_unavailable, last_reused = snapshot()
        elapsed = time.monotonic() - started
        unavailable = first_unavailable | last_unavailable
        reused = first_reused | last_reused
        uncertain = unavailable | reused
        remaining = sorted(identity for identity in last if identity[0] not in reused)
        deltas = []
        for identity in remaining:
            before = first.get(identity, {}).get('cpu_seconds')
            after = last[identity].get('cpu_seconds')
            if before is not None and after is not None and after >= before:
                deltas.append(after - before)
        complete_cpu = bool(tracked) and len(deltas) == len(remaining) and not uncertain
        cpu_delta = sum(deltas) if deltas or tracked and not remaining and not uncertain else None
        remaining_count = len(remaining) if remaining or tracked and not uncertain else None
        return {'remaining_identities': [{'pid': pid, 'birth': birth} for pid, birth in remaining[:32]],
                'remaining_identity_count': len(remaining),
                'remaining_identities_truncated': len(remaining) > 32,
                'root_pid': self.pid, 'observer_pid': os.getpid(),
                'observer_in_remaining': any(pid == os.getpid() for pid, _ in remaining),
                'remaining_count': remaining_count,
                'remaining_count_lower_bound': len(remaining),
                'remaining_coverage': 'partial' if uncertain or not tracked else 'complete_tracked',
                'remaining_cpu_delta_seconds': cpu_delta,
                'remaining_idle_cpu_observed': (cpu_delta == 0.0) if remaining and complete_cpu else None,
                'cpu_coverage': 'complete_tracked' if complete_cpu else 'partial',
                'unavailable_pids': sorted(unavailable)[:64],
                'unavailable_pid_count': len(unavailable),
                'reused_pids': sorted(reused)[:64], 'reused_pid_count': len(reused),
                'tracked_identity_count': len(tracked), 'window_elapsed_ms': elapsed * 1000,
                'sampler_stopped': self.thread is None or not self.thread.is_alive(),
                'scope': 'tracked PID and birth identities only; counts are confirmed lower bounds when partial; CPU delta is not whole-host idle CPU'}

    def report(self):
        changes = {}
        for key in ('cpu_seconds', 'read_bytes', 'write_bytes'):
            values = [max(0, row[key] - self.first[identity][key]) for identity, row in self.last.items()
                      if row[key] is not None and self.first[identity][key] is not None]
            changes[key] = sum(values) if values else None
        return {**changes, 'peak_private_bytes': self.peaks.get('private_bytes'),
                'peak_working_set_bytes': self.peaks.get('working_set_bytes'),
                'observed_processes': len(self.last), 'missing_pids': sorted(self.missing)[:64],
                'missing_pid_count':len(self.missing),
                'sample_count': self.samples, 'interval_ms': self.interval * 1000,
                'elapsed_ms': (time.monotonic() - self.started) * 1000 if self.started else None,
                'coverage': 'partial_sampled' if self.last else 'unknown',
                'errors': sorted(self.errors), 'sampler_stopped': self.thread is None or not self.thread.is_alive(),
                'note': 'Sampled tree peaks; processes completing between samples may be unobserved.'}


def disk_snapshot(paths):
    logical, allocated, files, missing = 0, 0, 0, 0
    volumes, unobserved_volumes = {}, 0
    for path in map(Path, paths):
        try:
            if path.is_symlink() or not path.is_file():
                missing += 1
                continue
            info = path.stat()
        except OSError:
            missing += 1
            continue
        logical += info.st_size
        files += 1
        identity = (os.path.normcase(path.absolute().anchor), info.st_dev)
        if identity not in volumes:
            if len(volumes) >= 64:
                unobserved_volumes += 1
            else:
                try:
                    free = shutil.disk_usage(path.absolute().parent).free
                except OSError:
                    free = None
                volumes[identity] = {'volume': identity[0], 'device': identity[1],
                                     'free_bytes': free}
        blocks = getattr(info, 'st_blocks', None)
        if blocks is None:
            allocated = None
        elif allocated is not None:
            allocated += blocks * 512
    return {'files': files, 'missing': missing, 'logical_bytes': logical,
            'allocated_bytes': allocated, 'reclaimed_bytes': None,
            'volume_free_bytes': next(iter(volumes.values()))['free_bytes']
                if len(volumes) == 1 and not missing and not unobserved_volumes else None,
            'volumes': list(volumes.values()), 'unobserved_volume_inputs': unobserved_volumes,
            'note': 'Logical/allocated bytes are not attributed physical disk recovery.'}
