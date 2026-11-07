"""CONTRACT-C13 (A19) release-blocking regression: the live failure, on the real five-node network.

What was observed live: an authority holds a record whose only observation is a provider's (a Steam fetch, not
redistributable), so its feed never signed an `obs` for it. A curator then set artwork on it, and the signed
`artwork` entry reached every follower naming a record they had never been told about. They quarantined it
("unknown or merged canonical"), retried it a fixed number of times and gave up.

Now the follower records the entry as *waiting for its record* (not a failure, no attempts counted), moves on, and
reports `waiting`. Once the authority raises its emit protocol to 1.5 (Admin → Peers, which schedules the
introduction) and `atlas feed introduce --missing` signs a `record` for it, the next sync applies the record and the
waiting artwork on the mirror and, through the mirror's relay, on the plain nodes. Everything is asserted on the
nodes' databases.

The same failure against real 1.0 and 1.1 followers: `python -m tools.skew_drill --scenario records`.

This covers C13 §10.2 on the wired network (relays included). The contract's own shape (upstream U with a relayed
empty feed F, the `records` 7/5/2 counts, `introduce --all` with a silent record Q, `atlas_sync_woken_total`) and
variants 7a and 7a' are `test_c13_contract_shape.py`; variant 3a is in-process (`tests/test_c13_peers.py`); variant
7b ("Look it up") is an open owner decision and not built.
"""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import select

from atlas import feed, service, sync
from atlas.contract import ObservationBatchV1
from atlas.models import Artwork, Canonical, NodeState, SyncFailure
from tools.multinode_drill import csrf
from tools.skew_drill import _current_cli

pytestmark = pytest.mark.federation_lab

ART = [{"kind": "key_art", "url": "https://img.example.org/lab-key.png"},
       {"kind": "screenshot", "url": "https://img.example.org/lab-shot.png"}]


def _provider_only(node, title: str, steam: str) -> str:
    body = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "metadata", "source": "steam", "source_scope": "federation-lab"},
        "observations": [{"external_id": steam, "title": title, "provider_ids": {"steam": steam}}]})
    with node.state.maker() as session:
        [result] = service.ingest(session, node.state.ctx, body, system=True)
        session.commit()
    return result.canonical_id


def _art(node, cid: str) -> list[tuple[str, str]]:
    with node.state.maker() as session:
        return sorted((a.kind, a.url) for a in session.scalars(select(Artwork).where(Artwork.canonical_id == cid)))


def _open(node, origin) -> list[tuple[int, str, str, str | None, str | None]]:
    with node.state.maker() as session:
        return [(f.seq, f.kind, f.disposition, f.reason, f.waiting_for) for f in session.scalars(
            select(SyncFailure).where(SyncFailure.node_id == origin.node_id, SyncFailure.resolved_at.is_(None))
            .order_by(SyncFailure.seq))]


