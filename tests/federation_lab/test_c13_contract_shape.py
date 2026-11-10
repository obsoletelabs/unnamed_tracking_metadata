"""CONTRACT-C13 §10.2 on real servers: the contract's own shape of the live failure, and its bridge variants.

`test_c13_live_regression` runs the failure on the wired five-node network (mirror and relays). This file runs the
steps §10.2 spells out, on a fresh network with no peers pinned:

* an upstream U (the lab's `official`) in legacy emission with five curated records, a provider-only record S with
  four curated sidecars (seq 6-10: the schema entry #78 adds is seq 8), a silent provider-only record Q, and a
  retained relayed feed F that is empty;
* a follower N (`local`) that looks U up (the API probe and the admin form), follows U's own feed, waits for S,
  and recovers in one pull after U raises its emit protocol and runs `atlas feed introduce --all`;
* variants 7a and 7a': U with the bridge on, and U at emit 1.5 while it still serves 1.1 followers.

Not here: variant 3a (an upstream that strips `/sync/info.records`) needs a stub between the nodes, so it is the
in-process `tests/test_c13_peers.py::test_the_caught_up_line_reads_the_records_the_upstream_reports`; variant 7b
("Look it up") is not built; a real 1.1 follower is `python -m tools.skew_drill --scenario records`.
"""

from __future__ import annotations

import json
import os
import re

import pytest
from sqlalchemy import select

from atlas import custom, facets, feed, service
from atlas.contract import ObservationBatchV1
from atlas.models import Canonical, ChangeLog, Peer, ReplicatedFeed, SyncFailure
from tools.federation_lab import Lab
from tools.multinode_drill import csrf
from tools.skew_drill import _current_cli

pytestmark = pytest.mark.federation_lab


@pytest.fixture
def network(request):
    """`official` (U), `indie` (whose feed U retains, empty) and `local` (N), started with no peers pinned. A test
    marked `@pytest.mark.parametrize("env", ...)` passes extra settings for U."""
    env = getattr(request, "param", None) or {}
    with Lab(postgres_admin_url=os.environ.get("ATLAS_LAB_PG_URL") or None, env={"official": env}) as lab:
        lab.start("official", "indie", "local")
        yield lab


def _provider_only(node, title: str, steam: str) -> str:
    body = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "metadata", "source": "steam", "source_scope": "federation-lab"},
        "observations": [{"external_id": steam, "title": title, "provider_ids": {"steam": steam}}]})
    with node.state.maker() as session:
        [result] = service.ingest(session, node.state.ctx, body, system=True)
        session.commit()
    return result.canonical_id


#: What `_sidecars` appends to an authority's feed. The schema entry is #78 (f07c442): an authority publishes its
#: seeded multiplayer definitions once, just before its first signed multiplayer value (`players_max` here), so a
#: mirror does not quarantine that value. It is not about S, so it never waits for S on a follower. The §10.2 scenarios
#: were written a day before #78 and expected four entries; `tests/test_lab_feed_shape.py` pins this tuple in the
#: default test run, so the opt-in lab cannot drift from the feed again unnoticed.
SIDECAR_KINDS = ("artwork", "achievements", "schema", "custom", "achievements")


def _sidecars(node, cid: str) -> None:
    """The seq 6-9 analogue of §10.2 (6-10 since #78, see SIDECAR_KINDS): artwork, achievements, custom fields and
    achievements again, by a curator."""
    with node.state.maker() as session:
        ctx = node.state.ctx
        facets.set_artwork(session, ctx, cid, "curator", [{"kind": "key_art", "url": "https://img.example.org/s.png"}],
                           redistributable=True)
        facets.set_achievements(session, ctx, cid, "curator", [{"name": "First Dive"}], redistributable=True)
        custom.set_values(session, ctx, cid, "curator", {"players_max": 4}, redistributable=True)
        facets.set_achievements(session, ctx, cid, "curator", [{"name": "First Dive"}, {"name": "Deep Dive"}],
                                redistributable=True)
        session.commit()


def _kinds(node, after: int = 0) -> list[tuple[int, str]]:
    with node.state.maker() as session:
        return [(r.seq, r.kind) for r in session.scalars(select(ChangeLog).where(ChangeLog.seq > after)
                                                         .order_by(ChangeLog.seq))]


def _open(node) -> list[tuple[int, str, str, str | None]]:
    with node.state.maker() as session:
        return [(f.seq, f.kind, f.disposition, f.waiting_for) for f in session.scalars(
            select(SyncFailure).where(SyncFailure.resolved_at.is_(None)).order_by(SyncFailure.seq))]


