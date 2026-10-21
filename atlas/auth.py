"""API tokens: random bearer secrets, stored only as SHA-256 hashes."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Token, User, now

SCOPES = ("read", "contribute", "curate", "sync", "metrics", "admin")
PREFIX = "atl_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    token_id: str
    name: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes


def create_token(
    session: Session, name: str, scopes: list[str], expires_in_days: int | None = None
) -> tuple[Token, str]:
    bad = [s for s in scopes if s not in SCOPES]
    if bad or not scopes:
        raise ValueError(f"scopes must be a non-empty subset of {SCOPES}")
    secret = PREFIX + secrets.token_urlsafe(32)
    row = Token(
        name=name, token_hash=hash_token(secret), scopes=sorted(set(scopes)),
        expires_at=(now() + timedelta(days=expires_in_days)) if expires_in_days else None,
    )
    session.add(row)
    session.flush()
    return row, secret


def sync_bootstrap(session: Session, secret: str | None) -> None:
    """Make the configured admin secret the only live bootstrap token; unsetting it revokes it."""
    digest = hash_token(secret) if secret else None
    found = False
    for row in session.scalars(select(Token).where(Token.name == "bootstrap-admin")):
        if row.token_hash == digest:
            found = True
            row.revoked_at = None
        elif row.revoked_at is None:
            row.revoked_at = now()
    if digest and not found:
        session.add(Token(name="bootstrap-admin", token_hash=digest, scopes=["admin"]))
    session.flush()


def authenticate(session: Session, header: str | None) -> Principal | None:
    if not header or not header.lower().startswith("bearer "):
        return None
    secret = header[7:].strip()
    if not secret:
        return None
    row = session.scalars(select(Token).where(Token.token_hash == hash_token(secret))).first()
    if row is None or row.revoked_at is not None:
        return None
    current = now()
    if row.expires_at is not None:
        exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=current.tzinfo)
        if exp <= current:
            return None
    scopes = frozenset(row.scopes)
    if row.user_id:
        owner = session.get(User, row.user_id)
        if owner is None or owner.status != "active":
            return None
        scopes &= {"read"}  # personal tokens never write: contributions go through proposals and review
    last = row.last_used_at
    if last is None or (current - (last if last.tzinfo else last.replace(tzinfo=current.tzinfo))).total_seconds() > 60:
        row.last_used_at = current
    return Principal(row.id, row.name, scopes)
