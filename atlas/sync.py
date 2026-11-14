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
    AuditEntry,
    Canonical,
    Crosswalk,
    Lock,
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


# Automatic retries stop here so a permanently broken entry is not re-run on every pull; an
# administrator can still retry it by hand.
AUTO_RETRY_LIMIT = 20


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
    try:
        with session.begin_nested():
            result = _apply(session, ctx, node_id, seq, kind, outcome.body)
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
    return NOOP if result == NOOP else APPLY


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
            detail: dict[str, Any] | None) -> None:
    message = message[:2000]
    now = datetime.now(UTC)
    failure = session.get(SyncFailure, (node_id, seq))
    if failure is None:
        session.add(SyncFailure(node_id=node_id, seq=seq, kind=kind[:32], error=message, attempts=1,
                                first_failed_at=now, last_attempt_at=now, disposition=disposition, reason=reason,
                                detail=detail))
    else:
        failure.error, failure.attempts, failure.last_attempt_at = message, failure.attempts + 1, now
        failure.disposition, failure.reason, failure.detail = disposition, reason, detail
        failure.resolved_at = None
    session.flush()


def _record_failure(session: Session, node_id: str, seq: int, kind: str, exc: Exception) -> None:
    _record(session, node_id, seq, kind, QUARANTINE, "apply_error", f"{type(exc).__name__}: {exc}", {"kind": kind})


