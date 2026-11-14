from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func, select

from .. import (
    accounts,
    auth,
    custom,
    dates,
    facets,
    feed,
    groups,
    integrations,
    mail,
    matching,
    profile,
    proposals,
    relations,
    service,
    social,
    vocab,
)
from .. import media as images
from ..api.routes import _guard_upstream, served_media
from ..merge import LOCK_FIELDS, UNION_FIELDS
from ..models import (
    Artwork,
    ArtworkVote,
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
from ..providers import ProviderError
from ..service import AtlasError
from . import CSP, release_year, templates
from .deps import (
    SESSION_COOKIE,
    Page,
    admin_form,
    admin_page,
    check_csrf,
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
LOCKABLE = list(LOCK_FIELDS)
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
    if name == "login.html":  # C10 §5.5: no group buttons unless the operator opted in (H27)
        from .. import group_signin

        ctx["group_members"] = group_signin.login_page_members(p.session, s, p.st.ctx.identity)
    if name == "me_settings.html" and p.user:
        links = {link.issuer_id for link in p.session.scalars(select(OidcLink).where(OidcLink.user_id == p.user.id))}
        ctx["oidc_issuers"] = [{"i": i, "linked": i.id in links,
                                "can": i.allow_all_users or p.user.oidc_allowed}
                               for i in p.session.scalars(select(OidcIssuer).where(OidcIssuer.enabled.is_(True)))]
        ctx["has_password"] = p.user.password_hash != "!"
        from .profile_pages import preference_rows

        ctx["pref_rows"] = preference_rows(p)
        ctx["prefs_linked"] = any(r["synced"] for r in ctx["pref_rows"])
        ctx["prefs_unlinked"] = bool(ctx["pref_rows"]) and ctx["pref_rows"][0]["unlinked"]
    if name.startswith("admin") and "fed_optional" not in ctx:  # §3.7: "optional" on the Federation nav label
        from . import setup_checklist

        ctx["fed_optional"] = not setup_checklist.connected(p.session)
    if p.user and "link_notice" not in ctx and not name.startswith(("me_linked_nodes", "group_", "admin_group")):
        from .group import link_invite_banner

        ctx["link_notice"] = link_invite_banner(p)  # addendum item 4: the administrator's "Invite users to link"
    unread = social.unread_count(p.session, p.user.id) if p.user else 0
    prefs = profile.values(p.session, s, p.user) if p.user else anonymous_prefs(p.request)
    resp = templates.TemplateResponse(p.request, name, {
        "p": p, "s": s, "app_name": s.app_name, "unread": unread, "registration_open": s.registration == "open",
        "pending_count": pending, "nav_pages": nav_pages, "prefs": prefs,
        "date_offer": not dates.style_chosen(prefs),
        "conflicts_open": profile.open_count(p.session, p.user.id) if p.user else 0, **ctx}, status_code=status)
    # An anonymous visitor who chose a date format gets pages in that format, so they are not shared-cacheable;
    # every public page varies on the cookie so a browser never reuses its cookie-less copy (§7.4).
    private = p.user or anon_csrf or bool(prefs)
    resp.headers["Cache-Control"] = "private, no-store" if private else "public, max-age=30"
    if not p.user:
        resp.headers["Vary"] = "Cookie"
    if anon_csrf and p.new_cookie:
        set_anon_cookie(resp, p)
    return resp


DATE_COOKIE = "atlas_date"


def anonymous_prefs(request: Request) -> dict[str, str]:
    """An anonymous visitor's date format, from the `atlas_date` cookie (an allowlisted value, or nothing)."""
    value = request.cookies.get(DATE_COOKIE, "")
    return {"date_format": value} if value in dates.STYLES else {}


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


#: C13 §6.4: a tile shows the cover; when no cover is cached here, the best other cached image of the record.
TILE_KINDS = ("key_art", "poster", "hero", "banner", "screenshot", "icon", "logo")


def primary_art(p: Page, ids: list[str]) -> dict[str, str]:
    """Cached tile image hash per game: key art first, best source first, then the fallbacks of `TILE_KINDS`."""
    if not ids:
        return {}
    out: dict[str, str] = {}
    rows = p.session.scalars(select(Artwork).where(
        Artwork.canonical_id.in_(ids), Artwork.kind.in_(TILE_KINDS), Artwork.blob_hash.is_not(None),
        Artwork.duplicate_of.is_(None)))
    ranked = sorted(rows, key=lambda a: (TILE_KINDS.index(a.kind), -p.st.ctx.settings.trust(a.source), a.priority))
    for a in ranked:
        out.setdefault(a.canonical_id, a.blob_hash)
    return out


ART_LABELS = {"key_art": "Cover", "banner": "Banner", "hero": "Hero", "logo": "Logo", "icon": "Icon",
              "screenshot": "Screenshots", "poster": "Poster"}


def artwork_view(p: Page, a: Any, votes: dict[str, tuple[int, int]], mine: dict[str, int]) -> dict[str, Any]:
    up, down = votes.get(a.id, (0, 0))
    if a.blob_hash:
        state = "Downloaded and checked on this server"
    elif not p.st.ctx.settings.media_cache_enabled:
        state = "Caching is switched off"
    elif a.fail_count:
        state = f"Download failed {a.fail_count} time{'' if a.fail_count == 1 else 's'}; retrying later"
    else:
        state = "Waiting to be downloaded and checked"
    tried = [e for e in (getattr(a, "sources_tried", None) or []) if isinstance(e, dict) and e.get("error")]
    if tried:  # C13 §6.3: why each place this image was asked of said no, kept after a later success too
        state += " (" + "; ".join(f"{'the peer' if str(e.get('source', '')).startswith('peer:') else 'its origin'}: "
                                  f"{str(e['error']).replace('_', ' ')}" for e in tried) + ")"
    return {"id": a.id, "kind": a.kind, "label": ART_LABELS.get(a.kind, a.kind), "blob": a.blob_hash, "url": a.url,
            "host": urlsplit(a.url).hostname or "", "width": a.width, "height": a.height, "source": a.source,
            "state": state, "preferred": a.preferred, "up": up, "down": down, "mine": mine.get(a.id, 0),
            "size": a.size_bytes, "type": a.content_type}


def artwork_votes(p: Page, ids: list[str]) -> tuple[dict[str, tuple[int, int]], dict[str, int]]:
    if not ids:
        return {}, {}
    s = p.session
    votes = {i: (int(up or 0), int(down or 0)) for i, up, down in s.execute(
        select(ArtworkVote.artwork_id, func.sum(case((ArtworkVote.value > 0, 1), else_=0)),
               func.sum(case((ArtworkVote.value < 0, 1), else_=0))).where(ArtworkVote.artwork_id.in_(ids))
        .group_by(ArtworkVote.artwork_id))}
    mine = {}
    if p.user:
        mine = {r.artwork_id: r.value for r in s.scalars(select(ArtworkVote).where(
            ArtworkVote.user_id == p.user.id, ArtworkVote.artwork_id.in_(ids)))}
    return votes, mine


def build_gallery(p: Page, art: list[Any]) -> list[dict[str, Any]]:
    """The game's cached images grouped by kind, default first. Uncached ones are listed separately as text."""
    votes, mine = artwork_votes(p, [a.id for a in art if a.blob_hash])
    groups: dict[str, list[dict[str, Any]]] = {}
    for a in art:
        if a.blob_hash:
            groups.setdefault(a.kind, []).append(artwork_view(p, a, votes, mine))
    return [{"kind": k, "label": ART_LABELS.get(k, k), "images": groups[k]} for k in facets.KINDS if k in groups]


def pending_artwork(rows: list[Any], caching: bool) -> list[dict[str, Any]]:
    """Images assigned to a game but not cached here, as text only: a source link and why it is waiting.

    They are never embedded: the content security policy forbids loading third-party images, and a
    hotlink would also tell the source site who is viewing the page."""
    out = []
    for a in rows[:50]:
        host = urlsplit(a.url).hostname or ""
        if not caching:
            state = "Caching is switched off"
        elif a.fail_count:
            state = f"Download failed {a.fail_count} time{'' if a.fail_count == 1 else 's'}; retrying later"
        else:
            state = "Waiting to be downloaded and checked"
        out.append({"kind": a.kind.replace("_", " "), "url": a.url, "host": host, "state": state, "source": a.source})
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


def title_of(p: Page, cid: str | None) -> str:
    row = p.session.get(Canonical, cid) if cid else None
    return (row.view.get("title") if row and row.view else None) or "(new record)"


# ---- public pages -------------------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def home(p: Annotated[Page, Depends(page)]):
    s = p.session
    recent = list(s.scalars(select(Canonical).where(Canonical.merged_into.is_(None), Canonical.norm_title != "")
                            .order_by(Canonical.updated_at.desc()).limit(12)))
    media_types = custom.types(s, enabled_only=True)
    record_filter = (Canonical.merged_into.is_(None), Canonical.norm_title != "")
    type_counts = {
        item.key: s.scalar(select(func.count()).select_from(Canonical).where(
            *record_filter, Canonical.media_type == item.key)) or 0
        for item in media_types
    }
    totals = {
        "records": sum(type_counts.values()),
        "artwork": s.scalar(select(func.count()).select_from(Artwork).where(Artwork.blob_hash.is_not(None))) or 0,
        "contributors": s.scalar(select(func.count()).select_from(User)) or 0}
    return render(p, "home.html", recent=recent, art=primary_art(p, [g.id for g in recent]), totals=totals,
                  media_types=media_types, type_counts=type_counts,
                  type_labels={item.key: item.label for item in media_types})


@router.get("/search", response_class=HTMLResponse)
def search(p: Annotated[Page, Depends(page)], q: Annotated[str, Query(max_length=200)] = "",
           page_no: Annotated[int, Query(alias="page", ge=1, le=1000)] = 1,
           type: Annotated[str, Query(max_length=32)] = "",
           released_from: Annotated[str, Query(max_length=40)] = "",
           released_to: Annotated[str, Query(max_length=40)] = "",
           sort: Annotated[str, Query(max_length=16)] = ""):
    """Browse and search. `released_from`/`released_to` take a year, a month or a day (C5.2); `sort=release`
    orders by first release with year-only dates at the start of their year."""
    released_from, released_to = released_from.strip(), released_to.strip()
    error, read_as = None, []
    prefs = profile.values(p.session, p.st.ctx.settings, p.user) if p.user else anonymous_prefs(p.request)
    bounds = {"released_from": released_from, "released_to": released_to}
    for key, raw in bounds.items():
        if raw and not dates.PARTIAL_RE.fullmatch(raw):  # typed in the viewer's format (§7.1)
            entry = dates.parse_entry(raw, prefs, any_year=True)
            chosen = entry.ok or (entry.ambiguous[0] if entry.ambiguous else None)
            if chosen is not None:
                bounds[key] = chosen.value
                read_as.append({"typed": raw, "value": chosen.value, "ambiguous": bool(entry.ambiguous),
                                "other": entry.ambiguous[1].value if entry.ambiguous else None, "key": key})
    released_from, released_to = bounds["released_from"], bounds["released_to"]
    try:
        rows = service.search(p.session, q, PER_PAGE + 1, (page_no - 1) * PER_PAGE, media_type=type or None,
                              released_from=released_from, released_to=released_to, sort=sort or "relevance")
    except AtlasError as exc:
        rows, error = [], str(exc)
    more = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]
    mtypes = custom.types(p.session, enabled_only=True)
    type_label = next((item.label for item in mtypes if item.key == type), None)
    filters = {k: v for k, v in (("released_from", released_from), ("released_to", released_to), ("sort", sort)) if v}
    keep = urlencode({**({"q": q} if q else {}), **({"type": type} if type else {}), **filters})
    return render(p, "search.html", status=400 if error else 200, q=q, results=rows, more=more, page=page_no,
                  art=primary_art(p, [g.id for g in rows]), mtypes=mtypes, mtype=type, mtype_label=type_label,
                  filters=filters, keep=keep, filter_error=error, sorts=service.SORTS, read_as=read_as)


@router.get("/media/{digest}")
def media(digest: str, p: Annotated[Page, Depends(page)]):
    return served_media(p.session, p.st, digest, "No such image.")  # C12 §12.5.5: redirect/none sources


@router.get("/robots.txt", response_class=HTMLResponse)
def robots():
    return HTMLResponse("User-agent: *\nDisallow: /api/\nDisallow: /me\nDisallow: /review\nDisallow: /admin\n",
                        media_type="text/plain")


ACH_FILTERS = ("all", "platinum", "rare", "hidden", "unobtainable")


def ach_filter(rows: list[dict[str, Any]], sets: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """SPEC-UI-POLISH-PASS §8.2: `?ach=all|platinum|rare|hidden|unobtainable|set:<key>`, filtered on the server
    so the links work without JavaScript. An unknown value shows everything."""
    if key == "platinum":
        return [a for a in rows if a.get("tier") == "platinum"]
    if key == "rare":
        return [a for a in rows if a.get("global_percent") is not None and a["global_percent"] < 15]
    if key == "hidden":
        return [a for a in rows if a.get("hidden")]
    if key == "unobtainable":
        return [a for a in rows if a.get("status") == "unobtainable"]
    if key.startswith("set:"):
        ids = {s.get("id") for s in sets if s.get("key") == key[4:]}
        return [a for a in rows if a.get("set") in ids]
    return rows


@router.get("/game/{cid}", response_class=HTMLResponse)
def game(cid: str, p: Annotated[Page, Depends(page)], ach: Annotated[str, Query(max_length=80)] = "all"):
    try:
        g, redirected = live_game(p, cid)
    except AtlasError as exc:
        raise HTTPException(404, "No such record.") from exc
    if redirected:
        return RedirectResponse(f"/game/{g.id}?from={redirected}", status_code=301)
    s, ctx = p.session, p.st.ctx
    listing = facets.achievement_listing(s, ctx, g.id)
    art = facets.merged_artwork(s, ctx, g.id, include_duplicates=False)  # exact duplicates once (§12.5.4)
    cached = [a for a in art if a.blob_hash and images.fetch_policy(ctx.settings, a.source, a.url) == "proxy"]
    cover_kinds = ("key_art", "poster", "cover") if g.media_type == "game" else ("poster", "key_art", "cover")
    key_art = next((a.blob_hash for kind in cover_kinds for a in cached if a.kind == kind), None)
    gallery = build_gallery(p, cached)
    pending = pending_artwork([a for a in art if not a.blob_hash], ctx.settings.media_cache_enabled)
    activity = []
    for pr in s.scalars(select(Proposal).where(Proposal.canonical_id == g.id, Proposal.status == "approved")
                        .order_by(Proposal.created_at.desc()).limit(10)):
        u = s.get(User, pr.proposer_id)
        activity.append({"user": u.username if u else "?", "kind": pr.kind, "when": pr.decided_at or pr.created_at,
                         "note": pr.note[:200]})
    extra: dict[str, Any] = {}
    if p.can_review:
        extra = {
            "observations": list(s.scalars(select(Observation).where(Observation.canonical_id == g.id)
                                           .order_by(Observation.observed_at.desc()).limit(100))),
            "locks": list(s.scalars(select(Lock).where(Lock.canonical_id == g.id))), "lockable": LOCKABLE,
            "can_split": ctx.settings.role == "authority" or True,
            "revertable": [{"id": r.id, "kind": r.kind, "when": r.decided_at or r.created_at,
                            "user": (s.get(User, r.proposer_id).username if s.get(User, r.proposer_id) else "?")}
                           for r in s.scalars(select(Proposal).where(
                               Proposal.canonical_id == g.id, Proposal.status == "approved",
                               Proposal.kind != "new_game").order_by(Proposal.created_at.desc()).limit(5))]}
    defs = {f.key: f for f in custom.fields_for(s, g.media_type, public=True)}
    values = custom.merged(s, ctx, g.id)
    details = [{"f": defs[k], "value": values[k]} for k in defs if k in values]
    single = vocab.crossplay_projection(values.get("crossplay"), values.get("players_max")) == vocab.NOT_APPLICABLE
    if g.media_type == "game" and "crossplay" in defs and single:
        # C7.2: derived for display only (never stored, never in the API), and never shown as "no"
        order = list(defs)
        at = next((i for i, d in enumerate(details) if order.index(d["f"].key) > order.index("crossplay")), len(details))
        details.insert(at, {"f": defs["crossplay"], "value": "Single-player", "derived": True})
    social_ctx: dict[str, Any] = {"comments": social.thread(s, canonical_id=g.id, viewer=p.user, ctx=ctx),
                                  "comments_on": ctx.settings.comments_enabled, "lists_on": ctx.settings.lists_enabled}
    if p.user:
        social_ctx["following"] = bool(s.get(Follow, (p.user.id, "game", g.id)))
        social_ctx["my_lists"] = list(s.scalars(select(UserList).where(UserList.user_id == p.user.id)
                                                .order_by(UserList.created_at)))
        social_ctx["in_lists"] = {i.list_id for i in s.scalars(select(ListItem).where(ListItem.canonical_id == g.id))}
        social_ctx["can_comment"] = mail.is_verified(ctx.settings, p.user)
    return render(p, "game.html", g=g, v=g.view, explain=_shown_explain(g.explain), key_art=key_art, gallery=gallery,
                  **social_ctx,
                  lbl=labels_for(p, g.media_type), details=details,
                  mtype_label=(s.get(MediaType, g.media_type).label if s.get(MediaType, g.media_type) else g.media_type),
                  pending_art=pending, caching=ctx.settings.media_cache_enabled, stores=integrations.STORES,
                  art_total=len(art), art_cached=len(cached),
                  art_kinds=facets.KINDS, activity=activity,
                  achievements=ach_filter(listing["results"], listing["sets"], ach),
                  ach_all=listing["results"], ach_sets=listing["sets"], ach=ach if ach in ACH_FILTERS or
                  ach.startswith("set:") else "all",
                  rel=relations.of(s, g.id, limit=RELATION_PREVIEW), rel_labels=relations.LABELS,
                  redirected_from=p.request.query_params.get("from"), **extra)


@router.post("/game/{cid}/artwork/retry")
def retry_game_artwork(cid: str, p: Annotated[Page, Depends(admin_form)]):
    """Make this record's uncached images eligible for the next background download pass straight away."""
    try:
        g, _ = live_game(p, cid)
    except AtlasError as exc:
        raise HTTPException(404, "No such record.") from exc
    rows = [a for a in facets.merged_artwork(p.session, p.st.ctx, g.id) if not a.blob_hash]
    for a in rows:
        a.fail_count, a.next_try_at = 0, None
    accounts.audit(p.session, p.me.id, "media_retry", g.id, {"images": len(rows)})
    return go(f"/game/{g.id}", "artretry" if rows else "artnone")


# ---- sign in / out ------------------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(p: Annotated[Page, Depends(page)], next: str = "/"):
    if p.user:
        return go(safe_next(next))
    return render(p, "login.html", anon_csrf=True, next=safe_next(next), error=None)


@router.post("/login")
def login(p: Annotated[Page, Depends(csrf_form)], username: Annotated[str, Form(max_length=64)],
          password: Annotated[str, Form(max_length=256)], next: Annotated[str, Form()] = "/"):
    from .. import instruments

    ip, name = client_ip(p.request), username.strip().lower()[:40]
    try:
        throttle(p, f"login:ip:{ip}", 30, 300, "Too many sign-in attempts")
        # keyed by address too, so a stranger cannot lock a real user out by guessing their name
        throttle(p, f"login:pair:{ip}:{name}", 10, 900, "Too many sign-in attempts for this account")
        throttle(p, f"login:user:{name}", 100, 3600, "This account is receiving too many sign-in attempts")
    except HTTPException:
        instruments.identity_flows.inc(flow="password", outcome="rate_limited")
        raise
    user = accounts.authenticate(p.session, username, password)
    instruments.identity_flows.inc(flow="password", outcome="bad_credentials" if user is None else "ok")
    if user is None:
        return render(p, "login.html", status=400, anon_csrf=True, next=safe_next(next),
                      error="Wrong username or password.")
    secret, row = accounts.create_session(p.session, p.st.ctx.settings, user)
    accounts.audit(p.session, user.id, "login", user.id)
    adopt_date_cookie(p, user)
    resp = go(safe_next(next))
    resp.set_cookie(SESSION_COOKIE, secret, httponly=True, samesite="lax", secure=secure_cookies(p), path="/",
                    max_age=p.st.ctx.settings.session_ttl_days * 86400)
    return resp


def adopt_date_cookie(p: Page, user: User) -> None:
    """§7.4: a date format chosen while signed out becomes the profile's, once, when the profile has none."""
    chosen = anonymous_prefs(p.request)
    if chosen and not dates.style_chosen(profile.values(p.session, p.st.ctx.settings, user)):
        profile.set_local(p.session, p.st.ctx.settings, p.st.ctx.identity, user, "date_format", chosen["date_format"])


def _same_origin(p: Page) -> bool:
    """An anonymous date-format choice carries no CSRF token (anonymous pages stay shared-cacheable), so it is
    accepted only from this site: the browser's Sec-Fetch-Site, else an Origin equal to this node's address."""
    site = p.request.headers.get("sec-fetch-site")
    if site is not None:
        return site == "same-origin"
    origin = p.request.headers.get("origin")
    return origin is not None and origin.rstrip("/") == p.st.ctx.settings.public_url.rstrip("/")


@router.post("/prefs/date-format")
def date_format_choice(p: Annotated[Page, Depends(page)], value: Annotated[str, Form(max_length=8)] = "",
                       next: Annotated[str, Form(max_length=300)] = "/", csrf: Annotated[str, Form()] = ""):
    """The one-time "month/day/year?" offer (SPEC-UI-POLISH-PASS §7.4). Signed in: the profile's `date_format`
    (synced like any preference, so "Keep" stores `dmy` explicitly and the offer never returns). Signed out: the
    `atlas_date` cookie, nothing else."""
    if value not in dates.STYLES:
        raise HTTPException(400, "Unknown date format.")
    target = safe_next(next)
    if p.user:
        check_csrf(p, csrf)
        profile.set_local(p.session, p.st.ctx.settings, p.st.ctx.identity, p.user, "date_format", value)
        return go(target, "saved")
    if not _same_origin(p):
        raise HTTPException(403, "Choose the date format from this site.")
    resp = go(target)
    resp.set_cookie(DATE_COOKIE, value, max_age=31536000, path="/", samesite="lax", secure=secure_cookies(p),
                    httponly=True)
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


RELEASE_INPUTS = ("date", "platform", "region", "edition", "category")


def _form_view(view: dict[str, Any]) -> dict[str, Any]:
    """The record as the editor shows it: the first release in the date field, whatever its precision."""
    out = dict(view)
    first = view.get("first_release") or {}
    if first.get("date"):
        out["release_date"] = first["date"]
    return out


def _release_rows(entries: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Exactly `dates.MAX_RELEASES` editor rows: the given entries, then empty rows."""
    rows = [{k: str(e.get(k) or "") for k in RELEASE_INPUTS} for e in (entries or [])[:dates.MAX_RELEASES]]
    rows += [{"date": "", "platform": "", "region": "", "edition": "", "category": "full"}
             for _ in range(dates.MAX_RELEASES - len(rows))]
    return rows


def _releases_from_form(f: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """(rows to show again, entries to propose) from the editor's release rows. A row with nothing but its
    default category is empty and ignored; any other row is an entry and must be valid (proposals checks).
    Rows past the limit are never read, so more than five cannot be submitted from this form."""
    rows, entries = [], []
    for i in range(dates.MAX_RELEASES):
        row = {k: (f.get(f"rel_{k}.{i}") or "").strip() for k in RELEASE_INPUTS}
        row["category"] = row["category"] or "full"
        rows.append(row)
        if not any(row[k] for k in ("date", "platform", "region", "edition")):
            continue
        entry: dict[str, Any] = {"date": row["date"], "precision": dates.precision_of(row["date"]) or "day",
                                 "category": row["category"]}
        entry.update({k: row[k] for k in ("platform", "region", "edition") if row[k]})
        entries.append(entry)
    return rows, entries


def _release_change(view: dict[str, Any], entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The release list to propose, or None when it is empty or equal to the record's (C5.2: an empty list
    never clears). An invalid list is passed on as entered so the proposal reports the exact error."""
    if not entries:
        return None
    try:
        cleaned = dates.clean_entries(entries)
    except dates.DateError:
        return entries
    same = sorted(map(_entry_value, cleaned)) == sorted(map(_entry_value, view.get("releases") or []))
    return None if same else cleaned


def _entry_value(e: dict[str, Any]) -> tuple[str, ...]:
    return (*dates.entry_key(e), e.get("date") or "")


def _read_dates(p: Page, f: dict[str, str], media_type: str
                ) -> tuple[dict[str, str], dict[str, list[dict]], list[str]]:
    """Date fields as typed in the viewer's format (SPEC-UI-POLISH-PASS §7.2), read into ISO. Returns the form with
    the readable values replaced, and the fields whose numeric entry has two readings ({name: [two options]}).
    A `<name>.read` radio answers the question. An unreadable value is left as typed, so the proposal reports the
    exact rule it breaks (release dates keep C5.1's year range; custom date fields are days of any year)."""
    prefs = profile.values(p.session, p.st.ctx.settings, p.user) if p.user else {}
    names = [("release_date", False), *((f"rel_date.{i}", False) for i in range(dates.MAX_RELEASES))]
    names += [(f"cf_{c.key}", True) for c in custom.fields_for(p.session, media_type, editable=True)
              if c.kind == "date"]
    out, ambiguous, errors = dict(f), {}, []
    for name, any_year in names:
        raw = (f.get(name) or "").strip()
        if not raw:
            continue
        result = dates.parse_entry(raw, prefs, any_year=any_year)
        if result.ambiguous:
            picked = (f.get(name + ".read") or "").strip()
            if picked in {d.value for d in result.ambiguous}:
                out[name] = picked
                continue
            ambiguous[name] = [{"value": d.value, "text": dates.format_day(d, None, {"date_format": "long"})}
                               for d in result.ambiguous]
        elif result.ok is not None:
            if any_year and result.ok.precision != "day":
                continue  # a custom date field takes a day: keep the text so validation names the rule
            out[name] = result.ok.value
        elif result.error and not any_year:
            errors.append(result.error)  # in the viewer's own terms, instead of the stored ISO rule
    return out, ambiguous, errors


AMBIGUOUS = "Some dates can be read two ways. Choose what you meant under each one, then save again."


def _edit_ctx(p: Page, mtype: str, **extra: Any) -> dict[str, Any]:
    media_type = p.session.get(MediaType, mtype)
    extra.setdefault("rel_rows", _release_rows(None))
    return {"lbl": labels_for(p, mtype), "mtype": mtype,
            "mtype_label": media_type.label if media_type else mtype.replace("_", " ").title(),
            "auto": proposals.can_auto_apply(p.st.ctx, p.me), "dates_gated": service.dates_gated(p.st.ctx),
            **extra}


@router.get("/game/{cid}/edit", response_class=HTMLResponse)
def edit_form(cid: str, p: Annotated[Page, Depends(user_page)]):
    g, _ = live_game(p, cid)
    cur = custom.merged(p.session, p.st.ctx, g.id, public_only=False)
    return render(p, "edit.html", new=False, v=_form_view(g.view), action=f"/game/{g.id}/edit", cancel=f"/game/{g.id}",
                  error=None, cfields=custom_inputs(p, g.media_type, cur),
                  **_edit_ctx(p, g.media_type, rel_rows=_release_rows(g.view.get("releases"))))


@router.post("/game/{cid}/edit")
def edit_submit(cid: str, p: Annotated[Page, Depends(user_form)], f: Annotated[dict[str, str], Depends(form_dict)]):
    g, _ = live_game(p, cid)
    typed = f
    f, ambiguous, date_errors = _read_dates(p, f, g.media_type)
    new = _form_values(f.get("title", "").strip(), f.get("release_date", "").strip(), f.get("developer", "").strip(),
                       f.get("publisher", "").strip(), f.get("series", "").strip(), f.get("description", "").strip(),
                       f.get("platforms", ""), f.get("tags", ""), f.get("features", ""))
    new.update(_extra_values(f))
    cur = custom.merged(p.session, p.st.ctx, g.id, public_only=False)
    payload = _changed(_form_view(g.view), new)
    rel_rows, entries = _releases_from_form(f)
    releases = _release_change(g.view, entries)
    if releases is not None:
        payload["releases"] = releases
    extra = custom_from_form(p, g.media_type, f, cur)
    if extra:
        payload["custom"] = extra
    note = f.get("note", "")
    try:
        if date_errors:
            raise AtlasError("; ".join(date_errors))
        if ambiguous:
            raise AtlasError(AMBIGUOUS)
        if not payload:
            raise AtlasError("You did not change anything.")
        pr = proposals.create(p.session, p.st.ctx, p.me, "edit", g.id, payload, note)
    except AtlasError as exc:
        p.session.rollback()
        shown = {**_form_view(g.view), **{k: v for k, v in new.items() if v}}
        if ambiguous:  # show what was typed, with the question under it
            shown["release_date"] = typed.get("release_date", "").strip() or shown.get("release_date", "")
            rel_rows = _releases_from_form(typed)[0]
        return render(p, "edit.html", status=400, new=False, v=shown, action=f"/game/{g.id}/edit",
                      cancel=f"/game/{g.id}", error=str(exc), note=note, ambiguous=ambiguous,
                      cfields=custom_inputs(p, g.media_type, {**cur, **{k: v for k, v in extra.items()}}),
                      **_edit_ctx(p, g.media_type, rel_rows=rel_rows))
    return go(f"/game/{g.id}", "applied" if pr.auto_applied else "proposed")


@router.get("/new", response_class=HTMLResponse)
def new_form(p: Annotated[Page, Depends(user_page)], title: Annotated[str, Query(max_length=500)] = "",
             type: Annotated[str, Query(max_length=32)] = "game"):
    mtypes = custom.types(p.session, enabled_only=True)
    if type not in {t.key for t in mtypes}:
        type = "game"
    return render(p, "edit.html", new=True, v={"title": title}, action="/new", cancel="/", error=None,
                  mtypes=mtypes, cfields=custom_inputs(p, type), **_edit_ctx(p, type))


@router.post("/new")
def new_submit(p: Annotated[Page, Depends(user_form)], f: Annotated[dict[str, str], Depends(form_dict)]):
    mtype = f.get("media_type", "game")
    typed = f
    f, ambiguous, date_errors = _read_dates(p, f, mtype)
    new = _form_values(f.get("title", "").strip(), f.get("release_date", "").strip(), f.get("developer", "").strip(),
                       f.get("publisher", "").strip(), f.get("series", "").strip(), f.get("description", "").strip(),
                       f.get("platforms", ""), f.get("tags", ""), f.get("features", ""))
    new.update(_extra_values(f))
    payload: dict[str, Any] = {k: v for k, v in new.items() if v not in ("", [])}
    rel_rows, entries = _releases_from_form(f)
    if entries:
        payload["releases"] = entries
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
        if date_errors:
            raise AtlasError("; ".join(date_errors))
        if ambiguous:
            raise AtlasError(AMBIGUOUS)
        pr = proposals.create(p.session, p.st.ctx, p.me, "new_game", None, payload, note)
    except AtlasError as exc:
        p.session.rollback()
        if ambiguous:
            new["release_date"] = typed.get("release_date", "").strip()
            rel_rows = _releases_from_form(typed)[0]
        return render(p, "edit.html", status=400, new=True, v=new, action="/new", cancel="/", error=str(exc),
                      note=note, ids_text=f.get("provider_ids", ""), ambiguous=ambiguous,
                      mtypes=custom.types(p.session, enabled_only=True),
                      cfields=custom_inputs(p, mtype, extra), **_edit_ctx(p, mtype, rel_rows=rel_rows))
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


#: At most this many of a record's linked ids are looked up in the cross-reference source per suggestion request.
CROSSREF_LOOKUPS = 2
CROSSREF_SLOW_SECONDS = 2.0  # after a lookup this slow, skip the rest (worst case ~2 s + one 4 s timeout)


def _crossref_suggestions(p: Page, g: Any, linked: dict[str, str]) -> list[dict[str, Any]]:
    """Store ids for this game from the ids it already has (Wikidata, issue #63). One candidate for a store is a
    likely match; several (the id is shared by a game and its edition) are offered without that mark."""
    from ..providers.wikidata import PROPERTIES, media_type_of

    kind = getattr(g, "media_type", None) or "game"
    found: dict[str, list[tuple[str, str]]] = {}
    asked = 0
    started = time.monotonic()
    for ns in sorted(linked):
        if ns not in PROPERTIES or media_type_of(ns) != kind or asked >= CROSSREF_LOOKUPS:
            continue
        if asked and time.monotonic() - started > CROSSREF_SLOW_SECONDS:
            break  # the first lookup was slow (uncached, Wikidata busy): do not make the page wait for another
        asked += 1
        for other, idents in p.st.providers.crossref(p.session, ns, str(linked[ns])).items():
            if media_type_of(other) != kind:
                continue  # a game is not offered a book id, nor a book a store id
            for ident in idents:
                if other not in linked and all(i != ident for i, _ in found.get(other, [])):
                    found.setdefault(other, []).append((ident, ns))
    out = []
    for other, pairs in found.items():
        for ident, via in pairs:
            out.append({"provider": other, "label": integrations.store_name(other), "likely": len(pairs) == 1,
                        "external_id": ident, "title": g.view.get("title"), "via": f"Wikidata, from its {via} id",
                        "link": integrations.store_link(other, ident)})
    return out


def suggest_links(p: Page, g: Any) -> list[dict[str, Any]]:
    """Likely store matches for a game: first the ids Wikidata lists next to an id it already has (when the
    node enabled Wikidata), then matches found by its own name in every provider this node can search for its
    media type.

    Stores the game is already linked to are skipped. A name match is marked likely when the title is the same
    (ignoring case and punctuation) and, where both are known, the release year agrees."""
    from ..providers.registry import media_types

    throttle(p, f"link-suggest:{p.me.id}", 10, 60, "Too many searches")
    title = str(g.view.get("title") or "")
    year = str(g.view.get("release_date") or "")[:4]
    linked = dict(g.view.get("provider_ids") or {})
    kind = getattr(g, "media_type", None) or "game"
    norm = re.compile(r"\W+")
    out: list[dict[str, Any]] = _crossref_suggestions(p, g, linked)
    for name in sorted(p.st.providers.providers):
        if name in linked or not title or kind not in media_types(p.st.providers.providers[name]):
            continue
        try:
            found = [r.to_item() for r in p.st.providers.lookup(p.session, name, query=title)[:5]]
        except ProviderError:
            continue  # a provider that is down just has no suggestions
        for item in found:
            same = norm.sub(" ", item["title"].casefold()).strip() == norm.sub(" ", title.casefold()).strip()
            item_year = str(item.get("release_date") or "")[:4]
            likely = same and (not year or not item_year or year == item_year)
            if any(s["provider"] == name and s["external_id"] == item["external_id"] for s in out):
                continue  # Wikidata already suggested it
            out.append({"provider": name, "label": integrations.store_name(name), "likely": likely, **item})
    return sorted(out, key=lambda s: (not s["likely"], "via" not in s, s["provider"]))[:12]


@router.get("/game/{cid}/links", response_class=HTMLResponse)
def link_by_search(cid: str, p: Annotated[Page, Depends(user_page)], provider: Annotated[str, Query(max_length=40)] = "",
                   q: Annotated[str, Query(max_length=200)] = ""):
    """Find this game in a store by name (with the node's configured providers) and link the match."""
    from ..providers.registry import media_types

    g, _ = live_game(p, cid)
    kind = getattr(g, "media_type", None) or "game"
    names = sorted(n for n, prov in p.st.providers.providers.items() if kind in media_types(prov))
    query = q.strip() or ("" if provider else str(g.view.get("title") or ""))
    results, error = None, None
    suggestions = None
    if not provider and not q.strip():
        suggestions = suggest_links(p, g)
    elif provider and query:
        throttle(p, f"link-search:{p.me.id}", 30, 60, "Too many searches")
        try:
            results = [r.to_item() for r in p.st.providers.lookup(p.session, provider, query=query)[:15]]
        except ProviderError as exc:
            results, error = [], f"{provider} could not be searched: {exc}"
    return render(p, "link_store.html", g=g, v=g.view, names=names, provider=provider or (names[0] if names else ""),
                  q=query, results=results, error=error, stores=integrations.STORES, suggestions=suggestions)


@router.post("/game/{cid}/links")
def propose_store_link(cid: str, p: Annotated[Page, Depends(user_form)], store: Annotated[str, Form(max_length=50)],
                       ident: Annotated[str, Form(max_length=256)], other: Annotated[str, Form(max_length=50)] = "",
                       note: Annotated[str, Form()] = ""):
    """Link a store id (Steam, GOG, Epic, TheGamesDB...) to an existing game; reviewed like other contributions."""
    g, _ = live_game(p, cid)
    namespace = (other if store == "other" else store).strip().lower()
    try:
        pr = proposals.create(p.session, p.st.ctx, p.me, "link_ids", g.id, {"provider_ids": {namespace: ident}}, note)
    except AtlasError as exc:
        raise HTTPException(409 if exc.code == "conflict" else 400, str(exc)) from exc
    return go(f"/game/{g.id}", "applied" if pr.auto_applied else "proposed")


def _artwork_of(p: Page, cid: str, aid: str) -> tuple[Any, Any]:
    g, _ = live_game(p, cid)
    art = p.session.get(Artwork, aid)
    if art is None or art.canonical_id != g.id or art.blob_hash is None:
        raise HTTPException(404, "No such image.")
    return g, art


def _panel_ctx(p: Page, g: Any, art: Any) -> dict[str, Any]:
    votes, mine = artwork_votes(p, [art.id])
    settings = p.st.ctx.settings
    kind_items = [a for a in facets.merged_artwork(p.session, p.st.ctx, g.id) if a.kind == art.kind and a.blob_hash
                  and (a.id == art.id or a.duplicate_of is None)  # as in the gallery: exact duplicates once
                  and images.fetch_policy(settings, a.source, a.url) == "proxy"]
    ids = [a.id for a in kind_items]
    if art.id not in ids:
        raise HTTPException(404, "No such image.")
    pos = ids.index(art.id)
    return {"g": g, "a": artwork_view(p, art, votes, mine), "title": g.view.get("title"),
            "prev_id": ids[pos - 1] if pos > 0 else None, "next_id": ids[pos + 1] if pos + 1 < len(ids) else None,
            "pos": pos + 1, "count": len(ids), "can_vote": bool(p.user and mail.is_verified(p.st.ctx.settings, p.user)),
            "message": p.request.query_params.get("m")}


@router.get("/game/{cid}/artwork/{aid}", response_class=HTMLResponse)
def artwork_viewer(cid: str, aid: str, p: Annotated[Page, Depends(page)], panel: int = 0):
    """One image with its source, status, votes and actions. `?panel=1` is the fragment shown in the in-page viewer."""
    g, art = _artwork_of(p, cid, aid)
    ctx = _panel_ctx(p, g, art)
    if panel:
        resp = templates.TemplateResponse(p.request, "_art_panel.html", {"p": p, **ctx})
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    return render(p, "artwork.html", **ctx)


@router.post("/game/{cid}/artwork/{aid}/vote")
def vote_artwork(cid: str, aid: str, p: Annotated[Page, Depends(user_form)], value: Annotated[str, Form()]):
    """Up, down or clear. Needs the same verified email as commenting; it only records an opinion."""
    g, art = _artwork_of(p, cid, aid)
    if value not in ("up", "down", "clear"):
        raise HTTPException(400, "Choose up, down or clear.")
    if not mail.is_verified(p.st.ctx.settings, p.me):
        raise HTTPException(403, "Confirm your email address before voting (see your profile).")
    row = p.session.get(ArtworkVote, (art.id, p.me.id))
    if value == "clear":
        if row is not None:
            p.session.delete(row)
    elif row is None:
        p.session.add(ArtworkVote(artwork_id=art.id, user_id=p.me.id, value=1 if value == "up" else -1))
    else:
        row.value = 1 if value == "up" else -1
    return go(f"/game/{g.id}/artwork/{art.id}", "voted")


@router.post("/game/{cid}/artwork/{aid}/default")
def propose_default_artwork(cid: str, aid: str, p: Annotated[Page, Depends(user_form)], note: Annotated[str, Form()] = ""):
    """Propose this image as the default of its kind. Reviewed like any other artwork change."""
    g, art = _artwork_of(p, cid, aid)
    try:
        pr = proposals.create(p.session, p.st.ctx, p.me, "artwork_default", g.id, {"artwork_id": art.id}, note)
    except AtlasError as exc:
        raise HTTPException(400, str(exc)) from exc
    return go(f"/game/{g.id}/artwork/{art.id}", "applied" if pr.auto_applied else "proposed")


@router.post("/game/{cid}/artwork/upload")
async def upload_artwork(cid: str, p: Annotated[Page, Depends(user_form)], file: Annotated[UploadFile, File()],
                         kind: Annotated[str, Form()], note: Annotated[str, Form()] = ""):
    """Upload an image file instead of linking one. It is checked and stored here, then goes through review."""
    g, _ = live_game(p, cid)
    s = p.st.ctx.settings
    data = await file.read(s.media_max_bytes + 1)
    if len(data) > s.media_max_bytes:
        raise HTTPException(400, f"Images may be at most {s.media_max_bytes // 1024 // 1024} MB.")
    try:
        # decoded and written again without EXIF/XMP/ICC metadata (C12 §12.6.2, H41)
        data, ext, _ctype, w, h = images.reencode_upload(data, s)
        digest = hashlib.sha256(data).hexdigest()
        already_stored = images.find_blob(s, digest) is not None
        images.store(s, data, ext)
    except images.MediaError as exc:
        raise HTTPException(400, f"That is not a usable image: {exc}") from exc
    except OSError as exc:
        raise HTTPException(503, "This node cannot store images right now; the administrator should check "
                            "ATLAS_MEDIA_DIR.") from exc
    url = s.public_url.rstrip("/") + "/media/" + digest
    try:
        pr = proposals.create(p.session, p.st.ctx, p.me, "artwork", g.id,
                              {"items": [{"kind": kind, "url": url, "width": w, "height": h}]}, note)
    except AtlasError as exc:
        if not already_stored:  # a refused proposal (limits, unverified email...) must not leave a file behind
            found = images.find_blob(s, digest)
            if found is not None:
                found[0].unlink(missing_ok=True)
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
        raise HTTPException(409 if exc.code == "emit_protocol" else 400, str(exc)) from exc
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
        mine.append({"id": r.id, "when": r.created_at, "cid": r.canonical_id, "title": title_of(p, r.canonical_id),
                     "kind": r.kind, "status": r.status, "decision_note": r.decision_note})
    toks = [{"id": t.id, "name": t.name, "scopes": t.scopes, "last_used": t.last_used_at}
            for t in s.scalars(select(Token).where(Token.user_id == p.me.id, Token.revoked_at.is_(None)))]
    st = p.st.ctx.settings
    return {"mine": mine, "tokens": toks, "min_len": st.password_min_length, "sync_refusal": sync_token_refusal(p),
            "public_url": st.public_url.rstrip("/"),
            "has_password": p.me.password_hash != "!", "auto_threshold": st.auto_approve_reputation,
            "verification": st.verification_mode(), **extra}


@router.get("/me", response_class=HTMLResponse)
def me(p: Annotated[Page, Depends(user_page)]):
    return render(p, "me.html", **_me_ctx(p))


def sync_token_refusal(p: Page) -> str | None:
    """Why this member may not create a personal sync token now (#84), or None. Read-only either way."""
    from .. import mail

    if not p.st.ctx.settings.self_service_sync_tokens:
        return ("this node does not let members create sync tokens. An administrator can turn on \"Members may "
                "create their own sync tokens\" (Admin → Settings) or create one for you under Admin → Tokens")
    if not mail.account_verified(p.me):
        return "confirm your email address first: sync tokens are for verified accounts"
    return None


@router.post("/me/tokens", response_class=HTMLResponse)
def token_create(p: Annotated[Page, Depends(user_form)], name: Annotated[str, Form(max_length=128)],
                 scope: Annotated[str, Form()] = "read"):
    scopes = ["read"]  # personal tokens are read-only; see auth.authenticate
    if scope == "sync":
        if (why := sync_token_refusal(p)):
            return render(p, "me.html", status=403, **_me_ctx(p, token_error=f"No sync token: {why}."))
        scopes = ["read", "sync"]
    active = p.session.scalar(select(func.count()).select_from(Token).where(
        Token.user_id == p.me.id, Token.revoked_at.is_(None)))
    if active >= 10:
        raise HTTPException(409, "You already have 10 active tokens; revoke one first.")
    row, secret = auth.create_token(p.session, name.strip() or "token", scopes)
    row.user_id = p.me.id
    accounts.audit(p.session, p.me.id, "token_create", row.id, {"scopes": scopes})
    return render(p, "me.html", **_me_ctx(p, new_token=secret, new_token_sync=scope == "sync"))


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
        "when": r.created_at, "note": r.note, "own": r.proposer_id == p.me.id,
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
    elif r.kind == "artwork_default":
        card["entries"] = [f"Make this the default {r.payload['kind'].replace('_', ' ')}: "
                           f"{p.st.ctx.settings.public_url.rstrip('/')}/game/{r.canonical_id}/artwork/{r.payload['artwork_id']}"]
    elif r.kind == "link_ids":
        card["entries"] = [f"{integrations.store_name(ns)} ({ns}): {ident}"
                           for ns, ident in r.payload["provider_ids"].items()]
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
        raise HTTPException({"not_found": 404, "forbidden": 403}.get(
            exc.code, 409 if exc.code in ("conflict", "emit_protocol") else 400), str(exc)) from exc
    return go("/review", "approved" if decision == "approve" else "rejected")


@router.post("/review/{pid}/revert")
def review_revert(pid: str, p: Annotated[Page, Depends(review_form)], note: Annotated[str, Form()] = ""):
    try:
        pr = proposals.revert(p.session, p.st.ctx, p.me, pid, note)
    except AtlasError as exc:
        raise HTTPException(409 if exc.code in ("conflict", "emit_protocol") else 404 if exc.code == "not_found" else 400,
                            str(exc)) from exc
    return go(f"/game/{pr.canonical_id}" if pr.canonical_id else "/review", "reverted")


def _shown_explain(explain: dict[str, Any] | None) -> dict[str, Any]:
    """The per-field explain the game page lists: the explain v2 `_meta` block (C11 §11.7.1) is not a field."""
    return {k: e for k, e in (explain or {}).items() if not k.startswith("_")}


def _brief(p: Page, cid: str) -> dict[str, Any]:
    row = p.session.get(Canonical, cid)
    v = row.view if row else {}
    return {"id": cid, "title": v.get("title", cid), "year": release_year(v),
            "developer": v.get("developer", ""), "sources": ", ".join(v.get("sources", []))}


def _concerns(item: ReviewItem) -> int:
    """How many rows an `unmerge_check` item asks a curator to look at (its evidence lists them, bounded)."""
    if item.kind != "unmerge_check":
        return 0
    return sum(len(e.get("value") or []) for e in item.evidence or [] if isinstance(e, dict)
               and isinstance(e.get("value"), list))


@router.get("/review/duplicates", response_class=HTMLResponse)
def duplicates(p: Annotated[Page, Depends(review_page)]):
    rows = p.session.scalars(select(ReviewItem).where(ReviewItem.status == "open")
                             .order_by(ReviewItem.created_at).limit(50)).all()
    # An `unmerge_check` item (C11 §11.10.3) names the survivor and the record split off it: merging them would
    # redo the merge that was just undone, so it is checked and dismissed, never merged from here.
    items = [{"id": r.id, "kind": r.kind, "confidence": r.confidence, "canonical": _brief(p, r.canonical_id),
              "mergeable": r.kind not in matching.UNMERGEABLE_KINDS, "concerns": _concerns(r),
              "candidates": [_brief(p, c) for c, _s in r.candidates[:3]]} for r in rows]
    return render(p, "duplicates.html", items=items)


@router.post("/review/duplicates/{rid}/merge")
def duplicates_merge(rid: str, p: Annotated[Page, Depends(review_form)], into: Annotated[str, Form()]):
    item = p.session.get(ReviewItem, rid)
    unmergeable = item is None or item.status != "open" or item.kind in matching.UNMERGEABLE_KINDS
    if unmergeable or into not in {c for c, _ in item.candidates}:
        raise HTTPException(400, "That merge is not available.")
    try:
        _guard_upstream(p.session, p.st, item.canonical_id)
        _guard_upstream(p.session, p.st, into)
        service.merge_canonicals(p.session, p.st.ctx, item.canonical_id, into, actor=p.me.username, cause="review",
                                 review_id=item.id, decision_id=item.decision_id)
    except AtlasError as exc:
        raise HTTPException(409, str(exc)) from exc
    return go("/review/duplicates", "merged")


@router.post("/review/duplicates/{rid}/dismiss")
def duplicates_dismiss(rid: str, p: Annotated[Page, Depends(review_form)]):
    item = p.session.get(ReviewItem, rid)
    if item is None:
        raise HTTPException(404, "No such item.")
    matching.close_review(p.session, item, "dismissed", p.me.username)
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
    if field == "releases":  # one release per line: date | platform | region | edition | category
        val = [_release_line(line) for line in lines(value)]
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
        raise HTTPException(409 if exc.code == "emit_protocol" else 404, str(exc)) from exc
    return go(f"/game/{cid}", "unlocked")


def _release_line(line: str) -> dict[str, Any]:
    """`2011-04 | PS3 | eu | | full` as a release entry (validated by `service.set_lock`)."""
    parts = [x.strip() for x in line.split("|")] + [""] * 5
    entry: dict[str, Any] = {"date": parts[0], "precision": dates.precision_of(parts[0]) or "day",
                             "category": parts[4] or "full"}
    entry.update({k: v for k, v in zip(("platform", "region", "edition"), parts[1:4], strict=True) if v})
    return entry


@router.post("/observation/{oid}/detach")
def observation_detach(oid: str, p: Annotated[Page, Depends(review_form)]):
    obs = p.session.get(Observation, oid)
    if obs is None or not obs.canonical_id:
        raise HTTPException(404, "No such record.")
    try:
        _guard_upstream(p.session, p.st, obs.canonical_id)
        new = service.detach_observation(p.session, p.st.ctx, oid, actor=p.me.username)
    except AtlasError as exc:
        raise HTTPException(409, str(exc)) from exc
    return go(f"/game/{new.id}", "detached")


# ---- relations between works (C6) ---------------------------------------------------------------------

RELATION_PREVIEW = 50  # per list on the record page; the editor and the API show up to 200


def _relations_page(p: Page, g: Canonical, *, q: str = "", kind: str = "", error: str | None = None,
                    status: int = 200):
    s = p.session
    q = q.strip()[:200]
    results: list[Canonical] | None = None
    if q:
        taken = {g.id}
        results = [r for r in service.search(s, q, 21) if r.id not in taken][:20]
    # bounded: MAX_OUTGOING local rows plus at most MAX_OUTGOING from the upstream feed
    own = list(s.scalars(select(relations.Relation).where(relations.Relation.from_id == g.id,
                                                          relations.Relation.ended_at.is_(None))
                         .order_by(relations.Relation.id).limit(2 * relations.MAX_OUTGOING)))
    history = relations.history(s, g.id)
    brief = relations._brief(s, [r.to_id for r in own] + [c for r in history for c in (r.from_id, r.to_id)])
    titles = {cid: (b.get("title") or "(new record)") for cid, b in brief.items()}
    own.sort(key=lambda r: (relations.type_order(r.type), titles.get(r.to_id, "").casefold(), r.to_id))
    return render(p, "relations_edit.html", status=status, g=g, v=g.view, own=own,
                  titles={r.to_id: titles.get(r.to_id, "(new record)") for r in own}, rel=relations.of(s, g.id),
                  rel_labels=relations.LABELS, types=relations.TYPES,
                  member_target=relations.MEMBER_TARGET, q=q, kind=kind if kind in relations.TYPES else "",
                  results=results, error=error, history=history,
                  history_titles={c: titles.get(c, "(new record)") for r in history for c in (r.from_id, r.to_id)},
                  gated=relations.gated(p.st.ctx), gate_message=relations.gate_message(p.st.ctx),
                  local=p.st.ctx.settings.role != "authority")


@router.get("/game/{cid}/relations", response_class=HTMLResponse)
def relations_edit(cid: str, p: Annotated[Page, Depends(review_page)], q: Annotated[str, Query(max_length=200)] = "",
                   type: Annotated[str, Query(max_length=32)] = ""):
    """The relation editor: the record's own relations with Remove buttons, and a title search to add one."""
    try:
        g, redirected = live_game(p, cid)
    except AtlasError as exc:
        raise HTTPException(404, "No such record.") from exc
    if redirected:
        return RedirectResponse(f"/game/{g.id}/relations", status_code=301)
    return _relations_page(p, g, q=q, kind=type)


def _relation_change(p: Page, cid: str, change, kind: str = "") -> Any:
    try:
        g, _redirected = live_game(p, cid)
    except AtlasError as exc:
        raise HTTPException(404, "No such record.") from exc
    try:
        change(g)
    except AtlasError as exc:
        p.session.rollback()
        g, _redirected = live_game(p, cid)
        return _relations_page(p, g, q=p.request.query_params.get("q", ""), kind=kind, error=str(exc),
                               status=400 if exc.code == "invalid" else 404 if exc.code == "not_found" else 409)
    return None


@router.post("/game/{cid}/relations/add")
def relations_add(cid: str, p: Annotated[Page, Depends(review_form)], to: Annotated[str, Form(max_length=40)],
                  type: Annotated[str, Form(max_length=32)], q: Annotated[str, Form(max_length=200)] = ""):
    local = p.st.ctx.settings.role != "authority"
    failed = _relation_change(p, cid, lambda g: relations.add(p.session, p.st.ctx, g.id, to.strip(), type,
                                                              actor=p.me.username, local_only=local), type)
    if failed is not None:
        return failed
    return go(f"/game/{cid}/relations" + (f"?{urlencode({'q': q, 'type': type})}" if q else ""), "relation-added")


@router.post("/game/{cid}/relations/remove")
def relations_remove(cid: str, p: Annotated[Page, Depends(review_form)], to: Annotated[str, Form(max_length=40)],
                     type: Annotated[str, Form(max_length=32)]):
    failed = _relation_change(p, cid, lambda g: relations.end(p.session, p.st.ctx, g.id, to.strip(), type,
                                                              actor=p.me.username))
    if failed is not None:
        return failed
    return go(f"/game/{cid}/relations", "relation-removed")


# ---- administration -----------------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
def admin(p: Annotated[Page, Depends(admin_page)]):
    s = p.session
    ctx = p.st.ctx
    users = list(s.scalars(select(User).order_by(User.created_at)))
    audit = []
    for a in s.scalars(select(AuditEntry).order_by(AuditEntry.created_at.desc()).limit(40)):
        who = s.get(User, a.actor_id) if a.actor_id else None
        d = a.detail if isinstance(a.detail, dict) else {}
        via = None
        if d.get("via") == "group":  # C10 §6.4 provenance: the asserting node and the grant used
            via = {"origin": groups.gc_fp(d["origin_node"]) if d.get("origin_node") else None,
                   "grant": d.get("grant_serial"), "session": d.get("session")}
        audit.append({"when": a.created_at, "who": who.username if who else "system", "action": a.action,
                      "target": a.target, "via": via})
    from . import setup_checklist

    setup = setup_checklist.card(s, ctx.settings, user_id=p.user.id if p.user else None,
                                 providers=len(p.st.providers.providers))
    return render(p, "admin.html", setup=setup, fed_optional=not setup["connected"], users=users, audit=audit,
                  role=ctx.settings.role, node_id=ctx.identity.node_id,
                  public_key=ctx.identity.public_b64, feed_head=feed.head_seq(s), upstream=ctx.settings.upstream_url,
                  providers=sorted(p.st.providers.providers), registration=ctx.settings.registration,
                  cursors=[{"upstream": c.upstream, "last_seq": c.last_seq, "updated": c.updated_at}
                           for c in s.scalars(select(SyncCursor))])


@router.post("/admin/setup/{action}")
def admin_setup_visibility(action: str, p: Annotated[Page, Depends(admin_form)]):
    """Hide or show the Getting started card (§3.7). Stored per node in node_state."""
    from . import setup_checklist

    if action not in ("hide", "show"):
        raise HTTPException(404)
    setup_checklist.set_hidden(p.session, action == "hide")
    accounts.audit(p.session, p.user.id, f"setup_{action}", "admin")
    p.session.commit()
    return go("/admin" + ("#getting-started" if action == "show" else ""), "saved")


USER_ROLES = ("user", "curator", "admin")
USER_STATUSES = ("active", "suspended")
USER_LIMIT = 200


def _users_page(p: Page, *, q: str = "", role: str = "", status: str = "", error: str | None = None,
                http_status: int = 200):
    """The user list with its filters. Filter values that are not known are ignored rather than trusted."""
    s = p.session
    query = select(User)
    q = q.strip()[:80]
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        query = query.where(User.username.ilike(like, escape="\\") | User.email.ilike(like, escape="\\"))
    if role in USER_ROLES:
        query = query.where(User.role == role)
    if status in USER_STATUSES:
        query = query.where(User.status == status)
    total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
    users = list(s.scalars(query.order_by(User.created_at).limit(USER_LIMIT)))
    counts = {r: n for r, n in s.execute(select(User.role, func.count()).group_by(User.role))}
    suspended = s.scalar(select(func.count()).select_from(User).where(User.status == "suspended")) or 0
    return render(p, "admin_users.html", status=http_status, users=users, total=total, limit=USER_LIMIT, uq=q,
                  role=role if role in USER_ROLES else "", filter_status=status if status in USER_STATUSES else "",
                  roles=USER_ROLES, statuses=USER_STATUSES, counts=counts, suspended=suspended, error=error,
                  group_grants=_group_grants_by_user(p), back=_users_back(q, role, status))


def _users_back(q: str, role: str, status: str) -> str:
    back = urlencode({k: v for k, v in (("q", q.strip()[:80]), ("role", role if role in USER_ROLES else ""),
                                         ("status", status if status in USER_STATUSES else "")) if v})
    return "/admin/users" + (f"?{back}" if back else "")


def _group_grants_by_user(p: Page) -> dict[str, list[dict]] | None:
    """#66: the rights a user holds here through the owner group (C10 §6), next to the local role, read only.
    Grants are signed elsewhere (Node group, Grants); this page never creates one. None when not in a group."""
    from .. import groups
    from ..models import GroupAccountLink

    group = groups.active(p.session)
    if group is None:
        return None
    here = [g for g in groups.grant_rows(p.session, group, p.st.ctx.identity.node_id) if g["here"]]
    if not here:
        return {}
    links = {link.account_uid: link.user_id for link in p.session.scalars(
        select(GroupAccountLink).where(GroupAccountLink.group_id == group.group_id))}
    out: dict[str, list[dict]] = {}
    for g in here:
        if (uid := links.get(g["account_uid"])) is not None:
            out.setdefault(uid, []).append({"role": g["role"], "serial": g["serial"], "not_after": g["not_after"]})
    return out


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(p: Annotated[Page, Depends(admin_page)], q: Annotated[str, Query(max_length=80)] = "",
                role: Annotated[str, Query(max_length=16)] = "", status: Annotated[str, Query(max_length=16)] = ""):
    return _users_page(p, q=q, role=role, status=status)


@router.post("/admin/users/{uid}", response_class=HTMLResponse)
def admin_user(uid: str, p: Annotated[Page, Depends(admin_form)], role: Annotated[str, Form()],
               status: Annotated[str, Form()], fq: Annotated[str, Form(max_length=80)] = "",
               frole: Annotated[str, Form(max_length=16)] = "", fstatus: Annotated[str, Form(max_length=16)] = "",
               oidc_field: Annotated[str, Form(alias="has_oidc")] = "", oidc: Annotated[str, Form()] = ""):
    """One save per row (#66): role, status and, when the row's form carries it, external sign-in."""
    target = p.session.get(User, uid)
    if target is None:
        raise HTTPException(404, "No such user.")
    try:
        if role != target.role:
            accounts.set_role(p.session, p.me, target, role)
        if status != target.status:
            accounts.set_status(p.session, p.me, target, status)
        if oidc_field and (oidc == "on") != bool(target.oidc_allowed):
            if target.id == p.me.id:
                raise AtlasError("you cannot change your own account here", "conflict")
            target.oidc_allowed = oidc == "on"
            accounts.audit(p.session, p.me.id, "oidc_allow", target.id, {"allowed": target.oidc_allowed})
    except AtlasError as exc:
        # Show the reason on the list itself, with the controls still there to try again. Nothing was changed:
        # the request's session is rolled back when the response is an error status.
        p.session.rollback()
        return _users_page(p, q=fq, role=frole, status=fstatus, error=f"Could not change {target.username}: {exc}",
                           http_status=409 if exc.code in ("conflict", "forbidden") else 400)
    return go(_users_back(fq, frole, fstatus), "saved")


def web_headers(response, path: str) -> None:
    if not path.startswith(("/api/", "/docs", "/openapi.json")):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


_ = Request
