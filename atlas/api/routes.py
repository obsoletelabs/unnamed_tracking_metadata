from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import (
    __version__,
    accounts,
    auth,
    custom,
    facets,
    feed,
    feed_schema,
    importer,
    key_rotation,
    matching,
    media,
    merges,
    money,
    prices,
    provenance,
    readiness,
    relations,
    service,
    sync,
    vocab,
)
from .. import client_changes as client_change_log
from ..contract import ObservationBatchV1, ProviderIdentity, ProviderKey
from ..merge import source_class
from ..models import (
    Artwork,
    Canonical,
    FollowerSeen,
    Lock,
    MatchDecision,
    MatchRejection,
    NodeState,
    Observation,
    ReplicatedChange,
    ReplicatedFeed,
    ReviewItem,
    SyncCursor,
    SyncFailure,
    Token,
)
from ..normalize import norm_namespace
from ..providers import ProviderError, media_policy
from ..resolve import Resolution, classify, conflict_review
from ..service import AtlasError
from ..signing import load_identity, signing_key_at_rest
from .deps import AppState, get_session, require, state

router = APIRouter(prefix="/api/v2")

Sess = Annotated[Session, Depends(get_session, scope="function")]
St = Annotated[AppState, Depends(state)]


def _iso(dt: datetime | None) -> str | None:
    """SQLite drops tzinfo; everything is stored as UTC."""
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def game_out(row: Canonical, redirected_from: str | None = None, session: Session | None = None,
             ctx: Any = None, viewer: Any = None) -> dict[str, Any]:
    out = {
        "id": row.id, "version": row.version, "redirected_from": redirected_from,
        "media_type": row.media_type, "updated_at": _iso(row.updated_at), **row.view,
    }
    if session is not None and ctx is not None:
        out["custom"] = custom.merged(session, ctx, row.id, viewer=viewer)
    return out


def _item_scope(session: Session, st: AppState, a: Artwork, viewer: Any) -> dict[str, Any]:
    """C14 §5.5: the additive `scope` of one image, for callers with scope detail only."""
    from .. import scope

    if viewer is None or not viewer.detail:
        return {}
    return {"scope": scope.of_artwork(st.ctx.settings, a, scope.ceiling_of(session, a.canonical_id),
                                      grouped=scope.in_group(session))}


def _viewer(session: Session, st: AppState, principal: Any) -> Any:
    """The visibility class of an API caller (CONTRACT-C14 §5.7, `scope.viewer_for_principal`)."""
    from .. import scope

    return scope.viewer_for_principal(session, st.ctx.settings, principal)


def _visible(session: Session, st: AppState, principal: Any, cid: str) -> tuple[Any, str | None, Any]:
    """`service.get_canonical` under the visibility rule; a refused record is the same 404 as an unknown one."""
    from .. import scope

    viewer = _viewer(session, st, principal)
    row, redirected = scope.get_visible(session, viewer, cid)
    return row, redirected, viewer


def scope_out(session: Session, st: AppState, row: Any, viewer: Any) -> dict[str, Any]:
    """The additive `scope` key of a record (C14 §5.5): only for viewers with scope.detail. For anyone else the key
    is absent (not null), so the answer cannot be told apart from a node without C14."""
    from .. import scope

    if viewer is None or not viewer.detail:
        return {}
    info = scope.record_summary(session, st.ctx.settings, scope.unwrap(row))
    return {"scope": {"ceiling": info["ceiling"], "widest": info["widest"], "counts": info["counts"]}}


def _guard_upstream(session: Session, st: AppState, cid: str) -> None:
    """Mirrors/locals may not rewrite records the upstream owns."""
    if st.ctx.settings.role == "authority":
        return
    owned = session.scalars(
        select(Observation.id).where(Observation.canonical_id == cid, Observation.origin_node.is_not(None)).limit(1)
    ).first()
    if owned:
        raise AtlasError("this record is managed by the upstream authority", "conflict")


# ---- health -------------------------------------------------------------------------------------------

@router.get("/health", tags=["ops"])
def health(st: St) -> dict[str, str]:
    # Liveness stays "ok" (the process serves); "media" tells operators artwork cannot be stored.
    return {"status": "ok", "version": __version__,
            "media": "ok" if media.storage_problem(st.ctx.settings) is None else "unwritable"}


@router.get("/ready", tags=["ops"])
def ready(st: St, response: Response) -> dict[str, str]:
    """Minimal on purpose (public). 503 `not_ready` when the database does not answer or migrations are pending;
    200 `degraded` when only artwork storage is unwritable; 200 `ready` otherwise. Unlike `/health` (liveness)
    this touches the database, so an orchestrator holds traffic, not restarts the process, while it fails.

    Deliberately not the `Sess` dependency: that one refreshes admin settings with a database query, which would
    raise (a 500) before the check ran during a real outage."""
    with st.maker() as session:
        result = readiness.check(session, st.ctx.settings)
    response.headers["Cache-Control"] = "no-store"
    if not result.serving:
        response.status_code = 503
    return {"status": result.status}


@router.get("/status", tags=["admin"])
def status(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]) -> dict[str, Any]:
    cursors = {c.upstream: c.last_seq for c in session.scalars(select(SyncCursor))}
    ready = readiness.check(session, st.ctx.settings)
    return {"role": st.ctx.settings.role, "feed_head": feed.head_seq(session),
            "upstream_cursors": cursors, "sync_failures": sync.open_failures(session),
            # "encrypted" (enc:v1, ATLAS_SECRET_KEY) or "plaintext" (legacy; every backup holds the key) (C9.1)
            "signing_key_at_rest": signing_key_at_rest(session),
            # named heads per upstream (observed, downloaded, verified, applied, parked, quarantined)
            "upstreams": sync.status(session, st.ctx)["upstreams"],
            # the detail the public /ready deliberately leaves out
            "checks": {"database": "ok" if ready.database else "unavailable", "schema": ready.schema,
                       "media": "ok" if ready.media else "unwritable", "readiness": ready.status,
                       "provider_failures": st.providers.failure_counts()}}


@router.get("/sync/failures", tags=["admin"])
def sync_failures(session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))],
                  limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    """Mirrored feed entries that could not be applied and still need attention."""
    rows = session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None))
                           .order_by(SyncFailure.node_id, SyncFailure.seq).limit(limit)).all()
    def retry_at(f: SyncFailure) -> str | None:
        when = sync.next_retry_at(f)
        return when.isoformat() if when else None

    return {"open": sync.open_failures(session),
            "parked": sync.open_failures(session, feed_schema.PARK),
            "quarantined": sync.open_failures(session, feed_schema.QUARANTINE),
            "waiting": sync.open_failures(session, sync.WAIT),  # C13 §5.2 (additive)
            "failures": [{"node_id": f.node_id, "seq": f.seq, "kind": f.kind, "error": f.error,
                          "disposition": f.disposition, "reason": f.reason, "detail": f.detail,
                          "attempts": f.attempts, "first_failed_at": f.first_failed_at.isoformat(),
                          "last_attempt_at": f.last_attempt_at.isoformat(),
                          "waiting_for": f.waiting_for, "next_try_at": retry_at(f)} for f in rows]}


@router.get("/sync/status", tags=["admin"])
def sync_status(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]) -> dict[str, Any]:
    """Federation compatibility and replication heads of every upstream feed (C1.7)."""
    s = st.ctx.settings
    return {"own_protocol": feed_schema.PROTOCOL, "min_follower_protocol": s.min_follower_protocol,
            "emit_protocol": feed_schema.emit_protocol(s), **sync.status(session, st.ctx),
            "followers": [{"token_id": f.token_id, "protocol": f.protocol, "last_seen_at": _iso(f.last_seen_at)}
                          for f in session.scalars(select(FollowerSeen).order_by(FollowerSeen.last_seen_at.desc())
                                                   .limit(200))]}


@router.get("/admin/peers/probe", tags=["admin"])
def probe_peer(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))],
               url: Annotated[str, Query(max_length=300)],
               peer_token: Annotated[str | None, Header(alias="Atlas-Peer-Token", max_length=200)] = None
               ) -> dict[str, Any]:
    """Look a node up before following it (C13 §7.1), for scripts, tests and the CLI: what the admin form's
    choose-feed screen shows, as JSON (`node`, `token`, `records`, `feeds[]` with `trust`, `warnings` and
    `recommended`, `default_feed`). The token for the probed node goes in the `Atlas-Peer-Token` header (never the
    URL). Nothing is pinned: `probe_token` is null, because adding a peer stays an admin-form action bound to a
    session. 400 for a bad address, 502 when the node cannot be verified, 504 when it did not answer in time.

    Private and loopback addresses are accepted on purpose (peers often share a host or network; the admin form
    probes the same way): the call is admin-only and makes two read requests. The 502 detail echoes the error the
    node gave, to the admin who asked."""
    from .. import peer_probe

    try:
        target = peer_probe.clean_url(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    try:
        result = peer_probe.probe(session, st.http, target, (peer_token or "").strip() or None)
    except peer_probe.ProbeError as exc:
        raise HTTPException(504 if exc.unreachable else 502, f"could not verify that node: {exc}") from None
    return {"url": target, **peer_probe.public(result)}


@router.post("/sync/failures/retry", tags=["admin"])
def retry_sync_failures(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))],
                        limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
    """Retry failed mirrored entries now, whatever the automatic attempt count."""
    service.write_lock(session)
    result = sync.retry_failures(session, st.ctx, limit=limit, due_only=False)
    session.commit()
    return {**result, "open": sync.open_failures(session)}


@router.get("/site", tags=["ops"])
def site(st: St, session: Sess) -> dict[str, Any]:
    """Public, non-sensitive description of this node: what a client or mirror needs to present it."""
    s = st.ctx.settings
    return {"name": s.app_name, "tagline": s.tagline, "role": s.role, "kind": s.kind(), "operator": s.node_operator, "node_id": st.ctx.identity.node_id,  # noqa: E501
            "theme": {"default": s.default_theme, "accent": s.accent_color, "accent_light": s.accent_color_light,
                      "logo": f"/media/{s.logo_hash}" if s.logo_hash else None},
            "nav_links": s.nav_links, "media_types": [t.key for t in custom.types(session, enabled_only=True)],
            "registration": s.registration, "email_verification": s.verification_mode(),
            "contract": 1}


