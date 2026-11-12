"""The super group: the owner group presents to its users as ONE node (CONTRACT-C14 slice C14-4, owner request
2026-11-10 and its refinement "invisible topology, write once, synced to all").

Write once, synced to all
    A user write made on any member (an edit or a new game applied there, a list created, renamed or deleted, a game
    added to or removed from a list) is recorded here as one `group.write` object signed by that member, under the
    C10 envelope and its own domain (`atlas-grp-write-v1`). Members pull each other with the sealed `group.writes`
    op during the group round. A member serves every write it holds (its own and those it received), in its own
    receive order, so a write reaches a member that was offline even when its origin is offline by then; every
    write is verified against its origin's key history, so a relaying member cannot alter or invent one.

Deterministic resolution
    Each write names one subject (`rec:<cid>/<field>`, `list:<key>`, `item:<key>/<cid>`) and carries a hybrid
    logical clock `<ms>.<counter>.<node>`, totally ordered. A subject's state is its applied write with the highest
    clock (last writer wins; a delete is a tombstone that wins the same way), so every member that holds the same
    writes holds the same state, whatever order they arrived in. A conflict is a real overwrite: write W beat a
    write X its writer had not seen (`X.hlc > W.base`) with a different value. Its id hashes (subject, W, X), so
    every member records the same conflict entries; they are shown to administrators only.

Acceptance
    Whether a write counts is a function of the write (its origin and signed clock) and the group's signed trust
    state only (`standing`), never of when a member received it: refused when dated at or after its origin's
    revocation, ban or leave (a member that applied it before hearing of the removal withdraws it, `_recheck_removed`);
    held while no certificate of its origin covers its date, until a renewal exists (D7). Records under a `private`
    or `local` ceiling never emit writes. Lists an account had before the super group or its link are written once
    (`backfill`). A value is checked by its field's own rule (`_field_ok`), so a write no member could apply is
    refused on arrival; one that still fails to apply on one member is held there (`failed`) in its own savepoint and
    retried, and the round goes on (one bad write never stops the others). `group_write` rows are inserted under one
    lock (`_serialise`), so their id order is their commit order and a puller's cursor never skips one.

Materialisation
    Record fields become one observation per record, source `group-write` (community class, trust 86) under the
    `group:write` scope: group data, so never signed into this node's feed and never sent to a main Atlas (H59,
    C14 §2.2), shown to visitors only through the partial public projection (D1, §5.7). A record a member lacks is
    created with the `group` ceiling when the write carries its title, or asked from the group (C14-2) and retried.
    Once a record field has a group state, this node's own `community` value of it is retired (`_retire_community`),
    so a tombstone or a group list value shows the same on every member. A revert is replicated only while the
    reverted write is still the field's state (`revert_allowed`). Lists follow the group account link (C10 §5.1): a
    write for an account not linked here waits, and is applied when it is linked.

Invisible topology
    Nothing in this module is shown on a user page except a generic "syncing" state for the user's own writes and
    the group's freshness. Which member wrote, served or holds anything appears only on admin pages.

Owner decisions this respects: D1 (partial projection), D2 (links are per node: replicated writes never touch a
node link, its queue or its probation), D7 (writes of a member whose certificate expired are held until it is
renewed), D8 (a member without the op is a degraded member, shown to administrators, never an error to users).
Wire: one new sealed op (advertised in `hello.ops`) and one new object type; nothing in the public feed changes.
Readers are tolerant: a write of a kind or version this release does not know is kept and served onward, unapplied.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import group_crypto as gc
from .ids import ulid
from .models import (
    Canonical,
    Crosswalk,
    GroupAccountLink,
    GroupWrite,
    GroupWriteConflict,
    GroupWriteState,
    ListItem,
    MediaType,
    NodeState,
    Observation,
    User,
    UserList,
    now,
)
from .settings import GROUP_WRITE_SOURCE

log = logging.getLogger("atlas.groups")

OP = "group.writes"
OBJECT = "group.write"
VERSION = 1
KINDS = ("record.field", "list.meta", "list.item")
SCOPE = "group:write"  # the observation scope of materialised record writes (group data, C14 §5.7)
WRITER = "write"  # `canonical.scope_ceiling_by` of a record created from a replicated write
PAGE = 200
MAX_PAGES = 20
MAX_VALUE = 32 * 1024  # canonical JSON bytes of one value
CLOCK_AHEAD = timedelta(hours=24)  # a write dated further ahead waits (a member with a broken clock cannot win forever)
LIST_KINDS = ("custom", "favourites", "wishlist")

#: node ids: today 16 hex; C16 D5 widens them to 64 hex (both accepted, so Z1 needs no change of this wire)
NODE_HEX = r"[0-9a-f]{16}(?:[0-9a-f]{48})?"
NODE_RE = re.compile(rf"^{NODE_HEX}\Z")
HLC_RE = re.compile(rf"^(\d{{13}})\.(\d{{5}})\.({NODE_HEX})\Z")
CID_RE = re.compile(r"^atlas:[0-9a-z]{26}\Z")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}\Z")
UID_RE = re.compile(r"^[0-9A-Z]{26}\Z")
LIST_KEY_RE = re.compile(r"^(?:[0-9A-Z]{26}|[0-9A-Z]{26}\.(?:favourites|wishlist))\Z")
OP_ID_RE = re.compile(rf"^{NODE_HEX}:\d{{1,12}}\Z")
#: fields a record write may not carry (they are not values of the record view)
NOT_FIELDS = frozenset({"custom", "media_type", "note", "upstream"})
#: the record fields this release writes and applies: the edit form's fields (`proposals.clean_edit`) and a new
#: game's provider ids. A well-formed write of another field is a later release's (kept and relayed, not applied).
TEXT_FIELDS = {"title": 500, "description": 20000, "developer": 200, "publisher": 200, "series": 200,
               "age_rating": 50, "website": 500}
LIST_FIELDS = ("genres", "themes", "tags", "features", "platforms", "languages", "aliases")
RECORD_FIELDS = frozenset({*TEXT_FIELDS, *LIST_FIELDS, "release_date", "releases", "provider_ids"})
#: a provider id map (the `proposals._validate` rule for a new game, with room for a later release's keys)
PROVIDER_KEY_RE = re.compile(r"^[a-z0-9._-]{1,50}\Z")
#: record fields kept in this node's own `community` observation that a group state replaces there (`title` and
#: `provider_ids` stay: a record always keeps its title, and provider ids are crosswalk evidence, not a view value)
RETIRED_FIELDS = RECORD_FIELDS - {"title", "provider_ids"}

#: NodeState keys (all start with `super_group_`, which the 0041 downgrade removes)
CLOCK = "super_group_clock"
SEQ = "super_group_seq"
CURSOR = "super_group_cursor:"  # + member: the member's receive order already pulled
ACK = "super_group_ack:"  # + member: how far that member has pulled from this node
SEEN = "super_group_seen:"  # + member: when a pull from it last succeeded
ERROR = "super_group_error:"  # + member: the last pull's error code (admins only)
NOTIFIED = "super_group_notified"  # own receive order already announced to the members with `notify`
MEMBERS = "super_group_members"  # the members that took part in the last round (for the "syncing" state)
RECHECKED = "super_group_rechecked"  # fingerprint of the removals the applied writes were last checked against
BACKFILLED = "super_group_backfilled:"  # + account uid: that account's lists and items were written once
RETRY_BATCH = 500


class WriteError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---- small helpers ------------------------------------------------------------------------------------------------

def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "group_super", False))


def _active_group(session: Session) -> Any:
    from . import groups

    group = groups.active(session)
    return group if group is not None and group.status in ("active", "resyncing") else None


def _state(session: Session, key: str) -> str | None:
    row = session.get(NodeState, key)
    return row.value if row is not None else None


def _put(session: Session, key: str, value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    row = session.get(NodeState, key)
    if row is None:
        session.add(NodeState(key=key, value=text))
        session.flush()  # sessions do not autoflush: a second `_put` or `_state` of this key must see it
    else:
        row.value = text


def _int(session: Session, key: str) -> int:
    raw = _state(session, key)
    return int(raw) if raw is not None and raw.isdigit() else 0


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _canon(value: Any) -> Any:
    """NFC strings, no floats (the C10 canonical-JSON rules); None stays None."""
    if isinstance(value, str):
        return gc.nfc(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if abs(value) < 2 ** 53 else str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, dict):
        return {gc.nfc(str(k)): _canon(v) for k, v in value.items()}
    return gc.nfc(str(value))


def _size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


# ---- the hybrid logical clock -------------------------------------------------------------------------------------

def _hlc(ms: int, counter: int, node: str) -> str:
    return f"{ms:013d}.{counter:05d}.{node}"


def tick(session: Session, node: str, wall_ms: int | None = None) -> str:
    """The next clock value of this node: never behind the wall clock, never behind anything it has seen."""
    wall = wall_ms if wall_ms is not None else int(time.time() * 1000)
    last = _state(session, CLOCK)
    ms, counter = (int(last.split(".")[0]), int(last.split(".")[1])) if last and "." in last else (0, 0)
    if wall > ms:
        ms, counter = wall, 0
    else:
        counter += 1
        if counter > 99999:  # pragma: no cover - 100 000 writes in one millisecond
            ms, counter = ms + 1, 0
    _put(session, CLOCK, f"{ms}.{counter}")
    return _hlc(ms, counter, node)


def observe(session: Session, hlc: str) -> None:
    """Move this node's clock past a clock it received (so its next write orders after what it has seen)."""
    m = HLC_RE.match(hlc)
    if m is None:
        return
    ms, counter = int(m.group(1)), int(m.group(2))
    last = _state(session, CLOCK)
    lms, lc = (int(last.split(".")[0]), int(last.split(".")[1])) if last and "." in last else (0, 0)
    if (ms, counter) > (lms, lc):
        _put(session, CLOCK, f"{ms}.{counter}")


