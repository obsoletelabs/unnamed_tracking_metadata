"""Administrators: see scheduled backups and take one now. Archives are never served over HTTP (they hold secrets)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from .. import backup
from ..models import AuditEntry
from .deps import Page as Ctx
from .deps import admin_form, admin_page, go
from .routes import render

router = APIRouter()


@router.get("/admin/backup", response_class=HTMLResponse)
def backup_page(p: Annotated[Ctx, Depends(admin_page)], error: str | None = None):
    s = p.st.ctx.settings
    return render(p, "admin_backup.html", enabled=bool(s.backup_dir), backups=backup.latest(s), error=error,
                  directory=s.backup_dir, interval=s.backup_interval_hours, keep=s.backup_keep)


@router.post("/admin/backup")
def backup_now(p: Annotated[Ctx, Depends(admin_form)]):
    s = p.st.ctx.settings
    if not s.backup_dir:
        return backup_page(p, error="Set ATLAS_BACKUP_DIR to a persistent folder first, then restart.")
    engine = p.session.get_bind()
    try:
        out = backup.scheduled(s, engine)  # type: ignore[arg-type]
        backup.verify(out)
    except (backup.BackupError, OSError) as exc:
        return backup_page(p, error=f"Backup failed: {exc}")
    p.session.add(AuditEntry(actor_id=p.user.id, action="backup", target=out.name, detail=None))
    return go("/admin/backup", "saved", p)
