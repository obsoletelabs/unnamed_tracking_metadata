"""merge the C14 line (0038-0041) and the C15 line (0060-0061) into one head

Revision ID: 0062
Revises: 0041, 0061

The programme built CONTRACT-C14 (scopes, ask the group, node links, super group: 0038-0041) and CONTRACT-C15
(media-type achievements flag, the book model: 0060-0061) as two branches from 0037. The two lines touch different
tables and columns, so this revision only joins them: no schema or data change in either direction. Downgrading
past it leaves both branch heads; `atlas migrate --revision <rev>` names the one to go back to.
"""
from __future__ import annotations

revision = '0062'
down_revision = ('0041', '0061')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
