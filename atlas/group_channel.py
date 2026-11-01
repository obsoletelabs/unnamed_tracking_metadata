"""The owner-group private channel: change streams, epochs, store and forward, elision (CONTRACT-C10 §3.3-§3.8).

Entries are signed by their origin (`atlas-grp-change-v1`) and travel only inside sealed member calls
(`POST /api/v2/group/call`, ops `heads`, `changes`, `snapshot`, `notify`). Nothing here touches `feed`, `sync`,
`change_log`, `/api/v2/changes`, mirrors or the public metrics labels: group kinds are not in `feed_schema`, so
`feed.append` refuses them (test A11-a).

Streams are per `(origin, epoch, scope)`, where scope is an `account_uid` or `_node` (D10.16). Each stream carries a
contiguous `seq`; the receiver keeps a high-water mark (`hwm`) and every outcome is a `group_change` row
(`applied | noop | parked | held | elided | rejected`), so nothing is ever silently dropped.
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import group_crypto as gc
from . import group_schema, groups, ids, profile, profile_schema
from .group_schema import NODE_SCOPE, PROFILE_KINDS
from .models import (
    GroupAccountLink,
    GroupAccountNode,
    GroupChange,
    GroupIdpConsent,
    GroupRevoked,
    GroupStream,
    NodeState,
    OwnerGroup,
    ProfileConflict,
    ProfileField,
    ProfileVersion,
    User,
    UserSession,
)
from .signing import body_hash, canonical_json, message_parts_ok, sign_message, verify_message

log = logging.getLogger("atlas.group")
PAGE = 500
GRACE = timedelta(hours=72)
STALE = timedelta(days=400)
EPOCH_RATE = 4
REJECT_BODY_KEEP = timedelta(days=30)
HELLO_TTL = 3600.0
FORKED = "group_forked_origins"  # NodeState: {origin: {"at": ..., "seq": ...}}
EPOCH_DUE = "group_epoch_due"  # NodeState: reason for this node's next epoch ("restore")
SNAPSHOT_DUE = "group_snapshot_due"  # NodeState: "1" after a restore: fetch snapshots for every linked account
AUTH_REJECTS = ("not_linked", "field_not_allowed", "no_consent")
METRIC_REASONS = ("bad_signature", "origin_revoked", "origin_expired", "not_linked", "field_not_allowed", "no_consent",
                  "stale", "time_regression", "fork", "bad_elision", "key_superseded", "epoch_rate", "bad_base")
ASSERTION_TTL = timedelta(seconds=120)
MINT_WINDOW = timedelta(minutes=10)


class ChannelError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _t(text: Any) -> datetime:
    return gc.parse_time(text)


# ---- entries (§3.4) ---------------------------------------------------------------------------------------------

def subject_of(kind: str, body: dict | None) -> str:
    if kind in PROFILE_KINDS and isinstance(body, dict) and isinstance(body.get("field"), str):
        return body["field"]
    return "-"


def entry_parts(group_id: str, e: dict) -> list[str]:
    return [group_id, e["origin"], e["epoch"], e["scope"], str(e["seq"]), e["kind"], e["subject"], e["body_hash"]]


def ref(origin: str, epoch: str, scope: str, seq: int) -> str:
    return f"{origin}/{epoch}/{scope}/{seq}"


def sign_entry(identity: Any, group_id: str, epoch: str, scope: str, seq: int, kind: str, body: dict) -> dict:
    e = {"origin": identity.node_id, "epoch": epoch, "scope": scope, "seq": seq, "kind": kind,
         "subject": subject_of(kind, body), "key_id": identity.key_id, "body_hash": body_hash(body), "body": body}
    e["sig"] = sign_message(identity.private, gc.DOMAINS["change"], entry_parts(group_id, e))
    return e


def verify_entry(public_b64: str, group_id: str, e: dict) -> bool:
    try:
        return verify_message(public_b64, gc.DOMAINS["change"], entry_parts(group_id, e), e["sig"])
    except (KeyError, TypeError, ValueError):
        return False


def row_entry(row: GroupChange, elide: bool = False) -> dict:
    e = {"origin": row.origin, "epoch": row.epoch, "scope": row.scope, "seq": row.seq, "kind": row.kind,
         "subject": row.subject, "key_id": row.key_id, "body_hash": row.body_hash, "sig": row.sig}
    if elide or row.body is None:
        e.update(body=None, elided=True)
    else:
        e["body"] = row.body
    return e


_ENTRY_KEYS = {"origin", "epoch", "scope", "seq", "kind", "subject", "key_id", "body_hash", "sig", "body"}


def shape_ok(e: Any) -> bool:
    if not isinstance(e, dict) or not _ENTRY_KEYS <= set(e) or set(e) - _ENTRY_KEYS - {"elided"}:
        return False
    if type(e["seq"]) is not int or e["seq"] < 1:
        return False
    parts = [e[k] for k in ("origin", "epoch", "scope", "kind", "subject", "key_id", "body_hash", "sig")]
    if not all(isinstance(p, str) for p in parts) or not message_parts_ok(parts):
        return False
    if not group_schema.NODE.match(e["origin"]) or not group_schema.EPOCH.match(e["epoch"]):
        return False
    if e["scope"] != NODE_SCOPE and not group_schema.ULID.match(e["scope"]):
        return False
    if e.get("elided") is True:
        return e["body"] is None
    return isinstance(e["body"], dict)


# ---- this node's streams and epochs (§3.6, §1.7) ----------------------------------------------------------------

def _state(session: Session, key: str) -> NodeState | None:
    return session.get(NodeState, key)


def _json_state(session: Session, key: str) -> dict:
    import json

    row = _state(session, key)
    try:
        return json.loads(row.value) if row is not None and row.value else {}
    except ValueError:
        return {}


def _set_json_state(session: Session, key: str, value: dict) -> None:
    import json

    row = _state(session, key)
    text = json.dumps(value, sort_keys=True)
    if row is None:
        session.add(NodeState(key=key, value=text))
    else:
        row.value = text


def current_epoch(session: Session, group: OwnerGroup, node_id: str) -> GroupStream | None:
    return session.scalars(select(GroupStream).where(
        GroupStream.group_id == group.group_id, GroupStream.origin == node_id, GroupStream.scope == NODE_SCOPE,
        GroupStream.closed_at.is_(None)).order_by(GroupStream.opened_at.desc())).first()


def may_emit(session: Session, group: OwnerGroup | None, identity: Any, tr: groups.Trust | None = None) -> bool:
    """This node emits only while active and while its own in-force certificate carries `profile.sync`. A node whose
    certificate has expired keeps local versions and emits them after renewal (§1.5)."""
    if group is None or group.status != "active" or not group.self_serial:
        return False
    tr = tr or groups.trust(session, group, own_node=identity.node_id)
    cert = tr.in_force(identity.node_id)
    return cert is not None and "profile.sync" in cert["body"]["scopes"]


def _append(session: Session, group: OwnerGroup, identity: Any, epoch: str, scope: str, kind: str, body: dict,
            now: datetime) -> str:
    if len(canonical_json(body)) > group_schema.MAX_BODY:
        raise ChannelError("a change entry body is at most 8 KiB")
    stream = session.get(GroupStream, (group.group_id, identity.node_id, epoch, scope), with_for_update=True)
    if stream is None:
        stream = GroupStream(group_id=group.group_id, origin=identity.node_id, epoch=epoch, scope=scope, hwm=0,
                             key_id=identity.key_id, opened_at=now)
        session.add(stream)
        session.flush()
    seq = stream.hwm + 1
    e = sign_entry(identity, group.group_id, epoch, scope, seq, kind, body)
    session.add(GroupChange(group_id=group.group_id, origin=identity.node_id, epoch=epoch, scope=scope, seq=seq,
                            kind=kind, subject=e["subject"], body=body, body_hash=e["body_hash"], key_id=e["key_id"],
                            sig=e["sig"], hlc=body.get("hlc"), issued_at=now, received_from=None, received_at=now,
                            disposition="applied", reason="own"))
    stream.hwm, stream.last_issued_at = seq, now
    session.flush()
    return ref(identity.node_id, epoch, scope, seq)


def open_epoch(session: Session, group: OwnerGroup, identity: Any, reason: str, now: datetime | None = None) -> str:
    """Close this node's open streams and open a new epoch; its first entry (seq 1, scope `_node`) is
    `stream.epoch` with the previous epoch's heads, signed by the current key (§3.6, §1.7)."""
    now = now or utcnow()
    prev = current_epoch(session, group, identity.node_id)
    previous = None
    if prev is not None:
        streams = list(session.scalars(select(GroupStream).where(
            GroupStream.group_id == group.group_id, GroupStream.origin == identity.node_id,
            GroupStream.epoch == prev.epoch)))
        previous = {"epoch": prev.epoch, "heads": {s.scope: s.hwm for s in streams}}
        for s in streams:
            s.closed_at = now
    taken = set(session.scalars(select(GroupStream.epoch).where(GroupStream.group_id == group.group_id,
                                                                GroupStream.origin == identity.node_id)))
    epoch = secrets.token_hex(4)
    while epoch in taken:
        epoch = secrets.token_hex(4)
    session.add(GroupStream(group_id=group.group_id, origin=identity.node_id, epoch=epoch, scope=NODE_SCOPE, hwm=0,
                            key_id=identity.key_id, opened_at=now))
    session.flush()
    _append(session, group, identity, epoch, NODE_SCOPE, "stream.epoch",
            {"_v": 1, "epoch": epoch, "previous": previous, "reason": reason, "issued_at": gc.stamp(now)}, now)
    groups.audit(session, None, "group_epoch_opened", epoch, {"reason": reason,
                                                               "previous": previous["epoch"] if previous else None})
    due = _state(session, EPOCH_DUE)
    if due is not None:
        session.delete(due)
    session.flush()
    return epoch


