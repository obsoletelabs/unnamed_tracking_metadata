"""Migration 0032 (relationships between works, CONTRACTS-v1 C6.3): upgrade on a 0031 database with records
(one already merged), the application writing relations, the database enforcing one active row per triple and
no self relation, downgrade keeping the rows aside, a merge made by the previous release while downgraded, and
upgrade again replaying that merge with the same `move` a 1.4 follower runs (the follower side is
`tests/test_relations.py::test_restore_after_a_downgrade_converges_with_a_follower_that_applied_the_merge`).

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from atlas import custom, db, relations
from atlas.service import Ctx
from atlas.signing import load_identity

from .conftest import build_settings
from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

NOW = "2026-11-02 10:00:00"
INDEXES = {"uq_relation_active", "ix_relation_from", "ix_relation_to", "ix_relation_origin"}
KEPT = "relation_0032_kept"
HEAD = "0060"  # A20 (media_type.achievements) is the head now; renumbered when the chain is restacked


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


# What a 0031 node holds: live records, one merged into another (its redirect), all with views and versions.
RECORDS = {"atlas:base": "Base Game", "atlas:dlc": "Base Game Expansion", "atlas:port": "Base Game Handheld",
           "atlas:mod": "Base Game Overhaul", "atlas:old": "Base Game (dup)",
           "atlas:hand2": "Base Game Handheld II"}


def seed(engine):
    with engine.begin() as c:
        for cid, title in RECORDS.items():
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', :n, :v, '{}', 4, :t, :t)"),
                      {"i": cid, "n": title.lower(), "v": json.dumps({"title": title, "sources": ["steam"]}),
                       "t": NOW})
        c.execute(text("UPDATE canonical SET merged_into = 'atlas:base' WHERE id = 'atlas:old'"))


def snapshot(engine):
    with engine.connect() as c:
        return {r.id: (r.view if isinstance(r.view, dict) else json.loads(r.view), r.version, r.merged_into)
                for r in c.execute(text("SELECT id, view, version, merged_into FROM canonical"))}


def relation_rows(engine):
    """Every row, active or ended, as (from, to, type, ended, end_reason)."""
    with engine.connect() as c:
        return sorted((r.from_id, r.to_id, r.type, r.ended_at is not None, r.end_reason or "")
                      for r in c.execute(text("SELECT from_id, to_id, type, ended_at, end_reason FROM relation")))


def app(url):
    settings = build_settings(database_url=url)
    maker = db.make_sessionmaker(db.make_engine(url), settings)
    with maker() as s:
        ctx = Ctx(settings=settings, identity=load_identity(s, settings))
        custom.seed(s)
        s.commit()
    return maker, ctx


def test_upgrade_writes_downgrade_keeps_and_reupgrade_replays_the_merge(url):
    db.upgrade(url, "0031")
    engine = create_engine(url)
    seed(engine)
    before = snapshot(engine)

    db.upgrade(url)
    assert version(engine) == HEAD
    tables = set(inspect(engine).get_table_names())
    assert "relation" in tables and KEPT not in tables
    assert INDEXES <= {i["name"] for i in inspect(engine).get_indexes("relation")}
    assert snapshot(engine) == before  # no existing record changes
    assert relation_rows(engine) == []

    maker, ctx = app(url)
    with maker() as s:  # the application writes through the redirect of the merged record
        relations.add(s, ctx, "atlas:dlc", "atlas:old", "expansion_of", actor="cur")
        relations.add(s, ctx, "atlas:port", "atlas:base", "port_of", actor="cur")
        relations.add(s, ctx, "atlas:hand2", "atlas:port", "port_of", actor="cur")
        relations.add(s, ctx, "atlas:mod", "atlas:base", "mod_of", actor="cur")
        relations.end(s, ctx, "atlas:mod", "atlas:base", "mod_of", actor="cur")
        relations.add(s, ctx, "atlas:mod", "atlas:base", "mod_of", actor="cur")
        s.commit()
    assert relation_rows(engine) == [("atlas:dlc", "atlas:base", "expansion_of", False, ""),
                                     ("atlas:hand2", "atlas:port", "port_of", False, ""),
                                     ("atlas:mod", "atlas:base", "mod_of", False, ""),
                                     ("atlas:mod", "atlas:base", "mod_of", True, "removed"),
                                     ("atlas:port", "atlas:base", "port_of", False, "")]

    # The database itself refuses a second active row for the same triple and a self relation.
    insert = text("INSERT INTO relation (id, from_id, to_id, type, source, redistributable, created_at, created_by) "
                  "VALUES (:i, :f, :t, :k, 'curator', false, :n, '')")
    for i, (f, t, k) in enumerate((("atlas:port", "atlas:base", "port_of"), ("atlas:base", "atlas:base", "port_of"))):
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(insert, {"i": f"01JX{i:022d}", "f": f, "t": t, "k": k, "n": NOW})
    with engine.begin() as c:  # an ended row does not count against the partial unique index
        c.execute(text("INSERT INTO relation (id, from_id, to_id, type, source, redistributable, created_at, "
                       "ended_at, end_reason, created_by) VALUES ('01JY0000000000000000000000', 'atlas:port', "
                       "'atlas:base', 'port_of', 'curator', false, :n, :n, 'removed', '')"), {"n": NOW})
    with engine.connect() as c:
        kept_count = c.execute(text("SELECT count(*) FROM relation")).scalar()
    assert kept_count == 6

    db.downgrade(url, "0031")
    assert version(engine) == "0031"
    tables = set(inspect(engine).get_table_names())
    assert "relation" not in tables and KEPT in tables  # kept aside, unread by the previous release
    with engine.connect() as c:
        assert c.execute(text(f"SELECT count(*) FROM {KEPT}")).scalar() == kept_count

    # The previous release merges the port into the base game while downgraded; it does not move relations.
    with engine.begin() as c:
        c.execute(text("UPDATE canonical SET merged_into = 'atlas:base' WHERE id = 'atlas:port'"))

    versions = {cid: v for cid, (_view, v, _m) in snapshot(engine).items()}

    # Only the schema step (alembic alone): the table is back and the node is marked for the replay.
    from alembic import command
    command.upgrade(db.alembic_config(url), "head")
    assert version(engine) == HEAD
    tables = set(inspect(engine).get_table_names())
    assert "relation" in tables and KEPT not in tables
    assert INDEXES <= {i["name"] for i in inspect(engine).get_indexes("relation")}
    with engine.connect() as c:
        assert c.execute(text("SELECT value FROM node_state WHERE key = :k"),
                         {"k": relations.RESTORE_KEY}).scalar() == "pending"

    # `atlas migrate` (db.upgrade) then runs the application step: the merge is replayed with `move`.
    db.upgrade(url)
    assert relation_rows(engine) == [("atlas:dlc", "atlas:base", "expansion_of", False, ""),
                                     ("atlas:hand2", "atlas:base", "port_of", False, ""),  # moved to the survivor
                                     ("atlas:mod", "atlas:base", "mod_of", False, ""),
                                     ("atlas:mod", "atlas:base", "mod_of", True, "removed"),
                                     ("atlas:port", "atlas:base", "port_of", True, "merge_self"),  # base -> base
                                     ("atlas:port", "atlas:base", "port_of", True, "removed")]
    after = {cid: v for cid, (_view, v, _m) in snapshot(engine).items()}
    assert {cid for cid in after if after[cid] != versions[cid]} == {"atlas:base", "atlas:port", "atlas:hand2"}
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM node_state WHERE key = :k"),
                         {"k": relations.RESTORE_KEY}).scalar() == 0
    db.upgrade(url)  # idempotent: nothing pending, nothing changes
    assert {cid: v for cid, (_view, v, _m) in snapshot(engine).items()} == after
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM relation")).scalar() == kept_count
        merged = {r[0] for r in c.execute(text("SELECT id FROM canonical WHERE merged_into IS NOT NULL"))}
        active = c.execute(text("SELECT from_id, to_id FROM relation WHERE ended_at IS NULL")).all()
    assert not any(f in merged or t in merged for f, t in active)  # nothing dangles

    maker, ctx = app(url)
    with maker() as s:  # the application reads and writes the restored table
        block = relations.of(s, "atlas:base")
        assert sorted((c["id"], c["type"]) for c in block["children"]) == [("atlas:dlc", "expansion_of"),
                                                                          ("atlas:hand2", "port_of"),
                                                                          ("atlas:mod", "mod_of")]
        with pytest.raises(relations.AtlasError, match="cycle"):
            relations.add(s, ctx, "atlas:base", "atlas:mod", "remake_of")
        s.rollback()
    engine.dispose()


def test_downgrade_of_an_empty_table_and_reupgrade(url):
    db.upgrade(url)
    engine = create_engine(url)
    db.downgrade(url, "0031")
    assert KEPT in set(inspect(engine).get_table_names())
    db.upgrade(url)
    assert version(engine) == HEAD and relation_rows(engine) == []
    engine.dispose()
