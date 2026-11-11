"""super group: user writes replicated across the owner group, their state and conflicts (CONTRACT-C14 slice C14-4)

Revision ID: 0041
Revises: 0040

Upgrade:
* New table `group_write`: every signed user write (`group.write`) made on this node or received from a member,
  in this node's receive order (`id`), unique by `op_id`. Empty.
* New table `group_write_state`: the last-writer-wins state of each subject. Empty.
* New table `group_write_conflict`: real overwrites of concurrent writes, for administrators. Empty.
* Nothing existing changes: the super group is off (`group_super`) until an administrator turns it on or applies
  the "Same owner, full trust" preset.

Downgrade:
* Refused while observations written from replicated writes exist (`source = 'group-write'`), unless
  `ATLAS_DOWNGRADE_FORCE=1`: the previous release does not know that source and would rank those rows as provider
  data. With the flag those observations are dropped (each record keeps its other observations), a record first
  created from a replicated write keeps its `group` ceiling under `scope_ceiling_by = 'operator'` (never public),
  `node_state["view_rebuild_pending"]` asks the application to re-derive every view once the schema is current
  again (as 0031 and 0034 do), and the three tables and the `super_group_*` node-state keys are removed.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = '0041'
down_revision = '0040'
branch_labels = None
depends_on = None

REBUILD_KEY = "view_rebuild_pending"


def upgrade() -> None:
    op.create_table(
        "group_write",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("op_id", sa.String(96), nullable=False, unique=True),
        sa.Column("origin_node", sa.String(96), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("hlc", sa.String(96), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("author_user_id", sa.String(26), nullable=True),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_group_write_origin_node", "group_write", ["origin_node"])
    op.create_index("ix_group_write_subject", "group_write", ["subject"])
    op.create_index("ix_group_write_author_user_id", "group_write", ["author_user_id"])
    op.create_table(
        "group_write_state",
        sa.Column("subject", sa.String(200), primary_key=True),
        sa.Column("op_id", sa.String(96), nullable=False),
        sa.Column("hlc", sa.String(96), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("waiting", sa.String(24), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tried_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_group_write_state_waiting", "group_write_state", ["waiting"])
    op.create_table(
        "group_write_conflict",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("winner_op", sa.String(96), nullable=False),
        sa.Column("loser_op", sa.String(96), nullable=False),
        sa.Column("winner_node", sa.String(96), nullable=False),
        sa.Column("loser_node", sa.String(96), nullable=False),
        sa.Column("winner_hlc", sa.String(96), nullable=False),
        sa.Column("loser_hlc", sa.String(96), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_by", sa.String(26), nullable=True),
    )
    op.create_index("ix_group_write_conflict_subject", "group_write_conflict", ["subject"])


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text("SELECT COUNT(*) FROM observation WHERE source = 'group-write'")).scalar() or 0
    if used and os.environ.get("ATLAS_DOWNGRADE_FORCE") != "1":
        raise RuntimeError(
            f"{used} observation(s) hold edits replicated from your other nodes (super group). The previous release "
            "does not know them and would rank them as provider data. Turn `group_super` off and set "
            "ATLAS_DOWNGRADE_FORCE=1 to drop them.")
    bind.execute(sa.text("DELETE FROM observation WHERE source = 'group-write'"))
    # A record first created here from a replicated write keeps its `group` ceiling (never public, never upstream),
    # now under the operator's name, which the previous release knows; it is not deleted (other rows may refer to it).
    bind.execute(sa.text("UPDATE canonical SET scope_ceiling_by = 'operator' WHERE scope_ceiling_by = 'write'"))
    # Every view is re-derived without the dropped observations once the schema is current again
    # (`atlas.dates_rebuild.finish_pending`, the same note 0031 and 0034 leave).
    state = sa.table("node_state", sa.column("key", sa.String), sa.column("value", sa.Text))
    bind.execute(state.delete().where(state.c.key == REBUILD_KEY))
    bind.execute(state.insert().values(key=REBUILD_KEY, value="pending"))
    op.drop_index("ix_group_write_conflict_subject", table_name="group_write_conflict")
    op.drop_table("group_write_conflict")
    op.drop_index("ix_group_write_state_waiting", table_name="group_write_state")
    op.drop_table("group_write_state")
    op.drop_index("ix_group_write_author_user_id", table_name="group_write")
    op.drop_index("ix_group_write_subject", table_name="group_write")
    op.drop_index("ix_group_write_origin_node", table_name="group_write")
    op.drop_table("group_write")
    bind.execute(sa.text("DELETE FROM node_state WHERE key LIKE 'super_group_%'"))
