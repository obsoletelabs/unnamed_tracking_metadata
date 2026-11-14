from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import accounts, mfa, ratelimit
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
    "voted": "Vote saved.", "artretry": "Download retry scheduled. The images are fetched in the background shortly.",
    "artnone": "No uncached images to retry.", "reset": "Your password was changed. Sign in with the new one.",
    "relation-added": "Relation added.", "relation-removed": "Relation removed.",
    # C13 §7.3: what the first pull after adding a peer did (its badge is on the peer's row)
    "peer_following": "Peer added and synced once: its status is below.",
    "peer_not_contacted": "Peer added. It did not answer in time, so it shows Not contacted yet; the next sync tries "
                          "again.",
    "peer_failed": "Peer added, but its first sync failed: the reason is next to it below.",
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
    rights: accounts.Rights | None = None  # C10 §6.3, computed once per request in `page`

    @property
    def me(self) -> User:
        """The signed-in user; only used on routes that already required one."""
        assert self.user is not None
        return self.user

    @property
    def can_review(self) -> bool:
        return bool(self.user and self.rights and self.rights.review)

    @property
    def can_moderate(self) -> bool:
        return bool(self.user and self.rights and self.rights.moderate)

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.rights and self.rights.admin)

    @property
    def is_local_admin(self) -> bool:
        """An administrator by the local role, in a session where that role applies: the only one who may change
        group policy, rotate keys, mint admin tokens or touch another administrator (C10 §6.3)."""
        return bool(self.user and self.rights and self.rights.local_admin)

    @property
    def flash(self) -> str | None:
        return FLASH.get(self.request.query_params.get("m", ""))


def page(request: Request, st: Annotated[AppState, Depends(state)],
         session: Annotated[Session, Depends(get_session, scope="function")]) -> Page:
    found = accounts.session_user(session, request.cookies.get(SESSION_COOKIE))
    if found and found[1].via_group is not None:
        from .. import group_signin

        if not group_signin.session_still_valid(session, st.ctx.settings, st.ctx.identity, found[0], found[1]):
            found = None  # checked on every request (§5.6): the session was deleted, the visitor is anonymous
    if found:
        user, login = found
        allowed_now = mfa.access_allowed(session, st.ctx.settings, user, login)
        rights = accounts.effective_rights(session, st.ctx.settings, user, login, st.ctx.identity.node_id,
                                           mfa_ok=allowed_now)
        accounts.pin_rights(session, user, rights, _provenance(session, login, rights))
        if not allowed_now:
            from ..models import UserMfa

            factor = session.get(UserMfa, user.id)
            enrolled = bool(factor and factor.enabled_at)
            allowed = ("/mfa", "/logout") if enrolled else ("/me/security", "/me/security/start",
                                                          "/me/security/enable", "/logout")
            if request.url.path not in allowed:
                raise MfaRequired("/mfa" if enrolled else "/me/security")
        return Page(request, st, session, user, login, login.csrf, rights=rights)
    token = request.cookies.get(ANON_CSRF_COOKIE) or ""
    fresh = None
    if len(token) < 20:
        token = fresh = secrets.token_urlsafe(24)
    return Page(request, st, session, None, None, token, new_cookie=fresh)


def _provenance(session: Session, login: UserSession, rights: accounts.Rights) -> dict | None:
    if not rights.federated and not rights.delegated:
        return None
    from .. import groups

    group = groups.load(session)
    return accounts.audit_provenance(login.via_group or (group.group_id if group else ""), login, rights)


def check_csrf(p: Page, token: str | None) -> None:
    if not token or not hmac.compare_digest(p.csrf, token):
        raise HTTPException(403, "Invalid or missing form token. Reload the page and try again.")


def csrf_form(p: Annotated[Page, Depends(page)], csrf: Annotated[str, Form()] = "") -> Page:
    """Dependency for every POST: verifies the form token and returns the page context."""
    check_csrf(p, csrf)
    return p


class LoginRequired(Exception):
    pass


class MfaRequired(Exception):
    def __init__(self, target: str):
        self.target = target


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


def moderate_page(p: Annotated[Page, Depends(user_page)]) -> Page:
    """The report queue (C10 §6.2, H17): reviewers and holders of a `moderator` grant."""
    if not p.can_moderate:
        raise HTTPException(403, "Moderator rights are required.")
    return p


def moderate_form(p: Annotated[Page, Depends(user_form)]) -> Page:
    if not p.can_moderate:
        raise HTTPException(403, "Moderator rights are required.")
    return p


LOCAL_ADMIN_ONLY = "Only an administrator of this node, signed in here, may do this. Rights from a group grant " \
                   "never reach it (C10 §6.3)."


def local_admin_page(p: Annotated[Page, Depends(user_page)]) -> Page:
    """`local_admin_only` (C10 §6.3): group policy, root and recovery, key rotation, admin tokens, other admins."""
    if not p.is_local_admin:
        raise HTTPException(403, LOCAL_ADMIN_ONLY)
    return p


def local_admin_form(p: Annotated[Page, Depends(user_form)]) -> Page:
    if not p.is_local_admin:
        raise HTTPException(403, LOCAL_ADMIN_ONLY)
    return p


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def throttle(p: Page, key: str, limit: int, window: int, what: str = "Too many attempts") -> None:
    ok, retry = ratelimit.hit(p.st.maker, key, limit, window)
    if not ok:
        raise HTTPException(429, f"{what}. Try again in {retry} seconds.", headers={"Retry-After": str(retry)})


def go(url: str, flash: str | None = None, p: Page | None = None) -> RedirectResponse:
    target, anchor, fragment = url.partition("#")
    if flash:
        target += ("&" if "?" in target else "?") + "m=" + flash
    resp = RedirectResponse(target + anchor + fragment, status_code=303)
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
