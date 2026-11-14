"""Migration 0028 (owner groups): upgrade, refused and allowed downgrade, re-upgrade, existing data untouched.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

TABLES = {"owner_group", "group_cert", "group_banned_node", "group_view", "group_member", "group_crl",
          "group_revoked", "group_enrolment", "group_nonce"}
NOW = "2026-11-01 10:00:00"


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


def add_group(engine, status: str, root_sealed: str | None) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO owner_group (group_id, name, genesis, succession, root_key_id, recovery, "
                       "root_sealed, box_sealed, root_state, status, joined_at, updated_at) VALUES "
                       "('9c1e4b7a2f30d811', 'Home', '{}', '[]', '9c1e4b7a2f30d811', '{}', :root, 'enc:v1:x', "
                       ":state, :status, :now, :now)"),
                  {"root": root_sealed, "state": "holder" if root_sealed else "none", "status": status, "now": NOW})
        c.execute(text("INSERT INTO group_member (group_id, node_id, root_public_key, box_public_key, url, name, "
                       "serial, scopes, not_after, status, rotations) VALUES ('9c1e4b7a2f30d811', '3f2a9c0d1e2b4a55', "
                       "'k', 'b', 'http://a.test', 'A', 'root/1-0123456789abcdef', '[]', :now, 'active', '[]')"),
                  {"now": NOW})


def test_migration_0028_up_down_and_guards(url):
    db.upgrade(url, "0024")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO node_state (key, value) VALUES ('m28-probe', 'kept')"))
    db.upgrade(url, "0028")
    assert version(engine) == "0028"
    tables = set(inspect(engine).get_table_names())
    assert TABLES <= tables
    indexes = {i["name"] for i in inspect(engine).get_indexes("group_cert")}
    assert "ix_group_cert_node" in indexes
    with engine.connect() as c:  # existing data untouched
        assert c.execute(text("SELECT value FROM node_state WHERE key='m28-probe'")).scalar() == "kept"
        defaults = c.execute(text("SELECT root_state FROM owner_group")).all()
        assert defaults == []

    add_group(engine, "active", "enc:v1:sealed-root")
    with pytest.raises(RuntimeError, match="still in an owner group"):
        db.downgrade(url, "0024")
    assert version(engine) == "0028" and TABLES <= set(inspect(engine).get_table_names())

    with engine.begin() as c:  # left, but still holding the sealed root: still refused
        c.execute(text("UPDATE owner_group SET status='left'"))
    with pytest.raises(RuntimeError, match="sealed group root"):
        db.downgrade(url, "0024")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM group_member")).scalar() == 1  # nothing was dropped

    with engine.begin() as c:
        c.execute(text("UPDATE owner_group SET root_sealed=NULL, root_state='none'"))
    db.downgrade(url, "0024")
    assert version(engine) == "0024"
    assert not TABLES & set(inspect(engine).get_table_names())
    with engine.connect() as c:
        assert c.execute(text("SELECT value FROM node_state WHERE key='m28-probe'")).scalar() == "kept"

    db.upgrade(url, "0028")  # and up again
    assert version(engine) == "0028" and TABLES <= set(inspect(engine).get_table_names())
    with engine.begin() as c:
        add_group(engine, "active", None)
        c.execute(text("INSERT INTO group_enrolment (id, side, group_id, invite_hash, secret_enc, scopes, state, "
                       "created_at, expires_at) VALUES ('01JBA4AAAAAAAAAAAAAAAAAAAA', 'inviter', '9c1e4b7a2f30d811', "
                       "'h', 'enc:v1:x', '[]', 'open', :now, :now)"), {"now": NOW})
    with engine.connect() as c:  # server defaults the models rely on
        row = c.execute(text("SELECT url_verified, failures FROM group_enrolment")).one()
        assert (bool(row[0]), row[1]) == (False, 0)
    engine.dispose()


def test_migration_0028_on_an_empty_database(url):
    db.upgrade(url, "0028")
    engine = create_engine(url)
    assert version(engine) == "0028"
    db.downgrade(url, "0024")
    assert version(engine) == "0024"
    engine.dispose()
