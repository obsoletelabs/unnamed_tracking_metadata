"""The federation wire, described once: protocol versions, feed kinds, their fields and vocabularies.

Every place that needs to know what a feed entry may contain reads this module: the follower's
classification (`classify`), the authority's emission gate (`check_emit`), `/sync/info`
(`kinds_advertised`), recovery (`promotion`) and the guide. Adding a field or a kind without
listing it here makes followers of this release *park* it (fail closed) instead of silently
dropping it. See CONTRACTS-v1 section C1 and docs/guide/federation.md "Compatibility and upgrades".

Rules (C1.1):

* A minor protocol bump adds something an older 1.1+ follower can safely park: a kind, a body
  version, an `ext` member or a vocabulary value.
* A major bump changes signature verification or the meaning of an existing field.
* A field may be added in a later minor without parking 1.1 followers only if it is listed as
  reserved-optional here first. In 1.1 that is `achievements.items[].key` only.

Protocol 1.2 (C7.1) adds the achievement tier `platinum`. An entry that carries it also lists the
`_requires` token `tier-platinum`, so a 1.1 follower parks the whole list (`unsupported_capability`)
instead of dropping it, and this release applies it. A token-less platinum entry still parks a 1.1
follower, as `unsupported_value`.

Protocol 1.3 (C5.2) adds `observation.payload.ext.release` (partial and regional release dates), a
`releases` lock and partial `release_date` lock values. An entry that carries any of them lists the
`_requires` token `dates-v1`, so a 1.1 or 1.2 follower parks it (`unsupported_capability`) instead of
stripping the dates or storing "2011" as a day-only `release_date`.

Protocol 1.4 (C6.3) adds the feed kind `relation` (`{op: add|end, from, to, type, source}`): a directed
relation between two works with a type from `vocab.RELATION_TYPES`. A 1.1-1.3 follower does not know the kind
and parks it (`unsupported_kind`); an unknown type or op parks a 1.4 follower (`unsupported_value`). Below
emit protocol 1.4 an authority keeps relations local (C1.6, H13): nothing is appended.

Protocol 1.5 (CONTRACT-C13 §3.2) adds the feed kind `record` (`{canonical_id, media_type, title, provider_ids,
because}`): a record introduced by identity only, signed before any other entry that names it, so a follower never
receives artwork, achievements, custom values, locks, merges or relations for a game it has never heard of. A
1.1-1.4 follower parks it (`unsupported_kind`); an unknown `because` parks a 1.5 follower (`unsupported_value`).

Protocol 1.6 (C8.3) is the price channel: a *separate* signed channel (`/api/v2/sync/price-changes`, its own
sequence, domain `atlas-price-v1`), never entries in this feed, announced by the capability `price-channel-v1`.
This release designs and registers it (`PRICE_CHANNEL`) but does not emit it: `CHANNEL_ENABLED` is False, the
capability is not advertised and the endpoint answers 404. The feed kind `price` is reserved, so a follower of this
release parks a `price` entry that ever appears in the main feed (`unsupported_kind`), and this node refuses to sign
one at any emit protocol.

Protocols 1.7-1.9 are provisional (CONTRACT-C11 §1.4, H36): 1.7 adds the kinds `unmerge` and `distinct` and lock
body version 2 (`_requires` `field-clear-v1` for an explicit clear, `merge-guard-v1` for the `_no_merge` guard); 1.8
achievements body v2 (`ach-v2`); 1.9 artwork body v2 (`art-v2`, artwork kind `trailer`). The permanent `*_SINCE`
maps record all of them and this release reserves them: a follower of this release parks them, and `check_emit`
refuses them at every emit protocol. In particular a `lock` body naming `_no_merge` is never signed as a version 1
body (it would park every pre-C11 follower).

The price channel and the C11/C12 reservations were 1.5-1.8 until A19: C13's `record` took the first minor no
release had advertised and each of them moved up one (CONTRACT-C13 §3.1, D13.1, owner decision H52). No release ever
advertised or applied any of them, so no node disagrees about what those numbers mean.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from . import vocab

# ---- versions -------------------------------------------------------------------------------------------

#: The federation wire protocol this release speaks and applies ("MAJOR.MINOR").
PROTOCOL = "1.5"
#: Pre-A1 releases advertise no protocol; they speak this.
LEGACY_PROTOCOL = "1.0"
#: Version of the observation payload schema (`SourceObservationV1`).
OBSERVATION_SCHEMA = 1
#: Default lowest follower protocol an authority serves: the previous Atlas minor release (C1.3).
DEFAULT_MIN_FOLLOWER_PROTOCOL = "1.0"
#: Request header a follower sends so its upstream can show which followers still need an upgrade.
PROTOCOL_HEADER = "Atlas-Protocol"

#: Named optional features this node advertises and understands in `_requires`.
CAPABILITIES: tuple[str, ...] = ("ext-v1", "dates-v1")
#: Vocabulary tokens (C1.4 `_requires`) this release understands: one per value a later minor added, such
#: as `tier-platinum` (1.2). They are not optional features, so `/sync/info.capabilities` does not list them.
VOCAB_TOKENS: tuple[str, ...] = tuple(sorted(set(vocab.VALUE_TOKEN.values())))
#: Operator-facing name of each gated vocabulary value, used in the emission gate's message.
VALUE_FEATURE: dict[tuple[str, str], str] = {("achievement_tier", "platinum"): "the platinum achievement tier"}
#: `_requires` tokens reserved by later protocol minors, with the protocol that ships them. A node
#: never emits one before its `emit_protocol` reaches that protocol (H13); this release cannot apply them.
RESERVED_TOKENS: dict[str, str] = {"tier-platinum": "1.2", "dates-v1": "1.3", "relation-v1": "1.4",
                                   "price-channel-v1": "1.6", "field-clear-v1": "1.7", "merge-guard-v1": "1.7",
                                   "ach-v2": "1.8", "art-v2": "1.9"}

# Permanent maps (CONTRACT-C11 §1.4 rule 1): the protocol that introduced each gated construct. `check_emit`
# consults them for the construct's whole life, so it keeps refusing it below that protocol after the construct
# ships (an entry leaves RESERVED_* when its applying release merges, never these maps). The H52 shift (C13 §3.1)
# moved the unreleased reservations up one minor; once a release that advertises any of them is tagged, its number
# is permanent.
#: Feed kinds added after 1.0.
KIND_SINCE: dict[str, str] = {"relation": "1.4", "record": "1.5", "price": "1.6", "unmerge": "1.7", "distinct": "1.7"}
#: `_requires` tokens.
TOKEN_SINCE: dict[str, str] = {"tier-platinum": "1.2", "dates-v1": "1.3", "relation-v1": "1.4",
                               "price-channel-v1": "1.6", "field-clear-v1": "1.7", "merge-guard-v1": "1.7",
                               "ach-v2": "1.8", "art-v2": "1.9"}
#: Closed-vocabulary values added after 1.0, as (vocabulary, value).
VALUE_SINCE: dict[tuple[str, str], str] = {("achievement_tier", "platinum"): "1.2", ("lock_field", "_no_merge"): "1.7",
                                           ("artwork_kind", "trailer"): "1.9"}
#: Body versions above 1, as (kind, version).
BODY_SINCE: dict[tuple[str, int], str] = {("lock", 2): "1.7", ("unlock", 2): "1.7", ("achievements", 2): "1.8",
                                          ("artwork", 2): "1.9"}
#: C13 §3.2: the protocol of the `record` kind, and the reserved namespaces a `record` (or a bridge observation)
#: never carries in `provider_ids`: an Atlas id or a curated namespace there could claim a follower's own record.
RECORD_PROTOCOL = "1.5"
RECORD_SOURCE = "atlas-record"
RECORD_RESERVED_NAMESPACES = frozenset({"atlas", RECORD_SOURCE, "curator", "manual", "community"})
RECORD_TITLE_MAX = 300
#: The lock "field" of the C11 merge guard. Only ever a lock body version 2 (`merge-guard-v1`).
NO_MERGE_FIELD = "_no_merge"

_PROTOCOL = re.compile(r"(\d{1,4})\.(\d{1,4})\Z")
ENVELOPE = ("_v", "_requires")
MAX_REQUIRES, MAX_TOKEN = 16, 40


def parse_protocol(value: Any) -> tuple[int, int]:
    """'1.3' -> (1, 3). Anything else raises ValueError: a malformed version is never guessed at."""
    match = _PROTOCOL.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"not a protocol version: {str(value)[:20]!r}")
    return int(match.group(1)), int(match.group(2))


def valid_protocol(value: Any) -> bool:
    try:
        parse_protocol(value)
    except ValueError:
        return False
    return True


def fmt(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def protocol_choices(low: str = LEGACY_PROTOCOL, high: str = PROTOCOL) -> list[str]:
    """Every protocol from `low` to `high` within this release's major (and N-1 major's last minor)."""
    lo, hi = parse_protocol(low), parse_protocol(high)
    out = []
    for major in range(lo[0], hi[0] + 1):
        first = lo[1] if major == lo[0] else 0
        last = hi[1] if major == hi[0] else first
        out += [fmt((major, minor)) for minor in range(first, last + 1)]
    return out


# ---- feed kinds -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class KindSpec:
    """One feed kind: what this release emits and applies, and the exact fields it understands."""

    name: str
    since: str  # protocol that introduced the kind
    emit: int  # highest body version this release writes
    apply: int  # highest body version this release applies
    fields: frozenset[str]  # top-level body fields (envelope keys excluded)
    summary: str
    # path -> allowed member names, for nested objects or lists of objects ("items[]", "observation")
    nested: dict[str, frozenset[str]] = field(default_factory=dict)
    # path -> name of the closed vocabulary its values must belong to
    values: dict[str, str] = field(default_factory=dict)
    # reserved-optional fields: a 1.1 follower accepts them (this release stores `items[].key`)
    optional: frozenset[str] = frozenset()


_OBSERVATION = frozenset({"id", "source_kind", "source", "source_scope", "external_id", "payload",
                          "source_updated_at", "observed_at"})
#: What an observation payload may carry (the same keys `sync._valid_observation` accepts), plus `ext`.
OBS_PAYLOAD = frozenset({"title", "platform", "platforms", "edition", "release_date", "provider_ids", "facts",
                         "source_updated_at", "available", "ext"})
#: `ext` members this release applies (`release` since 1.3) and the `_requires` token each one ships under.
EXT_APPLY: frozenset[str] = frozenset({"release"})
EXT_TOKENS = {"release": "dates-v1"}
_RELEASE = "observation.payload.ext.release"
#: C5.1 shapes inside `ext.release`; an unknown member parks the entry (`unsupported_field`).
RELEASE_NESTED = {_RELEASE: frozenset({"first", "entries"}),
                  f"{_RELEASE}.first": frozenset({"date", "precision"}),
                  f"{_RELEASE}.entries[]": frozenset({"date", "precision", "platform", "region", "edition",
                                                      "category"})}
RELEASE_VALUES = {f"{_RELEASE}.first.precision": "date_precision",
                  f"{_RELEASE}.entries[].precision": "date_precision",
                  f"{_RELEASE}.entries[].category": "release_category",
                  f"{_RELEASE}.entries[].region": "release_region"}

FEED_KINDS: dict[str, KindSpec] = {spec.name: spec for spec in (
    KindSpec("obs", "1.0", 1, 1, frozenset({"canonical_id", "media_type", "observation"}),
             "A redistributable source observation and the record it belongs to.",
             nested={"observation": _OBSERVATION, "observation.payload": OBS_PAYLOAD, **RELEASE_NESTED},
             values={"observation.source_kind": "source_kind", **RELEASE_VALUES}),
    KindSpec("artwork", "1.0", 1, 1, frozenset({"canonical_id", "source", "items"}),
             "The complete artwork set one source contributes to a record.",
             nested={"items[]": frozenset({"kind", "url", "priority", "width", "height"})},
             values={"items[].kind": "artwork_kind"}),
    KindSpec("achievements", "1.0", 1, 1, frozenset({"canonical_id", "source", "items"}),
             "The complete achievement list one source contributes to a record.",
             nested={"items[]": frozenset({"name", "description", "external_id", "hidden", "icon_url",
                                           "locked_icon_url", "kind", "tier", "points", "global_percent", "key"})},
             values={"items[].kind": "achievement_kind", "items[].tier": "achievement_tier"},
             optional=frozenset({"items[].key"})),
    KindSpec("custom", "1.0", 1, 1, frozenset({"canonical_id", "source", "values", "clear"}),
             "Curated custom-field values set or cleared on a record."),
    KindSpec("schema", "1.0", 1, 1, frozenset({"types", "fields"}),
             "The authority's media types and custom-field definitions.",
             nested={"types[]": frozenset({"key", "label", "enabled", "position", "labels"}),
                     "fields[]": frozenset({"media_type", "key", "label", "kind", "choices", "help", "public",
                                            "editable", "position"})},
             values={"fields[].kind": "field_kind"}),
    KindSpec("merge", "1.0", 1, 1, frozenset({"src", "dst"}), "One record merged into another."),
    KindSpec("detach", "1.0", 1, 1, frozenset({"observation_id", "canonical_id", "new_canonical_id"}),
             "One observation split off onto its own record."),
    KindSpec("lock", "1.0", 1, 1, frozenset({"canonical_id", "field", "value"}), "A pinned field value.",
             values={"field": "lock_field"}),
    KindSpec("unlock", "1.0", 1, 1, frozenset({"canonical_id", "field"}), "A pinned field value removed.",
             values={"field": "lock_field"}),
    KindSpec("key_rotation", "1.0", 1, 1, frozenset({"key_id", "public_key"}),
             "The authority's next signing key, signed by the current one."),
    KindSpec("relation", "1.4", 1, 1, frozenset({"op", "from", "to", "type", "source"}),
             "A relation between two works added or ended (`from` is the DLC, port, member...; `to` the parent).",
             values={"op": "relation_op", "type": "relation_type"}),
    KindSpec("record", "1.5", 1, 1, frozenset({"canonical_id", "media_type", "title", "provider_ids", "because"}),
             "A record introduced by identity only: its id, type, display title and store ids.",
             values={"because": "record_because"}),
)}

#: Kinds reserved by later protocols. A follower of this release parks them; this node never emits them.
#: (`relation` was reserved for 1.4 and shipped in A15.) `price` is reserved so that a price that reaches the main
#: feed by mistake parks instead of being dropped: prices belong to their own channel (C8.3, `PRICE_CHANNEL`).
#: `unmerge` and `distinct` (C11 §11.12, protocol 1.7 after the H52 shift) are reserved by A17: merge records,
#: unmerge and rejections are local in this release; the federated kinds ship with C11-5.
RESERVED_KINDS: dict[str, str] = {"price": "1.6", "unmerge": "1.7", "distinct": "1.7"}


# ---- the price channel (C8.3, protocol 1.6 after the H52 shift; designed, emission disabled) -------------

@dataclass(frozen=True)
class ChannelSpec:
    """A signed channel separate from the main feed: its own endpoint, sequence, signature domain and cursor.

    Unlike the main feed (contiguous, retained forever) a channel is prunable: a follower whose cursor is below
    the oldest retained entry gets `{"reset": true, "snapshot_url": ...}` and re-reads the snapshot."""

    name: str
    since: str  # protocol that ships it
    capability: str  # advertised in /sync/info.capabilities only while CHANNEL_ENABLED
    endpoint: str
    snapshot: str
    sequence: str  # node_state key of the channel's head
    domain: bytes  # signature domain (`signing.PRICE_DOMAIN`)
    cursor_suffix: str  # SyncCursor.upstream = "<node>" + cursor_suffix
    fields: frozenset[str]  # the entry body
    nested: dict[str, frozenset[str]]
    values: dict[str, str]  # path -> closed vocabulary
    summary: str


#: The C8.1 shape of one price channel entry body.
PRICE_FIELDS = frozenset({"id", "canonical_id", "provider", "product_id", "package_id", "edition", "platform",
                          "region", "currency", "amount_minor", "base_amount_minor", "discount_pct", "availability",
                          "observed_at", "valid_until", "source_kind", "provenance", "license", "retention_days"})
#: Whether this release emits the price channel. False in Release 0 (C8.3): no capability, endpoint 404, nothing
#: signed. It is a constant, not a setting: turning it on needs the release that implements the channel.
CHANNEL_ENABLED = False

PRICE_CHANNEL = ChannelSpec(
    name="price", since="1.6", capability="price-channel-v1", endpoint="/api/v2/sync/price-changes",
    snapshot="/api/v2/sync/price-snapshot", sequence="price_feed_seq", domain=b"atlas-price-v1\n",
    cursor_suffix="#price", fields=PRICE_FIELDS,
    nested={"provenance": frozenset({"node", "method", "fetch_id"}),
            "license": frozenset({"redistributable", "terms"})},
    values={"availability": "price_availability", "source_kind": "price_source_kind"},
    summary="Redistributable price observations (C8): latest per offer, prunable, never in the main feed.")


def capabilities() -> list[str]:
    """`/sync/info.capabilities`: the main feed's capabilities, plus a channel's only while it is enabled."""
    out = list(CAPABILITIES)
    if CHANNEL_ENABLED:  # read at call time
        out.append(PRICE_CHANNEL.capability)
    return out


@cache
def vocabularies() -> dict[str, Any]:
    """Closed vocabularies whose unknown values park an entry (`unsupported_value`). Imported lazily:
    the modules that own them append to the feed themselves."""
    from . import custom, dates, facets
    from .merge import LOCK_FIELDS

    return {
        "source_kind": frozenset({"host", "plugin", "manual", "metadata"}),
        "artwork_kind": frozenset(facets.KINDS),
        "achievement_kind": frozenset(vocab.ACHIEVEMENT_KINDS),
        "achievement_tier": frozenset(vocab.ACHIEVEMENT_TIERS),
        "field_kind": frozenset(custom.KINDS),
        "lock_field": frozenset(LOCK_FIELDS),
        "date_precision": frozenset(vocab.DATE_PRECISIONS),
        "release_category": frozenset(vocab.RELEASE_CATEGORIES),
        "release_region": OpenVocabulary(dates.REGION_RE),
        "relation_type": frozenset(vocab.RELATION_TYPES),
        "relation_op": frozenset(vocab.RELATION_OPS),
        "price_availability": frozenset(vocab.PRICE_AVAILABILITY),
        "price_source_kind": frozenset(vocab.PRICE_SOURCE_KINDS),
        "record_because": frozenset(vocab.RECORD_BECAUSE),
    }


class OpenVocabulary:
    """A vocabulary defined by a pattern (C5.1 regions: the groups or any ISO 3166-1 alpha-2 code). A value
    outside it parks the entry like an unknown closed value: a newer release may have added a region group."""

    def __init__(self, pattern: re.Pattern[str]):
        self.pattern = pattern

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and self.pattern.fullmatch(value) is not None

    def __iter__(self):
        """One member, the pattern itself, so the startup-replay marker (`sync.replay_marker`) changes when a
        release changes the pattern."""
        return iter((f"/{self.pattern.pattern}/",))


@cache
def facts_fields() -> frozenset[str]:
    from .contract import ObservedFacts

    return frozenset(ObservedFacts.model_fields)


def kinds_advertised() -> dict[str, dict[str, int]]:
    """The `/sync/info.kinds` map: per kind, the highest body version emitted and applied."""
    return {name: {"emit": spec.emit, "apply": spec.apply} for name, spec in FEED_KINDS.items()}


def known_kinds() -> frozenset[str]:
    return frozenset(FEED_KINDS)


def known_tokens() -> frozenset[str]:
    """Every `_requires` token this release can apply: its capabilities and its vocabulary tokens.
    Read at call time, so a test can play an older release by patching either tuple."""
    return frozenset(CAPABILITIES) | frozenset(VOCAB_TOKENS)


# ---- classification (follower side, C1.4) ---------------------------------------------------------------

APPLY, PARK, QUARANTINE = "apply", "parked", "quarantined"
PARK_REASONS = ("unsupported_kind", "unsupported_version", "unsupported_capability", "unsupported_field",
                "unsupported_value", "dropped_by_previous_release")
QUARANTINE_REASONS = ("invalid", "apply_error", "skipped")


@dataclass(frozen=True)
class Outcome:
    action: str  # apply | parked | quarantined
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None  # for "apply": the body to apply, ignorable extensions removed

    @property
    def message(self) -> str:
        parts = [self.reason.replace("_", " ")]
        if self.detail.get("kind"):
            parts.append(f"kind {self.detail['kind']!r}")
        if "_v" in self.detail:
            parts.append(f"body version {self.detail['_v']}")
        if self.detail.get("unknown"):
            parts.append("unknown: " + ", ".join(map(str, self.detail["unknown"]))[:400])
        if self.detail.get("requires"):
            parts.append("requires: " + ", ".join(self.detail["requires"]))
        return "; ".join(parts)


def _park(reason: str, kind: str, **detail: Any) -> Outcome:
    return Outcome(PARK, reason, {"kind": kind[:64], **detail})


def _invalid(kind: str, why: str) -> Outcome:
    return Outcome(QUARANTINE, "invalid", {"kind": kind[:64], "unknown": [why]})


def _members(value: Any, path: str) -> list[tuple[str, dict[str, Any]]]:
    """Objects at `path` ("items[]", "observation.payload") with their concrete paths. Values of the
    wrong type are skipped here; validation during apply reports them."""
    found: list[tuple[str, Any]] = [("", value)]
    for part in path.split("."):
        many = part.endswith("[]")
        name = part[:-2] if many else part
        step = []
        for where, obj in found:
            child = obj.get(name) if isinstance(obj, dict) else None
            here = f"{where}.{name}" if where else name
            if many:
                step += [(f"{here}[{i}]", item) for i, item in enumerate(child)] if isinstance(child, list) else []
            elif child is not None:
                step.append((here, child))
        found = step
    return [(where, obj) for where, obj in found if isinstance(obj, dict)]


def _requires(body: dict[str, Any]) -> list[str] | None:
    raw = body.get("_requires", [])
    if not isinstance(raw, list) or len(raw) > MAX_REQUIRES:
        return None
    if not all(isinstance(t, str) and 0 < len(t) <= MAX_TOKEN for t in raw):
        return None
    return raw


def classify(kind: str, body: Any) -> Outcome:
    """Decide what a follower of this release may do with one verified entry.

    `apply` carries the body to apply. `parked` blocks the applied head (the entry needs a newer
    release). `quarantined` (`invalid`) is a known construct that fails validation; it does not block."""
    spec = FEED_KINDS.get(kind)
    if spec is None:
        return _park("unsupported_kind", kind)
    if not isinstance(body, dict):
        return _invalid(kind, "body is not an object")
    # Envelope shapes this release does not know (a string or float `_v`, a `_requires` that is not a
    # short list) may be how a newer release encodes something: park (fail closed), never skip past it.
    version = body.get("_v", 1)
    if type(version) is not int or version < 1:
        return _park("unsupported_version", kind, _v=str(version)[:20],
                     unknown=["_v is not a positive integer"])
    if version > spec.apply:
        return _park("unsupported_version", kind, _v=version)
    requires = _requires(body)
    if requires is None:
        return _park("unsupported_capability", kind,
                     unknown=[f"_requires is not a list of at most {MAX_REQUIRES} short tokens"])
    tokens = known_tokens()
    unknown_tokens = [t for t in requires if t not in tokens]
    if unknown_tokens:
        return _park("unsupported_capability", kind, requires=unknown_tokens)

    unknown: list[str] = sorted(k for k in body if k not in spec.fields and k not in ENVELOPE)
    strip: list[tuple[dict[str, Any], str]] = []
    for path, allowed in spec.nested.items():
        for where, obj in _members(body, path):
            extra = sorted(k for k in obj if k not in allowed)
            unknown += [f"{where}.{k}" for k in extra]
    if kind == "obs":
        for where, payload in _members(body, "observation.payload"):
            for where_f, facts in _members(payload, "facts"):
                unknown += [f"{where}.{where_f}.{k}" for k in sorted(facts) if k not in facts_fields()]
            ext = payload.get("ext")
            if ext is None:
                continue
            if not isinstance(ext, dict):  # an extension shape this release does not know: fail closed
                return _park("unsupported_field", kind, unknown=[f"{where}.ext (not an object)"])
            for member in sorted(ext):
                if member in EXT_APPLY:
                    continue
                if member in requires or EXT_TOKENS.get(member) in requires:
                    unknown.append(f"{where}.ext.{member}")  # the authority says it must be understood
                else:
                    strip.append((payload, member))  # an optional extension this release may ignore
    if unknown:
        return _park("unsupported_field", kind, unknown=unknown[:50])

    vocab = vocabularies()
    unknown_values = []
    for path, name in spec.values.items():
        parent, _, leaf = path.rpartition(".")
        for where, obj in _members(body, parent) if parent else [("", body)]:
            value = obj.get(leaf)
            if value in (None, ""):
                continue
            if not isinstance(value, str):  # a vocabulary encoded differently by a newer release: fail closed
                unknown_values.append(f"{where + '.' if where else ''}{leaf} (not text)")
                continue
            if value not in vocab[name]:
                unknown_values.append(f"{where + '.' if where else ''}{leaf}={value[:40]}")
    if unknown_values:
        return _park("unsupported_value", kind, unknown=unknown_values[:50])

    if not strip and not any(k in body for k in ENVELOPE):
        return Outcome(APPLY, body=body)
    prepared = copy.deepcopy(body)
    for key in ENVELOPE:
        prepared.pop(key, None)
    if strip:
        payload = prepared["observation"]["payload"]
        for _, member in strip:
            payload["ext"].pop(member, None)
        if not payload["ext"]:
            payload.pop("ext")
    return Outcome(APPLY, body=prepared)


# ---- emission gate (authority side, C1.6 / H13) ---------------------------------------------------------

class EmitGateError(ValueError):
    """The entry needs a newer protocol than this node's `emit_protocol` allows."""

    def __init__(self, feature: str, needed: str, current: str):
        self.feature, self.needed, self.current = feature, needed, current
        super().__init__(f"{feature} needs federation protocol {needed}; this node emits {current}. "
                         f"Raise federation emit protocol to {needed} first (Admin → Peers).")


