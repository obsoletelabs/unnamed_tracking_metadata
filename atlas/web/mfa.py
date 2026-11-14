"""Second-factor setup and challenges; primary authentication never grants MFA assurance."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import delete

from .. import accounts, mfa, secrets_box
from ..models import UserMfa, UserSession, now
from ..service import AtlasError
from .deps import SESSION_COOKIE, Page, client_ip, go, secure_cookies, throttle, user_form, user_page
from .routes import render, when

router = APIRouter()


def _reauth(p: Page, password: str) -> None:
    throttle(p, f"mfa:manage:{p.me.id}", 10, 900)
    if not accounts.reauthenticated(p.me, password, p.login):
        if p.login.via_group is not None and p.me.password_hash == "!":
            raise HTTPException(403, "You signed in through your node group. Sign in on this node itself to "
                                "change two-factor settings.")
        raise HTTPException(400, "Confirm your current password, or sign in again with your linked provider.")


def _attempt(p: Page, code: str, *, pending: bool = False) -> bool:
    throttle(p, f"mfa:user:{p.me.id}", 10, 300)
    throttle(p, f"mfa:ip:{client_ip(p.request)}", 30, 300)
    try:
        return mfa.verify(p.session, p.st.ctx.settings, p.me.id, code, pending=pending, login_id=p.login.id)
    except secrets_box.SecretsError as exc:
        raise HTTPException(503, "Two-factor verification is unavailable. "
                            "Ask the operator to check ATLAS_SECRET_KEY.") from exc


def _security(p: Page, *, codes: list[str] | None = None, error: str | None = None, status: int = 200):
    factor = p.session.get(UserMfa, p.me.id)
    secret = None
    if (factor and factor.enabled_at is None and factor.setup_session_id == p.login.id
            and mfa._utc(factor.created_at) >= now() - timedelta(minutes=10)):
        try:
            secret = secrets_box.decrypt(p.st.ctx.settings, factor.secret_enc)
        except secrets_box.SecretsError:
            error = "The setup key is unavailable. Ask the operator to check ATLAS_SECRET_KEY."
    from .. import group_channel, group_signin, groups

    group = groups.active(p.session)
    linked = group is not None and group_channel.local_link(p.session, group.group_id, p.me.id) is not None
    return render(p, "security.html", status=status, factor=factor, setup_secret=secret, codes=codes,
                  error=error, required=mfa.required(p.st.ctx.settings, p.me),
                  available=secrets_box.available(p.st.ctx.settings), has_password=p.me.password_hash != "!",
                  linked=linked, activity=group_signin.activity(p.session, p.me) if group is not None else [],
                  when=when)


@router.get("/me/security", response_class=HTMLResponse)
def security(p: Annotated[Page, Depends(user_page)]):
    return _security(p)


@router.post("/me/security/start", response_class=HTMLResponse)
def start(p: Annotated[Page, Depends(user_form)], password: Annotated[str, Form(max_length=256)] = ""):
    _reauth(p, password)
    try:
        mfa.start(p.session, p.st.ctx.settings, p.me, p.login.id)
    except (AtlasError, secrets_box.SecretsError) as exc:
        return _security(p, error=str(exc), status=400)
    accounts.audit(p.session, p.me.id, "mfa_setup", p.me.id)
    return _security(p)


@router.post("/me/security/enable", response_class=HTMLResponse)
def enable(p: Annotated[Page, Depends(user_form)], code: Annotated[str, Form(max_length=64)]):
    if not _attempt(p, code, pending=True):
        return _security(p, error="That code is invalid, already used, or setup expired. Try the next code or start again.",
                         status=400)
    factor = p.session.get(UserMfa, p.me.id)
    factor.enabled_at = now()
    codes = mfa.recovery_codes(factor)
    p.login.mfa_verified_at = now()
    if p.login.via_group is not None and p.me.password_hash != "!":
        # this node's own factor, enrolled in a federated session after the local password was confirmed (C10 §5.5);
        # a password-less account cannot reach here (`_reauth` refuses federated sessions without a password)
        p.login.amr = [*(p.login.amr or []), accounts.LOCAL_OTP]
    p.session.execute(delete(UserSession).where(UserSession.user_id == p.me.id, UserSession.id != p.login.id))
    accounts.audit(p.session, p.me.id, "mfa_enabled", p.me.id)
    return _security(p, codes=codes)


@router.get("/mfa", response_class=HTMLResponse)
def challenge(p: Annotated[Page, Depends(user_page)]):
    factor = p.session.get(UserMfa, p.me.id)
    if not factor or not factor.enabled_at:
        return go("/me/security")
    return render(p, "mfa.html", error=None)


@router.post("/mfa", response_class=HTMLResponse)
def confirm(p: Annotated[Page, Depends(user_form)], code: Annotated[str, Form(max_length=64)]):
    if not _attempt(p, code):
        return render(p, "mfa.html", status=400,
                      error="That code is invalid or already used. Try the next code or a recovery code.")
    old_id = p.login.id
    secret, login = accounts.create_session(p.session, p.st.ctx.settings, p.me)
    login.mfa_verified_at = now()
    if p.login.via_group is not None:  # keep the provenance; mark this node's own second factor (C10 §5.5)
        login.via_group, login.origin_node, login.assertion_id = (p.login.via_group, p.login.origin_node,
                                                                   p.login.assertion_id)
        login.amr = [*(a for a in p.login.amr or [] if a != accounts.LOCAL_OTP), accounts.LOCAL_OTP]
    p.session.execute(delete(UserSession).where(UserSession.id == old_id))
    accounts.audit(p.session, p.me.id, "mfa_verified", p.me.id)
    response = go("/me")
    response.set_cookie(SESSION_COOKIE, secret, httponly=True, samesite="lax", secure=secure_cookies(p), path="/",
                        max_age=p.st.ctx.settings.session_ttl_days * 86400)
    return response


@router.post("/me/security/disable")
def disable(p: Annotated[Page, Depends(user_form)], code: Annotated[str, Form(max_length=64)],
            password: Annotated[str, Form(max_length=256)] = ""):
    _reauth(p, password)
    if mfa.required(p.st.ctx.settings, p.me):
        raise HTTPException(409, "This server requires two-factor sign-in for your role.")
    if not _attempt(p, code):
        return _security(p, error="That code is invalid or already used.", status=400)
    p.session.delete(p.session.get(UserMfa, p.me.id))
    p.session.execute(delete(UserSession).where(UserSession.user_id == p.me.id, UserSession.id != p.login.id))
    accounts.audit(p.session, p.me.id, "mfa_disabled", p.me.id)
    return go("/me/security", "saved")


@router.post("/me/security/recovery", response_class=HTMLResponse)
def regenerate(p: Annotated[Page, Depends(user_form)], code: Annotated[str, Form(max_length=64)],
               password: Annotated[str, Form(max_length=256)] = ""):
    _reauth(p, password)
    if not _attempt(p, code):
        return _security(p, error="That code is invalid or already used.", status=400)
    factor = p.session.get(UserMfa, p.me.id)
    codes = mfa.recovery_codes(factor)
    accounts.audit(p.session, p.me.id, "mfa_recovery_rotated", p.me.id)
    return _security(p, codes=codes)


@router.post("/me/security/signout-all")
def signout_all(p: Annotated[Page, Depends(user_form)]):
    """C10 §5.6 "Sign out of all nodes": every session here ends, and linked nodes are told (each one honours it
    only where this user's consent there allows)."""
    from .. import group_signin

    throttle(p, f"signout-all:{p.me.id}", 10, 3600)
    group_signin.signout_all(p.session, p.st.ctx.settings, p.st.ctx.identity, p.me)
    response = go("/", "bye")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
