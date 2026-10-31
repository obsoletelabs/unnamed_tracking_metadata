from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import (
    __version__,
    accounts,
    auth,
    custom,
    db,
    facets,
    feed,
    feed_schema,
    importer,
    key_rotation,
    media,
    service,
    sync,
)
from .. import client_changes as client_change_log
from ..contract import ObservationBatchV1, ProviderIdentity, ProviderKey
from ..models import (
    Artwork,
    Canonical,
    FollowerSeen,
    Lock,
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
from ..providers import ProviderError
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
             ctx: Any = None) -> dict[str, Any]:
    out = {
        "id": row.id, "version": row.version, "redirected_from": redirected_from,
        "media_type": row.media_type, "updated_at": _iso(row.updated_at), **row.view,
    }
    if session is not None and ctx is not None:
        out["custom"] = custom.merged(session, ctx, row.id)
    return out


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
def ready(session: Sess, st: St, response: Response) -> dict[str, str]:
    """Minimal on purpose (public): ready only when the database answers and its schema is current."""
    session.execute(select(1))
    if db.schema_state(session, st.ctx.settings.database_url) == "behind":
        response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        return {"status": "not_ready"}
    return {"status": "ready"}


@router.get("/status", tags=["admin"])
def status(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]) -> dict[str, Any]:
    cursors = {c.upstream: c.last_seq for c in session.scalars(select(SyncCursor))}
    return {"role": st.ctx.settings.role, "feed_head": feed.head_seq(session),
            "upstream_cursors": cursors, "sync_failures": sync.open_failures(session),
            # "encrypted" (enc:v1, ATLAS_SECRET_KEY) or "plaintext" (legacy; every backup holds the key) (C9.1)
            "signing_key_at_rest": signing_key_at_rest(session),
            # named heads per upstream (observed, downloaded, verified, applied, parked, quarantined)
            "upstreams": sync.status(session, st.ctx)["upstreams"],
            # the detail the public /ready deliberately leaves out
            "checks": {"database": "ok", "schema": db.schema_state(session, st.ctx.settings.database_url),
                       "media": "ok" if media.storage_problem(st.ctx.settings) is None else "unwritable",
                       "provider_failures": st.providers.failure_counts()}}


@router.get("/sync/failures", tags=["admin"])
def sync_failures(session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))],
                  limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    """Mirrored feed entries that could not be applied and still need attention."""
    rows = session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None))
                           .order_by(SyncFailure.node_id, SyncFailure.seq).limit(limit)).all()
    return {"open": sync.open_failures(session),
            "parked": sync.open_failures(session, feed_schema.PARK),
            "quarantined": sync.open_failures(session, feed_schema.QUARANTINE),
            "failures": [{"node_id": f.node_id, "seq": f.seq, "kind": f.kind, "error": f.error,
                          "disposition": f.disposition, "reason": f.reason, "detail": f.detail,
                          "attempts": f.attempts, "first_failed_at": f.first_failed_at.isoformat(),
                          "last_attempt_at": f.last_attempt_at.isoformat()} for f in rows]}


@router.get("/sync/status", tags=["admin"])
def sync_status(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]) -> dict[str, Any]:
    """Federation compatibility and replication heads of every upstream feed (C1.7)."""
    s = st.ctx.settings
    return {"own_protocol": feed_schema.PROTOCOL, "min_follower_protocol": s.min_follower_protocol,
            "emit_protocol": feed_schema.emit_protocol(s), **sync.status(session, st.ctx),
            "followers": [{"token_id": f.token_id, "protocol": f.protocol, "last_seen_at": _iso(f.last_seen_at)}
                          for f in session.scalars(select(FollowerSeen).order_by(FollowerSeen.last_seen_at.desc())
                                                   .limit(200))]}


@router.post("/sync/failures/retry", tags=["admin"])
def retry_sync_failures(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))],
                        limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
    """Retry failed mirrored entries now, whatever the automatic attempt count."""
    service.write_lock(session)
    result = sync.retry_failures(session, st.ctx, limit=limit, max_attempts=None)
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


