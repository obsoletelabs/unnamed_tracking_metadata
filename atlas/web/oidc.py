"""OIDC web endpoints: provider, relying party and their admin pages."""

from __future__ import annotations

import base64
import re
import time
from typing import Annotated
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import accounts, instruments, oidc, ratelimit, secrets_box
from ..api.deps import AppState, get_session, state
from ..ids import ulid
from ..models import OidcAccess, OidcClient, OidcIssuer, OidcLink, User
from ..service import AtlasError
from . import CSP
from .deps import (
    LOCAL_ADMIN_ONLY,
    SESSION_COOKIE,
    admin_form,
    admin_page,
    client_ip,
    go,
    page,
    secure_cookies,
    user_form,
    user_page,
)
from .deps import Page as Ctx
from .federation import clean_url, fingerprint
from .routes import render, safe_next

router = APIRouter()
St = Annotated[AppState, Depends(state)]
Sess = Annotated[Session, Depends(get_session, scope="function")]


def _provider_on(p: Ctx) -> None:
    if not p.st.ctx.settings.oidc_provider_enabled:
        raise HTTPException(404, "This server is not an OpenID provider.")


# ---- provider -----------------------------------------------------------------------------------------

@router.get("/.well-known/openid-configuration")
def discovery(st: St):
    if not st.ctx.settings.oidc_provider_enabled:
        raise HTTPException(404, "This server is not an OpenID provider.")
    return JSONResponse(oidc.discovery(st.ctx), headers={"Cache-Control": "public, max-age=300"})


@router.get("/oidc/jwks")
def jwks(st: St, session: Sess):
    if not st.ctx.settings.oidc_provider_enabled:
        raise HTTPException(404, "This server is not an OpenID provider.")
    return JSONResponse(oidc.jwks(session, st.ctx), headers={"Cache-Control": "public, max-age=300"})


AUTH_KEYS = ("response_type", "client_id", "redirect_uri", "scope", "state", "nonce", "code_challenge",
             "code_challenge_method")


def _params(source) -> dict[str, str]:
    return {k: str(source.get(k, ""))[:600] for k in AUTH_KEYS}


def _consent_response(response, redirect_uri: str):
    """Allow the selected, already validated client origin through the form redirect.

    Browsers also enforce form-action on the POST's redirect response. Other pages
    keep the site's self-only policy; authorization still requires the exact URI
    registered for this client. Quoting the authority prevents CSP source injection.
    """
    parts = urlsplit(redirect_uri)
    origin = parts.scheme + "://" + quote(parts.netloc, safe=":[]")
    response.headers["Content-Security-Policy"] = CSP.replace(
        "form-action 'self';", f"form-action 'self' {origin};", 1)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/oidc/authorize", response_class=HTMLResponse)
def authorize(request: Request, p: Annotated[Ctx, Depends(user_page)]):
    _provider_on(p)
    params = _params(request.query_params)
    started = time.monotonic()
    try:
        client, scope = oidc.check_authorize(p.session, params)
    except AtlasError as exc:
        instruments.oidc_consent.inc(decision="invalid_request")
        instruments.oidc_stage.observe(time.monotonic() - started, stage="authorize", outcome="error")
        raise HTTPException(400, f"Cannot sign you in: {exc}") from exc
    instruments.oidc_stage.observe(time.monotonic() - started, stage="authorize", outcome="ok")
    return _consent_response(
        render(p, "oidc_consent.html", client=client, params=params, scopes=scope.split()), params["redirect_uri"])


@router.post("/oidc/authorize")
async def authorize_decide(request: Request, p: Annotated[Ctx, Depends(user_form)]):
    _provider_on(p)
    form = await request.form()
    params = _params(form)
    try:
        client, scope = oidc.check_authorize(p.session, params)
    except AtlasError as exc:
        instruments.oidc_consent.inc(decision="invalid_request")
        raise HTTPException(400, f"Cannot sign you in: {exc}") from exc
    sep = "&" if "?" in params["redirect_uri"] else "?"
    if form.get("decision") != "allow":
        q = {"error": "access_denied"}
        instruments.oidc_consent.inc(decision="denied")
    else:
        q = {"code": oidc.issue_code(p.session, client, p.me, params, scope)}
        accounts.audit(p.session, p.me.id, "oidc_consent", client.client_id)
        instruments.oidc_consent.inc(decision="allowed")
        instruments.token_operations.inc(token="authorization_code", operation="issue", outcome="ok")
    if params["state"]:
        q["state"] = params["state"]
    return _consent_response(
        RedirectResponse(params["redirect_uri"] + sep + urlencode(q), status_code=303), params["redirect_uri"])


