"""Fixed-window rate limiter backed by the database (safe across workers/replicas).

A counter row is `(key, window, count)`. `window` is the Unix second at which the row's window **ends** (its
expiry; a multiple of the window size), so housekeeping can purge expired rows without knowing each key's window
size (CONTRACT-C14 §3.10, T-RL1). Before C14-0 `window` was the window's index (`now // size`) and `purge`
assumed 60 s windows, which deleted every counter with a window over about five minutes almost at once, so
hourly and daily limits never held. No migration is needed: rows in the old form hold small numbers, read as
long expired, and are purged on the first housekeeping cycle (the old counters restart once).
"""

from __future__ import annotations

import time

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import RateCounter


def hit(maker: sessionmaker[Session], key: str, limit: int, window_seconds: int = 60,
        clock=time.time, amount: int = 1) -> tuple[bool, int]:
    """Count `amount` (default one request; e.g. bytes for a byte budget) against `limit` per window.

    Returns (allowed, retry_after_seconds). Allowed while the window's total, this amount included, is at most
    `limit`; the amount is counted even when refused, like a refused request."""
    if type(amount) is not int or amount < 1:
        raise ValueError("amount must be a positive integer")
    if type(window_seconds) is not int or window_seconds < 1:
        raise ValueError("window_seconds must be a positive integer")
    t = clock()
    expires = (int(t // window_seconds) + 1) * window_seconds
    retry = max(1, expires - int(t))
    with maker() as s:
        for _ in range(3):
            res = s.execute(
                update(RateCounter).where(RateCounter.key == key, RateCounter.window == expires)
                .values(count=RateCounter.count + amount)
            )
            if res.rowcount:
                break
            try:
                s.add(RateCounter(key=key, window=expires, count=amount))
                s.flush()
                break
            except IntegrityError:
                s.rollback()
        row = s.get(RateCounter, (key, expires), populate_existing=True)
        count = row.count if row else amount
        s.commit()
    from .instruments import auth_rate_limit, rate_limit_operation

    auth_rate_limit.inc(operation=rate_limit_operation(key), decision="allowed" if count <= limit else "limited")
    return count <= limit, retry


def purge(maker: sessionmaker[Session], clock=time.time) -> int:
    """Delete counters whose window has ended. Returns how many rows went."""
    with maker() as s:
        gone = s.execute(delete(RateCounter).where(RateCounter.window <= int(clock()))).rowcount
        s.commit()
    return gone or 0
