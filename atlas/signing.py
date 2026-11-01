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
SIGNING_KEY = "signing_key"
# Stored form of an encrypted private key (CONTRACTS-v1 C9.1); a value without it is legacy plaintext base64.
ENC_PREFIX = "enc:v1:"
UNREADABLE = "signing key is encrypted; ATLAS_SECRET_KEY is missing or wrong"

log = logging.getLogger("atlas.signing")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def body_hash(body: Any) -> str:
    return hashlib.sha256(canonical_json(body)).hexdigest()


def key_id_for(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _message(node_id: str, seq: int, kind: str, digest: str) -> bytes:
    return DOMAIN + f"{node_id}\n{seq}\n{kind}\n{digest}".encode()


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    private: Ed25519PrivateKey = field(repr=False)
    root_public_b64: str | None = None

    @property
    def key_id(self) -> str:
        return key_id_for(base64.b64decode(self.public_b64))

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
    root_id = key_id_for(base64.b64decode(root.value, validate=True))
    if saved_id is None:
        session.add(NodeState(key="node_id", value=root_id))
    elif saved_id.value != root_id:
        raise ValueError("stored node identity does not match its trust anchor")
    if session.get(NodeState, "feed_seq") is None:
        session.add(NodeState(key="feed_seq", value="0"))
    if can_encrypt and at_rest(row.value) == "plaintext":
        row.value = seal(settings, key)  # same key: only its stored form changes
        session.add(AuditEntry(actor_id=None, action="signing_key_encrypted", target=root_id,
                               detail={"key_id": key_id_for(pub), "format": "enc:v1"}))
        log.info("signing_key_encrypted: the node signing key (key id %s) is now encrypted with ATLAS_SECRET_KEY",
                 key_id_for(pub))
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
           signature_b64: str) -> bool:
    try:
        digest = body_hash(body)
        if digest != claimed_hash:
            return False
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        pub.verify(base64.b64decode(signature_b64), _message(node_id, seq, kind, digest))
        return True
    except (InvalidSignature, ValueError):
        return False
