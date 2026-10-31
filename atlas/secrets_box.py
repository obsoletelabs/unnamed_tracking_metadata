"""Encryption of secrets kept in the database, keyed by ATLAS_SECRET_KEY (never stored in the database).

Tokens are Fernet. `ATLAS_SECRET_KEY_PREVIOUS` (comma separated) lists retired secrets that can still decrypt,
so the secret can be rotated: set the new one, keep the old one in `..._PREVIOUS`, run `atlas secrets rewrap`,
then drop the old one (CONTRACTS-v1 C9.1).
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .settings import Settings


class SecretsError(Exception):
    pass


def _key(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def previous_secrets(settings: Settings) -> list[str]:
    raw = settings.secret_key_previous or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _fernet(settings: Settings) -> MultiFernet:
    if not settings.secret_key:
        raise SecretsError("set ATLAS_SECRET_KEY (see `atlas secret-key`) to store secrets in the database")
    # The first key encrypts; every key decrypts. Tokens stay plain Fernet, so this is backward compatible.
    return MultiFernet([_key(settings.secret_key), *(_key(s) for s in previous_secrets(settings))])


def available(settings: Settings) -> bool:
    return bool(settings.secret_key)


def encrypt(settings: Settings, plain: str) -> str:
    return _fernet(settings).encrypt(plain.encode()).decode()


def decrypt(settings: Settings, token: str) -> str:
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretsError("stored secret cannot be decrypted (was ATLAS_SECRET_KEY changed?)") from exc


def rewrap_token(settings: Settings, token: str) -> str:
    """The same secret, encrypted with the current ATLAS_SECRET_KEY."""
    try:
        return _fernet(settings).rotate(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretsError("stored secret cannot be decrypted (was ATLAS_SECRET_KEY changed?)") from exc


def rewrap(session: Any, settings: Settings) -> dict[str, int]:
    """Re-encrypt every secret this node keeps in the database with the current secret (`atlas secrets rewrap`).

    Covers the node signing key, peer tokens, OIDC client secrets, provider credentials (Admin → Integrations)
    and TOTP secrets. Nothing is written unless every value decrypts: one undecryptable value aborts the run
    with a `SecretsError` naming where it is, so a retired secret is never dropped while still needed."""
    from sqlalchemy import select, text

    from . import integrations, signing
    from .models import AuditEntry, NodeState, OidcIssuer, Peer, Setting, UserMfa

    if not available(settings):
        raise SecretsError("set ATLAS_SECRET_KEY to the new secret (and the old one in ATLAS_SECRET_KEY_PREVIOUS)")
    counts = {"signing_key": 0, "peer_tokens": 0, "oidc_client_secrets": 0, "integration_secrets": 0,
              "mfa_secrets": 0}

    def again(where: str, token: str) -> str:
        try:
            return rewrap_token(settings, token)
        except SecretsError as exc:
            raise SecretsError(f"{where} cannot be decrypted with ATLAS_SECRET_KEY or ATLAS_SECRET_KEY_PREVIOUS; "
                               "nothing was changed") from exc

    # Hold the locks a signing-key rotation and the first-start encryption take, in their order, so this
    # never writes back a key another worker has just replaced. The order (feed_seq row, then advisory
    # 727275) is the one feed.append and key_rotation.rotate use. Only a still-plaintext key being
    # encrypted by a starting node at the same moment takes them the other way round; PostgreSQL then
    # aborts one side as a deadlock and nothing is half-written. Run rewrap once nodes have started.
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        session.execute(text("UPDATE node_state SET value=value WHERE key='feed_seq'"))
    elif dialect == "postgresql":
        session.execute(select(NodeState).where(NodeState.key == "feed_seq").with_for_update())
        session.execute(text("SELECT pg_advisory_xact_lock(727275)"))
    row = session.get(NodeState, signing.SIGNING_KEY, populate_existing=True)
    if row is not None:
        if row.value.startswith(signing.ENC_PREFIX):
            row.value = signing.ENC_PREFIX + again("the node signing key", row.value[len(signing.ENC_PREFIX):])
        else:
            row.value = signing.ENC_PREFIX + encrypt(settings, row.value)
        counts["signing_key"] = 1
    for peer in session.scalars(select(Peer).where(Peer.token_enc.is_not(None), Peer.token_enc != "")):
        peer.token_enc = again(f"the token of peer {peer.id}", peer.token_enc)
        counts["peer_tokens"] += 1
    for issuer in session.scalars(select(OidcIssuer)):
        issuer.client_secret_enc = again(f"the client secret of sign-in provider {issuer.id}", issuer.client_secret_enc)
        counts["oidc_client_secrets"] += 1
    keys = [integrations.PREFIX + field for field in integrations.SECRET_FIELDS]
    for setting in session.scalars(select(Setting).where(Setting.key.in_(keys))):
        if isinstance(setting.value, str) and setting.value:
            setting.value = again(f"the stored credential {setting.key}", setting.value)
            counts["integration_secrets"] += 1
    for factor in session.scalars(select(UserMfa)):
        factor.secret_enc = again(f"the authenticator secret of user {factor.user_id}", factor.secret_enc)
        counts["mfa_secrets"] += 1
    session.add(AuditEntry(actor_id=None, action="secrets_rewrapped", target="", detail=counts))
    session.flush()
    return counts
