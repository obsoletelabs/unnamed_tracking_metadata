from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from .. import accounts, auth, custom, facets, feed, mail, proposals, service, social
from ..api.routes import _guard_upstream, media_response
from ..merge import SCALAR_FIELDS, UNION_FIELDS
from ..models import (
    Artwork,
    AuditEntry,
    Canonical,
    Follow,
    ListItem,
    Lock,
    MediaType,
    Observation,
    OidcIssuer,
    OidcLink,
    Proposal,
    ReviewItem,
    SyncCursor,
    Token,
    User,
    UserList,
)
from ..models import Page as PageRow
from ..service import AtlasError
from . import CSP, templates
from .deps import (
    SESSION_COOKIE,
    Page,
    admin_form,
    admin_page,
    client_ip,
    csrf_form,
    form_dict,
    go,
    page,
    review_form,
    review_page,
    secure_cookies,
    set_anon_cookie,
    throttle,
    user_form,
    user_page,
)

router = APIRouter()
LOCKABLE = [*SCALAR_FIELDS, *UNION_FIELDS]
PER_PAGE = 48


# ---- helpers ------------------------------------------------------------------------------------------

def render(p: Page, name: str, status: int = 200, anon_csrf: bool = False, **ctx: Any) -> HTMLResponse:
    s = p.st.ctx.settings
    pending = 0
    if p.can_review:
        p.session.flush()
        pending = p.session.scalar(select(func.count()).select_from(Proposal).where(Proposal.status == "pending")) or 0
    nav_pages = list(p.session.scalars(select(PageRow).where(PageRow.published.is_(True), PageRow.in_nav.is_(True))
                                       .order_by(PageRow.position, PageRow.slug)))
    if name in ("login.html", "register.html"):
        ctx["issuers"] = list(p.session.scalars(select(OidcIssuer).where(OidcIssuer.enabled.is_(True))))
    if name == "me_settings.html" and p.user:
        links = {link.issuer_id for link in p.session.scalars(select(OidcLink).where(OidcLink.user_id == p.user.id))}
        ctx["oidc_issuers"] = [{"i": i, "linked": i.id in links,
                                "can": i.allow_all_users or p.user.oidc_allowed}
                               for i in p.session.scalars(select(OidcIssuer).where(OidcIssuer.enabled.is_(True)))]
        ctx["has_password"] = p.user.password_hash != "!"
    unread = social.unread_count(p.session, p.user.id) if p.user else 0
    resp = templates.TemplateResponse(p.request, name, {
        "p": p, "s": s, "app_name": s.app_name, "unread": unread, "registration_open": s.registration == "open",
        "pending_count": pending, "nav_pages": nav_pages, **ctx}, status_code=status)
    resp.headers["Cache-Control"] = "private, no-store" if p.user or anon_csrf else "public, max-age=30"
    if anon_csrf and p.new_cookie:
        set_anon_cookie(resp, p)
    return resp


def safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value and len(value) < 300:
        return value
    return "/"


def lines(text: str) -> list[str]:
    seen, out = set(), []
    for raw in (text or "").splitlines():
        item = raw.strip()
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            out.append(item)
    return out


def primary_art(p: Page, ids: list[str]) -> dict[str, str]:
    """Cached key-art hash per game, best source first."""
    if not ids:
        return {}
    out: dict[str, str] = {}
    rows = p.session.scalars(select(Artwork).where(
        Artwork.canonical_id.in_(ids), Artwork.kind == "key_art", Artwork.blob_hash.is_not(None)))
    ranked = sorted(rows, key=lambda a: (-p.st.ctx.settings.trust(a.source), a.priority))
    for a in ranked:
        out.setdefault(a.canonical_id, a.blob_hash)
    return out


DEFAULT_LABELS = {"developer": "Developer", "publisher": "Publisher", "series": "Series", "release_date": "Released",
                  "genres": "Genres", "themes": "Themes", "tags": "Tags", "features": "Features", "platforms": "Platforms",
                  "languages": "Languages", "aliases": "Also known as", "age_rating": "Age rating", "website": "Website",
                  "critic_score": "Critic score", "user_score": "Player score"}


def labels_for(p: Page, media_type: str) -> dict[str, str]:
    t = p.session.get(MediaType, media_type)
    return {**DEFAULT_LABELS, **((t.labels or {}) if t else {})}


