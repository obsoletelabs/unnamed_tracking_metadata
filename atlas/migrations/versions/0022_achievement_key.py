"""stable achievement key (ak1) stored on every achievement row

Revision ID: 0022
Revises: 0021

Achievement rows used to be deleted and re-inserted on every refresh, so their ids changed each time.
Rows are now reconciled in place, and each one carries a stored wire key (CONTRACTS-v1 C2.1/C2.2):
"ak1:" + the first 26 lower-case base32 characters of sha256(identity), where identity is
"<source>:<external_id>", or "<source>~<norm_name>" when the source gave no id. The key leaves out the
canonical id, so merges and detaches keep it.

Upgrade adds the column, backfills every existing row in Python, makes it NOT NULL and adds the unique
index uq_achievement_key (canonical_id, key). If one record holds two rows of the same source with the
same external id (possible: names differ), the second takes the name form of the identity; a further
clash (never seen, kept for safety) appends "#2", "#3", ... to the identity before hashing. Nothing is
deleted or rewritten except the new column.

Downgrade drops the index and the column. Rows, ids and every other column are untouched. The previous
release goes back to re-creating rows (new ids on each refresh). Upgrading again re-derives the keys,
which gives the same keys except for entries renamed through the editor after this release stored them.
"""
import base64
import hashlib
import re

import sqlalchemy as sa
from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None

BATCH = 1000


# Frozen copies of atlas.achievement_key / atlas.normalize as of this revision (a migration must not change
# meaning when application code moves on).
def _ns(source: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "-", (source or "").strip().casefold())[:50] or "unknown"


def _ak1(ident: str) -> str:
    return "ak1:" + base64.b32encode(hashlib.sha256(ident.encode("utf-8")).digest()).decode("ascii").lower()[:26]


def backfill(bind) -> int:
    """Give every row without a key its derived key. Returns the number of rows written."""
    rows = bind.execute(sa.text(
        "SELECT id, canonical_id, source, external_id, norm_name, key FROM achievement "
        "ORDER BY canonical_id, source, id")).all()
    used: dict[str, set[str]] = {}
    for r in rows:
        if r.key:
            used.setdefault(r.canonical_id, set()).add(r.key)
    updates = []
    for r in rows:
        if r.key:
            continue
        taken = used.setdefault(r.canonical_id, set())
        ns = _ns(r.source)
        candidates = [f"{ns}:{r.external_id}"] if r.external_id else []
        candidates.append(f"{ns}~{r.norm_name}")
        key = next((k for k in map(_ak1, candidates) if k not in taken), None)
        n = 2
        while key is None or key in taken:
            key, n = _ak1(f"{candidates[-1]}#{n}"), n + 1
        taken.add(key)
        updates.append({"i": r.id, "k": key})
    stmt = sa.text("UPDATE achievement SET key = :k WHERE id = :i")
    for start in range(0, len(updates), BATCH):
        bind.execute(stmt, updates[start:start + BATCH])
    return len(updates)


def upgrade() -> None:
    with op.batch_alter_table('achievement', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key', sa.String(length=30), nullable=True))
    backfill(op.get_bind())
    with op.batch_alter_table('achievement', schema=None) as batch_op:
        batch_op.alter_column('key', existing_type=sa.String(length=30), nullable=False)
        batch_op.create_index('uq_achievement_key', ['canonical_id', 'key'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('achievement', schema=None) as batch_op:
        batch_op.drop_index('uq_achievement_key')
        batch_op.drop_column('key')
