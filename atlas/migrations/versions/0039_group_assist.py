"""group assist: evidence of answers from the owner group, lookup ops, shared contribution state (CONTRACT-C14 §9)

Revision ID: 0039
Revises: 0038

Upgrade:
* New table `group_assist_item` (C14 §3.4/§3.5): the helper-signed bundles, carried feed entries, vouches, wakes,
  offers, merge hints and lookup attestations this node received from its other nodes. Empty.
* `observation.assist_item_id` (String(40), NULL): the item an observation was written from. NULL for every
  existing row.
* New table `assist_lookup_op` (C14 §4.3 step 8, Review #1 C14-9): on a helper, the provider lookups askers
  requested, by stable operation id, kept 15 minutes. Empty.
* New table `group_contrib_state` (owner decision H67, §7.7): contribution states shared inside the owner group,
  PK `(origin_node, contribution_id)`. Empty.
* The per-group backfill cursors of §3.1 (`node_state["assist_backfill:<group_id>"]`) start at the beginning: rows
  that were already waiting before this release get asked over successive group rounds (Review #1 C14-14).

Downgrade:
* Refused while any observation was written from a group answer, unless `ATLAS_DOWNGRADE_FORCE=1`: the previous
  release cannot tell those rows apart (they would count as this node's own data). With the flag the column and
  the evidence are dropped; the observations stay (their records keep the `group` ceiling of 0038).
* `assist_lookup_op` and `group_contrib_state` are dropped; the `assist_*` node-state keys are removed.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = '0039'
down_revision = '0038'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_assist_item",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("helper", sa.String(64), nullable=False),
        sa.Column("cid", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("body", sa.JSON(), nullable=True),
        sa.Column("envelope", sa.JSON(), nullable=True),
        sa.Column("signed_entries", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_group_assist_item_cid", "group_assist_item", ["cid", "superseded_at"])
    op.create_index("ix_group_assist_item_helper", "group_assist_item", ["helper", "kind"])
    with op.batch_alter_table("observation") as batch:
        batch.add_column(sa.Column("assist_item_id", sa.String(40), nullable=True))
    op.create_table(
        "assist_lookup_op",
        sa.Column("asker", sa.String(64), primary_key=True),
        sa.Column("op_id", sa.String(26), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "group_contrib_state",
        sa.Column("origin_node", sa.String(64), primary_key=True),
        sa.Column("contribution_id", sa.String(64), primary_key=True),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.get_bind().execute(sa.text("DELETE FROM node_state WHERE key LIKE 'assist_backfill:%'"))


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text("SELECT COUNT(*) FROM observation WHERE assist_item_id IS NOT NULL")).scalar() or 0
    if used and os.environ.get("ATLAS_DOWNGRADE_FORCE") != "1":
        raise RuntimeError(
            f"{used} observation(s) were written from answers of your other nodes. The previous release cannot tell "
            "them from this node's own data. Run `atlas group assist forget <node>` for each helper first, or set "
            "ATLAS_DOWNGRADE_FORCE=1 to keep them as they are.")
    op.drop_table("group_contrib_state")
    op.drop_table("assist_lookup_op")
    with op.batch_alter_table("observation") as batch:
        batch.drop_column("assist_item_id")
    op.drop_index("ix_group_assist_item_helper", table_name="group_assist_item")
    op.drop_index("ix_group_assist_item_cid", table_name="group_assist_item")
    op.drop_table("group_assist_item")
    bind.execute(sa.text("DELETE FROM node_state WHERE key LIKE 'assist_%' OR key LIKE 'prov_share:%'"))
