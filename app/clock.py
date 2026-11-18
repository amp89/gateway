"""Injectable clock: real time in the app, a fake one in tests."""
import time


class Clock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock(Clock):
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds
