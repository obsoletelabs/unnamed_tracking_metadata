"""price observations: the `price_observation` table (CONTRACTS-v1 C8.2, model only in Release 0)

Revision ID: 0033
Revises: 0032

Creates `price_observation` as C8.1/C8.2 define it: one observed price of one offer (provider, product, package,
edition, platform) in one storefront region, money as integers in ISO 4217 minor units, availability,
`observed_at`/`valid_until`, provenance, licence and `retention_days`. Additive columns recorded in
CONTRACT-AMENDMENTS A16: `license` is stored as `redistributable` + `license_terms` (the price channel filters on
the first), `purge_after` (= observed_at + retention_days, so the purge is one indexed range delete on every
database) and `dedup_key` (the offer plus `observed_at`, unique, so a re-sent observation is stored once).

Indexes: `(canonical_id, region, currency, observed_at)` and `(provider, product_id, region)` from C8.2 (a B-tree
is read backwards for "newest first"), plus the purge and dedup indexes.

Existing data: no price existed before this revision; nothing is backfilled and no record changes. Prices are
not part of `canonical.view`, so record versions and the signed feed are untouched.

Downgrade keeps the data: the table is renamed to `price_observation_0033_kept` (its indexes and constraints move
with it), which the previous release neither reads nor backs up. Upgrading again renames it back and points
every row whose record was merged meanwhile (by the previous release, which does not know prices) at the
surviving record, following merge chains, so reads by the live id find them. Rows whose retention passed while
downgraded are left to the purge job; reads already hide them (`purge_after`).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None


KEPT = "price_observation_0033_kept"
#: Longest merge chain followed when re-pointing kept rows (merges always target a live record, so chains are
#: short; this only bounds the loop).
MAX_CHAIN = 32


def upgrade() -> None:
    bind = op.get_bind()
    if KEPT in sa.inspect(bind).get_table_names():
        _restore(bind)
        return
    op.create_table(
        "price_observation",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("canonical_id", sa.String(40), sa.ForeignKey("canonical.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(128), nullable=False),
        sa.Column("package_id", sa.String(128), nullable=True),
        sa.Column("edition", sa.String(128), nullable=True),
        sa.Column("platform", sa.String(32), nullable=True),
        sa.Column("region", sa.String(2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("base_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("discount_pct", sa.Integer(), nullable=True),
        sa.Column("availability", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("redistributable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("license_terms", sa.String(64), nullable=False, server_default=""),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False, server_default=""),
        sa.CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="ck_price_amount"),
        sa.CheckConstraint("base_amount_minor IS NULL OR base_amount_minor >= 0", name="ck_price_base"),
        sa.CheckConstraint("discount_pct IS NULL OR (discount_pct >= 0 AND discount_pct <= 100)",
                           name="ck_price_discount"),
        sa.CheckConstraint("retention_days >= 1 AND retention_days <= 365", name="ck_price_retention"),
    )
    op.create_index("uq_price_dedup", "price_observation", ["dedup_key"], unique=True)
    op.create_index("ix_price_latest", "price_observation", ["canonical_id", "region", "currency", "observed_at"])
    op.create_index("ix_price_offer", "price_observation", ["provider", "product_id", "region"])
    op.create_index("ix_price_purge", "price_observation", ["purge_after"])


def _restore(bind) -> None:
    """Re-upgrade after a downgrade: bring the kept rows back and re-point those whose record was merged."""
    op.rename_table(KEPT, "price_observation")
    repoint = sa.text(
        "UPDATE price_observation SET canonical_id = (SELECT c.merged_into FROM canonical c "
        "WHERE c.id = price_observation.canonical_id) WHERE canonical_id IN "
        "(SELECT id FROM canonical WHERE merged_into IS NOT NULL)")
    for _ in range(MAX_CHAIN):
        if not bind.execute(repoint).rowcount:
            break


def downgrade() -> None:
    op.rename_table("price_observation", KEPT)
