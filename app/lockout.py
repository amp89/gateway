"""Failure counter with exponential lockout (SPEC 5.4): 5 failures -> 30 s, doubling, capped.

`gate` lets one attempt run at a time: each attempt costs a ~128 MB scrypt, and checking the lockout, hashing
and counting the result must not interleave (parallel requests would otherwise all pass the check).
"""
import threading
from contextlib import contextmanager

from .clock import Clock

# A short cap: the counter is shared by every client, so a long one would let anyone lock the admin out.
CAP_S = 300.0


class Busy(Exception):
    """Another attempt on the same counter is running."""


class Lockout:
    def __init__(self, clock: Clock, free: int = 5, base: float = 30.0, cap: float = CAP_S) -> None:
        self.clock, self.free, self.base, self.cap = clock, free, base, cap
        self.failures = 0
        self.locked_until = 0.0
        self.gate = threading.Lock()

    @contextmanager
    def attempt(self):
        """Hold the gate for one check-hash-count sequence; raises Busy instead of queueing."""
        if not self.gate.acquire(blocking=False):
            raise Busy()
        try:
            yield self
        finally:
            self.gate.release()

    def retry_after(self) -> int:
        """Seconds until the next attempt is allowed; 0 = allowed now."""
        left = self.locked_until - self.clock.monotonic()
        return max(0, int(left + 0.999))

    def fail(self) -> None:
        self.failures += 1
        over = self.failures - self.free
        if over >= 0:
            self.locked_until = self.clock.monotonic() + min(self.cap, self.base * (2**over))

    def success(self) -> None:
        self.failures = 0
        self.locked_until = 0.0
