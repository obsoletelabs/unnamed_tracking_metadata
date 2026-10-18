from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__, auth, feed, service
from ..contract import ObservationBatchV1, ProviderIdentity, ProviderKey
from ..models import Canonical, Observation, ReviewItem, SyncCursor, Token
from ..normalize import norm_namespace
from ..resolve import find
from ..service import AtlasError
from .deps import AppState, get_session, require, state

router = APIRouter(prefix="/api/v2")

Sess = Annotated[Session, Depends(get_session)]
St = Annotated[AppState, Depends(state)]


def game_out(row: Canonical, redirected_from: str | None = None) -> dict[str, Any]:
    return {
        "id": row.id, "version": row.version, "redirected_from": redirected_from,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None, **row.view,
    }


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
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/ready", tags=["ops"])
def ready(st: St, session: Sess) -> dict[str, Any]:
    session.execute(select(1))
    cursors = {c.upstream: c.last_seq for c in session.scalars(select(SyncCursor))}
    return {"status": "ready", "role": st.ctx.settings.role, "feed_head": feed.head_seq(session),
            "upstream_cursors": cursors}


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
    if len(body.items) > st.ctx.settings.max_batch:
        raise HTTPException(413, f"at most {st.ctx.settings.max_batch} items per request")
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
            entry["game"] = game_out(row) if row else None
        out.append(entry)
    return {"results": out}


@router.get("/games", tags=["read"])
def list_games(session: Sess, _: Annotated[auth.Principal, Depends(require("read"))],
               q: Annotated[str, Query(min_length=1, max_length=200)], limit: Annotated[int, Query(ge=1, le=100)] = 25):
    return {"results": [game_out(r) for r in service.search(session, q, limit)]}


@router.get("/games/{cid}", tags=["read"])
def get_game(cid: str, response: Response, session: Sess, _: Annotated[auth.Principal, Depends(require("read"))]):
    row, redirected = service.get_canonical(session, cid)
    response.headers["ETag"] = f'"{row.id}-{row.version}"'
    return game_out(row, redirected)


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
    value: str | list[str]


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


# ---- federation ---------------------------------------------------------------------------------------

@router.get("/sync/info", tags=["federation"])
def sync_info(st: St, _: Annotated[auth.Principal, Depends(require("read"))]):
    return {"node_id": st.ctx.identity.node_id, "public_key": st.ctx.identity.public_b64,
            "role": st.ctx.settings.role, "contract": 1}


@router.get("/sync/changes", tags=["federation"])
def sync_changes(session: Sess, _: Annotated[auth.Principal, Depends(require("sync"))],
                 since: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=5000)] = 500):
    rows = feed.read(session, since, limit)
    return {"head": feed.head_seq(session), "entries": [
        {"seq": r.seq, "kind": r.kind, "body": r.body, "body_hash": r.body_hash, "signature": r.signature,
         "key_id": r.key_id, "node_id": r.key_id, "created_at": r.created_at.isoformat()} for r in rows]}


# ---- tokens -------------------------------------------------------------------------------------------

class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/tokens", tags=["admin"], status_code=201)
def token_create(body: TokenBody, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]):
    try:
        row, secret = auth.create_token(session, body.name, body.scopes, body.expires_in_days)
    except ValueError as exc:
        raise AtlasError(str(exc)) from exc
    return {"id": row.id, "name": row.name, "scopes": row.scopes, "token": secret,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None}


@router.get("/tokens", tags=["admin"])
def token_list(session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]):
    rows = session.scalars(select(Token).order_by(Token.created_at)).all()
    return {"results": [{"id": t.id, "name": t.name, "scopes": t.scopes, "revoked": t.revoked_at is not None,
                         "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                         "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None} for t in rows]}


@router.delete("/tokens/{tid}", tags=["admin"], status_code=204)
def token_revoke(tid: str, session: Sess, _: Annotated[auth.Principal, Depends(require("admin"))]):
    row = session.get(Token, tid)
    if row is None:
        raise AtlasError("unknown token", "not_found")
    row.revoked_at = datetime.now(row.created_at.tzinfo)
    return Response(status_code=204)


_ = norm_namespace  # re-exported for plugins/tests
