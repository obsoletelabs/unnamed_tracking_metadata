"""Artwork and achievements: per-source sets attached to a canonical record, merged by trust.

C12 (A18) adds, without changing the `ak1` keys or the signed feed's v1 bodies:
* achievements: sets (groups, DLC sets, editions, platforms), status and retirement instead of deletion,
  same-name achievements kept apart (F1), source order, translations, rarity with source and freshness, the
  spoiler rules of §12.1.8, and the merged order of §12.1.3;
* artwork: roles (`cover` alias), measured dimensions, aspect classes, exact duplicates by content hash,
  licence and attribution, regional and language variants, and the ranking of §12.5.7.
Everything new is local to this node until the achievements and artwork v2 bodies ship (protocol 1.7/1.8, C12-3
and C12-5): the signed copy of a list is the v1 body, computed by `achievements_v1` / `artwork_v1`."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, false, select
from sqlalchemy.orm import Session

from . import achievement_key, feed, feed_schema, media, vocab
from .merge import CLASS_RANK, source_class
from .models import Achievement, AchievementSet, AchievementStat, AchievementText, Artwork, Canonical, RecordLive, now
from .normalize import norm_namespace
from .providers.media_policy import policy as media_policy
from .service import AtlasError, Ctx, touch_version, write_lock

KINDS = ("key_art", "banner", "logo", "icon", "screenshot", "hero", "poster")
#: Input aliases of artwork roles (§12.5.1): stored under the existing kind, never renamed on the wire.
KIND_ALIASES = {"cover": "key_art"}
#: §12.5.1 expected aspect classes per role; `logo` fits any.
ROLE_ASPECTS: dict[str, frozenset[str] | None] = {
    "key_art": frozenset({"portrait"}), "poster": frozenset({"portrait", "tall"}),
    "banner": frozenset({"capsule", "wide", "landscape"}), "hero": frozenset({"wide", "landscape"}), "logo": None,
    "icon": frozenset({"square"}), "screenshot": frozenset({"landscape", "other"})}
MAX_ART = 200
MAX_ACH = 2000
MAX_SETS = 50
MAX_LOCALES = 40
MAX_PLATFORMS = 10
#: Licences that keep an image out of the CC0 signed feed at every protocol (§12.5.5, H47).
WITHHELD_LICENCES = frozenset({"cc-by", "cc-by-sa", "publisher_press", "provider_terms"})
FEDERABLE_UPLOAD_LICENCES = frozenset({"owner_upload", "cc0"})
NEAR_DUPLICATE_BITS = 6
_BAD_URL = re.compile(r"[\s\\\x00-\x1f]")
LOCALE_RE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8}){0,2}\Z")
SET_KEY_RE = re.compile(r"[a-z0-9._-]{1,64}\Z")
PLATFORM_RE = re.compile(r"[a-z0-9._-]{1,50}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_PRINTABLE = re.compile(r"[\x00-\x1f\x7f]")
_PROSE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # control characters other than tab and newlines
SET_KIND_ORDER = {k: n for n, k in enumerate(vocab.ACHIEVEMENT_SET_KINDS)}
ORDERS = ("rarity", "source", "name")


log_ = logging.getLogger(__name__)
_norm_name = achievement_key.norm_name
_ROW_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")  # an Atlas row id (ULID, Crockford base32)


def _url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 1000 or _BAD_URL.search(value) \
            or not value.startswith(("http://", "https://")):
        raise AtlasError("invalid url")
    return value


def clean_locale(value: Any, what: str = "locale") -> str:
    """A BCP 47 tag as §12.1.7 allows (`de`, `de-AT`, `zh-Hant-TW`), stored lower case."""
    if not isinstance(value, str) or len(value) > 35 or not LOCALE_RE.fullmatch(value.lower()):
        raise AtlasError(f"{what} must be a language tag such as en, de or pt-br")
    return value.lower()


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or _PRINTABLE.search(value):
        raise AtlasError(f"{what} must be printable text of at most {limit} characters")
    return value.strip()


def _licence(value: Any, what: str = "licence") -> str:
    if value not in vocab.LICENCES:
        raise AtlasError(f"{what} must be one of {vocab.LICENCES}")
    return str(value)


def clean_artwork(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) > MAX_ART:
        raise AtlasError(f"at most {MAX_ART} artwork items")
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            raise AtlasError("artwork items must be objects")
        kind = KIND_ALIASES.get(it.get("kind"), it.get("kind"))  # type: ignore[arg-type]
        if kind not in KINDS:
            raise AtlasError(f"kind must be one of {KINDS} (or cover, stored as key_art)")
        url = _url(it.get("url"))
        if (kind, url) in seen:
            continue
        seen.add((kind, url))
        try:
            row = {"kind": kind, "url": url, "priority": int(it.get("priority", 100))}
        except (TypeError, ValueError) as exc:
            raise AtlasError("priority must be a whole number") from exc
        for dim in ("width", "height"):
            if it.get(dim) is not None:
                if not isinstance(it[dim], int) or not 1 <= it[dim] <= 50000:
                    raise AtlasError(f"{dim} out of range")
                row[dim] = it[dim]
        if not 0 <= row["priority"] <= 10000:
            raise AtlasError("priority out of range")
        if it.get("region") not in (None, ""):
            from .dates import REGION_RE

            if not isinstance(it["region"], str) or not REGION_RE.fullmatch(it["region"]):
                raise AtlasError("region must be ww, eu, na, asia, oce, latam or an ISO 3166-1 alpha-2 code")
            row["region"] = it["region"]
        if it.get("locale") not in (None, ""):
            row["locale"] = clean_locale(it["locale"])
        if it.get("licence") not in (None, ""):
            row["licence"] = _licence(it["licence"])
        if it.get("attribution") not in (None, ""):
            row["attribution"] = _text(it["attribution"], "attribution", 300)
        if it.get("source_page_url") not in (None, ""):
            page = _url(it["source_page_url"])
            if not page.startswith("https://"):
                raise AtlasError("source_page_url must be an https address")
            row["source_page_url"] = page
        if it.get("local_only") is not None:
            if not isinstance(it["local_only"], bool):
                raise AtlasError("local_only must be true or false")
            if it["local_only"]:
                row["local_only"] = True
        out.append(row)
    return out


ACH_KINDS = vocab.ACHIEVEMENT_KINDS
ACH_TIERS = vocab.ACHIEVEMENT_TIERS  # bronze | silver | gold | platinum (C7.1)
#: The tier that needs federation protocol 1.2 before an authority may publish it (C1.6, H13).
PLATINUM = "platinum"
PLATINUM_PROTOCOL = vocab.VALUE_PROTOCOL[("achievement_tier", PLATINUM)]
HIDDEN_NAME = "Hidden achievement"
MAX_POINTS = 100000
#: The item fields of the v1 `achievements` body (C2, C7): everything else stays local until body v2 (§12.4).
V1_ITEM_FIELDS = ("name", "description", "external_id", "hidden", "icon_url", "locked_icon_url", "kind", "tier",
                  "points", "global_percent", "key")
V1_ART_FIELDS = ("kind", "url", "priority", "width", "height")


def _percent(value: Any, what: str = "global_percent") -> float:
    if not (isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100):
        raise AtlasError(f"{what} must be 0-100")
    return float(value)


def _as_of(value: Any) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise AtlasError("rarity as_of must be a date (YYYY-MM-DD)")
    try:
        when = date.fromisoformat(value)
    except ValueError as exc:
        raise AtlasError("rarity as_of must be a date (YYYY-MM-DD)") from exc
    if when > _today() + timedelta(days=1):  # a future date would stay "fresh" and newest for ever (§12.1.6)
        raise AtlasError("rarity as_of must not be in the future")
    return value


def clean_sets(sets: Any) -> list[dict[str, Any]]:
    """The list-level `sets` of an achievement list (§12.1.1). `base` always exists and need not be listed."""
    if sets in (None, ""):
        return []
    if not isinstance(sets, list) or len(sets) > MAX_SETS:
        raise AtlasError(f"sets must be a list of at most {MAX_SETS} objects")
    out, keys = [], set()
    for s in sets:
        if not isinstance(s, dict):
            raise AtlasError("sets must be a list of objects")
        key = s.get("key")
        if not isinstance(key, str) or not SET_KEY_RE.fullmatch(key):
            raise AtlasError("set key must be 1-64 characters of a-z, 0-9, '.', '_' or '-'")
        if key in keys:
            raise AtlasError(f"set {key!r} is listed twice")
        keys.add(key)
        kind = s.get("kind") or ("base" if key == "base" else None)
        if kind not in vocab.ACHIEVEMENT_SET_KINDS:
            raise AtlasError(f"set kind must be one of {vocab.ACHIEVEMENT_SET_KINDS}")
        if (kind == "base") != (key == "base"):
            raise AtlasError("only the set `base` is of kind base")
        row: dict[str, Any] = {"key": key, "kind": kind, "name": _text(s.get("name") or "", "set name", 200)}
        dlc = s.get("dlc")
        if dlc not in (None, ""):
            if kind not in ("dlc", "expansion"):
                raise AtlasError("only a dlc or expansion set names a dlc record")
            if not isinstance(dlc, str) or len(dlc) > 40 or not dlc.startswith("atlas:"):
                raise AtlasError("set dlc must be an Atlas record id (atlas:...)")
            row["dlc"] = dlc
        edition = s.get("edition")
        if edition not in (None, ""):
            if kind != "edition":
                raise AtlasError("only an edition set names an edition")
            row["edition"] = _text(edition, "set edition", 100)
        platforms = s.get("platforms")
        if platforms not in (None, []):
            if not isinstance(platforms, list) or len(platforms) > MAX_PLATFORMS or not all(
                    isinstance(p, str) and PLATFORM_RE.fullmatch(p) for p in platforms):
                raise AtlasError(f"set platforms must be at most {MAX_PLATFORMS} platform slugs (pc, ps5, ...)")
            row["platforms"] = sorted(set(platforms))
        pos = s.get("position")
        if pos is not None:
            if not isinstance(pos, int) or isinstance(pos, bool) or not 0 <= pos <= 10000:
                raise AtlasError("set position must be a whole number from 0 to 10000")
            row["position"] = pos
        out.append(row)
    return out


def _clean_i18n(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or len(value) > MAX_LOCALES:
        raise AtlasError(f"i18n must map at most {MAX_LOCALES} language tags to a name and description")
    out: dict[str, dict[str, str]] = {}
    for tag, text in value.items():
        loc = clean_locale(tag, "i18n language")
        if not isinstance(text, dict):
            raise AtlasError("each i18n entry must be an object with name and description")
        name = text.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AtlasError("each translation needs a name (max 200)")
        name = _text(name, "translated name", 200)
        description = text.get("description") or ""
        if not isinstance(description, str) or len(description) > 2000 or _PROSE_CONTROL.search(description):
            raise AtlasError("translated description must be text of at most 2000 characters")
        out[loc] = {"name": name, "description": description}
    return out


def clean_achievements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and normalise an achievement list. Inside one list an item is identified by its external id and
    name (F1, C12 §12.1.4 as amended in A18 #3): two items that share a name but not an external id are both kept;
    an exact repeat (same external id and name) keeps the first and is counted."""
    if len(items) > MAX_ACH:
        raise AtlasError(f"at most {MAX_ACH} achievements")
    out, seen, keys = [], set(), set()
    dropped = 0
    for it in items:
        if not isinstance(it, dict):
            raise AtlasError("achievement items must be objects")
        name = (it.get("name") or "").strip()
        if not name or len(name) > 200:
            raise AtlasError("achievement name required (max 200)")
        ext = str(it.get("external_id") or "")[:200]
        ident = (ext, _norm_name(name))
        if ident in seen:
            dropped += 1
            continue
        seen.add(ident)
        rarity = it.get("rarity")
        pct = it.get("global_percent")
        if rarity is not None:
            if not isinstance(rarity, dict) or set(rarity) - {"percent", "as_of", "source", "stale", "band"}:
                raise AtlasError("rarity must be an object with percent and as_of")
            if pct is None and rarity.get("percent") is not None:
                pct = rarity["percent"]
        if pct is not None:
            pct = _percent(pct)
        row: dict[str, Any] = {
            "name": name, "description": str(it.get("description") or "")[:2000],
            "external_id": ext, "hidden": bool(it.get("hidden", False)),
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
            row["global_percent"] = pct
            as_of = it.get("percent_as_of") or (rarity or {}).get("as_of")
            if as_of not in (None, ""):
                row["percent_as_of"] = _as_of(as_of)
        if it.get("set") not in (None, "", "base"):
            if not isinstance(it["set"], str) or not SET_KEY_RE.fullmatch(it["set"]):
                raise AtlasError("set must be the key of one of the list's sets")
            row["set"] = it["set"]
        if it.get("status") not in (None, ""):
            if it["status"] not in vocab.ACHIEVEMENT_STATUSES:
                raise AtlasError(f"status must be one of {vocab.ACHIEVEMENT_STATUSES}")
            row["status"] = it["status"]
        if it.get("locale") not in (None, ""):
            row["locale"] = clean_locale(it["locale"])
        if it.get("i18n") not in (None, {}):
            row["i18n"] = _clean_i18n(it["i18n"])
        if it.get("icon_licence") not in (None, ""):
            row["icon_licence"] = _licence(it["icon_licence"], "icon_licence")
        if it.get("icon_attribution") not in (None, ""):
            row["icon_attribution"] = _text(it["icon_attribution"], "icon_attribution", 300)
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
    if dropped:
        log_.info("achievement list: %d repeated item(s) dropped (ach_dup_identity)", dropped)
    return out


def achievements_v1(items: list[dict[str, Any]], settings: Any = None, source: str = "") -> tuple[
        list[dict[str, Any]], dict[str, int]]:
    """The signed copy of a list while the feed carries achievements body v1 (§12.4 downgrade below 1.7).

    Drops every C12 field, every later item whose name repeats an earlier one (a v1 follower keeps the first of
    a name), and the icons of items whose icon licence requires attribution (§12.5.5). Returns the items and the
    counts of what was dropped (`ach_downgrade`)."""
    out, names = [], set()
    counts = {"items": 0, "fields": 0, "icons": 0}
    default = media_policy(settings, source).licence if settings is not None else None
    for it in items:
        norm = _norm_name(it["name"])
        if norm in names:
            counts["items"] += 1
            continue
        names.add(norm)
        row = {k: it[k] for k in V1_ITEM_FIELDS if k in it}
        counts["fields"] += sum(1 for k in it if k not in V1_ITEM_FIELDS and k != "id")
        if (it.get("icon_licence") or default) in WITHHELD_LICENCES:
            for field in ("icon_url", "locked_icon_url"):
                if row.pop(field, None) is not None:
                    counts["icons"] += 1
        out.append(row)
    return out, counts


def artwork_v1(items: list[dict[str, Any]], *, source: str, settings: Any) -> tuple[list[dict[str, Any]], int]:
    """The signed copy of an artwork list (§12.5.5, §12.8 downgrade): rows whose licence keeps them out of the CC0
    feed and `local_only` rows are withheld (counted as `licence_withheld`); the rest keep the v1 fields."""
    default = media_policy(settings, source).licence
    out, withheld = [], 0
    for it in items:
        if it.get("local_only") or (it.get("licence") or default) in WITHHELD_LICENCES:
            withheld += 1
            continue
        out.append({k: it[k] for k in V1_ART_FIELDS if k in it})
    return out, withheld


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


def _texts_of(session: Session, ids: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    out: dict[str, dict[str, dict[str, str]]] = {}
    for chunk in range(0, len(ids), 500):
        for t in session.scalars(select(AchievementText).where(AchievementText.achievement_id.in_(
                ids[chunk:chunk + 500]))):
            out.setdefault(t.achievement_id, {})[t.locale] = {"name": t.name, "description": t.description}
    return out


def achievement_items(rows: list[Achievement], session: Session | None = None) -> list[dict[str, Any]]:
    """Rows back to the item shape clean_achievements/set_achievements accept (used to snapshot for revert and by
    the editor). Retired rows are not part of a list. With a session the translations come along too."""
    rows = [a for a in rows if a.retired_at is None]
    texts = _texts_of(session, [a.id for a in rows]) if session is not None else {}
    out = []
    for a in rows:
        item: dict[str, Any] = {"name": a.name, "description": a.description, "external_id": a.external_id,
                                "hidden": a.hidden, "key": a.key}
        for field in ("icon_url", "locked_icon_url", "kind", "tier", "points", "global_percent", "status",
                      "icon_licence", "icon_attribution"):
            if getattr(a, field) is not None:
                item[field] = getattr(a, field)
        if a.global_percent is not None and a.percent_as_of is not None:
            item["percent_as_of"] = a.percent_as_of.isoformat()
        if a.set_key and a.set_key != "base":
            item["set"] = a.set_key
        if a.text_locale:
            item["locale"] = a.text_locale
        if texts.get(a.id):
            item["i18n"] = texts[a.id]
        out.append(item)
    return out


def set_items(session: Session, cid: str, source: str) -> list[dict[str, Any]]:
    """The list-level `sets` one source defines on a record, in the shape `clean_sets` accepts."""
    out = []
    for s in session.scalars(select(AchievementSet).where(
            AchievementSet.canonical_id == cid, AchievementSet.source == source).order_by(AchievementSet.set_key)):
        row: dict[str, Any] = {"key": s.set_key, "kind": s.kind, "name": s.name}
        for field, value in (("dlc", s.dlc_canonical_id), ("edition", s.edition), ("platforms", s.platforms),
                             ("position", s.position)):
            if value not in (None, []):
                row[field] = value
        out.append(row)
    return out


def achievement_list(session: Session, cid: str, source: str) -> dict[str, Any]:
    """A source's whole list as `set_achievements` takes it back: items in order, with sets and translations."""
    rows = list(session.scalars(select(Achievement).where(
        Achievement.canonical_id == cid, Achievement.source == source, Achievement.retired_at.is_(None)).order_by(
        Achievement.position.is_(None), Achievement.position, Achievement.source_order.is_(None),
        Achievement.source_order, Achievement.name, Achievement.id)))
    return {"items": achievement_items(rows, session), "sets": set_items(session, cid, source)}


def _live(session: Session, cid: str) -> Canonical:
    row = session.get(Canonical, cid)
    if row is None or row.merged_into:
        raise AtlasError("unknown or merged canonical", "not_found")
    return row


def adopt_local_blob(ctx: Ctx, art: Artwork) -> None:
    """An image uploaded to this node is addressed by its own public /media/<sha256> URL: it is already cached.

    Followers fetch the same URL from this node like any other image, so uploads federate without a special case."""
    base = media.own_media_base(ctx.settings)
    if not art.url.startswith(base) or art.blob_hash:
        return
    found = media.find_blob(ctx.settings, art.url[len(base):])
    if found is not None:
        path, ctype = found
        digest = art.url[len(base):]
        art.blob_hash, art.content_type, art.size_bytes, art.cached_at = digest, ctype, path.stat().st_size, now()
        data = path.read_bytes()
        try:
            _ext, _ctype, w, h = media.validate_image(data, ctx.settings)
        except media.MediaError:
            return
        media.measure(art, data, w, h)


_ART_FIELDS = ("region", "locale", "licence", "attribution", "source_page_url")


def set_artwork(session: Session, ctx: Ctx, cid: str, source: str, items: list[dict[str, Any]], *,
                redistributable: bool = False, log: bool = True, skipped: list[dict[str, Any]] | None = None) -> int:
    """Replace the artwork a source contributes to a record. Cached bytes survive for unchanged URLs.

    An upload (this node's own /media/ address) is `owner_upload` unless it says otherwise, and one whose licence
    cannot enter the CC0 feed must be marked `local_only` (§12.5.5, H47: `licence_not_federable`). An image another
    source of the record already lists (same kind and address) stays that source's; it is not stored for this one,
    and `skipped`, when given, receives `{kind, url, held_by, reason}` for each (Phase 4 note O3); the signed
    entry leaves such images out too. Returns the number of items given, as before."""
    write_lock(session)
    _live(session, cid)
    source = norm_namespace(source)
    items = clean_artwork(items)
    for i in items:
        if media.is_upload(ctx.settings, i["url"]):
            i.setdefault("licence", "owner_upload")
            if i["licence"] not in FEDERABLE_UPLOAD_LICENCES and not i.get("local_only") and redistributable:
                raise AtlasError(f"an uploaded image under {i['licence']!r} cannot enter the CC0 feed; mark it "
                                 "local_only (licence_not_federable)", "invalid", reason="licence_not_federable")
    existing = {(a.kind, a.url): a for a in session.scalars(
        select(Artwork).where(Artwork.canonical_id == cid, Artwork.source == source))}
    wanted = {(i["kind"], i["url"]) for i in items}
    held: set[tuple[str, str]] = set()
    for key, art in existing.items():
        if key not in wanted:
            session.delete(art)
    for i in items:
        art = existing.get((i["kind"], i["url"]))
        if art:
            art.priority = i["priority"]
            if "width" in i or "height" in i:
                art.declared_width = i.get("width", art.declared_width)
                art.declared_height = i.get("height", art.declared_height)
                if art.aspect is None:  # not measured yet: the declaration is what width/height hold
                    art.width, art.height = art.declared_width, art.declared_height
            art.redistributable = redistributable
        else:
            clash = session.scalars(select(Artwork).where(
                Artwork.canonical_id == cid, Artwork.kind == i["kind"], Artwork.url == i["url"])).first()
            if clash:  # another source already lists this exact image; it keeps it
                held.add((i["kind"], i["url"]))
                if skipped is not None:
                    skipped.append({"kind": i["kind"], "url": i["url"], "held_by": clash.source,
                                    "reason": "held_by_another_source"})
                continue
            art = Artwork(canonical_id=cid, source=source, kind=i["kind"], url=i["url"],
                          priority=i["priority"], width=i.get("width"), height=i.get("height"),
                          declared_width=i.get("width"), declared_height=i.get("height"),
                          redistributable=redistributable)
            adopt_local_blob(ctx, art)
            session.add(art)
        for field in _ART_FIELDS:
            setattr(art, field, i.get(field))
        art.local_only = bool(i.get("local_only"))
    session.flush()
    dedupe(session, ctx.settings, cid)
    # Clients see every change (C3.1); only redistributable data on an authority enters the signed feed.
    signed = log and redistributable and ctx.settings.role == "authority"
    touch_version(session, cid, cause="feed" if signed else "local")
    if signed:
        # what was not stored for this source is not signed as its own either, so a follower stores what this
        # node stores, attributed the same way (A18b #4)
        body, withheld = artwork_v1([i for i in items if (i["kind"], i["url"]) not in held], source=source,
                                    settings=ctx.settings)
        if withheld:
            log_.info("artwork %s/%s: %d image(s) kept out of the signed feed (licence_withheld)", cid, source,
                      withheld)
        feed.emit_or_local(session, ctx, "artwork", {"canonical_id": cid, "source": source, "items": body})
    return len(items)


def adopt_local_icons(ctx: Ctx, row: Achievement) -> None:
    """An icon uploaded to this node is addressed by its own /media/<sha256> URL and is already stored."""
    base = media.own_media_base(ctx.settings)
    for which in ("icon", "locked_icon"):
        url = getattr(row, f"{which}_url")
        if url and url.startswith(base) and not getattr(row, f"{which}_blob_hash"):
            digest = url[len(base):]
            if media.find_blob(ctx.settings, digest) is not None:
                setattr(row, f"{which}_blob_hash", digest)


_ICON_STATE = {"icon": ("icon_blob_hash", "icon_fail_count", "icon_next_try_at", "icon_last_error"),
               "locked_icon": ("locked_icon_blob_hash", "locked_icon_fail_count", "locked_icon_next_try_at",
                               "locked_icon_last_error")}


def _match_rows(rows: list[Achievement], items: list[dict[str, Any]]) -> list[Achievement | None]:
    """Pair each incoming item with at most one existing row of the same source (C2.2 step 2), retired rows
    included (a returning item revives its row, id and key; §12.1.3).

    All items are tried against their explicit key first, then their identity (external id and name), then their
    external id alone (a provider rename keeps the row), then their normalised name, so an item is never beaten to
    a row by another item's weaker match. The name pass matches only a name that is unique among the unmatched
    items and the unmatched rows (§12.1.4): two same-name achievements are never paired by name alone."""
    by_key = {r.key: r for r in rows}
    by_ext: dict[str, list[Achievement]] = {}
    for r in rows:
        if r.external_id:
            by_ext.setdefault(r.external_id, []).append(r)
    by_ident = {(r.external_id, r.norm_name): r for r in rows}
    match: list[Achievement | None] = [None] * len(items)
    claimed: set[str] = set()

    def claim(i: int, row: Achievement | None) -> None:
        if row is not None and row.id not in claimed and match[i] is None:
            match[i] = row
            claimed.add(row.id)

    for i, it in enumerate(items):
        claim(i, by_key.get(it.get("key", "")))
    for i, it in enumerate(items):
        if match[i] is None:
            claim(i, by_ident.get((it["external_id"], _norm_name(it["name"]))))
    for i, it in enumerate(items):
        if match[i] is None and it["external_id"]:
            claim(i, next((r for r in by_ext.get(it["external_id"], []) if r.id not in claimed), None))
    names_left: dict[str, int] = {}
    for i, it in enumerate(items):
        if match[i] is None:
            n = _norm_name(it["name"])
            names_left[n] = names_left.get(n, 0) + 1
    rows_left: dict[str, list[Achievement]] = {}
    for r in rows:
        if r.id not in claimed:
            rows_left.setdefault(r.norm_name, []).append(r)
    for i, it in enumerate(items):
        n = _norm_name(it["name"])
        if match[i] is None and names_left.get(n) == 1 and len(rows_left.get(n, [])) == 1:
            claim(i, rows_left[n][0])
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


def _today() -> date:
    return datetime.now(UTC).date()


def _write_sets(session: Session, cid: str, source: str, sets: list[dict[str, Any]]) -> None:
    have = {s.set_key: s for s in session.scalars(select(AchievementSet).where(
        AchievementSet.canonical_id == cid, AchievementSet.source == source))}
    wanted = {s["key"] for s in sets}
    for key, row in have.items():
        if key not in wanted:
            session.delete(row)
    for s in sets:
        row = have.get(s["key"])
        if row is None:
            row = AchievementSet(canonical_id=cid, source=source, set_key=s["key"])
            session.add(row)
        row.kind, row.name = s["kind"], s["name"]
        row.dlc_canonical_id, row.edition = s.get("dlc"), s.get("edition")
        row.platforms, row.position = s.get("platforms"), s.get("position")


def _write_texts(session: Session, row: Achievement, i18n: dict[str, dict[str, str]] | None) -> None:
    have = {t.locale: t for t in session.scalars(select(AchievementText).where(
        AchievementText.achievement_id == row.id))} if row.id else {}
    wanted = i18n or {}
    for loc, t in have.items():
        if loc not in wanted:
            session.delete(t)
    for loc, text in wanted.items():
        t = have.get(loc)
        if t is None:
            session.add(AchievementText(achievement_id=row.id, locale=loc, name=text["name"],
                                        description=text["description"]))
        else:
            t.name, t.description = text["name"], text["description"]


def set_achievements(session: Session, ctx: Ctx, cid: str, source: str, items: list[dict[str, Any]], *,
                     sets: list[dict[str, Any]] | None = None, redistributable: bool = False, log: bool = True,
                     ordered: bool = False, adopt_keys: bool = False, measured_at: date | None = None,
                     written_on: date | None = None) -> int:
    """Replace what one source says, reconciling its rows in place (CONTRACTS-v1 C2.2, C12 §12.1).

    Each item is matched to an existing row of this source by its key (or the row id the editor submits),
    then its identity, then its external id, then its (unique) normalised name. A matched row keeps its id and
    key and is updated; its cached icons survive when the address is unchanged. Unmatched items become new rows.
    Unmatched rows are **retired**, not deleted (§12.1.3): they keep their id and key, are hidden from lists, and
    come back when the item returns; `purge_retired` deletes them after the retention. `ordered` keeps the given
    order as `position` (for lists people arranged by hand); `source_order` is always the list index. `sets` is
    the list-level set definitions (absent: every item is in `base`). `adopt_keys` is for signed feed entries:
    the authority's key replaces a mirror's own, so every node publishes the same key. `measured_at` is the date
    a provider measured its percentages (an import passes today). `written_on` stands for "today" when a
    percent arrives without a date: a mirror passes the date of the signed entry's origin timestamp, so the
    authority and every mirror store the same date (§12.1.6)."""
    write_lock(session)
    _live(session, cid)
    source = norm_namespace(source)
    items = clean_achievements(items)
    set_rows = clean_sets(sets)
    from .matching import follow

    for s in set_rows:  # §12.1.1: a DLC set names the DLC's own record, never the record it belongs to
        if s.get("dlc") and (s["dlc"] == cid or follow(session, s["dlc"]) == cid):
            raise AtlasError(f"set {s['key']!r} names this record as its own dlc")
    known = {"base"} | {s["key"] for s in set_rows}
    for it in items:
        if it.get("set", "base") not in known:
            raise AtlasError(f"achievement {it['name'][:60]!r} names the unknown set {it['set']!r}")
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

    # 1. rows no item matched are retired (they keep id and key). One whose identity an item now takes is a
    # superseded copy and is deleted, so the identity is free (C2.2 step 5).
    idents = {(it["external_id"], _norm_name(it["name"])) for it in items}
    stamp = now()
    retiring: list[Achievement] = []
    for r in rows:
        if r.id in kept:
            continue
        if (r.external_id, r.norm_name) in idents:
            session.delete(r)
        else:
            retiring.append(r)
            if r.retired_at is None:
                r.retired_at = stamp
    session.flush()

    # 2. final keys. A matched row keeps its key. Under `adopt_keys` it takes the item's key instead when no
    # row of another source on the record (or a retired row of this one) holds that key; rows of this source are
    # all being reconciled here, so two of them may swap keys (step 3 makes that safe). A row whose key another
    # row adopts is re-keyed.
    other = set(session.scalars(select(Achievement.key).where(Achievement.canonical_id == cid,
                                                              Achievement.source != source)))
    other |= {r.key for r in retiring}
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
              and (r.norm_name != _norm_name(items[i]["name"]) or r.key != final[i]
                   or r.external_id != items[i]["external_id"])]
    for r in moving:
        r.norm_name, r.key = f"~{r.id}", f"~{r.id}"
    if moving:
        session.flush()

    # 4. update matched rows in place, add the new ones
    today = written_on or _today()
    texts: list[tuple[Achievement, dict[str, Any] | None]] = []
    for index, (it, row, key) in enumerate(zip(items, match, final, strict=True)):
        before_pct, before_as_of = (None, None) if row is None else (row.global_percent, row.percent_as_of)
        if row is None:
            row = Achievement(canonical_id=cid, source=source)
            session.add(row)
        else:
            for which, state in _ICON_STATE.items():  # an unchanged image is not downloaded again
                if getattr(row, f"{which}_url") != it.get(f"{which}_url"):
                    for attr, empty in zip(state, (None, 0, None, None), strict=True):
                        setattr(row, attr, empty)
        row.key, row.position, row.source_order = key, (index if ordered else None), index
        row.external_id, row.name, row.norm_name = it["external_id"], it["name"], _norm_name(it["name"])
        row.description, row.hidden = it["description"], it["hidden"]
        row.icon_url, row.locked_icon_url = it.get("icon_url"), it.get("locked_icon_url")
        row.kind, row.tier, row.points = it.get("kind"), it.get("tier"), it.get("points")
        row.global_percent, row.redistributable = it.get("global_percent"), redistributable
        row.set_key, row.status, row.text_locale = it.get("set", "base"), it.get("status"), it.get("locale")
        row.icon_licence, row.icon_attribution = it.get("icon_licence"), it.get("icon_attribution")
        row.retired_at = None
        # §12.1.6: when the percent was measured. A stated date wins; a provider import passes the fetch date; an
        # unchanged value keeps its date; otherwise the write date (on a mirror: the signed entry's origin date).
        pct = it.get("global_percent")
        if pct is None:
            row.percent_as_of = None
        elif it.get("percent_as_of"):
            row.percent_as_of = date.fromisoformat(it["percent_as_of"])
        elif measured_at is not None:
            row.percent_as_of = measured_at
        elif before_pct == pct and before_as_of is not None:
            row.percent_as_of = before_as_of
        else:
            row.percent_as_of = today
        adopt_local_icons(ctx, row)
        it["key"] = key  # the feed carries the stored key (reserved-optional since 1.1, C1.5)
        texts.append((row, it.get("i18n")))
    session.flush()
    for row, i18n in texts:
        _write_texts(session, row, i18n)
    _write_sets(session, cid, source, set_rows)
    session.flush()
    # Clients see every change (C3.1); only redistributable data on an authority enters the signed feed.
    signed = log and redistributable and ctx.settings.role == "authority"
    touch_version(session, cid, cause="feed" if signed else "local")
    if signed:
        body, counts = achievements_v1(items, ctx.settings, source)
        if any(counts.values()):
            log_.info("achievements %s/%s signed as v1: dropped %d item(s), %d field(s), %d icon(s) "
                      "(ach_downgrade)", cid, source, counts["items"], counts["fields"], counts["icons"])
        feed.emit_or_local(session, ctx, "achievements", {"canonical_id": cid, "source": source, "items": body})
    return len(items)


def purge_retired(session: Session, settings: Any, *, batch: int = 1000) -> int:
    """Delete achievement rows retired longer than `achievement_retired_retention_days` (H37), in batches."""
    cutoff = now() - timedelta(days=settings.achievement_retired_retention_days)
    total = 0
    while True:
        ids = list(session.scalars(select(Achievement.id).where(
            Achievement.retired_at.is_not(None), Achievement.retired_at < cutoff).limit(batch)))
        if not ids:
            return total
        session.execute(delete(AchievementText).where(AchievementText.achievement_id.in_(ids)))
        session.execute(delete(Achievement).where(Achievement.id.in_(ids)))
        session.flush()
        total += len(ids)
        if len(ids) < batch:
            return total


def store_stats(session: Session, cid: str, source: str, percents: dict[str, float], *,
                as_of: date | None = None) -> int:
    """Store a provider's global percentages fetched apart from its definitions (keyless Steam, §12.1.6).
    Local only; matched to rows at read time; never creates a row."""
    when = as_of or _today()
    have = {s.external_id: s for s in session.scalars(select(AchievementStat).where(
        AchievementStat.canonical_id == cid, AchievementStat.source == source))}
    n = 0
    for ext, pct in list(percents.items())[:MAX_ACH]:
        if not isinstance(ext, str) or not 0 < len(ext) <= 200:
            continue
        try:
            value = _percent(pct)
        except AtlasError:
            continue
        row = have.get(ext)
        if row is None:
            session.add(AchievementStat(canonical_id=cid, source=source, external_id=ext, percent=value, as_of=when,
                                        fetched_at=now()))
        else:
            row.percent, row.as_of, row.fetched_at = value, when, now()
        n += 1
    session.flush()
    return n


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


# ---- artwork ranking (§12.5.7) ------------------------------------------------------------------------------

def role_fit(art: Artwork) -> bool | None:
    """Whether the measured aspect suits the role (§12.5.3); None while unmeasured."""
    if art.aspect is None:
        return None
    want = ROLE_ASPECTS.get(art.kind)
    return True if want is None else art.aspect in want


def _region_match(have: str | None, want: str | None) -> int:
    if want is None:
        return 2
    if have == want:
        return 2
    return 1 if have in (None, "ww") else 0


def _lang_match(have: str | None, want: str | None) -> int:
    if want is None:
        return 2
    if have is None:
        return 1
    if have == want:
        return 2
    return 1 if have.split("-")[0] == want.split("-")[0] else 0


def art_rank(settings: Any, a: Artwork, *, region: str | None = None, lang: str | None = None,
             with_duplicate: bool = True) -> tuple[Any, ...]:
    """§12.5.7, smaller is better: curator first, then the community's `preferred`, then not a duplicate, role fit,
    class, trust, region and language match, priority, measured pixels; ties break on url and source only, values
    that are the same on every node."""
    cls = CLASS_RANK[source_class(a.source)]
    fit = role_fit(a)
    pixels = (a.width or 0) * (a.height or 0) if a.aspect is not None else 0
    return (cls != CLASS_RANK["curator"], not a.preferred, with_duplicate and a.duplicate_of is not None,
            fit is False, -cls, -settings.trust(a.source), -_region_match(a.region, region),
            -_lang_match(a.locale, lang), a.priority, -pixels, a.url, a.source)


def dedupe(session: Session, settings: Any, cid: str) -> None:
    """Exact duplicates (§12.5.4): rows of one record and kind holding the same bytes; the best-ranked one stays
    visible and the others point at it with `duplicate_of`. Rows are never deleted for being duplicates."""
    rows = list(session.scalars(select(Artwork).where(Artwork.canonical_id == cid)))
    groups: dict[tuple[str, str], list[Artwork]] = {}
    for a in rows:
        if a.blob_hash:
            groups.setdefault((a.kind, a.blob_hash), []).append(a)
    seen: set[str] = set()
    for group in groups.values():
        group.sort(key=lambda a: art_rank(settings, a, with_duplicate=False))
        best = group[0]
        for a in group:
            target = None if a is best else best.id
            if a.duplicate_of != target:
                a.duplicate_of = target
            seen.add(a.id)
    for a in rows:
        if a.id not in seen and a.duplicate_of is not None:
            a.duplicate_of = None


def near_duplicates(rows: list[Artwork]) -> dict[str, str]:
    """Rows of one kind and aspect class whose dhash differs in at most 6 bits from a better-ranked row: reported
    (curate API) as `near_duplicate_of`, never hidden (§12.5.4, D12.13). `rows` must be in rank order."""
    out: dict[str, str] = {}
    for n, a in enumerate(rows):
        if not a.dhash or a.duplicate_of:
            continue
        for b in rows[:n]:
            if b.kind == a.kind and b.aspect == a.aspect and b.dhash and not b.duplicate_of \
                    and b.blob_hash != a.blob_hash and media.hamming(a.dhash, b.dhash) <= NEAR_DUPLICATE_BITS:
                out[a.id] = b.id
                break
    return out


def legacy_art_rank(settings: Any, a: Artwork) -> tuple[Any, ...]:
    """The order `GET /games/{cid}/artwork` has always listed images in, across kinds (before C12): the community's
    `preferred` first, then trust, priority and age; url and source only break the remaining ties. Clients that
    take the first image rely on it, so it stays the API default (C12 §12.5.7, amendment A18 #18)."""
    return (not a.preferred, -settings.trust(a.source), a.priority, a.created_at, a.url, a.source)


ART_ORDERS = ("legacy", "quality")


def merged_artwork(session: Session, ctx: Ctx, cid: str, *, region: str | None = None, lang: str | None = None,
                   include_duplicates: bool = True, order: str = "quality", viewer: Any = None) -> list[Artwork]:
    """Every image of a record. `order="quality"` (default here) ranks by §12.5.7; `order="legacy"` keeps the
    pre-C12 list order that v1 API clients see by default. `viewer` (C14 §5.7): only the rows that viewer may see
    (None: every row, for internal callers)."""
    from . import scope

    if order not in ART_ORDERS:
        raise ValueError(f"unknown artwork order {order!r}")
    clause = scope.artwork_clause(viewer, ctx.settings)
    rows = list(session.scalars(select(Artwork).where(Artwork.canonical_id == cid,
                                                      *(() if clause is None else (clause,)))))
    if not include_duplicates:
        rows = [a for a in rows if a.duplicate_of is None]
    if order == "legacy":
        rows.sort(key=lambda a: legacy_art_rank(ctx.settings, a))
    else:
        rows.sort(key=lambda a: art_rank(ctx.settings, a, region=region, lang=lang))
    return rows


def artwork_items(rows: list[Artwork]) -> list[dict[str, Any]]:
    """Rows back to the item shape `set_artwork` accepts, with every field (proposal revert snapshots, QV15)."""
    out = []
    for a in rows:
        item: dict[str, Any] = {"kind": a.kind, "url": a.url, "priority": a.priority}
        width = a.declared_width if a.aspect is not None else a.width
        height = a.declared_height if a.aspect is not None else a.height
        if width is not None:
            item["width"] = width
        if height is not None:
            item["height"] = height
        for field in _ART_FIELDS:
            if getattr(a, field) is not None:
                item[field] = getattr(a, field)
        if a.local_only:
            item["local_only"] = True
        out.append(item)
    return out


def _media(blob: str | None) -> str | None:
    return f"/api/v2/media/{blob}" if blob else None


# ---- merged achievements (§12.1) -----------------------------------------------------------------------------

def _set_signature(kind: str, name: str, dlc: str | None, edition: str | None,
                   platforms: list[str] | None) -> tuple[Any, ...]:
    """Two sources' sets are one merged set when this is equal (§12.1.1)."""
    if kind == "base":
        return ("base",)
    if kind in ("dlc", "expansion") and dlc:
        return (kind, "dlc", dlc)
    if kind == "edition" and edition:
        return (kind, "edition", _norm_name(edition))
    if kind == "platform" and platforms:
        return (kind, "platforms", tuple(sorted(platforms)))
    return (kind, "name", _norm_name(name))


def _merged_sets(session: Session, ctx: Ctx, cid: str, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """(source, set key) -> merged set, for every pair in use. The merged id is the smallest member
    `"<source>/<key>"`, stable across nodes and trust settings."""
    from .matching import follow

    defined = {(s.source, s.set_key): s for s in session.scalars(select(AchievementSet).where(
        AchievementSet.canonical_id == cid))}
    members: dict[tuple[Any, ...], list[tuple[str, str, dict[str, Any]]]] = {}
    for source, key in pairs:
        s = defined.get((source, key))
        if key == "base" or s is None:
            info = {"kind": "base" if key == "base" else "other", "name": "" if key == "base" else key, "dlc": None,
                    "edition": None, "platforms": None, "position": None}
        else:
            dlc = s.dlc_canonical_id
            if dlc:
                live = follow(session, dlc)
                dlc = live if session.get(Canonical, live) is not None else None  # unknown here: absent (§12.4)
            info = {"kind": s.kind, "name": s.name, "dlc": dlc, "edition": s.edition, "platforms": s.platforms,
                    "position": s.position}
        sig = _set_signature(info["kind"], info["name"], info["dlc"], info["edition"], info["platforms"])
        members.setdefault(sig, []).append((source, key, info))
    out: dict[tuple[str, str], dict] = {}
    for group in members.values():
        sid = "/".join(min((s, k) for s, k, _ in group))
        best = min(group, key=lambda m: (-CLASS_RANK[source_class(m[0])], -ctx.settings.trust(m[0]), m[0], m[1]))[2]
        positions = [m[2]["position"] for m in group if m[2]["position"] is not None]
        merged = {"id": sid, "key": sid.split("/", 1)[1], "kind": best["kind"], "name": best["name"],
                  "dlc": best["dlc"], "edition": best["edition"], "platforms": sorted(best["platforms"] or []),
                  "position": min(positions) if positions else None,
                  "sources": sorted({s for s, _k, _i in group})}
        for source, key, _info in group:
            out[(source, key)] = merged
    return out


def _band(pct: float) -> str:
    return next(name for name, low in vocab.RARITY_BANDS if pct >= low)


def _rarity(cands: list[tuple[int, float, date | None, int, str]], today: date,
            max_age_days: int) -> dict[str, Any] | None:
    """§12.1.6 merged rarity. A candidate is (class rank, percent, as_of, trust, source)."""
    if not cands:
        return None
    cutoff = today - timedelta(days=max_age_days)
    fresh = [c for c in cands if c[2] is not None and c[2] >= cutoff]
    provider = [c for c in fresh if c[0] == CLASS_RANK["provider"]]
    if provider:  # (1) a measurement: newest, then trust, then source name
        best, stale = min(provider, key=lambda c: (-c[2].toordinal(), -c[3], c[4])), False  # type: ignore[union-attr]
    elif fresh:  # (2) a fresh curated or community value: highest class, then newest
        best, stale = min(fresh, key=lambda c: (-c[0], -c[2].toordinal(), -c[3], c[4])), False  # type: ignore[union-attr]
    else:  # (3) nothing fresh: the newest of any class, marked stale
        best, stale = min(cands, key=lambda c: (-(c[2] or date.min).toordinal(), -c[0], -c[3], c[4])), True
    return {"percent": best[1], "source": best[4], "as_of": best[2].isoformat() if best[2] else None,
            "stale": stale, "band": _band(best[1])}


def _pick_lang(want: str | None, texts: list[dict[str, dict[str, str]]]) -> tuple[str | None, dict[str, str] | None]:
    """§12.1.7: exact tag, then the language subtag, else None (the default text). `texts` is in source rank order;
    per locale the best-ranked source that has it wins."""
    if not want:
        return None, None
    for tag in (want, want.split("-")[0]):
        for t in texts:
            if tag in t:
                return tag, t[tag]
    return None, None


def achievement_listing(session: Session, ctx: Ctx, cid: str, *, reveal: bool | set[str] = False,
                        include_retired: bool = False, lang: str | None = None, order: str | None = None,
                        set_filter: str | None = None, platform: str | None = None,
                        viewer: Any = None) -> dict[str, Any]:
    """The merged achievements of a record (§12.1, §12.2): `sets`, `results`, `lang`, `revealed_any`.

    Entries: rows of different sources are one entry when they are in the same merged set and have the same
    normalised name, and that name is unique within each source's rows of the set (§12.1.5 rule 2; curator
    `same_as`/`distinct_from` links ship with C12-3). Inside a set the order is the positions of the
    highest-class source that has any, then rarity (or `order=source|name`), then name, then key. Hidden entries
    are redacted unless revealed (§12.1.8) and unrevealed ones are last in their set, ordered by key only.

    Identity (C2.3): `keys` are the stored keys of every row merged into the entry, sorted, and `key` is the
    smallest of them. It does not depend on `source_trust`, so every node names the entry alike. `id` is the
    top-trust row's id: stable on this node, kept for older clients, deprecated as an identity."""
    if order not in (None, *ORDERS):
        raise AtlasError(f"order must be one of {ORDERS}")
    want = clean_locale(lang, "lang") if lang else None
    from . import scope

    q = select(Achievement).where(Achievement.canonical_id == cid)
    if not include_retired:
        q = q.where(Achievement.retired_at.is_(None))
    if not scope.achievements_visible(session, viewer, ctx.settings, cid):  # C14 §5.7, D1: withheld by default
        q = q.where(false())
    elif (rows_rule := scope.achievement_clause(viewer, ctx.settings)) is not None:
        q = q.where(rows_rule)
    rows = list(session.scalars(q))
    trust = ctx.settings.trust
    rank = {s: (CLASS_RANK[source_class(s)], trust(s)) for s in {a.source for a in rows}}
    rows.sort(key=lambda a: (-trust(a.source), a.id))
    sets = _merged_sets(session, ctx, cid, {(a.source, a.set_key) for a in rows})
    texts = _texts_of(session, [a.id for a in rows]) if want else {}
    # stored percentages by external id (keyless Steam): they join any row that names the same external id
    stats: dict[str, list[tuple[str, float, date]]] = {}
    for st_row in session.scalars(select(AchievementStat).where(AchievementStat.canonical_id == cid)):
        stats.setdefault(st_row.external_id, []).append((st_row.source, st_row.percent, st_row.as_of))

    # rule 2: a name repeated inside one source's rows of a set keeps those rows apart
    per_source: dict[tuple[str, str, str], int] = {}
    for a in rows:
        k = (sets[(a.source, a.set_key)]["id"], a.source, a.norm_name)
        per_source[k] = per_source.get(k, 0) + 1
    split = {(sid, n) for (sid, _s, n), count in per_source.items() if count > 1}
    groups: dict[tuple[str, ...], list[Achievement]] = {}
    for a in rows:
        sid = sets[(a.source, a.set_key)]["id"]
        gk: tuple[str, ...] = (sid, a.norm_name, a.source, a.id) if (sid, a.norm_name) in split else (sid, a.norm_name)
        groups.setdefault(gk, []).append(a)

    # per set: whose positions order it (highest class, then trust, then name) and whose source order
    def best_source(cands: set[str]) -> str | None:
        return min(cands, key=lambda s: (-rank[s][0], -rank[s][1], s)) if cands else None

    positioned: dict[str, set[str]] = {}
    providers: dict[str, set[str]] = {}
    for a in rows:
        sid = sets[(a.source, a.set_key)]["id"]
        if a.position is not None:
            positioned.setdefault(sid, set()).add(a.source)
        if source_class(a.source) == "provider" and a.source_order is not None:
            providers.setdefault(sid, set()).add(a.source)
    pos_source = {sid: best_source(s) for sid, s in positioned.items()}
    order_source = {sid: best_source(s) for sid, s in providers.items()}

    asked = reveal if isinstance(reveal, set) else set()
    today, max_age = _today(), ctx.settings.achievement_percent_max_age_days
    entries = []
    for members in groups.values():
        first = members[0]
        sid = sets[(first.source, first.set_key)]["id"]
        mset = sets[(first.source, first.set_key)]
        active = [a for a in members if a.retired_at is None] or members
        m: dict[str, Any] = {"hidden": False, "kind": None, "tier": None, "points": None, "status": None,
                             "icon_blob": None, "locked_icon_blob": None, "icon_licence": None,
                             "icon_attribution": None, "description": ""}
        for a in active:
            m["description"] = m["description"] or a.description
            m["hidden"] = m["hidden"] or a.hidden
            for field in ("kind", "tier", "points", "status"):
                if m[field] is None:
                    m[field] = getattr(a, field)
            if m["icon_blob"] is None and a.icon_blob_hash:
                m["icon_blob"] = a.icon_blob_hash
                pol = media_policy(ctx.settings, a.source)
                m["icon_licence"] = a.icon_licence or pol.licence or ("owner_upload" if media.is_upload(
                    ctx.settings, a.icon_url) else None)
                m["icon_attribution"] = a.icon_attribution or pol.attribution
            m["locked_icon_blob"] = m["locked_icon_blob"] or a.locked_icon_blob_hash
        lead = active[0]
        identities = {a.key: achievement_key.identity(a.source, a.external_id, a.norm_name) for a in members}
        keys = sorted(identities)
        shown = not m["hidden"] or reveal is True or lead.id in asked or not asked.isdisjoint(keys)
        cands = []
        stat_sources: set[str] = set()
        for a in active:
            cls, tr = rank[a.source]
            if a.global_percent is not None:
                cands.append((cls, a.global_percent, a.percent_as_of, tr, a.source))
            for src_name, pct, as_of in stats.get(a.external_id, []) if a.external_id else []:
                if src_name not in stat_sources:
                    stat_sources.add(src_name)
                    cands.append((CLASS_RANK[source_class(src_name)], pct, as_of, trust(src_name), src_name))
        rarity = _rarity(cands, today, max_age)
        status = "retired" if all(a.retired_at is not None for a in members) else m["status"]
        pos_row = next((a for a in active if a.source == pos_source.get(sid)), None)
        ord_row = next((a for a in active if a.source == order_source.get(sid)), None)
        entry: dict[str, Any] = {
            "id": lead.id, "key": keys[0], "keys": keys, "set": sid, "platforms": mset["platforms"],
            "hidden": m["hidden"], "revealed": bool(m["hidden"] and shown), "status": status,
            "position": pos_row.position if pos_row is not None else None,
            "kind": m["kind"], "tier": m["tier"], "points": m["points"],
            "global_percent": rarity["percent"] if rarity else None, "rarity": rarity,
            "sources": [a.source for a in members] if shown else []}
        if shown:
            ranked = sorted(active, key=lambda a: (-rank[a.source][0], -rank[a.source][1], a.source, a.id))
            served, text = _pick_lang(want, [texts.get(a.id, {}) for a in ranked])
            external_ids: dict[str, str] = {}
            for a in members:
                if a.external_id:
                    external_ids.setdefault(a.source, a.external_id)
            entry.update(name=text["name"] if text else lead.name,
                         description=text["description"] if text else m["description"],
                         lang=served or lead.text_locale, external_ids=external_ids,
                         key_identity=identities[keys[0]], icon_url=_media(m["icon_blob"]),
                         locked_icon_url=_media(m["locked_icon_blob"]), icon_licence=m["icon_licence"],
                         icon_attribution=m["icon_attribution"])
        else:
            # no position either: a curated position would leak the order of the real names (§12.1.3, §12.1.8)
            entry.update(name=HIDDEN_NAME, description="", lang=None, external_ids={}, key_identity=None,
                         icon_url=None, locked_icon_url=None, icon_licence=None, icon_attribution=None,
                         position=None)
        entry["_sort"] = (ord_row.source_order if ord_row is not None else None, lead.name)
        entries.append(entry)

    # set summaries (counts before the set/platform filters, which select inside them)
    summary: dict[str, dict[str, Any]] = {}
    record = session.get(Canonical, cid)
    title = str(((record.view if record is not None else None) or {}).get("title") or "")
    for e in entries:
        mset = next(s for s in sets.values() if s["id"] == e["set"])
        s = summary.setdefault(e["set"], {**{k: mset[k] for k in ("id", "key", "kind", "name", "dlc", "edition",
                                                                   "platforms", "sources")},
                                          "count": 0, "hidden_count": 0, "_position": mset["position"]})
        s["count"] += 1
        s["hidden_count"] += int(e["hidden"])
        if s["kind"] == "base" and not s["name"]:
            s["name"] = title
    set_order = sorted(summary.values(), key=lambda s: (s["_position"] is None, s["_position"] or 0,
                                                       SET_KIND_ORDER.get(s["kind"], 99), s["id"]))
    set_rank = {s["id"]: n for n, s in enumerate(set_order)}

    def sort_key(e: dict[str, Any]) -> tuple[Any, ...]:
        concealed = e["hidden"] and not e["revealed"]
        if concealed:  # never by name, position or rarity: that would leak the order of the real names
            return (set_rank[e["set"]], 1, e["key"])
        if order == "name":
            tail: tuple[Any, ...] = (e["name"].casefold(), e["key"])
        elif order == "source":
            src = e["_sort"][0]
            tail = (src is None, src or 0, e["name"].casefold(), e["key"])
        else:
            pct = e["global_percent"]
            tail = (pct is None, -(pct or 0), e["name"], e["key"])
        return (set_rank[e["set"]], 0, e["position"] is None, e["position"] or 0, *tail)

    entries.sort(key=sort_key)
    for e in entries:
        del e["_sort"]
    if set_filter:
        entries = [e for e in entries if e["set"] == set_filter or e["set"].split("/", 1)[1] == set_filter]
    if platform:
        entries = [e for e in entries if not e["platforms"] or platform in e["platforms"]]
    for s in set_order:
        del s["_position"]
    return {"sets": set_order, "results": entries, "lang": want,
            "revealed_any": any(e["revealed"] for e in entries)}


def merged_achievements(session: Session, ctx: Ctx, cid: str, *, reveal: bool | set[str] = False,
                        **options: Any) -> list[dict[str, Any]]:
    """The merged entries of a record (see `achievement_listing`); hidden entries are redacted unless revealed.
    Icons are only ever our own verified copies (`/api/v2/media/<sha256>`); the origin URL is never handed out."""
    return achievement_listing(session, ctx, cid, reveal=reveal, **options)["results"]


def sets_elsewhere(session: Session, ctx: Ctx, cid: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """DLC sets listed on other records that point at this one (§12.1.1): where a client finds the achievements
    of a DLC that its base game's source lists. Names of achievements are never part of it."""
    aliases = [cid, *session.scalars(select(RecordLive.id).where(RecordLive.live_id == cid).limit(200))]
    out, seen = [], set()
    for s in session.scalars(select(AchievementSet).where(AchievementSet.dlc_canonical_id.in_(aliases),
                                                          AchievementSet.canonical_id != cid)
                             .order_by(AchievementSet.canonical_id, AchievementSet.source, AchievementSet.set_key)
                             .limit(limit)):
        other = session.get(Canonical, s.canonical_id)
        if other is None or other.merged_into:
            continue
        pairs = {(s.source, s.set_key)}
        merged = _merged_sets(session, ctx, s.canonical_id, pairs)[(s.source, s.set_key)]
        if (s.canonical_id, merged["id"]) in seen:
            continue
        seen.add((s.canonical_id, merged["id"]))
        out.append({"canonical_id": s.canonical_id, "set": merged["id"], "name": s.name})
    return out


# ---- merges ----------------------------------------------------------------------------------------------

def move_facets(session: Session, src: str, dst: str, settings: Any = None) -> None:
    """On merge: carry artwork, achievements, achievement sets and stats over; duplicates keep the survivor's row.

    Moved achievement rows keep their id and key (the key leaves out the canonical id). A row whose source and
    identity (external id and name), or whose source and key, the survivor already has is a duplicate: the
    survivor's row stays. A row whose key the survivor holds under another source is kept and re-keyed with its
    derived key, never dropped. A set key the survivor already uses for that source gets a `-2`, `-3`... suffix
    (§12.1.1); the merge record keeps the original key so an unmerge restores it."""
    have_art = {(a.kind, a.url) for a in session.scalars(select(Artwork).where(Artwork.canonical_id == dst))}
    for a in session.scalars(select(Artwork).where(Artwork.canonical_id == src)).all():
        if (a.kind, a.url) in have_art:
            session.delete(a)
        else:
            a.canonical_id, a.preferred = dst, False  # the survivor keeps its own defaults
    survivor = list(session.scalars(select(Achievement).where(Achievement.canonical_id == dst)))
    have_ach = {(a.source, a.external_id, a.norm_name) for a in survivor}
    have_key = {a.key: a.source for a in survivor}
    have_sets = {(s.source, s.set_key) for s in session.scalars(select(AchievementSet).where(
        AchievementSet.canonical_id == dst))}
    renamed: dict[tuple[str, str], str] = {}
    for s in session.scalars(select(AchievementSet).where(AchievementSet.canonical_id == src)).all():
        key = s.set_key
        if (s.source, key) in have_sets:
            n = 2
            while (s.source, f"{key[:60]}-{n}") in have_sets:
                n += 1
            key = f"{key[:60]}-{n}"
            renamed[(s.source, s.set_key)] = key
        s.canonical_id, s.set_key = dst, key
        have_sets.add((s.source, key))
    for a in session.scalars(select(Achievement).where(Achievement.canonical_id == src)).all():
        if (a.source, a.external_id, a.norm_name) in have_ach or have_key.get(a.key) == a.source:
            session.delete(a)
            continue
        if a.key in have_key:  # another source's row holds the key: keep this row under its derived key
            a.key = _new_key(a.source, {"name": a.name, "external_id": a.external_id}, set(have_key))
        a.set_key = renamed.get((a.source, a.set_key), a.set_key)
        a.canonical_id = dst
        have_ach.add((a.source, a.external_id, a.norm_name))
        have_key[a.key] = a.source
    have_stats = {(s.source, s.external_id) for s in session.scalars(select(AchievementStat).where(
        AchievementStat.canonical_id == dst))}
    for s in session.scalars(select(AchievementStat).where(AchievementStat.canonical_id == src)).all():
        if (s.source, s.external_id) in have_stats:
            session.delete(s)
        else:
            s.canonical_id = dst
    session.flush()
    if settings is not None:
        dedupe(session, settings, dst)
