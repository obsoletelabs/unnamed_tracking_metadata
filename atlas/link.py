"""Node links, main side (CONTRACT-C14 Part C, slice C14-3m).

A local node links to an account on this (main) node through the OAuth device grant (RFC 8628), registering a
per-main Ed25519 link key with a proof of possession over a server challenge that is consumed once (C14-7). The
account owner approves on this node by typing the node's per-flow confirmation code (C14-5) after a fresh local
authentication. Every later request is DPoP-bound (RFC 9449): the proof key must equal BOTH the token's `cnf.jkt`
and the link's one active key, at the link's current key epoch (C14-6); rotation is append-only and atomic, a fork
freezes the link. The subject is pairwise (C14-11) and the node's contributions are node-signed envelopes that
become ordinary proposals under this node's review rules (`proposals.create`), with per-link, per-key, per-account
and main-wide limits (C14-12). Every new link and every relink starts on probation (H73, D2, C14-8): all its
contributions are reviewed until the link itself earns trust. Batches are linearised against revocation and
replays get their stored answer (C14-9). Revocation states and flags follow C14-10.

`link_enabled` is on by default (H73) and takes effect only on an authority (`active`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import accounts, dpop, key_rotation, ratelimit, secrets_box, wire
from . import link_crypto as lc
from .models import (
    Canonical,
    LinkBannedKey,
    LinkChallenge,
    LinkContributionAnswer,
    LinkDevice,
    LinkNonce,
    LinkRefresh,
    NodeLink,
    NodeState,
    OidcAccess,
    Proposal,
    User,
    UserSession,
    now,
)
from .service import AtlasError, Ctx, write_lock
from .signing import ID_PATTERN, canonical_json, load_identity

log = logging.getLogger("atlas.link")

CLIENT_ID = "atlas-node"
REQUEST_SCOPES = frozenset({"openid", "atlas.link", "atlas.contribute"})
GRANT_SCOPES = ["read", "contribute"]
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"  # RFC 8628 §6.1 consonants (~34.6 bits for 8 characters)
CHALLENGE_TTL = timedelta(seconds=120)
DEVICE_TTL = timedelta(seconds=600)
INTERVAL = 5
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=90)
GRANT_TTL = timedelta(days=365)
ID_TTL = timedelta(minutes=10)
RECENT_AUTH = timedelta(minutes=5)
PAIRWISE_KEY = "link_pairwise_key"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
#: H71: per link. The grant carries them; a main can lower them for one link.
DEFAULT_LIMITS = {"per_minute": 10, "per_day": 200, "max_pending": 50}
KEY_DAILY = 400  # `link-key:<node_id>` across every link of one key
ACCOUNT_DAILY, ACCOUNT_PENDING = 400, 100  # `link-acct:<user_id>` (Review #1 C14-12)
NEW_KEY_DAILY = 20  # `link-new-key`
APPROVALS_PER_ACCOUNT_DAY = 10
CODE_ENTRY_PER_IP = (30, 600)
UCODE_MISS_GLOBAL = (300, 600)  # an alarm, never a main-wide pause (critic of Review #1)
UCODE_MISS_ACCOUNT = (20, 3600)
CONFIRM_ATTEMPTS = 5
TOKEN_PER_JKT = 60
BATCH_MAX = 20
RETRYABLE = ("daily_limit", "rate_limited", "too_many_pending")
FINAL = ("invalid", "unknown_game", "kind_not_allowed", "bad_signature", "email_unverified", "suspended",
         "link_mismatch", "seq_backwards")
KINDS = ("edit", "new_game", "link_ids", "artwork", "artwork_default", "achievements")
_HEX16 = re.compile(r"^" + ID_PATTERN + r"\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")
_CID = re.compile(r"^atlas:[0-9a-z]{26}\Z")
_B64KEY = re.compile(r"^[A-Za-z0-9+/]{43}=\Z")


def active(settings: Any) -> bool:
    """Links work only on an authority with `link_enabled` (on by default, H73)."""
    return getattr(settings, "role", "") == "authority" and bool(getattr(settings, "link_enabled", False))


class LinkError(Exception):
    """An OAuth-style refusal: `error` is the wire code, `status` the HTTP status."""

    def __init__(self, error: str, status: int = 400, description: str = "", retry_after: int | None = None):
        super().__init__(error)
        self.error, self.status, self.description, self.retry_after = error, status, description, retry_after

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.error}
        if self.description:
            out["error_description"] = self.description
        return out


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=now().tzinfo)


def _bulk(session: Session, stmt: Any) -> Any:
    """An ORM bulk UPDATE that refreshes loaded rows from the database (timezone-aware columns compare badly in
    Python on SQLite, so `evaluate` is not used)."""
    return session.execute(stmt, execution_options={"synchronize_session": "fetch"})


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def stamp(dt: datetime) -> str:
    from .group_crypto import stamp as gstamp

    return gstamp(dt)


# ---- the pairwise key (§6.4) ------------------------------------------------------------------------------------

def pairwise_key(session: Session, settings: Any) -> bytes:
    """32 random bytes kept in `node_state["link_pairwise_key"]`, sealed like the node signing key (`enc:v1:`
    when ATLAS_SECRET_KEY is set), in backups and in `atlas secrets rewrap`. Losing it changes every subject."""
    from .signing import ENC_PREFIX

    row = session.get(NodeState, PAIRWISE_KEY)
    if row is None:
        raw = base64.b64encode(secrets.token_bytes(32)).decode()
        value = ENC_PREFIX + secrets_box.encrypt(settings, raw) if secrets_box.available(settings) else raw
        try:
            with session.begin_nested():
                session.add(NodeState(key=PAIRWISE_KEY, value=value))
        except IntegrityError:
            pass
        row = session.get(NodeState, PAIRWISE_KEY, populate_existing=True)
    value = row.value  # type: ignore[union-attr]
    if value.startswith(ENC_PREFIX):
        value = secrets_box.decrypt(settings, value[len(ENC_PREFIX):])
    key = base64.b64decode(value)
    if len(key) != 32:
        raise ValueError("link_pairwise_key is not 32 bytes")
    return key


def subject_for(session: Session, settings: Any, user_id: str, node_id: str) -> str:
    from .oidc import pairwise_subject

    return pairwise_subject(pairwise_key(session, settings), user_id, node_id)


def user_code_hmac(key: bytes, user_code: str) -> str:
    return hmac.new(key, lc.UCODE_DOMAIN + user_code.encode(), hashlib.sha256).hexdigest()


def normal_code(text: str) -> str | None:
    """`wdjb-mjht`, `WDJB MJHT` -> `WDJBMJHT`; None unless 8 characters of the consonant alphabet."""
    code = re.sub(r"[\s-]", "", (text or "").upper())
    return code if len(code) == 8 and all(c in USER_CODE_ALPHABET for c in code) else None


def new_code() -> str:
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))


def show_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def fingerprint(node_id: str) -> str:
    return " ".join(node_id[i:i + 4] for i in range(0, len(node_id), 4))


# ---- DPoP proof ids (§6.5) ----------------------------------------------------------------------------------------

def record_proof(session: Session, proof: dpop.Proof) -> None:
    """Insert `(jkt, jti)`; the primary key makes a second use a duplicate key, which is a replay."""
    try:
        with session.begin_nested():
            session.add(LinkNonce(jkt=proof.jkt, jti=proof.jti, seen_at=now()))
    except IntegrityError as exc:
        raise dpop.DpopError("replayed_proof") from exc


# ---- discovery (§6.2) ---------------------------------------------------------------------------------------------

def discovery_members(ctx: Ctx) -> dict[str, Any]:
    """What `/.well-known/openid-configuration` gains while links are active. No `sync` member (H68)."""
    if not active(ctx.settings):
        return {}
    base = ctx.settings.public_url
    return {"device_authorization_endpoint": f"{base}/oidc/device",
            "atlas_link_challenge_endpoint": f"{base}/oidc/device/challenge",
            "revocation_endpoint": f"{base}/oidc/revoke",
            "dpop_signing_alg_values_supported": ["EdDSA"],
            # C16 §22.10: `wire` is the range this build speaks; `v` stays for v0 link clients (never changes).
            "atlas_link": {"v": 1, "wire": wire.DEFAULT.advertised(), "endpoint": f"{base}/api/v2/link",
                           "contributions": True,
                           "name": str(ctx.settings.app_name)[:80]}}


# ---- §6.3 step 0: the registration challenge ------------------------------------------------------------------------

def challenge(session: Session) -> dict[str, Any]:
    value = base64.b64encode(secrets.token_bytes(32)).decode()
    session.add(LinkChallenge(hash=sha(value), expires_at=now() + CHALLENGE_TTL))
    session.flush()
    return {"challenge": value, "expires_in": int(CHALLENGE_TTL.total_seconds())}


def _consume_challenge(session: Session, value: str) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        return False
    t = now()
    res = _bulk(session, update(LinkChallenge).where(
        LinkChallenge.hash == sha(value), LinkChallenge.used_at.is_(None), LinkChallenge.expires_at > t)
        .values(used_at=t))
    return res.rowcount == 1


# ---- §6.3 step 1: device authorization ----------------------------------------------------------------------------

def _scopes(raw: str) -> str:
    scopes = set((raw or "").split())
    if not scopes or not scopes <= REQUEST_SCOPES or not {"openid", "atlas.link"} <= scopes:
        raise LinkError("invalid_scope", description="scope is openid atlas.link [atlas.contribute]")
    return " ".join(sorted(scopes))


def parse_node(raw: str) -> dict[str, Any]:
    """The `atlas_node` form field: canonical JSON of the node's registration (§6.3 step 1)."""
    try:
        node = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise LinkError("invalid_request", description="atlas_node is not JSON") from exc
    if not isinstance(node, dict) or len(raw) > 64 * 1024:
        raise LinkError("invalid_request", description="atlas_node is an object")
    required = {"node_id", "root_public_key", "key_id", "public_key", "name", "public_url", "rotations",
                "confirm_hash"}
    if not required <= set(node) or set(node) - required - {"role", "relink"}:
        raise LinkError("invalid_request", description="atlas_node has unexpected or missing members")
    ok = (isinstance(node["node_id"], str) and _HEX16.match(node["node_id"])
          and isinstance(node["key_id"], str) and _HEX16.match(node["key_id"])
          and isinstance(node["root_public_key"], str) and _B64KEY.match(node["root_public_key"])
          and isinstance(node["public_key"], str) and _B64KEY.match(node["public_key"])
          and isinstance(node["name"], str) and 0 < len(node["name"].strip()) <= 80
          and (node["public_url"] is None or (isinstance(node["public_url"], str) and len(node["public_url"]) <= 300
                                              and node["public_url"].startswith(("https://", "http://"))))
          and isinstance(node["rotations"], list)
          and isinstance(node["confirm_hash"], str) and _HEX64.match(node["confirm_hash"])
          and (node.get("relink") is None or (isinstance(node["relink"], str) and _ULID.match(node["relink"])))
          and (node.get("role") is None or node["role"] in ("authority", "mirror", "local")))
    if not ok:
        raise LinkError("invalid_request", description="atlas_node is malformed")
    if canonical_json(node).decode() != raw:
        raise LinkError("invalid_request", description="atlas_node must be canonical JSON")
    return node


