"""Migration 0039 (CONTRACT-C14 C14-2): `group_assist_item`, `assist_lookup_op`, `group_contrib_state` and
`observation.assist_item_id`. A downgrade is refused while group-received observations exist, unless
ATLAS_DOWNGRADE_FORCE=1, and then also drops the assist node state.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

PREVIOUS, HEAD = "0038", "0039"
CID = "atlas:01m70000000000000000000001"


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


def test_assist_schema_up_and_down(url, monkeypatch):
    db.upgrade(url, PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO node_state (key, value) VALUES ('assist_backfill:g1', '{}')"))
        db.upgrade(url, HEAD)
        engine.dispose()
        insp = inspect(engine)
        assert version(engine) == HEAD
        assert {"group_assist_item", "assist_lookup_op", "group_contrib_state"} <= set(insp.get_table_names())
        assert "assist_item_id" in {c["name"] for c in insp.get_columns("observation")}
        pk = insp.get_pk_constraint("group_contrib_state")["constrained_columns"]
        assert pk == ["origin_node", "contribution_id"]
        assert set(insp.get_pk_constraint("assist_lookup_op")["constrained_columns"]) == {"asker", "op_id"}
        with engine.begin() as c:
            assert c.execute(text("SELECT count(*) FROM node_state WHERE key LIKE 'assist_backfill:%'")).scalar() == 0
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', 'kept', '{}', '{}', 1, CURRENT_TIMESTAMP, "
                           "CURRENT_TIMESTAMP)"), {"i": CID})
            c.execute(text("INSERT INTO observation (id, canonical_id, source_kind, source, source_scope, "
                           "external_id, payload, payload_hash, redistributable, is_current, observed_at, "
                           "assist_item_id) VALUES ('01M70000000000000000000001', :i, 'metadata', 'atlas-group', "
                           "'aaaaaaaaaaaaaaaa', :i, '{}', 'h', false, true, CURRENT_TIMESTAMP, 'item1')"), {"i": CID})
            c.execute(text("INSERT INTO node_state (key, value) VALUES ('assist_job:abc', '{}')"))

        monkeypatch.delenv("ATLAS_DOWNGRADE_FORCE", raising=False)
        with pytest.raises(Exception, match="group"):
            db.downgrade(url, PREVIOUS)
        engine.dispose()
        assert version(engine) == HEAD

        monkeypatch.setenv("ATLAS_DOWNGRADE_FORCE", "1")
        db.downgrade(url, PREVIOUS)
        engine.dispose()
        insp = inspect(engine)
        assert version(engine) == PREVIOUS
        assert "group_assist_item" not in insp.get_table_names()
        assert "assist_item_id" not in {c["name"] for c in insp.get_columns("observation")}
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM node_state WHERE key LIKE 'assist_%'")).scalar() == 0
        monkeypatch.delenv("ATLAS_DOWNGRADE_FORCE")
        db.upgrade(url, HEAD)
        engine.dispose()
        assert version(engine) == HEAD
    finally:
        engine.dispose()
