"""Per-field provenance (CONTRACT-C11 §11.7, §11.11.1): trust classes with per-origin ceilings, the trust digest,
the observations fed to the merge, the provenance API's bounded answers, and the explain v2 backfill job.

Read scope sees the winners only (what `explain` always exposed). Candidate values, decisions and per-observation
details need the curate scope; host and plugin observations are only ever counted per source, so the API never
discloses which host owns or plays a game, or when.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import matching
from .merge import (
    CLASS_RANK,
    CLEARED,
    COMMUNITY_SOURCES,
    CURATOR_SOURCES,
    EXPLAIN_VERSION,
    Obs,
    effective_class,
    source_class,
)
from .models import (
    Canonical,
    FieldConflict,
    Lock,
    MatchDecision,
    MatchRejection,
    MergeRecord,
    NodeState,
    Observation,
    RecordLive,
    now,
)
from .normalize import norm_namespace
from .settings import Settings
from .signing import canonical_json

MAX_GROUPS = 20  # candidate value groups per field
MAX_GROUP_SOURCES = 5
MAX_VALUE = 300  # characters of a candidate value
MAX_EMBEDDED = 20  # observations and decisions embedded in a provenance answer
MAX_PAGE = 100
HOST_KINDS = ("host", "plugin")


def _epoch(dt: datetime) -> int:
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


# ---- trust classes and the digest ------------------------------------------------------------------------

def authority_origins(session: Session, settings: Settings) -> frozenset[str]:
    """§11.7.1 "the configured authority": every upstream whose signed feed this node applies (merge and lock
    entries included), with its key pinned by the operator. That is each node in ATLAS_TRUSTED_KEYS (the
    environment upstream) and each node configured under Admin → Peers, enabled or not (data it already
    replicated keeps its class). Any other origin is not an authority."""
    from .models import Peer

    return frozenset(settings.trusted_keys) | frozenset(session.scalars(select(Peer.node_id)))


def origin_ceiling(settings: Settings, origin_node: str | None, authorities: frozenset[str] | None = None) -> str:
    """§11.7.1: local ingest is not capped (its reserved sources already needed the curate scope). A replicated
    observation is capped by `sync_origin_class_cap[node]`, else `curator` for a configured upstream authority
    (`authority_origins`: ATLAS_TRUSTED_KEYS and Admin → Peers) and `provider` for every other origin (H48)."""
    if origin_node is None:
        return "curator"
    cap = settings.sync_origin_class_cap.get(origin_node)
    if cap:
        return cap
    known = frozenset(settings.trusted_keys) if authorities is None else authorities
    return "curator" if origin_node in known else "provider"


def obs_of(settings: Settings, o: Observation, authorities: frozenset[str] | None = None) -> Obs:
    eff = effective_class(o.source, origin_ceiling(settings, o.origin_node, authorities))
    stamp = o.source_updated_at or _epoch(o.observed_at)
    return Obs(id=o.id, source=o.source, source_kind=o.source_kind, external_id=o.external_id, payload=o.payload,
               trust=settings.trust(norm_namespace(o.source)), stamp=stamp, cls=CLASS_RANK[eff],
               source_cls=CLASS_RANK[source_class(o.source)], origin=o.origin_node)


def trust_config(settings: Settings, authorities: frozenset[str] | None = None) -> dict[str, Any]:
    return {"classes": {"curator": sorted(CURATOR_SOURCES), "community": sorted(COMMUNITY_SOURCES),
                        "provider": "*"},
            "source_trust": dict(sorted(settings.source_trust.items())), "default_trust": settings.default_trust,
            "field_source_preference": {k: list(v) for k, v in sorted(settings.field_source_preference.items())},
            "sync_origin_class_cap": dict(sorted(settings.sync_origin_class_cap.items())),
            "authority_origins": sorted(settings.trusted_keys if authorities is None else authorities)}


def trust_digest(settings: Settings, authorities: frozenset[str] | None = None) -> str:
    """`_meta.trust_digest` (§11.7.2): two nodes with different trust configuration can see why views differ."""
    return "sha256:" + hashlib.sha256(canonical_json(trust_config(settings, authorities))).hexdigest()


def meta(settings: Settings, authorities: frozenset[str] | None = None) -> dict[str, Any]:
    return {"explain": EXPLAIN_VERSION, "matcher": matching.MATCHER,
            "trust_digest": trust_digest(settings, authorities)}


def merge_inputs(session: Session, settings: Settings, cid: str,
                 authorities: frozenset[str] | None = None) -> tuple[list[Obs], dict[str, Any]]:
    rows = session.scalars(
        select(Observation).where(Observation.canonical_id == cid, Observation.is_current.is_(True))).all()
    locks = {lk.field: (CLEARED if lk.cleared else lk.value)
             for lk in session.scalars(select(Lock).where(Lock.canonical_id == cid))}
    if authorities is None:
        authorities = authority_origins(session, settings)
    return [obs_of(settings, o, authorities) for o in rows], locks


def index_conflicts(session: Session, cid: str, explain: dict[str, Any]) -> None:
    """Keep `field_conflict` equal to the fields whose explain says `conflict: true` (bounded by the field count)."""
    from .merge import conflict_fields

    wanted = set(conflict_fields(explain))
    have = {r.field: r for r in session.scalars(select(FieldConflict).where(FieldConflict.canonical_id == cid))}
    for name, row in have.items():
        if name not in wanted:
            session.delete(row)
    for name in sorted(wanted - set(have)):
        session.add(FieldConflict(canonical_id=cid, field=name[:48], updated_at=now()))


# ---- the provenance API ----------------------------------------------------------------------------------

def _value_text(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_VALUE]
    if isinstance(value, list):
        return [v[:MAX_VALUE] if isinstance(v, str) else v for v in value[:100]]
    return value


def _field_value(o: Observation, field: str) -> Any:
    from .merge import _value

    return _value(Obs(o.id, o.source, o.source_kind, o.external_id, o.payload, 0, 0), field)


def provenance(session: Session, settings: Settings, row: Canonical, *, curate: bool, values: bool,
               viewer: Any = None) -> dict[str, Any]:
    """`GET /games/{cid}/provenance` (§11.11.1). `values` (curate only) adds candidate values per field, grouped by
    value (at most MAX_GROUPS groups, best first).

    C14 §5.7: for a viewer without scope.detail, `row` is the record as that viewer sees it (its projection), and
    observations they may not see (host/plugin library data, data received from the group) are neither listed nor
    counted."""
    from . import scope

    explain = row.explain or {}
    view = row.view or {}
    obs_rows = session.scalars(select(Observation).where(Observation.canonical_id == row.id,
                                                         Observation.is_current.is_(True))
                               .order_by(Observation.id.desc())).all()
    if viewer is not None and not viewer.detail:
        ceiling = getattr(row, "scope_ceiling", None)
        obs_rows = [o for o in obs_rows if not scope.is_host_obs(o) and not scope.is_group_obs(o, ceiling)]
    by_id = {o.id: o for o in obs_rows}
    locks = {lk.field: lk for lk in session.scalars(select(Lock).where(Lock.canonical_id == row.id))}
    authorities = authority_origins(session, settings) if values else None
    fields: dict[str, Any] = {}
    for name, info in explain.items():
        if name.startswith("_") or not isinstance(info, dict):
            continue
        entry: dict[str, Any] = {"strategy": info.get("strategy"), "reason": info.get("reason")}
        if name in view:
            entry["value"] = _value_text(view[name])
        if info.get("strategy") in ("lock", "cleared") and name in locks:
            lk = locks[name]
            entry["lock"] = {"set_by": lk.set_by, "local_only": lk.local_only, "created_at": _iso(lk.created_at),
                             "cleared": lk.cleared}
        if info.get("observation_id"):
            win = by_id.get(info["observation_id"])
            entry["winner"] = {"observation_id": info["observation_id"], "source": info.get("source"),
                               "class": info.get("class"), "trust": info.get("trust")}
            if win is not None and win.source_kind in HOST_KINDS:
                entry["winner"].pop("observation_id")  # host chains are never named individually
        if "conflict" in info:
            entry["conflict"] = info["conflict"]
        if info.get("strategy") == "union":
            entry["members"] = [{"value": view_value, "sources": info.get("members", {}).get(view_value.casefold(), [])}
                                for view_value in (view.get(name) or [])[:100] if isinstance(view_value, str)]
        if name == "provider_ids" and curate and info.get("conflicts"):
            entry["conflicts"] = info["conflicts"]
        if values and curate and info.get("strategy") == "priority":
            entry.update(_candidate_groups(settings, obs_rows, name, view.get(name), authorities))
        fields[name] = entry

    obs_out, counts = [], {}
    detail = viewer is not None and viewer.detail  # C14 §5.5: the additive `scope` key, scope.detail only
    grouped_node = scope.in_group(session) if detail else False
    for o in obs_rows:
        if o.source_kind in HOST_KINDS:
            counts[(o.source, o.source_kind)] = counts.get((o.source, o.source_kind), 0) + 1
            continue
        item = {"id": o.id, "source": o.source, "source_kind": o.source_kind, "is_current": o.is_current,
                "redistributable": o.redistributable}
        if curate or o.redistributable:
            item["external_id"] = o.external_id
        if curate:
            item.update({"observed_at": _iso(o.observed_at), "last_seen_at": _iso(o.last_seen_at),
                         "origin_node": o.origin_node})
        if detail:
            item["scope"] = scope.of_observation(settings, o, getattr(row, "scope_ceiling", None),
                                                 grouped=grouped_node)
        obs_out.append(item)
    grouped = [{"source": s, "source_kind": k, "count": n, **({"scope": "local"} if detail else {})}
               for (s, k), n in sorted(counts.items())]
    out: dict[str, Any] = {
        "id": row.id, "version": row.version, "matcher": (explain.get("_meta") or {}).get("matcher"),
        "trust_digest": (explain.get("_meta") or {}).get("trust_digest"), "fields": fields,
        "observations": obs_out[:MAX_EMBEDDED] + grouped,
        "observations_next": obs_out[MAX_EMBEDDED - 1]["id"] if len(obs_out) > MAX_EMBEDDED else None,
        "merges": [merge_out(m) for m in merges_of(session, row.id, limit=MAX_EMBEDDED)],
        "distinct": distinct_of(session, row.id, limit=MAX_EMBEDDED),
    }
    if curate:
        decisions = decisions_of(session, row.id, limit=MAX_EMBEDDED + 1)
        out["decisions"] = [matching.decision_out(d, full=False) for d in decisions[:MAX_EMBEDDED]]
        out["decisions_next"] = decisions[MAX_EMBEDDED - 1].id if len(decisions) > MAX_EMBEDDED else None
    return out


def _candidate_groups(settings: Settings, rows: list[Observation], field: str, winner: Any,
                      authorities: frozenset[str] | None = None) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    ranked = sorted((obs_of(settings, o, authorities) for o in rows), key=lambda o: (o.cls, o.trust, o.stamp, o.id),
                    reverse=True)
    raw = {o.id: o for o in rows}
    for ob in ranked:
        o = raw[ob.id]
        value = _field_value(o, field)
        if value in (None, ""):
            continue
        key = canonical_json(value).decode()
        group = groups.get(key)
        if group is None:
            best = {"source": o.source, "class": matching_class(ob.cls), "trust": ob.trust}
            if o.source_kind not in HOST_KINDS:
                best["observation_id"] = o.id
            group = groups[key] = {"value": _value_text(value), "count": 0, "sources": [], "best": best,
                                   "agrees": value == winner}
        group["count"] += 1
        if o.source not in group["sources"] and len(group["sources"]) < MAX_GROUP_SOURCES:
            group["sources"].append(o.source)
    items = list(groups.values())
    out: dict[str, Any] = {"candidates": items[:MAX_GROUPS]}
    if len(items) > MAX_GROUPS:
        out["more"] = len(items) - MAX_GROUPS
    return out


def matching_class(rank: int) -> str:
    from .merge import CLASS_NAME

    return CLASS_NAME.get(rank, "provider")


def observations_page(session: Session, cid: str, *, limit: int, cursor: str | None) -> dict[str, Any]:
    """`/provenance/observations` (curate): every observation of the record (current and history), newest id first,
    host and plugin chains included (curate scope), paged by observation id."""
    limit = max(1, min(limit, MAX_PAGE))
    stmt = select(Observation).where(Observation.canonical_id == cid)
    if cursor:
        stmt = stmt.where(Observation.id < cursor)
    rows = session.scalars(stmt.order_by(Observation.id.desc()).limit(limit + 1)).all()
    items = [{"id": o.id, "source": o.source, "source_kind": o.source_kind, "source_scope": o.source_scope,
              "external_id": o.external_id, "is_current": o.is_current, "redistributable": o.redistributable,
              "observed_at": _iso(o.observed_at), "last_seen_at": _iso(o.last_seen_at),
              "origin_node": o.origin_node, "payload_hash": o.payload_hash} for o in rows[:limit]]
    return {"results": items, "next": rows[limit - 1].id if len(rows) > limit else None}


def decisions_of(session: Session, cid: str, *, limit: int, cursor: str | None = None) -> list[MatchDecision]:
    """Decisions for a record and every record merged into it (through `record_live`), newest first."""
    ids = select(RecordLive.id).where(RecordLive.live_id == cid)
    stmt = select(MatchDecision).where((MatchDecision.canonical_id == cid) | MatchDecision.canonical_id.in_(ids))
    if cursor:
        stmt = stmt.where(MatchDecision.id < cursor)
    return list(session.scalars(stmt.order_by(MatchDecision.id.desc()).limit(max(1, min(limit, MAX_PAGE + 1)))))


def merges_of(session: Session, cid: str, *, limit: int) -> list[MergeRecord]:
    """Merge records whose survivor is the record or one merged into it, newest first."""
    ids = select(RecordLive.id).where(RecordLive.live_id == cid)
    stmt = select(MergeRecord).where((MergeRecord.dst == cid) | MergeRecord.dst.in_(ids) | (MergeRecord.src == cid))
    return list(session.scalars(stmt.order_by(MergeRecord.created_at.desc(), MergeRecord.id.desc())
                                .limit(max(1, min(limit, MAX_PAGE)))))


def merge_out(m: MergeRecord) -> dict[str, Any]:
    return {"merge_id": m.id, "src": m.src, "dst": m.dst, "cause": m.cause, "actor": m.actor,
            "at": _iso(m.created_at), "reverted_at": _iso(m.reverted_at), "reverted_by": m.reverted_by,
            "truncated": m.truncated, "item_count": m.item_count, "signed": m.feed_seq is not None}


def distinct_of(session: Session, cid: str, *, limit: int) -> list[dict[str, Any]]:
    ids = sorted(matching.alias_ids(session, cid))
    rows = session.scalars(select(MatchRejection).where(
        MatchRejection.ended_at.is_(None),
        ((MatchRejection.scope == "record") & (MatchRejection.a.in_(ids) | MatchRejection.b.in_(ids)))
        | ((MatchRejection.scope == "chain") & MatchRejection.b.in_(ids)))
        .order_by(MatchRejection.created_at.desc(), MatchRejection.id).limit(limit)).all()
    out = []
    for r in rows:
        other = r.a if r.scope == "chain" else (r.b if r.a in ids else r.a)
        out.append({"id": r.id, "other": other, "scope": r.scope, "actor": r.actor, "reason": r.reason,
                    "at": _iso(r.created_at), "local_only": r.local_only})
    return out


def conflicts_page(session: Session, field: str, *, limit: int, cursor: str | None) -> dict[str, Any]:
    """`/conflicts?field=` (§11.7.2): read from the `field_conflict` index, never from explain."""
    limit = max(1, min(limit, MAX_PAGE))
    stmt = select(FieldConflict).where(FieldConflict.field == field[:48])
    if cursor:
        stmt = stmt.where(FieldConflict.canonical_id > cursor)
    rows = session.scalars(stmt.order_by(FieldConflict.canonical_id).limit(limit + 1)).all()
    return {"field": field, "results": [{"canonical_id": r.canonical_id, "updated_at": _iso(r.updated_at)}
                                        for r in rows[:limit]],
            "next": rows[limit - 1].canonical_id if len(rows) > limit else None}


# ---- explain v2 backfill (`atlas match explain-backfill`) -------------------------------------------------

CURSOR = "c11_explain_cursor"


def backfill(maker: Any, ctx: Any, *, batch: int = 500, restart: bool = False, limit: int | None = None) -> dict[str, Any]:
    """Rewrite every live record's explain to v2 and fill `field_conflict` (§11.13), resumable through
    `node_state[c11_explain_cursor]`, one transaction per batch under the writer lock. When the recomputed view is
    byte-identical only explain is written and no version is bumped (explain is not client data); when the view
    changed (trust classes can change a winner, H34) the record is rebuilt with cause `local`, so clients see it."""
    from .merge import merge
    from .service import rebuild, write_lock

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
                    session.delete(state)
                session.commit()
                return {"records": done, "changed": changed, "finished": True}
            authorities = authority_origins(session, ctx.settings)
            for cid in ids:
                rec = session.get(Canonical, cid)
                obs, locks = merge_inputs(session, ctx.settings, cid, authorities)
                view, explain = merge(obs, locks, prefs=ctx.settings.field_source_preference,
                                      meta=meta(ctx.settings, authorities))
                if canonical_json(view) == canonical_json(rec.view or {}):
                    if rec.explain != explain:
                        rec.explain = explain
                    index_conflicts(session, cid, explain)
                else:
                    rebuild(session, ctx, cid, cause="local")
                    changed += 1
            last = ids[-1]
            state = session.get(NodeState, CURSOR)
            if state is None:
                session.add(NodeState(key=CURSOR, value=last))
            else:
                state.value = last
            session.commit()
            done += len(ids)
    return {"records": done, "changed": changed, "finished": False, "cursor": last}


def class_warning(settings: Settings) -> list[str]:
    """H34 admin warning: sources the numeric trust ranks above a higher class, which classes now override."""
    out = []
    for source, trust in sorted(settings.source_trust.items()):
        cls = CLASS_RANK[source_class(source)]
        for other, other_trust in sorted(settings.source_trust.items()):
            if CLASS_RANK[source_class(other)] > cls and trust > other_trust:
                out.append(f"{source} ({trust}) outranks {other} ({other_trust}) by trust, but {other} has the "
                           f"higher trust class and now wins")
    return out
