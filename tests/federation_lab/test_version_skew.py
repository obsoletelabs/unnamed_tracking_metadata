"""Version skew: a newer authority talking to an older follower (atlas#33).

The lab has one release of the code, so it plays "newer" by having the authority sign feed entries
the current code does not understand: an unknown entry kind, and an observation carrying a field the
current contract does not have. Both are exactly what the planned date-precision and relationship
changes will put on the wire. Once a newer release exists, run one node from it with
`ATLAS_LAB_PYTHON_<NODE>` for a real mixed-version network (docs/guide/federation-lab.md).

Before the compatibility gate (atlas#33) a follower lost such entries silently: the cursor moved past
them and nothing recorded it. Now it *parks* them (CONTRACTS-v1 C1.4): the entry is retained and
recorded, the applied cursor stops before it, and later entries keep downloading and relaying.
Acceptable outcomes are anything `disposition` reports as "applied", "blocked" or "recorded": never
"dropped".
"""

from __future__ import annotations

import pytest

from tools.federation_lab import disposition, observation_entry

pytestmark = pytest.mark.federation_lab


def _publish_around(lab, emit):
    """Ordinary games before and after a newer-release entry; returns (entry seq, canonical ids)."""
    official = lab.nodes["official"]
    first = official.post("Before The Newer Entry")
    seq = emit(official)
    last = official.post("After The Newer Entry")
    return seq, (first, last)


def test_unknown_entry_kind_is_not_silently_dropped(lab):
    official, local = lab.nodes["official"], lab.nodes["local"]
    seq, _ = _publish_around(lab, lambda node: node.emit(
        "relationship", {"canonical_id": "atlas:" + "0" * 26, "kind": "dlc_of", "target": "atlas:" + "1" * 26}))

    lab.sync("mirror", "official")
    lab.sync("local", "official")

    assert disposition(local, official, seq) != "dropped", (
        f"entry {seq} (kind 'relationship') was passed over: local's cursor is {local.cursor(official)}, "
        f"open sync failures {local.open_failures()}")
    for follower in (lab.nodes["mirror"], local):
        assert follower.cursor(official) == seq - 1, "nothing is applied past a parked entry"
        assert (official.node_id, seq, "relationship") in follower.open_failures()
        status = follower.sync_status(official)
        assert status["parked"]["first_seq"] == seq and status["parked"]["first_reason"] == "unsupported_kind"
        assert status["verified_head"] == official.head() and not status["in_sync"]


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
    assert local.sync_status(official)["parked"]["first_reason"] == "unsupported_field"


def test_mirror_relays_newer_entries_verbatim(lab):
    """A parked mirror still retains, verifies and relays the signed entry and everything after it
    byte-for-byte, so a downstream node upgraded later can read them. The entry before it applies;
    the entry after it waits (causal order) instead of being applied out of order."""
    official, mirror, local = lab.nodes["official"], lab.nodes["mirror"], lab.nodes["local"]
    seq, (first, last) = _publish_around(lab, lambda node: node.emit(
        "relationship", {"canonical_id": "atlas:" + "0" * 26, "kind": "dlc_of", "target": "atlas:" + "1" * 26}))

    lab.sync("mirror", "official")
    lab.sync("local", "official")

    for follower in (mirror, local):
        assert follower.retained(official) == official.own_feed(), f"{follower.name} must retain every signed entry"
        assert follower.cursor(official) == seq - 1
        assert first in follower.canonicals() and last not in follower.canonicals()
    assert official.own_feed()[seq][0] == "relationship"


def test_skipping_a_parked_entry_lets_the_rest_apply_and_is_audited(lab):
    """The operator escape hatch (`atlas sync skip`): the parked entry becomes quarantined and the
    entries after it apply; the signed entry stays retained for a later replay."""
    official, local = lab.nodes["official"], lab.nodes["local"]
    seq, (_, last) = _publish_around(lab, lambda node: node.emit("relationship", {"canonical_id": "atlas:" + "2" * 26}))
    lab.sync("mirror", "official")
    lab.sync("local", "official")
    assert local.cursor(official) == seq - 1

    local.skip(official, seq, "lab: relationships are not used on this node")

    assert local.cursor(official) == official.head() and last in local.canonicals()
    assert local.sync_status(official)["parked"] is None
    assert local.audit("sync_skip") == [f"{official.node_id}:{seq}"]
    assert lab.sync("local", "official") is None
    assert local.retained(official) == official.own_feed()


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
