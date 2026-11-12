"""wire 1: widened identifiers (D5), the follower wire table, renditions, the per-peer wire (CONTRACT-C16 Z1)

Revision ID: 0063
Revises: 0062

Upgrade:
* D5: every column that holds a node id, group id or key id in 16 characters (`group_*`, `owner_group`,
  `profile_*`, `node_link`, `upstream_*`, `user_session`, `link_banned_key`) becomes 64 characters, and the
  certificate serial and HLC columns that embed one become 96 (`notification.key` and `node_state.key`, which
  name them, 160).
  Existing values are **not changed**: an identity minted before wire 1 keeps its 64-bit id (its signed history,
  every follower pin and every group certificate name it), and wire builds compare it through the explicit
  legacy mapping (`signing.id_matches`: a 16-character
  id is the first 16 hex digits of the SHA-256 of the key, a 64-character id is all of it). New identities, new
  keys and new groups are minted in the 256-bit form. PostgreSQL alters the columns; SQLite (which does not
  enforce lengths) rebuilds those tables in batch mode so the declared widths match the models.
* `peer.remote_wire` (JSON, nullable): what a wire peer advertised `{min, max, use, high}` (design §2.3, §4.10).
* `follower_wire`: per follower token, the wire range it sent, the negotiated `use`, what was omitted for it and
  why it was refused (replaces `follower_seen.protocol` for wire followers; `follower_seen` stays for v0 rows).
* `feed_rendition` (cache, origin and mirror) and `rendition_received` (follower). Empty: wire 1 renders nothing.
* `node_state["wire_high_water"] = "1"`: the highest wire that ran on this database (a build below it refuses to
  start outside `atlas restore`, design §4.7).
* A leftover `setting.emit_protocol` row is left in place (ignored and logged once), so a rollback gets it back.

Downgrade:
* Refused while any widened column holds a value longer than its old width (a 256-bit id or a serial that embeds
  one): the previous release cannot store them. Otherwise the columns shrink back, the three tables and
  `peer.remote_wire` are dropped and `wire_high_water` is removed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0063'
down_revision = '0062'
branch_labels = None
depends_on = None

#: (table, column, old length, new length, nullable)
WIDEN: tuple[tuple[str, str, int, int, bool], ...] = (
    ("owner_group", "group_id", 16, 64, False), ("owner_group", "root_key_id", 16, 64, False),
    ("owner_group", "self_serial", 40, 96, True),
    ("group_account_link", "group_id", 16, 64, False),
    ("group_account_node", "group_id", 16, 64, False), ("group_account_node", "node_id", 16, 64, False),
    ("group_assertion_seen", "group_id", 16, 64, False), ("group_assertion_seen", "iss_node", 16, 64, False),
    ("group_banned_node", "group_id", 16, 64, False), ("group_banned_node", "node_id", 16, 64, False),
    ("group_banned_node", "crl_serial", 40, 96, False),
    ("group_cert", "group_id", 16, 64, False), ("group_cert", "serial", 40, 96, False),
    ("group_cert", "issuer", 16, 64, False), ("group_cert", "serial_r", 16, 64, True),
    ("group_cert", "parent_serial", 40, 96, True), ("group_cert", "node_id", 16, 64, False),
    ("group_change", "group_id", 16, 64, False), ("group_change", "origin", 16, 64, False),
    ("group_change", "hlc", 48, 96, True), ("group_change", "received_from", 16, 64, True),
    ("group_crl", "group_id", 16, 64, False), ("group_crl", "issuer", 16, 64, False),
    ("group_enrolment", "group_id", 16, 64, False), ("group_enrolment", "peer_node_id", 16, 64, True),
    ("group_enrolment", "cert_serial", 40, 96, True),
    ("group_grant", "group_id", 16, 64, False), ("group_grant", "serial", 40, 96, False),
    ("group_grant", "issuer", 16, 64, False), ("group_grant", "parent_serial", 40, 96, True),
    ("group_idp_consent", "group_id", 16, 64, False), ("group_idp_consent", "rp_node", 16, 64, False),
    ("group_member", "group_id", 16, 64, False), ("group_member", "node_id", 16, 64, False),
    ("group_member", "serial", 40, 96, False),
    ("group_nonce", "node_id", 16, 64, False),
    ("group_revoked", "group_id", 16, 64, False), ("group_revoked", "serial", 40, 96, False),
    ("group_revoked", "issuer", 16, 64, False), ("group_revoked", "node_id", 16, 64, True),
    ("group_signin_state", "group_id", 16, 64, False), ("group_signin_state", "idp_node", 16, 64, False),
    ("group_stream", "group_id", 16, 64, False), ("group_stream", "origin", 16, 64, False),
    ("group_view", "group_id", 16, 64, False), ("group_view", "node_id", 16, 64, False),
    ("link_banned_key", "node_id", 16, 64, False),
    ("node_link", "node_id", 16, 64, False), ("node_link", "current_key_id", 16, 64, False),
    ("profile_conflict", "winner_hlc", 48, 96, True), ("profile_conflict", "loser_hlc", 48, 96, True),
    ("profile_field", "hlc", 48, 96, False), ("profile_field", "origin_node", 16, 64, False),
    ("profile_superseded", "hlc", 48, 96, False),
    ("profile_sync_consent", "group_id", 16, 64, False),
    ("profile_version", "hlc", 48, 96, False), ("profile_version", "origin_node", 16, 64, False),
    ("upstream_contribution", "main_node_id", 16, 64, False), ("upstream_link", "main_node_id", 16, 64, False),
    ("user_session", "via_group", 16, 64, True), ("user_session", "origin_node", 16, 64, True),
    ("notification", "key", 80, 160, False),  # group notices name a 64-hex id or a 96-character serial
    ("node_state", "key", 64, 160, False),  # per-peer keys such as `sync_media_withheld:<node id>`
)
HIGH_WATER = "wire_high_water"


def _alter(widths: str) -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if bind.dialect.name == "postgresql":
        for table, column, old, new, _nullable in WIDEN:
            if table in existing:
                length = new if widths == "new" else old
                op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE VARCHAR({length})'))
        return
    # SQLite does not enforce VARCHAR lengths, but the declared type is what the schema says: rebuild each table
    # (batch mode copies the rows) so the declared widths match the models.
    by_table: dict[str, list[tuple[str, int, int, bool]]] = {}
    for table, column, old, new, nullable in WIDEN:
        if table in existing:
            by_table.setdefault(table, []).append((column, old, new, nullable))
    for table, columns in by_table.items():
        with op.batch_alter_table(table) as batch:
            for column, old, new, nullable in columns:
                to, frm = (new, old) if widths == "new" else (old, new)
                batch.alter_column(column, type_=sa.String(to), existing_type=sa.String(frm),
                                   existing_nullable=nullable)


def upgrade() -> None:
    _alter("new")
    with op.batch_alter_table("peer") as batch:
        batch.add_column(sa.Column("remote_wire", sa.JSON(), nullable=True))
    op.create_table(
        "follower_wire",
        sa.Column("token_id", sa.String(26), primary_key=True),
        sa.Column("wire_min", sa.Integer(), nullable=True),
        sa.Column("wire_max", sa.Integer(), nullable=True),
        sa.Column("use", sa.Integer(), nullable=True),
        sa.Column("omitted", sa.JSON(), nullable=True),
        sa.Column("omitted_since", sa.BigInteger(), nullable=True),
        sa.Column("refused", sa.String(24), nullable=True),
        sa.Column("legacy_protocol", sa.String(16), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "feed_rendition",
        sa.Column("origin_node", sa.String(64), primary_key=True),
        sa.Column("seq", sa.BigInteger(), primary_key=True),
        sa.Column("body_hash", sa.String(64), primary_key=True),
        sa.Column("rendition_version", sa.Integer(), primary_key=True),
        sa.Column("signer_id", sa.String(64), primary_key=True),
        sa.Column("signer_key_id", sa.String(64), primary_key=True),
        sa.Column("adapter_version", sa.Integer(), nullable=False),
        sa.Column("object", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "rendition_received",
        sa.Column("node_id", sa.String(64), primary_key=True),
        sa.Column("seq", sa.BigInteger(), primary_key=True),
        sa.Column("rendition_version", sa.Integer(), primary_key=True),
        sa.Column("signer_kind", sa.String(8), nullable=False),
        sa.Column("signer_id", sa.String(64), nullable=False),
        sa.Column("object", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    node_state = sa.table("node_state", sa.column("key", sa.String), sa.column("value", sa.Text))
    bind = op.get_bind()
    if bind.execute(sa.select(node_state.c.key).where(node_state.c.key == HIGH_WATER)).first() is None:
        op.bulk_insert(node_state, [{"key": HIGH_WATER, "value": "1"}])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table, column, old, _new, _nullable in WIDEN:
        if table not in existing:
            continue
        longest = bind.execute(sa.text(f'SELECT max(length("{column}")) FROM "{table}"')).scalar()
        if longest is not None and longest > old:
            raise RuntimeError(
                f"refusing to downgrade: {table}.{column} holds a value of {longest} characters (a 256-bit wire-1 "
                f"identifier); the previous release stores at most {old}. Restore the pre-upgrade backup instead.")
    op.drop_table("rendition_received")
    op.drop_table("feed_rendition")
    op.drop_table("follower_wire")
    with op.batch_alter_table("peer") as batch:
        batch.drop_column("remote_wire")
    op.execute(sa.text("DELETE FROM node_state WHERE key = :k").bindparams(k=HIGH_WATER))
    _alter("old")
