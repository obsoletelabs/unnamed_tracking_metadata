"""achievement kind, tier, points and cached icons

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('achievement', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('tier', sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column('points', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('locked_icon_url', sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column('icon_blob_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('icon_fail_count', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('icon_next_try_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('locked_icon_blob_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('locked_icon_fail_count', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('locked_icon_next_try_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_achievement_icon_blob_hash'), ['icon_blob_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_achievement_locked_icon_blob_hash'), ['locked_icon_blob_hash'],
                              unique=False)


def downgrade() -> None:
    with op.batch_alter_table('achievement', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_achievement_locked_icon_blob_hash'))
        batch_op.drop_index(batch_op.f('ix_achievement_icon_blob_hash'))
        for col in ('locked_icon_next_try_at', 'locked_icon_fail_count', 'locked_icon_blob_hash', 'icon_next_try_at',
                    'icon_fail_count', 'icon_blob_hash', 'locked_icon_url', 'points', 'tier', 'kind'):
            batch_op.drop_column(col)
