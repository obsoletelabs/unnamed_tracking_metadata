"""remember feed entries a mirror could not apply

Revision ID: 0020
Revises: 0019
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
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('error', sa.String(length=300), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('first_failed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_failed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['node_id'], ['replicated_feed.node_id']),
        sa.PrimaryKeyConstraint('node_id', 'seq'),
    )


def downgrade() -> None:
    op.drop_table('sync_failure')
