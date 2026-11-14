"""Migration 0041 (C14-4, super group): `group_write`, `group_write_state`, `group_write_conflict`. Nothing existing
changes. A downgrade is refused while observations of replicated writes exist, unless ATLAS_DOWNGRADE_FORCE=1, and
then drops them (the record keeps its other observations) and the `super_group_*` node state.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

PREVIOUS, HEAD = "0040", "0041"
TABLES = {"group_write", "group_write_state", "group_write_conflict"}
CID = "atlas:01m70000000000000000000041"
CID2 = "atlas:01m70000000000000000000042"


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


def test_super_group_schema_up_and_down(url, monkeypatch):
    db.upgrade(url, PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', 'kept', '{}', '{}', 1, CURRENT_TIMESTAMP, "
                           "CURRENT_TIMESTAMP)"), {"i": CID})
            c.execute(text("INSERT INTO observation (id, canonical_id, source_kind, source, source_scope, "
                           "external_id, payload, payload_hash, redistributable, is_current, observed_at) VALUES "
                           "('01M70000000000000000000041', :i, 'metadata', 'community', 'atlas', :i, '{}', 'h', "
                           "true, true, CURRENT_TIMESTAMP)"), {"i": CID})
        db.upgrade(url, HEAD)
        engine.dispose()
        insp = inspect(engine)
        assert version(engine) == HEAD
        assert TABLES <= set(insp.get_table_names())
        assert insp.get_pk_constraint("group_write_state")["constrained_columns"] == ["subject"]
        assert "tried_at" in {c["name"] for c in insp.get_columns("group_write_state")}
        with engine.begin() as c:
            assert c.execute(text("SELECT count(*) FROM observation")).scalar() == 1  # nothing existing changed
            c.execute(text("INSERT INTO observation (id, canonical_id, source_kind, source, source_scope, "
                           "external_id, payload, payload_hash, redistributable, is_current, observed_at) VALUES "
                           "('01M70000000000000000000042', :i, 'metadata', 'group-write', 'group:write', :i, '{}', "
                           "'h', false, true, CURRENT_TIMESTAMP)"), {"i": CID})
            c.execute(text("INSERT INTO group_write (op_id, origin_node, seq, kind, subject, hlc, status, envelope, "
                           "received_at) VALUES ('aaaaaaaaaaaaaaaa:1', 'aaaaaaaaaaaaaaaa', 1, 'record.field', "
                           ":s, '0000000000001.00000.aaaaaaaaaaaaaaaa', 'applied', '{}', CURRENT_TIMESTAMP)"),
                      {"s": f"rec:{CID}/title"})
            c.execute(text("INSERT INTO node_state (key, value) VALUES ('super_group_seq', '1')"))
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at, scope_ceiling, scope_ceiling_by) VALUES (:i, 'game', 'written', '{}', '{}', "
                           "1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'group', 'write')"), {"i": CID2})

        monkeypatch.delenv("ATLAS_DOWNGRADE_FORCE", raising=False)
        with pytest.raises(Exception, match="super group"):
            db.downgrade(url, PREVIOUS)
        engine.dispose()
        assert version(engine) == HEAD

        monkeypatch.setenv("ATLAS_DOWNGRADE_FORCE", "1")
        db.downgrade(url, PREVIOUS)
        engine.dispose()
        insp = inspect(engine)
        assert version(engine) == PREVIOUS
        assert not TABLES & set(insp.get_table_names())
        with engine.begin() as c:
            assert c.execute(text("SELECT source FROM observation")).scalars().all() == ["community"]
            assert c.execute(text("SELECT count(*) FROM node_state WHERE key LIKE 'super_group_%'")).scalar() == 0
            # a record first written from the group keeps its group ceiling, under a name the old release knows
            assert c.execute(text("SELECT scope_ceiling, scope_ceiling_by FROM canonical WHERE id = :i"),
                             {"i": CID2}).one() == ("group", "operator")
            # every view is re-derived without the dropped observations after the next upgrade
            assert c.execute(text("SELECT value FROM node_state WHERE key = 'view_rebuild_pending'")).scalar()
    finally:
        engine.dispose()
