"""CONTRACT-C13 §7 (A19): adding a peer asks which feed to follow, and its status says what is really going on.

The live failure: an operator pointed a follower at a mirror. The mirror's own feed is empty (it signs nothing);
everything is in the authority feed it relays. The previous release inferred the relayed feed only when the
operator happened to pin the authority's key. Real apps on SQLite files; the follower's outbound HTTP lands on the
authority and mirror apps in-process. Assertions are on the follower's `peer` rows, audit entries and records."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import main, secrets_box, sync
from atlas.models import AuditEntry, Canonical, Peer, ReplicatedFeed
from atlas.web import federation

from .conftest import build_settings
from .test_sync_compat import Node, pull
from .test_web import signup, token


def routed(nodes: dict[str, Node], strip: tuple[str, ...] = ()) -> httpx.Client:
    """Outbound HTTP lands on the apps in-process. `strip` removes A19's additive `/sync/info` keys, standing in
    for an upstream of an older release (1.0-1.4 send no `own_head`)."""
    def handle(request: httpx.Request) -> httpx.Response:
        target = nodes[request.url.host]
        r = target.client.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers),
                                  content=request.content)
        content = r.content
        if strip and request.url.path.endswith("/sync/info") and r.status_code == 200:
            content = json.dumps({k: v for k, v in r.json().items() if k not in strip}).encode()
        return httpx.Response(r.status_code, content=content, headers={"content-type": r.headers.get(
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


def option_tag(html: str, option: str) -> str:
    return re.search(rf'<form[^>]*data-feed-option="{option}"[^>]*>', html).group(0)


ACKS = {"ack_relayed_fingerprint": "on", "ack_empty": "on"}


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
    assert html.index('data-feed-option="own"') < html.index('data-feed-option="relayed"')  # own feed first
    assert "empty_feed" in option_tag(html, "own") and "own_feed_empty_but_relayed_has_data" in option_tag(html, "own")
    assert "⚠ Empty: following this receives nothing" in html
    assert own["empty"] == "1" and relayed["empty"] == ""

    relayed["csrf"] = t
    unacked = user.post("/admin/peers", data=relayed)  # a relayed feed: compare its fingerprint first
    assert unacked.status_code == 400 and "compared the fingerprint" in unacked.text
    assert peers(user) == []
    add = user.post("/admin/peers", data={**relayed, "ack_relayed_fingerprint": "on"})
    assert add.status_code == 303
    [peer] = peers(user)
    assert (peer.node_id, peer.follow, peer.via_node_id, peer.url) == (authority.node_id, "relayed", mirror.node_id,
                                                                       "http://mirror.test")
    # C13 §7.3 "Then": one page is pulled right away, so the operator sees the real badge without "Sync now"
    assert add.headers["location"] == f"/admin/peers?m=peer_following#peer-{peer.id}"
    assert peer.last_ok_at is not None and peer.last_error is None
    landed = user.get(add.headers["location"].split("#")[0]).text
    assert "Peer added and synced once" in landed and 'data-peer-status="caught_up"' in landed
    synced = user.post(f"/admin/peers/{peer.id}/sync", data={"csrf": t})
    assert synced.status_code == 200 and "Sync failed" not in synced.text
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
    expired = federation.probe_token(SimpleNamespace(csrf=t), relayed["url"], relayed["node_id"],
                                     relayed["public_key"], "relayed", relayed["via_node_id"], False, 1)
    other_session = federation.probe_token(SimpleNamespace(csrf="another-session"), relayed["url"],
                                           relayed["node_id"], relayed["public_key"], "relayed",
                                           relayed["via_node_id"], False)
    for change in ({"follow": "own"}, {"via_node_id": ""}, {"url": "http://central.test"},
                   {"probe_token": "0" * 64}, {"empty": "1"}, {"public_key": form_fields(html, "own")["public_key"]},
                   {"probe_token": expired}, {"probe_token": other_session}):
        r = user.post("/admin/peers", data={**relayed, **ACKS, **change})
        assert r.status_code == 400, change
    assert peers(user) == []


def test_add_peer_requires_explicit_choice_and_acks(world):
    """T-P2: no choice, no probe token, or a missing acknowledgement answers 400; nothing is pinned from form
    fields alone (the legacy add form without `follow` is gone from the admin route)."""
    authority, mirror, user, _ = world
    t = token(user, "/admin/peers")
    html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": mirror.token}).text
    own = {**form_fields(html, "own"), "csrf": t}
    legacy = {k: own[k] for k in ("csrf", "url", "node_id", "public_key", "name", "token")}
    assert user.post("/admin/peers", data=legacy).status_code == 400  # no choice
    assert user.post("/admin/peers", data={**legacy, "follow": "own", **ACKS}).status_code == 400  # no probe token
    no_empty_ack = user.post("/admin/peers", data={**own, "ack_relayed_fingerprint": "on"})
    assert no_empty_ack.status_code == 400 and "no entries yet" in no_empty_ack.text
    no_fp_ack = user.post("/admin/peers", data={**own, "ack_empty": "on"})  # plain http: the own feed needs it too
    assert no_fp_ack.status_code == 400 and "compared the fingerprint" in no_fp_ack.text
    assert peers(user) == []
    assert user.post("/admin/peers", data={**own, **ACKS}).status_code == 303
    [peer] = peers(user)
    assert (peer.follow, peer.node_id) == ("own", mirror.node_id)
    with user.st.maker() as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "peer_add")).one().detail["empty"] is True


def test_probe_always_checks_sync_scope(world):
    """T-P10: the probe always calls `/sync/changes`. A token without the sync scope is named on the screen and
    every follow button is disabled."""
    authority, mirror, user, _ = world
    with mirror.st.maker() as s:
        from atlas import auth

        _row, read_only = auth.create_token(s, "read-only", ["read"])
        s.commit()
    t = token(user, "/admin/peers")
    html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": read_only}).text
    assert 'data-probe="no-sync-scope"' in html and "needs the <em>sync</em> scope" in html
    for option in ("own", "relayed"):
        block = re.search(rf'<form[^>]*data-feed-option="{option}".*?</form>', html, re.S).group(0)
        assert re.search(r"<button[^>]*disabled", block)
    full = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": mirror.token}).text
    assert 'data-probe="no-sync-scope"' not in full


def test_key_mismatch_option_disabled(world, tmp_path):
    """T-P11: a feed whose key differs from the one this node already retains for that node id is offered
    disabled, never re-pinned from the probe, and refused by the add route even with a valid token."""
    authority, mirror, user, _ = world
    other = Node(tmp_path, "other", "authority")
    with user.st.maker() as s:
        s.add(ReplicatedFeed(node_id=authority.node_id, public_key=other.st.ctx.identity.trust_public_b64))
        s.commit()
    t = token(user, "/admin/peers")
    html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": mirror.token}).text
    tag = option_tag(html, "relayed")
    assert 'data-feed-trust="key_mismatch"' in tag
    block = re.search(r'<form[^>]*data-feed-option="relayed".*?</form>', html, re.S).group(0)
    assert "<button" not in block and "differs from the one you already trust" in block
    r = user.post("/admin/peers", data={**form_fields(html, "relayed"), "csrf": t, **ACKS})
    assert r.status_code == 409 and peers(user) == []


def test_an_older_upstream_that_relays_another_feed_is_followed_for_its_own_catalogue(tmp_path):
    """C13 §7.1 / T-P10: an authority of 1.0-1.4 sends no `own_head`. The probe learns it from `/sync/changes` and
    recommends the authority's own catalogue, not a non-empty copy of another authority it relays (R1)."""
    central, second = Node(tmp_path, "central", "authority"), Node(tmp_path, "second", "authority")
    central.publish("Hades")
    second.publish("Celeste")
    second.publish("Tunic")
    pull(central, second)  # central relays second's feed (2 entries); its own feed holds 1
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/u.db", role="local",
                                         media_cache_enabled=False, secret_key="k" * 32))
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as user:
        app.state.atlas.http = routed({"central.test": central}, strip=("own_head", "introduced_through",
                                                                         "media_withheld", "records"))
        signup(user, "alice")
        t = token(user, "/admin/peers")
        html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://central.test",
                                                     "token": central.token}).text
        own_tag, relayed_tag = option_tag(html, "own"), option_tag(html, "relayed")
        assert "recommended" in re.search(r'<form[^>]*data-feed-option="own".*?</h3>', html, re.S).group(0)
        assert "recommended" not in re.search(r'<form[^>]*data-feed-option="relayed".*?</h3>', html, re.S).group(0)
        assert "head_unknown" not in own_tag and "empty_feed" not in own_tag and "smaller_than_own" not in relayed_tag
        assert "2 signed entries" in html and "3 signed entries" in html  # each feed starts with `wire_start`



