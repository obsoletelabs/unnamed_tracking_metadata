"""Relationships between works (CONTRACTS-v1 C6, issue #86): DLC, expansions, ports, remakes, mods, series and
collection membership.

Model (C6.1, H11):

* Relations join **canonical records** (the work level) only. An edition is never a relation target: editions
  are the `editions[]` view of a record (`atlas.merge`), and `releases[].edition` refers to one by name.
* A relation is directed `from -> to`: `from` is the DLC, port, remake or member, `to` the parent, the base work,
  the series or the collection. Types are the closed vocabulary `vocab.RELATION_TYPES` (C6.2).
* `series_member` points at a record of media type `series`, `collection_member` at one of type `collection`
  (a bundle is a collection: each game in it is a `collection_member` of the bundle record).

Rules enforced on every add (local or replicated):

* no self relation, no relation to or from a merged record (ids follow merge redirects first);
* at most one active parent per (`from`, type family) for the hierarchical types, where the family `addon` is
  {dlc_of, expansion_of, standalone_expansion_of} and every other hierarchical type is its own family;
* no cycle: the add is refused when `to` already reaches `from` through active edges of the same group
  (hierarchical types together, membership types together). The search is a BFS bounded at depth 32 and 10 000
  visited records; a graph too large to check is refused (fail closed);
* at most `MAX_OUTGOING` active relations from one record. Incoming relations are not capped (a bundle may hold
  thousands of members); every read is bounded instead.

Replicated and local rows (C6.3, CONTRACT-AMENDMENTS A15.4): a row is *replicated* when every node of the feed
holds it: this authority published it (`feed_seq`) or it was applied from an upstream feed (`origin_node`). Every
other row is *local*: an authority's relation kept while gated below emit 1.4, or a mirror's overlay. Decisions
that every node must make alike (applying a replicated `add`, collapsing a merge) look only at replicated rows;
a local row that then breaks a rule is ended (`superseded`). Local state can therefore never change, or block,
what a follower does with its authority's entries.

Merges (C6.3): `move` carries the merged record's active relations to the survivor. One that would become a self
relation, a duplicate, a second parent or a cycle is ended instead (`end_reason` says which), so an active row
never names a merged record. Replicated rows are decided first and only against replicated rows, so every node
reaches the same replicated rows from the same `merge` entry and no `end` entries are signed for it.

Federation (C1.6, C6.3, protocol 1.4): an authority signs each change as a `relation` entry
(`{op: add|end, from, to, type, source}`) once its `emit_protocol` is 1.4 or higher. Below that, relations are
stored locally and nothing is appended (H13). On other roles relations are local overlays and never signed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from . import feed, feed_schema, vocab
from .models import Canonical, ChangeLog, NodeState, Relation, ReplicatedChange, now
from .resolve import follow
from .service import AtlasError, Ctx, bump_version, write_lock

log = logging.getLogger(__name__)

TYPES: tuple[str, ...] = vocab.RELATION_TYPES
MEMBERSHIP: tuple[str, ...] = ("series_member", "collection_member")
HIERARCHICAL: tuple[str, ...] = tuple(t for t in TYPES if t not in MEMBERSHIP)
ADDON = frozenset({"dlc_of", "expansion_of", "standalone_expansion_of"})
#: The media type a membership relation must point at (C6.2).
MEMBER_TARGET = {"series_member": "series", "collection_member": "collection"}
#: Bounds of the cycle search (C6.3).
MAX_DEPTH, MAX_VISITED = 32, 10_000
#: Active relations one record may have *from* it (its parents and memberships).
MAX_OUTGOING = 200
#: Items per list in a record's `relations` block (C6.3: bounded at 200).
LIST_LIMIT = 200
#: `GET /games/{id}/relations?depth=N`: the largest N and the largest graph returned.
MAX_EXPAND_DEPTH, MAX_EXPAND_NODES = 4, 200
#: The protocol that ships the `relation` feed kind.
RELATION_PROTOCOL = feed_schema.FEED_KINDS["relation"].since
END_REASONS = ("removed", "replaced", "merge_self", "merge_duplicate", "merge_conflict", "feed", "superseded")
_ORDER = {t: i for i, t in enumerate(TYPES)}
_CHUNK = 500
#: Rows one cycle-search step may read per chunk of 500 records; more is refused as too large (fail closed).
_ROW_CAP = 2 * MAX_VISITED
#: Rows one `expand` query reads at a time.
_PAGE = 256
#: Set by migration 0032 when it brings the kept table back; `finish_restore` then replays the merges.
RESTORE_KEY = "relation_0032_restore"

LABELS = {
    "version_of": ("Version of", "Versions"),
    "remake_of": ("Remake of", "Remakes"),
    "remaster_of": ("Remaster of", "Remasters"),
    "port_of": ("Port of", "Ports"),
    "dlc_of": ("DLC for", "DLC"),
    "expansion_of": ("Expansion for", "Expansions"),
    "standalone_expansion_of": ("Standalone expansion of", "Standalone expansions"),
    "mod_of": ("Mod of", "Mods"),
    "modpack_of": ("Modpack for", "Modpacks"),
    "total_conversion_of": ("Total conversion of", "Total conversions"),
    "series_member": ("Part of the series", "Games in this series"),
    "collection_member": ("Included in", "Contents"),
}


def family(kind: str) -> str:
    return "addon" if kind in ADDON else kind


def group(kind: str) -> tuple[str, ...]:
    return MEMBERSHIP if kind in MEMBERSHIP else HIERARCHICAL


def type_order(kind: str) -> int:
    return _ORDER.get(kind, len(_ORDER))


def gated(ctx: Ctx) -> bool:
    """True while this authority may not sign a relation: its `emit_protocol` is below 1.4 (C1.6, H13)."""
    if ctx.settings.role != "authority":
        return False
    return feed_schema.parse_protocol(feed_schema.emit_protocol(ctx.settings)) < \
        feed_schema.parse_protocol(RELATION_PROTOCOL)


def gate_message(ctx: Ctx, feature: str = "publishing relations") -> str:
    return str(feed_schema.EmitGateError(feature, RELATION_PROTOCOL, feed_schema.emit_protocol(ctx.settings)))


# ---- reads ----------------------------------------------------------------------------------------------

def _active() -> Any:
    return Relation.ended_at.is_(None)


def _replicated() -> Any:
    """Rows every node of the feed holds: published here (`feed_seq`) or applied from an upstream (`origin_node`)."""
    return Relation.feed_seq.is_not(None) | Relation.origin_node.is_not(None)


def _local() -> Any:
    return Relation.feed_seq.is_(None) & Relation.origin_node.is_(None)


def is_replicated(row: Relation) -> bool:
    return row.feed_seq is not None or row.origin_node is not None


def active_between(session: Session, from_id: str, to_id: str, kind: str) -> Relation | None:
    return session.scalars(select(Relation).where(Relation.from_id == from_id, Relation.to_id == to_id,
                                                  Relation.type == kind, _active())).first()


def _chunks(items: list[str]) -> Iterable[list[str]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i:i + _CHUNK]


def _search(session: Session, start: str, goal: str, kinds: Iterable[str], *, skip: str | None = None,
            alias: dict[str, str] | None = None, replicated_only: bool = False) -> list[str] | None:
    """A path (relation ids, in order) from `start` to `goal` along active `from -> to` edges of `kinds`, or None.
    Raises AtlasError when the search passes its bounds (fail closed). `skip`: a relation id to ignore; `alias`:
    ids to read as others (a merge in progress reads the merged record as the survivor); `replicated_only`: walk
    only rows every node of the feed holds."""
    alias = alias or {}
    kinds = tuple(kinds)
    prev: dict[str, tuple[str, str]] = {}  # record -> (relation id, record it was reached from)
    seen = {start}
    frontier = [start]

    def path(node: str) -> list[str]:
        out: list[str] = []
        while node != start:
            rid, node = prev[node]
            out.append(rid)
        return out[::-1]

    for _depth in range(MAX_DEPTH):
        if not frontier:
            return None
        nxt: list[str] = []
        lookup = sorted({k for f in frontier for k in (f, *[a for a, b in alias.items() if b == f])})
        for part in _chunks(lookup):
            query = select(Relation.id, Relation.from_id, Relation.to_id).where(
                Relation.from_id.in_(part), Relation.type.in_(kinds), _active())
            if replicated_only:
                query = query.where(_replicated())
            found = session.execute(query.limit(_ROW_CAP + 1)).all()
            if len(found) > _ROW_CAP:
                raise AtlasError(f"the relation graph here has more than {_ROW_CAP} relations in one step; "
                                 "it is too large to check for a cycle", "conflict")
            for rid, from_id, to_id in found:
                if rid == skip:
                    continue
                from_id, to_id = alias.get(from_id, from_id), alias.get(to_id, to_id)
                if to_id in seen:
                    continue
                prev[to_id] = (rid, from_id)
                if to_id == goal:
                    return path(goal)
                seen.add(to_id)
                nxt.append(to_id)
                if len(seen) > MAX_VISITED:
                    raise AtlasError(f"the relation graph here has more than {MAX_VISITED} records; "
                                     "it is too large to check for a cycle", "conflict")
        frontier = nxt
    if frontier:
        raise AtlasError(f"the relation chain here is deeper than {MAX_DEPTH}; it is too deep to check for a cycle",
                         "conflict")
    return None


def reaches(session: Session, start: str, goal: str, kinds: Iterable[str], *, skip: str | None = None,
            alias: dict[str, str] | None = None, replicated_only: bool = False) -> bool:
    """Whether `goal` is reachable from `start` (see `_search`)."""
    return _search(session, start, goal, kinds, skip=skip, alias=alias, replicated_only=replicated_only) is not None


def _other_parent(session: Session, from_id: str, kind: str, *, skip: str | None = None,
                  alias: dict[str, str] | None = None, replicated_only: bool = False) -> Relation | None:
    """An active hierarchical relation of the same family from `from_id` (C6.3: one parent per family)."""
    if kind in MEMBERSHIP:
        return None
    alias = alias or {}
    kinds = sorted(ADDON) if kind in ADDON else [kind]
    ids = [from_id, *[a for a, b in alias.items() if b == from_id]]
    query = select(Relation).where(Relation.from_id.in_(ids), Relation.type.in_(kinds), _active())
    if replicated_only:
        query = query.where(_replicated())
    rows = session.scalars(query.order_by(Relation.created_at, Relation.id))
    return next((r for r in rows if r.id != skip), None)


def _live(session: Session, cid: Any, what: str) -> Canonical:
    if not isinstance(cid, str) or not cid or len(cid) > 40:
        raise AtlasError(f"{what} must be an Atlas id")
    row = session.get(Canonical, follow(session, cid))
    if row is None or row.merged_into is not None:
        raise AtlasError(f"unknown record {cid}", "not_found")
    return row


def _target_fits(target: Canonical | None, kind: str) -> bool:
    """C6.2: membership types point at a series or collection record, hierarchical types never do."""
    if target is None:
        return False
    wanted = MEMBER_TARGET.get(kind)
    return target.media_type == wanted if wanted else target.media_type not in MEMBER_TARGET.values()


def _name(record: Canonical) -> str:
    """A record as people read it in a refusal: its title and its id."""
    title = (record.view or {}).get("title")
    return f"“{title}” ({record.id})" if title else record.id


def check(session: Session, source: Canonical, target: Canonical, kind: str, *,
          replicated_only: bool = False) -> None:
    """Refuse a relation that breaks a C6 rule. Raises AtlasError (`invalid` or `conflict`).

    `replicated_only` (a replicated `add`): judge it against replicated rows only, as its authority did; the local
    rows it then conflicts with are ended by `_supersede_local`."""
    if kind not in TYPES:
        raise AtlasError(f"unknown relation type {kind!r}; use one of {', '.join(TYPES)}")
    if source.id == target.id:
        raise AtlasError("a record cannot be related to itself")
    wanted = MEMBER_TARGET.get(kind)
    if wanted and target.media_type != wanted:
        raise AtlasError(f"{kind} must point at a record of type {wanted!r}; {_name(target)} is a {target.media_type!r}")
    if not wanted and target.media_type in MEMBER_TARGET.values():
        raise AtlasError(f"{kind} cannot point at a {target.media_type}; use "
                         f"{'series_member' if target.media_type == 'series' else 'collection_member'}")
    other = _other_parent(session, source.id, kind, replicated_only=replicated_only)
    if other is not None and not (other.to_id == target.id and other.type == kind):
        raise AtlasError(f"{_name(source)} already has a {other.type} parent ({other.to_id}); a record has at most one "
                         f"parent of the {family(kind)} kind. Remove that relation first.", "conflict")
    if reaches(session, target.id, source.id, group(kind), replicated_only=replicated_only):
        raise AtlasError(f"{_name(target)} already leads back to {_name(source)}; this relation would make a cycle",
                         "conflict")
    counted = select(func.count()).select_from(Relation).where(Relation.from_id == source.id, _active())
    if replicated_only:
        counted = counted.where(_replicated())
    count = session.scalar(counted) or 0
    if count >= MAX_OUTGOING:
        raise AtlasError(f"{_name(source)} already has {MAX_OUTGOING} relations; remove one first", "conflict")


# ---- writes ---------------------------------------------------------------------------------------------

#: Local rows one replicated change may end before it gives up (fail closed).
MAX_SUPERSEDED = 1000


def _supersede(session: Session, row: Relation, touched: set[str]) -> None:
    row.ended_at, row.end_reason = now(), "superseded"
    touched.update({row.from_id, row.to_id})
    session.flush()


def _supersede_local(session: Session, row: Relation, alias: dict[str, str] | None = None) -> set[str]:
    """`row` is a replicated relation that just became active (applied from the feed, or moved by a merge). End
    the *local* rows that now break a rule with it: a local parent of the same family and a local edge on a cycle
    through it. Replicated rows always win, so local state never changes replicated state. Returns the records
    whose relations changed."""
    alias = alias or {}
    touched: set[str] = set()
    if row.type not in MEMBERSHIP:
        kinds = sorted(ADDON) if row.type in ADDON else [row.type]
        ids = [row.from_id, *[a for a, b in alias.items() if b == row.from_id]]
        for other in session.scalars(select(Relation).where(Relation.from_id.in_(ids), Relation.type.in_(kinds),
                                                            _active(), _local(), Relation.id != row.id)
                                     .order_by(Relation.id)).all():
            _supersede(session, other, touched)
    for _ in range(MAX_SUPERSEDED):
        path = _search(session, row.to_id, row.from_id, group(row.type), alias=alias)
        if path is None:
            return touched
        local = [r for r in (session.get(Relation, rid) for rid in path) if r is not None and not is_replicated(r)]
        if not local:
            return touched  # a cycle of replicated rows only: its authority's check prevents it; leave it be
        _supersede(session, local[0], touched)
    raise AtlasError("too many local relations conflict with this change", "conflict")


def _supersede_local_or_leave(session: Session, row: Relation, alias: dict[str, str] | None = None) -> set[str]:
    """`_supersede_local`, except that a local graph too large to untangle is left as it is (logged): local rows
    must never make a replicated change fail."""
    try:
        with session.begin_nested():
            return _supersede_local(session, row, alias)
    except AtlasError as exc:
        log.warning("relation %s -> %s (%s): local relations left as they are: %s", row.from_id, row.to_id,
                    row.type, exc)
        return set()


def _end_local_duplicate(session: Session, from_id: str, to_id: str, kind: str, keep: str,
                         touched: set[str]) -> None:
    for other in session.scalars(select(Relation).where(Relation.from_id == from_id, Relation.to_id == to_id,
                                                        Relation.type == kind, _active(), _local(),
                                                        Relation.id != keep)).all():
        _supersede(session, other, touched)

def _signed(ctx: Ctx, log: bool, local_only: bool) -> bool:
    return log and not local_only and ctx.settings.role == "authority"


#: C15 §2.4: the only relation type that carries a `position`, and its bounds.
POSITION_TYPES = frozenset({"series_member"})
MAX_POSITION = 16


def clean_position(kind: str, position: Any) -> str | None:
    """A series position ("1", "2.5", "0"): printable, one line, at most 16 characters, only on `series_member`.
    None means no position. Raises AtlasError (a follower quarantines the entry as `invalid`)."""
    if position is None:
        return None
    if kind not in POSITION_TYPES:
        raise AtlasError(f"only a series_member relation has a position, not {kind}")
    if not isinstance(position, str):
        raise AtlasError("position must be text, such as \"1\" or \"2.5\"")
    cleaned = " ".join("".join(c if c.isprintable() or c == " " else " " for c in position).split())
    if not 0 < len(cleaned) <= MAX_POSITION:
        raise AtlasError(f"position must be 1-{MAX_POSITION} printable characters")
    return cleaned


def _body(op: str, src: str, dst: str, kind: str, source: str | None = None,
          position: str | None = None) -> dict[str, Any]:
    """A `relation` feed body. `position` is a member only when there is one, so every other body is signed
    exactly as before (C15 §2.4; `feed_schema.MEMBER_SINCE` leaves it out below the unified version)."""
    body: dict[str, Any] = {"op": op, "from": src, "to": dst, "type": kind}
    if source is not None:
        body["source"] = source
    if position is not None:
        body["position"] = position
    return body


def add(session: Session, ctx: Ctx, from_id: str, to_id: str, kind: str, *, source: str = "curator",
        actor: str = "", local_only: bool = False, log: bool = True,
        origin: tuple[str, int] | None = None, position: str | None = None) -> tuple[Relation, bool]:
    """Add a relation. Returns (row, created): an identical active relation is returned unchanged (idempotent).

    `log=False` is a feed apply (`origin` = upstream node and seq): it is stored as redistributable and never
    re-signed. On an authority a curator relation is signed when `emit_protocol` ≥ 1.4 and kept local below
    (C1.6). `local_only` keeps it off the feed on any role.

    `position` (C15 §2.4, `series_member` only): an active relation of the same pair with another position is
    ended (`repositioned`) and a new row added in the same transaction; an authority signs that as `end` + `add`
    (no new op), so a follower's history shows both."""
    write_lock(session)
    if kind not in TYPES:
        raise AtlasError(f"unknown relation type {kind!r}; use one of {', '.join(TYPES)}")
    if not isinstance(source, str) or not 0 < len(source) <= 128:
        raise AtlasError("source must be 1-128 characters")
    position = clean_position(kind, position)
    src, dst = _live(session, from_id, "from"), _live(session, to_id, "to")
    existing = active_between(session, src.id, dst.id, kind)
    if existing is not None and existing.position != position and origin is None:
        end(session, ctx, src.id, dst.id, kind, actor=actor, log=log, reason="repositioned")
        existing = active_between(session, src.id, dst.id, kind)
    elif existing is not None and existing.position != position and is_replicated(existing):
        existing.position = position  # an upstream that re-adds the pair without an `end` moves the position
        session.flush()
    if existing is not None and origin is not None and not is_replicated(existing):
        # an identical local row: the upstream's entry takes it over, so its `end` and a rescan find it
        existing.redistributable, existing.origin_node, existing.origin_seq = True, origin[0], origin[1]
        existing.source, existing.position = source, position
        session.flush()
        bump_version(session, src.id, "feed")
        bump_version(session, dst.id, "feed")
        return existing, True
    if existing is not None:
        if _pending_publication(existing) and _signed(ctx, log, local_only) and not gated(ctx):
            publish_pending(session, ctx, existing)
        return existing, False
    check(session, src, dst, kind, replicated_only=origin is not None)
    signed = _signed(ctx, log, local_only)
    row = Relation(from_id=src.id, to_id=dst.id, type=kind, source=source, created_by=actor[:128],
                   redistributable=signed or origin is not None, position=position,
                   origin_node=origin[0] if origin else None, origin_seq=origin[1] if origin else None)
    session.add(row)
    session.flush()
    touched = _supersede_local_or_leave(session, row) if origin is not None else set()
    if signed and not gated(ctx):
        row.feed_seq = feed.emit(session, ctx, "relation", _body("add", src.id, dst.id, kind, source, position))
    cause = "feed" if row.feed_seq or origin is not None else "local"
    for cid in sorted(touched | {src.id, dst.id}):
        bump_version(session, cid, cause)
    return row, True


