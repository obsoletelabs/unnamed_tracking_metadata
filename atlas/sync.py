"""Pull replication from a trusted upstream's signed change feed.

Compatibility (CONTRACTS-v1 C1, docs/guide/federation.md "Compatibility and upgrades"):

* The follower negotiates the federation protocol on `/sync/info` before it downloads anything, and
  refuses an upstream it cannot safely follow (fail closed, N-1).
* Every verified entry is classified against `atlas.feed_schema`. Entries this release cannot
  understand are *parked*: retained, verified and relayable, but not applied, and nothing after them
  is applied either (causal order). Entries it understands but cannot apply are *quarantined*:
  recorded, retried, and they do not block. Nothing is silently skipped.
* Per upstream there are separate heads: observed (unsigned claim) >= downloaded >= verified (signed,
  retained) >= applied (the local view). Only `applied` is the cursor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from . import client_changes, feed_schema, key_rotation, matching, service
from .contract import SourceObservationV1
from .feed_schema import APPLY, PARK, PROTOCOL, QUARANTINE
from .models import (
    Achievement,
    AuditEntry,
    Canonical,
    Crosswalk,
    Lock,
    MergeRecord,
    MergeRecordItem,
    NodeState,
    Observation,
    Peer,
    ReplicatedChange,
    ReplicatedFeed,
    SyncCursor,
    SyncFailure,
)
from .service import AtlasError, Ctx, keys_of
from .signing import key_id_for

log = logging.getLogger("atlas.sync")

NOOP = "noop"


class SyncError(Exception):
    pass


class SyncRefused(SyncError):
    """Page-level refusal from protocol negotiation. Nothing is downloaded and the cursor is untouched."""

    def __init__(self, reason: str, upstream_protocol: str):
        super().__init__(reason)
        self.reason, self.upstream_protocol = reason, upstream_protocol


class SyncRewound(SyncError):
    """The authority's signed history no longer matches what this node verified (a rewind or a fork).
    Recorded in node state so status never reports the upstream as synchronized (C1.7)."""

    def __init__(self, message: str, kind: str, advertised: int | None, verified: int):
        super().__init__(message)
        self.kind, self.advertised, self.verified = kind, advertised, verified


class InvalidEntry(ValueError):
    """A known entry whose values fail validation (quarantine:invalid)."""


def _is_upstream_origin(session: Session, cid: str) -> bool:
    return session.scalars(
        select(Observation.id).where(Observation.canonical_id == cid, Observation.origin_node.is_not(None)).limit(1)
    ).first() is not None


def _ensure_canonical(session: Session, cid: str, media_type: str | None = None) -> None:
    from .models import MediaType

    if session.get(Canonical, cid) is None:
        known = media_type and session.get(MediaType, media_type) is not None
        session.add(Canonical(id=cid, view={}, explain={}, media_type=media_type if known else "game"))
        session.flush()


_OBS_FIELDS = tuple(sorted(feed_schema.OBS_PAYLOAD))
_ID = re.compile(r"[0-9A-Za-z]{26}\Z")


def _valid_observation(o: Any) -> bool:
    """What an upstream may send: the same shape and limits as an observation submitted through the API."""
    try:
        if not isinstance(o, dict) or not _ID.match(str(o.get("id", ""))) or len(json.dumps(o.get("payload"))) > 100_000:
            return False
        for key, limit in (("source", 128), ("source_scope", 512), ("external_id", 256)):
            if not isinstance(o.get(key), str) or not 0 < len(o[key]) <= limit:
                return False
        if o.get("source_kind") not in ("host", "plugin", "manual", "metadata"):
            return False
        payload = o["payload"]
        if not isinstance(payload, dict) or set(payload) - set(_OBS_FIELDS):
            return False
        SourceObservationV1.model_validate({"external_id": o["external_id"], **payload})
        datetime.fromisoformat(o["observed_at"])
        return True
    except (ValidationError, ValueError, TypeError, KeyError):
        return False


#: C13 §5.2: an entry that names a record this node does not hold yet. Not a failure: it is recorded, does not
#: block (the cursor moves on), and is applied when its record arrives (`wake_waiting`). A sync_failure
#: disposition only; never a C10 group disposition.
WAIT = "wait"
WAITING_FOR_RECORD = "waiting_for_record"
#: C13 §5.4: quarantined entries are retried with exponential backoff instead of stopping after a fixed count:
#: the first retry on the next pull, then after 1, 2, 4 ... minutes, at most every 6 hours.
BACKOFF_CAP = timedelta(hours=6)


def retry_delay(attempts: int) -> timedelta:
    if attempts <= 1:
        return timedelta(0)
    return min(timedelta(minutes=2 ** min(attempts - 2, 20)), BACKOFF_CAP)


def next_retry_at(row: SyncFailure) -> datetime | None:
    if row.disposition != QUARANTINE or row.resolved_at is not None:
        return None
    last = _aware(row.last_attempt_at) or datetime.now(UTC)
    return last + retry_delay(row.attempts)


def _named(kind: str, body: dict[str, Any]) -> list[str]:
    from . import feed

    return feed.named(kind, body)


def missing_record(session: Session, kind: str, body: dict[str, Any]) -> str | None:
    """The first record an entry names that this node does not hold (C13 §5.2), or None."""
    for cid in _named(kind, body):
        if session.get(Canonical, cid) is None:
            return cid
    return None


# ---- applying one entry -----------------------------------------------------------------------------------

def apply_entry(session: Session, ctx: Ctx, node_id: str, seq: int, kind: str, body: dict[str, Any], *,
                may_park: bool = True) -> str:
    """Classify and apply one verified entry. Returns "apply", "noop", "parked" or "quarantined".

    Nothing is silently dropped: a parked or quarantined entry is recorded in `sync_failure`, and the
    entry's partial changes are rolled back. `may_park=False` is for entries at or below the applied
    head (retries, a skipped entry): they cannot block any more, so an unsupported one is recorded as
    quarantined unless it is already parked. A caller applying a retained entry wraps the call in
    `entry_at(<its created_at>)`, the origin timestamp the contract dates some values by (C12 §12.1.6)."""
    outcome = feed_schema.classify(kind, body)
    existing = session.get(SyncFailure, (node_id, seq))
    if outcome.action == PARK:
        if may_park or (existing is not None and existing.disposition == PARK and existing.resolved_at is None):
            log.warning("parked feed entry %s (%s) from %s: %s", seq, kind, node_id, outcome.message)
            _record(session, node_id, seq, kind, PARK, outcome.reason, outcome.message, outcome.detail)
            return PARK
        if existing is not None and existing.reason == "skipped":  # keep the administrator's decision visible
            _record(session, node_id, seq, kind, QUARANTINE, "skipped", existing.error, existing.detail)
        else:
            _record(session, node_id, seq, kind, QUARANTINE, outcome.reason, outcome.message, outcome.detail)
        return QUARANTINE
    if outcome.action == QUARANTINE:
        log.warning("invalid feed entry %s (%s) from %s: %s", seq, kind, node_id, outcome.message)
        _record(session, node_id, seq, kind, QUARANTINE, "invalid", outcome.message, outcome.detail)
        return QUARANTINE
    if (waiting := missing_record(session, kind, outcome.body or {})) is not None:
        if existing is None or existing.disposition != WAIT or existing.resolved_at is not None:
            log.info("feed entry %s (%s) from %s waits for record %s", seq, kind, node_id, waiting)
        _record(session, node_id, seq, kind, WAIT, WAITING_FOR_RECORD,
                f"waiting for record {waiting}: the feed has not introduced it yet", {"kind": kind},
                waiting_for=waiting, count=existing is None or existing.disposition != WAIT)
        return WAIT
    try:
        with session.begin_nested():
            result = _apply(session, ctx, node_id, seq, kind, _follow_redirects(session, node_id, seq, kind,
                                                                                outcome.body))
    except InvalidEntry as exc:
        log.warning("invalid feed entry %s (%s) from %s: %s", seq, kind, node_id, exc)
        _record(session, node_id, seq, kind, QUARANTINE, "invalid", f"InvalidEntry: {exc}", {"kind": kind})
        return QUARANTINE
    except (AtlasError, KeyError, TypeError, ValueError, AttributeError) as exc:
        log.warning("could not apply feed entry %s (%s) from %s: %s", seq, kind, node_id, exc)
        _record(session, node_id, seq, kind, QUARANTINE, "apply_error", f"{type(exc).__name__}: {exc}", {"kind": kind})
        return QUARANTINE
    if result == NOOP:
        log.debug("feed entry %s (%s) from %s was a no-op", seq, kind, node_id)
    failure = session.get(SyncFailure, (node_id, seq))
    if failure is not None and failure.resolved_at is None:
        failure.resolved_at = datetime.now(UTC)
        failure.last_attempt_at = failure.resolved_at
    if kind in ("obs", "record", "detach"):  # an entry that can create a record: wake what waits for it
        body = outcome.body or {}
        created = body.get("new_canonical_id" if kind == "detach" else "canonical_id")
        if isinstance(created, str):
            trigger = kind
            if kind == "obs" and isinstance(body.get("observation"), dict) \
                    and body["observation"].get("source") == feed_schema.RECORD_SOURCE:
                trigger = "bridge"
            wake_waiting(session, ctx, node_id, created, trigger=trigger)
    return NOOP if result == NOOP else APPLY


#: C13 §5.2 "Redirects": single-target sidecars land on the survivor of a record merged on this node. `merge` and
#: `detach` never follow (re-targeting a merge could join the wrong records).
REDIRECT_KINDS = frozenset({"artwork", "achievements", "custom", "lock", "unlock", "relation"})


def _follow_redirects(session: Session, node_id: str, seq: int, kind: str, body: Any) -> Any:
    """`body` with each record it names replaced by its survivor when that record was merged here, except when the
    merge is this feed's own, made after `seq`: that history is replayed as the authority ran it (A18b
    `_late_merge`, `_lock_through_merge`, `_achievements_as_they_ran`), and a re-targeted entry would overwrite
    what the merge carried."""
    if kind not in REDIRECT_KINDS or not isinstance(body, dict):
        return body
    out = body
    for field in _named_fields(kind):
        cid = body.get(field)
        if not isinstance(cid, str):
            continue
        row = session.get(Canonical, cid)
        if row is None or row.merged_into is None or _late_merge(session, node_id, seq, cid) is not None:
            continue
        own_later = session.scalars(select(MergeRecord.id).where(
            MergeRecord.src == cid, MergeRecord.reverted_at.is_(None), MergeRecord.origin_node == node_id,
            MergeRecord.origin_seq > seq).limit(1)).first()
        if own_later is not None:
            continue  # this feed's own later merge: rescan parks it for `replay --historical` (amendment A19 #7)
        survivor = service.follow(session, cid)
        if survivor != cid:
            if out is body:
                out = dict(body)
            out[field] = survivor
            log.info("feed entry %s (%s) from %s: %s was merged here into %s; applying to it", seq, kind, node_id,
                     cid, survivor)
    return out


def _named_fields(kind: str) -> tuple[str, ...]:
    from . import feed

    return feed.NAMES.get(kind, ())


def introduced_by(session: Session, node_id: str, cid: str) -> bool:
    """Whether `node_id`'s feed introduced record `cid` here (C13 §5.3 scope): an observation that feed signed sits
    on it (an `obs`, the `record` / bridge observation, or the observation a `detach` of that feed moved onto it),
    or the feed's `record` observation for that id exists (it may have moved on with a later merge)."""
    from . import feed

    if session.get(Observation, feed.record_obs_id(node_id, cid)) is not None:
        return True
    return session.scalars(select(Observation.id).where(
        Observation.canonical_id == cid, Observation.origin_node == node_id).limit(1)).first() is not None


#: C13 §9.1 `atlas_sync_woken_total{node_id, trigger}`: how many records arrived and applied what waited for them,
#: kept in node state (`sync_woken:<trigger>:<feed>`) so every worker and the metrics collector see one count.
WOKEN_TRIGGERS = ("obs", "record", "bridge", "detach")


def woken_counts(session: Session) -> dict[tuple[str, str], int]:
    out = {}
    for key, value in session.execute(select(NodeState.key, NodeState.value)
                                      .where(NodeState.key.like("sync_woken:%"))):
        _, trigger, node = key.split(":", 2)
        if value.isdigit():
            out[(node, trigger)] = int(value)
    return out


def wake_waiting(session: Session, ctx: Ctx, node_id: str, cid: str, *, limit: int | None = None,
                 trigger: str | None = None) -> int:
    """Apply, in feed order, every entry of `node_id`'s feed that waits for `cid` (C13 §5.3), before the caller
    moves on to the next entry of that feed. Scoped to that feed: another feed's entries naming the same record
    wake only when that feed introduces it. Rows are taken `sync_wake_batch` at a time, each batch flushed before
    the next is read, so the order holds and no single query or flush grows without bound. A wake that applied at
    least one entry counts once under `trigger` (the kind of entry that brought the record)."""
    if session.get(Canonical, cid) is None:
        return 0
    batch = limit or getattr(ctx.settings, "sync_wake_batch", 500)
    woken, after = 0, 0
    while True:
        rows = session.scalars(select(SyncFailure).where(
            SyncFailure.node_id == node_id, SyncFailure.disposition == WAIT, SyncFailure.resolved_at.is_(None),
            SyncFailure.waiting_for == cid, SyncFailure.seq > after).order_by(SyncFailure.seq).limit(batch)).all()
        if not rows:
            break
        for row in rows:
            after = row.seq
            entry = session.get(ReplicatedChange, (row.node_id, row.seq))
            if entry is None:
                continue
            with entry_at(entry.created_at):
                if apply_entry(session, ctx, row.node_id, row.seq, entry.kind, entry.body,
                               may_park=False) in (APPLY, NOOP):
                    woken += 1
        session.flush()
        if len(rows) < batch:
            break
    if woken:
        log.info("record %s arrived from %s (%s): %d waiting entr%s applied", cid, node_id, trigger or "wake", woken,
                 "y" if woken == 1 else "ies")
        if trigger in WOKEN_TRIGGERS:
            key = f"sync_woken:{trigger}:{node_id}"
            _set_state(session, key, str((_int_state(session, key) or 0) + 1))
    return woken


