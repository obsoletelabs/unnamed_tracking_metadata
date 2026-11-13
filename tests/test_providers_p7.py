"""Issue #54/#63/#32/#38 providers: TheGamesDB, Open Library, Wikidata cross-store ids, the integrations page and
Steam price fetching. Offline: every answer comes from `tests/fixtures/providers` (see its README for which were
recorded live and which follow published documentation)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from atlas import cli, integrations, price_sources, service
from atlas.models import Canonical, Crosswalk, PriceObservation
from atlas.providers import Fetcher, ProviderError, build_registry
from atlas.providers.openlibrary import OpenLibrary
from atlas.providers.registry import media_types
from atlas.providers.tgdb import TheGamesDb
from atlas.providers.wikidata import PROPERTIES, Wikidata, from_wikidata, to_wikidata

from . import test_web
from .conftest import batch, build_settings
from .test_web import signup, token

site = test_web.site  # the web fixture

FIX = Path(__file__).parent / "fixtures" / "providers"


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def mock(routes: dict[str, object], calls: list[httpx.Request] | None = None) -> httpx.Client:
    """A client answering by URL path substring; anything else is a 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        for key, body in routes.items():
            if key in str(request.url):
                if isinstance(body, httpx.Response):
                    return body
                return httpx.Response(200, json=body)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- registry -----------------------------------------------------------------------------------------

def test_registry_adds_new_sources_only_when_configured():
    off = build_registry(build_settings())
    assert set(off) == {"steam"}  # keyless new sources are opt-in; a keyed one needs its key
    on = build_registry(build_settings(tgdb_api_key="k", openlibrary_enabled=True))
    assert isinstance(on["tgdb"], TheGamesDb) and isinstance(on["openlibrary"], OpenLibrary)
    assert media_types(on["openlibrary"]) == ("book",) and media_types(on["steam"]) == ("game",)
    assert Fetcher(build_settings(), {}).crossref_source is None
    f = Fetcher(build_settings(wikidata_enabled=True, public_url="https://atlas.example"), {})
    assert isinstance(f.crossref_source, Wikidata) and "https://atlas.example" in f.crossref_source.user_agent
    f.rebuild(build_settings(wikidata_enabled=False))
    assert f.crossref_source is None


# ---- TheGamesDB ---------------------------------------------------------------------------------------

def test_tgdb_fetch_maps_the_documented_shape(session):
    calls: list[httpx.Request] = []
    client = mock({"Games/ByGameID": fixture("tgdb-bygameid-documented-shape.json")}, calls)
    r = TheGamesDb("key-1").fetch(client, "2259")
    assert r.title == "Portal 2" and r.platform == "pc" and r.release_date == "2011-04-19"
    assert r.facts["description"] == "Portal 2 draws from the award-winning formula of innovative gameplay."
    assert r.facts["aliases"] == ["Portal II"] and r.facts["features"] == ["Co-op"]
    assert r.facts["age_rating"] == "E10+ - Everyone 10+" and r.custom == {"players_max": 2}
    assert r.artwork == [{"kind": "key_art", "url": "https://cdn.thegamesdb.net/images/original/boxart/front/2259-1.jpg",
                          "priority": 40}]  # the back cover is left out
    assert calls[0].url.params["apikey"] == "key-1" and calls[0].url.params["id"] == "2259"
    # the item validates as an observation
    assert Fetcher(build_settings(), {}).as_batch("tgdb", [r]) is not None


def test_tgdb_error_status_and_unknown_game():
    bad = mock({"Games/ByGameID": {"code": 403, "status": "This API Key is invalid"}})
    with pytest.raises(ProviderError, match="invalid"):
        TheGamesDb("k").fetch(bad, "1")
    empty = mock({"Games/ByGameID": {"code": 200, "data": {"count": 0, "games": []}}})
    assert TheGamesDb("k").fetch(empty, "1") is None
    search = mock({"Games/ByGameName": {"code": 200, "data": {"games": [
        {"id": 1, "game_title": "Portal", "platform": 1, "release_date": "2007-10-10"}, {"id": 2}]}}})
    found = TheGamesDb("k").search(search, "portal")
    assert [(x.external_id, x.title, x.platform) for x in found] == [("1", "Portal", "pc")]