def _pending_publication(row: Relation) -> bool:
    """A curator relation an authority stored while it was gated (emit below 1.4): meant for the feed
    (`redistributable`), never signed (no `feed_seq`), not from an upstream."""
    return row.redistributable and row.feed_seq is None and row.origin_node is None and row.ended_at is None


def publish_pending(session: Session, ctx: Ctx, row: Relation) -> None:
    """Sign a relation stored while this authority was gated, now that it may (Phase 4 note O1: re-PUTting it used
    to answer `published: true` and sign nothing). It is judged against the published rows first, as every
    follower will judge the signed entry; a conflict raises (409) and nothing is signed."""
    src, dst = _live(session, row.from_id, "from"), _live(session, row.to_id, "to")
    check(session, src, dst, row.type, replicated_only=True)
    row.feed_seq = feed.emit(session, ctx, "relation", _body("add", src.id, dst.id, row.type, row.source,
                                                             row.position))
    bump_version(session, src.id, "feed")
    bump_version(session, dst.id, "feed")


def pending_count(session: Session, cid: str) -> int:
    """This record's own outgoing relations stored but not signed (an authority below emit 1.4)."""
    return session.scalar(select(func.count()).select_from(Relation).where(
        Relation.from_id == cid, _active(), Relation.redistributable.is_(True), Relation.feed_seq.is_(None),
        Relation.origin_node.is_(None))) or 0


def end(session: Session, ctx: Ctx, from_id: str, to_id: str, kind: str, *, actor: str = "", log: bool = True,
        reason: str = "removed", managed_ok: bool = False) -> Relation | None:
    """End the active relation `from -> to` of `kind`. Returns it, or None when there is none (a no-op).

    A relation that came from the upstream feed can only be ended by the feed (`log=False`) unless `managed_ok`.
    On an authority below emit 1.4, a relation that was already published is not ended locally (its followers
    would keep it): the curator raises the protocol first (409 `emit_protocol`)."""
    write_lock(session)
    if kind not in TYPES:
        raise AtlasError(f"unknown relation type {kind!r}")
    if not isinstance(from_id, str) or not isinstance(to_id, str):
        raise AtlasError("from and to must be Atlas ids")
    src, dst = follow(session, from_id), follow(session, to_id)
    row = active_between(session, src, dst, kind)
    if row is None or (not log and row.origin_node is None):
        return None  # a feed `end` only ends a row that came from the feed, never a local one
    if log and row.origin_node is not None and not managed_ok:
        raise AtlasError("this relation is managed by the upstream authority", "conflict")
    # only a relation the feed holds is ended in it: one stored while gated and never signed ends here only
    signed = log and ctx.settings.role == "authority" and row.feed_seq is not None and row.origin_node is None
    if signed and gated(ctx):
        raise AtlasError(gate_message(ctx, "removing a published relation"), "emit_protocol")
    row.ended_at, row.end_reason = now(), (reason if log else "feed")
    session.flush()
    cause = "local"
    if signed and not gated(ctx):
        feed.emit(session, ctx, "relation", _body("end", src, dst, kind))
        cause = "feed"
    bump_version(session, src, cause)
    bump_version(session, dst, cause)
    return row


