"""Admin: media types and custom fields."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from .. import accounts, custom
from ..models import Canonical, MediaType
from ..service import AtlasError
from .deps import Page as Ctx
from .deps import admin_form, admin_page, form_dict, go
from .routes import render

router = APIRouter()


def _err(exc: AtlasError) -> HTTPException:
    return HTTPException({"not_found": 404, "conflict": 409}.get(exc.code, 400), str(exc))


@router.get("/admin/schema", response_class=HTMLResponse)
def schema_home(p: Annotated[Ctx, Depends(admin_page)]):
    rows = []
    for t in custom.types(p.session):
        rows.append({"t": t, "fields": custom.fields_for(p.session, t.key),
                     "count": p.session.query(Canonical).filter(Canonical.media_type == t.key,
                                                                Canonical.merged_into.is_(None)).count()})
    from ..custom_compat import renames

    return render(p, "admin_schema.html", rows=rows, kinds=custom.KINDS, renamed_fields=renames(p.session))


@router.post("/admin/schema/types")
def type_save(p: Annotated[Ctx, Depends(admin_form)], f: Annotated[dict[str, str], Depends(form_dict)]):
    labels = {k[6:]: v for k, v in f.items() if k.startswith("label_")}
    try:
        custom.save_type(p.session, p.st.ctx, f.get("key", ""), f.get("label", ""), enabled=f.get("enabled") == "on",
                         position=int(f.get("position") or 100), labels=labels)
    except (AtlasError, ValueError) as exc:
        p.session.rollback()
        raise (_err(exc) if isinstance(exc, AtlasError) else HTTPException(400, "Order must be a number.")) from exc
    accounts.audit(p.session, p.me.id, "schema_type_save", f.get("key", "")[:64])
    return go("/admin/schema", "saved")


@router.post("/admin/schema/types/{key}/delete")
def type_delete(key: str, p: Annotated[Ctx, Depends(admin_form)]):
    try:
        custom.delete_type(p.session, p.st.ctx, key)
    except AtlasError as exc:
        raise _err(exc) from exc
    accounts.audit(p.session, p.me.id, "schema_type_delete", key[:64])
    return go("/admin/schema", "saved")


@router.get("/admin/schema/{mtype}", response_class=HTMLResponse)
def type_edit(mtype: str, p: Annotated[Ctx, Depends(admin_page)]):
    t = p.session.get(MediaType, mtype)
    if t is None:
        raise HTTPException(404, "No such media type.")
    return render(p, "admin_type.html", t=t, core=custom.CORE_LABELS, fields=custom.fields_for(p.session, mtype))


@router.get("/admin/schema/{mtype}/fields/{key}", response_class=HTMLResponse)
def field_form(mtype: str, key: str, p: Annotated[Ctx, Depends(admin_page)]):
    t = p.session.get(MediaType, mtype)
    if t is None:
        raise HTTPException(404, "No such media type.")
    row: Any = None
    if key != "new":
        rows = [f for f in custom.fields_for(p.session, mtype) if f.key == key]
        if not rows:
            raise HTTPException(404, "No such field.")
        row = rows[0]
    return render(p, "admin_field.html", t=t, row=row, kinds=custom.KINDS, error=None)


@router.post("/admin/schema/{mtype}/fields")
def field_save(mtype: str, p: Annotated[Ctx, Depends(admin_form)], f: Annotated[dict[str, str], Depends(form_dict)]):
    try:
        custom.save_field(p.session, p.st.ctx, mtype, f.get("key", ""), f.get("label", ""), f.get("kind", ""),
                          choices=f.get("choices", ""), help=f.get("help", ""), public=f.get("public") == "on",
                          editable=f.get("editable") == "on", position=int(f.get("position") or 100))
    except (AtlasError, ValueError) as exc:
        p.session.rollback()
        raise (_err(exc) if isinstance(exc, AtlasError) else HTTPException(400, "Order must be a number.")) from exc
    accounts.audit(p.session, p.me.id, "schema_field_save", f"{mtype}/{f.get('key', '')}"[:64])
    return go(f"/admin/schema/{mtype}", "saved")


@router.post("/admin/schema/{mtype}/fields/{key}/delete")
def field_delete(mtype: str, key: str, p: Annotated[Ctx, Depends(admin_form)]):
    try:
        custom.delete_field(p.session, p.st.ctx, mtype, key)
    except AtlasError as exc:
        raise _err(exc) from exc
    accounts.audit(p.session, p.me.id, "schema_field_delete", f"{mtype}/{key}"[:64])
    return go(f"/admin/schema/{mtype}", "saved")
