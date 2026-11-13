"""The upgrade journal (C16 §6.3, as amended by §22.4): what finishing an upgrade to wire 1 consists of, written down
before anything is served so a crash at any point resumes instead of skipping or repeating.

`NodeState["upgrade_v1"]` is one JSON row (later wire upgrades get `upgrade_w<N>`):

    {"from": "v0-1.0" | "v0-1.x" | "fresh", "started_at": ..., "pre_upgrade_head": {"seq", "body_hash"} | null,
     "first_v1_seq": int | null, "authority_for": node id | null,
     "stages": {stage: "pending" | "done" | "n/a"}, "cursors": {...}, "skipped_scope_ceiling": 0, "last_error": null}

* `open_journal` runs in `create_app`, before any thread starts and before the first request, in every worker. It is
  idempotent (insert if absent under the write lock), takes the point-of-no-return backup of an authority with a
  feed, and signs `wire_start` for it, so `first_v1_seq` is exact by construction.
* `step` runs at the top of every sync round until every stage is `done`/`n/a`: at most one batch per call, each in
  its own transaction, under a lease so two workers do not both work. Failures are written to `last_error`, the stage
  stays `pending` and the next round retries.
* "Upgrade complete" means every stage `done`/`n/a`. Until then `/ready` answers `degraded`, never `not_ready`: the
  node is correct while it introduces (`emit` introduces on demand).

A downgrade leaves the row inert (the previous release never reads it) and a re-upgrade resumes it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from . import feed
from .models import ChangeLog, NodeState, Relation, ReplicatedChange, SyncCursor
from .service import AtlasError

log = logging.getLogger("atlas")

KEY = "upgrade_v1"
LEASE = "upgrade_v1_lease"
LEASE_SECONDS = 120
STAGES = ("replay", "rescan", "introduce_missing", "introduce_all", "relations")
AUTHORITY_STAGES = ("introduce_missing", "introduce_all", "relations")
PENDING, DONE, NA = "pending", "done", "n/a"
RELATION_BATCH = 200
STALE_AFTER = timedelta(hours=24)


def _read(session: Session) -> dict[str, Any] | None:
    row = session.get(NodeState, KEY)
    if row is None:
        return None
    try:
        value = json.loads(row.value)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _write(session: Session, journal: dict[str, Any]) -> None:
    text = json.dumps(journal, sort_keys=True)
    row = session.get(NodeState, KEY)
    if row is None:
        session.add(NodeState(key=KEY, value=text))
    else:
        row.value = text
    session.flush()


def origin_of(session: Session) -> str:
    """Where this database comes from, judged from its current state (§6.3 item 1): `v0-1.0` when it follows feeds
    but never recorded a replay marker (a 1.0 release), `v0-1.x` when it has a replay marker of an older release,
    `fresh` for an empty one. A marker that already names a wire means the database was upgraded by an earlier
    build of this release (no journal yet): nothing is left to do for it."""
    marker = session.get(NodeState, "sync_replayed_for")
    follows = bool(session.scalar(select(func.count()).select_from(SyncCursor))
                   or session.scalar(select(func.count()).select_from(ReplicatedChange)))
    head = session.get(NodeState, "feed_seq")
    has_feed = head is not None and str(head.value) not in ("", "0")
    if marker is None:
        return "v0-1.0" if follows else ("v0-1.x" if has_feed else "fresh")
    return "v0-1.x"


def _own_head(session: Session) -> dict[str, Any] | None:
    row = session.execute(select(ChangeLog.seq, ChangeLog.body_hash).order_by(ChangeLog.seq.desc()).limit(1)).first()
    return {"seq": int(row[0]), "body_hash": row[1]} if row else None


def _marker_is_wire(session: Session) -> bool:
    marker = session.get(NodeState, "sync_replayed_for")
    return marker is not None and "|wire" in str(marker.value)


def fresh_journal(session: Session, ctx: Any) -> dict[str, Any]:
    origin = origin_of(session)
    already = _marker_is_wire(session)
    authority = ctx.settings.role == "authority"
    stages = {
        "replay": DONE if already or origin == "fresh" else PENDING,
        "rescan": PENDING if origin == "v0-1.0" and not already else NA,
        "introduce_missing": PENDING if authority and not already else NA,
        "introduce_all": PENDING if authority and ctx.settings.federation_introduce == "all" and not already else NA,
        "relations": PENDING if authority and not already else NA,
    }
    authority_for = None
    if origin == "fresh" or already:  # nothing to publish, rescan or replay: a new database has no earlier release
        for name in AUTHORITY_STAGES:
            stages[name] = DONE if authority and stages[name] != NA else NA
        if authority:
            authority_for = ctx.identity.node_id
    return {"from": origin, "started_at": datetime.now(UTC).isoformat(), "pre_upgrade_head": _own_head(session),
            "first_v1_seq": feed.wire_start_seq(session), "authority_for": authority_for,
            "stages_for": ctx.identity.node_id if authority else None, "stages": stages,
            "cursors": {}, "skipped_scope_ceiling": 0, "last_error": None}


def open_journal(maker: sessionmaker[Session], ctx: Any, engine: Any = None) -> dict[str, Any]:
    """Create the journal if it is absent (idempotent, race-safe under the write lock) and return it. On an authority
    with a feed it first takes the point-of-no-return backup (when `ATLAS_BACKUP_DIR` is set), then signs `wire_start`
    in the same transaction, so `first_v1_seq` is the first wire seq. Call before any thread starts."""
    from .service import write_lock

    with maker() as session:
        if session.get(NodeState, KEY) is not None:
            return _read(session) or {}
    taken = _pnr_backup(maker, ctx, engine)
    with maker() as session:
        write_lock(session)
        journal = _read(session)
        if journal is not None:  # another worker won the race
            return journal
        journal = fresh_journal(session, ctx)
        if ctx.settings.role == "authority" and journal["pre_upgrade_head"] is not None and not _marker_is_wire(session) \
                and feed.frozen(session) is None:
            if journal["first_v1_seq"] is None:
                journal["first_v1_seq"] = feed.ensure_wire_start(session, ctx.identity)
            if taken:
                journal["pnr_backup"] = taken
        _write(session, journal)
        session.commit()
        log.info("upgrade journal opened (from %s, first wire seq %s)", journal["from"], journal["first_v1_seq"])
        return journal


def _pnr_backup(maker: sessionmaker[Session], ctx: Any, engine: Any) -> str | None:
    """§6.4 rule 3: an authority with a feed gets a backup before its journal exists, so before it can sign a wire
    entry. Not a non-forking restore target (restoring any backup is frozen): it is at the new schema and as recent
    as possible. A failure is logged, never fatal."""
    from . import backup

    s = ctx.settings
    if engine is None or not s.backup_dir or s.role != "authority":
        return None
    with maker() as session:
        if session.get(NodeState, KEY) is not None or _own_head(session) is None or _marker_is_wire(session):
            return None
    try:
        out = backup.scheduled(s, engine)
        backup.verify(out)
        log.info("point-of-no-return backup written and verified: %s", out)
        return str(out)
    except Exception as exc:  # noqa: BLE001 - the upgrade must not fail on a full disk; the operator is told
        log.error("the point-of-no-return backup failed (%s); take one with `atlas backup create` before relying "
                  "on a rollback", exc)
        return None


# ---- the lease ----------------------------------------------------------------------------------------------------

def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def _acquire(session: Session, now: datetime) -> bool:
    """Compare-and-set under the write lock (the caller holds it): take the lease unless another holder has it."""
    row = session.get(NodeState, LEASE)
    if row is not None:
        try:
            held = json.loads(row.value)
            until = datetime.fromisoformat(held["until"])
            if held.get("holder") != _holder() and until > now:
                return False
        except (ValueError, KeyError, TypeError):
            pass
    value = json.dumps({"holder": _holder(), "until": (now + timedelta(seconds=LEASE_SECONDS)).isoformat()})
    if row is None:
        session.add(NodeState(key=LEASE, value=value))
    else:
        row.value = value
    session.flush()
    return True


# ---- stages -------------------------------------------------------------------------------------------------------

def complete(journal: dict[str, Any] | None) -> bool:
    return journal is None or all(v in (DONE, NA) for v in journal.get("stages", {}).values())


def _sync_with_identity(session: Session, ctx: Any, journal: dict[str, Any]) -> bool:
    """§6.3 item 4: the authority stages follow the identity, not the start."""
    changed = False
    authority = ctx.settings.role == "authority"
    node = ctx.identity.node_id
    stages = journal["stages"]
    if authority and journal.get("stages_for") != node:  # a fresh upgrade, a promotion, `recover-mirror`
        for name in AUTHORITY_STAGES:
            want = PENDING
            if name == "introduce_all" and ctx.settings.federation_introduce != "all":
                want = NA
            if stages.get(name) != want:
                stages[name] = want
                changed = True
        journal["cursors"].pop("relations", None)
        journal["authority_for"] = None
        journal["stages_for"] = node
        changed = True
    elif not authority:
        for name in AUTHORITY_STAGES:
            if stages.get(name) == PENDING:
                stages[name] = NA
                changed = True
    return changed


def _done(journal: dict[str, Any], stage: str) -> None:
    journal["stages"][stage] = DONE
    journal["last_error"] = None


def _stage_replay(session: Session, journal: dict[str, Any]) -> None:
    from . import sync

    marker = session.get(NodeState, "sync_replayed_for")
    if marker is not None and marker.value == sync.replay_marker():
        _done(journal, "replay")
        if journal["stages"].get("rescan") == PENDING:  # `startup_replay` ran `replay`, which holds the rescan
            _done(journal, "rescan")


def _stage_introduce(session: Session, ctx: Any, journal: dict[str, Any], scope: str) -> None:
    result = feed.introduce_batch(session, ctx, scope, cursor=f"upgrade:{scope}", clear_pending=False)
    journal["cursors"][f"introduce_{scope}"] = result["left"]
    if result["finished"]:
        _done(journal, f"introduce_{scope}")


def _stage_relations(session: Session, ctx: Any, journal: dict[str, Any]) -> None:
    """The pending-relation publication (§6.2 step 2): curator relations a v0 release stored while it gated them."""
    from . import relations

    after = int(journal["cursors"].get("relations") or 0)
    rows = list(session.scalars(select(Relation).where(Relation.id > after).order_by(Relation.id).limit(RELATION_BATCH)))
    last = after
    for row in rows:
        last = row.id
        if not relations._pending_publication(row):
            continue
        try:
            with session.begin_nested():
                relations.publish_pending(session, ctx, row)
        except AtlasError as exc:  # a relation that now conflicts with the published rows is never signed
            journal["skipped_conflict"] = int(journal.get("skipped_conflict") or 0) + 1
            journal["last_error"] = f"relation {row.id}: {exc}"[:300]
            continue
        if row.feed_seq is None:  # a C14 ceiling'd record: never signed (§4.1 rule 3)
            journal["skipped_scope_ceiling"] = int(journal.get("skipped_scope_ceiling") or 0) + 1
    journal["cursors"]["relations"] = last
    if len(rows) < RELATION_BATCH:
        _done(journal, "relations")


def step(maker: sessionmaker[Session], ctx: Any) -> dict[str, Any] | None:
    """One batch of the first unfinished stage (a cheap `NodeState` read once everything is done). Returns the
    journal, or None when there is none. Never raises: a failure goes to `last_error` and the stage stays pending."""
    from .service import write_lock

    with maker() as session:
        journal = _read(session)
        if journal is None or complete(journal) and journal.get("authority_for") == ctx.identity.node_id:
            return journal
        if complete(journal) and ctx.settings.role != "authority":
            return journal
    now = datetime.now(UTC)
    with maker() as session:
        write_lock(session)
        journal = _read(session)
        if journal is None or not _acquire(session, now):
            session.commit()
            return journal
        _sync_with_identity(session, ctx, journal)
        _write(session, journal)
        session.commit()
    stages = journal["stages"]
    for stage in STAGES:
        if stages.get(stage) != PENDING:
            continue
        try:
            with maker() as session:
                write_lock(session)
                journal = _read(session) or journal
                if journal["stages"].get(stage) != PENDING:
                    continue
                if stage in ("replay", "rescan"):
                    # passive: the sync loop's startup replay does the work; a stage it has not finished does not
                    # hold up the authority stages, which need no replay
                    _stage_replay(session, journal)
                    _write(session, journal)
                    session.commit()
                    if journal["stages"].get(stage) == PENDING:
                        continue
                elif stage == "relations":
                    _stage_relations(session, ctx, journal)
                else:
                    _stage_introduce(session, ctx, journal, stage.removeprefix("introduce_"))
                if ctx.settings.role == "authority" and all(
                        journal["stages"].get(s) in (DONE, NA) for s in AUTHORITY_STAGES):
                    journal["authority_for"] = ctx.identity.node_id
                _write(session, journal)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - recorded, retried next round (back-off is the round interval)
            log.warning("upgrade stage %s failed: %s", stage, exc)
            with maker() as session:
                journal = _read(session)
                if journal is not None:
                    journal["last_error"] = f"{stage}: {type(exc).__name__}: {exc}"[:300]
                    _write(session, journal)
                    session.commit()
        break  # one batch per call
    with maker() as session:
        return _read(session)


# ---- reporting ----------------------------------------------------------------------------------------------------

def status(session: Session) -> dict[str, Any] | None:
    """`{stage, left, last_error, skipped_scope_ceiling, complete, stages}` for admin `/status`; None without a journal."""
    journal = _read(session)
    if journal is None:
        return None
    pending = [s for s in STAGES if journal["stages"].get(s) == PENDING]
    left = journal["cursors"].get("introduce_" + pending[0].removeprefix("introduce_")) if pending else None
    return {"stage": pending[0] if pending else None, "left": left if isinstance(left, int) else None,
            "last_error": journal.get("last_error"), "skipped_scope_ceiling": journal.get("skipped_scope_ceiling", 0),
            "complete": not pending, "from": journal.get("from"), "first_v1_seq": journal.get("first_v1_seq"),
            "stages": journal["stages"]}


def unfinished(session: Session) -> bool:
    journal = _read(session)
    return journal is not None and not complete(journal)


def overdue(session: Session, now: datetime | None = None) -> bool:
    """`atlas check` warns about a journal still unfinished after 24 hours."""
    journal = _read(session)
    if journal is None or complete(journal):
        return False
    try:
        started = datetime.fromisoformat(journal["started_at"])
    except (KeyError, ValueError):
        return False
    return (now or datetime.now(UTC)) - started > STALE_AFTER
