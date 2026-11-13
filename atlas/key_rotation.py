"""Successor keys vouched for by the existing signed, append-only feed."""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import feed
from .models import ChangeLog, NodeState, ReplicatedChange
from .service import AtlasError, write_lock
from .settings import Settings
from .signing import SIGNING_KEY, NodeIdentity, id_matches, load_identity, seal, verify

MAX_ROTATIONS = 128
PROOF_FIELDS = ("seq", "kind", "body", "body_hash", "signature", "key_id", "node_id")


class RotationError(ValueError):
    pass


def proof(row, node_id):
    return {"seq": row.seq, "kind": row.kind, "body": row.body, "body_hash": row.body_hash,
            "signature": row.signature, "key_id": row.key_id, "node_id": node_id}


def own_history(session: Session, node_id: str) -> list[dict]:
    return [proof(row, node_id) for row in session.scalars(select(ChangeLog).where(ChangeLog.kind == "key_rotation")
                                                           .order_by(ChangeLog.seq))]


def trusted_history(session: Session, node_id: str) -> list[dict]:
    row = session.get(NodeState, "trusted_rotations:" + node_id)
    return json.loads(row.value) if row else []


def validate_history(root_id: str, root_public: str, certificates, advertised_key: str) -> tuple[list[dict], dict[str, str]]:
    """Validate every transition from an independently pinned root, before trusting any successor."""
    if not isinstance(certificates, list) or len(certificates) > MAX_ROTATIONS:
        raise RotationError("invalid or oversized signing-key history")
    try:
        raw_root = base64.b64decode(root_public, validate=True)
        if len(raw_root) != 32 or not id_matches(root_id, raw_root):
            raise RotationError("invalid pinned trust anchor")
    except (TypeError, ValueError) as exc:
        raise RotationError("invalid pinned trust anchor") from exc
    keys, history, previous_seq = {root_id: root_public}, [], 0
    current = root_id
    try:
        for raw in certificates:
            item = {field: raw[field] for field in PROOF_FIELDS}
            seq, body = item["seq"], item["body"]
            if (type(seq) is not int or not previous_seq < seq < 2**63 or item["node_id"] != root_id
                    or item["key_id"] != current or item["kind"] != "key_rotation"
                    or not isinstance(body, dict) or set(body) - {"_wire"} != {"key_id", "public_key"}
                    or ("_wire" in body and (type(body["_wire"]) is not int or body["_wire"] < 1))):  # C16 envelope
                raise RotationError("invalid signing-key transition")
            if not verify(keys[current], root_id, seq, "key_rotation", body, item["body_hash"], item["signature"]):
                raise RotationError("signing-key transition has an invalid signature")
            public = base64.b64decode(body["public_key"], validate=True)
            if len(public) != 32 or not id_matches(body["key_id"], public) or body["key_id"] in keys:
                raise RotationError("invalid or repeated successor key")
            current = body["key_id"]
            keys[current] = body["public_key"]
            history.append(item)
            previous_seq = seq
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, RotationError):
            raise
        raise RotationError("malformed signing-key transition") from exc
    if current != advertised_key:
        raise RotationError("current signing key is not vouched for by the pinned root")
    return history, keys


def remember(session: Session, root_id: str, history: list[dict]) -> None:
    row = session.get(NodeState, "trusted_rotations:" + root_id)
    old = json.loads(row.value) if row else []
    if len(history) < len(old) or history[:len(old)] != old:
        raise RotationError("upstream signing-key history rewound or forked")
    for item in history:
        retained = session.get(ReplicatedChange, (root_id, item["seq"]))
        if retained and (retained.kind != "key_rotation" or retained.body_hash != item["body_hash"]
                         or retained.signature != item["signature"]):
            raise RotationError("signing-key transition conflicts with retained history")
    if row is None:
        session.add(NodeState(key="trusted_rotations:" + root_id, value=json.dumps(history, sort_keys=True)))
    else:
        row.value = json.dumps(history, sort_keys=True)
    session.flush()


def expected_key(root_id: str, history: list[dict], seq: int) -> str:
    current = root_id
    for item in history:
        if seq <= item["seq"]:
            break
        current = item["body"]["key_id"]
    return current


def check_entry(root_id: str, history: list[dict], keys: dict[str, str], entry: dict) -> bool:
    if type(entry["seq"]) is not int or not 0 < entry["seq"] < 2**63:
        return False
    expected = expected_key(root_id, history, entry["seq"])
    if entry["node_id"] != root_id or entry["key_id"] != expected:
        return False
    transition = next((item for item in history if item["seq"] == entry["seq"]), None)
    if transition is not None and any(entry[field] != transition[field] for field in PROOF_FIELDS):
        return False
    if entry["kind"] == "key_rotation" and transition is None:
        return False
    return verify(keys[expected], root_id, entry["seq"], entry["kind"], entry["body"],
                  entry["body_hash"], entry["signature"])


def rotate(session: Session, expected: str, settings: Settings | None = None) -> NodeIdentity:
    """Replace the signing key. The successor is stored encrypted when ATLAS_SECRET_KEY is set (C9.1)."""
    if settings is None:
        settings = session.info.get("atlas_settings")
    if session.get_bind().dialect.name == "sqlite":
        # Acquire a writer lock even if the form dependency already read the account.
        session.execute(text("UPDATE node_state SET value=value WHERE key='feed_seq'"))
    write_lock(session)
    session.scalars(select(NodeState).where(NodeState.key == "feed_seq").with_for_update()).one()
    old = load_identity(session, settings)
    if old.key_id != expected:
        raise AtlasError("The signing key changed. Reload and confirm the current key.", "conflict")
    if len(own_history(session, old.node_id)) >= MAX_ROTATIONS:
        raise AtlasError("This identity reached its rotation limit; plan a new trust anchor.", "conflict")
    successor = NodeIdentity(old.node_id, Ed25519PrivateKey.generate(), old.trust_public_b64)
    feed.append(session, old, "key_rotation", {"key_id": successor.key_id, "public_key": successor.public_b64})
    session.get(NodeState, SIGNING_KEY).value = seal(settings, successor.private)
    session.flush()
    session.info.pop("atlas_feed_signer", None)
    return successor
