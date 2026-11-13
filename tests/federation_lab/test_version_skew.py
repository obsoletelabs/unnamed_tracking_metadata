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


def test_unknown_observation_field_is_ignored_not_dropped(lab):
    """C16 Z1 (W-2, tolerant readers): an unknown member of a known body is ignored and the entry applies."""
    official, local = lab.nodes["official"], lab.nodes["local"]
    entry = observation_entry("Game With A Newer Field", extra_payload={"release_date_precision": "month"})
    observation_id = entry["observation"]["id"]
    seq, _ = _publish_around(lab, lambda node: node.emit("obs", entry))

    lab.sync("mirror", "official")
    lab.sync("local", "official")

    assert disposition(local, official, seq, observation_id) == "applied", (
        f"observation {observation_id} with an unknown payload field did not apply: local's cursor is "
        f"{local.cursor(official)}, open sync failures {local.open_failures()}")
    assert local.sync_status(official)["parked"] is None


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


# ---- CONTRACT-C13 §10.1 T-V2 and T-V3 (A19): a follower of this release and an upstream or a past of an older one ----

ART = [{"kind": "key_art", "url": "https://img.example.org/skew-key.png"}]


def _orphan(official) -> tuple[str, int]:
    """A v0 authority's live shape: a provider-only record and curated artwork on it, signed without introducing the
    record. A wire-1 authority signs a `record` first (C16 Z1), so the lab signs the v0 entry directly."""
    from atlas import service
    from atlas.contract import ObservationBatchV1

    body = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "metadata", "source": "steam", "source_scope": "federation-lab"},
        "observations": [{"external_id": "7770101", "title": "Skew Provider Only", "provider_ids": {"steam": "7770101"}}]})
    with official.state.maker() as session:
        [result] = service.ingest(session, official.state.ctx, body, system=True)
        session.commit()
    cid = result.canonical_id
    return cid, official.emit("artwork", {"canonical_id": cid, "source": "curator", "items": ART}, v0=True)


def _as_1_0(path, body):
    """What an upstream that does not report its records says in `/sync/info`: no record counts, no introduction
    mark (a v0 upstream; the wire range stays, so the lab's wire-1 server still answers this follower)."""
    if path.endswith("/sync/info"):
        for key in ("records", "introduced_through", "media_withheld"):
            body.pop(key, None)
    return body


def test_v2_a_follower_under_a_1_0_upstream_with_orphans_says_waiting_not_failed(lab):
    """T-V2: the upstream speaks 1.0 (no `records`, no `introduced_through`) and its feed names a record it never
    published. This release's follower is caught up with one change waiting for its game, says the upstream has
    not published it, and does not claim a record count the upstream never reported."""
    official, mirror = lab.nodes["official"], lab.nodes["mirror"]
    official.post("Skew Curated")
    cid, art_seq = _orphan(official)
    mirror.pull(official, tamper=_as_1_0)
    assert mirror.open_failures() == [(official.node_id, art_seq, "artwork")]
    status = mirror.sync_status(official)
    assert status["state"] == "waiting" and status["applied_head"] == official.head()
    page = mirror.admin.get("/admin/peers").text
    assert 'data-peer-status="caught_up_waiting"' in page
    assert "Caught up · 1 change waiting for their game" in page
    assert "record count unknown (this upstream does not report it)" in page
    assert "has not published this game&#39;s record yet" in page or "has not published this game's record yet" in page
    assert f'data-waiting-for="{cid}" data-waiting-subreason="upstream_unpublished"' in page
    for wrong in ("degraded", "apply error", "upstream_defect"):
        assert wrong not in page.lower(), wrong


def test_v3_an_upgrade_from_1_0_rescans_and_recovers_the_dropped_sidecar_when_the_record_arrives(lab):
    """T-V3: under 1.0 the follower dropped the artwork (cursor past it, nothing recorded). After the upgrade,
    `atlas sync replay --rescan` finds it and makes it wait for its record; when the authority introduces the record,
    the next sync applies the record and the artwork, with nothing left open."""
    from sqlalchemy import delete, select

    from atlas.models import Artwork, SyncFailure
    from tools.skew_drill import _current_cli

    official, mirror = lab.nodes["official"], lab.nodes["mirror"]
    cid, art_seq = _orphan(official)
    assert lab.sync("mirror", "official") is None
    with mirror.state.maker() as session:  # what 1.0 left behind: the entry retained, the cursor past it, no row
        session.execute(delete(SyncFailure).where(SyncFailure.node_id == official.node_id))
        session.commit()
    assert mirror.cursor(official) == art_seq and mirror.open_failures() == []

    done = _current_cli(mirror, "sync", "replay", "--rescan")
    assert "1 entries now wait for a record" in done.stdout, (done.stdout, done.stderr)
    with mirror.state.maker() as session:
        row = session.get(SyncFailure, (official.node_id, art_seq))
        assert (row.disposition, row.reason, row.waiting_for) == ("wait", "dropped_by_previous_release", cid)

    done = _current_cli(official, "feed", "introduce", "--missing")
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert lab.sync("mirror", "official") is None
    assert mirror.open_failures() == []
    with mirror.state.maker() as session:
        assert [(a.kind, a.url) for a in session.scalars(select(Artwork).where(Artwork.canonical_id == cid))] == [
            (ART[0]["kind"], ART[0]["url"])]
    assert mirror.sync_status(official)["state"] == "in_sync"
