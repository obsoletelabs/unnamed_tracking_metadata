"""Admin pages: settings, branding, Markdown pages."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .. import integrations, media, pages, runtime, secrets_box
from ..models import AuditEntry, Page, Setting
from .deps import Page as Ctx
from .deps import admin_form, admin_page, go, page
from .routes import render

router = APIRouter()
LOGO_MAX = 512 * 1024


def _ink(color: str) -> str:
    """Black or white text, whichever is readable on this accent colour."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#08111f" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


def theme_css(s) -> str:
    d, light = s.accent_color, s.accent_color_light
    dark_rule = f"--accent:{d};--accent-ink:{_ink(d)};"
    light_rule = f"--accent:{light};--accent-ink:{_ink(light)};"
    return (f":root{{{dark_rule}}}\n@media (prefers-color-scheme: light){{:root:not([data-theme=dark]){{{light_rule}}}}}\n"
            f":root[data-theme=dark]{{{dark_rule}}}\n:root[data-theme=light]{{{light_rule}}}\n")


@router.get("/theme.css")
def theme(request: Request, p: Annotated[Ctx, Depends(page)]):
    css = theme_css(p.st.ctx.settings)
    etag = '"' + hashlib.sha256(css.encode()).hexdigest()[:16] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(css, media_type="text/css", headers={"ETag": etag, "Cache-Control": "public, max-age=300"})


@router.get("/p/{slug}", response_class=HTMLResponse)
def show_page(slug: str, p: Annotated[Ctx, Depends(page)]):
    row = p.session.get(Page, slug)
    if row is None or not row.published:
        raise HTTPException(404, "No such page.")
    return render(p, "page.html", row=row, body=pages.render_markdown(row.body))


# ---- settings -----------------------------------------------------------------------------------------

def _groups(p: Ctx) -> list[tuple[str, list[dict[str, Any]]]]:
    s = p.st.ctx.settings
    overridden = {r.key for r in p.session.scalars(select(Setting))}
    groups: dict[str, list[dict[str, Any]]] = {}
    for spec in runtime.REGISTRY:
        if spec.group == runtime.GROUP_POLICY:
            continue  # edited on /admin/group, by local administrators only (C10 §6.3)
        attr = runtime.current_value(spec, s)
        groups.setdefault(spec.group, []).append({"spec": spec, "value": runtime.display(spec, attr),
                                                  "raw": runtime.from_attr(spec, attr), "overridden": spec.key in overridden})  # noqa: E501
    return list(groups.items())


@router.get("/admin/settings", response_class=HTMLResponse)
def settings_form(p: Annotated[Ctx, Depends(admin_page)]):
    return render(p, "admin_settings.html", groups=_groups(p), error=None)


@router.post("/admin/settings")
async def settings_save(request: Request, p: Annotated[Ctx, Depends(admin_form)]):
    form = await request.form()
    values: dict[str, Any] = {}
    for spec in runtime.REGISTRY:
        if spec.group == runtime.GROUP_POLICY:
            continue  # never from this form: /admin/group is the only (local-admin) way to change them
        if spec.kind == "bool":
            values[spec.key] = form.get(spec.key) == "on"
        elif spec.key in form:
            values[spec.key] = form[spec.key]
    try:
        changed = runtime.save(p.session, p.st.base, p.me.id, values)  # type: ignore[arg-type]
    except runtime.SettingError as exc:
        p.session.rollback()
        return render(p, "admin_settings.html", status=400, groups=_groups(p), error=str(exc))
    p.st.refresh(p.session, force=True)
    return go("/admin/settings", "saved" if changed else "nochange")


@router.post("/admin/settings/reset/{key}")
def settings_reset(key: str, p: Annotated[Ctx, Depends(admin_form)]):
    if key not in runtime.BY_KEY or runtime.BY_KEY[key].group == runtime.GROUP_POLICY:
        raise HTTPException(404, "Unknown setting.")
    runtime.reset(p.session, p.me.id, key)
    p.st.refresh(p.session, force=True)
    return go("/admin/settings", "saved")


# ---- logo ---------------------------------------------------------------------------------------------

@router.post("/admin/branding/logo")
async def logo_upload(p: Annotated[Ctx, Depends(admin_form)], file: Annotated[UploadFile, File()]):
    data = await file.read(LOGO_MAX + 1)
    if len(data) > LOGO_MAX:
        raise HTTPException(400, "The logo must be smaller than 512 KB.")
    s = p.st.ctx.settings
    try:
        data, ext, _ctype, w, h = media.reencode_upload(data, s)  # metadata stripped (C12 §12.6.2)
    except media.MediaError as exc:
        raise HTTPException(400, f"That is not a usable image: {exc}") from exc
    if max(w, h) > 1024:
        raise HTTPException(400, "The logo may be at most 1024 pixels wide or tall.")
    digest = media.store(s, data, ext)
    _set(p, "logo_hash", digest)
    return go("/admin/settings", "saved")


