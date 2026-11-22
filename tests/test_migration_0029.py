"""Migration 0029 (private group channel and profile sync, CONTRACT-C10 §9.2, A11-w): upgrade with the bio and
profile_public seed, downgrade, re-upgrade; existing accounts keep their data both ways.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

TABLES = {"group_stream", "group_change", "group_account_link", "group_account_node", "profile_field",
          "profile_version", "profile_superseded", "profile_conflict", "profile_sync_consent", "group_signin_state",
          "group_web_handle", "group_assertion_seen", "group_idp_consent"}
NODE = "3f2a9c0d1e2b4a55"


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


def add_users(engine) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO node_state (key, value) VALUES ('node_id', :n)"), {"n": NODE})
        for uid, name, bio, public, deleted in (
                ("01JBA4AAAAAAAAAAAAAAAAAAA1", "ann", "Hello from Ann", True, None),
                ("01JBA4AAAAAAAAAAAAAAAAAAA2", "ben", "", False, None),
                ("01JBA4AAAAAAAAAAAAAAAAAAA3", "gone", "", False, "2026-10-01 10:00:00")):
            c.execute(text("INSERT INTO app_user (id, username, password_hash, role, status, bio, profile_public, "
                           "reputation, email_notifications, oidc_allowed, created_at, deleted_at) VALUES "
                           "(:id, :name, '!', 'user', 'active', :bio, :public, 0, false, true, '2026-01-01 10:00:00', "
                           ":deleted)"),
                      {"id": uid, "name": name, "bio": bio, "public": public, "deleted": deleted})


def test_migration_0029_up_seed_down_and_up_again(url):
    db.upgrade(url, "0028")
    engine = create_engine(url)
    add_users(engine)
    db.upgrade(url, "0029")
    assert version(engine) == "0029"
    assert TABLES <= set(inspect(engine).get_table_names())
    assert "ix_group_change_scope" in {i["name"] for i in inspect(engine).get_indexes("group_change")}
    with engine.connect() as c:
        fields = c.execute(text("SELECT user_id, field, value, origin_node, cause FROM profile_field "
                                "ORDER BY user_id, field")).all()
        versions = c.execute(text("SELECT user_id, field, hlc, is_head, cause, base, entry_ref FROM profile_version "
                                  "ORDER BY user_id, field")).all()
    raw = engine.dialect.name == "sqlite"  # SQLite hands JSON columns back as text

    def load(v):
        return json.loads(v) if raw else v

    seeded = {(u, f): load(v) for u, f, v, _, _ in fields}
    assert seeded == {("01JBA4AAAAAAAAAAAAAAAAAAA1", "bio"): "Hello from Ann",
                      ("01JBA4AAAAAAAAAAAAAAAAAAA1", "privacy.profile_public"): True,
                      ("01JBA4AAAAAAAAAAAAAAAAAAA2", "bio"): "",
                      ("01JBA4AAAAAAAAAAAAAAAAAAA2", "privacy.profile_public"): False}  # the deleted account: none
    assert {(o, cause) for _, _, _, o, cause in fields} == {(NODE, "migration")}
    assert len(versions) == 4
    for _, _, hlc, is_head, cause, base, entry_ref in versions:
        assert hlc.endswith(f"-0000-{NODE}") and len(hlc) == 13 + 1 + 4 + 1 + 16
        assert bool(is_head) and cause == "migration" and entry_ref is None
        assert load(base) == []

    with engine.begin() as c:  # rows the models write after the upgrade
        c.execute(text("INSERT INTO profile_conflict (id, user_id, field, kind, status, detected_at) VALUES "
                       "('01JBA4AAAAAAAAAAAAAAAAAAC1', '01JBA4AAAAAAAAAAAAAAAAAAA1', 'theme', 'concurrent', 'open', "
                       "'2026-11-01 10:00:00')"))
        c.execute(text("INSERT INTO group_stream (group_id, origin, epoch, scope, key_id, opened_at) VALUES "
                       "('9c1e4b7a2f30d811', :n, '0a0b0c0d', '_node', 'k', '2026-11-01 10:00:00')"), {"n": NODE})
    with engine.connect() as c:
        assert c.execute(text("SELECT hwm FROM group_stream")).scalar() == 0  # server default

    db.downgrade(url, "0028")
    assert version(engine) == "0028"
    assert not TABLES & set(inspect(engine).get_table_names())
    with engine.connect() as c:  # app_user keeps every account's bio and visibility
        rows = c.execute(text("SELECT username, bio, profile_public FROM app_user ORDER BY username")).all()
    assert [(n, b, bool(p)) for n, b, p in rows] == [("ann", "Hello from Ann", True), ("ben", "", False),
                                                      ("gone", "", False)]

    db.upgrade(url, "0029")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM profile_field")).scalar() == 4
    engine.dispose()


def test_migration_0029_on_an_empty_database(url):
    db.upgrade(url, "0029")  # pinned: 0030 (A12) is the head now
    engine = create_engine(url)
    assert version(engine) == "0029"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM profile_version")).scalar() == 0
    db.downgrade(url, "0028")
    assert version(engine) == "0028"
    engine.dispose()
