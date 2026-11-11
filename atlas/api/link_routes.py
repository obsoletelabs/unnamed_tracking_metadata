"""Node link endpoints of a main Atlas (CONTRACT-C14 §6.3, §6.5, §6.7, §7.3; slice C14-3m).

OAuth endpoints (`/oidc/device/challenge`, `/oidc/device`, `/oidc/revoke`) and the DPoP-bound link API
(`/api/v2/link*`). Every answer carries `Cache-Control: no-store`. Everything 404s while links are not active here
(`link_enabled` off, or this node is not an authority), as if the routes did not exist.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import instruments, link, ratelimit
from ..service import AtlasError
from .deps import AppState, get_session, state

router = APIRouter()
St = Annotated[AppState, Depends(state)]
Sess = Annotated[Session, Depends(get_session, scope="function")]
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _off() -> JSONResponse:
    return JSONResponse({"error": {"code": "not_found", "message": "Not Found"}}, status_code=404, headers=NO_STORE)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _metric(endpoint: str, result: str) -> None:
    instruments.link_requests.inc(endpoint=endpoint, result=result if result in instruments.LINK_RESULTS
                                  else "invalid")


def _oauth_error(endpoint: str, exc: link.LinkError) -> JSONResponse:
    _metric(endpoint, exc.error)
    extra = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
    return JSONResponse(exc.body(), status_code=exc.status, headers={**NO_STORE, **extra})


def _device_throttle(st: AppState, request: Request) -> JSONResponse | None:
    ok, retry = ratelimit.hit(st.maker, f"link-device:{_ip(request)}", 30, 600)  # §7.5: 30 / IP / 10 min
    if ok:
        return None
    _metric("device", "rate_limited")
    return JSONResponse({"error": "slow_down"}, status_code=429, headers={**NO_STORE, "Retry-After": str(retry)})


@router.post("/oidc/device/challenge", include_in_schema=False)
def device_challenge(request: Request, st: St, session: Sess):
    if not link.active(st.ctx.settings):
        return _off()
    if (refused := _device_throttle(st, request)) is not None:
        return refused
    _metric("challenge", "ok")
    return JSONResponse(link.challenge(session), headers=NO_STORE)


@router.post("/oidc/device", include_in_schema=False)
async def device_authorization(request: Request, st: St, session: Sess):
    if not link.active(st.ctx.settings):
        return _off()
    if (refused := _device_throttle(st, request)) is not None:
        return refused
    form = {k: str(v)[:70_000] for k, v in (await request.form()).items()}
    form["_ip"] = _ip(request)
    try:
        out = link.device_authorize(session, st.ctx, form, request.headers.get("dpop"))
    except link.LinkError as exc:
        return _oauth_error("device", exc)
    _metric("device", "ok")
    return JSONResponse(out, headers=NO_STORE)


@router.post("/oidc/revoke", include_in_schema=False)
async def revoke(request: Request, st: St, session: Sess):
    """RFC 7009 for link tokens: always 200, whether the token was known or not."""
    if not link.active(st.ctx.settings):
        return _off()
    form = await request.form()
    if form.get("client_id") == link.CLIENT_ID:
        link.revoke_token(session, str(form.get("token", ""))[:200])
    _metric("revoke", "ok")
    return JSONResponse({}, headers=NO_STORE)


# ---- the DPoP-bound link API -----------------------------------------------------------------------------------

def _caller(request: Request, st: AppState, session: Session, endpoint: str) -> link.Caller | JSONResponse:
    if not link.active(st.ctx.settings):
        return _off()
    htu = st.ctx.settings.public_url + request.url.path
    try:
        return link.authenticate(session, st.ctx, request.headers.get("authorization"), request.headers.get("dpop"),
                                 request.method, htu)
    except link.Unauthorized as exc:
        if endpoint == "rotation" and exc.reason in ("key_mismatch", "invalid_token"):
            # §6.7: a rotation by an already superseded key of an active link means someone else rotated it
            target = link.superseded_attempt(session, st.ctx, request.headers.get("authorization"),
                                             request.headers.get("dpop"), request.method, htu)
            if target is not None:
                link.freeze(session, st.ctx, target, "rotation_from_superseded_key")
        _metric(endpoint, "frozen" if exc.reason == "link_frozen" else "unauthorized")
        return JSONResponse({"error": "invalid_token", "error_description": exc.reason}, status_code=401,
                            headers={**NO_STORE, "WWW-Authenticate": 'DPoP error="invalid_token", algs="EdDSA"'})


def _atlas_error(endpoint: str, exc: AtlasError) -> JSONResponse:
    status = {"not_found": 404, "conflict": 409, "forbidden": 403}.get(exc.code, 400)
    _metric(endpoint, "refused")
    body: dict[str, Any] = {"error": {"code": exc.code, "message": str(exc)}}
    if exc.detail.get("reason"):
        body["error"]["reason"] = exc.detail["reason"]
    return JSONResponse(body, status_code=status, headers=NO_STORE)


@router.get("/api/v2/link", tags=["link"])
def link_info(request: Request, st: St, session: Sess):
    caller = _caller(request, st, session, "link")
    if isinstance(caller, JSONResponse):
        return caller
    _metric("link", "ok")
    return JSONResponse(link.describe(session, st.ctx, caller.link, caller.user), headers=NO_STORE)


@router.delete("/api/v2/link", tags=["link"])
def link_delete(request: Request, st: St, session: Sess):
    caller = _caller(request, st, session, "unlink")
    if isinstance(caller, JSONResponse):
        return caller
    # §6.7: the node may report its own key as possibly stolen; its pending proposals are then flagged for review
    reason = "compromised" if request.query_params.get("reason") == "compromised" else "node_unlink"
    link.revoke(session, st.ctx, caller.link, by=caller.user.id, reason=reason)
    _metric("unlink", "ok")
    return JSONResponse({"link_id": caller.link.id, "status": caller.link.status}, headers=NO_STORE)


@router.post("/api/v2/link/contributions", tags=["link"])
async def contributions_post(request: Request, st: St, session: Sess):
    caller = _caller(request, st, session, "contributions")
    if isinstance(caller, JSONResponse):
        return caller
    try:
        body = await request.json()
    except ValueError:
        body = None
    try:
        items = link.contributions(session, st.ctx, caller, body.get("items") if isinstance(body, dict) else None)
    except AtlasError as exc:
        return _atlas_error("contributions", exc)
    _metric("contributions", "ok")
    return JSONResponse({"items": items}, headers=NO_STORE)


@router.get("/api/v2/link/contributions", tags=["link"])
def contributions_get(request: Request, st: St, session: Sess, since: str | None = None):
    caller = _caller(request, st, session, "status")
    if isinstance(caller, JSONResponse):
        return caller
    try:
        out = link.contribution_status(session, caller.link, (since or "")[:120] or None)
    except AtlasError as exc:
        return _atlas_error("status", exc)
    _metric("status", "ok")
    return JSONResponse(out, headers=NO_STORE)


@router.post("/api/v2/link/contributions/{contribution_id:path}/withdraw", tags=["link"])
def contribution_withdraw(contribution_id: str, request: Request, st: St, session: Sess):
    caller = _caller(request, st, session, "withdraw")
    if isinstance(caller, JSONResponse):
        return caller
    try:
        out = link.withdraw(session, caller, contribution_id[:80])
    except AtlasError as exc:
        return _atlas_error("withdraw", exc)
    _metric("withdraw", "ok")
    return JSONResponse(out, headers=NO_STORE)


@router.post("/api/v2/link/rotation", tags=["link"])
async def rotation(request: Request, st: St, session: Sess):
    caller = _caller(request, st, session, "rotation")
    if isinstance(caller, JSONResponse):
        return caller
    try:
        body = await request.json()
    except ValueError:
        body = None
    try:
        out = link.rotate(session, st.ctx, caller, body)
    except AtlasError as exc:
        return _atlas_error("rotation", exc)  # a fork's freeze is committed with this answer
    _metric("rotation", "ok")
    return JSONResponse(out, headers=NO_STORE)
