"""Migration 0040 (CONTRACT-C14 C14-3): node links. Main side `node_link`, `link_challenge`, `link_device`,
`link_refresh`, `link_nonce`, `link_banned_key`, `link_contribution_answer` and the new `proposal`/`oidc_access`/
columns; node side `upstream_link`, `upstream_contribution`, `upstream_alias`. A downgrade is refused
while a link is active or a contribution is undecided, unless ATLAS_DOWNGRADE_FORCE=1, and then also drops the
`feed.access` grants and the pairwise-subject key.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

PREVIOUS, HEAD = "0039", "0040"
TABLES = {"node_link", "link_challenge", "link_device", "link_refresh", "link_nonce", "link_banned_key",
          "link_contribution_answer", "upstream_link", "upstream_contribution", "upstream_alias"}


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


def test_node_link_schema_up_and_down(url, monkeypatch):
    db.upgrade(url, PREVIOUS)
    engine = create_engine(url)
    try:
        db.upgrade(url, HEAD)
        engine.dispose()
        insp = inspect(engine)
        assert version(engine) == HEAD
        assert TABLES <= set(insp.get_table_names())
        assert {"via_link_id", "contribution_id", "stale_base", "link_flag"} <= {
            c["name"] for c in insp.get_columns("proposal")}
        assert {"link_id", "key_epoch", "jkt"} <= {c["name"] for c in insp.get_columns("oidc_access")}
        assert set(insp.get_pk_constraint("link_nonce")["constrained_columns"]) == {"jkt", "jti"}
        uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("upstream_contribution")}
        assert ("main_node_id", "seq") in uniques
        with engine.begin() as c:
            c.execute(text("INSERT INTO app_user (id, username, password_hash, role, reputation, status, created_at) "
                           "VALUES ('01M70000000000000000000001', 'dave', '!', 'user', 0, 'active', "
                           "CURRENT_TIMESTAMP)"))
            c.execute(text("INSERT INTO node_link (id, user_id, node_id, root_public_key, current_key_id, "
                           "current_jkt, key_epoch, rotations, subject, name, scopes, limits, review, trust, "
                           "reputation, show_name, last_seq, status, created_at) VALUES "
                           "('01M70000000000000000000002', '01M70000000000000000000001', 'aaaaaaaaaaaaaaaa', 'k', "
                           "'aaaaaaaaaaaaaaaa', 'j', 1, '[]', 'sub', 'NAS', '[]', '{}', 'standard', 'probation', 0, "
                           "false, 0, 'active', CURRENT_TIMESTAMP)"))
            c.execute(text("INSERT INTO user_permission (user_id, permission, granted_at) VALUES "
                           "('01M70000000000000000000001', 'feed.access', CURRENT_TIMESTAMP)"))
            c.execute(text("INSERT INTO node_state (key, value) VALUES ('link_pairwise_key', 'x')"))

        monkeypatch.delenv("ATLAS_DOWNGRADE_FORCE", raising=False)
        with pytest.raises(Exception, match="node link"):
            db.downgrade(url, PREVIOUS)
        engine.dispose()
        assert version(engine) == HEAD

        monkeypatch.setenv("ATLAS_DOWNGRADE_FORCE", "1")
        db.downgrade(url, PREVIOUS)
        engine.dispose()
        insp = inspect(engine)
        assert version(engine) == PREVIOUS
        assert not TABLES & set(insp.get_table_names())
        assert "via_link_id" not in {c["name"] for c in insp.get_columns("proposal")}
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM user_permission WHERE permission = 'feed.access'")
                             ).scalar() == 0
            assert c.execute(text("SELECT count(*) FROM node_state WHERE key = 'link_pairwise_key'")).scalar() == 0
        monkeypatch.delenv("ATLAS_DOWNGRADE_FORCE")
        db.upgrade(url, HEAD)
        engine.dispose()
        assert version(engine) == HEAD
    finally:
        engine.dispose()
