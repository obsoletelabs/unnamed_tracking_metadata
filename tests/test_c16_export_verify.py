"""C16 R2 (review of Z1, high): `atlas export-verify` on the shapes real v0 nodes hold, from both v0 heads
(0020: the 1.0 build 62c73f3; 0030: the A12 1.1 build c91b631) to this release, on SQLite and PostgreSQL.

Migrations rewrite some v0 columns on purpose (0031 moves a release date into `first_release`, 0034 supersedes
duplicate open review items, 0036 moves entries that only wait for their record to `wait`): those are reported as
derived, never as lost, while every other column of those rows, and every other row, must survive unchanged.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from atlas import db, export_verify

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

T = "2026-11-01 10:00:00"
UP = "356cae19243bb510"
DATED, UNDATED, GONE = ("atlas:01dddddddddddddddddddddddd", "atlas:01uuuuuuuuuuuuuuuuuuuuuuuu",
                        "atlas:01gggggggggggggggggggggggg")


@pytest.fixture(params=["sqlite", "postgresql"])
def url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path}/v0.db"
    pg = request.getfixturevalue("pg_url")
    engine = create_engine(pg)
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    engine.dispose()
    return pg


def seed(engine, head: str) -> None:
    """What a v0 node holds: a dated and an undated record, two open review items for one pair, and a quarantined
    sidecar of a record that never arrived (with its signed body)."""
    with engine.begin() as c:
        for cid, view, year in ((DATED, {"title": "Dated", "release_date": "2011-04-19", "sources": ["steam"]}, 2011),
                                (UNDATED, {"title": "Undated", "sources": ["gog"]}, None)):
            explain = {"release_date": {"strategy": "vote", "observation_id": "O1", "source": "steam"}} \
                if "release_date" in view else {}
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, year, view, explain, version, "
                           "created_at, updated_at) VALUES (:i, 'game', :n, :y, :v, :e, 3, :t, :t)"),
                      {"i": cid, "n": view["title"].lower(), "y": year, "v": json.dumps(view),
                       "e": json.dumps(explain), "t": T})
        for i in range(2):
            c.execute(text("INSERT INTO review_item (id, kind, canonical_id, candidates, confidence, status, "
                           "created_at) VALUES (:i, 'possible_duplicate', :c, :cand, 0.7, 'open', :t)"),
                      {"i": f"01JR{i:022d}", "c": DATED, "cand": json.dumps([[UNDATED, 0.7]]),
                       "t": f"2026-11-0{i + 1} 10:00:00"})
        c.execute(text("INSERT INTO replicated_feed (node_id, public_key) VALUES (:n, 'pk')"), {"n": UP})
        c.execute(text("INSERT INTO replicated_change (node_id, seq, kind, body, body_hash, signature, key_id, "
                       "created_at) VALUES (:n, 6, 'artwork', :b, 'h', 'sig', 'k', :t)"),
                  {"n": UP, "b": json.dumps({"canonical_id": GONE, "source": "steam", "items": []}), "t": T})
        extra = (", disposition, reason", ", 'quarantined', 'apply_error'") if head >= "0021" else ("", "")
        c.execute(text(f"INSERT INTO sync_failure (node_id, seq, kind, error, attempts, first_failed_at, "
                       f"last_attempt_at{extra[0]}) VALUES (:n, 6, 'artwork', :e, 2, :t, :t{extra[1]})"),
                  {"n": UP, "e": "AtlasError: unknown or merged canonical", "t": T})
        for i in range(5):
            c.execute(text("INSERT INTO node_state (key, value) VALUES (:k, :v)"), {"k": f"v0:{i}", "v": str(i)})


@pytest.mark.parametrize("head", ["0020", "0030"])
def test_a_real_v0_shape_upgrades_in_place_and_verifies(url, tmp_path, head):
    db.upgrade(url, head)
    engine = create_engine(url)
    seed(engine, head)
    manifest = export_verify.export(engine)
    assert manifest["revision"] == head
    assert manifest["tables"]["canonical"]["derived"] == ["explain", "norm_title", "view", "year"]
    export_verify.write(tmp_path / "m.json.gz", manifest)
    engine.dispose()

    db.upgrade(url)  # every migration from the v0 head to this release, in place
    engine = create_engine(url)
    with engine.connect() as c:  # the migrations did rewrite these on purpose
        view = c.execute(text("SELECT view FROM canonical WHERE id = :i"), {"i": DATED}).scalar()
        view = json.loads(view) if isinstance(view, str) else view
        assert view["first_release"] == {"date": "2011-04-19", "precision": "day"}
        assert sorted(c.execute(text("SELECT status FROM review_item")).scalars()) == ["open", "superseded"]
        assert c.execute(text("SELECT disposition FROM sync_failure")).scalar() == "wait"
    report = export_verify.verify(engine, export_verify.read(tmp_path / "m.json.gz"))
    assert report["ok"], report["problems"]
    assert report["revision_after"] == "0063"
    assert report["tables"]["canonical"]["derived_rewritten"] == 1  # the dated record only
    assert report["tables"]["review_item"]["derived_rewritten"] == 1  # the younger duplicate
    assert "0031" in report["tables"]["canonical"]["derived"]["view"]

    # Every other column of a row with a rewritten derived column is still checked.
    with engine.begin() as c:
        c.execute(text("UPDATE canonical SET version = 99 WHERE id = :i"), {"i": DATED})
    report = export_verify.verify(engine, manifest)
    assert not report["ok"] and report["tables"]["canonical"]["missing_or_changed"] == 1
    with engine.begin() as c:
        c.execute(text("UPDATE canonical SET version = 3 WHERE id = :i"), {"i": DATED})
        c.execute(text("DELETE FROM review_item WHERE status = 'superseded'"))
    report = export_verify.verify(engine, manifest)
    assert not report["ok"] and report["tables"]["review_item"]["missing_or_changed"] == 1
    engine.dispose()


def test_a_lost_duplicate_row_is_reported(tmp_path):
    """Rows are compared as a multiset: two identical rows (a table without a primary key) must stay two."""
    engine = create_engine(f"sqlite:///{tmp_path}/d.db")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE loose (a TEXT, b INTEGER)"))
        c.execute(text("INSERT INTO loose VALUES ('x', 1), ('x', 1), ('y', 2)"))
    manifest = export_verify.export(engine)
    assert manifest["tables"]["loose"]["rows"] == 3
    with engine.begin() as c:
        c.execute(text("DELETE FROM loose WHERE rowid = (SELECT min(rowid) FROM loose WHERE a = 'x')"))
    report = export_verify.verify(engine, manifest)
    assert not report["ok"] and report["tables"]["loose"]["missing_or_changed"] == 1
    engine.dispose()


def test_an_older_manifest_is_refused_with_a_clear_message(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/o.db")
    with pytest.raises(ValueError, match="export again"):
        export_verify.verify(engine, {"format": export_verify.FORMAT, "v": 1, "tables": {}})
    engine.dispose()


def test_every_derived_column_exists_in_this_release():
    """A DERIVED entry naming a column this release no longer has would silently hide nothing; keep the list true."""
    from atlas.models import Base

    for table, columns in export_verify.DERIVED.items():
        assert table in Base.metadata.tables, table
        assert set(columns) <= {c.name for c in Base.metadata.tables[table].columns}, table
