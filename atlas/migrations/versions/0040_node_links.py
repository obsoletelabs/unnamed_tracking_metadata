"""node links: main side and node side of linking a local node to a main Atlas (CONTRACT-C14 §6, §7, §9)

Revision ID: 0040
Revises: 0039

Upgrade (main side):
* New tables `node_link`, `link_challenge`, `link_device`, `link_refresh`, `link_nonce` (PK `(jkt, jti)`, so a
  replayed DPoP proof is a duplicate key), `link_banned_key` and `link_contribution_answer`. Empty.
* `oidc_access.link_id`, `oidc_access.key_epoch`, `oidc_access.jkt` (NULL): a node link's DPoP-bound access token.
* `proposal.via_link_id`, `proposal.contribution_id` (unique), `proposal.stale_base` (false), `proposal.link_flag`.
* `user_permission` gains the value `feed.access` (no schema change; the table is 0038's).

Upgrade (node side):
* New tables `upstream_link`, `upstream_contribution` (unique `(main_node_id, seq)`) and `upstream_alias`. Empty.

Downgrade:
* Refused while a link is active here (as a main) or while this node has unsent or pending contributions (as a
  node), unless `ATLAS_DOWNGRADE_FORCE=1`: links become unusable and the queue is lost. With the flag everything
  above is dropped, `feed.access` grants are removed (feed tokens keep working only under
  `self_service_sync_tokens`) and the `link_pairwise_key` node state is removed (every subject changes if the
  release is upgraded again; the links it named are gone anyway).
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = '0040'
down_revision = '0039'
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "node_link",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("user_id", sa.String(26), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("node_id", sa.String(16), nullable=False),
        sa.Column("root_public_key", sa.String(64), nullable=False),
        sa.Column("current_key_id", sa.String(16), nullable=False),
        sa.Column("current_jkt", sa.String(64), nullable=False),
        sa.Column("key_epoch", sa.Integer(), nullable=False),
        sa.Column("rotations", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(36), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("public_url", sa.String(300), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("review", sa.String(8), nullable=False),
        sa.Column("trust", sa.String(10), nullable=False),
        sa.Column("reputation", sa.Integer(), nullable=False),
        sa.Column("show_name", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        _ts("created_at", False), _ts("last_used_at"), _ts("revoked_at"),
        sa.Column("revoked_by", sa.String(26), nullable=True),
        sa.Column("revoke_reason", sa.String(32), nullable=True),
        sa.UniqueConstraint("user_id", "node_id", name="uq_node_link_user_node"),
        sa.UniqueConstraint("subject", name="uq_node_link_subject"),
    )
    op.create_index("ix_node_link_user_id", "node_link", ["user_id"])
    op.create_index("ix_node_link_node_id", "node_link", ["node_id"])
    op.create_index("ix_node_link_status", "node_link", ["status"])
    op.create_table(
        "link_challenge",
        sa.Column("hash", sa.String(64), primary_key=True),
        _ts("expires_at", False), _ts("used_at"),
    )
    op.create_index("ix_link_challenge_expires_at", "link_challenge", ["expires_at"])
    op.create_table(
        "link_device",
        sa.Column("device_code_hash", sa.String(64), primary_key=True),
        sa.Column("user_code_hmac", sa.String(64), nullable=False, unique=True),
        sa.Column("confirm_hash", sa.String(64), nullable=False),
        sa.Column("jkt", sa.String(64), nullable=False),
        sa.Column("node", sa.JSON(), nullable=False),
        sa.Column("scopes", sa.String(100), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=True),
        sa.Column("link_id", sa.String(26), nullable=True),
        sa.Column("show_name", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("confirm_attempts", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        _ts("last_poll_at"), _ts("created_at", False), _ts("expires_at", False),
    )
    op.create_index("ix_link_device_expires_at", "link_device", ["expires_at"])
    op.create_table(
        "link_refresh",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("link_id", sa.String(26), nullable=False),
        sa.Column("jkt", sa.String(64), nullable=False),
        sa.Column("key_epoch", sa.Integer(), nullable=False),
        _ts("expires_at", False), _ts("used_at"), _ts("revoked_at"),
    )
    op.create_index("ix_link_refresh_link_id", "link_refresh", ["link_id"])
    op.create_index("ix_link_refresh_expires_at", "link_refresh", ["expires_at"])
    op.create_table(
        "link_nonce",
        sa.Column("jkt", sa.String(64), primary_key=True),
        sa.Column("jti", sa.String(64), primary_key=True),
        _ts("seen_at", False),
    )
    op.create_index("ix_link_nonce_seen_at", "link_nonce", ["seen_at"])
    op.create_table(
        "link_banned_key",
        sa.Column("node_id", sa.String(16), primary_key=True),
        sa.Column("reason", sa.String(32), nullable=False),
        _ts("banned_at", False),
        sa.Column("by", sa.String(26), nullable=True),
    )
    op.create_table(
        "link_contribution_answer",
        sa.Column("contribution_id", sa.String(80), primary_key=True),
        sa.Column("link_id", sa.String(26), nullable=False),
        sa.Column("answer", sa.JSON(), nullable=False),
        _ts("retry_until"), _ts("at", False),
    )
    op.create_index("ix_link_contribution_answer_link_id", "link_contribution_answer", ["link_id"])
    with op.batch_alter_table("oidc_access") as batch:
        batch.add_column(sa.Column("link_id", sa.String(26), nullable=True))
        batch.add_column(sa.Column("key_epoch", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("jkt", sa.String(64), nullable=True))
        batch.create_index("ix_oidc_access_link_id", ["link_id"])
    with op.batch_alter_table("proposal") as batch:
        batch.add_column(sa.Column("via_link_id", sa.String(26), nullable=True))
        batch.add_column(sa.Column("contribution_id", sa.String(80), nullable=True))
        batch.add_column(sa.Column("stale_base", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("link_flag", sa.String(20), nullable=True))
        batch.create_index("ix_proposal_via_link_id", ["via_link_id"])
        batch.create_unique_constraint("uq_proposal_contribution_id", ["contribution_id"])
    # node side
    op.create_table(
        "upstream_link",
        sa.Column("main_node_id", sa.String(16), primary_key=True),
        sa.Column("issuer_url", sa.String(300), nullable=False),
        sa.Column("main_root_public_key", sa.String(64), nullable=False),
        sa.Column("main_name", sa.String(80), nullable=False),
        sa.Column("link_root_public", sa.String(64), nullable=False),
        sa.Column("link_key", sa.Text(), nullable=False),
        sa.Column("link_key_previous", sa.Text(), nullable=True),
        sa.Column("link_rotations", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("flow", sa.JSON(), nullable=True),
        sa.Column("link_id", sa.String(26), nullable=True),
        sa.Column("subject", sa.String(36), nullable=True),
        sa.Column("display_name", sa.String(80), nullable=True),
        sa.Column("grant", sa.JSON(), nullable=True),
        sa.Column("key_epoch", sa.Integer(), nullable=False),
        sa.Column("access_enc", sa.Text(), nullable=True),
        sa.Column("refresh_enc", sa.Text(), nullable=True),
        _ts("access_expires_at"),
        sa.Column("next_seq", sa.BigInteger(), nullable=False),
        sa.Column("status_cursor", sa.String(80), nullable=True),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        _ts("retry_at"), _ts("created_at", False), _ts("linked_at"), _ts("last_used_at"), _ts("unlinked_at"),
    )
    op.create_table(
        "upstream_contribution",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("main_node_id", sa.String(16), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("local_cid", sa.String(40), nullable=True),
        sa.Column("main_cid", sa.String(40), nullable=True),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(14), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        _ts("next_try_at"),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("proposal_id", sa.String(26), nullable=True),
        sa.Column("feed_seq", sa.BigInteger(), nullable=True),
        _ts("decided_at"),
        sa.Column("reviewer_note", sa.String(500), nullable=True),
        sa.Column("created_by", sa.String(26), nullable=True),
        _ts("created_at", False), _ts("updated_at", False),
        sa.UniqueConstraint("main_node_id", "seq", name="uq_upstream_contribution_seq"),
    )
    op.create_index("ix_upstream_contribution_main_node_id", "upstream_contribution", ["main_node_id"])
    op.create_index("ix_upstream_contribution_local_cid", "upstream_contribution", ["local_cid"])
    op.create_index("ix_upstream_contribution_state", "upstream_contribution", ["state"])
    op.create_table(
        "upstream_alias",
        sa.Column("local_cid", sa.String(40), primary_key=True),
        sa.Column("main_cid", sa.String(40), nullable=False),
        _ts("at", False),
    )
    op.create_index("ix_upstream_alias_main_cid", "upstream_alias", ["main_cid"])


def downgrade() -> None:
    bind = op.get_bind()
    links = bind.execute(sa.text("SELECT COUNT(*) FROM node_link WHERE status = 'active'")).scalar() or 0
    queued = bind.execute(sa.text("SELECT COUNT(*) FROM upstream_contribution WHERE state IN "
                                  "('queued', 'sent', 'pending')")).scalar() or 0
    if (links or queued) and os.environ.get("ATLAS_DOWNGRADE_FORCE") != "1":
        raise RuntimeError(
            f"{links} node link(s) are active on this node and {queued} contribution(s) to a main Atlas are not "
            "decided yet. The previous release has no node links: the links would stop working and the queue would "
            "be lost. Unlink first, or set ATLAS_DOWNGRADE_FORCE=1 to drop them.")
    op.drop_index("ix_upstream_alias_main_cid", table_name="upstream_alias")
    op.drop_table("upstream_alias")
    for name in ("state", "local_cid", "main_node_id"):
        op.drop_index(f"ix_upstream_contribution_{name}", table_name="upstream_contribution")
    op.drop_table("upstream_contribution")
    op.drop_table("upstream_link")
    with op.batch_alter_table("proposal") as batch:
        batch.drop_constraint("uq_proposal_contribution_id", type_="unique")
        batch.drop_index("ix_proposal_via_link_id")
        batch.drop_column("link_flag")
        batch.drop_column("stale_base")
        batch.drop_column("contribution_id")
        batch.drop_column("via_link_id")
    with op.batch_alter_table("oidc_access") as batch:
        batch.drop_index("ix_oidc_access_link_id")
        batch.drop_column("jkt")
        batch.drop_column("key_epoch")
        batch.drop_column("link_id")
    bind.execute(sa.text("DELETE FROM oidc_access WHERE client_id = 'atlas-node'"))
    op.drop_index("ix_link_contribution_answer_link_id", table_name="link_contribution_answer")
    op.drop_table("link_contribution_answer")
    op.drop_table("link_banned_key")
    op.drop_index("ix_link_nonce_seen_at", table_name="link_nonce")
    op.drop_table("link_nonce")
    op.drop_index("ix_link_refresh_expires_at", table_name="link_refresh")
    op.drop_index("ix_link_refresh_link_id", table_name="link_refresh")
    op.drop_table("link_refresh")
    op.drop_index("ix_link_device_expires_at", table_name="link_device")
    op.drop_table("link_device")
    op.drop_index("ix_link_challenge_expires_at", table_name="link_challenge")
    op.drop_table("link_challenge")
    for name in ("status", "node_id", "user_id"):
        op.drop_index(f"ix_node_link_{name}", table_name="node_link")
    op.drop_table("node_link")
    bind.execute(sa.text("DELETE FROM user_permission WHERE permission = 'feed.access'"))
    bind.execute(sa.text("DELETE FROM node_state WHERE key = 'link_pairwise_key'"))