def set_outgoing(session: Session, ctx: Ctx, cid: str, items: list[dict[str, Any]], *, actor: str = "",
                 source: str = "curator", local_only: bool = False) -> dict[str, int]:
    """Make `items` (`[{to, type}]`) the record's own outgoing relations (`PUT /games/{id}/relations`).

    Relations that came from the upstream feed are left alone (and stay in the record). Ends run before adds, so
    replacing a parent with another of the same family works in one call. All or nothing: a refused add raises
    and the caller's transaction rolls back. On an authority that may sign, a listed relation stored while it was
    gated is signed now (`published` counts them)."""
    write_lock(session)
    src = _live(session, cid, "id")
    if len(items) > MAX_OUTGOING:
        raise AtlasError(f"at most {MAX_OUTGOING} relations per record")
    wanted: list[tuple[str, str]] = []
    positions: dict[tuple[str, str], str | None] = {}
    for item in items:
        target = _live(session, item.get("to"), "to").id
        kind = item.get("type")
        if kind not in TYPES:
            raise AtlasError(f"unknown relation type {kind!r}; use one of {', '.join(TYPES)}")
        if (target, kind) not in wanted:
            wanted.append((target, kind))
            positions[(target, kind)] = clean_position(kind, item.get("position"))
    current = session.scalars(select(Relation).where(Relation.from_id == src.id, _active(),
                                                     Relation.origin_node.is_(None))
                              .order_by(Relation.created_at, Relation.id)).all()
    ended = added = published = 0
    for row in current:
        if (row.to_id, row.type) not in wanted:
            end(session, ctx, row.from_id, row.to_id, row.type, actor=actor, reason="replaced")
            ended += 1
    for target, kind in wanted:
        before = active_between(session, src.id, target, kind)
        was_pending = before is not None and _pending_publication(before)
        row, created = add(session, ctx, src.id, target, kind, source=source, actor=actor, local_only=local_only,
                           position=positions[(target, kind)])
        added += int(created)
        published += int(was_pending and row.feed_seq is not None)
    return {"added": added, "ended": ended, "published": published}


def move(session: Session, src: str, dst: str) -> set[str]:
    """On a merge of `src` into `dst`: carry `src`'s active relations to `dst` (C6.3). Returns the other records
    whose relations changed (their versions are bumped by the caller).

    `dst`'s own relations always stay. Each of `src`'s relations is moved unless the result would be a self
    relation (`merge_self`), a duplicate of an active one (`merge_duplicate`), a second parent of the same family
    or a cycle (`merge_conflict`); then it is ended instead.

    Replicated rows go first, in `(type, from_id, to_id)` order, and are judged against replicated rows only; a
    local row they then conflict with is ended (`superseded`). The order and the decisions therefore use only
    state every node of the feed shares (not `created_at`, which on a follower is when it applied the entry, nor
    the row id, which each node mints), so a follower applying the same `merge` reaches the same replicated rows
    without any signed `end`. Local rows go last and are judged against everything."""
    alias = {src: dst}
    rows = session.scalars(select(Relation).where((Relation.from_id == src) | (Relation.to_id == src), _active())
                           .order_by(Relation.type, Relation.from_id, Relation.to_id)).all()
    ordered = [r for r in rows if is_replicated(r)] + [r for r in rows if not is_replicated(r)]
    touched: set[str] = set()
    for row in ordered:
        if row.ended_at is not None:
            continue  # a local row a replicated one superseded
        shared = is_replicated(row)
        new_from = dst if row.from_id == src else row.from_id
        new_to = dst if row.to_id == src else row.to_id
        touched.update({new_from, new_to})
        same = select(Relation.id).where(Relation.from_id == new_from, Relation.to_id == new_to,
                                         Relation.type == row.type, _active(), Relation.id != row.id)
        if shared:
            same = same.where(_replicated())
        reason = None
        if new_from == new_to:
            reason = "merge_self"
        elif session.scalars(same.limit(1)).first() is not None:
            reason = "merge_duplicate"
        elif row.to_id == src and not _target_fits(session.get(Canonical, dst), row.type):
            reason = "merge_conflict"  # e.g. a series merged into a game: its members cannot point at a game
        elif _other_parent(session, new_from, row.type, skip=row.id, alias=alias, replicated_only=shared) is not None:
            reason = "merge_conflict"
        else:
            try:
                if reaches(session, new_to, new_from, group(row.type), skip=row.id, alias=alias,
                           replicated_only=shared):
                    reason = "merge_conflict"
            except AtlasError:
                reason = "merge_conflict"  # too large to verify: fail closed
        if reason:
            row.ended_at, row.end_reason = now(), reason
            session.flush()
            continue
        if shared:
            _end_local_duplicate(session, new_from, new_to, row.type, row.id, touched)
        row.from_id, row.to_id = new_from, new_to
        session.flush()
        if shared:
            touched |= _supersede_local_or_leave(session, row, alias)
    touched.discard(src)
    touched.discard(dst)
    return touched


def _merge_log(session: Session) -> Iterable[tuple[str, str]]:
    """The merges this node made or applied, oldest first: its own signed feed, then each upstream's retained feed
    in seq order."""
    for body in session.scalars(select(ChangeLog.body).where(ChangeLog.kind == "merge").order_by(ChangeLog.seq)
                                .execution_options(yield_per=1000)):
        yield str((body or {}).get("src")), str((body or {}).get("dst"))
    for body in session.scalars(select(ReplicatedChange.body).where(ReplicatedChange.kind == "merge")
                                .order_by(ReplicatedChange.node_id, ReplicatedChange.seq)
                                .execution_options(yield_per=1000)):
        yield str((body or {}).get("src")), str((body or {}).get("dst"))


def _named_merged(session: Session) -> list[str]:
    """Merged records an active relation still names (none, except right after a downgrade window)."""
    merged = select(Canonical.id).where(Canonical.merged_into.is_not(None))
    ids: set[str] = set()
    for f, t in session.execute(select(Relation.from_id, Relation.to_id).where(
            _active(), Relation.from_id.in_(merged) | Relation.to_id.in_(merged))):
        ids.update({f, t})
    if not ids:
        return []
    return sorted(set(session.scalars(merged.where(Canonical.id.in_(sorted(ids))))))


def finish_restore(session: Session) -> dict[str, int] | None:
    """After migration 0032 brought the kept table back (a re-upgrade after a downgrade): run, for each merge made
    while the previous release ran, the same `move` a 1.4 follower ran when it applied that merge, in the same
    order, and bump the versions of the records whose relations changed (ETags, `/changes`). None when there is
    nothing pending. The caller commits.

    Merges come from this node's own feed, then from the upstream feeds it retains, oldest first. A merge with no
    log entry is replayed last, in id order, into the record it now resolves to."""
    if session.get(NodeState, RESTORE_KEY) is None:
        return None
    write_lock(session)
    pending = set(_named_merged(session))
    touched: set[str] = set()
    replayed = 0
    started = False
    names = select(Relation.id).where(_active())
    if pending:
        for src, dst in _merge_log(session):
            started = started or src in pending
            if not started:
                continue
            row = session.get(Canonical, src)
            if row is None or row.merged_into is None or follow(session, src) != follow(session, dst):
                continue  # retained but not applied here
            if session.scalars(names.where((Relation.from_id == src) | (Relation.to_id == src)).limit(1)).first():
                touched |= move(session, src, dst) | {src, dst}
                replayed += 1
    for _ in range(MAX_SUPERSEDED):
        left = _named_merged(session)
        if not left:
            break
        target = follow(session, left[0])
        touched |= move(session, left[0], target) | {left[0], target}
        replayed += 1
    for cid in sorted(touched):
        if session.get(Canonical, cid) is not None:
            bump_version(session, cid, "local")
    session.delete(session.get(NodeState, RESTORE_KEY))
    session.flush()
    return {"merges_replayed": replayed, "records_bumped": len(touched)}


def apply_feed(session: Session, ctx: Ctx, node_id: str, seq: int, body: dict[str, Any]) -> str | None:
    """Apply a classified `relation` entry from an upstream. Returns "noop" when the state already matches.
    A rule violation (self, cycle, second parent, wrong target type) is the authority's invalid value: raised as
    ValueError so the follower quarantines it (C6.3) without blocking."""
    op, kind = body.get("op"), body.get("type")
    for key in ("from", "to"):
        value = body.get(key)
        if not isinstance(value, str) or not value.startswith("atlas:") or len(value) > 40:
            raise ValueError(f"{key} is not an Atlas id")
    if op == "add":
        source = body.get("source")
        if not isinstance(source, str) or not 0 < len(source) <= 128:
            raise ValueError("source must be 1-128 characters")
        if session.scalars(select(Relation.id).where(Relation.origin_node == node_id,
                                                     Relation.origin_seq == seq).limit(1)).first() is not None:
            return "noop"  # this very entry was applied before (a historical replay re-runs the range)
        try:
            _row, created = add(session, ctx, body["from"], body["to"], kind, source=source, actor=node_id[:128],
                                log=False, origin=(node_id, seq), position=body.get("position"))
        except AtlasError as exc:
            if exc.code in ("invalid", "conflict"):
                raise ValueError(str(exc)) from exc
            raise  # not_found: quarantined and retried (the record may arrive later)
        return None if created else "noop"
    if op == "end":
        if "position" in body:
            raise ValueError("an end entry has no position")
        return None if end(session, ctx, body["from"], body["to"], kind, log=False) is not None else "noop"
    raise ValueError(f"unknown relation op {op!r}")


def applied_from_feed(session: Session, node_id: str, seq: int, body: dict[str, Any]) -> bool:
    """For `atlas sync replay --rescan`: whether a retained `relation` entry at or below the applied head has its
    effect here. An `add` left a row with that origin; an `end` left no active relation older than it."""
    if body.get("op") == "add":
        return session.scalars(select(Relation.id).where(Relation.origin_node == node_id,
                                                         Relation.origin_seq == seq).limit(1)).first() is not None
    if body.get("op") == "end":
        src, dst = follow(session, str(body.get("from"))), follow(session, str(body.get("to")))
        row = active_between(session, src, dst, str(body.get("type")))
        return row is None or row.origin_seq is None or row.origin_node != node_id or row.origin_seq > seq
    return True


# ---- views ----------------------------------------------------------------------------------------------

