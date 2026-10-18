"""Core operations: ingest observations, rebuild canonicals, merge, detach, lock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from . import feed
from .contract import ObservationBatchV1
from .ids import canonical_id as new_canonical_id
from .ids import ulid
from .merge import Obs, merge
from .models import Canonical, Crosswalk, Lock, Observation, ReviewItem, now
from .normalize import norm_namespace, norm_title, year_of
from .resolve import Match, find, follow
from .settings import Settings
from .signing import NodeIdentity


class AtlasError(Exception):
    """Client-visible domain error; `code` maps to an HTTP status in the API layer."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Ctx:
    settings: Settings
    identity: NodeIdentity


@dataclass
class IngestResult:
    external_id: str
    canonical_id: str | None
    method: str
    confidence: float
    status: str  # created | updated | stale
    candidates: list[tuple[str, float]]
    review: bool = False


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def write_lock(session: Session) -> None:
    """Serialise writers on PostgreSQL (transaction-scoped). Resolution is read-then-write, so concurrent
    writers could otherwise both create a record for the same identity. SQLite already has one writer."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(727274)"))


def keys_of(obs: Observation) -> list[tuple[str, str]]:
    keys = [(norm_namespace(obs.source), obs.external_id)]
    keys += [(k, v) for k, v in (obs.payload.get("provider_ids") or {}).items()]
    return keys


def rebuild(session: Session, ctx: Ctx, cid: str) -> Canonical:
    row = session.get(Canonical, cid)
    if row is None:
        raise AtlasError(f"unknown canonical {cid}", "not_found")
    rows = session.scalars(
        select(Observation).where(Observation.canonical_id == cid, Observation.is_current.is_(True))
    ).all()
    obs = [
        Obs(
            id=o.id, source=o.source, source_kind=o.source_kind, external_id=o.external_id, payload=o.payload,
            trust=ctx.settings.trust(norm_namespace(o.source)),
            stamp=o.source_updated_at or _epoch(o.observed_at),
        )
        for o in rows
    ]
    locks = {lk.field: lk.value for lk in session.scalars(select(Lock).where(Lock.canonical_id == cid))}
    view, explain = merge(obs, locks)
    row.view, row.explain = view, explain
    row.norm_title = norm_title(view.get("title"))
    row.year = year_of(view.get("release_date"))
    row.version += 1
    row.updated_at = now()
    session.flush()
    return row


def _redistributable(ctx: Ctx, source: str, curator: bool) -> bool:
    return (
        curator
        and ctx.settings.role == "authority"
        and norm_namespace(source) in ctx.settings.redistributable_sources
    )


def _open_review(session: Session, kind: str, cid: str, candidates: list[tuple[str, float]], conf: float) -> bool:
    cands = [[c, s] for c, s in candidates if c != cid]
    if not cands:
        return False
    existing = session.scalars(
        select(ReviewItem).where(
            ReviewItem.canonical_id == cid, ReviewItem.kind == kind, ReviewItem.status == "open"
        )
    ).first()
    if existing:
        existing.candidates, existing.confidence = cands, conf
    else:
        session.add(ReviewItem(kind=kind, canonical_id=cid, candidates=cands, confidence=conf))
    return True


def ingest(session: Session, ctx: Ctx, batch: ObservationBatchV1, *, curator: bool = False,
           system: bool = False) -> list[IngestResult]:
    """`curator`: token holds the curate scope. `system`: the node itself (provider fetches, imports)."""
    trusted = curator or system
    write_lock(session)
    conn = batch.connection
    ns = norm_namespace(conn.source)
    reserved = set(ctx.settings.source_trust) | ctx.settings.redistributable_sources
    if ns in reserved and not trusted:
        raise AtlasError(f"source {ns!r} is reserved; it needs a token with the curate scope", "forbidden")
    horizon = int(now().timestamp()) + 86400
    results: list[IngestResult] = []
    touched: set[str] = set()

    for item in batch.observations:
        payload = item.reported()
        if payload.get("source_updated_at", 0) > horizon:  # a clock in the far future would freeze the record
            payload.pop("source_updated_at")
        stamp = payload.get("source_updated_at")
        prior = session.scalars(
            select(Observation).where(
                Observation.source == ns, Observation.source_scope == conn.source_scope,
                Observation.external_id == item.external_id, Observation.is_current.is_(True),
            )
        ).first()
        if prior and stamp is not None and prior.source_updated_at is not None \
                and stamp < prior.source_updated_at:
            results.append(IngestResult(item.external_id, prior.canonical_id, "crosswalk", 1.0, "stale", []))
            continue

        match: Match = find(
            session, ctx.settings, namespace=ns, external_id=item.external_id,
            provider_ids=item.provider_ids, title=item.title,
            release_date=payload.get("release_date"), platform=item.platform,
            developer=(payload.get("facts") or {}).get("developer"),
        )
        status = "updated" if match.assigned else "created"
        if match.assigned:
            cid = match.canonical_id  # type: ignore[assignment]
        else:
            cid = new_canonical_id()
            session.add(Canonical(id=cid, view={}, explain={}))
            session.flush()

        left = None
        if prior:
            prior.is_current = False
            if prior.canonical_id != cid:
                left = prior.canonical_id
        obs = Observation(
            id=ulid(), canonical_id=cid, source_kind=conn.source_kind, source=ns,
            source_scope=conn.source_scope, external_id=item.external_id, payload=payload,
            source_updated_at=stamp,
            redistributable=_redistributable(ctx, conn.source, curator),
        )
        session.add(obs)
        session.flush()

        conflicts: list[tuple[str, float]] = []
        for key in keys_of(obs):
            existing = session.get(Crosswalk, key)
            if existing is None:
                if not trusted and key[0] in reserved and key[0] != ns:
                    continue  # an uncurated claim about someone else's namespace may match, never register
                session.add(Crosswalk(namespace=key[0], external_id=key[1], canonical_id=cid))
            elif follow(session, existing.canonical_id) != cid:
                conflicts.append((follow(session, existing.canonical_id), 1.0))
        session.flush()
        rebuild(session, ctx, cid)
        touched.add(cid)
        left_row = session.get(Canonical, left) if left else None
        if left_row is not None and left_row.merged_into is None:
            rebuild(session, ctx, left)

        review = False
        if conflicts:
            review = _open_review(session, "id_conflict", cid, conflicts, 1.0)
        if not match.assigned and match.candidates:
            review = _open_review(session, "possible_duplicate", cid, match.candidates, match.confidence) or review
        if match.assigned and match.conflict:
            review = _open_review(
                session, "id_conflict", cid, [(c, s) for c, s in match.candidates if c != cid], 0.9
            ) or review

        if obs.redistributable:
            feed.append(session, ctx.identity, "obs", {
                "canonical_id": cid,
                "observation": {
                    "id": obs.id, "source_kind": obs.source_kind, "source": obs.source,
                    "source_scope": obs.source_scope, "external_id": obs.external_id, "payload": obs.payload,
                    "source_updated_at": obs.source_updated_at, "observed_at": obs.observed_at.isoformat(),
                },
            })
        results.append(IngestResult(
            item.external_id, cid, match.method if match.assigned else "none",
            match.confidence, status, match.candidates, review,
        ))
    return results


def get_canonical(session: Session, cid: str) -> tuple[Canonical, str | None]:
    """Return (live record, redirected_from or None)."""
    row = session.get(Canonical, cid)
    if row is None:
        raise AtlasError(f"unknown canonical {cid}", "not_found")
    if row.merged_into is None:
        return row, None
    live = session.get(Canonical, follow(session, cid))
    if live is None:  # pragma: no cover - defensive
        raise AtlasError(f"unknown canonical {cid}", "not_found")
    return live, cid


def merge_canonicals(session: Session, ctx: Ctx, src: str, dst: str, *, actor: str = "", log: bool = True) -> Canonical:
    write_lock(session)
    if src == dst:
        raise AtlasError("cannot merge a record into itself")
    s, d = session.get(Canonical, src), session.get(Canonical, dst)
    if s is None or d is None:
        raise AtlasError("unknown canonical", "not_found")
    if s.merged_into or d.merged_into:
        raise AtlasError("one of the records is already merged; use the live record", "conflict")
    session.execute(update(Observation).where(Observation.canonical_id == src).values(canonical_id=dst))
    session.execute(update(Crosswalk).where(Crosswalk.canonical_id == src).values(canonical_id=dst))
    dst_fields = {lk.field for lk in session.scalars(select(Lock).where(Lock.canonical_id == dst))}
    for lk in session.scalars(select(Lock).where(Lock.canonical_id == src)).all():
        if lk.field in dst_fields:
            session.delete(lk)
        else:
            lk.canonical_id = dst
    for item in session.scalars(select(ReviewItem).where(ReviewItem.status == "open")).all():
        involved = {item.canonical_id, *(c for c, _ in item.candidates)}
        if involved <= {src, dst} and src in involved:
            item.status, item.decided_by = "merged", actor
        elif item.canonical_id == src:
            item.canonical_id = dst
    s.merged_into = dst
    s.view, s.explain = {}, {}
    session.flush()
    rebuild(session, ctx, dst)
    if log and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "merge", {"src": src, "dst": dst})
    return d


def detach_observation(session: Session, ctx: Ctx, observation_id: str, *, new_id: str | None = None,
                       log: bool = True) -> Canonical:
    """Split one observation (a wrongly merged source record) onto its own canonical."""
    write_lock(session)
    obs = session.get(Observation, observation_id)
    if obs is None or obs.canonical_id is None:
        raise AtlasError("unknown observation", "not_found")
    old = obs.canonical_id
    siblings = session.scalars(
        select(Observation).where(Observation.canonical_id == old, Observation.id != obs.id)
    ).all()
    if not any(s.is_current for s in siblings):
        raise AtlasError("record has a single observation; nothing to split", "conflict")
    cid = new_id or new_canonical_id()
    session.add(Canonical(id=cid, view={}, explain={}))
    session.flush()
    obs.canonical_id = cid
    for s in siblings:  # superseded history of the same source record travels with it
        if not s.is_current and (s.source, s.source_scope, s.external_id) == (obs.source, obs.source_scope, obs.external_id):
            s.canonical_id = cid
    still_claimed = {k for s in siblings if s.is_current for k in keys_of(s)}
    for key in keys_of(obs):
        if key in still_claimed:
            continue
        row = session.get(Crosswalk, key)
        if row is not None and row.canonical_id == old:
            row.canonical_id = cid
        elif row is None:
            session.add(Crosswalk(namespace=key[0], external_id=key[1], canonical_id=cid))
    session.flush()
    rebuild(session, ctx, old)
    new = rebuild(session, ctx, cid)
    if log and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "detach", {"observation_id": observation_id, "new_canonical_id": cid})
    return new


def set_lock(session: Session, ctx: Ctx, cid: str, field: str, value: Any, *, actor: str = "",
             local_only: bool = False, log: bool = True) -> Canonical:
    from .merge import SCALAR_FIELDS, UNION_FIELDS

    if field not in SCALAR_FIELDS and field not in UNION_FIELDS:
        raise AtlasError(f"field {field!r} cannot be locked")
    if field in UNION_FIELDS and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise AtlasError(f"{field} needs a list of strings")
    if field in SCALAR_FIELDS and not isinstance(value, str):
        raise AtlasError(f"{field} needs a string")
    write_lock(session)
    row = session.get(Canonical, cid)
    if row is None or row.merged_into:
        raise AtlasError("unknown or merged canonical", "not_found")
    lock = session.get(Lock, (cid, field))
    if lock:
        lock.value, lock.set_by, lock.local_only = value, actor, local_only
    else:
        session.add(Lock(canonical_id=cid, field=field, value=value, set_by=actor, local_only=local_only))
    session.flush()
    rebuild(session, ctx, cid)
    if log and not local_only and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "lock", {"canonical_id": cid, "field": field, "value": value})
    return row


def clear_lock(session: Session, ctx: Ctx, cid: str, field: str, *, log: bool = True) -> Canonical:
    write_lock(session)
    lock = session.get(Lock, (cid, field))
    if lock is None:
        raise AtlasError("no such lock", "not_found")
    local_only = lock.local_only
    session.delete(lock)
    session.flush()
    row = rebuild(session, ctx, cid)
    if log and not local_only and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "unlock", {"canonical_id": cid, "field": field})
    return row


def search(session: Session, q: str, limit: int = 25) -> list[Canonical]:
    norm = norm_title(q)
    if not norm:
        return []
    like = "%" + "%".join(norm.split()) + "%"
    stmt = (
        select(Canonical)
        .where(Canonical.merged_into.is_(None), Canonical.norm_title.like(like))
        .order_by(Canonical.norm_title.asc())
        .limit(min(limit, 100))
    )
    rows = list(session.scalars(stmt))
    rows.sort(key=lambda r: (r.norm_title != norm, not r.norm_title.startswith(norm), len(r.norm_title)))
    return rows