# ---- reading ------------------------------------------------------------------------------------------

class ResolveItem(BaseModel):
    namespace: ProviderKey | None = Field(default=None, description="Namespace of `external_id`: a provider or "
                                                                    "source key, or `atlas` for an Atlas id.")
    external_id: str | None = Field(default=None, max_length=256)
    media_type: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=500, description="Title evidence. Never used when an id "
                                                                         "matches, and ignored when it is a "
                                                                         "placeholder such as `Steam app 12345`.")
    platform: str | None = Field(default=None, max_length=50)
    release_date: str | None = Field(default=None, max_length=10, description="`YYYY`, `YYYY-MM` or `YYYY-MM-DD`; "
                                                                               "only the year is used.")
    developer: str | None = Field(default=None, max_length=200)
    provider_ids: dict[ProviderKey, ProviderIdentity] | None = Field(
        default=None, max_length=32, description="Every id the client holds, `{namespace: id}`. `atlas` is an "
                                                 "Atlas id and is followed through merges.")


class ResolveRequest(BaseModel):
    items: list[ResolveItem] = Field(min_length=1, max_length=5000)
    include_view: bool = True


class ResolveCandidate(BaseModel):
    id: str
    score: float


class ResolveMatchedKey(BaseModel):
    namespace: str
    external_id: str
    canonical_id: str = Field(description="The live record this key points at, after merge redirects.")


class ResolveConflict(BaseModel):
    canonical_ids: list[str] = Field(description="Every record the client's ids point at.")
    own: str | None = Field(description="The record the item's own `namespace`/`external_id` points at: a hint for "
                                        "a curator, never something to assign.")
    review_id: str | None = Field(description="The open `id_conflict` review covering these records, if any.")


class ResolveResult(BaseModel):
    canonical_id: str | None = Field(description="Set only when the evidence allows assigning it (see `reason`).")
    method: Literal["crosswalk", "exact", "fuzzy", "none"] = Field(
        description="Pre-C4 field, kept for old clients. `none` whenever `canonical_id` is null, conflicts included.")
    confidence: float
    candidates: list[ResolveCandidate]
    assigned: bool = Field(description="`canonical_id` is not null.")
    reason: Literal["id", "redirect", "crosswalk", "conflict", "type_mismatch", "exact", "fuzzy", "ambiguous",
                    "none"] = Field(description="Why. Clients auto-assign only `id`, `redirect`, `crosswalk` and "
                                                "`exact`; never `conflict`, `type_mismatch` or `ambiguous`.")
    tier: Literal["id", "strong", "weak", "none"] = Field(
        description="Evidence tier: `id` (identity keys only, never the title), `strong` (title evidence that may "
                    "assign), `weak` (title candidates that must not be assigned), `none`.")
    redirected_from: str | None = Field(description="The Atlas id the client sent when it was merged into "
                                                    "`canonical_id` (`reason: redirect`).")
    matched_keys: list[ResolveMatchedKey] = Field(description="The client's ids that Atlas knows, and where each "
                                                              "points.")
    conflict: ResolveConflict | None = Field(description="Set only for `reason: conflict`.")
    game: dict[str, Any] | None = Field(default=None, description="The record (as `GET /games/{id}`) when "
                                                                  "`include_view` and `canonical_id` is set.")


class ResolveResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"results": [
        {"canonical_id": None, "method": "none", "confidence": 0.9,
         "candidates": [{"id": "atlas:0123456789abcdefghijklmnop", "score": 1.0},
                        {"id": "atlas:1123456789abcdefghijklmnop", "score": 1.0}],
         "assigned": False, "reason": "conflict", "tier": "id", "redirected_from": None,
         "matched_keys": [{"namespace": "steam", "external_id": "620",
                           "canonical_id": "atlas:0123456789abcdefghijklmnop"},
                          {"namespace": "igdb", "external_id": "72",
                           "canonical_id": "atlas:1123456789abcdefghijklmnop"}],
         "conflict": {"canonical_ids": ["atlas:0123456789abcdefghijklmnop", "atlas:1123456789abcdefghijklmnop"],
                      "own": "atlas:0123456789abcdefghijklmnop", "review_id": None},
         "game": None},
    ]}]})
    results: list[ResolveResult]


def _resolution_out(r: Resolution, session: Session, st: AppState, include_view: bool,
                    viewer: Any = None) -> dict[str, Any]:
    from .. import scope

    named = ([r.canonical_id] if r.canonical_id else []) + list(r.conflict or []) + [c for c, _s in r.candidates]
    for cid in named:
        row = session.get(Canonical, cid)
        if row is not None and scope.visible(session, viewer, row) is None:
            r = Resolution()  # C14 §5.7: a record the caller may not see resolves like no match at all
            break
    entry: dict[str, Any] = {
        "canonical_id": r.canonical_id, "method": r.method, "confidence": r.confidence,
        "candidates": [{"id": c, "score": s} for c, s in r.candidates], "assigned": r.assigned,
        "reason": r.reason, "tier": r.tier, "redirected_from": r.redirected_from,
        "matched_keys": [{"namespace": ns, "external_id": ext, "canonical_id": cid}
                         for ns, ext, cid in r.matched_keys],
        "conflict": {"canonical_ids": r.conflict, "own": r.own, "review_id": conflict_review(session, r.conflict)}
        if r.reason == "conflict" else None,
    }
    if include_view:
        row = session.get(Canonical, r.canonical_id) if r.canonical_id else None
        row = scope.visible(session, viewer, row) if row is not None else None
        entry["game"] = game_out(row, None, session, st.ctx, viewer=viewer) if row else None
    return entry


@router.post("/resolve", tags=["read"], response_model=ResolveResponse, response_model_exclude_unset=True,
             summary="Resolve client ids and titles to canonical Atlas ids (C4)")
def resolve(body: ResolveRequest, st: St, session: Sess,
            principal: Annotated[auth.Principal, Depends(require("read"))]):
    """Read-only. Identity evidence wins over titles; conflicting identity evidence returns `canonical_id: null`
    with `reason: conflict` (a behaviour change, H2: older Atlas returned the item's own id with `crosswalk`)."""
    if len(body.items) > st.ctx.settings.max_resolve:
        raise HTTPException(413, f"at most {st.ctx.settings.max_resolve} items per request")
    out = []
    viewer = _viewer(session, st, principal)
    for it in body.items:
        r = classify(
            session, st.ctx.settings, namespace=it.namespace or "", external_id=it.external_id or "",
            provider_ids=it.provider_ids, title=it.title, release_date=it.release_date,
            platform=it.platform, developer=it.developer, media_type=it.media_type,
        ) if (it.external_id or it.title or it.provider_ids) else Resolution()
        out.append(_resolution_out(r, session, st, body.include_view, viewer))
    return {"results": out}


@router.get("/games", tags=["read"])
def list_games(session: Sess, st: St, principal: Annotated[auth.Principal, Depends(require("read"))],
               q: Annotated[str, Query(max_length=200, description="Title search. Required unless a release "
                                       "filter is given.")] = "",
               limit: Annotated[int, Query(ge=1, le=100)] = 25,
               type: Annotated[str | None, Query(max_length=32)] = None,
               released_from: Annotated[str | None, Query(max_length=10, description=(
                   "`YYYY`, `YYYY-MM` or `YYYY-MM-DD`. Only records whose first release lies entirely on or "
                   "after the start of this period."))] = None,
               released_to: Annotated[str | None, Query(max_length=10, description=(
                   "`YYYY`, `YYYY-MM` or `YYYY-MM-DD`. Only records whose first release lies entirely on or "
                   "before the end of this period. A year-only release matches a filter on its year, never "
                   "one on a single month of it."))] = None,
               sort: Annotated[Literal["relevance", "title", "release", "-release"], Query(description=(
                   "`release` orders by the start of the first release, year before month before day, unknown "
                   "last; `-release` newest first. `relevance` ranks exact and prefix title matches first "
                   "*within each page* of the title order, so pages with `offset` follow the title order; use "
                   "`title` or `release` for a stable order across pages."))] = "relevance",
               offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
               scope: Annotated[str | None, Query(max_length=8, description=(
                   "C14: only records whose ceiling is `group`, `local` or `private`, or `public` (no ceiling and "
                   "at least one public item). Only for callers with scope detail; ignored otherwise."))] = None,
               has: Annotated[str | None, Query(max_length=8, description=(
                   "C14: only records with at least one `group` or `local` item. Only for callers with scope "
                   "detail; ignored otherwise."))] = None):
    # Only a missing q needs a filter: a blank q (`q=%20`) lists recent records, as it always did.
    if q == "" and not released_from and not released_to:
        raise HTTPException(422, "give q (a title search) or a release filter")
    viewer = _viewer(session, st, principal)
    rows = service.search(session, q, limit, offset, media_type=type, released_from=released_from,
                          released_to=released_to, sort=sort, viewer=viewer, has=has, scope_filter=scope)
    return {"results": [game_out(r, None, session, st.ctx, viewer=viewer) for r in rows]}


@router.get("/games/{cid}", tags=["read"])
def get_game(cid: str, response: Response, session: Sess, st: St,
             principal: Annotated[auth.Principal, Depends(require("read"))]):
    """One record. `relations` (C6.3) lists its active relations: `parents` (this record is a DLC, port,
    remake... of them), `children` (theirs to this one), `members_of` (the series and collections it is in) and
    `members` (when this record is a series or collection). Each list is ordered by type, then id, and holds
    at most 200 items; `counts` has the full sizes. `editions` (when known) names the record's editions.

    C14 (additive): `scope {ceiling, widest, counts}` for callers with scope detail only; for anyone else the key
    is absent."""
    row, redirected, viewer = _visible(session, st, principal, cid)
    response.headers["ETag"] = f'"{row.id}-{row.version}"'
    return {**game_out(row, redirected, session, st.ctx, viewer=viewer),
            "relations": relations.of(session, row.id, viewer=viewer, settings=st.ctx.settings),
            "unmerged": merges.unmerged_recently(session, row.id), **scope_out(session, st, row, viewer)}


class RelationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(max_length=40, description="The parent, base work, series or collection (an Atlas id; merge "
                                                "redirects are followed).")
    type: Literal[vocab.RELATION_TYPES] = Field(description="C6.2 relation type.")  # type: ignore[valid-type]


class RelationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relations: list[RelationItem] = Field(max_length=relations.MAX_OUTGOING, description=(
        "The record's own outgoing relations after the call: missing ones are ended, new ones added."))


@router.get("/games/{cid}/relations", tags=["read"])
def get_relations(cid: str, session: Sess, st: St, principal: Annotated[auth.Principal, Depends(require("read"))],
                  depth: Annotated[int, Query(ge=1, le=relations.MAX_EXPAND_DEPTH, description=(
                      "Hops to follow in both directions."))] = 1,
                  limit: Annotated[int, Query(ge=1, le=relations.MAX_EXPAND_NODES, description=(
                      "Most records returned."))] = relations.MAX_EXPAND_NODES):
    """The relation graph around a record (C6.3), breadth first up to `depth` hops and `limit` records, for
    navigation between a game, its DLC, ports, remakes, series and collections. `truncated` says that more
    exists. Deterministic order."""
    row, redirected, viewer = _visible(session, st, principal, cid)
    return {**relations.expand(session, row.id, depth, max_nodes=limit, viewer=viewer, settings=st.ctx.settings),
            "redirected_from": redirected, "editions": row.view.get("editions", [])}


@router.put("/games/{cid}/relations", tags=["curate"])
def put_relations(cid: str, body: RelationsBody, st: St, session: Sess,
                  p: Annotated[auth.Principal, Depends(require("curate"))]):
    """Replace the record's own outgoing relations (C6.3). Cycles, a second parent of the same kind, a self
    relation and a membership that does not point at a series or collection are refused (409/400) and nothing
    changes. On an authority below federation emit protocol 1.4 the relations are stored but not published
    (`published: false`, `reason: emit_protocol`, `pending`: how many wait); once the protocol is raised, putting
    them again signs the ones still waiting (`changed.published`). Relations from the upstream feed are kept as
    they are.

    On a mirror or local node the relations are local overlays, allowed on any record, including one its
    upstream manages (as in the web editor): they are never published, and the upstream's own relations always
    win over them (a conflicting overlay is ended, `superseded`), so an overlay cannot change or block what this
    node applies from its upstream."""
    row, _r = service.get_canonical(session, cid)
    local = st.ctx.settings.role != "authority"
    result = relations.set_outgoing(session, st.ctx, row.id, [i.model_dump() for i in body.relations],
                                    actor=p.name, local_only=local)
    session.refresh(row)
    out = {**game_out(row, None, session, st.ctx), "relations": relations.of(session, row.id), "changed": result}
    # Honest (Phase 4 O1): published only when every relation this record holds on its own is in the signed feed.
    if local:
        return {**out, "published": False, "reason": "local_node"}
    waiting = relations.pending_count(session, row.id)
    if waiting or relations.gated(st.ctx):
        return {**out, "published": False, "reason": "emit_protocol", "pending": waiting}
    return {**out, "published": True}


class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ceiling: Literal["local"] | None = Field(description=(
        "`local`: keep the record on this node only (nothing about it is signed into a feed or shared with your "
        "nodes). `null`: publish it (lift any ceiling; on an authority the record is introduced on the feed)."))


@router.put("/games/{cid}/scope", tags=["curate"])
def put_scope(cid: str, body: ScopeBody, st: St, session: Sess,
              p: Annotated[auth.Principal, Depends(require("curate"))]):
    """Set or lift a record's scope ceiling (CONTRACT-C14 §5.3). A ceiling cannot un-publish: `local` on a record
    this node's feed already introduced is refused (409 `conflict`). `group` is set only by the group assistance
    (C14-2) and `private` has no writer yet."""
    from .. import scope

    row, _r = service.get_canonical(session, cid)
    try:
        if body.ceiling == "local":
            scope.set_ceiling(session, st.ctx, row.id, "local", by="operator", actor=None)
            seq = None
        else:
            seq = scope.publish(session, st.ctx, row.id, actor=None)
    except scope.CeilingRefused as exc:
        raise AtlasError(str(exc), "conflict") from exc
    session.refresh(row)
    return {"id": row.id, "ceiling": row.scope_ceiling, "introduced_seq": seq}


@router.get("/games/{cid}/explain", tags=["read"])
def explain(cid: str, session: Sess, st: St, principal: Annotated[auth.Principal, Depends(require("read"))]):
    row, redirected, _shown_to = _visible(session, st, principal, cid)
    return {"id": row.id, "redirected_from": redirected, "explain": row.explain}


# ---- contributing -------------------------------------------------------------------------------------

@router.post("/observations", tags=["write"])
def submit(body: ObservationBatchV1, st: St, session: Sess,
           p: Annotated[auth.Principal, Depends(require("contribute"))],
           report_unchanged: Annotated[bool, Query(description=(
               "Report an observation whose payload is identical to the stored one as `unchanged` (C11 §11.9.1). "
               "Without it such results say `updated`, as before."))] = False):
    if len(body.observations) > st.ctx.settings.max_batch:
        raise HTTPException(413, f"at most {st.ctx.settings.max_batch} observations per request")
    results = service.ingest(session, st.ctx, body, curator=p.allows("curate"), media_type=body.media_type)
    return {"contract_version": 1, "results": [
        {"external_id": r.external_id,
         "status": r.status if report_unchanged or r.status != "unchanged" else "updated",
         "atlas": {"canonical_id": r.canonical_id, "match": r.method, "confidence": r.confidence,
                   "review": r.review, "candidates": [{"id": c, "score": s} for c, s in r.candidates],
                   "reason": r.reason, "decision_id": r.decision_id}}
        for r in results]}


# ---- provenance (C11 §11.11) ----------------------------------------------------------------------------

@router.get("/games/{cid}/provenance", tags=["read"])
def get_provenance(cid: str, st: St, session: Sess, p: Annotated[auth.Principal, Depends(require("read"))],
                   detail: Annotated[Literal["values"] | None, Query(description=(
                       "`values` (curate scope) adds the candidate values per field."))] = None):
    """Why each field has its value: the winning source, its trust class, the reason it won, the runners-up
    and any conflict (C11 §11.11.1). Host and plugin observations are counted, never shown, without curate
    scope."""
    row, redirected, viewer = _visible(session, st, p, cid)
    curate = p.allows("curate")
    if detail == "values" and not curate:
        raise AtlasError("detail=values needs the curate scope", "forbidden")
    out = provenance.provenance(session, st.ctx.settings, row, curate=curate, values=detail == "values",
                                viewer=viewer)
    return {**out, "redirected_from": redirected}


@router.get("/games/{cid}/provenance/observations", tags=["curate"])
def provenance_observations(cid: str, session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))],
                            limit: Annotated[int, Query(ge=1, le=provenance.MAX_PAGE)] = 50,
                            cursor: Annotated[str | None, Query(max_length=40)] = None):
    row, _r = service.get_canonical(session, cid)
    return provenance.observations_page(session, row.id, limit=limit, cursor=cursor)


@router.get("/games/{cid}/decisions", tags=["curate"])
def game_decisions(cid: str, session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))],
                   limit: Annotated[int, Query(ge=1, le=provenance.MAX_PAGE)] = 50,
                   cursor: Annotated[str | None, Query(max_length=40)] = None):
    """Match decisions (C11 §11.4) for the record and every record merged into it, newest first."""
    row, _r = service.get_canonical(session, cid)
    rows = provenance.decisions_of(session, row.id, limit=limit + 1, cursor=cursor)
    return {"results": [matching.decision_out(d) for d in rows[:limit]],
            "next": rows[limit - 1].id if len(rows) > limit else None}


@router.get("/observations/{oid}/decision", tags=["curate"])
def observation_decision(oid: str, session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))]):
    """The newest match decision recorded for this observation."""
    row = session.scalars(select(MatchDecision).where(MatchDecision.observation_id == oid[:40])
                          .order_by(MatchDecision.created_at.desc(), MatchDecision.id.desc()).limit(1)).first()
    if row is None:
        raise AtlasError("no decision recorded for this observation", "not_found")
    return matching.decision_out(row)


@router.get("/conflicts", tags=["curate"])
def field_conflicts(session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))],
                    field: Annotated[str, Query(min_length=1, max_length=48)],
                    limit: Annotated[int, Query(ge=1, le=provenance.MAX_PAGE)] = 50,
                    cursor: Annotated[str | None, Query(max_length=40)] = None):
    """Records whose `field` has disagreeing sources in the winning trust class (C11 §11.7.2)."""
    return provenance.conflicts_page(session, field, limit=limit, cursor=cursor)


# ---- curation -----------------------------------------------------------------------------------------

class MergeBody(BaseModel):
    into: str
    override_distinct: bool = Field(False, description=(
        "Merge even though the records are marked as different works, ending that decision (C11 §11.6)."))


class LockBody(BaseModel):
    value: (
        Annotated[str, Field(max_length=20000)]
        | Annotated[list[Annotated[str, Field(max_length=128)]], Field(max_length=100)]
        # `releases` (C5.2, protocol 1.3): at most five entries; more are refused, not truncated
        | Annotated[list[dict[str, Any]], Field(max_length=5)]
    ) | None = Field(None, description="A string, a list of strings, or for `releases` a list of release entries. "
                                     "`release_date` takes `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. Required unless "
                                     "`clear` is true.")
    clear: bool = Field(False, description="Mark the field deliberately empty (C11 §11.8); give no `value`.")


@router.get("/games/{cid}/observations", tags=["curate"])
def observations(cid: str, session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))]):
    rows = session.scalars(select(Observation).where(Observation.canonical_id == cid)
                           .order_by(Observation.observed_at.desc())).all()
    return {"results": [{"id": o.id, "source": o.source, "source_kind": o.source_kind, "external_id": o.external_id,
                         "current": o.is_current, "redistributable": o.redistributable, "payload": o.payload,
                         "observed_at": o.observed_at.isoformat()} for o in rows]}


