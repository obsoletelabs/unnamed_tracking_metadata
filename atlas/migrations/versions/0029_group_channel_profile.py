"""owner-group private channel and profile sync (CONTRACT-C10 §9.2)

Revision ID: 0029
Revises: 0028

Thirteen new tables: change streams and entries, group account links, local profile storage (displayed values,
the version DAG, pruned HLCs, the review queue, per-node consent) and the web link flow (state, parked handles,
seen assertions, the asserting node's consent).

Data step: every existing account gets a `bio` and a `privacy.profile_public` version (cause "migration", origin
this node, one HLC taken from the migration time) copied from `app_user`. `app_user.bio` and
`app_user.profile_public` stay the source of truth for older code and are dual-written from now on (D10.23).

Downgrade drops the new tables. `app_user` is untouched, so every account keeps its bio and visibility; synced
preferences other than those two, the version history and the group account links are lost.
"""
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
USER_FK = dict(ondelete='CASCADE')
TABLES = ('group_stream', 'group_change', 'group_account_link', 'group_account_node', 'profile_field',
          'profile_version', 'profile_superseded', 'profile_conflict', 'profile_sync_consent', 'group_signin_state',
          'group_web_handle', 'group_assertion_seen', 'group_idp_consent')


def upgrade() -> None:
    op.create_table(
        'group_stream',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('origin', sa.String(16), primary_key=True),
        sa.Column('epoch', sa.String(8), primary_key=True),
        sa.Column('scope', sa.String(26), primary_key=True),
        sa.Column('hwm', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('last_issued_at', TS, nullable=True),
        sa.Column('key_id', sa.String(64), nullable=False),
        sa.Column('key_bound_seq', sa.BigInteger(), nullable=True),
        sa.Column('advertised_head', sa.BigInteger(), nullable=True),
        sa.Column('advertised_at', TS, nullable=True),
        sa.Column('opened_at', TS, nullable=False),
        sa.Column('closed_at', TS, nullable=True),
        sa.Column('blocked_seq', sa.BigInteger(), nullable=True),
        sa.Column('blocked_reason', sa.String(32), nullable=True),
    )
    op.create_table(
        'group_change',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('origin', sa.String(16), primary_key=True),
        sa.Column('epoch', sa.String(8), primary_key=True),
        sa.Column('scope', sa.String(26), primary_key=True),
        sa.Column('seq', sa.BigInteger(), primary_key=True),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('subject', sa.String(48), nullable=False),
        sa.Column('body', sa.JSON(), nullable=True),
        sa.Column('body_hash', sa.String(64), nullable=False),
        sa.Column('key_id', sa.String(64), nullable=False),
        sa.Column('sig', sa.String(128), nullable=False),
        sa.Column('hlc', sa.String(48), nullable=True),
        sa.Column('issued_at', TS, nullable=True),
        sa.Column('received_from', sa.String(16), nullable=True),
        sa.Column('received_at', TS, nullable=False),
        sa.Column('disposition', sa.String(12), nullable=False),
        sa.Column('reason', sa.String(32), nullable=True),
    )
    op.create_index('ix_group_change_scope', 'group_change', ['group_id', 'scope', 'issued_at'])
    op.create_table(
        'group_account_link',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('account_uid', sa.String(26), primary_key=True),
        sa.Column('user_id', sa.String(26), sa.ForeignKey('app_user.id', **USER_FK), nullable=False),
        sa.Column('minted_here', sa.Boolean(), nullable=False),
        sa.Column('linked_at', TS, nullable=False),
        sa.UniqueConstraint('group_id', 'user_id', name='uq_group_account_link_user'),
    )
    op.create_table(
        'group_account_node',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('account_uid', sa.String(26), primary_key=True),
        sa.Column('node_id', sa.String(16), primary_key=True),
        sa.Column('status', sa.String(10), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=True),
        sa.Column('entry_ref', sa.String(120), nullable=False),
        sa.Column('updated_at', TS, nullable=False),
    )
    op.create_table(
        'profile_field',
        sa.Column('user_id', sa.String(26), sa.ForeignKey('app_user.id', **USER_FK), primary_key=True),
        sa.Column('field', sa.String(48), primary_key=True),
        sa.Column('value', sa.JSON(), nullable=True),
        sa.Column('hlc', sa.String(48), nullable=False),
        sa.Column('origin_node', sa.String(16), nullable=False),
        sa.Column('cause', sa.String(12), nullable=False),
        sa.Column('updated_at', TS, nullable=False),
    )
    op.create_table(
        'profile_version',
        sa.Column('user_id', sa.String(26), sa.ForeignKey('app_user.id', **USER_FK), primary_key=True),
        sa.Column('field', sa.String(48), primary_key=True),
        sa.Column('hlc', sa.String(48), primary_key=True),
        sa.Column('value', sa.JSON(), nullable=True),
        sa.Column('cleared', sa.Boolean(), nullable=False),
        sa.Column('base', sa.JSON(), nullable=False),
        sa.Column('cause', sa.String(12), nullable=False),
        sa.Column('origin_node', sa.String(16), nullable=False),
        sa.Column('entry_ref', sa.String(120), nullable=True),
        sa.Column('is_head', sa.Boolean(), nullable=False),
        sa.Column('held_since', TS, nullable=True),
        sa.Column('created_at', TS, nullable=False),
    )
    op.create_table(
        'profile_superseded',
        sa.Column('user_id', sa.String(26), primary_key=True),
        sa.Column('field', sa.String(48), primary_key=True),
        sa.Column('hlc', sa.String(48), primary_key=True),
        sa.Column('pruned_at', TS, nullable=False),
    )
    op.create_table(
        'profile_conflict',
        sa.Column('id', sa.String(26), primary_key=True),
        sa.Column('user_id', sa.String(26), sa.ForeignKey('app_user.id', **USER_FK), nullable=False),
        sa.Column('field', sa.String(48), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('winner_hlc', sa.String(48), nullable=True),
        sa.Column('loser_hlc', sa.String(48), nullable=True),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(10), nullable=False),
        sa.Column('detected_at', TS, nullable=False),
        sa.Column('resolved_at', TS, nullable=True),
        sa.UniqueConstraint('user_id', 'field', 'kind', 'loser_hlc', name='uq_profile_conflict'),
    )
    op.create_table(
        'profile_sync_consent',
        sa.Column('user_id', sa.String(26), sa.ForeignKey('app_user.id', **USER_FK), primary_key=True),
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('fields', sa.JSON(), nullable=False),
        sa.Column('signout_propagation', sa.Boolean(), nullable=False),
        sa.Column('updated_at', TS, nullable=False),
    )
    op.create_table(
        'group_signin_state',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('group_id', sa.String(16), nullable=False),
        sa.Column('idp_node', sa.String(16), nullable=False),
        sa.Column('purpose', sa.String(8), nullable=False),
        sa.Column('nonce', sa.String(64), nullable=False),
        sa.Column('binding', sa.String(64), nullable=False),
        sa.Column('link_user_id', sa.String(26), nullable=True),
        sa.Column('next_url', sa.String(300), nullable=False),
        sa.Column('expires_at', TS, nullable=False),
    )
    op.create_table(
        'group_web_handle',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('expires_at', TS, nullable=False),
    )
    op.create_table(
        'group_assertion_seen',
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('jti', sa.String(26), primary_key=True),
        sa.Column('iss_node', sa.String(16), nullable=False),
        sa.Column('expires_at', TS, nullable=False),
    )
    op.create_table(
        'group_idp_consent',
        sa.Column('user_id', sa.String(26), sa.ForeignKey('app_user.id', **USER_FK), primary_key=True),
        sa.Column('group_id', sa.String(16), primary_key=True),
        sa.Column('rp_node', sa.String(16), primary_key=True),
        sa.Column('granted_at', TS, nullable=False),
    )
    _seed()


def _seed() -> None:
    """bio and privacy.profile_public versions for every live account, from app_user (D10.23 dual write)."""
    conn = op.get_bind()
    node_state = sa.table('node_state', sa.column('key', sa.String), sa.column('value', sa.Text))
    node = conn.execute(sa.select(node_state.c.value).where(node_state.c.key == 'node_id')).scalar() or '0' * 16
    now = datetime.now(UTC)
    hlc = f"{int(now.timestamp() * 1000):013d}-0000-{node}"
    users = sa.table('app_user', sa.column('id', sa.String), sa.column('bio', sa.String),
                     sa.column('profile_public', sa.Boolean), sa.column('deleted_at', TS))
    fields, versions = [], []
    for uid, bio, public, _ in conn.execute(sa.select(users).where(users.c.deleted_at.is_(None))):
        for name, value in (('bio', bio or ''), ('privacy.profile_public', bool(public))):
            fields.append({'user_id': uid, 'field': name, 'value': value, 'hlc': hlc, 'origin_node': node,
                           'cause': 'migration', 'updated_at': now})
            versions.append({'user_id': uid, 'field': name, 'hlc': hlc, 'value': value, 'cleared': False, 'base': [],
                             'cause': 'migration', 'origin_node': node, 'entry_ref': None, 'is_head': True,
                             'held_since': None, 'created_at': now})
    if fields:
        field_t = sa.Table('profile_field', sa.MetaData(), autoload_with=conn)
        version_t = sa.Table('profile_version', sa.MetaData(), autoload_with=conn)
        conn.execute(field_t.insert(), fields)
        conn.execute(version_t.insert(), versions)


def downgrade() -> None:
    op.drop_index('ix_group_change_scope', table_name='group_change')
    for name in reversed(TABLES):
        op.drop_table(name)
