import base64

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import main, secrets_box, service, sync
from atlas.models import Canonical, Peer
from atlas.service import Ctx
from atlas.signing import key_id_for

from .conftest import batch, build_settings
from .test_sync import curate, make_node  # noqa: F401
from .test_web import fresh, signup, site, token  # noqa: F401


def pub_b64(identity):
    return base64.b64encode(identity.private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


# ---- secrets box --------------------------------------------------------------------------------------

def test_secrets_box_round_trip_and_requires_a_key():
    s = build_settings(secret_key="s" * 32)
    enc = secrets_box.encrypt(s, "atl_token")
    assert enc != "atl_token" and secrets_box.decrypt(s, enc) == "atl_token"
    with pytest.raises(secrets_box.SecretsError, match="cannot be decrypted"):
        secrets_box.decrypt(build_settings(secret_key="other" * 8), enc)
    with pytest.raises(secrets_box.SecretsError, match="ATLAS_SECRET_KEY"):
        secrets_box.encrypt(build_settings(), "x")
    assert secrets_box.available(s) and not secrets_box.available(build_settings())


# ---- the sync loop over several peers -----------------------------------------------------------------

def upstream_transport(a_maker, a_id):
    from atlas import feed

    def handler(request: httpx.Request) -> httpx.Response:
        with a_maker() as s:
            if request.url.path.endswith("/sync/info"):
                return httpx.Response(200, json={"node_id": a_id.node_id, "public_key": pub_b64(a_id), "role": "authority"})
            rows = feed.read(s, int(request.url.params["since"]), int(request.url.params["limit"]))
            return httpx.Response(200, json={"entries": [
                {"seq": r.seq, "kind": r.kind, "body": r.body, "body_hash": r.body_hash, "signature": r.signature,
                 "key_id": r.key_id, "node_id": r.key_id} for r in rows]})
    return httpx.MockTransport(handler)


def test_sync_peers_replicates_from_db_peers_and_records_status():
    a_maker, a_id, a_set = make_node(role="authority", redistributable_sources={"curator", "manual"})
    a_ctx = Ctx(a_set, a_id)
    m_maker, m_id, _ = make_node()
    m_set = build_settings(role="local", secret_key="k" * 32)
    m_ctx = Ctx(m_set, m_id)
    curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "Hades", "release_date": "2020-09-17"}])
    with m_maker() as s:
        s.add(Peer(name="central", url="http://central.test", node_id=a_id.node_id, public_key=pub_b64(a_id),
                   token_enc=secrets_box.encrypt(m_set, "atl_x")))
        s.add(Peer(name="broken", url="http://broken.test", node_id="deadbeefdeadbeef", public_key="AAAA"))
        s.add(Peer(name="paused", url="http://paused.test", node_id="0123456789abcdef", public_key="AAAA", enabled=False))
        s.commit()
    seen = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "broken.test":
            return httpx.Response(500)
        return upstream_transport(a_maker, a_id).handle_request(request)

    client = httpx.Client(transport=httpx.MockTransport(route))
    results = sync.sync_peers(m_maker, m_ctx, client)
    assert results["central"] == "ok, 2 entries" and "HTTPStatusError" in results["broken"] and "paused" not in results
    assert ("central.test", "Bearer atl_x") in seen and all(h != "paused.test" for h, _ in seen)
    with m_maker() as s:
        assert s.scalars(select(Canonical)).one().view["title"] == "Hades"
        rows = {p.name: p for p in s.scalars(select(Peer))}
        assert rows["central"].last_ok_at is not None and rows["central"].last_error is None
        assert rows["broken"].last_error and rows["broken"].last_ok_at is None


