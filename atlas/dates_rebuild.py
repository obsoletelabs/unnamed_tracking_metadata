"""Re-derive every record's view after the release-date merge changed (C5.2): `atlas dates rebuild`.

Migration 0031 already backfills the first release from each record's existing `release_date`, so a node
works without this job. Running it applies the hierarchical vote and the regional entries to records whose
observations disagree or carry `ext.release`. It works in batches of `batch` records, each in its own
transaction under the writer lock, and stores the last finished id in `node_state` so it can resume.

After a rollback (a downgrade of 0031 or 0034, Phase 4 defect D3) the same job restores what the downgrade removed
from the derived data: those downgrades leave `node_state["view_rebuild_pending"]`, and the application runs
`finish_pending` once the schema is current again (`atlas migrate`, `db.upgrade`, and every start until it is
done), so upgrading again really brings everything back without an operator step.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import Canonical, NodeState
from .service import Ctx, rebuild, write_lock

CURSOR = "release_rebuild_cursor"
#: Written by the downgrade of migrations 0031 and 0034 (the self-contained migrations spell it out); read here.
PENDING = "view_rebuild_pending"
_RUNNING = "running"


def run(maker: Any, ctx: Ctx, *, batch: int = 500, restart: bool = False, limit: int | None = None,
        index_conflicts: bool = False) -> dict[str, Any]:
    """`index_conflicts` (the rollback repair): a record whose view and release columns are unchanged gets only its
    explain and `field_conflict` rows rewritten, without a version bump (explain is not client data, as in
    `atlas match explain-backfill`); a downgrade of 0034 removes `explain._meta` and drops `field_conflict`."""
    from . import dates as dates_
    from .provenance import index_conflicts as index
    from .service import derive

    batch = max(1, min(batch, 5000))
    done = changed = 0
    with maker() as session:
        row = session.get(NodeState, CURSOR)
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
                state = session.get(NodeState, CURSOR)
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
            state = session.get(NodeState, CURSOR)
            if state is None:
                session.add(NodeState(key=CURSOR, value=last))
            else:
                state.value = last
            session.commit()
            done += len(ids)
    return {"rebuilt": done, "changed": changed, "finished": False, "cursor": last}


def finish_pending(maker: Any, settings: Any) -> dict[str, Any] | None:
    """Re-derive every live record once after a rollback and re-upgrade (see the module docstring). None when no
    downgrade asked for it. Resumable: an interrupted run continues from its cursor at the next start; the marker
    goes only when the last batch is done. Records whose derived data is unchanged keep their version."""
    from . import runtime
    from .signing import load_identity

    with maker() as session:
        row = session.get(NodeState, PENDING)
        if row is None:
            return None
        restart = row.value != _RUNNING
        row.value = _RUNNING
        ctx = Ctx(runtime.effective(settings, runtime.load(session)), load_identity(session, settings))
        session.commit()
    done = run(maker, ctx, restart=restart, index_conflicts=True)
    with maker() as session:
        row = session.get(NodeState, PENDING)
        if row is not None:
            session.delete(row)
        session.commit()
    return done