def _resolution_out(r: Resolution, session: Session, st: AppState, include_view: bool) -> dict[str, Any]:
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
        entry["game"] = game_out(row, None, session, st.ctx) if row else None
    return entry


@router.post("/resolve", tags=["read"], response_model=ResolveResponse, response_model_exclude_unset=True,
             summary="Resolve client ids and titles to canonical Atlas ids (C4)")
def resolve(body: ResolveRequest, st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("read"))]):
    """Read-only. Identity evidence wins over titles; conflicting identity evidence returns `canonical_id: null`
    with `reason: conflict` (a behaviour change, H2: older Atlas returned the item's own id with `crosswalk`)."""
    if len(body.items) > st.ctx.settings.max_resolve:
        raise HTTPException(413, f"at most {st.ctx.settings.max_resolve} items per request")
    out = []
    for it in body.items:
        r = classify(
            session, st.ctx.settings, namespace=it.namespace or "", external_id=it.external_id or "",
            provider_ids=it.provider_ids, title=it.title, release_date=it.release_date,
            platform=it.platform, developer=it.developer, media_type=it.media_type,
        ) if (it.external_id or it.title or it.provider_ids) else Resolution()
        out.append(_resolution_out(r, session, st, body.include_view))
    return {"results": out}


@router.get("/games", tags=["read"])
def list_games(session: Sess, st: St, _: Annotated[auth.Principal, Depends(require("read"))],
               q: Annotated[str, Query(min_length=1, max_length=200)], limit: Annotated[int, Query(ge=1, le=100)] = 25,
               type: Annotated[str | None, Query(max_length=32)] = None):
    rows = service.search(session, q, limit, media_type=type)
    return {"results": [game_out(r, None, session, st.ctx) for r in rows]}


@router.get("/games/{cid}", tags=["read"])
def get_game(cid: str, response: Response, session: Sess, st: St,
             _: Annotated[auth.Principal, Depends(require("read"))]):
    row, redirected = service.get_canonical(session, cid)
    response.headers["ETag"] = f'"{row.id}-{row.version}"'
    return game_out(row, redirected, session, st.ctx)


@router.get("/games/{cid}/explain", tags=["read"])
def explain(cid: str, session: Sess, _: Annotated[auth.Principal, Depends(require("read"))]):
    row, redirected = service.get_canonical(session, cid)
    return {"id": row.id, "redirected_from": redirected, "explain": row.explain}


# ---- contributing -------------------------------------------------------------------------------------

@router.post("/observations", tags=["write"])
def submit(body: ObservationBatchV1, st: St, session: Sess,
           p: Annotated[auth.Principal, Depends(require("contribute"))]):
    if len(body.observations) > st.ctx.settings.max_batch:
        raise HTTPException(413, f"at most {st.ctx.settings.max_batch} observations per request")
    results = service.ingest(session, st.ctx, body, curator=p.allows("curate"), media_type=body.media_type)
    return {"contract_version": 1, "results": [
        {"external_id": r.external_id, "status": r.status,
         "atlas": {"canonical_id": r.canonical_id, "match": r.method, "confidence": r.confidence,
                   "review": r.review, "candidates": [{"id": c, "score": s} for c, s in r.candidates]}}
        for r in results]}


# ---- curation -----------------------------------------------------------------------------------------

class MergeBody(BaseModel):
    into: str


class LockBody(BaseModel):
    value: (
        Annotated[str, Field(max_length=20000)]
        | Annotated[list[Annotated[str, Field(max_length=128)]], Field(max_length=100)]
    )


@router.get("/games/{cid}/observations", tags=["curate"])
def observations(cid: str, session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))]):
    rows = session.scalars(select(Observation).where(Observation.canonical_id == cid)
                           .order_by(Observation.observed_at.desc())).all()
    return {"results": [{"id": o.id, "source": o.source, "source_kind": o.source_kind, "external_id": o.external_id,
                         "current": o.is_current, "redistributable": o.redistributable, "payload": o.payload,
                         "observed_at": o.observed_at.isoformat()} for o in rows]}


