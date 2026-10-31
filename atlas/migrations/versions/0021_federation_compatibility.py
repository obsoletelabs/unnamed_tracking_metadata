"""federation compatibility: parked vs quarantined entries, peer protocol, follower protocol telemetry

Revision ID: 0021
Revises: 0020

A follower now parks a signed entry it cannot understand (an unknown kind, body version, capability,
field or vocabulary value) instead of silently skipping it, and stops applying at that entry until an
upgrade. Entries that are understood but fail to apply stay quarantined and do not block (CONTRACTS-v1
C1.4, C1.8).

Existing sync_failure rows were all apply failures of known kinds, so they are backfilled as
quarantined / apply_error. replicated_change.kind is widened from 16 to 32 characters so a newer
authority's longer kind name is retained (and parked) instead of failing the whole page on PostgreSQL.

Downgrade drops the new columns, index and table, and narrows replicated_change.kind again. It refuses
while a retained entry has a kind longer than 16 characters (it would not fit; delete nothing silently).
Parked rows survive the downgrade as ordinary open sync failures and every signed entry stays
retained. The older code cannot park, though: it retries those rows, passes over kinds it does not
know, marks them resolved and moves its cursor past them (the old silent skip). After upgrading again,
`atlas sync replay --rescan` re-classifies those resolved rows and `atlas sync replay --historical`
re-applies them in feed order. The operational node_state keys this release wrote (downloaded head,
refusal, rewind, protocol, replay marker) are deleted; the next pull recomputes them.
"""
import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('sync_failure', schema=None) as batch_op:
        batch_op.add_column(sa.Column('disposition', sa.String(length=16), nullable=False,
                                      server_default='quarantined'))
        batch_op.add_column(sa.Column('reason', sa.String(length=32), nullable=False, server_default='apply_error'))
        batch_op.add_column(sa.Column('detail', sa.JSON(), nullable=True))
    op.create_index('ix_sync_failure_parked', 'sync_failure', ['node_id', 'disposition', 'seq'])
    with op.batch_alter_table('peer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('remote_protocol', sa.String(length=16), nullable=True))
    with op.batch_alter_table('replicated_change', schema=None) as batch_op:
        batch_op.alter_column('kind', existing_type=sa.String(length=16), type_=sa.String(length=32),
                              existing_nullable=False)
    op.create_table(
        'follower_seen',
        sa.Column('token_id', sa.String(length=26), nullable=False),
        sa.Column('protocol', sa.String(length=16), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('token_id'),
    )


def downgrade() -> None:
    bind = op.get_bind()
    long_kinds = bind.execute(sa.text("SELECT count(*) FROM replicated_change WHERE length(kind) > 16")).scalar()
    if long_kinds:
        raise RuntimeError(f"{long_kinds} retained feed entries have a kind longer than 16 characters; "
                           "the previous schema cannot hold them. Keep this release, or remove that feed first.")
    op.drop_table('follower_seen')
    with op.batch_alter_table('replicated_change', schema=None) as batch_op:
        batch_op.alter_column('kind', existing_type=sa.String(length=32), type_=sa.String(length=16),
                              existing_nullable=False)
    with op.batch_alter_table('peer', schema=None) as batch_op:
        batch_op.drop_column('remote_protocol')
    op.drop_index('ix_sync_failure_parked', table_name='sync_failure')
    with op.batch_alter_table('sync_failure', schema=None) as batch_op:
        batch_op.drop_column('detail')
        batch_op.drop_column('reason')
        batch_op.drop_column('disposition')
    # Operational watermarks this release wrote; the previous release never reads them.
    op.execute("DELETE FROM node_state WHERE key LIKE 'sync_downloaded:%' OR key LIKE 'sync_refused:%' "
               "OR key LIKE 'sync_protocol:%' OR key LIKE 'sync_rewound:%' "
               "OR key = 'sync_replayed_for'")
