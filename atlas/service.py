"""Core operations: ingest observations, rebuild canonicals, merge, detach, lock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import case, select, text, update
from sqlalchemy.orm import Session

from . import client_changes, dates, feed, feed_schema
from .contract import ObservationBatchV1
from .db import sqlite_write_lock
from .ids import canonical_id as new_canonical_id
from .ids import ulid
from .merge import Obs, merge
from .models import Canonical, Crosswalk, Lock, Observation, ReviewItem, now
from .normalize import norm_namespace, norm_title
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
    """Serialise writers for the rest of the transaction. Resolution is read-then-write, so concurrent
    writers could otherwise both create a record for the same identity. PostgreSQL: a transaction-scoped
    advisory lock. SQLite: the transaction re-opens with BEGIN IMMEDIATE (the single writer lock), unless it
    already wrote (then it holds that lock) or a savepoint is open (`db.sqlite_write_lock`)."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(727274)"))
    elif dialect == "sqlite":
        sqlite_write_lock(session)


def _valid_type(session: Session, media_type: str | None) -> str:
    from .models import MediaType

    if media_type and media_type != "game":
        row = session.get(MediaType, media_type)
        if row is None or not row.enabled:
            raise AtlasError(f"unknown or disabled media type {media_type!r}")
        return media_type
    return "game"


def keys_of(obs: Observation) -> list[tuple[str, str]]:
    keys = [(norm_namespace(obs.source), obs.external_id)]
    keys += [(k, v) for k, v in (obs.payload.get("provider_ids") or {}).items()]
    return list(dict.fromkeys(keys))  # a source may list its own namespace in provider_ids; register each key once


def rebuild(session: Session, ctx: Ctx, cid: str, *, cause: str = "local", only_if_changed: bool = False) -> Canonical:
    """Re-derive a record's view and bump its version. `cause` is the client change cause when no
    `client_changes.cause` scope is active (a scope, such as a provider import or a sync apply, wins).
    `only_if_changed`: leave the record and its version alone when the derived data is the same."""
    session.flush()  # the merge reads observations and locks with SQL
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
    columns = dates.columns(view.get("first_release"))  # C5.2: year-only dates sort and filter too
    if only_if_changed and row.view == view and row.explain == explain and \
            all(getattr(row, k) == v for k, v in columns.items()):
        return row
    row.view, row.explain = view, explain
    row.norm_title = norm_title(view.get("title"))
    for column, value in columns.items():
        setattr(row, column, value)
    return bump_version(session, cid, cause)


def bump_version(session: Session, cid: str, cause: str = "local",
                 origin: tuple[str, int] | None = None) -> Canonical:
    """The single writer of `Canonical.version` (C3.1): advance it and record the bump in the local client
    change log. Nothing here touches the signed feed; whether a change is federated is decided by the
    caller's `feed.emit`, gated on `redistributable` and the node role as before."""
    row = session.get(Canonical, cid)
    if row is None:
        raise AtlasError(f"unknown canonical {cid}", "not_found")
    write_lock(session)  # seq order must equal commit order, or a polling client could skip a row
    row.version += 1
    row.updated_at = now()
    client_changes.record(session, cid, row.version, cause, origin)
    session.flush()
    return row