def _history_keys(node: dict[str, Any]) -> dict[str, str]:
    """Validate the posted link key history from its root to `key_id`; returns {key_id: public}."""
    if not lc.key_id_matches(node["root_public_key"], node["node_id"]):
        raise LinkError("invalid_request", description="node_id is not the root key's id")
    try:
        _, keys = key_rotation.validate_history(node["node_id"], node["root_public_key"], node["rotations"],
                                                node["key_id"])
    except key_rotation.RotationError as exc:
        raise LinkError("invalid_request", description=f"key history refused: {exc}") from exc
    if keys.get(node["key_id"]) != node["public_key"]:
        raise LinkError("invalid_request", description="public_key is not the history's current key")
    return keys


def banned(session: Session, key_ids: list[str]) -> bool:
    return bool(key_ids) and session.scalars(select(LinkBannedKey.node_id).where(
        LinkBannedKey.node_id.in_(key_ids))).first() is not None


def device_authorize(session: Session, ctx: Ctx, form: dict[str, str], proof_header: str | None) -> dict[str, Any]:
    """`POST /oidc/device`. Checks (§6.3 step 1): client, scope, the registration, the key history from its root,
    the PoP over the challenge (consumed atomically), the DPoP proof by the same key (its `jkt` is stored), bans,
    and that the node is not an authority. Answers the RFC 8628 device authorization response."""
    issuer = ctx.settings.public_url
    if form.get("client_id") != CLIENT_ID:
        raise LinkError("invalid_client", 401)
    scope = _scopes(form.get("scope", ""))
    node = parse_node(form.get("atlas_node", ""))
    if node.get("role") == "authority":
        raise LinkError("invalid_request", description="node_is_authority")
    keys = _history_keys(node)
    try:
        proof = dpop.verify_proof(proof_header, htm="POST", htu=f"{issuer}/oidc/device")
    except dpop.DpopError as exc:
        raise LinkError("invalid_dpop_proof", description=exc.reason) from exc
    if proof.jkt != dpop.jkt_of(node["public_key"]):
        raise LinkError("invalid_dpop_proof", description="the proof is not by the registered key")
    challenge_value = form.get("atlas_challenge", "")
    if not lc.verify_pop(node["public_key"], lc.registration_parts(issuer, node, challenge_value),
                         form.get("atlas_node_pop", "")):
        raise LinkError("invalid_request", description="the proof of possession does not verify")
    if not _consume_challenge(session, challenge_value):
        raise LinkError("invalid_request", description="unknown, expired or used challenge")
    try:
        record_proof(session, proof)
    except dpop.DpopError as exc:
        raise LinkError("invalid_dpop_proof", description=exc.reason) from exc
    if banned(session, list(keys)):
        raise LinkError("access_denied", 403, description="this node key may not link here")
    key = pairwise_key(session, ctx.settings)
    device_code = secrets.token_urlsafe(32)
    for _ in range(5):
        user_code = new_code()
        digest = user_code_hmac(key, user_code)
        if session.scalars(select(LinkDevice.device_code_hash).where(LinkDevice.user_code_hmac == digest)).first():
            continue
        break
    session.add(LinkDevice(device_code_hash=sha(device_code), user_code_hmac=digest, confirm_hash=node["confirm_hash"],
                           jkt=proof.jkt, node=node, scopes=scope, status="pending", ip=form.get("_ip", "")[:64],
                           interval=INTERVAL, created_at=now(), expires_at=now() + DEVICE_TTL))
    session.flush()
    accounts.audit(session, None, "link_start", node["node_id"], {"key_id": node["key_id"]})
    shown = show_code(user_code)
    return {"device_code": device_code, "user_code": shown, "verification_uri": f"{issuer}/link",
            "verification_uri_complete": f"{issuer}/link?code={shown}",
            "expires_in": int(DEVICE_TTL.total_seconds()), "interval": INTERVAL}