#: The origin timestamp of the entry being applied (set by `entry_at`, read by `_apply_body`). A context
#: variable keeps the signatures of `apply_entry` and `_apply`, which release-simulation tests replace, unchanged.
_ENTRY_AT: ContextVar[datetime | None] = ContextVar("atlas_sync_entry_at", default=None)


@contextmanager
def entry_at(at: datetime | None) -> Iterator[None]:
    token = _ENTRY_AT.set(_aware(at))
    try:
        yield
    finally:
        _ENTRY_AT.reset(token)


def _aware(at: datetime | None) -> datetime | None:
    """SQLite returns stored timestamps without a zone; they were written in UTC."""
    return at.replace(tzinfo=UTC) if at is not None and at.tzinfo is None else at


def origin_time(value: Any) -> datetime:
    """The origin timestamp an upstream serves with an entry (`created_at`), kept on the retained copy so every
    node (and every relay) dates the entry alike (C12 §12.1.6). It is not signed: anything that does not parse,
    or lies more than a day in the future, is replaced by this node's clock."""
    received = datetime.now(UTC)
    if not isinstance(value, str) or len(value) > 64:
        return received
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return received
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    return at if at <= received + timedelta(days=1) else received


def _record(session: Session, node_id: str, seq: int, kind: str, disposition: str, reason: str, message: str,
            detail: dict[str, Any] | None, *, waiting_for: str | None = None, count: bool = True) -> None:
    """`count=False` re-checks a waiting entry without counting an attempt: waiting is not failing (C13 §5.2)."""
    message = message[:2000]
    now = datetime.now(UTC)
    failure = session.get(SyncFailure, (node_id, seq))
    if failure is None:
        session.add(SyncFailure(node_id=node_id, seq=seq, kind=kind[:32], error=message, attempts=1,
                                first_failed_at=now, last_attempt_at=now, disposition=disposition, reason=reason,
                                detail=detail, waiting_for=waiting_for))
    else:
        failure.error, failure.last_attempt_at = message, now
        if count:
            failure.attempts += 1
        failure.disposition, failure.reason, failure.detail = disposition, reason, detail
        failure.waiting_for = waiting_for
        failure.resolved_at = None
    session.flush()


def _record_failure(session: Session, node_id: str, seq: int, kind: str, exc: Exception) -> None:
    _record(session, node_id, seq, kind, QUARANTINE, "apply_error", f"{type(exc).__name__}: {exc}", {"kind": kind})


def retry_failures(session: Session, ctx: Ctx, node_id: str | None = None, *, limit: int = 100,
                   max_attempts: int | None = None, due_only: bool | None = None,
                   started: datetime | None = None) -> dict[str, int]:
    """Re-apply quarantined entries, oldest first. Application is idempotent, so a retry that succeeds
    converges without duplicating anything. Parked entries are never retried out of order: they are
    re-classified in sequence by the pull and by `replay`.

    A retry runs after the entries behind it have applied, so it never regresses newer state: an entry that a
    later applied entry of the same feed replaced is resolved as superseded without being applied, and custom
    values are applied only for the keys no later applied entry set (`superseding`).

    Automatic retries (`due_only`, the default unless `max_attempts` is given) take only rows whose backoff has
    passed (C13 §5.4); an administrator's retry takes every row. Waiting entries whose record is here now are
    applied too when their own feed introduced that record (a wake a crash interrupted)."""
    due_only = max_attempts is None if due_only is None else due_only
    woken = _wake_ready(session, ctx, node_id, limit)
    query = select(SyncFailure).where(SyncFailure.resolved_at.is_(None), SyncFailure.disposition == QUARANTINE)
    if node_id is not None:
        query = query.where(SyncFailure.node_id == node_id)
    if max_attempts is not None:
        query = query.where(SyncFailure.attempts < max_attempts)
    if due_only:
        now = datetime.now(UTC)
        candidates = session.scalars(query.order_by(SyncFailure.last_attempt_at, SyncFailure.seq)
                                     .limit(limit * 10)).all()
        # `started`: a row that failed during this pull is not retried in it (its first retry is the next pull)
        rows = sorted((r for r in candidates if (next_retry_at(r) or now) <= now
                       and (started is None or (_aware(r.last_attempt_at) or now) < started)),
                      key=lambda r: (r.node_id, r.seq))[:limit]
    else:
        rows = session.scalars(query.order_by(SyncFailure.node_id, SyncFailure.seq).limit(limit)).all()
    resolved = 0
    for row in rows:
        entry = session.get(ReplicatedChange, (row.node_id, row.seq))
        if entry is None:
            _record_failure(session, row.node_id, row.seq, row.kind, KeyError("retained entry is missing"))
            continue
        if entry.kind == "merge":
            with entry_at(entry.created_at):
                result = retry_merge(session, ctx, row.node_id, row.seq, entry.body)
            if result in (APPLY, NOOP):
                resolved += 1
            continue
        later, body = superseding(session, row.node_id, row.seq, entry.kind, entry.body)
        if later is not None:
            _resolve_superseded(row, later)
            resolved += 1
            continue
        with entry_at(entry.created_at):
            result = apply_entry(session, ctx, row.node_id, row.seq, entry.kind, body, may_park=False)
        if result in (APPLY, NOOP):
            resolved += 1
    out = {"retried": len(rows), "resolved": resolved, "still_failing": len(rows) - resolved}
    if woken:
        out["woken"] = woken
    return out


def _wake_ready(session: Session, ctx: Ctx, node_id: str | None, limit: int) -> int:
    """Waiting entries whose record this node holds now and that their own feed introduced, oldest first (C13 §5.3:
    a safety net for a wake interrupted by a crash, and the startup wake after migration 0036). A row whose record
    came from another feed, or was created here, keeps waiting: one upstream must not be able to stage entries for a
    record another upstream introduces."""
    query = select(SyncFailure).where(SyncFailure.resolved_at.is_(None), SyncFailure.disposition == WAIT,
                                      SyncFailure.waiting_for.in_(select(Canonical.id)))
    if node_id is not None:
        query = query.where(SyncFailure.node_id == node_id)
    woken = 0
    for row in session.scalars(query.order_by(SyncFailure.node_id, SyncFailure.seq).limit(limit)).all():
        entry = session.get(ReplicatedChange, (row.node_id, row.seq))
        if entry is None or row.resolved_at is not None or row.disposition != WAIT:
            continue
        if not isinstance(row.waiting_for, str) or not introduced_by(session, row.node_id, row.waiting_for):
            continue
        with entry_at(entry.created_at):
            if apply_entry(session, ctx, row.node_id, row.seq, entry.kind, entry.body, may_park=False) in (APPLY,
                                                                                                          NOOP):
                woken += 1
    return woken


def open_waiting(session: Session, node_id: str | None = None) -> dict[str, Any]:
    """`waiting` (C13 §5.5): how many entries wait, for how many records, and the oldest."""
    base = [SyncFailure.disposition == WAIT, SyncFailure.resolved_at.is_(None)]
    if node_id is not None:
        base.append(SyncFailure.node_id == node_id)
    count = session.scalar(select(func.count()).select_from(SyncFailure).where(*base)) or 0
    if not count:
        return {"count": 0, "records": 0, "oldest_seq": None, "since": None}
    records = session.scalar(select(func.count(func.distinct(SyncFailure.waiting_for))).where(*base)) or 0
    oldest = session.scalar(select(func.min(SyncFailure.seq)).where(*base))
    since = session.scalar(select(func.min(SyncFailure.first_failed_at)).where(*base))
    return {"count": count, "records": records, "oldest_seq": oldest,
            "since": _aware(since).isoformat() if since else None}


# ---- retry ordering: a late retry never overwrites a later entry ------------------------------------------

#: The kinds that write each target. An entry of one of these kinds replaces its target's state (artwork and
#: achievements per record and source, a lock per record and field, a relation per (from, to, type) whether it
#: adds or ends it, the whole upstream schema), except `custom`, which upserts or clears single keys of one record
#: and source.
_TARGET_KINDS = {"artwork": ("artwork",), "achievements": ("achievements",), "lock": ("lock", "unlock"),
                 "unlock": ("lock", "unlock"), "schema": ("schema",), "custom": ("custom",),
                 "relation": ("relation",)}


def _target(kind: str, body: Any) -> tuple[Any, ...] | None:
    """What an entry writes, comparable between entries of one feed; None for kinds that do not replace state
    (an observation records its own supersession, merges are retried by `retry_merge`, detaches have their own
    idempotency rules)."""
    from .normalize import norm_namespace

    if kind not in _TARGET_KINDS or not isinstance(body, dict):
        return None
    if kind == "schema":
        return ("schema",)
    if kind == "relation":  # `add` and `end` of one (from, to, type) write the same relation
        ends = (body.get("from"), body.get("to"), body.get("type"))
        return ("relation", *ends) if all(isinstance(v, str) for v in ends) else None
    cid = body.get("canonical_id")
    if not isinstance(cid, str):
        return None
    if kind in ("lock", "unlock"):
        return ("lock", cid, body.get("field")) if isinstance(body.get("field"), str) else None
    source = body.get("source")
    if not isinstance(source, str):
        return None
    if kind == "custom":
        return ("custom", cid, source[:128])
    return (kind, cid, norm_namespace(source))


def _custom_keys(session: Session, body: dict[str, Any], media_type: str) -> set[str]:
    """The keys a `custom` entry sets or clears, as `_apply` resolves them (renamed keys followed, empty values
    dropped)."""
    from . import custom
    from .custom_compat import key_for

    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    try:
        keys = set(custom.clean_values(session, media_type, values, editable_only=False))
    except (AtlasError, TypeError, ValueError):
        keys = {key_for(session, media_type, str(k)) for k, v in values.items() if v not in (None, "")}
    clear = body.get("clear") if isinstance(body.get("clear"), list) else []
    return keys | {key_for(session, media_type, str(k)[:40]) for k in clear[:60]}


def superseding(session: Session, node_id: str, seq: int, kind: str, body: Any) -> tuple[int | None, Any]:
    """Before re-applying entry `seq` out of order: `(later_seq, None)` when a later entry of the same feed that
    has applied (at or below the applied head, no open failure) replaced everything this entry writes, otherwise
    `(None, body)` with the body to apply. For `custom` the body is reduced to the keys no later applied entry
    set or cleared. A later entry that has not applied (quarantined, skipped, parked, waiting) does not count:
    it is retried after this one, in feed order. Entries that do not classify as applicable are returned
    unchanged so their failure stays visible."""
    outcome = feed_schema.classify(kind, body)
    target = _target(kind, outcome.body) if outcome.action == APPLY else None
    if target is None:
        return None, body
    media_type = None
    if target[0] == "custom":
        game = session.get(Canonical, target[1])
        if game is None:
            return None, body
        media_type = game.media_type
        mine = _custom_keys(session, outcome.body, media_type)
        if not mine:
            return None, body
    later_rows = session.scalars(select(ReplicatedChange).where(
        ReplicatedChange.node_id == node_id, ReplicatedChange.seq > seq,
        ReplicatedChange.seq <= applied_head(session, node_id),
        ReplicatedChange.kind.in_(_TARGET_KINDS[kind])).order_by(ReplicatedChange.seq)
        .execution_options(yield_per=500))
    covered: set[str] = set()
    last = None
    for later in later_rows:
        failure = session.get(SyncFailure, (node_id, later.seq))
        if failure is not None and failure.resolved_at is None:
            continue  # it has not applied
        later_outcome = feed_schema.classify(later.kind, later.body)
        if later_outcome.action != APPLY or _target(later.kind, later_outcome.body) != target:
            continue
        if media_type is None:
            return later.seq, None
        covered |= _custom_keys(session, later_outcome.body, media_type) & mine
        last = later.seq
        if covered == mine:
            return last, None
    if not covered:
        return None, body
    residual = dict(body)
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    from .custom_compat import key_for
    residual["values"] = {k: v for k, v in values.items() if key_for(session, media_type, k) not in covered}
    if isinstance(body.get("clear"), list):
        residual["clear"] = [k for k in body["clear"] if key_for(session, media_type, str(k)[:40]) not in covered]
    log.info("retrying feed entry %s (custom) from %s without keys %s that later entries set", seq, node_id,
             sorted(covered))
    return None, residual


#: The kinds whose later entries a late merge re-applies on the merged records (they replace or upsert state that
#: the merge moves: locks, artwork, achievements, custom values, relations).
_MERGE_REPLAY_KINDS = ("artwork", "achievements", "lock", "unlock", "custom", "relation")


def _names(body: Any, records: set[str]) -> bool:
    """Whether a later entry writes state on one of `records` (a relation names two records)."""
    if not isinstance(body, dict):
        return False
    return bool({body.get("canonical_id"), body.get("from"), body.get("to")} & records)


def _later_applied(session: Session, node_id: str, seq: int, kinds: tuple[str, ...]) -> list[ReplicatedChange]:
    """Later entries of the feed, of `kinds`, that have applied: at or below the applied head, no open failure."""
    rows = session.scalars(select(ReplicatedChange).where(
        ReplicatedChange.node_id == node_id, ReplicatedChange.seq > seq,
        ReplicatedChange.seq <= applied_head(session, node_id),
        ReplicatedChange.kind.in_(kinds)).order_by(ReplicatedChange.seq)).all()
    out = []
    for later in rows:
        failure = session.get(SyncFailure, (node_id, later.seq))
        if failure is None or failure.resolved_at is not None:
            out.append(later)
    return out