def emit_protocol(settings: Any) -> str:
    """The effective emit protocol: the configured value, never below `min_follower_protocol` nor above
    what this release speaks. Unset means `min_follower_protocol` (C1.6)."""
    low = getattr(settings, "min_follower_protocol", DEFAULT_MIN_FOLLOWER_PROTOCOL) or DEFAULT_MIN_FOLLOWER_PROTOCOL
    chosen = getattr(settings, "emit_protocol", None) or low
    try:
        value = max(parse_protocol(chosen), parse_protocol(low))
    except ValueError:
        value = parse_protocol(DEFAULT_MIN_FOLLOWER_PROTOCOL)
    return fmt(min(value, parse_protocol(PROTOCOL)))


def check_emit(kind: str, body: dict[str, Any], current: str) -> dict[str, Any]:
    """Return the body to sign, or raise EmitGateError. Fail closed: an unknown kind is never emitted.

    Features that need a newer protocol than `current`:
    * a reserved kind, or a kind newer than `current` (`relation`, 1.4): refused; callers keep it local and
      append nothing;
    * a `_requires` token reserved for a later minor (`tier-platinum`, 1.2): refused;
    * a vocabulary value a later minor added (`tier: "platinum"`, 1.2): refused with the operator-facing
      message. At or above that protocol the value is signed and its token (`tier-platinum`) is added to
      `_requires` (C1.4), so older followers park the entry with an explicit reason;
    * `observation.payload.ext.release` (1.3): removed from the signed copy below 1.3 (stored locally in
      full); at or above 1.3 signed with the `_requires` token `dates-v1`, so 1.1/1.2 followers park it;
    * a `releases` lock or unlock, or a partial `release_date` lock value (1.3): refused below 1.3 (callers
      check first and answer 409), signed with `dates-v1` at or above it.

    Bodies with none of these are returned unchanged, so version-1 bodies are signed exactly as before."""
    have = parse_protocol(current)
    if kind in RESERVED_KINDS:
        raise EmitGateError(f"feed kind {kind!r}", RESERVED_KINDS[kind], current)
    spec = FEED_KINDS.get(kind)
    if spec is None:
        raise ValueError(f"refusing to emit unregistered feed kind {kind!r}; add it to atlas/feed_schema.py")
    since = KIND_SINCE.get(kind, spec.since)
    if since and parse_protocol(since) > have:
        raise EmitGateError(f"feed kind {kind!r}", since, current)
    version = body.get("_v", 1)
    if type(version) is not int or version > spec.emit:
        # A body version this release does not write (lock v2, achievements v2, ...): never signed by it.
        needed = BODY_SINCE.get((kind, version), PROTOCOL) if type(version) is int else PROTOCOL
        raise EmitGateError(f"{kind} body version {str(version)[:8]}", needed, current)
    if kind in ("lock", "unlock") and body.get("field") == NO_MERGE_FIELD:
        # C11 §11.12: the merge guard is a lock v2 construct. A v1 body naming it would park every follower
        # before C11 on an unknown `lock_field` value, so it is refused at every protocol.
        raise EmitGateError("the merge guard (_no_merge) as a version 1 lock", VALUE_SINCE[("lock_field", NO_MERGE_FIELD)],
                            current)
    known = known_tokens()
    for token in body.get("_requires", []) or []:
        needed = TOKEN_SINCE.get(token) or RESERVED_TOKENS.get(token)
        if needed and parse_protocol(needed) > have:
            raise EmitGateError(f"the {token!r} feature", needed, current)
        if needed and token not in known:  # reserved and not applied by this release: never signed by it
            raise EmitGateError(f"the {token!r} feature", needed, current)
    needed_tokens: list[str] = []
    for path, name in spec.values.items():
        parent, _, leaf = path.rpartition(".")
        for _where, obj in _members(body, parent) if parent else [("", body)]:
            value = obj.get(leaf)
            if not isinstance(value, str):
                continue
            needed = vocab.VALUE_PROTOCOL.get((name, value)) or VALUE_SINCE.get((name, value))
            if needed is None:
                continue
            if parse_protocol(needed) > have:
                raise EmitGateError(VALUE_FEATURE.get((name, value), f"the {name} value {value!r}"), needed, current)
            token = vocab.VALUE_TOKEN.get((name, value))
            if token and token not in needed_tokens:
                needed_tokens.append(token)
    dates_needed = parse_protocol(RESERVED_TOKENS["dates-v1"])
    if kind == "obs":
        payload = (body.get("observation") or {}).get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("ext"), dict) and "release" in payload["ext"]:
            if dates_needed > have:
                body = copy.deepcopy(body)
                ext = body["observation"]["payload"]["ext"]
                ext.pop("release")
                if not ext:
                    body["observation"]["payload"].pop("ext")
            else:
                needed_tokens.append("dates-v1")
    if kind in ("lock", "unlock") and _dates_lock(body):
        if dates_needed > have:
            raise EmitGateError("a partial or regional release date lock", RESERVED_TOKENS["dates-v1"], current)
        needed_tokens.append("dates-v1")
    present = body.get("_requires") if isinstance(body.get("_requires"), list) else []
    missing = [t for t in dict.fromkeys(needed_tokens) if t not in present]
    if missing:
        body = {**body, "_requires": [*present, *missing]}  # a new top-level dict: the caller's copy is untouched
    return body


def _dates_lock(body: dict[str, Any]) -> bool:
    """A lock (or unlock) only a 1.3 follower reads correctly: the `releases` field, or a `release_date`
    value that is not a full day ("2011" would become a day-only `release_date` on a 1.2 follower)."""
    field_name, value = body.get("field"), body.get("value")
    if field_name == "releases":
        return True
    return field_name == "release_date" and isinstance(value, str) and len(value) != 10


# ---- documentation --------------------------------------------------------------------------------------

def markdown_table() -> str:
    """The kinds table in docs/guide/federation.md (tests/test_sync_compat.py checks the guide contains it)."""
    rows = ["| Kind | Since | Emits / applies body version | Fields |", "| --- | --- | --- | --- |"]
    for spec in FEED_KINDS.values():
        fields = ", ".join(f"`{f}`" for f in sorted(spec.fields))
        rows.append(f"| `{spec.name}` | {spec.since} | {spec.emit} / {spec.apply} | {fields} |")
    for name, since in RESERVED_KINDS.items():
        rows.append(f"| `{name}` | {since} (reserved) | not in this release | parked by this release |")
    return "\n".join(rows)
