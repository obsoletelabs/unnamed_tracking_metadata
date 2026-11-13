"""Replays, outages, rewinds, forged and revoked identities: the database must stay correct."""

from __future__ import annotations

import base64

import pytest

from atlas.signing import body_hash, load_identity
from atlas.sync import SyncError
from tools.federation_lab import observation_entry
from tools.multinode_drill import csrf

pytestmark = pytest.mark.federation_lab


def _seed(lab, count=3):
    for index in range(count):
        lab.nodes["official"].post(f"Seed {index}")
    lab.nodes["indie"].post("Seed Indie")
    lab.sync_all()


def _on_changes(rewrite):
    """Apply `rewrite(page)` to the /sync/changes answer only."""
    def tamper(path, body):
        return rewrite(body) if path.endswith("/sync/changes") else body
    return tamper


# ---- replayed and duplicate batches ---------------------------------------------------------------------

def test_replay_after_losing_the_cursor_changes_nothing(lab):
    """A follower restored from an older backup has an old cursor; the authority re-sends entries it
    already applied. Re-application must be a no-op."""
    official, local = lab.nodes["official"], lab.nodes["local"]
    _seed(lab)
    settled = local.digest()

    local.set_cursor(official, 0)  # cursor lost, retained signed copies kept
    assert lab.sync("local", "official") is None
    assert local.digest() == settled

    local.set_cursor(official, 0)
    local.forget_retained(official)  # cursor AND retained copies lost: every entry is applied again
    assert lab.sync("local", "official") is None
    assert local.observations(official) == official.observations()
    assert local.digest() == settled, "re-applying the whole feed must reproduce identical state"
    lab.assert_converged()


def test_stale_page_replayed_by_the_upstream_is_rejected(lab):
    """An upstream (or a cache in between) answers with a page older than the cursor."""
    official, local = lab.nodes["official"], lab.nodes["local"]
    _seed(lab)
    from atlas import wire

    stale = official.client.get("/api/v2/sync/changes", params={"since": 0, "limit": 2},  # a wire-1 peer call
                                headers={**official.headers(official.follower),
                                         wire.HEADER: wire.request_header()}).json()
    settled = local.digest()

    with pytest.raises(SyncError, match="gap or rewind"):
        local.pull(official, tamper=_on_changes(lambda page: stale))
    assert local.digest() == settled


def test_duplicate_entries_inside_one_page_are_rejected_atomically(lab):
    official, local = lab.nodes["official"], lab.nodes["local"]
    for index in range(2):
        official.post(f"Duplicate {index}")
    settled = local.digest()

    def duplicate_last(page):
        return {**page, "entries": [*page["entries"], page["entries"][-1]]}

    with pytest.raises(SyncError, match="gap or rewind"):
        local.pull(official, tamper=_on_changes(duplicate_last))
    assert local.digest() == settled, "a rejected page must not leave a partial application behind"
    assert local.observations(official) == {}
    lab.sync_all()  # the genuine feed still applies afterwards
    lab.assert_converged()


# ---- offline and reconnect ------------------------------------------------------------------------------

def test_authority_offline_then_reconnect_converges(lab):
    official, indie, mirror, local = (lab.nodes[n] for n in ("official", "indie", "mirror", "local"))
    _seed(lab)
    official.stop()
    settled = mirror.digest()

    assert lab.sync("mirror", "official") is not None, "an unreachable authority must be reported"
    assert mirror.digest() == settled
    during = indie.post("Posted While Official Was Down")
    assert lab.sync("mirror", "indie") is None and lab.sync("local", "indie") is None
    assert during in local.canonicals(), "the other authority keeps flowing"

    official.start()
    after = official.post("Posted After Official Returned")
    lab.sync_all()
    lab.assert_converged()
    assert after in local.canonicals()


def test_mirror_offline_then_reconnect_converges(lab):
    official, mirror, local, local2 = (lab.nodes[n] for n in ("official", "mirror", "local", "local2"))
    _seed(lab)
    mirror.stop()
    official.post("Posted While Mirror Was Down")
    settled = (local.digest(), local2.digest())

    assert lab.sync("local", "official") is not None and lab.sync("local2", "official") is not None
    assert (local.digest(), local2.digest()) == settled, "a failed pull must not change the follower"

    mirror.start()
    lab.sync_all()
    lab.assert_converged()


# ---- cursor expiry / authority history loss -------------------------------------------------------------

def _rewind_official(lab, *, keep_back=2):
    """Restore the official authority from an older backup, then publish a different entry under a
    sequence number every follower already holds. Returns (canonical id of the new entry, kept length)."""
    official = lab.nodes["official"]
    keep = official.head() - keep_back
    official.stop()
    official.truncate_feed(keep)
    official.start()
    forked = official.post("Forked History")
    assert official.head() == keep + 1
    return forked, keep


def test_rewound_authority_is_refused_when_a_follower_rereads_its_feed(lab):
    """Atlas has no feed retention window today, so the way a cursor stops being valid is an authority
    restored from an older backup. A follower that re-reads from the start (its cursor was reset) holds
    signed copies of the old entries and must refuse the fork rather than blend the two histories."""
    official, mirror = lab.nodes["official"], lab.nodes["mirror"]
    _seed(lab, count=4)
    forked, _ = _rewind_official(lab)
    mirror.set_cursor(official, 0)
    settled = mirror.digest()

    error = lab.sync("mirror", "official")

    assert error is not None and "fork" in error.lower(), error
    assert forked not in mirror.canonicals()
    assert mirror.digest() == settled


