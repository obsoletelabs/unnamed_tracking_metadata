"""Migration 0035 (C12 achievements and media quality, A18): upgrade on a 0034 database holding achievements and
artwork; the application then writing sets, a DLC set, same-name achievements, a retired entry, translations,
stored percentages and curated artwork under a licence that keeps it out of the feed (with a vote); downgrade
leaving the previous release only rows it reads correctly (no retired row, one row per source and name, no
withheld image) and keeping the rest aside; upgrade again restoring every row, column, set, text and vote.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, inspect, select, text

from atlas import db, facets
from atlas.models import Achievement, AchievementSet, AchievementStat, AchievementText, Artwork, ArtworkVote, User
from atlas.service import Ctx
from atlas.signing import load_identity

from .conftest import build_settings
from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

HEAD = "0062"  # Z1 (C16 single version, merges the C14 and C15 lines) is the head now
STAMP = "2026-11-02 10:00:00"
NEW_TABLES = {"achievement_set", "achievement_text", "achievement_stat"}
KEPT_TABLES = {f"{t}_0035_kept" for t in NEW_TABLES}
KEPT_ROWS = {"achievement_rows_0035_kept", "achievement_cols_0035_kept", "artwork_rows_0035_kept",
             "artwork_cols_0035_kept"}
NEW_COLUMNS = {
    "achievement": {"set_key", "source_order", "status", "retired_at", "text_locale", "percent_as_of", "icon_licence",
                    "icon_attribution", "icon_last_error", "locked_icon_last_error"},
    "artwork": {"declared_width", "declared_height", "aspect", "dhash", "duplicate_of", "region", "locale", "licence",
                "attribution", "source_page_url", "last_error", "local_only"}}


@pytest.fixture(params=["sqlite", "postgresql"])
def url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path}/m.db"
    pg = request.getfixturevalue("pg_url")
    engine = create_engine(pg)
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    engine.dispose()
    return pg


def version(engine) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def columns(engine, table) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def seed(engine):
    """What a 0034 node holds: two records, a provider achievement list and a cached, declared-size image."""
    with engine.begin() as c:
        for cid, title in (("atlas:g", "Lantern Bay"), ("atlas:dlc", "Lantern Bay: Tides")):
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', :n, :v, '{}', 1, :t, :t)"),
                      {"i": cid, "n": title.lower(), "v": f'{{"title": "{title}"}}', "t": STAMP})
        for n, (name, ext, pct) in enumerate((("Lamp Lit", "ACH_LAMP", 71.5), ("Night Pilot", "ACH_PILOT", 8.0))):
            c.execute(text("INSERT INTO achievement (id, canonical_id, source, external_id, name, norm_name, description, "
                           "hidden, global_percent, redistributable, icon_fail_count, locked_icon_fail_count, key) "
                           "VALUES (:i, 'atlas:g', 'steam', :x, :n, :nn, '', false, :p, false, 0, 0, :k)"),
                      {"i": f"01JA{n:022d}", "x": ext, "n": name, "nn": name.lower(), "p": pct,
                       "k": f"ak1:{'a' * 25}{n}"})
        c.execute(text("INSERT INTO artwork (id, canonical_id, source, kind, url, width, height, priority, "
                       "redistributable, fail_count, created_at, preferred) VALUES ('01JW0000000000000000000000', "
                       "'atlas:g', 'steam', 'key_art', 'https://img.test/k.png', 600, 900, 100, false, 0, :t, false)"),
                  {"t": STAMP})


def maker(url, role="local"):
    settings = build_settings(database_url=url, role=role)
    return db.make_sessionmaker(db.make_engine(url), settings), settings


CURATED = [
    {"name": "Harbour Master", "external_id": "", "description": "Dock every ship.", "set": "base",
     "i18n": {"de": {"name": "Hafenmeister", "description": "Lege jedes Schiff an."}}, "locale": "en",
     "icon_url": "https://img.test/harbour.png", "locked_icon_url": "https://img.test/harbour-locked.png",
     "icon_licence": "cc-by", "icon_attribution": "A. Artist"},
    {"name": "Collector", "external_id": "COL_BASE", "description": "Collect the base shells.",
     "icon_url": "https://img.test/collector.png", "icon_licence": "cc0"},
    {"name": "Collector", "external_id": "COL_TIDES", "description": "Collect the tide shells.", "set": "tides"},
    {"name": "Old Route", "external_id": "OLD", "description": "Sail the old route."},
]
SETS = [{"key": "tides", "kind": "dlc", "name": "Tides", "dlc": "atlas:dlc", "platforms": ["pc"]}]


def apply_c12(url):
    """Every C12 construct, written by the application (not by SQL)."""
    mk, settings = maker(url)
    with mk() as s:
        ctx = Ctx(settings=settings, identity=load_identity(s))
        facets.set_achievements(s, ctx, "atlas:g", "curator", CURATED, sets=SETS, ordered=True)
        s.commit()
        # "Old Route" leaves the list: retired (kept, with its key), not deleted
        facets.set_achievements(s, ctx, "atlas:g", "curator", CURATED[:3], sets=SETS, ordered=True)
        facets.store_stats(s, "atlas:g", "steam", {"ACH_LAMP": 70.1, "ACH_PILOT": 7.9}, as_of=date(2026, 11, 1))
        facets.set_artwork(s, ctx, "atlas:g", "curator", [
            {"kind": "hero", "url": "https://img.test/press.jpg", "licence": "publisher_press",
             "attribution": "Example Press Kit", "region": "eu"},
            {"kind": "logo", "url": "https://img.test/fan.png", "local_only": True, "licence": "cc-by",
             "attribution": "A. Fan"},
            {"kind": "banner", "url": "https://img.test/free.png", "licence": "cc0", "locale": "en"}])
        # the media worker measured the provider image: 320x180 measured, 600x900 declared (§12.5.2)
        k = s.scalars(select(Artwork).where(Artwork.url == "https://img.test/k.png")).one()
        k.width, k.height, k.declared_width, k.declared_height = 320, 180, 600, 900
        user = User(username="voter", password_hash="x")
        s.add(user)
        s.flush()
        press = s.scalars(select(Artwork).where(Artwork.url == "https://img.test/press.jpg")).one()
        s.add(ArtworkVote(artwork_id=press.id, user_id=user.id, value=1))
        s.commit()
        snapshot = {
            "ach": {(a.source, a.external_id, a.name): (a.id, a.key, a.set_key, a.retired_at is not None,
                                                        a.source_order, a.text_locale, a.icon_url, a.locked_icon_url,
                                                        a.icon_licence)
                    for a in s.scalars(select(Achievement))},
            "texts": {(t.achievement_id, t.locale, t.name) for t in s.scalars(select(AchievementText))},
            "sets": {(x.canonical_id, x.source, x.set_key, x.kind, x.dlc_canonical_id) for x in s.scalars(
                select(AchievementSet))},
            "stats": {(x.source, x.external_id, x.percent) for x in s.scalars(select(AchievementStat))},
            "art": {a.url: (a.id, a.licence, a.attribution, a.region, a.locale, a.local_only, a.width, a.height,
                            a.declared_width, a.declared_height)
                    for a in s.scalars(select(Artwork))},
            "votes": {(v.artwork_id, v.value) for v in s.scalars(select(ArtworkVote))},
        }
    return snapshot


def test_upgrade_backfills_downgrade_keeps_aside_and_reupgrade_restores(url):
    db.upgrade(url, "0034")
    engine = create_engine(url)
    seed(engine)

    db.upgrade(url)
    assert version(engine) == HEAD
    tables = set(inspect(engine).get_table_names())
    assert NEW_TABLES <= tables and not ((KEPT_TABLES | KEPT_ROWS) & tables)
    for table, cols in NEW_COLUMNS.items():
        assert cols <= columns(engine, table)
    with engine.connect() as c:
        sets = {r[0] for r in c.execute(text("SELECT set_key FROM achievement"))}
        art = c.execute(text("SELECT width, height, declared_width, declared_height, aspect, local_only "
                             "FROM artwork")).one()
    assert sets == {"base"}  # every existing row is in the base set
    assert tuple(art[:4]) == (600, 900, 600, 900) and art[4] is None and art[5] in (False, 0)

    before = apply_c12(url)
    assert any(v[3] for (_s, _e, name), v in before["ach"].items() if name == "Old Route")  # retired
    assert {k for k in before["ach"] if k[2] == "Collector"} == {("curator", "COL_BASE", "Collector"),
                                                                 ("curator", "COL_TIDES", "Collector")}

    db.downgrade(url, "0034")
    assert version(engine) == "0034"
    tables = set(inspect(engine).get_table_names())
    assert (KEPT_TABLES | KEPT_ROWS) <= tables and not (NEW_TABLES & tables)
    for table, cols in NEW_COLUMNS.items():
        assert not (cols & columns(engine, table))
    with engine.connect() as c:
        ach = {(r[0], r[1]) for r in c.execute(text("SELECT source, name FROM achievement"))}
        idents = c.execute(text("SELECT source, norm_name, count(*) FROM achievement GROUP BY source, norm_name "
                                "HAVING count(*) > 1")).all()
        art_urls = {r[0] for r in c.execute(text("SELECT url FROM artwork"))}
        icons = {r[0]: (r[1], r[2]) for r in c.execute(text(
            "SELECT name, icon_url, locked_icon_url FROM achievement WHERE source = 'curator'"))}
        measured = c.execute(text("SELECT width, height FROM artwork WHERE url = 'https://img.test/k.png'")).one()
        votes = c.execute(text("SELECT count(*) FROM artwork_vote")).scalar()
        kept_ach = c.execute(text("SELECT count(*) FROM achievement_rows_0035_kept")).scalar()
        kept_art = c.execute(text("SELECT count(*) FROM artwork_rows_0035_kept")).scalar()
        unique = [ix for ix in inspect(engine).get_unique_constraints("achievement")]
        indexes = {ix["name"] for ix in inspect(engine).get_indexes("achievement")}
    # the previous release: one row per source and name (its unique constraint is back), no retired row
    assert not idents and ("curator", "Old Route") not in ach and ("curator", "Collector") in ach
    assert ("curator", "Harbour Master") in ach and ("steam", "Lamp Lit") in ach
    assert any(set(u["column_names"]) == {"canonical_id", "source", "norm_name"} for u in unique) \
        or "uq_achievement" in indexes
    assert kept_ach == 2  # the retired row and the second "Collector"
    # withheld licence and local-only images are not served by a release that would sign them into the feed
    assert art_urls == {"https://img.test/k.png", "https://img.test/free.png"}
    assert kept_art == 2 and votes == 0
    # nor a curated icon whose licence keeps it out of the CC0 feed; a cc0 icon stays
    assert icons["Harbour Master"] == (None, None)
    assert icons["Collector"] == ("https://img.test/collector.png", None)
    assert tuple(measured) == (320, 180)

    db.upgrade(url)
    assert version(engine) == HEAD
    tables = set(inspect(engine).get_table_names())
    assert NEW_TABLES <= tables and not ((KEPT_TABLES | KEPT_ROWS) & tables)
    mk, _settings = maker(url)
    with mk() as s:
        after = {
            "ach": {(a.source, a.external_id, a.name): (a.id, a.key, a.set_key, a.retired_at is not None,
                                                        a.source_order, a.text_locale, a.icon_url, a.locked_icon_url,
                                                        a.icon_licence)
                    for a in s.scalars(select(Achievement))},
            "texts": {(t.achievement_id, t.locale, t.name) for t in s.scalars(select(AchievementText))},
            "sets": {(x.canonical_id, x.source, x.set_key, x.kind, x.dlc_canonical_id) for x in s.scalars(
                select(AchievementSet))},
            "stats": {(x.source, x.external_id, x.percent) for x in s.scalars(select(AchievementStat))},
            "art": {a.url: (a.id, a.licence, a.attribution, a.region, a.locale, a.local_only, a.width, a.height,
                            a.declared_width, a.declared_height)
                    for a in s.scalars(select(Artwork))},
            "votes": {(v.artwork_id, v.value) for v in s.scalars(select(ArtworkVote))},
        }
    assert after == before  # every row, column, set, text, stat and vote is back, with the same ids and keys
    db.upgrade(url)  # idempotent
    assert version(engine) == HEAD
    engine.dispose()


def test_downgrade_of_an_empty_database_and_reupgrade(url):
    db.upgrade(url)
    engine = create_engine(url)
    db.downgrade(url, "0034")
    assert KEPT_TABLES <= set(inspect(engine).get_table_names())
    db.upgrade(url)
    assert version(engine) == HEAD and NEW_TABLES <= set(inspect(engine).get_table_names())
    assert not (KEPT_TABLES & set(inspect(engine).get_table_names()))
    db.downgrade(url, "0033")  # through 0035 and 0034: both keep their tables aside
    assert KEPT_TABLES | {"match_decision_0034_kept"} <= set(inspect(engine).get_table_names())
    db.upgrade(url)
    assert version(engine) == HEAD
    engine.dispose()


def test_icons_and_declarations_the_previous_release_rewrote_are_not_overwritten(url):
    db.upgrade(url, "0034")
    engine = create_engine(url)
    seed(engine)
    db.upgrade(url)
    apply_c12(url)
    db.downgrade(url, "0034")
    with engine.begin() as c:  # the previous release gives the row a new icon and the image a new declaration
        c.execute(text("UPDATE achievement SET icon_url = 'https://img.test/new.png' WHERE name = 'Harbour Master'"))
        c.execute(text("UPDATE artwork SET width = 700, height = 1000 WHERE url = 'https://img.test/k.png'"))
    db.upgrade(url)
    mk, _settings = maker(url)
    with mk() as s:
        harbour = s.scalars(select(Achievement).where(Achievement.name == "Harbour Master")).one()
        k = s.scalars(select(Artwork).where(Artwork.url == "https://img.test/k.png")).one()
        assert (harbour.icon_url, harbour.locked_icon_url, harbour.icon_licence) == (
            "https://img.test/new.png", None, "cc-by")
        assert (k.width, k.height, k.declared_width, k.declared_height) == (700, 1000, 700, 1000)
    engine.dispose()


def test_rows_written_while_downgraded_win_over_kept_rows(url):
    """A row the previous release added with the identity of a kept row stays; the kept one is not forced in."""
    db.upgrade(url, "0034")
    engine = create_engine(url)
    seed(engine)
    db.upgrade(url)
    mk, settings = maker(url)
    with mk() as s:
        ctx = Ctx(settings=settings, identity=load_identity(s))
        facets.set_achievements(s, ctx, "atlas:g", "curator", [{"name": "Echo", "external_id": "E1"},
                                                                {"name": "Echo", "external_id": "E2"}])
        s.commit()
    db.downgrade(url, "0034")
    with engine.begin() as c:  # the old release re-uses the kept row's key for a new row of another name
        kept_key = c.execute(text("SELECT data FROM achievement_rows_0035_kept")).scalar()
        key = (kept_key if isinstance(kept_key, dict) else __import__("json").loads(kept_key))["key"]
        c.execute(text("INSERT INTO achievement (id, canonical_id, source, external_id, name, norm_name, description, "
                       "hidden, redistributable, icon_fail_count, locked_icon_fail_count, key) VALUES "
                       "('01JZ0000000000000000000000', 'atlas:g', 'curator', 'E9', 'Later', 'later', '', false, false, "
                       "0, 0, :k)"), {"k": key})
    db.upgrade(url)
    with mk() as s:
        names = sorted((a.name, a.external_id) for a in s.scalars(select(Achievement).where(
            Achievement.source == "curator")))
    assert names == [("Echo", "E1"), ("Later", "E9")]  # the clashing kept row is dropped, nothing fails
    engine.dispose()