def retry_merge(session: Session, ctx: Ctx, node_id: str, seq: int, body: Any) -> str:
    """Retry a quarantined merge out of order without regressing newer state.

    A merge moves the source record's locks, artwork, achievements and custom values onto the survivor. Run after
    later entries already applied to either record, it would overwrite what they set. So the merge is applied and
    then those later entries (`_MERGE_REPLAY_KINDS` on src or dst) are re-applied in feed order, all in one
    savepoint: the result is what in-order application gives. When a later applied merge involves either record,
    the order of the two merges decides what survives, so the entry is kept quarantined (`out_of_order`) for an
    administrator rather than reported resolved. If a replayed entry fails, everything is rolled back and the merge
    stays quarantined."""
    outcome = feed_schema.classify("merge", body)
    if outcome.action != APPLY or not isinstance(outcome.body, dict):
        return apply_entry(session, ctx, node_id, seq, "merge", body, may_park=False)
    src, dst = outcome.body.get("src"), outcome.body.get("dst")
    records = {r for r in (src, dst) if isinstance(r, str)}
    for record in (src, dst):
        if isinstance(record, str) and session.get(Canonical, record) is not None:
            records.add(service.follow(session, record))
    clashing = [later.seq for later in _later_applied(session, node_id, seq, ("merge",))
                if isinstance(later.body, dict) and {later.body.get("src"), later.body.get("dst")} & records]
    if clashing:
        log.warning("quarantined merge %s from %s stays quarantined: later merges %s of the same records already "
                    "applied, and merges are order dependent", seq, node_id, clashing)
        _record(session, node_id, seq, "merge", QUARANTINE, "out_of_order",
                f"later merges {clashing} of the same records already applied; re-applying this merge now could "
                "leave different state than the authority", {"kind": "merge", "later": clashing})
        return QUARANTINE
    replay = [later for later in _later_applied(session, node_id, seq, _MERGE_REPLAY_KINDS)
              if _names(later.body, records)]
    try:
        with session.begin_nested():
            result = apply_entry(session, ctx, node_id, seq, "merge", body, may_park=False)
            if result != APPLY or not replay:
                return result
            for later in replay:
                later_outcome = feed_schema.classify(later.kind, later.body)
                if later_outcome.action == APPLY:
                    with entry_at(later.created_at):  # each replayed entry keeps its own origin time (§12.1.6)
                        _apply(session, ctx, node_id, later.seq, later.kind, later_outcome.body)
            row = session.get(SyncFailure, (node_id, seq))
            if row is not None:
                row.detail = {**(row.detail or {}), "replayed": [later.seq for later in replay]}
    except (AtlasError, KeyError, TypeError, ValueError, AttributeError) as exc:
        log.warning("quarantined merge %s from %s stays quarantined: re-applying later entry failed: %s",
                    seq, node_id, exc)
        _record(session, node_id, seq, "merge", QUARANTINE, "apply_error",
                f"re-applying later entries after the merge failed: {type(exc).__name__}: {exc}", {"kind": "merge"})
        return QUARANTINE
    log.info("retried merge %s from %s and re-applied later entries %s", seq, node_id, [r.seq for r in replay])
    return APPLY


def _resolve_superseded(row: SyncFailure, later: int) -> None:
    log.info("quarantined feed entry %s (%s) from %s is superseded by entry %s, which already applied; "
             "not re-applying it", row.seq, row.kind, row.node_id, later)
    row.resolved_at = row.last_attempt_at = datetime.now(UTC)
    row.detail = {**(row.detail or {}), "superseded_by": later}


ABANDONED = "abandoned"


def abandon_waiting(session: Session, node_id: str) -> int:
    """C13 §5.2: removing a peer resolves its feed's open `wait` rows as `abandoned`, so they stop counting toward
    `sync_max_open_failures` and leave the waiting list. The retained entries stay; `atlas sync rescan` re-flags
    them if the feed is followed again."""
    now = datetime.now(UTC)
    rows = list(session.scalars(select(SyncFailure).where(
        SyncFailure.node_id == node_id, SyncFailure.disposition == WAIT, SyncFailure.resolved_at.is_(None))))
    for row in rows:
        row.reason, row.resolved_at = ABANDONED, now
    session.flush()
    return len(rows)


def open_failures(session: Session, disposition: str | None = None) -> int:
    query = select(func.count()).select_from(SyncFailure).where(SyncFailure.resolved_at.is_(None))
    if disposition is not None:
        query = query.where(SyncFailure.disposition == disposition)
    return session.scalar(query) or 0


def _not_found(exc: AtlasError) -> bool:
    return getattr(exc, "code", "") == "not_found"


def _apply(session: Session, ctx: Ctx, node_id: str, seq: int, kind: str, body: dict[str, Any]) -> str | None:
    # Version bumps are `replicated` client changes with the upstream origin (an inner merge or detach
    # keeps the origin). The local client log is never relayed: relay serves `replicated_change` only.
    with client_changes.cause(session, "replicated", (node_id, seq)):
        return _apply_body(session, ctx, node_id, seq, kind, body)


def _apply_body(session: Session, ctx: Ctx, node_id: str, seq: int, kind: str, body: dict[str, Any]) -> str | None:
    """Apply a classified entry. Returns "noop" for an idempotent replay; raises on anything it cannot do."""
    if kind == "obs":
        cid = body["canonical_id"]
        o = body["observation"]
        if not isinstance(cid, str) or not cid.startswith("atlas:") or len(cid) > 40:
            raise InvalidEntry("canonical_id is not an Atlas id")
        if not _valid_observation(o):
            raise InvalidEntry("observation fails the observation contract")
        _ensure_canonical(session, cid, body.get("media_type"))
        if session.get(Observation, o["id"]) is not None:
            return NOOP  # idempotent replay
        # C13 R5: a feed supersedes only its own observations (and this node's local ones). Another feed's
        # observation of the same source record stays current: one authority cannot retire what another signed.
        priors = [p for p in session.scalars(
            select(Observation).where(
                Observation.source == o["source"], Observation.source_scope == o["source_scope"],
                Observation.external_id == o["external_id"], Observation.is_current.is_(True),
            )
        ) if p.origin_node is None or p.origin_node == node_id]
        # A later entry of the same feed already superseded this observation (an in-order replay of
        # retained history, `replay --historical`): store it as history and leave the newer one current.
        superseded = any(p.origin_node == node_id and p.origin_seq is not None and p.origin_seq > seq for p in priors)
        if not superseded:
            for p in priors:
                p.is_current = False
        obs = Observation(
            id=o["id"], canonical_id=cid, source_kind=o["source_kind"], source=o["source"],
            source_scope=o["source_scope"], external_id=o["external_id"], payload=o["payload"],
            source_updated_at=o.get("source_updated_at"),
            observed_at=datetime.fromisoformat(o["observed_at"]), redistributable=True,
            origin_node=node_id, origin_seq=seq, is_current=not superseded,
        )
        session.add(obs)
        session.flush()
        touched = {cid}
        for key in keys_of(obs):
            row = session.get(Crosswalk, key)
            if row is None:
                session.add(Crosswalk(namespace=key[0], external_id=key[1], canonical_id=cid))
            elif row.canonical_id != cid:
                other = service.follow(session, row.canonical_id)
                if other != cid and not _is_upstream_origin(session, other):
                    if matching.active_rejection(session, other, cid) is not None:
                        # C11 §11.6: a curator here said these are different works. Both stay; the key stays on
                        # the local record, and no review pairs them again (§11.5 rule 2).
                        log.info("not folding %s into %s: marked as different works", other, cid)
                    else:
                        # a purely local record now has an authoritative twin: fold it in
                        service.merge_canonicals(session, ctx, other, cid, actor="sync", log=False, cause="sync",
                                                 origin=(node_id, seq))
                # else: both are authoritative; the authority left the key where it was (and opened a
                # review), so the mirror does too.
        session.flush()
        for t in touched:
            if session.get(Canonical, t) and session.get(Canonical, t).merged_into is None:  # type: ignore[union-attr]
                service.rebuild(session, ctx, t)
    elif kind in ("artwork", "achievements"):
        from . import facets

        # A missing record or an invalid item raises: quarantined and retried, never silently dropped.
        cid = body.get("canonical_id")
        late = _late_merge(session, node_id, seq, cid) if kind == "achievements" else None
        if kind == "achievements" and (late is not None or (
                isinstance(cid, str) and _later_merges_into(session, node_id, seq, cid))):
            # `replay --historical`: the list ran before a merge this feed made later
            from .merge import source_class

            at = _ENTRY_AT.get()
            _achievements_as_they_ran(session, ctx, node_id, seq, body, late=late,
                                      curated=source_class(body["source"]) in ("curator", "community"),
                                      written_on=at.astimezone(UTC).date() if at is not None else None)
        elif kind == "artwork":
            facets.set_artwork(session, ctx, body["canonical_id"], body["source"], body["items"],
                               redistributable=True, log=False)
            _note_media_origin(session, node_id, body)
        else:  # the authority's stored keys win, so mirror and authority publish the same keys (C2.2)
            from .merge import source_class

            # a curated list was arranged by hand: its order is its position here too (C12 §12.1.3, QV4)
            curated = source_class(body["source"]) in ("curator", "community")
            # a percent without a date is dated by the entry's origin timestamp, as on the authority (§12.1.6)
            at = _ENTRY_AT.get()
            facets.set_achievements(session, ctx, body["canonical_id"], body["source"], body["items"],
                                    redistributable=True, log=False, adopt_keys=True, ordered=curated,
                                    written_on=at.astimezone(UTC).date() if at is not None else None)
    elif kind == "schema":
        from . import custom

        if not ctx.settings.inherit_schema:
            return NOOP  # this node chose not to adopt the upstream schema
        custom.apply_snapshot(session, body, node_id=node_id)
    elif kind == "custom":
        from . import custom
        from .custom_compat import key_for

        game = session.get(Canonical, body["canonical_id"])
        if game is None:
            raise AtlasError("unknown canonical", "not_found")
        values = custom.clean_values(session, game.media_type, body.get("values") or {}, editable_only=False)
        clear = tuple(key_for(session, game.media_type, str(k)[:40]) for k in (body.get("clear") or [])[:60])
        custom.set_values(session, ctx, body["canonical_id"], str(body["source"])[:128], values,
                          redistributable=True, log=False, clear=clear)
    elif kind == "merge":
        src, dst = body["src"], body["dst"]
        s, d = session.get(Canonical, src), session.get(Canonical, dst)
        if s is None or d is None:
            raise AtlasError("merge of an unknown record", "not_found")
        if s.merged_into and service.follow(session, src) == service.follow(session, dst):
            return NOOP  # already merged
        if s.merged_into or d.merged_into:
            raise AtlasError("merge of a record that was merged elsewhere", "conflict")
        service.merge_canonicals(session, ctx, src, dst, actor="sync", log=False, cause="sync", origin=(node_id, seq))
    elif kind == "record":
        return _apply_record(session, ctx, node_id, seq, body)
    elif kind == "detach":
        obs = session.get(Observation, body["observation_id"])
        if obs is None:
            # C13 §5.2: the authority never signed that observation (a provider fetch). There is nothing to move
            # here; the new record arrives as its own `record` entry right after this one.
            log.info("detach %s from %s: observation %s was never federated", seq, node_id, body["observation_id"])
            return NOOP
        if obs.canonical_id == body["new_canonical_id"]:
            return NOOP  # already detached
        if session.get(Canonical, body["new_canonical_id"]) is not None:
            # The detach created this record, so it already ran; the observation may have moved on since
            # (a later merge or detach). Re-running it would undo that history.
            return NOOP
        service.detach_observation(session, ctx, body["observation_id"], new_id=body["new_canonical_id"], log=False,
                                   actor="sync")
    elif kind in ("lock", "unlock") and (late := _late_merge(session, node_id, seq, body.get("canonical_id"))):
        return _lock_through_merge(session, ctx, node_id, late, kind, body)
    elif kind == "unlock" and _lock_carried_later(session, node_id, seq, body["canonical_id"], body["field"]):
        return NOOP  # the lock there now came with a later merge: it was not there when this unlock ran
    elif kind == "lock":
        existing = session.get(Lock, (body["canonical_id"], body["field"]))
        if existing is not None and existing.local_only:
            return NOOP  # this node's own pin wins
        service.set_lock(session, ctx, body["canonical_id"], body["field"], body["value"], actor=node_id, log=False)
    elif kind == "unlock":
        try:
            service.clear_lock(session, ctx, body["canonical_id"], body["field"], log=False)
        except AtlasError as exc:
            if _not_found(exc) and session.get(Lock, (body["canonical_id"], body["field"])) is None:
                return NOOP  # no such lock: already in the state the entry describes
            raise
    elif kind == "relation":
        from . import relations

        try:
            return relations.apply_feed(session, ctx, node_id, seq, body)
        except ValueError as exc:  # self, cycle, second parent, wrong target: quarantine:invalid (C6.3)
            raise InvalidEntry(str(exc)) from exc
    elif kind == "key_rotation":
        return None  # Verified with the complete key history before any entry is applied.
    else:  # classify() parks unknown kinds; reaching here means the registry and this function disagree
        raise KeyError(f"no apply rule for feed kind {kind!r}")
    return None


def _note_media_origin(session: Session, node_id: str, body: dict[str, Any]) -> None:
    """C13 §6.1: the rows an `artwork` entry listed came from `node_id`'s feed, so their bytes can be asked of the
    peer that serves it. Only the content address is taken from the URL (never anything else)."""
    from . import media
    from .models import Artwork

    urls = {i.get("url") for i in body.get("items") or [] if isinstance(i, dict) and isinstance(i.get("url"), str)}
    if not urls:
        return
    for art in session.scalars(select(Artwork).where(Artwork.canonical_id == body["canonical_id"],
                                                     Artwork.url.in_(sorted(urls)[:200]))):
        art.origin_node = node_id[:64]
        if art.peer_digest is None and (digest := media.url_digest(art.url)):
            art.peer_digest, art.digest_provenance = digest, "url"
    session.flush()


def _apply_record(session: Session, ctx: Ctx, node_id: str, seq: int, body: dict[str, Any]) -> str | None:
    """C13 §5.1: a `record` creates the record (or finds it) with its identity observation: source `atlas-record`,
    scoped to the signing feed, with the id the authority stored (`feed.record_obs_id`), the display title and
    the store ids. It ranks below every provider. Its store ids fold a purely local twin in, as an observation's
    do. Applied again (or after the bridge observation of the same record) it changes nothing."""
    from . import feed

    cid = body.get("canonical_id")
    if not isinstance(cid, str) or not cid.startswith("atlas:") or len(cid) > 40:
        raise InvalidEntry("canonical_id is not an Atlas id")
    row = session.get(Canonical, cid)
    if row is not None and row.merged_into is not None:
        return NOOP  # merged since: the survivor carries what it introduced
    oid = feed.record_obs_id(node_id, cid)
    if session.get(Observation, oid) is not None:
        return NOOP
    payload: dict[str, Any] = {}
    title = body.get("title")
    if isinstance(title, str) and title.strip():
        payload["title"] = " ".join(title.split())[:feed_schema.RECORD_TITLE_MAX]
    if ids := feed.strip_ids(body.get("provider_ids")):
        payload["provider_ids"] = ids
    at = _ENTRY_AT.get() or datetime.now(UTC)
    synthetic = {"canonical_id": cid, "media_type": body.get("media_type"),
                 "observation": {"id": oid, "source_kind": "metadata", "source": feed_schema.RECORD_SOURCE,
                                 "source_scope": node_id, "external_id": cid, "payload": payload,
                                 "source_updated_at": None, "observed_at": at.isoformat()}}
    result = _apply_body(session, ctx, node_id, seq, "obs", synthetic)
    _review_cross_origin(session, ctx, node_id, cid, payload.get("provider_ids") or {})
    if result != NOOP:  # §5.1 step 4 (H56, off unless the operator turns it on): fetch it from this node's providers
        from . import enrich

        enrich.queue(session, ctx.settings, node_id, cid, payload.get("provider_ids") or {})
    return result