def ensure_epoch(session: Session, group: OwnerGroup, identity: Any, now: datetime | None = None) -> str:
    """The open epoch for emitting; opens one at first membership, after a restore, and after a signing-key
    rotation (the rotation epoch bounds what the old key may still deliver, §1.7)."""
    cur = current_epoch(session, group, identity.node_id)
    due = _state(session, EPOCH_DUE)
    if cur is None:
        return open_epoch(session, group, identity, due.value if due is not None else "first", now)
    if due is not None:
        return open_epoch(session, group, identity, due.value, now)
    if cur.key_id != identity.key_id:
        return open_epoch(session, group, identity, "rotation", now)
    return cur.epoch


def local_link(session: Session, group_id: str, user_id: str) -> GroupAccountLink | None:
    return session.scalars(select(GroupAccountLink).where(GroupAccountLink.group_id == group_id,
                                                          GroupAccountLink.user_id == user_id)).first()


def link_by_uid(session: Session, group_id: str, uid: str) -> GroupAccountLink | None:
    return session.get(GroupAccountLink, (group_id, uid))


def _emitted(session: Session, user_id: str, field_name: str) -> set[str]:
    """HLCs of this register that are in the shared version set (emitted here, or received)."""
    return set(session.scalars(select(ProfileVersion.hlc).where(
        ProfileVersion.user_id == user_id, ProfileVersion.field == field_name, ProfileVersion.entry_ref.isnot(None))))


def emit_version(session: Session, settings: Any, identity: Any, user: User, version: ProfileVersion,
                 now: datetime | None = None) -> str | None:
    """Send a local version to the group when this user is linked, the field is accepted here (operator policy and
    the user's consent on this node) and this node may emit. Returns the entry reference, or None (local only).

    The emitted `base` names only versions in the shared set: local-only history (edits made before linking, or
    while the field was not synced) is never named, so no receiver waits for a version it can never get."""
    group = groups.active(session)
    if group is None:
        return None
    link = local_link(session, group.group_id, user.id)
    if link is None or version.field not in profile.accept(session, settings, user.id, group.group_id):
        return None
    if not may_emit(session, group, identity):
        return None
    now = now or utcnow()
    epoch = ensure_epoch(session, group, identity, now)
    shared = _emitted(session, user.id, version.field)
    body: dict[str, Any] = {"_v": 1, "account_uid": link.account_uid, "field": version.field}
    if not version.cleared:
        body["value"] = version.value
    body.update(hlc=version.hlc, base=[b for b in version.base if b in shared], cause=version.cause,
                issued_at=gc.stamp(now))
    kind = "profile.field.clear" if version.cleared else "profile.field.set"
    return _append(session, group, identity, epoch, link.account_uid, kind, body, now)


def emit_pending(session: Session, settings: Any, identity: Any, user: User, skip: set[str] | None = None,
                 now: datetime | None = None) -> int:
    """Emit this node's local-only head versions of every accepted field (after a link, a consent change or a
    certificate renewal). Returns how many were sent."""
    group = groups.active(session)
    if group is None or local_link(session, group.group_id, user.id) is None or not may_emit(session, group, identity):
        return 0
    sent = 0
    for name in profile.accept(session, settings, user.id, group.group_id):
        if skip and name in skip:
            continue
        for head in profile.heads(session, user.id, name):
            if head.entry_ref is None and head.origin_node == identity.node_id:
                head.entry_ref = emit_version(session, settings, identity, user, head, now)
                sent += head.entry_ref is not None
    return sent


def emit_resolve(session: Session, settings: Any, identity: Any, user: User, field_name: str, chosen: str,
                 discarded: list[str], now: datetime | None = None) -> str | None:
    group = groups.active(session)
    if group is None:
        return None
    link = local_link(session, group.group_id, user.id)
    if link is None or field_name not in profile.accept(session, settings, user.id, group.group_id) \
            or not may_emit(session, group, identity):
        return None
    now = now or utcnow()
    shared = _emitted(session, user.id, field_name)
    body = {"_v": 1, "account_uid": link.account_uid, "field": field_name, "chosen_hlc": chosen,
            "discarded": sorted(h for h in discarded if h in shared)[:64], "issued_at": gc.stamp(now)}
    return _append(session, group, identity, ensure_epoch(session, group, identity, now), link.account_uid,
                   "profile.conflict.resolve", body, now)


def _record_node(session: Session, group_id: str, uid: str, node: str, status: str, info: dict, entry: str,
                 issued: datetime) -> GroupAccountNode:
    row = session.get(GroupAccountNode, (group_id, uid, node))
    if row is None:
        row = GroupAccountNode(group_id=group_id, account_uid=uid, node_id=node, status=status, evidence=info,
                               entry_ref=entry, updated_at=issued)
        session.add(row)
    elif _aware(row.updated_at) <= issued:  # type: ignore[operator]
        row.status, row.evidence, row.entry_ref, row.updated_at = status, info, entry, issued
    session.flush()
    return row


def emit_link(session: Session, group: OwnerGroup, identity: Any, uid: str, *, minted: bool,
              evidence: dict | None = None, now: datetime | None = None) -> str:
    now = now or utcnow()
    body = {"_v": 1, "account_uid": uid, "node_id": identity.node_id, "minted": True if minted else None,
            "evidence": None if minted else evidence, "issued_at": gc.stamp(now)}
    entry = _append(session, group, identity, ensure_epoch(session, group, identity, now), uid, "account.link", body,
                    now)
    _record_node(session, group.group_id, uid, identity.node_id, "linked",
                 {"minted": minted, "evidence": evidence, "linked_at": gc.stamp(now)}, entry, now)
    return entry


def emit_unlink(session: Session, group: OwnerGroup, identity: Any, uid: str, reason: str,
                now: datetime | None = None) -> str | None:
    now = now or utcnow()
    entry = None
    if may_emit(session, group, identity):
        body = {"_v": 1, "account_uid": uid, "node_id": identity.node_id, "reason": reason,
                "issued_at": gc.stamp(now)}
        entry = _append(session, group, identity, ensure_epoch(session, group, identity, now), uid,
                        "account.unlink", body, now)
    _record_node(session, group.group_id, uid, identity.node_id, "unlinked", {"reason": reason}, entry or "-", now)
    return entry


# ---- linking, unlinking, deletion (§4.5, §5.3) -----------------------------------------------------------------

def mint_account(session: Session, settings: Any, group: OwnerGroup, identity: Any, user: User,
                 now: datetime | None = None) -> GroupAccountLink:
    """The user's `account_uid` here, minting one (and emitting `account.link {minted: true}`) when there is none."""
    link = local_link(session, group.group_id, user.id)
    if link is not None:
        return link
    now = now or utcnow()
    link = GroupAccountLink(group_id=group.group_id, account_uid=ids.ulid(), user_id=user.id, minted_here=True,
                            linked_at=now)
    session.add(link)
    session.flush()
    emit_link(session, group, identity, link.account_uid, minted=True, now=now)
    emit_pending(session, settings, identity, user, now=now)
    groups.audit(session, user.id, "account_linked", user.id, {"minted": True})
    return link


def complete_link(session: Session, settings: Any, group: OwnerGroup, identity: Any, user: User, uid: str,
                  assertion: dict, now: datetime | None = None) -> GroupAccountLink:
    """The relying-party side after the assertion verified (§5.3 step 9): store the link and emit `account.link`
    with the assertion as evidence."""
    now = now or utcnow()
    if local_link(session, group.group_id, user.id) is not None:
        raise ChannelError("this account is already linked in this group")
    if link_by_uid(session, group.group_id, uid) is not None:
        raise ChannelError("that group account is already linked to another account here")
    link = GroupAccountLink(group_id=group.group_id, account_uid=uid, user_id=user.id, minted_here=False,
                            linked_at=now)
    session.add(link)
    session.flush()
    emit_link(session, group, identity, uid, minted=False, evidence=assertion, now=now)
    groups.audit(session, user.id, "account_linked", user.id, {"idp_node": assertion["body"]["iss_node"]})
    return link


