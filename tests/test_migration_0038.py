"""Migration 0038 (CONTRACT-C14 C14-1, T-S8): `canonical.scope_ceiling(_by)`, `scope_projection` and
`user_permission`. Upgrade a 0037 database, check the new schema and the rebuild marker; a downgrade is refused while
a ceiling exists; with ATLAS_DOWNGRADE_FORCE=1 the ceilings are kept aside and restored by the next upgrade.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

PREVIOUS, HEAD = "0037", "0038"
CID = "atlas:01m70000000000000000000000"


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


def state(engine, key):
    with engine.connect() as c:
        return c.execute(text("SELECT value FROM node_state WHERE key = :k"), {"k": key}).scalar()


def test_scope_schema_up_and_down_keeps_ceilings(url, monkeypatch):
    db.upgrade(url, PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', 'kept', '{}', '{}', 1, CURRENT_TIMESTAMP, "
                           "CURRENT_TIMESTAMP)"), {"i": CID})
        db.upgrade(url, HEAD)
        insp = inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("canonical")}
        assert version(engine) == HEAD
        assert cols["scope_ceiling"]["nullable"] and cols["scope_ceiling_by"]["nullable"]
        assert {"scope_projection", "user_permission"} <= set(insp.get_table_names())
        assert state(engine, "scope_projection_pending") == ""  # the worker rebuilds the projection
        with engine.begin() as c:
            assert c.execute(text("SELECT scope_ceiling FROM canonical WHERE id = :i"), {"i": CID}).scalar() is None
            c.execute(text("UPDATE canonical SET scope_ceiling = 'local', scope_ceiling_by = 'operator' "
                           "WHERE id = :i"), {"i": CID})

        monkeypatch.delenv("ATLAS_DOWNGRADE_FORCE", raising=False)
        with pytest.raises(Exception, match="scope ceiling"):
            db.downgrade(url, PREVIOUS)
        engine.dispose()
        assert version(engine) == HEAD  # refused: nothing changed

        monkeypatch.setenv("ATLAS_DOWNGRADE_FORCE", "1")
        db.downgrade(url, PREVIOUS)
        engine.dispose()
        assert version(engine) == PREVIOUS
        assert "scope_ceiling" not in {c["name"] for c in inspect(engine).get_columns("canonical")}
        assert "scope_projection" not in inspect(engine).get_table_names()
        assert '"local"' in (state(engine, "c14_0038_kept") or "")

        monkeypatch.delenv("ATLAS_DOWNGRADE_FORCE")
        db.upgrade(url, HEAD)
        engine.dispose()
        with engine.connect() as c:
            row = c.execute(text("SELECT scope_ceiling, scope_ceiling_by FROM canonical WHERE id = :i"),
                            {"i": CID}).one()
        assert tuple(row) == ("local", "operator")
        assert state(engine, "c14_0038_kept") is None
    finally:
        engine.dispose()
