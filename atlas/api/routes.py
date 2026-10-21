from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__, accounts, auth, custom, db, facets, feed, importer, key_rotation, media, service
from ..contract import ObservationBatchV1, ProviderIdentity, ProviderKey
from ..models import Artwork, Canonical, Lock, Observation, ReplicatedChange, ReplicatedFeed, ReviewItem, SyncCursor, Token
from ..normalize import norm_namespace
from ..providers import ProviderError
from ..resolve import find
from ..service import AtlasError
from ..signing import load_identity
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
            "upstream_cursors": cursors,
            # the detail the public /ready deliberately leaves out
            "checks": {"database": "ok", "schema": db.schema_state(session, st.ctx.settings.database_url),
                       "media": "ok" if media.storage_problem(st.ctx.settings) is None else "unwritable",
                       "provider_failures": st.providers.failure_counts()}}


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
    namespace: ProviderKey | None = None
    external_id: str | None = Field(default=None, max_length=256)
    title: str | None = Field(default=None, max_length=500)
    platform: str | None = Field(default=None, max_length=50)
    release_date: str | None = Field(default=None, max_length=10)
    developer: str | None = Field(default=None, max_length=200)
    provider_ids: dict[ProviderKey, ProviderIdentity] | None = Field(default=None, max_length=32)


class ResolveRequest(BaseModel):
    items: list[ResolveItem] = Field(min_length=1, max_length=5000)
    include_view: bool = True


@router.post("/resolve", tags=["read"])
def resolve(body: ResolveRequest, st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("read"))]):
    if len(body.items) > st.ctx.settings.max_resolve:
        raise HTTPException(413, f"at most {st.ctx.settings.max_resolve} items per request")
    out = []
    for it in body.items:
        m = find(
            session, st.ctx.settings, namespace=it.namespace or "", external_id=it.external_id or "",
            provider_ids=it.provider_ids, title=it.title, release_date=it.release_date,
            platform=it.platform, developer=it.developer,
        ) if (it.external_id or it.title or it.provider_ids) else None
        if m is None:
            out.append({"canonical_id": None, "method": "none", "confidence": 0.0, "candidates": []})
            continue
        entry: dict[str, Any] = {
            "canonical_id": m.canonical_id, "method": m.method if m.assigned else "none",
            "confidence": m.confidence, "candidates": [{"id": c, "score": s} for c, s in m.candidates],
        }
        if body.include_view and m.canonical_id:
            row = session.get(Canonical, m.canonical_id)
            entry["game"] = game_out(row, None, session, st.ctx) if row else None
        out.append(entry)
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
    results = service.ingest(session, st.ctx, body, curator=p.allows("curate"))
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
                     reveal: Annotated[str | None, Query(max_length=2000)] = None):
    """Hidden achievements come back redacted. `reveal=all` or `reveal=<id>,<id>` unlocks their real text, but only
    for a signed-in user's own token or a curator/admin token; any other caller still gets the redacted rows."""
    row, redirected = service.get_canonical(session, cid)
    allowed = principal.personal or principal.allows("curate")
    wanted: bool | set[str] = False
    if reveal and allowed:
        wanted = True if reveal.strip().lower() == "all" else {i.strip() for i in reveal.split(",") if i.strip()}
    results = facets.merged_achievements(session, st.ctx, row.id, reveal=wanted)
    return {"id": row.id, "redirected_from": redirected,
            "hidden_count": sum(1 for r in results if r["hidden"]), "results": results}


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

@router.get("/sync/info", tags=["federation"])
def sync_info(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("read"))]):
    identity = load_identity(session)
    feeds = [{"node_id": row.node_id, "public_key": row.public_key,
              "rotations": key_rotation.trusted_history(session, row.node_id),
              "signing_key_id": key_rotation.expected_key(row.node_id,
                                key_rotation.trusted_history(session, row.node_id), 2**63 - 1),
              "head": session.scalar(select(func.max(ReplicatedChange.seq))
                                     .where(ReplicatedChange.node_id == row.node_id)) or 0}
             for row in session.scalars(select(ReplicatedFeed).order_by(ReplicatedFeed.node_id))]
    return {"node_id": identity.node_id, "public_key": identity.trust_public_b64,
            "signing_key_id": identity.key_id, "rotations": key_rotation.own_history(session, identity.node_id),
            "role": st.ctx.settings.role, "kind": st.ctx.settings.kind(), "operator": st.ctx.settings.node_operator,
            "contract": 1, "name": st.ctx.settings.app_name, "feeds": feeds}


@router.get("/sync/changes", tags=["federation"])
def sync_changes(st: St, session: Sess, _: Annotated[auth.Principal, Depends(require("sync"))],
                 since: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=5000)] = 500,
                 node_id: Annotated[str | None, Query(max_length=64)] = None):
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
