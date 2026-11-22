"""Artwork and achievements: per-source sets attached to a canonical record, merged by trust."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import achievement_key, feed, feed_schema, media, vocab
from .models import Achievement, Artwork, Canonical, now
from .normalize import norm_namespace
from .service import AtlasError, Ctx, touch_version, write_lock

KINDS = ("key_art", "banner", "logo", "icon", "screenshot", "hero", "poster")
MAX_ART = 200
MAX_ACH = 2000
_BAD_URL = re.compile(r"[\s\\\x00-\x1f]")


log_ = logging.getLogger(__name__)
_norm_name = achievement_key.norm_name
_ROW_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")  # an Atlas row id (ULID, Crockford base32)


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


ACH_KINDS = vocab.ACHIEVEMENT_KINDS
ACH_TIERS = vocab.ACHIEVEMENT_TIERS  # bronze | silver | gold | platinum (C7.1)
#: The tier that needs federation protocol 1.2 before an authority may publish it (C1.6, H13).
PLATINUM = "platinum"
PLATINUM_PROTOCOL = vocab.VALUE_PROTOCOL[("achievement_tier", PLATINUM)]
HIDDEN_NAME = "Hidden achievement"
MAX_POINTS = 100000


def clean_achievements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) > MAX_ACH:
        raise AtlasError(f"at most {MAX_ACH} achievements")
    out, seen, keys = [], set(), set()
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
        # identity hints for the in-place reconcile (C2.2): a stored key (from the feed or a round trip) or
        # the row id the editor submits. A key repeated on a later item is dropped there; that item gets its own.
        k = it.get("key")
        if k not in (None, ""):
            if not achievement_key.is_key(k):
                raise AtlasError("achievement key must be ak1: followed by 26 base32 characters")
            if k not in keys:
                keys.add(k)
                row["key"] = k
        rid = it.get("id")
        if isinstance(rid, str) and _ROW_ID.match(rid):
            row["id"] = rid
        out.append(row)
    return out


def platinum_gated(ctx: Ctx) -> bool:
    """True while this node may not publish a platinum entry: it is an authority (its writes enter the
    signed feed) and its `emit_protocol` is below 1.2 (C1.6, H13). Mirrors and local nodes sign nothing, so
    platinum is never gated there; a mirror applies it from its upstream whatever its own settings say."""
    if ctx.settings.role != "authority":
        return False
    current = feed_schema.emit_protocol(ctx.settings)
    return feed_schema.parse_protocol(current) < feed_schema.parse_protocol(PLATINUM_PROTOCOL)


def platinum_gate_message(ctx: Ctx) -> str:
    return str(feed_schema.EmitGateError(feed_schema.VALUE_FEATURE[("achievement_tier", PLATINUM)],
                                         PLATINUM_PROTOCOL, feed_schema.emit_protocol(ctx.settings)))


def check_platinum_gate(ctx: Ctx, items: list[dict[str, Any]]) -> None:
    """Refuse a list with a platinum entry before anything is written, while `platinum_gated` (C1.6).

    Raises AtlasError with code `emit_protocol` (HTTP 409): the request is valid, this node's federation
    settings are what stand in the way. More than one platinum entry is allowed (C7.1: the editor warns)."""
    if any(isinstance(it, dict) and it.get("tier") == PLATINUM for it in items) and platinum_gated(ctx):
        raise AtlasError(platinum_gate_message(ctx), "emit_protocol")


def achievement_items(rows: list[Achievement]) -> list[dict[str, Any]]:
    """Rows back to the item shape clean_achievements/set_achievements accept (used to snapshot for revert)."""
    out = []
    for a in rows:
        item: dict[str, Any] = {"name": a.name, "description": a.description, "external_id": a.external_id,
                                "hidden": a.hidden, "key": a.key}
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
    # Clients see every change (C3.1); only redistributable data on an authority enters the signed feed.
    signed = log and redistributable and ctx.settings.role == "authority"
    touch_version(session, cid, cause="feed" if signed else "local")
    if signed:
        feed.emit(session, ctx, "artwork", {"canonical_id": cid, "source": source, "items": items})
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


_ICON_STATE = {"icon": ("icon_blob_hash", "icon_fail_count", "icon_next_try_at"),
               "locked_icon": ("locked_icon_blob_hash", "locked_icon_fail_count", "locked_icon_next_try_at")}


def _match_rows(rows: list[Achievement], items: list[dict[str, Any]]) -> list[Achievement | None]:
    """Pair each incoming item with at most one existing row of the same source (C2.2 step 2).

    All items are tried against their explicit key first, then their external id together with their name,
    then their external id alone, then their normalised name, so an item is never beaten to a row by another
    item's weaker match. The external-id-and-name pass keeps a list that repeats one external id under two
    names stable: each name stays on its own row (and key) however the list is ordered."""
    by_key = {r.key: r for r in rows}
    by_ext: dict[str, list[Achievement]] = {}
    for r in rows:
        if r.external_id:
            by_ext.setdefault(r.external_id, []).append(r)
    by_name = {r.norm_name: r for r in rows}
    by_ext_name = {(r.external_id, r.norm_name): r for r in rows if r.external_id}
    match: list[Achievement | None] = [None] * len(items)
    claimed: set[str] = set()

    def claim(i: int, row: Achievement | None) -> None:
        if row is not None and row.id not in claimed and match[i] is None:
            match[i] = row
            claimed.add(row.id)

    for i, it in enumerate(items):
        claim(i, by_key.get(it.get("key", "")))
    for i, it in enumerate(items):
        if match[i] is None and it["external_id"]:
            claim(i, by_ext_name.get((it["external_id"], _norm_name(it["name"]))))
    for i, it in enumerate(items):
        if match[i] is None and it["external_id"]:
            claim(i, next((r for r in by_ext.get(it["external_id"], []) if r.id not in claimed), None))
    for i, it in enumerate(items):
        if match[i] is None:
            claim(i, by_name.get(_norm_name(it["name"])))
    return match


def _new_key(source: str, item: dict[str, Any], taken: set[str]) -> str:
    """The key of a new row: the item's own key when it is free, else the derived one (C2.2 step 4)."""
    if item.get("key") and item["key"] not in taken:
        return item["key"]
    norm = _norm_name(item["name"])
    # the name form is the fallback for a list that repeats one external id under two names
    candidates = [achievement_key.identity(source, item["external_id"], norm),
                  achievement_key.name_identity(source, norm)]
    for ident in candidates:
        key = achievement_key.ak1(ident)
        if key not in taken:
            return key
    n = 2
    while (key := achievement_key.ak1(f"{candidates[-1]}#{n}")) in taken:
        n += 1
    return key


