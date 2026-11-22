"""owner-group delegation grants and federated sign-in sessions (CONTRACT-C10 §9.3)

Revision ID: 0030
Revises: 0029

One new table, `group_grant`, holds every signed administrative delegation grant (§6.1), with a `held_reason`
column as `group_cert` has (CONTRACT-AMENDMENTS A12). `user_session` gains the
provenance of a session opened by a group sign-in assertion (§5.4): the group, the asserting node, the assertion id
and the authentication methods it reported. Existing sessions keep NULL in all four, which means "local".

Downgrade first deletes every federated session (`via_group IS NOT NULL`), so nobody stays signed in through a
mechanism the older code cannot check, then drops the columns and the table. Grants are lost on downgrade; they
are signed objects the group re-sends on the next sync after a later upgrade.
"""
import sqlalchemy as sa
from alembic import op

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'group_grant',
        sa.Column('group_id', sa.String(16), sa.ForeignKey('owner_group.group_id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('serial', sa.String(40), primary_key=True),
        sa.Column('issuer', sa.String(16), nullable=False),
        sa.Column('serial_n', sa.BigInteger(), nullable=False),
        sa.Column('parent_serial', sa.String(40), nullable=True),
        sa.Column('authorized_by_grant', sa.String(40), nullable=True),
        sa.Column('account_uid', sa.String(26), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('nodes', sa.JSON(), nullable=False),
        sa.Column('not_before', TS, nullable=False),
        sa.Column('not_after', TS, nullable=False),
        sa.Column('body', sa.JSON(), nullable=False),
        sa.Column('body_hash', sa.String(64), nullable=False),
        sa.Column('received_at', TS, nullable=False),
        # Not in the §9.3 sketch (CONTRACT-AMENDMENTS A12): like group_cert.held_reason, a grant is held while a
        # root succession is pending, and both objects of an authority-bearing serial collision are rejected.
        sa.Column('held_reason', sa.String(24), nullable=True),
    )
    op.create_index('ix_group_grant_account', 'group_grant', ['group_id', 'account_uid', 'not_after'])
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.add_column(sa.Column('via_group', sa.String(16), nullable=True))
        batch_op.add_column(sa.Column('origin_node', sa.String(16), nullable=True))
        batch_op.add_column(sa.Column('assertion_id', sa.String(26), nullable=True))
        batch_op.add_column(sa.Column('amr', sa.JSON(), nullable=True))
        batch_op.create_index('ix_user_session_origin', ['origin_node'])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM user_session WHERE via_group IS NOT NULL"))
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.drop_index('ix_user_session_origin')
        batch_op.drop_column('amr')
        batch_op.drop_column('assertion_id')
        batch_op.drop_column('origin_node')
        batch_op.drop_column('via_group')
    op.drop_index('ix_group_grant_account', table_name='group_grant')
    op.drop_table('group_grant')
