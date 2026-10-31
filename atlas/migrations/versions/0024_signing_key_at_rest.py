"""encrypt the node signing key at rest (data only)

Revision ID: 0024
Revises: 0023

The node's Ed25519 private key was kept as plain base64 in `node_state` (`signing_key`), so every database
dump and backup archive held it (CONTRACTS-v1 C9.1, D8). It is now stored as `enc:v1:<fernet token>`,
encrypted with ATLAS_SECRET_KEY exactly like peer tokens. There is no schema change.

Upgrade encrypts a legacy plaintext key in place when the secret is known at migration time: the settings
`atlas migrate` and auto-migration pass, else the ATLAS_SECRET_KEY environment variable. Without a secret it
changes nothing; the application encrypts the key on the first start that has one. The key itself (and so
the node id and key id) never changes, only its stored form.

Downgrade turns an encrypted key back into plaintext, because the previous release cannot read `enc:v1:`.
That needs the secret (ATLAS_SECRET_KEY, or one of ATLAS_SECRET_KEY_PREVIOUS); without it, or with a wrong
one, the downgrade is refused and nothing is changed. A plaintext key is left as it is.

The Fernet derivation is copied from `atlas/secrets_box.py` so this migration keeps working if that module
changes.
"""
import base64
import hashlib
import os

import sqlalchemy as sa
from alembic import context, op
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None

PREFIX = 'enc:v1:'
KEY = 'signing_key'


def _box() -> MultiFernet | None:
    secrets = context.config.attributes.get('atlas_secrets')
    if secrets is None:
        secrets = (os.environ.get('ATLAS_SECRET_KEY'), os.environ.get('ATLAS_SECRET_KEY_PREVIOUS'))
    current, previous = secrets
    if not current:
        return None
    keys = [current, *(p.strip() for p in (previous or '').split(',') if p.strip())]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(hashlib.sha256(k.encode()).digest())) for k in keys])


def _table():
    return sa.table('node_state', sa.column('key', sa.String), sa.column('value', sa.Text))


def _stored() -> str | None:
    node_state = _table()
    return op.get_bind().execute(sa.select(node_state.c.value).where(node_state.c.key == KEY)).scalar()


def _write(value: str) -> None:
    node_state = _table()
    op.execute(node_state.update().where(node_state.c.key == KEY).values(value=value))


def upgrade() -> None:
    value = _stored()
    if value is None or value.startswith(PREFIX):
        return
    box = _box()
    if box is None:
        return  # no secret now: the application encrypts it on the first start with ATLAS_SECRET_KEY
    if len(base64.b64decode(value, validate=True)) != 32:
        raise RuntimeError('node_state.signing_key is not a 32-byte Ed25519 key; refusing to encrypt it')
    _write(PREFIX + box.encrypt(value.encode()).decode())


def downgrade() -> None:
    value = _stored()
    if value is None or not value.startswith(PREFIX):
        return
    box = _box()
    if box is None:
        raise RuntimeError('the node signing key is encrypted and the previous release cannot read it: set '
                           'ATLAS_SECRET_KEY to decrypt it during the downgrade (nothing was changed)')
    try:
        plain = box.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise RuntimeError('the node signing key cannot be decrypted with ATLAS_SECRET_KEY or '
                           'ATLAS_SECRET_KEY_PREVIOUS; downgrade refused (nothing was changed)') from None
    _write(plain)
