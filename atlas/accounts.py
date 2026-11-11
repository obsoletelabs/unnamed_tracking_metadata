"""Accounts, password hashing and server-side sessions."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session, object_session

from .models import AuditEntry, Setting, Token, User, UserMfa, UserSession, now
from .service import AtlasError, write_lock
from .settings import Settings

USERNAME = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}\Z")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}\Z")
_N, _R, _P = 2**15, 8, 1
_DUMMY = None
_INITIAL_ADMIN_CLAIMED = "initial_admin_claimed"


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


def _initial_admin_claimed(session: Session) -> bool:
    """True once the bootstrap has been used, including on installs from before it was recorded."""
    if session.get(Setting, _INITIAL_ADMIN_CLAIMED) is not None:
        return True
    return session.scalar(select(func.count()).select_from(User).where(
        (User.role == "admin") | User.deleted_at.is_not(None))) > 0


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
        # The configured name bootstraps the first administrator once. Deleting that account
        # frees the name, and it must not hand the role to whoever registers it next.
        first = username == settings.initial_admin.strip().lower() and not _initial_admin_claimed(session)
        if first:
            session.merge(Setting(key=_INITIAL_ADMIN_CLAIMED, value=True))
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
    sign-in within the last 15 minutes counts, but never a session opened by a group sign-in assertion: that sign-in
    happened on another node, so it proves nothing here (C10 §5.5, T8)."""
    if user.password_hash == "!":
        if login is None or login.via_group is not None:
            return False
        created = login.created_at if login.created_at.tzinfo else login.created_at.replace(tzinfo=now().tzinfo)
        return now() - created <= REAUTH_WINDOW
    return verify_password(password, user.password_hash)


def local_can_review(settings: Settings, user: User) -> bool:
    """Review rights from the local role or reputation alone (the start of §6.3 step 1)."""
    return user.role in ("curator", "admin") or (
        settings.curator_reputation > 0 and user.reputation >= settings.curator_reputation)


# ---- effective rights (CONTRACT-C10 §5.5, §6.3) ------------------------------------------------------------------
#
# The web layer computes `Rights` once per request (`effective_rights`) and pins them on the request's database
# session, so every check below it (`can_review`, `can_moderate`, `is_admin`, the service layer) sees the same
# answer: local role, plus valid signed grants, capped and gated by local policy. Nothing of it is stored.

RIGHTS_KEY = "atlas_rights"  # Session.info: {user_id: Rights} for this request
PROVENANCE_KEY = "atlas_audit_provenance"  # Session.info: (user_id, detail) merged into that user's audit rows
ROLE_RANK = {"none": 0, "moderator": 1, "curator": 2, "admin": 3, "group_admin": 4}
LOCAL_OTP = "local_otp"  # amr marker: this node's own second factor was completed in a federated session


@dataclass
class Rights:
    review: bool = False
    moderate: bool = False
    admin: bool = False
    local_admin: bool = False  # admin from the local role (never from a grant): the local_admin_only gate
    group_admin: bool = False  # may use this node's delegated issuer powers (a capped-in group_admin grant)
    federated: bool = False  # the session was opened by a group sign-in assertion
    sources: list[dict] = field(default_factory=list)  # the grants that added rights: {serial, role, issuer}
    banner: str | None = None  # why some rights are not applied in this session

    @property
    def delegated(self) -> bool:
        return bool(self.sources)


def _pinned(user: User) -> Rights | None:
    s = object_session(user)
    if s is None:
        return None
    return (s.info.get(RIGHTS_KEY) or {}).get(user.id)


def pin_rights(session: Session, user: User, rights: Rights, provenance: dict | None) -> None:
    session.info.setdefault(RIGHTS_KEY, {})[user.id] = rights
    if provenance is not None:
        session.info[PROVENANCE_KEY] = (user.id, provenance)
    else:
        session.info.pop(PROVENANCE_KEY, None)


def can_review(settings: Settings, user: User) -> bool:
    pinned = _pinned(user)
    return pinned.review if pinned is not None else local_can_review(settings, user)


def can_moderate(settings: Settings, user: User) -> bool:
    """The report queue and comment actions (§6.2, H17): reviewers, and holders of a `moderator` grant."""
    pinned = _pinned(user)
    return pinned.moderate if pinned is not None else local_can_review(settings, user)


def is_admin(user: User) -> bool:
    pinned = _pinned(user)
    return pinned.admin if pinned is not None else user.role == "admin"


def is_local_admin(user: User) -> bool:
    pinned = _pinned(user)
    return pinned.local_admin if pinned is not None else user.role == "admin"


def local_mfa_done(login: UserSession | None) -> bool:
    """This node's own second factor was completed in this session (a remote `otp` does not count)."""
    if login is None or login.mfa_verified_at is None:
        return False
    return login.via_group is None or LOCAL_OTP in (login.amr or [])


def effective_rights(session: Session, settings: Settings, user: User, login: UserSession | None,
                     own_node: str | None = None, mfa_ok: bool = True) -> Rights:
    """§6.3: the local role, plus every valid grant linked to this user here, capped, gated, and never reducing
    local rights. `mfa_ok` is `mfa.access_allowed` for this session (computed by the caller)."""
    federated = login is not None and login.via_group is not None
    r = Rights(federated=federated)
    review, admin = local_can_review(settings, user), user.role == "admin"
    if federated and settings.group_federated_privilege != "allow" and (review or admin):
        if settings.group_federated_privilege == "deny" or not local_mfa_done(login):
            review = admin = False
            r.banner = ("Sign in with your local password for administrator or reviewer rights."
                        if settings.group_federated_privilege == "deny"
                        else "Enter this node's own two-factor code for administrator or reviewer rights.")
    if mfa_ok:
        r.review, r.moderate, r.admin, r.local_admin = review or admin, review or admin, admin, admin
    if own_node is not None and settings.group_max_delegated_role != "none":
        _add_grants(session, settings, user, login, own_node, r, mfa_ok)
    return r