# ---- §6.3 step 3: the approval on this node -----------------------------------------------------------------------

def find_device(session: Session, settings: Any, user_code: str) -> LinkDevice | None:
    code = normal_code(user_code)
    if code is None:
        return None
    row = session.scalars(select(LinkDevice).where(
        LinkDevice.user_code_hmac == user_code_hmac(pairwise_key(session, settings), code))).first()
    if row is None or row.status != "pending" or _utc(row.expires_at) <= now():  # type: ignore[operator]
        return None
    return row


def code_miss(session: Session, settings: Any, user: User) -> dict[str, Any]:
    """Count an unknown or expired user code (§6.3, Review #1 C14-7, critic of Review #1): per account (20 per hour,
    then refused) and main-wide (300 per 10 minutes: an alarm for admins plus a 429 with `Retry-After: 2` for later
    misses; code entry is never paused for everyone)."""
    acct_ok, retry = ratelimit.take(session, [(f"link-ucode-miss-acct:{user.id}", UCODE_MISS_ACCOUNT[0],
                                               UCODE_MISS_ACCOUNT[1])])
    global_ok, _ = ratelimit.take(session, [("link-ucode-miss", UCODE_MISS_GLOBAL[0], UCODE_MISS_GLOBAL[1])])
    if not global_ok:
        session.merge(NodeState(key="link_ucode_alarm", value=stamp(now())))
    return {"account_blocked": not acct_ok, "retry_after": retry, "delay": 0 if global_ok else 2}