def _follow_own(follower, upstream) -> tuple[str, str]:
    """Look `upstream` up on `follower`'s Admin → Peers and follow its own feed (both acknowledgements: the lab is
    plain http). Returns the peer id and the probe page."""
    form = {"csrf": csrf(follower.admin, "/admin/peers"), "url": upstream.url, "token": upstream.follower,
            "name": "Atlas Central"}
    probe = follower.admin.post("/admin/peers/probe", data=form)
    assert probe.status_code == 200, probe.text[:300]
    block = re.search(r'<form[^>]*data-feed-option="own".*?</form>', probe.text, re.S).group(0)
    fields = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', block))
    fields.update(ack_relayed_fingerprint="on")
    added = follower.admin.post("/admin/peers", data=fields)
    assert added.status_code == 303, added.text[:300]
    with follower.state.maker() as session:
        peer = session.scalars(select(Peer).where(Peer.node_id == upstream.node_id)).one()
    return peer.id, probe.text


def _woken(node, upstream, trigger: str) -> int:
    metrics = node.client.get("/metrics", headers=node.headers(node.scraper)).text
    found = re.search(rf'atlas_sync_woken_total{{node_id="{upstream.node_id}",trigger="{trigger}"}} (\d+)', metrics)
    return int(found.group(1)) if found else 0


def test_contract_shape_waits_then_recovers_with_introduce_all(network):
    official, indie, local = network.nodes["official"], network.nodes["indie"], network.nodes["local"]
    # Step 1: U in legacy emission, seeded to the live shape.
    curated = [official.post(f"Lab Curated {n}") for n in range(5)]
    s = _provider_only(official, "Subnautica 2", "1962700")
    _sidecars(official, s)
    q = _provider_only(official, "Quiet Game", "1962701")
    with official.state.maker() as session:  # F: U retains indie's feed, which holds nothing
        session.add(ReplicatedFeed(node_id=indie.node_id, public_key=indie.root_key))
        session.commit()
    assert _kinds(official) == [(n, "obs") for n in range(1, 6)] + list(enumerate(SIDECAR_KINDS, start=6))
    waiting = [(6, "artwork"), (7, "achievements"), (9, "custom"), (10, "achievements")]  # 8 is the schema
    info = official.client.get("/api/v2/sync/info", headers=official.headers()).json()
    assert info["records"] == {"live": 7, "introduced": 5, "not_federated": 2}
    assert info["introduced_through"] is None

    # Step 2: N looks U up. The API probe and the form agree: U's own feed first and recommended; F empty and smaller.
    probe = local.client.get("/api/v2/admin/peers/probe", params={"url": official.url},
                             headers={**local.headers(local.admin_token), "Atlas-Peer-Token": official.follower})
    assert probe.status_code == 200, probe.text
    feeds = probe.json()["feeds"]
    assert [(f["origin"], f["node_id"], f["head"], f["recommended"]) for f in feeds] == [
        ("own", official.node_id, 10, True), ("relayed", indie.node_id, 0, False)]
    assert feeds[1]["warnings"] == ["empty_feed", "smaller_than_own"]
    assert probe.json()["records"] == {"live": 7, "introduced": 5, "not_federated": 2}
    page = local.admin.post("/admin/peers/probe", data={"csrf": csrf(local.admin, "/admin/peers"),
                                                        "url": official.url, "token": official.follower}).text
    block = re.search(r'<form[^>]*data-feed-option="relayed".*?</form>', page, re.S).group(0)
    relayed = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', block))
    assert local.admin.post("/admin/peers", data={**relayed, "ack_relayed_fingerprint": "on"}).status_code == 400
    peer_id, _ = _follow_own(local, official)

    # Step 3: adding the peer pulled the first page. Caught up, four changes waiting for their game.
    assert _open(local) == [(seq, kind, "wait", s) for seq, kind in waiting]  # the schema entry applied at once
    status = local.sync_status(official)
    assert status["state"] == "waiting" and status["applied_head"] == 10
    peers_page = local.admin.get("/admin/peers").text
    assert "Caught up · 4 changes waiting for their game" in peers_page
    assert re.search(r"Atlas Central has 2 records it does not publish", peers_page)
    assert "waiting for its game" in peers_page.lower()
    for wrong in ("degraded", "apply error", "unknown or merged canonical"):
        assert wrong not in peers_page.lower(), wrong
    group = re.search(rf'<div class="card waiting-group" data-waiting-for="{s}"[^>]*>.*?</div>', peers_page, re.S)
    assert group is not None and 'data-waiting-subreason="upstream_unpublished"' in group.group(0)
    assert "achievements ×2, artwork, custom · entries 6–10" in group.group(0)
    assert "data-waiting-title=\"none\"><strong>A game Atlas Central hasn't published yet" in group.group(0)
    api = local.client.get("/api/v2/sync/status", headers=local.headers(local.admin_token)).json()
    [summary] = api["peers"]
    assert summary["status"]["code"] == "caught_up_waiting"
    assert [(g["canonical_id"], g["subreason"], g["kinds"], g["seqs"]) for g in summary["waiting"]] == [
        (s, "upstream_unpublished", {"achievements": 2, "artwork": 1, "custom": 1}, [6, 10])]

    # Step 4: 25 more pulls change nothing.
    for _ in range(25):
        assert local.sync(peer_id) is None
    with local.state.maker() as session:
        rows = session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None))).all()
        assert sorted((r.seq, r.disposition, r.attempts) for r in rows) == [(n, "wait", 1) for n, _ in waiting]

    # Step 5: U raises its emit protocol and runs `atlas feed introduce --all`: a record for S and Q, nothing else.
    form = {"csrf": csrf(official.admin, "/admin/peers"), "emit_protocol": "1.5", "password": network.password,
            "followers_upgraded": "on"}
    assert official.admin.post("/admin/peers/protocol", data=form).status_code == 303
    done = _current_cli(official, "feed", "introduce", "--all")
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert json.loads(done.stdout)["finished"] is True
    assert [kind for _, kind in _kinds(official, after=10)] == ["record", "record"]
    with official.state.maker() as session:
        bodies = {r.body["canonical_id"] for r in session.scalars(select(ChangeLog).where(ChangeLog.seq > 10))}
        assert bodies == {s, q}
        assert feed.introduced_through(session) == official.head() == 12

    # Step 6: one pull on N applies both records and the four waiting entries, in feed order, with no manual retry.
    assert local.sync(peer_id) is None
    assert _open(local) == []
    with local.state.maker() as session:
        assert session.get(Canonical, s).view["title"] == "Subnautica 2"
        assert session.get(Canonical, q).view["title"] == "Quiet Game"
        assert all(session.get(Canonical, c) is not None for c in curated)
    status = local.sync_status(official)
    assert status["state"] == "in_sync" and status["applied_head"] == 12
    peers_page = local.admin.get("/admin/peers").text
    assert 'data-peer-status="caught_up"' in peers_page and "7 of 7 records published" in peers_page
    assert _woken(local, official, "record") == 1


