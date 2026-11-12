"""Signed renditions of feed entries for older wire readers (C16 §22.2, design §4.3-§4.5).

The origin signs one canonical entry per seq at its highest wire, carrying `_wire` (the lowest version that reads
it). A follower whose negotiated `use` is below an entry's `_wire` receives, *in addition to* the canonical row
(critic CR-1: the canonical row is always present), a rendition: a separate object signed under its own domain
`atlas-rendition-v1`, so the same key never signs two feed messages for one seq (it is not a fork). A rendition is
produced by a pure, deterministic adapter, signed once and cached; the origin or a mirror (as itself) may sign it.

Wire 1 is the first wire version, so no real down-adapter exists and this build never attaches a rendition. It
ships the object, the signing domain, the verifier the follower runs on any rendition it is offered, and the
canonical-first rule (a follower that can read the canonical body applies it and ignores renditions): the
one-way doors of design §8.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

from . import signing
from .signing import canonical_json

DOMAIN = b"atlas-rendition-v1\n"
TYPE = "atlas.rendition"
SIGNER_KINDS = ("origin", "mirror")
#: Outcomes of `check` other than "apply this rendition".
UNNEEDED, BAD = "rendition_unneeded", "bad_rendition"


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def parts(obj: dict[str, Any]) -> list[str]:
    """The signed parts (design §4.3), printable ASCII without spaces (C10 §1.3)."""
    origin, adapter, signer = obj["origin"], obj["adapter"], obj["signer"]
    return [origin["node_id"], str(origin["seq"]), origin["kind"], origin["body_hash"], str(obj["rendition_version"]),
            adapter["id"], str(adapter["from"]), str(adapter["to"]), str(adapter["version"]), signer["kind"],
            signer["id"], signer["key_id"], obj["kind"], _sha(obj["body"]), _sha(obj.get("omitted") or [])]


def build(canonical: dict[str, Any], *, version: int, adapter: dict[str, Any], kind: str, body: dict[str, Any],
          omitted: list[str], signer_kind: str, identity: signing.NodeIdentity) -> dict[str, Any]:
    """Sign a rendition of `canonical` (a feed entry as `/sync/changes` serves it) with this node's key."""
    if signer_kind not in SIGNER_KINDS:
        raise ValueError("signer kind is origin or mirror")
    obj = {"t": TYPE, "_v": 1, "rendition_version": version,
           "origin": {k: canonical[k] for k in ("node_id", "seq", "kind", "body_hash", "key_id", "signature")},
           "adapter": {k: adapter[k] for k in ("id", "from", "to", "version")},
           "omitted": sorted(set(omitted)), "kind": kind, "body": body,
           "signer": {"kind": signer_kind, "id": identity.node_id, "key_id": identity.key_id}}
    obj["sig"] = signing.sign_message(identity.private, DOMAIN, parts(obj))
    return obj


@dataclass(frozen=True)
class Verdict:
    result: str  # "apply" | rendition_unneeded | bad_rendition | "fork"
    why: str = ""
    label: str = ""  # "rendition by M of X entry N" for a mirror's rendition


def check(entry: dict[str, Any], rendition: dict[str, Any] | None, *, own_max: int, use: int,
          signer_keys: dict[str, str], mirror_standing: set[str], critical: bool) -> Verdict:
    """What a follower does with one item, after the canonical row verified as today (design §4.4).

    0. canonical `_wire` <= `own_max`: apply the canonical body, ignore every rendition (`rendition_unneeded`):
       the downgrade defence, whatever version a peer advertised.
    1-4. otherwise the rendition must reference exactly the verified canonical row, be signed by the origin or by
       a node with mirror/relay standing (never a mirror for a `critical`/`_requires` entry), verify in its own
       domain with a current key (`signer_keys`: signer id -> current public key), and be at `use`.
    A rendition naming another origin-signed body hash for the same seq is evidence of a fork."""
    body = entry.get("body") if isinstance(entry.get("body"), dict) else {}
    wire = body.get("_wire")
    if type(wire) is int and wire <= own_max:
        return Verdict(UNNEEDED if rendition is not None else "canonical")
    if rendition is None:
        return Verdict(BAD, "no rendition for an entry above this node's wire")
    try:
        if rendition.get("t") != TYPE or rendition.get("_v") != 1:
            return Verdict(BAD, "not a rendition object")
        origin, signer = rendition["origin"], rendition["signer"]
        for key in ("node_id", "seq", "kind", "key_id", "signature"):
            if origin.get(key) != entry.get(key):
                return Verdict(BAD, f"origin.{key} does not match the verified entry")
        if origin.get("body_hash") != entry.get("body_hash"):
            return Verdict("fork", "a rendition of another origin-signed body for this seq")
        if signer.get("kind") not in SIGNER_KINDS:
            return Verdict(BAD, "unknown signer kind")
        is_origin = signer["kind"] == "origin" and signer.get("id") == entry.get("node_id")
        if not is_origin:
            if signer["kind"] != "mirror" or signer.get("id") not in mirror_standing:
                return Verdict(BAD, "signer has no mirror standing here")
            if critical:
                return Verdict(BAD, "a mirror may not render an entry with a critical member or _requires")
        pub = signer_keys.get(signer.get("id", ""))
        if pub is None or not signing.id_matches(signer.get("key_id", ""), base64.b64decode(pub)):
            return Verdict(BAD, "signer key is not current")
        if not signing.verify_message(pub, DOMAIN, parts(rendition), rendition.get("sig", "")):
            return Verdict(BAD, "rendition signature does not verify")
        adapter = rendition["adapter"]
        if rendition["rendition_version"] != use or adapter.get("to") != use or adapter.get("from") != wire:
            return Verdict(BAD, "rendition is not at the negotiated version")
    except (KeyError, TypeError, ValueError, AttributeError):
        return Verdict(BAD, "malformed rendition")
    label = "" if is_origin else f"rendition by {signer['id'][:16]} of {entry['node_id'][:16]} entry {entry['seq']}"
    return Verdict("apply", label=label)
