"""Migration 0021 (federation compatibility): upgrade backfills existing rows, downgrade keeps what matters.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

NOW = "2026-10-31 12:00:00"


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


def columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_upgrade_backfills_and_downgrade_keeps_parked_rows(url):
    db.upgrade(url, "0020")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO replicated_feed (node_id, public_key) VALUES ('n1', 'k')"))
        c.execute(text("INSERT INTO replicated_change (node_id, seq, kind, body, body_hash, signature, key_id, "
                       "created_at) VALUES ('n1', 1, 'obs', '{}', 'h', 's', 'k', :t)"), {"t": NOW})
        c.execute(text("INSERT INTO sync_failure (node_id, seq, kind, error, attempts, first_failed_at, "
                       "last_attempt_at) VALUES ('n1', 1, 'obs', 'ValueError: old', 3, :t, :t)"), {"t": NOW})

    db.upgrade(url)
    with engine.begin() as c:
        row = c.execute(text("SELECT disposition, reason, detail, attempts, error FROM sync_failure")).one()
        assert tuple(row) == ("quarantined", "apply_error", None, 3, "ValueError: old")  # backfilled, nothing lost
        c.execute(text("INSERT INTO replicated_change (node_id, seq, kind, body, body_hash, signature, key_id, "
                       "created_at) VALUES ('n1', 2, 'a_kind_name_of_32_characters_xx', '{}', 'h', 's', 'k', :t)"),
                  {"t": NOW})
        c.execute(text("INSERT INTO sync_failure (node_id, seq, kind, error, attempts, first_failed_at, last_attempt_at,"
                       " disposition, reason) VALUES ('n1', 2, 'relation', 'parked', 1, :t, :t, 'parked', "
                       "'unsupported_kind')"), {"t": NOW})
        c.execute(text("INSERT INTO follower_seen (token_id, protocol, last_seen_at) VALUES ('t', '1.1', :t)"),
                  {"t": NOW})
        c.execute(text("INSERT INTO node_state (key, value) VALUES ('sync_downloaded:n1', '2'), "
                       "('sync_refused:n1', '{}'), ('sync_protocol:n1', '1.1'), ('sync_head:n1', '2')"))
    assert {"disposition", "reason", "detail"} <= columns(engine, "sync_failure")
    assert "remote_protocol" in columns(engine, "peer")

    # A retained kind longer than the old column refuses the downgrade instead of truncating it.
    with pytest.raises(RuntimeError, match="longer than 16"):
        command.downgrade(db.alembic_config(url), "0020")
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM replicated_change")).scalar() == 2  # refused cleanly
        c.execute(text("UPDATE replicated_change SET kind = 'relation' WHERE seq = 2"))

    command.downgrade(db.alembic_config(url), "0020")
    engine.dispose()
    engine = create_engine(url)
    assert {"disposition", "reason", "detail"}.isdisjoint(columns(engine, "sync_failure"))
    assert "remote_protocol" not in columns(engine, "peer")
    assert "follower_seen" not in inspect(engine).get_table_names()
    with engine.begin() as c:
        # The parked row survives as an ordinary open failure the older release shows and retries.
        rows = c.execute(text("SELECT seq, kind, resolved_at FROM sync_failure ORDER BY seq")).all()
        assert [tuple(r) for r in rows] == [(1, "obs", None), (2, "relation", None)]
        keys = {k for (k,) in c.execute(text("SELECT key FROM node_state"))}
        assert "sync_head:n1" in keys and not keys & {"sync_downloaded:n1", "sync_refused:n1", "sync_protocol:n1"}
        assert c.execute(text("SELECT count(*) FROM replicated_change")).scalar() == 2

    db.upgrade(url)  # and forward again
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM sync_failure WHERE disposition = 'quarantined'")).scalar() == 2
    engine.dispose()