@router.get("/me/apps", response_class=HTMLResponse)
def my_apps(p: Annotated[Ctx, Depends(user_page)]):
    """Apps currently signed in with this account through Atlas, with a way to end them."""
    _provider_on(p)
    rows = p.session.execute(
        select(OidcClient.client_id, OidcClient.name, func.count(OidcAccess.id), func.max(OidcAccess.expires_at))
        .join(OidcAccess, OidcAccess.client_id == OidcClient.client_id)
        .where(OidcAccess.user_id == p.me.id, OidcAccess.expires_at > func.now())
        .group_by(OidcClient.client_id, OidcClient.name)).all()
    return render(p, "me_apps.html", apps=[{"id": r[0], "name": r[1], "sessions": r[2], "until": r[3]} for r in rows])


@router.post("/me/apps/{client_id}/revoke")
def revoke_app(client_id: str, p: Annotated[Ctx, Depends(user_form)]):
    _provider_on(p)
    p.session.execute(delete(OidcAccess).where(OidcAccess.user_id == p.me.id, OidcAccess.client_id == client_id))
    accounts.audit(p.session, p.me.id, "oidc_revoke", client_id)
    instruments.token_operations.inc(token="access_token", operation="revoke", outcome="ok")
    return go("/me/apps", "revoked", p)


def _client_credentials(request: Request, form) -> tuple[str, str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            return cid, secret
        except (ValueError, UnicodeDecodeError):
            return "", ""
    return str(form.get("client_id", "")), str(form.get("client_secret", ""))


@router.post("/oidc/token")
async def token(request: Request, st: St, session: Sess):
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if not st.ctx.settings.oidc_provider_enabled:
        return JSONResponse({"error": "unsupported"}, status_code=404, headers=headers)
    started, outcome = time.monotonic(), "invalid"
    try:
        response, outcome = await _token(request, st, session, headers)
        return response
    finally:
        if outcome != "rate_limited":  # a throttled request never reached the exchange
            instruments.token_operations.inc(token="authorization_code", operation="exchange", outcome=outcome)
        instruments.oidc_stage.observe(time.monotonic() - started, stage="token",
                                       outcome="ok" if outcome == "ok" else "error")


async def _token(request: Request, st: AppState, session: Session, headers: dict[str, str]):
    ip = request.client.host if request.client else "?"
    form = await request.form()
    cid, secret = _client_credentials(request, form)
    client = oidc.authenticate_client(session, cid, secret)
    if client is None:  # only failed authentications count against the address
        ok, retry = ratelimit.hit(st.maker, f"oidc:token-fail:{ip}", 30, 60)
        status = 401 if ok else 429
        response = JSONResponse({"error": "invalid_client" if ok else "slow_down"}, status_code=status,
                                headers={**headers, **({} if ok else {"Retry-After": str(retry)})})
        return response, "client_auth_failed" if ok else "rate_limited"
    ok, retry = ratelimit.hit(st.maker, f"oidc:token:{client.client_id}", 600, 60)
    if not ok:
        response = JSONResponse({"error": "slow_down"}, status_code=429, headers={**headers, "Retry-After": str(retry)})
        return response, "rate_limited"
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=headers), "unsupported_grant"
    try:
        out = oidc.exchange_code(session, st.ctx, client, str(form.get("code", "")), str(form.get("redirect_uri", "")),
                                 str(form.get("code_verifier", "")))
    except AtlasError as exc:
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=headers), getattr(
            exc, "metric_reason", "invalid")
    instruments.token_operations.inc(token="access_token", operation="issue", outcome="ok")
    return JSONResponse(out, headers=headers), "ok"


