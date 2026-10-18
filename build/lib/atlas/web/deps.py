from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import accounts, ratelimit
from ..api.deps import AppState, get_session, state
from ..models import User, UserSession

SESSION_COOKIE = "atlas_session"
ANON_CSRF_COOKIE = "atlas_csrf"

FLASH = {
    "saved": "Saved.", "nochange": "Nothing changed.", "proposed": "Thanks. Your proposal is waiting for review.",
    "applied": "Your change was applied immediately.", "approved": "Approved.", "rejected": "Rejected.",
    "reverted": "Reverted.", "merged": "Merged.", "locked": "Field locked.", "unlocked": "Lock removed.",
    "withdrawn": "Proposal withdrawn.", "dismissed": "Dismissed.", "detached": "Split into its own record.",
    "token": "Token created. Copy it now; it will not be shown again.", "revoked": "Token revoked.",
    "password": "Password changed. Other sessions were signed out.", "bye": "Signed out.",
    "verified": "Your email address is confirmed.", "mailsent": "We sent you an email. Check your inbox.",
    "reset": "Your password was changed. Sign in with the new one.",
}


@dataclass
class Page:
    """Everything a template needs about the current request."""

    request: Request
    st: AppState
    session: Session
    user: User | None
    login: UserSession | None
    csrf: str
    new_cookie: str | None = None  # anonymous CSRF token to set

    @property
    def me(self) -> User:
        """The signed-in user; only used on routes that already required one."""
        assert self.user is not None
        return self.user

    @property
    def can_review(self) -> bool:
        return bool(self.user and accounts.can_review(self.st.ctx.settings, self.user))

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user.role == "admin")

    @property
    def flash(self) -> str | None:
        return FLASH.get(self.request.query_params.get("m", ""))


def page(request: Request, st: Annotated[AppState, Depends(state)],
         session: Annotated[Session, Depends(get_session, scope="function")]) -> Page:
    found = accounts.session_user(session, request.cookies.get(SESSION_COOKIE))
    if found:
        return Page(request, st, session, found[0], found[1], found[1].csrf)
    token = request.cookies.get(ANON_CSRF_COOKIE) or ""
    fresh = None
    if len(token) < 20:
        token = fresh = secrets.token_urlsafe(24)
    return Page(request, st, session, None, None, token, new_cookie=fresh)


def check_csrf(p: Page, token: str | None) -> None:
    if not token or not hmac.compare_digest(p.csrf, token):
        raise HTTPException(403, "Invalid or missing form token. Reload the page and try again.")


def csrf_form(p: Annotated[Page, Depends(page)], csrf: Annotated[str, Form()] = "") -> Page:
    """Dependency for every POST: verifies the form token and returns the page context."""
    check_csrf(p, csrf)
    return p


class LoginRequired(Exception):
    pass


def user_page(p: Annotated[Page, Depends(page)]) -> Page:
    if p.user is None:
        raise LoginRequired
    return p


def user_form(p: Annotated[Page, Depends(csrf_form)]) -> Page:
    if p.user is None:
        raise LoginRequired
    return p


def review_page(p: Annotated[Page, Depends(user_page)]) -> Page:
    if not p.can_review:
        raise HTTPException(403, "Review rights are required.")
    return p


def review_form(p: Annotated[Page, Depends(user_form)]) -> Page:
    if not p.can_review:
        raise HTTPException(403, "Review rights are required.")
    return p


def admin_page(p: Annotated[Page, Depends(user_page)]) -> Page:
    if not p.is_admin:
        raise HTTPException(403, "Administrators only.")
    return p


def admin_form(p: Annotated[Page, Depends(user_form)]) -> Page:
    if not p.is_admin:
        raise HTTPException(403, "Administrators only.")
    return p


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def throttle(p: Page, key: str, limit: int, window: int, what: str = "Too many attempts") -> None:
    ok, retry = ratelimit.hit(p.st.maker, key, limit, window)
    if not ok:
        raise HTTPException(429, f"{what}. Try again in {retry} seconds.", headers={"Retry-After": str(retry)})


def go(url: str, flash: str | None = None, p: Page | None = None) -> RedirectResponse:
    resp = RedirectResponse(url + (("&" if "?" in url else "?") + "m=" + flash if flash else ""), status_code=303)
    if p is not None and p.new_cookie:
        set_anon_cookie(resp, p)
    return resp


def secure_cookies(p: Page) -> bool:
    return p.st.ctx.settings.public_url.startswith("https://")


def set_anon_cookie(resp, p: Page) -> None:
    resp.set_cookie(ANON_CSRF_COOKIE, p.csrf, httponly=True, samesite="lax", secure=secure_cookies(p), path="/",
                    max_age=86400)


async def form_dict(request: Request) -> dict[str, str]:
    """All single-valued form fields (for forms whose inputs depend on admin-defined fields)."""
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}