def test_sidecars_for_unintroduced_record_wait_for_their_game_and_recover(lab):
    official = lab.nodes["official"]
    mirror, local, local2 = lab.nodes["mirror"], lab.nodes["local"], lab.nodes["local2"]
    first = official.post("Lab C13 Before")
    cid = _provider_only(official, "Lab C13 Provider Only", "7770001")
    response = official.client.put(f"/api/v2/games/{cid}/artwork", headers=official.headers(official.admin_token),
                                   json={"items": ART})
    assert response.status_code == 200, response.text
    art_seq = official.head()
    later = official.post("Lab C13 After")
    assert [kind for _, (kind, _, _) in sorted(official.own_feed().items())] == ["obs", "artwork", "obs"]

    # The live failure shape, now waiting instead of quarantined: nothing blocks, nothing is lost.
    for follower, upstream in ((mirror, "official"), (local, "official")):
        assert lab.sync(follower.name, upstream) is None
        assert _open(follower, official) == [(art_seq, "artwork", "wait", "waiting_for_record", cid)]
        assert follower.cursor(official) == official.head()
        status = follower.sync_status(official)
        assert status["state"] == "waiting" and status["waiting"]["count"] == 1, status
        assert status["quarantined"]["count"] == 0
        assert _art(follower, cid) == []
        with follower.state.maker() as session:
            assert session.get(Canonical, cid) is None
            assert session.get(Canonical, first) is not None and session.get(Canonical, later) is not None
            row = session.get(SyncFailure, (official.node_id, art_seq))
            assert row.attempts == 1  # waiting is not failing
    info = official.client.get("/api/v2/sync/info", headers=official.headers()).json()
    assert info["records"]["not_federated"] == 1 and info["introduced_through"] is None

    # What the operator sees (§10.2 step 3): a caught-up peer with one change waiting for its game, never "degraded".
    page = mirror.admin.get("/admin/peers").text
    assert re.search(r'data-peer-status="caught_up_waiting"', page)
    assert "waiting for its game" in page.lower()
    for wrong in ("degraded", "apply error", "unknown or merged canonical"):
        assert wrong not in page.lower(), wrong
    with mirror.state.maker() as session:
        [group] = sync.waiting_groups(session, official.node_id)
        assert (group["subreason"], group["canonical_id"], group["kinds"]) == (
            "upstream_unpublished", cid, {"artwork": 1})

    # Step 4: more pulls change nothing: still waiting, no attempts counted.
    for _ in range(25):
        assert lab.sync("mirror", "official") is None
    with mirror.state.maker() as session:
        row = session.get(SyncFailure, (official.node_id, art_seq))
        assert (row.disposition, row.attempts, row.resolved_at) == ("wait", 1, None)

    # Step 2 on the lab's shape: probing the mirror lists its own (empty) feed first, flagged and not recommended;
    # following it without the acknowledgement answers 400 and pins nothing.
    probe = local2.admin.post("/admin/peers/probe", data={"csrf": csrf(local2.admin, "/admin/peers"),
                                                         "url": mirror.url, "token": mirror.follower})
    assert probe.status_code == 200
    own_tag = re.search(r'<form[^>]*data-feed-option="own"[^>]*>', probe.text).group(0)
    assert "empty_feed" in own_tag and probe.text.index('data-feed-option="own"') < probe.text.index(
        'data-feed-option="relayed"')
    own_head = re.search(r'<form[^>]*data-feed-option="own".*?</h3>', probe.text, re.S).group(0)
    assert "recommended" not in own_head  # (local2 already follows the relayed feeds, so none is offered here)
    own = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"',
                          re.search(r'<form[^>]*data-feed-option="own".*?</form>', probe.text, re.S).group(0)))
    refused = local2.admin.post("/admin/peers", data={**own, "ack_relayed_fingerprint": "on"})
    assert refused.status_code == 400

    # The authority raises its emit protocol to 1.5 in Admin → Peers: that schedules the introduction.
    form = {"csrf": csrf(official.admin, "/admin/peers"), "emit_protocol": "1.5", "password": lab.password,
            "followers_upgraded": "on"}
    assert official.admin.post("/admin/peers/protocol", data=form).status_code == 303
    with official.state.maker() as session:
        assert session.get(NodeState, feed.INTRODUCE_PENDING).value == "missing"
    done = _current_cli(official, "feed", "introduce", "--missing")
    assert done.returncode == 0, (done.stdout, done.stderr)
    result = json.loads(done.stdout)
    assert (result["introduced"], result["finished"], result["left"]) == (1, True, 0), result
    assert official.own_feed()[official.head()][0] == "record"
    with official.state.maker() as session:
        assert session.get(NodeState, feed.INTRODUCE_PENDING) is None
        assert feed.introduced_through(session) == official.head()

    # The next sync applies the record and the waiting artwork everywhere, the plain nodes through the relay.
    lab.sync_all()
    for follower in (mirror, local, local2):
        assert _open(follower, official) == [], follower.name
        assert _art(follower, cid) == sorted((a["kind"], a["url"]) for a in ART), follower.name
        assert follower.sync_status(official)["state"] == "in_sync", follower.name
        with follower.state.maker() as session:
            assert session.get(Canonical, cid).view["title"] == "Lab C13 Provider Only"
            row = session.get(SyncFailure, (official.node_id, art_seq))
            assert row is None or row.resolved_at is not None
    # Converged on everything the feed carries. The provider-only record's view is the exception by design: the
    # authority's view also holds the Steam data it may not redistribute; the followers hold the record's identity.
    published = official.observations()
    others = {v[0] for v in published.values()} - {cid}
    for follower in (mirror, local, local2):
        assert follower.observations(official) == published, follower.name
        assert follower.canonicals(others) == official.canonicals(others), follower.name
        assert follower.retained(official) == official.own_feed(), follower.name
        assert follower.cursor(official) == official.head(), follower.name
