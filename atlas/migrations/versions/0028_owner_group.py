"""owner federation group: genesis, certificates, revocation, views, enrolment (CONTRACT-C10 §9.1)

Revision ID: 0028
Revises: 0024

Nine new tables; no existing table changes, so upgrading a node changes nothing it already holds. 0025-0027
are reserved for other programme branches (H25); this revision follows 0024 directly.

Downgrade drops the tables. It is refused, and nothing is changed, while this node is still in a group
(owner_group.status not in left/revoked) or still holds the sealed group root (root_sealed set): dropping them
would destroy the only copy of G and leave the other members unable to tell that this node left. Leave the
group (or move the root) first, then downgrade.
"""
import sqlalchemy as sa
from alembic import op

revision = '0028'
down_revision = '0024'
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'owner_group',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('genesis', sa.JSON(), nullable=False),
        sa.Column('succession', sa.JSON(), nullable=False),
        sa.Column('root_key_id', sa.String(16), nullable=False),
        sa.Column('recovery', sa.JSON(), nullable=False),
        sa.Column('root_sealed', sa.Text(), nullable=True),
        sa.Column('box_sealed', sa.Text(), nullable=False),
        sa.Column('self_serial', sa.String(40), nullable=True),
        sa.Column('root_state', sa.String(10), nullable=False, server_default='none'),
        sa.Column('succession_pending', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('state_hash', sa.String(64), nullable=True),
        sa.Column('joined_at', TS, nullable=False),
        sa.Column('updated_at', TS, nullable=False),
        sa.Column('root_moved', sa.JSON(), nullable=True),
        sa.Column('succession_cancels', sa.JSON(), nullable=True),
        sa.Column('box_previous_sealed', sa.Text(), nullable=True),
        sa.Column('box_previous_until', TS, nullable=True),
        sa.Column('box_pending_sealed', sa.Text(), nullable=True),
    )
    op.create_table(
        'group_cert',
        sa.Column('group_id', sa.String(16), sa.ForeignKey('owner_group.group_id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('serial', sa.String(40), primary_key=True),
        sa.Column('issuer', sa.String(16), nullable=False),
        sa.Column('serial_n', sa.BigInteger(), nullable=False),
        sa.Column('serial_r', sa.String(16), nullable=True),
        sa.Column('parent_serial', sa.String(40), nullable=True),
        sa.Column('authorized_by_grant', sa.String(40), nullable=True),
        sa.Column('node_id', sa.String(16), nullable=False),
        sa.Column('member_rev', sa.Integer(), nullable=False),
        sa.Column('body', sa.JSON(), nullable=False),
        sa.Column('body_hash', sa.String(64), nullable=False),
        sa.Column('not_after', TS, nullable=False),
        sa.Column('received_at', TS, nullable=False),
        sa.Column('held_reason', sa.String(24), nullable=True),
    )
    op.create_index('ix_group_cert_node', 'group_cert', ['group_id', 'node_id', 'not_after'])
    op.create_table(
        'group_banned_node',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('node_id', sa.String(16), primary_key=True),
        sa.Column('root_public_key', sa.String(100), nullable=False),
        sa.Column('reason', sa.String(24), nullable=False),
        sa.Column('revoked_at', TS, nullable=False),
        sa.Column('crl_serial', sa.String(40), nullable=False),
    )
    op.create_table(
        'group_view',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('node_id', sa.String(16), primary_key=True),
        sa.Column('body', sa.JSON(), nullable=False),
        sa.Column('issued_at', TS, nullable=False),
        sa.Column('received_at', TS, nullable=False),
    )
    op.create_table(
        'group_member',
        sa.Column('group_id', sa.String(16), sa.ForeignKey('owner_group.group_id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('node_id', sa.String(16), primary_key=True),
        sa.Column('root_public_key', sa.String(100), nullable=False),
        sa.Column('box_public_key', sa.String(100), nullable=False),
        sa.Column('url', sa.String(300), nullable=False),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('serial', sa.String(40), nullable=False),
        sa.Column('scopes', sa.JSON(), nullable=False),
        sa.Column('delegate', sa.JSON(), nullable=True),
        sa.Column('not_after', TS, nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('rotations', sa.JSON(), nullable=False),
        sa.Column('last_seen_at', TS, nullable=True),
        sa.Column('last_protocol', sa.String(16), nullable=True),
        sa.Column('last_error', sa.String(300), nullable=True),
    )
    op.create_table(
        'group_crl',
        sa.Column('group_id', sa.String(16), sa.ForeignKey('owner_group.group_id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('issuer', sa.String(16), primary_key=True),
        sa.Column('crl_seq', sa.BigInteger(), primary_key=True),
        sa.Column('body', sa.JSON(), nullable=False),
        sa.Column('body_hash', sa.String(64), primary_key=True),
        sa.Column('received_at', TS, nullable=False),
    )
    op.create_table(
        'group_revoked',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('serial', sa.String(40), primary_key=True),
        sa.Column('issuer', sa.String(16), nullable=False),
        sa.Column('reason', sa.String(24), nullable=False),
        sa.Column('node_id', sa.String(16), nullable=True),
        sa.Column('root_public_key', sa.String(100), nullable=True),
        sa.Column('revoked_at', TS, nullable=False),
        sa.Column('suspect_since', TS, nullable=True),
        sa.Column('first_seen_at', TS, nullable=False),
    )
    op.create_table(
        'group_enrolment',
        sa.Column('id', sa.String(26), primary_key=True),
        sa.Column('side', sa.String(8), nullable=False),
        sa.Column('group_id', sa.String(16), nullable=False),
        sa.Column('invite_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('secret_enc', sa.Text(), nullable=False),
        sa.Column('peer_url', sa.String(300), nullable=True),
        sa.Column('peer_node_id', sa.String(16), nullable=True),
        sa.Column('scopes', sa.JSON(), nullable=False),
        sa.Column('delegate', sa.JSON(), nullable=True),
        sa.Column('transcript', sa.JSON(), nullable=True),
        sa.Column('transcript_hash', sa.String(64), nullable=True),
        sa.Column('commit_j', sa.String(64), nullable=True),
        sa.Column('nonce_j', sa.String(44), nullable=True),
        sa.Column('url_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('confirmed_at', TS, nullable=True),
        sa.Column('cert_serial', sa.String(40), nullable=True),
        sa.Column('created_by', sa.String(26), nullable=True),
        sa.Column('created_at', TS, nullable=False),
        sa.Column('expires_at', TS, nullable=False),
    )
    op.create_index('ix_group_enrolment_expiry', 'group_enrolment', ['expires_at'])
    op.create_table(
        'group_nonce',
        sa.Column('node_id', sa.String(16), primary_key=True),
        sa.Column('nonce', sa.String(32), primary_key=True),
        sa.Column('seen_at', TS, nullable=False),
    )
    op.create_index('ix_group_nonce_seen', 'group_nonce', ['seen_at'])


def downgrade() -> None:
    owner_group = sa.table('owner_group', sa.column('status', sa.String), sa.column('root_sealed', sa.Text))
    blocking = op.get_bind().execute(sa.select(sa.func.count()).select_from(owner_group).where(sa.or_(
        owner_group.c.status.notin_(('left', 'revoked')), owner_group.c.root_sealed.isnot(None)))).scalar()
    if blocking:
        raise RuntimeError('refusing to downgrade below 0028: this node is still in an owner group or holds the '
                           'sealed group root; leave the group (or move the root) first, nothing was changed')
    op.drop_index('ix_group_nonce_seen', table_name='group_nonce')
    op.drop_table('group_nonce')
    op.drop_index('ix_group_enrolment_expiry', table_name='group_enrolment')
    op.drop_table('group_enrolment')
    op.drop_table('group_revoked')
    op.drop_table('group_crl')
    op.drop_table('group_member')
    op.drop_table('group_view')
    op.drop_table('group_banned_node')
    op.drop_index('ix_group_cert_node', table_name='group_cert')
    op.drop_table('group_cert')
    op.drop_table('owner_group')
