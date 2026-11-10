"""the per-type achievements flag (CONTRACT-C15 §2.7, A20)

Revision ID: 0060
Revises: 0037

Upgrade: `media_type.achievements` (boolean, NOT NULL, server default true). Every existing type, built-in or
custom, local or inherited, gets true, so records that show achievements today keep them, and a follower replaying an
authority's history from seq 0 applies the same `achievements` entries it applies today. An administrator may turn
the flag off per type; the built-in `book` (A21) is seeded with false.

Downgrade: the column is dropped. Achievement rows are never touched by either step: a type that had the flag off
shows its stored achievements again after a downgrade.

Numbering: 0060 is deliberately far from the live head (0037) because other programme lanes add migrations in
parallel; it is renumbered when the chain is restacked. `down_revision` is the head of this branch's base.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0060'
down_revision = '0037'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("media_type") as batch:
        batch.add_column(sa.Column("achievements", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("media_type") as batch:
        batch.drop_column("achievements")