# ---- Open Library -------------------------------------------------------------------------------------

def test_openlibrary_search_and_fetch_from_recorded_answers():
    client = mock({"/works/OL893414W.json": fixture("openlibrary-work-OL893414W.json"),
                   "search.json": fixture("openlibrary-search-dune.json")})
    found = OpenLibrary().search(client, "dune herbert")
    assert found and found[0].external_id == "OL893414W" and found[0].title == "Dune"
    assert found[0].release_date is None and found[0].release == {"first": {"date": "1965", "precision": "year"}}
    got = OpenLibrary().fetch(client, "OL893414W")
    assert got.facts["developer"] == "Frank Herbert" and got.facts["description"].startswith("Set on the desert planet")
    assert got.artwork[0]["url"] == "https://covers.openlibrary.org/b/id/11481354-L.jpg"
    assert OpenLibrary().fetch(client, "../etc") is None and OpenLibrary().fetch(client, "OL1M") is None  # works only
    assert Fetcher(build_settings(), {}).as_batch("openlibrary", [got]) is not None


def test_book_import_needs_a_book_media_type(site):
    from atlas import importer
    from atlas.models import MediaType

    client = mock({"/works/OL893414W.json": fixture("openlibrary-work-OL893414W.json"),
                   "search.json": fixture("openlibrary-search-dune.json")})
    st = site.st
    st.providers.client = client
    st.providers.providers["openlibrary"] = OpenLibrary()
    with st.maker() as s:  # A21 seeds `book` as a built-in; a node that deleted it cannot import books
        s.delete(s.get(MediaType, "book"))
        s.commit()
    with st.maker() as s:
        with pytest.raises(service.AtlasError, match="media type 'book'"):
            importer.import_from_provider(st, s, "openlibrary", "OL893414W")
        s.rollback()
    with st.maker() as s:
        from atlas import custom

        custom.seed(s)
        s.commit()
    with st.maker() as s:
        got = importer.import_from_provider(st, s, "openlibrary", "OL893414W")
        s.commit()
        assert got.row.media_type == "book" and got.row.view["title"] == "Dune"


# ---- Wikidata -----------------------------------------------------------------------------------------

def test_wikidata_value_mapping_round_trips_and_refuses_what_atlas_cannot_link():
    assert to_wikidata("gog", "the_witcher_3_wild_hunt") == "game/the_witcher_3_wild_hunt"
    assert from_wikidata("gog", "game/the_witcher_3_wild_hunt") == "the_witcher_3_wild_hunt"
    assert from_wikidata("gog", "movie/some_film") is None
    assert from_wikidata("itch", "https://poncle.itch.io/vampire-survivors") == "poncle/vampire-survivors"
    assert to_wikidata("itch", "poncle/vampire-survivors") == "https://poncle.itch.io/vampire-survivors"
    assert from_wikidata("openlibrary", "OL79034A") is None and from_wikidata("steam", "12a") is None
    assert to_wikidata("steam", '1" } DELETE') is None  # nothing unvalidated reaches the query
    assert to_wikidata("rawg", "portal-2") is None and "rawg" not in PROPERTIES and "igdb" not in PROPERTIES
    q = Wikidata().query("steam", "292030")
    assert 'wdt:P1733 "292030"' in q and "LIMIT 20" in q


def test_wikidata_crossref_reads_recorded_answers(session):
    calls: list[httpx.Request] = []
    client = mock({"query.wikidata.org": fixture("wikidata-steam-292030.json")}, calls)
    got = Wikidata("https://atlas.example").crossref(client, "steam", "292030")
    assert got == {"gog": ["the_witcher_3_wild_hunt", "the_witcher_3_wild_hunt_expansion_pass"],
                   "epic": ["the-witcher-3-wild-hunt"]}
    assert "atlas-metadata/2" in calls[0].headers["user-agent"] and "atlas.example" in calls[0].headers["user-agent"]
    itch = mock({"query.wikidata.org": fixture("wikidata-steam-1794680.json")})
    assert Wikidata().crossref(itch, "steam", "1794680").get("itch") == ["poncle/vampire-survivors"]
    with pytest.raises(ProviderError):
        Wikidata().crossref(mock({"query.wikidata.org": {"oops": 1}}), "steam", "1")