def alarm(session: Session) -> datetime | None:
    """When the main-wide unknown-code alarm last fired (shown to admins for an hour)."""
    row = session.get(NodeState, "link_ucode_alarm")
    if row is None:
        return None
    try:
        when = datetime.fromisoformat(row.value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if now() - when < timedelta(hours=1) else None


def code_entry_refusal(settings: Any, user: User, login: UserSession | None) -> str | None:
    """Why this session may not enter or approve link codes (§6.3 step 3), or None."""
    if login is not None and login.via_group is not None and not settings.link_federated_approval:
        return "Sign in on this site itself to link a node: a session opened through another node cannot approve " \
               "links here."
    created = _utc(user.created_at)
    if created is not None and now() - created < timedelta(hours=24):
        return "Accounts younger than one day cannot link nodes yet."
    return None


def recent_auth_ok(session: Session, settings: Any, user: User, login: UserSession | None, password: str,
                   mfa_code: str) -> bool:
    """§6.3 step 3: a password entry (and the second factor when the account has one) in this request, which is
    within the last five minutes by construction. An account without a password needs a sign-in on this node in
    the last five minutes, never one opened through another node."""
    from . import mfa
    from .models import UserMfa

    if user.password_hash == "!":
        if login is None or login.via_group is not None:
            return False
        created = _utc(login.created_at)
        if created is None or now() - created > RECENT_AUTH:
            return False
    elif not accounts.verify_password(password or "", user.password_hash):
        return False
    factor = session.get(UserMfa, user.id)
    if factor is not None and factor.enabled_at is not None:
        return mfa.verify(session, settings, user.id, mfa_code or "")
    return True


def approve(session: Session, ctx: Ctx, user: User, device: LinkDevice, confirm: str, *, show_name: bool) -> None:
    """Mark the flow approved by `user` once the confirmation code from the node's screen matches (constant time).
    Five wrong codes deny the flow. The caller checked CSRF, POST and recent authentication."""
    code = normal_code(confirm)
    typed = hashlib.sha256((code or "").encode()).hexdigest()
    if code is None or not hmac.compare_digest(typed, device.confirm_hash):
        device.confirm_attempts += 1
        if device.confirm_attempts >= CONFIRM_ATTEMPTS:
            device.status = "denied"
            accounts.audit(session, user.id, "link_deny", device.node.get("node_id", ""), {"reason": "confirm"})
        session.flush()
        raise AtlasError("That confirmation code does not match the one on your node's screen.", "invalid",
                         reason="confirm_mismatch", left=max(0, CONFIRM_ATTEMPTS - device.confirm_attempts))
    node = device.node
    relink = node.get("relink")
    existing = session.scalars(select(NodeLink).where(NodeLink.user_id == user.id,
                                                      NodeLink.node_id == node["node_id"])).first()
    if relink and (existing is None or existing.id != relink):
        raise AtlasError("This node asks to replace a link your account does not have.", "conflict",
                         reason="relink_mismatch")
    if existing is not None and existing.status == "banned":
        raise AtlasError("This node key may not link here.", "forbidden", reason="banned")
    if existing is not None and existing.status == "frozen" and not relink:
        raise AtlasError("This node's link is frozen because its key may have been stolen. Start a relink from "
                         "the node (Admin -> Main Atlas -> Relink).", "conflict", reason="frozen")
    windows = [(f"link-approve:{user.id}", APPROVALS_PER_ACCOUNT_DAY, 86400)]
    if existing is None:
        windows.append(("link-main-new", max(1, ctx.settings.link_main_new_daily), 86400))
        seen = session.scalars(select(NodeLink.id).where(NodeLink.node_id == node["node_id"])).first()
        if seen is None:
            windows.append(("link-new-key", NEW_KEY_DAILY, 86400))
    if existing is None and ctx.settings.link_main_new_daily == 0:
        raise AtlasError("This site accepts no new links today.", "conflict", reason="rate_limited")
    ok, retry = ratelimit.take(session, windows)
    if not ok:
        raise AtlasError(f"Too many links today. Try again in {retry // 60 + 1} minutes.", "conflict",
                         reason="rate_limited", retry_after=retry)
    device.status, device.user_id, device.show_name = "approved", user.id, bool(show_name)
    device.link_id = existing.id if existing is not None else None
    accounts.audit(session, user.id, "link_approve", node["node_id"], {"key_id": node["key_id"],
                                                                         "relink": bool(existing)})
    session.flush()


def deny(session: Session, user: User, device: LinkDevice) -> None:
    device.status = "denied"
    accounts.audit(session, user.id, "link_deny", device.node.get("node_id", ""), {"reason": "user"})
    session.flush()


# ---- §6.3 step 4 and §6.5: tokens ------------------------------------------------------------------------------------

def _proof_at_token(session: Session, ctx: Ctx, proof_header: str | None) -> dpop.Proof:
    try:
        proof = dpop.verify_proof(proof_header, htm="POST", htu=f"{ctx.settings.public_url}/oidc/token")
    except dpop.DpopError as exc:
        raise LinkError("invalid_dpop_proof", description=exc.reason) from exc
    ok, retry = ratelimit.take(session, [(f"link-token:{proof.jkt}", TOKEN_PER_JKT, 60)])
    if not ok:
        raise LinkError("slow_down", 429, retry_after=retry)
    try:
        record_proof(session, proof)
    except dpop.DpopError as exc:
        raise LinkError("invalid_dpop_proof", description=exc.reason) from exc
    return proof


def token(session: Session, ctx: Ctx, form: dict[str, str], proof_header: str | None) -> dict[str, Any]:
    """`POST /oidc/token` for `client_id=atlas-node`: the device-code grant and the refresh-token grant."""
    if form.get("client_id") != CLIENT_ID:
        raise LinkError("invalid_client", 401)
    grant = form.get("grant_type")
    if grant == DEVICE_GRANT:
        return _device_token(session, ctx, form.get("device_code", ""), proof_header)
    if grant == "refresh_token":
        return _refresh(session, ctx, form.get("refresh_token", ""), proof_header)
    raise LinkError("unsupported_grant_type")


def _device_token(session: Session, ctx: Ctx, device_code: str, proof_header: str | None) -> dict[str, Any]:
    proof = _proof_at_token(session, ctx, proof_header)
    row = session.get(LinkDevice, sha(device_code or ""))
    if row is None or not hmac.compare_digest(row.jkt, proof.jkt):
        raise LinkError("invalid_grant")  # a poll with another key is refused (Review #1 C14-7)
    if row.status == "used":
        raise LinkError("invalid_grant")
    if _utc(row.expires_at) <= now():  # type: ignore[operator]
        raise LinkError("expired_token")
    if row.status == "denied":
        raise LinkError("access_denied")
    if row.status == "pending":
        last, row.last_poll_at = _utc(row.last_poll_at), now()
        if last is not None and (now() - last).total_seconds() < row.interval:
            row.interval += 5
            session.flush()
            raise LinkError("slow_down")
        session.flush()
        raise LinkError("authorization_pending")
    consumed = _bulk(session, update(LinkDevice).where(
        LinkDevice.device_code_hash == row.device_code_hash, LinkDevice.status == "approved")
        .values(status="used")).rowcount
    if consumed != 1:  # a concurrent poll consumed it first
        raise LinkError("invalid_grant")
    user = session.get(User, row.user_id)
    if user is None or user.status != "active" or user.deleted_at is not None:
        raise LinkError("access_denied")
    write_lock(session)
    link = _create_or_reactivate(session, ctx, user, row)
    accounts.audit(session, user.id, "link_done", link.id, {"node_id": link.node_id, "key_epoch": link.key_epoch})
    return _issue(session, ctx, link, user, row.scopes, with_id_token=True)


def _create_or_reactivate(session: Session, ctx: Ctx, user: User, device: LinkDevice) -> NodeLink:
    """A new link, or the same row reactivated for a relink (stable subject, §6.4). Either way the link starts on
    probation (H73, D2: every relink restarts probation; C14-8)."""
    node = device.node
    keys = [node["node_id"], *[c["body"]["key_id"] for c in node["rotations"]]]
    link = session.scalars(select(NodeLink).where(NodeLink.user_id == user.id, NodeLink.node_id == node["node_id"])
                           .with_for_update()).first()
    jkt = dpop.jkt_of(node["public_key"])
    review = ctx.settings.link_review_default
    if link is None:
        link = NodeLink(id=_new_id(), user_id=user.id, node_id=node["node_id"],
                        root_public_key=node["root_public_key"], current_key_id=node["key_id"], current_jkt=jkt,
                        key_epoch=1, rotations=node["rotations"],
                        subject=subject_for(session, ctx.settings, user.id, node["node_id"]),
                        name=node["name"].strip()[:80], public_url=node["public_url"], scopes=list(GRANT_SCOPES),
                        limits=dict(DEFAULT_LIMITS), review=review, trust="probation", reputation=0,
                        show_name=device.show_name, last_seq=0, status="active", created_at=now())
        session.add(link)
        session.flush()
        return link
    if link.status == "banned" or banned(session, keys):
        raise LinkError("access_denied", 403)
    if link.status == "frozen":
        # owner-authorised relink (§6.7): the keys of the stored history the new one does not keep are banned
        old = {link.node_id, *[c["body"]["key_id"] for c in link.rotations or []]}
        for kid in sorted(old - set(keys)):
            session.merge(LinkBannedKey(node_id=kid, reason="forked", banned_at=now(), by=user.id))
    _revoke_tokens(session, link.id)
    link.current_key_id, link.current_jkt, link.rotations = node["key_id"], jkt, node["rotations"]
    link.key_epoch += 1
    link.name, link.public_url, link.show_name = node["name"].strip()[:80], node["public_url"], device.show_name
    link.status, link.trust, link.reputation = "active", "probation", 0  # D2: always restarts probation
    link.revoked_at = link.revoked_by = link.revoke_reason = None
    link.review = review if link.review != "always" else "always"
    session.flush()
    return link


def _new_id() -> str:
    from .ids import ulid

    return ulid()


def _issue(session: Session, ctx: Ctx, link: NodeLink, user: User, scope: str, *, with_id_token: bool
           ) -> dict[str, Any]:
    """Fresh DPoP-bound tokens (access 1 h, refresh 90 days, both bound to the current key and epoch) and a signed
    link grant. The id token carries the pairwise subject and `cnf.jkt`; the display name only with the H65 name
    mapping on; never an email or the internal user id."""
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    t = now()
    session.add(OidcAccess(id=sha(access), client_id=CLIENT_ID, user_id=user.id, scope=scope,
                           expires_at=t + ACCESS_TTL, link_id=link.id, key_epoch=link.key_epoch,
                           jkt=link.current_jkt))
    session.add(LinkRefresh(id=sha(refresh), link_id=link.id, jkt=link.current_jkt, key_epoch=link.key_epoch,
                            expires_at=t + REFRESH_TTL))
    identity = load_identity(session, ctx.settings)
    body = {"link_id": link.id, "subject": link.subject, "node_id": link.node_id,
            "node_root_public_key": link.root_public_key, "scopes": list(link.scopes),
            "limits": dict(link.limits), "review": effective_review(ctx, link), "trust": link.trust,
            "key_epoch": link.key_epoch, "issued_at": stamp(t), "not_after": stamp(t + GRANT_TTL)}
    grant = lc.make("atlas.link", identity.node_id, f"link:{link.id}:{link.key_epoch}:{int(t.timestamp())}", body,
                    {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, identity.private)
    out: dict[str, Any] = {"token_type": "DPoP", "access_token": access,
                           "expires_in": int(ACCESS_TTL.total_seconds()), "refresh_token": refresh, "scope": scope,
                           "atlas_link": grant}
    if with_id_token:
        from .oidc import sign_jwt

        claims: dict[str, Any] = {"iss": ctx.settings.public_url, "aud": CLIENT_ID, "sub": link.subject,
                                  "iat": int(t.timestamp()), "exp": int((t + ID_TTL).timestamp()),
                                  "atlas_link_id": link.id, "atlas_node_id": link.node_id,
                                  "cnf": {"jkt": link.current_jkt}}
        if link.show_name:
            claims["preferred_username"] = user.username
        out["id_token"] = sign_jwt(identity, claims)
    session.flush()
    return out


def effective_review(ctx: Ctx, link: NodeLink) -> str:
    return "always" if "always" in (link.review, ctx.settings.link_review_default) else "standard"


def _refresh(session: Session, ctx: Ctx, refresh_token: str, proof_header: str | None) -> dict[str, Any]:
    proof = _proof_at_token(session, ctx, proof_header)
    row = session.get(LinkRefresh, sha(refresh_token or ""))
    if row is None:
        raise LinkError("invalid_grant")
    link = lock(session, row.link_id)
    if link is None:
        raise LinkError("invalid_grant")
    if row.used_at is not None or row.revoked_at is not None:
        # §6.5: a refresh token used twice revokes the link's tokens; after a rotation it freezes the link (§6.7)
        if row.used_at is not None:
            if row.key_epoch < link.key_epoch and link.status == "active":
                freeze(session, ctx, link, "refresh_reuse_after_rotation")
            else:
                _revoke_tokens(session, link.id)
            session.commit()
        raise LinkError("invalid_grant")
    if link.status != "active":
        raise LinkError("invalid_grant")
    if not (hmac.compare_digest(proof.jkt, row.jkt) and hmac.compare_digest(proof.jkt, link.current_jkt)
            and row.key_epoch == link.key_epoch):
        raise LinkError("invalid_grant")
    if _utc(row.expires_at) <= now():  # type: ignore[operator]
        raise LinkError("invalid_grant")
    used = _bulk(session, update(LinkRefresh).where(LinkRefresh.id == row.id, LinkRefresh.used_at.is_(None))
                           .values(used_at=now())).rowcount
    if used != 1:
        raise LinkError("invalid_grant")
    user = session.get(User, link.user_id)
    if user is None or user.status != "active" or user.deleted_at is not None:
        raise LinkError("invalid_grant")
    link.last_used_at = now()
    return _issue(session, ctx, link, user, "openid atlas.contribute atlas.link", with_id_token=False)


def revoke_token(session: Session, token_value: str) -> None:
    """`POST /oidc/revoke` (RFC 7009) for link tokens: a refresh token or an access token. Always succeeds."""
    digest = sha(token_value or "")
    _bulk(session, update(LinkRefresh).where(LinkRefresh.id == digest, LinkRefresh.revoked_at.is_(None))
                    .values(revoked_at=now()))
    _bulk(session, delete(OidcAccess).where(OidcAccess.id == digest, OidcAccess.client_id == CLIENT_ID))
    session.flush()


def _revoke_tokens(session: Session, link_id: str) -> None:
    """End every token of the link at once. Access rows are expired, not deleted, so a later request that still
    presents one can be recognised (`superseded_attempt`); housekeeping deletes them an hour later."""
    t = now()
    _bulk(session, update(OidcAccess).where(OidcAccess.link_id == link_id, OidcAccess.expires_at > t)
                    .values(expires_at=t))
    _bulk(session, update(LinkRefresh).where(LinkRefresh.link_id == link_id, LinkRefresh.revoked_at.is_(None))
                    .values(revoked_at=now()))


# ---- §6.5: authenticating a resource request ---------------------------------------------------------------------

@dataclass
class Caller:
    link: NodeLink
    user: User
    access: OidcAccess
    proof: dpop.Proof


class Unauthorized(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def lock(session: Session, link_id: str) -> NodeLink | None:
    """The `node_link` row, locked for the rest of the transaction (§7.4 step 1, Review #1 C14-9): unlink, revoke,
    ban, freeze, rotation and contribution batches all take it, so they are linearised. SQLite: the writer lock
    (`write_lock`); PostgreSQL: `SELECT ... FOR UPDATE`."""
    write_lock(session)
    return session.scalars(select(NodeLink).where(NodeLink.id == link_id).with_for_update()
                           .execution_options(populate_existing=True)).first()


def authenticate(session: Session, ctx: Ctx, authorization: str | None, proof_header: str | None, htm: str,
                 htu: str, *, locked: bool = True) -> Caller:
    """Every `/api/v2/link*` request (§6.5, Review #1 C14-6): `Authorization: DPoP <token>` + a proof with `ath`
    whose key equals BOTH the token's `cnf.jkt` and the link's `current_jkt`, at the link's `key_epoch`; the link
    `active`; the proof id unseen. A superseded key is never accepted, whatever token it presents."""
    if not active(ctx.settings):
        raise Unauthorized("links_off")
    if not authorization or not authorization.startswith("DPoP ") or len(authorization) > 200:
        raise Unauthorized("missing_token")
    access_token = authorization[5:].strip()
    try:
        proof = dpop.verify_proof(proof_header, htm=htm, htu=htu, access_token=access_token, require_ath=True)
    except dpop.DpopError as exc:
        raise Unauthorized(exc.reason) from exc
    row = session.get(OidcAccess, sha(access_token))
    if row is None or row.client_id != CLIENT_ID or not row.link_id or _utc(row.expires_at) <= now():  # type: ignore[operator]
        raise Unauthorized("invalid_token")
    link = lock(session, row.link_id) if locked else session.get(NodeLink, row.link_id)
    if link is None:
        raise Unauthorized("invalid_token")
    if not (row.jkt and hmac.compare_digest(proof.jkt, row.jkt) and hmac.compare_digest(proof.jkt, link.current_jkt)
            and row.key_epoch == link.key_epoch):
        raise Unauthorized("key_mismatch")
    if link.status != "active":
        raise Unauthorized(f"link_{link.status}")
    user = session.get(User, link.user_id)
    if user is None or user.status != "active" or user.deleted_at is not None:
        raise Unauthorized("account_unavailable")
    try:
        record_proof(session, proof)
    except dpop.DpopError as exc:
        raise Unauthorized(exc.reason) from exc
    link.last_used_at = now()
    return Caller(link, user, row, proof)


def superseded_attempt(session: Session, ctx: Ctx, authorization: str | None, proof_header: str | None, htm: str,
                       htu: str) -> NodeLink | None:
    """§6.7: a rotation request signed by an already superseded key of a link, with a token of that link, means
    someone else rotated the key: the caller freezes the link. Returns the link, or None."""
    if not authorization or not authorization.startswith("DPoP "):
        return None
    access_token = authorization[5:].strip()
    try:
        proof = dpop.verify_proof(proof_header, htm=htm, htu=htu, access_token=access_token, require_ath=True)
    except dpop.DpopError:
        return None
    row = session.get(OidcAccess, sha(access_token))  # expired rows are kept for an hour (`_revoke_tokens`)
    link = session.get(NodeLink, row.link_id) if row is not None and row.link_id else None
    if link is None or link.status != "active":
        return None
    old = {dpop.jkt_of(link.root_public_key)} | {dpop.jkt_of(c["body"]["public_key"]) for c in link.rotations or []}
    old.discard(link.current_jkt)
    return link if proof.jkt in old else None


def rotation_retry(session: Session, ctx: Ctx, authorization: str | None, proof_header: str | None, htm: str,
                   htu: str, body: Any) -> dict[str, Any] | None:
    """§6.7: the node repeats the rotation it already made here because it never saw the answer (a timeout, a lost
    response, a grant it could not check). The repeat is recognised only when it is the IDENTICAL rotation: the
    posted history equals the stored one, the proof is by the key immediately before the current one with a token
    of the epoch just ended (expired rows are kept for an hour), and `new_key_pop` by the current key still
    verifies for the current epoch. Then every token of the link is revoked again and fresh ones are issued, bound
    to the CURRENT key only, so a replay by someone holding just the old key gains nothing. Anything else is not a
    retry (the caller then checks `superseded_attempt`). Returns the token answer, or None."""
    if not authorization or not authorization.startswith("DPoP ") or len(authorization) > 200 \
            or not isinstance(body, dict) or not isinstance(body.get("rotations"), list) \
            or not isinstance(body.get("new_key_pop"), str):
        return None
    access_token = authorization[5:].strip()
    try:
        proof = dpop.verify_proof(proof_header, htm=htm, htu=htu, access_token=access_token, require_ath=True)
    except dpop.DpopError:
        return None
    row = session.get(OidcAccess, sha(access_token))
    if row is None or row.client_id != CLIENT_ID or not row.link_id or not row.jkt:
        return None
    link = lock(session, row.link_id)
    stored = list(link.rotations or []) if link is not None else []
    if link is None or link.status != "active" or not stored or body["rotations"] != stored \
            or row.key_epoch != link.key_epoch - 1 or not hmac.compare_digest(proof.jkt, row.jkt):
        return None
    before = stored[-2]["body"]["public_key"] if len(stored) > 1 else link.root_public_key
    if not hmac.compare_digest(proof.jkt, dpop.jkt_of(before)):
        return None
    parts = lc.rotation_parts(ctx.settings.public_url, link.id, link.node_id, link.current_key_id, link.key_epoch)
    if not lc.verify_pop(stored[-1]["body"]["public_key"], parts, body["new_key_pop"]):
        return None
    user = session.get(User, link.user_id)
    if user is None or user.status != "active" or user.deleted_at is not None:
        return None
    try:
        record_proof(session, proof)
    except dpop.DpopError:
        return None
    _revoke_tokens(session, link.id)
    accounts.audit(session, user.id, "link_rotation_retry", link.id,
                   {"key_id": link.current_key_id, "key_epoch": link.key_epoch})
    return _issue(session, ctx, link, user, row.scope, with_id_token=False)


# ---- §6.7: rotation, freeze, revocation, bans -------------------------------------------------------------------

def rotate(session: Session, ctx: Ctx, caller: Caller, body: Any) -> dict[str, Any]:
    """`POST /api/v2/link/rotation {rotations, new_key_pop}` with a DPoP proof by the CURRENT key (§6.7, critic of
    Review #1). Under the row lock: the posted history must validate from the registered root and extend the stored
    one append-only; a diverging history is a fork and freezes the link. `new_key_pop` proves the new key. Then the
    key moves, `key_epoch += 1`, every token of the link is revoked and new ones are returned. Trust is kept."""
    link = caller.link
    if not isinstance(body, dict) or not isinstance(body.get("rotations"), list) \
            or not isinstance(body.get("new_key_pop"), str):
        raise AtlasError("rotations and new_key_pop are required", "invalid")
    posted, stored = body["rotations"], list(link.rotations or [])
    if not posted:
        raise AtlasError("the history has no new key", "invalid")
    new_kid = posted[-1].get("body", {}).get("key_id") if isinstance(posted[-1], dict) else None
    try:
        _, keys = key_rotation.validate_history(link.node_id, link.root_public_key, posted, new_kid)
    except key_rotation.RotationError as exc:
        raise AtlasError(f"key history refused: {exc}", "invalid") from exc
    if len(posted) <= len(stored) or posted[:len(stored)] != stored:
        freeze(session, ctx, link, "rotation_fork")
        raise AtlasError("the key history diverges from the one registered here; the link is frozen", "conflict",
                         reason="fork")
    new_public = keys[new_kid]
    parts = lc.rotation_parts(ctx.settings.public_url, link.id, link.node_id, new_kid, link.key_epoch + 1)
    if not lc.verify_pop(new_public, parts, body["new_key_pop"]):
        raise AtlasError("new_key_pop does not verify", "invalid")
    if banned(session, list(keys)):
        raise AtlasError("this key may not link here", "forbidden")
    _revoke_tokens(session, link.id)
    link.rotations, link.current_key_id = posted, new_kid
    link.current_jkt, link.key_epoch = dpop.jkt_of(new_public), link.key_epoch + 1
    accounts.audit(session, caller.user.id, "link_rotation", link.id, {"key_id": new_kid, "key_epoch": link.key_epoch})
    return _issue(session, ctx, link, caller.user, caller.access.scope, with_id_token=False)


def _flag_pending(session: Session, link_id: str, flag: str) -> int:
    rows = list(session.scalars(select(Proposal).where(Proposal.via_link_id == link_id,
                                                       Proposal.status == "pending")))
    for p in rows:
        if p.link_flag != "link_compromised":  # never downgrade a compromised flag
            p.link_flag = flag
    return len(rows)


def freeze(session: Session, ctx: Ctx, link: NodeLink, reason: str) -> None:
    """A fork, a rotation from a superseded key, refresh reuse after a rotation, or a reported theft (§6.7,
    Review #1 C14-6): every token refused, nothing accepted, pending proposals flagged `link_compromised`."""
    if link.status in ("revoked", "banned"):
        return
    link.status = "frozen"
    _revoke_tokens(session, link.id)
    _flag_pending(session, link.id, "link_compromised")
    accounts.audit(session, None, "link_freeze", link.id, {"reason": reason})
    from . import social

    social.notify(session, ctx, link.user_id, "link", "Your linked node's key may have been stolen. Its link is "
                  "frozen; relink it from the node to continue.", "/me/links", key=f"link-freeze:{link.id}")
    session.flush()


def revoke(session: Session, ctx: Ctx, link: NodeLink, *, by: str | None, reason: str) -> None:
    """Unlink (the account owner, an admin, the node itself, or a suspended/deleted account): immediate. Pending
    proposals stay pending (H69) and are flagged `link_revoked` (`link_compromised` for reason `compromised`)."""
    if link.status in ("revoked", "banned"):
        return
    link.status, link.revoked_at, link.revoked_by, link.revoke_reason = "revoked", now(), by, reason[:32]
    link.show_name = False  # H65: unlinking deletes the name mapping
    _revoke_tokens(session, link.id)
    _flag_pending(session, link.id, "link_compromised" if reason == "compromised" else "link_revoked")
    accounts.audit(session, by, "link_revoke", link.id, {"reason": reason[:32], "node_id": link.node_id})
    session.flush()


def ban(session: Session, ctx: Ctx, link: NodeLink, *, by: str | None, reason: str = "admin") -> None:
    """Revoke and ban every key of the link's history: none of them can link again under any account."""
    revoke(session, ctx, link, by=by, reason="compromised" if reason == "compromised" else "banned")
    for kid in {link.node_id, *[c["body"]["key_id"] for c in link.rotations or []]}:
        session.merge(LinkBannedKey(node_id=kid, reason=reason[:32], banned_at=now(), by=by))
    link.status = "banned"
    _flag_pending(session, link.id, "link_compromised")
    accounts.audit(session, by, "link_key_ban", link.id, {"node_id": link.node_id})
    session.flush()


def revoke_for_account(session: Session, ctx: Ctx, user_id: str, reason: str) -> int:
    """A suspended or deleted account loses its links (§6.7)."""
    n = 0
    for link in session.scalars(select(NodeLink).where(NodeLink.user_id == user_id, NodeLink.status.in_(
            ("active", "frozen")))):
        revoke(session, ctx, link, by=None, reason=reason)
        n += 1
    return n


def withdraw_all_pending(session: Session, user: User, link: NodeLink) -> int:
    from . import proposals

    n = 0
    for p in list(session.scalars(select(Proposal).where(Proposal.via_link_id == link.id,
                                                         Proposal.status == "pending"))):
        proposals.withdraw(session, user, p.id)
        n += 1
    return n


def adjust_reputation(session: Session, ctx: Ctx, link_id: str, delta: int) -> None:
    """§7.4 step 3 (C14-8, H73): a reviewer's decision on a link proposal moves the link's own reputation by the
    account's amount; the link leaves probation at `auto_approve_reputation` (never with 0, auto-approval off) and
    a revert below it returns it to probation. Nothing the node sends can promote it."""
    link = session.get(NodeLink, link_id)
    if link is None:
        return
    link.reputation += delta
    threshold = ctx.settings.auto_approve_reputation
    if link.trust == "probation" and threshold > 0 and link.reputation >= threshold and link.status == "active":
        link.trust = "trusted"
        accounts.audit(session, None, "link_trusted", link.id, {"reputation": link.reputation})
    elif link.trust == "trusted" and (threshold <= 0 or link.reputation < threshold):
        link.trust = "probation"
        accounts.audit(session, None, "link_probation", link.id, {"reputation": link.reputation})


# ---- §7.3/§7.4: contributions -----------------------------------------------------------------------------------

def _refused(cid: str, reason: str, retry: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": cid, "status": "refused", "reason": reason}
    if retry is not None:
        out["retry_after"] = retry
    return out


def _field_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _stale(row: Canonical | None, base: Any, payload: dict[str, Any]) -> bool:
    """§7.4 step 4: the main's record moved since `base` and a touched field changed."""
    if row is None or not isinstance(base, dict):
        return False
    version = base.get("main_version")
    if type(version) is not int or version >= (row.version or 0):
        return False
    fields = base.get("fields") if isinstance(base.get("fields"), dict) else {}
    view = row.view or {}
    for name in payload:
        seen = fields.get(name)
        if isinstance(seen, str) and seen != _field_hash(view.get(name)):
            return True
    return False


def _store(session: Session, link: NodeLink, cid: str, answer: dict[str, Any], retry: int | None = None) -> None:
    session.merge(LinkContributionAnswer(contribution_id=cid, link_id=link.id, answer=answer, at=now(),
                                         retry_until=now() + timedelta(seconds=retry) if retry else None))


def _check_limits(session: Session, ctx: Ctx, link: NodeLink) -> tuple[str, int] | None:
    """§7.5: per link (minute, day, pending), per key, per account (day, pending across all its links), main-wide.
    Counted in the database in the batch's transaction, all windows at once or none (`ratelimit.take`)."""
    limits = {**DEFAULT_LIMITS, **(link.limits or {})}
    pending = session.scalar(select(func.count()).select_from(Proposal).where(
        Proposal.via_link_id == link.id, Proposal.status == "pending")) or 0
    if pending >= int(limits["max_pending"]):
        return "too_many_pending", 3600
    acct_links = select(NodeLink.id).where(NodeLink.user_id == link.user_id).scalar_subquery()
    acct_pending = session.scalar(select(func.count()).select_from(Proposal).where(
        Proposal.via_link_id.in_(acct_links), Proposal.status == "pending")) or 0
    if acct_pending >= ACCOUNT_PENDING:
        return "too_many_pending", 3600
    ok, retry = ratelimit.take(session, [
        (f"link:{link.id}:m", int(limits["per_minute"]), 60), (f"link:{link.id}:d", int(limits["per_day"]), 86400),
        (f"link-key:{link.node_id}", KEY_DAILY, 86400), (f"link-acct:{link.user_id}", ACCOUNT_DAILY, 86400),
        ("link-main", ctx.settings.link_main_daily, 86400)])
    if not ok:
        if not ratelimit_ok_main(session, ctx):
            session.merge(NodeState(key="link_main_alarm", value=stamp(now())))
        return "rate_limited", retry
    return None


def ratelimit_ok_main(session: Session, ctx: Ctx) -> bool:
    return ratelimit.used(session, "link-main", 86400) < ctx.settings.link_main_daily


def contributions(session: Session, ctx: Ctx, caller: Caller, items: Any) -> list[dict[str, Any]]:
    """`POST /api/v2/link/contributions` (§7.4), the whole batch in the caller's transaction under the link's row
    lock (taken by `authenticate`): status, key epoch and `last_seq` are re-read under it, so a batch is either fully
    accepted before a revocation commits or refused after it. Duplicates come before limits and get their stored
    answer; the first retryable refusal stops the batch."""
    from . import proposals

    if not isinstance(items, list) or not 1 <= len(items) <= BATCH_MAX:
        raise AtlasError(f"items is a list of 1-{BATCH_MAX} contributions", "invalid")
    link, user = caller.link, caller.user
    keys = {link.node_id: link.root_public_key, **{c["body"]["key_id"]: c["body"]["public_key"]
                                                    for c in link.rotations or []}}
    current_public = keys[link.current_key_id]
    out: list[dict[str, Any]] = []
    stop: tuple[str, int] | None = None
    for obj in items:
        cid = obj.get("id") if isinstance(obj, dict) and isinstance(obj.get("id"), str) else ""
        cid = cid if re.fullmatch(rf"contrib:{link.node_id}/[0-9A-HJKMNP-TV-Z]{{26}}", cid) else ""
        if not cid:
            out.append(_refused(str(obj.get("id", ""))[:80] if isinstance(obj, dict) else "", "invalid"))
            continue
        stored = session.get(LinkContributionAnswer, cid)
        if stored is not None and stored.link_id == link.id:
            ans = dict(stored.answer)
            if ans.get("status") in ("accepted", "duplicate"):
                p = session.get(Proposal, ans.get("proposal_id"))
                out.append({"id": cid, "status": "duplicate", "proposal_id": ans.get("proposal_id"),
                            "state": p.status if p is not None else ans.get("state")})
                continue
            until = _utc(stored.retry_until)
            if ans.get("reason") in FINAL or (until is not None and until > now()):
                if until is not None:
                    ans["retry_after"] = max(1, int((until - now()).total_seconds()))
                out.append(ans)
                if ans.get("reason") in RETRYABLE:
                    stop = (ans["reason"], ans.get("retry_after", 60))
                continue
        if stop is not None:  # the batch stopped at a retryable refusal: the rest waits behind it
            ans = _refused(cid, stop[0], stop[1])
            _store(session, link, cid, ans, stop[1])
            out.append(ans)
            continue
        if not lc.verify(obj, current_public, t="atlas.contribution") or obj.get("issuer") != link.node_id \
                or obj["signer"].get("id") != link.node_id:
            ans = _refused(cid, "bad_signature")
            _store(session, link, cid, ans)
            out.append(ans)
            continue
        body = obj["body"]
        if body.get("link_id") != link.id or body.get("node_id") != link.node_id \
                or body.get("subject") != link.subject:
            ans = _refused(cid, "link_mismatch")
            _store(session, link, cid, ans)
            out.append(ans)
            continue
        seq = body.get("seq")
        if type(seq) is not int or seq <= link.last_seq:
            ans = _refused(cid, "seq_backwards")
            _store(session, link, cid, ans)
            out.append(ans)
            continue
        kind = body.get("kind")
        main_cid = body.get("canonical_id")
        payload = body.get("payload")
        if kind not in KINDS or not isinstance(payload, dict) or (kind == "new_game") != (main_cid is None) \
                or (main_cid is not None and not (isinstance(main_cid, str) and _CID.match(main_cid))):
            ans = _refused(cid, "kind_not_allowed" if kind not in KINDS else "invalid")
            _store(session, link, cid, ans)
            link.last_seq = seq
            out.append(ans)
            continue
        limited = _check_limits(session, ctx, link)
        if limited is not None:
            stop = limited
            ans = _refused(cid, limited[0], limited[1])
            _store(session, link, cid, ans, limited[1])
            out.append(ans)
            continue
        row = session.get(Canonical, main_cid) if main_cid else None
        stale = _stale(row, body.get("base"), payload)
        auto = link.trust == "trusted" and effective_review(ctx, link) == "standard"
        note = body.get("note") if isinstance(body.get("note"), str) else ""
        try:
            with session.begin_nested():
                p = proposals.create(session, ctx, user, kind, main_cid, payload, note, auto_apply=auto,
                                     via_link=link, contribution_id=cid, stale_base=stale)
        except AtlasError as exc:
            reason = exc.detail.get("reason", "invalid")
            if reason in RETRYABLE:
                stop = (reason, 3600)
                ans = _refused(cid, reason, 3600)
                _store(session, link, cid, ans, 3600)
            else:
                ans = _refused(cid, reason if reason in FINAL else "invalid")
                _store(session, link, cid, ans)
                link.last_seq = seq
            out.append(ans)
            continue
        link.last_seq = seq
        ans = {"id": cid, "status": "accepted", "proposal_id": p.id, "state": p.status}
        _store(session, link, cid, ans)
        accounts.audit(session, user.id, "contribution_accept", p.id,
                       {"via": "node_link", "link_id": link.id, "node_id": link.node_id, "contribution_id": cid})
        out.append(ans)
    session.flush()
    return out


def contribution_status(session: Session, link: NodeLink, since: str | None, limit: int = 200) -> dict[str, Any]:
    """`GET /api/v2/link/contributions?since=C`: decisions in the order they happened. The cursor is
    `<ISO time>|<proposal id>` of the last item returned."""
    when = func.coalesce(Proposal.decided_at, Proposal.created_at)
    q = select(Proposal).where(Proposal.via_link_id == link.id)
    if since:
        stamp_text, _, pid = since.partition("|")
        try:
            t = datetime.fromisoformat(stamp_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AtlasError("since is a cursor from an earlier answer", "invalid") from exc
        q = q.where(or_(when > t, (when == t) & (Proposal.id > pid)))
    rows = list(session.scalars(q.order_by(when, Proposal.id).limit(limit)))
    items = []
    for p in rows:
        items.append({"id": p.contribution_id, "proposal_id": p.id, "state": p.status,
                      "decided_at": stamp(_utc(p.decided_at)) if p.decided_at else None,  # type: ignore[arg-type]
                      "reviewer_note": (p.decision_note or "")[:500] or None,
                      "main_canonical_id": p.canonical_id, "feed_seq": None})
    nxt = None
    if rows:
        last = rows[-1]
        nxt = f"{(_utc(last.decided_at) or _utc(last.created_at)).isoformat()}|{last.id}"  # type: ignore[union-attr]
    return {"items": items, "next": nxt}


def withdraw(session: Session, caller: Caller, contribution_id: str) -> dict[str, Any]:
    from . import proposals

    p = session.scalars(select(Proposal).where(Proposal.contribution_id == contribution_id,
                                               Proposal.via_link_id == caller.link.id)).first()
    if p is None:
        raise AtlasError("unknown contribution", "not_found")
    proposals.withdraw(session, caller.user, p.id)
    accounts.audit(session, caller.user.id, "contribution_withdraw", p.id, {"contribution_id": contribution_id})
    return {"id": contribution_id, "proposal_id": p.id, "state": p.status}


def describe(session: Session, ctx: Ctx, link: NodeLink, user: User) -> dict[str, Any]:
    """`GET /api/v2/link`."""
    limits = {**DEFAULT_LIMITS, **(link.limits or {})}
    pending = session.scalar(select(func.count()).select_from(Proposal).where(
        Proposal.via_link_id == link.id, Proposal.status == "pending")) or 0
    out = {"link_id": link.id, "subject": link.subject, "scopes": list(link.scopes), "limits": limits,
           "used_today": ratelimit.used(session, f"link:{link.id}:d", 86400), "pending": pending,
           "review": effective_review(ctx, link), "trust": link.trust, "status": link.status,
           "key_epoch": link.key_epoch}
    if link.show_name:
        out["preferred_username"] = user.username
    return out


def label(link: NodeLink | None, user: User | None) -> str:
    """How the review queue and credits name a link's contributions (H65): the account's display name with the
    name mapping on, otherwise "a linked node (5f0c...)"; never an email or an internal id."""
    if link is None:
        return "a linked node"
    if link.show_name and user is not None:
        return f"{user.username} (via node {link.name})"
    return f"a linked node ({link.subject[:4]}…)"


# ---- retention (§6.4, Review #1 C14-11) -----------------------------------------------------------------------------

def purge(session: Session) -> None:
    t = now()
    _bulk(session, delete(LinkNonce).where(LinkNonce.seen_at < t - timedelta(minutes=10)))
    _bulk(session, delete(LinkChallenge).where(LinkChallenge.expires_at < t - timedelta(hours=24)))
    _bulk(session, delete(LinkDevice).where(LinkDevice.expires_at < t - timedelta(hours=24)))
    _bulk(session, delete(LinkRefresh).where(or_(LinkRefresh.expires_at < t - timedelta(days=7),
                                                  LinkRefresh.revoked_at < t - timedelta(days=7),
                                                  LinkRefresh.used_at < t - timedelta(days=7))))
    _bulk(session, update(NodeLink).where(NodeLink.status.in_(("revoked", "banned")),
                                           NodeLink.revoked_at < t - timedelta(days=90), NodeLink.name != "")
                    .values(name="", public_url=None, show_name=False))
    _bulk(session, delete(LinkContributionAnswer).where(LinkContributionAnswer.at < t - timedelta(days=90)))


def counts(session: Session) -> dict[str, int]:
    return dict(session.execute(select(NodeLink.status, func.count()).group_by(NodeLink.status)).all())


_ = Ed25519PrivateKey