def touch_version(session: Session, cid: str, *, cause: str = "local") -> Canonical:
    """Advance a record version when client-visible data outside `view` changes."""
    return bump_version(session, cid, cause)


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
    session.flush()  # sessions do not autoflush: one ingest item may open the same review twice
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
           system: bool = False, force_canonical: str | None = None,
           media_type: str | None = None) -> list[IngestResult]:
    """`curator`: token holds the curate scope. `system`: the node itself (provider fetches, imports)."""
    trusted = curator or system
    write_lock(session)
    media_type = _valid_type(session, media_type)
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

        if force_canonical:
            target = follow(session, force_canonical)
            if session.get(Canonical, target) is None:
                raise AtlasError(f"unknown canonical {force_canonical}", "not_found")
            match: Match = Match(target, "forced", 1.0, [(target, 1.0)])
        else:
            match = find(
                session, ctx.settings, namespace=ns, external_id=item.external_id,
                provider_ids=item.provider_ids, title=item.title,
                release_date=payload.get("release_date"), platform=item.platform,
                developer=(payload.get("facts") or {}).get("developer"),
                media_type=media_type,
            )
        status = "updated" if match.assigned else "created"
        if match.assigned:
            cid = match.canonical_id  # type: ignore[assignment]
        else:
            cid = new_canonical_id()
            session.add(Canonical(id=cid, view={}, explain={}, media_type=media_type))
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
        cause = "provider" if system else ("feed" if obs.redistributable else "local")
        rebuild(session, ctx, cid, cause=cause)
        touched.add(cid)
        left_row = session.get(Canonical, left) if left else None
        if left_row is not None and left_row.merged_into is None:
            rebuild(session, ctx, left, cause=cause)

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
            feed.emit(session, ctx, "obs", {
                "canonical_id": cid, "media_type": session.get(Canonical, cid).media_type,
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
    from .custom import move_values
    from .facets import move_facets
    from .social import move_list_items

    move_facets(session, src, dst)
    move_values(session, src, dst)
    move_list_items(session, src, dst)
    s.merged_into = dst
    s.view, s.explain = {}, {}
    with client_changes.cause(session, "merge"):  # both ids reach clients: the redirect and the survivor
        bump_version(session, src, "merge")
        rebuild(session, ctx, dst, cause="merge")
    if log and ctx.settings.role == "authority":
        feed.emit(session, ctx, "merge", {"src": src, "dst": dst})
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
    # The split record is still the same kind of thing: a movie or anime must not turn into a game.
    original = session.get(Canonical, old)
    session.add(Canonical(id=cid, view={}, explain={}, media_type=original.media_type if original else "game"))
    session.flush()
    obs.canonical_id = cid
    for s in siblings:  # superseded history of the same source record travels with it
        same_record = (s.source, s.source_scope, s.external_id) == (obs.source, obs.source_scope, obs.external_id)
        if not s.is_current and same_record:
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
    if not any(s.is_current and s.source == obs.source for s in siblings):
        from .models import Achievement, Artwork

        for model in (Artwork, Achievement):
            for row in session.scalars(select(model).where(model.canonical_id == old, model.source == obs.source)):
                row.canonical_id = cid
    session.flush()
    with client_changes.cause(session, "detach"):
        rebuild(session, ctx, old, cause="detach")
        new = rebuild(session, ctx, cid, cause="detach")
    if log and ctx.settings.role == "authority":
        feed.emit(session, ctx, "detach", {
            "observation_id": observation_id, "canonical_id": old, "new_canonical_id": cid,
        })
    return new


DATES_PROTOCOL = feed_schema.RESERVED_TOKENS["dates-v1"]  # "1.3"


def dates_gated(ctx: Ctx) -> bool:
    """True while this node may not sign a partial or regional release date: it is an authority (its writes
    enter the signed feed) and its `emit_protocol` is below 1.3 (C1.6, H13). Observations are still stored
    in full; only the signed copy omits `ext.release`. Locks cannot be split like that, so they are refused."""
    if ctx.settings.role != "authority":
        return False
    return feed_schema.parse_protocol(feed_schema.emit_protocol(ctx.settings)) < \
        feed_schema.parse_protocol(DATES_PROTOCOL)


def dates_gate_message(ctx: Ctx, feature: str) -> str:
    return str(feed_schema.EmitGateError(feature, DATES_PROTOCOL, feed_schema.emit_protocol(ctx.settings)))


def clean_lock(field: str, value: Any, *, replicated: bool = False) -> Any:
    """Validate a lock value. `release_date` takes a PartialDate ("2011", "2011-04", "2011-04-19"; a day may
    have any year, as the legacy key always allowed); `releases` takes at most five release entries (C5.1).
    Raises AtlasError.

    `replicated`: the value comes from a signed feed entry, which the authority may have written before C5,
    when a `release_date` lock took any text ("TBA"). Such a value is stored as it is, so a follower that
    applies or replays it matches the authority instead of quarantining the entry (the merge shows it in the
    legacy key, as before). Only new local locks are held to the date rules."""
    from .merge import LOCK_FIELDS, SCALAR_FIELDS, UNION_FIELDS

    if field not in LOCK_FIELDS:
        raise AtlasError(f"field {field!r} cannot be locked")
    if field in UNION_FIELDS and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise AtlasError(f"{field} needs a list of strings")
    if field in SCALAR_FIELDS and not isinstance(value, str):
        raise AtlasError(f"{field} needs a string")
    try:
        if field == "release_date":
            if replicated and dates.try_parse_first(value) is None:
                return value  # a pre-C5 free-text lock (checked to be a string above)
            return dates.parse_first(value).value
        if field == "releases":
            return dates.clean_entries(value)
    except dates.DateError as exc:
        raise AtlasError(f"{field}: {exc}") from exc
    return value


def lock_needs_dates_protocol(field: str, value: Any) -> bool:
    """A lock older followers would misread: a partial first release (a 1.2 follower would store "2011" as a
    day-only `release_date`) or a `releases` lock (a field they do not know)."""
    if field == "releases":
        return True
    return field == "release_date" and isinstance(value, str) and dates.precision_of(value) != "day"


def set_lock(session: Session, ctx: Ctx, cid: str, field: str, value: Any, *, actor: str = "",
             local_only: bool = False, log: bool = True) -> Canonical:
    value = clean_lock(field, value, replicated=not log)
    signed = log and not local_only and ctx.settings.role == "authority"
    if signed and lock_needs_dates_protocol(field, value) and dates_gated(ctx):
        what = "a release-dates lock" if field == "releases" else "a partial release date lock"
        raise AtlasError(dates_gate_message(ctx, what), "emit_protocol")
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
    rebuild(session, ctx, cid, cause="feed" if signed else "local")
    if signed:
        feed.emit(session, ctx, "lock", {"canonical_id": cid, "field": field, "value": value})
    return row


def clear_lock(session: Session, ctx: Ctx, cid: str, field: str, *, log: bool = True) -> Canonical:
    write_lock(session)
    lock = session.get(Lock, (cid, field))
    if lock is None:
        raise AtlasError("no such lock", "not_found")
    local_only = lock.local_only
    signed = log and not local_only and ctx.settings.role == "authority"
    if signed and field == "releases" and dates_gated(ctx):
        raise AtlasError(dates_gate_message(ctx, "removing a release-dates lock"), "emit_protocol")
    session.delete(lock)
    session.flush()
    row = rebuild(session, ctx, cid, cause="feed" if signed else "local")
    if signed:
        feed.emit(session, ctx, "unlock", {"canonical_id": cid, "field": field})
    return row


SORTS = ("relevance", "title", "release", "-release")


def search(session: Session, q: str, limit: int = 25, offset: int = 0,
           media_type: str | None = None, *, released_from: str | None = None, released_to: str | None = None,
           sort: str = "relevance") -> list[Canonical]:
    """Records by title. `released_from`/`released_to` are PartialDates ("2011" = the whole year): a record
    matches when its first release lies entirely inside the range, so a year-only "2011" matches 2011..2011
    but not April 2011 (it might be any month). `sort=release` orders by the start of the first release, then
    by precision (year before month before day), unknown dates last; `-release` reverses the dates."""
    if sort not in SORTS:
        raise AtlasError(f"sort must be one of {', '.join(SORTS)}")
    try:
        low = dates.parse_bound(released_from, "released_from")
        high = dates.parse_bound(released_to, "released_to")
    except dates.DateError as exc:
        raise AtlasError(str(exc)) from exc
    if low and high and low.start() > high.end():
        raise AtlasError("released_from is after released_to")
    norm = norm_title(q)
    stmt = (
        select(Canonical)
        .where(Canonical.merged_into.is_(None), Canonical.norm_title != "")
        .where(Canonical.media_type == media_type if media_type else Canonical.media_type.is_not(None))
        .limit(min(limit, 100))
        .offset(max(offset, 0))
    )
    if low is not None:
        stmt = stmt.where(Canonical.release_start >= low.start())
    if high is not None:
        stmt = stmt.where(Canonical.release_end <= high.end())
    if norm:
        like = "%" + "%".join(norm.split()) + "%"
        stmt = stmt.where(Canonical.norm_title.like(like))
    rank = case((Canonical.release_precision == "year", 0), (Canonical.release_precision == "month", 1), else_=2)
    unknown_last = case((Canonical.release_start.is_(None), 1), else_=0)
    if sort == "release":
        stmt = stmt.order_by(unknown_last, Canonical.release_start.asc(), rank.asc(), Canonical.norm_title, Canonical.id)
    elif sort == "-release":
        stmt = stmt.order_by(unknown_last, Canonical.release_start.desc(), rank.desc(), Canonical.norm_title,
                             Canonical.id)
    elif sort == "title" or norm:
        stmt = stmt.order_by(Canonical.norm_title.asc(), Canonical.id)
    else:
        stmt = stmt.order_by(Canonical.updated_at.desc())
    rows = list(session.scalars(stmt))
    if not norm or sort != "relevance":
        return rows
    rows.sort(key=lambda r: (r.norm_title != norm, not r.norm_title.startswith(norm), len(r.norm_title)))
    return rows
