"""Signed objects of the node link (CONTRACT-C14 §1.4, normative envelope; Review #1 C14-13).

Two object types travel between a node and a main: the link grant (`atlas.link`, signed by the main's node key)
and a contribution (`atlas.contribution`, signed by the node's link key). Unlike the C10 group envelope, `t` and
`_v` are inside the signed parts:

    [issuer, t, str(_v), signer.kind, signer.id, signer.key_id, id, body_hash]

under `LINK_DOMAINS[t]`. `group_crypto.make_object`/`verify_object` are never used for these (they need
`group_id` and the group object types), and `verify_object` refuses a link object.

Also here: the node key proof of possession at link time and at rotation (`atlas-link-pop-v1`), the link key's own
rotation certificates (the feed's `key_rotation` layout under the link root, so `key_rotation.validate_history`
validates them unchanged), and the user-code HMAC domain (`atlas-link-ucode-v1`, never signed).
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .signing import body_hash, canonical_json, id_matches, key_id_like, sign_message, verify_message

LINK_TYPES = ("atlas.link", "atlas.contribution")
LINK_DOMAINS: dict[str, bytes] = {
    "atlas.link": b"atlas-link-grant-v1\n",
    "atlas.contribution": b"atlas-contrib-v1\n",
}
POP_DOMAIN = b"atlas-link-pop-v1\n"
#: HMAC inputs only (never signed): the user code's stored form (§6.3) and the pairwise subject (§6.4).
UCODE_DOMAIN = b"atlas-link-ucode-v1\n"
SUB_DOMAIN = b"atlas-link-sub-v1\n"
SUPPORTED_VERSION = 1


class LinkCryptoError(ValueError):
    pass


def domains() -> list[bytes]:
    """The signing domains of the link (part of the prefix-free check of C10 §11 A10-a)."""
    return [*LINK_DOMAINS.values(), POP_DOMAIN]


def public_b64(private: Ed25519PrivateKey) -> str:
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def key_id(public: str, like: str | None = None) -> str:
    """A link key's id: 256-bit (D5), or in the form of `like` (the link's root id) for an existing link."""
    return key_id_like(base64.b64decode(public, validate=True), like)


def key_id_matches(public: str, kid: object) -> bool:
    return id_matches(kid, base64.b64decode(public, validate=True))


def parts(obj: dict) -> list[str]:
    signer = obj["signer"]
    return [obj["issuer"], obj["t"], str(obj["_v"]), signer["kind"], signer["id"], signer["key_id"], obj["id"],
            obj["body_hash"]]


def make(t: str, issuer: str, oid: str, body: dict, signer: dict, private: Ed25519PrivateKey) -> dict:
    if t not in LINK_TYPES:
        raise LinkCryptoError(f"unknown link object type {t!r}")
    obj = {"t": t, "_v": 1, "issuer": issuer, "id": oid, "body": body,
           "signer": {"kind": signer["kind"], "id": signer["id"], "key_id": signer["key_id"]},
           "body_hash": body_hash(body)}
    obj["sig"] = sign_message(private, LINK_DOMAINS[t], parts(obj))
    return obj


def well_formed(obj: Any) -> bool:
    try:
        if not isinstance(obj, dict) or obj.get("t") not in LINK_TYPES or type(obj.get("_v")) is not int:
            return False
        if not 1 <= obj["_v"] <= SUPPORTED_VERSION or not isinstance(obj.get("body"), dict):
            return False
        signer = obj.get("signer")
        if not isinstance(signer, dict) or set(signer) != {"kind", "id", "key_id"}:
            return False
        if not all(isinstance(x, str) and x for x in (obj.get("issuer"), obj.get("id"), obj.get("body_hash"),
                                                         obj.get("sig"), *signer.values())):
            return False
        return set(obj) == {"t", "_v", "issuer", "id", "body", "signer", "body_hash", "sig"}
    except (TypeError, AttributeError):
        return False


def verify(obj: Any, public_b64: str, *, t: str | None = None) -> bool:
    """True when `obj` is a well-formed link object of type `t` (when given), its body hash matches, and the
    signature verifies under `public_b64` (whose key id must be `signer.key_id`)."""
    if not well_formed(obj) or (t is not None and obj["t"] != t):
        return False
    try:
        if not key_id_matches(public_b64, obj["signer"]["key_id"]) or body_hash(obj["body"]) != obj["body_hash"]:
            return False
        return verify_message(public_b64, LINK_DOMAINS[obj["t"]], parts(obj), obj["sig"])
    except (ValueError, TypeError, KeyError):
        return False


# ---- proof of possession (§6.3 step 1, §6.7 rotation) -------------------------------------------------------

def node_hash(atlas_node: dict) -> str:
    return hashlib.sha256(canonical_json(atlas_node)).hexdigest()


def registration_parts(issuer: str, atlas_node: dict, challenge: str) -> list[str]:
    return [issuer, atlas_node["node_id"], atlas_node["key_id"], node_hash(atlas_node), challenge]


def rotation_parts(issuer: str, link_id: str, node_id: str, new_key_id: str, epoch: int) -> list[str]:
    return [issuer, link_id, node_id, new_key_id, str(epoch)]


def sign_pop(private: Ed25519PrivateKey, parts_: list[str]) -> str:
    return sign_message(private, POP_DOMAIN, parts_)


def verify_pop(public: str, parts_: list[str], signature: str) -> bool:
    return isinstance(signature, str) and verify_message(public, POP_DOMAIN, parts_, signature)


# ---- the link key's own rotation history ----------------------------------------------------------------------

def rotation_certificate(previous: Ed25519PrivateKey, root_id: str, seq: int, successor_public: str) -> dict:
    """One transition of a link key history, in the feed's `key_rotation` layout under the link root id, signed
    by the previous key. `key_rotation.validate_history(root_id, root_public, history, current)` validates a
    chain of them. A link key never signs a feed, so the layout cannot be mistaken for a feed entry of any node."""
    from .signing import _message

    body = {"key_id": key_id(successor_public, root_id), "public_key": successor_public}
    digest = body_hash(body)
    signature = base64.b64encode(previous.sign(_message(root_id, seq, "key_rotation", digest))).decode()
    return {"seq": seq, "kind": "key_rotation", "body": body, "body_hash": digest, "signature": signature,
            "key_id": key_id(public_b64(previous), root_id), "node_id": root_id}
