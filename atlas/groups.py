"""Owner federation groups: genesis, membership certificates, revocation, succession and freshness (CONTRACT-C10 §1).

The validity rules (`Trust.valid`, §1.4) are pure functions of the objects this node holds, evaluated at the
receiver's own time. Storage is in the 0028 tables; `group_revoked` and `group_banned_node` are append-only.
Group objects are never written to the public feed: no kind here is registered in `feed_schema` (G-V4).
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from . import group_crypto as gc
from . import key_rotation, secrets_box
from .models import (
    AuditEntry,
    GroupBannedNode,
    GroupCert,
    GroupCrl,
    GroupEnrolment,
    GroupGrant,
    GroupMember,
    GroupNonce,
    GroupRevoked,
    GroupView,
    NodeState,
    Notification,
    OwnerGroup,
    User,
    UserSession,
)
from .settings import Settings
from .signing import ENC_PREFIX, NodeIdentity, canonical_json, key_id_for

GROUP_PROTOCOL = "1.0"
MIN_GROUP_PROTOCOL = "1.0"
SCOPES = ("profile.sync", "group.relay", "identity.assert", "group.enrol", "grant.issue")
DEFAULT_SCOPES = ("profile.sync", "group.relay", "identity.assert")  # H16
DELEGATE_SCOPES = ("group.enrol", "grant.issue")
GRANT_ROLES = ("admin", "curator", "moderator")  # what a delegate may grant (§1.3)
GRANT_ALL_ROLES = ("group_admin", "admin", "curator", "moderator")  # what a grant may carry (§6.1)
ROLE_RANK = {"moderator": 1, "curator": 2, "admin": 3, "group_admin": 4}
GRANT_FIELDS = frozenset({"serial", "issuer", "parent_serial", "authorized_by_grant", "account_uid", "account_label",
                          "role", "nodes", "issued_at", "not_before", "not_after", "note"})
GRANT_AUTHORITY = ("issuer", "parent_serial", "authorized_by_grant", "account_uid", "role", "nodes", "not_before",
                   "not_after")
GRANT_DAYS = 30  # default grant lifetime (H24); the genesis `grant_max_days` (at most 90) bounds it
ACCOUNT_UID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")
REASONS = {"key_compromise": 5, "removed": 4, "left": 3, "enrolment_aborted": 2, "superseded": 1}
CASCADING = {"removed", "key_compromise", "left"}
BANNING = {"removed", "key_compromise"}
MAX_MEMBERS = 32
MAX_CRL_ENTRIES = 10_000
EXPIRY_WARNING = timedelta(days=30)
PRUNE_AFTER = timedelta(days=30)
VIEW_MIN_INTERVAL = timedelta(minutes=1)
DEFAULT_POLICY = {"max_members": 32, "max_chain_depth": 2, "cert_max_days": 365, "delegate_max_days": 90,
                  "grant_max_days": 90, "succession_delay_hours": 24}
CERT_DAYS, DELEGATE_DAYS = 365, 30
SKEW = timedelta(seconds=gc.SKEW)
ROOT_SERIAL = re.compile(r"^root/([1-9][0-9]{0,17})-([0-9a-f]{16})\Z")
NODE_SERIAL = re.compile(r"^([0-9a-f]{16})/([1-9][0-9]{0,17})\Z")
NODE_ID = re.compile(r"^[0-9a-f]{16}\Z")
RECOVERY_PENDING = "group_recovery_pending"  # NodeState key: enc:v1 recovery file awaiting download + confirm
RECOVERY_VERIFIED = "group_recovery_verified"  # NodeState key: when a recovery file holding G was verified
SUCCESSION_ROOT = "group_succession_root"  # NodeState key: {index, key_id, sealed} of a G' this node created
# Outside review #1 (C10 §C10.17). Markers are NodeState keys, so no schema change is needed (0028-0030 stay as
# deployed): the review's fixes fit the existing tables.
CRL_HELD_PREFIX = "group_crl_held:"  # + body_hash[:40]: an old-root CRL received while a succession is pending (R1-1)
SUCCESSION_CRL_MISSING = "group_succession_crl_missing"  # index of an active succession whose old_root_last_crl
#                                                          this node does not hold yet (R1-1): freshness fails
SUCCESSION_CONFIRM_PREFIX = "group_succession_confirm:"  # + index: a post-delay view from another member (R1-5)
R1_RETIRE_DONE = "group_r1_retire_done"  # the highest succession index whose old-root revocations were retired
R1_BANS_DONE = "group_r1_bans_done"  # delegate-only bans stored before R1-13 were re-derived once
VIEW_UNBACKED_PREFIX = "group_view_unbacked:"  # + node: issued_at of that member's newest view its state did not back
TRUST_EPOCH = "group_trust_epoch"  # "skipped:<stamp>" after `resync --skip` (R1-3): no waiver until a full pull


class GroupError(Exception):
    """A refused group operation; `code` maps to an HTTP status like AtlasError."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


# ---- time ---------------------------------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def t(text: Any) -> datetime:
    return gc.parse_time(text)


# ---- serials ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Serial:
    issuer: str  # 'root' or node id
    n: int
    r: str | None

    @property
    def order(self) -> tuple[int, str]:
        return self.n, self.r or ""


def parse_serial(serial: Any) -> Serial | None:
    if not isinstance(serial, str):
        return None
    if m := ROOT_SERIAL.match(serial):
        return Serial("root", int(m.group(1)), m.group(2))
    if m := NODE_SERIAL.match(serial):
        return Serial(m.group(1), int(m.group(2)), None)
    return None


def root_serial(n: int) -> str:
    return f"root/{n}-{secrets.token_hex(8)}"


# ---- genesis, successions, cancels (§1.1, §1.8) ----------------------------------------------------------------

def _recovery_ok(recovery: Any) -> bool:
    try:
        keys, threshold = recovery["keys"], recovery["threshold"]
        if type(threshold) is not int or not isinstance(keys, list) or not 1 <= len(keys) <= 9:
            return False
        if threshold * 2 <= len(keys) or threshold > len(keys):  # t > n/2 (§1.1)
            return False
        ids = [k["key_id"] for k in keys]
        return len(set(ids)) == len(ids) and all(gc.key_id_of(k["public_key"]) == k["key_id"] for k in keys)
    except (KeyError, TypeError, gc.GroupCryptoError):
        return False


def _policy_ok(policy: Any) -> bool:
    try:
        return (set(policy) == set(DEFAULT_POLICY) and all(type(v) is int for v in policy.values())
                and 1 <= policy["max_members"] <= MAX_MEMBERS and policy["max_chain_depth"] == 2
                and 1 <= policy["cert_max_days"] <= 365 and 1 <= policy["delegate_max_days"] <= 90
                and 1 <= policy["grant_max_days"] <= 90 and 0 <= policy["succession_delay_hours"] <= 24 * 30)
    except (TypeError, KeyError):
        return False


def verify_genesis(genesis: Any, group_id: str | None = None) -> bool:
    """The genesis self-verifies: root signature, recovery PoPs, t > n/2, policy bounds (§1.1)."""
    try:
        if not gc.well_formed(genesis, group_id) or genesis["t"] != "group.genesis" or genesis["id"] != "genesis":
            return False
        gid, body, signer = genesis["group_id"], genesis["body"], genesis["signer"]
        if signer != {"kind": "root", "id": gid, "key_id": gid} or gc.key_id_of(body["root_public_key"]) != gid:
            return False
        if not isinstance(body.get("name"), str) or not 1 <= len(body["name"]) <= 80:
            return False
        if not _recovery_ok(body["recovery"]) or not _policy_ok(body["policy"]):
            return False
        gc.parse_time(body["created_at"])
        if not gc.verify_object(genesis, body["root_public_key"]):
            return False
        pops = {p["key_id"]: p["sig"] for p in genesis.get("pop") or [] if p.get("kind") == "recovery"}
        for key in body["recovery"]["keys"]:
            if key["key_id"] not in pops or not gc.verify_object(genesis, key["public_key"], pops[key["key_id"]]):
                return False
        return True
    except (KeyError, TypeError, AttributeError, gc.GroupCryptoError):
        return False


def _quorum(obj: dict, recovery: dict) -> bool:
    """`sig` plus `cosigs` from at least `threshold` distinct keys of the pinned recovery set."""
    keys = {k["key_id"]: k["public_key"] for k in recovery["keys"]}
    signer = obj["signer"]
    if signer["kind"] != "recovery" or signer["id"] != obj["group_id"] or signer["key_id"] not in keys:
        return False
    if not gc.verify_object(obj, keys[signer["key_id"]]):
        return False
    good = {signer["key_id"]}
    for co in obj.get("cosigs") or []:
        if isinstance(co, dict) and co.get("key_id") in keys and co["key_id"] not in good \
                and gc.verify_object(obj, keys[co["key_id"]], co.get("sig")):
            good.add(co["key_id"])
    return len(good) >= recovery["threshold"]


def verify_succession(obj: Any, group: OwnerGroup, index: int, previous_root_key_id: str,
                      pinned: dict) -> bool:
    try:
        if not gc.well_formed(obj, group.group_id) or obj["t"] != "group.succession":
            return False
        body = obj["body"]
        if obj["id"] != f"succession:{index}" or body["index"] != index:
            return False
        if body["previous_root_key_id"] != previous_root_key_id:
            return False
        if gc.key_id_of(body["new_root_public_key"]) != body["new_root_key_id"]:
            return False
        if not _recovery_ok(body["recovery"]):
            return False
        policy = group.genesis["body"]["policy"]
        if type(body["activation_delay_hours"]) is not int or \
                body["activation_delay_hours"] < policy["succession_delay_hours"]:
            return False
        carry = body["carry"]
        if not isinstance(carry, list) or len(carry) > policy["max_members"] + 256:
            return False
        if any(not isinstance(c, dict) or set(c) != {"serial", "body_hash"} for c in carry):
            return False
        if type(body["old_root_last_serial_n"]) is not int or not isinstance(body["old_root_last_crl"], dict):
            return False
        gc.parse_time(body["issued_at"])
        if not _quorum(obj, pinned):
            return False
        return gc.verify_object(obj, body["new_root_public_key"], obj.get("new_root_pop"))
    except (KeyError, TypeError, gc.GroupCryptoError):
        return False


def verify_cancel(obj: Any, group: OwnerGroup, pinned: dict) -> bool:
    try:
        if not gc.well_formed(obj, group.group_id) or obj["t"] != "group.succession_cancel":
            return False
        body = obj["body"]
        if obj["id"] != f"succession_cancel:{body['index']}" or type(body["index"]) is not int:
            return False
        gc.parse_time(body["issued_at"])
        return _quorum(obj, pinned)  # a G-signed cancel is never honoured (H26, RC-6)
    except (KeyError, TypeError, gc.GroupCryptoError):
        return False


@dataclass
class RootKey:
    index: int
    key_id: str
    public: str
    succession: dict | None  # the succession body that introduced this key (None for genesis)


def root_chain(group: OwnerGroup) -> list[RootKey]:
    chain = [RootKey(0, group.group_id, group.genesis["body"]["root_public_key"], None)]
    for obj in group.succession or []:
        body = obj["body"]
        chain.append(RootKey(body["index"], body["new_root_key_id"], body["new_root_public_key"], body))
    return chain


def pending_successions(group: OwnerGroup) -> list[dict]:
    return [p for p in (group.succession_pending or []) if not p.get("cancelled")]


# ---- delegate and certificate bodies (§1.3) --------------------------------------------------------------------

def _delegate_ok(delegate: Any, policy: dict) -> bool:
    try:
        if set(delegate) != {"may_enrol", "max_members", "max_serials", "may_grant_roles", "may_grant_nodes",
                             "may_grant_scopes", "max_depth"}:
            return False
        if type(delegate["may_enrol"]) is not bool or delegate["max_depth"] != 1:
            return False
        members, serials = delegate["max_members"], delegate["max_serials"]
        if type(members) is not int or not 1 <= members <= policy["max_members"]:
            return False
        if type(serials) is not int or not 1 <= serials <= 4 * members:
            return False
        roles, nodes, scopes = delegate["may_grant_roles"], delegate["may_grant_nodes"], delegate["may_grant_scopes"]
        if not isinstance(roles, list) or not set(roles) <= set(GRANT_ROLES) or "group_admin" in roles:
            return False
        if not isinstance(nodes, list) or any(not isinstance(n, str) or not NODE_ID.match(n) for n in nodes):
            return False
        return isinstance(scopes, list) and set(scopes) <= set(DEFAULT_SCOPES)
    except TypeError:
        return False


def default_delegate(max_members: int = 4, grant_roles: list[str] | None = None,
                     grant_nodes: list[str] | None = None) -> dict:
    """`max_members` > 0 lets the node enrol; `grant_roles` with `grant_nodes` let it sign grants (§1.3)."""
    members = max(max_members, 1)
    return {"may_enrol": max_members > 0, "max_members": members, "max_serials": 2 * members,
            "may_grant_roles": list(grant_roles or []), "may_grant_nodes": list(grant_nodes or []),
            "may_grant_scopes": list(DEFAULT_SCOPES), "max_depth": 1}


def _member_ok(member: Any) -> bool:
    try:
        if set(member) - {"url_verified"} != set(gc.MEMBER_FIELDS):
            return False
        if not NODE_ID.match(member["node_id"]) or key_id_for(gc.unb64(member["root_public_key"], 32)) != \
                member["node_id"]:
            return False
        gc.unb64(member["box_public_key"], 32)
        if type(member["rev"]) is not int or member["rev"] < 1:
            return False
        if not isinstance(member["url"], str) or not 8 <= len(member["url"]) <= 300:
            return False
        if not isinstance(member["name"], str) or not 1 <= len(member["name"]) <= 80:
            return False
        return type(member.get("url_verified", False)) is bool
    except (KeyError, TypeError, gc.GroupCryptoError):
        return False


def normal_origin(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url.strip().lower())
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{parts.hostname}:{port}"


