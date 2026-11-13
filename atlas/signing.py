"""Node identity and Ed25519 signing for the change feed."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import secrets_box
from .models import AuditEntry, NodeState
from .settings import Settings

DOMAIN = b"atlas-feed-v2\n"
#: The price channel's domain (CONTRACTS-v1 C8.3, protocol 1.5): the feed's message layout under a different
#: prefix, so a price signature never verifies as a feed entry or the other way round. The channel is designed
#: but disabled in this release (`feed_schema.CHANNEL_ENABLED`); nothing is signed with it outside tests.
PRICE_DOMAIN = b"atlas-price-v1\n"
#: The `kind` part of a price channel message.
PRICE_KIND = "price"
SIGNING_KEY = "signing_key"
# Stored form of an encrypted private key (CONTRACTS-v1 C9.1); a value without it is legacy plaintext base64.
ENC_PREFIX = "enc:v1:"
UNREADABLE = "signing key is encrypted; ATLAS_SECRET_KEY is missing or wrong"

log = logging.getLogger("atlas.signing")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def body_hash(body: Any) -> str:
    return hashlib.sha256(canonical_json(body)).hexdigest()


#: D5 (CONTRACT-C16 owner decision, design §7.1): identifiers are the full SHA-256 of a public key, 64 hex digits,
#: from wire 1 on. Identities minted before wire 1 keep their 16-digit (64-bit) form: their signed history, every
#: follower pin and every group certificate name it, and it cannot be re-signed. The two forms are told apart by
#: their length, never by guessing, and the legacy one is the first 16 digits of the wide one (`id_matches`).
WIDE_ID_LEN, LEGACY_ID_LEN = 64, 16
ID_RE = re.compile(r"(?:[0-9a-f]{16}|[0-9a-f]{64})\Z")
#: A regular-expression fragment for either id form (for the patterns that embed an id: serials, HLCs).
ID_PATTERN = r"(?:[0-9a-f]{64}|[0-9a-f]{16})"


def key_id_for(public_raw: bytes, *, legacy: bool = False) -> str:
    """The id of a public key: 64 hex digits (wire 1), or the 16-digit v0 form with `legacy=True`."""
    digest = hashlib.sha256(public_raw).hexdigest()
    return digest[:LEGACY_ID_LEN] if legacy else digest


def id_form(value: Any) -> str | None:
    """"w1" (64 hex digits), "v0" (16) or None for anything else."""
    if not isinstance(value, str) or not ID_RE.match(value):
        return None
    return "w1" if len(value) == WIDE_ID_LEN else "v0"


def id_matches(value: Any, public_raw: bytes) -> bool:
    """Whether `value` is the id of `public_raw`, in either form (the explicit D5 legacy mapping)."""
    form = id_form(value)
    if form is None:
        return False
    return value == key_id_for(public_raw, legacy=form == "v0")


def key_id_like(public_raw: bytes, like: str | None) -> str:
    """The id of a key in the same form as `like` (an identity's node id): a v0 identity keeps naming its keys in
    the v0 form, so its followers' rotation histories and its feed entries agree; anything else is wide."""
    return key_id_for(public_raw, legacy=id_form(like) == "v0")


def short_id(value: str) -> str:
    """The display form shown to people (fingerprints compare on the full value)."""
    return value[:LEGACY_ID_LEN]


def _message(node_id: str, seq: int, kind: str, digest: str, domain: bytes = DOMAIN) -> bytes:
    return domain + f"{node_id}\n{seq}\n{kind}\n{digest}".encode()


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    private: Ed25519PrivateKey = field(repr=False)
    root_public_b64: str | None = None

    @property
    def key_id(self) -> str:
        """The current signing key's id, in this identity's form (a v0 identity keeps the v0 form, D5)."""
        return key_id_like(base64.b64decode(self.public_b64), self.node_id)

    @property
    def wide_id(self) -> str:
        """The 256-bit form of this node's id (equal to `node_id` for an identity minted by wire 1)."""
        return key_id_for(base64.b64decode(self.trust_public_b64))

    @property
    def trust_public_b64(self) -> str:
        return self.root_public_b64 or self.public_b64

    @property
    def public_b64(self) -> str:
        raw = self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode()

    def sign(self, seq: int, kind: str, digest: str) -> str:
        return base64.b64encode(self.private.sign(_message(self.node_id, seq, kind, digest))).decode()

    def sign_price(self, seq: int, digest: str) -> str:
        """Sign one price channel entry (`price_feed_seq` = `seq`) under `PRICE_DOMAIN` (C8.3)."""
        return base64.b64encode(self.private.sign(
            _message(self.node_id, seq, PRICE_KIND, digest, PRICE_DOMAIN))).decode()


def at_rest(value: str) -> str:
    """How a stored `signing_key` value is protected: "encrypted" (`enc:v1:`) or "plaintext" (legacy)."""
    return "encrypted" if value.startswith(ENC_PREFIX) else "plaintext"


def signing_key_at_rest(session: Session) -> str | None:
    row = session.get(NodeState, SIGNING_KEY)
    return at_rest(row.value) if row is not None else None


def seal(settings: Settings | None, key: Ed25519PrivateKey) -> str:
    """The stored form of a private key: `enc:v1:<fernet>` when ATLAS_SECRET_KEY is set, else legacy base64."""
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    plain = base64.b64encode(raw).decode()
    if settings is not None and secrets_box.available(settings):
        return ENC_PREFIX + secrets_box.encrypt(settings, plain)
    log.warning("signing_key_plaintext: the node signing key is stored unencrypted because ATLAS_SECRET_KEY is "
                "not set; every database backup contains it")
    return plain


def unseal(settings: Settings | None, value: str) -> Ed25519PrivateKey:
    if value.startswith(ENC_PREFIX):
        if settings is None or not secrets_box.available(settings):
            raise secrets_box.SecretsError(UNREADABLE)
        try:
            plain = secrets_box.decrypt(settings, value[len(ENC_PREFIX):])
        except secrets_box.SecretsError:
            raise secrets_box.SecretsError(UNREADABLE) from None
    else:
        plain = value
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(plain))


