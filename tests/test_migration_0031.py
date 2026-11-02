"""Migration 0031 (partial and regional release dates, CONTRACTS-v1 C5.2): upgrade on existing data, the
application writing partial dates, downgrade back to what the previous release derives, and upgrade again.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

NOW = "2026-11-02 10:00:00"
COLUMNS = {"release_start", "release_end", "release_precision"}


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


# What a 0030 node holds: views derived by the previous release (release_date only, any lock text kept).
RECORDS = {
    "atlas:day": ({"title": "Day", "release_date": "2011-04-19", "sources": ["steam"]},
                  {"release_date": {"strategy": "vote", "observation_id": "O1", "source": "steam"}}, 2011),
    "atlas:yearlock": ({"title": "Year Lock", "release_date": "2011", "sources": ["steam"]},
                       {"release_date": {"strategy": "lock"}}, 2011),
    "atlas:monthlock": ({"title": "Month Lock", "release_date": "2012-06", "sources": ["steam"]},
                        {"release_date": {"strategy": "lock"}}, 2012),
    "atlas:garbage": ({"title": "Soon", "release_date": "soon", "sources": ["steam"]},
                      {"release_date": {"strategy": "lock"}}, None),
    "atlas:leap": ({"title": "Leap", "release_date": "2024-02-29", "sources": ["gog"]},
                   {"release_date": {"strategy": "vote", "observation_id": "O2", "source": "gog"}}, 2024),
    "atlas:none": ({"title": "Undated", "sources": ["gog"]}, {}, None),
}
LOCKS = {"atlas:yearlock": "2011", "atlas:monthlock": "2012-06", "atlas:garbage": "soon"}


def seed(engine):
    with engine.begin() as c:
        for cid, (view, explain, year) in RECORDS.items():
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, year, view, explain, version, "
                           "created_at, updated_at) VALUES (:i, 'game', :n, :y, :v, :e, 3, :t, :t)"),
                      {"i": cid, "n": view["title"].lower(), "y": year, "v": json.dumps(view),
                       "e": json.dumps(explain), "t": NOW})
        for cid, value in LOCKS.items():
            c.execute(text("INSERT INTO field_lock (canonical_id, field, value, set_by, local_only, created_at) "
                           "VALUES (:c, 'release_date', :v, 'curator', false, :t)"),
                      {"c": cid, "v": json.dumps(value), "t": NOW})


def rows(engine) -> dict[str, dict]:
    cols = COLUMNS & {c["name"] for c in inspect(engine).get_columns("canonical")}
    extra = "".join(f", {c}" for c in sorted(cols))
    out = {}
    with engine.connect() as c:
        for r in c.execute(text(f"SELECT id, year, view, explain, version{extra} FROM canonical")).mappings():
            r = dict(r)
            for k in ("view", "explain"):
                r[k] = json.loads(r[k]) if isinstance(r[k], str) else r[k]
            for k in ("release_start", "release_end"):
                if isinstance(r.get(k), str):  # SQLite returns text through a raw connection
                    r[k] = date.fromisoformat(r[k])
            out[r["id"]] = r
    return out


def test_upgrade_backfills_downgrade_restores_and_reupgrade(url):
    db.upgrade(url, "0030")
    engine = create_engine(url)
    seed(engine)
    before = rows(engine)

    db.upgrade(url)
    assert version(engine) == "0031"
    names = {c["name"] for c in inspect(engine).get_columns("canonical")}
    assert COLUMNS <= names
    assert "ix_canonical_release_start" in {i["name"] for i in inspect(engine).get_indexes("canonical")}
    after = rows(engine)

    day = after["atlas:day"]
    assert day["view"]["release_date"] == "2011-04-19"  # existing day dates keep their meaning
    assert day["view"]["first_release"] == {"date": "2011-04-19", "precision": "day"}
    assert day["view"]["release_year"] == 2011 and day["year"] == 2011
    assert day["explain"]["first_release"] == day["explain"]["release_date"]
    assert (day["release_start"], day["release_end"], day["release_precision"]) == (
        date(2011, 4, 19), date(2011, 4, 19), "day")
    year = after["atlas:yearlock"]
    assert year["view"]["first_release"] == {"date": "2011", "precision": "year"}
    assert "release_date" not in year["view"] and "release_date" not in year["explain"]  # day-only key
    assert (year["release_start"], year["release_end"], year["release_precision"]) == (
        date(2011, 1, 1), date(2011, 12, 31), "year")
    month = after["atlas:monthlock"]
    assert month["view"]["first_release"] == {"date": "2012-06", "precision": "month"}
    assert (month["release_start"], month["release_end"]) == (date(2012, 6, 1), date(2012, 6, 30))
    leap = after["atlas:leap"]
    assert (leap["release_start"], leap["release_precision"]) == (date(2024, 2, 29), "day")
    for untouched in ("atlas:garbage", "atlas:none"):  # not a date, or no date: left exactly as it was
        assert {k: after[untouched][k] for k in ("view", "explain", "year", "version")} == \
               {k: before[untouched][k] for k in ("view", "explain", "year", "version")}
        assert after[untouched]["release_start"] is None and after[untouched]["release_precision"] is None
    assert all(after[cid]["version"] == before[cid]["version"] for cid in RECORDS)  # nothing re-versioned
    with engine.connect() as c:  # locks are never modified
        raw = c.execute(text("SELECT canonical_id, value FROM field_lock")).all()
        sqlite = engine.dialect.name == "sqlite"  # SQLite hands JSON back as text; PostgreSQL decodes it
        assert {r.canonical_id: json.loads(r.value) if sqlite else r.value for r in raw} == LOCKS

    # The application writes what only this release can hold: a partial first release and entries.
    with engine.begin() as c:
        view = {"title": "Partial", "first_release": {"date": "2015-03", "precision": "month"},
                "release_year": 2015, "releases": [{"date": "2015-03", "precision": "month", "platform": "pc",
                                                    "category": "full"}], "sources": ["igdb"]}
        c.execute(text("INSERT INTO canonical (id, media_type, norm_title, year, view, explain, version, created_at, "
                       "updated_at, release_start, release_end, release_precision) VALUES ('atlas:partial', 'game', "
                       "'partial', 2015, :v, :e, 1, :t, :t, :s, :d, 'month')"),
                  {"v": json.dumps(view), "e": json.dumps({"first_release": {"strategy": "vote"},
                                                           "releases": {"strategy": "vote"}}),
                   "t": NOW, "s": date(2015, 3, 1), "d": date(2015, 3, 31)})
        c.execute(text("INSERT INTO field_lock (canonical_id, field, value, set_by, local_only, created_at) "
                       "VALUES ('atlas:day', 'releases', :v, 'curator', false, :t)"),
                  {"v": json.dumps([{"date": "2011", "precision": "year", "category": "full"}]), "t": NOW})

    db.downgrade(url, "0030")
    assert version(engine) == "0030"
    assert not COLUMNS & {c["name"] for c in inspect(engine).get_columns("canonical")}
    assert "ix_canonical_release_start" not in {i["name"] for i in inspect(engine).get_indexes("canonical")}
    down = rows(engine)
    for cid in RECORDS:  # exactly what the previous release derived
        assert {k: down[cid][k] for k in ("view", "explain", "year")} == \
               {k: before[cid][k] for k in ("view", "explain", "year")}, cid
    partial = down["atlas:partial"]
    assert partial["view"] == {"title": "Partial", "sources": ["igdb"]} and partial["explain"] == {}
    assert partial["year"] is None  # the previous release derives the year from release_date only
    with engine.connect() as c:  # the releases lock stays; the previous release ignores unknown lock fields
        assert c.execute(text("SELECT count(*) FROM field_lock WHERE field = 'releases'")).scalar() == 1

    db.upgrade(url)
    again = rows(engine)
    for cid in ("atlas:day", "atlas:yearlock", "atlas:monthlock", "atlas:leap", "atlas:garbage", "atlas:none"):
        assert {k: again[cid][k] for k in ("view", "explain", "year", "release_start", "release_end",
                                           "release_precision")} == \
               {k: after[cid][k] for k in ("view", "explain", "year", "release_start", "release_end",
                                           "release_precision")}, cid
    engine.dispose()