def unlink(session: Session, settings: Any, identity: Any, user: User, reason: str = "user",
           actor: str | None = None, now: datetime | None = None) -> bool:
    """Unlink this node from the user's group account: local values stay as plain local data; `account.unlink`
    tells the others to stop sending this account here (§4.5)."""
    group = groups.load(session)
    if group is None:
        return False
    link = local_link(session, group.group_id, user.id)
    if link is None:
        return False
    now = now or utcnow()
    emit_unlink(session, group, identity, link.account_uid, reason, now) if group.status == "active" else \
        _record_node(session, group.group_id, link.account_uid, identity.node_id, "unlinked", {"reason": reason}, "-",
                     now)
    session.execute(delete(GroupIdpConsent).where(GroupIdpConsent.user_id == user.id,
                                                  GroupIdpConsent.group_id == group.group_id))
    session.delete(link)
    groups.audit(session, actor or user.id, "account_unlinked", user.id, {"reason": reason})
    session.flush()
    return True


def on_account_deleted(session: Session, settings: Any, identity: Any, user: User) -> None:
    """`social.delete_account`: emit `account.unlink {reason: deleted}`, then drop every group and profile row of
    the user. Deletion never propagates (D10.21): the other nodes keep their own account and values."""
    if identity is not None:
        unlink(session, settings, identity, user, reason="deleted", actor=None)
    session.execute(delete(GroupAccountLink).where(GroupAccountLink.user_id == user.id))
    session.execute(delete(GroupIdpConsent).where(GroupIdpConsent.user_id == user.id))
    profile.delete_user(session, user.id)


# ---- serving (§3.3, §3.4 elision, §3.7 data minimisation) ------------------------------------------------------

def may_serve_account(session: Session, group_id: str, requester: str, uid: str) -> bool:
    """A requester gets an account scope only when it holds a live link for it (or this node asserted a link to
    it for that user, so the new node can fetch its snapshot)."""
    row = session.get(GroupAccountNode, (group_id, uid, requester))
    if row is not None and row.status == "linked":
        return True
    link = link_by_uid(session, group_id, uid)
    return link is not None and session.get(GroupIdpConsent, (link.user_id, group_id, requester)) is not None \
        and (row is None or row.status == "linked")


def _relays(tr: groups.Trust, node: str) -> bool:
    cert = tr.in_force(node)
    return cert is not None and "group.relay" in cert["body"]["scopes"]


def serve_heads(session: Session, group: OwnerGroup, own: str, requester: str, tr: groups.Trust) -> dict:
    relay = _relays(tr, own)
    out: dict[str, dict[str, dict[str, int]]] = {}
    for s in session.scalars(select(GroupStream).where(GroupStream.group_id == group.group_id, GroupStream.hwm > 0)):
        if s.origin == requester or (s.origin != own and not relay):
            continue
        if s.scope != NODE_SCOPE and not may_serve_account(session, group.group_id, requester, s.scope):
            continue
        out.setdefault(s.origin, {}).setdefault(s.epoch, {})[s.scope] = s.hwm
    return out


def _accept_param(params: dict) -> set[str] | None:
    accept = params.get("accept")
    if not isinstance(accept, list) or not all(isinstance(a, str) for a in accept) or len(accept) > 64:
        return None
    return set(accept)


def serve_changes(session: Session, group: OwnerGroup, own: str, requester: str, tr: groups.Trust,
                  params: dict) -> dict | str:
    """Up to 500 entries after `since`, verbatim (never re-signed). Profile entries whose field is not in the
    requester's signed `accept` are elided: the envelope stays, the value never leaves this node."""
    origin, epoch, scope, since = params.get("origin"), params.get("epoch"), params.get("scope"), params.get("since")
    limit = params.get("limit", PAGE)
    accept = _accept_param(params)
    if not (isinstance(origin, str) and isinstance(epoch, str) and isinstance(scope, str) and type(since) is int
            and since >= 0 and type(limit) is int and limit > 0 and accept is not None):
        return "not_found"
    if origin != own and not _relays(tr, own):
        return "forbidden"
    if scope != NODE_SCOPE and not may_serve_account(session, group.group_id, requester, scope):
        return "forbidden"
    rows = session.scalars(select(GroupChange).where(
        GroupChange.group_id == group.group_id, GroupChange.origin == origin, GroupChange.epoch == epoch,
        GroupChange.scope == scope, GroupChange.seq > since).order_by(GroupChange.seq).limit(min(limit, PAGE)))
    out, expect = [], since + 1
    for row in rows:
        if row.seq != expect:
            break  # serve contiguous entries only
        hide = row.kind in PROFILE_KINDS and row.subject not in accept
        if row.body is None and not hide:
            break  # a body this node does not hold is never served as elided to someone who accepts it (§3.4)
        out.append(row_entry(row, elide=hide))
        expect += 1
    return {"entries": out}


def serve_snapshot(session: Session, group: OwnerGroup, own: str, requester: str, params: dict) -> dict | str:
    """The signed original entries of the current heads of the requested fields, the account's link entries and
    the epoch entries that bind their keys (§4.4 snapshot; no unsigned bulk copy)."""
    uid, accept = params.get("account_uid"), _accept_param(params)
    if not isinstance(uid, str) or accept is None:
        return "not_found"
    if not may_serve_account(session, group.group_id, requester, uid):
        return "forbidden"
    link = link_by_uid(session, group.group_id, uid)
    if link is None:
        return "not_found"
    refs: list[str] = []
    for name in profile_schema.REGISTRY:
        if name in accept:
            refs += [h.entry_ref for h in profile.heads(session, link.user_id, name) if h.entry_ref]
    links = [r.entry_ref for r in session.scalars(select(GroupAccountNode).where(
        GroupAccountNode.group_id == group.group_id, GroupAccountNode.account_uid == uid,
        GroupAccountNode.status == "linked")) if r.entry_ref and r.entry_ref != "-"]
    entries, epochs = [], {}
    for item in links + refs:
        origin, epoch, scope, seq = item.split("/")
        row = session.get(GroupChange, (group.group_id, origin, epoch, scope, int(seq)))
        if row is None or row.body is None:
            continue
        entries.append(row_entry(row))
        first = session.get(GroupChange, (group.group_id, origin, epoch, NODE_SCOPE, 1))
        if first is not None and first.body is not None:
            epochs[(origin, epoch)] = row_entry(first)
    return {"entries": entries, "epochs": list(epochs.values())}


# ---- receiving (§3.4 receiver acceptance) -----------------------------------------------------------------------

@dataclass
class Outcome:
    disposition: str | None  # None: not stored (the stream is retried later)
    reason: str = ""
    stop: bool = False


@dataclass
class Rx:
    session: Session
    settings: Any
    identity: Any
    group: OwnerGroup
    tr: groups.Trust
    now: datetime
    served_by: str | None
    counts: dict[str, int] = field(default_factory=dict)
    origins: dict[str, int] = field(default_factory=dict)

    @property
    def gid(self) -> str:
        return self.group.group_id


def _keys_of(tr: groups.Trust, node: str) -> dict[str, str]:
    root = tr.root_public(node)
    if root is None:
        return {}
    found = tr._history(node, root)
    return dict(found[1]) if found else {}


def forked(session: Session) -> dict:
    return _json_state(session, FORKED)


def _store(rx: Rx, e: dict, disposition: str, reason: str | None, *, body: Any = "keep",
           issued: datetime | None = None) -> GroupChange:
    keep = e.get("body") if body == "keep" else body
    hlc = e["body"].get("hlc") if isinstance(e.get("body"), dict) and isinstance(e["body"].get("hlc"), str) else None
    row = GroupChange(group_id=rx.gid, origin=e["origin"], epoch=e["epoch"], scope=e["scope"], seq=e["seq"],
                      kind=e["kind"][:40], subject=e["subject"][:48], body=keep, body_hash=e["body_hash"],
                      key_id=e["key_id"], sig=e["sig"], hlc=hlc, issued_at=issued, received_from=rx.served_by,
                      received_at=rx.now, disposition=disposition, reason=reason)
    rx.session.add(row)
    if disposition == "rejected" and reason:
        rx.counts[reason] = rx.counts.get(reason, 0) + 1
        rx.origins[e["origin"]] = rx.origins.get(e["origin"], 0) + 1
    rx.session.flush()
    return row


