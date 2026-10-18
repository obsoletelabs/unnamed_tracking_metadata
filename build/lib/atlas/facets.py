"""Artwork and achievements: per-source sets attached to a canonical record, merged by trust."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import feed
from .models import Achievement, Artwork, Canonical
from .normalize import norm_namespace
from .service import AtlasError, Ctx, write_lock

KINDS = ("key_art", "banner", "logo", "icon", "screenshot", "hero", "poster")
MAX_ART = 200
MAX_ACH = 2000
_BAD_URL = re.compile(r"[\s\\\x00-\x1f]")


def _norm_name(name: str) -> str:
    return re.sub(r"\W+", " ", name.casefold()).strip()


def _url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 1000 or _BAD_URL.search(value) \
            or not value.startswith(("http://", "https://")):
        raise AtlasError("invalid url")
    return value


def clean_artwork(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) > MAX_ART:
        raise AtlasError(f"at most {MAX_ART} artwork items")
    out, seen = [], set()
    for it in items:
        kind = it.get("kind")
        if kind not in KINDS:
            raise AtlasError(f"kind must be one of {KINDS}")
        url = _url(it.get("url"))
        if (kind, url) in seen:
            continue
        seen.add((kind, url))
        row = {"kind": kind, "url": url, "priority": int(it.get("priority", 100))}
        for dim in ("width", "height"):
            if it.get(dim) is not None:
                if not isinstance(it[dim], int) or not 1 <= it[dim] <= 50000:
                    raise AtlasError(f"{dim} out of range")
                row[dim] = it[dim]
        if not 0 <= row["priority"] <= 10000:
            raise AtlasError("priority out of range")
        out.append(row)
    return out


def clean_achievements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) > MAX_ACH:
        raise AtlasError(f"at most {MAX_ACH} achievements")
    out, seen = [], set()
    for it in items:
        name = (it.get("name") or "").strip()
        if not name or len(name) > 200:
            raise AtlasError("achievement name required (max 200)")
        key = _norm_name(name)
        if key in seen:
            continue
        seen.add(key)
        pct = it.get("global_percent")
        if pct is not None and not (isinstance(pct, (int, float)) and 0 <= pct <= 100):
            raise AtlasError("global_percent must be 0-100")
        row: dict[str, Any] = {
            "name": name, "description": str(it.get("description") or "")[:2000],
            "external_id": str(it.get("external_id") or "")[:200], "hidden": bool(it.get("hidden", False)),
        }
        if it.get("icon_url"):
            row["icon_url"] = _url(it["icon_url"])
        if pct is not None:
            row["global_percent"] = float(pct)
        out.append(row)
    return out


def _live(session: Session, cid: str) -> Canonical:
    row = session.get(Canonical, cid)
    if row is None or row.merged_into:
        raise AtlasError("unknown or merged canonical", "not_found")
    return row


def set_artwork(session: Session, ctx: Ctx, cid: str, source: str, items: list[dict[str, Any]], *,
                redistributable: bool = False, log: bool = True) -> int:
    """Replace the artwork a source contributes to a record. Cached bytes survive for unchanged URLs."""
    write_lock(session)
    _live(session, cid)
    source = norm_namespace(source)
    items = clean_artwork(items)
    existing = {(a.kind, a.url): a for a in session.scalars(
        select(Artwork).where(Artwork.canonical_id == cid, Artwork.source == source))}
    wanted = {(i["kind"], i["url"]) for i in items}
    for key, art in existing.items():
        if key not in wanted:
            session.delete(art)
    for i in items:
        art = existing.get((i["kind"], i["url"]))
        if art:
            art.priority, art.width, art.height = i["priority"], i.get("width", art.width), i.get("height", art.height)
            art.redistributable = redistributable
        else:
            clash = session.scalars(select(Artwork).where(
                Artwork.canonical_id == cid, Artwork.kind == i["kind"], Artwork.url == i["url"])).first()
            if clash:  # another source already lists this exact image; it keeps it
                continue
            session.add(Artwork(canonical_id=cid, source=source, kind=i["kind"], url=i["url"],
                                priority=i["priority"], width=i.get("width"), height=i.get("height"),
                                redistributable=redistributable))
    session.flush()
    if log and redistributable and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "artwork", {"canonical_id": cid, "source": source, "items": items})
    return len(items)


def set_achievements(session: Session, ctx: Ctx, cid: str, source: str, items: list[dict[str, Any]], *,
                     redistributable: bool = False, log: bool = True) -> int:
    write_lock(session)
    _live(session, cid)
    source = norm_namespace(source)
    items = clean_achievements(items)
    session.execute(delete(Achievement).where(Achievement.canonical_id == cid, Achievement.source == source))
    for i in items:
        session.add(Achievement(
            canonical_id=cid, source=source, external_id=i["external_id"], name=i["name"],
            norm_name=_norm_name(i["name"]), description=i["description"], icon_url=i.get("icon_url"),
            hidden=i["hidden"], global_percent=i.get("global_percent"), redistributable=redistributable))
    session.flush()
    if log and redistributable and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "achievements", {"canonical_id": cid, "source": source, "items": items})
    return len(items)


def merged_artwork(session: Session, ctx: Ctx, cid: str) -> list[Artwork]:
    rows = list(session.scalars(select(Artwork).where(Artwork.canonical_id == cid)))
    rows.sort(key=lambda a: (-ctx.settings.trust(a.source), a.priority, a.created_at))
    return rows


def merged_achievements(session: Session, ctx: Ctx, cid: str) -> list[dict[str, Any]]:
    rows = list(session.scalars(select(Achievement).where(Achievement.canonical_id == cid)))
    rows.sort(key=lambda a: -ctx.settings.trust(a.source))
    merged: dict[str, dict[str, Any]] = {}
    for a in rows:  # highest-trust source first; later sources only fill gaps
        m = merged.setdefault(a.norm_name, {"name": a.name, "description": "", "icon_url": None, "hidden": a.hidden,
                                           "global_percent": None, "external_ids": {}, "sources": []})
        m["description"] = m["description"] or a.description
        m["icon_url"] = m["icon_url"] or a.icon_url
        if m["global_percent"] is None:
            m["global_percent"] = a.global_percent
        if a.external_id:
            m["external_ids"].setdefault(a.source, a.external_id)
        m["sources"].append(a.source)
    return sorted(merged.values(), key=lambda m: (m["global_percent"] is None, -(m["global_percent"] or 0), m["name"]))


def move_facets(session: Session, src: str, dst: str) -> None:
    """On merge: carry artwork/achievements over; duplicates keep the survivor's row."""
    have_art = {(a.kind, a.url) for a in session.scalars(select(Artwork).where(Artwork.canonical_id == dst))}
    for a in session.scalars(select(Artwork).where(Artwork.canonical_id == src)).all():
        if (a.kind, a.url) in have_art:
            session.delete(a)
        else:
            a.canonical_id = dst
    have_ach = {(a.source, a.norm_name) for a in session.scalars(
        select(Achievement).where(Achievement.canonical_id == dst))}
    for a in session.scalars(select(Achievement).where(Achievement.canonical_id == src)).all():
        if (a.source, a.norm_name) in have_ach:
            session.delete(a)
        else:
            a.canonical_id = dst
    session.flush()
