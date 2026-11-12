"""The federation feed, described once: wire version, feed kinds, their fields and vocabularies.

Every place that needs to know what a feed entry may contain reads this module: the follower's
classification (`classify`), the authority's signing check (`check_emit`), `/sync/info`
(`kinds_advertised`), recovery (`promotion`) and the guide. See docs/guide/federation.md
"Compatibility and upgrades", CONTRACT-C16 §22 and the C16 version-negotiation design §4.1.

Wire 1 (C16 Z1) collapses the v0 protocol steps 1.1-1.5 and every per-feature emission gate into one version:

* There is no emit protocol. A node signs every construct its release ships; which version a peer reads is
  negotiated per request (`atlas.wire`). `SINCE` records the wire version that ships each construct: every
  construct shipped so far, including those v0 releases introduced at protocols 1.2-1.5 and those C15 registered
  under its unified version, is `1`.
* Every entry a wire build signs carries `_wire` (the lowest wire version that reads it) in its envelope.
* Readers are tolerant (W-2): an unknown member of a known body is ignored (`ignored_member` on the apply outcome;
  the signed bytes are retained and re-classified after an upgrade), not parked. Must-understand constructs park an
  older reader instead: a new kind, a higher body version, a new closed-vocabulary value, a new `_requires` token,
  and *critical* members (`CRITICAL_EXT`), whose token `check_emit` always adds to `_requires`.
* Constructs no release applies yet stay in `RESERVED_*`: a reader parks them and `check_emit` refuses them
  (`ReservedConstructError`). The release that ships one moves it into `SINCE` at its own wire version.
* The kind `wire_start` is the first entry a wire authority signs (`feed.ensure_wire_start`): its seq is the
  point of no return between v0 history and wire entries.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from . import vocab
from .wire import WIRE_MAX

# ---- versions -------------------------------------------------------------------------------------------

#: What v0 releases before A1 speak (they advertise no protocol). Read only on the legacy (v0 upstream) path.
LEGACY_PROTOCOL = "1.0"
#: Version of the observation payload schema (`SourceObservationV1`).
OBSERVATION_SCHEMA = 1

#: Named optional features this node advertises and understands in `_requires`.
CAPABILITIES: tuple[str, ...] = ("ext-v1", "dates-v1", "book-v1")
#: Vocabulary tokens (`_requires`) this release understands, one per value added after the first release, such
#: as `tier-platinum`. They are not optional features, so `/sync/info.capabilities` does not list them.
VOCAB_TOKENS: tuple[str, ...] = tuple(sorted(set(vocab.VALUE_TOKEN.values())))

#: C15 §2.3: the book lock fields (`merge.BOOK_LOCKS` is this tuple; `merge` imports this module lazily).
BOOK_LOCK_FIELDS = ("authors", "subtitle", "page_count", "original_language")
#: Design §4.1: the wire version that ships each construct, keyed ("kind", name), ("token", name),
#: ("value", (vocabulary, value)), ("body", (kind, version)) or ("member", (kind, path)). All 1 in wire 1.
SINCE: dict[tuple[str, Any], int] = {
    ("kind", "relation"): 1, ("kind", "record"): 1, ("kind", "wire_start"): 1,
    ("token", "ext-v1"): 1, ("token", "tier-platinum"): 1, ("token", "dates-v1"): 1, ("token", "book-v1"): 1,
    ("value", ("achievement_tier", "platinum")): 1,
    **{("value", ("lock_field", f)): 1 for f in BOOK_LOCK_FIELDS},
    ("member", ("schema", "types[].achievements")): 1, ("member", ("relation", "position")): 1,
}
#: Reserved constructs: designed, applied by no release yet. A reader parks them; `check_emit` refuses them.
#: `price` belongs to its own channel (C8.3), so a price in the main feed parks instead of being dropped; `unmerge`,
#: `distinct`, lock/unlock body 2 (`field-clear-v1`, `merge-guard-v1`), achievements body 2 (`ach-v2`) and artwork
#: body 2 (`art-v2`, artwork kind `trailer`) are the C11/C12 reservations.
RESERVED_KINDS: frozenset[str] = frozenset({"price", "unmerge", "distinct"})
RESERVED_TOKENS: frozenset[str] = frozenset({"relation-v1", "price-channel-v1", "field-clear-v1", "merge-guard-v1",
                                             "ach-v2", "art-v2"})
RESERVED_VALUES: frozenset[tuple[str, str]] = frozenset({("lock_field", "_no_merge"), ("artwork_kind", "trailer")})
RESERVED_BODIES: frozenset[tuple[str, int]] = frozenset({("lock", 2), ("unlock", 2), ("achievements", 2),
                                                         ("artwork", 2)})
#: C13 §3.2: the reserved namespaces a `record` (or a v0 bridge observation) never carries in `provider_ids`: an
#: Atlas id or a curated namespace there could claim a follower's own record.
RECORD_SOURCE = "atlas-record"
RECORD_RESERVED_NAMESPACES = frozenset({"atlas", RECORD_SOURCE, "curator", "manual", "community"})
RECORD_TITLE_MAX = 300
#: The lock "field" of the C11 merge guard. Only ever a lock body version 2 (`merge-guard-v1`, reserved).
NO_MERGE_FIELD = "_no_merge"

_PROTOCOL = re.compile(r"(\d{1,4})\.(\d{1,4})\Z")
#: Envelope members: never applied, removed before a body is applied.
ENVELOPE = ("_v", "_requires", "_wire")
MAX_REQUIRES, MAX_TOKEN = 16, 40


def parse_protocol(value: Any) -> tuple[int, int]:
    """'1.3' -> (1, 3): a v0 node's protocol string (legacy reader only). Anything else raises ValueError."""
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