def clean_member_url(raw: str, production: bool) -> str:
    from urllib.parse import urlsplit

    url = (raw or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment or len(url) > 300 or any(c.isspace() for c in url):
        raise GroupError("use a plain http(s) address such as https://atlas.example.org")
    if production and parts.scheme != "https":
        raise GroupError("member addresses must be https in production (D10.12)")
    return url


# ---- the trust evaluator (§1.4) ---------------------------------------------------------------------------------

@dataclass
class Verdict:
    ok: bool
    reason: str = ""


@dataclass
class Trust:
    group: OwnerGroup
    certs: dict[str, dict]  # serial -> signed envelope (held rows excluded)
    revoked: dict[str, GroupRevoked]
    banned: dict[str, GroupBannedNode]
    rotations: dict[str, list]
    now: datetime
    grants: dict[str, dict] = field(default_factory=dict)  # serial -> signed grant envelope (held rows excluded)
    _memo: dict[str, Verdict] = field(default_factory=dict)
    _busy: set[str] = field(default_factory=set)
    _keys: dict[str, tuple[str, dict[str, str]] | None] = field(default_factory=dict)

    @property
    def group_id(self) -> str:
        return self.group.group_id

    @property
    def policy(self) -> dict:
        return self.group.genesis["body"]["policy"]

    def chain(self) -> list[RootKey]:
        return root_chain(self.group)

    # -- keys
    def root_public(self, node_id: str) -> str | None:
        """The node's root key from any certificate this node holds (all must agree, §1.4 step 8)."""
        found = {c["body"]["member"]["root_public_key"] for c in self.certs.values()
                 if c["body"]["member"]["node_id"] == node_id}
        return found.pop() if len(found) == 1 else None

    def _history(self, node_id: str, root_public: str) -> tuple[str, dict[str, str]] | None:
        """(current key id, every key id -> public) from the node's validated rotation history (§1.7)."""
        cache = f"{node_id}:{root_public}"
        if cache in self._keys:
            return self._keys[cache]
        history = self.rotations.get(node_id) or []
        try:
            tip = history[-1]["body"]["key_id"] if history else node_id
            _, keys = key_rotation.validate_history(node_id, root_public, history, tip)
            found: tuple[str, dict[str, str]] | None = (tip, keys)
        except (key_rotation.RotationError, KeyError, TypeError, IndexError):
            found = None
        self._keys[cache] = found
        return found

    def current_key(self, node_id: str, root_public: str | None = None) -> tuple[str, str] | None:
        """(key_id, public_b64) of the node's current signing key per its validated rotation history (§1.7)."""
        root_public = root_public or self.root_public(node_id)
        if root_public is None:
            return None
        found = self._history(node_id, root_public)
        return (found[0], found[1][found[0]]) if found else None

    def all_keys(self, node_id: str) -> set[str]:
        root = self.root_public(node_id)
        keys = {root} if root else set()
        for item in self.rotations.get(node_id) or []:
            keys.add(item["body"]["public_key"])
        return keys

    # -- signatures
    def root_key_for(self, obj: dict) -> RootKey | None:
        for key in self.chain():
            if key.key_id == obj["signer"]["key_id"]:
                return key
        return None

    def signature_ok(self, obj: dict) -> bool:
        signer = obj["signer"]
        if signer["kind"] == "root":
            key = self.root_key_for(obj)
            return signer["id"] == self.group_id and key is not None and gc.verify_object(obj, key.public)
        if signer["kind"] == "node":
            current = self.current_key(signer["id"])
            return current is not None and current[0] == signer["key_id"] and gc.verify_object(obj, current[1])
        return False

    def member_pop_ok(self, body: dict, strict: bool = False) -> bool:
        """The member signed its own member object (§1.3).

        `strict` (a new or changed member object: enrolment hello, member update) needs the node's current key.
        Otherwise (verifying a certificate already issued, or a renewal that reuses the member object) any key in
        the node's validated history is accepted, so a routine signing-key rotation needs no certificate change
        (§1.7; CONTRACT-AMENDMENTS A10.11). An issuer only ever signs a member object whose PoP was checked
        strictly, so a superseded key cannot introduce a new url, name or box key."""
        member, pop = body["member"], body.get("member_pop")
        if not isinstance(pop, dict) or not isinstance(pop.get("key_id"), str):
            return False
        found = self._history(member["node_id"], member["root_public_key"])
        if found is None:
            return False
        tip, keys = found
        if strict and pop["key_id"] != tip:
            return False
        public = keys.get(pop["key_id"])
        return public is not None and gc.verify_member_pop(public, self.group_id, member, pop)

    def pop_order(self, node_id: str, pop: Any) -> int:
        """The position of a member PoP's key in the node's validated history (root key 0, then each rotation)."""
        key_id = pop.get("key_id") if isinstance(pop, dict) else None
        if key_id == node_id:
            return 0
        for i, item in enumerate(self.rotations.get(node_id) or [], start=1):
            if isinstance(item, dict) and item.get("body", {}).get("key_id") == key_id:
                return i
        return -1

    def carried(self, obj: dict, key: RootKey) -> Verdict:
        """§1.4 step 7: objects of an older root key must be carried by content by every later succession."""
        later = [k for k in self.chain() if k.index > key.index]
        if not later:
            return Verdict(True)
        serial = parse_serial(obj["body"].get("serial"))
        bound = later[0].succession["old_root_last_serial_n"]
        if serial is not None and serial.issuer == "root" and serial.n > bound:
            return Verdict(False, "old_root_bound")
        for k in later:
            carry = {(c["serial"], c["body_hash"]) for c in k.succession["carry"]}
            if (obj["body"].get("serial"), obj["body_hash"]) not in carry:
                if any(c[0] == obj["body"].get("serial") for c in carry):
                    return Verdict(False, "carry_mismatch")
                return Verdict(False, "not_carried")
        return Verdict(True)

    # -- certificates
    def shape_ok(self, x: dict) -> Verdict:
        if not gc.well_formed(x, self.group_id) or x["t"] != "group.cert":
            return Verdict(False, "malformed")
        b = x["body"]
        try:
            serial = parse_serial(b["serial"])
            if serial is None or x["id"] != f"cert:{b['serial']}":
                return Verdict(False, "malformed")
            if serial.issuer == "root":
                if b["issuer"] != "root" or x["signer"]["kind"] != "root":
                    return Verdict(False, "malformed")
            elif b["issuer"] != serial.issuer or x["signer"] != {**x["signer"], "kind": "node", "id": serial.issuer}:
                return Verdict(False, "signer_mismatch")  # §1.3 signer/body agreement
            if not _member_ok(b["member"]):
                return Verdict(False, "malformed")
            scopes = b["scopes"]
            if not isinstance(scopes, list) or len(set(scopes)) != len(scopes) or not set(scopes) <= set(SCOPES):
                return Verdict(False, "bad_scopes")
            delegated = bool(set(scopes) & set(DELEGATE_SCOPES))
            if delegated != (b["delegate"] is not None):
                return Verdict(False, "bad_scopes")
            if b["delegate"] is not None and not _delegate_ok(b["delegate"], self.policy):
                return Verdict(False, "bad_delegate")
            start, end, issued = t(b["not_before"]), t(b["not_after"]), t(b["issued_at"])
            limit = self.policy["delegate_max_days"] if delegated else self.policy["cert_max_days"]
            if end <= start or end - start > timedelta(days=limit) + SKEW or issued > end:
                return Verdict(False, "bad_lifetime")
            if b["parent_serial"] is not None and parse_serial(b["parent_serial"]) is None:
                return Verdict(False, "malformed")
            if b["readmit"] is not None and parse_serial(b["readmit"]) is None:
                return Verdict(False, "malformed")
        except (KeyError, TypeError, gc.GroupCryptoError):
            return Verdict(False, "malformed")
        return Verdict(True)

    def genuine(self, x: dict) -> bool:
        """Shape, signature and member PoP: the object is what its signer and its member signed."""
        return self.shape_ok(x).ok and self.signature_ok(x) and self.member_pop_ok(x["body"])

    def valid(self, x: dict) -> Verdict:
        serial = x.get("body", {}).get("serial", "?")
        key = f"{serial}:{x.get('body_hash')}"
        if key in self._memo:
            return self._memo[key]
        if key in self._busy:
            return Verdict(False, "cycle")
        self._busy.add(key)
        try:
            verdict = self._valid(x)
        finally:
            self._busy.discard(key)
        self._memo[key] = verdict
        return verdict

    def _valid(self, x: dict) -> Verdict:
        shape = self.shape_ok(x)
        if not shape.ok:
            return shape
        b, now = x["body"], self.now
        serial = parse_serial(b["serial"])
        assert serial is not None
        # 2. signature (the message includes signer.id) and the member's own PoP over url, name and box key
        if not self.signature_ok(x):
            return Verdict(False, "bad_signature")
        member = b["member"]
        if not self.member_pop_ok(b):
            return Verdict(False, "bad_member_pop")
        # 3. time window at the receiver's clock
        if not (t(b["not_before"]) - SKEW <= now <= t(b["not_after"]) + SKEW) or t(b["issued_at"]) > now + SKEW:
            return Verdict(False, "expired" if now > t(b["not_after"]) else "not_yet_valid")
        # 7. root succession: an older root key's objects must be carried by content
        if x["signer"]["kind"] == "root":
            carried = self.carried(x, self.root_key_for(x))  # type: ignore[arg-type]
            if not carried.ok:
                return carried
        # 6. revocation of the object itself, and the node bans
        if b["serial"] in self.revoked:
            return Verdict(False, "revoked")
        node = member["node_id"]
        ban = self.banned.get(node)
        if ban is not None:
            if ban.reason == "key_compromise":
                return Verdict(False, "banned")
            if isinstance(ban, DerivedBan):
                # R1-13: a delegate's ban refuses non-root issuers; a root certificate needs no readmit
                if serial.issuer != "root":
                    return Verdict(False, "banned")
            elif serial.issuer != "root" or b["readmit"] != ban.crl_serial:
                return Verdict(False, "banned")
        # 8. the node's root key never changes; issuer pinning; monotone member.rev
        others = [c for s, c in self.certs.items() if s != b["serial"] and c["body"]["member"]["node_id"] == node]
        if any(c["body"]["member"]["root_public_key"] != member["root_public_key"] for c in others):
            return Verdict(False, "root_key_changed")
        genuine = [c for c in others if self.genuine(c)]
        if serial.issuer != "root" and any(c["body"]["issuer"] != serial.issuer for c in genuine):
            return Verdict(False, "other_issuer")
        for c in genuine:
            other = parse_serial(c["body"]["serial"])
            if other is not None and other.issuer == serial.issuer and other.order < serial.order \
                    and c["body"]["member"]["rev"] > member["rev"]:
                return Verdict(False, "member_rev_regressed")
            # R1-9: a higher-rev member object whose PoP key is older in the node's history than the PoP of a
            # genuine lower-rev object was made with a superseded key (a thief of the old key with a compromised
            # issuer); refused even though non-strict PoP checks accept any historical key
            if c["body"]["member"]["rev"] < member["rev"] and \
                    self.pop_order(node, c["body"]["member_pop"]) > self.pop_order(node, b["member_pop"]):
                return Verdict(False, "member_pop_superseded")
        # 4. issuer authority, evaluated now
        if serial.issuer != "root":
            verdict = self._delegated(x, serial)
            if not verdict.ok:
                return verdict
        return Verdict(True)

    def _delegated(self, x: dict, serial: Serial) -> Verdict:
        b, issuer = x["body"], serial.issuer
        if any(r.node_id == issuer and r.reason in CASCADING for r in self.revoked.values()):
            return Verdict(False, "issuer_revoked")  # cascade by reason (§1.6 item 2)
        parent = self.certs.get(b["parent_serial"] or "")
        if parent is None or parent["body"]["member"]["node_id"] != issuer:
            return Verdict(False, "no_parent")
        rev = self.revoked.get(b["parent_serial"])
        if rev is not None and rev.reason != "superseded":
            return Verdict(False, "parent_revoked")  # cascade by chain (§1.6 item 1)
        p = self.in_force(issuer)
        if p is None:
            return Verdict(False, "issuer_not_member")
        pb = p["body"]
        if pb["issuer"] != "root":
            return Verdict(False, "chain_too_deep")  # step 5: root -> delegating node -> member
        if "group.enrol" not in pb["scopes"] or not pb["delegate"] or not pb["delegate"]["may_enrol"]:
            return Verdict(False, "issuer_cannot_enrol")
        if not set(b["scopes"]) <= set(pb["delegate"]["may_grant_scopes"]) or b["delegate"] is not None:
            return Verdict(False, "scope_escalation")
        if t(b["not_after"]) > t(pb["not_after"]):
            return Verdict(False, "outlives_issuer")
        if serial.n > pb["delegate"]["max_serials"]:
            return Verdict(False, "delegate_serials_exhausted")
        lower = {c["body"]["member"]["node_id"] for s, c in self.certs.items()
                 if (o := parse_serial(s)) is not None and o.issuer == issuer and o.n < serial.n
                 and c["body"]["member"]["node_id"] != b["member"]["node_id"] and self.valid(c).ok}
        if len(lower) >= pb["delegate"]["max_members"]:
            return Verdict(False, "delegate_members_exhausted")
        return Verdict(True)

    def node_certs(self, node_id: str) -> list[dict]:
        return [c for c in self.certs.values() if c["body"]["member"]["node_id"] == node_id]

    def in_force(self, node_id: str) -> dict | None:
        """The valid certificate with the highest (n, r) among root-issued ones, else the highest n from the
        node's issuer. `issued_at` plays no part (§1.4)."""
        valid = [c for c in self.node_certs(node_id) if self.valid(c).ok]
        root = [c for c in valid if c["body"]["issuer"] == "root"]
        pool = root or valid
        if not pool:
            return None
        return max(pool, key=lambda c: parse_serial(c["body"]["serial"]).order)  # type: ignore[union-attr]

    def may_enrol_now(self, node_id: str) -> dict | None:
        """The node's in-force certificate when it currently carries `group.enrol` with a delegate, else None."""
        p = self.in_force(node_id)
        if p is None or p["body"]["issuer"] != "root" or "group.enrol" not in p["body"]["scopes"]:
            return None
        delegate = p["body"]["delegate"]
        return p if delegate and delegate.get("may_enrol") else None

    def was_delegate(self, node_id: str) -> bool | None:
        """Whether any genuine root-issued certificate ever gave `node_id` the right to enrol (whatever its state
        now: a revoked delegate may still revoke what it issued, §1.6). None when no root certificate of the node
        is known yet (a CRL that depends on it is deferred, not refused)."""
        roots = [c for c in self.node_certs(node_id) if c["body"]["issuer"] == "root" and self.genuine(c)]
        if not roots:
            return None
        return any(("group.enrol" in c["body"]["scopes"] and (c["body"]["delegate"] or {}).get("may_enrol"))
                   or ("grant.issue" in c["body"]["scopes"] and c["body"]["delegate"]) for c in roots)

    # -- grants (§6.1; validity follows §1.4)
    def may_grant_now(self, node_id: str) -> dict | None:
        """The node's in-force certificate when it is root-issued and carries `grant.issue` with a delegate."""
        p = self.in_force(node_id)
        if p is None or p["body"]["issuer"] != "root" or "grant.issue" not in p["body"]["scopes"]:
            return None
        return p if p["body"]["delegate"] else None

    def grant_shape_ok(self, x: dict) -> Verdict:
        if not gc.well_formed(x, self.group_id) or x["t"] != "group.grant":
            return Verdict(False, "malformed")
        b = x["body"]
        try:
            if set(b) != GRANT_FIELDS:
                return Verdict(False, "malformed")
            serial = parse_serial(b["serial"])
            if serial is None or x["id"] != f"grant:{b['serial']}":
                return Verdict(False, "malformed")
            if serial.issuer == "root":
                if b["issuer"] != "root" or x["signer"]["kind"] != "root" or b["parent_serial"] is not None \
                        or b["authorized_by_grant"] is not None:
                    return Verdict(False, "malformed")
            elif b["issuer"] != serial.issuer or x["signer"] != {**x["signer"], "kind": "node", "id": serial.issuer}:
                return Verdict(False, "signer_mismatch")
            elif parse_serial(b["parent_serial"]) is None:
                return Verdict(False, "malformed")
            if b["authorized_by_grant"] is not None and parse_serial(b["authorized_by_grant"]) is None:
                return Verdict(False, "malformed")
            if not isinstance(b["account_uid"], str) or not ACCOUNT_UID.match(b["account_uid"]):
                return Verdict(False, "malformed")
            if not isinstance(b["account_label"], str) or len(b["account_label"]) > 80 \
                    or not isinstance(b["note"], str) or len(b["note"]) > 200:
                return Verdict(False, "malformed")
            if b["role"] not in GRANT_ALL_ROLES:
                return Verdict(False, "bad_role")
            nodes = b["nodes"]
            if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_MEMBERS or len(set(nodes)) != len(nodes):
                return Verdict(False, "bad_nodes")
            if nodes != ["*"] and any(not isinstance(n, str) or not NODE_ID.match(n) for n in nodes):
                return Verdict(False, "bad_nodes")
            start, end, issued = t(b["not_before"]), t(b["not_after"]), t(b["issued_at"])
            if end <= start or end - start > timedelta(days=self.policy["grant_max_days"]) + SKEW or issued > end:
                return Verdict(False, "bad_lifetime")
        except (KeyError, TypeError, gc.GroupCryptoError):
            return Verdict(False, "malformed")
        return Verdict(True)

    def grant_valid(self, x: dict) -> Verdict:
        key = f"grant:{x.get('body', {}).get('serial', '?')}:{x.get('body_hash')}"
        if key in self._memo:
            return self._memo[key]
        if key in self._busy:
            return Verdict(False, "cycle")
        self._busy.add(key)
        try:
            verdict = self._grant_valid(x)
        finally:
            self._busy.discard(key)
        self._memo[key] = verdict
        return verdict

    def _grant_valid(self, x: dict) -> Verdict:
        shape = self.grant_shape_ok(x)
        if not shape.ok:
            return shape
        b, now = x["body"], self.now
        serial = parse_serial(b["serial"])
        assert serial is not None
        if not self.signature_ok(x):
            return Verdict(False, "bad_signature")
        if not (t(b["not_before"]) - SKEW <= now <= t(b["not_after"]) + SKEW) or t(b["issued_at"]) > now + SKEW:
            return Verdict(False, "expired" if now > t(b["not_after"]) else "not_yet_valid")
        if x["signer"]["kind"] == "root":
            carried = self.carried(x, self.root_key_for(x))  # type: ignore[arg-type]
            if not carried.ok:
                return carried
        if b["serial"] in self.revoked:
            return Verdict(False, "revoked")
        abg = b["authorized_by_grant"]
        if abg is not None and abg in self.revoked:
            return Verdict(False, "authorizer_revoked")  # cascade by authorized_by_grant (§1.6 item 1)
        if serial.issuer != "root":
            verdict = self._grant_delegated(x, serial)
            if not verdict.ok:
                return verdict
        if abg is not None:
            a = self.grants.get(abg)
            if a is None:
                return Verdict(False, "no_authorizer")
            if not self.grant_valid(a).ok:
                return Verdict(False, "authorizer_invalid")
            ab = a["body"]
            if ab["role"] != "group_admin" or (serial.issuer not in ab["nodes"] and ab["nodes"] != ["*"]):
                return Verdict(False, "authorizer_not_group_admin_here")
            if t(b["not_after"]) > t(ab["not_after"]) or ROLE_RANK[b["role"]] > ROLE_RANK[ab["role"]]:
                return Verdict(False, "outlives_authorizer")
        return Verdict(True)

    def _grant_delegated(self, x: dict, serial: Serial) -> Verdict:
        b, issuer = x["body"], serial.issuer
        if any(r.node_id == issuer and r.reason in CASCADING for r in self.revoked.values()):
            return Verdict(False, "issuer_revoked")
        parent = self.certs.get(b["parent_serial"] or "")
        if parent is None or parent["body"]["member"]["node_id"] != issuer:
            return Verdict(False, "no_parent")
        rev = self.revoked.get(b["parent_serial"])
        if rev is not None and rev.reason != "superseded":
            return Verdict(False, "parent_revoked")
        p = self.in_force(issuer)
        if p is None:
            return Verdict(False, "issuer_not_member")
        pb = p["body"]
        if pb["issuer"] != "root":
            return Verdict(False, "chain_too_deep")
        if "grant.issue" not in pb["scopes"] or not pb["delegate"]:
            return Verdict(False, "issuer_cannot_grant")
        if b["role"] == "group_admin" or b["role"] not in pb["delegate"]["may_grant_roles"]:
            return Verdict(False, "role_escalation")
        if "*" in b["nodes"] or not set(b["nodes"]) <= set(pb["delegate"]["may_grant_nodes"]):
            return Verdict(False, "node_escalation")
        if t(b["not_after"]) > t(pb["not_after"]):
            return Verdict(False, "outlives_issuer")
        if serial.n > pb["delegate"]["max_serials"]:
            return Verdict(False, "delegate_serials_exhausted")
        return Verdict(True)

    def grants_for(self, account_uid: str, node_id: str) -> list[dict]:
        """Valid grants of `account_uid` that cover `node_id` (its id or `*`)."""
        return [g for g in self.grants.values() if g["body"]["account_uid"] == account_uid
                and (node_id in g["body"]["nodes"] or g["body"]["nodes"] == ["*"]) and self.grant_valid(g).ok]

    def sole_issuer(self, node_id: str, issuer: str) -> bool:
        """True when every genuine certificate of `node_id` was issued by `issuer`: only then may that issuer's
        revocation ban the node or cascade over what the node signed (§1.4 step 8 'who may (re)issue', §1.6)."""
        genuine = [c for c in self.node_certs(node_id) if self.genuine(c)]
        return bool(genuine) and all(c["body"]["issuer"] == issuer for c in genuine)

    def nodes(self) -> list[str]:
        return sorted({c["body"]["member"]["node_id"] for c in self.certs.values()})

    def members(self) -> dict[str, dict]:
        return {n: c for n in self.nodes() if (c := self.in_force(n)) is not None}

    def status_of(self, node_id: str) -> str:
        cert = self.in_force(node_id)
        if cert is not None:
            return "expiring" if t(cert["body"]["not_after"]) - self.now <= EXPIRY_WARNING else "active"
        if node_id in self.banned:
            return "revoked"
        latest = max(self.node_certs(node_id), key=lambda c: t(c["body"]["issued_at"]), default=None)
        if latest is not None:
            rev = self.revoked.get(latest["body"]["serial"])
            if rev is not None and rev.reason == "left":
                return "left"
            if rev is not None or any(r.node_id == node_id and r.reason != "superseded"
                                      for r in self.revoked.values()):
                return "revoked"
            if self.now > t(latest["body"]["not_after"]):
                return "expired"
        return "revoked"


def _revoked_map(session: Session, group_id: str) -> dict[str, GroupRevoked]:
    return {r.serial: r for r in session.scalars(select(GroupRevoked).where(GroupRevoked.group_id == group_id))}


@dataclass
class DerivedBan:
    """A node ban from a non-root signer (R1-13): never stored, in effect only while that signer is the sole issuer
    of every genuine certificate of the node. It has `removed` strength whatever the entry's reason, so a genuine
    root-issued certificate for the node (which ends the sole-issuer condition) lifts it in any arrival order."""
    node_id: str
    root_public_key: str | None
    revoked_at: datetime
    crl_serial: str
    derived_by: str
    reason: str = "removed"


def _derive_bans(tr: Trust) -> None:
    """Add the derived delegate bans to `tr.banned` (stored root bans win). `sole_issuer` counts genuine
    certificates (signature and PoP, never bans), so the derivation has no cycle (§C10.17 R1-13)."""
    for r in tr.revoked.values():
        if r.issuer == "root" or r.reason not in BANNING or not r.node_id or r.node_id in tr.banned:
            continue
        if tr.sole_issuer(r.node_id, r.issuer):
            tr.banned[r.node_id] = DerivedBan(r.node_id, r.root_public_key, aware(r.revoked_at),  # type: ignore[arg-type]
                                              r.serial, r.issuer)


def trust(session: Session, group: OwnerGroup, now: datetime | None = None, extra_rotations: dict | None = None,
          own_node: str | None = None) -> Trust:
    certs = {c.serial: c.body for c in session.scalars(select(GroupCert).where(
        GroupCert.group_id == group.group_id, GroupCert.held_reason.is_(None)))}
    rotations = {m.node_id: list(m.rotations or []) for m in session.scalars(
        select(GroupMember).where(GroupMember.group_id == group.group_id))}
    if own_node:
        rotations[own_node] = key_rotation.own_history(session, own_node)
    rotations.update(extra_rotations or {})
    banned = {b.node_id: b for b in session.scalars(select(GroupBannedNode).where(
        GroupBannedNode.group_id == group.group_id))}
    grants = {g.serial: g.body for g in session.scalars(select(GroupGrant).where(
        GroupGrant.group_id == group.group_id, GroupGrant.held_reason.is_(None)))}
    tr = Trust(group, certs, _revoked_map(session, group.group_id), banned, rotations, now or utcnow(),
               grants=grants)
    _derive_bans(tr)
    return tr


_REQUEST_TRUST = "atlas_request_trust"  # Session.info: the read-only Trust built for this request's checks


def request_trust(session: Session, group: OwnerGroup, own_node: str) -> Trust:
    """`trust(..., own_node=own_node)` built once for a request's read-only checks (the federated-session check,
    the effective-rights computation and its freshness checks). Any flush, commit or rollback of the session drops
    it, so nothing written later is judged against a stale certificate or grant set."""
    key = (group.group_id, own_node)
    cached = session.info.get(_REQUEST_TRUST)
    if cached is not None and cached[0] == key:
        return cached[1]
    tr = trust(session, group, own_node=own_node)
    session.info[_REQUEST_TRUST] = (key, tr)
    return tr


def _drop_request_trust(session: Session, *_args) -> None:
    session.info.pop(_REQUEST_TRUST, None)


for _event in ("after_flush", "after_commit", "after_rollback"):
    event.listen(Session, _event, _drop_request_trust)


# ---- loading, secrets, audit ------------------------------------------------------------------------------------

def load(session: Session) -> OwnerGroup | None:
    """This node's group (at most one, D10.5); a group this node left or was removed from still loads."""
    return session.scalars(select(OwnerGroup).order_by(OwnerGroup.joined_at)).first()


def active(session: Session) -> OwnerGroup | None:
    group = load(session)
    return group if group is not None and group.status in ("active", "resyncing", "halted") else None


def seal_secret(settings: Settings | None, raw: bytes) -> str:
    if settings is None or not secrets_box.available(settings):
        raise GroupError("owner groups need ATLAS_SECRET_KEY: keys and invite secrets are stored encrypted (D10.4)",
                         "forbidden")
    return ENC_PREFIX + secrets_box.encrypt(settings, base64.b64encode(raw).decode())


def open_secret(settings: Settings | None, value: str) -> bytes:
    if not value.startswith(ENC_PREFIX) or settings is None or not secrets_box.available(settings):
        raise GroupError("the group secrets cannot be read: ATLAS_SECRET_KEY is missing or wrong", "forbidden")
    try:
        return base64.b64decode(secrets_box.decrypt(settings, value[len(ENC_PREFIX):]))
    except secrets_box.SecretsError:
        raise GroupError("the group secrets cannot be read: ATLAS_SECRET_KEY is missing or wrong",
                         "forbidden") from None


def box_keys(settings: Settings | None, group: OwnerGroup, now: datetime | None = None) -> list[X25519PrivateKey]:
    keys = [gc.x_from_raw(open_secret(settings, group.box_sealed))]
    until = aware(group.box_previous_until)
    if group.box_previous_sealed and until and (now or utcnow()) <= until:
        keys.append(gc.x_from_raw(open_secret(settings, group.box_previous_sealed)))
    if group.box_pending_sealed:
        keys.append(gc.x_from_raw(open_secret(settings, group.box_pending_sealed)))
    return keys


def audit(session: Session, actor: str | None, action: str, target: str = "", detail: dict | None = None,
          cli: bool = False) -> None:
    detail = dict(detail or {})
    if cli:
        detail["cli"] = True
    session.add(AuditEntry(actor_id=actor, action=action[:48], target=target[:128], detail=detail or None))


def _journal(session: Session, group: OwnerGroup, kind: str, obj: dict, **extra: Any) -> None:
    """Append an accepted CRL, succession, cancel or activation to the revocation journal (R1-3)."""
    from . import group_journal

    group_journal.record(session, group.group_id, kind, obj, **extra)


def notify_admins(session: Session, key: str, title: str) -> None:
    """The existing Notification row for every local administrator (§2.6, §1.8)."""
    for admin in session.scalars(select(User).where(User.role == "admin", User.deleted_at.is_(None))):
        session.add(Notification(user_id=admin.id, kind="group", key=key[:80], title=title[:200], url="/admin/group"))


# ---- storing objects (§1.6, §1.8, §3.8 order) --------------------------------------------------------------------

AUTHORITY_FIELDS = ("member", "scopes", "delegate", "issuer", "parent_serial", "not_before", "not_after", "readmit")


def _cert_row(group: OwnerGroup, obj: dict, now: datetime, held: str | None = None) -> GroupCert:
    b = obj["body"]
    s = parse_serial(b["serial"])
    assert s is not None
    return GroupCert(group_id=group.group_id, serial=b["serial"], issuer=b["issuer"], serial_n=s.n, serial_r=s.r,
                     parent_serial=b["parent_serial"], authorized_by_grant=None, node_id=b["member"]["node_id"],
                     member_rev=b["member"]["rev"], body=obj, body_hash=obj["body_hash"],
                     not_after=t(b["not_after"]), received_at=now, held_reason=held)


def ingest_cert(session: Session, group: OwnerGroup, obj: Any, now: datetime, tr: Trust | None = None) -> str:
    """Store a certificate whose shape and signature verify and whose signer may issue at all. Returns 'stored',
    'known', 'held', 'collision' or a rejection reason. Validity (§1.4) is evaluated on every read, at the
    reader's time.

    A node-signed certificate is stored only while its signer holds `group.enrol` and only within the signer's
    `max_serials`, so a compromised ordinary member cannot fill the store (or every member's `state`) with
    certificates it has no authority to sign."""
    tr = tr or trust(session, group, now)
    shape = tr.shape_ok(obj)
    if not shape.ok:
        return shape.reason
    b = obj["body"]
    existing = session.get(GroupCert, (group.group_id, b["serial"]))
    if existing is not None:
        if existing.body_hash == obj["body_hash"]:
            return "known"
        if not tr.signature_ok(obj):
            return "bad_signature"  # an unsigned look-alike is no collision
        if _audit_once(session, "group_serial_collision", b["serial"],  # once per serial, not every sync round
                       {"stored": existing.body_hash, "received": obj["body_hash"]}):
            notify_admins(session, f"group-collision:{b['serial']}",
                          "Two different group certificates share one serial")
        if any(existing.body["body"].get(f) != b.get(f) for f in AUTHORITY_FIELDS):
            existing.held_reason = "collision"  # both rejected: they disagree on authority (§1.3)
            return "collision"
        if obj["body_hash"] < existing.body_hash:  # every node keeps the same one
            existing.body, existing.body_hash = obj, obj["body_hash"]
        return "collision"
    if not tr.signature_ok(obj):
        return "bad_signature"
    if session.get(GroupGrant, (group.group_id, b["serial"])) is not None:
        if _audit_once(session, "group_serial_collision", b["serial"], {"received": obj["body_hash"], "kind": "cert"}):
            notify_admins(session, f"group-collision:{b['serial']}", "A group certificate reuses a grant serial")
        return "collision"  # one serial space per issuer, certificates and grants together (§1.3)
    if obj["signer"]["kind"] == "node":
        p = tr.may_enrol_now(obj["signer"]["id"])
        if p is None:
            return "issuer_cannot_enrol"
        if parse_serial(b["serial"]).n > p["body"]["delegate"]["max_serials"]:  # type: ignore[union-attr]
            return "delegate_serials_exhausted"
    held = None
    if obj["signer"]["kind"] == "root" and pending_successions(group):
        held = "succession_pending"  # §1.8: a G thief gains nothing during the window
    session.add(_cert_row(group, obj, now, held))
    session.flush()
    return "held" if held else "stored"


def _grant_row(group: OwnerGroup, obj: dict, now: datetime, held: str | None = None) -> GroupGrant:
    b = obj["body"]
    s = parse_serial(b["serial"])
    assert s is not None
    return GroupGrant(group_id=group.group_id, serial=b["serial"], issuer=b["issuer"], serial_n=s.n,
                      parent_serial=b["parent_serial"], authorized_by_grant=b["authorized_by_grant"],
                      account_uid=b["account_uid"], role=b["role"], nodes=list(b["nodes"]),
                      not_before=t(b["not_before"]), not_after=t(b["not_after"]), body=obj, body_hash=obj["body_hash"],
                      received_at=now, held_reason=held)


def ingest_grant(session: Session, group: OwnerGroup, obj: Any, now: datetime, tr: Trust | None = None) -> str:
    """Store a grant whose shape and signature verify and whose signer may grant at all (§6.1), as `ingest_cert`
    does for certificates: 'stored', 'known', 'held', 'collision' or a rejection reason. Validity (§1.4) is
    evaluated on every read, at the reader's time."""
    tr = tr or trust(session, group, now)
    shape = tr.grant_shape_ok(obj)
    if not shape.ok:
        return shape.reason
    b = obj["body"]
    existing = session.get(GroupGrant, (group.group_id, b["serial"]))
    if existing is not None:
        if existing.body_hash == obj["body_hash"]:
            return "known"
        if not tr.signature_ok(obj):
            return "bad_signature"
        if _audit_once(session, "group_serial_collision", b["serial"],
                       {"stored": existing.body_hash, "received": obj["body_hash"], "kind": "grant"}):
            notify_admins(session, f"group-collision:{b['serial']}", "Two different group grants share one serial")
        if any(existing.body["body"].get(f) != b.get(f) for f in GRANT_AUTHORITY):
            existing.held_reason = "collision"  # both rejected (§1.3)
            return "collision"
        if obj["body_hash"] < existing.body_hash:
            existing.body, existing.body_hash = obj, obj["body_hash"]
        return "collision"
    if not tr.signature_ok(obj):
        return "bad_signature"
    if session.get(GroupCert, (group.group_id, b["serial"])) is not None:
        if _audit_once(session, "group_serial_collision", b["serial"], {"received": obj["body_hash"], "kind": "grant"}):
            notify_admins(session, f"group-collision:{b['serial']}", "A group grant reuses a certificate serial")
        return "collision"  # one serial space per issuer (§1.3): a grant may not reuse a certificate's serial
    if obj["signer"]["kind"] == "node":
        p = tr.may_grant_now(obj["signer"]["id"])
        if p is None:
            return "issuer_cannot_grant"
        if parse_serial(b["serial"]).n > p["body"]["delegate"]["max_serials"]:  # type: ignore[union-attr]
            return "delegate_serials_exhausted"
    held = "succession_pending" if obj["signer"]["kind"] == "root" and pending_successions(group) else None
    session.add(_grant_row(group, obj, now, held))
    session.flush()
    return "held" if held else "stored"


MAX_NODE_CRL_ENTRIES = 4 * MAX_MEMBERS + 128  # a delegate's max_serials plus its own certificates (§1.3)
MAX_NODE_CRLS = 8  # stored CRL rows per non-root issuer after compaction (forks included)


def _entry_scope(tr: Trust, signer: dict, entry: Any) -> str:
    """One CRL entry against §1.6 'who may revoke what': 'ok', 'deferred' (it names a certificate, or depends on
    a delegation, this node has not seen yet: nothing of it is applied) or 'invalid'.

    For a node signer an entry is in scope only when the named certificate is known and matches the entry:
    - its own certificate, reason `left`, `node_id` = the signer (self-leave); or
    - a certificate the signer issued (serial `<signer>/<n>`), and a genuine root certificate once gave the
      signer `group.enrol`. An ordinary member never issues, so any such entry from it is invalid.
    Nothing is taken from an entry's own `node_id` for a certificate this node has not seen."""
    if not isinstance(entry, dict):
        return "invalid"
    s = parse_serial(entry.get("serial"))
    if s is None or entry.get("reason") not in REASONS:
        return "invalid"
    try:
        t(entry["revoked_at"])
        if entry.get("suspect_since") is not None:
            t(entry["suspect_since"])
    except (KeyError, gc.GroupCryptoError):
        return "invalid"
    cert = tr.certs.get(entry["serial"])
    if cert is not None and (cert["body"]["member"]["node_id"] != entry.get("node_id")
                             or cert["body"]["member"]["root_public_key"] != entry.get("root_public_key")):
        return "invalid"
    grant = tr.grants.get(entry["serial"])
    if grant is not None and (entry.get("node_id") is not None or entry.get("root_public_key") is not None):
        return "invalid"  # grant entries carry null node fields (§1.6)
    if signer["kind"] == "root":
        return "ok"
    issuer = signer["id"]
    if s.issuer == issuer:  # a delegate revokes what it issued (depth 1: no further descendants)
        delegate = tr.was_delegate(issuer)
        if delegate is False:
            return "invalid"  # an ordinary member may only revoke its own serial, with reason `left`
        return "deferred" if delegate is None or (cert is None and grant is None) else "ok"
    if grant is not None or entry["reason"] != "left" or entry.get("node_id") != issuer:
        return "invalid"
    return "deferred" if cert is None else "ok"  # self-leave, applied once the signer's certificate is known


def _crl_scope_ok(tr: Trust, crl: dict) -> str:
    """'ok', 'deferred' (some entry needs an object this node has not seen yet) or 'invalid' (§1.6: any entry out
    of the signer's scope makes the whole CRL invalid)."""
    signer = crl["signer"]
    if signer["kind"] != "root" and len(crl["body"]["entries"]) > MAX_NODE_CRL_ENTRIES:
        return "invalid"
    scopes = {_entry_scope(tr, signer, entry) for entry in crl["body"]["entries"]}
    if "invalid" in scopes:
        return "invalid"
    return "deferred" if "deferred" in scopes else "ok"


def _entry_node(tr: Trust, signer: dict, entry: dict) -> tuple[str | None, str | None]:
    """(node_id, root_public_key) the entry may act on for bans and the cascade by reason (§1.6), or (None, None)
    when it only revokes the serial itself. Root is trusted for any entry. A node signer acts on a node only for
    its own self-leave, or for a node whose every genuine certificate it issued: a delegate cannot ban or
    cascade over a root-enrolled node (or another delegate's node) by minting a look-alike certificate."""
    if signer["kind"] == "root":
        return entry.get("node_id"), entry.get("root_public_key")
    issuer = signer["id"]
    cert = tr.certs.get(entry["serial"])
    if cert is None:
        return None, None
    node = cert["body"]["member"]["node_id"]
    if (node == issuer and entry["reason"] == "left") or tr.sole_issuer(node, issuer):
        return node, cert["body"]["member"]["root_public_key"]
    return None, None


def crl_signature_ok(tr: Trust, crl: Any) -> bool:
    try:
        if not gc.well_formed(crl, tr.group_id) or crl["t"] != "group.crl":
            return False
        body, signer = crl["body"], crl["signer"]
        if type(body["crl_seq"]) is not int or body["crl_seq"] < 1 or not isinstance(body["entries"], list):
            return False
        if len(body["entries"]) > MAX_CRL_ENTRIES or crl["id"] != f"crl:{body['issuer']}/{body['crl_seq']}":
            return False
        gc.parse_time(body["issued_at"])
        if signer["kind"] == "root":
            if body["issuer"] != "root":
                return False
            key = tr.root_key_for(crl)
            if key is None or not gc.verify_object(crl, key.public):
                return False
            later = [k for k in tr.chain() if k.index > key.index]
            if later:
                # R1-1: after a succession the old root's revocation state is exactly `old_root_last_crl`. Any
                # other CRL of that key, lower seq included, is invalid, so a stolen old G cannot ban carried
                # members with a forged lower-sequence CRL (§1.8 checked only the upper bound)
                last = later[0].succession["old_root_last_crl"]
                if body["crl_seq"] != last["crl_seq"] or crl["body_hash"] != last["body_hash"]:
                    return False
            return True
        if signer["kind"] == "node":
            # A CRL entry can only take authority away, so issuer status plays no part (§1.6). The key must be
            # the signer's current one.
            return body["issuer"] == signer["id"] and tr.signature_ok(crl)
    except (KeyError, TypeError, gc.GroupCryptoError):
        return False
    return False


def ingest_crl(session: Session, group: OwnerGroup, crl: Any, now: datetime, tr: Trust | None = None) -> str:
    tr = tr or trust(session, group, now)
    if not crl_signature_ok(tr, crl):
        return "invalid"
    body = crl["body"]
    issuer, seq = body["issuer"], body["crl_seq"]
    if session.get(GroupCrl, (group.group_id, issuer, seq, crl["body_hash"])) is not None:
        return "known"
    if _crl_scope_ok(tr, crl) == "invalid":
        return "invalid"  # any entry out of the signer's scope makes the whole CRL invalid
    if crl["signer"]["kind"] == "root" and pending_successions(group) and group.status != "halted":
        # (a halted group never activates its conflicting successions, and revocation must keep working there)
        # R1-1: held like every other old-root object while a succession is pending; a cancel releases it, an
        # activation drops it unless it is exactly the succession's `old_root_last_crl`
        key = CRL_HELD_PREFIX + crl["body_hash"][:40]
        if session.get(NodeState, key) is None:
            session.add(NodeState(key=key, value=json.dumps({"crl": crl, "received_at": gc.stamp(now)})))
            _journal(session, group, "crl", crl, received_at=gc.stamp(now))
            session.flush()
        return "held"
    stored = list(session.scalars(select(GroupCrl).where(GroupCrl.group_id == group.group_id,
                                                         GroupCrl.issuer == issuer)))
    key = crl["signer"].get("key_id")
    top = max((r.crl_seq for r in stored), default=0)
    compacts = issuer != "root"  # R1-1: root CRLs are never compacted (the owner may name any as old_root_last_crl)
    for row in stored:  # an older CRL already compacted away: a newer stored one of the same key covers it
        if compacts and row.crl_seq > seq and row.body["signer"].get("key_id") == key and _covers(row.body, crl):
            _audit_once(session, "group_crl_rewind_refused", f"{issuer}:{seq}", {"crl_seq": seq, "stored": top})
            return "known"
    redundant = [r for r in stored if compacts and r.crl_seq < seq and r.body["signer"].get("key_id") == key
                 and _covers(crl, r.body)]
    if issuer != "root" and len(stored) - len(redundant) + 1 > MAX_NODE_CRLS:
        _audit_once(session, "group_crl_refused", issuer, {"crl_seq": seq, "why": "too_many_crls"})
        return "refused"  # a node spamming forks or rewinds: keep what is stored, refuse the excess
    if any(r.crl_seq == seq for r in stored):
        if _audit_once(session, "group_crl_fork", f"{issuer}:{seq}", {"crl_seq": seq, "body_hash": crl["body_hash"]}):
            notify_admins(session, f"group-crl-fork:{issuer}:{seq}",
                          "A group revocation list was forked; both are applied")
    if seq < top:
        _audit_once(session, "group_crl_rewind_refused", f"{issuer}:{seq}", {"crl_seq": seq, "stored": top})
    issued = t(body["issued_at"])
    listed = {e["serial"] for e in body["entries"]}
    missing = []
    for row in stored:
        if row.crl_seq >= seq:
            continue
        for entry in row.body["body"]["entries"]:
            cert = tr.certs.get(entry["serial"]) or tr.grants.get(entry["serial"])
            pruned = cert is not None and t(cert["body"]["not_after"]) < issued - PRUNE_AFTER
            if entry["serial"] not in listed and not pruned:
                missing.append(entry["serial"])
    if missing:
        if _audit_once(session, "group_crl_shrink", f"{issuer}:{seq}",
                       {"crl_seq": seq, "missing": sorted(set(missing))[:50]}):
            notify_admins(session, f"group-crl-shrink:{issuer}:{seq}",
                          "A group revocation list left out earlier revocations; they stay revoked")
    session.add(GroupCrl(group_id=group.group_id, issuer=issuer, crl_seq=seq, body=crl, body_hash=crl["body_hash"],
                         received_at=now))
    # Compaction (node issuers only): a lower-seq CRL of the same key whose every entry the new one lists again
    # (with a reason at least as strong) is redundant. Its entries are already in `group_revoked` and the new CRL
    # serves them to other members, so nothing is lost; it bounds storage, `state` and rebuild work.
    for old in redundant:
        session.delete(old)
    session.flush()
    _journal(session, group, "crl", crl, received_at=gc.stamp(now))
    missing = session.get(NodeState, SUCCESSION_CRL_MISSING)
    if missing is not None and crl["signer"]["kind"] == "root":
        index = int(missing.value)
        succ = (group.succession or [])[index - 1]["body"] if 1 <= index <= len(group.succession or []) else None
        if succ is not None and succ["old_root_last_crl"] == {"crl_seq": seq, "body_hash": crl["body_hash"]}:
            session.delete(missing)  # the exact old_root_last_crl arrived: retire now (R1-1)
            session.flush()
            _retire_old_root_revocations(session, group, index, now)
    return "stored"


def _covers(new: dict, old: dict) -> bool:
    """Every entry of `old` is listed in `new` with a reason at least as strong."""
    strongest: dict[str, int] = {}
    for e in new["body"]["entries"]:
        if isinstance(e, dict) and e.get("reason") in REASONS:
            strongest[e["serial"]] = max(strongest.get(e["serial"], 0), REASONS[e["reason"]])
    return all(isinstance(e, dict) and REASONS.get(e.get("reason"), 9) <= strongest.get(e.get("serial"), 0)
               for e in old["body"]["entries"])


def _audit_once(session: Session, action: str, target: str, detail: dict) -> bool:
    """Audit a diagnostic once per (action, target), so a relayed object does not re-audit every sync round."""
    exists = session.scalars(select(AuditEntry.id).where(AuditEntry.action == action,
                                                         AuditEntry.target == target[:128]).limit(1)).first()
    if exists is not None:
        return False
    audit(session, None, action, target, detail)
    session.flush()
    return True


def apply_crls(session: Session, group: OwnerGroup, now: datetime, actor: str | None = None) -> list[str]:
    """Union every valid, in-scope CRL entry into the append-only index (§1.6). Returns newly revoked serials."""
    tr = trust(session, group, now)
    revoked = tr.revoked
    added: list[str] = []
    for row in session.scalars(select(GroupCrl).where(GroupCrl.group_id == group.group_id)
                               .order_by(GroupCrl.issuer, GroupCrl.crl_seq)):
        crl = row.body
        if not crl_signature_ok(tr, crl) or _crl_scope_ok(tr, crl) == "invalid":
            continue
        signer = crl["signer"]
        for entry in crl["body"]["entries"]:
            if _entry_scope(tr, signer, entry) != "ok":
                continue  # deferred: applied once the certificate (or delegation) it names is known (A10.4)
            serial, reason = entry["serial"], entry["reason"]
            at, suspect = t(entry["revoked_at"]), entry.get("suspect_since")
            suspect_at = t(suspect) if suspect else None
            node, node_key = _entry_node(tr, signer, entry)
            current = revoked.get(serial)
            if current is None:
                current = GroupRevoked(group_id=group.group_id, serial=serial, issuer=crl["body"]["issuer"],
                                       reason=reason, node_id=node, root_public_key=node_key, revoked_at=at,
                                       suspect_since=suspect_at, first_seen_at=now)
                session.add(current)
                revoked[serial] = current
                added.append(serial)
            else:  # never removed: the strongest reason, the earliest times (§1.6)
                if REASONS[reason] > REASONS[current.reason] and (node is not None or current.node_id is None):
                    current.reason = reason
                if at < aware(current.revoked_at):  # type: ignore[operator]
                    current.revoked_at = at
                if suspect_at and (current.suspect_since is None or suspect_at < aware(current.suspect_since)):
                    current.suspect_since = suspect_at
                if current.node_id is None and node is not None:  # a later entry with authority over the node
                    current.node_id, current.root_public_key = node, node_key
            if reason in BANNING and node and node_key and signer["kind"] == "root":
                # R1-13: only root entries create stored node bans; a delegate's ban is derived in `trust()`
                ban = session.get(GroupBannedNode, (group.group_id, node))
                if ban is None:
                    session.add(GroupBannedNode(group_id=group.group_id, node_id=node, root_public_key=node_key,
                                                reason=reason, revoked_at=at, crl_serial=serial))
                    session.flush()
                elif REASONS[reason] > REASONS[ban.reason]:
                    ban.reason = reason
    if added:
        session.flush()
        audit(session, actor, "group_crl_applied", group.group_id, {"serials": added[:50]})
        for serial in added:  # §5.6: a node gone from the group ends every sign-in session it asserted
            row = revoked[serial]
            if row.node_id and row.reason in CASCADING:
                end_federated_sessions(session, group.group_id, row.node_id, f"issuer_{row.reason}", actor)
    return added


def end_federated_sessions(session: Session, group_id: str, origin_node: str, reason: str,
                           actor: str | None = None, user_id: str | None = None) -> int:
    """Delete the sign-in sessions `origin_node` asserted here (all, or one user's), in the caller's transaction."""
    q = delete(UserSession).where(UserSession.via_group == group_id, UserSession.origin_node == origin_node)
    if user_id is not None:
        q = q.where(UserSession.user_id == user_id)
    count = session.execute(q.execution_options(synchronize_session=False)).rowcount or 0
    if count:
        audit(session, actor, "group_sessions_ended", origin_node,
              {"reason": reason, "count": count, **({"user_id": user_id} if user_id else {})})
    return count


def ingest_succession(session: Session, group: OwnerGroup, obj: Any, now: datetime,
                      delivered_by: str | None = None) -> str:
    try:
        index = obj["body"]["index"]
    except (KeyError, TypeError):
        return "invalid"
    chain = root_chain(group)
    current = chain[-1]
    if type(index) is not int:
        return "invalid"
    if index <= current.index:
        active_obj = (group.succession or [])[index - 1] if index >= 1 else None
        if active_obj is not None and active_obj["body_hash"] != obj.get("body_hash") and \
                verify_succession(obj, group, index, chain[index - 1].key_id, _pinned_at(group, index)):
            _halt(session, group, index)
            return "conflict"
        return "known"
    if index != current.index + 1 or not verify_succession(obj, group, index, current.key_id, group.recovery):
        return "invalid"
    pending = list(group.succession_pending or [])
    for p in pending:
        if p["body"]["body"]["index"] == index:
            if p["body"]["body_hash"] == obj["body_hash"]:
                return "known"
            _halt(session, group, index)
            return "conflict"
    delay = obj["body"]["activation_delay_hours"]
    entry = {"body": obj, "received_at": gc.stamp(now), "activates_at": gc.stamp(now + timedelta(hours=delay))}
    if delivered_by:
        entry["delivered_by"] = delivered_by  # R1-5: its view never confirms the activation
    pending.append(entry)
    group.succession_pending = pending
    _journal(session, group, "succession", obj, received_at=entry["received_at"])
    audit(session, None, "group_succession_pending", group.group_id,
          {"index": index, "new_root_key_id": obj["body"]["new_root_key_id"], "activation_delay_hours": delay})
    notify_admins(session, f"group-succession:{index}",
                  "A root succession for your node group is pending; cancel it with a recovery file if it is not yours")
    session.flush()
    return "pending"


def _pinned_at(group: OwnerGroup, index: int) -> dict:
    """The recovery set that was pinned when succession `index` was validated."""
    if index <= 1:
        return group.genesis["body"]["recovery"]
    return group.succession[index - 2]["body"]["recovery"]


def _halt(session: Session, group: OwnerGroup, index: int) -> None:
    if group.status != "halted":
        group.status = "halted"
        audit(session, None, "group_succession_conflict", group.group_id, {"index": index})
        notify_admins(session, f"group-succession-conflict:{index}",
                      "Recovery keys of your node group are compromised: create a new group")
        session.flush()


def ingest_cancel(session: Session, group: OwnerGroup, obj: Any, now: datetime) -> str:
    if not verify_cancel(obj, group, group.recovery):
        return "invalid"
    changed = False
    pending = []
    for p in group.succession_pending or []:
        p = dict(p)
        if not p.get("cancelled") and p["body"]["body"]["index"] == obj["body"]["index"] \
                and p["body"]["body_hash"] == obj["body"]["succession_body_hash"]:
            p["cancelled"] = True
            changed = True
        pending.append(p)
    cancels = list(group.succession_cancels or [])
    if all(c["body_hash"] != obj["body_hash"] for c in cancels):
        cancels.append(obj)
        group.succession_cancels = cancels
        _journal(session, group, "cancel", obj, received_at=gc.stamp(now))
    if changed:
        group.succession_pending = pending
        audit(session, None, "group_succession_cancelled", group.group_id, {"index": obj["body"]["index"]})
        _release_held(session, group, now)
    session.flush()
    return "cancelled" if changed else "known"


def _release_held(session: Session, group: OwnerGroup, now: datetime | None = None) -> None:
    """A cancelled or activated succession releases every object held for it (§1.4 step 7, §1.8): certificates
    and grants alike are then judged on every read, by the carry rule after an activation. Held old-root CRLs
    (R1-1) are ingested again: after a cancel they are stored; after an activation only the exact
    `old_root_last_crl` passes `crl_signature_ok`, so the others are dropped."""
    for model in (GroupCert, GroupGrant):
        for row in session.scalars(select(model).where(model.group_id == group.group_id,
                                                       model.held_reason == "succession_pending")):
            row.held_reason = None
    if pending_successions(group):
        return  # another succession is still pending: its hold continues
    now = now or utcnow()
    for row in list(session.scalars(select(NodeState).where(NodeState.key.like(CRL_HELD_PREFIX + "%")))):
        held = json.loads(row.value)
        session.delete(row)
        session.flush()
        outcome = ingest_crl(session, group, held["crl"], now)
        if outcome == "stored":
            stored = session.get(GroupCrl, (group.group_id, held["crl"]["body"]["issuer"],
                                            held["crl"]["body"]["crl_seq"], held["crl"]["body_hash"]))
            if stored is not None and held.get("received_at"):
                stored.received_at = t(held["received_at"])  # it reached this node then (the R1-1 default)
        audit(session, None, "group_crl_released", held["crl"]["id"], {"outcome": outcome})


def _own_node(session: Session, group: OwnerGroup) -> str | None:
    row = session.get(GroupCert, (group.group_id, group.self_serial)) if group.self_serial else None
    return row.node_id if row is not None else None


def succession_confirmed(session: Session, group: OwnerGroup, entry: dict, now: datetime,
                         own_node: str | None = None) -> bool:
    """R1-5: a due succession activates only after a view, issued after `activates_at`, from a member other than
    this node and the one that first delivered it, that shows the succession not cancelled. With no such member
    (a two-member group, or one where only the deliverer is left) the waiver applies. A pending entry stored
    before this rule has no `delivered_by`: any other member's view confirms it."""
    index = entry["body"]["body"]["index"]
    if session.get(NodeState, SUCCESSION_CONFIRM_PREFIX + str(index)) is not None:
        return True
    own_node = own_node or _own_node(session, group)
    others = set(trust(session, group, now).members()) - {own_node, entry.get("delivered_by")}
    return not others


def note_view_for_succession(session: Session, group: OwnerGroup, view: dict, own_node: str | None,
                             now: datetime) -> None:
    """Record the R1-5 confirmation from a stored view (called by `ingest_view`)."""
    body = view["body"]
    node, issued = body.get("node_id"), t(body["issued_at"])
    if node == own_node:
        return
    for p in pending_successions(group):
        index = p["body"]["body"]["index"]
        if node == p.get("delivered_by") or issued < t(p["activates_at"]) or now < t(p["activates_at"]):
            continue
        cancelled = body.get("succession_cancelled") or []
        shows = bool(body.get("succession_pending")) or int(body.get("succession_index") or 0) >= index
        if shows and index not in cancelled and session.get(NodeState, SUCCESSION_CONFIRM_PREFIX + str(index)) is None:
            session.add(NodeState(key=SUCCESSION_CONFIRM_PREFIX + str(index), value=node))
            audit(session, None, "group_succession_confirmed", group.group_id, {"index": index, "by": node})
            session.flush()


def activate_successions(session: Session, group: OwnerGroup, now: datetime, own_node: str | None = None) -> int:
    done = 0
    while True:
        due = [p for p in pending_successions(group) if t(p["activates_at"]) <= now]
        if not due:
            return done
        entry = due[0]
        if not succession_confirmed(session, group, entry, now, own_node):
            if _audit_once(session, "group_succession_awaiting_confirmation", str(entry["body"]["body"]["index"]),
                           {"activates_at": entry["activates_at"]}):
                notify_admins(session, f"group-succession-wait:{entry['body']['body']['index']}",
                              "A root succession is due; it activates after another member confirms it on a pull")
            return done
        obj = entry["body"]
        group.succession = [*(group.succession or []), obj]
        group.root_key_id = obj["body"]["new_root_key_id"]
        group.recovery = obj["body"]["recovery"]
        group.succession_pending = [p for p in group.succession_pending or [] if p is not entry and p != entry]
        mine = session.get(NodeState, SUCCESSION_ROOT)
        held = json.loads(mine.value) if mine is not None else None
        if held and held.get("index") == obj["body"]["index"] and \
                held.get("key_id") == obj["body"]["new_root_key_id"]:
            group.root_sealed, group.root_state = held["sealed"], "holder"  # this node signed the succession
            session.delete(mine)
            audit(session, None, "group_root_activated_here", group.group_id, {"index": obj["body"]["index"]})
        elif group.root_state == "holder":
            group.root_state = "moved"  # the old G no longer signs anything (§1.8)
        audit(session, None, "group_root_succession", group.group_id,
              {"index": obj["body"]["index"], "new_root_key_id": obj["body"]["new_root_key_id"]})
        session.flush()
        _release_held(session, group, now)  # held old-root objects are now judged by the carry rule
        state = session.get(NodeState, SUCCESSION_CONFIRM_PREFIX + str(obj["body"]["index"]))
        if state is not None:
            session.delete(state)
        _journal(session, group, "activation", obj, received_at=entry["received_at"], activated_at=gc.stamp(now))
        _retire_old_root_revocations(session, group, obj["body"]["index"], now)
        session.flush()
        done += 1


def _root_valid_crls(session: Session, group: OwnerGroup, tr: Trust) -> list[GroupCrl]:
    return [r for r in session.scalars(select(GroupCrl).where(GroupCrl.group_id == group.group_id)
                                       .order_by(GroupCrl.issuer, GroupCrl.crl_seq))
            if crl_signature_ok(tr, r.body) and _crl_scope_ok(tr, r.body) != "invalid"]


def _retire_old_root_revocations(session: Session, group: OwnerGroup, index: int, now: datetime) -> list[str]:
    """R1-1, at the activation of succession `index`: the old root's revocation state becomes exactly the CRL
    named by `old_root_last_crl`. Every serial and node ban that an excluded old-root CRL touched is recomputed
    from the CRLs that remain valid (every non-root CRL, the exact `old_root_last_crl`, CRLs of current root keys):
    the strongest reason, the earliest times and any ban only from those; a serial or ban with no remaining source
    is removed. Rows the excluded CRLs never named are left as they are. Cascades already executed are not undone
    and none is re-run. If this node does not hold `old_root_last_crl` yet, nothing is retired: the marker
    `group_succession_crl_missing` fails freshness until it arrives (`ingest_crl` then retires)."""
    succ = group.succession[index - 1]["body"]
    old_key = succ["previous_root_key_id"]
    last = succ["old_root_last_crl"]
    rows = list(session.scalars(select(GroupCrl).where(GroupCrl.group_id == group.group_id, GroupCrl.issuer == "root")))
    old_rows = [r for r in rows if r.body["signer"].get("key_id") == old_key]
    exact = [r for r in old_rows if r.crl_seq == last["crl_seq"] and r.body_hash == last["body_hash"]]
    if last["crl_seq"] > 0 and not exact:
        session.merge(NodeState(key=SUCCESSION_CRL_MISSING, value=str(index)))
        if _audit_once(session, "group_succession_crl_missing", str(index), {"old_root_last_crl": last}):
            notify_admins(session, f"group-succession-crl:{index}",
                          "This node lacks the revocation list a root succession names; privileged group actions "
                          "wait until it is pulled")
        session.flush()
        return []
    excluded = [r for r in old_rows if r not in exact]
    named: set[str] = set()
    nodes: set[str] = set()
    for r in excluded:
        for e in r.body["body"]["entries"]:
            if isinstance(e, dict) and isinstance(e.get("serial"), str):
                named.add(e["serial"])
                if isinstance(e.get("node_id"), str):
                    nodes.add(e["node_id"])
    for r in excluded:
        session.delete(r)  # they fail crl_signature_ok now; `state` stops serving them
    session.flush()
    tr = trust(session, group, now)
    sources: dict[str, dict] = {}
    root_bans: dict[str, dict] = {}
    for row in _root_valid_crls(session, group, tr):
        signer = row.body["signer"]
        for e in row.body["body"]["entries"]:
            if _entry_scope(tr, signer, e) != "ok":
                continue
            node, node_key = _entry_node(tr, signer, e)
            src = sources.setdefault(e["serial"], {"reason": e["reason"], "at": t(e["revoked_at"]), "suspect": None,
                                                   "node": None, "key": None, "issuers": []})
            src["issuers"].append(row.body["body"]["issuer"])
            if REASONS[e["reason"]] > REASONS[src["reason"]] and (node is not None or src["node"] is None):
                src["reason"] = e["reason"]
            src["at"] = min(src["at"], t(e["revoked_at"]))
            if e.get("suspect_since"):
                s_at = t(e["suspect_since"])
                src["suspect"] = s_at if src["suspect"] is None else min(src["suspect"], s_at)
            if src["node"] is None and node is not None:
                src["node"], src["key"] = node, node_key
            if signer["kind"] == "root" and e["reason"] in BANNING and node and node_key:
                ban = root_bans.get(node)
                if ban is None or REASONS[e["reason"]] > REASONS[ban["reason"]]:
                    root_bans[node] = {"reason": e["reason"], "at": t(e["revoked_at"]), "serial": e["serial"],
                                       "key": node_key}
    retired: list[str] = []
    changed: list[str] = []
    for serial in sorted(named):
        row = session.get(GroupRevoked, (group.group_id, serial))
        if row is None:
            continue
        src = sources.get(serial)
        if src is None:
            session.delete(row)
            retired.append(serial)
            continue
        new = {"reason": src["reason"], "revoked_at": src["at"], "suspect_since": src["suspect"],
               "node_id": src["node"], "root_public_key": src["key"],
               "issuer": row.issuer if row.issuer in src["issuers"] else src["issuers"][0]}
        if any((aware(getattr(row, k)) if isinstance(getattr(row, k), datetime) else getattr(row, k)) != v
               for k, v in new.items()):
            for k, v in new.items():
                setattr(row, k, v)
            changed.append(serial)
    for node in sorted(nodes):
        ban = session.get(GroupBannedNode, (group.group_id, node))
        if ban is None:
            continue
        keep = root_bans.get(node)
        if keep is None:
            session.delete(ban)
            retired.append(f"ban:{node}")
        elif (ban.reason, ban.crl_serial) != (keep["reason"], keep["serial"]):
            ban.reason, ban.crl_serial, ban.revoked_at = keep["reason"], keep["serial"], keep["at"]
            changed.append(f"ban:{node}")
    if retired or changed:
        audit(session, None, "group_revocation_retired_by_succession", group.group_id,
              {"index": index, "retired": retired[:50], "changed": changed[:50]})
        notify_admins(session, f"group-retired:{index}",
                      "A root succession retired revocations that only the replaced root key had signed")
    marker = session.get(NodeState, R1_RETIRE_DONE)
    if marker is None or int(marker.value) < index:
        session.merge(NodeState(key=R1_RETIRE_DONE, value=str(index)))
    session.flush()
    return retired


def ingest_root_moved(session: Session, group: OwnerGroup, obj: Any, own_node: str, now: datetime) -> str:
    try:
        tr = trust(session, group, now)
        if not gc.well_formed(obj, group.group_id) or obj["t"] != "group.root_moved":
            return "invalid"
        key = tr.root_key_for(obj)
        if key is None or key.key_id != group.root_key_id or not gc.verify_object(obj, key.public):
            return "invalid"
        body = obj["body"]
        if not NODE_ID.match(body["to_node"]) or t(body["issued_at"]) > now + SKEW:
            return "invalid"
    except (KeyError, TypeError, gc.GroupCryptoError):
        return "invalid"
    current = group.root_moved
    if current is not None and (current["body_hash"] == obj["body_hash"]
                                or t(current["body"]["issued_at"]) >= t(body["issued_at"])):
        return "known"
    group.root_moved = obj
    if body["to_node"] != own_node and group.root_state == "holder":
        group.root_state = "moved"  # two nodes never keep issuing root objects (§1.9)
        audit(session, None, "group_root_moved", group.group_id, {"to_node": body["to_node"], "disabled_here": True})
        notify_admins(session, "group-root-moved", "The group root moved to another member; it is disabled here")
    session.flush()
    return "stored"


def ingest_view(session: Session, group: OwnerGroup, obj: Any, now: datetime, tr: Trust | None = None,
                own_node: str | None = None) -> str:
    tr = tr or trust(session, group, now)
    try:
        if not gc.well_formed(obj, group.group_id) or obj["t"] != "group.view":
            return "invalid"
        body = obj["body"]
        node = body["node_id"]
        if obj["signer"]["kind"] != "node" or obj["signer"]["id"] != node:
            return "invalid"  # signer/body agreement (§1.3)
        if obj["id"] != f"view:{node}/{body['issued_at']}" or tr.in_force(node) is None or not tr.signature_ok(obj):
            return "invalid"
        issued = t(body["issued_at"])
        if issued > now + SKEW or not isinstance(body["crl_seq"], dict):
            return "invalid"
    except (KeyError, TypeError, gc.GroupCryptoError):
        return "invalid"
    row = session.get(GroupView, (group.group_id, node))
    if row is not None and aware(row.issued_at) >= issued:  # type: ignore[operator]
        return "known"
    if row is None:
        session.add(GroupView(group_id=group.group_id, node_id=node, body=obj, issued_at=issued, received_at=now))
    else:
        row.body, row.issued_at, row.received_at = obj, issued, now
    session.flush()
    if pending_successions(group):
        note_view_for_succession(session, group, obj, own_node or _own_node(session, group), now)
    return "stored"


def ingest_rotations(session: Session, group: OwnerGroup, node_id: str, history: Any, now: datetime,
                     tr: Trust | None = None) -> str:
    """A member's validated signing-key history, with fork protection and key uniqueness (§1.7)."""
    tr = tr or trust(session, group, now)
    root_public = tr.root_public(node_id)
    if root_public is None or not isinstance(history, list):
        return "unknown"
    tip = history[-1]["body"]["key_id"] if history and isinstance(history[-1], dict) else node_id
    try:
        validated, keys = key_rotation.validate_history(node_id, root_public, history, tip)
    except (key_rotation.RotationError, KeyError, TypeError):
        return "invalid"
    others = set()
    for other in tr.nodes():
        if other != node_id:
            others |= tr.all_keys(other)
            cert = tr.in_force(other)
            if cert:
                others.add(cert["body"]["member"]["box_public_key"])
    if set(keys.values()) & others:
        audit(session, None, "group_rotation_fork", node_id, {"why": "key_reused_from_another_member"})
        return "refused"
    row = session.get(GroupMember, (group.group_id, node_id))
    old = list(row.rotations or []) if row is not None else []
    if len(validated) < len(old) or validated[:len(old)] != old:
        if validated == old[:len(validated)]:
            return "known"  # an older copy of the same history
        audit(session, None, "group_rotation_fork", node_id, {"stored": len(old), "received": len(validated)})
        notify_admins(session, f"group-rotation-fork:{node_id}", "A group member's signing-key history forked")
        if row is not None:
            row.status = "fork"
        return "fork"
    if row is not None and validated != old:
        row.rotations = validated
        session.flush()
        return "stored"
    if row is None:
        session.info.setdefault("group_rotations", {})[node_id] = validated
    return "known" if validated == old else "stored"


# ---- rebuild and state -----------------------------------------------------------------------------------------

def rebuild(session: Session, group: OwnerGroup, own_node: str, now: datetime | None = None,
            settings: Settings | None = None) -> Trust:
    """Activate due successions, union CRLs, and recompute the derived member rows in this transaction."""
    now = now or utcnow()
    activate_successions(session, group, now, own_node)
    _review1_heal(session, group, now)
    apply_crls(session, group, now)
    tr = trust(session, group, now, own_node=own_node)
    pending_rotations = session.info.pop("group_rotations", {})
    tr.rotations.update(pending_rotations)
    existing = {m.node_id: m for m in session.scalars(select(GroupMember).where(GroupMember.group_id == group.group_id))}
    for node in tr.nodes():
        cert = tr.in_force(node)
        latest = cert or max(tr.node_certs(node), key=lambda c: parse_serial(c["body"]["serial"]).order)  # type: ignore[union-attr]
        b = latest["body"]
        row = existing.get(node)
        status = tr.status_of(node)
        if row is not None and row.status == "fork" and status in ("active", "expiring"):
            status = "fork"  # a forked history halts that origin until revocation or re-enrolment
        values = {"root_public_key": b["member"]["root_public_key"], "box_public_key": b["member"]["box_public_key"],
                  "url": b["member"]["url"], "name": b["member"]["name"], "serial": b["serial"],
                  "scopes": list(b["scopes"]), "delegate": b["delegate"], "not_after": t(b["not_after"]),
                  "status": status}
        if row is None:
            row = GroupMember(group_id=group.group_id, node_id=node, rotations=tr.rotations.get(node, []), **values)
            session.add(row)
        else:
            for k, v in values.items():
                setattr(row, k, v)
            if node in pending_rotations:
                row.rotations = pending_rotations[node]
    own = tr.in_force(own_node)
    if own is not None:
        group.self_serial = own["body"]["serial"]
        if group.box_pending_sealed and settings is not None:
            pending = gc.x_from_raw(open_secret(settings, group.box_pending_sealed))
            if gc.x_public_b64(pending) == own["body"]["member"]["box_public_key"]:
                group.box_previous_sealed, group.box_previous_until = group.box_sealed, now + timedelta(minutes=15)
                group.box_sealed, group.box_pending_sealed = group.box_pending_sealed, None
                audit(session, None, "group_box_rekeyed", own_node, {"serial": own["body"]["serial"]})
    elif group.status in ("active", "resyncing") and tr.node_certs(own_node):
        status = tr.status_of(own_node)
        if status in ("revoked", "left"):
            group.status = status
    group.state_hash = state_hash(session, group)
    session.flush()
    return tr


def _review1_heal(session: Session, group: OwnerGroup, now: datetime) -> None:
    """One-time repairs of databases written before outside review #1: retire old-root revocations of
    successions already active (R1-1), and re-derive node bans that only a non-root signer justified (R1-13)."""
    done = session.get(NodeState, R1_RETIRE_DONE)
    upto = int(done.value) if done is not None else 0
    for index in range(upto + 1, len(group.succession or []) + 1):
        if session.get(NodeState, SUCCESSION_CRL_MISSING) is not None:
            break
        _retire_old_root_revocations(session, group, index, now)
    if session.get(NodeState, R1_BANS_DONE) is not None:
        return
    tr = trust(session, group, now)
    supported: dict[str, str] = {}
    for row in _root_valid_crls(session, group, tr):
        if row.body["signer"]["kind"] != "root":
            continue
        for e in row.body["body"]["entries"]:
            if _entry_scope(tr, row.body["signer"], e) == "ok" and e["reason"] in BANNING and e.get("node_id"):
                if REASONS[e["reason"]] > REASONS.get(supported.get(e["node_id"], ""), 0):
                    supported[e["node_id"]] = e["reason"]
    rederived = []
    for ban in list(session.scalars(select(GroupBannedNode).where(GroupBannedNode.group_id == group.group_id))):
        if ban.node_id not in supported:
            session.delete(ban)
            rederived.append(ban.node_id)
        elif REASONS[supported[ban.node_id]] < REASONS[ban.reason]:
            ban.reason = supported[ban.node_id]
            rederived.append(ban.node_id)
    if rederived:
        audit(session, None, "group_ban_rederived", group.group_id, {"nodes": rederived[:50]})
    session.add(NodeState(key=R1_BANS_DONE, value="1"))
    session.flush()


def state_hash(session: Session, group: OwnerGroup) -> str:
    certs = sorted((c.serial, c.body_hash) for c in session.scalars(select(GroupCert).where(
        GroupCert.group_id == group.group_id, GroupCert.held_reason.is_(None))))
    crls = sorted((c.issuer, c.crl_seq, c.body_hash) for c in session.scalars(select(GroupCrl).where(
        GroupCrl.group_id == group.group_id)))
    rotations = sorted((m.node_id, len(m.rotations or [])) for m in session.scalars(select(GroupMember).where(
        GroupMember.group_id == group.group_id)))
    data = {"g": group.genesis["body_hash"], "s": [s["body_hash"] for s in group.succession or []],
            "p": [p["body"]["body_hash"] for p in group.succession_pending or []],
            "c": [c["body_hash"] for c in group.succession_cancels or []],
            "m": group.root_moved["body_hash"] if group.root_moved else None, "certs": certs, "crls": crls,
            "rot": rotations}
    grants = sorted((g.serial, g.body_hash) for g in session.scalars(select(GroupGrant).where(
        GroupGrant.group_id == group.group_id, GroupGrant.held_reason.is_(None))))
    if grants:  # left out while a group has none, so a grant-free group hashes as it did before A12
        data["grants"] = grants
    return gc.sha256_hex(canonical_json(data))


def crl_seqs(session: Session, group_id: str) -> dict[str, int]:
    seqs: dict[str, int] = {}
    for row in session.scalars(select(GroupCrl).where(GroupCrl.group_id == group_id)):
        seqs[row.issuer] = max(seqs.get(row.issuer, 0), row.crl_seq)
    return seqs


def crl_tops(session: Session, group_id: str) -> dict[str, list]:
    """{issuer: [top crl_seq, sorted body_hashes at that seq]} (R1-2: a view commits to CRL content)."""
    tops: dict[str, list] = {}
    for row in session.scalars(select(GroupCrl).where(GroupCrl.group_id == group_id)):
        top = tops.get(row.issuer)
        if top is None or row.crl_seq > top[0]:
            tops[row.issuer] = [row.crl_seq, [row.body_hash]]
        elif row.crl_seq == top[0]:
            top[1].append(row.body_hash)
    return {k: [v[0], sorted(set(v[1]))] for k, v in tops.items()}


def make_view(session: Session, group: OwnerGroup, identity: NodeIdentity, now: datetime | None = None,
              force: bool = False) -> dict | None:
    """Sign this node's freshness view, at most once a minute (§1.6)."""
    now = now or utcnow()
    row = session.get(GroupView, (group.group_id, identity.node_id))
    if row is not None and not force and now - aware(row.issued_at) < VIEW_MIN_INTERVAL:  # type: ignore[operator]
        return row.body
    issued = gc.stamp(now)
    body = {"node_id": identity.node_id, "state_hash": group.state_hash or state_hash(session, group),
            "crl_seq": crl_seqs(session, group.group_id),
            "revoked_count": len(_revoked_map(session, group.group_id)),
            "succession_index": len(group.succession or []), "succession_pending": bool(pending_successions(group)),
            "issued_at": issued,
            # additive body fields (R1-2, R1-5); an A12 node's `ingest_view` accepts and ignores them
            "crl_top": crl_tops(session, group.group_id),
            "succession_cancelled": sorted(p["body"]["body"]["index"] for p in group.succession_pending or []
                                           if p.get("cancelled"))}
    obj = gc.make_object("group.view", group.group_id, f"view:{identity.node_id}/{issued}", body,
                         {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, identity.private)
    if row is None:
        session.add(GroupView(group_id=group.group_id, node_id=identity.node_id, body=obj, issued_at=t(issued),
                              received_at=now))
    else:
        row.body, row.issued_at, row.received_at = obj, t(issued), now
    session.flush()
    return obj


def export_state(session: Session, group: OwnerGroup, identity: NodeIdentity, now: datetime | None = None) -> dict:
    """The `state` op result (§3.3): everything a member needs to verify membership offline."""
    now = now or utcnow()
    if group.status in ("active", "resyncing") and group.self_serial:
        make_view(session, group, identity, now)
    certs = [c.body for c in session.scalars(select(GroupCert).where(
        GroupCert.group_id == group.group_id, GroupCert.held_reason.is_(None)).order_by(GroupCert.serial))]
    crls = [c.body for c in session.scalars(select(GroupCrl).where(GroupCrl.group_id == group.group_id)
                                            .order_by(GroupCrl.issuer, GroupCrl.crl_seq))]
    # R1-1: a pending succession travels with the exact old_root_last_crl, even while it is held here
    wanted = {(p["body"]["body"]["old_root_last_crl"]["crl_seq"], p["body"]["body"]["old_root_last_crl"]["body_hash"])
              for p in pending_successions(group)}
    for row in session.scalars(select(NodeState).where(NodeState.key.like(CRL_HELD_PREFIX + "%"))):
        held = json.loads(row.value)["crl"]
        if (held["body"]["crl_seq"], held["body_hash"]) in wanted:
            crls.append(held)
    rotations = {m.node_id: m.rotations for m in session.scalars(select(GroupMember).where(
        GroupMember.group_id == group.group_id)) if m.rotations}
    own = key_rotation.own_history(session, identity.node_id)
    if own:
        rotations[identity.node_id] = own
    else:
        rotations.pop(identity.node_id, None)
    views = [v.body for v in session.scalars(select(GroupView).where(GroupView.group_id == group.group_id))]
    grants = [g.body for g in session.scalars(select(GroupGrant).where(
        GroupGrant.group_id == group.group_id, GroupGrant.held_reason.is_(None)).order_by(GroupGrant.serial))]
    return {"genesis": group.genesis, "grants": grants, "succession": list(group.succession or []),
            "succession_pending": [p["body"] for p in group.succession_pending or []],
            "succession_cancel": list(group.succession_cancels or []), "root_moved": group.root_moved,
            "certs": certs, "crls": crls, "rotations": rotations, "views": views,
            "state_hash": group.state_hash or state_hash(session, group)}


def ingest_state(session: Session, group: OwnerGroup, state: Any, own_node: str, now: datetime | None = None,
                 settings: Settings | None = None, served_by: str | None = None) -> dict[str, int]:
    """Apply a member's `state` in the §3.8 order: successions and cancels, root_moved, CRLs, certificates,
    rotations, views. Every object is verified here; nothing is trusted because of who served it."""
    now = now or utcnow()
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    if not isinstance(state, dict):
        raise GroupError("malformed state")
    if state.get("genesis", {}).get("body_hash") != group.genesis["body_hash"]:
        raise GroupError("the member serves a different genesis for this group (§1.1); refused", "conflict")
    # Halted (conflicting successions, §1.8): no root-signed object, succession, enrolment or move is accepted,
    # but revocation keeps working, so CRLs, the rotations needed to verify them, and views are still taken.
    halted = group.status == "halted"
    if not halted:
        for obj in list(state.get("succession") or []) + list(state.get("succession_pending") or []):
            bump("succession:" + ingest_succession(session, group, obj, now, delivered_by=served_by))
            if group.status == "halted":
                break
    halted = group.status == "halted"
    if not halted:
        for obj in state.get("succession_cancel") or []:
            bump("cancel:" + ingest_cancel(session, group, obj, now))
        activate_successions(session, group, now, own_node)
        if state.get("root_moved"):
            bump("root_moved:" + ingest_root_moved(session, group, state["root_moved"], own_node, now))
    certs = [c for c in state.get("certs") or [] if isinstance(c, dict)]
    rotations = state.get("rotations") if isinstance(state.get("rotations"), dict) else {}

    def take_rotations(count: bool) -> None:
        for node, history in rotations.items():
            if node != own_node and isinstance(node, str):
                outcome = ingest_rotations(session, group, node, history, now)
                if count:
                    bump("rotations:" + outcome)

    # Root certificates, then rotations (their root keys are now known), then delegate-signed certificates,
    # CRLs and views, with one rebuild between the steps that depend on each other.
    if not halted:
        for c in certs:
            if c.get("signer", {}).get("kind") == "root":
                bump("cert:" + ingest_cert(session, group, c, now))
    take_rotations(count=False)
    rebuild(session, group, own_node, now, settings)
    if not halted:
        tr = trust(session, group, now, own_node=own_node)
        for c in certs:
            if c.get("signer", {}).get("kind") != "root":
                bump("cert:" + ingest_cert(session, group, c, now, tr))
                tr = trust(session, group, now, own_node=own_node)
    take_rotations(count=True)  # nodes first certified by a delegate in this state
    rebuild(session, group, own_node, now, settings)
    tr = trust(session, group, now, own_node=own_node)
    if not halted:  # grants (§6.1) after every certificate their issuers' authority rests on, before the CRLs
        grants = [g for g in state.get("grants") or [] if isinstance(g, dict)]
        for g in sorted(grants, key=lambda g: g.get("signer", {}).get("kind") != "root"):
            bump("grant:" + ingest_grant(session, group, g, now, tr))
        tr = trust(session, group, now, own_node=own_node)
    for crl in state.get("crls") or []:
        bump("crl:" + ingest_crl(session, group, crl, now, tr))
    for view in state.get("views") or []:
        bump("view:" + ingest_view(session, group, view, now, tr, own_node))
    rebuild(session, group, own_node, now, settings)
    return counts


def refresh(session: Session, group: OwnerGroup, own_node: str, now: datetime | None = None,
            settings: Settings | None = None) -> Trust:
    """For read paths (the admin page, a member's `state` call): rebuild only when the stored objects changed or
    a succession is due; otherwise read without writing. The group loop rebuilds every round regardless."""
    now = now or utcnow()
    due = any(t(p["activates_at"]) <= now for p in pending_successions(group))
    if due or group.state_hash is None or group.state_hash != state_hash(session, group):
        return rebuild(session, group, own_node, now, settings)
    return trust(session, group, now, own_node=own_node)


# ---- freshness (§1.6) ------------------------------------------------------------------------------------------

@dataclass
class Freshness:
    ok: bool
    waived: bool = False
    behind_from: str | None = None  # a member whose view announces newer CRLs or successions: pull from it
    view_age: float | None = None
    reason: str = ""


_WAIVED_SEEN: set[tuple] = set()


def _view_ahead(session: Session, group: OwnerGroup, body: dict, ours: dict[str, int], index: int) -> bool:
    """Does a view announce revocations or a succession this node does not hold (R1-2)? A higher top seq, an equal
    top seq with a body_hash this node does not store, or a higher succession index. A view without `crl_top` (an
    A12 member) is compared by seq only."""
    if any(ours.get(issuer, 0) < seq for issuer, seq in body["crl_seq"].items()) or body["succession_index"] > index:
        return True
    tops = body.get("crl_top")
    if not isinstance(tops, dict) or body["succession_index"] != index:
        return False  # a member behind on successions may still list CRLs this node retired (R1-1)
    for issuer, top in tops.items():
        try:
            seq, hashes = int(top[0]), list(top[1])
        except (TypeError, ValueError, IndexError):
            return True  # a malformed commitment is not evidence that this node is current
        if seq != ours.get(issuer, 0):
            continue  # lower: this node is ahead; higher: caught by crl_seq above
        held = set(session.scalars(select(GroupCrl.body_hash).where(
            GroupCrl.group_id == group.group_id, GroupCrl.issuer == issuer, GroupCrl.crl_seq == seq)))
        if any(h not in held for h in hashes):
            return True
    return False


def view_unbacked(session: Session, node: str, issued: datetime) -> bool:
    row = session.get(NodeState, VIEW_UNBACKED_PREFIX + node)
    return row is not None and issued <= t(row.value)


def check_view_backed(session: Session, group: OwnerGroup, node: str) -> bool:
    """After pulling `state` from `node` itself: does its newest view announce CRLs that its own state did not
    deliver? Such a view is ignored by freshness until a newer view of `node` is backed (R1-2): one compromised
    member cannot block privileged actions group-wide by announcing a CRL that does not exist."""
    row = session.get(GroupView, (group.group_id, node))
    if row is None:
        return True
    ours = crl_seqs(session, group.group_id)
    backed = not _view_ahead(session, group, row.body["body"], ours, len(group.succession or []))
    key = VIEW_UNBACKED_PREFIX + node
    marker = session.get(NodeState, key)
    if backed:
        if marker is not None:
            session.delete(marker)
            session.flush()
        return True
    session.merge(NodeState(key=key, value=row.body["body"]["issued_at"]))
    if _audit_once(session, "group_view_unbacked", f"{node}:{row.body['body']['issued_at']}",
                   {"node": node, "issued_at": row.body["body"]["issued_at"]}):
        notify_admins(session, f"group-view-unbacked:{node}",
                      f"Member {gc_fp(node)} announces revocations it does not serve; its views are ignored")
    session.flush()
    return False


def freshness(session: Session, group: OwnerGroup, own_node: str, depends_on: str | None, max_age_hours: int,
              now: datetime | None = None, tr: Trust | None = None, action: str | None = None) -> Freshness:
    """May this node take a privileged action that depends on `depends_on`'s authority now? A caller that already
    built `trust(..., own_node=own_node)` for this request passes it, so the certificate set is loaded once.

    The exact guarantee (C10 §C10.17 R1-2): the action happens only if a member other than this node and
    `depends_on` signed, within `max_age_hours`, a view whose CRL heads (seq and hash) this node already holds. A
    two-member group waives this (audited once per `action`), except after `resync --skip` (R1-3)."""
    now = now or utcnow()
    if group.status == "resyncing":
        return Freshness(False, reason="resyncing")
    if group.status == "halted":
        return Freshness(False, reason="halted")
    if session.get(NodeState, SUCCESSION_CRL_MISSING) is not None:
        return Freshness(False, reason="succession_crl_missing")
    skipped = session.get(NodeState, TRUST_EPOCH)
    tr = tr or trust(session, group, now, own_node=own_node)
    live = set(tr.members()) - {own_node, depends_on}
    if not live:
        if skipped is not None:
            return Freshness(False, reason="resync_skipped")
        seen = (group.group_id, own_node, action, depends_on)
        if action and seen not in _WAIVED_SEEN:
            _WAIVED_SEEN.add(seen)  # per process: the audit table is read once, not on every request
            _audit_once(session, "group_freshness_waived", f"{action}:{depends_on or '-'}",
                        {"action": action, "depends_on": depends_on})
        return Freshness(True, waived=True, reason="freshness cannot be confirmed by a third node")
    ours = crl_seqs(session, group.group_id)
    index = len(group.succession or [])
    newest = None
    for view in session.scalars(select(GroupView).where(GroupView.group_id == group.group_id)):
        if view.node_id not in live or view_unbacked(session, view.node_id, aware(view.issued_at)):  # type: ignore[arg-type]
            continue
        body = view.body["body"]
        if _view_ahead(session, group, body, ours, index):
            return Freshness(False, behind_from=view.node_id, reason="a member announces newer revocations")
        age = (now - aware(view.issued_at)).total_seconds()  # type: ignore[operator]
        newest = age if newest is None else min(newest, age)
    if newest is None or newest > max_age_hours * 3600:
        return Freshness(False, view_age=newest, reason="no third-party view newer than the limit")
    return Freshness(True, view_age=newest)


# ---- create, root unlock, issue, revoke (§1.1-1.6) ---------------------------------------------------------------

def _names(settings: Settings, user: User | None, group_name: str) -> list[str]:
    import socket

    names = [settings.app_name, settings.node_operator, group_name, socket.gethostname()]
    if user is not None:
        names.append(user.username)
    return [n for n in names if n]


@dataclass
class Created:
    group: OwnerGroup
    recovery_files: list[dict]


def _box_new(settings: Settings) -> tuple[str, str]:
    box = X25519PrivateKey.generate()
    return seal_secret(settings, gc.x_private_raw(box)), gc.x_public_b64(box)


def own_member(settings: Settings, identity: NodeIdentity, box_public: str, name: str | None = None,
               url: str | None = None, rev: int = 1) -> dict:
    return {"node_id": identity.node_id, "root_public_key": identity.trust_public_b64, "box_public_key": box_public,
            "url": url or settings.public_url, "name": gc.nfc(name or settings.app_name)[:80], "rev": rev}


def create_group(session: Session, settings: Settings, identity: NodeIdentity, name: str, root_passphrase: str,
                 recovery_passphrases: list[str], threshold: int = 1, user: User | None = None,
                 member_name: str | None = None, now: datetime | None = None, cli: bool = False) -> Created:
    """Genesis, the sealed root on this node (H22), mandatory recovery files, and this node's own certificate."""
    now = now or utcnow()
    if load(session) is not None and load(session).status not in ("left", "revoked"):  # type: ignore[union-attr]
        raise GroupError("this node is already in a group (one group per node, D10.5)", "conflict")
    if not secrets_box.available(settings):
        raise GroupError("owner groups need ATLAS_SECRET_KEY (D10.4)", "forbidden")
    name = gc.nfc((name or "").strip())
    if not 1 <= len(name) <= 80:
        raise GroupError("the group name is 1-80 characters")
    count = len(recovery_passphrases)
    if (threshold, count) not in ((1, 1), (2, 3)):
        raise GroupError("recovery is 1-of-1 or 2-of-3")
    names = _names(settings, user, name)
    try:
        gc.check_passphrase(root_passphrase, names)
        for phrase in recovery_passphrases:
            gc.check_passphrase(phrase, names)
    except gc.WeakPassphrase as exc:
        raise GroupError(f"passphrase refused: {exc}") from None
    if any(phrase == root_passphrase for phrase in recovery_passphrases):
        raise GroupError("use a recovery-file passphrase that differs from the root passphrase")
    clean_member_url(settings.public_url, settings.environment == "production")
    old = load(session)
    if old is not None:  # a group this node left: its rows go before a new genesis
        session.delete(old)
        session.flush()
    root = Ed25519PrivateKey.generate()
    root_pub = gc.ed_public_b64(root)
    group_id = gc.key_id_of(root_pub)
    recovery_keys = [Ed25519PrivateKey.generate() for _ in range(count)]
    recovery = {"threshold": threshold, "keys": [{"key_id": gc.key_id_of(gc.ed_public_b64(k)),
                                                  "public_key": gc.ed_public_b64(k)} for k in recovery_keys]}
    body = {"name": name, "created_at": gc.stamp(now), "root_public_key": root_pub, "recovery": recovery,
            "policy": dict(DEFAULT_POLICY)}
    genesis = gc.make_object("group.genesis", group_id, "genesis", body,
                             {"kind": "root", "id": group_id, "key_id": group_id}, root)
    genesis["pop"] = [{"kind": "recovery", "key_id": gc.key_id_of(gc.ed_public_b64(k)), "sig": gc.co_sign(genesis, k)}
                      for k in recovery_keys]
    assert verify_genesis(genesis)
    # One wrap of G under the root passphrase, two copies: the node's sealed root and, for 1-of-1, the recovery
    # file (v2, R1-5). The file never holds raw G, so cracking its passphrase yields R only.
    wrapped_root = gc.wrap_root(root, root_passphrase, group_id)
    files = [gc.make_recovery_file(genesis, [], k, None, recovery_passphrases[i], gc.stamp(now),
                                   root_wrapped=wrapped_root if count == 1 else None)
             for i, k in enumerate(recovery_keys)]
    del recovery_keys  # R is dropped from memory (§1.2)
    box_sealed, box_public = _box_new(settings)
    group = OwnerGroup(group_id=group_id, name=name, genesis=genesis, succession=[], root_key_id=group_id,
                       recovery=recovery, root_sealed=seal_secret(settings, canonical_json(wrapped_root)),
                       box_sealed=box_sealed, root_state="holder", status="active", joined_at=now, updated_at=now)
    session.add(group)
    session.flush()
    member = own_member(settings, identity, box_public, member_name)
    pop = gc.member_pop(identity.private, group_id, member, identity.key_id)
    issue_cert(session, group, identity, member, pop, list(SCOPES[:3]), None, signer_root=root, days=CERT_DAYS,
               url_verified=True, now=now, actor=user.id if user else None, cli=cli)
    stored = seal_secret(settings, json.dumps(files).encode())
    session.merge(NodeState(key=RECOVERY_PENDING, value=stored))
    audit(session, user.id if user else None, "group_created", group_id,
          {"name": name, "recovery": f"{threshold}-of-{count}"}, cli)
    session.flush()
    return Created(group, files)


def recovery_pending(session: Session) -> bool:
    return session.get(NodeState, RECOVERY_PENDING) is not None


def pending_recovery_files(session: Session, settings: Settings) -> list[dict]:
    row = session.get(NodeState, RECOVERY_PENDING)
    if row is None:
        return []
    return json.loads(open_secret(settings, row.value))


def confirm_recovery(session: Session, settings: Settings, group: OwnerGroup, passphrase: str,
                     actor: str | None = None) -> None:
    """Finish creation: the downloaded recovery file's passphrase is confirmed a second time (§1.9)."""
    files = pending_recovery_files(session, settings)
    if not files:
        raise GroupError("there is no recovery file waiting for confirmation", "conflict")
    try:
        keys = gc.open_recovery_file(files[0], passphrase)
    except gc.WrongPassphrase:
        raise GroupError("that is not the recovery-file passphrase") from None
    if keys.group_id != group.group_id:
        raise GroupError("that recovery file belongs to another group")
    session.delete(session.get(NodeState, RECOVERY_PENDING))
    holds = keys.root is not None and gc.key_id_of(gc.ed_public_b64(keys.root)) == group.root_key_id
    if keys.root_wrapped is not None and group.root_sealed:
        # v2: the file carries the very wrap sealed on this node (one wrap, two copies), so it holds the current G
        # under the current root passphrase without asking for that passphrase here
        holds = json.loads(open_secret(settings, group.root_sealed)) == keys.root_wrapped
    if holds:
        session.merge(NodeState(key=RECOVERY_VERIFIED, value=gc.stamp()))
    audit(session, actor, "group_root_exported", group.group_id, {"confirmed": True})


def verify_recovery_root(keys: gc.RecoveryKeys, group: OwnerGroup, root_passphrase: str | None) -> bool:
    """`root verify-recovery`: does the file hold the group's current G? A v2 file needs the root passphrase too
    (it unwraps G and compares the public key), so the §1.9 leave rule asks for both (R1-5)."""
    if not keys.holds_root:
        return False
    try:
        root = gc.recovery_root(keys, root_passphrase)
    except gc.WrongPassphrase:
        raise GroupError("wrong root passphrase for the root key in this recovery file", "forbidden") from None
    return gc.key_id_of(gc.ed_public_b64(root)) == group.root_key_id


def skip_resync(session: Session, group: OwnerGroup, actor: str | None = None, cli: bool = False) -> None:
    """`continue without resync` starts a new trust epoch (R1-3): the two-member waiver is off, and every
    privileged action needs a fresh third-party view, until a full pull succeeds (or `--confirm-root`)."""
    group.status = "active"
    session.merge(NodeState(key=TRUST_EPOCH, value=f"skipped:{gc.stamp()}"))
    audit(session, actor, "group_trust_epoch_skipped", group.group_id, {}, cli)
    session.flush()


def confirm_trust_epoch(session: Session, settings: Settings, group: OwnerGroup, passphrase: str,
                        actor: str | None = None, cli: bool = False) -> None:
    """`resync --confirm-root`: the owner ends a skipped trust epoch with the root passphrase (R1-3)."""
    if session.get(NodeState, TRUST_EPOCH) is None:
        raise GroupError("no skipped resync to confirm", "conflict")
    unlock_root(session, settings, group, passphrase)
    session.delete(session.get(NodeState, TRUST_EPOCH))
    audit(session, actor, "group_resync_confirmed", group.group_id, {}, cli)
    session.flush()


def unlock_root(session: Session, settings: Settings, group: OwnerGroup, passphrase: str) -> Ed25519PrivateKey:
    """G, in memory for one operation (§1.2). Refused while resyncing, halted, moved or a succession is pending."""
    if group.root_state != "holder" or not group.root_sealed:
        raise GroupError("the group root is not on this node", "forbidden")
    if group.status == "resyncing":
        raise GroupError("this node is resyncing; root operations wait until it has pulled every member's state",
                         "conflict")
    if group.status == "halted":
        raise GroupError("the group is halted: conflicting root successions", "conflict")
    if pending_successions(group):
        raise GroupError("a root succession is pending; root operations are refused until it is decided", "conflict")
    if recovery_pending(session):
        raise GroupError("download the recovery file and confirm its passphrase first", "conflict")
    wrapped = json.loads(open_secret(settings, group.root_sealed))
    try:
        root = gc.unwrap_root(wrapped, passphrase, group.group_id)
    except gc.WrongPassphrase:
        raise GroupError("wrong root passphrase", "forbidden") from None
    if gc.key_id_of(gc.ed_public_b64(root)) != group.root_key_id:
        raise GroupError("the sealed root key is not the group's current root key", "forbidden")
    return root


def next_root_n(session: Session, group_id: str) -> int:
    ns = [r.serial_n for r in session.scalars(select(GroupCert).where(GroupCert.group_id == group_id,
                                                                      GroupCert.issuer == "root"))]
    ns += [r.serial_n for r in session.scalars(select(GroupGrant).where(GroupGrant.group_id == group_id,
                                                                       GroupGrant.issuer == "root"))]
    ns += [s.n for r in session.scalars(select(GroupRevoked).where(GroupRevoked.group_id == group_id))
           if (s := parse_serial(r.serial)) is not None and s.issuer == "root"]
    return max(ns, default=0) + 1


def next_node_n(session: Session, group_id: str, node_id: str) -> int:
    """Certificates and grants of one delegate share its serial space; `max_serials` bounds both (§1.3)."""
    ns = [r.serial_n for r in session.scalars(select(GroupCert).where(GroupCert.group_id == group_id,
                                                                      GroupCert.issuer == node_id))]
    ns += [r.serial_n for r in session.scalars(select(GroupGrant).where(GroupGrant.group_id == group_id,
                                                                       GroupGrant.issuer == node_id))]
    return max(ns, default=0) + 1


def issue_cert(session: Session, group: OwnerGroup, identity: NodeIdentity, member: dict, pop: dict,
               scopes: list[str], delegate: dict | None, *, signer_root: Ed25519PrivateKey | None, days: int,
               enrolment: dict | None = None, url_verified: bool = False, readmit: str | None = None,
               now: datetime | None = None, actor: str | None = None, cli: bool = False,
               parent_serial: str | None = None) -> dict:
    """Sign a certificate as root (with G unlocked) or as this delegating node, store it and check it is valid."""
    now = now or utcnow()
    if group.status != "active":
        raise GroupError(f"the group is {group.status} here; certificates cannot be issued now", "conflict")
    if len(set(scopes)) != len(scopes) or not set(scopes) <= set(SCOPES):
        raise GroupError("unknown scope")
    tr = rebuild(session, group, identity.node_id, now)
    others = {n: c for n, c in tr.members().items() if n != member["node_id"]}
    if len(others) + 1 > group.genesis["body"]["policy"]["max_members"]:
        raise GroupError("the group already has its maximum number of members", "conflict")
    for node, cert in others.items():
        m = cert["body"]["member"]
        if normal_origin(m["url"]) == normal_origin(member["url"]):
            raise GroupError(f"another member ({gc_fp(node)}) already uses that address", "conflict")
        if m["name"].casefold() == member["name"].casefold():
            raise GroupError(f"another member ({gc_fp(node)}) already uses that name", "conflict")
    member = {**member, "url_verified": bool(url_verified)}
    start = now
    if signer_root is not None:
        serial = root_serial(next_root_n(session, group.group_id))
        signer = {"kind": "root", "id": group.group_id, "key_id": group.root_key_id}
        issuer, parent, private = "root", None, signer_root
        end = start + timedelta(days=min(days, DELEGATE_DAYS if delegate else CERT_DAYS))
    else:
        own = tr.in_force(identity.node_id)
        if own is None or own["body"]["delegate"] is None or "group.enrol" not in own["body"]["scopes"]:
            raise GroupError("this node may not enrol members (no group.enrol delegation)", "forbidden")
        serial = f"{identity.node_id}/{next_node_n(session, group.group_id, identity.node_id)}"
        signer = {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}
        issuer, parent, private = identity.node_id, parent_serial or own["body"]["serial"], identity.private
        end = min(start + timedelta(days=days), t(own["body"]["not_after"]))
    body = {"serial": serial, "issuer": issuer, "parent_serial": parent, "member": member, "member_pop": pop,
            "scopes": list(scopes), "delegate": delegate, "readmit": readmit, "issued_at": gc.stamp(start),
            "not_before": gc.stamp(start), "not_after": gc.stamp(end), "enrolment": enrolment}
    obj = gc.make_object("group.cert", group.group_id, f"cert:{serial}", body, signer, private)
    outcome = ingest_cert(session, group, obj, now, tr)
    if outcome != "stored":
        raise GroupError(f"the new certificate was not stored ({outcome})", "conflict")
    verdict = trust(session, group, now, own_node=identity.node_id,
                    extra_rotations=session.info.get("group_rotations")).valid(obj)
    if not verdict.ok:
        raise GroupError(f"the new certificate does not verify ({verdict.reason})", "conflict")
    rebuild(session, group, identity.node_id, now)
    audit(session, actor, "group_cert_issued", serial,
          {"node_id": member["node_id"], "scopes": list(scopes), "delegate": bool(delegate), "issuer": issuer}, cli)
    if member["node_id"] != identity.node_id:
        notify_admins(session, f"group-cert:{serial}", f"Node {gc_fp(member['node_id'])} ({member['name'][:40]}) "
                      "was enrolled in your node group")
    return obj


def gc_fp(node_id: str) -> str:
    return " ".join(node_id[i:i + 4] for i in range(0, len(node_id), 4))


def _crl_stream(session: Session, group: OwnerGroup, issuer: str, root_key: RootKey | None) -> tuple[int, list]:
    """The next crl_seq and the cumulative entries of this issuer (§1.6; a new root continues the numbering)."""
    rows = list(session.scalars(select(GroupCrl).where(GroupCrl.group_id == group.group_id, GroupCrl.issuer == issuer)))
    seq = max((r.crl_seq for r in rows), default=0) + 1
    entries: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.crl_seq):
        for entry in row.body["body"]["entries"]:
            entries.setdefault(entry["serial"], entry)
    return seq, list(entries.values())


def sign_crl(session: Session, group: OwnerGroup, new_entries: list[dict], *, root: Ed25519PrivateKey | None,
             identity: NodeIdentity, now: datetime) -> dict:
    issuer = "root" if root is not None else identity.node_id
    seq, entries = _crl_stream(session, group, issuer, None)
    tr = trust(session, group, now, own_node=identity.node_id)
    known = {e["serial"] for e in entries}
    for entry in new_entries:
        if entry["serial"] not in known:
            entries.append(entry)
            known.add(entry["serial"])
        else:  # an upgrade of the reason (never a downgrade) is a new entry for the same serial
            entries = [entry if e["serial"] == entry["serial"] and REASONS[entry["reason"]] > REASONS[e["reason"]]
                       else e for e in entries]
    issued = gc.stamp(now)
    # Pruning: entries whose certificate expired more than 30 days before this CRL may be left out.
    kept = [e for e in entries if not ((c := tr.certs.get(e["serial"]) or tr.grants.get(e["serial"])) is not None
                                       and t(c["body"]["not_after"]) < now - PRUNE_AFTER)]
    body = {"issuer": issuer, "crl_seq": seq, "issued_at": issued, "entries": kept}
    if root is not None:
        signer = {"kind": "root", "id": group.group_id, "key_id": group.root_key_id}
        private = root
    else:
        signer = {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}
        private = identity.private
    crl = gc.make_object("group.crl", group.group_id, f"crl:{issuer}/{seq}", body, signer, private)
    outcome = ingest_crl(session, group, crl, now, tr)
    if outcome != "stored":
        raise GroupError(f"the revocation list was not accepted ({outcome})", "conflict")
    return crl


def crl_entry(cert: dict, reason: str, now: datetime, suspect_since: datetime | None = None) -> dict:
    member = cert["body"]["member"]
    return {"serial": cert["body"]["serial"], "node_id": member["node_id"], "root_public_key": member["root_public_key"],
            "revoked_at": gc.stamp(now), "reason": reason,
            "suspect_since": gc.stamp(suspect_since) if suspect_since else None}


def revoke_node(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity, node_id: str,
                reason: str, *, root: Ed25519PrivateKey | None, suspect_since: datetime | None = None,
                now: datetime | None = None, actor: str | None = None, cli: bool = False) -> dict:
    """Revoke every certificate of `node_id` this signer may revoke; `removed`/`key_compromise` ban the node."""
    now = now or utcnow()
    if reason not in ("removed", "key_compromise"):
        raise GroupError("revoke a node as 'removed' or 'key_compromise'")
    if node_id == identity.node_id:
        raise GroupError("a node cannot remove itself; use leave", "conflict")
    if reason == "key_compromise" and suspect_since is None:
        raise GroupError("give the time from which the node is suspect (suspect_since)")
    tr = rebuild(session, group, identity.node_id, now)
    certs = [c for c in tr.node_certs(node_id) if c["body"]["serial"] not in tr.revoked
             or REASONS[tr.revoked[c["body"]["serial"]].reason] < REASONS[reason]]
    if root is None:
        certs = [c for c in certs if c["body"]["issuer"] == identity.node_id]
        fresh = freshness(session, group, identity.node_id, None, settings.group_view_max_age_hours, now,
                          action="revoke")
        if not fresh.ok:
            raise GroupError(f"revocations by a delegate need a fresh view of the group: {fresh.reason}", "conflict")
    if not certs:
        raise GroupError("no certificate of that node can be revoked by this signer", "not_found")
    crl = sign_crl(session, group, [crl_entry(c, reason, now, suspect_since) for c in certs], root=root,
                   identity=identity, now=now)
    rebuild(session, group, identity.node_id, now)
    audit(session, actor, "group_member_revoked", node_id,
          {"reason": reason, "serials": [c["body"]["serial"] for c in certs], "crl": crl["id"],
           "suspect_since": gc.stamp(suspect_since) if suspect_since else None}, cli)
    return crl


def issue_grant(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity, *, account_uid: str,
                role: str, nodes: list[str], days: int = GRANT_DAYS, root: Ed25519PrivateKey | None,
                account_label: str = "", note: str = "", authorized_by_grant: str | None = None,
                now: datetime | None = None, actor: str | None = None, cli: bool = False) -> dict:
    """Sign an administrative delegation grant (§6.1) as root (G unlocked) or as this node under its `grant.issue`
    delegation, store it, check it verifies, and audit `group_grant_issued`.

    `authorized_by_grant` names the group-admin grant of the human using this node's issuer power; the new grant
    then lives no longer, and reaches no higher, than that grant, and falls with it."""
    now = now or utcnow()
    if group.status != "active":
        raise GroupError(f"the group is {group.status} here; grants cannot be issued now", "conflict")
    if role not in GRANT_ALL_ROLES:
        raise GroupError("unknown role")
    if not isinstance(account_uid, str) or not ACCOUNT_UID.match(account_uid):
        raise GroupError("the account is not a group account id (a ULID)")
    nodes = list(dict.fromkeys(nodes))
    if not nodes or (nodes != ["*"] and any(not NODE_ID.match(n) for n in nodes)):
        raise GroupError("name the member nodes the grant covers, or * for every member")
    policy = group.genesis["body"]["policy"]
    if type(days) is not int or not 1 <= days <= policy["grant_max_days"]:
        raise GroupError(f"a grant lasts 1 to {policy['grant_max_days']} days")
    tr = rebuild(session, group, identity.node_id, now)
    unknown = [n for n in nodes if n != "*" and n not in tr.members()]
    if unknown:
        raise GroupError(f"not a current member: {', '.join(gc_fp(n) for n in unknown)}", "not_found")
    start, end = now, now + timedelta(days=days)
    if root is not None:
        if authorized_by_grant is not None:
            raise GroupError("a root grant rests on the root alone")
        serial = root_serial(next_root_n(session, group.group_id))
        signer = {"kind": "root", "id": group.group_id, "key_id": group.root_key_id}
        issuer, parent, private = "root", None, root
    else:
        if role == "group_admin" or nodes == ["*"]:
            raise GroupError("only the group root may grant group_admin or cover every node (*)", "forbidden")
        own = tr.may_grant_now(identity.node_id)
        if own is None:
            raise GroupError("this node may not grant (no grant.issue delegation)", "forbidden")
        delegate = own["body"]["delegate"]
        if role not in delegate["may_grant_roles"]:
            raise GroupError(f"this node may grant only: {', '.join(delegate['may_grant_roles']) or 'nothing'}",
                             "forbidden")
        outside = [n for n in nodes if n not in delegate["may_grant_nodes"]]
        if outside:
            raise GroupError(f"this node may not grant for {', '.join(gc_fp(n) for n in outside)}", "forbidden")
        fresh = freshness(session, group, identity.node_id, None, settings.group_view_max_age_hours, now,
                          action="issue")
        if not fresh.ok:
            raise GroupError(f"a delegated grant needs a fresh view of the group: {fresh.reason}", "conflict")
        n = next_node_n(session, group.group_id, identity.node_id)
        if n > delegate["max_serials"]:
            raise GroupError("this node has used every serial its delegation allows; ask the root to renew it",
                             "conflict")
        serial = f"{identity.node_id}/{n}"
        signer = {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}
        issuer, parent, private = identity.node_id, own["body"]["serial"], identity.private
        end = min(end, t(own["body"]["not_after"]))
        if authorized_by_grant is not None:
            a = tr.grants.get(authorized_by_grant)
            if a is None or not tr.grant_valid(a).ok or a["body"]["role"] != "group_admin":
                raise GroupError("the authorizing group-admin grant is not valid", "forbidden")
            end = min(end, t(a["body"]["not_after"]))
    body = {"serial": serial, "issuer": issuer, "parent_serial": parent, "authorized_by_grant": authorized_by_grant,
            "account_uid": account_uid, "account_label": gc.nfc(account_label.strip())[:80], "role": role,
            "nodes": nodes,
            "issued_at": gc.stamp(start), "not_before": gc.stamp(start), "not_after": gc.stamp(end),
            "note": gc.nfc(note.strip())[:200]}
    obj = gc.make_object("group.grant", group.group_id, f"grant:{serial}", body, signer, private)
    outcome = ingest_grant(session, group, obj, now, tr)
    if outcome != "stored":
        raise GroupError(f"the new grant was not stored ({outcome})", "conflict")
    verdict = trust(session, group, now, own_node=identity.node_id).grant_valid(obj)
    if not verdict.ok:
        raise GroupError(f"the new grant does not verify ({verdict.reason})", "conflict")
    rebuild(session, group, identity.node_id, now)
    audit(session, actor, "group_grant_issued", serial,
          {"account_uid": account_uid, "role": role, "nodes": nodes, "issuer": issuer,
           "authorized_by_grant": authorized_by_grant, "not_after": body["not_after"]}, cli)
    return obj


def revoke_grant(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity, serial: str, *,
                 root: Ed25519PrivateKey | None, now: datetime | None = None, actor: str | None = None,
                 cli: bool = False) -> dict:
    """Revoke one grant (§1.6: root any, a delegate only what it issued). Grants it authorized fall with it."""
    now = now or utcnow()
    tr = rebuild(session, group, identity.node_id, now)
    if serial not in tr.grants:
        raise GroupError("no such grant", "not_found")
    if serial in tr.revoked:
        raise GroupError("that grant is already revoked", "conflict")
    if root is None and (parse_serial(serial) or Serial("", 0, None)).issuer != identity.node_id:
        raise GroupError("this node may revoke only the grants it issued; the root revokes any", "forbidden")
    entry = {"serial": serial, "node_id": None, "root_public_key": None, "revoked_at": gc.stamp(now),
             "reason": "removed", "suspect_since": None}
    crl = sign_crl(session, group, [entry], root=root, identity=identity, now=now)
    tr = rebuild(session, group, identity.node_id, now)
    fallen = sorted(s for s, g in tr.grants.items() if g["body"]["authorized_by_grant"] == serial)
    audit(session, actor, "group_grant_revoked", serial,
          {"crl": crl["id"], "account_uid": tr.grants[serial]["body"]["account_uid"], "cascaded": fallen[:50]}, cli)
    return crl


def grant_rows(session: Session, group: OwnerGroup, own_node: str, now: datetime | None = None) -> list[dict]:
    """Every stored grant with its status here, for the admin page and `atlas group grants`."""
    now = now or utcnow()
    tr = trust(session, group, now, own_node=own_node)
    out = []
    for row in session.scalars(select(GroupGrant).where(GroupGrant.group_id == group.group_id)
                               .order_by(GroupGrant.received_at)):
        b = row.body["body"]
        verdict = tr.grant_valid(row.body) if row.held_reason is None else Verdict(False, row.held_reason)
        out.append({"serial": row.serial, "account_uid": b["account_uid"], "account_label": b["account_label"],
                    "role": b["role"], "nodes": list(b["nodes"]), "issuer": b["issuer"],
                    "authorized_by_grant": b["authorized_by_grant"], "not_after": b["not_after"],
                    "status": "valid" if verdict.ok else verdict.reason,
                    "here": verdict.ok and (own_node in b["nodes"] or b["nodes"] == ["*"]), "note": b["note"]})
    return out


def renew(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity, nodes: list[str] | None,
          *, root: Ed25519PrivateKey, now: datetime | None = None, actor: str | None = None, cli: bool = False,
          member_update: dict | None = None) -> list[dict]:
    """Root renewal: a new serial with the same member object (or a member-signed update, §1.7) and, in the same
    operation, the old serial revoked as `superseded` (never cascades). Banned nodes and nodes whose certificate
    is not root-issued are skipped (§1.4)."""
    now = now or utcnow()
    tr = rebuild(session, group, identity.node_id, now)
    targets = nodes if nodes is not None else sorted(tr.members())
    issued, superseded = [], []
    for node in targets:
        cert = tr.in_force(node)
        if node in tr.banned or cert is None or cert["body"]["issuer"] != "root":
            continue
        b = cert["body"]
        member = {k: v for k, v in b["member"].items() if k != "url_verified"}
        pop = b["member_pop"]
        if member_update is not None and member_update["member"]["node_id"] == node:
            new = member_update["member"]
            if new["root_public_key"] != member["root_public_key"] or new["rev"] <= member["rev"]:
                raise GroupError("a member update keeps the root key and raises member.rev", "conflict")
            if not tr.member_pop_ok({"member": new, "member_pop": member_update["member_pop"]}, strict=True):
                raise GroupError("the member update is not signed by the member (member_pop)", "conflict")
            member, pop = new, member_update["member_pop"]
        else:
            current = tr.current_key(node)
            if current is not None and pop.get("key_id") != current[0]:
                # R1-9: after a rotation the renewal re-signs the PoP with the current key, at the same rev
                if node == identity.node_id:
                    pop = gc.member_pop(identity.private, group.group_id, member, identity.key_id)
                else:
                    repop = _held_repop(session, tr, node, member)
                    if repop is not None:
                        pop = repop
                    elif _audit_once(session, "group_renew_stale_pop", f"{node}:{member['rev']}",
                                     {"node": node, "pop_key_id": pop.get("key_id"), "current": current[0]}):
                        notify_admins(session, f"group-renew-pop:{node}",
                                      f"Node {gc_fp(node)} rotated its key; ask it to send a member update so its "
                                      "certificate carries a proof by its current key")
        days = DELEGATE_DAYS if b["delegate"] else CERT_DAYS
        obj = issue_cert(session, group, identity, member, pop, b["scopes"], b["delegate"], signer_root=root,
                         days=days, enrolment=b.get("enrolment"), url_verified=b["member"].get("url_verified", False)
                         and member["url"] == b["member"]["url"], now=now, actor=actor, cli=cli)
        issued.append(obj)
        superseded.append(crl_entry(cert, "superseded", now))
    if superseded:
        sign_crl(session, group, superseded, root=root, identity=identity, now=now)
        rebuild(session, group, identity.node_id, now)
        audit(session, actor, "group_cert_renewed", group.group_id,
              {"serials": [o["body"]["serial"] for o in issued]}, cli)
    return issued


def _held_repop(session: Session, tr: Trust, node: str, member: dict) -> dict | None:
    """A member-signed re-PoP (same member object, current key) the member sent with `member-update`."""
    row = session.get(NodeState, MEMBER_UPDATE_PREFIX + node)
    if row is None:
        return None
    held = json.loads(row.value)
    if {k: v for k, v in held["member"].items() if k != "url_verified"} != member:
        return None
    if not tr.member_pop_ok({"member": member, "member_pop": held["member_pop"]}, strict=True):
        return None
    return held["member_pop"]


def leave_allowed(session: Session, group: OwnerGroup) -> str | None:
    """None when leaving is allowed; otherwise why not (§1.9: never destroy G silently)."""
    if group.root_sealed and group.root_state == "holder":
        if session.get(NodeState, RECOVERY_VERIFIED) is None:
            return ("this node holds the group root: move the root to another member, or export a recovery file "
                    "holding the root key and verify it (`atlas group root verify-recovery`) first")
    return None


def leave(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity, *,
          now: datetime | None = None, actor: str | None = None, cli: bool = False) -> dict | None:
    """Revoke this node's own certificates with reason `left` (a self CRL that other members pull)."""
    now = now or utcnow()
    why = leave_allowed(session, group)
    if why:
        raise GroupError(why, "conflict")
    tr = rebuild(session, group, identity.node_id, now)
    own = [c for c in tr.node_certs(identity.node_id) if c["body"]["serial"] not in tr.revoked]
    crl = None
    if own:
        crl = sign_crl(session, group, [crl_entry(c, "left", now) for c in own], root=None, identity=identity, now=now)
    if group.root_sealed:
        group.root_sealed, group.root_state = None, "none"
        audit(session, actor, "group_root_destroyed_local", group.group_id, {}, cli)
    rebuild(session, group, identity.node_id, now)
    group.status = "left"
    for row in session.scalars(select(GroupEnrolment).where(GroupEnrolment.state.in_(("open", "hello", "revealed",
                                                                                         "confirmed")))):
        row.state = "aborted"
    audit(session, actor, "group_left", group.group_id, {"crl": crl["id"] if crl else None}, cli)
    return crl


# ---- member updates: a new address or name (§1.3 `member_pop` with a higher `rev`; #122) ------------------------

MEMBER_UPDATE_PREFIX = "group_member_update:"  # NodeState key on a root holder: a member-signed update to apply


def root_holder_hint(session: Session, group: OwnerGroup) -> str | None:
    """Which member most likely holds the group root: the target of the newest `root_moved`, otherwise the node of
    the first root-issued certificate (the node that created the group). A hint for the admin, never a check."""
    if group.root_moved:
        return group.root_moved["body"].get("to_node")
    for row in session.scalars(select(GroupCert).where(GroupCert.group_id == group.group_id)):
        serial = parse_serial(row.serial)
        if serial is not None and serial.issuer == "root" and serial.n == 1:
            return row.node_id
    return None


def own_address(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity) -> dict | None:
    """This node's address and name as the group knows them (its in-force certificate) against its configuration:
    `{url, name, configured_url, changed}`. None when this node has no certificate in force."""
    tr = trust(session, group, own_node=identity.node_id)
    cert = tr.in_force(identity.node_id)
    if cert is None:
        return None
    member = cert["body"]["member"]
    configured = (settings.public_url or "").strip().rstrip("/")
    return {"url": member["url"], "name": member["name"], "configured_url": configured,
            "changed": bool(configured) and configured != member["url"].rstrip("/"),
            "issuer": cert["body"]["issuer"]}


def member_update(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity, *,
                  url: str | None = None, name: str | None = None, now: datetime | None = None) -> dict:
    """`atlas group member-update` (§1.3): this node's member object with a new url and/or name and `rev + 1`,
    signed by this node (`member_pop`, current key). The box key and the root key never change here."""
    now = now or utcnow()
    tr = rebuild(session, group, identity.node_id, now)
    cert = tr.in_force(identity.node_id)
    if cert is None:
        raise GroupError("this node has no certificate in force in the group", "conflict")
    old = {k: v for k, v in cert["body"]["member"].items() if k != "url_verified"}
    new_url = clean_member_url(url, settings.environment == "production") if url else old["url"]
    new_name = gc.nfc((name or "").strip())[:80] or old["name"]
    if new_url == old["url"] and new_name == old["name"]:
        raise GroupError("the group already has this address and name for this node", "conflict")
    member = {**old, "url": new_url, "name": new_name, "rev": old["rev"] + 1}
    if not _member_ok(member):
        raise GroupError("the new member object is malformed")
    _member_unique(tr, member)
    return {"member": member, "member_pop": gc.member_pop(identity.private, group.group_id, member, identity.key_id)}


def _member_unique(tr: Trust, member: dict) -> None:
    for node, cert in tr.members().items():
        if node == member["node_id"]:
            continue
        other = cert["body"]["member"]
        if normal_origin(other["url"]) == normal_origin(member["url"]):
            raise GroupError(f"another member ({gc_fp(node)}) already uses that address", "conflict")
        if other["name"].casefold() == member["name"].casefold():
            raise GroupError(f"another member ({gc_fp(node)}) already uses that name", "conflict")


def check_member_update(session: Session, settings: Settings, group: OwnerGroup, own_node: str, update: Any,
                        sender: str | None = None, now: datetime | None = None) -> dict:
    """Everything a root holder checks before it keeps or applies an update: the shape, that the sender (when it
    came over the channel) is the member itself, the same root key, a higher `rev`, the member's own PoP under
    its current key, a clean url and uniqueness. Returns the in-force certificate being replaced."""
    now = now or utcnow()
    try:
        member, pop = update["member"], update["member_pop"]
        if set(update) != {"member", "member_pop"} or not _member_ok(member) or "url_verified" in member:
            raise GroupError("the member update is malformed")
        node = member["node_id"]
    except (KeyError, TypeError, AttributeError):
        raise GroupError("the member update is malformed") from None
    if sender is not None and sender != node:
        raise GroupError("a member may only update its own address and name", "forbidden")
    tr = trust(session, group, now, own_node=own_node)
    cert = tr.in_force(node)
    if cert is None or node in tr.banned:
        raise GroupError("that node has no certificate in force", "conflict")
    if cert["body"]["issuer"] != "root":
        raise GroupError("that node was enrolled by a delegate: the delegate that enrolled it re-issues it", "conflict")
    old = cert["body"]["member"]
    if member["root_public_key"] != old["root_public_key"] or member["box_public_key"] != old["box_public_key"]:
        raise GroupError("a member update keeps the root key and the box key", "conflict")
    if member["rev"] <= old["rev"]:
        raise GroupError("a member update raises member.rev", "conflict")
    if not tr.member_pop_ok({"member": member, "member_pop": pop}, strict=True):
        raise GroupError("the member update is not signed by the member (member_pop)", "conflict")
    if clean_member_url(member["url"], settings.environment == "production") != member["url"]:
        raise GroupError("the new address is not a plain http(s) address")
    _member_unique(tr, member)
    return cert


def hold_member_update(session: Session, settings: Settings, group: OwnerGroup, own_node: str, update: Any,
                       sender: str, now: datetime | None = None) -> str:
    """A member sent its own signed update over the channel. Only a node holding the root keeps it, for its
    administrator to apply with the root passphrase; nothing is signed here. Returns 'held' or 'known'."""
    if not (group.root_state == "holder" and group.root_sealed):
        raise GroupError("not_root", "conflict")
    check_member_update(session, settings, group, own_node, update, sender=sender, now=now)
    key = MEMBER_UPDATE_PREFIX + sender
    row = session.get(NodeState, key)
    held = json.loads(row.value) if row is not None else None
    if held is not None and held["member"]["rev"] >= update["member"]["rev"]:
        return "known"
    session.merge(NodeState(key=key, value=json.dumps({"member": update["member"],
                                                       "member_pop": update["member_pop"]})))
    audit(session, None, "group_member_update_held", sender,
          {"url": update["member"]["url"], "name": update["member"]["name"], "rev": update["member"]["rev"]})
    notify_admins(session, f"group-member-update:{sender}:{update['member']['rev']}",
                  f"Node {gc_fp(sender)} asks to change its group address to {update['member']['url'][:80]}")
    session.flush()
    return "held"


def held_member_updates(session: Session, group: OwnerGroup, own_node: str) -> list[dict]:
    """The updates waiting on this root holder, each with the address and name they replace."""
    tr = trust(session, group, own_node=own_node)
    out = []
    for row in session.scalars(select(NodeState).where(NodeState.key.like(MEMBER_UPDATE_PREFIX + "%"))
                               .order_by(NodeState.key)):
        update = json.loads(row.value)
        node = update["member"]["node_id"]
        cert = tr.in_force(node)
        out.append({"node_id": node, "url": update["member"]["url"], "name": update["member"]["name"],
                    "rev": update["member"]["rev"], "old_url": cert["body"]["member"]["url"] if cert else None,
                    "old_name": cert["body"]["member"]["name"] if cert else None})
    return out


def drop_member_update(session: Session, node_id: str, actor: str | None = None) -> None:
    row = session.get(NodeState, MEMBER_UPDATE_PREFIX + node_id)
    if row is not None:
        session.delete(row)
        audit(session, actor, "group_member_update_dropped", node_id)
        session.flush()


def apply_member_update(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity,
                        update: dict, *, root: Ed25519PrivateKey, now: datetime | None = None,
                        actor: str | None = None, cli: bool = False) -> dict:
    """Root: re-issue the member's certificate with its signed new member object and supersede the old serial
    (`renew` with `member_update`). The scopes, delegate and lifetime rules are those of a renewal."""
    now = now or utcnow()
    check_member_update(session, settings, group, identity.node_id, update, now=now)
    node = update["member"]["node_id"]
    issued = renew(session, settings, group, identity, [node], root=root, now=now, actor=actor, cli=cli,
                   member_update=update)
    if not issued:
        raise GroupError("the certificate could not be re-issued", "conflict")
    row = session.get(NodeState, MEMBER_UPDATE_PREFIX + node)
    if row is not None:
        session.delete(row)
    audit(session, actor, "group_member_updated", node,
          {"url": update["member"]["url"], "name": update["member"]["name"], "rev": update["member"]["rev"],
           "serial": issued[0]["body"]["serial"]}, cli)
    session.flush()
    return issued[0]


# ---- recovery: moving the root, root succession and its cancellation (§1.8, §1.9) ------------------------------

@dataclass
class Imported:
    moved: dict
    reexport: dict | None  # a v2 recovery file to write before the import is committed (R1-5)


def import_root(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity,
                keys: gc.RecoveryKeys, new_passphrase: str | None, old_root: str, *, root_passphrase: str | None = None,
                reexport_passphrase: str | None = None, now: datetime | None = None, actor: str | None = None,
                cli: bool = False) -> Imported:
    """`atlas group root import`: put G from a recovery file on this member and sign `root_moved` (§1.9).

    The owner says whether the old root node is decommissioned or still running; either way the new holder
    signs `group.root_moved` at once, and a running old holder disables its root when it sees it. The new
    holder is `resyncing` until it has pulled every member, so it starts from the full CRL and serial state.

    A v2 file (R1-5) holds G wrapped under the root passphrase: `root_passphrase` unwraps it, and it stays the
    node's root passphrase unless `new_passphrase` differs. Then a re-export (v2, under the new root passphrase and
    `reexport_passphrase`) is required and returned, so the file in the drawer never needs a passphrase the owner
    no longer uses. A v1 file (raw G) needs a new root passphrase; `reexport_passphrase` returns a v2 file."""
    now = now or utcnow()
    if old_root not in ("decommissioned", "running"):
        raise GroupError("say whether the old root node is 'decommissioned' or still 'running'")
    if keys.group_id != group.group_id:
        raise GroupError("that recovery file belongs to another group")
    if not keys.holds_root:
        raise GroupError("this recovery file holds no root key (only a 1-of-1 file or an export-recovery file "
                         "does); recover with a root succession instead", "conflict")
    try:
        root_key = gc.recovery_root(keys, root_passphrase)
    except gc.WrongPassphrase:
        raise GroupError("wrong root passphrase for the root key in this recovery file; if it is forgotten, "
                         "recover with a root succession instead", "forbidden") from None
    if keys.root_wrapped is not None and not new_passphrase:
        new_passphrase = root_passphrase  # keep the file's root passphrase (the default)
    if not new_passphrase:
        raise GroupError("choose a root passphrase for this node")
    reexport_needed = keys.root_wrapped is not None and new_passphrase != root_passphrase
    if reexport_needed and not reexport_passphrase:
        raise GroupError("a new root passphrase needs a re-exported recovery file: give its passphrase", "conflict")
    if gc.key_id_of(gc.ed_public_b64(root_key)) != group.root_key_id:
        raise GroupError("this file holds an older root key: a root succession replaced it", "conflict")
    if group.status != "active":
        raise GroupError(f"the group is {group.status} here; the root cannot be imported now", "conflict")
    if pending_successions(group):
        raise GroupError("a root succession is pending; the root cannot be imported until it is decided", "conflict")
    if group.root_state == "holder" and group.root_sealed:
        raise GroupError("this node already holds the group root", "conflict")
    tr = rebuild(session, group, identity.node_id, now, settings)
    if tr.in_force(identity.node_id) is None:
        raise GroupError("only an active member can take the group root", "forbidden")
    try:
        gc.check_passphrase(new_passphrase, _names(settings, None, group.name))
    except gc.WeakPassphrase as exc:
        raise GroupError(f"passphrase refused: {exc}") from None
    previous = group.root_moved["body"]["to_node"] if group.root_moved else None
    body = {"from_node": previous, "to_node": identity.node_id, "old_root": old_root, "issued_at": gc.stamp(now)}
    moved = gc.make_object("group.root_moved", group.group_id, f"root_moved:{identity.node_id}", body,
                           {"kind": "root", "id": group.group_id, "key_id": group.root_key_id}, root_key)
    outcome = ingest_root_moved(session, group, moved, identity.node_id, now)
    if outcome != "stored":
        raise GroupError(f"the root_moved notice was not accepted ({outcome})", "conflict")
    wrapped = gc.wrap_root(root_key, new_passphrase, group.group_id)
    reexport = None
    if reexport_passphrase:
        if reexport_passphrase == new_passphrase:
            raise GroupError("use a recovery-file passphrase that differs from the root passphrase")
        try:
            gc.check_passphrase(reexport_passphrase, _names(settings, None, group.name))
        except gc.WeakPassphrase as exc:
            raise GroupError(f"passphrase refused: {exc}") from None
        reexport = gc.make_recovery_file(group.genesis, list(group.succession or []), keys.recovery, None,
                                         reexport_passphrase, gc.stamp(now), root_wrapped=wrapped)
    group.root_sealed = seal_secret(settings, canonical_json(wrapped))
    group.root_state = "holder"
    group.status = "resyncing"  # root operations wait until every member's state is pulled (§1.9)
    session.merge(NodeState(key=RECOVERY_VERIFIED, value=gc.stamp(now)))  # the file holding G was just opened
    audit(session, actor, "group_root_imported", group.group_id,
          {"old_root": old_root, "from_node": previous, "file_version": keys.version, "reexported": bool(reexport)},
          cli)
    notify_admins(session, "group-root-imported", "The group root was imported on this node from a recovery file")
    session.flush()
    return Imported(moved, reexport)


def _quorum_keys(group: OwnerGroup, files: list[gc.RecoveryKeys]) -> list[Ed25519PrivateKey]:
    """Distinct recovery keys of the currently pinned set, at least `threshold` of them (§1.8)."""
    pinned = {k["key_id"] for k in (group.recovery or {}).get("keys", [])}
    keys: dict[str, Ed25519PrivateKey] = {}
    for f in files:
        if f.group_id != group.group_id:
            raise GroupError("a recovery file belongs to another group")
        if f.recovery is None:
            raise GroupError("a file holds no recovery key (an export-recovery file holds the root key only)")
        kid = gc.key_id_of(gc.ed_public_b64(f.recovery))
        if kid not in pinned:
            raise GroupError("a recovery file is not in the group's current recovery set (an older file?)", "conflict")
        keys.setdefault(kid, f.recovery)
    if len(keys) < group.recovery["threshold"]:
        raise GroupError(f"this needs {group.recovery['threshold']} different recovery files", "conflict")
    return list(keys.values())


def _signed_by_quorum(group: OwnerGroup, t_: str, oid: str, body: dict, keys: list[Ed25519PrivateKey]) -> dict:
    first = keys[0]
    obj = gc.make_object(t_, group.group_id, oid, body,
                         {"kind": "recovery", "id": group.group_id, "key_id": gc.key_id_of(gc.ed_public_b64(first))},
                         first)
    obj["cosigs"] = [{"key_id": gc.key_id_of(gc.ed_public_b64(k)), "sig": gc.co_sign(obj, k)} for k in keys[1:]]
    return obj


def succession_crl_choice(session: Session, group: OwnerGroup, suspect_since: datetime | None = None,
                          chosen: dict | None = None) -> tuple[GroupCrl | None, list[dict]]:
    """The old-root CRL a succession names as `old_root_last_crl`, and its entries (R1-1).

    Default: the highest-seq CRL of the current root key that **this node received** (`received_at`, never the
    signer-chosen `issued_at`, which a thief of G can backdate) before `suspect_since`, with no fork at its seq;
    without `suspect_since`, the newest unforked one. `chosen` ({crl_seq, body_hash}) overrides the default."""
    rows = [r for r in session.scalars(select(GroupCrl).where(GroupCrl.group_id == group.group_id,
                                                              GroupCrl.issuer == "root"))
            if r.body["signer"].get("key_id") == group.root_key_id]
    if chosen is not None:
        hit = [r for r in rows if r.crl_seq == chosen.get("crl_seq") and r.body_hash == chosen.get("body_hash")]
        if not hit:
            raise GroupError("this node does not hold that revocation list of the current root key", "not_found")
        return hit[0], list(hit[0].body["body"]["entries"])
    seqs: dict[int, int] = {}
    for r in rows:
        seqs[r.crl_seq] = seqs.get(r.crl_seq, 0) + 1
    pool = [r for r in rows if seqs[r.crl_seq] == 1
            and (suspect_since is None or aware(r.received_at) < suspect_since)]  # type: ignore[operator]
    best = max(pool, key=lambda r: r.crl_seq, default=None)
    return best, list(best.body["body"]["entries"]) if best is not None else []


def issue_succession(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity,
                     files: list[gc.RecoveryKeys], new_root_passphrase: str, *, exclude_nodes: tuple[str, ...] = (),
                     reason: str = "compromised", delay_hours: int | None = None,
                     new_recovery_passphrase: str | None = None, now: datetime | None = None,
                     actor: str | None = None, cli: bool = False, suspect_since: datetime | None = None,
                     last_crl: dict | None = None, confirm_compromise: set[str] | frozenset[str] = frozenset(),
                     ) -> tuple[dict, list[dict]]:
    """`atlas group root succession`: a recovery quorum replaces G with a new root key G' held on this node.

    Carries every current member's root-issued certificate by content except `exclude_nodes` (§1.8), bounds the
    old root by its last serial and last CRL, and is pending for at least the genesis delay. G' is stored here
    sealed with a new root passphrase and becomes this node's root when the succession activates. With a 1-of-1
    recovery set a new recovery file (R and G') is returned, because the old file holds the old G."""
    now = now or utcnow()
    if group.status != "active":
        raise GroupError(f"the group is {group.status} here; a succession cannot be issued now", "conflict")
    if pending_successions(group):
        raise GroupError("a root succession is already pending", "conflict")
    if reason not in ("compromised", "lost"):
        raise GroupError("the reason is 'compromised' or 'lost'")
    keys = _quorum_keys(group, files)
    policy = group.genesis["body"]["policy"]
    delay = policy["succession_delay_hours"] if delay_hours is None else delay_hours
    if delay < policy["succession_delay_hours"]:
        raise GroupError(f"the activation delay is at least {policy['succession_delay_hours']} hours (genesis policy)")
    names = _names(settings, None, group.name)
    one_of_one = group.recovery["threshold"] == 1 and len(group.recovery["keys"]) == 1
    try:
        gc.check_passphrase(new_root_passphrase, names)
        if one_of_one:
            if not new_recovery_passphrase:
                raise GroupError("a 1-of-1 recovery file holds the root key: give a passphrase for the new file")
            gc.check_passphrase(new_recovery_passphrase, names)
    except gc.WeakPassphrase as exc:
        raise GroupError(f"passphrase refused: {exc}") from None
    if new_recovery_passphrase and new_recovery_passphrase == new_root_passphrase:
        raise GroupError("use a recovery-file passphrase that differs from the root passphrase")
    if identity.node_id in exclude_nodes:
        raise GroupError("this node signs the succession and is always carried", "conflict")
    tr = rebuild(session, group, identity.node_id, now, settings)
    if tr.in_force(identity.node_id) is None:
        raise GroupError("only an active member can hold the new root", "forbidden")
    carry = [{"serial": c["body"]["serial"], "body_hash": c["body_hash"]}
             for node, c in sorted(tr.members().items()) if node not in exclude_nodes and c["signer"]["kind"] == "root"]
    last, last_entries = succession_crl_choice(session, group, suspect_since, last_crl)
    unconfirmed = sorted(e["serial"] for e in last_entries
                         if e.get("reason") == "key_compromise" and e["serial"] not in confirm_compromise)
    if unconfirmed:  # the §1.2 confused-deputy guard: a key_compromise entry bans a node identity for ever
        raise GroupError("the chosen revocation list bans these serials as key_compromise; confirm each one: "
                         + ", ".join(unconfirmed), "conflict")
    new_root = Ed25519PrivateKey.generate()
    new_public = gc.ed_public_b64(new_root)
    index = len(group.succession or []) + 1
    body = {"index": index, "previous_root_key_id": group.root_key_id, "new_root_public_key": new_public,
            "new_root_key_id": gc.key_id_of(new_public), "reason": reason, "issued_at": gc.stamp(now),
            "carry": carry, "old_root_last_serial_n": next_root_n(session, group.group_id) - 1,
            "old_root_last_crl": {"crl_seq": last.crl_seq, "body_hash": last.body_hash} if last
            else {"crl_seq": 0, "body_hash": "none"},
            "activation_delay_hours": delay, "recovery": group.recovery}
    obj = _signed_by_quorum(group, "group.succession", f"succession:{index}", body, keys)
    obj["new_root_pop"] = gc.co_sign(obj, new_root)
    outcome = ingest_succession(session, group, obj, now, delivered_by=identity.node_id)
    if outcome != "pending":
        raise GroupError(f"the succession was not accepted ({outcome})", "conflict")
    wrapped_new = gc.wrap_root(new_root, new_root_passphrase, group.group_id)
    sealed = seal_secret(settings, canonical_json(wrapped_new))
    session.merge(NodeState(key=SUCCESSION_ROOT, value=json.dumps(
        {"index": index, "key_id": body["new_root_key_id"], "sealed": sealed})))
    files_out = []
    if one_of_one:  # v2: G' is wrapped under the new root passphrase inside the new file (R1-5)
        files_out = [gc.make_recovery_file(group.genesis, [*(group.succession or []), obj], keys[0], None,
                                           new_recovery_passphrase or "", gc.stamp(now), root_wrapped=wrapped_new)]
    del new_root
    audit(session, actor, "group_succession_issued", group.group_id,
          {"index": index, "new_root_key_id": body["new_root_key_id"], "carry": len(carry),
           "excluded": sorted(exclude_nodes), "activation_delay_hours": delay, "reason": reason}, cli)
    activate_successions(session, group, now, identity.node_id)  # zero delay: at once once confirmed (H26, R1-5)
    session.flush()
    return obj, files_out


def cancel_succession(session: Session, settings: Settings, group: OwnerGroup, files: list[gc.RecoveryKeys], *,
                      index: int | None = None, now: datetime | None = None, actor: str | None = None,
                      cli: bool = False) -> dict:
    """`atlas group root succession-cancel`: a recovery quorum of the pinned set cancels a pending succession
    permanently on every member that receives it (§1.8). A G-signed cancel is never honoured."""
    now = now or utcnow()
    pending = [p for p in pending_successions(group) if index is None or p["body"]["body"]["index"] == index]
    if not pending:
        raise GroupError("no matching root succession is pending", "not_found")
    target = pending[0]["body"]
    keys = _quorum_keys(group, files)
    body = {"index": target["body"]["index"], "succession_body_hash": target["body_hash"], "issued_at": gc.stamp(now)}
    obj = _signed_by_quorum(group, "group.succession_cancel", f"succession_cancel:{body['index']}", body, keys)
    if ingest_cancel(session, group, obj, now) != "cancelled":
        raise GroupError("the cancellation was not accepted", "conflict")
    mine = session.get(NodeState, SUCCESSION_ROOT)
    if mine is not None and json.loads(mine.value).get("index") == body["index"]:
        session.delete(mine)  # the G' this node created for the cancelled succession is destroyed
    audit(session, actor, "group_succession_cancel_issued", group.group_id, {"index": body["index"]}, cli)
    session.flush()
    return obj


def purge_nonces(session: Session, now: datetime | None = None) -> None:
    session.execute(delete(GroupNonce).where(GroupNonce.seen_at < (now or utcnow()) - timedelta(minutes=15)))


def expire_enrolments(session: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    count = 0
    for row in session.scalars(select(GroupEnrolment).where(GroupEnrolment.state.in_(
            ("open", "hello", "revealed", "confirmed")), GroupEnrolment.expires_at < now)):
        row.state = "expired"
        count += 1
    return count
