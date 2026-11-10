"""Shared contribution state inside the owner group (CONTRACT-C14 §7.7, owner decision H67).

A node that proposes changes to a main Atlas (C14-3n) records each contribution's state here with `record`; the
state is a C10 group object (`t = "group.contrib_state"`, its own signing domain) signed by this node, holding no
payload value, no reviewer note and no account data. Members pull each other's items with the sealed `link.state`
op during the group round (`pull`). A received item is stored only when its signer is the member that was called,
under its current key, keyed by `(origin_node = signer, contribution_id)`, so no member can speak for another's
links; an older `updated_at` than the stored one is ignored. Received items are `group` scope: never public,
never sent upstream, never served onward (a node serves only its own items).

`group_share_contribution_state` (on by default inside a group) switches it per member: with it off a node neither
serves nor pulls, and its own items are replaced by `forgotten` tombstones for the others to drop. Items of a member
removed from the group (CRL) are dropped on the next round.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import group_crypto as gc
from .models import GroupContribState, now

STATES = ("queued", "sent", "pending", "approved", "rejected", "reverted", "withdrawn", "failed", "link_revoked",
          "unsent", "forgotten")
KINDS = ("edit", "record", "artwork", "relation", "merge", "other")
PAGE = 200
ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,40}\Z")
NODE_RE = re.compile(r"^[0-9a-f]{16}\Z")
CID_RE = re.compile(r"^atlas:[0-9a-z]{26}\Z")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}\Z")


class StateError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "group_share_contribution_state", True))


def _body_ok(body: Any) -> bool:
    if not isinstance(body, dict) or set(body) != {"main", "link_subject_hint", "contribution_id", "kind",
                                                   "local_cid", "main_cid", "fields", "state", "decided_at",
                                                   "feed_seq", "updated_at"}:
        return False
    return (isinstance(body["main"], str) and NODE_RE.match(body["main"]) is not None
            and isinstance(body["link_subject_hint"], str) and re.fullmatch(r"[0-9a-f]{4}", body["link_subject_hint"])
            is not None
            and isinstance(body["contribution_id"], str) and ID_RE.match(body["contribution_id"]) is not None
            and body["kind"] in KINDS and body["state"] in STATES
            and all(v is None or (isinstance(v, str) and CID_RE.match(v)) for v in (body["local_cid"],
                                                                                    body["main_cid"]))
            and isinstance(body["fields"], list) and len(body["fields"]) <= 64
            and all(isinstance(f, str) and FIELD_RE.match(f) for f in body["fields"])
            and (body["decided_at"] is None or _when(body["decided_at"]) is not None)
            and (body["feed_seq"] is None or (type(body["feed_seq"]) is int and body["feed_seq"] >= 0))
            and _when(body["updated_at"]) is not None)


def _when(text: Any) -> datetime | None:
    if not isinstance(text, str) or len(text) > 40:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def record(session: Session, settings: Any, identity: Any, *, main: str, subject: str, contribution_id: str,
           kind: str, state: str, local_cid: str | None = None, main_cid: str | None = None,
           fields: list[str] | None = None, decided_at: datetime | None = None, feed_seq: int | None = None) -> bool:
    """Sign and keep this node's own contribution state for the group (the C14-3n queue calls this on every state
    change). Returns False outside a group or with the policy off (nothing is kept)."""
    from . import groups

    group = groups.active(session)
    if group is None or not enabled(settings):
        return False
    body = {"main": main, "link_subject_hint": subject[:4].lower(), "contribution_id": contribution_id, "kind": kind,
            "local_cid": local_cid, "main_cid": main_cid, "fields": sorted(set(fields or []))[:64], "state": state,
            "decided_at": gc.stamp(decided_at) if decided_at else None, "feed_seq": feed_seq,
            "updated_at": gc.stamp()}
    if not _body_ok(body):
        raise StateError("invalid")
    obj = gc.make_object("group.contrib_state", group.group_id, f"contrib:{identity.node_id}/{contribution_id}", body,
                         {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, identity.private)
    row = session.get(GroupContribState, (identity.node_id, contribution_id))
    if row is None:
        session.add(GroupContribState(origin_node=identity.node_id, contribution_id=contribution_id, envelope=obj,
                                      updated_at=now(), received_at=now()))
    else:
        row.envelope, row.updated_at = obj, now()
    session.flush()
    return True


def forget_own(session: Session, identity: Any) -> int:
    """Tombstone every own item (`forgotten`): the link was deleted or the policy was turned off. Tombstones are
    still served with the policy off, so the other members drop their copies."""
    from . import groups

    group = groups.active(session)
    if group is None:
        return 0
    n = 0
    for row in session.scalars(select(GroupContribState).where(GroupContribState.origin_node == identity.node_id)):
        body = dict((row.envelope or {}).get("body") or {})
        if not body or body.get("state") == "forgotten":
            continue
        body.update(state="forgotten", updated_at=gc.stamp())
        row.envelope = gc.make_object("group.contrib_state", group.group_id,
                                      f"contrib:{identity.node_id}/{row.contribution_id}", body,
                                      {"kind": "node", "id": identity.node_id, "key_id": identity.key_id},
                                      identity.private)
        row.updated_at = now()
        n += 1
    session.flush()
    return n


def serve(session: Session, settings: Any, identity: Any, params: Any) -> dict:
    """`link.state`: this node's own items only (received ones are never served onward), after `since`."""
    since = params.get("since") if isinstance(params, dict) else None
    if since is not None and (not isinstance(since, str) or not ID_RE.match(since)):
        raise StateError("invalid")
    q = select(GroupContribState).where(GroupContribState.origin_node == identity.node_id)
    if since:
        q = q.where(GroupContribState.contribution_id > since)
    rows = list(session.scalars(q.order_by(GroupContribState.contribution_id).limit(PAGE)))
    off = not enabled(settings)  # with the policy off only tombstones travel, so the others drop their copies
    return {"items": [r.envelope for r in rows
                      if not off or ((r.envelope or {}).get("body") or {}).get("state") == "forgotten"],
            "next": rows[-1].contribution_id if len(rows) == PAGE else None}


