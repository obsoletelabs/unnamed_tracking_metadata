"""Cross-node sign-in in the owner group (CONTRACT-C10 §5.4-§5.6, A12).

The browser flow lives in `web/group_link.py` (the A11 link flow with `purpose: "signin"`, plus the IdP-initiated
`group.start` ticket). This module holds what that flow decides with: the start ticket, the relying-party checks
that are specific to sign-in, the federated `UserSession`, the per-request validity of such a session, "sign out of
all nodes", and the user's federated activity list.

Nothing here matches accounts by email, username or display name: only `(group_id, account_uid)` through
`group_account_link`. A federated session never carries more than local policy gives it (`accounts.effective_rights`),
and no password hash, credential or session token ever leaves this node.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import accounts, group_channel, group_schema, groups, ids
from . import group_crypto as gc
from .models import AuditEntry, GroupAccountLink, GroupAccountNode, OwnerGroup, User, UserMfa, UserSession, now
from .settings import Settings
from .signing import body_hash, verify_message

START_TTL = timedelta(seconds=120)
REMOTE_MFA_WINDOW = timedelta(minutes=10)
START_KEYS = {"idp_node", "rp_node", "issued_at", "not_after", "nonce"}
ACTIVITY_ACTIONS = ("group_signin", "group_signin_refused", "group_assertion_issued", "account_linked",
                    "account_unlinked", "group_signout_all", "group_sessions_ended")
_NAME_CLEAN = re.compile(r"[^a-z0-9_-]")
NOT_LINKED = "No account here is linked. Sign in with your password, then link."


class SigninRefused(Exception):
    pass


# ---- the IdP-initiated start ticket (§5.5 item 1) ---------------------------------------------------------------

def start_ticket(group: OwnerGroup, identity: Any, rp_member: dict, rp_node: str,
                 now: datetime | None = None) -> dict:
    """A `group.start` envelope signed by this node (the IdP) and sealed to the relying party's box key."""
    now = now or groups.utcnow()
    nonce = ids.ulid()
    body = {"idp_node": identity.node_id, "rp_node": rp_node, "issued_at": gc.stamp(now),
            "not_after": gc.stamp(now + START_TTL), "nonce": nonce}
    env = gc.make_object("group.start", group.group_id, f"start:{nonce}", body,
                         {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, identity.private)
    return gc.seal_request(rp_member["box_public_key"], group.group_id, rp_node, env)


def start_ticket_ok(env: Any, tr: groups.Trust, group: OwnerGroup, own: str, now: datetime | None = None) -> dict | None:
    """The ticket body when it is signed by an in-force member with `identity.assert` (current key), names this
    node, and is inside its 120-second lifetime. The nonce is checked by the caller (insert-or-fail)."""
    now = now or groups.utcnow()
    try:
        if not isinstance(env, dict) or env.get("t") != "group.start" or env.get("_v") != 1 \
                or env.get("group_id") != group.group_id or not isinstance(env.get("body"), dict):
            return None
        b, signer = env["body"], env["signer"]
        if set(b) != START_KEYS or b["rp_node"] != own or b["idp_node"] == own or signer.get("kind") != "node" \
                or signer.get("id") != b["idp_node"] or env["id"] != f"start:{b['nonce']}":
            return None
        if not isinstance(b["nonce"], str) or not group_schema.ULID.match(b["nonce"]) or body_hash(b) != env["body_hash"]:
            return None
        cert = tr.in_force(b["idp_node"])
        if cert is None or "identity.assert" not in cert["body"]["scopes"]:
            return None
        key = tr.current_key(b["idp_node"])
        if key is None or key[0] != signer["key_id"] or not verify_message(
                key[1], gc.DOMAINS["group.start"], gc.object_parts(env), env["sig"]):
            return None
        ia, na = gc.parse_time(b["issued_at"]), gc.parse_time(b["not_after"])
        if ia > now + groups.SKEW or now > na or na - ia > START_TTL:
            return None
        return b
    except (KeyError, TypeError, AttributeError, gc.GroupCryptoError, ValueError):
        return None


# ---- who may sign in from where ----------------------------------------------------------------------------------

def asserting_member(tr: groups.Trust, node_id: str, own: str) -> dict | None:
    """The in-force certificate of a member that may assert sign-ins here (§5.4), or None."""
    if node_id == own:
        return None
    cert = tr.in_force(node_id)
    return cert if cert is not None and "identity.assert" in cert["body"]["scopes"] else None


def login_page_members(session: Session, settings: Settings, identity: Any) -> list[dict]:
    """§5.5 item 2 (opt-in, H27): one button per member with `identity.assert` and at least one account linked
    both here and there. Empty unless `group_signin` and `group_signin_public` are both on."""
    if not (settings.group_signin and settings.group_signin_public):
        return []
    group = groups.active(session)
    if group is None or group.status != "active":
        return []
    tr = groups.trust(session, group, own_node=identity.node_id)
    linked_here = select(GroupAccountLink.account_uid).where(GroupAccountLink.group_id == group.group_id)
    out = []
    for node, cert in sorted(tr.members().items(), key=lambda kv: kv[1]["body"]["member"]["name"].casefold()):
        if asserting_member(tr, node, identity.node_id) is None:
            continue
        held = session.scalar(select(func.count()).select_from(GroupAccountNode).where(
            GroupAccountNode.group_id == group.group_id, GroupAccountNode.node_id == node,
            GroupAccountNode.status == "linked", GroupAccountNode.account_uid.in_(linked_here)))
        if held:
            out.append({"node_id": node, "fp": groups.gc_fp(node), "name": cert["body"]["member"]["name"]})
    return out


def open_targets(session: Session, identity: Any, user: User) -> list[dict]:
    """On the IdP: the members where this user's group account is linked ('Open <node> signed in', §8.1)."""
    group = groups.active(session)
    if group is None or group.status != "active":
        return []
    link = group_channel.local_link(session, group.group_id, user.id)
    if link is None:
        return []
    tr = groups.trust(session, group, own_node=identity.node_id)
    own = tr.in_force(identity.node_id)
    if own is None or "identity.assert" not in own["body"]["scopes"]:
        return []
    out = []
    for row in session.scalars(select(GroupAccountNode).where(
            GroupAccountNode.group_id == group.group_id, GroupAccountNode.account_uid == link.account_uid,
            GroupAccountNode.status == "linked", GroupAccountNode.node_id != identity.node_id)):
        cert = tr.in_force(row.node_id)
        if cert is not None:
            m = cert["body"]["member"]
            out.append({"node_id": row.node_id, "fp": groups.gc_fp(row.node_id), "name": m["name"], "url": m["url"],
                        "url_verified": bool(m.get("url_verified"))})
    return sorted(out, key=lambda r: r["name"].casefold())


# ---- the relying party, after the assertion verified (§5.4 sign-in checks, §5.5) ---------------------------------

def _provision(session: Session, hint: str) -> User:
    """A new account with no password (`oidc._create_account`'s shape). `username_hint` only suggests a name; the
    account never takes over an existing name, and no email is copied."""
    base = _NAME_CLEAN.sub("", (hint or "member").lower())[:24].strip("-_") or "member"
    if not accounts.USERNAME.match(base):
        base = "member" + base
    name = base
    while session.scalars(select(User.id).where(User.username == name)).first():
        name = f"{base[:24]}-{secrets.token_hex(2)}"
    user = User(username=name, email=None, password_hash="!", role="user", oidc_allowed=False)
    session.add(user)
    session.flush()
    return user


def signin_user(session: Session, settings: Settings, identity: Any, group: OwnerGroup,
                a: dict) -> tuple[User, bool]:
    """The local account an already-verified sign-in assertion opens: (user, provisioned now). Refuses unless a
    live link `(account_uid -> local user)` exists here and the issuer's own `account.link` is live (§5.4), or
    auto-provisioning is on and registration is open (§5.5)."""
    b = a["body"]
    if not settings.group_signin:
        raise SigninRefused("Sign-in through the group is turned off on this node.")
    uid, iss = b["account_uid"], b["iss_node"]
    link = group_channel.link_by_uid(session, group.group_id, uid)
    held = session.get(GroupAccountNode, (group.group_id, uid, iss))
    if link is None:
        known = session.scalars(select(GroupAccountNode.node_id).where(
            GroupAccountNode.group_id == group.group_id, GroupAccountNode.account_uid == uid)).first()
        if not (settings.group_auto_provision and settings.registration == "open"):
            raise SigninRefused(NOT_LINKED)
        if known is not None and (held is None or held.status != "linked"):
            raise SigninRefused(NOT_LINKED)  # the group knows this account, and not as held by the issuer
        user = _provision(session, b["username_hint"])
        session.add(GroupAccountLink(group_id=group.group_id, account_uid=uid, user_id=user.id, minted_here=False,
                                     linked_at=groups.utcnow()))
        session.flush()
        if group_channel.may_emit(session, group, identity):
            group_channel.emit_link(session, group, identity, uid, minted=False, evidence=a)
        groups.audit(session, user.id, "account_linked", user.id, {"idp_node": iss, "provisioned": True})
        return user, True
    if held is None or held.status != "linked":
        raise SigninRefused("This node has not yet confirmed that the other node holds your account. Try again "
                            "after the next sync, or sign in with your password.")
    user = session.get(User, link.user_id)
    if user is None or user.deleted_at is not None or user.status != "active":
        raise SigninRefused("This account cannot sign in here.")
    return user, False


def open_session(session: Session, settings: Settings, user: User, group: OwnerGroup,
                 a: dict) -> tuple[str, UserSession]:
    """An ordinary `UserSession` with the provenance columns (§5.5). The other node's `otp` stands in for this
    node's second factor only under `group_trust_remote_mfa`, and only when it is less than 10 minutes old."""
    b = a["body"]
    secret, row = accounts.create_session(session, settings, user)
    row.via_group, row.origin_node, row.assertion_id = group.group_id, b["iss_node"], b["jti"]
    row.amr = list(b["amr"])
    factor = session.get(UserMfa, user.id)
    if factor is not None and factor.enabled_at is not None and settings.group_trust_remote_mfa \
            and "otp" in b["amr"] and gc.parse_time(b["auth_time"]) >= now() - REMOTE_MFA_WINDOW:
        row.mfa_verified_at = now()
    session.flush()
    return secret, row


def session_still_valid(session: Session, settings: Settings, identity: Any, user: User,
                        login: UserSession) -> bool:
    """§5.6, checked on every request: the asserting node is still an in-force member with `identity.assert`, the
    account is still linked here and still held there, and group sign-in is still on. Otherwise the session is
    deleted now (there is no 'remaining session' privilege, §6.4)."""
    group = groups.load(session)
    reason = None
    if group is None or group.group_id != login.via_group or group.status in ("left", "revoked", "halted"):
        reason = "group_gone"
    elif not settings.group_signin:
        reason = "signin_off"
    else:
        tr = groups.trust(session, group, own_node=identity.node_id)
        link = group_channel.local_link(session, group.group_id, user.id)
        held = session.get(GroupAccountNode, (group.group_id, link.account_uid, login.origin_node)) if link else None
        if asserting_member(tr, login.origin_node or "", identity.node_id) is None:
            reason = "issuer_not_member"
        elif link is None:
            reason = "unlinked_here"
        elif held is None or held.status != "linked":
            reason = "unlinked_there"
    if reason is None:
        return True
    session.delete(login)
    groups.audit(session, None, "group_sessions_ended", user.id,
                 {"reason": reason, "count": 1, "user_id": user.id, "origin_node": login.origin_node})
    session.commit()  # now: the request that follows may end in LoginRequired, which rolls its session back
    return False


def end_user_federated(session: Session, user_id: str, group_id: str, origin_node: str | None, reason: str) -> int:
    """Unlink (here, or `account.unlink` from the other side): that user's federated sessions end; local stay."""
    q = delete(UserSession).where(UserSession.user_id == user_id, UserSession.via_group == group_id)
    if origin_node is not None:
        q = q.where(UserSession.origin_node == origin_node)
    count = session.execute(q.execution_options(synchronize_session=False)).rowcount or 0
    if count:
        groups.audit(session, None, "group_sessions_ended", user_id,
                     {"reason": reason, "count": count, "user_id": user_id, "origin_node": origin_node})
    return count


# ---- sign out of all nodes (§5.6) --------------------------------------------------------------------------------

def signout_all(session: Session, settings: Settings, identity: Any, user: User,
                now_: datetime | None = None) -> bool:
    """End every session of the user here and emit `account.signout_all {account_uid, before}`. Each linked node
    honours it only where the user's consent there has `signout_propagation` on. Returns whether it was emitted."""
    at = now_ or groups.utcnow()
    accounts.end_all_sessions(session, user.id)
    group = groups.active(session)
    link = group_channel.local_link(session, group.group_id, user.id) if group is not None else None
    emitted = False
    if group is not None and link is not None and group_channel.may_emit(session, group, identity):
        body = {"_v": 1, "account_uid": link.account_uid, "before": gc.stamp(at), "issued_at": gc.stamp(at)}
        group_channel._append(session, group, identity, group_channel.ensure_epoch(session, group, identity, at),
                              link.account_uid, "account.signout_all", body, at)
        emitted = True
    groups.audit(session, user.id, "group_signout_all", user.id, {"emitted": emitted})
    session.flush()
    return emitted


# ---- the user's federated activity (§8.1 /me/security) -----------------------------------------------------------

def activity(session: Session, user: User, days: int = 90) -> list[dict]:
    """Sign-ins here through another node, assertions this node issued for the user, links and unlinks,
    sign-out-everywhere events and ended federated sessions: the last 90 days of the local audit."""
    since = now() - timedelta(days=days)
    rows = session.scalars(select(AuditEntry).where(
        AuditEntry.action.in_(ACTIVITY_ACTIONS), AuditEntry.created_at >= since,
        (AuditEntry.actor_id == user.id) | (AuditEntry.target == user.id)).order_by(AuditEntry.created_at.desc())
        .limit(100))
    out = []
    names = {m.node_id: m.name for m in session.scalars(select(groups.GroupMember))}
    for r in rows:
        d = r.detail or {}
        node = d.get("idp_node") or d.get("aud_node") or d.get("origin_node")
        out.append({"when": r.created_at, "action": r.action, "node": node,
                    "node_name": names.get(node or "", ""), "fp": groups.gc_fp(node) if node and len(node) == 16
                    else "", "purpose": d.get("purpose"), "reason": d.get("reason")})
    return out
