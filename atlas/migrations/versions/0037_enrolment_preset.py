"""the trust preset an administrator chose for a node-group enrolment (SPEC-UI-POLISH-PASS §4.3, U3)

Revision ID: 0037
Revises: 0036

Upgrade: `group_enrolment.preset` (NULL, or `same_owner` / `cautious`) and `group_enrolment.preset_admin` (false).
The preset is applied to this node's own local policy when the enrolment becomes active, and only on the side that
chose it; a cancelled or expired enrolment changes nothing. Existing enrolments get NULL / false: no preset.

Downgrade: both columns are dropped. A preset already applied stays applied (it is ordinary local policy, stored
in `setting`); an enrolment still open loses its preset and then activates with the policy as it is.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("group_enrolment") as batch:
        batch.add_column(sa.Column("preset", sa.String(16), nullable=True))
        batch.add_column(sa.Column("preset_admin", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("group_enrolment") as batch:
        batch.drop_column("preset_admin")
        batch.drop_column("preset")
