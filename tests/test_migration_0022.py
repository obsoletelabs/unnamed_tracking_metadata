"""Migration 0022 (stable achievement key): backfill, NOT NULL, unique index, downgrade and re-upgrade.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, exc, inspect, text

from atlas import db
from atlas.achievement_key import ak1, is_key

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

NOW = "2026-10-31 12:00:00"
LONG_SOURCE = "s" * 50  # the longest normalised source, so the identity is longer than 200 characters
LONG_EXT = "E" * 200


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


ROWS = [  # id, canonical, source, external_id, name, norm_name
    ("01AAAAAAAAAAAAAAAAAAAAAAA1", "atlas:one", "steam", "ACH_WIN", "Winner", "winner"),
    ("01AAAAAAAAAAAAAAAAAAAAAAA2", "atlas:one", "curator", "", "Winner", "winner"),
    ("01AAAAAAAAAAAAAAAAAAAAAAA3", "atlas:one", "steam", "ACH_WIN", "Winner Again", "winner again"),  # same ext id
    ("01AAAAAAAAAAAAAAAAAAAAAAA4", "atlas:one", LONG_SOURCE, LONG_EXT, "Long", "long"),
    ("01AAAAAAAAAAAAAAAAAAAAAAA5", "atlas:two", "steam", "ACH_WIN", "Winner", "winner"),  # another record
]


def seed(engine):
    with engine.begin() as c:
        for cid in ("atlas:one", "atlas:two"):
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', '', '{}', '{}', 1, :t, :t)"), {"i": cid, "t": NOW})
        for rid, cid, src, ext, name, norm in ROWS:
            c.execute(text(
                "INSERT INTO achievement (id, canonical_id, source, external_id, name, norm_name, description, "
                "hidden, redistributable, icon_fail_count, locked_icon_fail_count, position) "
                "VALUES (:i, :c, :s, :e, :n, :nn, 'd', false, false, 0, 0, NULL)"),
                {"i": rid, "c": cid, "s": src, "e": ext, "n": name, "nn": norm})


def keys(engine) -> dict[str, str]:
    with engine.connect() as c:
        return {r.id: r.key for r in c.execute(text("SELECT id, key FROM achievement"))}


def test_backfill_unique_index_downgrade_and_reupgrade(url):
    db.upgrade(url, "0021")
    engine = create_engine(url)
    seed(engine)

    db.upgrade(url)
    first = keys(engine)
    assert set(first) == {r[0] for r in ROWS} and all(is_key(k) for k in first.values())
    assert first[ROWS[0][0]] == ak1("steam:ACH_WIN")
    assert first[ROWS[1][0]] == ak1("curator~winner")
    assert first[ROWS[2][0]] == ak1("steam~winner again")  # a repeated external id takes the name form
    assert first[ROWS[3][0]] == ak1(f"{LONG_SOURCE}:{LONG_EXT}") and len(first[ROWS[3][0]]) == 30
    assert first[ROWS[4][0]] == first[ROWS[0][0]]  # the key leaves out the canonical id
    column = next(c for c in inspect(engine).get_columns("achievement") if c["name"] == "key")
    assert column["nullable"] is False
    index = next(i for i in inspect(engine).get_indexes("achievement") if i["name"] == "uq_achievement_key")
    assert index["unique"] and index["column_names"] == ["canonical_id", "key"]
    with pytest.raises(exc.IntegrityError), engine.begin() as c:  # the unique index holds
        c.execute(text("UPDATE achievement SET key = :k WHERE id = :i"),
                  {"k": first[ROWS[0][0]], "i": ROWS[1][0]})
    with pytest.raises(exc.IntegrityError), engine.begin() as c:  # and NOT NULL does
        c.execute(text("UPDATE achievement SET key = NULL WHERE id = :i"), {"i": ROWS[1][0]})

    command.downgrade(db.alembic_config(url), "0021")
    engine.dispose()
    engine = create_engine(url)
    assert "key" not in {c["name"] for c in inspect(engine).get_columns("achievement")}
    assert "uq_achievement_key" not in {i["name"] for i in inspect(engine).get_indexes("achievement")}
    with engine.connect() as c:  # every row and its id survives the rollback
        assert {r.id: (r.name, r.external_id) for r in c.execute(text("SELECT id, name, external_id FROM achievement"))
                } == {r[0]: (r[4], r[3]) for r in ROWS}

    db.upgrade(url)
    engine.dispose()
    assert keys(create_engine(url)) == first  # re-upgrading derives the same keys