# ---- feed kinds -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class KindSpec:
    """One feed kind: what this release emits and applies, and the exact fields it understands."""

    name: str
    since: int  # wire version that ships the kind
    emit: int  # highest body version this release writes
    apply: int  # highest body version this release applies
    fields: frozenset[str]  # top-level body fields (envelope keys excluded)
    summary: str
    # path -> allowed member names, for nested objects or lists of objects ("items[]", "observation")
    nested: dict[str, frozenset[str]] = field(default_factory=dict)
    # path -> name of the closed vocabulary its values must belong to
    values: dict[str, str] = field(default_factory=dict)
    # reserved-optional fields (v0 history: a 1.1 follower accepted them; this release stores `items[].key`)
    optional: frozenset[str] = frozenset()


_OBSERVATION = frozenset({"id", "source_kind", "source", "source_scope", "external_id", "payload",
                          "source_updated_at", "observed_at"})
#: What an observation payload may carry (the same keys `sync._valid_observation` accepts), plus `ext`.
OBS_PAYLOAD = frozenset({"title", "platform", "platforms", "edition", "release_date", "provider_ids", "facts",
                         "source_updated_at", "available", "ext"})
#: `ext` members this release applies and the `_requires` token each one ships under.
EXT_APPLY: frozenset[str] = frozenset({"release", "book"})
EXT_TOKENS = {"release": "dates-v1", "book": "book-v1"}
#: Design §4.1 critical-member rule: `ext` members whose being ignored would change what a record is (a book filed as
#: a bare record with no authors or editions; a partial release date read as a full day). `check_emit` always adds
#: their token to `_requires`, so a reader that cannot apply them parks the entry instead of stripping the member.
CRITICAL_EXT: frozenset[str] = frozenset({"release", "book"})
_RELEASE = "observation.payload.ext.release"
#: C5.1 shapes inside `ext.release`; an unknown member is ignored (`ignored_member`).
RELEASE_NESTED = {_RELEASE: frozenset({"first", "entries"}),
                  f"{_RELEASE}.first": frozenset({"date", "precision"}),
                  f"{_RELEASE}.entries[]": frozenset({"date", "precision", "platform", "region", "edition",
                                                      "category"})}
RELEASE_VALUES = {f"{_RELEASE}.first.precision": "date_precision",
                  f"{_RELEASE}.entries[].precision": "date_precision",
                  f"{_RELEASE}.entries[].category": "release_category",
                  f"{_RELEASE}.entries[].region": "release_region"}
