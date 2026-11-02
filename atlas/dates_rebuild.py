"""Re-derive every record's view after the release-date merge changed (C5.2): `atlas dates rebuild`.

Migration 0031 already backfills the first release from each record's existing `release_date`, so a node
works without this job. Running it applies the hierarchical vote and the regional entries to records whose
observations disagree or carry `ext.release`. It works in batches of `batch` records, each in its own
transaction under the writer lock, and stores the last finished id in `node_state` so it can resume.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import Canonical, NodeState
from .service import Ctx, rebuild, write_lock

CURSOR = "release_rebuild_cursor"


def run(maker: Any, ctx: Ctx, *, batch: int = 500, restart: bool = False, limit: int | None = None) -> dict[str, Any]:
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
