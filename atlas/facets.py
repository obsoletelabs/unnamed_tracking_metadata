"""Artwork and achievements: per-source sets attached to a canonical record, merged by trust."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import feed, media
from .models import Achievement, Artwork, Canonical, now
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


ACH_KINDS = ("progression", "missable", "win_condition")
ACH_TIERS = ("bronze", "silver", "gold")
HIDDEN_NAME = "Hidden achievement"
MAX_POINTS = 100000


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
        if pct is not None and not (isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0 <= pct <= 100):
            raise AtlasError("global_percent must be 0-100")
        row: dict[str, Any] = {
            "name": name, "description": str(it.get("description") or "")[:2000],
            "external_id": str(it.get("external_id") or "")[:200], "hidden": bool(it.get("hidden", False)),
        }
        for field in ("icon_url", "locked_icon_url"):
            if it.get(field):
                row[field] = _url(it[field])
        if it.get("kind") not in (None, ""):
            if it["kind"] not in ACH_KINDS:
                raise AtlasError(f"kind must be one of {ACH_KINDS}")
            row["kind"] = it["kind"]
        if it.get("tier") not in (None, ""):
            if it["tier"] not in ACH_TIERS:
                raise AtlasError(f"tier must be one of {ACH_TIERS}")
            row["tier"] = it["tier"]
        pts = it.get("points")
        if pts is not None:
            if not isinstance(pts, int) or isinstance(pts, bool) or not 0 <= pts <= MAX_POINTS:
                raise AtlasError(f"points must be a whole number from 0 to {MAX_POINTS}")
            row["points"] = pts
        if pct is not None:
            row["global_percent"] = float(pct)
        out.append(row)
    return out


def achievement_items(rows: list[Achievement]) -> list[dict[str, Any]]:
    """Rows back to the item shape clean_achievements/set_achievements accept (used to snapshot for revert)."""
    out = []
    for a in rows:
        item: dict[str, Any] = {"name": a.name, "description": a.description, "external_id": a.external_id,
                                "hidden": a.hidden}
        for field in ("icon_url", "locked_icon_url", "kind", "tier", "points", "global_percent"):
            if getattr(a, field) is not None:
                item[field] = getattr(a, field)
        out.append(item)
    return out


def _live(session: Session, cid: str) -> Canonical:
    row = session.get(Canonical, cid)
    if row is None or row.merged_into:
        raise AtlasError("unknown or merged canonical", "not_found")
    return row


def adopt_local_blob(ctx: Ctx, art: Artwork) -> None:
    """An image uploaded to this node is addressed by its own public /media/<sha256> URL: it is already cached.

    Followers fetch the same URL from this node like any other image, so uploads federate without a special case."""
    base = ctx.settings.public_url.rstrip("/") + "/media/"
    if not art.url.startswith(base) or art.blob_hash:
        return
    found = media.find_blob(ctx.settings, art.url[len(base):])
    if found is not None:
        path, ctype = found
        digest = art.url[len(base):]
        art.blob_hash, art.content_type, art.size_bytes, art.cached_at = digest, ctype, path.stat().st_size, now()


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
            art = Artwork(canonical_id=cid, source=source, kind=i["kind"], url=i["url"],
                          priority=i["priority"], width=i.get("width"), height=i.get("height"),
                          redistributable=redistributable)
            adopt_local_blob(ctx, art)
            session.add(art)
    session.flush()
    if log and redistributable and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "artwork", {"canonical_id": cid, "source": source, "items": items})
    return len(items)


def adopt_local_icons(ctx: Ctx, row: Achievement) -> None:
    """An icon uploaded to this node is addressed by its own /media/<sha256> URL and is already stored."""
    base = ctx.settings.public_url.rstrip("/") + "/media/"
    for which in ("icon", "locked_icon"):
        url = getattr(row, f"{which}_url")
        if url and url.startswith(base) and not getattr(row, f"{which}_blob_hash"):
            digest = url[len(base):]
            if media.find_blob(ctx.settings, digest) is not None:
                setattr(row, f"{which}_blob_hash", digest)


def set_achievements(session: Session, ctx: Ctx, cid: str, source: str, items: list[dict[str, Any]], *,
                     redistributable: bool = False, log: bool = True, ordered: bool = False) -> int:
    """Replace what one source says. `ordered` keeps the given order (for lists people arranged by hand)."""
    write_lock(session)
    _live(session, cid)
    source = norm_namespace(source)
    items = clean_achievements(items)
    old = {a.norm_name: a for a in session.scalars(select(Achievement).where(
        Achievement.canonical_id == cid, Achievement.source == source))}
    session.execute(delete(Achievement).where(Achievement.canonical_id == cid, Achievement.source == source))
    session.flush()
    for index, i in enumerate(items):
        prev = old.get(_norm_name(i["name"]))
        row = Achievement(position=index if ordered else None,

            canonical_id=cid, source=source, external_id=i["external_id"], name=i["name"],
            norm_name=_norm_name(i["name"]), description=i["description"], icon_url=i.get("icon_url"),
            locked_icon_url=i.get("locked_icon_url"), hidden=i["hidden"], kind=i.get("kind"), tier=i.get("tier"),
            points=i.get("points"), global_percent=i.get("global_percent"), redistributable=redistributable)
        if prev is not None:  # an unchanged image is not downloaded again
            if prev.icon_url == row.icon_url:
                row.icon_blob_hash, row.icon_fail_count = prev.icon_blob_hash, prev.icon_fail_count
                row.icon_next_try_at = prev.icon_next_try_at
            if prev.locked_icon_url == row.locked_icon_url:
                row.locked_icon_blob_hash = prev.locked_icon_blob_hash
                row.locked_icon_fail_count = prev.locked_icon_fail_count
                row.locked_icon_next_try_at = prev.locked_icon_next_try_at
        adopt_local_icons(ctx, row)
        session.add(row)
    session.flush()
    if log and redistributable and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "achievements", {"canonical_id": cid, "source": source, "items": items})
    return len(items)


def set_preferred_artwork(session: Session, art: Artwork, restore: list[str] | None = None) -> None:
    """Make `art` the default of its kind on its game (or, on revert, put the previous defaults back)."""
    write_lock(session)
    for other in session.scalars(select(Artwork).where(Artwork.canonical_id == art.canonical_id,
                                                       Artwork.kind == art.kind, Artwork.preferred.is_(True))):
        other.preferred = False
    if restore is None:
        art.preferred = True
    else:
        for other in session.scalars(select(Artwork).where(Artwork.id.in_(restore))):
            other.preferred = True
    session.flush()


def merged_artwork(session: Session, ctx: Ctx, cid: str) -> list[Artwork]:
    rows = list(session.scalars(select(Artwork).where(Artwork.canonical_id == cid)))
    rows.sort(key=lambda a: (not a.preferred, -ctx.settings.trust(a.source), a.priority, a.created_at))
    return rows


def _media(blob: str | None) -> str | None:
    return f"/api/v2/media/{blob}" if blob else None


def merged_achievements(session: Session, ctx: Ctx, cid: str, *,
                        reveal: bool | set[str] = False) -> list[dict[str, Any]]:
    """One entry per achievement name; highest-trust source first, later sources only fill gaps.

    Hidden entries are redacted (no real name, description, icon or provider ids) unless `reveal` is True or
    holds their id. A hidden flag from any source wins: one source knowing it is secret keeps it secret.
    Icons are only ever our own verified copies (`/api/v2/media/<sha256>`); the origin URL is never handed out.
    """
    rows = list(session.scalars(select(Achievement).where(Achievement.canonical_id == cid)))
    rows.sort(key=lambda a: (-ctx.settings.trust(a.source), a.id))
    merged: dict[str, dict[str, Any]] = {}
    for a in rows:
        m = merged.setdefault(a.norm_name, {
            "id": a.id, "name": a.name, "description": "", "hidden": False, "kind": None, "tier": None,
            "points": None, "global_percent": None, "icon_blob": None, "locked_icon_blob": None,
            "position": None, "external_ids": {}, "sources": []})
        if a.position is not None and (m["position"] is None or a.position < m["position"]):
            m["position"] = a.position
        m["description"] = m["description"] or a.description
        m["hidden"] = m["hidden"] or a.hidden
        for field in ("kind", "tier", "points", "global_percent"):
            if m[field] is None:
                m[field] = getattr(a, field)
        m["icon_blob"] = m["icon_blob"] or a.icon_blob_hash
        m["locked_icon_blob"] = m["locked_icon_blob"] or a.locked_icon_blob_hash
        if a.external_id:
            m["external_ids"].setdefault(a.source, a.external_id)
        m["sources"].append(a.source)
    out = []
    for m in merged.values():
        shown = not m["hidden"] or reveal is True or (isinstance(reveal, set) and m["id"] in reveal)
        entry = {"id": m["id"], "hidden": m["hidden"], "revealed": bool(m["hidden"] and shown),
                 "kind": m["kind"], "tier": m["tier"], "points": m["points"], "global_percent": m["global_percent"],
                 "sources": m["sources"] if shown else []}
        if shown:
            entry.update(name=m["name"], description=m["description"], external_ids=m["external_ids"],
                         icon_url=_media(m["icon_blob"]), locked_icon_url=_media(m["locked_icon_blob"]))
        else:
            entry.update(name=HIDDEN_NAME, description="", external_ids={}, icon_url=None, locked_icon_url=None)
        out.append(entry)
    pos = {m["id"]: m["position"] for m in merged.values()}
    return sorted(out, key=lambda m: (m["hidden"] and not m["revealed"], pos[m["id"]] is None,
                                      pos[m["id"]] or 0, m["global_percent"] is None,
                                      -(m["global_percent"] or 0), m["name"]))


def move_facets(session: Session, src: str, dst: str) -> None:
    """On merge: carry artwork/achievements over; duplicates keep the survivor's row."""
    have_art = {(a.kind, a.url) for a in session.scalars(select(Artwork).where(Artwork.canonical_id == dst))}
    for a in session.scalars(select(Artwork).where(Artwork.canonical_id == src)).all():
        if (a.kind, a.url) in have_art:
            session.delete(a)
        else:
            a.canonical_id, a.preferred = dst, False  # the survivor keeps its own defaults
    have_ach = {(a.source, a.norm_name) for a in session.scalars(
        select(Achievement).where(Achievement.canonical_id == dst))}
    for a in session.scalars(select(Achievement).where(Achievement.canonical_id == src)).all():
        if (a.source, a.norm_name) in have_ach:
            session.delete(a)
        else:
            a.canonical_id = dst
    session.flush()