def _origins(session: Session, cid: str) -> set[str]:
    return set(session.scalars(select(Observation.origin_node).where(
        Observation.canonical_id == cid, Observation.origin_node.is_not(None)).distinct()))


def _review_cross_origin(session: Session, ctx: Ctx, node_id: str, cid: str, ids: dict[str, Any]) -> None:
    """C13 §5.1 step 3, second half: a store id of this `record` already belongs to a record another upstream
    introduced. Never merged here: a `cross_origin_duplicate` review item names both, with the shared keys as
    evidence (one open item per pair)."""
    others: dict[str, list[dict[str, str]]] = {}
    for ns, ext in sorted(ids.items())[:32]:
        row = session.get(Crosswalk, (ns, str(ext)))
        if row is None:
            continue
        other = service.follow(session, row.canonical_id)
        if other == cid or service.follow(session, cid) == other:
            continue
        origins = _origins(session, other)
        if origins and node_id not in origins:
            others.setdefault(other, []).append({"namespace": ns, "external_id": str(ext)[:256]})
    for other, keys in others.items():
        matching.open_review(session, ctx.settings, "cross_origin_duplicate", cid, [(other, 1.0)], 1.0,
                             reason="cross_origin_duplicate",
                             evidence=[{"kind": "shared_store_id", **k} for k in keys])


# ---- entries on a record this feed merged later (`replay --historical`) -------------------------------------

def _late_merge(session: Session, node_id: str, seq: int, cid: Any) -> MergeRecord | None:
    """The live merge of `cid` this node applied from `node_id`'s own feed at a sequence after `seq`.

    The entry at `seq` ran on `cid` while it was live on the authority, and that later merge carried the result to
    the survivor. A follower that dropped the entry (a 1.0 release, Phase 4 D1) moved nothing, so a historical
    replay must land the entry where the authority's merge put it. None when `cid` is live, or was merged here in
    some other way (that case is not this feed's history, and the entry is refused as before)."""
    if not isinstance(cid, str):
        return None
    row = session.get(Canonical, cid)
    if row is None or row.merged_into is None:
        return None
    rec = session.scalars(select(MergeRecord).where(MergeRecord.src == cid, MergeRecord.reverted_at.is_(None))
                          .order_by(MergeRecord.created_at.desc())).first()
    if rec is None or rec.origin_node != node_id or rec.origin_seq is None or rec.origin_seq <= seq:
        return None
    return rec


def _merge_items(session: Session, rec: MergeRecord, *kinds: str) -> dict[tuple[str, str], MergeRecordItem]:
    return {(i.kind, i.ref): i for i in session.scalars(select(MergeRecordItem).where(
        MergeRecordItem.merge_id == rec.id, MergeRecordItem.kind.in_(kinds)))}


def _add_merge_item(session: Session, rec: MergeRecord, kind: str, ref: str, data: Any, hashed: str | None) -> None:
    last = session.scalar(select(func.max(MergeRecordItem.n)).where(MergeRecordItem.merge_id == rec.id))
    session.add(MergeRecordItem(merge_id=rec.id, n=(-1 if last is None else last) + 1, kind=kind, ref=ref[:900],
                                data=data, data_hash=hashed))
    rec.item_count = (rec.item_count or 0) + 1
    session.flush()


def _truncated(rec: MergeRecord) -> None:
    if rec.truncated:  # a large sync merge kept identity items only: what it carried is not known here
        raise AtlasError(f"this entry ran on {rec.src}, merged since into {rec.dst} by a merge whose snapshot was "
                         "truncated, so where it would have landed is not known here", "merged_since")


def _later_merges_into(session: Session, node_id: str, seq: int, cid: str) -> list[MergeRecord]:
    """Live merges into `cid` this node applied from `node_id`'s feed after `seq`, oldest first: what they carried
    was not on `cid` when the entry at `seq` ran on the authority."""
    return list(session.scalars(select(MergeRecord).where(
        MergeRecord.dst == cid, MergeRecord.reverted_at.is_(None), MergeRecord.origin_node == node_id,
        MergeRecord.origin_seq > seq).order_by(MergeRecord.origin_seq)))


def _carried_ids(session: Session, recs: list[MergeRecord]) -> set[str]:
    return {ref for m in recs for _, ref in _merge_items(session, m, "ach")}


def _carried(session: Session, rec: MergeRecord, source: str) -> list[Achievement]:
    ids = sorted(ref for _, ref in _merge_items(session, rec, "ach"))
    if not ids:
        return []
    return list(session.scalars(select(Achievement).where(Achievement.id.in_(ids), Achievement.source == source)))


def _achievements_as_they_ran(session: Session, ctx: Ctx, node_id: str, seq: int, body: dict[str, Any], *,
                              late: MergeRecord | None, curated: bool, written_on: Any) -> None:
    """Replay a list exactly as it ran on the authority (`replay --historical` over merges this feed made later).

    Rows a later merge carried onto the record are set aside while the list reconciles, then carried in again by
    the merge's own rules (`facets.move_facets`: the record's duplicates win). When the record itself was merged
    later (`late`), the rows that merge carried for this source come back to it for the duration, so the list
    reconciles in place against them (ids and keys kept), and then move on to the survivor again. Rows the replay
    adds there are recorded on the merge record as carried, so a later replay and an unmerge treat them as the
    merge's own."""
    from . import facets, merges
    from .normalize import norm_namespace

    cid, source = body["canonical_id"], norm_namespace(body["source"])
    later = _later_merges_into(session, node_id, seq, cid)
    for m in [*later, *([late] if late is not None else [])]:
        _truncated(m)
    record = session.get(Canonical, cid)
    merged_into = record.merged_into  # type: ignore[union-attr]
    if late is not None:  # first: what `late` carried includes what the later merges had carried onto `cid`
        for r in _carried(session, late, source):
            r.canonical_id = cid
        record.merged_into = None  # type: ignore[union-attr]
    for m in later:
        for r in _carried(session, m, source):
            r.canonical_id = m.src  # set aside on the record it came from (merged: nothing else lives there)
    session.flush()
    try:
        facets.set_achievements(session, ctx, cid, source, body["items"], redistributable=True, log=False,
                                adopt_keys=True, ordered=curated, written_on=written_on)
    finally:
        record.merged_into = merged_into  # type: ignore[union-attr]
        session.flush()
    for m in later:  # carried in again, as the merge did
        facets.move_facets(session, m.src, cid, ctx.settings)
    if late is None:
        return
    keys = {r.id: r.key for r in session.scalars(select(Achievement).where(Achievement.canonical_id == cid))}
    survivor = service.follow(session, cid)
    facets.move_facets(session, cid, survivor, ctx.settings)
    items = _merge_items(session, late, "ach")
    for aid, key in keys.items():
        row = session.get(Achievement, aid)
        if row is None:
            continue  # a duplicate of the survivor's own row: the survivor's stays, as the merge decided
        hashed = merges.data_hash(merges._ach_data(row))
        item = items.get(("ach", aid))
        if item is None:
            _add_merge_item(session, late, "ach", aid, {"key": key}, hashed)
        else:
            item.data_hash = hashed  # what the merge would have carried, not an edit made on the survivor
    service.touch_version(session, survivor, cause="local")


def _lock_carried_later(session: Session, node_id: str, seq: int, cid: Any, field: str) -> bool:
    """Whether a merge this feed made after `seq` carried a lock on `field` onto `cid`."""
    if not isinstance(cid, str):
        return False
    return any(("lock", field) in _merge_items(session, m, "lock")
               for m in _later_merges_into(session, node_id, seq, cid))


def _lock_through_merge(session: Session, ctx: Ctx, node_id: str, rec: MergeRecord, kind: str,
                        body: dict[str, Any]) -> str | None:
    """Replay a lock or unlock as it ran on the authority: on the merged record before the merge. The merge then
    moved the lock to the survivor unless the survivor had a lock on that field (its own wins: `lock_dropped`)."""
    from . import merges

    _truncated(rec)
    field = body["field"]
    survivor = service.follow(session, rec.src)
    items = _merge_items(session, rec, "lock", "lock_dropped")
    moved, dropped = items.get(("lock", field)), items.get(("lock_dropped", field))
    lock = session.get(Lock, (survivor, field))
    if dropped is not None or (lock is not None and (lock.local_only or lock.cleared)):
        return NOOP  # the survivor's own lock, or this node's own pin or clear, wins
    if kind == "unlock":
        if moved is None or lock is None:
            return NOOP  # the merge carried no lock for this field
        session.delete(lock)
        session.flush()
        service.rebuild(session, ctx, survivor, cause="local")
        return None
    if lock is not None and moved is None:
        return NOOP  # the survivor had its own lock at merge time: the merge kept it
    value = service.clean_lock(field, body["value"], replicated=True)
    if lock is None:
        lock = Lock(canonical_id=survivor, field=field, value=value, set_by=node_id, local_only=False, cleared=False)
        session.add(lock)
    else:
        lock.value, lock.set_by = value, node_id
    session.flush()
    hashed = merges.data_hash(merges._lock_data(lock))
    if moved is None:
        _add_merge_item(session, rec, "lock", field, None, hashed)
    else:
        moved.data_hash = hashed
    service.rebuild(session, ctx, survivor, cause="local")
    return None


# ---- heads --------------------------------------------------------------------------------------------------

def _state(session: Session, key: str) -> str | None:
    row = session.get(NodeState, key)
    return row.value if row else None


def _set_state(session: Session, key: str, value: str) -> None:
    row = session.get(NodeState, key)
    if row is None:
        session.add(NodeState(key=key, value=value))
    else:
        row.value = value


def applied_head(session: Session, node_id: str) -> int:
    cursor = session.get(SyncCursor, node_id)
    return cursor.last_seq if cursor else 0


def verified_head(session: Session, node_id: str) -> int:
    """Retained entries are contiguous from 1 and each was verified before it was stored."""
    return session.scalar(select(func.max(ReplicatedChange.seq)).where(ReplicatedChange.node_id == node_id)) or 0


def _int_state(session: Session, key: str) -> int | None:
    value = _state(session, key)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _open_for(session: Session, node_id: str) -> int:
    return session.scalar(select(func.count()).select_from(SyncFailure).where(
        SyncFailure.node_id == node_id, SyncFailure.resolved_at.is_(None),
        SyncFailure.disposition.in_((QUARANTINE, WAIT)))) or 0


def _blocking(session: Session, node_id: str, applied: int) -> SyncFailure | None:
    row = session.get(SyncFailure, (node_id, applied + 1))
    return row if row is not None and row.resolved_at is None and row.disposition == PARK else None


def upstream_status(session: Session, ctx_or_settings: Any, node_id: str) -> dict[str, Any]:
    """The named heads of one upstream feed (C1.7). Invariant: applied <= verified <= downloaded <= observed."""
    settings = getattr(ctx_or_settings, "settings", ctx_or_settings)
    applied, verified = applied_head(session, node_id), verified_head(session, node_id)
    downloaded = max(_int_state(session, "sync_downloaded:" + node_id) or 0, verified)
    claimed = _int_state(session, "sync_head:" + node_id)
    observed = max(claimed or 0, downloaded)
    parked_rows = list(session.scalars(select(SyncFailure).where(
        SyncFailure.node_id == node_id, SyncFailure.disposition == PARK, SyncFailure.resolved_at.is_(None))
        .order_by(SyncFailure.seq)))
    blocking = _blocking(session, node_id, applied)
    held_back = verified - applied if blocking is not None else 0
    cap = getattr(settings, "sync_max_parked_backlog", 50_000)
    parked = None
    if parked_rows:
        first = parked_rows[0]
        historical = sum(1 for r in parked_rows if r.seq <= applied)
        parked = {"first_seq": first.seq, "count": held_back + historical,
                  "reasons": dict(Counter(r.reason for r in parked_rows)),
                  "first_reason": first.reason, "first_kind": first.kind, "first_error": first.error,
                  "blocking": blocking is not None, "backlog_full": blocking is not None and held_back >= cap}
    quarantined_count = session.scalar(select(func.count()).select_from(SyncFailure).where(
        SyncFailure.node_id == node_id, SyncFailure.disposition == QUARANTINE, SyncFailure.resolved_at.is_(None))) or 0
    oldest = session.scalar(select(func.min(SyncFailure.seq)).where(
        SyncFailure.node_id == node_id, SyncFailure.disposition == QUARANTINE, SyncFailure.resolved_at.is_(None)))
    refused_raw = _state(session, "sync_refused:" + node_id)
    try:
        refused = json.loads(refused_raw) if refused_raw else None
    except ValueError:
        refused = {"reason": refused_raw}
    upstream_protocol = _state(session, "sync_protocol:" + node_id) or (refused or {}).get("upstream_protocol")
    rewound_raw = _state(session, "sync_rewound:" + node_id)
    try:
        rewound = json.loads(rewound_raw) if rewound_raw else None
    except ValueError:
        rewound = {"reason": rewound_raw}
    waiting = open_waiting(session, node_id)
    bound = getattr(settings, "sync_max_open_failures", 10_000)
    failures_full = applied < verified and quarantined_count + waiting["count"] >= bound
    if refused:
        state = "refused"
    elif rewound:
        state = "rewound"  # the authority's history no longer matches what this node verified
    elif (parked and parked["backlog_full"]) or failures_full:
        state = "backlog_full"
    elif parked:
        state = "parked"
    elif claimed is None:
        state = "unknown"  # the upstream never told us its head: never claim to be synchronized
    elif claimed > verified or applied < verified:
        state = "behind"
    elif quarantined_count:
        state = "degraded"
    elif waiting["count"]:
        state = "waiting"  # caught up; some entries wait for records this feed has not introduced (C13 §5.5)
    else:
        state = "in_sync"
    return {"observed_head": observed, "downloaded_head": downloaded, "verified_head": verified,
            "applied_head": applied, "parked": parked,
            "quarantined": {"count": quarantined_count, "oldest_seq": oldest}, "waiting": waiting,
            "refused": refused, "rewound": rewound, "upstream_protocol": upstream_protocol,
            "own_protocol": PROTOCOL,
            "upstream_behind": claimed is not None and claimed < verified,
            "state": state, "in_sync": state == "in_sync"}


