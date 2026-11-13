"""Closed vocabularies shared by Atlas, the metadata-atlas plugin and the Tracking App (CONTRACTS-v1 C7).

This module is the single registry of the enum values Atlas accepts, stores and sends. The rest of
Atlas reads its constants from here (`facets.ACH_TIERS`, the seeded crossplay field, the federation
classifier in `feed_schema.vocabularies`). `contracts/vocab-v1.json` is generated from it
(`python -m tools.vocab_check generate`) and a test fails when the two disagree. Other repositories
generate the same file from their own registries; `tools/vocab_check.py compare` checks that the
overlapping keys agree (X2).

Rules (C7.3, CONTRACTS-v1 section 1):

* Absent means unknown. An omitted or null value never means "no" and never clears a stored value.
* `not_applicable` exists only where it can be derived (crossplay, from `players_max == 1`) or a source
  says so explicitly. A derived value is never stored and never sent.
* No boolean is used for a fact that can be unknown.

`date_precision` and `release_category` ship with protocol 1.3 (partial and regional release dates, C5):
Atlas accepts them in `ext.release` and in `releases` locks (`atlas/dates.py`), and a follower parks an
entry with a value it does not know. `relation_type` ships with protocol 1.4 (relationships between works,
C6): Atlas stores relations of these types (`atlas/relations.py`) and signs them as the `relation` feed kind.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Version of the shared file. A new value is a minor change inside v1 (followers park it, C1.1); renaming
#: or removing a value, or changing what one means, needs vocab-v2.
VERSION = 1

#: C7.1. Stored as `achievement.tier` (String(8): "platinum" is exactly 8 characters). The host calls it `medal`.
ACHIEVEMENT_TIERS: tuple[str, ...] = ("bronze", "silver", "gold", "platinum")
#: C7.1. Stored as `achievement.kind`. The host stores it as `tier` (legacy naming, unchanged in this programme).
ACHIEVEMENT_KINDS: tuple[str, ...] = ("progression", "missable", "win_condition")
#: C7.2. The value of the standard `crossplay` game field. Absent = unknown (Steam never states "no").
CROSSPLAY: tuple[str, ...] = ("yes", "partial", "no")
#: C7.2. Derived when `players_max == 1` and nothing is stored; shown as "single-player", never counted as "no".
NOT_APPLICABLE = "not_applicable"
#: C6.2, the `relation` feed kind (protocol 1.4). Directed `from -> to`; `to` is the parent, the base work, or
#: the series or collection. The host's `same_game` is a merge, and Atlas never sends `custom` or `related_to`.
RELATION_TYPES: tuple[str, ...] = (
    "version_of", "remake_of", "remaster_of", "port_of", "dlc_of", "expansion_of", "standalone_expansion_of",
    "mod_of", "modpack_of", "total_conversion_of", "series_member", "collection_member")
#: C6.3 operations of a `relation` feed entry (Atlas-only; not part of the shared file).
RELATION_OPS: tuple[str, ...] = ("add", "end")
#: C15 §2.3 (C7 additions inside vocab v1, registered under the unified release version): the role of a book's
#: creator in `ext.book.authors[].role`, and an edition's format in `ext.book.editions[].format`. `book_format` is
#: identical to the host app's book formats (#265 §3.1), so the mapping is the identity.
BOOK_CREATOR_ROLES: tuple[str, ...] = ("author", "editor", "translator", "illustrator", "narrator", "contributor")
BOOK_FORMATS: tuple[str, ...] = ("paperback", "hardcover", "ebook", "audiobook", "other")
#: C13 §3.2, why an authority signed a `record` entry (protocol 1.5; Atlas-only, not part of the shared file).
RECORD_BECAUSE: tuple[str, ...] = ("publish", "sidecar", "merge", "detach", "relation", "backfill")
#: C5, partial and regional release dates (protocol 1.3; validated in `atlas/dates.py`).
DATE_PRECISIONS: tuple[str, ...] = ("year", "month", "day")
RELEASE_CATEGORIES: tuple[str, ...] = ("full", "early_access", "beta", "rerelease", "port", "demo")

#: C8.1 availability of a price observation (Atlas-only; not part of the shared file, which C7 defines).
PRICE_AVAILABILITY: tuple[str, ...] = ("available", "unavailable", "preorder", "free", "unreleased", "delisted",
                                       "unknown")
#: C8.1 `source_kind` of a price observation: a provider fetch or a curator/import entry (Atlas-only).
PRICE_SOURCE_KINDS: tuple[str, ...] = ("provider", "manual")

#: C12 §12.1.1 kind of an achievement set (Atlas-only until achievements body v2, protocol 1.7, ships them).
ACHIEVEMENT_SET_KINDS: tuple[str, ...] = ("base", "dlc", "expansion", "edition", "platform", "update", "event",
                                          "other")
#: C12 §12.1.3 stated status of an achievement. `retired` is derived (a row its source stopped listing) and
#: never sent by a source; the API shows it only with `include_retired=1`.
ACHIEVEMENT_STATUSES: tuple[str, ...] = ("active", "unobtainable")
#: C12 §12.1.6 rarity band, derived from the merged percent: (band, lowest percent of the band).
RARITY_BANDS: tuple[tuple[str, float], ...] = (("common", 50.0), ("uncommon", 20.0), ("rare", 5.0),
                                              ("very_rare", 1.0), ("ultra_rare", 0.0))
#: C12 §12.5.3 aspect class of an image, derived from its measured size (never sent on the wire).
ASPECT_CLASSES: tuple[str, ...] = ("tall", "portrait", "square", "landscape", "capsule", "wide", "other")
#: C12 §12.5.5 licence of an image (artwork rows and achievement icons). Absent = `unknown`.
LICENCES: tuple[str, ...] = ("provider_terms", "publisher_press", "owner_upload", "cc0", "cc-by", "cc-by-sa",
                             "unknown")
#: C12 §12.6.1 why the last download of an image failed (`last_error`).
MEDIA_ERRORS: tuple[str, ...] = (
    "invalid_url", "https_required", "host_not_allowed", "not_public", "dns_failed", "port_not_allowed", "timeout",
    "too_large", "bad_type", "bad_image", "dimensions", "status_4xx", "status_404", "status_410", "status_5xx",
    "too_many_redirects", "hash_mismatch", "storage")
#: C12 §12.6.1 the state of an image row as the API reports it (this release: no revalidation or quota, so
#: `gone` and `evicted` are not produced yet).
MEDIA_STATES: tuple[str, ...] = ("cached", "pending", "failing", "gone", "evicted", "not_fetched", "blocked")

#: The protocol that ships each vocabulary value a 1.1 follower does not know (C1.1, H13). The authority
#: refuses to write such a value into its signed feed until its `emit_protocol` reaches this protocol.
VALUE_PROTOCOL: dict[tuple[str, str], str] = {("achievement_tier", "platinum"): "1.2"}
#: The `_requires` token an entry carries when it contains that value (C1.4), so an older follower parks it
#: with an explicit reason instead of guessing.
VALUE_TOKEN: dict[tuple[str, str], str] = {("achievement_tier", "platinum"): "tier-platinum"}

#: Keys of the shared file that only Atlas publishes (not compared across repositories).
ATLAS_ONLY = frozenset({"feed_kinds"})
#: Path of the generated file, relative to the repository root.
FILE = "contracts/vocab-v1.json"


def export() -> dict[str, Any]:
    """The content of `contracts/vocab-v1.json`, in the contract's key order (C7.4)."""
    from . import feed_schema  # feed_schema reads this module's constants; import lazily to stay acyclic

    return {
        "version": VERSION,
        "achievement_tier": list(ACHIEVEMENT_TIERS),
        "achievement_kind": list(ACHIEVEMENT_KINDS),
        "crossplay": list(CROSSPLAY),
        "relation_type": list(RELATION_TYPES),
        "date_precision": list(DATE_PRECISIONS),
        "release_category": list(RELEASE_CATEGORIES),
        "book_format": list(BOOK_FORMATS),
        "book_creator_role": list(BOOK_CREATOR_ROLES),
        "feed_kinds": list(feed_schema.FEED_KINDS),
    }


def render(data: dict[str, Any]) -> str:
    """The exact bytes of the file: two-space indent, keys in the given order, LF, trailing newline.
    `tools/vocab_check.py` has the same function; the test keeps them identical."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def digest(text: str) -> str:
    """SHA-256 of the file as published (LF line endings), the value in `vocab-v1.json.sha256`."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def crossplay_projection(stored: Any, players_max: Any) -> str | None:
    """The crossplay answer to show (C7.2): the stored value when there is one, `not_applicable` when the
    game is single-player (`players_max == 1`) and nothing is stored, otherwise None (unknown).

    `not_applicable` is a display value only: it is never stored, never sent in the API or the feed, and
    never counted as "no"."""
    if stored in CROSSPLAY:
        return str(stored)
    if isinstance(players_max, (int, float)) and not isinstance(players_max, bool) and players_max == 1:
        return NOT_APPLICABLE
    return None
