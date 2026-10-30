"""store a record's media type in a column as wide as the type keys

Revision ID: 0019
Revises: 0018

Media type keys may be 32 characters (media_type.key), but canonical.media_type held 16, so a
record of a longer custom type failed on PostgreSQL. SQLite does not enforce the length.
"""
import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('canonical', schema=None) as batch_op:
        batch_op.alter_column('media_type', existing_type=sa.String(length=16), type_=sa.String(length=32),
                              existing_nullable=False, existing_server_default='game')


def downgrade() -> None:
    # Fails if a record already uses a type key longer than 16 characters.
    with op.batch_alter_table('canonical', schema=None) as batch_op:
        batch_op.alter_column('media_type', existing_type=sa.String(length=32), type_=sa.String(length=16),
                              existing_nullable=False, existing_server_default='game')