def _add_grants(session: Session, settings: Settings, user: User, login: UserSession | None, own_node: str,
                r: Rights, mfa_ok: bool) -> None:
    from . import groups
    from .models import GroupAccountLink

    group = groups.load(session)
    if group is None or group.status not in ("active", "resyncing"):
        return
    link = session.scalars(select(GroupAccountLink).where(GroupAccountLink.group_id == group.group_id,
                                                          GroupAccountLink.user_id == user.id)).first()
    if link is None or link.account_uid in settings.group_denied_accounts.split():
        return
    tr = groups.request_trust(session, group, own_node)  # once per request, shared with every freshness check
    grants = tr.grants_for(link.account_uid, own_node)
    if not grants:
        return
    if not mfa_ok:
        return
    if r.federated and settings.group_federated_privilege == "deny":
        r.banner = "Sign in with your local password for administrator or reviewer rights."
        return
    needs_mfa = settings.group_delegated_requires_mfa or settings.require_mfa_for_privileged or (
        r.federated and settings.group_federated_privilege == "local_mfa")
    if needs_mfa and not local_mfa_done(login):
        r.banner = "Your group role applies here after this node's own two-factor step (set it up under Security)."
        return
    cap = ROLE_RANK[settings.group_max_delegated_role]
    for g in grants:
        b = g["body"]
        issuer = None if b["issuer"] == "root" else b["issuer"]
        fresh = groups.freshness(session, group, own_node, issuer, settings.group_view_max_age_hours, tr=tr,
                                 action="grant")
        if not fresh.ok:
            r.banner = f"Your group role is paused on this node: {fresh.reason}."
            continue
        rank = ROLE_RANK[b["role"]]
        rank = rank if (rank == ROLE_RANK["group_admin"] and cap == ROLE_RANK["admin"]) else min(rank, cap)
        if rank <= 0:
            continue
        r.moderate = True
        r.review = r.review or rank >= ROLE_RANK["curator"]
        r.admin = r.admin or rank >= ROLE_RANK["admin"]
        r.group_admin = r.group_admin or rank >= ROLE_RANK["group_admin"]
        r.sources.append({"serial": b["serial"], "role": b["role"], "issuer": b["issuer"]})


def audit_provenance(group_id: str, login: UserSession | None, rights: Rights) -> dict | None:
    """The §6.4 `detail` for actions in a federated session or with delegated rights (None otherwise)."""
    if not rights.federated and not rights.delegated:
        return None
    return {"via": "group", "group_id": group_id,
            "origin_node": login.origin_node if login is not None and rights.federated else None,
            "grant_serial": rights.sources[0]["serial"] if rights.sources else None,
            "grant_serials": [g["serial"] for g in rights.sources] or None,
            "assertion_id": login.assertion_id if login is not None and rights.federated else None,
            "session": "federated" if rights.federated else "local"}


@event.listens_for(Session, "before_flush")
def _stamp_provenance(session: Session, _ctx, _instances) -> None:
    pinned = session.info.get(PROVENANCE_KEY)
    if not pinned:
        return
    user_id, detail = pinned
    for obj in session.new:
        if isinstance(obj, AuditEntry) and obj.actor_id == user_id and not (obj.detail or {}).get("via"):
            obj.detail = {**(obj.detail or {}), **detail}


def set_role(session: Session, actor: User, target: User, role: str) -> None:
    if not is_admin(actor):
        raise AtlasError("administrators only", "forbidden")
    if role not in ("user", "curator", "admin"):
        raise AtlasError("unknown role")
    if not is_local_admin(actor):
        # A role is a standing right that would outlive the grant that let the actor set it (C10 §6.4: no
        # privilege remains after revocation), so roles are changed by local administrators only.
        raise AtlasError("only a local administrator may change roles: a role would outlive the group grant "
                         "(C10 §6.3, §6.4)", "forbidden")
    if target.id == actor.id and role != "admin":
        raise AtlasError("you cannot demote yourself", "conflict")
    target.role = role
    audit(session, actor.id, "set_role", target.id, {"role": role})


def set_status(session: Session, actor: User, target: User, status: str) -> None:
    if not is_admin(actor):
        raise AtlasError("administrators only", "forbidden")
    if not is_local_admin(actor) and target.role == "admin":
        raise AtlasError("only a local administrator may suspend an administrator (C10 §6.3)", "forbidden")
    if status not in ("active", "suspended") or target.id == actor.id:
        raise AtlasError("invalid status change", "conflict")
    target.status = status
    if status == "suspended":
        end_all_sessions(session, target.id)
        for tok in session.scalars(select(Token).where(Token.user_id == target.id, Token.revoked_at.is_(None))):
            tok.revoked_at = now()
        from .models import NodeLink  # CONTRACT-C14 §6.7: a suspended account's node links are revoked

        for link in session.scalars(select(NodeLink).where(NodeLink.user_id == target.id,
                                                           NodeLink.status.in_(("active", "frozen")))):
            from . import link as links

            links.revoke(session, None, link, by=actor.id, reason="account_suspended")  # type: ignore[arg-type]
    audit(session, actor.id, "set_status", target.id, {"status": status})
