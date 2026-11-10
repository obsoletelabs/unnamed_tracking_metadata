"""scopes: the record ceiling, the visibility projection and per-account permissions (CONTRACT-C14 §5.3, §5.7, §9)

Revision ID: 0038
Revises: 0037

Upgrade:
* `canonical.scope_ceiling` and `canonical.scope_ceiling_by` (String(8), NULL). NULL means "derived from the
  record's items", which is what every existing record is. A ceiling kept aside by an earlier downgrade
  (`node_state["c14_0038_kept"]`, `{cid: [ceiling, by]}`) is restored and the key removed.
* New table `scope_projection` (C14-1 §5.7, see CONTRACT-AMENDMENTS "C14-1"): what a viewer without
  `scope.detail` sees of a record that holds host/plugin library data or data received from the group. It is
  derived data. The upgrade leaves it empty and marks a rebuild (`node_state["scope_projection_pending"]`), which
  the worker (or `atlas scope reproject`) runs in batches over the records that need a row. No group data exists
  before this release, so until a record's row is written it is shown exactly as the previous release showed it
  (host/plugin values included); the Sharing page says while the rebuild is still running.
* New table `user_permission(user_id, permission, granted_by, granted_at)`, PK `(user_id, permission)`. Empty: the
  default holders of `scope.detail` (administrators, curators, every account while registration is closed,
  owner-group sessions) need no row.

Downgrade:
* Refused while any record has a ceiling, unless `ATLAS_DOWNGRADE_FORCE=1` (the previous release would publish a
  `group` record and show it everywhere). With the flag, every ceiling is kept aside as JSON in the existing
  `node_state` table (key `c14_0038_kept`, which the previous release never reads) and restored by the next upgrade.
* `scope_projection` and `user_permission` are dropped (a granted `scope.detail` is lost; the default holders keep
  their rights because those come from roles and settings). The ceiling columns are dropped.
"""
from __future__ import annotations

import json
import os

import sqlalchemy as sa
from alembic import op

revision = '0038'
down_revision = '0037'
branch_labels = None
depends_on = None

KEPT = "c14_0038_kept"
PENDING = "scope_projection_pending"


def upgrade() -> None:
    with op.batch_alter_table("canonical") as batch:
        batch.add_column(sa.Column("scope_ceiling", sa.String(8), nullable=True))
        batch.add_column(sa.Column("scope_ceiling_by", sa.String(8), nullable=True))
    op.create_table(
        "scope_projection",
        sa.Column("canonical_id", sa.String(40), sa.ForeignKey("canonical.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tier", sa.String(8), primary_key=True),
        sa.Column("present", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("view", sa.JSON(), nullable=False),
        sa.Column("explain", sa.JSON(), nullable=False),
        sa.Column("norm_title", sa.String(500), nullable=False, server_default=""),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("release_start", sa.Date(), nullable=True),
        sa.Column("release_end", sa.Date(), nullable=True),
        sa.Column("release_precision", sa.String(5), nullable=True),
    )
    op.create_index("ix_scope_projection_tier", "scope_projection", ["tier", "present"])
    op.create_table(
        "user_permission",
        sa.Column("user_id", sa.String(26), sa.ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission", sa.String(32), primary_key=True),
        sa.Column("granted_by", sa.String(26), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
    )
    bind = op.get_bind()
    raw = bind.execute(sa.text("SELECT value FROM node_state WHERE key = :k"), {"k": KEPT}).scalar()
    if raw:
        try:
            kept = json.loads(raw)
        except ValueError:
            kept = {}
        for cid, pair in (kept.items() if isinstance(kept, dict) else []):
            if isinstance(pair, list) and len(pair) == 2 and pair[0] in ("group", "private", "local"):
                bind.execute(sa.text("UPDATE canonical SET scope_ceiling = :c, scope_ceiling_by = :b WHERE id = :i"),
                             {"c": pair[0], "b": pair[1], "i": cid})
        bind.execute(sa.text("DELETE FROM node_state WHERE key = :k"), {"k": KEPT})
    bind.execute(sa.text("DELETE FROM node_state WHERE key = :k"), {"k": PENDING})
    bind.execute(sa.text("INSERT INTO node_state (key, value) VALUES (:k, :v)"), {"k": PENDING, "v": ""})


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, scope_ceiling, scope_ceiling_by FROM canonical "
                                "WHERE scope_ceiling IS NOT NULL")).all()
    if rows and os.environ.get("ATLAS_DOWNGRADE_FORCE") != "1":
        raise RuntimeError(
            f"{len(rows)} record(s) have a scope ceiling (shared with your nodes only, or kept on this node). The "
            "previous release would show and publish them like any other record. Lift the ceilings first, or set "
            "ATLAS_DOWNGRADE_FORCE=1 to keep them aside and restore them on the next upgrade.")
    bind.execute(sa.text("DELETE FROM node_state WHERE key IN (:k, :p)"), {"k": KEPT, "p": PENDING})
    if rows:
        bind.execute(sa.text("INSERT INTO node_state (key, value) VALUES (:k, :v)"),
                     {"k": KEPT, "v": json.dumps({r[0]: [r[1], r[2]] for r in rows}, sort_keys=True)})
    op.drop_table("user_permission")
    op.drop_index("ix_scope_projection_tier", table_name="scope_projection")
    op.drop_table("scope_projection")
    with op.batch_alter_table("canonical") as batch:
        batch.drop_column("scope_ceiling_by")
        batch.drop_column("scope_ceiling")
