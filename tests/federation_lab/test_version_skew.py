"""Version skew: a newer authority talking to an older follower (atlas#33).

The lab has one release of the code, so it plays "newer" by having the authority sign feed entries
the current code does not understand: an unknown entry kind, and an observation carrying a field the
current contract does not have. Both are exactly what the planned date-precision and relationship
changes will put on the wire. Once a newer release exists, run one node from it with
`ATLAS_LAB_PYTHON_<NODE>` for a real mixed-version network (docs/guide/federation-lab.md).

The two `xfail` scenarios DEMONSTRATE the Phase 0 finding and are strict: they pass today because the
follower loses data silently (`sync._apply` ignores an unknown kind and still advances the cursor;
`_valid_observation` rejects an unknown payload field and still advances the cursor). When the
compatibility gate lands they start passing, strict mode turns that into a failure, and the fix PR
must delete the markers. Acceptable outcomes after the fix are anything `disposition` reports as
"applied", "blocked" or "recorded": never "dropped".
"""

from __future__ import annotations

import pytest

from tools.federation_lab import disposition, observation_entry

pytestmark = pytest.mark.federation_lab

GATE = dict(
    strict=True, raises=AssertionError,
    reason="atlas#33: no federation compatibility gate. sync._apply skips unknown entry kinds and "
           "_valid_observation drops unknown payload fields while the cursor still advances, so a "
           "newer authority's data is lost silently. Remove this marker in the PR that adds the gate.",
)


def _publish_around(lab, emit):
    """Ordinary games before and after a newer-release entry; returns (entry seq, canonical ids)."""
    official = lab.nodes["official"]
    first = official.post("Before The Newer Entry")
    seq = emit(official)
    last = official.post("After The Newer Entry")
    return seq, (first, last)


@pytest.mark.xfail(**GATE)
def test_unknown_entry_kind_is_not_silently_dropped(lab):
    official, local = lab.nodes["official"], lab.nodes["local"]
    seq, _ = _publish_around(lab, lambda node: node.emit(
        "relationship", {"canonical_id": "atlas:" + "0" * 26, "kind": "dlc_of", "target": "atlas:" + "1" * 26}))

    lab.sync("mirror", "official")
    lab.sync("local", "official")

    assert disposition(local, official, seq) != "dropped", (
        f"entry {seq} (kind 'relationship') was passed over: local's cursor is {local.cursor(official)}, "
        f"open sync failures {local.open_failures()}")


@pytest.mark.xfail(**GATE)
def test_unknown_observation_field_is_not_silently_dropped(lab):
    official, local = lab.nodes["official"], lab.nodes["local"]
    entry = observation_entry("Game With A Newer Field", extra_payload={"release_date_precision": "month"})
    observation_id = entry["observation"]["id"]
    seq, _ = _publish_around(lab, lambda node: node.emit("obs", entry))

    lab.sync("mirror", "official")
    lab.sync("local", "official")

    assert disposition(local, official, seq, observation_id) != "dropped", (
        f"observation {observation_id} with an unknown payload field vanished: local's cursor is "
        f"{local.cursor(official)}, open sync failures {local.open_failures()}")


def test_mirror_relays_newer_entries_verbatim(lab):
    """What does work today: a mirror retains and relays the signed entry byte-for-byte, so a
    downstream node upgraded later can still read it, and the entries around it apply normally."""
    official, mirror, local = lab.nodes["official"], lab.nodes["mirror"], lab.nodes["local"]
    seq, (first, last) = _publish_around(lab, lambda node: node.emit(
        "relationship", {"canonical_id": "atlas:" + "0" * 26, "kind": "dlc_of", "target": "atlas:" + "1" * 26}))

    lab.sync("mirror", "official")
    lab.sync("local", "official")

    for follower in (mirror, local):
        assert follower.retained(official) == official.own_feed(), f"{follower.name} must retain every signed entry"
        assert follower.cursor(official) == official.head()
        assert {first, last} <= set(follower.canonicals()), "entries around the unknown one still apply"
    assert official.own_feed()[seq][0] == "relationship"


def test_older_follower_still_reads_entries_it_understands_from_a_newer_authority(lab):
    """Additive-compatible traffic keeps working: a known kind with the current shape from the same
    authority that also emits newer entries."""
    official, local = lab.nodes["official"], lab.nodes["local"]
    entry = observation_entry("Plain Observation From A Newer Authority")
    official.emit("obs", entry)

    lab.sync("mirror", "official")
    assert lab.sync("local", "official") is None
    assert entry["observation"]["id"] in local.observations(official)
    assert disposition(local, official, official.head(), entry["observation"]["id"]) == "applied"