def test_following_a_mirrors_own_feed_reports_empty_and_never_falls_back(world):
    """T-P4: `own` follows the answering node's own feed and nothing else; an empty one says so."""
    authority, mirror, user, _ = world
    t = token(user, "/admin/peers")
    html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": mirror.token}).text
    user.post("/admin/peers", data={**form_fields(html, "own"), "csrf": t, **ACKS})
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
        [summary] = sync.status(s, user.st.ctx)["peers"]
        assert (summary["name"], summary["status"]["code"], summary["follow"]) == ("off", "paused", "relayed")
        assert summary["status"] == {"code": "paused", "label": "Paused", "tone": "warn", "detail": None,
                                     "action": "resume"}
        assert summary["waiting"] == []
    statuses = {sync.peer_status(Peer(enabled=True, last_ok_at=None, last_error=None), None),
                sync.peer_status(Peer(enabled=True, last_ok_at=None, last_error="ConnectError: refused"), None)}
    assert statuses == {"not_contacted", "unreachable"}


def test_a_peer_that_does_not_answer_its_first_pull_is_saved_and_not_contacted(world):
    """C13 §7.3: the first pull runs within the probe timeout. When the node does not answer in time the peer is
    still saved, shows "Not contacted yet" (not an error), and the next sync tries again."""
    authority, mirror, user, _ = world
    t = token(user, "/admin/peers")
    html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://mirror.test", "token": mirror.token}).text
    relayed = {**form_fields(html, "relayed"), "csrf": t, **ACKS}

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)
    working, user.st.http = user.st.http, httpx.Client(transport=httpx.MockTransport(slow))
    add = user.post("/admin/peers", data=relayed)
    [peer] = peers(user)
    assert add.status_code == 303 and "m=peer_not_contacted" in add.headers["location"]
    assert (peer.last_ok_at, peer.last_error) == (None, None)
    page = user.get("/admin/peers?m=peer_not_contacted").text
    assert 'data-peer-status="not_contacted"' in page and "Not contacted yet" in page
    user.st.http = working
    assert user.post(f"/admin/peers/{peer.id}/sync", data={"csrf": t}).status_code == 200
    assert 'data-peer-status="caught_up"' in user.get("/admin/peers").text