def _advance(stream: GroupStream, seq: int, issued: datetime | None) -> None:
    stream.hwm = seq
    if issued is not None:
        stream.last_issued_at = issued


def _suspect_peer(rx: Rx, why: str) -> None:
    if rx.served_by:
        groups.audit(rx.session, None, "group_peer_suspect", rx.served_by, {"reason": why})
        rx.counts[why] = rx.counts.get(why, 0) + 1


def _fork(rx: Rx, e: dict, existing: GroupChange) -> Outcome:
    halted = forked(rx.session)
    if e["origin"] not in halted:
        halted[e["origin"]] = {"at": gc.stamp(rx.now), "seq": e["seq"], "epoch": e["epoch"], "scope": e["scope"]}
        _set_json_state(rx.session, FORKED, halted)
        groups.audit(rx.session, None, "group_stream_fork", e["origin"],
                     {"epoch": e["epoch"], "scope": e["scope"], "seq": e["seq"],
                      "body_hashes": sorted([existing.body_hash, e["body_hash"]])})
        groups.notify_admins(rx.session, f"group-fork:{e['origin']}",
                             "A node in your group sent two different histories: probably two copies of one node "
                             "are running (a cloned VM). Its changes are stopped until it is revoked or opens a new "
                             "epoch.")
    rx.counts["fork"] = rx.counts.get("fork", 0) + 1
    return Outcome(None, "fork", stop=True)


def key_bound(session: Session, group_id: str, origin: str, epoch: str, scope: str) -> int | None:
    """`previous.heads[scope]` of a rotation (or restore) epoch that closed `epoch`, once known (§1.7)."""
    for row in session.scalars(select(GroupChange).where(
            GroupChange.group_id == group_id, GroupChange.origin == origin, GroupChange.kind == "stream.epoch",
            GroupChange.scope == NODE_SCOPE, GroupChange.seq == 1)):
        prev = (row.body or {}).get("previous") or {}
        if prev.get("epoch") == epoch and row.disposition == "applied":
            return int((prev.get("heads") or {}).get(scope, 0))
    return None


def origin_authority(rx: Rx, origin: str, issued: datetime, stream: GroupStream, seq: int) -> str:
    """§3.4 step 2: the origin's certificate was valid at `issued_at` with `profile.sync`, and it is not revoked
    or banned now; after expiry only announced data inside the 72 h grace (§1.5)."""
    tr = rx.tr
    cert = tr.in_force(origin)
    if cert is not None:
        if "profile.sync" not in cert["body"]["scopes"]:
            return "origin_scope"
        if issued < _t(cert["body"]["not_before"]) - groups.SKEW:
            return "origin_expired"
        return "ok"
    status = tr.status_of(origin)
    if origin in tr.banned or status in ("revoked", "left"):
        return "origin_revoked"
    certs = [c for c in tr.node_certs(origin) if tr.genuine(c) and c["body"]["serial"] not in tr.revoked]
    if not certs:
        return "origin_revoked"
    latest = max(certs, key=lambda c: _t(c["body"]["not_after"]))
    end = _t(latest["body"]["not_after"])
    if rx.now > end + groups.SKEW:
        adv_at = _aware(stream.advertised_at)
        if ("profile.sync" in latest["body"]["scopes"] and issued <= end and rx.now <= end + GRACE
                and _aware(stream.opened_at) < end and stream.advertised_head is not None  # type: ignore[operator]
                and adv_at is not None and adv_at < end and seq <= stream.advertised_head):
            return "ok"
        return "origin_expired"
    return "origin_scope"


def receive(rx: Rx, e: Any, accept: set[str]) -> Outcome:
    """Verify and apply one entry served for `(origin, epoch, scope)`; the order of checks is normative (§3.4)."""
    s = rx.session
    if not shape_ok(e):
        _suspect_peer(rx, "bad_signature")
        return Outcome(None, "malformed", stop=True)
    origin, epoch, scope, seq = e["origin"], e["epoch"], e["scope"], e["seq"]
    if origin == rx.identity.node_id or origin in forked(s):
        return Outcome(None, "skipped", stop=True)
    keys = _keys_of(rx.tr, origin)
    stream = s.get(GroupStream, (rx.gid, origin, epoch, scope))
    existing = s.get(GroupChange, (rx.gid, origin, epoch, scope, seq))
    if existing is not None:
        if existing.body_hash != e["body_hash"]:
            public = keys.get(e["key_id"])
            if public is None or e["key_id"] != existing.key_id and stream is not None and \
                    e["key_id"] != stream.key_id or not verify_entry(public, rx.gid, e):
                _suspect_peer(rx, "bad_signature")
                return Outcome(None, "bad_signature", stop=True)
            return _fork(rx, e, existing)
        if stream is not None and seq == stream.hwm + 1:
            _advance(stream, seq, _aware(existing.issued_at))
        return Outcome("noop", "repeat")
    if scope == NODE_SCOPE and seq == 1 and stream is None:
        return _accept_epoch(rx, e, keys)
    epoch_row = s.get(GroupStream, (rx.gid, origin, epoch, NODE_SCOPE))
    if epoch_row is None:
        return Outcome(None, "unknown_epoch", stop=True)
    if epoch_row.blocked_reason == "epoch_refused":
        return Outcome(None, "epoch_refused", stop=True)
    public = keys.get(epoch_row.key_id)
    if e["key_id"] != epoch_row.key_id or public is None or not verify_entry(public, rx.gid, e):
        _suspect_peer(rx, "bad_signature")
        return Outcome(None, "bad_signature", stop=True)
    if stream is None:
        stream = GroupStream(group_id=rx.gid, origin=origin, epoch=epoch, scope=scope, hwm=0, key_id=epoch_row.key_id,
                             opened_at=rx.now)
        s.add(stream)
        s.flush()
    if stream.blocked_seq is not None:
        return Outcome(None, "blocked", stop=True)
    if seq <= stream.hwm:
        return Outcome("noop", "repeat")
    if seq > stream.hwm + 1:
        return Outcome(None, "gap", stop=True)
    current = rx.tr.current_key(origin)
    if current is None or current[0] != epoch_row.key_id:
        bound = stream.key_bound_seq if stream.key_bound_seq is not None else key_bound(s, rx.gid, origin, epoch,
                                                                                       scope)
        if bound is None:
            return Outcome(None, "key_superseded", stop=True)  # held until the rotation epoch arrives
        if seq > bound:
            _store(rx, e, "rejected", "key_superseded", body=None)
            _advance(stream, seq, None)
            return Outcome("rejected", "key_superseded")
    elif stream.key_bound_seq is not None and seq > stream.key_bound_seq:
        _store(rx, e, "rejected", "key_superseded", body=None)
        _advance(stream, seq, None)
        return Outcome("rejected", "key_superseded")
    if e.get("elided"):
        if e["kind"] in PROFILE_KINDS and e["subject"] in accept:
            _suspect_peer(rx, "bad_elision")
            return Outcome(None, "bad_elision", stop=True)
        _store(rx, e, "elided", None, body=None)
        _advance(stream, seq, None)
        return Outcome("elided")
    body = e["body"]
    if body_hash(body) != e["body_hash"]:
        _suspect_peer(rx, "bad_signature")
        return Outcome(None, "bad_signature", stop=True)
    try:
        issued = _t(body.get("issued_at"))
    except (gc.GroupCryptoError, TypeError, ValueError):
        _store(rx, e, "rejected", "invalid")
        _advance(stream, seq, None)
        return Outcome("rejected", "invalid")
    auth = origin_authority(rx, origin, issued, stream, seq)
    if auth != "ok":
        _store(rx, e, "rejected", auth, issued=issued)
        _advance(stream, seq, issued)
        return Outcome("rejected", auth)
    last = _aware(stream.last_issued_at)
    if last is not None and issued < last - groups.SKEW:
        _store(rx, e, "rejected", "time_regression", issued=issued)
        _advance(stream, seq, None)
        return Outcome("rejected", "time_regression")
    if issued > rx.now + groups.SKEW:
        return Outcome(None, "future", stop=True)
    if issued < rx.now - STALE:
        _store(rx, e, "rejected", "stale", issued=issued)
        _advance(stream, seq, None)
        return Outcome("rejected", "stale")
    out = group_schema.classify(e["kind"], body)
    if out.action == group_schema.PARK:
        _store(rx, e, "parked", out.reason[:32], issued=issued)
        stream.blocked_seq, stream.blocked_reason = seq, out.reason[:32]
        return Outcome("parked", out.reason, stop=True)
    if out.action == group_schema.QUARANTINE or not _consistent(e, body):
        _store(rx, e, "rejected", "invalid", issued=issued)
        _advance(stream, seq, issued)
        return Outcome("rejected", "invalid")
    if out.action == group_schema.HELD:
        _store(rx, e, "held", "unsupported_field", issued=issued)
        _advance(stream, seq, issued)
        return Outcome("held", "unsupported_field")
    result = _apply(rx, e, out.body or {}, issued, accept)
    if result.disposition is None:
        return result
    _advance(stream, seq, issued)
    return result


