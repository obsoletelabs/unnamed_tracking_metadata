"""Change-entry kinds of the owner-group private channel and their classification (CONTRACT-C10 §3.4-§3.5).

The registry has the shape of `feed_schema.FEED_KINDS` and classification follows C1.4 (fail closed: an unknown
kind, body version or capability parks), with one difference (D10.15): a `profile.field.*` entry naming a field
this release does not know is `held` and does not block its stream, because fields are independent registers.

Group kinds are never registered in `feed_schema`, so `feed.append` refuses them (G-V4, test A11-a).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from . import group_crypto as gc
from . import profile_schema
from .feed_schema import (
    APPLY,
    CAPABILITIES,
    ENVELOPE,
    PARK,
    QUARANTINE,
    KindSpec,
    Outcome,
    _invalid,
    _park,
    _requires,
)
from .signing import ID_PATTERN

HELD = "held"
CAPABILITIES_GROUP = ("seal-x25519-chacha20poly1305-v1", "store-forward-v1", "assert-link-v1")
HLC = re.compile(r"^[0-9]{13}-[0-9a-f]{4}-" + ID_PATTERN + r"\Z")
ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")
NODE = re.compile(r"^" + ID_PATTERN + r"\Z")
EPOCH = re.compile(r"^[0-9a-f]{8}\Z")
CAUSES = ("user", "import", "default", "migration")
MAX_BASE = 8
MAX_BODY = 8 * 1024
NODE_SCOPE = "_node"


def _spec(name: str, fields: set[str], summary: str) -> KindSpec:
    return KindSpec(name=name, since=1, emit=1, apply=1, fields=frozenset(fields | {"issued_at"}),
                    summary=summary)


GROUP_KINDS: dict[str, KindSpec] = {s.name: s for s in (
    _spec("profile.field.set", {"account_uid", "field", "value", "hlc", "base", "cause"}, "a preference version"),
    _spec("profile.field.clear", {"account_uid", "field", "hlc", "base", "cause"}, "a preference reset to default"),
    _spec("profile.conflict.resolve", {"account_uid", "field", "chosen_hlc", "discarded"}, "a conflict decision"),
    _spec("account.link", {"account_uid", "node_id", "minted", "evidence", "mint_nonce"}, "a node holds this account"),
    _spec("account.unlink", {"account_uid", "node_id", "reason"}, "a node no longer holds this account"),
    _spec("account.signout_all", {"account_uid", "before"}, "end sessions created before `before`"),
    _spec("stream.epoch", {"epoch", "previous", "reason"}, "the first entry of an epoch"),
    # C10 R1-6: the issuing node's witness of an assertion it signed (a 1.0 node of the a12 line parks it)
    _spec("account.assert_issued", {"account_uid", "aud_node", "jti", "assertion_hash", "purpose"},
          "this node issued an identity assertion"),
)}
PROFILE_KINDS = ("profile.field.set", "profile.field.clear", "profile.conflict.resolve")
ACCOUNT_KINDS = (*PROFILE_KINDS, "account.link", "account.unlink", "account.signout_all", "account.assert_issued")
MINT_NONCE = re.compile(r"^[0-9a-f]{32}\Z")
HASH = re.compile(r"^[0-9a-f]{64}\Z")
# Reserved for a later wire (D10.24): a node of this release parks them, blocking only their own stream.
RESERVED = ("profile.list.upsert", "profile.list_item.upsert", "profile.list_item.delete", "admin.setting.set",
            "profile.avatar.blob")
EPOCH_REASONS = ("first", "restore", "rotation", "manual", "promotion")
UNLINK_REASONS = ("user", "admin", "deleted")


def kinds_advertised() -> dict[str, dict[str, int]]:
    return {name: {"emit": s.emit, "apply": s.apply} for name, s in GROUP_KINDS.items()}


def proto_of(use: int) -> str:
    """The inner `proto` of a group call (design §5.2): `w<use>`, inside the signed, sealed request."""
    return f"w{use}"


def proto_use(proto: Any, prof: Any = None) -> int | None:
    """The wire a group call's inner `proto` names, when this build speaks it; None otherwise (a v0 member sends
    "1.0", which is never a wire)."""
    from . import wire

    prof = prof or wire.DEFAULT
    if not isinstance(proto, str) or not proto.startswith("w") or not proto[1:].isdigit() or len(proto) > 5:
        return None
    use = int(proto[1:])
    return use if prof.min_peer <= use <= prof.max else None


def negotiate(remote: dict[str, Any], prof: Any = None) -> int | None:
    """The wire to speak with a member from its `hello` (`wire: {min, max}`), or None when there is no overlap or
    the member is v0 (it answers `group_protocol` only): fail closed."""
    from . import wire

    prof = prof or wire.DEFAULT
    try:
        theirs = wire.range_of(remote.get("wire") if isinstance(remote, dict) else None)
        return wire.negotiate(prof.speaks, theirs)
    except (wire.InvalidWire, wire.WireUnsupported):
        return None


def _hlc_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(HLC.match(value))


def classify(kind: str, body: Any) -> Outcome:
    """apply | parked (blocks this stream) | held (unknown field, does not block) | quarantined (invalid)."""
    spec = GROUP_KINDS.get(kind)
    if spec is None:
        return _park("unsupported_kind", kind)
    if not isinstance(body, dict):
        return _invalid(kind, "body is not an object")
    version = body.get("_v", 1)
    if type(version) is not int or version < 1:
        return _park("unsupported_version", kind, _v=str(version)[:20])
    if version > spec.apply:
        return _park("unsupported_version", kind, _v=version)
    requires = _requires(body)
    if requires is None:
        return _park("unsupported_capability", kind, unknown=["_requires is malformed"])
    unknown_tokens = [t for t in requires if t not in CAPABILITIES and t not in CAPABILITIES_GROUP]
    if unknown_tokens:
        return _park("unsupported_capability", kind, requires=unknown_tokens)
    unknown = sorted(k for k in body if k not in spec.fields and k not in ENVELOPE)
    if unknown:
        return _park("unsupported_field", kind, unknown=unknown[:50])
    if kind in PROFILE_KINDS:
        field = body.get("field")
        if not isinstance(field, str):
            return _invalid(kind, "field is missing")
        if not profile_schema.known(field):
            return Outcome(HELD, "unsupported_field", {"kind": kind, "unknown": [field[:48]]})
    why = _invalid_reason(kind, body)
    if why:
        return _invalid(kind, why)
    clean = {k: v for k, v in body.items() if k not in ENVELOPE}
    if kind == "profile.field.set":
        clean["value"] = profile_schema.validate(body["field"], body["value"])
    return Outcome(APPLY, body=clean)


def _invalid_reason(kind: str, b: dict) -> str | None:
    if not isinstance(b.get("issued_at"), str):
        return "issued_at is missing"
    if kind in ACCOUNT_KINDS and not (isinstance(b.get("account_uid"), str) and ULID.match(b["account_uid"])):
        return "account_uid is not a ULID"
    if kind in ("profile.field.set", "profile.field.clear"):
        if not _hlc_ok(b.get("hlc")):
            return "hlc is malformed"
        base = b.get("base")
        if not isinstance(base, list) or len(base) > MAX_BASE or not all(_hlc_ok(h) for h in base) \
                or len(set(base)) != len(base):
            return "base is malformed"
        if b.get("cause") not in CAUSES:
            return "cause is unknown"
        if kind == "profile.field.set":
            if "value" not in b:
                return "value is missing"
            try:
                profile_schema.validate(b["field"], b["value"])
            except profile_schema.FieldError as exc:
                return f"value: {exc}"
        elif "value" in b:
            return "a clear carries no value"
    elif kind == "profile.conflict.resolve":
        disc = b.get("discarded")
        if not _hlc_ok(b.get("chosen_hlc")) or not isinstance(disc, list) or len(disc) > 64 \
                or not all(_hlc_ok(h) for h in disc):
            return "chosen_hlc or discarded is malformed"
    elif kind == "account.link":
        if not (isinstance(b.get("node_id"), str) and NODE.match(b["node_id"])):
            return "node_id is malformed"
        minted, evidence = b.get("minted"), b.get("evidence")
        if not ((minted is True and evidence is None) or (minted in (None, False) and isinstance(evidence, dict))):
            return "a link is either minted or carries evidence"
        nonce = b.get("mint_nonce")
        if nonce is not None and (minted is not True or not (isinstance(nonce, str) and MINT_NONCE.match(nonce))):
            return "mint_nonce is malformed or on a link that is not minted"
    elif kind == "account.assert_issued":
        if not (isinstance(b.get("aud_node"), str) and NODE.match(b["aud_node"])):
            return "aud_node is malformed"
        if not (isinstance(b.get("jti"), str) and ULID.match(b["jti"])) or b.get("purpose") not in ("link", "signin"):
            return "jti or purpose is malformed"
        if not (isinstance(b.get("assertion_hash"), str) and HASH.match(b["assertion_hash"])):
            return "assertion_hash is malformed"
    elif kind == "account.unlink":
        if not (isinstance(b.get("node_id"), str) and NODE.match(b["node_id"])) or b.get("reason") not in UNLINK_REASONS:
            return "node_id or reason is malformed"
    elif kind == "account.signout_all":
        if not isinstance(b.get("before"), str):
            return "before is missing"
        try:  # §5.6: `before` is the emitter's `issued_at`; a sign-out never reaches into the future
            before, issued = gc.parse_time(b["before"]), gc.parse_time(b["issued_at"])
        except (gc.GroupCryptoError, TypeError, ValueError):
            return "before or issued_at is not a timestamp"
        if before > issued + timedelta(seconds=gc.SKEW):
            return "before is later than issued_at"
    elif kind == "stream.epoch":
        prev = b.get("previous")
        if not (isinstance(b.get("epoch"), str) and EPOCH.match(b["epoch"])) or b.get("reason") not in EPOCH_REASONS:
            return "epoch or reason is malformed"
        if prev is not None:
            if not isinstance(prev, dict) or not (isinstance(prev.get("epoch"), str) and EPOCH.match(prev["epoch"])):
                return "previous is malformed"
            heads = prev.get("heads")
            if not isinstance(heads, dict) or not all(isinstance(k, str) and type(v) is int and v >= 0
                                                      for k, v in heads.items()):
                return "previous.heads is malformed"
    return None


__all__ = ["APPLY", "HELD", "PARK", "QUARANTINE", "GROUP_KINDS", "classify", "proto_of", "proto_use", "negotiate"]
