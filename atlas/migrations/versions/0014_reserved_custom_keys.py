"""Preserve custom fields whose names became core fields.

Revision ID: 0014
Revises: 0013
"""

import json
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# Frozen here: future application schema changes must not change this migration.
RESERVED = {"age_rating", "genres", "themes", "languages", "aliases", "website", "critic_score", "user_score"}


def upgrade() -> None:
    conn = op.get_bind()
    metadata = sa.MetaData()
    fields = sa.Table("field_def", metadata, autoload_with=conn)
    values = sa.Table("custom_value", metadata, autoload_with=conn)
    games = sa.Table("canonical", metadata, autoload_with=conn)
    proposals = sa.Table("proposal", metadata, autoload_with=conn)
    state = sa.Table("node_state", metadata, autoload_with=conn)
    audit = sa.Table("audit_entry", metadata, autoload_with=conn)
    rows = conn.execute(sa.select(fields).order_by(fields.c.media_type, fields.c.key)).mappings().all()
    occupied = {}
    for row in rows:
        occupied.setdefault(row["media_type"], set()).add(row["key"])
    # Orphan values are still data; never overwrite them when choosing a new key.
    for kind, key in conn.execute(sa.select(games.c.media_type, values.c.key)
                                 .join(values, values.c.canonical_id == games.c.id).distinct()):
        occupied.setdefault(kind, set()).add(key)
    aliases = []
    for row in rows:
        old, kind = row["key"], row["media_type"]
        if old not in RESERVED:
            continue
        base, suffix = f"custom_{old}", 1
        new = base
        while new in occupied[kind]:
            suffix += 1
            new = f"{base}_{suffix}"
        occupied[kind].add(new)
        conn.execute(fields.update().where(fields.c.id == row["id"]).values(key=new))
        ids = sa.select(games.c.id).where(games.c.media_type == kind)
        conn.execute(values.update().where(values.c.canonical_id.in_(ids), values.c.key == old).values(key=new))
        aliases.append({"media_type": kind, "old": old, "new": new})
        conn.execute(audit.insert().values(id=uuid.uuid4().hex[:26], actor_id=None, action="custom_field_renamed",
                                          target=f"{kind}:{new}", detail={"old": old, "new": new},
                                          created_at=datetime.now(UTC)))
    if not aliases:
        return
    for row in conn.execute(sa.select(proposals)).mappings():
        payload = dict(row["payload"] or {})
        kind = payload.get("media_type", "game")
        if row["canonical_id"]:
            kind = conn.execute(sa.select(games.c.media_type).where(games.c.id == row["canonical_id"])).scalar() or kind
        previous = dict(row["previous"] or {})
        changed = False
        for mapping in aliases:
            if mapping["media_type"] != kind:
                continue
            for document in (payload, previous):
                custom = document.get("custom")
                if isinstance(custom, dict) and mapping["old"] in custom:
                    copied = dict(custom)
                    if mapping["new"] in copied:
                        raise RuntimeError("Custom proposal key conflict; repair explicitly before upgrading")
                    copied[mapping["new"]] = copied.pop(mapping["old"])
                    document["custom"] = copied
                    changed = True
        if changed:
            conn.execute(proposals.update().where(proposals.c.id == row["id"])
                         .values(payload=payload, previous=previous if row["previous"] is not None else None))
    conn.execute(state.insert().values(key="custom_field_renames", value=json.dumps(aliases, sort_keys=True)))


def downgrade() -> None:
    # Names may already be stored by clients and in new signed entries. Keep the additive aliases and data;
    # older code also accepts custom_* keys. Do not rewrite history or reintroduce the collision.
    pass