def _consistent(e: dict, body: dict) -> bool:
    kind = e["kind"]
    if kind == "stream.epoch":
        return False  # only as seq 1 of `_node`, handled by _accept_epoch
    if kind in group_schema.ACCOUNT_KINDS and (e["scope"] != body.get("account_uid") or e["scope"] == NODE_SCOPE):
        return False
    if e["subject"] != subject_of(kind, body):
        return False
    if kind in ("profile.field.set", "profile.field.clear") and body["hlc"].rsplit("-", 1)[1] != e["origin"]:
        return False
    return True


def _accept_epoch(rx: Rx, e: dict, keys: dict[str, str]) -> Outcome:
    """A new epoch from `origin` (§3.6): signed by its current key, newer than anything held from it, origin valid
    now (no grace), at most 4 per 24 h. Older epochs are closed at this node's hwm (or the announced heads)."""
    s, origin = rx.session, e["origin"]
    current = rx.tr.current_key(origin)
    body = e.get("body")
    if e.get("elided") or not isinstance(body, dict) or body_hash(body) != e["body_hash"] \
            or e["kind"] != "stream.epoch":
        _suspect_peer(rx, "bad_signature")
        return Outcome(None, "bad_signature", stop=True)
    public = keys.get(e["key_id"])
    if public is None or not verify_entry(public, rx.gid, e):
        _suspect_peer(rx, "bad_signature")
        return Outcome(None, "bad_signature", stop=True)

    def refuse(reason: str) -> Outcome:
        s.add(GroupStream(group_id=rx.gid, origin=origin, epoch=e["epoch"], scope=NODE_SCOPE, hwm=0,
                          key_id=e["key_id"], opened_at=rx.now, blocked_seq=1, blocked_reason="epoch_refused"))
        _store(rx, e, "rejected", reason, body=None)
        return Outcome("rejected", reason, stop=True)

    out = group_schema.classify("stream.epoch", body)
    if out.action != group_schema.APPLY or body.get("epoch") != e["epoch"] or e["subject"] != "-":
        return refuse("invalid")
    if current is None or current[0] != e["key_id"]:
        return refuse("key_superseded")
    cert = rx.tr.in_force(origin)
    if cert is None:
        return refuse("origin_revoked" if rx.tr.status_of(origin) in ("revoked", "left") else "origin_expired")
    issued = _t(body["issued_at"])
    newest = s.scalar(select(func.max(GroupStream.last_issued_at)).where(GroupStream.group_id == rx.gid,
                                                                       GroupStream.origin == origin))
    if newest is not None and issued < _aware(newest) - groups.SKEW:  # type: ignore[operator]
        return refuse("time_regression")
    if issued > rx.now + groups.SKEW:
        return Outcome(None, "future", stop=True)
    recent = s.scalar(select(func.count()).select_from(GroupStream).where(
        GroupStream.group_id == rx.gid, GroupStream.origin == origin, GroupStream.scope == NODE_SCOPE,
        GroupStream.opened_at > rx.now - timedelta(hours=24), GroupStream.blocked_reason.is_(None))) or 0
    if recent >= EPOCH_RATE:
        return Outcome(None, "epoch_rate", stop=True)
    prev = body.get("previous") or {}
    heads = prev.get("heads") or {}
    for old in s.scalars(select(GroupStream).where(GroupStream.group_id == rx.gid, GroupStream.origin == origin,
                                                   GroupStream.closed_at.is_(None))):
        old.closed_at = rx.now
        if old.epoch == prev.get("epoch"):
            old.key_bound_seq = int(heads.get(old.scope, old.hwm)) if old.key_bound_seq is None else old.key_bound_seq
        elif old.key_bound_seq is None:
            old.key_bound_seq = old.hwm
    stream = GroupStream(group_id=rx.gid, origin=origin, epoch=e["epoch"], scope=NODE_SCOPE, hwm=1,
                         key_id=e["key_id"], opened_at=rx.now, last_issued_at=issued)
    s.add(stream)
    _store(rx, e, "applied", None, issued=issued)
    return Outcome("applied", "epoch")


def _strip(rx: Rx, e: dict, reason: str, issued: datetime) -> Outcome:
    """Authorization refusals are final (the stream advances) and never keep the value (§3.4)."""
    _store(rx, e, "rejected", reason, body=None, issued=issued)
    return Outcome("rejected", reason)


def _apply(rx: Rx, e: dict, body: dict, issued: datetime, accept: set[str]) -> Outcome:
    s, kind, uid = rx.session, e["kind"], e["scope"]
    if kind in PROFILE_KINDS:
        link = link_by_uid(s, rx.gid, uid)
        node = s.get(GroupAccountNode, (rx.gid, uid, e["origin"]))
        if link is None or node is None or node.status != "linked":
            return _strip(rx, e, "not_linked", issued)
        name = body["field"]
        if name not in profile.policy_fields(rx.settings):
            return _strip(rx, e, "field_not_allowed", issued)
        if name not in profile.consent_fields(s, link.user_id, rx.gid):
            return _strip(rx, e, "no_consent", issued)
        if kind == "profile.conflict.resolve":
            _store(rx, e, "applied", None, issued=issued)
            return Outcome("applied")
        user = s.get(User, link.user_id)
        assert user is not None
        rejected = set(s.scalars(select(GroupChange.hlc).where(
            GroupChange.group_id == rx.gid, GroupChange.scope == uid, GroupChange.subject == name,
            GroupChange.disposition == "rejected", GroupChange.hlc.isnot(None))))
        result = profile.ingest_remote(
            s, rx.settings, rx.identity.node_id, user, name, hlc=body["hlc"], value=body.get("value"),
            cleared=kind == "profile.field.clear", base=list(body["base"]), cause=body["cause"], origin=e["origin"],
            entry_ref=ref(e["origin"], e["epoch"], uid, e["seq"]), extra_known=rejected, now=rx.now)
        if result == "future":
            return Outcome(None, "future", stop=True)
        if result == "bad_base":
            _store(rx, e, "rejected", "bad_base", issued=issued)
            return Outcome("rejected", "bad_base")
        disposition = {"applied": "applied", "held": "held", "noop": "noop"}[result]
        _store(rx, e, disposition, "missing_base" if result == "held" else None, issued=issued)
        return Outcome(disposition)
    if kind == "account.link":
        return _apply_link(rx, e, body, issued)
    if kind == "account.unlink":
        if body["node_id"] != e["origin"]:
            _store(rx, e, "rejected", "bad_link", issued=issued)
            return Outcome("rejected", "bad_link")
        _record_node(s, rx.gid, uid, e["origin"], "unlinked", {"reason": body["reason"]},
                     ref(e["origin"], e["epoch"], uid, e["seq"]), issued)
        link = link_by_uid(s, rx.gid, uid)
        if link is not None and body["reason"] == "deleted":
            from . import social
            from .service import Ctx

            member = s.get(groups.GroupMember, (rx.gid, e["origin"]))
            name = member.name if member is not None else e["origin"]
            social.notify(s, Ctx(rx.settings, rx.identity), link.user_id, "group_account",
                          f"Your account on {name} was deleted. Your account and settings here are unchanged.",
                          "/me/linked-nodes", key=f"deleted:{e['origin']}")
        _store(rx, e, "applied", None, issued=issued)
        return Outcome("applied")
    if kind == "account.signout_all":
        link = link_by_uid(s, rx.gid, uid)
        if link is not None:
            row = profile.consent(s, link.user_id, rx.gid)
            if row is None or row.signout_propagation:
                before = _t(body["before"])
                s.execute(delete(UserSession).where(UserSession.user_id == link.user_id,
                                                    UserSession.created_at < before))
        _store(rx, e, "applied", None, issued=issued)
        return Outcome("applied")
    _store(rx, e, "rejected", "invalid", issued=issued)
    return Outcome("rejected", "invalid")


def _ulid_time(uid: str) -> datetime:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    ms = 0
    for ch in uid[:10]:
        ms = ms * 32 + alphabet.index(ch)
    return datetime.fromtimestamp(ms / 1000, UTC)