def test_fetcher_caches_crossref_and_survives_an_outage(session):
    calls: list[httpx.Request] = []
    s = build_settings(wikidata_enabled=True)
    f = Fetcher(s, {}, mock({"query.wikidata.org": fixture("wikidata-steam-292030.json")}, calls))
    assert f.crossref(session, "steam", "292030")["epic"] == ["the-witcher-3-wild-hunt"]
    assert f.crossref(session, "steam", "292030")["epic"] == ["the-witcher-3-wild-hunt"]
    assert len(calls) == 1  # the second answer came from the cache
    down = Fetcher(s, {}, mock({"query.wikidata.org": httpx.Response(503)}))
    assert down.crossref(session, "steam", "1") == {} and down.failure_counts() == {"wikidata": 1}
    assert Fetcher(build_settings(), {}).crossref(session, "steam", "292030") == {}  # not enabled: no call


def test_link_page_suggests_store_ids_from_wikidata(site):
    st = site.st
    st.base = st.base.model_copy(update={"wikidata_enabled": True, "steam_enabled": False})  # as Admin would
    st.providers.rebuild(st.base)
    calls: list[httpx.Request] = []
    st.providers.client = mock({"query.wikidata.org": fixture("wikidata-steam-292030.json")}, calls)
    with st.maker() as s:  # the fixture game gets a Steam id
        service.ingest(s, st.ctx, batch("steam", [{"external_id": "292030", "title": "Hades",
                                                   "release_date": "2020-09-17"}]), system=True,
                       force_canonical=site.gid)
        s.commit()
    with st.maker() as s:
        assert service.get_canonical(s, site.gid)[0].view["provider_ids"].get("steam") == "292030"
    signup(site, "alice")
    page = site.get(f"/game/{site.gid}/links").text
    assert "Suggested matches" in page, page[page.find("<main"):][:3000]
    assert "Wikidata, from its steam id" in page
    assert "the-witcher-3-wild-hunt" in page and 'href="https://store.epicgames.com/p/the-witcher-3-wild-hunt"' in page
    # one Epic candidate is a likely match; two GOG candidates (game and expansion pass) are not
    items = {li.split('name="ident" value="')[1].split('"')[0]: li for li in page.split("<li>")[1:] if 'name="ident"' in li}
    assert "Likely match" in items["the-witcher-3-wild-hunt"]
    assert "Likely match" not in items["the_witcher_3_wild_hunt"]
    assert "Likely match" not in items["the_witcher_3_wild_hunt_expansion_pass"]
    t = token(site, f"/game/{site.gid}/links")
    done = site.post(f"/game/{site.gid}/links", data={"csrf": t, "store": "epic", "ident": "the-witcher-3-wild-hunt"})
    assert done.status_code == 303
    with st.maker() as s:
        assert s.get(Crosswalk, ("epic", "the-witcher-3-wild-hunt")) is not None
    assert "the-witcher-3-wild-hunt</code>" not in site.get(f"/game/{site.gid}/links").text  # linked: not offered


def test_name_search_suggestions_respect_the_records_media_type(site):
    from atlas.providers.base import ProviderResult

    class Books:
        name = "books"
        redistributable = True
        requests_per_minute = 600
        media_types = ("book",)

        def search(self, client, query):
            raise AssertionError("a book source must not be searched for a game")

        def fetch(self, client, external_id):
            return None

    class Store:
        name = "gogish"
        redistributable = False
        requests_per_minute = 600

        def search(self, client, query):
            return [ProviderResult(external_id="hades-gog", title="Hades", release_date="2020-09-17")]

        def fetch(self, client, external_id):
            return None

    site.st.providers.providers.pop("steam", None)
    site.st.providers.providers.update(books=Books(), gogish=Store())
    signup(site, "alice")
    page = site.get(f"/game/{site.gid}/links").text
    assert "hades-gog" in page and 'value="books"' not in page