@router.post("/games/{cid}/merge", tags=["curate"])
def merge(cid: str, body: MergeBody, st: St, session: Sess,
          p: Annotated[auth.Principal, Depends(require("curate"))],
          dry_run: Annotated[bool, Query(description="Preview the merge and change nothing.")] = False):
    """Merge this record into `into`. With `dry_run=1` nothing changes: the answer shows the survivor's view and
    explain as they would be, and each field change (C11 §11.10.4). The merge is recorded, so a curator can undo
    it with `POST /merges/{id}/unmerge`."""
    _guard_upstream(session, st, cid)
    _guard_upstream(session, st, body.into)
    if dry_run:
        return service.merge_preview(session, st.ctx, cid, body.into)
    return game_out(service.merge_canonicals(session, st.ctx, cid, body.into, actor=p.name,
                                             override_distinct=body.override_distinct))


# ---- merge records and unmerge (C11 §11.10) -------------------------------------------------------------

@router.get("/merges", tags=["curate"])
def merge_list(session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))],
               canonical_id: Annotated[str, Query(min_length=1, max_length=40)],
               limit: Annotated[int, Query(ge=1, le=provenance.MAX_PAGE)] = 50):
    """Merges into the record (or into a record merged into it), and the merge of the record itself."""
    return {"results": [provenance.merge_out(m) for m in provenance.merges_of(session, canonical_id, limit=limit)]}


@router.get("/merges/{merge_id}", tags=["curate"])
def merge_get(merge_id: str, session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))]):
    return merges.detail(session, merges.get(session, merge_id[:40]))


class UnmergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = Field(False, description="Report what would move back and change nothing.")
    remember_distinct: bool = Field(True, description=(
        "Remember the two records as different works, so matching never merges them again."))


@router.post("/merges/{merge_id}/unmerge", tags=["curate"])
def merge_undo(merge_id: str, st: St, session: Sess, p: Annotated[auth.Principal, Depends(require("curate"))],
               body: UnmergeBody | None = None,
               if_match: Annotated[str | None, Header(alias="If-Match", max_length=80)] = None):
    """Undo a recorded merge (C11 §11.10.3): the merged record comes back with its observations, keys, locks,
    artwork, achievements and the rest, and rows edited after the merge are reported, not overwritten. Merges are
    undone last in, first out. `If-Match` takes the survivor's ETag."""
    body = body or UnmergeBody()
    return merges.unmerge(session, st.ctx, merge_id[:40], actor=p.name, dry_run=body.dry_run,
                          remember_distinct=body.remember_distinct, if_match=if_match)


# ---- "different works" decisions (C11 §11.6) -----------------------------------------------------------

class DistinctBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: str = Field(max_length=40)
    b: str = Field(max_length=40)
    note: str = Field("", max_length=500)


@router.get("/distinct", tags=["curate"])
def distinct_list(session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))],
                  canonical_id: Annotated[str, Query(min_length=1, max_length=40)],
                  limit: Annotated[int, Query(ge=1, le=provenance.MAX_PAGE)] = 50):
    return {"results": provenance.distinct_of(session, canonical_id, limit=limit)}


def _mark_distinct(session: Session, a: str, b: str, *, actor: str, note: str = "") -> dict[str, Any]:
    ra, _x = service.get_canonical(session, a)
    rb, _y = service.get_canonical(session, b)
    if ra.id == rb.id:
        raise AtlasError("these ids name the same record", "conflict")
    service.write_lock(session)
    row, created = matching.add_rejection(session, "record", ra.id, rb.id, reason="curator", actor=actor, note=note,
                                          local_only=True)
    matching.reevaluate_reviews(session, {ra.id, rb.id}, actor=actor)
    return {**matching.rejection_out(row), "created": created}


@router.post("/distinct", tags=["curate"])
def distinct_add(body: DistinctBody, session: Sess, p: Annotated[auth.Principal, Depends(require("curate"))]):
    """Mark two records as different works: matching never merges them and no review pairs them again. Local to
    this node in this release (federated distinct needs emit protocol 1.7)."""
    return _mark_distinct(session, body.a, body.b, actor=p.name, note=body.note)


@router.delete("/distinct/{rejection_id}", tags=["curate"])
def distinct_end(rejection_id: str, session: Sess, p: Annotated[auth.Principal, Depends(require("curate"))]):
    row = session.get(MatchRejection, rejection_id[:40])
    if row is None:
        raise AtlasError("unknown decision", "not_found")
    service.write_lock(session)
    matching.end_rejection(session, row, reason="curator", actor=p.name)
    return matching.rejection_out(row)


class ClientChangedCanonical(BaseModel):
    id: str = Field(description="Canonical Atlas ID touched by the feed page.")
    version: int = Field(description="Current client-visible version of this canonical record.")
    merged_into: str | None = Field(description="Redirect destination when this record was merged.")


class ClientChangesPage(BaseModel):
    """`feed=signed` (the default, unchanged v1): a page of the signed federation feed."""

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "feed": "signed",
        "local_feed": True,
        "node_id": "atlas-authority",
        "head": 124,
        "next_cursor": 124,
        "has_more": False,
        "results": [
            {"id": "atlas:0123456789abcdefghijklmnop", "version": 8, "merged_into": None},
            {"id": "atlas:1123456789abcdefghijklmnop", "version": 5,
             "merged_into": "atlas:2123456789abcdefghijklmnop"},
        ],
    }]})
    feed: Literal["signed"] = Field("signed", description="Which change log this page reads.")
    local_feed: bool = Field(True, description="Hint: this node also serves `feed=local`, which covers every "
                                                "change including provider imports. Recommended for clients.")
    node_id: str = Field(description="Authority feed selected for this page.")
    head: int = Field(description="Highest feed sequence currently retained by the selected node.")
    next_cursor: int = Field(description="Pass this value as `since` for the next page.")
    has_more: bool = Field(description="Whether another page is available from the current feed snapshot.")
    results: list[ClientChangedCanonical] = Field(description="Unique canonical records touched by this page.")


class LocalChangedCanonical(ClientChangedCanonical):
    causes: list[str] = Field(description="Why the record changed within this page, sorted: feed, replicated, local, "
                                          "provider, merge or detach. Informational; refresh the record whatever it is.")