_BOOK = "observation.payload.ext.book"
#: C15 §2.3 shapes inside `ext.book` (`contract.BookInfoV1`); an unknown member is ignored (`ignored_member`).
#: Open maps (`authors[].ids`, `editions[].provider_ids`) are validated by the contract, not listed here.
BOOK_NESTED = {_BOOK: frozenset({"authors", "subtitle", "original_language", "page_count", "series", "editions"}),
               f"{_BOOK}.authors[]": frozenset({"name", "role", "ids"}),
               f"{_BOOK}.series": frozenset({"name", "position"}),
               f"{_BOOK}.editions[]": frozenset({"key", "name", "isbn_13", "isbn_10", "format", "publisher",
                                                 "published", "pages", "language", "provider_ids"}),
               f"{_BOOK}.editions[].published": frozenset({"date", "precision"})}
BOOK_VALUES = {f"{_BOOK}.authors[].role": "book_creator_role",
               f"{_BOOK}.editions[].format": "book_format",
               f"{_BOOK}.editions[].published.precision": "date_precision"}

FEED_KINDS: dict[str, KindSpec] = {spec.name: spec for spec in (
    KindSpec("obs", 1, 1, 1, frozenset({"canonical_id", "media_type", "observation"}),
             "A redistributable source observation and the record it belongs to.",
             nested={"observation": _OBSERVATION, "observation.payload": OBS_PAYLOAD, **RELEASE_NESTED,
                     **BOOK_NESTED},
             values={"observation.source_kind": "source_kind", **RELEASE_VALUES, **BOOK_VALUES}),
    KindSpec("artwork", 1, 1, 1, frozenset({"canonical_id", "source", "items"}),
             "The complete artwork set one source contributes to a record.",
             nested={"items[]": frozenset({"kind", "url", "priority", "width", "height"})},
             values={"items[].kind": "artwork_kind"}),
    KindSpec("achievements", 1, 1, 1, frozenset({"canonical_id", "source", "items"}),
             "The complete achievement list one source contributes to a record.",
             nested={"items[]": frozenset({"name", "description", "external_id", "hidden", "icon_url",
                                           "locked_icon_url", "kind", "tier", "points", "global_percent", "key"})},
             values={"items[].kind": "achievement_kind", "items[].tier": "achievement_tier"},
             optional=frozenset({"items[].key"})),
    KindSpec("custom", 1, 1, 1, frozenset({"canonical_id", "source", "values", "clear"}),
             "Curated custom-field values set or cleared on a record."),
    KindSpec("schema", 1, 1, 1, frozenset({"types", "fields"}),
             "The authority's media types and custom-field definitions.",
             nested={"types[]": frozenset({"key", "label", "enabled", "position", "labels", "achievements"}),
                     "fields[]": frozenset({"media_type", "key", "label", "kind", "choices", "help", "public",
                                            "editable", "position"})},
             values={"fields[].kind": "field_kind"}),
    KindSpec("merge", 1, 1, 1, frozenset({"src", "dst"}), "One record merged into another."),
    KindSpec("detach", 1, 1, 1, frozenset({"observation_id", "canonical_id", "new_canonical_id"}),
             "One observation split off onto its own record."),
    KindSpec("lock", 1, 1, 1, frozenset({"canonical_id", "field", "value"}), "A pinned field value.",
             values={"field": "lock_field"}),
    KindSpec("unlock", 1, 1, 1, frozenset({"canonical_id", "field"}), "A pinned field value removed.",
             values={"field": "lock_field"}),
    KindSpec("key_rotation", 1, 1, 1, frozenset({"key_id", "public_key"}),
             "The authority's next signing key, signed by the current one."),
    KindSpec("relation", 1, 1, 1, frozenset({"op", "from", "to", "type", "source", "position"}),
             "A relation between two works added or ended (`from` is the DLC, port, member...; `to` the parent). "
             "`position` (C15 §2.4, only on `series_member`): the member's place in the series.",
             values={"op": "relation_op", "type": "relation_type"}),
    KindSpec("record", 1, 1, 1, frozenset({"canonical_id", "media_type", "title", "provider_ids", "because"}),
             "A record introduced by identity only: its id, type, display title and store ids.",
             values={"because": "record_because"}),
    KindSpec("wire_start", 1, 1, 1, frozenset({"first_seq", "wire"}),
             "The first entry a wire authority signs: the boundary between v0 history and wire entries.",
             nested={"wire": frozenset({"min", "max"})}),
)}


