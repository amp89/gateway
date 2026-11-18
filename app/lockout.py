"""Failure counter with exponential lockout (SPEC 5.4): 5 failures -> 30 s, doubling, capped at 1 h."""
from .clock import Clock


class Lockout:
    def __init__(self, clock: Clock, free: int = 5, base: float = 30.0, cap: float = 3600.0) -> None:
        self.clock, self.free, self.base, self.cap = clock, free, base, cap
        self.failures = 0
        self.locked_until = 0.0

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
