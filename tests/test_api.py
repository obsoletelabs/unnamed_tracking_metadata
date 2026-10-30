import pytest
from fastapi.testclient import TestClient

from atlas import main

from .conftest import build_settings

ADMIN = "x" * 40


@pytest.fixture
def client(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/t.db", bootstrap_admin_token=ADMIN)
    app = main.create_app(s)
    with TestClient(app) as c:
        c.app_state = app.state.atlas
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


def mint(client, scopes, **kw):
    r = client.post("/api/v2/tokens", json={"name": "t", "scopes": scopes, **kw}, headers=H(ADMIN))
    assert r.status_code == 201, r.text
    return r.json()["token"]


def obs(source, ext, title, **extra):
    source = {"steam": "playnite", "gog": "gogplugin"}.get(source, source)
    return {"connection": {"source_kind": "plugin", "source": source, "source_scope": "inst-1"},
            "observations": [{"external_id": ext, "title": title, **extra}]}


def test_auth_required_and_scopes(client):
    assert client.get("/api/v2/health").json()["status"] == "ok"
    assert client.get("/api/v2/games?q=x").status_code == 401
    ro = mint(client, ["read"])
    assert client.get("/api/v2/games?q=x", headers=H(ro)).status_code == 200
    assert client.post("/api/v2/observations", json=obs("steam", "1", "A"), headers=H(ro)).status_code == 403
    assert client.post("/api/v2/tokens", json={"name": "n", "scopes": ["read"]}, headers=H(ro)).status_code == 403


def test_token_expiry_and_revocation(client):
    r = client.post("/api/v2/tokens", json={"name": "t", "scopes": ["read"]}, headers=H(ADMIN)).json()
    assert client.get("/api/v2/games?q=x", headers=H(r["token"])).status_code == 200
    assert client.delete(f"/api/v2/tokens/{r['id']}", headers=H(ADMIN)).status_code == 204
    assert client.get("/api/v2/games?q=x", headers=H(r["token"])).status_code == 401


def test_submit_resolve_get_flow(client):
    w = mint(client, ["contribute", "read"])
    r = client.post("/api/v2/observations", json=obs("steam", "620", "Portal 2", release_date="2011-04-19"),
                    headers=H(w))
    assert r.status_code == 200
    cid = r.json()["results"][0]["atlas"]["canonical_id"]
    res = client.post("/api/v2/resolve", headers=H(w), json={"items": [
        {"namespace": "playnite", "external_id": "620"},
        {"title": "Portal 2", "release_date": "2011-04-19"},
        {"title": "Unknown Thing"},
    ]}).json()["results"]
    assert res[0]["canonical_id"] == cid and res[0]["method"] == "crosswalk" and res[0]["game"]["title"] == "Portal 2"
    assert res[1]["canonical_id"] == cid and res[2]["canonical_id"] is None
    wrong_type = client.post("/api/v2/resolve", headers=H(w), json={"items": [
        {"namespace": "playnite", "external_id": "620", "media_type": "movie"},
    ]}).json()["results"][0]
    assert wrong_type["canonical_id"] is None
    g = client.get(f"/api/v2/games/{cid}", headers=H(w))
    assert g.json()["provider_ids"] == {"playnite": "620"} and g.headers["etag"]
    assert client.get("/api/v2/games/atlas:nope", headers=H(w)).status_code == 404
    assert client.get(f"/api/v2/games/{cid}/explain", headers=H(w)).json()["explain"]["title"]["source"] == "playnite"


def test_observation_and_resolve_contract_accept_media_type(client):
    write = mint(client, ["contribute"])
    read = mint(client, ["read"])
    game = {**obs("qa-game-source", "game-1", "Dune", release_date="2021-10-22"), "media_type": "game"}
    movie = {**obs("qa-movie-source", "movie-1", "Dune", release_date="2021-10-22"), "media_type": "movie"}
    game_response = client.post("/api/v2/observations", json=game, headers=H(write))
    movie_response = client.post("/api/v2/observations", json=movie, headers=H(write))
    assert game_response.status_code == movie_response.status_code == 200
    game_id = game_response.json()["results"][0]["atlas"]["canonical_id"]
    movie_id = movie_response.json()["results"][0]["atlas"]["canonical_id"]
    assert game_id != movie_id
    result = client.post("/api/v2/resolve", headers=H(read), json={"items": [
        {"title": "Dune", "release_date": "2021-10-22", "media_type": "movie"},
    ]}).json()["results"][0]
    assert result["canonical_id"] == movie_id and result["game"]["media_type"] == "movie"


def test_personal_data_not_returned(client):
    w = mint(client, ["contribute", "read"])
    client.post("/api/v2/observations", headers=H(w),
                json=obs("steam", "1", "A", facts={"playtime_seconds": 500, "tags": ["x"]}))
    row = client.get("/api/v2/games?q=A", headers=H(w)).json()["results"][0]
    assert "playtime_seconds" not in str(row)


def test_contract_validation_rejects_unknown_fields(client):
    w = mint(client, ["contribute"])
    bad = obs("steam", "1", "A", surprise=True)
    assert client.post("/api/v2/observations", json=bad, headers=H(w)).status_code == 422


def test_curator_flow_merge_lock_review(client):
    w = mint(client, ["contribute", "read", "curate"])
    a = client.post("/api/v2/observations", headers=H(w), json=obs("steam", "1", "Mystery Game")).json()
    b = client.post("/api/v2/observations", headers=H(w), json=obs("gog", "2", "Mystery Game")).json()
    ida, idb = (x["results"][0]["atlas"]["canonical_id"] for x in (a, b))
    assert ida != idb and b["results"][0]["atlas"]["review"]
    rev = client.get("/api/v2/review", headers=H(w)).json()["results"]
    assert len(rev) == 1
    m = client.post(f"/api/v2/review/{rev[0]['id']}/merge", json={"into": ida}, headers=H(w))
    assert m.status_code == 200 and m.json()["id"] == ida
    assert client.get(f"/api/v2/games/{idb}", headers=H(w)).json()["redirected_from"] == idb
    lk = client.put(f"/api/v2/games/{ida}/locks/title", json={"value": "Fixed"}, headers=H(w))
    assert lk.json()["title"] == "Fixed"
    assert client.put(f"/api/v2/games/{ida}/locks/bogus", json={"value": "x"}, headers=H(w)).status_code == 400


def test_feed_published_only_for_curated(client):
    c = mint(client, ["contribute", "read", "curate", "sync"])
    client.post("/api/v2/observations", headers=H(c), json={
        "connection": {"source_kind": "manual", "source": "manual", "source_scope": "ops"},
        "observations": [{"external_id": "m1", "title": "Curated", "release_date": "2020-01-01"}]})
    client.post("/api/v2/observations", headers=H(c), json=obs("steam", "9", "Provider Only"))
    ch = client.get("/api/v2/sync/changes?since=0", headers=H(c)).json()
    assert ch["head"] == 1 and ch["entries"][0]["kind"] == "obs"
    info = client.get("/api/v2/sync/info", headers=H(c)).json()
    assert info["node_id"] == ch["entries"][0]["key_id"]


def test_rate_limit_and_body_limit(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/r.db", bootstrap_admin_token=ADMIN,
                       rate_limit_per_minute=3, max_body_bytes=2048)
    with TestClient(main.create_app(s)) as c:
        codes = [c.get("/api/v2/games?q=x", headers=H(ADMIN)).status_code for _ in range(5)]
        assert codes[:3] == [200] * 3 and codes[3] == 429
        big = c.post("/api/v2/resolve", headers=H(ADMIN), content=b"x" * 5000)
        assert big.status_code == 413
        chunked = c.post("/api/v2/resolve", headers=H(ADMIN), content=iter([b"x" * 1500, b"x" * 1500]))
        assert chunked.status_code == 413


def test_large_achievement_catalogue_is_readable_in_bounded_pages(client):
    cid = client.post("/api/v2/observations", json=obs("steam", "620", "Portal 2"),
                      headers=H(mint(client, ["contribute"]))).json()["results"][0]["atlas"]["canonical_id"]
    curator = H(mint(client, ["curate"]))
    items = [{"name": f"Achievement {n:04d}", "external_id": f"ACH_{n}", "position": n} for n in range(450)]
    assert client.put(f"/api/v2/games/{cid}/achievements", json={"items": items}, headers=curator).json() == {
        "count": 450}
    read = H(mint(client, ["read"]))
    unpaged = client.get(f"/api/v2/games/{cid}/achievements", headers=read).json()
    assert len(unpaged["results"]) == 450 and unpaged["total"] == 450 and unpaged["next_offset"] is None
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = client.get(f"/api/v2/games/{cid}/achievements?limit=200&offset={offset}", headers=read).json()
        assert len(page["results"]) <= 200 and page["total"] == 450
        seen += [a["id"] for a in page["results"]]
        offset, pages = page["next_offset"], pages + 1
    assert pages == 3 and seen == [a["id"] for a in unpaged["results"]] and len(set(seen)) == 450
    assert client.get(f"/api/v2/games/{cid}/achievements?limit=201", headers=read).status_code == 422
    assert client.get(f"/api/v2/games/{cid}/achievements?limit=10&offset=900", headers=read).json()["results"] == []
