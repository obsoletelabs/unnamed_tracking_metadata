"""Migration 0037 (SPEC-UI-POLISH-PASS §4.3, T-P8): `group_enrolment.preset` and `preset_admin`. Upgrade a 0036
database holding an enrolment, check the defaults, downgrade and upgrade again.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

PREVIOUS, HEAD = "0036", "0037"


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


def columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("group_enrolment")}


def version(engine) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_enrolment_preset_columns_up_and_down(url):
    db.upgrade(url, PREVIOUS)
    engine = create_engine(url)
    try:
        before = columns(engine)
        assert "preset" not in before and "preset_admin" not in before
        db.upgrade(url, HEAD)
        assert version(engine) == HEAD and {"preset", "preset_admin"} <= columns(engine)
        db.downgrade(url, PREVIOUS)
        assert version(engine) == PREVIOUS and "preset" not in columns(engine)
        db.upgrade(url, HEAD)
        assert {"preset", "preset_admin"} <= columns(engine)
        cols = {c["name"]: c for c in inspect(engine).get_columns("group_enrolment")}
        assert cols["preset"]["nullable"] and not cols["preset_admin"]["nullable"]
    finally:
        engine.dispose()
