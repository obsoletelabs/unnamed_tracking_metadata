"""CONTRACT-C13 §7 (A19): adding a peer asks which feed to follow, and its status says what is really going on.

The live failure: an operator pointed a follower at a mirror. The mirror's own feed is empty (it signs nothing);
everything is in the authority feed it relays. The previous release inferred the relayed feed only when the
operator happened to pin the authority's key. Real apps on SQLite files; the follower's outbound HTTP lands on the
authority and mirror apps in-process. Assertions are on the follower's `peer` rows, audit entries and records."""

from __future__ import annotations

import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import main, secrets_box, sync
from atlas.models import AuditEntry, Canonical, Peer

from .conftest import build_settings
from .test_sync_compat import Node, pull
from .test_web import signup, token


def routed(nodes: dict[str, Node]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        target = nodes[request.url.host]
        r = target.client.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers),
                                  content=request.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get(
            "content-type", "application/json")})
    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def world(tmp_path):
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    cid = authority.publish("Hades")
    pull(mirror, authority)  # the mirror relays the authority feed; its own feed is empty
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/u.db", role="local",
                                         media_cache_enabled=False, secret_key="k" * 32))
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as user:
        app.state.atlas.http = routed({"central.test": authority, "mirror.test": mirror})
        user.st, user.st_app = app.state.atlas, app
        signup(user, "alice")  # the first account administers the node
        yield authority, mirror, user, cid


def form_fields(html: str, option: str) -> dict[str, str]:
    block = re.search(rf'<form[^>]*data-feed-option="{option}".*?</form>', html, re.S).group(0)
    return dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', block))


def peers(user) -> list[Peer]:
    with user.st.maker() as s:
        return list(s.scalars(select(Peer).order_by(Peer.created_at)))


def test_probing_a_mirror_offers_its_own_empty_feed_and_recommends_the_relayed_authority(world):
    """T-P1/T-P2: the choose-feed screen lists the mirror's own feed (empty) and the authority feed it relays, with
    the relayed one recommended; choosing it pins the authority's key, the relay and the mode."""
    authority, mirror, user, cid = world
    t = token(user, "/admin/peers")
    probe = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": mirror.token,
                                                  "name": "Mirror"})
    assert probe.status_code == 200 and "Confirm this node" in probe.text
    html = probe.text
    own, relayed = form_fields(html, "own"), form_fields(html, "relayed")
    assert own["node_id"] == mirror.node_id and relayed["node_id"] == authority.node_id
    assert relayed["via_node_id"] == mirror.node_id and own["via_node_id"] == ""
    assert re.search(r'data-feed-option="relayed"[^>]*>.*?recommended', html, re.S)
    assert "has signed nothing of its own" in html

    relayed["csrf"] = t
    add = user.post("/admin/peers", data=relayed)
    assert add.status_code == 303
    [peer] = peers(user)
    assert (peer.node_id, peer.follow, peer.via_node_id, peer.url) == (authority.node_id, "relayed", mirror.node_id,
                                                                       "http://mirror.test")
    synced = user.post(f"/admin/peers/{peer.id}/sync", data={"csrf": t})
    assert synced.status_code == 200 and "Synced 1 entries, 1 applied." in synced.text
    with user.st.maker() as s:
        assert s.get(Canonical, cid).view["title"] == "Hades"
        audit = s.scalars(select(AuditEntry).where(AuditEntry.action == "peer_add")).one()
        assert audit.detail["follow"] == "relayed" and audit.detail["via"] == mirror.node_id
    page = user.get("/admin/peers").text
    assert 'data-peer-status="caught_up"' in page and "an authority feed it relays" in page


def test_a_tampered_choice_is_refused(world):
    """T-P3: the probe token binds the mode and the feed to what the probe offered, in this session."""
    authority, mirror, user, _ = world
    t = token(user, "/admin/peers")
    html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": mirror.token}).text
    relayed = {**form_fields(html, "relayed"), "csrf": t}
    for change in ({"follow": "own"}, {"via_node_id": ""}, {"url": "http://central.test"},
                   {"probe_token": "0" * 64}):
        r = user.post("/admin/peers", data={**relayed, **change})
        assert r.status_code == 400, change
    assert peers(user) == []


