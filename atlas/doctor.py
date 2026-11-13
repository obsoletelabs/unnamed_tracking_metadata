"""`atlas doctor media-types` (CONTRACT-C15 §2.2, A20): records an older release filed as games by mistake.

Before A20 a follower that was sent an `obs` or `record` naming a media type it did not have created the record as a
`game` (`sync._ensure_canonical`'s old fallback). A20 makes such entries wait for the type instead, but the records
the old fallback already created stay misfiled. This module finds them: a live record of type `game` whose
originating replicated `obs`/`record` (the first entry of a feed that created it) named another type that this node
now knows. It never retypes on its own (a curator may have edited the record since); `retype` does it per peer feed,
on an administrator's request, audited.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEntry, Canonical, MediaType, NodeState, Peer, ReplicatedChange
from .service import Ctx, rebuild, write_lock

log = logging.getLogger("atlas.doctor")

#: node_state key of the startup scan: `<sorted known media types>|<number found then>` (re-scans when the types change).
SCANNED = "doctor_media_types_scanned"
_KINDS = ("obs", "record")


def misfiled(session: Session, node_id: str | None = None) -> list[dict[str, Any]]:
    """Every misfiled record: `{canonical_id, node_id, seq, named, current, title}`, in (feed, seq) order. Read-only.

    The originating entry is the lowest seq of each feed's `obs`/`record` entries for that record id; a record two
    feeds introduced with different types is reported once per feed."""
    query = select(ReplicatedChange.node_id, ReplicatedChange.seq, ReplicatedChange.body).where(
        ReplicatedChange.kind.in_(_KINDS))
    if node_id is not None:
        query = query.where(ReplicatedChange.node_id == node_id)
    first: dict[tuple[str, str], tuple[int, Any]] = {}
    for node, seq, body in session.execute(query.order_by(ReplicatedChange.node_id, ReplicatedChange.seq)
                                           .execution_options(yield_per=500)):
        if not isinstance(body, dict) or not isinstance(body.get("canonical_id"), str):
            continue
        first.setdefault((node, body["canonical_id"]), (seq, body.get("media_type")))
    known = set(session.scalars(select(MediaType.key)))
    out = []
    for (node, cid), (seq, named) in first.items():
        if not isinstance(named, str) or named in ("", "game") or named not in known:
            continue
        row = session.get(Canonical, cid)
        if row is None or row.merged_into is not None or row.media_type != "game":
            continue
        title = (row.view or {}).get("title")
        out.append({"canonical_id": cid, "node_id": node, "seq": seq, "named": named, "current": row.media_type,
                    "title": title if isinstance(title, str) else None})
    return out


def report(session: Session) -> dict[str, Any]:
    """Grouped by peer feed, with its name, for the CLI and the startup notice."""
    names = {n: name for n, name in session.execute(select(Peer.node_id, Peer.name))}
    peers: dict[str, dict[str, Any]] = {}
    for item in misfiled(session):
        group = peers.setdefault(item["node_id"], {"node_id": item["node_id"], "peer": names.get(item["node_id"]),
                                                   "count": 0, "types": {}, "records": []})
        group["count"] += 1
        group["types"][item["named"]] = group["types"].get(item["named"], 0) + 1
        if len(group["records"]) < 200:
            group["records"].append(item)
    return {"count": sum(g["count"] for g in peers.values()), "peers": list(peers.values())}


def retype(session: Session, ctx: Ctx, node_id: str, *, actor_id: str | None = None,
           only: set[str] | None = None) -> int:
    """"Retype" for one peer feed: give each of its misfiled records the type its originating entry named, then
    rebuild the record. Local only (the authority's type was right; nothing is signed). Audited, once per call."""
    write_lock(session)
    changed = []
    for item in misfiled(session, node_id):
        if only is not None and item["canonical_id"] not in only:
            continue
        row = session.get(Canonical, item["canonical_id"])
        if row is None or row.media_type != "game":
            continue
        row.media_type = item["named"]
        session.flush()
        rebuild(session, ctx, row.id)
        changed.append(item["canonical_id"])
    if changed:
        session.add(AuditEntry(actor_id=actor_id, action="doctor_retype", target=node_id[:128],
                               detail={"count": len(changed), "records": changed[:100]}))
        session.flush()
        log.info("retyped %d record(s) from %s", len(changed), node_id)
    return len(changed)


def _types_marker(session: Session) -> str:
    """The value the scan is keyed to: the sorted set of media types this node knows. A misfile can only be reported
    once its named type exists here, so a type that arrives later (A21 seeding `book`, a custom type from a schema,
    one an administrator creates) must make the scan run again."""
    return ",".join(sorted(session.scalars(select(MediaType.key))))


def startup_scan(session: Session) -> int | None:
    """Count misfiled records and log a notice, once per set of known media types: at startup after the upgrade to
    A20, and again (startup or a later sync round) whenever a type was added or removed since the last scan. None
    when the known types have not changed since then. Cheap to call often: the scan itself only runs on a change."""
    marker = _types_marker(session)
    state = session.get(NodeState, SCANNED)
    previous = state.value if state is not None else None
    if previous is not None and previous.split("|", 1)[0] == marker:
        return None
    found = report(session)
    value = f"{marker}|{found['count']}"
    if state is None:
        session.add(NodeState(key=SCANNED, value=value))
    else:
        state.value = value
    session.flush()
    if found["count"]:
        log.warning("%d record(s) were filed as games by an older release although their upstream named another "
                    "media type; review them with `atlas doctor media-types` (nothing is changed automatically)",
                    found["count"])
    return found["count"]
