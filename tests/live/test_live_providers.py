"""Live provider checks (issue #26): opt-in, see conftest.py. Each check fetches one well-known record, asserts
the fields Atlas relies on, and (for game sources) imports it into a scratch node with
`importer.import_from_provider`, the path the Admin -> Import page uses."""

from __future__ import annotations

import os

import pytest

from atlas import importer, main, price_sources, service
from atlas.providers import Fetcher, build_registry
from atlas.providers.igdb import Igdb
from atlas.providers.openlibrary import OpenLibrary
from atlas.providers.rawg import Rawg
from atlas.providers.steam import Steam
from atlas.providers.tgdb import TheGamesDb
from atlas.providers.wikidata import Wikidata

from ..conftest import build_settings
from .conftest import need

pytestmark = pytest.mark.live

PORTAL_2 = {"steam": "620", "rawg": "4200", "igdb": "72"}


def shape(report, provider, result):
    """Record which fields the live answer filled (values are not kept)."""
    report[provider] = {
        "title": bool(result.title), "release_date": result.release_date is not None,
        "release_precision": ((result.release or {}).get("first") or {}).get("precision"),
        "facts": sorted(k for k, v in result.facts.items() if v not in (None, "", [])),
        "custom": sorted(result.custom), "artwork_kinds": sorted({a["kind"] for a in result.artwork}),
        "provider_ids": sorted(result.provider_ids),
    }


def import_live(tmp_path, provider, external_id, **settings):
    """Import through the real importer into a throwaway SQLite node; returns (Imported, view)."""
    s = build_settings(database_url=f"sqlite:///{tmp_path}/live.db", media_dir=str(tmp_path / "media"),
                       media_cache_enabled=False, **settings)
    st = main.create_app(s).state.atlas
    with st.maker() as session:
        got = importer.import_from_provider(st, session, provider, external_id)
        session.commit()
        row, _ = service.get_canonical(session, got.row.id)
        return got, row.view


def test_steam_storefront(client, report, tmp_path):
    r = Steam().fetch(client, PORTAL_2["steam"])
    assert r is not None and r.title == "Portal 2" and r.release_date
    assert r.facts["developer"] == "Valve" and r.facts["genres"] and r.facts["languages"]
    shape(report, "steam", r)
    assert Steam().search(client, "Portal 2")
    assert Steam().achievement_stats(client, PORTAL_2["steam"])  # keyless global percentages
    got, view = import_live(tmp_path, "steam", PORTAL_2["steam"])
    assert view["title"] == "Portal 2" and got.artwork > 0


def test_steam_achievement_names_with_key(client):
    key = need("ATLAS_STEAM_API_KEY")["ATLAS_STEAM_API_KEY"]
    rows = Steam().achievements(client, PORTAL_2["steam"], key)
    assert rows and all(r["name"] and r["external_id"] for r in rows)


def test_rawg(client, report, tmp_path):
    key = need("ATLAS_RAWG_API_KEY")["ATLAS_RAWG_API_KEY"]
    r = Rawg(key).fetch(client, PORTAL_2["rawg"])
    assert r is not None and "Portal 2" in r.title and r.facts["genres"]
    shape(report, "rawg", r)
    assert Rawg(key).search(client, "Portal 2")
    _got, view = import_live(tmp_path, "rawg", PORTAL_2["rawg"], rawg_api_key=key, steam_enabled=False)
    assert "Portal 2" in view["title"]


def test_igdb(client, report, tmp_path):
    env = need("ATLAS_IGDB_CLIENT_ID", "ATLAS_IGDB_CLIENT_SECRET")
    p = Igdb(env["ATLAS_IGDB_CLIENT_ID"], env["ATLAS_IGDB_CLIENT_SECRET"])
    r = p.fetch(client, PORTAL_2["igdb"])
    assert r is not None and r.title == "Portal 2" and r.provider_ids.get("steam") == "620"
    shape(report, "igdb", r)
    assert p.search(client, "Portal 2")
    _got, view = import_live(tmp_path, "igdb", PORTAL_2["igdb"], igdb_client_id=env["ATLAS_IGDB_CLIENT_ID"],
                             igdb_client_secret=env["ATLAS_IGDB_CLIENT_SECRET"], steam_enabled=False)
    assert view["title"] == "Portal 2"


def test_thegamesdb(client, report, tmp_path):
    key = need("ATLAS_TGDB_API_KEY")["ATLAS_TGDB_API_KEY"]
    found = TheGamesDb(key).search(client, "Portal 2")
    match = [f for f in found if f.title == "Portal 2"]
    assert match, [f.title for f in found]
    r = TheGamesDb(key).fetch(client, match[0].external_id)
    assert r is not None and r.title == "Portal 2"
    shape(report, "tgdb", r)
    _got, view = import_live(tmp_path, "tgdb", match[0].external_id, tgdb_api_key=key, steam_enabled=False)
    assert view["title"] == "Portal 2"


def test_openlibrary(client, report):
    found = OpenLibrary().search(client, "Dune Frank Herbert")
    assert any(f.external_id == "OL893414W" for f in found)
    r = OpenLibrary().fetch(client, "OL893414W")
    assert r is not None and r.title == "Dune" and r.facts["developer"] == "Frank Herbert"
    shape(report, "openlibrary", r)
    assert Fetcher(build_settings(), {}).as_batch("openlibrary", [r]) is not None


def test_wikidata_crossref(client, report):
    got = Wikidata(os.environ.get("ATLAS_PUBLIC_URL", "")).crossref(client, "steam", "292030")
    assert "the-witcher-3-wild-hunt" in got.get("epic", []) and "the_witcher_3_wild_hunt" in got.get("gog", [])
    report["wikidata"] = {"namespaces": sorted(got)}


def test_steam_price_overview(client, report):
    quotes = price_sources.SteamPrices().quotes(client, ["620", "440"], "us")
    priced = [q for q in quotes if q.availability == "available"]
    assert priced and priced[0].currency == "USD" and priced[0].amount_minor > 0
    assert all(q.product_id != "440" for q in quotes)  # free: no price observation
    report["steam_prices"] = {"quotes": len(quotes)}


def test_registry_builds_every_configured_provider():
    env = {k: os.environ.get(k) for k in ("ATLAS_RAWG_API_KEY", "ATLAS_IGDB_CLIENT_ID", "ATLAS_IGDB_CLIENT_SECRET",
                                          "ATLAS_TGDB_API_KEY")}
    s = build_settings(rawg_api_key=env["ATLAS_RAWG_API_KEY"], igdb_client_id=env["ATLAS_IGDB_CLIENT_ID"],
                       igdb_client_secret=env["ATLAS_IGDB_CLIENT_SECRET"], tgdb_api_key=env["ATLAS_TGDB_API_KEY"],
                       openlibrary_enabled=True)
    expected = {"steam", "openlibrary"} | {name for name, key in (("rawg", "ATLAS_RAWG_API_KEY"),
                                                                  ("tgdb", "ATLAS_TGDB_API_KEY")) if env[key]}
    assert expected <= set(build_registry(s))
