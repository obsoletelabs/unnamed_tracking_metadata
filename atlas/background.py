"""Background task metrics (#75): one outcome and duration per run, and how late each loop woke up.

These wrap the existing loops in `atlas.main`; they never change what a loop does or how it handles a failure.
Counters are per process (every worker runs its own loops); see docs/guide/observability.md for aggregation.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from . import instruments


@contextmanager
def operation(name: str) -> Iterator[None]:
    """Count one run of a background operation as `ok` or `error` (the exception still propagates) and time it."""
    started, outcome = time.monotonic(), "error"
    try:
        yield
        outcome = "ok"
    finally:
        instruments.background_ops.inc(operation=name, outcome=outcome)
        instruments.background_duration.observe(time.monotonic() - started, operation=name)


def skipped(name: str) -> None:
    instruments.background_ops.inc(operation=name, outcome="skipped")


def wait(stop: threading.Event, loop: str, seconds: float) -> bool:
    """`stop.wait(seconds)` that records how much later than planned the loop woke up. Returns True when stopping."""
    planned = time.monotonic() + max(0.0, seconds)
    if stop.wait(seconds):
        return True
    instruments.background_delay.observe(max(0.0, time.monotonic() - planned), loop=loop)
    return False
