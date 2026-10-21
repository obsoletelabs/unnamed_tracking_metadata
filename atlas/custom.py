"""Media types and admin-defined fields: schema, validated values, trust-based merge, replication."""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import feed
from .models import Canonical, CustomValue, FieldDef, MediaType, Setting
from .service import AtlasError, Ctx, write_lock

KINDS = ("text", "longtext", "number", "date", "bool", "choice", "list", "url")
CORE_LABELS = ("developer", "publisher", "series", "release_date", "genres", "themes", "tags", "features", "platforms", "languages",  # noqa: E501
               "aliases", "age_rating", "website", "critic_score", "user_score")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}\Z")
_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")
MAX_FIELDS_PER_TYPE = 40
RESERVED = {"title", "description", "id", "sources", "provider_ids", "media_type", "custom", "version", "artwork",
            "achievements", "updated_at", "redirected_from", *CORE_LABELS}


def seed(session: Session) -> None:
    if session.get(MediaType, "game") is None:
        session.add(MediaType(key="game", label="Game", position=0, origin="local"))
        session.flush()


# Standard multiplayer fields every game node offers (issue #39). Added once per node; an administrator who
# deletes or renames one is not overridden on the next start. Mirrors receive them from their upstream.
MULTIPLAYER_FIELDS = (
    ("players_min", "Minimum players", "number", None, "Fewest players needed to play (1 for single-player)."),
    ("players_max", "Maximum players", "number", None, "Most players in one session, local or online."),
    ("crossplay", "Crossplay", "choice", ["yes", "no", "partial"],
     "Can players on different platforms play together? 'partial' means only some platforms or modes."),
)
_MULTIPLAYER_SEEDED = "defaults.multiplayer_fields"


def seed_multiplayer(session: Session, ctx: Ctx) -> None:
    if ctx.settings.role == "mirror" or session.get(Setting, _MULTIPLAYER_SEEDED) is not None:
        return
    if session.get(MediaType, "game") is None:
        return
    for position, (key, label, kind, choices, help_text) in enumerate(MULTIPLAYER_FIELDS):
        exists = session.scalars(select(FieldDef.id).where(FieldDef.media_type == "game", FieldDef.key == key)).first()
        if exists is None:
            # Written directly, not through save_field: seeding must not append to the change feed. Followers
            # carry identical definitions of their own, and the next schema change publishes them with the rest.
            c = check_field("game", key, label, kind, choices, help_text, True, True, 50 + position)
            session.add(FieldDef(media_type="game", origin="local", key=c["key"], label=c["label"], kind=c["kind"],
                                 choices=c["choices"], help=c["help"], public=True, editable=True, position=c["position"]))
    session.add(Setting(key=_MULTIPLAYER_SEEDED, value=True))
    session.flush()


# ---- schema administration ---------------------------------------------------------------------------

def snapshot(session: Session) -> dict[str, Any]:
    types = [{"key": t.key, "label": t.label, "enabled": t.enabled, "position": t.position, "labels": t.labels or {}}
             for t in session.scalars(select(MediaType).where(MediaType.origin == "local"))]
    fields = [{"media_type": f.media_type, "key": f.key, "label": f.label, "kind": f.kind, "choices": f.choices or [],
               "help": f.help, "public": f.public, "editable": f.editable, "position": f.position}
              for f in session.scalars(select(FieldDef).where(FieldDef.origin == "local"))]
    return {"types": types, "fields": fields}


def publish(session: Session, ctx: Ctx) -> None:
    """Replicate the current schema to mirrors (authority only)."""
    if ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "schema", snapshot(session))


