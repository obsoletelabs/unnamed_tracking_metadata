"""Encrypted TOTP enrolment, single-use recovery codes and session assurance."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import accounts, secrets_box
from .models import User, UserMfa, UserSession, now
from .service import AtlasError
from .settings import Settings


def _counter() -> int:
    return int(time.time()) // 30


def totp_code(secret: str, counter: int) -> str:
    """RFC 6238 SHA-1, six digits and a 30-second counter (authenticator defaults)."""
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.digest(key, struct.pack(">Q", counter), "sha1")
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1_000_000:06d}"


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=now().tzinfo)


def required(settings: Settings, user: User) -> bool:
    return settings.require_mfa_for_privileged and accounts.can_review(settings, user)


def access_allowed(session: Session, settings: Settings, user: User, login: UserSession | None) -> bool:
    factor = session.get(UserMfa, user.id)
    if factor is None or factor.enabled_at is None:
        return not required(settings, user)
    return bool(login and login.mfa_verified_at and _utc(login.mfa_verified_at) >= _utc(factor.enabled_at))


def start(session: Session, settings: Settings, user: User, login_id: str) -> UserMfa:
    factor = session.get(UserMfa, user.id)
    if factor and factor.enabled_at:
        raise AtlasError("Two-factor sign-in is already enabled.", "conflict")
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    encrypted = secrets_box.encrypt(settings, secret)
    if factor is None:
        factor = UserMfa(user_id=user.id, secret_enc=encrypted, setup_session_id=login_id)
        session.add(factor)
    else:
        factor.secret_enc, factor.created_at = encrypted, now()
        factor.setup_session_id = login_id
        factor.last_step, factor.recovery_hashes = None, []
        factor.revision += 1
    session.flush()
    return factor


def verify(session: Session, settings: Settings, user_id: str, code: str, *, pending: bool = False,
           login_id: str = "") -> bool:
    """Consume one time step or recovery code atomically, including under concurrent requests."""
    if session.get_bind().dialect.name == "sqlite":
        # Obtain SQLite's writer lock before reading the one-use state.
        session.execute(update(UserMfa).where(UserMfa.user_id == user_id).values(revision=UserMfa.revision))
    factor = session.scalars(select(UserMfa).where(UserMfa.user_id == user_id).with_for_update()
                             .execution_options(populate_existing=True)).first()
    if factor is None or (factor.enabled_at is None and not pending):
        return False
    if pending and (factor.enabled_at is not None or factor.setup_session_id != login_id
                    or _utc(factor.created_at) < now() - timedelta(minutes=10)):
        return False
    supplied = code.strip().replace("-", "").replace(" ", "")
    values = {"revision": factor.revision + 1}
    if len(supplied) == 6 and supplied.isascii() and supplied.isdigit():
        secret = secrets_box.decrypt(settings, factor.secret_enc)
        counter = _counter()
        matches = [step for step in (counter - 1, counter, counter + 1) if step >= 0
                   and (factor.last_step is None or step > factor.last_step)
                   and hmac.compare_digest(totp_code(secret, step), supplied)]
        if not matches:
            return False
        values["last_step"] = max(matches)
    elif not pending and len(supplied) == 20 and all(c in "0123456789abcdef" for c in supplied.lower()):
        digest = hashlib.sha256(supplied.lower().encode()).hexdigest()
        remaining = [stored for stored in factor.recovery_hashes if not hmac.compare_digest(stored, digest)]
        if len(remaining) == len(factor.recovery_hashes):
            return False
        values["recovery_hashes"] = remaining
    else:
        return False
    changed = session.execute(update(UserMfa).where(UserMfa.user_id == user_id, UserMfa.revision == factor.revision)
                              .values(**values).execution_options(synchronize_session=False)).rowcount
    session.expire(factor)
    return changed == 1


def recovery_codes(factor: UserMfa) -> list[str]:
    codes = [secrets.token_hex(10) for _ in range(10)]
    factor.recovery_hashes = [hashlib.sha256(code.encode()).hexdigest() for code in codes]
    return ["-".join(code[i:i + 5] for i in range(0, 20, 5)) for code in codes]