@router.post("/admin/branding/logo/remove")
def logo_remove(p: Annotated[Ctx, Depends(admin_form)]):
    runtime.reset(p.session, p.me.id, "logo_hash")
    p.st.refresh(p.session, force=True)
    return go("/admin/settings", "saved")


def _set(p: Ctx, key: str, value: Any) -> None:
    row = p.session.get(Setting, key)
    if row is None:
        p.session.add(Setting(key=key, value=value, updated_by=p.me.id))
    else:
        row.value, row.updated_by = value, p.me.id
    p.session.add(AuditEntry(actor_id=p.me.id, action="setting", target=key, detail={"to": value}))
    p.session.flush()
    p.st.refresh(p.session, force=True)


# ---- pages --------------------------------------------------------------------------------------------

@router.get("/admin/pages", response_class=HTMLResponse)
def pages_list(p: Annotated[Ctx, Depends(admin_page)]):
    rows = p.session.scalars(select(Page).order_by(Page.position, Page.slug)).all()
    return render(p, "admin_pages.html", rows=rows)


@router.get("/admin/pages/new", response_class=HTMLResponse)
def page_new(p: Annotated[Ctx, Depends(admin_page)]):
    return render(p, "admin_page_edit.html", row=None, error=None)


@router.get("/admin/pages/{slug}", response_class=HTMLResponse)
def page_edit(slug: str, p: Annotated[Ctx, Depends(admin_page)]):
    row = p.session.get(Page, slug)
    if row is None:
        raise HTTPException(404, "No such page.")
    return render(p, "admin_page_edit.html", row=row, error=None)


@router.post("/admin/pages")
def page_save(p: Annotated[Ctx, Depends(admin_form)], slug: Annotated[str, Form(max_length=64)],
              title: Annotated[str, Form(max_length=200)], body: Annotated[str, Form(max_length=100_000)] = "",
              position: Annotated[int, Form(ge=0, le=10000)] = 100, published: Annotated[str, Form()] = "",
              in_nav: Annotated[str, Form()] = ""):
    slug = slug.strip().lower()
    if not runtime.valid_slug(slug) or slug in ("new",):
        return render(p, "admin_page_edit.html", status=400, row=None, error="The address may use a-z, 0-9 and '-'.",
                      form={"slug": slug, "title": title, "body": body})
    row = p.session.get(Page, slug)
    if row is None:
        row = Page(slug=slug, title="", body="")
        p.session.add(row)
    row.title, row.body, row.position = title.strip() or slug, body, position
    row.published, row.in_nav, row.updated_by = published == "on", in_nav == "on", p.me.id
    p.session.add(AuditEntry(actor_id=p.me.id, action="page_save", target=slug, detail=None))
    return go("/admin/pages", "saved")