# ---- Admin -> Integrations ----------------------------------------------------------------------------

def test_integrations_page_lists_the_access_matrix_and_toggles_keyless_sources(site):
    signup(site, "alice")
    page = site.get("/admin/integrations").text
    for label in ("TheGamesDB", "Open Library", "Wikidata", "Epic Games Store", "itch.io", "GOG"):
        assert label in page
    assert "ATLAS_TGDB_API_KEY" in page and "terms forbid scraping" in page and "No adapter" in page
    assert site.st.providers.crossref_source is None and "openlibrary" not in site.st.providers.providers
    t = token(site, "/admin/integrations")
    assert site.post("/admin/integrations/wikidata", data={"csrf": t, "wikidata_enabled": "on"}).status_code == 303
    assert site.post("/admin/integrations/openlibrary", data={"csrf": t, "openlibrary_enabled": "on"}).status_code == 303
    site.get("/admin/integrations")
    assert isinstance(site.st.providers.crossref_source, Wikidata) and "openlibrary" in site.st.providers.providers
    site.post("/admin/integrations/wikidata", data={"csrf": t})  # unticked: off again
    site.get("/admin/integrations")
    assert site.st.providers.crossref_source is None


def test_integration_secrets_include_tgdb_and_itch_links_are_safe():
    assert "tgdb_api_key" in integrations.SECRET_FIELDS
    assert set(integrations.TOGGLES) == {"steam_enabled", "openlibrary_enabled", "wikidata_enabled"}
    assert integrations.store_link("itch", "poncle/vampire-survivors") == "https://poncle.itch.io/vampire-survivors"
    assert integrations.store_link("itch", "evil.com/x") is None and integrations.store_link("itch", "a/../b") is None
    assert integrations.store_link("openlibrary", "OL893414W") == "https://openlibrary.org/works/OL893414W"


# ---- Prices (#38) -------------------------------------------------------------------------------------

def _linked(session, ctx, pairs):
    out = {}
    for ext, title in pairs:
        [r] = service.ingest(session, ctx, batch("steam", [{"external_id": ext, "title": title}]), system=True)
        out[ext] = r.canonical_id
    session.commit()
    return out


def test_steam_price_source_maps_recorded_answer():
    client = mock({"appdetails": fixture("steam-price-overview-gb.json")})
    quotes = price_sources.SteamPrices().quotes(client, ["620", "440", "1", "x"], "gb")
    assert [(q.product_id, q.availability, q.currency, q.amount_minor, q.base_amount_minor) for q in quotes] == [
        ("620", "available", "GBP", 819, 819), ("1", "unavailable", None, None, None)]  # 440 is free: no quote


def test_collect_stores_prices_once_and_spaces_requests(session, ctx):
    ids = _linked(session, ctx, [("620", "Portal 2"), ("440", "TF2"), ("1", "Nothing")])
    calls: list[httpx.Request] = []
    client = mock({"appdetails": fixture("steam-price-overview-gb.json")}, calls)
    slept: list[float] = []
    t = iter([0.0, 0.0, 1.0, 1.0])
    when = datetime(2026, 11, 7, 12, tzinfo=UTC)
    src = price_sources.SteamPrices()
    src.batch_size = 2  # two requests, so the spacing is exercised
    report = price_sources.collect(session, src, "gb", client=client, sleep=slept.append, clock=lambda: next(t),
                                   now=lambda: when, node="atlas.test")
    assert report.requests == 2 and slept == [3.0]  # 20 requests a minute: 3 s apart
    assert report.stored == 1 and report.statuses.get("unavailable") == 1 and report.skipped == 1
    row = session.scalars(select(PriceObservation)).one()
    assert (row.canonical_id, row.product_id, row.region, row.currency, row.amount_minor) == (
        ids["620"], "620", "gb", "GBP", 819)
    assert row.redistributable is False and row.license_terms == "steam-storefront" and row.source_kind == "provider"
    assert row.provenance["method"] == "api" and row.provenance["node"] == "atlas.test"
    again = price_sources.collect(session, src, "gb", client=client, sleep=lambda _s: None, now=lambda: when)
    assert again.stored == 0 and again.duplicate == 1  # deterministic: the same observation is not stored twice
    assert session.scalar(select(PriceObservation.id).where(PriceObservation.id != row.id)) is None