# ---- subjects and bodies ------------------------------------------------------------------------------------------

def subject_of(kind: str, target: dict) -> str:
    if kind == "record.field":
        return f"rec:{target['cid']}/{target['field']}"
    if kind == "list.meta":
        return f"list:{target['list']}"
    if kind == "list.item":
        return f"item:{target['list']}/{target['cid']}"
    raise WriteError("unknown_kind")


def _target_ok(kind: str, target: Any) -> bool:
    if not isinstance(target, dict):
        return False
    if kind == "record.field":
        return (set(target) == {"cid", "field", "media_type"} and isinstance(target["cid"], str)
                and CID_RE.match(target["cid"]) is not None and isinstance(target["field"], str)
                and FIELD_RE.match(target["field"]) is not None and target["field"] not in NOT_FIELDS
                and (target["media_type"] is None or (isinstance(target["media_type"], str)
                                                      and FIELD_RE.match(target["media_type"]) is not None)))
    account_ok = isinstance(target.get("account"), str) and UID_RE.match(target["account"]) is not None
    list_ok = isinstance(target.get("list"), str) and LIST_KEY_RE.match(target["list"]) is not None
    if kind == "list.meta":
        return set(target) == {"list", "account"} and account_ok and list_ok
    if kind == "list.item":
        return (set(target) == {"list", "account", "cid"} and account_ok and list_ok
                and isinstance(target["cid"], str) and CID_RE.match(target["cid"]) is not None)
    return False


def _value_ok(kind: str, target: dict, value: Any, deleted: bool) -> bool:
    if deleted:
        return value is None
    if _size(value) > MAX_VALUE:
        return False
    if kind == "record.field":
        return _field_ok(target["field"], value)
    if kind == "list.meta":
        return (isinstance(value, dict) and set(value) == {"name", "description", "public", "kind"}
                and isinstance(value["name"], str) and 0 < len(value["name"].strip()) <= 80
                and not any(ord(c) < 32 for c in value["name"])
                and isinstance(value["description"], str) and len(value["description"]) <= 300
                and isinstance(value["public"], bool) and value["kind"] in LIST_KINDS)
    if kind == "list.item":
        return isinstance(value, dict) and set(value) == {"note"} and isinstance(value["note"], str) \
            and len(value["note"]) <= 200
    return False


def _field_ok(field: str, value: Any) -> bool:
    """A record field's value by that field's own rule, the one an edit made on this node passes
    (`proposals.clean_edit`): a write any member could not apply is refused at the door, so one bad write can never
    break a member's round (review C14-4, high)."""
    if field == "provider_ids":
        return isinstance(value, dict) and 0 < len(value) <= 32 and all(
            isinstance(k, str) and PROVIDER_KEY_RE.match(k) is not None and isinstance(v, str) and 0 < len(v) <= 256
            for k, v in value.items())
    if field not in RECORD_FIELDS:
        return False
    from . import proposals

    try:
        clean = proposals.clean_edit({field: value})
    except Exception:  # noqa: BLE001 - AtlasError for a refused value; anything else is refused the same way
        return False
    return field in clean


def body_ok(body: Any) -> str | None:
    """None when `body` is a well-formed write this release can apply; otherwise a reason. `unknown_kind` means a
    well-formed write of a later release (tolerant reader: kept and served onward, not applied)."""
    if not isinstance(body, dict):
        return "invalid"
    for key in ("v", "op_id", "origin", "seq", "kind", "subject", "hlc"):
        if key not in body:
            return "invalid"
    if type(body["v"]) is not int or type(body["seq"]) is not int or body["seq"] < 1:
        return "invalid"
    if not isinstance(body["origin"], str) or NODE_RE.match(body["origin"]) is None:
        return "invalid"
    if body["op_id"] != f"{body['origin']}:{body['seq']}" or not isinstance(body["hlc"], str):
        return "invalid"
    m = HLC_RE.match(body["hlc"])
    if m is None or m.group(3) != body["origin"]:
        return "invalid"
    if not isinstance(body["subject"], str) or not 0 < len(body["subject"]) <= 200:
        return "invalid"
    if body["v"] > VERSION or body["kind"] not in KINDS:
        return "unknown_kind"
    for key in ("base", "value", "deleted", "target"):
        if key not in body:
            return "invalid"
    if body["base"] is not None and (not isinstance(body["base"], str) or HLC_RE.match(body["base"]) is None):
        return "invalid"
    if not isinstance(body["deleted"], bool) or not _target_ok(body["kind"], body["target"]):
        return "invalid"
    if subject_of(body["kind"], body["target"]) != body["subject"]:
        return "invalid"
    if body["kind"] == "record.field" and body["target"]["field"] not in RECORD_FIELDS:
        return "unknown_kind"  # a field of a later release: kept and relayed, applied by the release that knows it
    if not _value_ok(body["kind"], body["target"], body["value"], body["deleted"]):
        return "invalid"
    return None


# ---- writing (the origin) -----------------------------------------------------------------------------------------

#: the PostgreSQL advisory lock that orders `group_write` inserts (any constant no other lock of Atlas uses)
INSERT_LOCK = 727_404


def _serialise(session: Session) -> None:
    """Hold, until this transaction ends, the lock under which every `group_write` row is inserted, so a row's id
    order is its commit order. `serve` pages by id and a puller moves its cursor to the highest id it got: without
    this, on PostgreSQL a transaction holding an uncommitted lower id while a later one commits a higher id would
    let a member move past the lower one for good (review C14-4, medium; `feed.append` holds a row lock for the
    same reason). SQLite already has one writer at a time, and the writer holds it until it commits."""
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy import text

        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": INSERT_LOCK})


def _own_seq(session: Session, node: str) -> int:
    held = session.scalar(select(func.max(GroupWrite.seq)).where(GroupWrite.origin_node == node)) or 0
    seq = max(_int(session, SEQ), held) + 1
    _put(session, SEQ, str(seq))
    return seq


def _trust(session: Session, group: Any, identity: Any) -> Any:
    from . import groups

    return groups.trust(session, group, own_node=identity.node_id)


