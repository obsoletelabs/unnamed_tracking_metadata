"""One provider budget per credential, for every caller on the node (CONTRACT-C14 §4.3 step 6, Review #1 C14-3).

The `Fetcher`'s token buckets are per process: the web process, the sync worker and the CLI each believed they owned
a provider's whole `requests_per_minute`, and two of the owner's nodes configured with the same API key each
believed they owned the whole quota. Every upstream request is now also counted in the database under
`prov-budget:<provider>:<cred_fp>` (a minute window from `requests_per_minute` and, when the adapter declares one,
a day window from `requests_per_day`); `cred_fp` is an HMAC of the credential, never the credential.

* Group lookups (`assist.lookup`, served for another member) take atomically, before the provider call, in their
  own committed transaction (`ratelimit.take`: every window or none, a refused take counts nothing), and only from
  the assist share: the credential's window total must stay at or below `(1 - reserve) × limit`.
* This node's own calls check the budget before the request (read only) and count it after, in the caller's
  transaction (a deviation recorded in CONTRACT-AMENDMENTS: an atomic take before the request would hold the
  SQLite write lock across a provider round trip). Concurrent own calls can overshoot by the calls in flight.
* When `n` in-force members report the same `cred_group_fp` for a provider (`assist.status`), every node's limits
  for it are divided by `n` (rounded down, at least 1 a minute), so the owner's nodes stay inside one quota.
"""

from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar
from typing import Any

from sqlalchemy.orm import Session

from . import ratelimit
from .models import NodeState

SHARE = "prov_share:"
DAY = 86400
#: Set while a group lookup runs: its take was already made atomically, so the Fetcher neither checks nor counts
#: the same request again.
TAKEN: ContextVar[bool] = ContextVar("atlas_provider_budget_taken", default=False)


def credential(settings: Any, provider: str) -> str:
    """The configured credential of a built-in provider ("" for a keyless one); never leaves this module."""
    if provider == "steam":
        return str(getattr(settings, "steam_api_key", "") or "")
    if provider == "rawg":
        return str(getattr(settings, "rawg_api_key", "") or "")
    if provider == "igdb":
        return f"{getattr(settings, 'igdb_client_id', '') or ''}:{getattr(settings, 'igdb_client_secret', '') or ''}"
    if provider == "tgdb":
        return str(getattr(settings, "tgdb_api_key", "") or "")
    return ""


def cred_fp(settings: Any, provider: str) -> str:
    """`HMAC-SHA256(node secret, provider || credential)[:16]`. A keyless provider is counted per node (its quota
    is the node's address), so its fingerprint is the node's own."""
    secret = str(getattr(settings, "secret_key", "") or "atlas-cred-fp-local").encode()
    return hmac.new(secret, f"{provider}\n{credential(settings, provider)}".encode(), hashlib.sha256).hexdigest()[:16]


def cred_group_fp(group_id: str, settings: Any, provider: str) -> str | None:
    """§4.3 step 6(b): comparable between members, sent only inside the sealed `assist.status`. None for a
    keyless provider (no shared credential to protect)."""
    cred = credential(settings, provider)
    if not cred.strip(":"):
        return None
    raw = f"atlas-cred-fp-v1\n{group_id}\n{provider}\n{cred}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def share(session: Session, provider: str) -> int:
    row = session.get(NodeState, SHARE + provider)
    return int(row.value) if row is not None and row.value.isdigit() and int(row.value) > 1 else 1


def set_share(session: Session, provider: str, n: int) -> None:
    row = session.get(NodeState, SHARE + provider)
    if n <= 1:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        session.add(NodeState(key=SHARE + provider, value=str(n)))
    else:
        row.value = str(n)


def limits(session: Session, settings: Any, p: Any) -> list[tuple[str, int, int]]:
    """(key, limit, window seconds) of the credential's budget, after the shared-credential split."""
    n = share(session, p.name)
    key = f"prov-budget:{p.name}:{cred_fp(settings, p.name)}"
    out = [(key, max(1, int(p.requests_per_minute) // n), 60)]
    per_day = getattr(p, "requests_per_day", None)
    if type(per_day) is int and per_day > 0:
        out.append((key, max(1, per_day // n), DAY))
    return out


def allows(session: Session, settings: Any, p: Any) -> bool:
    """Read-only check before one of this node's own requests."""
    if TAKEN.get():
        return True
    return all(ratelimit.used(session, key, size) < limit for key, limit, size in limits(session, settings, p))


def count(session: Session, settings: Any, p: Any) -> None:
    """Count one of this node's own upstream requests (after it was made, in the caller's transaction)."""
    if TAKEN.get():
        return
    ratelimit.take(session, [(key, 2**31 - 2, size) for key, _limit, size in limits(session, settings, p)])


def assist_windows(session: Session, settings: Any, p: Any, reserve_pct: int) -> list[tuple[str, int, int]]:
    """The budget plus the assist sub-share on the same keys, for one atomic take."""
    base = limits(session, settings, p)
    share_of = max(0, min(95, int(reserve_pct)))
    return base + [(key, limit * (100 - share_of) // 100, size) for key, limit, size in base]


def take_assist(maker: Any, settings: Any, p: Any, reserve_pct: int) -> tuple[bool, int]:
    """One group lookup's take, before the provider call, committed on its own."""
    with maker() as s:
        windows = assist_windows(s, settings, p, reserve_pct)
    if any(limit < 1 for _key, limit, _size in windows):
        return False, 60
    return ratelimit.take(maker, windows)


def headroom(session: Session, settings: Any, p: Any, reserve_pct: int) -> float:
    """§4.4: the assist share left (the smaller of the windows), 0.0 when only the reserve is left."""
    left = 1.0
    for key, limit, size in assist_windows(session, settings, p, reserve_pct)[len(limits(session, settings, p)):]:
        if limit < 1:
            return 0.0
        left = min(left, max(0.0, (limit - ratelimit.used(session, key, size)) / limit))
    return round(left, 2)
