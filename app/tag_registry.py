from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .main import Base, Db, GameTag, MetadataProvider, Tag, require_api_token, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="/app/templates")

class TagRegistryEntry(Base):
    __tablename__ = "tag_registry"
    __table_args__ = (UniqueConstraint("tag_id", name="uq_tag_registry_tag"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(40), default="general", index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class ProviderTagMapping(Base):
    __tablename__ = "provider_tag_mappings"
    __table_args__ = (UniqueConstraint("provider_id", "external_tag", name="uq_provider_external_tag_mapping"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("metadata_providers.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)
    external_tag: Mapped[str] = mapped_column(String(200), index=True)
    normalized_tag: Mapped[str] = mapped_column(String(200), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    mapping_method: Mapped[str] = mapped_column(String(30), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="approved", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

CATEGORIES = {"general", "genre", "theme", "gameplay", "feature", "perspective", "audience", "technical", "mode"}
METHODS = {"manual", "llm", "alias", "import", "fuzzy"}

def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")[:80]

def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().casefold()).strip()

def backfill(db):
    changed = False
    for tag in db.scalars(select(Tag)).all():
        if db.scalar(select(TagRegistryEntry).where(TagRegistryEntry.tag_id == tag.id)):
            continue
        base = slugify(tag.name) or f"tag-{tag.id}"; candidate = base; n = 2
        while db.scalar(select(TagRegistryEntry).where(TagRegistryEntry.slug == candidate)):
            candidate = f"{base}-{n}"; n += 1
        db.add(TagRegistryEntry(tag_id=tag.id, slug=candidate, display_name=tag.name)); changed = True
    if changed: db.commit()

def entry(db, slug):
    result = db.scalar(select(TagRegistryEntry).where(TagRegistryEntry.slug == slug))
    if not result: raise HTTPException(404, "Canonical tag not found")
    return result

def serialize(db, item):
    count = db.scalar(select(func.count(GameTag.game_id)).where(GameTag.tag_id == item.tag_id)) or 0
    return {"id": item.slug, "tag_id": item.tag_id, "name": item.display_name, "description": item.description,
            "category": item.category, "status": item.status, "game_count": count,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None}

@router.get("/api/v1/tag-registry")
def registry(db: Db, q: str = "", category: str = "", provider: str = "", status: str = "active", limit: int = 100, offset: int = 0,
             authorization: Annotated[str | None, Header()] = None):
    require_api_token(db, authorization, "read"); backfill(db)
    if status not in {"all", "active", "deprecated"}: raise HTTPException(422, "Invalid status")
    statement = select(TagRegistryEntry)
    if status != "all": statement = statement.where(TagRegistryEntry.status == status)
    if q.strip():
        term = q.strip()[:120]; statement = statement.where(TagRegistryEntry.display_name.ilike(f"%{term}%") | TagRegistryEntry.slug.ilike(f"%{term}%"))
    if category.strip(): statement = statement.where(TagRegistryEntry.category == category.strip().lower())
    if provider.strip():
        statement = statement.join(ProviderTagMapping, ProviderTagMapping.tag_id == TagRegistryEntry.tag_id).join(MetadataProvider).where(MetadataProvider.slug == provider.strip().lower(), ProviderTagMapping.status == "approved")
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    items = db.scalars(statement.order_by(TagRegistryEntry.display_name).offset(offset).limit(limit)).all()
    return {"items": [serialize(db, item) for item in items], "total": total, "limit": limit, "offset": offset, "categories": sorted(CATEGORIES)}

@router.get("/api/v1/tag-registry/resolve")
def resolve(provider: str, tag: str, db: Db, authorization: Annotated[str | None, Header()] = None):
    require_api_token(db, authorization, "read"); backfill(db)
    mapping = db.scalar(select(ProviderTagMapping).join(MetadataProvider).where(MetadataProvider.slug == provider.strip().lower(), ProviderTagMapping.normalized_tag == normalize(tag), ProviderTagMapping.status == "approved"))
    if not mapping: return {"provider": provider.strip().lower(), "provider_tag": tag, "mapped": False}
    item = db.scalar(select(TagRegistryEntry).where(TagRegistryEntry.tag_id == mapping.tag_id))
    return {"provider": provider.strip().lower(), "provider_tag": tag, "mapped": bool(item), "canonical_tag": item.slug if item else None, "canonical_name": item.display_name if item else None, "confidence": mapping.confidence if item else None, "mapping_method": mapping.mapping_method if item else None}

@router.get("/api/v1/tag-registry/{slug}")
def get_registry_tag(slug: str, db: Db, authorization: Annotated[str | None, Header()] = None):
    require_api_token(db, authorization, "read"); backfill(db); return serialize(db, entry(db, slug))

@router.get("/api/v1/tag-registry/mappings")
def mappings(db: Db, provider: str = "", status: str = "approved", limit: int = 100, offset: int = 0, authorization: Annotated[str | None, Header()] = None):
    require_api_token(db, authorization, "read"); backfill(db)
    statement = select(ProviderTagMapping, MetadataProvider, TagRegistryEntry).join(MetadataProvider).join(TagRegistryEntry, TagRegistryEntry.tag_id == ProviderTagMapping.tag_id)
    if provider.strip(): statement = statement.where(MetadataProvider.slug == provider.strip().lower())
    if status != "all": statement = statement.where(ProviderTagMapping.status == status)
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    rows = db.execute(statement.order_by(MetadataProvider.slug, ProviderTagMapping.external_tag).offset(offset).limit(limit)).all()
    return {"items": [{"provider": p.slug, "provider_name": p.name, "external_tag": m.external_tag, "canonical_tag": t.slug, "canonical_name": t.display_name, "confidence": m.confidence, "mapping_method": m.mapping_method, "status": m.status} for m,p,t in rows], "limit": limit, "offset": offset}

@router.get("/admin/tags", response_class=HTMLResponse)
def admin_tags(request: Request, db: Db):
    user = require_admin(request, db); backfill(db)
    entries = db.scalars(select(TagRegistryEntry).order_by(TagRegistryEntry.display_name)).all(); providers = db.scalars(select(MetadataProvider).order_by(MetadataProvider.slug)).all()
    maps = db.execute(select(ProviderTagMapping, MetadataProvider, TagRegistryEntry).join(MetadataProvider).join(TagRegistryEntry, TagRegistryEntry.tag_id == ProviderTagMapping.tag_id).order_by(MetadataProvider.slug, ProviderTagMapping.external_tag).limit(300)).all()
    return templates.TemplateResponse(request, "tag_registry.html", {"request": request, "app_name": "Atlas Metadata", "user": user, "entries": entries, "providers": providers, "mappings": maps, "categories": sorted(CATEGORIES), "notice": request.query_params.get("notice"), "error": request.query_params.get("error")})

@router.post("/admin/tags/create")
def create_tag(request: Request, db: Db, name: Annotated[str, Form()], slug: Annotated[str, Form()] = "", category: Annotated[str, Form()] = "general", description: Annotated[str, Form()] = ""):
    require_admin(request, db); backfill(db); name = name.strip(); slug = slugify(slug or name); category = category.strip().lower()
    if not name or not slug or category not in CATEGORIES: raise HTTPException(422, "Invalid canonical tag")
    if db.scalar(select(TagRegistryEntry).where(TagRegistryEntry.slug == slug)): raise HTTPException(409, "Canonical tag already exists")
    tag = Tag(name=name.casefold()); db.add(tag); db.flush(); db.add(TagRegistryEntry(tag_id=tag.id, slug=slug, display_name=name, description=description.strip()[:2000], category=category)); db.commit()
    return RedirectResponse("/admin/tags?notice=" + quote_plus("Canonical tag created"), 303)

@router.post("/admin/tags/mappings")
def save_mapping(request: Request, db: Db, provider: Annotated[str, Form()], external_tag: Annotated[str, Form()], canonical_slug: Annotated[str, Form()], confidence: Annotated[str, Form()] = "1", mapping_method: Annotated[str, Form()] = "manual"):
    require_admin(request, db); backfill(db); provider = provider.strip().lower(); external_tag = external_tag.strip()
    if mapping_method not in METHODS or not external_tag or len(external_tag) > 200: raise HTTPException(422, "Invalid mapping")
    try: score = float(confidence)
    except ValueError as exc: raise HTTPException(422, "Confidence must be between 0 and 1") from exc
    if not 0 <= score <= 1: raise HTTPException(422, "Confidence must be between 0 and 1")
    p = db.scalar(select(MetadataProvider).where(MetadataProvider.slug == provider))
    if not p: raise HTTPException(404, "Provider not found")
    target = entry(db, canonical_slug)
    row = db.scalar(select(ProviderTagMapping).where(ProviderTagMapping.provider_id == p.id, ProviderTagMapping.external_tag == external_tag))
    if row: row.tag_id, row.normalized_tag, row.confidence, row.mapping_method, row.status, row.updated_at = target.tag_id, normalize(external_tag), score, mapping_method, "approved", datetime.now(timezone.utc)
    else: db.add(ProviderTagMapping(provider_id=p.id, tag_id=target.tag_id, external_tag=external_tag, normalized_tag=normalize(external_tag), confidence=score, mapping_method=mapping_method))
    db.commit(); return RedirectResponse("/admin/tags?notice=" + quote_plus("Provider tag mapping saved"), 303)
