"""Migration 0023 (local client change log): upgrade, data written through the model, downgrade, re-upgrade.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, inspect, text

from atlas import client_changes, db, service
from atlas.models import Canonical, ClientChange, NodeState
from atlas.service import Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings
from .test_migration_0022 import url  # noqa: F401  (sqlite + postgresql fixture)
from .test_postgres import pg_url  # noqa: F401

NOW = "2026-10-31 12:00:00"


def test_create_use_downgrade_and_reupgrade(url):  # noqa: F811
    db.upgrade(url, "0022")
    engine = create_engine(url)
    with engine.begin() as c:  # a record that exists before the upgrade
        c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                       "updated_at) VALUES ('atlas:old', 'game', '', '{}', '{}', 7, :t, :t)"), {"t": NOW})
    assert "client_change" not in inspect(engine).get_table_names()

    db.upgrade(url)
    engine.dispose()
    engine = create_engine(url)
    cols = {c["name"]: c for c in inspect(engine).get_columns("client_change")}
    assert set(cols) == {"seq", "canonical_id", "version", "cause", "origin_node", "origin_seq", "created_at"}
    assert not cols["canonical_id"]["nullable"] and cols["origin_node"]["nullable"]
    index = next(i for i in inspect(engine).get_indexes("client_change") if i["name"] == "ix_client_change_cid")
    assert index["column_names"] == ["canonical_id", "seq"]
    with engine.connect() as c:  # nothing is backfilled; the log starts under a new epoch
        assert c.execute(text("SELECT count(*) FROM client_change")).scalar() == 0
        first_epoch = c.execute(text("SELECT value FROM node_state WHERE key = 'client_change_epoch'")).scalar()
    assert first_epoch and len(first_epoch) == 16

    # The application writes through the migrated schema: autoincrement seq, causes, origins.
    maker = db.make_sessionmaker(engine)
    with maker() as session:
        ctx = Ctx(build_settings(database_url=url), load_identity(session))
        service.bump_version(session, "atlas:old", "provider")
        [res] = service.ingest(session, ctx, batch("manual", [{"external_id": "n", "title": "New"}], kind="manual"),
                               curator=True)
        session.commit()
        with client_changes.cause(session, "replicated", ("upstream-node", 41)):
            service.bump_version(session, res.canonical_id)
        session.commit()
        got = [(r.canonical_id, r.version, r.cause, r.origin_node, r.origin_seq)
               for r in session.query(ClientChange).order_by(ClientChange.seq)]
        assert got == [("atlas:old", 8, "provider", None, None), (res.canonical_id, 2, "feed", None, None),
                       (res.canonical_id, 3, "replicated", "upstream-node", 41)]
        first = client_changes.page(session, 0, 10)
        assert first["head"] == 3 and first["epoch"] == first_epoch
        top = client_changes.head(session)
    engine.dispose()

    command.downgrade(db.alembic_config(url), "0022")
    engine = create_engine(url)
    assert "client_change" not in inspect(engine).get_table_names()
    with engine.begin() as c:  # records and their versions survive the rollback
        assert c.execute(text("SELECT version FROM canonical WHERE id = 'atlas:old'")).scalar() == 8
        assert c.execute(text("SELECT count(*) FROM node_state WHERE key = 'client_change_epoch'")).scalar() == 0
        # The previous release changes a record during the rollback window; it logs nothing.
        c.execute(text("UPDATE canonical SET version = version + 1 WHERE id = :id"), {"id": res.canonical_id})
    engine.dispose()

    db.upgrade(url)
    engine = create_engine(url)
    maker = db.make_sessionmaker(engine)
    with maker() as session:
        # A recreated log is empty under a new epoch; a cursor from before the rollback is told to reset.
        page = client_changes.page(session, top, 10, first_epoch)
        assert page["reset"] is True and page["head"] == 0 and page["epoch"] != first_epoch
        service.bump_version(session, "atlas:old", "local")
        session.commit()
        assert [r.seq for r in session.query(ClientChange)] == [1]
        assert session.get(Canonical, "atlas:old").version == 9
        # New writes push the head past the stale cursor. Its old epoch still resets it, so the client
        # re-reads every cached record, including the one the previous release changed in the window.
        for _ in range(top + 5):
            service.bump_version(session, "atlas:old", "local")
            session.commit()
        assert client_changes.head(session) > top
        stale = client_changes.page(session, top, 10, first_epoch)
        assert stale["reset"] is True and stale["results"] == [] and stale["next_cursor"] == stale["head"]
        assert session.get(NodeState, client_changes.EPOCH).value == stale["epoch"] == page["epoch"]
        assert client_changes.page(session, stale["next_cursor"], 10, stale["epoch"])["reset"] is False
    engine.dispose()