@router.get("/oidc/userinfo")
def userinfo(request: Request, st: St, session: Sess):
    started = time.monotonic()
    auth = request.headers.get("authorization", "")
    claims = oidc.userinfo(session, auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    instruments.oidc_stage.observe(time.monotonic() - started, stage="userinfo",
                                   outcome="ok" if claims is not None else "error")
    if not st.ctx.settings.oidc_provider_enabled or claims is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return JSONResponse(claims, headers={"Cache-Control": "no-store"})


# ---- relying party ------------------------------------------------------------------------------------

BINDING_COOKIE = "atlas_oidc"


def _bound(resp, binding: str, p: Ctx):
    resp.set_cookie(BINDING_COOKIE, binding, httponly=True, samesite="lax", secure=secure_cookies(p),
                    path="/oidc/callback", max_age=600)
    return resp


@router.get("/oidc/start/{iid}")
def start(iid: str, p: Annotated[Ctx, Depends(page)], next: str = "/"):
    issuer = p.session.get(OidcIssuer, iid)
    if issuer is None or not issuer.enabled:
        raise HTTPException(404, "No such sign-in provider.")
    url, binding = oidc.start_login(p.session, p.st.ctx, issuer, safe_next(next), None)
    return _bound(RedirectResponse(url, status_code=303), binding, p)


@router.post("/me/oidc/{iid}/link")
def link_start(iid: str, p: Annotated[Ctx, Depends(user_form)]):
    issuer = p.session.get(OidcIssuer, iid)
    if issuer is None or not issuer.enabled:
        raise HTTPException(404, "No such sign-in provider.")
    if not (issuer.allow_all_users or p.me.oidc_allowed):
        raise HTTPException(403, "External sign-in is not enabled for your account. Ask an administrator.")
    url, binding = oidc.start_login(p.session, p.st.ctx, issuer, "/me/settings", p.me)
    return _bound(RedirectResponse(url, status_code=303), binding, p)


@router.post("/me/oidc/{iid}/unlink")
def unlink(iid: str, p: Annotated[Ctx, Depends(user_form)]):
    rows = p.session.scalars(select(OidcLink).where(OidcLink.issuer_id == iid, OidcLink.user_id == p.me.id)).all()
    if not rows:
        raise HTTPException(404, "That sign-in is not linked.")
    if p.me.password_hash == "!" and len(p.session.scalars(select(OidcLink).where(OidcLink.user_id == p.me.id)).all()) \
            <= len(rows):
        raise HTTPException(409, "This is your only way to sign in. Set a password first (use 'Forgot password').")
    for r in rows:
        p.session.delete(r)
    accounts.audit(p.session, p.me.id, "oidc_unlink", iid)
    return go("/me/settings", "saved")


@router.get("/oidc/callback/{iid}")
def callback(iid: str, request: Request, p: Annotated[Ctx, Depends(page)]):
    issuer = p.session.get(OidcIssuer, iid)
    if issuer is None or not issuer.enabled:
        raise HTTPException(404, "No such sign-in provider.")
    flow = "oidc_link" if p.user else "oidc_signin"  # a signed-in browser at the callback is linking
    if request.query_params.get("error"):
        instruments.identity_flows.inc(flow=flow, outcome="provider_cancelled")
        raise HTTPException(400, "The provider did not complete the sign-in.")
    ok, retry = ratelimit.hit(p.st.maker, f"oidc:cb:{client_ip(request)}", 30, 300)
    if not ok:
        instruments.identity_flows.inc(flow=flow, outcome="rate_limited")
        raise HTTPException(429, f"Too many attempts. Try again in {retry} seconds.")
    started = time.monotonic()
    try:
        user, next_url, linked = oidc.finish_login(p.session, p.st.ctx, p.st.http, issuer,
                                                   request.query_params.get("code", ""),
                                                   request.query_params.get("state", ""),
                                                   request.cookies.get(BINDING_COOKIE, ""),
                                                   p.user.id if p.user else None)
    except AtlasError as exc:
        p.session.rollback()
        instruments.identity_flows.inc(flow=flow, outcome=getattr(exc, "metric_reason", "verification_failed"))
        instruments.oidc_stage.observe(time.monotonic() - started, stage="callback", outcome="error")
        raise HTTPException(400, str(exc)) from exc
    instruments.identity_flows.inc(flow=flow, outcome="ok")
    instruments.oidc_stage.observe(time.monotonic() - started, stage="callback", outcome="ok")
    secret, _row = accounts.create_session(p.session, p.st.ctx.settings, user)
    accounts.audit(p.session, user.id, "login_oidc", issuer.id)
    resp = go(next_url, "saved" if linked else None)
    resp.delete_cookie(BINDING_COOKIE, path="/oidc/callback")
    resp.set_cookie(SESSION_COOKIE, secret, httponly=True, samesite="lax", secure=secure_cookies(p), path="/",
                    max_age=p.st.ctx.settings.session_ttl_days * 86400)
    return resp


# ---- admin --------------------------------------------------------------------------------------------

@router.get("/admin/oidc", response_class=HTMLResponse)
def admin_oidc(p: Annotated[Ctx, Depends(admin_page)]):
    return _admin(p)


def _admin(p: Ctx, *, new_client: tuple[str, str] | None = None, probe: dict | None = None, error: str | None = None,
           status: int = 200):
    s = p.st.ctx.settings
    clients = list(p.session.scalars(select(OidcClient).order_by(OidcClient.created_at)))
    issuers = []
    for i in p.session.scalars(select(OidcIssuer).order_by(OidcIssuer.created_at)):
        n = p.session.scalar(select(func.count()).select_from(OidcLink).where(OidcLink.issuer_id == i.id)) or 0
        issuers.append({"i": i, "links": n, "fp": fingerprint(i.node_id)})
    return render(p, "admin_oidc.html", status=status, clients=clients, issuers=issuers, provider_on=s.oidc_provider_enabled,
                  issuer_url=s.public_url, can_store=secrets_box.available(s), new_client=new_client, probe=probe,
                  error=error, callback_base=f"{s.public_url}/oidc/callback/")


@router.post("/admin/oidc/clients", response_class=HTMLResponse)
def client_create(p: Annotated[Ctx, Depends(admin_form)], name: Annotated[str, Form(max_length=80)],
                  redirect_uris: Annotated[str, Form(max_length=2000)], require_pkce: Annotated[str, Form()] = "",
                  id_token_alg: Annotated[str, Form()] = "RS256"):
    try:
        row, secret = oidc.create_client(p.session, name, redirect_uris.splitlines(), require_pkce == "on", id_token_alg)
    except AtlasError as exc:
        return _admin(p, error=str(exc), status=400)
    accounts.audit(p.session, p.me.id, "oidc_client_create", row.client_id)
    return _admin(p, new_client=(row.client_id, secret))


@router.post("/admin/oidc/clients/{cid}/toggle")
def client_toggle(cid: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(OidcClient, cid)
    if row is None:
        raise HTTPException(404, "No such client.")
    row.enabled = not row.enabled
    accounts.audit(p.session, p.me.id, "oidc_client_toggle", row.client_id, {"enabled": row.enabled})
    return go("/admin/oidc", "saved")


@router.post("/admin/oidc/clients/{cid}/delete")
def client_delete(cid: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(OidcClient, cid)
    if row is None:
        raise HTTPException(404, "No such client.")
    p.session.execute(OidcClient.__table__.update().where(OidcClient.client_id == cid).values(enabled=False))
    from ..models import OidcAccess, OidcCode

    p.session.execute(OidcCode.__table__.delete().where(OidcCode.client_id == cid))
    p.session.execute(OidcAccess.__table__.delete().where(OidcAccess.client_id == cid))
    p.session.delete(row)
    accounts.audit(p.session, p.me.id, "oidc_client_delete", cid)
    return go("/admin/oidc", "saved")


@router.post("/admin/oidc/issuers/probe", response_class=HTMLResponse)
def issuer_probe(p: Annotated[Ctx, Depends(admin_form)], url: Annotated[str, Form(max_length=300)],
                 name: Annotated[str, Form(max_length=80)] = ""):
    target = clean_url(url)
    try:
        info = oidc.probe_issuer(p.st.http, target)
    except AtlasError as exc:
        return _admin(p, error=str(exc), status=400)
    if p.session.scalars(select(OidcIssuer.id).where(OidcIssuer.issuer_url == info["issuer_url"])).first():
        return _admin(p, error="That provider is already configured.", status=409)
    iid = ulid()  # fixed now so the exact redirect address can be registered on the provider before adding it
    return _admin(p, probe={**info, "fp": fingerprint(info["node_id"]), "name": name.strip(), "iid": iid,
                            "redirect": f"{p.st.ctx.settings.public_url}/oidc/callback/{iid}"})


@router.post("/admin/oidc/issuers")
def issuer_add(p: Annotated[Ctx, Depends(admin_form)], iid: Annotated[str, Form(max_length=26)],
               issuer_url: Annotated[str, Form(max_length=300)],
               name: Annotated[str, Form(max_length=80)], client_id: Annotated[str, Form(max_length=40)],
               client_secret: Annotated[str, Form(max_length=200)], node_id: Annotated[str, Form(max_length=64)],
               public_key: Annotated[str, Form(max_length=100)],
               authorization_endpoint: Annotated[str, Form(max_length=400)],
               token_endpoint: Annotated[str, Form(max_length=400)], auto_create: Annotated[str, Form()] = "",
               allow_all_users: Annotated[str, Form()] = "", trust_email: Annotated[str, Form()] = ""):
    if (auto_create == "on" or trust_email == "on") and not p.is_local_admin:
        raise HTTPException(403, LOCAL_ADMIN_ONLY)  # C10 §6.3
    base = clean_url(issuer_url)
    try:
        enc = secrets_box.encrypt(p.st.ctx.settings, client_secret)
        ok = oidc.key_id_for(base64.b64decode(public_key)) == node_id
    except (secrets_box.SecretsError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok or not (authorization_endpoint.startswith(base + "/") and token_endpoint.startswith(base + "/")):
        raise HTTPException(400, "The provider details do not check out. Look the provider up again.")
    if not client_id.strip() or not client_secret.strip():
        raise HTTPException(400, "Enter the client id and secret you got from the provider.")
    if not re.fullmatch(r"[0-9A-Za-z]{26}", iid) or p.session.get(OidcIssuer, iid) is not None:
        raise HTTPException(400, "Look the provider up again.")
    p.session.add(OidcIssuer(id=iid, name=name.strip() or base, issuer_url=base, client_id=client_id.strip(),
                             client_secret_enc=enc, node_id=node_id, public_key=public_key,
                             authorization_endpoint=authorization_endpoint, token_endpoint=token_endpoint,
                             auto_create=auto_create == "on", allow_all_users=allow_all_users == "on",
                             trust_email=trust_email == "on"))
    accounts.audit(p.session, p.me.id, "oidc_issuer_add", base)
    return go("/admin/oidc", "saved")


@router.post("/admin/oidc/issuers/{iid}")
def issuer_update(iid: str, p: Annotated[Ctx, Depends(admin_form)], enabled: Annotated[str, Form()] = "",
                  auto_create: Annotated[str, Form()] = "", allow_all_users: Annotated[str, Form()] = "",
                  trust_email: Annotated[str, Form()] = ""):
    row = p.session.get(OidcIssuer, iid)
    if row is None:
        raise HTTPException(404, "No such provider.")
    if ((auto_create == "on" and not row.auto_create) or (trust_email == "on" and not row.trust_email)) \
            and not p.is_local_admin:
        raise HTTPException(403, LOCAL_ADMIN_ONLY)  # C10 §6.3: enabling either is for local administrators
    row.enabled, row.auto_create = enabled == "on", auto_create == "on"
    row.allow_all_users, row.trust_email = allow_all_users == "on", trust_email == "on"
    accounts.audit(p.session, p.me.id, "oidc_issuer_update", iid)
    return go("/admin/oidc", "saved")


@router.post("/admin/oidc/issuers/{iid}/delete")
def issuer_delete(iid: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(OidcIssuer, iid)
    if row is None:
        raise HTTPException(404, "No such provider.")
    from ..models import OidcState

    p.session.execute(OidcLink.__table__.delete().where(OidcLink.issuer_id == iid))
    p.session.execute(OidcState.__table__.delete().where(OidcState.issuer_id == iid))
    p.session.delete(row)
    accounts.audit(p.session, p.me.id, "oidc_issuer_delete", iid)
    return go("/admin/oidc", "saved")


@router.post("/admin/users/{uid}/oidc")
def user_oidc(uid: str, p: Annotated[Ctx, Depends(admin_form)], allowed: Annotated[str, Form()] = ""):
    u = p.session.get(User, uid)
    if u is None:
        raise HTTPException(404, "No such user.")
    u.oidc_allowed = allowed == "on"
    accounts.audit(p.session, p.me.id, "oidc_allow", uid, {"allowed": u.oidc_allowed})
    return go("/admin/users", "saved")
