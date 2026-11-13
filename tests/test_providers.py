import httpx
import pytest
from fastapi.testclient import TestClient

from atlas import main
from atlas.providers import Fetcher, ProviderError, build_registry
from atlas.providers.base import parse_date

from .conftest import build_settings

STEAM_DETAILS = {"620": {"success": True, "data": {
    "type": "game", "name": "Portal 2", "short_description": "The <b>sequel</b>.",
    "developers": ["Valve"], "publishers": ["Valve"], "genres": [{"description": "Action"}],
    "categories": [{"description": "Co-op"}], "release_date": {"coming_soon": False, "date": "19 Apr, 2011"}}}}


def make_client(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "storesearch" in request.url.path:
            return httpx.Response(200, json={"items": [{"id": 620, "name": "Portal 2", "type": "app"}]})
        if "appdetails" in request.url.path:
            return httpx.Response(200, json=STEAM_DETAILS)
        if "api.rawg.io" in request.url.host:
            return httpx.Response(200, json={"id": 4200, "name": "Portal 2", "released": "2011-04-18",
                                             "description_raw": "Puzzle.", "developers": [{"name": "Valve"}],
                                             "genres": [{"name": "Puzzle"}], "tags": [{"name": "Co-op"}]})
        if "twitch" in request.url.host:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "igdb" in request.url.host:
            return httpx.Response(200, json=[{"id": 72, "name": "Portal 2", "first_release_date": 1303171200,
                                              "summary": "S", "genres": [{"name": "Puzzle"}],
                                              "involved_companies": [{"developer": True, "company": {"name": "Valve"}}],
                                              "external_games": [{"category": 1, "uid": "620"}]}])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_date():
    assert parse_date("19 Apr, 2011") == "2011-04-19"
    assert parse_date("Apr 19, 2011") == "2011-04-19"
    assert parse_date("Apr 2011") is None and parse_date("Coming soon") is None


def test_cache_budget_and_stale_on_error(session):
    calls: list[str] = []
    s = build_settings(provider_ttl_seconds=60)
    f = Fetcher(s, build_registry(s), make_client(calls))
    r = f.lookup(session, "steam", external_id="620")
    assert r[0].title == "Portal 2" and r[0].facts["description"] == "The sequel."
    f.lookup(session, "steam", external_id="620")
    assert len(calls) == 1  # second served from cache

    # expire the entry, then break the network: stale data is served
    from atlas.models import ProviderCache
    row = session.get(ProviderCache, ("steam", "fetch:620"))
    from datetime import timedelta
    row.expires_at = row.expires_at - timedelta(days=1)
    session.flush()
    f.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert f.lookup(session, "steam", external_id="620")[0].title == "Portal 2"


def test_unconfigured_and_exhausted_budget(session):
    s = build_settings()
    f = Fetcher(s, build_registry(s), make_client([]))
    with pytest.raises(ProviderError, match="not configured"):
        f.lookup(session, "rawg", query="x")
    f._budget(f.providers["steam"], "fetch").tokens = 0
    with pytest.raises(ProviderError, match="budget"):
        f.lookup(session, "steam", external_id="999")


def test_circuit_opens_after_repeated_failures(session):
    s = build_settings()
    f = Fetcher(s, build_registry(s), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    for i in range(5):
        with pytest.raises(ProviderError):
            f.lookup(session, "steam", external_id=str(i))
    with pytest.raises(ProviderError, match="circuit"):
        f.lookup(session, "steam", external_id="x")


def test_cross_provider_merge_via_api(tmp_path):
    calls: list[str] = []
    s = build_settings(database_url=f"sqlite:///{tmp_path}/p.db", bootstrap_admin_token="a" * 40,
                       rawg_api_key="k", igdb_client_id="i", igdb_client_secret="s")
    app = main.create_app(s)
    app.state.atlas.providers.client = make_client(calls)
    h = {"Authorization": "Bearer " + "a" * 40}
    with TestClient(app) as c:
        # Open Library is on by default (C15 D15.7)
        assert {p["name"] for p in c.get("/api/v2/providers", headers=h).json()["results"]} == {
            "steam", "rawg", "igdb", "openlibrary"}
        found = c.get("/api/v2/providers/steam/search?q=portal", headers=h).json()["results"]
        assert found[0]["external_id"] == "620"
        a = c.post("/api/v2/providers/steam/fetch", json={"external_id": "620"}, headers=h).json()
        b = c.post("/api/v2/providers/igdb/fetch", json={"external_id": "72"}, headers=h).json()
        r = c.post("/api/v2/providers/rawg/fetch", json={"external_id": "4200"}, headers=h).json()
        assert a["game"]["id"] == b["game"]["id"] == r["game"]["id"]
        game = r["game"]
        assert game["provider_ids"] == {"igdb": "72", "rawg": "4200", "steam": "620"}
        assert game["developer"] == "Valve" and game["release_date"] in ("2011-04-19", "2011-04-18")
        assert c.post("/api/v2/providers/steam/fetch", json={"external_id": "123"}, headers=h).status_code in (404, 502)
