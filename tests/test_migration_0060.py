"""Migration 0060 (CONTRACT-C15 §2.7, A20): `media_type.achievements`, true for every existing type. Upgrade a 0037
database that holds built-in, custom and inherited types, check the values, downgrade and upgrade again.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

PREVIOUS, HEAD = "0037", "0060"


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


def test_achievements_flag_is_true_for_every_existing_type(url):
    db.upgrade(url, PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            for key, origin in (("game", "local"), ("anime", "local"), ("comic", "local"), ("manga", "upstream")):
                c.execute(text("INSERT INTO media_type (key, label, enabled, position, labels, origin) "
                               "VALUES (:k, :k, :e, 1, '{}', :o)"), {"k": key, "o": origin, "e": True})
        assert "achievements" not in {c["name"] for c in inspect(engine).get_columns("media_type")}
        db.upgrade(url, HEAD)
        assert version(engine) == HEAD
        with engine.connect() as c:
            rows = dict(c.execute(text("SELECT key, achievements FROM media_type")).all())
        assert rows and all(bool(v) for v in rows.values()), rows
        cols = {c["name"]: c for c in inspect(engine).get_columns("media_type")}
        assert not cols["achievements"]["nullable"]
        db.downgrade(url, PREVIOUS)
        assert version(engine) == PREVIOUS
        assert "achievements" not in {c["name"] for c in inspect(engine).get_columns("media_type")}
        db.upgrade(url, HEAD)
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM media_type WHERE achievements")).scalar() == len(rows)
    finally:
        engine.dispose()