def set_achievements(session: Session, ctx: Ctx, cid: str, source: str, items: list[dict[str, Any]], *,
                     redistributable: bool = False, log: bool = True, ordered: bool = False,
                     adopt_keys: bool = False) -> int:
    """Replace what one source says, reconciling its rows in place (CONTRACTS-v1 C2.2).

    Each item is matched to an existing row of this source by its key (or the row id the editor submits),
    then its external id and name, then its external id, then its normalised name. A matched row keeps its id
    and key and is updated; its cached icons survive when the address is unchanged. Unmatched items become new
    rows and unmatched rows are deleted. `ordered` keeps the given order (for lists people arranged by hand).
    `adopt_keys` is for signed feed entries: the authority's key replaces a mirror's own, so every node
    publishes the same key."""
    write_lock(session)
    _live(session, cid)
    source = norm_namespace(source)
    items = clean_achievements(items)
    if log and redistributable:  # the list would be signed: refuse platinum before any row changes (C1.6)
        check_platinum_gate(ctx, items)
    rows = list(session.scalars(select(Achievement).where(
        Achievement.canonical_id == cid, Achievement.source == source).order_by(Achievement.id)))
    by_id = {r.id: r for r in rows}
    for it in items:  # a row id stands for that row's key, unless another item already carries the key
        row = by_id.get(it.pop("id", ""))
        if row is not None and "key" not in it and all(o.get("key") != row.key for o in items):
            it["key"] = row.key
    match = _match_rows(rows, items)
    kept = {r.id for r in match if r is not None}

    # 1. delete first and flush, so the names and keys they free can be reused below (C2.2 step 5)
    for r in rows:
        if r.id not in kept:
            session.delete(r)
    session.flush()

    # 2. final keys. A matched row keeps its key. Under `adopt_keys` it takes the item's key instead when no
    # row of another source on the record holds that key; rows of this source are all being reconciled here,
    # so two of them may swap keys (step 3 makes that safe). A row whose key another row adopts is re-keyed.
    other = set(session.scalars(select(Achievement.key).where(Achievement.canonical_id == cid,
                                                              Achievement.source != source)))
    final: list[str] = [""] * len(items)
    if adopt_keys:
        for i, (it, row) in enumerate(zip(items, match, strict=True)):
            want = it.get("key")
            if row is not None and want and want not in other and want not in final:
                final[i] = want
    taken = set(final)
    for i, row in enumerate(match):
        if row is not None and not final[i] and row.key not in taken:
            final[i] = row.key
            taken.add(row.key)
    reserved = other | taken
    for i, it in enumerate(items):
        if not final[i]:
            cand = it if match[i] is None else {**it, "key": None}
            final[i] = _new_key(source, cand, reserved)
            reserved.add(final[i])
    if adopt_keys and (refused := sum(1 for it, key in zip(items, final, strict=True)
                                      if it.get("key") and it["key"] != key)):
        # mirror and authority now name these entries differently (C2.8); counted so it is not silent
        log_.warning("achievements %s/%s: %d authority key(s) not adopted (held by another source here)",
                     cid, source, refused)

    # 3. renames and re-keys go through a temporary value first, so a swap never trips a unique constraint
    moving = [r for i, r in enumerate(match) if r is not None
              and (r.norm_name != _norm_name(items[i]["name"]) or r.key != final[i])]
    for r in moving:
        r.norm_name, r.key = f"~{r.id}", f"~{r.id}"
    if moving:
        session.flush()

    # 4. update matched rows in place, add the new ones
    for index, (it, row, key) in enumerate(zip(items, match, final, strict=True)):
        if row is None:
            row = Achievement(canonical_id=cid, source=source)
            session.add(row)
        else:
            for which, state in _ICON_STATE.items():  # an unchanged image is not downloaded again
                if getattr(row, f"{which}_url") != it.get(f"{which}_url"):
                    for attr, empty in zip(state, (None, 0, None), strict=True):
                        setattr(row, attr, empty)
        row.key, row.position = key, (index if ordered else None)
        row.external_id, row.name, row.norm_name = it["external_id"], it["name"], _norm_name(it["name"])
        row.description, row.hidden = it["description"], it["hidden"]
        row.icon_url, row.locked_icon_url = it.get("icon_url"), it.get("locked_icon_url")
        row.kind, row.tier, row.points = it.get("kind"), it.get("tier"), it.get("points")
        row.global_percent, row.redistributable = it.get("global_percent"), redistributable
        adopt_local_icons(ctx, row)
        it["key"] = key  # the feed carries the stored key (reserved-optional since 1.1, C1.5)
    session.flush()
    # Clients see every change (C3.1); only redistributable data on an authority enters the signed feed.
    signed = log and redistributable and ctx.settings.role == "authority"
    touch_version(session, cid, cause="feed" if signed else "local")
    if signed:
        feed.emit(session, ctx, "achievements", {"canonical_id": cid, "source": source, "items": items})
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

    Hidden entries are redacted (no real name, description, icon, provider ids or `key_identity`) unless
    `reveal` is True or holds their id or one of their keys. A hidden flag from any source wins: one source
    knowing it is secret keeps it secret. Icons are only ever our own verified copies (`/api/v2/media/<sha256>`);
    the origin URL is never handed out.

    Identity (C2.3): `keys` are the stored keys of every row merged into the entry, sorted, and `key` is the
    smallest of them. It does not depend on `source_trust`, so every node names the entry alike. `id` is the
    top-trust row's id: stable on this node, kept for older clients, deprecated as an identity.
    """
    rows = list(session.scalars(select(Achievement).where(Achievement.canonical_id == cid)))
    rows.sort(key=lambda a: (-ctx.settings.trust(a.source), a.id))
    merged: dict[str, dict[str, Any]] = {}
    for a in rows:
        m = merged.setdefault(a.norm_name, {
            "id": a.id, "name": a.name, "description": "", "hidden": False, "kind": None, "tier": None,
            "points": None, "global_percent": None, "icon_blob": None, "locked_icon_blob": None,
            "position": None, "external_ids": {}, "sources": [], "identities": {}})
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
        m["identities"][a.key] = achievement_key.identity(a.source, a.external_id, a.norm_name)
    out = []
    asked = reveal if isinstance(reveal, set) else set()
    for m in merged.values():
        keys = sorted(m["identities"])
        shown = not m["hidden"] or reveal is True or m["id"] in asked or not asked.isdisjoint(keys)
        entry = {"id": m["id"], "key": keys[0], "keys": keys, "hidden": m["hidden"],
                 "revealed": bool(m["hidden"] and shown), "kind": m["kind"], "tier": m["tier"],
                 "points": m["points"], "global_percent": m["global_percent"],
                 "sources": m["sources"] if shown else []}
        if shown:
            entry.update(name=m["name"], description=m["description"], external_ids=m["external_ids"],
                         key_identity=m["identities"][keys[0]],
                         icon_url=_media(m["icon_blob"]), locked_icon_url=_media(m["locked_icon_blob"]))
        else:
            entry.update(name=HIDDEN_NAME, description="", external_ids={}, key_identity=None, icon_url=None,
                         locked_icon_url=None)
        out.append(entry)
    pos = {m["id"]: m["position"] for m in merged.values()}
    return sorted(out, key=lambda m: (m["hidden"] and not m["revealed"], pos[m["id"]] is None,
                                      pos[m["id"]] or 0, m["global_percent"] is None,
                                      -(m["global_percent"] or 0), m["name"], m["key"]))


def move_facets(session: Session, src: str, dst: str) -> None:
    """On merge: carry artwork/achievements over; duplicates keep the survivor's row.

    Moved achievement rows keep their id and key (the key leaves out the canonical id). A row whose source and
    name, or whose source and key, the survivor already has is a duplicate: the survivor's row stays. A row
    whose key the survivor holds under another source is kept and re-keyed with its derived key, never dropped."""
    have_art = {(a.kind, a.url) for a in session.scalars(select(Artwork).where(Artwork.canonical_id == dst))}
    for a in session.scalars(select(Artwork).where(Artwork.canonical_id == src)).all():
        if (a.kind, a.url) in have_art:
            session.delete(a)
        else:
            a.canonical_id, a.preferred = dst, False  # the survivor keeps its own defaults
    survivor = list(session.scalars(select(Achievement).where(Achievement.canonical_id == dst)))
    have_ach = {(a.source, a.norm_name) for a in survivor}
    have_key = {a.key: a.source for a in survivor}
    for a in session.scalars(select(Achievement).where(Achievement.canonical_id == src)).all():
        if (a.source, a.norm_name) in have_ach or have_key.get(a.key) == a.source:
            session.delete(a)
            continue
        if a.key in have_key:  # another source's row holds the key: keep this row under its derived key
            a.key = _new_key(a.source, {"name": a.name, "external_id": a.external_id}, set(have_key))
        a.canonical_id = dst
        have_ach.add((a.source, a.norm_name))
        have_key[a.key] = a.source
    session.flush()
