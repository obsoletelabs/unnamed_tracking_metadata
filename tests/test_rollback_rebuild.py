"""Rollback and re-upgrade bring derived data back without an operator step (Phase 4 defect D3).

Data written by this release through the application (partial and regional release dates from `ext.release` and a
`releases` lock, provider values that conflict, a curator lock), then a downgrade through 0034 or through 0031,
then `db.upgrade` (what `atlas migrate` runs): every record's view, explain (with `_meta`), year and release
columns, and the `field_conflict` index equal what they were before the downgrade. A re-upgrade without settings
(a bare `db.upgrade`) leaves the work to the next start, which finishes it; an interrupted rebuild resumes.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, text

from atlas import dates_rebuild, db, main, service
from atlas.models import Canonical, FieldConflict, NodeState
from atlas.service import Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings
from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

RELEASE = {"first": {"date": "2015-03", "precision": "month"},
           "entries": [{"date": "2015-03", "precision": "month", "platform": "pc", "region": "ww", "category": "full"},
                       {"date": "2015-09-21", "precision": "day", "platform": "ps4", "region": "na",
                        "category": "port"}]}
LOCKED = [{"date": "2011", "precision": "year", "platform": "pc", "region": "ww", "category": "full"}]


@pytest.fixture(params=["sqlite", "postgresql"])
def url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path}/r.db"
    pg = request.getfixturevalue("pg_url")
    engine = create_engine(pg)
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    engine.dispose()
    return pg


def node(url):
    settings = build_settings(database_url=url, emit_protocol="1.4")
    db.upgrade(url, settings=settings)
    engine = db.make_engine(url)
    maker = db.make_sessionmaker(engine, settings)
    with maker() as s:
        ctx = Ctx(settings, load_identity(s, settings))
        s.commit()
    return settings, engine, maker, ctx


def seed(maker, ctx) -> dict[str, str]:
    """Records only this release derives fully, written through the application."""
    with maker() as s:
        dated = service.ingest(s, ctx, batch("igdb", [{"external_id": "d1", "title": "Dated By Ext",
                                                       "ext": {"release": RELEASE}}]), system=True)[0].canonical_id
        conflict = service.ingest(s, ctx, batch("igdb", [{"external_id": "c1", "title": "Conflicted Game",
                                                          "release_date": "2013-06-07",
                                                          "facts": {"developer": "Studio One"}}]),
                                  system=True)[0].canonical_id
        second = service.ingest(s, ctx, batch("steam", [{"external_id": "c1", "title": "Conflicted Game",
                                                         "release_date": "2013-06-07",
                                                         "facts": {"developer": "Studio Two Corporation"}}]),
                                system=True, force_canonical=conflict)[0].canonical_id
        assert second == conflict
        locked = service.ingest(s, ctx, batch("igdb", [{"external_id": "l1", "title": "Locked Releases",
                                                        "release_date": "2012-05-01"}]), system=True)[0].canonical_id
        service.set_lock(s, ctx, locked, "releases", LOCKED, actor="curator")
        plain = service.ingest(s, ctx, batch("igdb", [{"external_id": "p1", "title": "Plain Day",
                                                       "release_date": "2010-02-03"}]), system=True)[0].canonical_id
        s.commit()
    return {"dated": dated, "conflict": conflict, "locked": locked, "plain": plain}


def state(maker) -> tuple[dict, set]:
    with maker() as s:
        records = {c.id: {"view": c.view, "explain": c.explain, "year": c.year, "release_start": c.release_start,
                          "release_end": c.release_end, "release_precision": c.release_precision,
                          "version": c.version}
                   for c in s.scalars(select(Canonical).where(Canonical.merged_into.is_(None)))}
        conflicts = {(f.canonical_id, f.field) for f in s.scalars(select(FieldConflict))}
    return records, conflicts


def without_version(records: dict) -> dict:
    return {cid: {k: v for k, v in r.items() if k != "version"} for cid, r in records.items()}


def pending(engine) -> str | None:
    with engine.connect() as c:
        return c.execute(text("SELECT value FROM node_state WHERE key = 'view_rebuild_pending'")).scalar()


@pytest.mark.parametrize("target", ["0033", "0030"])  # undoes 0034 (and 0035) / everything back to 0031
def test_upgrading_again_brings_the_derived_data_back(url, target):
    settings, engine, maker, ctx = node(url)
    ids = seed(maker, ctx)
    before, conflicts = state(maker)
    assert before[ids["dated"]]["view"]["first_release"] == RELEASE["first"]
    assert len(before[ids["dated"]]["view"]["releases"]) == 2
    assert before[ids["locked"]]["view"]["releases"] == LOCKED
    assert "_meta" in before[ids["conflict"]]["explain"] and (ids["conflict"], "developer") in conflicts

    db.downgrade(url, target, settings)
    assert pending(engine) == "pending"
    with engine.connect() as c:  # the downgrade really removed derived data (what D3 was about)
        explain = c.execute(text("SELECT explain FROM canonical WHERE id = :i"), {"i": ids["conflict"]}).scalar()
    assert "_meta" not in (explain if isinstance(explain, dict) else __import__("json").loads(explain))

    db.upgrade(url, settings=settings)  # = `atlas migrate`
    after, again = state(maker)
    assert without_version(after) == without_version(before)
    assert again == conflicts
    assert after[ids["dated"]]["view"]["releases"] == before[ids["dated"]]["view"]["releases"]
    assert after[ids["dated"]]["release_precision"] == "month"
    # a record whose view came back unchanged keeps its version (only explain is rewritten, as explain-backfill
    # does); one whose view the downgrade had changed is bumped, so clients see it come back
    assert after[ids["plain"]]["version"] == before[ids["plain"]]["version"]
    if target == "0030":
        assert after[ids["locked"]]["version"] > before[ids["locked"]]["version"]
    assert pending(engine) is None
    with maker() as s:  # the repair's cursor is gone too, and it never touched `atlas dates rebuild`'s
        assert s.get(NodeState, dates_rebuild.PENDING_CURSOR) is None
        assert s.get(NodeState, dates_rebuild.CURSOR) is None
    db.upgrade(url, settings=settings)  # nothing left to do: idempotent
    assert state(maker) == (after, again)
    engine.dispose()


def test_a_bare_upgrade_leaves_the_rebuild_to_the_next_start(url, tmp_path):
    settings, engine, maker, ctx = node(url)
    ids = seed(maker, ctx)
    before, conflicts = state(maker)
    db.downgrade(url, "0030", settings)
    db.upgrade(url)  # no settings: the schema is current, the data step waits
    assert pending(engine) == "pending"
    with maker() as s:
        assert "releases" not in s.get(Canonical, ids["locked"]).view
    app = main.create_app(build_settings(database_url=url, emit_protocol="1.4", media_dir=str(tmp_path / "media"),
                                         auto_migrate=False))
    assert pending(engine) is None
    after, again = state(app.state.atlas.maker)
    assert without_version(after) == without_version(before) and again == conflicts
    engine.dispose()


def test_an_interrupted_rebuild_resumes_where_it_stopped(url, monkeypatch):
    settings, engine, maker, ctx = node(url)
    seed(maker, ctx)
    before, conflicts = state(maker)
    db.downgrade(url, "0030", settings)
    db.upgrade(url)
    real = dates_rebuild.run
    seen: list[bool] = []

    def crash_after_one_batch(m, c, **kw):
        seen.append(kw["restart"])
        real(m, c, **{**kw, "batch": 2, "limit": 2})
        raise RuntimeError("process stopped")

    monkeypatch.setattr(dates_rebuild, "run", crash_after_one_batch)
    with pytest.raises(RuntimeError):
        dates_rebuild.finish_pending(maker, settings)
    assert pending(engine) == "running"
    with maker() as s:
        assert s.get(NodeState, dates_rebuild.PENDING_CURSOR) is not None
    monkeypatch.setattr(dates_rebuild, "run", lambda m, c, **kw: seen.append(kw["restart"]) or real(m, c, **kw))
    assert dates_rebuild.finish_pending(maker, settings)["finished"] is True
    assert seen == [True, False]  # the first run starts over, the second continues from the cursor
    after, again = state(maker)
    assert without_version(after) == without_version(before) and again == conflicts
    assert pending(engine) is None
    engine.dispose()


def test_the_repair_leaves_an_operators_rebuild_cursor_alone(url):
    """`atlas dates rebuild` stopped half way (its cursor in `node_state`); the rollback repair runs from the start
    under its own cursor and finishes, and the operator's rebuild still resumes where it stopped."""
    settings, engine, maker, ctx = node(url)
    seed(maker, ctx)
    before, conflicts = state(maker)
    assert dates_rebuild.run(maker, ctx, batch=1, limit=1)["finished"] is False
    with maker() as s:
        operator = s.get(NodeState, dates_rebuild.CURSOR).value
    db.downgrade(url, "0030", settings)
    db.upgrade(url, settings=settings)
    assert pending(engine) is None
    after, again = state(maker)
    assert without_version(after) == without_version(before) and again == conflicts
    with maker() as s:
        assert s.get(NodeState, dates_rebuild.CURSOR).value == operator
        assert s.get(NodeState, dates_rebuild.PENDING_CURSOR) is None
    engine.dispose()