def apply_snapshot(session: Session, snap: dict[str, Any], node_id: str | None = None) -> None:
    """Mirror side: make the upstream-origin definitions equal to the snapshot; local ones always win.

    Every definition goes through the same validation as one typed into the admin UI; a bad entry is skipped (a
    misbehaving upstream must not be able to stall replication or plant markup in labels)."""
    from .custom_compat import legacy_snapshot

    snap = legacy_snapshot(session, snap)
    keep_t: set[str] = set()
    for t in snap.get("types", []) if isinstance(snap.get("types"), list) else []:
        try:
            key, label, enabled, position, labels = check_type(t.get("key"), t.get("label"), t.get("enabled", True),
                                                               t.get("position", 100), t.get("labels"))
        except (AtlasError, AttributeError):
            continue
        existing = session.get(MediaType, key)
        if existing is not None and existing.origin == "local":
            continue
        if existing is not None and node_id and existing.origin_node not in (None, node_id):
            continue  # another authority owns this shared schema name
        if existing is None:
            existing = MediaType(key=key, origin="upstream", origin_node=node_id)
            session.add(existing)
        elif node_id:
            existing.origin_node = node_id
        existing.label, existing.enabled, existing.position, existing.labels = label, enabled, position, labels
        keep_t.add(key)
    session.flush()
    for t in session.scalars(select(MediaType).where(MediaType.origin == "upstream")).all():
        if node_id and t.origin_node != node_id:
            continue
        if t.key in keep_t:
            continue
        if session.scalars(select(Canonical.id).where(Canonical.media_type == t.key).limit(1)).first():
            t.enabled = False  # records still use it: keep the type, just stop offering it
        else:
            session.delete(t)
    keep_f: set[tuple[str, str]] = set()
    for f in snap.get("fields", []) if isinstance(snap.get("fields"), list) else []:
        try:
            c = check_field(f.get("media_type"), f.get("key"), f.get("label"), f.get("kind"), f.get("choices"),
                            f.get("help"), f.get("public", True), f.get("editable", True), f.get("position", 100))
        except (AtlasError, AttributeError):
            continue
        if session.get(MediaType, c["media_type"]) is None:
            continue
        row = session.scalars(select(FieldDef).where(FieldDef.media_type == c["media_type"],
                                                     FieldDef.key == c["key"])).first()
        if row is not None and row.origin == "local":
            continue
        if row is not None and node_id and row.origin_node not in (None, node_id):
            continue
        if row is None:
            row = FieldDef(media_type=c["media_type"], key=c["key"], origin="upstream", origin_node=node_id,
                           kind=c["kind"], label="")
            session.add(row)
        elif node_id:
            row.origin_node = node_id
        row.label, row.kind, row.choices, row.help = c["label"], c["kind"], c["choices"], c["help"]
        row.public, row.editable, row.position = c["public"], c["editable"], c["position"]
        keep_f.add((c["media_type"], c["key"]))
    for row in session.scalars(select(FieldDef).where(FieldDef.origin == "upstream")).all():
        if node_id and row.origin_node != node_id:
            continue
        if (row.media_type, row.key) not in keep_f:
            session.delete(row)
    session.flush()


