"""Migration 0030 (delegation grants and federated sessions, CONTRACT-C10 §9.3, A12-p): upgrade, write a grant and
a federated session, downgrade (federated sessions are deleted, local ones stay), and upgrade again.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

GROUP = "9c1e4b7a2f30d811"
NODE = "3f2a9c0d1e2b4a55"
NOW = "2026-11-01 10:00:00"
COLUMNS = {"via_group", "origin_node", "assertion_id", "amr"}


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


def seed(engine) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO app_user (id, username, password_hash, role, status, bio, profile_public, "
                       "reputation, email_notifications, oidc_allowed, created_at) VALUES "
                       "('01JBA4AAAAAAAAAAAAAAAAAAA1', 'ann', '!', 'user', 'active', '', false, 0, false, true, :now)"),
                  {"now": NOW})
        c.execute(text("INSERT INTO user_session (id, user_id, csrf, created_at, expires_at) VALUES "
                       "('local-session-hash', '01JBA4AAAAAAAAAAAAAAAAAAA1', 'c', :now, '2026-12-01 10:00:00')"),
                  {"now": NOW})
        c.execute(text("INSERT INTO owner_group (group_id, name, genesis, succession, root_key_id, recovery, "
                       "root_sealed, box_sealed, root_state, status, joined_at, updated_at) VALUES "
                       "(:g, 'Home', '{}', '[]', :g, '{}', NULL, 'enc:v1:x', 'none', 'active', :now, :now)"),
                  {"g": GROUP, "now": NOW})


def test_migration_0030_up_down_and_up_again(url):
    db.upgrade(url, "0029")
    engine = create_engine(url)
    seed(engine)
    db.upgrade(url, "0030")
    assert version(engine) == "0030"
    insp = inspect(engine)
    assert "group_grant" in insp.get_table_names()
    assert COLUMNS <= {c["name"] for c in insp.get_columns("user_session")}
    assert "ix_user_session_origin" in {i["name"] for i in insp.get_indexes("user_session")}
    assert "ix_group_grant_account" in {i["name"] for i in insp.get_indexes("group_grant")}
    with engine.connect() as c:  # existing sessions are local: all four provenance columns NULL
        row = c.execute(text("SELECT via_group, origin_node, assertion_id, amr FROM user_session")).one()
        assert tuple(row) == (None, None, None, None)
    with engine.begin() as c:  # what the models write after the upgrade
        c.execute(text("INSERT INTO group_grant (group_id, serial, issuer, serial_n, parent_serial, "
                       "authorized_by_grant, account_uid, role, nodes, not_before, not_after, body, body_hash, "
                       "received_at, held_reason) VALUES (:g, 'root/2-0123456789abcdef', 'root', 2, NULL, NULL, "
                       "'01JBA4AAAAAAAAAAAAAAAAAAU1', 'moderator', :nodes, :now, '2026-12-01 10:00:00', '{}', :h, "
                       ":now, NULL)"), {"g": GROUP, "nodes": f'["{NODE}"]', "now": NOW, "h": "a" * 64})
        c.execute(text("INSERT INTO user_session (id, user_id, csrf, created_at, expires_at, via_group, origin_node, "
                       "assertion_id, amr) VALUES ('federated-session-hash', '01JBA4AAAAAAAAAAAAAAAAAAA1', 'c', :now, "
                       "'2026-12-01 10:00:00', :g, :n, '01JBA4AAAAAAAAAAAAAAAAAAJ1', '[\"pwd\"]')"),
                  {"g": GROUP, "n": NODE, "now": NOW})
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM user_session WHERE via_group IS NOT NULL")).scalar() == 1
        assert c.execute(text("SELECT role FROM group_grant")).scalar() == "moderator"

    db.downgrade(url, "0029")
    assert version(engine) == "0029"
    insp = inspect(engine)
    assert "group_grant" not in insp.get_table_names()
    assert not COLUMNS & {c["name"] for c in insp.get_columns("user_session")}
    with engine.connect() as c:  # the federated session is gone, the local one stays
        assert [r[0] for r in c.execute(text("SELECT id FROM user_session"))] == ["local-session-hash"]
        assert c.execute(text("SELECT username FROM app_user")).scalar() == "ann"

    db.upgrade(url, "0030")
    assert version(engine) == "0030"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM group_grant")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM user_session")).scalar() == 1
    engine.dispose()


def test_migration_0030_on_an_empty_database(url):
    db.upgrade(url)
    engine = create_engine(url)
    assert version(engine) == "0030"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM group_grant")).scalar() == 0
    db.downgrade(url, "0029")
    assert version(engine) == "0029"
    engine.dispose()
