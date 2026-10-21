"""artwork votes and a community-chosen default image

Revision ID: 0017
Revises: 0016
"""
import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('artwork', schema=None) as batch_op:
        batch_op.add_column(sa.Column('preferred', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('artwork_vote',
    sa.Column('artwork_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('value', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['artwork_id'], ['artwork.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('artwork_id', 'user_id')
    )
    with op.batch_alter_table('artwork_vote', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_artwork_vote_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('artwork_vote', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_artwork_vote_user_id'))
    op.drop_table('artwork_vote')
    with op.batch_alter_table('artwork', schema=None) as batch_op:
        batch_op.drop_column('preferred')