def test_a_peer_whose_key_does_not_match_is_refused():
    a_maker, a_id, a_set = make_node(role="authority")
    m_maker, m_id, _ = make_node()
    m_ctx = Ctx(build_settings(role="local"), m_id)
    other_maker, other_id, _ = make_node()
    with m_maker() as s:  # pinned to the wrong key: whoever answers at this URL is not who the admin trusted
        s.add(Peer(name="imposter", url="http://x.test", node_id=a_id.node_id, public_key=pub_b64(other_id)))
        s.commit()
    client = httpx.Client(transport=upstream_transport(a_maker, a_id))
    out = sync.sync_peers(m_maker, m_ctx, client)
    assert "SyncError" in out["imposter"] and "does not match" in out["imposter"]


def test_a_peer_with_an_undecryptable_token_is_reported_not_crashing():
    m_maker, m_id, _ = make_node()
    m_ctx = Ctx(build_settings(role="local", secret_key="new" * 12), m_id)
    with m_maker() as s:
        s.add(Peer(name="old", url="http://x.test", node_id="abc", public_key="AAAA",
                   token_enc=secrets_box.encrypt(build_settings(secret_key="old" * 12), "t")))
        s.commit()
    out = sync.sync_peers(m_maker, m_ctx, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert "cannot be decrypted" in out["old"]


# ---- admin UI -----------------------------------------------------------------------------------------

@pytest.fixture
def pair_sites(tmp_path):
    """An authority and a user node, each a full app; the user node reaches the authority in-process."""
    a = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/a.db", role="authority",
                                       media_cache_enabled=False, bootstrap_admin_token="a" * 40))
    u = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/u.db", role="local",
                                       media_cache_enabled=False, secret_key="k" * 32))
    with TestClient(a, base_url="http://central.test") as ca, TestClient(u, base_url="http://localhost:8000",
                                                                       follow_redirects=False) as cu:
        u.state.atlas.http = ca  # the user node's outbound calls land on the authority app
        ca.headers["Authorization"] = "Bearer " + "a" * 40
        cu.st, cu.st_app, ca.st = u.state.atlas, u, a.state.atlas
        yield ca, cu


def test_admin_adds_a_peer_after_confirming_its_identity_and_syncs(pair_sites):
    central, node = pair_sites
    with central.st.maker() as s:
        [r] = service.ingest(s, central.st.ctx, batch("curator", [{"external_id": "c1", "title": "Hades",
                             "release_date": "2020-09-17"}], kind="metadata"), curator=True)
        s.commit()
    tok = central.post("/api/v2/tokens", json={"name": "mirror", "scopes": ["read", "sync"]}).json()["token"]
    signup(node, "alice")
    t = token(node, "/admin/peers")
    probe = node.post("/admin/peers/probe", data={"csrf": t, "url": "http://central.test", "token": tok, "name": "Central"})
    assert probe.status_code == 200 and "Confirm this node" in probe.text
    nid = central.st.ctx.identity.node_id
    assert " ".join(nid[i:i + 4] for i in range(0, 16, 4)) in probe.text  # the fingerprint to compare
    import re

    block = re.search(r'<form[^>]*data-feed-option="own".*?</form>', probe.text, re.S).group(0)
    fields = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', block))
    assert (fields["node_id"], fields["follow"], fields["empty"]) == (nid, "own", "")
    add = node.post("/admin/peers", data={**fields, "csrf": t, "ack_relayed_fingerprint": "on"})
    assert add.status_code == 303
    with node.st.maker() as s:
        peer = s.scalars(select(Peer)).one()
        assert peer.token_enc and tok not in peer.token_enc and peer.node_id == nid
        assert peer.last_ok_at is not None  # adding it pulled the first page at once (C13 §7.3)
    with node.st.maker() as s:
        assert s.get(Canonical, r.canonical_id).view["title"] == "Hades"
    synced = node.post(f"/admin/peers/{peer.id}/sync", data={"csrf": t})
    assert synced.status_code == 200 and "Sync failed" not in synced.text
    with node.st.maker() as s:
        assert s.get(Canonical, r.canonical_id).view["title"] == "Hades"
    again = node.post("/admin/peers/probe", data={"csrf": t, "url": "http://central.test", "token": tok})
    assert "already a peer" in again.text
    assert node.post(f"/admin/peers/{peer.id}/toggle", data={"csrf": t}).status_code == 303
    assert "paused" in node.get("/admin/peers").text
    assert node.post(f"/admin/peers/{peer.id}/delete", data={"csrf": t}).status_code == 303
    assert "No peers yet" in node.get("/admin/peers").text