class LocalChangesPage(BaseModel):
    """`feed=local`: a page of this node's local, unsigned client change log (C3.2)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "feed": "local", "node_id": "3f2a9c0d1e2b4a55", "epoch": "9c41e07a5b2d8f13", "head": 9123,
        "next_cursor": 9000, "has_more": True, "reset": False,
        "results": [{"id": "atlas:0123456789abcdefghijklmnop", "version": 14, "merged_into": None,
                     "causes": ["provider"]}],
    }]})
    feed: Literal["local"] = Field(description="Which change log this page reads.")
    node_id: str = Field(description="This node. The local log is never federated, so it has no other origin.")
    epoch: str = Field(description="Identifies this incarnation of the log. It changes whenever the log is recreated "
                                   "or rewound (a downgrade and re-upgrade, a restore, a recovered node). Store it "
                                   "with `next_cursor` and send it back as `epoch`.")
    head: int = Field(description="Highest sequence in this node's local change log.")
    next_cursor: int = Field(description="Pass this value as `since` for the next page.")
    has_more: bool = Field(description="Whether another page is available from the current log snapshot.")
    reset: bool = Field(description="True when `since` cannot be continued (pruned, the log was recreated, or "
                                    "`epoch` differs). "
                                    "`results` is then empty: store `next_cursor`, then re-read every cached record.")
    results: list[LocalChangedCanonical] = Field(description="Unique canonical records changed in this page.")


ChangesPage = Annotated[ClientChangesPage | LocalChangesPage, Field(discriminator="feed")]


def _client_changed_ids(kind: str, body: dict[str, Any]) -> list[str]:
    """Canonical ids affected by a signed feed event, in feed order."""
    if kind == "merge":
        values = (body.get("src"), body.get("dst"))
    elif kind == "detach":
        # `canonical_id` was added for clients; older signed events still have the new id.
        values = (body.get("canonical_id"), body.get("new_canonical_id"))
    elif kind == "relation":  # both ends show the relation in their record (C6.3)
        values = (body.get("from"), body.get("to"))
    else:
        values = (body.get("canonical_id"),)
    return [value for value in values if isinstance(value, str) and value]


@router.get(
    "/changes",
    tags=["games"],
    response_model=ChangesPage,
    summary="List canonical records changed since a client cursor",
    responses={
        200: {
            "content": {
                "application/json": {
                    "examples": {
                        "signed": {"summary": "Default (v1): the signed feed", "value": {
                            "feed": "signed",
                            "local_feed": True,
                            "node_id": "atlas-authority",
                            "head": 124,
                            "next_cursor": 124,
                            "has_more": False,
                            "results": [
                                {"id": "atlas:0123456789abcdefghijklmnop", "version": 8, "merged_into": None},
                                {"id": "atlas:1123456789abcdefghijklmnop", "version": 5,
                                 "merged_into": "atlas:2123456789abcdefghijklmnop"},
                            ],
                        }},
                        "local": {"summary": "feed=local: every change on this node (recommended)", "value": {
                            "feed": "local", "node_id": "3f2a9c0d1e2b4a55", "epoch": "9c41e07a5b2d8f13",
                            "head": 9123, "next_cursor": 9000, "has_more": True, "reset": False,
                            "results": [{"id": "atlas:0123456789abcdefghijklmnop", "version": 14,
                                         "merged_into": None, "causes": ["provider"]}],
                        }},
                    }
                }
            }
        }
    },
)
def client_changes(
    st: St,
    session: Sess,
    principal: Annotated[auth.Principal, Depends(require("read"))],
    since: Annotated[int, Query(ge=0, description="Last `next_cursor` stored by the client.")] = 0,
    limit: Annotated[int, Query(ge=1, le=500, description="Maximum feed events in this page.")] = 100,
    node_id: Annotated[
        str | None, Query(max_length=64, description="Optional authority node whose replicated feed is retained here. "
                                                     "Not allowed with `feed=local`.")
    ] = None,
    feed_name: Annotated[
        Literal["signed", "local"] | None,
        Query(alias="feed", description="`local` pages this node's local change log, which also covers provider "
                                        "imports, local edits, merges and detaches (recommended). Omit it, or pass "
                                        "`signed`, for the signed feed (unchanged v1 behaviour)."),
    ] = None,
    epoch: Annotated[
        str | None,
        Query(max_length=64, description="`feed=local` only: the `epoch` stored with `since`. When it differs from "
                                         "the log's current epoch the page is a `reset`. Always send it."),
    ] = None,
) -> dict[str, Any]:
    """Return current versions of canonical records touched by a bounded feed page.

    The sequence cursor is independent of timestamps and includes merge redirects.
    Clients should retain `next_cursor` and request the next page while `has_more` is true.
    The two feeds have separate cursors; never pass a cursor from one to the other.
    """
    if feed_name == "local":
        if node_id is not None:
            raise HTTPException(400, "node_id is not allowed with feed=local: the local log has no other origin.")
        from .. import scope

        # C14 §5.7: a caller sees only the records it may read; the others are left out as if absent.
        hidden = scope.hidden_ids(_viewer(session, st, principal))
        return {"feed": "local", "node_id": st.ctx.identity.node_id,
                **client_change_log.page(session, since, limit, epoch, hidden=hidden)}
    if epoch is not None:
        raise HTTPException(400, "epoch is only used with feed=local.")
    origin = node_id or st.ctx.identity.node_id
    if origin == st.ctx.identity.node_id:
        entries = feed.read(session, since, limit)
        head = feed.head_seq(session)
    else:
        if session.get(ReplicatedFeed, origin) is None:
            raise HTTPException(404, "This node does not retain that authority's feed.")
        entries = list(
            session.scalars(
                select(ReplicatedChange)
                .where(ReplicatedChange.node_id == origin, ReplicatedChange.seq > since)
                .order_by(ReplicatedChange.seq)
                .limit(limit)
            )
        )
        head = session.scalar(
            select(func.max(ReplicatedChange.seq)).where(ReplicatedChange.node_id == origin)
        ) or 0

    ids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for cid in _client_changed_ids(entry.kind, entry.body or {}):
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
    records = {
        row.id: row
        for row in session.scalars(select(Canonical).where(Canonical.id.in_(ids)))
    } if ids else {}
    next_cursor = entries[-1].seq if entries else since
    return {
        "feed": "signed",
        "local_feed": True,
        "node_id": origin,
        "head": head,
        "next_cursor": next_cursor,
        # Do not make a client spin if a replica is missing a feed segment.
        "has_more": bool(entries) and next_cursor < head,
        "results": [
            {"id": cid, "version": records[cid].version, "merged_into": records[cid].merged_into}
            for cid in ids if cid in records
        ],
    }


@router.post("/observations/{oid}/detach", tags=["curate"])
def detach(oid: str, st: St, session: Sess, p: Annotated[auth.Principal, Depends(require("curate"))]):
    obs = session.get(Observation, oid)
    if obs is not None and obs.canonical_id:
        _guard_upstream(session, st, obs.canonical_id)
    return game_out(service.detach_observation(session, st.ctx, oid, actor=p.name))


@router.put("/games/{cid}/locks/{field}", tags=["curate"])
def lock(cid: str, field: str, body: LockBody, st: St, session: Sess,
         p: Annotated[auth.Principal, Depends(require("curate"))]):
    """Pin a field to a value, or with `{"clear": true}` mark it deliberately empty (C11 §11.8). A clear is kept on
    this node only in this release."""
    local = st.ctx.settings.role != "authority" or body.clear
    if not local:
        _guard_upstream(session, st, cid)
    service.set_lock(session, st.ctx, cid, field, body.value, actor=p.name, local_only=local, clear=body.clear)
    row, _r = service.get_canonical(session, cid)
    return game_out(row)


@router.delete("/games/{cid}/locks/{field}", tags=["curate"])
def unlock(cid: str, field: str, st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))]):
    lk = session.get(Lock, (cid, field))
    if lk is not None and not lk.local_only and st.ctx.settings.role != "authority":
        raise AtlasError("this lock is managed by the upstream authority", "conflict")
    return game_out(service.clear_lock(session, st.ctx, cid, field))


@router.get("/review", tags=["curate"])
def review_list(session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))],
                status: str = "open", limit: Annotated[int, Query(ge=1, le=200)] = 50,
                kind: Annotated[str | None, Query(max_length=32)] = None,
                order: Annotated[Literal["created", "priority"], Query(description=(
                    "`priority` puts the most urgent first (C11 §11.5): identity conflicts, then likely matches."
                ))] = "created"):
    """Review items with the matcher's reason, evidence and decision (C11 §11.5)."""
    stmt = select(ReviewItem).where(ReviewItem.status == status)
    if kind:
        stmt = stmt.where(ReviewItem.kind == kind)
    if order == "priority":
        stmt = stmt.order_by(ReviewItem.priority.desc(), ReviewItem.created_at, ReviewItem.id)
    else:
        stmt = stmt.order_by(ReviewItem.created_at, ReviewItem.id)
    return {"results": [matching.review_out(r) for r in session.scalars(stmt.limit(limit)).all()]}


def _open_item(session: Session, rid: str) -> ReviewItem:
    item = session.get(ReviewItem, rid)
    if item is None or item.status != "open":
        raise AtlasError("unknown or closed review item", "not_found")
    return item


@router.post("/review/{rid}/dismiss", tags=["curate"])
def review_dismiss(rid: str, session: Sess, p: Annotated[auth.Principal, Depends(require("curate"))]):
    item = session.get(ReviewItem, rid)
    if item is None:
        raise AtlasError("unknown review item", "not_found")
    matching.close_review(session, item, "dismissed", p.name)
    return {"id": rid, "status": item.status}


@router.post("/review/{rid}/distinct", tags=["curate"])
def review_distinct(rid: str, session: Sess, p: Annotated[auth.Principal, Depends(require("curate"))]):
    """Resolve the item as "different works": every candidate is remembered as distinct from the record."""
    item = _open_item(session, rid)
    others = sorted({c for c, _ in item.candidates} - {item.canonical_id})
    if not others:
        raise AtlasError("this review item names no other record")
    out = [_mark_distinct(session, item.canonical_id, o, actor=p.name) for o in others]
    if item.status == "open":
        matching.close_review(session, item, "distinct", p.name)
    return {"id": rid, "status": item.status, "distinct": out}


class ReviewDetachBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: str = Field(max_length=40)


@router.post("/review/{rid}/detach", tags=["curate"])
def review_detach(rid: str, body: ReviewDetachBody, st: St, session: Sess,
                  p: Annotated[auth.Principal, Depends(require("curate"))]):
    """Resolve the item by splitting one wrongly attached observation off its record."""
    item = _open_item(session, rid)
    obs = session.get(Observation, body.observation_id)
    involved = {item.canonical_id, *(c for c, _ in item.candidates)}
    if obs is None or obs.canonical_id not in involved:
        raise AtlasError("the observation is not on a record of this review item", "not_found")
    _guard_upstream(session, st, obs.canonical_id)
    row = service.detach_observation(session, st.ctx, obs.id, actor=p.name)
    if item.status == "open":
        matching.close_review(session, item, "resolved", p.name)
    return {"id": rid, "status": item.status, "game": game_out(row)}


@router.post("/review/{rid}/merge", tags=["curate"])
def review_merge(rid: str, body: MergeBody, st: St, session: Sess,
                 p: Annotated[auth.Principal, Depends(require("curate"))]):
    item = _open_item(session, rid)
    if item.kind in matching.UNMERGEABLE_KINDS:
        # It names a survivor and the record an unmerge split off it: merging would redo the undone merge.
        raise AtlasError("an unmerge check is resolved by detaching rows or dismissing it, not by merging",
                         "conflict")
    if body.into not in {c for c, _ in item.candidates}:
        raise AtlasError("'into' must be one of the candidates")
    _guard_upstream(session, st, item.canonical_id)
    _guard_upstream(session, st, body.into)
    row = service.merge_canonicals(session, st.ctx, item.canonical_id, body.into, actor=p.name, cause="review",
                                   review_id=item.id, decision_id=item.decision_id,
                                   override_distinct=body.override_distinct)
    return game_out(row)


# ---- providers ----------------------------------------------------------------------------------------

class FetchBody(BaseModel):
    external_id: str = Field(min_length=1, max_length=128)


@router.get("/providers", tags=["providers"])
def providers_list(st: St, _: Annotated[auth.Principal, Depends(require("read"))]):
    return {"results": [{"name": p.name, "requests_per_minute": p.requests_per_minute}
                        for p in st.providers.providers.values()]}


@router.get("/providers/{name}/search", tags=["providers"])
def provider_search(name: str, q: Annotated[str, Query(min_length=1, max_length=200)], st: St, session: Sess,
                    _: Annotated[auth.Principal, Depends(require("read"))]):
    """Lightweight candidate list; nothing is stored. Pick one and POST .../fetch."""
    try:
        results = st.providers.lookup(session, name, query=q)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"results": [r.to_item() for r in results]}


@router.post("/providers/{name}/fetch", tags=["providers"])
def provider_fetch(name: str, body: FetchBody, st: St, session: Sess,
                   _: Annotated[auth.Principal, Depends(require("contribute"))]):
    """Fetch full details (cached), merge into the canonical record, return it."""
    try:
        got = importer.import_from_provider(st, session, name, body.external_id)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"match": got.method, "confidence": got.confidence, "review": got.review,
            "game": game_out(got.row, None, session, st.ctx)}


# ---- schema and custom values -------------------------------------------------------------------------

@router.get("/schema", tags=["ops"])
def schema(st: St, session: Sess):
    """Public: the media types this node describes and the extra fields each one has."""
    out = []
    for t in custom.types(session, enabled_only=True):
        out.append({"key": t.key, "label": t.label, "labels": t.labels or {}, "fields": [
            {"key": f.key, "label": f.label, "kind": f.kind, "choices": f.choices or [], "help": f.help,
             "editable": f.editable} for f in custom.fields_for(session, t.key, public=True)]})
    return {"media_types": out, "contract": 1}


class CustomBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict, max_length=60)
    clear: list[str] = Field(default_factory=list, max_length=60)


@router.put("/games/{cid}/custom", tags=["curate"])
def put_custom(cid: str, body: CustomBody, st: St, session: Sess,
               _: Annotated[auth.Principal, Depends(require("curate"))]):
    _guard_upstream(session, st, cid)
    row = session.get(Canonical, cid)
    if row is None or row.merged_into:
        raise AtlasError("unknown or merged canonical", "not_found")
    values = custom.clean_values(session, row.media_type, body.values, editable_only=False)
    custom.set_values(session, st.ctx, cid, "curator", values, clear=tuple(body.clear),
                      redistributable=st.ctx.settings.role == "authority")
    return {"custom": custom.merged(session, st.ctx, cid)}


