"""Accounts, password hashing and server-side sessions."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import AuditEntry, Token, User, UserMfa, UserSession, now
from .service import AtlasError, write_lock
from .settings import Settings

USERNAME = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}\Z")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}\Z")
_N, _R, _P = 2**15, 8, 1
_DUMMY = None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=128 * 1024 * 1024, dklen=len(digest) // 2)
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def audit(session: Session, actor: str | None, action: str, target: str = "", detail=None) -> None:
    session.add(AuditEntry(actor_id=actor, action=action, target=target[:128], detail=detail))


def register(session: Session, settings: Settings, username: str, password: str, email: str | None = None) -> User:
    if settings.registration != "open":
        raise AtlasError("registration is closed", "forbidden")
    username = username.strip().lower()
    if not USERNAME.match(username):
        raise AtlasError("username must be 3-32 characters: letters, digits, '_' or '-'")
    if len(password) < settings.password_min_length or len(password) > 200:
        raise AtlasError(f"password must be {settings.password_min_length}-200 characters")
    email = (email or "").strip().lower() or None
    if email and not EMAIL.match(email):
        raise AtlasError("invalid email address")
    if settings.verification_mode() == "required" and not email:
        raise AtlasError("an email address is required to create an account on this server")
    taken = session.scalars(select(User.id).where(
        (User.username == username) | ((User.email == email) if email else False))).first()
    if taken:
        raise AtlasError("username or email already in use", "conflict")
    write_lock(session)  # two simultaneous first registrations must not both count zero users
    session.flush()
    if settings.initial_admin:
        first = username == settings.initial_admin.strip().lower()
    else:
        first = session.scalar(select(func.count()).select_from(User)) == 0
    user = User(username=username, email=email, password_hash=hash_password(password),
                role="admin" if first else "user")
    session.add(user)
    session.flush()
    audit(session, user.id, "register", user.id, {"first_admin": first})
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    global _DUMMY
    user = session.scalars(select(User).where(User.username == username.strip().lower())).first()
    if user is None:  # spend the same time so unknown names are not distinguishable
        _DUMMY = _DUMMY or hash_password("dummy-password")
        verify_password(password, _DUMMY)
        return None
    if not verify_password(password, user.password_hash) or user.status != "active":
        return None
    user.last_login_at = now()
    return user


def create_session(session: Session, settings: Settings, user: User) -> tuple[str, UserSession]:
    secret = secrets.token_urlsafe(32)
    row = UserSession(id=hashlib.sha256(secret.encode()).hexdigest(), user_id=user.id, csrf=secrets.token_urlsafe(24),
                      expires_at=now() + timedelta(days=settings.session_ttl_days))
    session.add(row)
    session.flush()
    return secret, row


def session_user(session: Session, secret: str | None) -> tuple[User, UserSession] | None:
    if not secret:
        return None
    row = session.get(UserSession, hashlib.sha256(secret.encode()).hexdigest())
    if row is None:
        return None
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=now().tzinfo)
    if exp <= now():
        session.delete(row)
        return None
    user = session.get(User, row.user_id)
    if user is None or user.status != "active":
        return None
    factor = session.get(UserMfa, user.id)
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=now().tzinfo)
    if factor and factor.enabled_at and row.mfa_verified_at is None and created < now() - timedelta(minutes=10):
        session.delete(row)
        return None
    return user, row


def end_session(session: Session, secret: str | None) -> None:
    if secret:
        session.execute(delete(UserSession).where(UserSession.id == hashlib.sha256(secret.encode()).hexdigest()))


def end_all_sessions(session: Session, user_id: str) -> None:
    session.execute(delete(UserSession).where(UserSession.user_id == user_id))


def check_csrf(row: UserSession, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(row.csrf, token or "")


REAUTH_WINDOW = timedelta(minutes=15)


def reauthenticated(user: User, password: str, login: UserSession | None) -> bool:
    """Confirm a sensitive action. Accounts that sign in only through another provider have no password; for them a
    sign-in within the last 15 minutes counts."""
    if user.password_hash == "!":
        if login is None:
            return False
        created = login.created_at if login.created_at.tzinfo else login.created_at.replace(tzinfo=now().tzinfo)
        return now() - created <= REAUTH_WINDOW
    return verify_password(password, user.password_hash)


def can_review(settings: Settings, user: User) -> bool:
    return user.role in ("curator", "admin") or (
        settings.curator_reputation > 0 and user.reputation >= settings.curator_reputation)


def set_role(session: Session, actor: User, target: User, role: str) -> None:
    if actor.role != "admin":
        raise AtlasError("administrators only", "forbidden")
    if role not in ("user", "curator", "admin"):
        raise AtlasError("unknown role")
    if target.id == actor.id and role != "admin":
        raise AtlasError("you cannot demote yourself", "conflict")
    target.role = role
    audit(session, actor.id, "set_role", target.id, {"role": role})


def set_status(session: Session, actor: User, target: User, status: str) -> None:
    if actor.role != "admin":
        raise AtlasError("administrators only", "forbidden")
    if status not in ("active", "suspended") or target.id == actor.id:
        raise AtlasError("invalid status change", "conflict")
    target.status = status
    if status == "suspended":
        end_all_sessions(session, target.id)
        for tok in session.scalars(select(Token).where(Token.user_id == target.id, Token.revoked_at.is_(None))):
            tok.revoked_at = now()
    audit(session, actor.id, "set_status", target.id, {"status": status})