@router.post("/admin/pages/{slug}/delete")
def page_delete(slug: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(Page, slug)
    if row is None:
        raise HTTPException(404, "No such page.")
    p.session.delete(row)
    p.session.add(AuditEntry(actor_id=p.me.id, action="page_delete", target=slug, detail=None))
    return go("/admin/pages", "saved")


# ---- integrations --------------------------------------------------------------------------------------

DOCS = "https://github.com/obsoletelabs/unnamed_tracking_metadata/blob/v2/docs/guide"
CATALOGUE = (
    {"name": "steam", "label": "Steam", "gives": "Descriptions, genres, features, languages, artwork, achievements",
     "needs": ["ATLAS_STEAM_ENABLED"], "optional": ["ATLAS_STEAM_API_KEY (achievement names)"]},
    {"name": "igdb", "label": "IGDB", "gives": "Descriptions, genres, themes, scores, cross-store ids",
     "needs": ["ATLAS_IGDB_CLIENT_ID", "ATLAS_IGDB_CLIENT_SECRET"], "optional": []},
    {"name": "rawg", "label": "RAWG", "gives": "Descriptions, genres, tags, scores",
     "needs": ["ATLAS_RAWG_API_KEY"], "optional": []},
    {"name": "tgdb", "label": "TheGamesDB", "gives": "Overviews, alternate titles, player counts, box art "
     "(official API; a key has a monthly allowance; data kept on this node only)",
     "needs": ["ATLAS_TGDB_API_KEY"], "optional": []},
    {"name": "openlibrary", "label": "Open Library", "gives": "Books: descriptions, authors, subjects, covers "
     "(keyless, CC0; needs a `book` media type)", "needs": ["ATLAS_OPENLIBRARY_ENABLED"], "optional": []},
    {"name": "wikidata", "label": "Wikidata", "gives": "Store link suggestions: GOG, Epic, itch.io, TheGamesDB and "
     "Steam ids for a game from an id it already has (keyless, CC0)", "needs": ["ATLAS_WIKIDATA_ENABLED"],
     "optional": []},
    {"name": "gog", "label": "GOG", "gives": "No adapter: GOG publishes no API terms for its catalogue. Ids are "
     "suggested through Wikidata, linked by members, or sent by plugins.", "needs": [], "optional": []},
    {"name": "epic", "label": "Epic Games Store", "gives": "No adapter: Epic offers no public catalogue API and its "
     "terms forbid scraping the store. Ids are suggested through Wikidata or linked by hand.", "needs": [],
     "optional": []},
    {"name": "itch", "label": "itch.io", "gives": "No adapter: the official API only covers a key holder's own "
     "games and purchases, with no catalogue search. Ids (`creator/game`) are suggested through Wikidata or linked "
     "by hand.", "needs": [], "optional": []},
)


def _field_state(p: Ctx, field: str) -> dict[str, Any]:
    stored = p.session.get(Setting, integrations.PREFIX + field) is not None
    in_env = bool(getattr(p.st.base, field, None))
    return {"stored": stored, "env": in_env,
            "state": "Stored in the database" if stored else ("Set in the environment" if in_env else "Not set")}


@router.post("/admin/media/retry")
def media_retry(p: Annotated[Ctx, Depends(admin_form)]):
    n = media.retry_pending(p.session)
    p.session.add(AuditEntry(actor_id=p.me.id, action="media_retry", target="artwork", detail={"images": n}))
    return go("/admin/integrations", "saved")


@router.post("/admin/media/clear")
def media_clear(p: Annotated[Ctx, Depends(admin_form)], confirm: Annotated[str, Form()] = ""):
    if confirm != "clear":
        raise HTTPException(400, "Type clear to confirm.")
    n = media.clear_cache(p.session, p.st.ctx.settings)
    p.session.add(AuditEntry(actor_id=p.me.id, action="media_clear", target="artwork", detail={"images": n}))
    return go("/admin/integrations", "saved")


@router.get("/admin/integrations", response_class=HTMLResponse)
def integrations_page(p: Annotated[Ctx, Depends(admin_page)]):
    active = p.st.providers.providers
    failures = p.st.providers.failure_counts()
    rows = []
    for item in CATALOGUE:
        spec = integrations.PROVIDERS.get(item["name"])
        fields = [{"key": f, "label": label, **_field_state(p, f)} for f, label in (spec[1] if spec else ())]
        toggle = integrations.TOGGLE_OF.get(item["name"])
        is_active = item["name"] in active or (item["name"] == "wikidata" and p.st.providers.crossref_source is not None)
        rows.append({**item, "active": is_active, "available": bool(item["needs"]), "fields": fields,
                     "failures": failures.get(item["name"], 0), "editable": spec is not None,
                     "toggle": {"key": toggle[0], "label": toggle[1], "on": bool(getattr(p.st.ctx.settings, toggle[0]))}
                     if toggle else None, "importable": item["name"] in active})
    return render(p, "admin_integrations.html", rows=rows, art=media.stats(p.session),
                  caching=p.st.ctx.settings.media_cache_enabled, docs=DOCS, can_store=secrets_box.available(p.st.base),
                  stores=integrations.STORES,
                  steam_on=p.st.ctx.settings.steam_enabled)


@router.post("/admin/integrations/{name}")
async def integrations_save(name: str, request: Request, p: Annotated[Ctx, Depends(admin_form)]):
    spec = integrations.PROVIDERS.get(name)
    if spec is None:
        raise HTTPException(404, "Unknown provider.")
    form = await request.form()
    changed: list[str] = []
    for field, _label in spec[1]:
        key = integrations.PREFIX + field
        value = str(form.get(field, "")).strip()
        row = p.session.get(Setting, key)
        if form.get("clear_" + field) == "on":
            if row is not None:
                p.session.delete(row)
                changed.append(field)
            continue
        if not value:
            continue
        if len(value) > 300 or any(ord(c) < 32 for c in value):
            raise HTTPException(400, "Credentials are at most 300 characters, on one line.")
        if not secrets_box.available(p.st.base):
            raise HTTPException(400, "Set ATLAS_SECRET_KEY (see `atlas secret-key`) before storing credentials here.")
        token = secrets_box.encrypt(p.st.base, value)
        if row is None:
            p.session.add(Setting(key=key, value=token, updated_by=p.me.id))
        else:
            row.value, row.updated_by = token, p.me.id
        changed.append(field)
    if name in integrations.TOGGLE_OF:
        field = integrations.TOGGLE_OF[name][0]
        key = integrations.PREFIX + field
        wanted = form.get(field) == "on"
        row = p.session.get(Setting, key)
        if (row.value if row is not None else getattr(p.st.base, field)) != wanted:
            if row is None:
                p.session.add(Setting(key=key, value=wanted, updated_by=p.me.id))
            else:
                row.value, row.updated_by = wanted, p.me.id
            changed.append(field)
    if changed:  # field names only, never values
        p.session.add(AuditEntry(actor_id=p.me.id, action="integration", target=name, detail={"fields": changed}))
    p.session.flush()
    p.st.refresh(p.session, force=True)
    return go("/admin/integrations", "saved" if changed else "nochange")
