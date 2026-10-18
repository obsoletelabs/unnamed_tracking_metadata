"""Email: queueing, SMTP delivery with retries, verification and password-reset tokens."""

from __future__ import annotations

import hashlib
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from .models import EmailToken, Outbox, User, now
from .service import AtlasError
from .settings import Settings

VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(hours=1)
MAX_ATTEMPTS = 6


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def queue(session: Session, to: str, subject: str, body: str) -> Outbox:
    row = Outbox(to_address=to[:254], subject=subject[:200], body=body, next_attempt_at=now())
    session.add(row)
    session.flush()
    return row


def issue_token(session: Session, user: User, purpose: str, email: str) -> str:
    """Create a one-time secret for `purpose`, invalidating older unused ones of the same kind."""
    session.execute(delete(EmailToken).where(EmailToken.user_id == user.id, EmailToken.purpose == purpose,
                                             EmailToken.used_at.is_(None)))
    secret = secrets.token_urlsafe(32)
    ttl = VERIFY_TTL if purpose == "verify" else RESET_TTL
    session.add(EmailToken(id=_hash(secret), user_id=user.id, purpose=purpose, email=email, expires_at=now() + ttl))
    session.flush()
    return secret


def lookup_token(session: Session, secret: str, purpose: str) -> EmailToken | None:
    row = session.get(EmailToken, _hash(secret or ""))
    if row is None or row.purpose != purpose or row.used_at is not None:
        return None
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=now().tzinfo)
    return row if exp > now() else None


def send_verification(session: Session, settings: Settings, user: User) -> bool:
    if not user.email:
        return False
    secret = issue_token(session, user, "verify", user.email)
    queue(session, user.email, f"Confirm your email for {settings.app_name}",
          f"Hello {user.username},\n\nConfirm your email address by opening this link (valid for 48 hours):\n\n"
          f"{settings.public_url}/verify/{secret}\n\nIf you did not create this account you can ignore this message.\n")
    return True


def send_reset(session: Session, settings: Settings, user: User) -> bool:
    if not user.email:
        return False
    secret = issue_token(session, user, "reset", user.email)
    queue(session, user.email, f"Reset your {settings.app_name} password",
          f"Hello {user.username},\n\nSomeone asked to reset the password for this account. To choose a new one, open "
          f"this link within one hour:\n\n{settings.public_url}/reset/{secret}\n\nIf this wasn't you, ignore this "
          f"message; your password has not changed.\n")
    return True


def notify_email_changed(session: Session, settings: Settings, user: User, old: str) -> None:
    queue(session, old, f"Your {settings.app_name} email address was changed",
          f"The email address on the account '{user.username}' was changed. If this was not you, contact the "
          f"administrators of {settings.public_url} immediately.\n")


def consume_verification(session: Session, secret: str) -> User:
    row = lookup_token(session, secret, "verify")
    if row is None:
        raise AtlasError("This confirmation link is invalid or has expired.", "not_found")
    user = session.get(User, row.user_id)
    if user is None or user.email != row.email:
        raise AtlasError("This confirmation link is for an address that is no longer on the account.", "conflict")
    user.email_verified_at = now()
    row.used_at = now()
    return user


def is_verified(settings: Settings, user: User) -> bool:
    """Whether this user may contribute under the node's email policy."""
    return (settings.verification_mode() != "required" or user.role == "admin"
            or user.email_verified_at is not None)


# ---- delivery ----------------------------------------------------------------------------------------

def _send_smtp(settings: Settings, row: Outbox) -> None:
    msg = EmailMessage()
    msg["From"] = settings.mail_from if "<" in settings.mail_from else formataddr((settings.app_name, settings.mail_from))
    msg["To"] = row.to_address
    msg["Subject"] = row.subject
    msg.set_content(row.body or "")
    ctx = ssl.create_default_context()
    if settings.smtp_port == 465:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with server:
        if settings.smtp_port != 465 and settings.smtp_starttls:
            server.starttls(context=ctx)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def deliver_pending(maker: sessionmaker[Session], settings: Settings, limit: int = 20) -> int:
    """Send queued messages (smtp backend). A failure is recorded and retried with backoff."""
    if settings.mail_backend != "smtp" or not settings.smtp_host:
        return 0
    sent = 0
    with maker() as session:
        rows = session.scalars(select(Outbox).where(
            Outbox.sent_at.is_(None), Outbox.attempts < MAX_ATTEMPTS,
            (Outbox.next_attempt_at.is_(None)) | (Outbox.next_attempt_at <= now())
        ).order_by(Outbox.created_at).limit(limit)).all()
        for row in rows:
            claimed = session.execute(update(Outbox).where(Outbox.id == row.id, Outbox.attempts == row.attempts)
                                      .values(attempts=row.attempts + 1))
            session.commit()  # another worker may have claimed it; only one proceeds
            if not claimed.rowcount:
                continue
            session.refresh(row)
            try:
                _send_smtp(settings, row)
            except (OSError, smtplib.SMTPException) as exc:
                row.error = f"{type(exc).__name__}: {exc}"[:300]
                row.next_attempt_at = now() + timedelta(minutes=min(60, 2 ** row.attempts))
            else:
                row.sent_at, row.error, row.body = now(), None, None  # nothing sensitive is kept once delivered
                sent += 1
            session.commit()
    return sent


def purge_old(session: Session) -> None:
    cutoff = now() - timedelta(days=7)
    session.execute(delete(EmailToken).where(EmailToken.expires_at < cutoff))
    session.execute(delete(Outbox).where(Outbox.created_at < now() - timedelta(days=30)))