@router.post("/games/{cid}/merge", tags=["curate"])
def merge(cid: str, body: MergeBody, st: St, session: Sess,
          p: Annotated[auth.Principal, Depends(require("curate"))]):
    _guard_upstream(session, st, cid)
    _guard_upstream(session, st, body.into)
    return game_out(service.merge_canonicals(session, st.ctx, cid, body.into, actor=p.name))


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
    _: Annotated[auth.Principal, Depends(require("read"))],
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
        return {"feed": "local", "node_id": st.ctx.identity.node_id,
                **client_change_log.page(session, since, limit, epoch)}
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
def detach(oid: str, st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("curate"))]):
    obs = session.get(Observation, oid)
    if obs is not None and obs.canonical_id:
        _guard_upstream(session, st, obs.canonical_id)
    return game_out(service.detach_observation(session, st.ctx, oid))


@router.put("/games/{cid}/locks/{field}", tags=["curate"])
def lock(cid: str, field: str, body: LockBody, st: St, session: Sess,
         p: Annotated[auth.Principal, Depends(require("curate"))]):
    local = st.ctx.settings.role != "authority"
    if not local:
        _guard_upstream(session, st, cid)
    service.set_lock(session, st.ctx, cid, field, body.value, actor=p.name, local_only=local)
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
                status: str = "open", limit: Annotated[int, Query(ge=1, le=200)] = 50):
    rows = session.scalars(select(ReviewItem).where(ReviewItem.status == status)
                           .order_by(ReviewItem.created_at).limit(limit)).all()
    return {"results": [{"id": r.id, "kind": r.kind, "canonical_id": r.canonical_id, "candidates": r.candidates,
                         "confidence": r.confidence, "status": r.status} for r in rows]}


@router.post("/review/{rid}/dismiss", tags=["curate"])
def review_dismiss(rid: str, session: Sess, p: Annotated[auth.Principal, Depends(require("curate"))]):
    item = session.get(ReviewItem, rid)
    if item is None:
        raise AtlasError("unknown review item", "not_found")
    item.status, item.decided_by = "dismissed", p.name
    return {"id": rid, "status": item.status}


@router.post("/review/{rid}/merge", tags=["curate"])
def review_merge(rid: str, body: MergeBody, st: St, session: Sess,
                 p: Annotated[auth.Principal, Depends(require("curate"))]):
    item = session.get(ReviewItem, rid)
    if item is None or item.status != "open":
        raise AtlasError("unknown or closed review item", "not_found")
    if body.into not in {c for c, _ in item.candidates}:
        raise AtlasError("'into' must be one of the candidates")
    _guard_upstream(session, st, item.canonical_id)
    _guard_upstream(session, st, body.into)
    row = service.merge_canonicals(session, st.ctx, item.canonical_id, body.into, actor=p.name)
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


def _art_out(a: Artwork) -> dict[str, Any]:
    return {"id": a.id, "kind": a.kind, "url": a.url, "width": a.width, "height": a.height, "source": a.source,
            "preferred": a.preferred, "cached": a.blob_hash is not None,
            "media_url": f"/api/v2/media/{a.blob_hash}" if a.blob_hash else None,
            # always usable: the cached bytes when we have them, else a redirect to the original source
            "image_url": f"/api/v2/artwork/{a.id}/image"}


@router.get("/games/{cid}/artwork", tags=["read"])
def get_artwork(cid: str, st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("read"))]):
    row, redirected = service.get_canonical(session, cid)
    items = [_art_out(a) for a in facets.merged_artwork(session, st.ctx, row.id)]
    primary: dict[str, Any] = {}
    for it in items:
        primary.setdefault(it["kind"], it)
    return {"id": row.id, "redirected_from": redirected, "primary": primary, "results": items}


@router.put("/games/{cid}/artwork", tags=["curate"])
def put_artwork(cid: str, body: ArtworkBody, st: St, session: Sess,
                _: Annotated[auth.Principal, Depends(require("curate"))]):
    _guard_upstream(session, st, cid)
    n = facets.set_artwork(session, st.ctx, cid, "curator", body.items,
                           redistributable=st.ctx.settings.role == "authority")
    return {"count": n}


