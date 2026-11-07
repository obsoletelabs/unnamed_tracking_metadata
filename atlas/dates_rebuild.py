"""Re-derive every record's view after the release-date merge changed (C5.2): `atlas dates rebuild`.

Migration 0031 already backfills the first release from each record's existing `release_date`, so a node
works without this job. Running it applies the hierarchical vote and the regional entries to records whose
observations disagree or carry `ext.release`. It works in batches of `batch` records, each in its own
transaction under the writer lock, and stores the last finished id in `node_state` so it can resume.

After a rollback (a downgrade of 0031 or 0034, Phase 4 defect D3) the same job restores what the downgrade removed
from the derived data: those downgrades leave `node_state["view_rebuild_pending"]`, and the application runs
`finish_pending` once the schema is current again (`atlas migrate`, `db.upgrade`, and every start until it is
done), so upgrading again really brings everything back without an operator step. On PostgreSQL one process claims the
work (a session advisory lock, released if it dies); every other process that starts meanwhile skips it. It keeps
its own cursor, so an operator's `atlas dates rebuild` in progress is neither reset nor advanced by it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text

from .models import Canonical, NodeState
from .service import Ctx, rebuild, write_lock

CURSOR = "release_rebuild_cursor"
#: Written by the downgrade of migrations 0031 and 0034 (the self-contained migrations spell it out); read here.
PENDING = "view_rebuild_pending"
_RUNNING = "running"
#: The cursor of the rollback repair (`finish_pending`): separate from `atlas dates rebuild`'s.
PENDING_CURSOR = "view_rebuild_cursor"
#: PostgreSQL session advisory lock id of the rollback repair (write_lock is 727274, the signing key 727275).
_CLAIM = 727276

log = logging.getLogger("atlas.dates_rebuild")


def run(maker: Any, ctx: Ctx, *, batch: int = 500, restart: bool = False, limit: int | None = None,
        index_conflicts: bool = False, cursor: str = CURSOR) -> dict[str, Any]:
    """`cursor` is the `node_state` key the progress is kept under. `index_conflicts` (the rollback repair): a
    record whose view and release columns are unchanged gets only its explain and `field_conflict` rows rewritten,
    without a version bump (explain is not client data, as in `atlas match explain-backfill`); a downgrade of 0034
    removes `explain._meta` and drops `field_conflict`."""
    from . import dates as dates_
    from .provenance import index_conflicts as index
    from .service import derive

    batch = max(1, min(batch, 5000))
    done = changed = 0
    with maker() as session:
        row = session.get(NodeState, cursor)
        if restart and row is not None:
            session.delete(row)
            session.commit()
            row = None
        last = row.value if row is not None else ""
    while limit is None or done < limit:
        take = batch if limit is None else min(batch, limit - done)
        with maker() as session:
            write_lock(session)
            ids = list(session.scalars(select(Canonical.id).where(Canonical.id > last, Canonical.merged_into.is_(None))
                                       .order_by(Canonical.id).limit(take)))
            if not ids:
                state = session.get(NodeState, cursor)
                if state is not None:
                    session.delete(state)  # finished: the next run starts from the beginning
                session.commit()
                return {"rebuilt": done, "changed": changed, "finished": True}
            for cid in ids:
                if index_conflicts:
                    rec = session.get(Canonical, cid)
                    view, explain = derive(session, ctx, cid)
                    columns = dates_.columns(view.get("first_release"))
                    if rec.view == view and all(getattr(rec, k) == v for k, v in columns.items()):
                        if rec.explain != explain:
                            rec.explain = explain
                        index(session, cid, explain)
                        continue
                before = session.get(Canonical, cid).version
                if rebuild(session, ctx, cid, cause="local", only_if_changed=True).version != before:
                    changed += 1
            last = ids[-1]
            state = session.get(NodeState, cursor)
            if state is None:
                session.add(NodeState(key=cursor, value=last))
            else:
                state.value = last
            session.commit()
            done += len(ids)
    return {"rebuilt": done, "changed": changed, "finished": False, "cursor": last}


def finish_pending(maker: Any, settings: Any) -> dict[str, Any] | None:
    """Re-derive every live record once after a rollback and re-upgrade (see the module docstring). None when no
    downgrade asked for it, `{"busy": True, ...}` when another process holds the claim. Resumable: an interrupted
    run continues from its cursor at the next start; the marker goes only when the last batch is done. Records
    whose derived data is unchanged keep their version."""
    with maker() as session:
        if session.get(NodeState, PENDING) is None:
            return None  # the common case: no claim, no extra connection
        engine = session.get_bind()
    if engine.dialect.name != "postgresql":
        return _finish(maker, settings)
    with engine.connect() as claim:  # held until the work is done; a dead process's connection frees it
        if not claim.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _CLAIM}).scalar():
            claim.rollback()
            log.info("another process is re-deriving the records after a downgrade; not starting a second run")
            return {"rebuilt": 0, "changed": 0, "finished": False, "busy": True}
        claim.commit()  # a session lock outlives the transaction: no transaction stays open meanwhile
        try:
            return _finish(maker, settings)
        finally:
            claim.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _CLAIM})
            claim.commit()


def _finish(maker: Any, settings: Any) -> dict[str, Any] | None:
    from . import runtime
    from .signing import load_identity

    with maker() as session:
        row = session.get(NodeState, PENDING)
        if row is None:
            return None  # the process that held the claim before finished it
        restart = row.value != _RUNNING
        row.value = _RUNNING
        ctx = Ctx(runtime.effective(settings, runtime.load(session)), load_identity(session, settings))
        session.commit()
    done = run(maker, ctx, restart=restart, index_conflicts=True, cursor=PENDING_CURSOR)
    with maker() as session:
        row = session.get(NodeState, PENDING)
        if row is not None:
            session.delete(row)
        session.commit()
    return done