def test_rewound_authority_is_not_silently_ignored_by_an_up_to_date_follower(lab):
    """The follower compares the advertised head with what it already verified (atlas#33 C1.7) and
    reports the rewind instead of claiming success. Recovery itself is #15 / #24."""
    official, mirror = lab.nodes["official"], lab.nodes["mirror"]
    _seed(lab, count=4)
    forked, _ = _rewind_official(lab)

    error = lab.sync("mirror", "official")

    assert error is not None or forked in mirror.canonicals(), (
        f"mirror reports success but holds a different history than the authority: cursor "
        f"{mirror.cursor(official)}, authority head {official.head()}, no error recorded")
    assert "fork or rewind" in error and forked not in mirror.canonicals()
    status = mirror.sync_status(official)
    assert not status["in_sync"] and status["state"] == "rewound", status
    assert status["rewound"]["advertised_head"] < status["verified_head"]


# ---- forged, rotated and revoked identities ------------------------------------------------------------

def test_bad_signature_is_rejected_and_nothing_is_applied(lab):
    official, local = lab.nodes["official"], lab.nodes["local"]
    _seed(lab)
    official.post("Signed Then Tampered")
    settled = local.digest()
    forged_title = "Substituted By An Attacker"

    def corrupt_signature(page):
        entries = [dict(e) for e in page["entries"]]
        raw = bytearray(base64.b64decode(entries[-1]["signature"]))
        raw[0] ^= 0xFF
        entries[-1]["signature"] = base64.b64encode(bytes(raw)).decode()
        return {**page, "entries": entries}

    def swap_body(page):
        entries = [dict(e) for e in page["entries"]]
        entries[-1]["body"] = observation_entry(forged_title)
        return {**page, "entries": entries}

    for rewrite in (corrupt_signature, swap_body):
        with pytest.raises(SyncError, match="bad signature"):
            local.pull(official, tamper=_on_changes(rewrite))
        assert local.digest() == settled, f"{rewrite.__name__}: a rejected page must not change the database"
    assert forged_title not in str(local.canonicals())
    assert lab.sync("local", "official") is None, "the genuine feed still applies afterwards"


def test_key_rotation_converges_and_old_key_is_revoked(lab):
    official, local = lab.nodes["official"], lab.nodes["local"]
    _seed(lab)
    with official.state.maker() as session:
        old_key = load_identity(session)
    before = official.client.get("/api/v2/sync/info", headers=official.headers()).json()

    rotated = official.admin.post("/admin/keys/rotate", data={
        "csrf": csrf(official.admin), "password": lab.password,
        "confirm": before["signing_key_id"], "followers_upgraded": "on"})
    assert rotated.status_code == 303
    after_rotation = official.post("Published Under The New Key")
    official.post("Another Under The New Key")

    lab.sync_all()  # followers hop the rotation without re-pinning
    lab.assert_converged()
    assert after_rotation in local.canonicals()
    info = official.client.get("/api/v2/sync/info", headers=official.headers()).json()
    assert info["node_id"] == before["node_id"] and info["signing_key_id"] != before["signing_key_id"]

    # The retired key later signs a plausible next entry: a leaked or revoked key must not be accepted.
    seq = official.head() + 1
    body = observation_entry("Signed By The Revoked Key")
    digest = body_hash(body)
    forged = {"seq": seq, "kind": "obs", "body": body, "body_hash": digest,
              "signature": old_key.sign(seq, "obs", digest), "key_id": old_key.key_id,
              "node_id": official.node_id, "created_at": "2026-01-01T00:00:00+00:00"}
    settled = local.digest()

    with pytest.raises(SyncError, match="bad signature"):
        local.pull(official, tamper=_on_changes(lambda page: {**page, "entries": [*page["entries"], forged], "head": seq}))
    assert local.digest() == settled
    assert "Signed By The Revoked Key" not in str(local.canonicals())


def test_wrong_authority_is_refused(lab):
    official, indie, mirror, local = (lab.nodes[n] for n in ("official", "indie", "mirror", "local"))
    _seed(lab)
    settled = local.digest()

    # 1. Pinned to the official identity, but the URL serves the independent authority.
    with pytest.raises(SyncError, match="not trusted"):
        local.pull(indie, authority=official)
    # 2. The right node id with another authority's public key pinned.
    with pytest.raises(SyncError, match="does not match"):
        local.pull(official, pinned_key=indie.root_key)
    # 3. A relay that does not hold the pinned authority's feed.
    with pytest.raises(SyncError, match="not trusted"):
        local.pull(mirror, authority=local)

    assert local.digest() == settled
    assert local.observations(indie) == indie.observations(), "the independent authority was never mixed up"
    lab.assert_converged()


def test_revoked_sync_credential_stops_replication_without_damage(lab):
    official, mirror = lab.nodes["official"], lab.nodes["mirror"]
    _seed(lab)
    official.revoke_token("drill-follower")
    official.post("Posted After Revocation")
    settled = mirror.digest()

    error = lab.sync("mirror", "official")
    assert error is not None and ("401" in error or "403" in error), error
    assert mirror.digest() == settled

    # An administrator issues a replacement credential; replication resumes and converges.
    response = official.client.post("/api/v2/tokens", headers=official.headers(official.admin_token),
                                    json={"name": "replacement-follower", "scopes": ["read", "sync"]})
    assert response.status_code == 201
    mirror.set_peer_token(lab.peers[("mirror", "official")], response.json()["token"])
    lab.sync_all()
    lab.assert_converged()
