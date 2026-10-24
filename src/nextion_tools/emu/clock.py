"""A clock a test moves by hand, for timers that must fire exactly when the test says.

    clock = ManualClock()
    display = Display(..., hmi=hmi, clock=clock)
    clock.advance(0.05)
    display.tick()          # runs whatever is now due
"""

import threading


class ManualClock:
    def __init__(self, start: float = 1000.0):
        self._now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds
