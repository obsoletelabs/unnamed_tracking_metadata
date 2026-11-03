"""Merge records and unmerge (CONTRACT-C11 §11.10).

Every merge writes a `merge_record` and its snapshot items in the same transaction as the merge itself, so it can
be undone: what moved from `src` to `dst` (chains, crosswalk keys, aliases, locks, artwork, achievements, custom
values, list items, follows, prices, relations, review items) and every row the merge deleted because the survivor
already had one (`*_dropped`, with the full row). Each moved row that a person can edit carries `data_hash`; an
unmerge leaves a row edited on `dst` after the merge where it is and reports it (`edited_after_merge`).

Unmerge is LIFO along a chain of merges (a merge whose survivor was merged again is blocked until that later
merge is undone). In this release merge records, unmerge and rejections are local: an authority cannot undo a merge
it already signed into its feed until the federated `unmerge` kind ships (protocol 1.6, C11-5).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, func, select, update
from sqlalchemy.orm import Session

from . import client_changes, feed_schema, matching
from .models import (
    Achievement,
    Artwork,
    ArtworkVote,
    AuditEntry,
    Canonical,
    Crosswalk,
    CustomValue,
    Follow,
    ListItem,
    Lock,
    MatchRejection,
    MergeRecord,
    MergeRecordItem,
    Observation,
    PriceObservation,
    RecordLive,
    Relation,
    ReviewItem,
    now,
)
from .signing import canonical_json

MERGE_MAX_ITEMS = 100_000
CAUSES = ("curator", "review", "bridge", "sync", "proposal")
#: How long `GET /games/{dst}` lists an unmerged record (§11.10.7).
UNMERGED_DAYS = 90
#: Items that are always recorded, even when a `sync` merge is over MERGE_MAX_ITEMS (`truncated`).
IDENTITY_KINDS = ("chain", "xwalk", "alias", "rejection_ended")
UNMERGE_PROTOCOL = feed_schema.KIND_SINCE["unmerge"]


class MergeError(Exception):
    def __init__(self, message: str, code: str, **detail: Any) -> None:
        super().__init__(message)
        self.code, self.detail = code, detail


# ---- row snapshots ---------------------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def row_dict(row: Any) -> dict[str, Any]:
    return {c.key: _plain(getattr(row, c.key)) for c in row.__mapper__.column_attrs}


def row_from(model: Any, data: dict[str, Any]) -> Any:
    values = {}
    for attr in model.__mapper__.column_attrs:
        if attr.key not in data:
            continue
        value = data[attr.key]
        column = attr.columns[0]
        if value is not None and isinstance(column.type, DateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        values[attr.key] = value
    return model(**values)


def data_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _lock_data(lk: Lock) -> dict[str, Any]:
    return {"value": lk.value, "cleared": lk.cleared, "local_only": lk.local_only}


def _art_data(a: Artwork) -> dict[str, Any]:
    return {"kind": a.kind, "url": a.url, "priority": a.priority, "preferred": a.preferred, "source": a.source}


_ACH_FIELDS = ("source", "external_id", "name", "description", "hidden", "icon_url", "locked_icon_url", "kind", "tier",
               "points", "global_percent", "position", "key")


def _ach_data(a: Achievement) -> dict[str, Any]:
    return {k: getattr(a, k) for k in _ACH_FIELDS}


def _custom_data(cv: CustomValue) -> dict[str, Any]:
    return {"key": cv.key, "source": cv.source, "value": cv.value}


def _list_data(item: ListItem) -> dict[str, Any]:
    return {"list_id": item.list_id, "note": item.note}


def _relation_state(r: Relation) -> dict[str, Any]:
    return {"from_id": r.from_id, "to_id": r.to_id, "ended_at": _plain(r.ended_at), "end_reason": r.end_reason}


class Pre:
    """What `src` (and the parts of `dst` that decide duplicates) held before the move."""

    def __init__(self, session: Session, src: str, dst: str) -> None:
        q = session.scalars
        self.chains = sorted({(o.source, o.source_scope, o.external_id) for o in q(
            select(Observation).where(Observation.canonical_id == src))})
        self.xwalk = sorted((c.namespace, c.external_id) for c in q(select(Crosswalk).where(Crosswalk.canonical_id == src)))
        own = session.get(RecordLive, src)
        self.aliases = [(src, own.live_id if own else None)]
        self.aliases += [(r.id, r.live_id) for r in q(select(RecordLive).where(RecordLive.live_id == src))]
        self.locks = {lk.field: row_dict(lk) for lk in q(select(Lock).where(Lock.canonical_id == src))}
        self.dst_locks = set(q(select(Lock.field).where(Lock.canonical_id == dst)))
        self.art = {a.id: row_dict(a) for a in q(select(Artwork).where(Artwork.canonical_id == src))}
        self.votes = [row_dict(v) for v in q(select(ArtworkVote).where(ArtworkVote.artwork_id.in_(sorted(self.art))))] \
            if self.art else []
        self.ach = {a.id: row_dict(a) for a in q(select(Achievement).where(Achievement.canonical_id == src))}
        self.custom = {(cv.key, cv.source): row_dict(cv)
                       for cv in q(select(CustomValue).where(CustomValue.canonical_id == src))}
        self.dst_custom = {(k, src_) for k, src_ in session.execute(select(CustomValue.key, CustomValue.source)
                                                                    .where(CustomValue.canonical_id == dst))}
        self.lists = {i.list_id: row_dict(i) for i in q(select(ListItem).where(ListItem.canonical_id == src))}
        self.dst_lists = set(q(select(ListItem.list_id).where(ListItem.canonical_id == dst)))
        self.follows = {f.user_id: row_dict(f) for f in q(select(Follow).where(Follow.target_type == "game",
                                                                            Follow.target_id == src))}
        self.dst_follows = set(q(select(Follow.user_id).where(Follow.target_type == "game", Follow.target_id == dst)))
        self.prices = list(q(select(PriceObservation.id).where(PriceObservation.canonical_id == src)))
        self.relations = {r.id: _relation_state(r) for r in q(select(Relation).where(
            (Relation.from_id == src) | (Relation.to_id == src), Relation.ended_at.is_(None)))}
        self.reviews = {r.id: {"status": r.status, "canonical_id": r.canonical_id, "pair_key": r.pair_key,
                               "candidates": r.candidates, "resolved_at": _plain(r.resolved_at)}
                        for r in q(select(ReviewItem).where(ReviewItem.status == "open"))
                        if {r.canonical_id, *(c for c, _s in r.candidates or [])} & {src, dst}}

    def size(self) -> int:
        return (len(self.chains) + len(self.xwalk) + len(self.aliases) + len(self.locks) + len(self.art)
                + len(self.votes) + len(self.ach) + len(self.custom) + len(self.lists) + len(self.follows)
                + len(self.prices) + len(self.relations) + len(self.reviews))


def begin(session: Session, src: Canonical, dst: Canonical, *, cause: str, actor: str, decision_id: str | None,
          review_id: str | None, origin: tuple[str, int] | None) -> tuple[MergeRecord, Pre]:
    """Snapshot `src` before anything moves. A local merge over MERGE_MAX_ITEMS is refused (`merge_too_large`); a
    `sync` merge (the upstream's decision) never is: it is recorded `truncated`, identity items only."""
    if cause not in CAUSES:
        raise ValueError(f"unknown merge cause {cause!r}")
    pre = Pre(session, src.id, dst.id)
    size = pre.size()
    truncated = size > MERGE_MAX_ITEMS
    if truncated and cause != "sync":
        raise MergeError(f"this merge would snapshot {size} items (at most {MERGE_MAX_ITEMS}); nothing changed",
                         "merge_too_large", items=size)
    rec = MergeRecord(src=src.id, dst=dst.id, cause=cause, actor=actor[:128], decision_id=decision_id,
                      review_id=review_id, origin_node=origin[0] if origin else None,
                      origin_seq=origin[1] if origin else None, src_view=dict(src.view or {}),
                      src_explain=dict(src.explain or {}), item_count=0, truncated=truncated)
    session.add(rec)
    session.flush()
    return rec, pre


def finish(session: Session, rec: MergeRecord, pre: Pre, ended: list[str]) -> None:
    """After the moves: write the snapshot items and point every alias of `src` at `dst` (`record_live`)."""
    src, dst = rec.src, rec.dst
    items: list[tuple[str, str, Any, str | None]] = []
    items += [("chain", "\n".join(c), None, None) for c in pre.chains]
    items += [("xwalk", f"{ns}\n{ext}", None, None) for ns, ext in pre.xwalk]
    items += [("alias", cid, prev, None) for cid, prev in pre.aliases]
    items += [("rejection_ended", rid, None, None) for rid in ended]
    if not rec.truncated:
        for name, data in sorted(pre.locks.items()):
            if name in pre.dst_locks:
                items.append(("lock_dropped", name, data, None))
            else:
                lk = session.get(Lock, (dst, name))
                items.append(("lock", name, None, data_hash(_lock_data(lk)) if lk else None))
        dropped_art = set()
        for aid, data in sorted(pre.art.items()):
            a = session.get(Artwork, aid)
            if a is None:
                dropped_art.add(aid)
                items.append(("art_dropped", aid, data, None))
            else:
                items.append(("art", aid, {"preferred": data["preferred"]}, data_hash(_art_data(a))))
        items += [("vote_dropped", f"{v['artwork_id']}\n{v['user_id']}", v, None)
                  for v in pre.votes if v["artwork_id"] in dropped_art]
        for aid, data in sorted(pre.ach.items()):
            a = session.get(Achievement, aid)
            if a is None:
                items.append(("ach_dropped", aid, data, None))
            else:
                items.append(("ach", aid, {"key": data["key"]}, data_hash(_ach_data(a))))
        for (key, source), data in sorted(pre.custom.items()):
            if (key, source) in pre.dst_custom:
                items.append(("custom_dropped", f"{key}\n{source}", data, None))
            else:
                cv = session.get(CustomValue, (dst, key, source))
                items.append(("custom", f"{key}\n{source}", None, data_hash(_custom_data(cv)) if cv else None))
        for list_id, data in sorted(pre.lists.items()):
            if list_id in pre.dst_lists:
                items.append(("list_item_dropped", list_id, data, None))
            else:
                item = session.get(ListItem, (list_id, dst))
                items.append(("list_item", list_id, None, data_hash(_list_data(item)) if item else None))
        for user_id, data in sorted(pre.follows.items()):
            items.append(("follow_dropped" if user_id in pre.dst_follows else "follow", user_id,
                          data if user_id in pre.dst_follows else None, None))
        items += [("price", pid, None, None) for pid in sorted(pre.prices)]
        for rid, before in sorted(pre.relations.items()):
            r = session.get(Relation, rid)
            if r is not None and _relation_state(r) != before:
                items.append(("relation", rid, {"before": before, "after": _relation_state(r)}, None))
        for rid, before in sorted(pre.reviews.items()):
            r = session.get(ReviewItem, rid)
            if r is not None and (r.status, r.canonical_id, r.pair_key) != (before["status"], before["canonical_id"],
                                                                           before["pair_key"]):
                items.append(("review", rid, before, None))
    for n, (kind, ref, data, hashed) in enumerate(items):
        session.add(MergeRecordItem(merge_id=rec.id, n=n, kind=kind, ref=ref[:900], data=data, data_hash=hashed))
    rec.item_count = len(items)
    # §11.6: every id that redirected to src now redirects to dst.
    session.execute(update(RecordLive).where(RecordLive.live_id == src).values(live_id=dst))
    own = session.get(RecordLive, src)
    if own is None:
        session.add(RecordLive(id=src, live_id=dst))
    else:
        own.live_id = dst
    session.flush()


# ---- reading ---------------------------------------------------------------------------------------------

def get(session: Session, merge_id: str) -> MergeRecord:
    rec = session.get(MergeRecord, merge_id)
    if rec is None:
        raise MergeError("unknown merge", "not_found")
    return rec


def items_of(session: Session, rec: MergeRecord) -> list[MergeRecordItem]:
    return list(session.scalars(select(MergeRecordItem).where(MergeRecordItem.merge_id == rec.id)
                                .order_by(MergeRecordItem.n)))


def unmerged_recently(session: Session, dst: str) -> list[dict[str, Any]]:
    """`GET /games/{dst}.unmerged` (§11.10.7): records split off this one in the last UNMERGED_DAYS days."""
    since = now() - timedelta(days=UNMERGED_DAYS)
    rows = session.scalars(select(MergeRecord).where(MergeRecord.dst == dst, MergeRecord.reverted_at.is_not(None),
                                                     MergeRecord.reverted_at >= since)
                           .order_by(MergeRecord.reverted_at.desc()).limit(20)).all()
    return [{"id": r.src, "at": _plain(r.reverted_at)} for r in rows]


def detail(session: Session, rec: MergeRecord) -> dict[str, Any]:
    from .provenance import merge_out

    counts: dict[str, int] = {k: n for k, n in session.execute(select(MergeRecordItem.kind, func.count()).where(
        MergeRecordItem.merge_id == rec.id).group_by(MergeRecordItem.kind))}
    return {**merge_out(rec), "decision_id": rec.decision_id, "review_id": rec.review_id,
            "origin_node": rec.origin_node, "late_chains": rec.late_chains, "items": dict(sorted(counts.items())),
            "src_view": rec.src_view}


# ---- unmerge (§11.10.2 - §11.10.3) -----------------------------------------------------------------------

def _blocked_by(session: Session, rec: MergeRecord) -> list[str]:
    return list(session.scalars(select(MergeRecord.id).where(MergeRecord.src == rec.dst,
                                                             MergeRecord.reverted_at.is_(None))))


def check(session: Session, ctx: Any, rec: MergeRecord, *, if_match: str | None = None) -> None:
    """§11.10.2 preconditions, raising a typed MergeError."""
    from .sync import _is_upstream_origin

    if rec.reverted_at is not None:
        raise MergeError("this merge was already undone", "already_reverted")
    src, dst = session.get(Canonical, rec.src), session.get(Canonical, rec.dst)
    if src is None or dst is None or src.merged_into != rec.dst or dst.merged_into is not None:
        blocked = _blocked_by(session, rec)
        raise MergeError("undo the later merges first (merges are undone last in, first out)", "unmerge_blocked",
                         blocked_by=blocked)
    if rec.cause == "sync" or _is_upstream_origin(session, rec.src) or _is_upstream_origin(session, rec.dst):
        raise MergeError("the upstream authority owns this merge; only its unmerge can split it", "upstream_owned")
    if if_match is not None and if_match.strip() not in (f'"{dst.id}-{dst.version}"', f"{dst.id}-{dst.version}"):
        raise MergeError("the surviving record changed since you looked at it", "stale_version",
                         version=dst.version)
    if rec.feed_seq is not None and ctx.settings.role == "authority":
        # The merge is in this node's signed feed: undoing it locally would leave every mirror merged.
        raise MergeError(str(feed_schema.EmitGateError("undoing a published merge (feed kind 'unmerge')",
                                                       UNMERGE_PROTOCOL, feed_schema.emit_protocol(ctx.settings))),
                         "emit_protocol")


def legacy_check(session: Session, cid: str) -> None:
    """`legacy_merge`: the record was merged before merge records existed (migration 0034)."""
    row = session.get(Canonical, cid)
    if row is None or row.merged_into is None:
        raise MergeError("unknown merge", "not_found")
    live = session.scalars(select(MergeRecord.id).where(MergeRecord.src == cid, MergeRecord.reverted_at.is_(None))).first()
    if live is None:
        raise MergeError("this merge predates merge records; detach its observations instead", "legacy_merge")
    raise MergeError(f"use the merge id {live}", "not_found", merge_id=live)


def _chain_key(ref: str) -> tuple[str, str, str]:
    source, scope, ext = (ref.split("\n") + ["", "", ""])[:3]
    return source, scope, ext


def unmerge(session: Session, ctx: Any, merge_id: str, *, actor: str, dry_run: bool = False,
            remember_distinct: bool = True, if_match: str | None = None) -> dict[str, Any]:
    """§11.10.3, in one transaction under the writer lock. `dry_run` returns the preview and changes nothing."""
    from . import service

    service.write_lock(session)
    rec = get(session, merge_id)
    check(session, ctx, rec, if_match=if_match)
    items = items_of(session, rec)
    src_id, dst_id = rec.src, rec.dst
    moves = {k: 0 for k in ("chains", "observations", "crosswalk", "locks", "artwork", "achievements", "custom",
                            "list_items", "follows", "prices", "relations")}
    restores = {k: 0 for k in ("locks", "artwork", "votes", "achievements", "custom", "list_items", "follows")}
    conflicts: list[dict[str, str]] = []
    listed_chains = {_chain_key(i.ref) for i in items if i.kind == "chain"}
    listed_keys = {tuple(i.ref.split("\n", 1)) for i in items if i.kind == "xwalk"}

    def edited(kind: str, ref: str) -> None:
        conflicts.append({"kind": kind, "ref": ref, "reason": "edited_after_merge"})

    if dry_run:
        for i in items:
            if i.kind == "chain":
                source, scope, ext = _chain_key(i.ref)
                n = session.scalar(select(func.count()).select_from(Observation).where(
                    Observation.source == source, Observation.source_scope == scope, Observation.external_id == ext,
                    Observation.canonical_id == dst_id)) or 0
                moves["chains"] += int(n > 0)
                moves["observations"] += n
            elif i.kind == "xwalk":
                ns, ext = i.ref.split("\n", 1)
                row = session.get(Crosswalk, (ns, ext))
                moves["crosswalk"] += int(row is not None and row.canonical_id == dst_id)
            else:
                _preview_item(session, i, dst_id, moves, restores, edited)
        late = _late_chains(session, src_id, dst_id, listed_chains, listed_keys, apply=False)
        return _result(rec, "preview", moves, restores, conflicts, late, remember_distinct, session)

    src = session.get(Canonical, src_id)
    src.merged_into = None
    session.flush()
    for i in items:  # chains and keys first (§11.10.3 step 3 order)
        if i.kind == "chain":
            source, scope, ext = _chain_key(i.ref)
            n = session.execute(update(Observation).where(
                Observation.source == source, Observation.source_scope == scope, Observation.external_id == ext,
                Observation.canonical_id == dst_id).values(canonical_id=src_id)).rowcount or 0
            moves["chains"] += int(n > 0)
            moves["observations"] += n
        elif i.kind == "xwalk":
            ns, ext = i.ref.split("\n", 1)
            row = session.get(Crosswalk, (ns, ext))
            if row is not None and row.canonical_id == dst_id:
                row.canonical_id = src_id
                moves["crosswalk"] += 1
    session.flush()
    for i in items:
        if i.kind == "alias":
            row = session.get(RecordLive, i.ref)
            if i.data is None:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(RecordLive(id=i.ref, live_id=i.data))
            else:
                row.live_id = i.data
        elif i.kind == "rejection_ended":
            rej = session.get(MatchRejection, i.ref)
            if rej is not None and rej.ended_at is not None and matching.active_rejection(session, rej.a, rej.b) is None:
                rej.ended_at, rej.end_reason = None, None
        else:
            _restore_item(session, i, src_id, dst_id, moves, restores, edited)
    session.flush()
    late = _late_chains(session, src_id, dst_id, listed_chains, listed_keys, apply=True)
    session.flush()
    review_id = None
    if conflicts or late["review"]:
        concern = [*({"kind": c["kind"], "ref": c["ref"]} for c in conflicts),
                   *({"kind": "chain", "ref": "\n".join(c)} for c in late["review"])][:200]
        item = ReviewItem(kind="unmerge_check", canonical_id=dst_id, candidates=[[src_id, 1.0]], confidence=1.0,
                          reason="unmerge", evidence=[{"signal": "items", "value": concern, "effect": 0}],
                          pair_key=matching.pair_key("unmerge_check", [src_id, dst_id, rec.id]),
                          priority=matching.REVIEW_PRIORITY["unmerge_check"])
        session.add(item)
        session.flush()
        review_id = item.id
    with client_changes.cause(session, "unmerge"):  # both ids reach clients: the restored record and the survivor
        service.rebuild(session, ctx, src_id, cause="unmerge")
        service.rebuild(session, ctx, dst_id, cause="unmerge")
    if remember_distinct:
        matching.add_rejection(session, "record", src_id, dst_id, reason="unmerge", actor=actor, local_only=True)
    rec.reverted_at, rec.reverted_by = now(), actor[:128]
    rec.late_chains = {"moved": ["\n".join(c) for c in late["moved"]][:1000],
                       "review": ["\n".join(c) for c in late["review"]][:1000], "truncated": False}
    session.add(AuditEntry(actor_id=None, action="match_unmerge", target=rec.id,
                           detail={"actor": actor[:128], "src": src_id, "dst": dst_id, "conflicts": len(conflicts)}))
    matching.reevaluate_reviews(session, {src_id, dst_id})
    out = _result(rec, "unmerged", moves, restores, conflicts, late, remember_distinct, session)
    out["review_id"] = review_id
    return out


def _result(rec: MergeRecord, status: str, moves: dict[str, int], restores: dict[str, int],
            conflicts: list[dict[str, str]], late: dict[str, list], remember: bool, session: Session) -> dict[str, Any]:
    return {"merge_id": rec.id, "src": rec.src, "dst": rec.dst, "status": status, "cause": rec.cause,
            "merged_at": _plain(rec.created_at), "moves": moves, "restores": restores,
            "late_chains": {"to_src": len(late["moved"]), "stay": len(late["stay"]), "review": len(late["review"])},
            "conflicts": conflicts, "truncated": rec.truncated, "blocked_by": _blocked_by(session, rec),
            "remember_distinct": remember}


def _late_chains(session: Session, src: str, dst: str, listed: set[tuple[str, str, str]],
                 listed_keys: set[tuple[str, ...]], *, apply: bool) -> dict[str, list]:
    """§11.10.3 step 5: chains first seen on `dst` after the merge stay there, except a chain whose keys now all
    point at `src` (it moves) or at both (it goes to review). In a dry run, the keys that will move back are the
    snapshot's crosswalk keys."""
    from .normalize import norm_namespace
    from .service import keys_of

    current = session.scalars(select(Observation).where(Observation.canonical_id == dst,
                                                        Observation.is_current.is_(True))
                              .order_by(Observation.source, Observation.source_scope, Observation.external_id)).all()
    out: dict[str, list] = {"moved": [], "stay": [], "review": []}
    for o in current:
        chain = (o.source, o.source_scope, o.external_id)
        if chain in listed:
            continue
        owners = set()
        own = (norm_namespace(o.source), o.external_id)
        own_row = None
        for key in keys_of(o):
            row = session.get(Crosswalk, key)
            if row is None:
                continue
            if key == own and key not in listed_keys:
                own_row = row  # registered by this chain itself after the merge: it follows the chain
                continue
            owner = row.canonical_id
            if not apply and owner == dst and key in listed_keys:
                owner = src
            owners.add(owner)
        if owners == {src}:
            out["moved"].append(chain)
            if apply:
                session.execute(update(Observation).where(
                    Observation.source == chain[0], Observation.source_scope == chain[1],
                    Observation.external_id == chain[2], Observation.canonical_id == dst).values(canonical_id=src))
                if own_row is not None and own_row.canonical_id == dst:
                    own_row.canonical_id = src
        elif src in owners and dst in owners:
            out["review"].append(chain)
        else:
            out["stay"].append(chain)
    return out


def _preview_item(session: Session, i: MergeRecordItem, dst: str, moves: dict[str, int], restores: dict[str, int],
                  edited: Any) -> None:
    kind, ref = i.kind, i.ref
    if kind == "lock":
        lk = session.get(Lock, (dst, ref))
        if lk is not None:
            if i.data_hash and data_hash(_lock_data(lk)) != i.data_hash:
                edited("lock", ref)
            else:
                moves["locks"] += 1
    elif kind == "lock_dropped":
        restores["locks"] += 1
    elif kind == "art":
        a = session.get(Artwork, ref)
        if a is not None and a.canonical_id == dst:
            if i.data_hash and data_hash(_art_data(a)) != i.data_hash:
                edited("art", ref)
            else:
                moves["artwork"] += 1
    elif kind == "art_dropped":
        restores["artwork"] += 1
    elif kind == "vote_dropped":
        restores["votes"] += 1
    elif kind == "ach":
        a = session.get(Achievement, ref)
        if a is not None and a.canonical_id == dst:
            if i.data_hash and data_hash(_ach_data(a)) != i.data_hash:
                edited("ach", ref)
            else:
                moves["achievements"] += 1
    elif kind == "ach_dropped":
        restores["achievements"] += 1
    elif kind == "custom":
        key, source = ref.split("\n", 1)
        cv = session.get(CustomValue, (dst, key, source))
        if cv is not None:
            if i.data_hash and data_hash(_custom_data(cv)) != i.data_hash:
                edited("custom", ref)
            else:
                moves["custom"] += 1
    elif kind == "custom_dropped":
        restores["custom"] += 1
    elif kind == "list_item":
        item = session.get(ListItem, (ref, dst))
        if item is not None:
            if i.data_hash and data_hash(_list_data(item)) != i.data_hash:
                edited("list_item", ref)
            else:
                moves["list_items"] += 1
    elif kind == "list_item_dropped":
        restores["list_items"] += 1
    elif kind == "follow":
        moves["follows"] += int(session.get(Follow, (ref, "game", dst)) is not None)
    elif kind == "follow_dropped":
        restores["follows"] += 1
    elif kind == "price":
        p = session.get(PriceObservation, ref)
        moves["prices"] += int(p is not None and p.canonical_id == dst)
    elif kind == "relation":
        r = session.get(Relation, ref)
        moves["relations"] += int(r is not None and _relation_state(r) == i.data["after"])


def _restore_item(session: Session, i: MergeRecordItem, src: str, dst: str, moves: dict[str, int],
                  restores: dict[str, int], edited: Any) -> None:
    kind, ref = i.kind, i.ref
    if kind == "lock":
        lk = session.get(Lock, (dst, ref))
        if lk is None:
            return
        if i.data_hash and data_hash(_lock_data(lk)) != i.data_hash:
            edited("lock", ref)
            return
        lk.canonical_id = src
        moves["locks"] += 1
    elif kind == "lock_dropped":
        if session.get(Lock, (src, ref)) is None:
            session.add(row_from(Lock, {**i.data, "canonical_id": src}))
            restores["locks"] += 1
    elif kind == "art":
        a = session.get(Artwork, ref)
        if a is None or a.canonical_id != dst:
            return
        if i.data_hash and data_hash(_art_data(a)) != i.data_hash:
            edited("art", ref)
            return
        a.canonical_id, a.preferred = src, bool((i.data or {}).get("preferred"))
        moves["artwork"] += 1
    elif kind == "art_dropped":
        if session.get(Artwork, ref) is None:
            session.add(row_from(Artwork, {**i.data, "canonical_id": src}))
            restores["artwork"] += 1
            session.flush()
    elif kind == "vote_dropped":
        art_id, user_id = ref.split("\n", 1)
        if session.get(Artwork, art_id) is not None and session.get(ArtworkVote, (art_id, user_id)) is None:
            from .models import User

            if session.get(User, user_id) is not None:  # the account may have been deleted since
                session.add(row_from(ArtworkVote, i.data))
                restores["votes"] += 1
    elif kind == "ach":
        a = session.get(Achievement, ref)
        if a is None or a.canonical_id != dst:
            return
        if i.data_hash and data_hash(_ach_data(a)) != i.data_hash:
            edited("ach", ref)
            return
        a.canonical_id = src
        key = (i.data or {}).get("key")
        if key and key != a.key and session.scalars(select(Achievement.id).where(
                Achievement.canonical_id == src, Achievement.key == key)).first() is None:
            a.key = key
        moves["achievements"] += 1
    elif kind == "ach_dropped":
        if session.get(Achievement, ref) is None and session.scalars(select(Achievement.id).where(
                Achievement.canonical_id == src,
                ((Achievement.source == i.data["source"]) & (Achievement.norm_name == i.data["norm_name"]))
                | (Achievement.key == i.data["key"]))).first() is None:
            session.add(row_from(Achievement, {**i.data, "canonical_id": src}))
            restores["achievements"] += 1
    elif kind == "custom":
        key, source = ref.split("\n", 1)
        cv = session.get(CustomValue, (dst, key, source))
        if cv is None:
            return
        if i.data_hash and data_hash(_custom_data(cv)) != i.data_hash:
            edited("custom", ref)
            return
        cv.canonical_id = src
        moves["custom"] += 1
    elif kind == "custom_dropped":
        key, source = ref.split("\n", 1)
        if session.get(CustomValue, (src, key, source)) is None:
            session.add(row_from(CustomValue, {**i.data, "canonical_id": src}))
            restores["custom"] += 1
    elif kind == "list_item":
        item = session.get(ListItem, (ref, dst))
        if item is None:
            return
        if i.data_hash and data_hash(_list_data(item)) != i.data_hash:
            edited("list_item", ref)
            return
        item.canonical_id = src
        moves["list_items"] += 1
    elif kind == "list_item_dropped":
        from .models import UserList

        if session.get(UserList, ref) is not None and session.get(ListItem, (ref, src)) is None:
            session.add(row_from(ListItem, {**i.data, "canonical_id": src}))
            restores["list_items"] += 1
    elif kind == "follow":
        f = session.get(Follow, (ref, "game", dst))
        if f is not None:
            f.target_id = src
            moves["follows"] += 1
    elif kind == "follow_dropped":
        from .models import User

        if session.get(User, ref) is not None and session.get(Follow, (ref, "game", src)) is None:
            session.add(row_from(Follow, {**i.data, "target_id": src}))
            restores["follows"] += 1
    elif kind == "price":
        p = session.get(PriceObservation, ref)
        if p is not None and p.canonical_id == dst:
            p.canonical_id = src
            moves["prices"] += 1
    elif kind == "relation":
        r = session.get(Relation, ref)
        if r is not None and _relation_state(r) == i.data["after"]:
            before = i.data["before"]
            r.from_id, r.to_id, r.end_reason = before["from_id"], before["to_id"], before["end_reason"]
            r.ended_at = datetime.fromisoformat(before["ended_at"]) if before["ended_at"] else None
            moves["relations"] += 1
    elif kind == "review":
        r = session.get(ReviewItem, ref)
        if r is None or r.status not in ("merged", "superseded"):
            return
        before = i.data
        if before.get("pair_key") and session.scalars(select(ReviewItem.id).where(
                ReviewItem.pair_key == before["pair_key"], ReviewItem.status == "open")).first() is not None:
            return
        r.status, r.canonical_id, r.pair_key = before["status"], before["canonical_id"], before.get("pair_key")
        r.candidates, r.resolved_at, r.decided_by = before.get("candidates") or r.candidates, None, None
    session.flush()