def retry_failures(session: Session, ctx: Ctx, node_id: str | None = None, *, limit: int = 100,
                   max_attempts: int | None = AUTO_RETRY_LIMIT) -> dict[str, int]:
    """Re-apply quarantined entries, oldest first. Application is idempotent, so a retry that succeeds
    converges without duplicating anything. Parked entries are never retried out of order: they are
    re-classified in sequence by the pull and by `replay`.

    A retry runs after the entries behind it have applied, so it never regresses newer state: an entry that a
    later applied entry of the same feed replaced is resolved as superseded without being applied, and custom
    values are applied only for the keys no later applied entry set (`superseding`)."""
    query = select(SyncFailure).where(SyncFailure.resolved_at.is_(None), SyncFailure.disposition == QUARANTINE)
    if node_id is not None:
        query = query.where(SyncFailure.node_id == node_id)
    if max_attempts is not None:
        query = query.where(SyncFailure.attempts < max_attempts)
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
    return {"retried": len(rows), "resolved": resolved, "still_failing": len(rows) - resolved}


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
        prior = session.scalars(
            select(Observation).where(
                Observation.source == o["source"], Observation.source_scope == o["source_scope"],
                Observation.external_id == o["external_id"], Observation.is_current.is_(True),
            )
        ).first()
        # A later entry of the same feed already superseded this observation (an in-order replay of
        # retained history, `replay --historical`): store it as history and leave the newer one current.
        superseded = bool(prior and prior.origin_node == node_id and prior.origin_seq is not None
                          and prior.origin_seq > seq)
        if prior and not superseded:
            prior.is_current = False
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
        if kind == "artwork":
            facets.set_artwork(session, ctx, body["canonical_id"], body["source"], body["items"],
                               redistributable=True, log=False)
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
    elif kind == "detach":
        obs = session.get(Observation, body["observation_id"])
        if obs is not None and obs.canonical_id == body["new_canonical_id"]:
            return NOOP  # already detached
        if session.get(Canonical, body["new_canonical_id"]) is not None:
            # The detach created this record, so it already ran; the observation may have moved on since
            # (a later merge or detach). Re-running it would undo that history.
            return NOOP
        service.detach_observation(session, ctx, body["observation_id"], new_id=body["new_canonical_id"], log=False,
                                   actor="sync")
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
    if refused:
        state = "refused"
    elif rewound:
        state = "rewound"  # the authority's history no longer matches what this node verified
    elif parked and parked["backlog_full"]:
        state = "backlog_full"
    elif parked:
        state = "parked"
    elif claimed is None:
        state = "unknown"  # the upstream never told us its head: never claim to be synchronized
    elif claimed > verified or applied < verified:
        state = "behind"
    elif quarantined_count:
        state = "degraded"
    else:
        state = "in_sync"
    return {"observed_head": observed, "downloaded_head": downloaded, "verified_head": verified,
            "applied_head": applied, "parked": parked,
            "quarantined": {"count": quarantined_count, "oldest_seq": oldest},
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
    return {"upstreams": {n: upstream_status(session, ctx_or_settings, n) for n in upstream_nodes(session)}}


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
    rows = session.scalars(select(ReplicatedChange).where(ReplicatedChange.node_id == node_id,
                                                          ReplicatedChange.seq > applied)
                           .order_by(ReplicatedChange.seq).execution_options(yield_per=500))
    for row in rows:
        if row.seq != seq + 1 or (limit is not None and processed >= limit):
            break
        failure = session.get(SyncFailure, (node_id, row.seq))
        skipped = failure is not None and failure.disposition == QUARANTINE and failure.reason == "skipped"
        with entry_at(row.created_at):
            result = apply_entry(session, ctx, node_id, row.seq, row.kind, row.body, may_park=not skipped)
        if result == PARK:
            break
        seq, processed = row.seq, processed + 1
    if seq != applied:
        cursor = session.get(SyncCursor, node_id)
        if cursor is None:
            session.add(SyncCursor(upstream=node_id, last_seq=seq))
        else:
            cursor.last_seq, cursor.updated_at = seq, datetime.now(UTC)
        session.flush()
    return processed, seq


def pull_once(maker: sessionmaker[Session], ctx: Ctx, client: httpx.Client, upstream_url: str,
              token: str | None, trusted: dict[str, str] | None = None) -> int:
    """Negotiate, fetch one page from the verified head, verify and retain it, then apply in order.
    Returns the number of entries fetched. `trusted` maps node id -> public key."""
    keys = ctx.settings.trusted_keys if trusted is None else trusted
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers[feed_schema.PROTOCOL_HEADER] = PROTOCOL
    info = client.get(f"{upstream_url}/api/v2/sync/info", headers=headers, timeout=15)
    info.raise_for_status()
    meta = top = info.json()
    node_id = meta["node_id"]
    relay = False
    if node_id not in keys:
        available = {f.get("node_id") for f in meta.get("feeds", []) if isinstance(f, dict)}
        choices = available & keys.keys()
        if len(choices) == 1:
            node_id = choices.pop()
            meta = next(f for f in meta["feeds"] if f.get("node_id") == node_id)
            relay = True
        elif len(choices) > 1:
            raise SyncError("relay has several trusted feeds; configure a separate peer for each authority")
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
        resp = client.get(f"{upstream_url}/api/v2/sync/changes", params=params, headers=headers, timeout=30)
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
            retry_failures(session, ctx, node_id)
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
             token: str | None, max_pages: int = 1000, trusted: dict[str, str] | None = None) -> int:
    total = 0
    for _ in range(max_pages):
        n = pull_once(maker, ctx, client, upstream_url, token, trusted)
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
    jobs: list[tuple[str, str, str | None, dict[str, str], str | None]] = []
    if s.upstream_url:
        jobs.append(("env", s.upstream_url, s.upstream_token, s.trusted_keys, None))
    with maker() as session:
        for peer in session.scalars(select(Peer).where(Peer.enabled.is_(True))):
            try:
                token = secrets_box.decrypt(s, peer.token_enc) if peer.token_enc else None
            except secrets_box.SecretsError as exc:
                peer.last_error = str(exc)[:300]
                results[peer.name] = peer.last_error
                continue
            jobs.append((peer.name, peer.url, token, {peer.node_id: peer.public_key}, peer.id))
        session.commit()
    for name, url, token, trusted, peer_id in jobs:
        try:
            n = pull_all(maker, ctx, client, url, token, trusted=trusted)
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
    for row in rows:
        counts["scanned"] += 1
        existing = session.get(SyncFailure, (node_id, row.seq))
        if existing is not None and existing.resolved_at is None:
            continue  # already tracked
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
    return counts


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
        _set_state(session, "sync_replayed_for", marker)
        session.commit()
    if any(r["parked"] for r in result.values()):
        log.warning("feed entries are still parked after the startup replay; see Admin → Peers")
    return result