def _apply_link(rx: Rx, e: dict, body: dict, issued: datetime) -> Outcome:
    """`account.link` (§4.3 item 2): a node links only itself; `minted` only from the node that minted the uid
    (at mint time, and never against another minting claim); otherwise the evidence check below."""
    s, uid, origin = rx.session, e["scope"], e["origin"]
    entry = ref(origin, e["epoch"], uid, e["seq"])
    if body["node_id"] != origin:
        _store(rx, e, "rejected", "bad_link", issued=issued)
        return Outcome("rejected", "bad_link")
    status = "linked"
    if body.get("minted"):
        others = [r for r in s.scalars(select(GroupAccountNode).where(
            GroupAccountNode.group_id == rx.gid, GroupAccountNode.account_uid == uid,
            GroupAccountNode.node_id != origin)) if (r.evidence or {}).get("minted")]
        start = _ulid_time(uid)
        if others or not (start - groups.SKEW <= issued <= start + MINT_WINDOW):
            _store(rx, e, "rejected", "not_minter", issued=issued)
            groups.audit(s, None, "account_link_suspect", uid, {"node": origin, "reason": "not_minter"})
            return Outcome("rejected", "not_minter")
        info = {"minted": True, "evidence": None, "linked_at": body["issued_at"]}
    else:
        verdict = evidence_check(rx, uid, origin, body["evidence"], issued)
        if verdict == "retry":
            return Outcome(None, "evidence_pending", stop=True)
        if verdict == "suspect":
            status = "suspect"
        elif verdict != "ok":
            _store(rx, e, "rejected", verdict[:32], issued=issued)
            groups.audit(s, None, "account_link_suspect", uid, {"node": origin, "reason": verdict})
            return Outcome("rejected", verdict)
        info = {"minted": False, "evidence": body["evidence"], "linked_at": body["issued_at"]}
    _record_node(s, rx.gid, uid, origin, status, info, entry, issued)
    if status == "suspect":
        _suspect_link_review(rx, uid, origin, body["evidence"]["body"]["iss_node"])
    _store(rx, e, "applied", status if status != "linked" else None, issued=issued)
    return Outcome("applied")


ASSERTION_KEYS = {"jti", "iss_node", "aud_node", "purpose", "account_uid", "nonce", "state_hash", "auth_time", "amr",
                  "issued_at", "not_after", "username_hint"}


def assertion_shape_ok(a: Any, group_id: str) -> bool:
    try:
        if not isinstance(a, dict) or a.get("t") != "group.assertion" or a.get("_v") != 1 \
                or a.get("group_id") != group_id or not isinstance(a.get("body"), dict):
            return False
        b, signer = a["body"], a["signer"]
        if set(b) != ASSERTION_KEYS or signer.get("kind") != "node" or signer.get("id") != b["iss_node"]:
            return False
        if a["id"] != f"assert:{b['jti']}" or not group_schema.ULID.match(b["jti"]):
            return False
        if not message_parts_ok(gc.object_parts(a)) or not isinstance(a.get("sig"), str):
            return False
        if not isinstance(b["amr"], list) or not set(b["amr"]) <= {"pwd", "otp", "oidc", "recovery"}:
            return False
        return body_hash(b) == a["body_hash"]
    except (KeyError, TypeError, AttributeError):
        return False


def verify_assertion_sig(a: dict, public_b64: str) -> bool:
    return verify_message(public_b64, gc.DOMAINS["group.assertion"], gc.object_parts(a), a["sig"])


def _epoch_closed_before(session: Session, group_id: str, node: str, key_id: str, when: datetime) -> bool:
    """Was `key_id` retired by a rotation epoch of `node` issued before `when`?"""
    for row in session.scalars(select(GroupChange).where(
            GroupChange.group_id == group_id, GroupChange.origin == node, GroupChange.kind == "stream.epoch",
            GroupChange.disposition == "applied")):
        b = row.body or {}
        if b.get("reason") != "rotation" or _t(b["issued_at"]) >= when:
            continue
        prev = session.get(GroupStream, (group_id, node, (b.get("previous") or {}).get("epoch", ""), NODE_SCOPE))
        if prev is not None and prev.key_id == key_id and row.key_id != key_id:
            return True
    return False


def evidence_check(rx: Rx, uid: str, origin: str, a: Any, link_issued: datetime) -> str:
    """The historical evidence check of §4.3 (not the live RP check). ok | suspect | retry | <reason>."""
    s, tr = rx.session, rx.tr
    if not assertion_shape_ok(a, rx.gid):
        return "bad_evidence"
    b = a["body"]
    if b["purpose"] != "link" or b["account_uid"] != uid or b["aud_node"] != origin:
        return "bad_evidence"
    iss, ia, na = b["iss_node"], _t(b["issued_at"]), _t(b["not_after"])
    if iss == origin or na - ia > ASSERTION_TTL or na < ia:
        return "bad_evidence"
    if not (ia <= link_issued <= na + groups.SKEW):
        return "evidence_time"
    keys = _keys_of(tr, iss)
    public = keys.get(a["signer"]["key_id"])
    if public is None:
        return "retry" if not keys else "bad_evidence"
    if not verify_assertion_sig(a, public):
        return "bad_evidence"
    if _epoch_closed_before(s, rx.gid, iss, a["signer"]["key_id"], ia):
        return "key_superseded"
    ban = tr.banned.get(iss)
    if ban is not None and _aware(ban.revoked_at) <= ia:  # type: ignore[operator]
        return "issuer_revoked"
    ok_cert = False
    for c in tr.node_certs(iss):
        cb = c["body"]
        rev = tr.revoked.get(cb["serial"])
        if not tr.genuine(c) or "identity.assert" not in cb["scopes"]:
            continue
        if rev is not None and rev.reason != "superseded" and _aware(rev.revoked_at) <= ia:  # type: ignore[operator]
            continue
        if _t(cb["not_before"]) - groups.SKEW <= ia <= _t(cb["not_after"]) + groups.SKEW:
            ok_cert = True
    if not ok_cert:
        return "issuer_not_valid"
    held = s.get(GroupAccountNode, (rx.gid, uid, iss))
    if held is None or held.status == "suspect":
        return "retry" if held is None else "issuer_link_suspect"
    linked_at = (held.evidence or {}).get("linked_at")
    if not isinstance(linked_at, str) or _t(linked_at) >= ia:
        return "issuer_not_holder"
    fresh = groups.freshness(s, rx.group, rx.identity.node_id, iss, rx.settings.group_view_max_age_hours, rx.now)
    if not fresh.ok:
        return "retry"
    for r in s.scalars(select(GroupRevoked).where(GroupRevoked.group_id == rx.gid, GroupRevoked.node_id == iss,
                                                  GroupRevoked.reason == "key_compromise")):
        since = _aware(r.suspect_since)
        if since is not None and ia >= since:
            return "suspect"
    return "ok"


def _suspect_link_review(rx: Rx, uid: str, node: str, evidenced_by: str) -> None:
    link = link_by_uid(rx.session, rx.gid, uid)
    user = rx.session.get(User, link.user_id) if link is not None else None
    if user is None:
        return
    profile._open_review(rx.session, rx.settings, user, "account", "suspect_link", None, node,
                         {"node_id": node, "evidenced_by": evidenced_by}, rx.now)
    groups.audit(rx.session, None, "account_link_suspect", uid, {"node": node, "evidenced_by": evidenced_by})


def resolve_suspect_link(session: Session, settings: Any, identity: Any, user: User, row: ProfileConflict,
                         choice: str, now: datetime) -> None:
    """"Remove" refuses that node for this account here; "keep" leaves the link inert until it is linked again with
    fresh evidence from an honest node (§4.3 cascade)."""
    group = groups.load(session)
    link = local_link(session, group.group_id, user.id) if group is not None else None
    if group is None or link is None or row.loser_hlc is None:
        return
    node = session.get(GroupAccountNode, (group.group_id, link.account_uid, row.loser_hlc))
    if node is not None and choice in ("remove", "loser"):
        node.status = "unlinked"
        groups.audit(session, user.id, "account_unlinked", user.id, {"node": row.loser_hlc, "reason": "suspect"})


def review_revocations(session: Session, settings: Any, group: OwnerGroup, now: datetime | None = None) -> int:
    """`suspect_since` on a `key_compromise` revocation (§1.6, §4.5): review values set from that node after that
    instant, and make links it evidenced after it inert (`suspect`). Nothing is reverted automatically."""
    now = now or utcnow()
    opened = 0
    for r in session.scalars(select(GroupRevoked).where(GroupRevoked.group_id == group.group_id,
                                                        GroupRevoked.reason == "key_compromise",
                                                        GroupRevoked.node_id.isnot(None))):
        since = _aware(r.suspect_since)
        if since is None:
            continue
        for pf in session.scalars(select(ProfileField).where(ProfileField.origin_node == r.node_id)):
            if profile.hlc_time(pf.hlc) >= since or _aware(pf.updated_at) >= since:  # type: ignore[operator]
                user = session.get(User, pf.user_id)
                if user is not None and profile._open_review(session, settings, user, pf.field, "suspect", None,
                                                             pf.hlc, {"origin": r.node_id}, now) is not None:
                    opened += 1
        for node in session.scalars(select(GroupAccountNode).where(GroupAccountNode.group_id == group.group_id,
                                                                   GroupAccountNode.status == "linked")):
            ev = (node.evidence or {}).get("evidence")
            if isinstance(ev, dict) and ev.get("body", {}).get("iss_node") == r.node_id \
                    and _t(ev["body"]["issued_at"]) >= since:
                node.status = "suspect"
                rx = Rx(session, settings, None, group, groups.trust(session, group), now, None)
                _suspect_link_review(rx, node.account_uid, node.node_id, r.node_id)
                opened += 1
    session.flush()
    return opened


