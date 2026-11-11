"""Migration 0033 (price observations, CONTRACTS-v1 C8.2): upgrade on a 0032 database holding records (one merged)
and relations, the application storing prices, the database refusing negative money, impossible discounts, bad
retention and a second copy of one observation; downgrade keeping the rows aside; the previous release merging
priced records while downgraded (including a chain); upgrade again bringing every row back on the surviving record.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from atlas import db, prices

from .conftest import build_settings
from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

NOW = datetime.now(UTC).replace(microsecond=0)
STAMP = "2026-11-02 10:00:00"
INDEXES = {"uq_price_dedup", "ix_price_latest", "ix_price_offer", "ix_price_purge"}
KEPT = "price_observation_0033_kept"
HEAD = "0061"  # A21 (the book model) is the head now; renumbered when the chain is restacked
RECORDS = {"atlas:game": "Priced Game", "atlas:dlc": "Priced Game DLC", "atlas:dup": "Priced Game (dup)",
           "atlas:dup2": "Priced Game (dup 2)", "atlas:old": "Old Merged"}


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


def seed(engine):
    """What a 0032 node holds: records with views and versions, one already merged, and a relation."""
    with engine.begin() as c:
        for cid, title in RECORDS.items():
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', :n, :v, '{}', 3, :t, :t)"),
                      {"i": cid, "n": title.lower(), "v": json.dumps({"title": title, "sources": ["steam"]}),
                       "t": STAMP})
        c.execute(text("UPDATE canonical SET merged_into = 'atlas:game' WHERE id = 'atlas:old'"))
        c.execute(text("INSERT INTO relation (id, from_id, to_id, type, source, redistributable, created_at, "
                       "created_by) VALUES ('01JR0000000000000000000000', 'atlas:dlc', 'atlas:game', 'dlc_of', "
                       "'curator', true, :t, 'cur')"), {"t": STAMP})


def snapshot(engine):
    with engine.connect() as c:
        records = sorted((r.id, r.version, r.merged_into, r.view if isinstance(r.view, str) else json.dumps(r.view))
                         for r in c.execute(text("SELECT id, version, merged_into, view FROM canonical")))
        relations = sorted(tuple(r) for r in c.execute(text("SELECT id, from_id, to_id, type FROM relation")))
        changes = c.execute(text("SELECT count(*) FROM change_log")).scalar()
    return records, relations, changes


def price_rows(engine, table="price_observation"):
    with engine.connect() as c:
        return sorted((r.canonical_id, r.product_id, r.region, r.currency, r.amount_minor, r.base_amount_minor,
                       r.discount_pct, r.retention_days)
                      for r in c.execute(text(f"SELECT * FROM {table}")))


def obs(cid, **kw):
    base = {"canonical_id": cid, "provider": "steam", "product_id": "620", "region": "us", "currency": "USD",
            "amount_minor": 999, "base_amount_minor": 1999, "availability": "available",
            "observed_at": (NOW - timedelta(hours=1)).isoformat(), "license": {"redistributable": True}}
    return prices.PriceIn.model_validate({**base, **kw})


def maker(url):
    settings = build_settings(database_url=url)
    return db.make_sessionmaker(db.make_engine(url), settings)


def test_upgrade_writes_downgrade_keeps_and_reupgrade_repoints(url):
    db.upgrade(url, "0032")
    engine = create_engine(url)
    seed(engine)
    before = snapshot(engine)

    db.upgrade(url)
    assert version(engine) == HEAD
    assert "price_observation" in inspect(engine).get_table_names()
    assert INDEXES <= {i["name"] for i in inspect(engine).get_indexes("price_observation")}
    assert snapshot(engine) == before  # no record, relation or feed entry changes
    assert price_rows(engine) == []

    with maker(url)() as s:  # the application writes through the merged record's redirect
        results = prices.ingest(s, [obs("atlas:old"),
                                    obs("atlas:dup", region="jp", currency="JPY", amount_minor=1980,
                                        base_amount_minor=1980),
                                    obs("atlas:dup2", region="kw", currency="KWD", amount_minor=1500,
                                        base_amount_minor=3000),
                                    obs("atlas:dlc", product_id="621", retention_days=365)])
        s.commit()
    assert [r["status"] for r in results] == ["stored"] * 4
    stored = price_rows(engine)
    assert stored == sorted([("atlas:game", "620", "us", "USD", 999, 1999, 50, 30),
                             ("atlas:dup", "620", "jp", "JPY", 1980, 1980, 0, 30),
                             ("atlas:dup2", "620", "kw", "KWD", 1500, 3000, 50, 30),
                             ("atlas:dlc", "621", "us", "USD", 999, 1999, 50, 365)])
    assert snapshot(engine) == before  # prices never touch records or the feed

    # The database itself refuses what the API refuses.
    insert = text("INSERT INTO price_observation (id, canonical_id, provider, product_id, region, currency, "
                  "amount_minor, base_amount_minor, discount_pct, availability, observed_at, source_kind, provenance, "
                  "redistributable, license_terms, retention_days, purge_after, dedup_key, created_at, created_by) "
                  "VALUES (:i, 'atlas:game', 'steam', 'x', 'us', 'USD', :a, 100, :d, 'available', :t, 'manual', '{}', "
                  "false, '', :r, :t, :k, :t, '')")
    with engine.connect() as c:
        taken = c.execute(text("SELECT dedup_key FROM price_observation LIMIT 1")).scalar()
    bad = [{"a": -1, "d": 0, "r": 30, "k": "k1"}, {"a": 1, "d": 101, "r": 30, "k": "k2"},
           {"a": 1, "d": 1, "r": 0, "k": "k3"}, {"a": 1, "d": 1, "r": 366, "k": "k4"},
           {"a": 1, "d": 1, "r": 30, "k": taken}]
    for i, values in enumerate(bad):
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(insert, {"i": f"01JX{i:022d}", "t": STAMP, **values})
    assert len(price_rows(engine)) == 4

    db.downgrade(url, "0032")
    assert version(engine) == "0032"
    tables = set(inspect(engine).get_table_names())
    assert "price_observation" not in tables and KEPT in tables  # kept aside, unread by the previous release
    assert price_rows(engine, KEPT) == stored
    assert snapshot(engine) == before

    # The previous release merges priced records while downgraded, one of them twice over (dup2 -> dup -> game).
    with engine.begin() as c:
        c.execute(text("UPDATE canonical SET merged_into = 'atlas:dup' WHERE id = 'atlas:dup2'"))
        c.execute(text("UPDATE canonical SET merged_into = 'atlas:game' WHERE id = 'atlas:dup'"))

    db.upgrade(url)
    assert version(engine) == HEAD
    tables = set(inspect(engine).get_table_names())
    assert "price_observation" in tables and KEPT not in tables
    assert INDEXES <= {i["name"] for i in inspect(engine).get_indexes("price_observation")}
    assert price_rows(engine) == sorted([("atlas:game", "620", "us", "USD", 999, 1999, 50, 30),
                                         ("atlas:game", "620", "jp", "JPY", 1980, 1980, 0, 30),
                                         ("atlas:game", "620", "kw", "KWD", 1500, 3000, 50, 30),
                                         ("atlas:dlc", "621", "us", "USD", 999, 1999, 50, 365)])
    db.upgrade(url)  # idempotent
    assert len(price_rows(engine)) == 4

    with maker(url)() as s:  # the application reads, dedups and purges the restored table
        latest = prices.latest(s, "atlas:game", restricted=True)["results"]
        assert sorted((r["region"], r["amount"]) for r in latest) == [("jp", "1980"), ("kw", "1.500"), ("us", "9.99")]
        again = prices.ingest(s, [obs("atlas:game")])
        assert again[0]["status"] == "duplicate"
        assert prices.purge(s, now=NOW + timedelta(days=31)) == 3
        s.commit()
    assert price_rows(engine) == [("atlas:dlc", "621", "us", "USD", 999, 1999, 50, 365)]
    engine.dispose()


def test_downgrade_of_an_empty_table_and_reupgrade(url):
    db.upgrade(url)
    engine = create_engine(url)
    db.downgrade(url, "0032")
    assert KEPT in set(inspect(engine).get_table_names())
    db.upgrade(url)
    assert version(engine) == HEAD and price_rows(engine) == []
    db.downgrade(url, "0031")  # through 0033 and 0032: both keep their tables aside
    assert {KEPT, "relation_0032_kept"} <= set(inspect(engine).get_table_names())
    db.upgrade(url)
    assert version(engine) == HEAD
    assert {"price_observation", "relation"} <= set(inspect(engine).get_table_names())
    engine.dispose()
