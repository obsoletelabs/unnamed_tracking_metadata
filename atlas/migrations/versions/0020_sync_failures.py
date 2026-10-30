"""keep mirrored feed entries that fail to apply so they can be retried and reported

Revision ID: 0020
Revises: 0019

A mirror used to log and skip an entry it could not apply, leaving no trace and no way to
converge. The signed entry is retained in replicated_change; this table records that it still
needs applying.
"""
import sqlalchemy as sa
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sync_failure',
        sa.Column('node_id', sa.String(length=64), nullable=False),
        sa.Column('seq', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('first_failed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('node_id', 'seq'),
    )
    op.create_index('ix_sync_failure_open', 'sync_failure', ['resolved_at', 'node_id', 'seq'])


def downgrade() -> None:
    op.drop_index('ix_sync_failure_open', table_name='sync_failure')
    op.drop_table('sync_failure')