def test_api_probe_lists_the_feeds_as_json(world):
    """C13 §7.1, the API variant: `GET /api/v2/admin/peers/probe?url=` (admin scope) returns what the choose-feed
    screen shows, as JSON. The peer token goes in a header, never the URL; nothing is pinned (`probe_token` null)."""
    from atlas import auth

    authority, mirror, user, _ = world
    with user.st.maker() as s:
        _row, admin = auth.create_token(s, "ops", ["admin"])
        _row, reader = auth.create_token(s, "reader", ["read"])
        s.commit()
    headers = {"Authorization": f"Bearer {admin}", "Atlas-Peer-Token": mirror.token}
    r = user.get("/api/v2/admin/peers/probe", params={"url": "http://mirror.test/"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == "http://mirror.test" and body["probe_token"] is None
    assert body["node"]["node_id"] == mirror.node_id and body["node"]["protocol_ok"] is True
    assert body["token"] == {"read": True, "sync": True}
    assert body["records"] is None  # a mirror signs no records of its own
    assert [(f["origin"], f["node_id"], f["head"], f["recommended"]) for f in body["feeds"]] == [
        ("own", mirror.node_id, 0, False), ("relayed", authority.node_id, 2, True)]  # wire_start + 1
    assert body["feeds"][0]["warnings"] == ["empty_feed", "own_feed_empty_but_relayed_has_data"]
    assert body["default_feed"] == authority.node_id and "probe_token" not in body["feeds"][0]
    assert peers(user) == []
    with mirror.st.maker() as s:
        _row, read_only = auth.create_token(s, "read-only", ["read"])
        s.commit()
    no_sync = user.get("/api/v2/admin/peers/probe", params={"url": "http://mirror.test"},
                       headers={"Authorization": f"Bearer {admin}", "Atlas-Peer-Token": read_only}).json()
    assert no_sync["token"] == {"read": True, "sync": False}  # the change feed refused that token (403)
    anonymous = user.get("/api/v2/admin/peers/probe", params={"url": "http://mirror.test"},
                         headers={"Authorization": f"Bearer {admin}"})
    assert anonymous.status_code == 502 and "401" in anonymous.text
    assert user.get("/api/v2/admin/peers/probe", params={"url": "ftp://x"},
                    headers={"Authorization": f"Bearer {admin}"}).status_code == 400
    assert user.get("/api/v2/admin/peers/probe", params={"url": "http://nowhere.test"},
                    headers={"Authorization": f"Bearer {admin}"}).status_code == 502
    assert user.get("/api/v2/admin/peers/probe", params={"url": "http://mirror.test"},
                    headers={"Authorization": f"Bearer {reader}"}).status_code == 403


def test_unreachable_means_three_intervals_without_success(world):
    """C13 §5.5: one failed attempt after a recent success keeps the feed's own status (with the error on the line
    under it); "Can't reach it" needs no success for 3 x the sync interval, or no success ever."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    heads = {"state": "in_sync", "verified_head": 5, "observed_head": 5, "applied_head": 5,
             "waiting": {"count": 0, "records": 0}, "quarantined": {"count": 0}}

    def peer(ok_ago: int | None) -> Peer:
        return Peer(name="Central", enabled=True, follow="own", node_id="n",
                    last_ok_at=None if ok_ago is None else now - timedelta(seconds=ok_ago),
                    last_error="ConnectError: connection refused")
    assert sync.peer_status(peer(60), heads, interval=60, now=now) == "caught_up"
    assert sync.peer_status(peer(179), heads, interval=60, now=now) == "caught_up"
    assert sync.peer_status(peer(180), heads, interval=60, now=now) == "unreachable"
    assert sync.peer_status(peer(None), None, interval=60, now=now) == "unreachable"
    recent = sync.status_object("caught_up", heads, peer=peer(60), interval=60, now=now)
    assert recent["detail"].startswith("Last attempt failed: ConnectError")
    down = sync.status_object("unreachable", heads, peer=peer(3 * 3600), interval=60, now=now)
    assert (down["label"], down["tone"], down["action"]) == ("Can't reach it", "bad", "check_address")
    assert down["detail"] == "Last success 3 h ago · ConnectError: connection refused"
    moved = Peer(name="Central", enabled=True, last_ok_at=now, last_error="SyncError: now answers as 9a1c")
    assert sync.peer_status(moved, heads, interval=60, now=now) == "needs_you"


def test_badges_say_what_is_going_on_in_numbers(world):
    """C13 §5.5: the badge texts name their counts; the caught-up line says how many records the upstream publishes
    and how many it keeps off its feed, or that an older upstream does not say (variant 3a)."""
    peer = Peer(name="Atlas Central", enabled=True, follow="own", node_id="n")
    base = {"state": "in_sync", "verified_head": 23, "observed_head": 23, "applied_head": 23,
            "waiting": {"count": 0, "records": 0}, "quarantined": {"count": 0}}
    copying = {**base, "state": "behind", "applied_head": 11}
    assert sync.status_label("catching_up", copying) == "Copying: 11 of 23"
    assert sync.status_detail("catching_up", copying, peer=peer) == "12 entries to go"
    waiting = {**base, "waiting": {"count": 4, "records": 1}}
    assert sync.status_label("caught_up_waiting", waiting) == "Caught up · 4 changes waiting for their game"
    line = sync.status_detail("caught_up_waiting", waiting, peer=peer,
                              records={"live": 7, "introduced": 5, "not_federated": 2})
    assert line == ("1 game hasn't arrived from Atlas Central yet · 5 of 7 records published · Atlas Central has 2 "
                    "records it does not publish")
    done = sync.status_object("caught_up", base, peer=peer, records={"live": 7, "introduced": 7, "not_federated": 0})
    assert (done["label"], done["tone"]) == ("Caught up", "ok") and "7 of 7 records published" in done["detail"]
    held = sync.status_object("caught_up", base, peer=peer, records={"live": 7, "introduced": 5, "not_federated": 2})
    assert held["tone"] == "warn"
    old = sync.status_object("caught_up", base, peer=peer, records=None)  # 3a: a 1.0 upstream sends no `records`
    assert "record count unknown" in old["detail"]
    assert sync.status_label("empty", base) == "Connected · nothing published"
    assert sync.status_label("needs_you", base) == "Blocked: needs you"


def test_the_caught_up_line_reads_the_records_the_upstream_reports(tmp_path):
    """C13 §3.4/§5.5 end to end: the follower remembers `/sync/info.records` of the feed's own node on each pull; an
    upstream that strips it (an older release, variant 3a) leaves the count unknown."""
    authority = Node(tmp_path, "authority", "authority")
    authority.publish("Hades")
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/u.db", role="local",
                                         media_cache_enabled=False, secret_key="k" * 32))
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as user:
        app.state.atlas.http = routed({"central.test": authority})
        signup(user, "alice")
        t = token(user, "/admin/peers")
        html = user.post("/admin/peers/probe", data={"csrf": t, "url": "http://central.test",
                                                     "token": authority.token, "name": "Central"}).text
        assert "1 of 1 published on its own feed" in html
        user.post("/admin/peers", data={**form_fields(html, "own"), "csrf": t, **ACKS})
        page = user.get("/admin/peers").text
        assert "Caught up" in page and "1 of 1 records published" in page
        app.state.atlas.http = routed({"central.test": authority}, strip=("records",))
        [peer] = peers(SimpleNamespace(st=app.state.atlas))
        user.post(f"/admin/peers/{peer.id}/sync", data={"csrf": t})
        assert "record count unknown" in user.get("/admin/peers").text
