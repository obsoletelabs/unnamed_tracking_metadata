"""Fixed-window rate limiter backed by the database (safe across workers/replicas)."""

from __future__ import annotations

import time

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import RateCounter


def hit(maker: sessionmaker[Session], key: str, limit: int, window_seconds: int = 60,
        clock=time.time) -> tuple[bool, int]:
    """Count one request. Returns (allowed, retry_after_seconds)."""
    window = int(clock() // window_seconds)
    retry = window_seconds - int(clock() % window_seconds)
    with maker() as s:
        for _ in range(3):
            res = s.execute(
                update(RateCounter).where(RateCounter.key == key, RateCounter.window == window)
                .values(count=RateCounter.count + 1)
            )
            if res.rowcount:
                break
            try:
                s.add(RateCounter(key=key, window=window, count=1))
                s.flush()
                break
            except IntegrityError:
                s.rollback()
        row = s.get(RateCounter, (key, window))
        count = row.count if row else 1
        s.commit()
    return count <= limit, retry


def purge(maker: sessionmaker[Session], older_than_windows: int = 5, window_seconds: int = 60,
          clock=time.time) -> None:
    cutoff = int(clock() // window_seconds) - older_than_windows
    with maker() as s:
        s.execute(delete(RateCounter).where(RateCounter.window < cutoff))
        s.commit()
