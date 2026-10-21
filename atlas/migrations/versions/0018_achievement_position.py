"""achievement order chosen by editors

Revision ID: 0018
Revises: 0017
"""
import sqlalchemy as sa
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('achievement', schema=None) as batch_op:
        batch_op.add_column(sa.Column('position', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('achievement', schema=None) as batch_op:
        batch_op.drop_column('position')