def test_a_second_process_skips_the_repair_while_one_holds_it(url):
    """PostgreSQL, several workers or replicas starting together: the one holding the claim does the work, the
    others neither start a second run nor touch the marker or the cursor. A claim dies with its connection."""
    if not url.startswith("postgresql"):
        pytest.skip("the claim is a PostgreSQL advisory lock; SQLite has a single writer")
    settings, engine, maker, ctx = node(url)
    seed(maker, ctx)
    db.downgrade(url, "0030", settings)
    db.upgrade(url)
    with engine.connect() as other:  # another worker holds the claim
        assert other.execute(text("SELECT pg_try_advisory_lock(727276)")).scalar() is True
        other.commit()
        assert dates_rebuild.finish_pending(maker, settings) == {"rebuilt": 0, "changed": 0, "finished": False,
                                                                 "busy": True}
        assert pending(engine) == "pending"
        with maker() as s:
            assert s.get(NodeState, dates_rebuild.PENDING_CURSOR) is None
    # that worker's connection closed (it finished or died): the claim is free again
    assert dates_rebuild.finish_pending(maker, settings)["finished"] is True
    assert pending(engine) is None
    engine.dispose()


def test_a_fresh_or_never_downgraded_database_does_no_rebuild(url):
    settings, engine, maker, ctx = node(url)
    seed(maker, ctx)
    assert pending(engine) is None
    assert dates_rebuild.finish_pending(maker, settings) is None
    engine.dispose()