# ---- the price channel (C8.3; designed, emission disabled) ------------------------------------------------

@dataclass(frozen=True)
class ChannelSpec:
    """A signed channel separate from the main feed: its own endpoint, sequence, signature domain and cursor.

    Unlike the main feed (contiguous, retained forever) a channel is prunable: a follower whose cursor is below
    the oldest retained entry gets `{"reset": true, "snapshot_url": ...}` and re-reads the snapshot."""

    name: str
    since: int | None  # wire version that ships it (None: reserved, no release ships it yet)
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
#: Whether this release emits the price channel. False (C8.3): no capability, endpoint 404, nothing signed. It is a
#: constant, not a setting: turning it on needs the release that implements the channel.
CHANNEL_ENABLED = False

PRICE_CHANNEL = ChannelSpec(
    name="price", since=None, capability="price-channel-v1", endpoint="/api/v2/sync/price-changes",
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
        "book_format": frozenset(vocab.BOOK_FORMATS),
        "book_creator_role": frozenset(vocab.BOOK_CREATOR_ROLES),
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


# ---- classification (follower side) -----------------------------------------------------------------------

APPLY, PARK, QUARANTINE = "apply", "parked", "quarantined"
PARK_REASONS = ("unsupported_kind", "unsupported_version", "unsupported_capability", "unsupported_field",
                "unsupported_value", "dropped_by_previous_release")
#: The note on an `apply` outcome whose body had members this release does not know (design §4.1, W-2): they were
#: left out of the applied body; the signed bytes are retained and re-classified after an upgrade.
IGNORED_MEMBER = "ignored_member"
#: `media_type_not_adopted` (CONTRACT-C15 §2.2, A20): an entry names a media type this node does not have and will not
#: get from that feed (`inherit_schema` is off, or the feed's schema could not create it). Retried with backoff like
#: every quarantined row, so it applies once an administrator creates the type locally.
QUARANTINE_REASONS = ("invalid", "apply_error", "skipped", "media_type_not_adopted")


@dataclass(frozen=True)
class Outcome:
    action: str  # apply | parked | quarantined
    reason: str = ""  # for "apply": "" or `ignored_member`
    detail: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None  # for "apply": the body to apply, envelope and ignored members removed

    @property
    def message(self) -> str:
        parts = [self.reason.replace("_", " ")]
        if self.detail.get("kind"):
            parts.append(f"kind {self.detail['kind']!r}")
        if "_v" in self.detail:
            parts.append(f"body version {self.detail['_v']}")
        if "_wire" in self.detail:
            parts.append(f"wire {self.detail['_wire']}")
        if self.detail.get("unknown"):
            parts.append("unknown: " + ", ".join(map(str, self.detail["unknown"]))[:400])
        if self.detail.get("ignored"):
            parts.append("ignored: " + ", ".join(map(str, self.detail["ignored"]))[:400])
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


def classify(kind: str, body: Any, *, wire_max: int = WIRE_MAX) -> Outcome:
    """Decide what a follower of this release may do with one verified canonical entry.

    `apply` carries the body to apply (envelope removed; with reason `ignored_member` when unknown members of the
    known body were left out). `parked` blocks the applied head: the entry needs a newer release (a must-understand
    construct, or a `_wire` above `wire_max` that no rendition covered). `quarantined` (`invalid`) is a known
    construct that fails validation; it does not block."""
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
    if "_wire" in body:
        wire = body["_wire"]
        if type(wire) is not int or wire < 1:
            return _park("unsupported_version", kind, _wire=str(wire)[:20],
                         unknown=["_wire is not a positive integer"])
        if wire > wire_max:  # readable only through a rendition this build was not given (design §4.4-§4.5)
            return _park("unsupported_version", kind, _wire=wire)
    requires = _requires(body)
    if requires is None:
        return _park("unsupported_capability", kind,
                     unknown=[f"_requires is not a list of at most {MAX_REQUIRES} short tokens"])
    tokens = known_tokens()
    unknown_tokens = [t for t in requires if t not in tokens]
    if unknown_tokens:
        return _park("unsupported_capability", kind, requires=unknown_tokens)

    # Tolerant reader (W-2): unknown members of a known body are ignored, never parked. A must-understand member
    # reaches a reader only together with its `_requires` token, which parked above if this release lacks it.
    ignored: list[str] = []
    strip: list[tuple[str, str]] = []  # (path of the containing object, member)
    for k in sorted(body):
        if k not in spec.fields and k not in ENVELOPE:
            ignored.append(k)
            strip.append(("", k))
    for path, allowed in spec.nested.items():
        for where, obj in _members(body, path):
            for k in sorted(obj):
                if k not in allowed:
                    ignored.append(f"{where}.{k}")
                    strip.append((path, k))
    if kind == "obs":
        for where, payload in _members(body, "observation.payload"):
            for where_f, facts in _members(payload, "facts"):
                for k in sorted(facts):
                    if k not in facts_fields():
                        ignored.append(f"{where}.{where_f}.{k}")
                        strip.append(("observation.payload.facts", k))
            ext = payload.get("ext")
            if ext is None:
                continue
            if not isinstance(ext, dict):  # an extension shape this release does not know: fail closed
                return _park("unsupported_field", kind, unknown=[f"{where}.ext (not an object)"])
            must = []
            for member in sorted(ext):
                if member in EXT_APPLY:
                    continue
                if member in requires or EXT_TOKENS.get(member) in requires:
                    must.append(f"{where}.ext.{member}")  # the authority says it must be understood
                else:
                    ignored.append(f"{where}.ext.{member}")  # an optional extension this release may ignore
                    strip.append(("observation.payload.ext", member))
            if must:
                return _park("unsupported_field", kind, unknown=must[:50])

    vocabs = vocabularies()
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
            if value not in vocabs[name]:
                unknown_values.append(f"{where + '.' if where else ''}{leaf}={value[:40]}")
    if unknown_values:
        return _park("unsupported_value", kind, unknown=unknown_values[:50])

    if not strip and not any(k in body for k in ENVELOPE):
        return Outcome(APPLY, body=body)
    prepared = copy.deepcopy(body) if strip else dict(body)
    for key in ENVELOPE:
        prepared.pop(key, None)
    for path, member in strip:
        for _, obj in _members(prepared, path) if path else [("", prepared)]:
            obj.pop(member, None)
    for _, payload in _members(prepared, "observation.payload"):
        if isinstance(payload.get("ext"), dict) and not payload["ext"]:
            payload.pop("ext")
    if ignored:
        return Outcome(APPLY, IGNORED_MEMBER, {"kind": kind[:64], "ignored": ignored[:50]}, body=prepared)
    return Outcome(APPLY, body=prepared)


# ---- signing check (authority side, design §4.1) ------------------------------------------------------------

class FeedPolicyError(ValueError):
    """An entry this node does not sign. Subclasses keep their own classes on purpose (C16 §4.1), so callers tell
    them apart: `ReservedConstructError` (a construct no release applies yet), `scope.ScopeCeiling` (C14 §5.4) and
    `feed.UnintroducedCanonical` (C13 §4.1)."""

    code = "feed_policy"

    def __init__(self, feature: str, message: str | None = None):
        self.feature = feature
        super().__init__(message or f"{feature} is not signed into the feed by this release")


class ReservedConstructError(FeedPolicyError):
    """A reserved construct (`RESERVED_*`): no Atlas release applies it yet, so this node never signs it. Answered
    as 409 `reserved_feature`."""

    code = "reserved_feature"

    def __init__(self, feature: str):
        super().__init__(feature, f"{feature} is reserved for a later Atlas release; this release never signs it "
                                  "into the feed.")


def wire_of(kind: str, body: dict[str, Any]) -> int:
    """The `_wire` of a body: the highest `SINCE` among its must-understand constructs (1 for everything in wire 1)."""
    found = [SINCE.get(("kind", kind), 1)]
    found += [SINCE.get(("token", t), 1) for t in body.get("_requires", []) or [] if isinstance(t, str)]
    version = body.get("_v", 1)
    if type(version) is int and version > 1:
        found.append(SINCE.get(("body", (kind, version)), WIRE_MAX))
    return max(found)


def check_emit(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """Return the body to sign, or raise. Fail closed: an unregistered kind is never signed (ValueError), a reserved
    construct never either (`ReservedConstructError`).

    The returned body is a new top-level dict that carries `_wire` (every wire-signed entry does, design §4.1 CR-5)
    and the `_requires` tokens its must-understand constructs need: a vocabulary value's token (`tier-platinum`), a
    critical `ext` member's (`dates-v1`, `book-v1`) and a partial or regional release date lock's (`dates-v1`). The
    caller's body and the locally stored state are untouched."""
    if kind in RESERVED_KINDS:
        raise ReservedConstructError(f"the feed kind {kind!r}")
    spec = FEED_KINDS.get(kind)
    if spec is None:
        raise ValueError(f"refusing to emit unregistered feed kind {kind!r}; add it to atlas/feed_schema.py")
    version = body.get("_v", 1)
    if type(version) is not int or version > spec.emit:
        if type(version) is int and (kind, version) in RESERVED_BODIES:
            raise ReservedConstructError(f"{kind} body version {version}")
        raise ValueError(f"refusing to emit {kind} body version {str(version)[:8]}: this release writes {spec.emit}")
    if kind in ("lock", "unlock") and body.get("field") == NO_MERGE_FIELD:
        # C11 §11.12: the merge guard is a lock v2 construct (reserved); a v1 body naming it would park every reader.
        raise ReservedConstructError("the merge guard (_no_merge)")
    known = known_tokens()
    for token in body.get("_requires", []) or []:
        if token in RESERVED_TOKENS or token not in known:
            raise ReservedConstructError(f"the {str(token)[:40]!r} feature")
    needed_tokens: list[str] = []
    for path, name in spec.values.items():
        parent, _, leaf = path.rpartition(".")
        for _where, obj in _members(body, parent) if parent else [("", body)]:
            value = obj.get(leaf)
            if not isinstance(value, str):
                continue
            if (name, value) in RESERVED_VALUES:
                raise ReservedConstructError(f"the {name} value {value!r}")
            token = vocab.VALUE_TOKEN.get((name, value))
            if token and token not in needed_tokens:
                needed_tokens.append(token)
    if kind == "obs":
        payload = (body.get("observation") or {}).get("payload")
        ext_members = payload["ext"] if isinstance(payload, dict) and isinstance(payload.get("ext"), dict) else {}
        needed_tokens += [EXT_TOKENS[m] for m in sorted(CRITICAL_EXT & set(ext_members))]
    if kind in ("lock", "unlock") and _dates_lock(body):
        needed_tokens.append("dates-v1")
    present = body.get("_requires") if isinstance(body.get("_requires"), list) else []
    missing = [t for t in dict.fromkeys(needed_tokens) if t not in present]
    out = {**body, "_requires": [*present, *missing]} if missing else {**body}
    out["_wire"] = wire_of(kind, out)
    return out


def _dates_lock(body: dict[str, Any]) -> bool:
    """A lock (or unlock) only a dates-aware reader reads correctly: the `releases` field, or a `release_date`
    value that is not a full day ("2011" would become a day-only `release_date` on an older reader)."""
    field_name, value = body.get("field"), body.get("value")
    if field_name == "releases":
        return True
    return field_name == "release_date" and isinstance(value, str) and len(value) != 10


# ---- documentation --------------------------------------------------------------------------------------

def markdown_table() -> str:
    """The kinds table in docs/guide/federation.md (tests/test_sync_compat.py checks the guide contains it)."""
    rows = ["| Kind | Wire | Emits / applies body version | Fields |", "| --- | --- | --- | --- |"]
    for spec in FEED_KINDS.values():
        fields = ", ".join(f"`{f}`" for f in sorted(spec.fields))
        rows.append(f"| `{spec.name}` | {spec.since} | {spec.emit} / {spec.apply} | {fields} |")
    for name in sorted(RESERVED_KINDS):
        rows.append(f"| `{name}` | reserved | not in this release | parked by this release |")
    return "\n".join(rows)
