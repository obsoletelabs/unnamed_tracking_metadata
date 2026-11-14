"""Fixed-window rate limiter backed by the database (safe across workers/replicas).

A counter row is `(key, window, count)`. `window` is the Unix second at which the row's window **ends** (its
expiry; a multiple of the window size), so housekeeping can purge expired rows without knowing each key's window
size (CONTRACT-C14 §3.10, T-RL1). Before C14-0 `window` was the window's index (`now // size`) and `purge`
assumed 60 s windows, which deleted every counter with a window over about five minutes almost at once, so
hourly and daily limits never held. No migration is needed: rows in the old form hold small numbers, read as
long expired, and are purged on the first housekeeping cycle (the old counters restart once). During a rolling
upgrade, while old and new workers run side by side, each new-worker purge also deletes the old workers' live
counters, so the old workers' limits are not enforced until the rollout finishes (one restart cycle).

A stored count never exceeds `limit + 1`: a hit that would take the total past `limit` stores `limit + 1`
instead. The answer is the same (refused until the window ends), but a byte budget fed refused amounts for an
hour cannot overflow the `Integer` column (int32 on PostgreSQL) and turn a 429 into a 500.
"""

from __future__ import annotations

import time

from sqlalchemy import case, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import RateCounter

_COUNT_MAX = 2**31 - 1  # rate_counter.count is Integer (int32 on PostgreSQL)


def hit(maker: sessionmaker[Session], key: str, limit: int, window_seconds: int = 60,
        clock=time.time, amount: int = 1) -> tuple[bool, int]:
    """Count `amount` (default one request; e.g. bytes for a byte budget) against `limit` per window.

    Returns (allowed, retry_after_seconds). Allowed while the window's total, this amount included, is at most
    `limit`; the amount is counted even when refused, like a refused request (the stored total is capped at
    `limit + 1`, see the module notes)."""
    if type(amount) is not int or amount < 1:
        raise ValueError("amount must be a positive integer")
    if type(window_seconds) is not int or window_seconds < 1:
        raise ValueError("window_seconds must be a positive integer")
    cap = min(int(limit), _COUNT_MAX - 1)  # a limit past int32 behaves as the largest storable one
    over = cap + 1  # stored instead of any total past the limit
    t = clock()
    expires = (int(t // window_seconds) + 1) * window_seconds
    retry = max(1, expires - int(t))
    with maker() as s:
        for _ in range(3):
            res = s.execute(
                update(RateCounter).where(RateCounter.key == key, RateCounter.window == expires)
                .values(count=case((RateCounter.count > cap - amount, over), else_=RateCounter.count + amount))
            )
            if res.rowcount:
                break
            try:
                s.add(RateCounter(key=key, window=expires, count=min(amount, over)))
                s.flush()
                break
            except IntegrityError:
                s.rollback()
        row = s.get(RateCounter, (key, expires), populate_existing=True)
        count = row.count if row else min(amount, over)
        s.commit()
    from .instruments import auth_rate_limit, rate_limit_operation

    auth_rate_limit.inc(operation=rate_limit_operation(key), decision="allowed" if count <= cap else "limited")
    return count <= cap, retry


def purge(maker: sessionmaker[Session], clock=time.time) -> int:
    """Delete counters whose window has ended. Returns how many rows went."""
    with maker() as s:
        gone = s.execute(delete(RateCounter).where(RateCounter.window <= int(clock()))).rowcount
        s.commit()
    return gone or 0