def upstream_nodes(session: Session) -> list[str]:
    nodes = set(session.scalars(select(SyncCursor.upstream))) | set(session.scalars(select(ReplicatedFeed.node_id)))
    nodes |= set(session.scalars(select(Peer.node_id)))
    for prefix in ("sync_head:", "sync_refused:", "sync_rewound:"):
        nodes |= {k.removeprefix(prefix) for k in session.scalars(select(NodeState.key)
                                                                  .where(NodeState.key.like(prefix + "%")))}
    return sorted(nodes)


def status(session: Session, ctx_or_settings: Any) -> dict[str, Any]:
    upstreams = {n: upstream_status(session, ctx_or_settings, n) for n in upstream_nodes(session)}
    settings = getattr(ctx_or_settings, "settings", ctx_or_settings)
    return {"upstreams": upstreams, "peers": [peer_summary(session, p, upstreams.get(p.node_id), settings)
                                              for p in session.scalars(select(Peer).order_by(Peer.created_at))]}


#: C13 §7.5: one honest status per peer, in the order they are decided.
PEER_STATUSES = ("paused", "not_contacted", "needs_you", "unreachable", "empty", "catching_up",
                 "caught_up_waiting", "caught_up")
_TRANSPORT_ERRORS = ("ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ReadError",
                     "WriteError", "RemoteProtocolError", "HTTPStatusError", "NetworkError", "TimeoutException")


#: C13 §5.5: a peer is "Can't reach it" only once transport errors have lasted this many sync intervals without a
#: success. A single failed attempt after a recent success keeps the feed's own status, with the error on the line
#: under it, so one dropped connection does not turn a healthy peer red.
UNREACHABLE_INTERVALS = 3


def _transport_error(error: str | None) -> bool:
    return bool(error) and error.startswith(_TRANSPORT_ERRORS)  # type: ignore[union-attr]


def _down_long(peer: Peer, interval: int | None, now: datetime | None) -> bool:
    """No success for `UNREACHABLE_INTERVALS` sync intervals (never reached counts as long)."""
    if peer.last_ok_at is None or not interval:
        return True
    now = now or datetime.now(UTC)
    return now - _aware(peer.last_ok_at) >= timedelta(seconds=UNREACHABLE_INTERVALS * interval)  # type: ignore[operator]


def peer_status(peer: Peer, heads: dict[str, Any] | None, *, interval: int | None = None,
                now: datetime | None = None) -> str:
    """`needs_you`: something only the operator can resolve (refused, rewound, parked behind an upgrade, a full
    backlog). Entries that wait or failed to apply leave a caught-up feed `caught_up_waiting`, with a line that
    names them (C13 §5.5: `degraded` maps there). `empty`: contacted, but the feed it follows holds nothing (a
    mirror's own feed). `unreachable`: transport errors and no success for 3 × `interval` seconds (the sync
    interval); without an interval any transport error counts, as before."""
    if not peer.enabled:
        return "paused"
    if peer.last_ok_at is None and peer.last_error is None and not heads:
        return "not_contacted"
    if heads and heads["state"] in ("refused", "rewound", "backlog_full", "parked"):
        return "needs_you"
    if peer.last_error:  # a transport failure is the network's; anything else (a refused key, a moved node) is yours
        if not _transport_error(peer.last_error):
            return "needs_you"
        if _down_long(peer, interval, now):
            return "unreachable"
    if not heads or heads["state"] == "unknown":
        return "not_contacted" if peer.last_ok_at is None else "catching_up"
    if heads["verified_head"] == 0 and heads["observed_head"] == 0:
        return "empty"
    if heads["state"] == "behind":
        return "catching_up"
    if heads["waiting"]["count"] or heads["quarantined"]["count"]:
        return "caught_up_waiting"  # C13 §5.5: `degraded` is caught up with a line naming what did not apply
    return "caught_up"


#: C13 §5.5 / §8.2: the generic badge text and tone of each status code. `status_label` gives the exact text with
#: the counts ("Copying: 11 of 23", "Caught up · 4 changes waiting for their game").
PEER_STATUS_LABELS = {"paused": ("Paused", "warn"), "not_contacted": ("Not contacted yet", "warn"),
                      "needs_you": ("Blocked: needs you", "bad"), "unreachable": ("Can't reach it", "bad"),
                      "empty": ("Connected · nothing published", "warn"), "catching_up": ("Copying", "warn"),
                      "caught_up_waiting": ("Caught up · changes waiting for their game", "warn"),
                      "caught_up": ("Caught up", "ok")}
_STATUS_ACTIONS = {"paused": "resume", "not_contacted": "sync_now", "needs_you": "see_details",
                   "unreachable": "check_address", "empty": "choose_again", "catching_up": "sync_now",
                   "caught_up_waiting": "see_waiting", "caught_up": None}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def status_label(code: str, heads: dict[str, Any] | None) -> str:
    """The badge text of C13 §5.5, with the counts it names."""
    if heads and code == "catching_up" and heads["observed_head"]:
        return f"Copying: {heads['applied_head']} of {heads['observed_head']}"
    if heads and code == "caught_up_waiting":
        if heads["waiting"]["count"]:
            return f"Caught up · {_plural(heads['waiting']['count'], 'change')} waiting for their game"
        return f"Caught up · {_plural(heads['quarantined']['count'], 'change')} not applied"
    return PEER_STATUS_LABELS[code][0]