# ---- artwork, achievements, media --------------------------------------------------------------------

class ArtworkBody(BaseModel):
    items: list[dict[str, Any]] = Field(max_length=200)


ACHIEVEMENT_PAGE_MAX = 200


class AchievementsBody(BaseModel):
    items: list[dict[str, Any]] = Field(max_length=2000)
    #: C12 §12.1.1: the list's sets (absent: every item is in `base`)
    sets: list[dict[str, Any]] | None = Field(default=None, max_length=50)


def _art_out(settings: Any, a: Artwork, near: dict[str, str] | None = None) -> dict[str, Any]:
    fetch = media.fetch_policy(settings, a.source, a.url)
    pol = media_policy.policy(settings, a.source)
    out = {"id": a.id, "kind": a.kind, "url": a.url, "width": a.width, "height": a.height, "source": a.source,
           "preferred": a.preferred, "cached": a.blob_hash is not None,
           "media_url": f"/api/v2/media/{a.blob_hash}" if a.blob_hash else None,
           # #46 (additive): the same bytes on the token-free path, for <img> tags and hosts that cannot send an
           # Authorization header (`media_url` needs a `read` token). Only while this node serves them (`proxy`).
           "public_url": f"/media/{a.blob_hash}" if a.blob_hash and fetch == "proxy" else None,
           # the cached bytes when we have them; a redirect only to the source's allow-listed hosts (H50)
           "image_url": f"/api/v2/artwork/{a.id}/image",
           # C12 §12.5 (additive): measured vs declared size, role fit, duplicates, variants, licence, state
           "declared_width": a.declared_width, "declared_height": a.declared_height, "aspect": a.aspect,
           "role_fit": facets.role_fit(a), "class": source_class(a.source), "duplicate_of": a.duplicate_of,
           "region": a.region, "locale": a.locale,
           "licence": a.licence or pol.licence or ("owner_upload" if media.is_upload(settings, a.url) else "unknown"),
           "attribution": a.attribution or pol.attribution, "source_page_url": a.source_page_url,
           "local_only": a.local_only, "serve": fetch, "state": media.describe_state(settings, a),
           "last_error": a.last_error}
    if near is not None:
        out["near_duplicate_of"] = near.get(a.id)
    return out


ArtRegion = Annotated[str | None, Query(max_length=8, description=(
    "Prefer artwork of this region (`ww`, `eu`, `na`, `asia`, `oce`, `latam` or ISO 3166-1 alpha-2)."))]
ArtLang = Annotated[str | None, Query(max_length=35, description="Prefer artwork (or text) in this language tag.")]


ArtOrder = Annotated[Literal["legacy", "quality"], Query(description=(
    "`legacy` (default): the order this list has always had, so a client that takes the first image keeps getting "
    "the same one. `quality`: the C12 §12.5.7 ranking (curator, community choice, role fit, class, trust, region "
    "and language, priority, measured size); `results` and `primary` both follow it."))]


@router.get("/games/{cid}/artwork", tags=["read"])
def get_artwork(cid: str, st: St, session: Sess, principal: Annotated[auth.Principal, Depends(require("read"))],
                region: ArtRegion = None, lang: ArtLang = None, include_duplicates: bool = False,
                order: ArtOrder = "legacy"):
    """Every image of a record. By default `results` keeps its pre-C12 order (community choice, trust, priority,
    age) and `primary` is the first of each kind in it, as before. `best` is always the C12 §12.5.7 pick of each
    kind (curator, community choice, role fit, class, trust, region and language match, priority, measured size);
    `order=quality` sorts `results` (and so `primary`) by that ranking too. `region` and `lang` only steer the
    C12 ranking. Exact duplicates are left out unless `include_duplicates=1`. Curator tokens also see
    `near_duplicate_of` (a perceptual match, reported and never hidden)."""
    from ..dates import REGION_RE

    if region is not None and not REGION_RE.fullmatch(region):
        raise HTTPException(422, "region must be ww, eu, na, asia, oce, latam or an ISO 3166-1 alpha-2 code")
    want_lang = facets.clean_locale(lang, "lang") if lang else None
    row, redirected, viewer = _visible(session, st, principal, cid)
    ranked = facets.merged_artwork(session, st.ctx, row.id, region=region, lang=want_lang,
                                   include_duplicates=include_duplicates, viewer=viewer)
    near = facets.near_duplicates(ranked) if principal.allows("curate") else None
    by_id = {a.id: {**_art_out(st.ctx.settings, a, near), **_item_scope(session, st, a, viewer)} for a in ranked}
    best: dict[str, Any] = {}
    for a in ranked:
        if a.duplicate_of is None:
            best.setdefault(a.kind, by_id[a.id])
    if order == "quality":
        rows = ranked
    else:
        rows = sorted(ranked, key=lambda a: facets.legacy_art_rank(st.ctx.settings, a))
    items = [by_id[a.id] for a in rows]
    primary: dict[str, Any] = {}
    for it in items:
        if it["duplicate_of"] is None:
            primary.setdefault(it["kind"], it)
    return {"id": row.id, "redirected_from": redirected, "order": order, "primary": primary, "best": best,
            "results": items}


@router.put("/games/{cid}/artwork", tags=["curate"])
def put_artwork(cid: str, body: ArtworkBody, st: St, session: Sess,
                _: Annotated[auth.Principal, Depends(require("curate"))]):
    """Replace the curator artwork of a record. `count` is the number of items given (unchanged meaning); `stored`
    how many are now the curator's; `skipped` lists each image another source of the record already lists (same
    kind and address), which stays that source's: `{kind, url, held_by, reason: held_by_another_source}`."""
    _guard_upstream(session, st, cid)
    skipped: list[dict[str, Any]] = []
    n = facets.set_artwork(session, st.ctx, cid, "curator", body.items,
                           redistributable=st.ctx.settings.role == "authority", skipped=skipped)
    return {"count": n, "stored": n - len(skipped), "skipped": skipped}


@router.get("/artwork/misfits", tags=["curate"])
def get_artwork_misfits(session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))],
                        limit: Annotated[int, Query(ge=1, le=200)] = 50,
                        cursor: Annotated[str | None, Query(max_length=26)] = None):
    """Measured images whose aspect does not suit their role (C12 §12.5.3), by id, for curators. Scans at most
    2000 measured rows per call; follow `next_cursor` until it is null."""
    q = select(Artwork).where(Artwork.aspect.is_not(None)).order_by(Artwork.id)
    if cursor:
        q = q.where(Artwork.id > cursor)
    out, last = [], None
    for a in session.scalars(q.limit(2000)):
        last = a.id
        if facets.role_fit(a) is False:
            out.append({"id": a.id, "canonical_id": a.canonical_id, "kind": a.kind, "aspect": a.aspect,
                        "width": a.width, "height": a.height, "source": a.source})
            if len(out) >= limit:
                break
    more = last is not None and session.scalar(select(Artwork.id).where(
        Artwork.aspect.is_not(None), Artwork.id > last).limit(1)) is not None
    return {"results": out, "next_cursor": last if more else None}


SetQuery = Annotated[str | None, Query(alias="set", max_length=200, description=(
    "Only this set (`<source>/<key>` or `<key>`)."))]
PlatformQuery = Annotated[str | None, Query(max_length=50, description="Only sets that apply to this platform.")]
OrderQuery = Annotated[str | None, Query(max_length=10, description=(
    "`rarity` (default), `source` (the provider's list order) or `name`."))]


@router.get("/games/{cid}/achievements", tags=["read"])
def get_achievements(cid: str, st: St, session: Sess, principal: Annotated[auth.Principal, Depends(require("read"))],
                     response: Response,
                     reveal: Annotated[str | None, Query(max_length=2000)] = None,
                     limit: Annotated[int | None, Query(ge=1, le=ACHIEVEMENT_PAGE_MAX)] = None,
                     offset: Annotated[int, Query(ge=0)] = 0, set_: SetQuery = None,
                     platform: PlatformQuery = None, lang: ArtLang = None, order: OrderQuery = None,
                     include_retired: bool = False):
    """Hidden achievements come back redacted. `reveal=all` or `reveal=<id or key>,...` unlocks their real text, but
    only for a signed-in user's own token or a curator/admin token; any other caller still gets the redacted rows.
    A response that reveals anything is `Cache-Control: private, no-store` (C12 §12.1.8).

    Each entry carries a stable `key` (`ak1:…`, the smallest of `keys`, the stored keys of the rows merged into it)
    and `key_identity` (null while redacted). `id` stays for older clients and is deprecated as an identity.

    C12 (additive): `sets` lists the record's achievement sets (base game, DLC, editions, platforms) and every
    entry names its `set`, `status`, `rarity` (percent, source, as_of, stale, band) and `lang`. `set`, `platform`,
    `lang`, `order` and `include_retired` (curators only) select and arrange; `sets_elsewhere` names DLC sets
    listed on another record that point at this one.

    Large catalogues can be read in pages: pass `limit` (at most 200) and `offset`, and follow `next_offset` until it
    is null. The order is stable, so pages never overlap or skip. Without `limit` the whole list is returned."""
    row, redirected, viewer = _visible(session, st, principal, cid)
    allowed = principal.personal or principal.allows("curate")
    wanted: bool | set[str] = False
    if reveal and allowed:
        wanted = True if reveal.strip().lower() == "all" else {i.strip() for i in reveal.split(",") if i.strip()}
    listing = facets.achievement_listing(session, st.ctx, row.id, reveal=wanted, lang=lang, order=order,
                                         set_filter=set_, platform=platform,
                                         include_retired=include_retired and principal.allows("curate"),
                                         viewer=viewer)
    results = listing["results"]
    total = len(results)
    hidden_count = sum(1 for r in results if r["hidden"])
    next_offset = None
    if limit is not None:
        next_offset = offset + limit if offset + limit < total else None
        results = results[offset:offset + limit]
    if listing["revealed_any"]:
        response.headers["Cache-Control"] = "private, no-store"
    return {"id": row.id, "redirected_from": redirected, "hidden_count": hidden_count, "total": total,
            "next_offset": next_offset, "sets": listing["sets"], "lang": listing["lang"],
            "sets_elsewhere": facets.sets_elsewhere(session, st.ctx, row.id), "results": results}