def accept(session: Session, group: Any, tr: Any, server: str, obj: Any) -> bool:
    """Store one item pulled from `server`: signed by `server` itself under its current key, a valid body, and not
    older than what is stored."""
    if not isinstance(obj, dict) or obj.get("t") != "group.contrib_state" or obj.get("group_id") != group.group_id:
        return False
    signer = obj.get("signer") or {}
    key = tr.current_key(server)
    if key is None or signer.get("kind") != "node" or signer.get("id") != server or signer.get("key_id") != key[0]:
        return False
    if not gc.verify_object(obj, key[1]) or not _body_ok(obj.get("body")):
        return False
    body = obj["body"]
    row = session.get(GroupContribState, (server, body["contribution_id"]))
    if row is not None:
        held = _when(((row.envelope or {}).get("body") or {}).get("updated_at"))
        if held is not None and _when(body["updated_at"]) <= held:
            return False
        row.envelope, row.updated_at, row.received_at = obj, _when(body["updated_at"]), now()
    else:
        session.add(GroupContribState(origin_node=server, contribution_id=body["contribution_id"], envelope=obj,
                                      updated_at=_when(body["updated_at"]), received_at=now()))
    session.flush()
    return True


def pull(session: Session, settings: Any, identity: Any, group: Any, tr: Any, http: Any, call: Any) -> dict[str, int]:
    """One round: drop items of members no longer in the group, then pull every member that serves `link.state`."""
    from . import assist

    members = set(tr.members())
    dropped = session.execute(delete(GroupContribState).where(
        GroupContribState.origin_node != identity.node_id,
        GroupContribState.origin_node.not_in(members or {""}))).rowcount or 0
    out = {"stored": 0, "dropped": dropped}
    if not enabled(settings):
        out["forgotten"] = forget_own(session, identity)
        return out
    for node in sorted(members - {identity.node_id}):
        if not assist.supports(session, node, "link.state"):
            continue
        since = None
        for _ in range(20):
            try:
                got = call(session, settings, identity, group, tr.members()[node]["body"]["member"], "link.state",
                           {"since": since}, http)
            except Exception:  # noqa: BLE001 - a member with the policy off, or unreachable, shares nothing
                break
            if not isinstance(got, dict):
                break
            for obj in (got.get("items") or [])[:PAGE]:
                if accept(session, group, tr, node, obj):
                    out["stored"] += 1
            since = got.get("next")
            if not isinstance(since, str):
                break
    for row in session.scalars(select(GroupContribState).where(GroupContribState.origin_node != identity.node_id)):
        if ((row.envelope or {}).get("body") or {}).get("state") == "forgotten":
            session.delete(row)
    session.flush()
    return out


def received(session: Session, own_node: str, local_cid: str | None = None) -> list[dict]:
    """Items other members shared, for the admin group page and the record page (signed-in operators only)."""
    q = select(GroupContribState).where(GroupContribState.origin_node != own_node)
    rows = list(session.scalars(q.order_by(GroupContribState.updated_at.desc()).limit(200)))
    out = []
    for r in rows:
        body = (r.envelope or {}).get("body") or {}
        if local_cid is not None and body.get("local_cid") != local_cid:
            continue
        out.append({"node": r.origin_node, **{k: body.get(k) for k in ("contribution_id", "kind", "state", "local_cid",
                                                                       "main_cid", "fields")},
                    "decided_at": _when(body.get("decided_at")), "updated_at": r.updated_at})
    return out