def custom_inputs(p: Page, media_type: str, current: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Editable custom fields with their current value rendered for an input."""
    out = []
    for f in custom.fields_for(p.session, media_type, editable=True):
        v = (current or {}).get(f.key)
        shown = "\n".join(v) if isinstance(v, list) else ("yes" if v is True else "no" if v is False else
                                                          "" if v is None else str(v))
        out.append({"f": f, "value": shown})
    return out


def custom_from_form(p: Page, media_type: str, form: dict[str, str], current: dict[str, Any] | None = None
                     ) -> dict[str, Any]:
    """Raw custom values that differ from what is there now (empty input never clears)."""
    out: dict[str, Any] = {}
    for f in custom.fields_for(p.session, media_type, editable=True):
        raw = (form.get("cf_" + f.key) or "").strip()
        if not raw:
            continue
        try:
            value = custom.validate_value(f, raw)
        except AtlasError:
            out[f.key] = raw  # let proposals.create report the precise error
            continue
        if value != (current or {}).get(f.key):
            out[f.key] = raw
    return out


def live_game(p: Page, cid: str) -> tuple[Canonical, str | None]:
    return service.get_canonical(p.session, cid)


def when(dt) -> str:
    if dt is None:
        return ""
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).strftime("%Y-%m-%d %H:%M")


def title_of(p: Page, cid: str | None) -> str:
    row = p.session.get(Canonical, cid) if cid else None
    return (row.view.get("title") if row and row.view else None) or "(new game)"


# ---- public pages -------------------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def home(p: Annotated[Page, Depends(page)]):
    s = p.session
    recent = list(s.scalars(select(Canonical).where(Canonical.merged_into.is_(None), Canonical.norm_title != "")
                            .order_by(Canonical.updated_at.desc()).limit(12)))
    totals = {
        "games": s.scalar(select(func.count()).select_from(Canonical).where(
            Canonical.merged_into.is_(None), Canonical.norm_title != "")) or 0,
        "artwork": s.scalar(select(func.count()).select_from(Artwork).where(Artwork.blob_hash.is_not(None))) or 0,
        "contributors": s.scalar(select(func.count()).select_from(User)) or 0}
    return render(p, "home.html", recent=recent, art=primary_art(p, [g.id for g in recent]), totals=totals)


@router.get("/search", response_class=HTMLResponse)
def search(p: Annotated[Page, Depends(page)], q: Annotated[str, Query(max_length=200)] = "",
           page_no: Annotated[int, Query(alias="page", ge=1, le=1000)] = 1,
           type: Annotated[str, Query(max_length=32)] = ""):
    rows = service.search(p.session, q, PER_PAGE + 1, (page_no - 1) * PER_PAGE, media_type=type or None) \
        if q.strip() else []
    more = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]
    return render(p, "search.html", q=q, results=rows, more=more, page=page_no, art=primary_art(p, [g.id for g in rows]),
                  mtypes=custom.types(p.session, enabled_only=True), mtype=type)


@router.get("/media/{digest}")
def media(digest: str, p: Annotated[Page, Depends(page)]):
    return media_response(p.st, digest)


@router.get("/robots.txt", response_class=HTMLResponse)
def robots():
    return HTMLResponse("User-agent: *\nDisallow: /api/\nDisallow: /me\nDisallow: /review\nDisallow: /admin\n",
                        media_type="text/plain")


@router.get("/game/{cid}", response_class=HTMLResponse)
def game(cid: str, p: Annotated[Page, Depends(page)]):
    try:
        g, redirected = live_game(p, cid)
    except AtlasError as exc:
        raise HTTPException(404, "No such game.") from exc
    if redirected:
        return RedirectResponse(f"/game/{g.id}?from={redirected}", status_code=301)
    s, ctx = p.session, p.st.ctx
    art = facets.merged_artwork(s, ctx, g.id)
    cached = [a for a in art if a.blob_hash]
    key_art = next((a.blob_hash for a in cached if a.kind == "key_art"), None)
    shots = [a for a in cached if a.kind in ("screenshot", "hero", "banner")][:12]
    activity = []
    for pr in s.scalars(select(Proposal).where(Proposal.canonical_id == g.id, Proposal.status == "approved")
                        .order_by(Proposal.created_at.desc()).limit(10)):
        u = s.get(User, pr.proposer_id)
        activity.append({"user": u.username if u else "?", "kind": pr.kind, "when": when(pr.decided_at or pr.created_at),
                         "note": pr.note[:200]})
    extra: dict[str, Any] = {}
    if p.can_review:
        extra = {
            "observations": list(s.scalars(select(Observation).where(Observation.canonical_id == g.id)
                                           .order_by(Observation.observed_at.desc()).limit(100))),
            "locks": list(s.scalars(select(Lock).where(Lock.canonical_id == g.id))), "lockable": LOCKABLE,
            "can_split": ctx.settings.role == "authority" or True,
            "revertable": [{"id": r.id, "kind": r.kind, "when": when(r.decided_at or r.created_at),
                            "user": (s.get(User, r.proposer_id).username if s.get(User, r.proposer_id) else "?")}
                           for r in s.scalars(select(Proposal).where(
                               Proposal.canonical_id == g.id, Proposal.status == "approved",
                               Proposal.kind != "new_game").order_by(Proposal.created_at.desc()).limit(5))]}
    defs = {f.key: f for f in custom.fields_for(s, g.media_type, public=True)}
    values = custom.merged(s, ctx, g.id)
    details = [{"f": defs[k], "value": values[k]} for k in defs if k in values]
    social_ctx: dict[str, Any] = {"comments": social.thread(s, canonical_id=g.id, viewer=p.user, ctx=ctx),
                                  "comments_on": ctx.settings.comments_enabled, "lists_on": ctx.settings.lists_enabled}
    if p.user:
        social_ctx["following"] = bool(s.get(Follow, (p.user.id, "game", g.id)))
        social_ctx["my_lists"] = list(s.scalars(select(UserList).where(UserList.user_id == p.user.id)
                                                .order_by(UserList.created_at)))
        social_ctx["in_lists"] = {i.list_id for i in s.scalars(select(ListItem).where(ListItem.canonical_id == g.id))}
        social_ctx["can_comment"] = mail.is_verified(ctx.settings, p.user)
    return render(p, "game.html", g=g, v=g.view, explain=g.explain, key_art=key_art, shots=shots, **social_ctx,
                  lbl=labels_for(p, g.media_type), details=details,
                  mtype_label=(s.get(MediaType, g.media_type).label if s.get(MediaType, g.media_type) else g.media_type),
                  art_total=len(art), art_cached=len(cached), art_kinds=facets.KINDS, activity=activity,
                  achievements=facets.merged_achievements(s, ctx, g.id),
                  redirected_from=p.request.query_params.get("from"), **extra)


# ---- sign in / out ------------------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(p: Annotated[Page, Depends(page)], next: str = "/"):
    if p.user:
        return go(safe_next(next))
    return render(p, "login.html", anon_csrf=True, next=safe_next(next), error=None)


@router.post("/login")
def login(p: Annotated[Page, Depends(csrf_form)], username: Annotated[str, Form(max_length=64)],
          password: Annotated[str, Form(max_length=256)], next: Annotated[str, Form()] = "/"):
    ip, name = client_ip(p.request), username.strip().lower()[:40]
    throttle(p, f"login:ip:{ip}", 30, 300, "Too many sign-in attempts")
    # keyed by address too, so a stranger cannot lock a real user out by guessing their name
    throttle(p, f"login:pair:{ip}:{name}", 10, 900, "Too many sign-in attempts for this account")
    throttle(p, f"login:user:{name}", 100, 3600, "This account is receiving too many sign-in attempts")
    user = accounts.authenticate(p.session, username, password)
    if user is None:
        return render(p, "login.html", status=400, anon_csrf=True, next=safe_next(next),
                      error="Wrong username or password.")
    secret, row = accounts.create_session(p.session, p.st.ctx.settings, user)
    accounts.audit(p.session, user.id, "login", user.id)
    resp = go(safe_next(next))
    resp.set_cookie(SESSION_COOKIE, secret, httponly=True, samesite="lax", secure=secure_cookies(p), path="/",
                    max_age=p.st.ctx.settings.session_ttl_days * 86400)
    return resp


@router.post("/logout")
def logout(p: Annotated[Page, Depends(csrf_form)]):
    accounts.end_session(p.session, p.request.cookies.get(SESSION_COOKIE))
    resp = go("/", "bye")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/register", response_class=HTMLResponse)
def register_form(p: Annotated[Page, Depends(page)]):
    if p.st.ctx.settings.registration != "open":
        raise HTTPException(403, "Registration is closed on this server.")
    return render(p, "register.html", anon_csrf=True, error=None, min_len=p.st.ctx.settings.password_min_length)


@router.post("/register")
def register(p: Annotated[Page, Depends(csrf_form)], username: Annotated[str, Form(max_length=64)],
             password: Annotated[str, Form(max_length=256)], email: Annotated[str, Form(max_length=254)] = ""):
    throttle(p, f"register:ip:{client_ip(p.request)}", 5, 3600, "Too many accounts created from this address")
    s = p.st.ctx.settings
    try:
        user = accounts.register(p.session, s, username, password, email)
    except AtlasError as exc:
        p.session.rollback()
        return render(p, "register.html", status=400, anon_csrf=True, error=str(exc), min_len=s.password_min_length,
                      username=username, email=email)
    if user.email and s.verification_mode() != "off":
        mail.send_verification(p.session, s, user)
    secret, _row = accounts.create_session(p.session, s, user)
    resp = go("/", "mailsent" if user.email and s.verification_mode() != "off" else "saved")
    resp.set_cookie(SESSION_COOKIE, secret, httponly=True, samesite="lax", secure=secure_cookies(p), path="/",
                    max_age=s.session_ttl_days * 86400)
    return resp


# ---- contributing -------------------------------------------------------------------------------------

def _form_values(title, release_date, developer, publisher, series, description, platforms, tags, features):
    out: dict[str, Any] = {"title": title, "release_date": release_date, "developer": developer,
                           "publisher": publisher, "series": series, "description": description}
    for key, raw in (("platforms", platforms), ("tags", tags), ("features", features)):
        out[key] = lines(raw)
    return out


EXTRA_TEXT = ("age_rating", "website")
EXTRA_LISTS = ("genres", "themes", "languages", "aliases")


def _extra_values(f: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {k: f.get(k, "").strip() for k in EXTRA_TEXT}
    out.update({k: lines(f.get(k, "")) for k in EXTRA_LISTS})
    return out


def _changed(current: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for k, v in new.items():
        if v in ("", [], None):
            continue
        old = current.get(k)
        if (old or ([] if isinstance(v, list) else "")) != v:
            diff[k] = v
    return diff


def _edit_ctx(p: Page, mtype: str, **extra: Any) -> dict[str, Any]:
    return {"lbl": labels_for(p, mtype), "auto": proposals.can_auto_apply(p.st.ctx, p.me), **extra}


@router.get("/game/{cid}/edit", response_class=HTMLResponse)
def edit_form(cid: str, p: Annotated[Page, Depends(user_page)]):
    g, _ = live_game(p, cid)
    cur = custom.merged(p.session, p.st.ctx, g.id, public_only=False)
    return render(p, "edit.html", new=False, v=g.view, action=f"/game/{g.id}/edit", cancel=f"/game/{g.id}",
                  error=None, cfields=custom_inputs(p, g.media_type, cur), **_edit_ctx(p, g.media_type))


@router.post("/game/{cid}/edit")
def edit_submit(cid: str, p: Annotated[Page, Depends(user_form)], f: Annotated[dict[str, str], Depends(form_dict)]):
    g, _ = live_game(p, cid)
    new = _form_values(f.get("title", "").strip(), f.get("release_date", "").strip(), f.get("developer", "").strip(),
                       f.get("publisher", "").strip(), f.get("series", "").strip(), f.get("description", "").strip(),
                       f.get("platforms", ""), f.get("tags", ""), f.get("features", ""))
    new.update(_extra_values(f))
    cur = custom.merged(p.session, p.st.ctx, g.id, public_only=False)
    payload = _changed(g.view, new)
    extra = custom_from_form(p, g.media_type, f, cur)
    if extra:
        payload["custom"] = extra
    note = f.get("note", "")
    try:
        if not payload:
            raise AtlasError("You did not change anything.")
        pr = proposals.create(p.session, p.st.ctx, p.me, "edit", g.id, payload, note)
    except AtlasError as exc:
        p.session.rollback()
        shown = {**g.view, **{k: v for k, v in new.items() if v}}
        return render(p, "edit.html", status=400, new=False, v=shown, action=f"/game/{g.id}/edit",
                      cancel=f"/game/{g.id}", error=str(exc), note=note,
                      cfields=custom_inputs(p, g.media_type, {**cur, **{k: v for k, v in extra.items()}}),
                      **_edit_ctx(p, g.media_type))
    return go(f"/game/{g.id}", "applied" if pr.auto_applied else "proposed")


@router.get("/new", response_class=HTMLResponse)
def new_form(p: Annotated[Page, Depends(user_page)], title: Annotated[str, Query(max_length=500)] = "",
             type: Annotated[str, Query(max_length=32)] = "game"):
    mtypes = custom.types(p.session, enabled_only=True)
    if type not in {t.key for t in mtypes}:
        type = "game"
    return render(p, "edit.html", new=True, v={"title": title}, action="/new", cancel="/", error=None, mtype=type,
                  mtypes=mtypes, cfields=custom_inputs(p, type), **_edit_ctx(p, type))


@router.post("/new")
def new_submit(p: Annotated[Page, Depends(user_form)], f: Annotated[dict[str, str], Depends(form_dict)]):
    mtype = f.get("media_type", "game")
    new = _form_values(f.get("title", "").strip(), f.get("release_date", "").strip(), f.get("developer", "").strip(),
                       f.get("publisher", "").strip(), f.get("series", "").strip(), f.get("description", "").strip(),
                       f.get("platforms", ""), f.get("tags", ""), f.get("features", ""))
    new.update(_extra_values(f))
    payload: dict[str, Any] = {k: v for k, v in new.items() if v not in ("", [])}
    payload["media_type"] = mtype
    ids = {}
    for line in lines(f.get("provider_ids", "")):
        key, _, val = line.partition(":")
        ids[key.strip().lower()] = val.strip()
    if ids:
        payload["provider_ids"] = ids
    extra = custom_from_form(p, mtype, f)
    if extra:
        payload["custom"] = extra
    note = f.get("note", "")
    try:
        pr = proposals.create(p.session, p.st.ctx, p.me, "new_game", None, payload, note)
    except AtlasError as exc:
        p.session.rollback()
        return render(p, "edit.html", status=400, new=True, v=new, action="/new", cancel="/", error=str(exc),
                      note=note, ids_text=f.get("provider_ids", ""), mtype=mtype,
                      mtypes=custom.types(p.session, enabled_only=True),
                      cfields=custom_inputs(p, mtype, extra), **_edit_ctx(p, mtype))
    if pr.auto_applied and pr.canonical_id:
        return go(f"/game/{pr.canonical_id}", "applied")
    return go("/me", "proposed")


@router.post("/game/{cid}/artwork")
def propose_artwork(cid: str, p: Annotated[Page, Depends(user_form)], kind: Annotated[str, Form()],
                    url: Annotated[str, Form(max_length=1000)], note: Annotated[str, Form()] = ""):
    g, _ = live_game(p, cid)
    try:
        pr = proposals.create(p.session, p.st.ctx, p.me, "artwork", g.id,
                              {"items": [{"kind": kind, "url": url.strip()}]}, note)
    except AtlasError as exc:
        raise HTTPException(400, str(exc)) from exc
    return go(f"/game/{g.id}", "applied" if pr.auto_applied else "proposed")


def parse_achievement_line(line: str) -> dict[str, Any]:
    """`Name | Description | extras...`; extras are recognised by value, in any order: a kind, a tier, a whole
    number (points), `hidden`, an http(s) address (icon) or a second one (locked icon)."""
    name, _, rest = line.partition("|")
    desc, _, extras = rest.partition("|")
    item: dict[str, Any] = {"name": name.strip(), "description": desc.strip()}
    for token in (t.strip() for t in extras.split("|")):
        low = token.casefold().replace(" ", "_")
        if low in facets.ACH_KINDS:
            item["kind"] = low
        elif low in facets.ACH_TIERS:
            item["tier"] = low
        elif low == "hidden":
            item["hidden"] = True
        elif token.isdigit():
            item["points"] = int(token)
        elif token.startswith(("http://", "https://")):
            item["locked_icon_url" if "icon_url" in item else "icon_url"] = token
        elif token:
            raise HTTPException(400, f"Don't know what '{token[:40]}' means in: {name.strip()[:60]}")
    return item


@router.post("/game/{cid}/achievements")
def propose_achievements(cid: str, p: Annotated[Page, Depends(user_form)],
                         lines_text: Annotated[str, Form(alias="lines", max_length=100000)],
                         note: Annotated[str, Form()] = ""):
    g, _ = live_game(p, cid)
    items = []
    for line in lines(lines_text)[:200]:
        items.append(parse_achievement_line(line))
    try:
        pr = proposals.create(p.session, p.st.ctx, p.me, "achievements", g.id,
                              {"items": items}, note)
    except AtlasError as exc:
        raise HTTPException(400, str(exc)) from exc
    return go(f"/game/{g.id}", "applied" if pr.auto_applied else "proposed")


@router.post("/proposal/{pid}/withdraw")
def withdraw(pid: str, p: Annotated[Page, Depends(user_form)]):
    try:
        proposals.withdraw(p.session, p.me, pid)
    except AtlasError as exc:
        raise HTTPException(404 if exc.code == "not_found" else 409, str(exc)) from exc
    return go("/me", "withdrawn")


# ---- account ------------------------------------------------------------------------------------------

def _me_ctx(p: Page, **extra: Any) -> dict[str, Any]:
    s = p.session
    mine = []
    for r in s.scalars(select(Proposal).where(Proposal.proposer_id == p.me.id)
                       .order_by(Proposal.created_at.desc()).limit(50)):
        mine.append({"id": r.id, "when": when(r.created_at), "cid": r.canonical_id, "title": title_of(p, r.canonical_id),
                     "kind": r.kind, "status": r.status, "decision_note": r.decision_note})
    toks = [{"id": t.id, "name": t.name, "scopes": t.scopes, "last_used": when(t.last_used_at) or None}
            for t in s.scalars(select(Token).where(Token.user_id == p.me.id, Token.revoked_at.is_(None)))]
    st = p.st.ctx.settings
    return {"mine": mine, "tokens": toks, "min_len": st.password_min_length,
            "has_password": p.me.password_hash != "!", "auto_threshold": st.auto_approve_reputation,
            "verification": st.verification_mode(), **extra}


@router.get("/me", response_class=HTMLResponse)
def me(p: Annotated[Page, Depends(user_page)]):
    return render(p, "me.html", **_me_ctx(p))


@router.post("/me/tokens", response_class=HTMLResponse)
def token_create(p: Annotated[Page, Depends(user_form)], name: Annotated[str, Form(max_length=128)],
                 scope: Annotated[str, Form()] = "read"):
    scopes = ["read"]  # personal tokens are read-only; see auth.authenticate
    active = p.session.scalar(select(func.count()).select_from(Token).where(
        Token.user_id == p.me.id, Token.revoked_at.is_(None)))
    if active >= 10:
        raise HTTPException(409, "You already have 10 active tokens; revoke one first.")
    row, secret = auth.create_token(p.session, name.strip() or "token", scopes)
    row.user_id = p.me.id
    accounts.audit(p.session, p.me.id, "token_create", row.id)
    return render(p, "me.html", **_me_ctx(p, new_token=secret))


@router.post("/me/tokens/{tid}/revoke")
def token_revoke(tid: str, p: Annotated[Page, Depends(user_form)]):
    row = p.session.get(Token, tid)
    if row is None or row.user_id != p.me.id:
        raise HTTPException(404, "No such token.")
    row.revoked_at = row.revoked_at or datetime.now(UTC)
    return go("/me", "revoked")


@router.post("/me/password")
def password(p: Annotated[Page, Depends(user_form)], new: Annotated[str, Form(max_length=256)],
             current: Annotated[str, Form(max_length=256)] = ""):
    throttle(p, f"pw:user:{p.me.id}", 5, 900, "Too many attempts")
    u = p.me
    s = p.st.ctx.settings
    if not accounts.reauthenticated(u, current, p.login):
        raise HTTPException(400, "Your current password is wrong (or sign in again first).")
    if not s.password_min_length <= len(new) <= 200:
        raise HTTPException(400, f"The new password must be {s.password_min_length}-200 characters.")
    u.password_hash = accounts.hash_password(new)
    accounts.end_all_sessions(p.session, u.id)
    secret, _row = accounts.create_session(p.session, s, u)
    accounts.audit(p.session, u.id, "password_change", u.id)
    resp = go("/me", "password")
    resp.set_cookie(SESSION_COOKIE, secret, httponly=True, samesite="lax", secure=secure_cookies(p), path="/",
                    max_age=s.session_ttl_days * 86400)
    return resp


# ---- review -------------------------------------------------------------------------------------------

FIELD_LABELS = {"title": "Title", "release_date": "Release date", "developer": "Developer", "publisher": "Publisher",
                "series": "Series", "description": "Description", "tags": "Tags", "features": "Features",
                "platforms": "Platforms", "provider_ids": "Store IDs", "genres": "Genres", "themes": "Themes",
                "languages": "Languages", "aliases": "Also known as", "age_rating": "Age rating", "website": "Website"}


def _show(v: Any) -> str:
    if isinstance(v, list):
        return ", ".join(str(i) for i in v)
    if isinstance(v, dict):
        return ", ".join(f"{k}: {x}" for k, x in v.items())
    return "" if v is None else str(v)


def _proposal_card(p: Page, r: Proposal) -> dict[str, Any]:
    u = p.session.get(User, r.proposer_id)
    card: dict[str, Any] = {
        "id": r.id, "kind": r.kind, "cid": r.canonical_id, "title": title_of(p, r.canonical_id) if r.canonical_id
        else (r.payload.get("title") or "(new game)"), "user": u.username if u else "?", "rep": u.reputation if u else 0,
        "when": when(r.created_at), "note": r.note, "own": r.proposer_id == p.me.id,
        "rows": [], "entries": []}
    if r.kind in ("edit", "new_game"):
        cur = (p.session.get(Canonical, r.canonical_id).view if r.canonical_id and p.session.get(Canonical, r.canonical_id)
               else {})
        row = p.session.get(Canonical, r.canonical_id) if r.canonical_id else None
        mtype = row.media_type if row else r.payload.get("media_type", "game")
        defs = {f.key: f for f in custom.fields_for(p.session, mtype)}
        old_custom = custom.merged(p.session, p.st.ctx, r.canonical_id, public_only=False) if r.canonical_id else {}
        for k, v in r.payload.items():
            if k == "custom":
                for ck, cv in v.items():
                    card["rows"].append((defs[ck].label if ck in defs else ck, _show(old_custom.get(ck)), _show(cv)))
            elif k != "media_type":
                card["rows"].append((FIELD_LABELS.get(k, k), _show(cur.get(k)) if r.kind == "edit" else "", _show(v)))
    elif r.kind == "artwork":
        card["entries"] = [f"{i['kind'].replace('_', ' ')}: {i['url']}" for i in r.payload["items"]]
    else:
        card["entries"] = [f"{i['name']}" + (f" — {i['description']}" if i.get("description") else "")
                         for i in r.payload["items"][:50]]
    return card


@router.get("/review", response_class=HTMLResponse)
def review_queue(p: Annotated[Page, Depends(review_page)]):
    p.session.flush()
    rows = p.session.scalars(select(Proposal).where(Proposal.status == "pending")
                             .order_by(Proposal.created_at).limit(50)).all()
    dups = p.session.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")) or 0
    return render(p, "review.html", items=[_proposal_card(p, r) for r in rows], dup_count=dups)


@router.post("/review/{pid}")
def review_decide(pid: str, p: Annotated[Page, Depends(review_form)], decision: Annotated[str, Form()],
                  note: Annotated[str, Form(max_length=500)] = ""):
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "Unknown decision.")
    try:
        proposals.review(p.session, p.st.ctx, p.me, pid, approve=decision == "approve", note=note)
    except AtlasError as exc:
        raise HTTPException({"not_found": 404, "forbidden": 403}.get(exc.code, 409 if exc.code == "conflict" else 400),
                            str(exc)) from exc
    return go("/review", "approved" if decision == "approve" else "rejected")


@router.post("/review/{pid}/revert")
def review_revert(pid: str, p: Annotated[Page, Depends(review_form)], note: Annotated[str, Form()] = ""):
    try:
        pr = proposals.revert(p.session, p.st.ctx, p.me, pid, note)
    except AtlasError as exc:
        raise HTTPException(409 if exc.code == "conflict" else 404 if exc.code == "not_found" else 400, str(exc)) from exc
    return go(f"/game/{pr.canonical_id}" if pr.canonical_id else "/review", "reverted")


def _brief(p: Page, cid: str) -> dict[str, Any]:
    row = p.session.get(Canonical, cid)
    v = row.view if row else {}
    return {"id": cid, "title": v.get("title", cid), "year": (v.get("release_date") or "")[:4],
            "developer": v.get("developer", ""), "sources": ", ".join(v.get("sources", []))}


@router.get("/review/duplicates", response_class=HTMLResponse)
def duplicates(p: Annotated[Page, Depends(review_page)]):
    rows = p.session.scalars(select(ReviewItem).where(ReviewItem.status == "open")
                             .order_by(ReviewItem.created_at).limit(50)).all()
    items = [{"id": r.id, "kind": r.kind, "confidence": r.confidence, "canonical": _brief(p, r.canonical_id),
              "candidates": [_brief(p, c) for c, _s in r.candidates[:3]]} for r in rows]
    return render(p, "duplicates.html", items=items)


@router.post("/review/duplicates/{rid}/merge")
def duplicates_merge(rid: str, p: Annotated[Page, Depends(review_form)], into: Annotated[str, Form()]):
    item = p.session.get(ReviewItem, rid)
    if item is None or item.status != "open" or into not in {c for c, _ in item.candidates}:
        raise HTTPException(400, "That merge is not available.")
    try:
        _guard_upstream(p.session, p.st, item.canonical_id)
        _guard_upstream(p.session, p.st, into)
        service.merge_canonicals(p.session, p.st.ctx, item.canonical_id, into, actor=p.me.username)
    except AtlasError as exc:
        raise HTTPException(409, str(exc)) from exc
    return go("/review/duplicates", "merged")


@router.post("/review/duplicates/{rid}/dismiss")
def duplicates_dismiss(rid: str, p: Annotated[Page, Depends(review_form)]):
    item = p.session.get(ReviewItem, rid)
    if item is None:
        raise HTTPException(404, "No such item.")
    item.status, item.decided_by = "dismissed", p.me.username
    return go("/review/duplicates", "dismissed")


# ---- curation on a game page --------------------------------------------------------------------------

@router.post("/game/{cid}/merge")
def game_merge(cid: str, p: Annotated[Page, Depends(review_form)], into: Annotated[str, Form(max_length=40)]):
    try:
        _guard_upstream(p.session, p.st, cid)
        _guard_upstream(p.session, p.st, into.strip())
        target = service.merge_canonicals(p.session, p.st.ctx, cid, into.strip(), actor=p.me.username)
    except AtlasError as exc:
        raise HTTPException(404 if exc.code == "not_found" else 409 if exc.code == "conflict" else 400, str(exc)) from exc
    return go(f"/game/{target.id}", "merged")


@router.post("/game/{cid}/lock")
def game_lock(cid: str, p: Annotated[Page, Depends(review_form)], field: Annotated[str, Form()],
              value: Annotated[str, Form(max_length=20000)]):
    val: Any = lines(value) if field in UNION_FIELDS else value.strip()
    try:
        local = p.st.ctx.settings.role != "authority"
        if not local:
            _guard_upstream(p.session, p.st, cid)
        service.set_lock(p.session, p.st.ctx, cid, field, val, actor=p.me.username, local_only=local)
    except AtlasError as exc:
        raise HTTPException(400 if exc.code == "invalid" else 409, str(exc)) from exc
    return go(f"/game/{cid}", "locked")


@router.post("/game/{cid}/unlock")
def game_unlock(cid: str, p: Annotated[Page, Depends(review_form)], field: Annotated[str, Form()]):
    lk = p.session.get(Lock, (cid, field))
    if lk is not None and not lk.local_only and p.st.ctx.settings.role != "authority":
        raise HTTPException(409, "This lock is managed by the upstream authority.")
    try:
        service.clear_lock(p.session, p.st.ctx, cid, field)
    except AtlasError as exc:
        raise HTTPException(404, str(exc)) from exc
    return go(f"/game/{cid}", "unlocked")


@router.post("/observation/{oid}/detach")
def observation_detach(oid: str, p: Annotated[Page, Depends(review_form)]):
    obs = p.session.get(Observation, oid)
    if obs is None or not obs.canonical_id:
        raise HTTPException(404, "No such record.")
    try:
        _guard_upstream(p.session, p.st, obs.canonical_id)
        new = service.detach_observation(p.session, p.st.ctx, oid)
    except AtlasError as exc:
        raise HTTPException(409, str(exc)) from exc
    return go(f"/game/{new.id}", "detached")


# ---- administration -----------------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
def admin(p: Annotated[Page, Depends(admin_page)]):
    s = p.session
    ctx = p.st.ctx
    users = list(s.scalars(select(User).order_by(User.created_at)))
    audit = []
    for a in s.scalars(select(AuditEntry).order_by(AuditEntry.created_at.desc()).limit(40)):
        who = s.get(User, a.actor_id) if a.actor_id else None
        audit.append({"when": when(a.created_at), "who": who.username if who else "system", "action": a.action,
                      "target": a.target})
    return render(p, "admin.html", users=users, audit=audit, role=ctx.settings.role, node_id=ctx.identity.node_id,
                  public_key=ctx.identity.public_b64, feed_head=feed.head_seq(s), upstream=ctx.settings.upstream_url,
                  providers=sorted(p.st.providers.providers), registration=ctx.settings.registration,
                  cursors=[{"upstream": c.upstream, "last_seq": c.last_seq, "updated": when(c.updated_at)}
                           for c in s.scalars(select(SyncCursor))])


@router.post("/admin/users/{uid}")
def admin_user(uid: str, p: Annotated[Page, Depends(admin_form)], role: Annotated[str, Form()],
               status: Annotated[str, Form()]):
    target = p.session.get(User, uid)
    if target is None:
        raise HTTPException(404, "No such user.")
    try:
        if role != target.role:
            accounts.set_role(p.session, p.me, target, role)
        if status != target.status:
            accounts.set_status(p.session, p.me, target, status)
    except AtlasError as exc:
        raise HTTPException(409 if exc.code == "conflict" else 400, str(exc)) from exc
    return go("/admin", "saved")


def web_headers(response, path: str) -> None:
    if not path.startswith(("/api/", "/docs", "/openapi.json")):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


_ = Request
