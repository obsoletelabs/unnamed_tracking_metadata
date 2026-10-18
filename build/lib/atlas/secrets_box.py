"""Encryption of secrets kept in the database, keyed by ATLAS_SECRET_KEY (never stored in the database)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .settings import Settings


class SecretsError(Exception):
    pass


def _fernet(settings: Settings) -> Fernet:
    if not settings.secret_key:
        raise SecretsError("set ATLAS_SECRET_KEY (see `atlas secret-key`) to store secrets in the database")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def available(settings: Settings) -> bool:
    return bool(settings.secret_key)


def encrypt(settings: Settings, plain: str) -> str:
    return _fernet(settings).encrypt(plain.encode()).decode()


def decrypt(settings: Settings, token: str) -> str:
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretsError("stored secret cannot be decrypted (was ATLAS_SECRET_KEY changed?)") from exc
