"""CONTRACT-C13 §3.1-§3.2: the `record` kind at protocol 1.5 and the H52 shift of the unreleased reservations."""

from __future__ import annotations

import pytest

from atlas import feed_schema

RECORD = {"canonical_id": "atlas:01m5a1vhdrabwv215gqg1g8nx9", "media_type": "game", "title": "Subnautica 2",
          "provider_ids": {"steam": "1962700"}, "because": "sidecar"}


def _below(version: str) -> str:
    major, minor = feed_schema.parse_protocol(version)
    return feed_schema.fmt((major, minor - 1))


def test_record_kind_is_registered_at_1_5():
    spec = feed_schema.FEED_KINDS["record"]
    assert (spec.since, spec.emit, spec.apply) == ("1.5", 1, 1)
    assert spec.fields == {"canonical_id", "media_type", "title", "provider_ids", "because"}
    assert feed_schema.KIND_SINCE["record"] == "1.5" == feed_schema.RECORD_PROTOCOL
    assert feed_schema.PROTOCOL == "1.5"
    assert feed_schema.vocabularies()["record_because"] == {"publish", "sidecar", "merge", "detach", "relation",
                                                            "backfill"}
    assert "| `record` | 1.5 | 1 / 1 |" in feed_schema.markdown_table()


def test_check_emit_since_maps():
    """T-A7: after the H52 shift every gated construct is refused one minor below its `since`, and `record` is
    refused at 1.4 and signed unchanged at 1.5."""
    shifted = {"price": "1.6", "unmerge": "1.7", "distinct": "1.7"}
    assert {k: v for k, v in feed_schema.KIND_SINCE.items() if k in shifted} == shifted
    assert feed_schema.RESERVED_KINDS == shifted
    assert feed_schema.TOKEN_SINCE == {"tier-platinum": "1.2", "dates-v1": "1.3", "relation-v1": "1.4",
                                       "price-channel-v1": "1.6", "field-clear-v1": "1.7", "merge-guard-v1": "1.7",
                                       "ach-v2": "1.8", "art-v2": "1.9",
                                       "book-v1": feed_schema.UNIFIED}
    # book-v1 (A21) joins the one release (UNIFIED), not the C13 reserved list
    assert feed_schema.RESERVED_TOKENS == {k: v for k, v in feed_schema.TOKEN_SINCE.items() if k != "book-v1"}
    assert feed_schema.VALUE_SINCE[("lock_field", "_no_merge")] == "1.7"
    assert feed_schema.VALUE_SINCE[("artwork_kind", "trailer")] == "1.9"
    assert feed_schema.BODY_SINCE == {("lock", 2): "1.7", ("unlock", 2): "1.7", ("achievements", 2): "1.8",
                                      ("artwork", 2): "1.9"}
    assert feed_schema.PRICE_CHANNEL.since == "1.6"

    with pytest.raises(feed_schema.EmitGateError) as err:
        feed_schema.check_emit("record", RECORD, "1.4")
    assert (err.value.needed, err.value.current) == ("1.5", "1.4")
    assert feed_schema.check_emit("record", RECORD, "1.5") is RECORD  # a version-1 body, no envelope added
    for kind, since in feed_schema.KIND_SINCE.items():
        if feed_schema.parse_protocol(since) <= feed_schema.parse_protocol(feed_schema.PROTOCOL):
            with pytest.raises(feed_schema.EmitGateError):
                feed_schema.check_emit(kind, {}, _below(since))
    for (kind, version), since in feed_schema.BODY_SINCE.items():
        with pytest.raises(feed_schema.EmitGateError) as err:
            feed_schema.check_emit(kind, {"_v": version}, _below(since))
        assert err.value.needed == since


def test_a_1_4_follower_parks_a_record_entry(monkeypatch):
    """T-V4: a 1.4 release does not know the kind, so it parks it (never drops it)."""
    older = {k: v for k, v in feed_schema.FEED_KINDS.items() if k != "record"}
    monkeypatch.setattr(feed_schema, "FEED_KINDS", older)
    outcome = feed_schema.classify("record", RECORD)
    assert (outcome.action, outcome.reason) == (feed_schema.PARK, "unsupported_kind")


def test_a_1_5_follower_parks_an_unknown_because_and_applies_a_known_one():
    assert feed_schema.classify("record", RECORD).action == feed_schema.APPLY
    outcome = feed_schema.classify("record", {**RECORD, "because": "a_newer_reason"})
    assert (outcome.action, outcome.reason) == (feed_schema.PARK, "unsupported_value")
    outcome = feed_schema.classify("record", {**RECORD, "summary": "provider prose"})
    assert (outcome.action, outcome.reason) == (feed_schema.PARK, "unsupported_field")


def test_the_settings_accept_emit_1_5_and_nothing_above():
    from tests.conftest import build_settings

    assert feed_schema.emit_protocol(build_settings(emit_protocol="1.5")) == "1.5"
    with pytest.raises(ValueError, match="1.5 at most"):
        build_settings(emit_protocol="1.6")