@router.get("/games/{cid}/achievements", tags=["read"])
def get_achievements(cid: str, st: St, session: Sess, principal: Annotated[auth.Principal, Depends(require("read"))],
                     reveal: Annotated[str | None, Query(max_length=2000)] = None,
                     limit: Annotated[int | None, Query(ge=1, le=ACHIEVEMENT_PAGE_MAX)] = None,
                     offset: Annotated[int, Query(ge=0)] = 0):
    """Hidden achievements come back redacted. `reveal=all` or `reveal=<id or key>,...` unlocks their real text, but
    only for a signed-in user's own token or a curator/admin token; any other caller still gets the redacted rows.

    Each entry carries a stable `key` (`ak1:…`, the smallest of `keys`, the stored keys of the rows merged into it)
    and `key_identity` (null while redacted). `id` stays for older clients and is deprecated as an identity.

    Large catalogues can be read in pages: pass `limit` (at most 200) and `offset`, and follow `next_offset` until it
    is null. The order is stable, so pages never overlap or skip. Without `limit` the whole list is returned."""
    row, redirected = service.get_canonical(session, cid)
    allowed = principal.personal or principal.allows("curate")
    wanted: bool | set[str] = False
    if reveal and allowed:
        wanted = True if reveal.strip().lower() == "all" else {i.strip() for i in reveal.split(",") if i.strip()}
    results = facets.merged_achievements(session, st.ctx, row.id, reveal=wanted)
    total = len(results)
    hidden_count = sum(1 for r in results if r["hidden"])
    next_offset = None
    if limit is not None:
        next_offset = offset + limit if offset + limit < total else None
        results = results[offset:offset + limit]
    return {"id": row.id, "redirected_from": redirected, "hidden_count": hidden_count, "total": total,
            "next_offset": next_offset, "results": results}


@router.put("/games/{cid}/achievements", tags=["curate"])
def put_achievements(cid: str, body: AchievementsBody, st: St, session: Sess,
                     _: Annotated[auth.Principal, Depends(require("curate"))]):
    _guard_upstream(session, st, cid)
    n = facets.set_achievements(session, st.ctx, cid, "curator", body.items, ordered=True,
                                redistributable=st.ctx.settings.role == "authority")
    return {"count": n}


@router.get("/artwork/{art_id}/image", tags=["read"])
def get_artwork_image(art_id: str, st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("read"))]):
    """Cached bytes when available; otherwise pass the client through to the image's original public address."""
    from fastapi.responses import RedirectResponse

    art = session.get(Artwork, art_id)
    if art is None:
        raise HTTPException(404, "no such image")
    if art.blob_hash and media.find_blob(st.ctx.settings, art.blob_hash):
        return media_response(st, art.blob_hash)
    try:
        media.check_url(art.url, st.ctx.settings)  # only ever send clients to plain public addresses
    except media.MediaError:
        raise HTTPException(404, "image is not available") from None
    return RedirectResponse(art.url, status_code=307, headers={"Cache-Control": "private, max-age=300"})


def media_response(st: AppState, digest: str):
    from fastapi.responses import FileResponse

    found = media.find_blob(st.ctx.settings, digest)
    if found is None:
        raise HTTPException(404, "no such image")
    path, ctype = found
    return FileResponse(path, media_type=ctype, headers={
        "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.get("/media/{digest}", tags=["read"])
def get_media(digest: str, st: St, _: Annotated[auth.Principal, Depends(require("read"))]):
    return media_response(st, digest)


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
        feeds.append(item)
    return {"node_id": identity.node_id, "public_key": identity.trust_public_b64,
            "signing_key_id": identity.key_id, "rotations": key_rotation.own_history(session, identity.node_id),
            "role": s.role, "kind": s.kind(), "operator": s.node_operator,
            "contract": 1, "name": s.app_name,
            "protocol": feed_schema.PROTOCOL, "min_follower_protocol": s.min_follower_protocol,
            "emit_protocol": feed_schema.emit_protocol(s), "observation_schema": feed_schema.OBSERVATION_SCHEMA,
            "kinds": feed_schema.kinds_advertised(), "capabilities": list(feed_schema.CAPABILITIES),
            "feeds": feeds}


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