def prune(session: Session, now: datetime | None = None) -> int:
    """Rows rejected for reasons other than authorization keep their body 30 days for forensics (§3.4)."""
    now = now or utcnow()
    rows = session.scalars(select(GroupChange).where(
        GroupChange.disposition == "rejected", GroupChange.body.isnot(None),
        GroupChange.received_at < now - REJECT_BODY_KEEP).limit(5000)).all()
    for row in rows:
        row.body = None
    session.flush()
    return len(rows)


# ---- snapshots (§4.4) -------------------------------------------------------------------------------------------

def verified_snapshot(rx: Rx, uid: str, snap: Any) -> tuple[list[tuple[dict, dict, datetime]], int]:
    """The snapshot entries of `uid` whose signature verifies under the key their epoch entry binds, whose body
    classifies and whose origin was authorized when it issued them. Returns (entries, refused count)."""
    s = rx.session
    if not (isinstance(snap, dict) and isinstance(snap.get("entries"), list)
            and isinstance(snap.get("epochs"), list)):
        return [], 0
    epoch_keys: dict[tuple[str, str], tuple[str, str]] = {}
    for ep in snap["epochs"]:
        if (not shape_ok(ep) or ep.get("elided") or ep["kind"] != "stream.epoch" or ep["scope"] != NODE_SCOPE
                or ep["seq"] != 1 or body_hash(ep["body"]) != ep["body_hash"]):
            continue
        known = s.get(GroupStream, (rx.gid, ep["origin"], ep["epoch"], NODE_SCOPE))
        public = _keys_of(rx.tr, ep["origin"]).get(ep["key_id"])
        if public is not None and verify_entry(public, rx.gid, ep) and (known is None or known.key_id == ep["key_id"]):
            epoch_keys[(ep["origin"], ep["epoch"])] = (ep["key_id"], public)
    out, refused = [], 0
    for e in snap["entries"]:
        if not shape_ok(e) or e.get("elided") or e["scope"] != uid:
            refused += 1
            continue
        key = epoch_keys.get((e["origin"], e["epoch"]))
        if (key is None or key[0] != e["key_id"] or not verify_entry(key[1], rx.gid, e)
                or body_hash(e["body"]) != e["body_hash"]):
            refused += 1
            continue
        cls = group_schema.classify(e["kind"], e["body"])
        if cls.action != group_schema.APPLY or not _consistent(e, e["body"]):
            refused += 1
            continue
        issued = _t(e["body"]["issued_at"])
        if e["origin"] != rx.identity.node_id and e["kind"] != "account.link" and origin_authority(
                rx, e["origin"], issued, GroupStream(opened_at=rx.now), e["seq"]) != "ok":
            refused += 1
            continue
        out.append((e, cls.body or {}, issued))
    # minted links first: the evidence check of the other links needs the minter's link
    out.sort(key=lambda x: (x[0]["kind"] != "account.link", not x[1].get("minted"), x[0]["origin"], x[0]["epoch"],
                            x[0]["seq"]))
    return out, refused


def snapshot_values(rx: Rx, uid: str, snap: Any, accept: set[str]) -> dict[str, tuple[Any, bool, str]]:
    """field -> (value, cleared, origin) of the displayed version in a verified snapshot (the link wizard)."""
    per: dict[str, list[profile.V]] = {}
    for e, body, _ in verified_snapshot(rx, uid, snap)[0]:
        if e["kind"] in ("profile.field.set", "profile.field.clear") and body["field"] in accept:
            per.setdefault(body["field"], []).append(profile.V(
                body["hlc"], body.get("value"), e["kind"] == "profile.field.clear", tuple(body["base"]), body["cause"],
                e["origin"]))
    out = {}
    for name, vs in per.items():
        winner = profile.compute(vs).winner
        if winner is not None:
            out[name] = (winner.value, winner.cleared, winner.origin)
    return out


def install_snapshot(rx: Rx, uid: str, snap: Any, accept: set[str], skip_fields: set[str] | None = None) -> dict:
    """Install a verified snapshot: link entries first (minted rule and evidence check), then the head versions of
    accepted fields. Base references the snapshot does not carry are recorded as superseded (the history behind a
    head), so the stream fetched later is a no-op for them. This node's own entries (lost in a restore) come back
    verified under its own epoch keys."""
    s = rx.session
    entries, refused = verified_snapshot(rx, uid, snap)
    out = {"links": 0, "values": 0, "refused": refused}
    for e, body, issued in entries:
        if s.get(GroupChange, (rx.gid, e["origin"], e["epoch"], e["scope"], e["seq"])) is not None:
            continue
        if e["kind"] == "account.link":
            if e["origin"] == rx.identity.node_id:
                continue
            result = _apply_link(rx, {**e}, body, issued)
            out["links" if result.disposition == "applied" else "refused"] += 1
            continue
        if e["kind"] not in ("profile.field.set", "profile.field.clear"):
            continue
        name = body["field"]
        if name not in accept or (skip_fields and name in skip_fields):
            continue
        link = link_by_uid(s, rx.gid, uid)
        node = s.get(GroupAccountNode, (rx.gid, uid, e["origin"]))
        if link is None or (e["origin"] != rx.identity.node_id and (node is None or node.status != "linked")):
            out["refused"] += 1
            continue
        user = s.get(User, link.user_id)
        missing = set(body["base"]) - {r.hlc for r in profile.live_versions(s, link.user_id, name)}
        for h in missing:
            s.merge(profile.ProfileSuperseded(user_id=link.user_id, field=name, hlc=h, pruned_at=rx.now))
        s.flush()
        result = profile.ingest_remote(
            s, rx.settings, rx.identity.node_id, user, name, hlc=body["hlc"], value=body.get("value"),
            cleared=e["kind"] == "profile.field.clear", base=list(body["base"]), cause=body["cause"],
            origin=e["origin"], entry_ref=ref(e["origin"], e["epoch"], uid, e["seq"]), now=rx.now)
        if result in ("applied", "noop"):
            out["values"] += 1
            _mark_snapshot(s, rx, e)
        else:
            out["refused"] += 1
    return out


def _mark_snapshot(s: Session, rx: Rx, e: dict) -> None:
    if s.get(GroupChange, (rx.gid, e["origin"], e["epoch"], e["scope"], e["seq"])) is None:
        _store(rx, e, "applied", "snapshot", issued=_t(e["body"]["issued_at"]))


# ---- the sync loop (§3.8) ---------------------------------------------------------------------------------------

_HELLO: dict[str, tuple[float, dict]] = {}
WAKE = threading.Event()  # set by an inbound `notify`: the loop runs its next round now


def hello_result(identity: Any) -> dict:
    """The `hello` op answer: protocol, kinds, the profile schema and capabilities (§3.5)."""
    return {"group_protocol": group_schema.PROTOCOL, "min_group_protocol": group_schema.MIN_PROTOCOL,
            "node": {"node_id": identity.node_id, "signing_key_id": identity.key_id},
            "kinds": group_schema.kinds_advertised(), "profile_schema": profile_schema.SCHEMA_VERSION,
            "profile_fields": profile_schema.fields(), "capabilities": list(group_schema.CAPABILITIES_GROUP),
            "ops": ["hello", "state", "rotations", "heads", "changes", "snapshot", "notify"]}


def accept_for_scope(session: Session, settings: Any, group_id: str, scope: str) -> list[str]:
    if scope == NODE_SCOPE:
        return []
    link = link_by_uid(session, group_id, scope)
    return profile.accept(session, settings, link.user_id, group_id) if link is not None else []