@router.put("/games/{cid}/achievements", tags=["curate"])
def put_achievements(cid: str, body: AchievementsBody, st: St, session: Sess,
                     _: Annotated[auth.Principal, Depends(require("curate"))]):
    _guard_upstream(session, st, cid)
    n = facets.set_achievements(session, st.ctx, cid, "curator", body.items, sets=body.sets, ordered=True,
                                redistributable=st.ctx.settings.role == "authority")
    return {"count": n}


# ---- prices (C8) ------------------------------------------------------------------------------------------

def _price_filters(region: str | None, currency: str | None) -> None:
    if region is not None and not money.valid_region(region):
        raise HTTPException(422, "region must be an ISO 3166-1 alpha-2 code in lower case (prices have no `ww`)")
    if currency is not None and not money.valid_currency(currency):
        raise HTTPException(422, "currency must be an active ISO 4217 alphabetic code in upper case")


PriceRegion = Annotated[str | None, Query(min_length=2, max_length=2, description=(
    "ISO 3166-1 alpha-2 storefront region, lower case (`us`, `jp`).") )]
PriceCurrency = Annotated[str | None, Query(min_length=3, max_length=3, description="ISO 4217 code (`USD`).")]
PriceProvider = Annotated[str | None, Query(max_length=32, description="Only this provider (`steam`).")]


@router.get("/games/{cid}/prices", tags=["read"])
def get_prices(cid: str, session: Sess, st: St, principal: Annotated[auth.Principal, Depends(require("read"))],
               region: PriceRegion = None, currency: PriceCurrency = None, provider: PriceProvider = None):
    """The newest observed price of every offer of a record (C8.2): one row per provider, product, package,
    edition, platform, region and currency, ordered by provider, region, currency. Money is integer minor units
    (`amount_minor`, `base_amount_minor`) with the ISO 4217 `exponent`; `amount` / `base_amount` are the same
    values as exact decimal strings. `sale` is true below the list price, `stale` once `valid_until` passed.
    Prices whose licence does not allow redistribution are returned only to curator and admin tokens. Prices are
    not part of the record's view or version, and are never published in the federation feed."""
    _price_filters(region, currency)
    row, redirected, _v = _visible(session, st, principal, cid)
    result = prices.latest(session, row.id, region=region, currency=currency, provider=provider,
                           restricted=principal.allows("curate"))
    return {"id": row.id, "redirected_from": redirected, **result}


@router.get("/games/{cid}/prices/history", tags=["read"])
def get_price_history(cid: str, session: Sess, st: St, principal: Annotated[auth.Principal, Depends(require("read"))],
                      region: Annotated[str, Query(min_length=2, max_length=2, description=(
                          "The storefront region (required: price history is regional)."))],
                      currency: PriceCurrency = None, provider: PriceProvider = None,
                      product_id: Annotated[str | None, Query(max_length=128)] = None,
                      edition: Annotated[str | None, Query(max_length=128)] = None,
                      platform: Annotated[str | None, Query(max_length=32)] = None,
                      since: Annotated[datetime | None, Query(description="Observed at or after (ISO 8601).")] = None,
                      until: Annotated[datetime | None, Query(description="Observed before (ISO 8601).")] = None,
                      limit: Annotated[int, Query(ge=1, le=prices.MAX_HISTORY)] = 100,
                      cursor: Annotated[str | None, Query(max_length=200, description=(
                          "`next_cursor` of the previous page."))] = None):
    """Every retained observation of a record in one region, newest first (`observed_at`, then id; stable across
    pages). Follow `next_cursor` until it is null. Observations older than their retention (30 days by default,
    at most 365) are gone."""
    _price_filters(region, currency)
    row, redirected, _v = _visible(session, st, principal, cid)
    result = prices.history(session, row.id, region=region, currency=currency, provider=provider,
                            product_id=product_id, edition=edition, platform=platform, since=since, until=until,
                            limit=limit, cursor=cursor, restricted=principal.allows("curate"))
    return {"id": row.id, "redirected_from": redirected, **result}


@router.post("/prices", tags=["curate"])
def post_prices(body: prices.PriceBatch, session: Sess,
                principal: Annotated[auth.Principal, Depends(require("curate"))]):
    """Store up to 500 price observations (C8.1). Amounts are integers in the currency's minor units; a float,
    a string amount, an unknown currency or region, or amounts that disagree with `discount_pct` reject the whole
    request (422). Each item then reports `stored`, `duplicate` (already stored, nothing changes), `conflict`
    (the same offer and `observed_at` is stored with other amounts; the stored one is kept), `unknown_record`,
    `expired` (already past `retention_days`) or `invalid`. Nothing is published to followers."""
    results = prices.ingest(session, body.prices, actor=principal.name)
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"results": results, "counts": counts}


@router.get("/prices/status", tags=["admin"])
def price_status(session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]):
    """How many price observations are stored, how many the next purge removes, and the price channel's state
    (protocol 1.6, disabled in this release)."""
    return prices.status(session)


@router.get("/artwork/{art_id}/image", tags=["read"])
def get_artwork_image(art_id: str, st: St, session: Sess,
                      principal: Annotated[auth.Principal, Depends(require("read"))]):
    """The image of one artwork row (C12 §12.5.5, §12.5.8, §12.7.2, H50).

    * the cached bytes of a `proxy` source (`public, max-age=300`, digest as ETag);
    * otherwise, for a provider row (a `redirect` source, or a `proxy` source not cached yet), a 307 to the
      original (`private, max-age=300`, unchanged), but only when its host is in the source's `redirect_hosts` or
      in the admin's explicit `media_allowed_hosts`: there is no open redirect;
    * 404 for any other host, for a source that is `none`, and for an uncached curated, community or uploaded
      image (those are only ever served from this node's cache)."""
    from fastapi.responses import RedirectResponse

    from .. import instruments

    result = "miss"
    try:
        art = session.get(Artwork, art_id)
        if art is None:
            raise HTTPException(404, "no such image")
        from .. import scope

        viewer = _viewer(session, st, principal)
        clause = scope.artwork_clause(viewer, st.ctx.settings)
        if clause is not None and session.scalars(
                select(Artwork.id).where(Artwork.id == art.id, clause)).first() is None:
            raise HTTPException(404, "no such image")  # C14 §5.7: exactly the answer for an unknown id
        settings = st.ctx.settings
        fetch = media.fetch_policy(settings, art.source, art.url)
        if fetch == "none":
            result = "refused"
            raise HTTPException(404, "image is not served here")
        if fetch == "proxy" and art.blob_hash and media.find_blob(settings, art.blob_hash):
            result = "hit"
            group = scope.is_group_art(art, scope.ceiling_of(session, art.canonical_id))
            return media_response(st, art.blob_hash, etag=True, access="group" if group else "public", viewer=viewer)
        if source_class(art.source) != "provider" or media.is_upload(settings, art.url):
            raise HTTPException(404, "image is not available yet")
        result = "refused"
        try:
            _scheme, host, _port, _ip = media.check_url(art.url, settings)  # public https addresses only
        except media.MediaError:
            raise HTTPException(404, "image is not available") from None
        allowed = set(media_policy.policy(settings, art.source).redirect_hosts)
        allowed |= {h.lower() for h in (settings.media_allowed_hosts or ())}
        if host.lower() not in allowed:
            raise HTTPException(404, "image is not available")
        result = "redirect"
        return RedirectResponse(art.url, status_code=307, headers={"Cache-Control": "private, max-age=300"})
    finally:
        instruments.media_cache.inc(route="image", result=result)


