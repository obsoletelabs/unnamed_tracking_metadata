"""This node's append-only signed change feed."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ChangeLog, NodeState
from .signing import NodeIdentity, body_hash


def append(session: Session, identity: NodeIdentity, kind: str, body: dict[str, Any]) -> int:
    """Append an entry. Holds a row lock so seq order equals commit order (gap-free)."""
    head = session.execute(
        select(NodeState).where(NodeState.key == "feed_seq").with_for_update()
    ).scalar_one_or_none()
    if head is None:
        head = NodeState(key="feed_seq", value="0")
        session.add(head)
        session.flush()
    seq = int(head.value) + 1
    head.value = str(seq)
    digest = body_hash(body)
    session.add(
        ChangeLog(
            seq=seq, kind=kind, body=body, body_hash=digest,
            signature=identity.sign(seq, digest), key_id=identity.node_id,
        )
    )
    session.flush()
    return seq


def read(session: Session, since: int, limit: int) -> list[ChangeLog]:
    return list(
        session.scalars(
            select(ChangeLog).where(ChangeLog.seq > since).order_by(ChangeLog.seq).limit(limit)
        )
    )


def head_seq(session: Session) -> int:
    row = session.get(NodeState, "feed_seq")
    return int(row.value) if row else 0
