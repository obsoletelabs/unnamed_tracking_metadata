"""CONTRACT-C13 §3.1-§3.2: the `record` kind at protocol 1.5 and the H52 shift of the unreleased reservations."""

from __future__ import annotations

import pytest

from atlas import feed_schema

RECORD = {"canonical_id": "atlas:01m5a1vhdrabwv215gqg1g8nx9", "media_type": "game", "title": "Subnautica 2",
          "provider_ids": {"steam": "1962700"}, "because": "sidecar"}


def test_record_kind_is_registered_in_wire_1():
    spec = feed_schema.FEED_KINDS["record"]
    assert (spec.since, spec.emit, spec.apply) == (1, 1, 1)
    assert spec.fields == {"canonical_id", "media_type", "title", "provider_ids", "because"}
    assert feed_schema.SINCE[("kind", "record")] == 1
    assert feed_schema.vocabularies()["record_because"] == {"publish", "sidecar", "merge", "detach", "relation",
                                                            "backfill"}
    assert "| `record` | 1 | 1 / 1 |" in feed_schema.markdown_table()


def test_reserved_constructs_are_refused_and_shipped_ones_signed_with_wire():
    """C16 Z1: the reservations stay reserved (refused at every call, there is no emit protocol any more) and every
    shipped construct is signed, carrying `_wire: 1`."""
    assert feed_schema.RESERVED_KINDS == {"price", "unmerge", "distinct"}
    assert feed_schema.RESERVED_TOKENS == {"relation-v1", "price-channel-v1", "field-clear-v1", "merge-guard-v1",
                                           "ach-v2", "art-v2"}
    assert feed_schema.RESERVED_VALUES == {("lock_field", "_no_merge"), ("artwork_kind", "trailer")}
    assert feed_schema.RESERVED_BODIES == {("lock", 2), ("unlock", 2), ("achievements", 2), ("artwork", 2)}
    assert feed_schema.PRICE_CHANNEL.since is None
    assert set(feed_schema.SINCE.values()) == {1}
    signed = feed_schema.check_emit("record", RECORD)
    assert signed == {**RECORD, "_wire": 1} and "_wire" not in RECORD  # the caller's body is untouched
    for kind in feed_schema.RESERVED_KINDS:
        with pytest.raises(feed_schema.ReservedConstructError):
            feed_schema.check_emit(kind, {})
    for kind, version in feed_schema.RESERVED_BODIES:
        with pytest.raises(feed_schema.ReservedConstructError):
            feed_schema.check_emit(kind, {"_v": version})


def test_a_1_4_follower_parks_a_record_entry(monkeypatch):
    """T-V4: a 1.4 release does not know the kind, so it parks it (never drops it)."""
    older = {k: v for k, v in feed_schema.FEED_KINDS.items() if k != "record"}
    monkeypatch.setattr(feed_schema, "FEED_KINDS", older)
    outcome = feed_schema.classify("record", RECORD)
    assert (outcome.action, outcome.reason) == (feed_schema.PARK, "unsupported_kind")


def test_a_wire_1_follower_parks_an_unknown_because_and_ignores_an_unknown_member():
    """An unknown value of a closed vocabulary still parks; an unknown member is ignored (W-2, tolerant reader)."""
    assert feed_schema.classify("record", RECORD).action == feed_schema.APPLY
    outcome = feed_schema.classify("record", {**RECORD, "because": "a_newer_reason"})
    assert (outcome.action, outcome.reason) == (feed_schema.PARK, "unsupported_value")
    outcome = feed_schema.classify("record", {**RECORD, "summary": "provider prose"})
    assert (outcome.action, outcome.reason) == (feed_schema.APPLY, feed_schema.IGNORED_MEMBER)
    assert "summary" not in outcome.body
