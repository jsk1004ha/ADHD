"""Synchronous limits for one maintenance operation, separate from model slots.

Bytes count content processed by the caller, not physical operating-system I/O.
CPU is calling-thread time, checked at explicit work boundaries, not an OS quota.
"""
from __future__ import annotations

import math
import time
from typing import Callable


class BudgetExhausted(RuntimeError):
    """A maintenance operation stopped before publishing partial work."""


class MaintenanceBudget:
    def __init__(self, *, cpu_seconds: float = 10.0,
                 read_bytes: int = 512 * 1024 * 1024,
                 write_bytes: int = 512 * 1024 * 1024,
                 clock: Callable[[], float] = time.thread_time):
        if (not isinstance(cpu_seconds, (int, float)) or isinstance(cpu_seconds, bool)
                or not math.isfinite(cpu_seconds) or cpu_seconds <= 0
                or type(read_bytes) is not int or read_bytes < 0
                or type(write_bytes) is not int or write_bytes < 0):
            raise ValueError("invalid-maintenance-budget")
        self.cpu_seconds = cpu_seconds
        self.read_limit = read_bytes
        self.write_limit = write_bytes
        self.read_bytes = 0
        self.write_bytes = 0
        self._clock = clock
        self._started = clock()

    def consume(self, *, read: int = 0, write: int = 0) -> None:
        if type(read) is not int or type(write) is not int or read < 0 or write < 0:
            raise ValueError("invalid-maintenance-byte-count")
        self.read_bytes += read
        self.write_bytes += write
        if self.read_bytes > self.read_limit:
            raise BudgetExhausted("maintenance-read-budget-exhausted")
        if self.write_bytes > self.write_limit:
            raise BudgetExhausted("maintenance-write-budget-exhausted")
        self.check()

    def ensure(self, *, read: int = 0, write: int = 0) -> None:
        """Reject a known upcoming bounded transfer before starting it."""
        if type(read) is not int or type(write) is not int or read < 0 or write < 0:
            raise ValueError("invalid-maintenance-byte-count")
        if self.read_bytes + read > self.read_limit:
            raise BudgetExhausted("maintenance-read-budget-exhausted")
        if self.write_bytes + write > self.write_limit:
            raise BudgetExhausted("maintenance-write-budget-exhausted")
        self.check()

    def check(self) -> None:
        if self._clock() - self._started > self.cpu_seconds:
            raise BudgetExhausted("maintenance-cpu-budget-exhausted")
