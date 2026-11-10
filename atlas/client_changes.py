"""This node's local, unsigned client change log (CONTRACTS-v1 C3).

Every client-visible version bump of a canonical record is recorded here by `service.bump_version`, whatever
caused it: an entry this node signed into its own feed (`feed`), an applied upstream entry (`replicated`),
a local-only edit (`local`), a provider import (`provider`), a merge (`merge`) or a detach (`detach`).
`GET /api/v2/changes?feed=local` pages through it.

The log is private to this node and its own API clients:
- rows hold a canonical id, a version and a cause, never record content, so non-redistributable provider
  data is never exposed by it;
- nothing that builds or serves a signed feed reads it (`feed`, `/sync/changes`, relay, promotion), and a
  recovery candidate starts with an empty log.

A cursor is a `seq` together with the log's **epoch**, a random id kept in `node_state`. Every event that
recreates or rewinds the log gets a new epoch: the 0023 upgrade (so a downgrade and re-upgrade), a backup
restore and a recovery candidate. A seq alone cannot tell a recreated log from progress once the new head
has passed the old cursor; a client that sends its stored epoch is told to `reset` whenever it differs.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session, aliased

from .models import Canonical, ClientChange, NodeState

CAUSES = ("feed", "replicated", "local", "provider", "merge", "detach", "unmerge")
_SCOPE = "atlas_client_change_scope"
_PENDING = "atlas_client_change_pending"
PRUNED_AT = "client_change_pruned_at"
PRUNE_EVERY = timedelta(days=1)
EPOCH = "client_change_epoch"

Origin = tuple[str, int] | None


@contextlib.contextmanager
def cause(session: Session, name: str, origin: Origin = None) -> Iterator[None]:
    """Attribute every version bump inside the block to `name`.

    The innermost scope wins, so a merge applied from an upstream entry is recorded as `merge`. An inner
    scope without an origin inherits the nearest enclosing origin (the upstream node and seq)."""
    if name not in CAUSES:
        raise ValueError(f"unknown client change cause {name!r}")
    stack: list[tuple[str, Origin]] = session.info.setdefault(_SCOPE, [])
    if origin is None and stack:
        origin = next((o for _, o in reversed(stack) if o is not None), None)
    stack.append((name, origin))
    try:
        yield
    finally:
        stack.pop()


def _effective(session: Session, default: str, origin: Origin) -> tuple[str, Origin]:
    stack: list[tuple[str, Origin]] = session.info.get(_SCOPE) or []
    if stack:
        name, scoped = stack[-1]
        return name, origin if origin is not None else scoped
    return default, origin


def record(session: Session, cid: str, version: int, cause_name: str = "local", origin: Origin = None) -> None:
    """Record one version bump. Only `service.bump_version` calls this; it already holds the writer lock,
    so on PostgreSQL seq order equals commit order and a polling client never skips a later-committed seq.

    Bumps of one record with the same cause and origin inside one (sub)transaction share a row, which keeps
    a provider import (observation, artwork, achievements, custom values) to a single row."""
    name, origin = _effective(session, cause_name, origin)
    if name not in CAUSES:
        raise ValueError(f"unknown client change cause {name!r}")
    txn = session.get_nested_transaction() or session.get_transaction()
    pending: dict[Any, ClientChange] | None = None
    cache = session.info.get(_PENDING)
    if cache is not None and cache[0] is txn:
        pending = cache[1]
    else:
        pending = {}
        session.info[_PENDING] = (txn, pending)
    key = (cid, name, origin)
    row = pending.get(key)
    if row is not None:
        state = inspect(row)
        if state.pending or (state.persistent and not state.deleted):
            row.version = version
            return
    row = ClientChange(canonical_id=cid, version=version, cause=name,
                       origin_node=origin[0] if origin else None, origin_seq=origin[1] if origin else None)
    session.add(row)
    pending[key] = row


def head(session: Session) -> int:
    return session.scalar(select(func.max(ClientChange.seq))) or 0


def new_epoch() -> str:
    return secrets.token_hex(8)


def epoch(session: Session) -> str:
    """The current log epoch. Migration 0023 creates it; a database without one (an unmerged development
    revision of 0023) gets one here, which only ever makes clients reset."""
    state = session.get(NodeState, EPOCH)
    if state is None:
        state = NodeState(key=EPOCH, value=new_epoch())
        session.add(state)
        session.flush()
    return state.value


def renew_epoch(session: Session) -> str:
    """Start a new epoch: call this whenever the log is recreated or rewound (restore, recovery)."""
    value = new_epoch()
    state = session.get(NodeState, EPOCH)
    if state is None:
        session.add(NodeState(key=EPOCH, value=value))
    else:
        state.value = value
    session.flush()
    return value


def page(session: Session, since: int, limit: int, client_epoch: str | None = None, *,
         hidden: Any = None) -> dict[str, Any]:
    """One page of the local feed after `since`. `reset` means the client's cursor cannot be continued:
    entries it has not seen were pruned, or the log was recreated or rewound (a downgrade and re-upgrade, a
    restore or a recovery candidate), which a client that sends its stored `client_epoch` always learns.
    Without an epoch only a cursor above the head is recognised as stale.

    `hidden` (C14 §5.7, `scope.hidden_ids(viewer)`): a subquery of the record ids the caller may not see. Their
    entries are left out exactly as if the record did not exist (the cursor still moves past them)."""
    current = epoch(session)
    top = head(session)
    floor = session.scalar(select(func.min(ClientChange.seq)))
    recreated = client_epoch is not None and client_epoch != current
    if recreated or since > top or (floor is not None and since < floor - 1):
        return {"epoch": current, "head": top, "next_cursor": top, "has_more": False, "reset": True,
                "results": []}
    rows = list(session.scalars(
        select(ClientChange).where(ClientChange.seq > since).order_by(ClientChange.seq).limit(limit)))
    ids: list[str] = []
    causes: dict[str, set[str]] = {}
    for row in rows:
        if row.canonical_id not in causes:
            ids.append(row.canonical_id)
            causes[row.canonical_id] = set()
        causes[row.canonical_id].add(row.cause)
    records: dict[str, Canonical] = {}
    if ids:
        q = select(Canonical).where(Canonical.id.in_(ids))
        if hidden is not None:
            q = q.where(Canonical.id.not_in(hidden))
        records = {r.id: r for r in session.scalars(q)}
    next_cursor = rows[-1].seq if rows else since
    return {
        "epoch": current,
        "head": top,
        "next_cursor": next_cursor,
        "has_more": bool(rows) and next_cursor < top,
        "reset": False,
        "results": [
            {"id": cid, "version": records[cid].version, "merged_into": records[cid].merged_into,
             "causes": sorted(causes[cid])}
            for cid in ids if cid in records
        ],
    }


def prune(session: Session, retain_days: int, *, at: datetime | None = None) -> int:
    """Delete rows older than `retain_days` that have a newer row for the same record.

    The newest row of every record is always kept, so a client whose cursor is older than the pruned rows
    still sees every record changed since. Its cursor may now be below the oldest kept row, which the API
    reports as `reset` (the documented failure mode, C3.2)."""
    cutoff = (at or datetime.now(UTC)) - timedelta(days=retain_days)
    newer = aliased(ClientChange)
    has_newer = select(newer.seq).where(newer.canonical_id == ClientChange.canonical_id,
                                        newer.seq > ClientChange.seq).exists()
    result = session.execute(delete(ClientChange).where(ClientChange.created_at < cutoff, has_newer)
                             .execution_options(synchronize_session=False))
    session.info.pop(_PENDING, None)
    return int(result.rowcount or 0)


def maybe_prune(session: Session, retain_days: int, *, at: datetime | None = None) -> int | None:
    """The daily retention job: prune at most once per `PRUNE_EVERY`. Returns rows deleted, or None if
    it was not due yet."""
    at = at or datetime.now(UTC)
    state = session.get(NodeState, PRUNED_AT)
    if state is not None:
        try:
            last = datetime.fromisoformat(state.value)
        except ValueError:
            last = None
        if last is not None and at - last < PRUNE_EVERY:
            return None
    deleted = prune(session, retain_days, at=at)
    if state is None:
        session.add(NodeState(key=PRUNED_AT, value=at.isoformat()))
    else:
        state.value = at.isoformat()
    session.flush()
    return deleted
