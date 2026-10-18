"""Email verification, password reset, email change, outbox."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from .. import accounts, mail
from ..models import EmailToken, Outbox, User, now
from ..service import AtlasError
from .deps import (
    SESSION_COOKIE,
    admin_form,
    admin_page,
    client_ip,
    csrf_form,
    go,
    page,
    secure_cookies,
    throttle,
    user_form,
)
from .deps import Page as Ctx
from .routes import render, when

router = APIRouter()
GENERIC = "If an account with that address exists, we have sent it a message."


def _no_referrer(resp: Response) -> Response:
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- verification -------------------------------------------------------------------------------------

@router.get("/verify/{secret}", response_class=HTMLResponse)
def verify_page(secret: str, p: Annotated[Ctx, Depends(page)]):
    """Only renders a button: a mail scanner pre-fetching the link must not confirm anything."""
    ok = mail.lookup_token(p.session, secret, "verify") is not None
    return _no_referrer(render(p, "verify.html", anon_csrf=True, secret=secret, valid=ok))


@router.post("/verify/{secret}")
def verify_confirm(secret: str, p: Annotated[Ctx, Depends(csrf_form)]):
    throttle(p, f"verify:ip:{client_ip(p.request)}", 20, 3600, "Too many attempts")
    try:
        mail.consume_verification(p.session, secret)
    except AtlasError as exc:
        p.session.rollback()
        return _no_referrer(render(p, "verify.html", status=400, anon_csrf=True, secret=secret, valid=False,
                                   error=str(exc)))
    return go("/me" if p.user else "/login", "verified")


@router.post("/me/verify")
def resend(p: Annotated[Ctx, Depends(user_form)]):
    throttle(p, f"verify:user:{p.me.id}", 3, 3600, "Too many verification emails requested")
    if p.me.email_verified_at is not None:
        return go("/me", "verified")
    if not p.me.email:
        raise HTTPException(400, "Add an email address first.")
    mail.send_verification(p.session, p.st.ctx.settings, p.me)
    return go("/me", "mailsent")


@router.post("/me/email")
def change_email(p: Annotated[Ctx, Depends(user_form)], email: Annotated[str, Form(max_length=254)],
                 current: Annotated[str, Form(max_length=256)] = ""):
    throttle(p, f"email:user:{p.me.id}", 5, 3600, "Too many attempts")
    s = p.st.ctx.settings
    if not accounts.reauthenticated(p.me, current, p.login):
        raise HTTPException(400, "Your current password is wrong (or sign in again first).")
    new = email.strip().lower()
    if not accounts.EMAIL.match(new):
        raise HTTPException(400, "That does not look like an email address.")
    if new == p.me.email:
        return go("/me", "nochange")
    taken = p.session.scalars(select(User.id).where(User.email == new)).first()
    if taken:
        raise HTTPException(409, "That address is already used by another account.")
    old = p.me.email
    p.me.email, p.me.email_verified_at = new, None
    if old:
        mail.notify_email_changed(p.session, s, p.me, old)
    if s.verification_mode() != "off":
        mail.send_verification(p.session, s, p.me)
    accounts.audit(p.session, p.me.id, "email_change", p.me.id)
    return go("/me", "mailsent")


# ---- password reset -----------------------------------------------------------------------------------

@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(p: Annotated[Ctx, Depends(page)]):
    return render(p, "forgot.html", anon_csrf=True, sent=False)


@router.post("/forgot", response_class=HTMLResponse)
def forgot(p: Annotated[Ctx, Depends(csrf_form)], who: Annotated[str, Form(max_length=254)]):
    throttle(p, f"forgot:ip:{client_ip(p.request)}", 5, 3600, "Too many reset requests")
    key = who.strip().lower()[:254]
    throttle(p, f"forgot:target:{key}", 3, 3600, "Too many reset requests for this account")
    user = p.session.scalars(select(User).where((User.username == key) | (User.email == key))).first()
    if user is not None and user.status == "active":
        mail.send_reset(p.session, p.st.ctx.settings, user)
    # The same answer whether or not the account exists.
    return render(p, "forgot.html", anon_csrf=True, sent=True)


@router.get("/reset/{secret}", response_class=HTMLResponse)
def reset_form(secret: str, p: Annotated[Ctx, Depends(page)]):
    ok = mail.lookup_token(p.session, secret, "reset") is not None
    return _no_referrer(render(p, "reset.html", anon_csrf=True, secret=secret, valid=ok, error=None,
                               min_len=p.st.ctx.settings.password_min_length))


@router.post("/reset/{secret}")
def reset(secret: str, p: Annotated[Ctx, Depends(csrf_form)], password: Annotated[str, Form(max_length=256)]):
    throttle(p, f"reset:ip:{client_ip(p.request)}", 10, 3600, "Too many attempts")
    s = p.st.ctx.settings
    row = mail.lookup_token(p.session, secret, "reset")
    if row is None:
        return _no_referrer(render(p, "reset.html", status=400, anon_csrf=True, secret=secret, valid=False, error=None,
                                   min_len=s.password_min_length))
    if not s.password_min_length <= len(password) <= 200:
        return _no_referrer(render(p, "reset.html", status=400, anon_csrf=True, secret=secret, valid=True,
                                   error=f"The password must be {s.password_min_length}-200 characters.",
                                   min_len=s.password_min_length))
    user = p.session.get(User, row.user_id)
    if user is None or user.status != "active" or user.email != row.email:
        raise HTTPException(400, "This reset link is no longer valid.")
    user.password_hash = accounts.hash_password(password)
    if user.email_verified_at is None:  # they just proved they control the address
        user.email_verified_at = now()
    row.used_at = now()
    p.session.execute(EmailToken.__table__.update().where(EmailToken.user_id == user.id, EmailToken.purpose == "reset",
                                                          EmailToken.used_at.is_(None)).values(used_at=now()))
    accounts.end_all_sessions(p.session, user.id)
    accounts.audit(p.session, user.id, "password_reset", user.id)
    resp = go("/login", "reset")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    _ = secure_cookies
    return resp


# ---- admin: outbox and manual verification ------------------------------------------------------------

@router.get("/admin/outbox", response_class=HTMLResponse)
def outbox(p: Annotated[Ctx, Depends(admin_page)]):
    s = p.st.ctx.settings
    rows = p.session.scalars(select(Outbox).order_by(Outbox.created_at.desc()).limit(50)).all()
    pending = p.session.scalar(select(func.count()).select_from(Outbox).where(Outbox.sent_at.is_(None))) or 0
    return render(p, "admin_outbox.html", rows=rows, when=when, backend=s.mail_backend,
                  smtp_ready=bool(s.smtp_host), pending=pending)


@router.post("/admin/users/{uid}/verify")
def admin_verify(uid: str, p: Annotated[Ctx, Depends(admin_form)]):
    u = p.session.get(User, uid)
    if u is None:
        raise HTTPException(404, "No such user.")
    u.email_verified_at = now()
    accounts.audit(p.session, p.me.id, "verify_email", u.id)
    return go("/admin", "saved")