def test_peer_validation_and_permissions(pair_sites):
    central, node = pair_sites
    signup(node, "alice")
    bob = fresh(type("S", (), {"st_app": node.st_app}))
    signup(bob, "bob")
    t = token(node, "/admin/peers")
    for url in ("ftp://x.test", "https://user:pw@x.test", "https://x.test/?a=1", "http://", "not a url"):
        assert node.post("/admin/peers/probe", data={"csrf": t, "url": url}).status_code == 400, url
    wrong = node.post("/admin/peers", data={"csrf": t, "url": "http://central.test", "name": "x",
                                            "node_id": "0000000000000000", "follow": "own",
                                            "public_key": central.st.ctx.identity.public_b64})
    assert wrong.status_code == 400 and "does not match" in wrong.text
    assert node.post("/admin/peers/probe", data={"csrf": t, "url": "http://unreachable.invalid"}).status_code in (200, 400)
    tb = token(bob, "/me")
    assert bob.get("/admin/peers").status_code == 403
    assert bob.post("/admin/peers/probe", data={"csrf": tb, "url": "http://central.test"}).status_code == 403
    assert node.post("/admin/peers/probe", data={"url": "http://central.test"}).status_code in (403, 422)


def test_storing_a_peer_token_needs_the_secret_key(tmp_path):
    u = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/n.db", role="local",
                                       media_cache_enabled=False))
    with TestClient(u, base_url="http://localhost:8000", follow_redirects=False) as c:
        signup(c, "alice")
        t = token(c, "/admin/peers")
        ident = u.state.atlas.ctx.identity
        from types import SimpleNamespace

        from atlas.web import federation

        chosen = federation.probe_token(SimpleNamespace(csrf=t), "http://x.test", ident.node_id, ident.public_b64,
                                        "own", None, False)
        r = c.post("/admin/peers", data={"csrf": t, "url": "http://x.test", "name": "x", "node_id": ident.node_id,
                                         "public_key": ident.public_b64, "token": "atl_secret", "follow": "own",
                                         "probe_token": chosen, "ack_relayed_fingerprint": "on"})
        assert r.status_code == 400 and "ATLAS_SECRET_KEY" in r.text


def test_admin_token_page_creates_scoped_tokens_once_and_revokes(pair_sites):
    central, node = pair_sites
    signup(node, "alice")
    t = token(node, "/admin/tokens")
    r = node.post("/admin/tokens", data={"csrf": t, "name": "mirror-eu", "scopes": ["read", "sync"], "days": "30"})
    assert r.status_code == 200 and "Copy it now" in r.text
    secret = __import__("re").search(r"(atl_[A-Za-z0-9_-]+)", r.text).group(1)
    assert secret not in node.get("/admin/tokens").text  # shown once
    assert "mirror-eu" in node.get("/admin/tokens").text
    api = TestClient(node.st_app)
    assert api.get("/api/v2/sync/info", headers={"Authorization": "Bearer " + secret}).status_code == 200
    assert api.get("/api/v2/review", headers={"Authorization": "Bearer " + secret}).status_code == 403
    for bad in ({"name": "x", "scopes": ["nonsense"]}, {"name": "x", "scopes": ["read"], "days": "99999"},
                {"name": "x", "scopes": ["read"], "days": "abc"}):
        assert node.post("/admin/tokens", data={"csrf": t, **bad}).status_code in (400, 422)
    with node.st.maker() as s:
        from atlas.models import Token
        tid = s.scalars(select(Token).where(Token.name == "mirror-eu")).one().id
    assert node.post(f"/admin/tokens/{tid}/revoke", data={"csrf": t}).status_code == 303
    assert api.get("/api/v2/sync/info", headers={"Authorization": "Bearer " + secret}).status_code == 401


_ = key_id_for