def media_response(st: AppState, digest: str, *, etag: bool = False, access: str = "public", viewer: Any = None):
    """`etag` is for addresses whose bytes can change (the per-row image route): a short public cache and the
    digest as ETag. `/media/<sha256>` names the bytes themselves and is immutable. `access="group"` (C14 §5.7):
    bytes only data received from the group references, never `immutable`: `private, no-store` for a signed-in
    viewer, `private, max-age=300` for a visitor while `group_data_public_pages` is on."""
    from fastapi.responses import FileResponse

    found = media.find_blob(st.ctx.settings, digest)
    if found is None:
        raise HTTPException(404, "no such image")
    path, ctype = found
    headers = {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; sandbox"}
    if etag:
        headers.update({"Cache-Control": "public, max-age=300", "ETag": f'"{digest}"'})
    if access == "group":
        signed_in = viewer is not None and viewer.kind in ("user", "detail")
        headers["Cache-Control"] = "private, no-store" if signed_in else "private, max-age=300"
        headers.pop("ETag", None)
    return FileResponse(path, media_type=ctype, headers=headers)


@router.get("/media/{digest}", tags=["read"])
def get_media(digest: str, st: St, session: Sess, principal: Annotated[auth.Principal, Depends(require("read"))]):
    return served_media(session, st, digest, "no such image", viewer=_viewer(session, st, principal))


def served_media(session: Session, st: AppState, digest: str, missing: str, *, viewer: Any = None):
    """`/media/<sha256>` (API and site): cached bytes still servable under the source's policy (§12.5.5), and
    only when the viewer may see a row that references them (C14 §5.7; a refusal is the same 404)."""
    from .. import instruments, scope

    access = scope.media_access(session, viewer, st.ctx.settings, digest) if viewer is not None else "public"
    if access is None or not media.servable(session, st.ctx.settings, digest):  # redirect/none (§12.5.5)
        instruments.media_cache.inc(route="media", result="refused")
        raise HTTPException(404, missing)
    try:
        response = media_response(st, digest, access=access, viewer=viewer)
    except HTTPException:
        instruments.media_cache.inc(route="media", result="miss")
        raise
    instruments.media_cache.inc(route="media", result="hit")
    return response


@router.get("/sync/media/{digest}", tags=["federation"])
def sync_media(digest: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")], st: St, session: Sess,
               _: Annotated[auth.Principal, Depends(require("sync"))]):
    """C13 §6.2: the cached bytes of an image this node federates, for the followers that received its row. A blob is
    served only when a row referencing it may federate (C12 §12.5.5 applied to bytes): redistributable, not local-only,
    of a source whose policy federates, with the licence `cc0` or `owner_upload` (or `unknown` on a curated URL row
    when `federation_media_unknown_licence` is on, H58). Anything else is 404, so this never discloses provider,
    private or licence-withheld media. Content addressed: the follower checks the hash."""
    settings = st.ctx.settings
    rows = session.scalars(select(Artwork).where(
        Artwork.blob_hash == digest, Artwork.redistributable.is_(True), Artwork.local_only.is_(False)).limit(20))
    for row in rows:
        pol = media_policy.policy(settings, row.source)
        licence = row.licence or pol.licence or ("owner_upload" if media.is_upload(settings, row.url) else "unknown")
        if pol.federate and (licence in facets.FEDERABLE_UPLOAD_LICENCES or (
                licence == "unknown" and settings.federation_media_unknown_licence
                and source_class(row.source) != "provider")):
            return media_response(st, digest)
    raise HTTPException(404, "no such image")


# ---- federation ---------------------------------------------------------------------------------------

ProtocolHeader = Annotated[str | None, Header(alias=feed_schema.PROTOCOL_HEADER, max_length=32)]


def _note_follower(session: Session, who: auth.Principal, protocol: str | None, *, default: str | None) -> None:
    """Remember which protocol a follower credential speaks (token id only), at most once a minute."""
    value = protocol if feed_schema.valid_protocol(protocol) else default
    if value is None or not who.token_id:
        return
    row = session.get(FollowerSeen, who.token_id)
    now = datetime.now(UTC)
    if row is not None:
        seen = row.last_seen_at if row.last_seen_at.tzinfo else row.last_seen_at.replace(tzinfo=UTC)
        if row.protocol == value and (now - seen).total_seconds() <= 60:
            return  # fresh enough: a read request takes no write lock
    upsert_follower_seen(session, who.token_id, value, now)


def upsert_follower_seen(session: Session, token_id: str, protocol: str, now: datetime) -> None:
    """Insert or update in one statement, so two concurrent requests with the same token cannot both
    insert the primary key (an IntegrityError and a failed sync request on PostgreSQL)."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - Atlas supports SQLite and PostgreSQL only
        raise RuntimeError(f"unsupported database dialect {dialect}")
    stmt = insert(FollowerSeen).values(token_id=token_id, protocol=protocol, last_seen_at=now)
    session.execute(stmt.on_conflict_do_update(index_elements=[FollowerSeen.token_id],
                                               set_={"protocol": protocol, "last_seen_at": now}))
    cached = session.identity_map.get(session.identity_key(FollowerSeen, token_id))
    if cached is not None:
        session.expire(cached)  # the statement bypassed the ORM; reload on next access


@router.get("/sync/info", tags=["federation"])
def sync_info(st: St, session: Sess, who: Annotated[auth.Principal, Depends(require("read"))],
              atlas_protocol: ProtocolHeader = None):
    identity = load_identity(session, st.ctx.settings)
    s = st.ctx.settings
    _note_follower(session, who, atlas_protocol, default=None)
    feeds = []
    for row in session.scalars(select(ReplicatedFeed).order_by(ReplicatedFeed.node_id)):
        history = key_rotation.trusted_history(session, row.node_id)
        item = {"node_id": row.node_id, "public_key": row.public_key, "rotations": history,
                "signing_key_id": key_rotation.expected_key(row.node_id, history, 2**63 - 1),
                "head": session.scalar(select(func.max(ReplicatedChange.seq))
                                       .where(ReplicatedChange.node_id == row.node_id)) or 0}
        seen = session.get(NodeState, "sync_protocol:" + row.node_id)
        if seen is not None and feed_schema.valid_protocol(seen.value):
            item["protocol"] = seen.value  # what the relayed authority was seen advertising; not re-signed
        item["empty"] = item["head"] == 0  # C13 §3.4: known but nothing retained yet (a relay, not an origin)
        feeds.append(item)
    # C13 §3.4 (additive): this node's own feed head, so a prober can tell an empty origin from a relay; what its
    # authority feed introduced; and how much of its cached media it does not serve to peers.
    extra: dict[str, Any] = {"own_head": feed.head_seq(session)}
    try:  # additive: a schema still behind (a rolled-back database) answers without them, never with an error
        with session.begin_nested():
            extra["introduced_through"] = feed.introduced_through(session)
            extra["media_withheld"] = session.scalar(select(func.count()).select_from(Artwork).where(
                Artwork.blob_hash.is_not(None),
                (Artwork.redistributable.is_(False)) | (Artwork.local_only.is_(True)))) or 0
            if s.role == "authority":
                extra["records"] = feed.records_summary(session)
    except SQLAlchemyError:
        extra = {"own_head": extra["own_head"]}
    return {"node_id": identity.node_id, "public_key": identity.trust_public_b64,
            "signing_key_id": identity.key_id, "rotations": key_rotation.own_history(session, identity.node_id),
            "role": s.role, "kind": s.kind(), "operator": s.node_operator,
            "contract": 1, "name": s.app_name,
            "protocol": feed_schema.PROTOCOL, "min_follower_protocol": s.min_follower_protocol,
            "emit_protocol": feed_schema.emit_protocol(s), "observation_schema": feed_schema.OBSERVATION_SCHEMA,
            "kinds": feed_schema.kinds_advertised(), "capabilities": feed_schema.capabilities(),
            "feeds": feeds, **extra}


@router.get("/sync/price-changes", tags=["federation"])
def sync_price_changes(_: Annotated[auth.Principal, Depends(require("sync"))],
                       since: Annotated[int, Query(ge=0)] = 0):
    """The price channel (C8.3, protocol 1.6): designed, not enabled in this release. It answers 404, and
    `/sync/info` does not list the `price-channel-v1` capability, so no follower ever reads prices from it."""
    raise HTTPException(404, f"the price channel (protocol {feed_schema.PRICE_CHANNEL.since}) is not enabled on this node")


@router.get("/sync/digest", tags=["federation"])
def sync_digest(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("sync"))],
                node_id: Annotated[str | None, Query(max_length=64)] = None):
    """Digest of the replicated observations of this node's feed, or of a retained authority feed with
    `node_id`. It hashes every observation, so answers are cached per feed for DIGEST_TTL seconds."""
    if node_id and node_id != st.ctx.identity.node_id:
        if session.get(ReplicatedFeed, node_id) is None:
            raise HTTPException(404, "This node does not retain that authority's feed.")
        return {"node_id": node_id, **_cached_digest(st, session, node_id)}
    return {"node_id": st.ctx.identity.node_id, **_cached_digest(st, session, None)}


DIGEST_TTL = 60.0
_digest_lock = threading.Lock()


def _cached_digest(st: AppState, session: Session, node_id: str | None) -> dict[str, Any]:
    """Per app instance (stored on its state), so one sync token cannot make the node hash the whole
    table on every request."""
    clock = time.monotonic()
    with _digest_lock:
        cache: dict[str | None, tuple[float, dict[str, Any]]] = st.__dict__.setdefault("_digest_cache", {})
        hit = cache.get(node_id)
        if hit is not None and clock - hit[0] < DIGEST_TTL:
            return {**hit[1], "cached_seconds": round(clock - hit[0], 1)}
    result = sync.view_digest(session, node_id)
    with _digest_lock:
        if len(cache) > 256:
            cache.clear()
        cache[node_id] = (clock, result)
    return {**result, "cached_seconds": 0.0}


@router.get("/sync/changes", tags=["federation"])
def sync_changes(st: St, session: Sess, who: Annotated[auth.Principal, Depends(require("sync"))],
                 since: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=5000)] = 500,
                 node_id: Annotated[str | None, Query(max_length=64)] = None, atlas_protocol: ProtocolHeader = None):
    # A follower that sends no Atlas-Protocol header is a pre-1.1 release.
    _note_follower(session, who, atlas_protocol, default=feed_schema.LEGACY_PROTOCOL)
    origin = node_id or st.ctx.identity.node_id
    if origin == st.ctx.identity.node_id:
        rows = feed.read(session, since, limit)
        head = feed.head_seq(session)
    else:
        if session.get(ReplicatedFeed, node_id) is None:
            raise HTTPException(404, "This node does not retain that authority's feed.")
        rows = list(session.scalars(select(ReplicatedChange).where(ReplicatedChange.node_id == node_id,
                            ReplicatedChange.seq > since).order_by(ReplicatedChange.seq).limit(limit)))
        head = session.scalar(select(func.max(ReplicatedChange.seq)).where(ReplicatedChange.node_id == node_id)) or 0
    return {"head": head, "entries": [
        {"seq": r.seq, "kind": r.kind, "body": r.body, "body_hash": r.body_hash, "signature": r.signature,
         "key_id": r.key_id, "node_id": origin, "created_at": r.created_at.isoformat()} for r in rows]}


# ---- tokens -------------------------------------------------------------------------------------------

class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/tokens", tags=["admin"], status_code=201)
def token_create(body: TokenBody, session: Sess, who: Annotated[auth.Principal, Depends(require("admin"))]):
    try:
        row, secret = auth.create_token(session, body.name, body.scopes, body.expires_in_days)
    except ValueError as exc:
        raise AtlasError(str(exc)) from exc
    # The actor is a token, not a user: record which one (never the secret).
    accounts.audit(session, None, "token_create", row.id, {"scopes": row.scopes, "via": "api", "by_token": who.token_id})
    return {"id": row.id, "name": row.name, "scopes": row.scopes, "token": secret,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None}


@router.get("/tokens", tags=["admin"])
def token_list(session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]):
    rows = session.scalars(select(Token).order_by(Token.created_at)).all()
    return {"results": [{"id": t.id, "name": t.name, "scopes": t.scopes, "revoked": t.revoked_at is not None,
                         "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                         "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None} for t in rows]}


@router.delete("/tokens/{tid}", tags=["admin"], status_code=204)
def token_revoke(tid: str, session: Sess, who: Annotated[auth.Principal, Depends(require("admin"))]):
    row = session.get(Token, tid)
    if row is None:
        raise AtlasError("unknown token", "not_found")
    row.revoked_at = datetime.now(row.created_at.tzinfo)
    accounts.audit(session, None, "token_revoke", tid, {"via": "api", "by_token": who.token_id})
    return Response(status_code=204)


_ = norm_namespace  # re-exported for plugins/tests