def load_identity(session: Session, settings: Settings | None = None) -> NodeIdentity:
    """Load this node's key, creating one on first start.

    `settings` (default: the `atlas_settings` the session maker carries) decides whether the key is encrypted
    at rest (CONTRACTS-v1 C9.1). An encrypted key that cannot be decrypted raises `SecretsError`; it is never
    replaced. A legacy plaintext key is encrypted in place once a secret is available."""
    if settings is None:
        settings = session.info.get("atlas_settings")
    can_encrypt = settings is not None and secrets_box.available(settings)
    row = session.get(NodeState, SIGNING_KEY, populate_existing=True)
    root = session.get(NodeState, "root_public_key", populate_existing=True)
    saved_id = session.get(NodeState, "node_id", populate_existing=True)
    legacy = row is not None and can_encrypt and at_rest(row.value) == "plaintext"
    if (row is None or root is None or saved_id is None or session.get(NodeState, "feed_seq") is None
            or legacy):
        # Missing rows cannot be protected by SELECT FOR UPDATE. Serialize first start, legacy identity
        # upgrades and the in-place encryption, then re-read after another worker may have committed.
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(727275)"))
        elif session.get_bind().dialect.name == "sqlite":
            session.execute(text("UPDATE node_state SET value=value WHERE key='signing_key'"))
        row = session.get(NodeState, SIGNING_KEY, populate_existing=True)
        root = session.get(NodeState, "root_public_key", populate_existing=True)
        saved_id = session.get(NodeState, "node_id", populate_existing=True)
    if row is None:
        head = session.get(NodeState, "feed_seq")
        if root is not None or (head is not None and int(head.value) > 0):
            raise ValueError("node signing key is missing; restore a verified backup instead of replacing the identity")
        row = NodeState(key=SIGNING_KEY, value=seal(settings, Ed25519PrivateKey.generate()))
        session.add(row)
        session.flush()
    key = unseal(settings, row.value)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if root is None:
        root = NodeState(key="root_public_key", value=base64.b64encode(pub).decode())
        session.add(root)
    root_raw = base64.b64decode(root.value, validate=True)
    if saved_id is None:
        root_id = key_id_for(root_raw)  # D5: a new identity is minted in the 256-bit form
        session.add(NodeState(key="node_id", value=root_id))
    elif id_matches(saved_id.value, root_raw):
        root_id = saved_id.value  # an identity minted before wire 1 keeps its stored (v0) form
    else:
        raise ValueError("stored node identity does not match its trust anchor")
    if session.get(NodeState, "feed_seq") is None:
        session.add(NodeState(key="feed_seq", value="0"))
    if can_encrypt and at_rest(row.value) == "plaintext":
        row.value = seal(settings, key)  # same key: only its stored form changes
        session.add(AuditEntry(actor_id=None, action="signing_key_encrypted", target=root_id,
                               detail={"key_id": key_id_like(pub, root_id), "format": "enc:v1"}))
        log.info("signing_key_encrypted: the node signing key (key id %s) is now encrypted with ATLAS_SECRET_KEY",
                 key_id_like(pub, root_id))
    session.flush()
    return NodeIdentity(node_id=root_id, private=key, root_public_b64=root.value)


_PART = re.compile(r"[\x21-\x7e]+\Z")


def message_parts_ok(parts: list[str]) -> bool:
    """Every element of a domain-separated message is printable ASCII without space or newline (C10 §1.3)."""
    return all(isinstance(part, str) and _PART.match(part) for part in parts)


def domain_message(domain: bytes, parts: list[str]) -> bytes:
    if not domain.startswith(b"atlas-") or not domain.endswith(b"\n"):
        raise ValueError("signing domains start with 'atlas-' and end with a newline")
    if not message_parts_ok(parts):
        raise ValueError("a signed message part is empty or holds a control character, space or non-ASCII text")
    return domain + "\n".join(parts).encode("ascii")


def sign_message(private: Ed25519PrivateKey, domain: bytes, parts: list[str]) -> str:
    """Sign `domain + "\\n".join(parts)` (owner groups, CONTRACT-C10 §1.3). The feed message is unchanged."""
    return base64.b64encode(private.sign(domain_message(domain, parts))).decode()


def verify_message(public_b64: str, domain: bytes, parts: list[str], signature_b64: str) -> bool:
    try:
        message = domain_message(domain, parts)
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64, validate=True))
        pub.verify(base64.b64decode(signature_b64, validate=True), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify(public_b64: str, node_id: str, seq: int, kind: str, body: Any, claimed_hash: str,
           signature_b64: str, *, domain: bytes = DOMAIN) -> bool:
    try:
        digest = body_hash(body)
        if digest != claimed_hash:
            return False
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        pub.verify(base64.b64decode(signature_b64), _message(node_id, seq, kind, digest, domain))
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_price(public_b64: str, node_id: str, seq: int, body: Any, claimed_hash: str, signature_b64: str) -> bool:
    """Verify one price channel entry (C8.3). A main-feed signature over the same body and seq fails here."""
    return verify(public_b64, node_id, seq, PRICE_KIND, body, claimed_hash, signature_b64, domain=PRICE_DOMAIN)
