"""Contract tests: exactly the requests the Tracking App's `official.metadata-atlas` plugin makes.

The plugin (obsoletelabs/unnamed_tracking_app_plugins, official/metadata-atlas/plugin.py) is the host's only Atlas
client. These tests pin the response fields it reads, so an Atlas change that would silently break the app fails
here first (issues #16 and #30).
"""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas import main, media

from .conftest import build_settings
from .test_media_facets import client_for, png, resolver  # noqa: F401

ADMIN = "x" * 40
ATLAS_ID = re.compile(r"^atlas:[0-9a-z]{26}$")  # the plugin refuses anything else


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def atlas(tmp_path, resolver):  # noqa: F811
    s = build_settings(database_url=f"sqlite:///{tmp_path}/c.db", bootstrap_admin_token=ADMIN,
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False)
    app = main.create_app(s)
    with TestClient(app) as c:
        c.state, c.settings = app.state.atlas, s

        def mint(scopes):
            r = c.post("/api/v2/tokens", json={"name": "t", "scopes": scopes}, headers=H(ADMIN))
            assert r.status_code == 201
            return r.json()["token"]

        c.mint = mint
        c.read = H(mint(["read"]))  # the documented token for the plugin: read scope only
        c.write = H(mint(["contribute", "curate", "read"]))
        yield c


def seed(atlas, ext="620", title="Portal 2", date="2011-04-19", source="steamsync"):
    body = {"connection": {"source_kind": "plugin", "source": source, "source_scope": "i1"},
            "observations": [{"external_id": ext, "title": title, "release_date": date, "facts": {
                "developer": "Valve", "publisher": "Valve", "description": "Puzzles.", "genres": ["Puzzle"],
                "features": ["Co-op"], "languages": ["English"]}}]}
    r = atlas.post("/api/v2/observations", json=body, headers=atlas.write)
    assert r.status_code == 200, r.text
    return r.json()["results"][0]["atlas"]["canonical_id"]


@pytest.mark.parametrize("query", ["Portal 2", "portal 2", "PORTAL  2", "Portal: 2", "portal"])
def test_search_survives_case_and_punctuation_with_a_read_only_token(atlas, query):
    cid = seed(atlas)
    got = atlas.get("/api/v2/games", params={"q": query, "limit": 5}, headers=atlas.read)
    assert got.status_code == 200
    hits = got.json()["results"]
    assert cid in [h["id"] for h in hits], query
    hit = next(h for h in hits if h["id"] == cid)
    assert ATLAS_ID.match(hit["id"]) and hit["title"] == "Portal 2"
    assert isinstance(hit.get("provider_ids", {}), dict)


def test_game_record_has_every_field_the_plugin_reads(atlas):
    """Fields with no data are omitted (the plugin reads them with defaults); present ones keep their types."""
    cid = seed(atlas)
    game = atlas.get(f"/api/v2/games/{cid}", headers=atlas.read).json()
    assert game["id"] == cid and game["title"] == "Portal 2" and game["release_date"] == "2011-04-19"
    assert game["developer"] == game["publisher"] == "Valve" and game["description"] == "Puzzles."
    assert game["features"] == ["Co-op"] and game["genres"] == ["Puzzle"] and game["languages"] == ["English"]
    for key in ("tags", "features", "platforms", "genres", "themes", "languages"):
        assert isinstance(game.get(key, []), list), key
    assert all(isinstance(k, str) and isinstance(v, (str, int)) for k, v in (game.get("provider_ids") or {}).items())
    assert isinstance(game["custom"], dict)  # admin-defined fields, such as the multiplayer fields


def test_unknown_atlas_id_is_a_clean_404_the_plugin_treats_as_no_match(atlas):
    assert atlas.get("/api/v2/games/atlas:00000000000000000000000000", headers=atlas.read).status_code == 404
    assert atlas.get("/api/v2/games/atlas:00000000000000000000000000/artwork", headers=atlas.read).status_code == 404


def test_candidate_from_another_provider_resolves_by_crosswalk(atlas):
    cid = seed(atlas)
    r = atlas.post("/api/v2/resolve", headers=atlas.read, json={
        "items": [{"title": "Portal 2", "provider_ids": {"steamsync": "620"}}], "include_view": True})
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["canonical_id"] == cid and result["method"] in {"crosswalk", "exact"}
    assert result["game"]["title"] == "Portal 2"


def test_year_only_candidate_resolves_only_when_certain(atlas):
    """The app often knows just a title and year; the plugin sends YYYY-01-01 and accepts only certain matches."""
    cid = seed(atlas)
    r = atlas.post("/api/v2/resolve", headers=atlas.read, json={
        "items": [{"title": "Portal 2", "release_date": "2011-01-01"}, {"title": "Never Heard Of It"}],
        "include_view": True}).json()["results"]
    assert r[0]["canonical_id"] in (cid, None)  # either is safe; a wrong record never is
    assert r[1]["canonical_id"] is None and r[1]["method"] == "none"


def test_read_token_cannot_mutate(atlas):
    cid = seed(atlas)
    assert atlas.put(f"/api/v2/games/{cid}/artwork", headers=atlas.read,
                     json={"items": [{"kind": "key_art", "url": "https://img.test/a.png"}]}).status_code == 403
    assert atlas.post("/api/v2/observations", headers=atlas.read, json={}).status_code in (403, 422)


def test_artwork_flow_matches_what_the_plugin_publishes_to_the_host(atlas):
    cid = seed(atlas)
    assert atlas.put(f"/api/v2/games/{cid}/artwork", headers=atlas.write, json={"items": [
        {"kind": "key_art", "url": "https://img.test/c.png"}, {"kind": "banner", "url": "https://img.test/b.png"}]}
    ).status_code == 200
    # Before caching, the plugin must find nothing to publish rather than an uncached link.
    pre = atlas.get(f"/api/v2/games/{cid}/artwork", headers=atlas.read).json()["results"]
    assert pre and all(not a["cached"] and a["media_url"] is None for a in pre)

    media.cache_pending(atlas.state.maker, atlas.settings, client_for(lambda _r: httpx.Response(200, content=png())))
    post = atlas.get(f"/api/v2/games/{cid}/artwork", headers=atlas.read).json()["results"]
    assert {a["kind"] for a in post} == {"key_art", "banner"} and all(a["cached"] for a in post)
    for item in post:
        digest = item["media_url"].rsplit("/", 1)[-1]
        assert re.fullmatch(r"[0-9a-f]{64}", digest) and isinstance(item["width"], int) and item["width"] > 0
        # The host downloads artwork itself, without a token, from the public path the plugin builds.
        got = atlas.get(f"/media/{digest}")  # no Authorization header: the client sends none by default
        assert got.status_code == 200 and got.headers["content-type"] == "image/png"
        assert "immutable" in got.headers["cache-control"] and "authorization" not in atlas.headers


def test_achievement_list_is_available_for_hosts_that_can_use_it(atlas):
    cid = seed(atlas)
    facets_items = [{"name": "Wake Up Call", "description": "Finish chapter 1", "global_percent": 40.5}]
    assert atlas.put(f"/api/v2/games/{cid}/achievements", headers=atlas.write, json={"items": facets_items}
                     ).status_code == 200
    got = atlas.get(f"/api/v2/games/{cid}/achievements", headers=atlas.read).json()["results"]
    assert got[0]["name"] == "Wake Up Call" and got[0]["global_percent"] == 40.5


def test_health_endpoint_needs_no_token_and_is_cheap(atlas):
    r = atlas.get("/api/v2/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
