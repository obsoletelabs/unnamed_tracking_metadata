"""local, unsigned client change log (client_change)

Revision ID: 0023
Revises: 0022

Provider imports and other non-redistributable writes (artwork, achievements, custom values such as
crossplay, provider observations) never reached the signed feed, so `GET /api/v2/changes` could not tell
a client that a record had changed (#78 part 2, #62; CONTRACTS-v1 C3.1). This table is a per-node log of
client-visible version bumps that `GET /api/v2/changes?feed=local` pages through. It holds ids, versions
and a cause only, never record content, and is never federated, relayed or promoted.

Upgrade creates the empty table and its index. Nothing is backfilled: the log starts with the first
change after the upgrade, and clients do one full read when they switch to `feed=local`.

Downgrade drops the table. It is a derived, disposable log (no record data lives only here), so nothing a
record needs is lost. The previous release goes back to not bumping versions for provider imports.
Upgrading again starts an empty log whose sequence restarts at 1; a client whose stored cursor is now
above the head gets `"reset": true` and re-reads, so a restarted sequence is never mistaken for progress.
"""
import sqlalchemy as sa
from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'client_change',
        sa.Column('seq', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('canonical_id', sa.String(length=40), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('cause', sa.String(length=16), nullable=False),
        sa.Column('origin_node', sa.String(length=64), nullable=True),
        sa.Column('origin_seq', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('seq'),
        sqlite_autoincrement=True,  # SQLite never reuses a seq
    )
    op.create_index('ix_client_change_cid', 'client_change', ['canonical_id', 'seq'])


def downgrade() -> None:
    op.drop_index('ix_client_change_cid', table_name='client_change')
    op.drop_table('client_change')