def test_collect_stops_after_repeated_failures_and_dry_run_stores_nothing(session, ctx):
    _linked(session, ctx, [(str(i), f"Game {i}") for i in range(620, 625)])
    src = price_sources.SteamPrices()
    src.batch_size = 1
    calls: list[httpx.Request] = []
    down = mock({"appdetails": httpx.Response(503)}, calls)
    report = price_sources.collect(session, src, "us", client=down, sleep=lambda _s: None, max_failures=3)
    assert report.failures == 3 and len(calls) == 3 and report.stopped.startswith("3 failed requests")
    ok = mock({"appdetails": {"620": {"success": True, "data": {"price_overview": {
        "currency": "USD", "initial": 999, "final": 199, "discount_percent": 80}}}}})
    dry = price_sources.collect(session, src, "us", client=ok, sleep=lambda _s: None, dry_run=True)
    assert dry.statuses == {"would_store": 1} and session.scalar(select(PriceObservation.id)) is None
    with pytest.raises(ValueError):
        price_sources.collect(session, src, "ww", client=ok)


def test_collect_skips_merged_records(session, ctx):
    ids = _linked(session, ctx, [("620", "Portal 2")])
    session.get(Canonical, ids["620"]).merged_into = ids["620"]  # any non-null marks it merged away
    session.flush()
    assert list(price_sources.linked_products(session, "steam", 10)) == []


