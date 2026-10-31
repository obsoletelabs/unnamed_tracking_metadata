"""This node's append-only signed change feed."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import feed_schema
from .models import ChangeLog, NodeState
from .signing import NodeIdentity, body_hash, load_identity


def emit(session: Session, ctx: Any, kind: str, body: dict[str, Any]) -> int:
    """Append an entry under this node's configured emit protocol (C1.6). Raises
    `feed_schema.EmitGateError` for a feature its followers may not understand yet."""
    return append(session, ctx.identity, kind, body, protocol=feed_schema.emit_protocol(ctx.settings))


def append(session: Session, identity: NodeIdentity, kind: str, body: dict[str, Any], *,
           protocol: str = feed_schema.PROTOCOL) -> int:
    """Append an entry. Holds a row lock so seq order equals commit order (gap-free).

    Fails closed: a kind missing from `atlas/feed_schema.py`, or a construct newer than `protocol`, is
    never signed. Version-1 bodies are signed exactly as before (no envelope keys are added)."""
    body = feed_schema.check_emit(kind, body, protocol)
    return append_unchecked(session, identity, kind, body)


def append_unchecked(session: Session, identity: NodeIdentity, kind: str, body: dict[str, Any]) -> int:
    """Sign and append without the emission gate. Only for tests and the federation lab, which use it
    to play a newer release that emits entries this release does not know."""
    head = session.execute(
        select(NodeState).where(NodeState.key == "feed_seq").with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if head is None:
        head = NodeState(key="feed_seq", value="0")
        session.add(head)
        session.flush()
    seq = int(head.value) + 1
    head.value = str(seq)
    # A different worker may have rotated the key. The database, under the feed lock,
    # decides which key signs this sequence; a long-lived Ctx must not keep using the old one.
    transaction = session.get_transaction()
    cached = session.info.get("atlas_feed_signer")
    if cached is None or cached[0] is not transaction:
        cached = (transaction, load_identity(session))
        session.info["atlas_feed_signer"] = cached
    signer = cached[1]
    if signer.node_id != identity.node_id:
        raise ValueError("cannot append a different node's identity to this feed")
    digest = body_hash(body)
    session.add(
        ChangeLog(
            seq=seq, kind=kind, body=body, body_hash=digest,
            signature=signer.sign(seq, kind, digest), key_id=signer.key_id,
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
