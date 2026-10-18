"""Admin pages: settings, branding, Markdown pages."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .. import media, pages, runtime
from ..models import AuditEntry, Page, Setting
from .deps import Page as Ctx
from .deps import admin_form, admin_page, go, page
from .routes import render, when

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
    if key not in runtime.BY_KEY:
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
        ext, _ctype, w, h = media.validate_image(data, s)
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
    return render(p, "admin_pages.html", rows=rows, when=when)


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