def test_prices_fetch_cli_is_gated_and_runs_end_to_end(monkeypatch, capsys, tmp_path):
    from atlas import signing
    from atlas.settings import get_settings

    monkeypatch.setenv("ATLAS_DATABASE_URL", f"sqlite:///{tmp_path}/p.db")
    monkeypatch.setenv("ATLAS_ROLE", "authority")
    monkeypatch.delenv("ATLAS_PRICE_SOURCES", raising=False)
    get_settings.cache_clear()
    try:
        assert cli.main(["migrate"]) == 0
        settings, maker = cli._maker()
        with maker() as s:
            ctx = service.Ctx(settings, signing.load_identity(s, settings))
            ids = _linked(s, ctx, [("620", "Portal 2")])
        capsys.readouterr()
        assert cli.main(["prices", "fetch", "--source", "steam", "--region", "gb"]) == 2  # off by default
        assert "ATLAS_PRICE_SOURCES" in capsys.readouterr().out
        assert cli.main(["prices", "fetch", "--source", "humble"]) == 2  # no such source
        capsys.readouterr()

        monkeypatch.setenv("ATLAS_PRICE_SOURCES", "steam")
        get_settings.cache_clear()
        real = httpx.Client
        monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=fixture("steam-price-overview-gb.json"))), **kw))
        assert cli.main(["prices", "fetch", "--source", "steam", "--region", "gb"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["stored"] == 1 and out["source"] == "steam" and out["region"] == "gb"
        with maker() as s:
            row = s.scalars(select(PriceObservation)).one()
            assert (row.canonical_id, row.amount_minor, row.currency) == (ids["620"], 819, "GBP")
    finally:
        get_settings.cache_clear()
    assert price_sources.enabled(build_settings(price_sources="Steam, nope ,steam")) == ["steam"]


# ---- review fixes: secrets, reserved sources, bad answers, cross-reference filtering --------------------

SECRET = "sekret-key-0123456789"


def test_provider_errors_never_carry_query_string_keys():
    """An upstream 4xx/5xx or network failure must not put `apikey=`/`key=`/`client_secret=` in the error text."""
    from atlas.providers.base import get_json, redact_urls
    from atlas.providers.rawg import Rawg

    denied = mock({"Games/ByGameID": httpx.Response(401, json={"code": 401}),
                   "api.rawg.io": httpx.Response(500, text="boom")})
    with pytest.raises(ProviderError) as err:
        TheGamesDb(SECRET).fetch(denied, "1")
    assert SECRET not in str(err.value) and "HTTP 401" in str(err.value) and err.value.__cause__ is None
    with pytest.raises(ProviderError) as err:
        Rawg(SECRET).fetch(denied, "1")
    assert SECRET not in str(err.value)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    down = httpx.Client(transport=httpx.MockTransport(offline))
    with pytest.raises(ProviderError) as err:
        get_json(down, "GET", "https://api.thegamesdb.net/v1/Games", params={"apikey": SECRET})
    assert SECRET not in str(err.value) and "ConnectError" in str(err.value)
    assert redact_urls("x 'https://a.example/p?key=1&b=2' y https://b.example/q#k=3") == \
        "x 'https://a.example/p' y https://b.example/q"


def test_link_search_error_does_not_show_the_key(site):
    site.st.providers.providers["tgdb"] = TheGamesDb(SECRET)
    site.st.providers.client = mock({"Games/ByGameName": httpx.Response(403, text="nope")})
    signup(site, "alice")
    page = site.get(f"/game/{site.gid}/links", params={"provider": "tgdb", "q": "Hades"}).text
    assert "tgdb could not be searched" in page and SECRET not in page


def test_tgdb_non_object_answer_is_a_provider_error():
    with pytest.raises(ProviderError, match="bad response"):
        TheGamesDb("k").fetch(mock({"Games/ByGameID": [1, 2]}), "1")


@pytest.mark.parametrize("source", ["tgdb", "openlibrary", "wikidata", "steam"])
def test_contributor_tokens_cannot_write_provider_sources(session, ctx, source):
    """The node's own provider sources are reserved even when ATLAS_SOURCE_TRUST does not list them."""
    from atlas.service import AtlasError, Ctx
    from atlas.settings import PROVIDER_SOURCES

    bare = Ctx(settings=build_settings(source_trust={"curator": 100}), identity=ctx.identity)
    for c in (ctx, bare):
        with pytest.raises(AtlasError, match="reserved"):
            service.ingest(session, c, batch(source, [{"external_id": "1", "title": "Forged"}], kind="plugin"))
    assert service.ingest(session, bare, batch(source, [{"external_id": "1", "title": "Mine"}]), system=True)
    every = build_registry(build_settings(rawg_api_key="k", igdb_client_id="i", igdb_client_secret="s",
                                          tgdb_api_key="k", openlibrary_enabled=True))
    assert set(every) | {"wikidata"} <= PROVIDER_SOURCES


def test_crossref_suggestions_follow_the_records_media_type(site, monkeypatch):
    from atlas.providers.wikidata import TIMEOUT, media_type_of
    from atlas.web import routes

    assert media_type_of("openlibrary") == "book" and media_type_of("gog") == "game"
    st = site.st
    st.base = st.base.model_copy(update={"wikidata_enabled": True, "steam_enabled": False})
    st.providers.rebuild(st.base)
    asked: list[tuple[str, str]] = []

    def crossref(session, ns, ident):
        asked.append((ns, ident))
        return {"openlibrary": ["OL45883W"], "epic": ["hades"]}

    monkeypatch.setattr(st.providers, "crossref", crossref)
    with st.maker() as s:
        for src, ext in (("steam", "1145360"), ("gog", "hades")):
            service.ingest(s, st.ctx, batch(src, [{"external_id": ext, "title": "Hades"}]), system=True,
                           force_canonical=site.gid)
        s.commit()
    signup(site, "alice")
    page = site.get(f"/game/{site.gid}/links").text
    assert 'value="hades"' in page and "OL45883W" not in page  # a game is not offered a book id
    assert len(asked) == 2
    # a slow first lookup stops further ones, so a cold cache cannot hold the page for every linked id
    asked.clear()
    monkeypatch.setattr(routes, "CROSSREF_SLOW_SECONDS", -1.0)
    site.get(f"/game/{site.gid}/links")
    assert len(asked) == 1
    # each Wikidata lookup has its own short timeout
    calls: list[httpx.Request] = []
    Wikidata().crossref(mock({"query.wikidata.org": fixture("wikidata-steam-292030.json")}, calls), "steam", "1")
    assert calls[0].extensions["timeout"]["read"] == TIMEOUT