def _brief(session: Session, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for part in _chunks(sorted(set(ids))):
        for cid, view, media_type in session.execute(select(Canonical.id, Canonical.view, Canonical.media_type)
                                                     .where(Canonical.id.in_(part))):
            out[cid] = {"title": (view or {}).get("title"), "media_type": media_type}
    return out


def _sorted_rows(session: Session, where: Any, kinds: tuple[str, ...], other: Any, limit: int) -> tuple[list, int]:
    total = session.scalar(select(func.count()).select_from(Relation).where(where, Relation.type.in_(kinds),
                                                                             _active())) or 0
    rows: list[tuple[str, str, str | None]] = []
    for kind in kinds:  # type order first (the vocabulary order), then the other record's id: deterministic
        if len(rows) >= limit:
            break
        rows += list(session.execute(select(other, Relation.type, Relation.position)
                                     .where(where, Relation.type == kind, _active())
                                     .order_by(other).limit(limit - len(rows))))
    return rows, total


def of(session: Session, cid: str, *, limit: int = LIST_LIMIT) -> dict[str, Any]:
    """The `relations` block of a record (C6.3), each list ordered by type then id and bounded at `limit`:
    `parents` (this -> parent, hierarchical), `children` (child -> this), `members_of` (this -> series or
    collection) and `members` (member -> this series or collection). `counts` has the full sizes and `truncated`
    says whether any list was cut."""
    limit = max(1, min(limit, LIST_LIMIT))
    lists = {"parents": (Relation.from_id == cid, HIERARCHICAL, Relation.to_id),
             "children": (Relation.to_id == cid, HIERARCHICAL, Relation.from_id),
             "members_of": (Relation.from_id == cid, MEMBERSHIP, Relation.to_id),
             "members": (Relation.to_id == cid, MEMBERSHIP, Relation.from_id)}
    found: dict[str, list[tuple[str, str, str | None]]] = {}
    counts: dict[str, int] = {}
    for name, (where, kinds, other) in lists.items():
        found[name], counts[name] = _sorted_rows(session, where, kinds, other, limit)
    brief = _brief(session, (rid for rows in found.values() for rid, _, _ in rows))
    out: dict[str, Any] = {name: [{"id": rid, "type": kind, **({"position": pos} if pos is not None else {}),
                                   **brief.get(rid, {"title": None, "media_type": None})}
                                  for rid, kind, pos in rows] for name, rows in found.items()}
    out["counts"] = counts
    out["truncated"] = any(counts[name] > len(found[name]) for name in found)
    return out


def _type_rank() -> Any:
    """The vocabulary order of `Relation.type` in SQL (the order `type_order` gives in Python)."""
    return case(_ORDER, value=Relation.type, else_=len(_ORDER))


def expand(session: Session, cid: str, depth: int = 1, *, max_nodes: int = MAX_EXPAND_NODES) -> dict[str, Any]:
    """The relation graph around a record, breadth first in both directions up to `depth` hops (at most
    `MAX_EXPAND_DEPTH`) and `max_nodes` records. Deterministic order. Used by the API and edition navigation.

    Bounded in the database as well as in the output: rows are read in pages, in the order they are used, and
    reading stops at the first row that would add a record beyond `max_nodes`. `edges` are the active relations
    among the returned records (one query on at most `max_nodes` ids)."""
    depth = max(1, min(depth, MAX_EXPAND_DEPTH))
    max_nodes = max(1, min(max_nodes, MAX_EXPAND_NODES))
    order = [cid]
    seen = {cid}
    frontier = [cid]
    truncated = False
    reached = 0
    for level in range(1, depth + 1):
        nxt: list[str] = []
        for part in _chunks(sorted(frontier)):
            query = (select(Relation.from_id, Relation.to_id).where(
                (Relation.from_id.in_(part)) | (Relation.to_id.in_(part)), _active())
                .order_by(_type_rank(), Relation.from_id, Relation.to_id))
            offset = 0
            while not truncated:
                page = session.execute(query.offset(offset).limit(_PAGE)).all()
                for a, b in page:
                    for other in (a, b):
                        if other in seen:
                            continue
                        if len(order) >= max_nodes:
                            truncated = True
                            break
                        seen.add(other)
                        order.append(other)
                        nxt.append(other)
                    if truncated:
                        break
                if len(page) < _PAGE:
                    break
                offset += _PAGE
            if truncated:
                break
        if nxt:
            reached = level
        frontier = nxt
        if not frontier or truncated:
            break
    if not truncated and frontier:
        # records one hop beyond the requested depth: the graph goes on, so say so
        inside = sorted(seen)
        for part in _chunks(sorted(frontier)):
            probe = select(Relation.id).where(_active(), (Relation.from_id.in_(part) & Relation.to_id.not_in(inside))
                                              | (Relation.to_id.in_(part) & Relation.from_id.not_in(inside)))
            if session.scalars(probe.limit(1)).first() is not None:
                truncated = True
                break
    edges = []
    ids = sorted(seen)
    for part in _chunks(ids):
        edges += session.execute(select(Relation.from_id, Relation.to_id, Relation.type, Relation.position).where(
            _active(), Relation.from_id.in_(part), Relation.to_id.in_(ids))).all()
    brief = _brief(session, order)
    return {"id": cid, "depth": depth, "reached_depth": reached, "truncated": truncated,
            "nodes": [{"id": n, **brief.get(n, {"title": None, "media_type": None})} for n in order],
            "edges": [{"from": a, "to": b, "type": k, **({"position": pos} if pos is not None else {})}
                      for a, b, k, pos in sorted(edges, key=lambda e: (type_order(e[2]), e[0], e[1]))]}


def history(session: Session, cid: str, limit: int = 20) -> list[Relation]:
    """Ended relations of a record, newest first (shown to curators)."""
    return list(session.scalars(select(Relation).where((Relation.from_id == cid) | (Relation.to_id == cid),
                                                       Relation.ended_at.is_not(None))
                                .order_by(Relation.ended_at.desc(), Relation.id).limit(limit)))
