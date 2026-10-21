"""Lossless aliases for custom keys reserved by the expanded core schema."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import FieldDef, NodeState

STATE_KEY = "custom_field_renames"
LEGACY_KEYS = frozenset({"age_rating", "genres", "themes", "languages", "aliases", "website", "critic_score", "user_score"})


def renames(session: Session) -> list[dict[str, str]]:
    row = session.get(NodeState, STATE_KEY)
    return json.loads(row.value) if row else []


def key_for(session: Session, media_type: str, key: str) -> str:
    return next((r["new"] for r in renames(session) if r["media_type"] == media_type and r["old"] == key), key)


def legacy_snapshot(session: Session, snapshot: dict) -> dict:
    """Read historical signed schema without modifying its original body or signature."""
    fields = snapshot.get("fields")
    if not isinstance(fields, list):
        return snapshot
    aliases = renames(session)
    occupied: dict[str, set[str]] = {}
    for row in session.scalars(select(FieldDef)):
        occupied.setdefault(row.media_type, set()).add(row.key)
    for field in fields:
        if isinstance(field, dict) and isinstance(field.get("media_type"), str) and isinstance(field.get("key"), str):
            occupied.setdefault(field["media_type"], set()).add(field["key"])
    changed = []
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("key"), str) or field["key"] not in LEGACY_KEYS:
            changed.append(field)
            continue
        media_type, old = field.get("media_type"), field["key"]
        if not isinstance(media_type, str) or len(media_type) > 32:
            changed.append(field)
            continue
        new = next((r["new"] for r in aliases if r["media_type"] == media_type and r["old"] == old), None)
        if new is None:
            base, suffix = f"custom_{old}", 1
            new = base
            while new in occupied[media_type]:
                suffix += 1
                new = f"{base}_{suffix}"
            aliases.append({"media_type": media_type, "old": old, "new": new})
            occupied[media_type].add(new)
        changed.append({**field, "key": new})
    if aliases:
        row = session.get(NodeState, STATE_KEY)
        if row is None:
            session.add(NodeState(key=STATE_KEY, value=json.dumps(aliases, sort_keys=True)))
        else:
            row.value = json.dumps(aliases, sort_keys=True)
        session.flush()
    return {**snapshot, "fields": changed}