def test_following_a_mirrors_own_feed_reports_empty_and_never_falls_back(world):
    """T-P4: `own` follows the answering node's own feed and nothing else; an empty one says so."""
    authority, mirror, user, _ = world
    t = token(user, "/admin/peers")
    html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": mirror.token}).text
    user.post("/admin/peers", data={**form_fields(html, "own"), "csrf": t})
    [peer] = peers(user)
    assert peer.follow == "own" and peer.node_id == mirror.node_id
    note = user.post(f"/admin/peers/{peer.id}/sync", data={"csrf": t})
    assert "Sync failed" not in note.text
    page = user.get("/admin/peers").text
    assert 'data-peer-status="empty"' in page and "This feed holds nothing yet." in page
    with user.st.maker() as s:  # nothing of the authority's was taken through the mirror
        assert s.scalars(select(Canonical)).first() is None

    with user.st.maker() as s:  # an `own` peer pinned to a key the node no longer answers with
        s.add(Peer(name="moved", url="http://mirror.test", node_id=authority.node_id,
                   public_key=authority.st.ctx.identity.trust_public_b64, follow="own"))
        s.commit()
    with pytest.raises(sync.SyncError, match="now answers as"):
        sync.pull_once(user.st.maker, user.st.ctx, user.st.http, "http://mirror.test", mirror.token,
                       {authority.node_id: authority.st.ctx.identity.trust_public_b64}, follow="own")


def test_a_peer_added_before_a19_is_inferred_but_not_rewritten_until_confirmed(world):
    """T-P5: a peer without a mode syncs as before (relayed inferred from the pinned key); what was inferred is
    recorded for the operator, shown in a banner, and only the operator's confirmation writes it (audited)."""
    authority, mirror, user, cid = world
    with user.st.maker() as s:
        s.add(Peer(name="legacy", url="http://mirror.test", node_id=authority.node_id,
                   public_key=authority.st.ctx.identity.trust_public_b64,
                   token_enc=secrets_box.encrypt(user.st.ctx.settings, mirror.token)))
        s.commit()
    results = sync.sync_peers(user.st.maker, user.st.ctx, user.st.http)
    assert results["legacy"].startswith("ok")
    [peer] = peers(user)
    assert peer.follow is None and peer.via_node_id is None  # not written back
    with user.st.maker() as s:
        assert sync.inferred_follow(s, peer.id)["follow"] == "relayed"
        assert sync.inferred_follow(s, peer.id)["via"] == mirror.node_id
        assert s.get(Canonical, cid) is not None
    page = user.get("/admin/peers").text
    assert 'data-banner="confirm-follow"' in page and "Confirm relayed feed" in page
    t = token(user, "/admin/peers")
    assert user.post(f"/admin/peers/{peer.id}/follow", data={"csrf": t, "follow": "relayed"}).status_code == 303
    [peer] = peers(user)
    assert (peer.follow, peer.via_node_id) == ("relayed", mirror.node_id)
    with user.st.maker() as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "peer_follow")).one().detail["inferred"] \
            == "relayed"
    assert 'data-banner="confirm-follow"' not in user.get("/admin/peers").text
    assert user.post(f"/admin/peers/{peer.id}/follow", data={"csrf": t, "follow": "both"}).status_code == 400


def test_sync_status_lists_peers_with_their_status(world):
    """T-P6: `/api/v2/sync/status` gains `peers[]` (additive), each with the same status the page shows."""
    authority, mirror, user, _ = world
    with user.st.maker() as s:
        s.add(Peer(name="off", url="http://mirror.test", node_id=authority.node_id,
                   public_key=authority.st.ctx.identity.trust_public_b64, enabled=False, follow="relayed"))
        s.commit()
        assert [(p["name"], p["status"], p["follow"]) for p in sync.status(s, user.st.ctx)["peers"]] == [
            ("off", "paused", "relayed")]
    statuses = {sync.peer_status(Peer(enabled=True, last_ok_at=None, last_error=None), None),
                sync.peer_status(Peer(enabled=True, last_ok_at=None, last_error="ConnectError: refused"), None)}
    assert statuses == {"not_contacted", "unreachable"}
