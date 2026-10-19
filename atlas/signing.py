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

DOMAIN = b"atlas-feed-v2\n"


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
    private: Ed25519PrivateKey
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


def load_identity(session: Session) -> NodeIdentity:
    """Load this node's key, creating one on first start."""
    row = session.get(NodeState, "signing_key", populate_existing=True)
    if row is None:
        root = session.get(NodeState, "root_public_key")
        head = session.get(NodeState, "feed_seq")
        if root is not None or (head is not None and int(head.value) > 0):
            raise ValueError("node signing key is missing; restore a verified backup instead of replacing the identity")
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        row = NodeState(key="signing_key", value=base64.b64encode(raw).decode())
        session.add(row)
        session.flush()
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(row.value))
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    root = session.get(NodeState, "root_public_key", populate_existing=True)
    if root is None:
        root = NodeState(key="root_public_key", value=base64.b64encode(pub).decode())
        session.add(root)
    root_id = key_id_for(base64.b64decode(root.value, validate=True))
    saved_id = session.get(NodeState, "node_id", populate_existing=True)
    if saved_id is None:
        session.add(NodeState(key="node_id", value=root_id))
    elif saved_id.value != root_id:
        raise ValueError("stored node identity does not match its trust anchor")
    if session.get(NodeState, "feed_seq") is None:
        session.add(NodeState(key="feed_seq", value="0"))
    session.flush()
    return NodeIdentity(node_id=root_id, private=key, root_public_b64=root.value)


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
