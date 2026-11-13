"""CONTRACT-C15 §4.1 / C16 (A20): every construct added after A19 is registered under `feed_schema.UNIFIED`, the one
release version, and under nothing else (T-B9). A20's own construct is `schema.types[].achievements`."""

from __future__ import annotations

import pytest

from atlas import feed_schema
from atlas.feed_schema import EmitGateError, check_emit, classify

SINCE_MAPS = ("KIND_SINCE", "TOKEN_SINCE", "VALUE_SINCE", "BODY_SINCE", "MEMBER_SINCE")
#: Entries that existed before A20 and happen to equal today's protocol (C13's `record`, 1.5). They are permanent
#: numbers of an earlier step, not post-A19 constructs, so they are written as literals.
PRE_A20 = {("KIND_SINCE", "record")}

SCHEMA = {"types": [{"key": "comic", "label": "Comic", "enabled": True, "position": 7, "labels": {},
                     "achievements": False}], "fields": []}


def test_unified_is_the_protocol_of_this_release_but_its_own_object():
    assert feed_schema.UNIFIED == feed_schema.PROTOCOL
    assert isinstance(feed_schema.UNIFIED, feed_schema.UnifiedVersion)
    assert feed_schema.parse_protocol(feed_schema.UNIFIED) == feed_schema.parse_protocol(feed_schema.PROTOCOL)
    literal = "".join(["1", ".", "5"])  # built at run time: not the interned constant
    assert literal == feed_schema.UNIFIED and literal is not feed_schema.UNIFIED


def test_t_b9_every_c15_since_entry_is_unified_by_identity():
    """T-B9: each registered construct *is* `UNIFIED` (identity), and every since-map entry that equals the
    unified number either is `UNIFIED` or is one of the pre-A20 entries."""
    assert feed_schema.UNIFIED_CONSTRUCTS, "A20 registers schema.types[].achievements"
    for name, key in feed_schema.UNIFIED_CONSTRUCTS:
        assert getattr(feed_schema, name)[key] is feed_schema.UNIFIED, (name, key)
    for name in SINCE_MAPS:
        for key, since in getattr(feed_schema, name).items():
            if since is feed_schema.UNIFIED:
                assert (name, key) in feed_schema.UNIFIED_CONSTRUCTS, f"{name}[{key!r}] is UNIFIED but unregistered"
            elif since == feed_schema.UNIFIED:
                assert (name, key) in PRE_A20, f"{name}[{key!r}] writes the unified number as a literal"


def test_no_new_protocol_minor_is_advertised():
    """C16: books and A20 join the one release; nothing in the registry names a protocol above `PROTOCOL` except
    the old C11/C12 reservations (1.6-1.9), which C16 folds or keeps reserved."""
    assert feed_schema.PROTOCOL == "1.5"
    reserved = set(feed_schema.RESERVED_TOKENS.values()) | set(feed_schema.RESERVED_KINDS.values())
    for name in SINCE_MAPS:
        for key, since in getattr(feed_schema, name).items():
            if feed_schema.parse_protocol(since) > feed_schema.parse_protocol(feed_schema.PROTOCOL):
                assert since in reserved, (name, key, since)


def test_schema_achievements_member_applies_on_this_release():
    outcome = classify("schema", SCHEMA)
    assert outcome.action == "apply" and outcome.body["types"][0]["achievements"] is False


def test_schema_achievements_member_is_signed_at_unified_and_left_out_below():
    at = check_emit("schema", SCHEMA, feed_schema.UNIFIED)
    assert at["types"][0]["achievements"] is False and "_requires" not in at
    below = check_emit("schema", SCHEMA, "1.4")
    assert "achievements" not in below["types"][0] and below["types"][0]["key"] == "comic"
    assert SCHEMA["types"][0]["achievements"] is False  # the caller's body is untouched
    old = {"types": [{"key": "comic", "label": "Comic"}], "fields": []}
    assert check_emit("schema", old, "1.0") is old  # nothing to strip: signed exactly as before


def test_moving_unified_moves_the_gate(monkeypatch):
    """The unification PR changes one constant: the member then needs that version, with no other edit."""
    later = feed_schema.UnifiedVersion("1.6")
    monkeypatch.setattr(feed_schema, "UNIFIED", later)
    monkeypatch.setitem(feed_schema.MEMBER_SINCE, ("schema", "types[].achievements"), later)
    assert "achievements" not in check_emit("schema", SCHEMA, "1.5")["types"][0]
    monkeypatch.setattr(feed_schema, "PROTOCOL", "1.6")
    assert check_emit("schema", SCHEMA, "1.6")["types"][0]["achievements"] is False


@pytest.mark.parametrize("current", ["1.0", "1.4", "1.5"])
def test_member_gate_never_raises(current):
    """An optional member is left out, never a reason to refuse the whole schema entry."""
    try:
        check_emit("schema", SCHEMA, current)
    except EmitGateError as exc:  # pragma: no cover - the assertion below explains
        pytest.fail(f"schema refused at {current}: {exc}")