def emit(session: Session, ctx: Any, kind: str, target: dict, value: Any, *, deleted: bool = False,
         author: str | None = None, tr: Any = None) -> GroupWrite | None:
    """Record one user write made on this node and apply it here (state, conflicts, materialisation). Does nothing
    outside an active group or with `group_super` off. The members pull it in their next round. The origin judges
    its own write by the same `standing` rule as every other member, so a write made while this node's certificate
    had lapsed is held here too (D7) rather than counted on one member only."""
    settings, identity = ctx.settings, ctx.identity
    if not enabled(settings):
        return None
    group = _active_group(session)
    if group is None:
        return None
    value = None if deleted else _canon(value)
    if not _target_ok(kind, target) or not _value_ok(kind, target, value, deleted):
        raise WriteError("invalid")
    subject = subject_of(kind, target)
    _serialise(session)
    session.flush()
    state = session.get(GroupWriteState, subject)
    seq = _own_seq(session, identity.node_id)
    body = {"v": VERSION, "op_id": f"{identity.node_id}:{seq}", "origin": identity.node_id, "seq": seq,
            "kind": kind, "subject": subject, "hlc": tick(session, identity.node_id),
            "base": state.hlc if state is not None else None, "value": value, "deleted": deleted,
            "target": dict(target)}
    obj = gc.make_object(OBJECT, group.group_id, body["op_id"], body,
                         {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, identity.private)
    stand = standing(tr if tr is not None else _trust(session, group, identity), identity.node_id, body["hlc"])
    row = GroupWrite(op_id=body["op_id"], origin_node=identity.node_id, seq=seq, kind=kind, subject=subject,
                     hlc=body["hlc"], reason=stand, author_user_id=author,
                     status="applied" if stand is None else ("refused" if stand == "origin_removed" else "held"),
                     envelope=obj, received_at=now())
    session.add(row)
    session.flush()
    if stand is None:
        _settle(session, ctx, row, body)
    from .group_channel import WAKE

    WAKE.set()  # the group round runs soon and tells the members (`notify`), who then pull
    return row


def _guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """A user's own action never fails because replicating it failed: the write stays saved here, logged."""
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            session = args[0]
            with session.begin_nested():
                return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("super group: could not record a write: %s", type(exc).__name__)
            return None
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def record_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """The record fields of an edit or new-game payload (custom fields and form-only keys left out)."""
    return {k: v for k, v in payload.items() if isinstance(k, str) and k in RECORD_FIELDS}


@_guarded
def record_written(session: Session, ctx: Any, cid: str, fields: dict[str, Any], media_type: str | None,
                   author: str | None = None) -> int:
    """An edit or a new game was applied on this node (`proposals.apply`, `revert_applied`): one write per field.
    A field whose new value is None or empty is a tombstone."""
    if not enabled(ctx.settings) or not CID_RE.match(cid or "") or kept_here(session, cid):
        return 0
    group = _active_group(session)
    if group is None:
        return 0
    tr = _trust(session, group, ctx.identity)
    n = 0
    for field, value in sorted(record_fields(fields).items()):
        gone = value is None or value == "" or value == []
        if emit(session, ctx, "record.field", {"cid": cid, "field": field, "media_type": media_type},
                None if gone else value, deleted=gone, author=author, tr=tr) is not None:
            n += 1
    return n


def kept_here(session: Session, cid: str) -> bool:
    """A record under a `private` or `local` ceiling (C14-1 §5.3): its edits and list entries never leave this node,
    super group or not. (A `group` ceiling is exactly what the super group shares.)"""
    row = session.get(Canonical, cid)
    if row is not None and row.merged_into:
        row = session.get(Canonical, row.merged_into) or row
    return row is not None and row.scope_ceiling in ("private", "local")


def value_before(session: Session, cid: str, field: str, written: Any, fallback: Any) -> Any:
    """For a revert: the value a field had before this node wrote `written` to it, from the write history (the
    write that one replaced, through its `base`); `fallback` (the reverted observation's value) when the history
    does not know. None means the field had no value (a tombstone)."""
    subject = f"rec:{cid}/{field}"
    want = _canon(written)
    for row in session.scalars(select(GroupWrite).where(GroupWrite.subject == subject, GroupWrite.status == "applied")
                               .order_by(GroupWrite.id.desc())):
        body = (row.envelope or {}).get("body") or {}
        if body.get("deleted") or body.get("value") != want:
            continue
        if body.get("base") is None:
            return None
        prior = session.scalars(select(GroupWrite).where(GroupWrite.subject == subject,
                                                         GroupWrite.hlc == body["base"])).first()
        if prior is None:
            break
        pb = (prior.envelope or {}).get("body") or {}
        return None if pb.get("deleted") else pb.get("value")
    return fallback


def revert_allowed(session: Session, ctx: Any, cid: str, field: str, written: Any) -> bool:
    """A revert of the value this node wrote to a field is replicated only while that write is still the field's
    group state. Once another write (any member's) replaced it, writing the old value with a new clock would
    overwrite that later edit everywhere, without a conflict entry (review C14-4, high); the later edit stands, and
    the revert only restores this node's own edit history. True when the field has no group state at all."""
    state = session.get(GroupWriteState, f"rec:{cid}/{field}")
    if state is None:
        return True
    row = session.scalars(select(GroupWrite).where(GroupWrite.op_id == state.op_id)).first()
    return (row is not None and row.origin_node == ctx.identity.node_id and not state.deleted
            and state.value == _canon(written))


def account_of(session: Session, user_id: str) -> str | None:
    """The group account uid of a local user (C10 §5.1), or None when the account is not linked."""
    group = _active_group(session)
    if group is None:
        return None
    row = session.scalars(select(GroupAccountLink).where(GroupAccountLink.group_id == group.group_id,
                                                         GroupAccountLink.user_id == user_id)).first()
    return row.account_uid if row is not None else None


def list_key(session: Session, lst: UserList, uid: str) -> str:
    """A list's key in the group: favourites and wishlist are one per account; a custom list keeps its id."""
    return f"{uid}.{lst.kind}" if lst.kind in ("favourites", "wishlist") else lst.id


@_guarded
def list_written(session: Session, ctx: Any, user: User, lst: UserList, *, deleted: bool = False) -> bool:
    if not enabled(ctx.settings):
        return False
    uid = account_of(session, user.id)
    if uid is None:
        return False
    value = None if deleted else {"name": lst.name, "description": lst.description or "", "public": bool(lst.public),
                                  "kind": lst.kind if lst.kind in LIST_KINDS else "custom"}
    return emit(session, ctx, "list.meta", {"list": list_key(session, lst, uid), "account": uid}, value,
                deleted=deleted, author=user.id) is not None


@_guarded
def list_item_written(session: Session, ctx: Any, user: User, lst: UserList, cid: str, note: str | None = None, *,
                      deleted: bool = False) -> bool:
    """A game was added to (or removed from) a list here. The note written is the one this node actually stored
    (`note` is only a fallback): adding a game that is already on the list keeps its note here, so it must keep it
    on every member too."""
    if not enabled(ctx.settings) or not CID_RE.match(cid or "") or kept_here(session, cid):
        return False
    uid = account_of(session, user.id)
    if uid is None:
        return False
    if not deleted:
        session.flush()
        stored = session.get(ListItem, (lst.id, cid))
        if stored is not None:
            note = stored.note
    return emit(session, ctx, "list.item", {"list": list_key(session, lst, uid), "account": uid, "cid": cid},
                None if deleted else {"note": (note or "")[:200]}, deleted=deleted, author=user.id) is not None


def list_items_moved(session: Session, ctx: Any, src: str, dst: str, moved: list[tuple[str, str | None]]) -> int:
    """Two records were merged here and the lists followed the survivor (`social.move_list_items`): the move is
    written once like any list change (the merged-away entry removed, the survivor's added with its note), so every
    member's lists hold the same entries (review C14-4, low). Returns the number of lists written."""
    if not enabled(ctx.settings) or not moved:
        return 0
    n = 0
    for list_id, note in moved:
        lst = session.get(UserList, list_id)
        user = session.get(User, lst.user_id) if lst is not None else None
        if user is None:
            continue
        list_item_written(session, ctx, user, lst, src, deleted=True)
        if note is not None:
            list_item_written(session, ctx, user, lst, dst, note)
        n += 1
    return n


# ---- settling: state, conflicts, materialisation ------------------------------------------------------------------

def conflict_id(subject: str, winner: str, loser: str) -> str:
    return hashlib.sha256(f"{subject}\n{winner}\n{loser}".encode()).hexdigest()[:32]


def _same(a: dict, b: dict) -> bool:
    return bool(a.get("deleted")) == bool(b.get("deleted")) and a.get("value") == b.get("value")


def _overwrote(winner: dict, loser: dict) -> bool:
    """`winner` (the later clock) replaced `loser` without its writer having seen it, with a different value."""
    return winner["hlc"] > loser["hlc"] and (winner.get("base") is None or winner["base"] < loser["hlc"]) \
        and not _same(winner, loser)


def _note_conflict(session: Session, subject: str, winner: dict, loser: dict) -> None:
    cid = conflict_id(subject, winner["op_id"], loser["op_id"])
    if session.get(GroupWriteConflict, cid) is not None:
        return
    session.add(GroupWriteConflict(id=cid, subject=subject, winner_op=winner["op_id"], loser_op=loser["op_id"],
                                   winner_node=winner["origin"], loser_node=loser["origin"],
                                   winner_hlc=winner["hlc"], loser_hlc=loser["hlc"], detected_at=now()))


def _settle(session: Session, ctx: Any, row: GroupWrite, body: dict) -> None:
    """Count one applied write: conflicts against every other applied write of its subject, then the subject's
    state (highest clock wins) and, when that changed, what users see."""
    for other in session.scalars(select(GroupWrite).where(GroupWrite.subject == row.subject,
                                                          GroupWrite.status == "applied", GroupWrite.id != row.id)):
        ob = (other.envelope or {}).get("body") or {}
        if not ob:
            continue
        if _overwrote(body, ob):
            _note_conflict(session, row.subject, body, ob)
        elif _overwrote(ob, body):
            _note_conflict(session, row.subject, ob, body)
    state = session.get(GroupWriteState, row.subject)
    if state is not None and state.hlc >= body["hlc"]:
        session.flush()
        return
    if state is None:
        state = GroupWriteState(subject=row.subject, op_id=body["op_id"], hlc=body["hlc"], deleted=body["deleted"],
                                value=body["value"], updated_at=now())
        session.add(state)
    else:
        state.op_id, state.hlc, state.deleted, state.value = body["op_id"], body["hlc"], body["deleted"], body["value"]
        state.updated_at = now()
    session.flush()
    materialize(session, ctx, row.subject)


def _winning_target(session: Session, state: GroupWriteState) -> dict:
    row = session.scalars(select(GroupWrite).where(GroupWrite.op_id == state.op_id)).first()
    return (((row.envelope or {}).get("body") or {}).get("target") or {}) if row is not None else {}


def _wait(session: Session, subjects: list[GroupWriteState], reason: str | None) -> str | None:
    for s in subjects:
        s.waiting = reason
    session.flush()
    return reason


def materialize(session: Session, ctx: Any, subject: str) -> str | None:
    """Make what users see on this node match the state of `subject`. Returns why it must wait, or None. Whatever
    fails here rolls back to a savepoint and leaves the state waiting (`invalid`, counted on Admin > Node group):
    the state itself stays settled, and the round goes on (review C14-4, high)."""
    if subject.startswith("rec:"):
        cid = subject[4:].split("/", 1)[0]
        try:
            with session.begin_nested():
                return _materialize_record(session, ctx, cid)
        except Exception as exc:  # noqa: BLE001
            log.warning("super group: a record write could not be applied: %s", type(exc).__name__)
            return _wait(session, list(session.scalars(select(GroupWriteState).where(
                GroupWriteState.subject.like(f"rec:{cid}/%")))), "invalid")
    state = session.get(GroupWriteState, subject)
    if state is None:
        return None
    try:
        with session.begin_nested():
            reason = (_materialize_list if subject.startswith("list:") else _materialize_item)(session, ctx, state)
    except Exception as exc:  # noqa: BLE001 - a write that cannot be shown here waits; it never breaks the round
        log.warning("super group: %s could not be applied: %s", subject.split(":", 1)[0], type(exc).__name__)
        reason = "invalid"
    return _wait(session, [state], reason)


def _without(payload: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    """`payload` (a `community` observation's) without the record fields `fields` (the inverse of
    `proposals._merge_edit`)."""
    out = dict(payload)
    facts = dict(out.get("facts") or {})
    ext = dict(out.get("ext") or {})
    release = dict(ext.get("release") or {})
    for field in fields:
        if field == "release_date":
            out.pop("release_date", None)
            release.pop("first", None)
        elif field == "releases":
            release.pop("entries", None)
        elif field == "platforms":
            out.pop("platforms", None)
            out.pop("platform", None)
        else:
            facts.pop(field, None)
    for key, value in (("facts", facts), ("ext", {**ext, "release": release} if release else
                                          {k: v for k, v in ext.items() if k != "release"})):
        if value:
            out[key] = value
        else:
            out.pop(key, None)
    return out


def _retire_community(session: Session, record: str, fields: set[str]) -> bool:
    """Write once, synced to all: once the group holds a state for a record field, that state is the field's whole
    community layer on every member. This node's own `community` observation (an edit applied here, or one made
    before the super group) gives the field up, so a local value can neither outrank a group tombstone nor be unioned
    into a group list on one member only (review C14-4, high: a revert to "no value" left the other member's local
    edit showing there alone). Returns whether anything changed."""
    from . import proposals

    fields = fields & RETIRED_FIELDS
    if not fields:
        return False
    changed = False
    for obs in session.scalars(select(Observation).where(
            Observation.canonical_id == record, Observation.source == proposals.SOURCE,
            Observation.is_current.is_(True))):
        slim = _without(obs.payload or {}, fields)
        if slim != obs.payload:
            obs.payload, obs.payload_hash = slim, None  # the hash is recomputed lazily (migration 0034)
            changed = True
    if changed:
        session.flush()
    return changed


def refresh_record(session: Session, ctx: Any, cid: str) -> None:
    """After this node rewrote its own `community` observation of a record (a revert), show the group's states of
    that record again (nothing when the record has none)."""
    if enabled(ctx.settings) and CID_RE.match(cid or ""):
        materialize(session, ctx, f"rec:{cid}/")


def _materialize_record(session: Session, ctx: Any, cid: str) -> str | None:
    from . import assist, proposals, service

    states = list(session.scalars(select(GroupWriteState).where(GroupWriteState.subject.like(f"rec:{cid}/%"))))
    if not states:
        return None
    fields = {s.subject.split("/", 1)[1]: s.value for s in states if not s.deleted}
    row = session.get(Canonical, cid)
    if row is not None and row.merged_into:
        row = session.get(Canonical, row.merged_into) or row
    if row is None:
        media = next((t.get("media_type") for t in (_winning_target(session, s) for s in states)
                      if t.get("media_type")), None) or "game"
        if "title" not in fields:  # an edit of a record this node lacks: ask the group for it, then retry
            assist.note_missing(session, ctx.settings, cid, reason="user")
            return _wait(session, states, "no_record")
        if session.get(MediaType, media) is None:
            return _wait(session, states, "media_type")
        row = Canonical(id=cid, view={}, explain={}, media_type=media, scope_ceiling="group", scope_ceiling_by=WRITER)
        session.add(row)
        session.flush()
    core = {k: v for k, v in fields.items() if k != "provider_ids"}
    try:
        payload = proposals._merge_edit({}, core) if core else {}
    except Exception:  # noqa: BLE001 - a value this node's validators refuse (an unparsable date)
        return _wait(session, states, "invalid")
    if fields.get("provider_ids"):
        payload["provider_ids"] = fields["provider_ids"]
    retired = _retire_community(session, row.id, {s.subject.split("/", 1)[1] for s in states})
    current = list(session.scalars(select(Observation).where(
        Observation.source == GROUP_WRITE_SOURCE, Observation.source_scope == SCOPE, Observation.external_id == cid,
        Observation.is_current.is_(True))))
    if len(current) == 1 and current[0].payload == payload and current[0].canonical_id == row.id:
        if retired:
            service.rebuild(session, ctx, row.id, cause="local")
        return _wait(session, states, None)
    for o in current:
        o.is_current = False
    if payload:
        obs = Observation(id=ulid(), canonical_id=row.id, source_kind="metadata", source=GROUP_WRITE_SOURCE,
                          source_scope=SCOPE, external_id=cid, payload=payload, redistributable=False,
                          origin_node=None, is_current=True)
        session.add(obs)
        session.flush()
        for key in service.keys_of(obs):  # only free crosswalk keys: never a merge or a fold-in (as C14 §3.4)
            if session.get(Crosswalk, key) is None:
                session.add(Crosswalk(namespace=key[0], external_id=key[1], canonical_id=row.id))
    session.flush()
    service.rebuild(session, ctx, row.id, cause="local")
    return _wait(session, states, None)


def _local_user(session: Session, uid: str) -> str | None:
    group = _active_group(session)
    if group is None:
        return None
    link = session.get(GroupAccountLink, (group.group_id, uid))
    return link.user_id if link is not None else None


def _find_list(session: Session, user_id: str, key: str) -> UserList | None:
    if "." in key:
        kind = key.split(".", 1)[1]
        return session.scalars(select(UserList).where(UserList.user_id == user_id, UserList.kind == kind)
                               .order_by(UserList.created_at)).first()
    row = session.get(UserList, key)
    return row if row is not None and row.user_id == user_id else None


LIST_NAME_MAX = 80  # `social.create_list` and `_value_ok`


def _suffixed(name: str, key: str) -> str:
    """A clashing list name with the end of its key, never longer than a list name may be (review C14-4, low)."""
    tail = f" ({key[-6:]})"
    return f"{name[:LIST_NAME_MAX - len(tail)]}{tail}"


def _key_of_row(session: Session, row: UserList) -> str:
    """A local list's key in the group (as `list_key`), from its own row."""
    if row.kind in ("favourites", "wishlist"):
        uid = account_of(session, row.user_id)
        return f"{uid}.{row.kind}" if uid else row.id
    return row.id


def _free_name(session: Session, user_id: str, name: str, key: str, own_id: str | None) -> str:
    """The name a list is shown under here. Two lists of one account may carry the same name (each was created on
    a different member before they were linked); the unique constraint needs one of them renamed, and every member
    must rename the same one: the list with the smaller key keeps the plain name, the other gets the end of its key.
    So all members show the same names once they hold the same lists, whatever order the lists arrived in."""
    q = select(UserList).where(UserList.user_id == user_id, UserList.name == name)
    if own_id is not None:
        q = q.where(UserList.id != own_id)
    other = session.scalars(q).first()
    if other is None:
        return name
    other_key = _key_of_row(session, other)
    if other_key < key:
        return _suffixed(name, key)
    moved = _suffixed(name, other_key)
    if session.scalars(select(UserList.id).where(UserList.user_id == user_id, UserList.name == moved)).first():
        return _suffixed(name, key)  # pragma: no cover - both suffixed names taken: keep this one suffixed
    other.name = moved
    session.flush()
    return name


def _materialize_list(session: Session, ctx: Any, state: GroupWriteState) -> str | None:
    target = _winning_target(session, state)
    user_id = _local_user(session, target.get("account", ""))
    if user_id is None:
        return "no_account"
    key = state.subject[5:]
    row = _find_list(session, user_id, key)
    if state.deleted:
        if row is not None:
            session.query(ListItem).filter(ListItem.list_id == row.id).delete()
            session.delete(row)
        return None
    value = state.value or {}
    if not ctx.settings.lists_enabled:
        return "lists_off"  # the same rules as a list made here; shown once an administrator turns lists on
    if row is None:
        held = session.scalar(select(func.count()).select_from(UserList).where(UserList.user_id == user_id)) or 0
        if held >= ctx.settings.max_lists_per_user:
            return "limit"
        row = UserList(id=key if "." not in key else ulid(), user_id=user_id, kind=value["kind"],
                       name=_free_name(session, user_id, value["name"].strip(), key, None),
                       description=value["description"], public=value["public"])
        session.add(row)
    else:
        row.name = _free_name(session, user_id, value["name"].strip(), key, row.id)
        row.description, row.public = value["description"], value["public"]
    session.flush()
    for item in session.scalars(select(GroupWriteState).where(GroupWriteState.subject.like(f"item:{key}/%"),
                                                              GroupWriteState.waiting == "no_list")):
        materialize(session, ctx, item.subject)
    return None


def _materialize_item(session: Session, ctx: Any, state: GroupWriteState) -> str | None:
    from . import assist

    target = _winning_target(session, state)
    user_id = _local_user(session, target.get("account", ""))
    if user_id is None:
        return "no_account"
    key, cid = state.subject[5:].rsplit("/", 1)
    lst = _find_list(session, user_id, key)
    if not ctx.settings.lists_enabled and not state.deleted:
        return "lists_off"
    if lst is None and state.deleted:
        return None
    if lst is None:
        if "." not in key:
            return "no_list"
        kind = key.split(".", 1)[1]  # favourites and wishlist exist for every account
        lst = UserList(user_id=user_id, kind=kind, public=False,
                       name=_free_name(session, user_id, "Favourites" if kind == "favourites" else "Wishlist", key,
                                       None))
        session.add(lst)
        session.flush()
    live = _live(session, session.get(Canonical, cid))
    held = live.id if live is not None else cid  # merged here: the entry is the survivor's, as `add_to_list` makes it
    if held != cid:
        stale = session.get(ListItem, (lst.id, cid))
        if stale is not None:
            session.delete(stale)
    existing = session.get(ListItem, (lst.id, held))
    if state.deleted:
        if existing is not None and held == cid:
            session.delete(existing)
        return None  # a survivor's own entry is its own subject (review C14-4, low)
    if live is None:
        assist.note_missing(session, ctx.settings, cid, reason="user")
        return "no_record"
    if held != cid and existing is not None:
        return None
    note = ((state.value or {}).get("note") or "")[:200]
    if existing is None:
        count = session.scalar(select(func.count()).select_from(ListItem).where(ListItem.list_id == lst.id)) or 0
        if count >= ctx.settings.max_list_items:
            return "limit"
        session.add(ListItem(list_id=lst.id, canonical_id=held, note=note))
    else:
        existing.note = note
    return None


def _live(session: Session, row: Canonical | None) -> Canonical | None:
    """The record a merged-away record now lives on (itself when it was not merged; None when unknown here)."""
    seen = 0
    while row is not None and row.merged_into and seen < 16:
        row = session.get(Canonical, row.merged_into)
        seen += 1
    return None if row is not None and row.merged_into else row


def on_feed_introduced(session: Session, ctx: Any, cid: str) -> bool:
    """A record this node first held from a replicated write is now introduced by a feed it follows: the `group`
    ceiling the write set is lifted (an operator's never is). The write's own observation stays group data."""
    from . import scope, service

    row = session.get(Canonical, cid)
    if row is None or row.scope_ceiling_by != WRITER:
        return False
    row.scope_ceiling, row.scope_ceiling_by = None, None
    session.flush()
    service.rebuild(session, ctx, cid, cause="local")
    scope.reproject(session, ctx, cid)
    return True


# ---- receiving ----------------------------------------------------------------------------------------------------

def _origin_keys(tr: Any, origin: str) -> dict[str, str]:
    from .group_channel import _keys_of

    return _keys_of(tr, origin)


def _hlc_time(hlc: str) -> datetime:
    return datetime.fromtimestamp(int(hlc[:13]) / 1000, tz=UTC)


def _windows(tr: Any, origin: str) -> tuple[list[tuple[datetime, datetime, datetime | None]], datetime | None]:
    """The origin's signed standing, from the group's trust state only: for each genuine certificate the time it
    covers (not_before - skew .. not_after + skew) and when it was revoked (a `superseded` rotation does not count),
    and when the node was banned. Cached on the trust object for one round."""
    from . import groups

    cache = tr.__dict__.setdefault("_super_group_windows", {})
    if origin in cache:
        return cache[origin]
    windows = []
    for c in tr.node_certs(origin):
        if not tr.genuine(c):
            continue
        b = c["body"]
        rev = tr.revoked.get(b["serial"])
        revoked = groups.aware(rev.revoked_at) if rev is not None and rev.reason != "superseded" else None
        windows.append((groups.t(b["not_before"]) - groups.SKEW, groups.t(b["not_after"]) + groups.SKEW, revoked))
    ban = tr.banned.get(origin)
    banned = groups.aware(ban.revoked_at) if ban is not None else None
    cache[origin] = (windows, banned)
    return cache[origin]


def standing(tr: Any, origin: str, hlc: str) -> str | None:
    """Whether a write is accepted, decided from the write itself (its origin and its signed clock) against the
    group's signed trust state, never from when this node happened to receive it: every member that holds the same
    writes and the same certificates and revocations decides the same way, whatever the order of arrival.
    None: accepted. `origin_removed`: written at or after its origin was revoked, banned or left (refused for good).
    `origin_expired`: no certificate of its origin covers the time it was written (D7: held until one does)."""
    at = _hlc_time(hlc)
    windows, banned = _windows(tr, origin)
    if banned is not None and at >= banned:
        return "origin_removed"
    removed = False
    for start, end, revoked in windows:
        if not start <= at <= end:
            continue
        if revoked is not None and at >= revoked:
            removed = True
            continue
        return None
    if removed:
        return "origin_removed"
    # Not covered by any certificate. In the gap after a removal: a later re-enrolment never reaches back over it.
    if any(revoked is not None and revoked <= at for _start, _end, revoked in windows):
        return "origin_removed"
    # D7 covers a lapse only: a certificate had ended before the write and an unrevoked one starts after it. A write
    # dated before the origin's first certificate (before it joined) is not one: it stays held (review C14-4, low).
    lapsed = any(end < at for _start, end, _revoked in windows)
    if lapsed and any(start > at and revoked is None for start, _end, revoked in windows):
        return None
    return "origin_expired"


def _clock_ahead(hlc: str) -> bool:
    return int(hlc[:13]) > int((datetime.now(UTC) + CLOCK_AHEAD).timestamp() * 1000)


def _verified(ctx: Any, tr: Any, obj: dict) -> bool:
    """The write is signed by its origin's key (from the group's key history; this node's own current key too)."""
    body, signer = obj.get("body") or {}, obj.get("signer") or {}
    origin = body.get("origin")
    if not isinstance(origin, str) or signer.get("kind") != "node" or signer.get("id") != origin:
        return False
    if origin == ctx.identity.node_id:
        pub = {ctx.identity.key_id: ctx.identity.public_b64} if hasattr(ctx.identity, "public_b64") else {}
        pub.update(_origin_keys(tr, origin))
    else:
        pub = _origin_keys(tr, origin)
    key = pub.get(signer.get("key_id") or "")
    return key is not None and gc.verify_object(obj, key)


def receive(session: Session, ctx: Any, group: Any, tr: Any, obj: Any) -> str:
    """Store and apply one pulled write. Returns `applied`, `held`, `known`, `refused` or `invalid`."""
    own = ctx.identity.node_id
    if not isinstance(obj, dict) or obj.get("t") != OBJECT or obj.get("group_id") != group.group_id:
        return "invalid"
    body, signer = obj.get("body"), obj.get("signer") or {}
    why = body_ok(body)
    if why == "invalid":
        return "invalid"
    if signer.get("kind") != "node" or signer.get("id") != body["origin"] or obj.get("id") != body["op_id"]:
        return "invalid"
    if session.scalars(select(GroupWrite.id).where(GroupWrite.op_id == body["op_id"])).first() is not None:
        return "known"
    stand = standing(tr, body["origin"], body["hlc"])
    if stand == "origin_removed":
        return "refused"  # written after its origin was removed: never taken, on any member
    if not _verified(ctx, tr, obj):
        return "invalid"
    held_reason = None
    if why == "unknown_kind":
        held_reason = "unknown_kind"
    elif stand == "origin_expired":
        held_reason = "origin_expired"  # D7: held until a (renewed) certificate covers the time it was written
    elif _clock_ahead(body["hlc"]):
        held_reason = "clock_ahead"  # temporary: released once this node's clock reaches it (same result later)
    row = GroupWrite(op_id=body["op_id"], origin_node=body["origin"], seq=body["seq"], kind=str(body["kind"])[:24],
                     subject=body["subject"], hlc=body["hlc"], status="held" if held_reason else "applied",
                     reason=held_reason, envelope=obj, received_at=now())
    session.add(row)
    session.flush()
    if body["origin"] == own:  # a restore lost this node's own write: keep numbering after it
        _put(session, SEQ, str(max(_int(session, SEQ), body["seq"])))
    if held_reason:
        return "held"
    observe(session, body["hlc"])
    _settle(session, ctx, row, body)
    return "applied"


def _receive_one(session: Session, ctx: Any, group: Any, tr: Any, obj: Any) -> str:
    """`receive` in its own savepoint: a write that fails to apply here is kept, held as `failed` (counted on Admin
    > Node group, served onward like any write this node holds, never applied here), and the round goes on, so the
    cursor moves past it and every other write still arrives (review C14-4, high: one bad write had rolled back the
    whole round, every round)."""
    try:
        with session.begin_nested():
            return receive(session, ctx, group, tr, obj)
    except Exception as exc:  # noqa: BLE001
        if _transient(exc):
            raise  # the round rolls back and its cursor stays: the next round receives this write again
        log.warning("super group: a write could not be applied here: %s", type(exc).__name__)
    body = obj.get("body") if isinstance(obj, dict) else None
    try:
        if (body_ok(body) == "invalid" or obj.get("group_id") != group.group_id or obj.get("id") != body["op_id"]
                or not _verified(ctx, tr, obj)):
            return "invalid"
        with session.begin_nested():
            if session.scalars(select(GroupWrite.id).where(GroupWrite.op_id == body["op_id"])).first() is None:
                session.add(GroupWrite(op_id=body["op_id"], origin_node=body["origin"], seq=body["seq"],
                                       kind=str(body["kind"])[:24], subject=body["subject"], hlc=body["hlc"],
                                       status="held", reason="failed", envelope=obj, received_at=now()))
                session.flush()
    except Exception as exc:  # noqa: BLE001 - not even storable: counted, skipped
        if _transient(exc):
            raise
        log.warning("super group: a write could not be kept: %s", type(exc).__name__)
        return "invalid"
    return "failed"


def _transient(exc: BaseException) -> bool:
    """Contention or a lost connection (SQLite `database is locked`, a PostgreSQL deadlock or lock timeout): not
    the write's fault. Keeping it as `failed`, or skipping it as `invalid`, would move the cursor past a good write
    and lose it on this node (review of C16 Z1: the PostgreSQL-authority lab lost a write this way)."""
    import sqlite3

    from sqlalchemy import exc as sa_exc

    from .db import error_class

    return isinstance(exc, sa_exc.DBAPIError | sqlite3.Error) and error_class(exc) in (
        "lock_timeout", "deadlock", "serialization", "connection")


def retry(session: Session, ctx: Any, tr: Any) -> dict[str, int]:
    """Each round: writes held for their origin's certificate (D7) or a clock ahead, and states waiting for an
    account link, a list or a record."""
    out = {"released": 0, "refused": 0, "withdrawn": 0, "materialized": 0}
    for row in list(session.scalars(select(GroupWrite).where(
            GroupWrite.status == "held", GroupWrite.reason.in_(("origin_expired", "clock_ahead", "failed")))
            .order_by(GroupWrite.id))):
        body = (row.envelope or {}).get("body") or {}
        stand = standing(tr, row.origin_node, row.hlc)
        if stand == "origin_removed":
            row.status, row.reason = "refused", "origin_removed"
            out["refused"] += 1
            continue
        if stand is not None or _clock_ahead(row.hlc):
            row.reason = stand or "clock_ahead"
            continue
        try:
            with session.begin_nested():  # a write that still fails stays held (`failed`): the round goes on
                row.status, row.reason = "applied", None
                session.flush()
                observe(session, row.hlc)
                _settle(session, ctx, row, body)
        except Exception as exc:  # noqa: BLE001
            log.warning("super group: a held write still cannot be applied: %s", type(exc).__name__)
            row.status, row.reason = "held", "failed"
            continue
        out["released"] += 1
    out["withdrawn"] = _recheck_removed(session, ctx, tr)
    # states waiting to be shown here: oldest attempt first, so a few that wait for good (an account never linked
    # here, a value this release refuses) never starve the others
    done: set[str] = set()
    stamp = now()
    for state in list(session.scalars(select(GroupWriteState).where(GroupWriteState.waiting.is_not(None))
                                      .order_by(GroupWriteState.tried_at.asc().nullsfirst(), GroupWriteState.subject)
                                      .limit(RETRY_BATCH))):
        key = state.subject if not state.subject.startswith("rec:") else state.subject.split("/", 1)[0]
        state.tried_at = stamp
        if key in done:
            continue
        done.add(key)
        if materialize(session, ctx, state.subject) is None:
            out["materialized"] += 1
    session.flush()
    return out


def _removed_cutoffs(tr: Any) -> dict[str, list[str]]:
    """Every node with a ban or a (non-rotation) revocation, with the signed facts that decide it (a fingerprint:
    the recheck below runs again only when these change)."""
    from . import groups

    out: dict[str, list[str]] = {}
    for node, ban in tr.banned.items():
        out.setdefault(node, []).append(f"ban:{_iso(groups.aware(ban.revoked_at))}")  # type: ignore[arg-type]
    for c in tr.certs.values():
        b = c["body"]
        rev = tr.revoked.get(b["serial"])
        if rev is not None and rev.reason != "superseded":
            out.setdefault(b["member"]["node_id"], []).append(f"{b['serial']}:{rev.reason}")
    return {k: sorted(v) for k, v in out.items()}


def _recheck_removed(session: Session, ctx: Any, tr: Any) -> int:
    """A revocation that arrived after some of its node's writes: the writes it dates as written at or after the
    removal are withdrawn here too (status `refused`, their conflict entries dropped, their subjects recomputed), so
    a member that applied them before it heard of the removal ends where a member that refused them on arrival is."""
    cutoffs = _removed_cutoffs(tr)
    mark = hashlib.sha256(json.dumps(cutoffs, sort_keys=True).encode()).hexdigest()
    if _state(session, RECHECKED) == mark:
        return 0
    withdrawn = 0
    subjects: set[str] = set()
    for node in sorted(cutoffs):
        for row in list(session.scalars(select(GroupWrite).where(GroupWrite.origin_node == node,
                                                                 GroupWrite.status.in_(("applied", "held"))))):
            if standing(tr, node, row.hlc) != "origin_removed":
                continue
            was_applied = row.status == "applied"
            row.status, row.reason = "refused", "origin_removed"
            withdrawn += 1
            session.query(GroupWriteConflict).filter(
                (GroupWriteConflict.winner_op == row.op_id) | (GroupWriteConflict.loser_op == row.op_id)).delete(
                synchronize_session=False)
            if was_applied:
                subjects.add(row.subject)
    session.flush()
    for subject in sorted(subjects):
        _recompute(session, ctx, subject)
    _put(session, RECHECKED, mark)
    return withdrawn


def _recompute(session: Session, ctx: Any, subject: str) -> None:
    """A subject's state from its applied writes again (after one was withdrawn). With none left it is shown as
    removed and its state row goes, exactly as on a member that never applied the withdrawn write."""
    state = session.get(GroupWriteState, subject)
    best = None
    for row in session.scalars(select(GroupWrite).where(GroupWrite.subject == subject,
                                                        GroupWrite.status == "applied")):
        if best is None or row.hlc > best.hlc:
            best = row
    if best is None:
        if state is None:
            return
        state.deleted, state.value = True, None
        session.flush()
        materialize(session, ctx, subject)  # a tombstone: the field, list or item goes
        session.delete(state)
        session.flush()
        return
    body = (best.envelope or {}).get("body") or {}
    if state is None:
        state = GroupWriteState(subject=subject, op_id=best.op_id, hlc=best.hlc, deleted=bool(body.get("deleted")),
                                value=body.get("value"), updated_at=now())
        session.add(state)
    else:
        state.op_id, state.hlc, state.deleted, state.value = best.op_id, best.hlc, bool(body.get("deleted")), \
            body.get("value")
        state.updated_at = now()
    session.flush()
    materialize(session, ctx, subject)


# ---- backfill: what existed before the super group (or the account link) --------------------------------------------

def backfill(session: Session, ctx: Any, tr: Any) -> int:
    """Once per linked account: write the lists, favourites, wishlist and list entries it already had on this node
    before the super group was turned on or before the account was linked here, so they reach every member like a
    new write. Only subjects with no state yet are written (a list another member already wrote is left to that
    write). Favourites and wishlist are one per account, so the entries of every member end up in one list
    everywhere. Record edits made before the super group are not replayed: the records themselves already reach
    the members through the feeds and ask-the-group (C14-2); only edits from then on are written once, synced to
    all. Returns the number of writes made."""
    if not enabled(ctx.settings):
        return 0
    group = _active_group(session)
    if group is None:
        return 0
    made = 0
    for link_row in list(session.scalars(select(GroupAccountLink).where(GroupAccountLink.group_id == group.group_id)
                                         .order_by(GroupAccountLink.account_uid))):
        uid, user_id = link_row.account_uid, link_row.user_id
        if _state(session, BACKFILLED + uid) is not None:
            continue
        user = session.get(User, user_id)
        if user is None:
            continue
        try:
            with session.begin_nested():  # one account that cannot be written never stops the round
                made += _backfill_account(session, ctx, tr, uid, user_id)
        except Exception as exc:  # noqa: BLE001 - tried again next round
            log.warning("super group: an account's lists could not be written: %s", type(exc).__name__)
            continue
        _put(session, BACKFILLED + uid, _iso(datetime.now(UTC)))
    session.flush()
    return made


def _backfill_account(session: Session, ctx: Any, tr: Any, uid: str, user_id: str) -> int:
    made = 0
    for lst in list(session.scalars(select(UserList).where(UserList.user_id == user_id)
                                    .order_by(UserList.created_at, UserList.id))):
        key = list_key(session, lst, uid)
        if session.get(GroupWriteState, f"list:{key}") is None:
            value = {"name": lst.name, "description": lst.description or "", "public": bool(lst.public),
                     "kind": lst.kind if lst.kind in LIST_KINDS else "custom"}
            if emit(session, ctx, "list.meta", {"list": key, "account": uid}, value, author=user_id,
                    tr=tr) is not None:
                made += 1
        for item in list(session.scalars(select(ListItem).where(ListItem.list_id == lst.id)
                                         .order_by(ListItem.canonical_id))):
            cid = item.canonical_id
            if not CID_RE.match(cid or "") or kept_here(session, cid):
                continue
            if session.get(GroupWriteState, f"item:{key}/{cid}") is not None:
                continue
            if emit(session, ctx, "list.item", {"list": key, "account": uid, "cid": cid},
                    {"note": (item.note or "")[:200]}, author=user_id, tr=tr) is not None:
                made += 1
    return made


# ---- serving and pulling (the sealed `group.writes` op) -----------------------------------------------------------

def serve(session: Session, settings: Any, identity: Any, tr: Any, requester: str, params: Any) -> dict:
    """`group.writes`: every write this node holds after `since` (its own receive order), its own and relayed ones,
    except those of removed members. `since` also tells this node how far the requester has come (`ACK`)."""
    if not enabled(settings):
        raise WriteError("forbidden")
    since = params.get("since", 0) if isinstance(params, dict) else None
    if since is None:
        since = 0
    if type(since) is not int or since < 0:
        raise WriteError("invalid")
    head = session.scalar(select(func.max(GroupWrite.id))) or 0
    acked = min(since, head)  # a requester cannot claim to have pulled writes this node does not hold yet
    if acked > _int(session, ACK + requester):
        _put(session, ACK + requester, str(acked))
    rows = list(session.scalars(select(GroupWrite).where(GroupWrite.id > since, GroupWrite.status != "refused")
                                .order_by(GroupWrite.id).limit(PAGE)))
    # Every write this node accepted or holds, whoever wrote it: a write of a member removed later, dated before
    # its removal, counts on every member, so it is relayed too (each receiver judges it by `standing` itself).
    items = [r.envelope for r in rows]
    return {"items": items, "last": rows[-1].id if rows else since, "more": len(rows) == PAGE}


def _ops_of(session: Session, settings: Any, identity: Any, group: Any, node: str, cert: dict, http: Any,
            call: Callable[..., Any]) -> list[str] | None:
    """The ops a member advertised (`hello`), from the channel's in-memory cache, this node's stored copy, or a
    fresh `hello`. Reads only: the caller stores what it learned after its network calls."""
    from . import assist, group_channel

    cached = group_channel._HELLO.get(node)
    if cached is not None and time.monotonic() - cached[0] <= group_channel.HELLO_TTL:
        return [o for o in cached[1].get("ops") or [] if isinstance(o, str)]
    try:
        hello = call(session, settings, identity, group, cert["body"]["member"], "hello", {}, http)
    except Exception:  # noqa: BLE001 - unreachable now: fall back to what it said last time
        return (assist._json(session, "assist_hello:" + node) or {}).get("ops")
    if not isinstance(hello, dict):
        return None
    group_channel._HELLO[node] = (time.monotonic(), hello)
    return [o for o in hello.get("ops") or [] if isinstance(o, str)]


def pull(session: Session, ctx: Any, group: Any, tr: Any, http: Any, call: Callable[..., Any]) -> dict[str, Any]:
    """One round: pull every member that serves `group.writes`, tell the members (`notify`) when this node has new
    writes of its own, then apply what arrived and retry what waits. Every network call happens before this round
    writes anything, so the SQLite write lock is never held across a call to another node. A member without the op
    is degraded (D8): shown to administrators, skipped here, never an error for users."""
    from . import assist

    settings, identity = ctx.settings, ctx.identity
    out: dict[str, Any] = {"received": 0, "applied": 0, "held": 0, "invalid": 0, "members": {}}
    if not enabled(settings):
        return out
    own_head = session.scalar(select(func.max(GroupWrite.id)).where(GroupWrite.origin_node == identity.node_id)) or 0
    announce = own_head > _int(session, NOTIFIED)
    learned: dict[str, list[str]] = {}
    fetched: dict[str, tuple[str, list, int]] = {}
    taking: list[str] = []
    # 1. the network: hello where needed, the pages of every member, `notify` to those reached
    for node, cert in sorted(tr.members().items()):
        if node == identity.node_id:
            continue
        ops = _ops_of(session, settings, identity, group, node, cert, http, call)
        if ops is not None:
            learned[node] = ops
        if OP not in (ops or []):
            fetched[node] = ("unsupported", [], 0)
            continue
        taking.append(node)
        cursor = start = _int(session, CURSOR + node)
        items: list = []
        outcome = "ok"
        for _ in range(MAX_PAGES):
            try:
                got = call(session, settings, identity, group, cert["body"]["member"], OP, {"since": cursor}, http)
            except Exception as exc:  # noqa: BLE001 - offline, policy off: this member waits for the next round
                text = str(exc)
                outcome = "off" if "forbidden" in text else ("unreachable" if "unreachable" in text else "error")
                break
            if not isinstance(got, dict) or type(got.get("last")) is not int:
                outcome = "error"
                break
            items.extend((got.get("items") or [])[:PAGE])
            cursor = max(cursor, got["last"])
            if not got.get("more"):
                break
        fetched[node] = (outcome, items, cursor if cursor >= start else start)
        if outcome == "ok" and announce:
            try:
                call(session, settings, identity, group, cert["body"]["member"], "notify", {}, http)
            except Exception:  # noqa: BLE001 - a hint only
                pass
    # 2. this node's database
    for node, ops in learned.items():
        assist._put(session, "assist_hello:" + node, {"ops": ops})
    if any(items for _outcome, items, _cursor in fetched.values()):
        _serialise(session)
    for node, (outcome, items, cursor) in fetched.items():
        for obj in items:
            res = _receive_one(session, ctx, group, tr, obj)
            out["received"] += 1
            if res in ("applied", "held", "invalid", "failed"):
                out[res] = out.get(res, 0) + 1
        out["members"][node] = outcome
        if outcome == "unsupported":
            _put(session, ERROR + node, outcome)
            continue
        _put(session, CURSOR + node, str(cursor))
        if outcome == "ok":
            _put(session, SEEN + node, _iso(datetime.now(UTC)))
            session.query(NodeState).filter(NodeState.key == ERROR + node).delete()
        else:
            _put(session, ERROR + node, outcome)
    _put(session, MEMBERS, taking)
    if announce:
        _put(session, NOTIFIED, str(own_head))
    out["backfilled"] = backfill(session, ctx, tr)
    out.update(retry(session, ctx, tr))
    session.flush()
    return out


# ---- what users and administrators see ----------------------------------------------------------------------------

def _members_taking(session: Session) -> list[str]:
    raw = _state(session, MEMBERS)
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [n for n in value if isinstance(n, str)] if isinstance(value, list) else []


def user_status(session: Session, settings: Any, user_id: str | None) -> dict | None:
    """The only super-group facts a user page shows: whether this user's own writes are still on their way to the
    other nodes (a generic "syncing" state) and how fresh the group is. Never a node name, id or count."""
    if not enabled(settings) or _active_group(session) is None:
        return None
    members = _members_taking(session)
    syncing = False
    if user_id and members:
        floor = min(_int(session, ACK + n) for n in members)
        syncing = session.scalars(select(GroupWrite.id).where(GroupWrite.author_user_id == user_id,
                                                              GroupWrite.id > floor).limit(1)).first() is not None
    seen = [_when(_state(session, SEEN + n)) for n in members]
    synced = min(seen) if members and all(seen) else None
    return {"syncing": syncing, "synced_at": synced}


def overview(session: Session, settings: Any, identity: Any) -> dict | None:
    """Admin > Node group: the topology the users never see (D8 degraded members included)."""
    from . import groups

    group = _active_group(session)
    if group is None:
        return None
    tr = groups.trust(session, group, own_node=identity.node_id)
    own_head = session.scalar(select(func.max(GroupWrite.id))) or 0
    members = []
    for node, cert in sorted(tr.members().items()):
        if node == identity.node_id:
            continue
        ack = _int(session, ACK + node)
        members.append({"node": node, "name": cert["body"]["member"].get("name") or node,
                        "error": _state(session, ERROR + node), "seen": _when(_state(session, SEEN + node)),
                        "cursor": _int(session, CURSOR + node), "behind": max(own_head - ack, 0),
                        "status": tr.status_of(node)})
    held = dict(session.execute(select(GroupWrite.reason, func.count()).where(GroupWrite.status == "held")
                                .group_by(GroupWrite.reason)).all())
    waiting = dict(session.execute(select(GroupWriteState.waiting, func.count())
                                   .where(GroupWriteState.waiting.is_not(None))
                                   .group_by(GroupWriteState.waiting)).all())
    conflicts = list(session.scalars(select(GroupWriteConflict).order_by(
        GroupWriteConflict.seen_at.is_not(None), GroupWriteConflict.detected_at.desc()).limit(50)))
    names = {m["node"]: m["name"] for m in members}
    names[identity.node_id] = "this node"
    return {"enabled": enabled(settings), "members": members, "held": held, "waiting": waiting,
            "writes": session.scalar(select(func.count()).select_from(GroupWrite)) or 0,
            "own": session.scalar(select(func.count()).select_from(GroupWrite)
                                  .where(GroupWrite.origin_node == identity.node_id)) or 0,
            "conflicts": conflicts, "open_conflicts": sum(1 for c in conflicts if c.seen_at is None),
            "names": names}


def admin_bar(session: Session, settings: Any, identity: Any) -> dict | None:
    """The compact group bar for administrators (every page): which member served this page and the group's health."""
    from . import groups

    if not enabled(settings):
        return None
    group = _active_group(session)
    if group is None:
        return None
    rows = list(session.scalars(select(groups.GroupMember).where(groups.GroupMember.group_id == group.group_id)))
    own = next((m for m in rows if m.node_id == identity.node_id), None)
    others = [m for m in rows if m.node_id != identity.node_id and m.status in ("active", "expiring")]
    unreached = sum(1 for m in others if _state(session, ERROR + m.node_id))
    open_conflicts = session.scalar(select(func.count()).select_from(GroupWriteConflict)
                                    .where(GroupWriteConflict.seen_at.is_(None))) or 0
    return {"served_by": (own.name if own is not None and own.name else identity.node_id), "members": len(others) + 1,
            "unreached": unreached, "conflicts": open_conflicts}


def acknowledge(session: Session, conflict: str, user_id: str) -> bool:
    row = session.get(GroupWriteConflict, conflict)
    if row is None or row.seen_at is not None:
        return False
    row.seen_at, row.seen_by = now(), user_id
    return True


def subject_label(subject: str) -> str:
    """A short admin label for a subject (`rec:<cid>/<field>` -> `<cid> · <field>`)."""
    kind, _, rest = subject.partition(":")
    if kind == "rec":
        cid, _, field = rest.partition("/")
        return f"{cid} · {field}"
    if kind == "item":
        key, _, cid = rest.rpartition("/")
        return f"list {key[-6:]} · {cid}"
    return f"list {rest[-6:]}"


__all__ = ["OP", "KINDS", "WriteError", "emit", "record_written", "list_written", "list_item_written", "receive",
           "pull", "serve", "retry", "materialize", "user_status", "overview", "admin_bar", "acknowledge",
           "conflict_id", "body_ok", "tick", "observe", "enabled"]
