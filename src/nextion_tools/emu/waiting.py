"""Condition waits that also give up as soon as something else (e.g. the emulated MCU) dies."""

import threading
import time
from typing import Callable, Optional

# How often a wait runs abort_check.
POLL = 0.5


def wait_for(cond: threading.Condition, predicate: Callable[[], bool], timeout: float,
             abort_check: Optional[Callable[[], None]]) -> bool:
    """Like Condition.wait_for (call with `cond` held), but runs abort_check() every POLL seconds.

    abort_check raises to end the wait early, e.g. when the MCU under test has crashed.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if abort_check is not None:
            abort_check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        cond.wait(min(remaining, POLL))
    return True