def _line(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        raise AtlasError("text expected")
    cleaned = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    if len(cleaned) > limit:
        raise AtlasError(f"at most {limit} characters")
    return cleaned


def check_type(key: Any, label: Any, enabled: Any, position: Any, labels: Any) -> tuple[str, str, bool, int, dict[str, str]]:
    """The one set of rules for a media type, used by the admin UI and by replicated definitions alike."""
    key = _line(key, 32).lower()
    if not _TYPE.match(key):
        raise AtlasError("media type keys use a-z, 0-9 and '_' and start with a letter (max 32)")
    label = _line(label, 60)
    if not label:
        raise AtlasError("a label of up to 60 characters is required")
    clean_labels = {}
    for k, v in (labels if isinstance(labels, dict) else {}).items():
        if k in CORE_LABELS and isinstance(v, str) and v.strip():
            clean_labels[k] = _line(v, 40)
    try:
        pos = max(0, min(int(position), 10000))
    except (TypeError, ValueError):
        pos = 100
    return key, label, bool(enabled), pos, clean_labels


def check_field(media_type: Any, key: Any, label: Any, kind: Any, choices: Any, help: Any, public: Any, editable: Any,
                position: Any) -> dict[str, Any]:
    key = _line(key, 40).lower()
    if not _KEY.match(key) or key in RESERVED:
        raise AtlasError("field keys use a-z, 0-9 and '_', start with a letter and may not reuse a built-in name")
    if kind not in KINDS:
        raise AtlasError(f"kind must be one of {', '.join(KINDS)}")
    label = _line(label, 80)
    if not label:
        raise AtlasError("a label of up to 80 characters is required")
    options = clean_choices(choices)
    if kind == "choice" and len(options) < 2:
        raise AtlasError("a choice field needs at least two options")
    try:
        pos = max(0, min(int(position), 10000))
    except (TypeError, ValueError):
        pos = 100
    return {"media_type": media_type, "key": key, "label": label, "kind": kind,
            "choices": options if kind == "choice" else [], "help": _line(help or "", 300), "public": bool(public),
            "editable": bool(editable), "position": pos}


def save_type(session: Session, ctx: Ctx, key: str, label: str, *, enabled: bool = True, position: int = 100,
              labels: dict[str, str] | None = None) -> MediaType:
    key, label, enabled, position, labels = check_type(key, label, enabled, position, labels)
    if key == "game":
        enabled = True  # the built-in type is always available
    write_lock(session)
    row = session.get(MediaType, key)
    if row is not None and row.origin != "local":
        raise AtlasError("this media type is managed by the upstream authority", "conflict")
    if row is None:
        row = MediaType(key=key, origin="local")
        session.add(row)
    row.label, row.enabled, row.position, row.labels = label, enabled, position, labels
    session.flush()
    publish(session, ctx)
    return row


def delete_type(session: Session, ctx: Ctx, key: str) -> None:
    write_lock(session)
    row = session.get(MediaType, key)
    if row is None or row.origin != "local":
        raise AtlasError("unknown media type", "not_found")
    if key == "game":
        raise AtlasError("the game type cannot be removed", "conflict")
    if session.scalars(select(Canonical.id).where(Canonical.media_type == key).limit(1)).first():
        raise AtlasError("records of this type exist; disable the type instead", "conflict")
    session.execute(delete(FieldDef).where(FieldDef.media_type == key))
    session.delete(row)
    session.flush()
    publish(session, ctx)


def clean_choices(raw: Any) -> list[str]:
    items = raw.splitlines() if isinstance(raw, str) else list(raw or [])
    out = []
    for item in items:
        item = str(item).strip()
        if item and item not in out:
            out.append(item)
    if any(len(i) > 60 for i in out) or len(out) > 100:
        raise AtlasError("choices: at most 100 options of up to 60 characters")
    return out


def save_field(session: Session, ctx: Ctx, media_type: str, key: str, label: str, kind: str, *, choices: Any = None,
               help: str = "", public: bool = True, editable: bool = True, position: int = 100) -> FieldDef:
    c = check_field(media_type, key, label, kind, choices, help, public, editable, position)
    key, label, options, help, public, editable, position = (
        c["key"], c["label"], c["choices"], c["help"], c["public"], c["editable"], c["position"])
    write_lock(session)
    mt = session.get(MediaType, media_type)
    if mt is None:
        raise AtlasError("unknown media type", "not_found")
    row = session.scalars(select(FieldDef).where(FieldDef.media_type == media_type, FieldDef.key == key)).first()
    if row is not None and row.origin != "local":
        raise AtlasError("this field is managed by the upstream authority", "conflict")
    if row is None:
        count = len(session.scalars(select(FieldDef.id).where(FieldDef.media_type == media_type)).all())
        if count >= MAX_FIELDS_PER_TYPE:
            raise AtlasError(f"at most {MAX_FIELDS_PER_TYPE} custom fields per media type")
        row = FieldDef(media_type=media_type, key=key, origin="local", kind=kind, label=label)
        session.add(row)
    elif row.kind != kind:
        raise AtlasError("a field's kind cannot change; create a new field instead", "conflict")
    row.label, row.choices, row.help = label, options, help
    row.public, row.editable, row.position = public, editable, position
    session.flush()
    publish(session, ctx)
    return row


def delete_field(session: Session, ctx: Ctx, media_type: str, key: str) -> None:
    write_lock(session)
    row = session.scalars(select(FieldDef).where(FieldDef.media_type == media_type, FieldDef.key == key)).first()
    if row is None or row.origin != "local":
        raise AtlasError("unknown field", "not_found")
    ids = session.scalars(select(Canonical.id).where(Canonical.media_type == media_type)).all()
    if ids:
        session.execute(delete(CustomValue).where(CustomValue.key == key, CustomValue.canonical_id.in_(ids)))
    session.delete(row)
    session.flush()
    publish(session, ctx)


def fields_for(session: Session, media_type: str, *, editable: bool | None = None,
               public: bool | None = None) -> list[FieldDef]:
    stmt = select(FieldDef).where(FieldDef.media_type == media_type).order_by(FieldDef.position, FieldDef.key)
    rows = list(session.scalars(stmt))
    if editable is not None:
        rows = [r for r in rows if r.editable == editable]
    if public is not None:
        rows = [r for r in rows if r.public == public]
    return rows


def types(session: Session, *, enabled_only: bool = False) -> list[MediaType]:
    rows = list(session.scalars(select(MediaType).order_by(MediaType.position, MediaType.key)))
    return [t for t in rows if t.enabled] if enabled_only else rows


# ---- values -------------------------------------------------------------------------------------------

def validate_value(f: FieldDef, raw: Any) -> Any:
    """Typed, bounded JSON for a field value. Empty means 'nothing to say' (returns None)."""
    if raw is None or (isinstance(raw, (str, list)) and not raw):
        return None
    if f.kind in ("text", "longtext"):
        if not isinstance(raw, str):
            raise AtlasError(f"{f.label}: text expected")
        text = raw.strip()
        limit = 500 if f.kind == "text" else 20000
        if len(text) > limit or (f.kind == "text" and any(ord(c) < 32 for c in text)):
            raise AtlasError(f"{f.label}: at most {limit} characters{', on one line' if f.kind == 'text' else ''}")
        return text or None
    if f.kind == "number":
        try:
            n = float(str(raw).strip()) if not isinstance(raw, (int, float)) or isinstance(raw, bool) else float(raw)
        except ValueError as exc:
            raise AtlasError(f"{f.label}: a number is required") from exc
        if not math.isfinite(n) or abs(n) > 1e12:
            raise AtlasError(f"{f.label}: number out of range")
        return int(n) if n == int(n) else n
    if f.kind == "date":
        try:
            if not isinstance(raw, str):
                raise ValueError
            date.fromisoformat(raw.strip())
        except ValueError as exc:
            raise AtlasError(f"{f.label}: use YYYY-MM-DD") from exc
        return raw.strip()
    if f.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in ("true", "yes", "on", "1"):
            return True
        if str(raw).strip().lower() in ("false", "no", "off", "0"):
            return False
        raise AtlasError(f"{f.label}: yes or no")
    if f.kind == "choice":
        if raw not in (f.choices or []):
            raise AtlasError(f"{f.label}: choose one of {', '.join(f.choices or [])}")
        return raw
    if f.kind == "list":
        items = raw.splitlines() if isinstance(raw, str) else raw
        if not isinstance(items, list) or len(items) > 50 or not all(isinstance(i, str) for i in items):
            raise AtlasError(f"{f.label}: a list of up to 50 short items")
        cleaned = [i.strip() for i in items if i.strip()]
        if any(len(i) > 128 for i in cleaned):
            raise AtlasError(f"{f.label}: each item is at most 128 characters")
        return cleaned or None
    if f.kind == "url":
        text = str(raw).strip()
        if not text.startswith("https://") or len(text) > 1000 or any(c.isspace() or ord(c) < 32 for c in text):
            raise AtlasError(f"{f.label}: use an https:// address")
        return text
    raise AtlasError("unknown field kind")  # pragma: no cover


def clean_values(session: Session, media_type: str, values: dict[str, Any], *, editable_only: bool) -> dict[str, Any]:
    """Validate a {key: raw} mapping; unknown or non-editable keys are errors, empty values are dropped."""
    from .custom_compat import key_for

    defs = {f.key: f for f in fields_for(session, media_type)}
    out: dict[str, Any] = {}
    seen: set[str] = set()
    for key, raw in (values or {}).items():
        key = key_for(session, media_type, key)
        if key in seen:
            raise AtlasError(f"custom field {key!r} was supplied twice through an old and new name")
        seen.add(key)
        f = defs.get(key)
        if f is None:
            raise AtlasError(f"unknown field {key!r} for {media_type}")
        if editable_only and not f.editable:
            raise AtlasError(f"{f.label} cannot be edited by contributors")
        value = validate_value(f, raw)
        if value is not None:
            out[key] = value
    return out


def set_values(session: Session, ctx: Ctx, cid: str, source: str, values: dict[str, Any], *,
               redistributable: bool = False, log: bool = True, clear: tuple[str, ...] = ()) -> None:
    """Upsert validated values for one source; `clear` removes that source's value for the given keys."""
    write_lock(session)
    row = session.get(Canonical, cid)
    if row is None or row.merged_into:
        raise AtlasError("unknown or merged canonical", "not_found")
    for key, value in values.items():
        cv = session.get(CustomValue, (cid, key, source))
        if cv is None:
            session.add(CustomValue(canonical_id=cid, key=key, source=source, value=value,
                                    redistributable=redistributable))
        else:
            cv.value, cv.redistributable = value, redistributable
    for key in clear:
        cv = session.get(CustomValue, (cid, key, source))
        if cv is not None:
            session.delete(cv)
    session.flush()
    if log and redistributable and ctx.settings.role == "authority":
        feed.append(session, ctx.identity, "custom", {"canonical_id": cid, "source": source, "values": values,
                                                      "clear": list(clear)})


def source_values(session: Session, cid: str, source: str) -> dict[str, Any]:
    return {cv.key: cv.value for cv in session.scalars(
        select(CustomValue).where(CustomValue.canonical_id == cid, CustomValue.source == source))}


def merged(session: Session, ctx: Ctx, cid: str, *, public_only: bool = True) -> dict[str, Any]:
    """Per key, the value from the most trusted source. Fields without a definition are not shown."""
    row = session.get(Canonical, cid)
    if row is None:
        return {}
    defs = {f.key: f for f in fields_for(session, row.media_type)}
    best: dict[str, tuple[int, Any]] = {}
    for cv in session.scalars(select(CustomValue).where(CustomValue.canonical_id == cid)):
        f = defs.get(cv.key)
        if f is None or (public_only and not f.public):
            continue
        trust = ctx.settings.trust(cv.source)
        if cv.key not in best or trust > best[cv.key][0]:
            best[cv.key] = (trust, cv.value)
    return {k: v for k, (_, v) in best.items()}


def move_values(session: Session, src: str, dst: str) -> None:
    have = {(cv.key, cv.source) for cv in session.scalars(select(CustomValue).where(CustomValue.canonical_id == dst))}
    for cv in session.scalars(select(CustomValue).where(CustomValue.canonical_id == src)).all():
        if (cv.key, cv.source) in have:
            session.delete(cv)
        else:
            cv.canonical_id = dst
    session.flush()
