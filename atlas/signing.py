"""Node identity and Ed25519 signing for the change feed."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy.orm import Session

from .models import NodeState

DOMAIN = b"atlas-feed-v1\n"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def body_hash(body: Any) -> str:
    return hashlib.sha256(canonical_json(body)).hexdigest()


def key_id_for(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _message(node_id: str, seq: int, digest: str) -> bytes:
    return DOMAIN + f"{node_id}\n{seq}\n{digest}".encode()


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    private: Ed25519PrivateKey

    @property
    def public_b64(self) -> str:
        raw = self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode()

    def sign(self, seq: int, digest: str) -> str:
        return base64.b64encode(self.private.sign(_message(self.node_id, seq, digest))).decode()


def load_identity(session: Session) -> NodeIdentity:
    """Load this node's key, creating one on first start."""
    row = session.get(NodeState, "signing_key")
    if row is None:
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        row = NodeState(key="signing_key", value=base64.b64encode(raw).decode())
        session.add(row)
        session.flush()
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(row.value))
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return NodeIdentity(node_id=key_id_for(pub), private=key)


def verify(public_b64: str, node_id: str, seq: int, body: Any, claimed_hash: str, signature_b64: str) -> bool:
    try:
        digest = body_hash(body)
        if digest != claimed_hash:
            return False
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        pub.verify(base64.b64decode(signature_b64), _message(node_id, seq, digest))
        return True
    except (InvalidSignature, ValueError):
        return False
