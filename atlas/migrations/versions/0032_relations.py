"""relationships between works: the `relation` table (CONTRACTS-v1 C6.3, protocol 1.4)

Revision ID: 0032
Revises: 0031

Creates `relation` as C6.3 defines it (directed `from_id -> to_id` between canonical records, a type from
`vocab.RELATION_TYPES`, the source, `redistributable`, `created_at`, `ended_at`, `CHECK (from_id <> to_id)` and
one *active* row per `(from_id, to_id, type)` through a partial unique index), plus the additive columns
recorded in CONTRACT-AMENDMENTS A15: `created_by`, `end_reason`, `origin_node`/`origin_seq` (the upstream feed
entry an applied relation came from) and `feed_seq` (this node's own `add` entry once published).

Existing data: no relation existed before this revision, so nothing is backfilled and no record changes. The
`series` and `collection` media types (the targets of `series_member` / `collection_member`) are seeded by the
application at start (`custom.seed`), like the other built-in types, not here.

Downgrade keeps the data: the table is renamed to `relation_0032_kept`, which the previous release does not
read (its merges and backups ignore it). Upgrading again renames it back. A relation whose record was merged
while the previous release ran (that release does not move relations) is ended with `end_reason = "downgraded"`
instead of being redirected, so no active relation names a merged record and no cycle or second parent can
appear; a curator sees it under "Ended relations" and can add it again. The `series` and `collection` media
types stay (the previous release lists them as ordinary types).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None

ACTIVE = sa.text("ended_at IS NULL")


KEPT = "relation_0032_kept"


def upgrade() -> None:
    bind = op.get_bind()
    if KEPT in sa.inspect(bind).get_table_names():
        _restore(bind)
        return
    op.create_table(
        "relation",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("from_id", sa.String(40), sa.ForeignKey("canonical.id"), nullable=False),
        sa.Column("to_id", sa.String(40), sa.ForeignKey("canonical.id"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("redistributable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("end_reason", sa.String(16), nullable=True),
        sa.Column("origin_node", sa.String(64), nullable=True),
        sa.Column("origin_seq", sa.BigInteger(), nullable=True),
        sa.Column("feed_seq", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("from_id <> to_id", name="ck_relation_not_self"),
    )
    op.create_index("uq_relation_active", "relation", ["from_id", "to_id", "type"], unique=True,
                    sqlite_where=ACTIVE, postgresql_where=ACTIVE)
    op.create_index("ix_relation_from", "relation", ["from_id", "ended_at"])
    op.create_index("ix_relation_to", "relation", ["to_id", "ended_at"])
    op.create_index("ix_relation_origin", "relation", ["origin_node", "origin_seq"])


def _restore(bind) -> None:
    """Re-upgrade after a downgrade: bring the kept table back (its indexes and constraint moved with it) and end
    the active relations that name a record merged in the meantime."""
    op.rename_table(KEPT, "relation")
    canonical = sa.table("canonical", sa.column("id", sa.String), sa.column("merged_into", sa.String))
    relation = sa.table("relation", sa.column("id", sa.String), sa.column("from_id", sa.String),
                        sa.column("to_id", sa.String), sa.column("ended_at", sa.DateTime(timezone=True)),
                        sa.column("end_reason", sa.String))
    merged = sa.select(canonical.c.id).where(canonical.c.merged_into.is_not(None))
    bind.execute(relation.update()
                 .where(relation.c.ended_at.is_(None),
                        relation.c.from_id.in_(merged) | relation.c.to_id.in_(merged))
                 .values(ended_at=sa.func.current_timestamp(), end_reason="downgraded"))


def downgrade() -> None:
    op.rename_table("relation", KEPT)  # kept for a re-upgrade; the previous release never reads it