@pytest.mark.parametrize("network", [{"ATLAS_FEDERATION_BRIDGE_RECORD_OBS": "true",
                                      "ATLAS_MIN_FOLLOWER_PROTOCOL": "1.1"}], indirect=True)
def test_bridge_variants_introduce_with_observations_older_followers_apply(network):
    """§10.2 variants 7a and 7a': with the bridge on, U introduces S and Q with 1.0 observations (emit stays legacy);
    after U raises its emit protocol to 1.5 while it still serves 1.1 followers, a new record is introduced with the
    bridge too, never with `record`, so a 1.1 follower would not park anything. N converges with nothing open."""
    official, local = network.nodes["official"], network.nodes["local"]
    official.post("Lab Curated")
    s = _provider_only(official, "Subnautica 2", "1962700")
    _sidecars(official, s)
    q = _provider_only(official, "Quiet Game", "1962701")
    assert [kind for _, kind in _kinds(official, after=1)] == list(SIDECAR_KINDS)
    peer_id, _ = _follow_own(local, official)
    assert [row[1] for row in _open(local)] == ["artwork", "achievements", "custom", "achievements"]

    # 7a: the bridge, emit unchanged
    done = _current_cli(official, "feed", "introduce", "--all")
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert [kind for _, kind in _kinds(official, after=1 + len(SIDECAR_KINDS))] == ["obs", "obs"]
    assert local.sync(peer_id) is None
    assert _open(local) == []
    with local.state.maker() as session:
        assert session.get(Canonical, s) is not None and session.get(Canonical, q) is not None
    assert _woken(local, official, "bridge") == 1

    # 7a': emit 1.5 while min_follower_protocol is 1.1: still the bridge
    form = {"csrf": csrf(official.admin, "/admin/peers"), "emit_protocol": "1.5", "password": network.password,
            "followers_upgraded": "on"}
    assert official.admin.post("/admin/peers/protocol", data=form).status_code == 303
    head = official.head()
    r = _provider_only(official, "Raft 2", "1962702")
    response = official.client.put(f"/api/v2/games/{r}/artwork", headers=official.headers(official.admin_token),
                                   json={"items": [{"kind": "key_art", "url": "https://img.example.org/r.png"}]})
    assert response.status_code == 200, response.text
    assert [kind for _, kind in _kinds(official, after=head)] == ["obs", "artwork"]
    with official.state.maker() as session:
        assert session.scalars(select(ChangeLog).where(ChangeLog.kind == "record")).first() is None
    assert local.sync(peer_id) is None
    assert _open(local) == []
    assert local.sync_status(official)["state"] == "in_sync"