def _ago(at: datetime, now: datetime) -> str:
    seconds = max(0, int((now - at).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{seconds // 60} min ago"
    if seconds < 36 * 3600:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def upstream_records(session: Session, node_id: str) -> dict[str, int] | None:
    """What the feed's own node last said about its records (`/sync/info.records`, C13 §3.4; unsigned, display
    only). None for an older upstream, which does not say, or a feed followed through a relay."""
    raw = _state(session, "sync_records:" + node_id)
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def status_detail(code: str, heads: dict[str, Any] | None, *, peer: Peer | None = None,
                  records: dict[str, int] | None = None, interval: int | None = None,
                  now: datetime | None = None) -> str | None:
    """The line under the badge (C13 §5.5): what is waiting or failing, how far the copy is, and what the upstream
    says it does not publish, in words."""
    now = now or datetime.now(UTC)
    name = peer.name if peer is not None and peer.name else "The upstream"
    parts = []
    last_ok = _aware(peer.last_ok_at) if peer is not None and peer.last_ok_at else None
    if code == "unreachable" and peer is not None:
        parts.append((f"Last success {_ago(last_ok, now)}" if last_ok else "Never reached") + " · "
                     + str(peer.last_error or "")[:160])
    elif peer is not None and _transport_error(peer.last_error):
        parts.append("Last attempt failed: " + str(peer.last_error)[:160])
    elif code == "needs_you" and peer is not None and peer.last_error:
        parts.append(str(peer.last_error)[:200])
    if heads:
        if code == "empty":
            parts.append("This feed has no entries.")
        if code == "catching_up" and heads["observed_head"] > heads["applied_head"]:
            left = heads["observed_head"] - heads["applied_head"]
            parts.append(f"{left} entr{'y' if left == 1 else 'ies'} to go")
        if heads["waiting"]["count"]:
            records_waiting = heads["waiting"].get("records") or 0
            parts.append(f"{_plural(records_waiting, 'game')} {'has' if records_waiting == 1 else 'have'}n't "
                         f"arrived from {name if name != 'The upstream' else 'the upstream'} yet")
        if heads["quarantined"]["count"]:
            parts.append(_plural(heads["quarantined"]["count"], "change") + " could not be applied")
        if code == "caught_up":
            head = heads["applied_head"]
            parts.append(f"{head} of {heads['observed_head'] or head} entries"
                         + (f" · checked {_ago(last_ok, now)}" if last_ok else ""))
        if code in ("caught_up", "caught_up_waiting"):
            if records is not None and type(records.get("live")) is int and type(records.get("introduced")) is int:
                parts.append(f"{records['introduced']} of {records['live']} records published")
                if records.get("not_federated"):
                    parts.append(f"{name} has {_plural(records['not_federated'], 'record')} it does not publish")
            elif peer is not None and (peer.follow or FOLLOW_OWN) == FOLLOW_OWN:
                parts.append("record count unknown (this upstream does not report it)")
    return " · ".join(parts) or None


def status_object(code: str, heads: dict[str, Any] | None, **detail: Any) -> dict[str, Any]:
    """C13 §8.2: `peers[].status` as `{code, label, tone, detail, action}`."""
    tone = PEER_STATUS_LABELS[code][1]
    records = detail.get("records")
    if code == "caught_up" and records and records.get("not_federated"):
        tone = "warn"  # §5.5: the upstream keeps records off its feed
    return {"code": code, "label": status_label(code, heads), "tone": tone,
            "detail": status_detail(code, heads, **detail), "action": _STATUS_ACTIONS[code]}


def describe_peer(session: Session, settings: Any, peer: Peer, heads: dict[str, Any] | None, *,
                  now: datetime | None = None) -> dict[str, Any]:
    """The status object of one peer, as Admin → Peers and `/sync/status` show it."""
    interval = getattr(settings, "sync_interval_seconds", None)
    code = peer_status(peer, heads, interval=interval, now=now)
    return status_object(code, heads, peer=peer, records=upstream_records(session, peer.node_id),
                         interval=interval, now=now)


def peer_summary(session: Session, peer: Peer, heads: dict[str, Any] | None, settings: Any = None) -> dict[str, Any]:
    inferred = inferred_follow(session, peer.id) if peer.follow is None else None
    return {"id": peer.id, "name": peer.name, "url": peer.url, "node_id": peer.node_id, "enabled": peer.enabled,
            "follow": peer.follow, "via_node_id": peer.via_node_id,
            "inferred_follow": (inferred or {}).get("follow"),
            "status": describe_peer(session, settings, peer, heads),
            "waiting": waiting_groups(session, peer.node_id, name=peer.name),
            "last_ok_at": _aware(peer.last_ok_at).isoformat() if peer.last_ok_at else None,
            "last_error": peer.last_error, "remote_protocol": peer.remote_protocol}


UPSTREAM_UNPUBLISHED, UPSTREAM_DEFECT, BLOCKED_BY_ENTRY = "upstream_unpublished", "upstream_defect", "blocked_by_entry"
_INTRODUCES = {"obs": "canonical_id", "record": "canonical_id", "detach": "new_canonical_id"}
WAITING_GROUPS_SHOWN = 50


def _blocked_by(session: Session, node_id: str, cid: str, first_seq: int) -> int | None:
    """C13 §5.2 `blocked_by_entry`: a lower entry of the same feed that would introduce `cid` is itself parked or
    quarantined (for example a `record` parked on a release too old for it). Its seq, or None."""
    rows = session.scalars(select(SyncFailure).where(
        SyncFailure.node_id == node_id, SyncFailure.resolved_at.is_(None), SyncFailure.seq < first_seq,
        SyncFailure.disposition.in_((PARK, QUARANTINE)), SyncFailure.kind.in_(tuple(_INTRODUCES)))
        .order_by(SyncFailure.seq).limit(50))
    for row in rows:
        entry = session.get(ReplicatedChange, (node_id, row.seq))
        if entry is not None and isinstance(entry.body, dict) and entry.body.get(_INTRODUCES[entry.kind]) == cid:
            return row.seq
    return None


def _group_title(session: Session, node_id: str, cid: Any, blocked: int | None) -> tuple[str | None, str | None]:
    """C13 §8.1: a waiting group's title, and where it comes from. `here`: this node holds the record already (it
    came from another feed or was made here; the group still waits for its own feed to introduce it, §5.3).
    `record` / `observation`: the entry of the same feed that would bring the record carries a title but did not
    apply (`blocked_by_entry`). Otherwise None ("A game … hasn't published yet"). Display only, never signed here."""
    if not isinstance(cid, str):
        return None, None
    row = session.get(Canonical, cid)
    if row is not None:
        title = (row.view or {}).get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:200], "here"
    if blocked is not None:
        entry = session.get(ReplicatedChange, (node_id, blocked))
        body = entry.body if entry is not None and isinstance(entry.body, dict) else {}
        if entry is not None and entry.kind == "record":
            title, provenance = body.get("title"), "record"
        else:
            payload = (body.get("observation") or {}).get("payload") if isinstance(body.get("observation"), dict) \
                else None
            title, provenance = (payload or {}).get("title") if isinstance(payload, dict) else None, "observation"
        if isinstance(title, str) and title.strip():
            return " ".join(title.split())[:200], provenance
    return None, None


def waiting_note(cid: str, seqs: tuple[int, int]) -> str:
    """§8.2 "Copy a note" text for the upstream's operator."""
    span = f"entry {seqs[0]}" if seqs[0] == seqs[1] else f"entries {seqs[0]}–{seqs[1]}"
    return (f"Your Atlas feed has changes for {cid} ({span}) but never published the record itself. Upgrade to "
            "Atlas with protocol 1.5 or later and run `atlas feed introduce --missing`.")[:600]


def waiting_groups(session: Session, node_id: str | None = None, *, name: str | None = None,
                   limit: int = WAITING_GROUPS_SHOWN) -> list[dict[str, Any]]:
    """C13 §8.1-§8.2: open `wait` rows grouped by feed and by the record they wait for, each with its sub-reason
    (`upstream_unpublished`, `upstream_defect`, `blocked_by_entry`), kinds, seq range and the copy-a-note text.
    `peers[].waiting[]` in `/sync/status`, and the waiting section of Admin → Peers. `name` is unused by the
    note (the upstream's operator reads it) and kept for callers that label the group."""
    query = select(SyncFailure.node_id, SyncFailure.waiting_for, func.count(), func.min(SyncFailure.seq),
                   func.max(SyncFailure.seq), func.min(SyncFailure.first_failed_at)).where(
        SyncFailure.disposition == WAIT, SyncFailure.resolved_at.is_(None))
    if node_id is not None:
        query = query.where(SyncFailure.node_id == node_id)
    groups = session.execute(query.group_by(SyncFailure.node_id, SyncFailure.waiting_for)
                             .order_by(func.min(SyncFailure.seq)).limit(limit)).all()
    out = []
    for node, cid, count, first, last, since in groups:
        kinds = Counter(session.scalars(select(SyncFailure.kind).where(
            SyncFailure.node_id == node, SyncFailure.waiting_for == cid, SyncFailure.disposition == WAIT,
            SyncFailure.resolved_at.is_(None)).limit(1000)))
        through = _int_state(session, "sync_introduced_through:" + node)
        blocked = _blocked_by(session, node, cid, first) if isinstance(cid, str) else None
        title, provenance = _group_title(session, node, cid, blocked)
        if blocked is not None:
            sub, label = BLOCKED_BY_ENTRY, f"Waiting for its game: entry {blocked}, which brings it, did not apply"
        elif through is not None and applied_head(session, node) >= through:
            sub, label = UPSTREAM_DEFECT, f"Waiting for its game: {name or 'the upstream'}'s feed is missing it"
        else:
            sub, label = UPSTREAM_UNPUBLISHED, "Waiting for its game"
        out.append({"cause": WAITING_FOR_RECORD, "subreason": sub, "label": label, "count": count,
                    "node_id": node, "canonical_id": cid, "title": title, "title_provenance": provenance,
                    "kinds": dict(sorted(kinds.items())), "seqs": [first, last], "blocked_by": blocked,
                    "since": _aware(since).isoformat() if since else None, "retry": "on_arrival",
                    "action": "copy_note", "note": waiting_note(str(cid), (first, last))})
    return out


# ---- protocol negotiation (C1.3) ----------------------------------------------------------------------------

def negotiate(info: dict[str, Any]) -> str:
    """Return the upstream's protocol, or raise SyncRefused. A pre-A1 upstream advertises nothing: 1.0."""
    own = feed_schema.parse_protocol(PROTOCOL)
    raw = info.get("protocol")
    if raw is None:
        upstream, minimum = feed_schema.LEGACY_PROTOCOL, feed_schema.LEGACY_PROTOCOL
    else:
        upstream, minimum = raw, info.get("min_follower_protocol", feed_schema.LEGACY_PROTOCOL)
    try:
        u, m = feed_schema.parse_protocol(upstream), feed_schema.parse_protocol(minimum)
    except ValueError as exc:
        raise SyncRefused(f"upstream advertises a malformed federation protocol ({exc}); refusing to guess.",
                          str(raw)[:16]) from exc
    if own < m:
        raise SyncRefused(f"upstream needs protocol ≥ {minimum}; this node speaks {PROTOCOL}. Upgrade Atlas.", upstream)
    if u[0] < own[0] - 1:
        raise SyncRefused(f"upstream protocol {upstream} is older than this node supports (N-1).", upstream)
    return upstream


def _record_refusal(maker: sessionmaker[Session], node_id: str, upstream_url: str, exc: SyncRefused) -> None:
    with maker() as session:
        service.write_lock(session)  # concurrent pulls of one upstream would both insert the row
        _set_state(session, "sync_refused:" + node_id, json.dumps(
            {"reason": exc.reason, "upstream_protocol": exc.upstream_protocol, "at": datetime.now(UTC).isoformat()},
            sort_keys=True))
        _note_peer_protocol(session, node_id, upstream_url, exc.upstream_protocol)
        session.commit()


def _record_rewind(maker: sessionmaker[Session], node_id: str, exc: SyncRewound) -> None:
    """Remember a detected rewind or fork in its own transaction (the pull itself rolled back)."""
    with maker() as session:
        service.write_lock(session)
        _set_state(session, "sync_rewound:" + node_id, json.dumps(
            {"reason": str(exc)[:500], "kind": exc.kind, "advertised_head": exc.advertised,
             "verified_head": exc.verified, "at": datetime.now(UTC).isoformat()}, sort_keys=True))
        session.commit()


def _note_peer_protocol(session: Session, node_id: str, upstream_url: str, protocol: str) -> None:
    for peer in session.scalars(select(Peer).where(Peer.node_id == node_id, Peer.url == upstream_url)):
        peer.remote_protocol = protocol[:16]


#: Additive `/sync/info` keys (C13 §3.4) remembered per feed: unsigned, display only.
INFO_KEYS = ("introduced_through", "media_withheld")
#: `/sync/info.records` (C13 §3.4), remembered as `sync_records:<feed>` for the caught-up line ("7 of 7 records").
RECORD_COUNTS = ("live", "introduced", "not_federated")


def _note_info(maker: sessionmaker[Session], node_id: str, info: dict[str, Any]) -> None:
    """Remember `/sync/info.introduced_through` and `media_withheld` of a feed's own node. Absent or null clears the
    value: an older upstream, or one that has not finished `introduce --missing`."""
    values: dict[str, str | None] = {}
    for name in INFO_KEYS:
        value = info.get(name)
        values["sync_" + name + ":" + node_id] = str(value) if type(value) is int and 0 <= value < 2**63 else None
    records = info.get("records")
    if isinstance(records, dict) and all(type(records.get(k)) is int and 0 <= records[k] < 2**63
                                         for k in RECORD_COUNTS):
        values["sync_records:" + node_id] = json.dumps({k: records[k] for k in RECORD_COUNTS}, sort_keys=True)
    else:
        values["sync_records:" + node_id] = None
    with maker() as session:
        if all(_state(session, k) == v for k, v in values.items()):
            return
        service.write_lock(session)
        for key, value in values.items():
            if value is None:
                session.execute(NodeState.__table__.delete().where(NodeState.key == key))
            else:
                _set_state(session, key, value)
        session.commit()


def _note_downloaded(maker: sessionmaker[Session], node_id: str, seq: int) -> None:
    """Highest sequence fetched, in its own short transaction so it survives a page that fails to verify."""
    with maker() as session:
        # Read-then-write: without the lock two concurrent pulls both see no row and both insert it (D2).
        service.write_lock(session)
        current = _int_state(session, "sync_downloaded:" + node_id) or 0
        if seq > current:
            _set_state(session, "sync_downloaded:" + node_id, str(seq))
            session.commit()


# ---- the pull ----------------------------------------------------------------------------------------------

def _apply_pending(session: Session, ctx: Ctx, node_id: str, *, limit: int | None = None) -> tuple[int, int]:
    """Apply verified entries from applied+1 in order, stopping at the first one that parks.
    Returns (entries processed, new applied head); the cursor is updated here."""
    applied = applied_head(session, node_id)
    processed, seq = 0, applied
    # C13 §5.4: a feed whose entries keep failing or waiting stops applying at a bound (`backlog_full`) rather
    # than recording without limit; downloading and relaying go on.
    bound = getattr(ctx.settings, "sync_max_open_failures", 10_000)
    open_now = _open_for(session, node_id)
    rows = session.scalars(select(ReplicatedChange).where(ReplicatedChange.node_id == node_id,
                                                          ReplicatedChange.seq > applied)
                           .order_by(ReplicatedChange.seq).execution_options(yield_per=500))
    for row in rows:
        if row.seq != seq + 1 or (limit is not None and processed >= limit):
            break
        if open_now >= bound:
            log.warning("feed %s: %d open failures or waiting entries (the bound is %d); applying paused",
                        node_id, open_now, bound)
            break
        failure = session.get(SyncFailure, (node_id, row.seq))
        skipped = failure is not None and failure.disposition == QUARANTINE and failure.reason == "skipped"
        with entry_at(row.created_at):
            result = apply_entry(session, ctx, node_id, row.seq, row.kind, row.body, may_park=not skipped)
        if result == PARK:
            break
        if result in (QUARANTINE, WAIT):
            open_now += 1
        seq, processed = row.seq, processed + 1
    if seq != applied:
        cursor = session.get(SyncCursor, node_id)
        if cursor is None:
            session.add(SyncCursor(upstream=node_id, last_seq=seq))
        else:
            cursor.last_seq, cursor.updated_at = seq, datetime.now(UTC)
        session.flush()
    return processed, seq


FOLLOW_OWN, FOLLOW_RELAYED = "own", "relayed"
INFERRED = "peer_inferred:"


def _note_inferred(maker: sessionmaker[Session], peer_id: str, mode: str, via: str | None) -> None:
    """C13 §7.4: a peer added before A19 has no explicit choice. What the pull inferred is recorded for the
    operator to confirm; it is never written onto the peer."""
    value = json.dumps({"follow": mode, "via": via, "at": datetime.now(UTC).isoformat()}, sort_keys=True)
    with maker() as session:
        current = _state(session, INFERRED + peer_id)
        if current is None or json.loads(current).get("follow") != mode or json.loads(current).get("via") != via:
            service.write_lock(session)
            _set_state(session, INFERRED + peer_id, value)
            session.commit()


def inferred_follow(session: Session, peer_id: str) -> dict[str, Any] | None:
    raw = _state(session, INFERRED + peer_id)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def pull_once(maker: sessionmaker[Session], ctx: Ctx, client: httpx.Client, upstream_url: str,
              token: str | None, trusted: dict[str, str] | None = None, *, follow: str | None = None,
              peer_id: str | None = None, timeout: float | None = None) -> int:
    """Negotiate, fetch one page from the verified head, verify and retain it, then apply in order.
    Returns the number of entries fetched. `trusted` maps node id -> public key. `follow` is the peer's explicit
    choice (C13 §7.4): `own` never falls back to a relayed feed, `relayed` always takes the pinned relayed feed;
    None (a peer added before A19, or the environment upstream) infers as before. `timeout` bounds each request
    (the first pull after adding a peer uses the probe timeout, C13 §7.3)."""
    keys = ctx.settings.trusted_keys if trusted is None else trusted
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers[feed_schema.PROTOCOL_HEADER] = PROTOCOL
    info = client.get(f"{upstream_url}/api/v2/sync/info", headers=headers, timeout=timeout or 15)
    info.raise_for_status()
    meta = top = info.json()
    node_id = meta["node_id"]
    relay = False
    available = {f.get("node_id") for f in meta.get("feeds", []) if isinstance(f, dict)}
    if follow == FOLLOW_OWN:
        # C13 §7.4: the operator chose this node's own feed. Never fall back to a feed it relays: a node that
        # answers with another identity is not the peer that was pinned.
        if node_id not in keys:
            raise SyncError(f"this peer follows the node's own feed, but {upstream_url} now answers as {node_id}; "
                            "check the address, or add the authority feed it relays as its own peer")
    elif follow == FOLLOW_RELAYED:
        choices = available & keys.keys()
        if not choices:
            raise SyncError(f"{upstream_url} no longer relays the pinned authority feed")
        node_id = choices.pop()
        meta = next(f for f in meta["feeds"] if f.get("node_id") == node_id)
        relay = True
    elif node_id not in keys:
        choices = available & keys.keys()
        if len(choices) == 1:
            node_id = choices.pop()
            meta = next(f for f in meta["feeds"] if f.get("node_id") == node_id)
            relay = True
        elif len(choices) > 1:
            raise SyncError("relay has several trusted feeds; configure a separate peer for each authority")
    if follow is None and peer_id is not None:
        inferred = FOLLOW_RELAYED if relay else FOLLOW_OWN
        _note_inferred(maker, peer_id, inferred, top.get("node_id") if relay else None)
    if not relay:  # C13 §3.4: `introduced_through` (the `upstream_defect` sub-reason) and `media_withheld`
        _note_info(maker, node_id, top)
    pub = keys.get(node_id)
    if pub is None:
        raise SyncError(f"upstream key {node_id} is not trusted (pin it under Admin → Peers or in ATLAS_TRUSTED_KEYS)")
    try:
        transport_protocol = negotiate(top)
    except SyncRefused as exc:
        _record_refusal(maker, node_id, upstream_url, exc)
        raise
    feed_protocol = meta.get("protocol") if relay else transport_protocol
    if not feed_schema.valid_protocol(feed_protocol):
        feed_protocol = None  # a relay that does not know what its authority speaks

    import base64
    if key_id_for(base64.b64decode(pub)) != node_id:
        raise SyncError("configured trusted key does not match its key id")
    try:
        history, signing_keys = key_rotation.validate_history(node_id, pub, meta.get("rotations", []),
                                                              meta.get("signing_key_id", node_id))
    except key_rotation.RotationError as exc:
        raise SyncError(str(exc)) from exc

    with maker() as session:
        applied, verified = applied_head(session, node_id), verified_head(session, node_id)
        held_back = verified - applied if _blocking(session, node_id, applied) is not None else 0
        rewound = _state(session, "sync_rewound:" + node_id) is not None
    # Fetch from the verified head: entries after a parked one keep downloading (and relaying) up to the cap.
    # After a detected rewind, re-fetch the last verified entry too: the record clears only once the
    # authority serves that exact signed entry again.
    since = verified - 1 if rewound and verified > 0 else verified
    room = ctx.settings.sync_max_parked_backlog - held_back
    entries: list[dict[str, Any]] = []
    advertised_head = None
    if room > 0:
        params: dict[str, Any] = {"since": since, "limit": min(ctx.settings.sync_batch_size, room)}
        if relay:
            params["node_id"] = node_id
        resp = client.get(f"{upstream_url}/api/v2/sync/changes", params=params, headers=headers, timeout=timeout or 30)
        resp.raise_for_status()
        page = resp.json()
        entries = page["entries"]
        advertised_head = page.get("head")
        if entries and type(entries[-1].get("seq")) is int:
            _note_downloaded(maker, node_id, entries[-1]["seq"])
    else:
        log.warning("sync backlog for %s is full (%s entries held back behind a parked entry); not downloading",
                    node_id, held_back)
    if (not relay and type(advertised_head) is int and advertised_head < verified):
        exc = SyncRewound(f"authority feed fork or rewind: it advertises head {advertised_head} but this node "
                          f"already verified {verified} of its signed entries (restored from an older backup?). "
                          "Nothing was applied; compare histories before trusting it again.",
                          "rewind", advertised_head, verified)
        _record_rewind(maker, node_id, exc)
        raise exc
    try:
        _store_and_apply(maker, ctx, node_id, pub, history, signing_keys, entries, since, advertised_head,
                         relay, upstream_url, transport_protocol, feed_protocol)
    except SyncRewound as exc:
        _record_rewind(maker, node_id, exc)
        raise
    return len(entries)


def _store_and_apply(maker: sessionmaker[Session], ctx: Ctx, node_id: str, pub: str, history: Any,
                     signing_keys: Any, entries: list[dict[str, Any]], since: int, advertised_head: Any,
                     relay: bool, upstream_url: str, transport_protocol: str, feed_protocol: str | None) -> None:
    with maker() as session:
        try:
            service.write_lock(session)  # SQLite: BEGIN IMMEDIATE (atlas.db); PostgreSQL: advisory lock
            started = datetime.now(UTC)
            expected = since
            try:
                key_rotation.remember(session, node_id, history)
            except key_rotation.RotationError as exc:
                raise SyncError(str(exc)) from exc
            retained_feed = session.get(ReplicatedFeed, node_id)
            if retained_feed is None:
                session.add(ReplicatedFeed(node_id=node_id, public_key=pub))
                session.flush()
            elif retained_feed.public_key != pub:
                raise SyncError("retained authority key differs from the pinned key")
            for e in entries:
                if e["seq"] != expected + 1:
                    raise SyncError(f"feed gap or rewind: expected {expected + 1}, got {e['seq']}")
                if not isinstance(e.get("kind"), str) or not 0 < len(e["kind"]) <= 32:
                    raise SyncError(f"entry {e['seq']} has a feed kind this node cannot store")
                if not key_rotation.check_entry(node_id, history, signing_keys, e):
                    raise SyncError(f"bad signature on entry {e['seq']}")
                retained_entry = session.get(ReplicatedChange, (node_id, e["seq"]))
                if retained_entry is None:
                    session.add(ReplicatedChange(node_id=node_id, seq=e["seq"], kind=e["kind"], body=e["body"],
                                                 body_hash=e["body_hash"], signature=e["signature"], key_id=e["key_id"],
                                                 created_at=origin_time(e.get("created_at"))))
                elif (retained_entry.body_hash, retained_entry.signature) != (e["body_hash"], e["signature"]):
                    raise SyncRewound(f"authority feed fork: already retained entry {e['seq']} changed",
                                      "fork", advertised_head if type(advertised_head) is int else None,
                                      verified_head(session, node_id))
                expected = e["seq"]
            session.flush()
            _apply_pending(session, ctx, node_id)
            retry_failures(session, ctx, node_id, started=started)
            verified_now = verified_head(session, node_id)
            if type(advertised_head) is int and verified_now <= advertised_head < 2**63:
                # Operational watermark only: this transport-reported head is not signed
                # and never authorizes an entry or advances the applied cursor.
                watermark = _int_state(session, "sync_head:" + node_id)
                _set_state(session, "sync_head:" + node_id, str(max(watermark or 0, advertised_head)))
            elif type(advertised_head) is int and advertised_head < verified_now and relay:
                log.info("relay %s is behind on %s: it serves %s, this node verified %s", upstream_url, node_id,
                         advertised_head, verified_now)
                if _int_state(session, "sync_head:" + node_id) is None:
                    _set_state(session, "sync_head:" + node_id, str(verified_now))
            session.execute(NodeState.__table__.delete().where(NodeState.key == "sync_refused:" + node_id))
            if not relay and entries and since < verified_head(session, node_id):
                # The authority served the last entry this node had verified, unchanged: no longer rewound.
                session.execute(NodeState.__table__.delete().where(NodeState.key == "sync_rewound:" + node_id))
            if feed_protocol:
                _set_state(session, "sync_protocol:" + node_id, feed_protocol)
            _note_peer_protocol(session, node_id, upstream_url, transport_protocol)
            session.commit()
        except Exception:
            session.rollback()
            raise


def pull_all(maker: sessionmaker[Session], ctx: Ctx, client: httpx.Client, upstream_url: str,
             token: str | None, max_pages: int = 1000, trusted: dict[str, str] | None = None, *,
             follow: str | None = None, peer_id: str | None = None) -> int:
    total = 0
    for _ in range(max_pages):
        n = pull_once(maker, ctx, client, upstream_url, token, trusted, follow=follow, peer_id=peer_id)
        total += n
        if n < ctx.settings.sync_batch_size:
            break
    return total


def sync_peers(maker: sessionmaker[Session], ctx: Ctx, client: httpx.Client) -> dict[str, str]:
    """Replicate from every enabled peer (plus the environment-configured upstream). Failures are per peer."""
    from . import secrets_box
    from .models import now

    results: dict[str, str] = {}
    s = ctx.settings
    jobs: list[tuple[str, str, str | None, dict[str, str], str | None, str | None]] = []
    if s.upstream_url:
        jobs.append(("env", s.upstream_url, s.upstream_token, s.trusted_keys, None, None))
    with maker() as session:
        for peer in session.scalars(select(Peer).where(Peer.enabled.is_(True))):
            try:
                token = secrets_box.decrypt(s, peer.token_enc) if peer.token_enc else None
            except secrets_box.SecretsError as exc:
                peer.last_error = str(exc)[:300]
                results[peer.name] = peer.last_error
                continue
            jobs.append((peer.name, peer.url, token, {peer.node_id: peer.public_key}, peer.id, peer.follow))
        session.commit()
    for name, url, token, trusted, peer_id, follow in jobs:
        try:
            n = pull_all(maker, ctx, client, url, token, trusted=trusted, follow=follow, peer_id=peer_id)
            results[name] = f"ok, {n} entries"
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            results[name] = error
            log.warning("sync with %s failed: %s", name, error)
        if peer_id:
            with maker() as session:
                row = session.get(Peer, peer_id)
                if row is not None:
                    row.last_error = error
                    if error is None:
                        row.last_ok_at = now()
                session.commit()
    return results


# ---- operator tools: replay, rescan, skip, reconcile (C1.8, C1.9) -----------------------------------------

def _historical_parked(session: Session, node_id: str, applied: int) -> SyncFailure | None:
    """The oldest open parked row at or below the applied head (recorded by `rescan`)."""
    return session.scalars(select(SyncFailure).where(
        SyncFailure.node_id == node_id, SyncFailure.disposition == PARK, SyncFailure.resolved_at.is_(None),
        SyncFailure.seq <= applied).order_by(SyncFailure.seq).limit(1)).first()


def _replay_history(session: Session, ctx: Ctx, node_id: str, first: int, last: int) -> dict[str, int]:
    """Re-apply every retained entry from `first` to `last` in feed order (`replay --historical`).

    Entries an older release skipped are applied in their causal position, and every later entry is
    applied again after them, so dependants (a merge, detach or artwork on a record the old release never
    created) converge too. Application is idempotent: entries already reflected are no-ops. An entry the
    administrator skipped stays skipped; one this release still cannot apply stays parked (it can no
    longer block: the cursor is past it)."""
    counts = {"reapplied": 0, "recovered_parked": 0}
    rows = session.scalars(select(ReplicatedChange).where(
        ReplicatedChange.node_id == node_id, ReplicatedChange.seq >= first, ReplicatedChange.seq <= last)
        .order_by(ReplicatedChange.seq).execution_options(yield_per=500))
    for entry in rows:
        failure = session.get(SyncFailure, (node_id, entry.seq))
        if failure is not None and failure.resolved_at is None and failure.reason == "skipped":
            continue
        was_parked = failure is not None and failure.resolved_at is None and failure.disposition == PARK
        with entry_at(entry.created_at):
            result = apply_entry(session, ctx, node_id, entry.seq, entry.kind, entry.body, may_park=False)
        counts["reapplied"] += 1
        if was_parked and result in (APPLY, NOOP):
            counts["recovered_parked"] += 1
    return counts


def replay(session: Session, ctx: Ctx, node_id: str | None = None, *, limit: int | None = None,
           historical: bool = False) -> dict[str, Any]:
    """Re-classify parked entries from applied+1 in order and apply until the next park, without the network.

    Run after an upgrade (automatically once at startup, and by `atlas sync replay`). Idempotent. The
    caller commits (or rolls back for a dry run).

    Parked rows at or below the applied head (recorded by `rescan` for entries an older release skipped)
    are never applied out of order (C1.7). They are only applied by an explicit `historical=True`
    (`atlas sync replay --historical`), which re-applies the whole retained range from the oldest of them
    in feed order; the startup replay never does this."""
    service.write_lock(session)
    out: dict[str, Any] = {}
    for node in [node_id] if node_id else upstream_nodes(session):
        before = applied_head(session, node)
        counts = {"reapplied": 0, "recovered_parked": 0}
        oldest = _historical_parked(session, node, before)
        if historical and oldest is not None:
            counts = _replay_history(session, ctx, node, oldest.seq, before)
        processed, after = _apply_pending(session, ctx, node, limit=limit)
        state = upstream_status(session, ctx, node)
        remaining = _historical_parked(session, node, after)
        out[node] = {"applied_before": before, "applied_head": after, "applied": processed, **counts,
                     "historical_parked": remaining.seq if remaining is not None else None,
                     "state": state["state"], "parked": state["parked"], "refused": state["refused"],
                     "rewound": state["rewound"], "verified_head": state["verified_head"]}
    return out


def rescan(session: Session, node_id: str) -> dict[str, int]:
    """One-time recovery (C1.8): find retained entries at or below the applied head that an older
    release silently dropped (unknown kind or field, invalid observation) and record them as parked or
    quarantined. An observation this release accepts but that is missing locally (an older release
    rejected, for example, an `ext` member it did not know) is parked as `dropped_by_previous_release`.

    Rows that are already *resolved* are classified again: after a code rollback, the older release
    retries a parked row as an ordinary failure, ignores the kind it does not know, marks the row
    resolved and moves its cursor past it. This release never resolves an entry it cannot apply, so a
    resolved row whose entry still classifies as parked was resolved by an older release.

    Classification only (C1.8): the cursor never moves back and nothing is re-applied here. `atlas sync
    replay --historical` applies the recovered entries in feed order."""
    applied = applied_head(session, node_id)
    counts = {"scanned": 0, "parked": 0, "quarantined": 0}
    rows = session.scalars(select(ReplicatedChange).where(ReplicatedChange.node_id == node_id,
                                                          ReplicatedChange.seq <= applied)
                           .order_by(ReplicatedChange.seq).execution_options(yield_per=500))
    introduced: dict[str, int] = {}  # record id -> first retained entry of this feed that introduced it
    unseen: list[tuple[int, str, str]] = []  # sidecars that named a record before this feed introduced it
    for row in rows:
        counts["scanned"] += 1
        body = row.body if isinstance(row.body, dict) else {}
        cid = body.get("canonical_id")
        made = body.get(_INTRODUCING.get(row.kind, ""))
        if isinstance(made, str):
            introduced.setdefault(made, row.seq)
        existing = session.get(SyncFailure, (node_id, row.seq))
        if existing is not None and existing.resolved_at is None:
            continue  # already tracked
        abandoned = existing is not None and existing.disposition == WAIT and existing.reason == ABANDONED
        if (existing is None or abandoned) and row.kind in _SIDECARS and \
                feed_schema.classify(row.kind, body).action == APPLY:
            missing = missing_record(session, row.kind, body)
            if missing is not None:  # C13 §5.6: an earlier release dropped it; it waits for its record now
                _record(session, node_id, row.seq, row.kind, WAIT, "dropped_by_previous_release",
                        f"waiting for record {missing}: an earlier release dropped this entry because the feed "
                        "had not introduced it (found by rescan)", {"kind": row.kind}, waiting_for=missing)
                counts["waiting"] = counts.get("waiting", 0) + 1
                continue
            if isinstance(cid, str) and cid not in introduced and row.kind in _SIDECARS_STATELESS:
                unseen.append((row.seq, row.kind, cid))
        outcome = feed_schema.classify(row.kind, row.body)
        if outcome.action == PARK:
            _record(session, node_id, row.seq, row.kind, PARK, outcome.reason, outcome.message, outcome.detail)
            counts["parked"] += 1
        elif outcome.action == QUARANTINE:
            _record(session, node_id, row.seq, row.kind, QUARANTINE, "invalid", outcome.message, outcome.detail)
            counts["quarantined"] += 1
        elif row.kind == "relation":
            from . import relations

            if not relations.applied_from_feed(session, node_id, row.seq, outcome.body or {}):
                _record(session, node_id, row.seq, row.kind, PARK, "dropped_by_previous_release",
                        "an earlier release skipped this relation; `atlas sync replay --historical` applies it",
                        {"kind": "relation"})
                counts["parked"] += 1
        elif row.kind == "obs":
            body = outcome.body or {}
            o = body.get("observation")
            cid = body.get("canonical_id")
            valid = isinstance(cid, str) and cid.startswith("atlas:") and len(cid) <= 40 and _valid_observation(o)
            if isinstance(o, dict) and session.get(Observation, str(o.get("id"))) is not None:
                continue  # applied (observations are never deleted)
            if valid:
                _record(session, node_id, row.seq, row.kind, PARK, "dropped_by_previous_release",
                        "an earlier release skipped this observation; `atlas sync replay` applies it", {"kind": "obs"})
                counts["parked"] += 1
            else:
                _record(session, node_id, row.seq, row.kind, QUARANTINE, "invalid",
                        "InvalidEntry: observation fails the observation contract (found by rescan)", {"kind": "obs"})
                counts["quarantined"] += 1
    for seq, kind, cid in unseen:
        # The record arrived later in this feed (a bridge observation or `record`), after an earlier release had
        # dropped this entry without recording it: park it so `replay --historical` re-applies it in feed order.
        if introduced.get(cid, 0) > seq:
            _record(session, node_id, seq, kind, PARK, "dropped_by_previous_release",
                    f"an earlier release dropped this {kind} entry because its record had not arrived yet; "
                    "`atlas sync replay --historical` applies it", {"kind": kind})
            counts["parked"] += 1
    counts["parked"] += _rescan_replaced_state(session, node_id, applied)
    if waiting := _rescan_waiting(session, node_id):
        counts["waiting"] = counts.get("waiting", 0) + waiting
    return counts


def _rescan_waiting(session: Session, node_id: str) -> int:
    """C13 §5.6: an entry an earlier release quarantined because its record was unknown (or that it gave up
    retrying) waits for that record now, so it is applied as soon as the record arrives."""
    converted = 0
    rows = session.scalars(select(SyncFailure).where(
        SyncFailure.node_id == node_id, SyncFailure.resolved_at.is_(None), SyncFailure.disposition == QUARANTINE,
        SyncFailure.reason == "apply_error")).all()
    for row in rows:
        entry = session.get(ReplicatedChange, (row.node_id, row.seq))
        if entry is None or not isinstance(entry.body, dict):
            continue
        waiting = missing_record(session, entry.kind, entry.body)
        if waiting is not None:
            row.disposition, row.reason, row.waiting_for = WAIT, WAITING_FOR_RECORD, waiting
            converted += 1
    session.flush()
    return converted


#: Kinds whose entries replace a whole piece of state (a source's achievement list, one field's lock), so only the
#: newest entry per piece says what this node should hold. A 1.0 release dropped such an entry when its value was
#: new to it (a `platinum` tier, a `releases` lock) without recording anything (Phase 4 D1).
_REPLACING = ("achievements", "lock", "unlock")
#: C13 §5.6 rescan: kinds naming a record that a 1.0 release dropped without recording anything when the record had not
#: arrived (`AtlasError` swallowed, or an early return); `_SIDECARS_STATELESS` are those whose drop is also found when
#: the record arrived later (`_rescan_replaced_state` judges achievements and locks by comparing state).
_SIDECARS = ("artwork", "achievements", "custom", "lock", "unlock", "merge", "detach", "relation")
_SIDECARS_STATELESS = ("artwork", "custom")
#: Entries that bring a record into existence on a follower, and the field that names it.
_INTRODUCING = {"obs": "canonical_id", "record": "canonical_id", "detach": "new_canonical_id"}


def _state_key(kind: str, body: dict[str, Any]) -> tuple[str, str, str] | None:
    cid = body.get("canonical_id")
    if not isinstance(cid, str) or not cid.startswith("atlas:") or len(cid) > 40:
        return None
    if kind == "achievements":
        from .normalize import norm_namespace

        source = body.get("source")
        return ("achievements", cid, norm_namespace(source)) if isinstance(source, str) and source else None
    field = body.get("field")
    return ("lock", cid, field) if isinstance(field, str) and field else None


#: What a list entry says about each item that this node stores as given (C2.2 step 4). Translations (`i18n`),
#: the order and percent dates are not compared: they never made a 1.0 release drop an entry.
def _item_state(it: dict[str, Any]) -> tuple[Any, ...]:
    return (it["external_id"], it["name"], it.get("description") or "", bool(it.get("hidden")), it.get("icon_url"),
            it.get("locked_icon_url"), it.get("kind"), it.get("tier"), it.get("points"), it.get("global_percent"),
            it.get("set") or "base", it.get("status"), it.get("locale"), it.get("icon_licence"),
            it.get("icon_attribution"))


def _row_state(r: Achievement) -> tuple[Any, ...]:
    return (r.external_id, r.name, r.description or "", bool(r.hidden), r.icon_url, r.locked_icon_url, r.kind,
            r.tier, r.points, r.global_percent, r.set_key or "base", r.status, r.text_locale, r.icon_licence,
            r.icon_attribution)


def _replaced_state_holds(session: Session, node_id: str, seq: int, kind: str, body: dict[str, Any]) -> bool:
    """Whether this node holds what the newest `achievements`/`lock`/`unlock` entry for its piece of state says.

    On a record this feed merged after the entry, the entry is judged where the authority's merge put it: on the
    survivor, by the merge's rules (the rows and lock it carried, the survivor's own duplicates winning)."""
    from .achievement_key import norm_name
    from .facets import clean_achievements
    from .normalize import norm_namespace

    cid = body["canonical_id"]
    record = session.get(Canonical, cid)
    if record is None:
        return False  # the entry's record never arrived (its observation was dropped as well)
    late = None
    if record.merged_into is not None:
        late = _late_merge(session, node_id, seq, cid)
        if late is None:
            return True  # merged here in another way: not this feed's history, nothing to replay it onto
        if late.truncated:
            return False  # replay records why it cannot land it (quarantined), instead of nothing
    if kind == "achievements":
        try:
            wanted = clean_achievements(json.loads(json.dumps(body.get("items") or [])))
        except (AtlasError, TypeError, ValueError):
            return False  # replay records why this release refuses it (quarantined), instead of nothing
        source = norm_namespace(body["source"])
        if late is None:  # rows a later merge carried here were not here when the entry ran
            arrived = _carried_ids(session, _later_merges_into(session, node_id, seq, cid))
            have = [r for r in session.scalars(select(Achievement).where(
                Achievement.canonical_id == cid, Achievement.source == source, Achievement.retired_at.is_(None)))
                if r.id not in arrived]
            return sorted(map(_row_state, have)) == sorted(map(_item_state, wanted))
        carried_ids = _carried_ids(session, [late])
        chain = _merge_chain(session, cid)
        arrived = _carried_ids(session, [m for other in chain[1:]
                                         for m in _later_merges_into(session, node_id, late.origin_seq or seq, other)])
        rows = [r for r in session.scalars(select(Achievement).where(
            Achievement.canonical_id == chain[-1], Achievement.source == source)) if r.id not in arrived]
        own = [r for r in rows if r.id not in carried_ids]  # the survivor's own rows win over carried duplicates
        own_ident = {(r.external_id, r.norm_name) for r in own}
        own_keys = {r.key for r in own}
        wanted = [i for i in wanted if (i["external_id"], norm_name(i["name"])) not in own_ident
                  and i.get("key") not in own_keys]
        joined = _carried_ids(session, _later_merges_into(session, node_id, seq, cid))  # not the entry's rows
        have = [r for r in rows if r.id in carried_ids and r.id not in joined and r.retired_at is None]
        return sorted(map(_row_state, have)) == sorted(map(_item_state, wanted))
    target = cid if late is None else service.follow(session, cid)
    lock = session.get(Lock, (target, body["field"]))
    if lock is not None and (lock.local_only or lock.cleared):
        return True  # this node's own pin or clear wins over the feed (as when the entry is applied)
    if late is not None:
        items = _merge_items(session, late, "lock", "lock_dropped")
        if ("lock_dropped", body["field"]) in items:
            return True  # the survivor's own lock won the merge
        moved = ("lock", body["field"]) in items
        if kind == "unlock":
            return not (moved and lock is not None)
        if lock is not None and not moved:
            return True  # the survivor had its own lock at merge time: the merge kept it
    if kind == "unlock":
        return lock is None or _lock_carried_later(session, node_id, seq, cid, body["field"])
    if lock is None:
        return False
    try:
        return lock.value == service.clean_lock(body["field"], body.get("value"), replicated=True)
    except AtlasError:
        return False


def _merge_chain(session: Session, cid: str) -> list[str]:
    """`cid` and every record it was merged into since, survivor last (the same bounded walk as `follow`)."""
    chain = [cid]
    for _ in range(20):
        row = session.get(Canonical, chain[-1])
        if row is None or row.merged_into is None:
            break
        chain.append(row.merged_into)
    return chain


def _rescan_replaced_state(session: Session, node_id: str, applied: int) -> int:
    """Rescan step for entries that replace state (`_REPLACING`): park the newest entry per piece of state when
    this node does not hold what it says (`dropped_by_previous_release`). Older entries for the same piece are
    left alone: `replay --historical` re-applies the whole range from the oldest parked entry, in feed order, so
    the newest one wins as it did on the authority. An entry on a record merged since is judged on the survivor,
    unless a later entry on a record of its merge chain replaced that piece there (that one decides). Idempotent:
    once applied, the state holds and nothing is recorded again."""
    newest: dict[tuple[str, str, str], int] = {}
    rows = session.execute(select(ReplicatedChange.seq, ReplicatedChange.kind, ReplicatedChange.body).where(
        ReplicatedChange.node_id == node_id, ReplicatedChange.seq <= applied,
        ReplicatedChange.kind.in_(_REPLACING)).order_by(ReplicatedChange.seq).execution_options(yield_per=500))
    for seq, kind, body in rows:
        key = _state_key(kind, body if isinstance(body, dict) else {})
        if key is not None:
            newest[key] = seq
    parked = 0
    for (group, cid, piece), seq in sorted(newest.items(), key=lambda kv: kv[1]):
        chain = _merge_chain(session, cid)
        if len(chain) > 1:
            late = _late_merge(session, node_id, seq, cid)
            after = late.origin_seq if late is not None and late.origin_seq is not None else seq
            if any(newest.get((group, other, piece), -1) > after for other in chain[1:]):
                continue  # an entry after the merge replaced this piece on the survivor: that one decides
        existing = session.get(SyncFailure, (node_id, seq))
        if existing is not None and existing.resolved_at is None:
            continue  # already tracked (parked, quarantined or skipped by the administrator)
        entry = session.get(ReplicatedChange, (node_id, seq))
        outcome = feed_schema.classify(entry.kind, entry.body)
        if outcome.action != APPLY:
            continue  # the main rescan loop recorded it
        if _replaced_state_holds(session, node_id, seq, entry.kind, outcome.body or {}):
            continue
        _record(session, node_id, seq, entry.kind, PARK, "dropped_by_previous_release",
                f"an earlier release skipped this {entry.kind} entry; `atlas sync replay --historical` applies it",
                {"kind": entry.kind})
        parked += 1
    return parked


def skip(session: Session, ctx: Ctx, node_id: str, seq: int, reason: str, *, actor_id: str | None = None,
         via: str = "cli") -> dict[str, Any]:
    """Administrator escape hatch: a parked entry becomes quarantined (`skipped`), so the entries after it
    can apply. Audited. The signed entry stays retained and can still be replayed after an upgrade."""
    reason = " ".join(reason.split())[:300]
    if not reason:
        raise ValueError("give a reason; it is written to the audit log")
    service.write_lock(session)
    row = session.get(SyncFailure, (node_id, seq))
    if row is None or row.resolved_at is not None or row.disposition != PARK:
        raise ValueError(f"entry {seq} of {node_id} is not parked")
    before = row.reason
    row.disposition, row.reason = QUARANTINE, "skipped"
    row.detail = {**(row.detail or {}), "skipped_reason": reason, "parked_reason": before}
    row.error = f"skipped by an administrator ({reason}); was {before}: {row.error}"[:2000]
    row.last_attempt_at = datetime.now(UTC)
    session.add(AuditEntry(actor_id=actor_id, action="sync_skip", target=f"{node_id}:{seq}"[:128],
                           detail={"reason": reason, "parked_reason": before, "kind": row.kind, "via": via}))
    session.flush()
    _, applied = _apply_pending(session, ctx, node_id)
    return {"node_id": node_id, "seq": seq, "applied_head": applied}


def view_digest(session: Session, node_id: str | None) -> dict[str, Any]:
    """A hash of the observations one authority's feed published and the live record each one sits on
    (so merges and detaches count). `node_id=None` is this node's own feed.

    Scope (`atlas-view-v1`): observations only. Artwork, achievements, custom values, locks and the
    schema are not covered, so equal digests mean equal replicated *observations*, not an identical node.
    One query for the merge map plus one streaming query for the observations."""
    query = select(Observation)
    if node_id is None:
        query = query.where(Observation.origin_node.is_(None), Observation.redistributable.is_(True))
    else:
        query = query.where(Observation.origin_node == node_id)
    merged = dict(session.execute(select(Canonical.id, Canonical.merged_into)
                                  .where(Canonical.merged_into.is_not(None))).all())

    def live_of(cid: str) -> str:
        for _ in range(20):  # the same bounded walk as service.follow, without a query per hop
            if cid not in merged:
                return cid
            cid = merged[cid]
        return cid

    digest = hashlib.sha256()
    count = 0
    for o in session.scalars(query.order_by(Observation.id).execution_options(yield_per=1000)):
        live = live_of(o.canonical_id) if o.canonical_id else None
        digest.update(json.dumps([o.id, live, o.source_kind, o.source, o.source_scope, o.external_id, o.payload,
                                  o.is_current], sort_keys=True, default=str, separators=(",", ":")).encode())
        digest.update(b"\n")
        count += 1
    return {"algorithm": "atlas-view-v1", "observations": count, "digest": digest.hexdigest()}


def reconcile(session: Session, client: httpx.Client, upstream_url: str, token: str | None,
              node_id: str) -> dict[str, Any]:
    """Compare this node's view of `node_id` with the upstream's (`/sync/digest`). Read-only."""
    local = view_digest(session, node_id)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers[feed_schema.PROTOCOL_HEADER] = PROTOCOL
    params = {"node_id": node_id}  # an authority answers for its own feed, a relay for the retained one
    try:
        resp = client.get(f"{upstream_url}/api/v2/sync/digest", params=params, headers=headers, timeout=60)
    except httpx.HTTPError as exc:
        return {"local": local, "upstream": None, "match": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    if resp.status_code == 404:
        return {"local": local, "upstream": None, "match": None,
                "error": "the upstream does not offer /sync/digest (older release)"}
    resp.raise_for_status()
    remote = resp.json()
    return {"local": local, "upstream": remote, "match": remote.get("digest") == local["digest"], "error": None}


def replay_marker() -> str:
    from . import __version__

    # the vocabularies are part of the registry: a release that learns a new value (platinum, 1.2) replays
    registry = feed_schema.markdown_table() + repr(sorted((k, sorted(v)) for k, v in feed_schema.vocabularies().items()))
    return f"{__version__}|{PROTOCOL}|" + hashlib.sha256(registry.encode()).hexdigest()[:16]


def startup_replay(maker: sessionmaker[Session], ctx: Ctx) -> dict[str, Any] | None:
    """Run `replay` once after an upgrade (a new release, protocol or registry). Idempotent."""
    with maker() as session:
        marker = replay_marker()
        if _state(session, "sync_replayed_for") == marker:
            return None
        result = replay(session, ctx)
        # C13 §5.3: waiting entries whose record their own feed introduced while a wake could not finish (a crash)
        woken = _wake_ready(session, ctx, None, 10_000)
        if woken:
            log.info("startup: %d waiting feed entries applied", woken)
        _set_state(session, "sync_replayed_for", marker)
        session.commit()
    if any(r["parked"] for r in result.values()):
        log.warning("feed entries are still parked after the startup replay; see Admin → Peers")
    return result