def sync(session: Session, settings: Any, identity: Any, group: OwnerGroup, http: Any,
         call: Any, now: datetime | None = None) -> dict[str, str]:
    """One channel round after the trust state was pulled: hello (cached 1 h), heads, then changes for every
    stream whose remote head is above the local hwm, from the origin itself or a relay. Pull first; `notify`
    is only a hint. Returns node -> outcome."""
    if group.status != "active":
        return {}
    now = now or utcnow()
    outcomes: dict[str, str] = {}
    tr = groups.trust(session, group, now, own_node=identity.node_id)
    review_revocations(session, settings, group, now)
    if session.scalar(select(func.count()).select_from(GroupAccountLink).where(
            GroupAccountLink.group_id == group.group_id)) and may_emit(session, group, identity, tr):
        ensure_epoch(session, group, identity, now)
    members = list(tr.members().items())
    secrets.SystemRandom().shuffle(members)
    rx_counts: dict[str, int] = {}
    rx_origins: dict[str, int] = {}
    for node, cert in members:
        if node == identity.node_id:
            continue
        member = cert["body"]["member"]
        try:
            cached = _HELLO.get(node)
            if cached is None or time.monotonic() - cached[0] > HELLO_TTL:
                hello = call(session, settings, identity, group, member, "hello", {}, http)
                _HELLO[node] = (time.monotonic(), hello if isinstance(hello, dict) else {})
            hello = _HELLO[node][1]
            if "heads" not in (hello.get("ops") or []) or group_schema.negotiate(hello) is None:
                outcomes[node] = "protocol"
                continue
            heads = call(session, settings, identity, group, member, "heads", {}, http)
            rx = Rx(session, settings, identity, group, tr, now, node)
            _pull_member(rx, member, heads, call, http)
            for k, v in rx.counts.items():
                rx_counts[k] = rx_counts.get(k, 0) + v
            for k, v in rx.origins.items():
                rx_origins[k] = rx_origins.get(k, 0) + v
            outcomes[node] = "ok"
        except Exception as exc:  # noqa: BLE001 - one member failing never stops the round
            if not isinstance(exc, (groups.GroupError, ChannelError)) and type(exc).__name__ != "CallError":
                log.warning("group channel round failed for a member",
                            extra={"atlas_event": {"event": "group.channel_failed", "error_type": type(exc).__name__}})
            outcomes[node] = f"channel: {exc}"[:300]
    rejected = {k: v for k, v in rx_counts.items() if k not in ("fork",)}
    if rejected:
        groups.audit(session, None, "group_entries_rejected", group.group_id,
                     {"reasons": rejected, "origins": rx_origins})
    _snapshot_if_due(session, settings, identity, group, http, call, tr, now)
    prune(session, now)
    timeout = timedelta(days=getattr(settings, "group_hold_timeout_days", 7))
    for row in session.scalars(select(ProfileVersion).where(ProfileVersion.held_since.isnot(None),
                                                            ProfileVersion.held_since <= now - timeout)).all():
        user = session.get(User, row.user_id)
        if user is not None:
            profile.release_held(session, settings, identity.node_id, user, row.field, timeout=timeout, now=now)
    for link in session.scalars(select(GroupAccountLink).where(GroupAccountLink.group_id == group.group_id)).all():
        user = session.get(User, link.user_id)
        if user is not None:
            emit_pending(session, settings, identity, user, now=now)
    session.flush()
    return outcomes


def _pull_member(rx: Rx, member: dict, heads: Any, call: Any, http: Any) -> None:
    s, node = rx.session, member["node_id"]
    if not isinstance(heads, dict):
        raise ChannelError("heads is not an object")
    relay = _relays(rx.tr, node)
    wanted: list[tuple[str, str, str, int]] = []
    for origin, epochs in heads.items():
        if origin == rx.identity.node_id or not isinstance(epochs, dict) or (origin != node and not relay):
            continue
        if not group_schema.NODE.match(str(origin)):
            continue
        valid_now = rx.tr.in_force(origin) is not None
        for epoch, scopes in epochs.items():
            if not isinstance(scopes, dict) or not group_schema.EPOCH.match(str(epoch)):
                continue
            for scope, head in scopes.items():
                if type(head) is not int or head < 1:
                    continue
                if scope != NODE_SCOPE and link_by_uid(s, rx.gid, scope) is None:
                    continue
                stream = s.get(GroupStream, (rx.gid, origin, epoch, scope))
                if valid_now and stream is not None and (stream.advertised_head or 0) < head:
                    stream.advertised_head, stream.advertised_at = head, rx.now
                wanted.append((origin, epoch, scope, head))
    # `_node` streams first: an epoch's key binding comes from its `stream.epoch` entry
    wanted.sort(key=lambda w: (w[2] != NODE_SCOPE, w[0], w[1], w[2]))
    for origin, epoch, scope, head in wanted:
        _fetch(rx, member, origin, epoch, scope, head, call, http)
        if scope == NODE_SCOPE:
            continue
        stream = s.get(GroupStream, (rx.gid, origin, epoch, scope))
        if stream is not None and rx.tr.in_force(origin) is not None and (stream.advertised_head or 0) < head:
            stream.advertised_head, stream.advertised_at = head, rx.now


def _fetch(rx: Rx, member: dict, origin: str, epoch: str, scope: str, head: int, call: Any, http: Any) -> None:
    s = rx.session
    for _ in range(20):
        stream = s.get(GroupStream, (rx.gid, origin, epoch, scope))
        hwm = stream.hwm if stream is not None else 0
        if stream is not None and (stream.blocked_seq is not None or (stream.key_bound_seq is not None
                                                                      and hwm >= stream.key_bound_seq)):
            return
        if hwm >= head:
            return
        accept = accept_for_scope(s, rx.settings, rx.gid, scope)
        page = call(s, rx.settings, rx.identity, rx.group, member, "changes",
                    {"origin": origin, "epoch": epoch, "scope": scope, "since": hwm, "limit": PAGE, "accept": accept},
                    http)
        entries = page.get("entries") if isinstance(page, dict) else None
        if not isinstance(entries, list) or not entries:
            return
        accepted = set(accept)
        for e in entries:
            if isinstance(e, dict) and (e.get("origin"), e.get("epoch"), e.get("scope")) != (origin, epoch, scope):
                _suspect_peer(rx, "bad_signature")
                return
            out = receive(rx, e, accepted)
            if out.stop:
                return
        if len(entries) < PAGE:
            return


def _snapshot_if_due(session: Session, settings: Any, identity: Any, group: OwnerGroup, http: Any, call: Any,
                     tr: groups.Trust, now: datetime) -> None:
    """After a restore, fetch a snapshot of every linked account from each member: the restored node re-learns
    its lost effects (its own signed entries come back verified, §3.6)."""
    due = _state(session, SNAPSHOT_DUE)
    if due is None:
        return
    done = not session.scalar(select(func.count()).select_from(GroupAccountLink).where(
        GroupAccountLink.group_id == group.group_id))
    for node, cert in tr.members().items():
        if node == identity.node_id:
            continue
        for link in session.scalars(select(GroupAccountLink).where(GroupAccountLink.group_id == group.group_id)).all():
            accept = profile.accept(session, settings, link.user_id, group.group_id)
            try:
                snap = call(session, settings, identity, group, cert["body"]["member"], "snapshot",
                            {"account_uid": link.account_uid, "accept": accept}, http)
            except Exception:  # noqa: BLE001 - retried next round
                continue
            install_snapshot(Rx(session, settings, identity, group, tr, now, node), link.account_uid, snap,
                             set(accept))
            done = True
    if done:
        session.delete(due)


def fetch_snapshot(session: Session, settings: Any, identity: Any, group: OwnerGroup, member_node: str, uid: str,
                   accept: list[str], http: Any) -> Any:
    from .api import group_routes

    tr = groups.trust(session, group, own_node=identity.node_id)
    cert = tr.in_force(member_node)
    if cert is None:
        raise ChannelError("that node is not a member now")
    return group_routes.call(session, settings, identity, group, cert["body"]["member"], "snapshot",
                             {"account_uid": uid, "accept": accept}, http)


# ---- link assertions (§5.4, link purpose) -----------------------------------------------------------------------

def sign_assertion(session: Session, group: OwnerGroup, identity: Any, *, aud_node: str, uid: str, nonce: str,
                   state_hash: str, auth_time: datetime, amr: list[str], username_hint: str,
                   now: datetime | None = None) -> dict:
    now = now or utcnow()
    jti = ids.ulid()
    body = {"jti": jti, "iss_node": identity.node_id, "aud_node": aud_node, "purpose": "link", "account_uid": uid,
            "nonce": nonce, "state_hash": state_hash, "auth_time": gc.stamp(auth_time), "amr": amr,
            "issued_at": gc.stamp(now), "not_after": gc.stamp(now + ASSERTION_TTL),
            "username_hint": username_hint[:32]}
    a = {"t": "group.assertion", "_v": 1, "group_id": group.group_id, "id": f"assert:{jti}", "body": body,
         "signer": {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, "body_hash": body_hash(body)}
    a["sig"] = sign_message(identity.private, gc.DOMAINS["group.assertion"], gc.object_parts(a))
    return a


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


__all__ = ["receive", "sync", "serve_heads", "serve_changes", "serve_snapshot", "emit_version", "unlink"]
