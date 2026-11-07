"""Migration 0034 (C11 matching and provenance, A17): upgrade on a 0033 database holding records (a merge chain),
observations, keys, locks and duplicate open review items; the application then writing decisions, rejections, a
merge record, an explicit clear and a variant key; downgrade keeping every new row aside and leaving the previous
release a schema it reads correctly; upgrade again restoring it all, with the merge still undoable.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, select, text

from atlas import db, merges, service
from atlas.models import Canonical, Lock, MergeRecord
from atlas.service import Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings
from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

HEAD = "0036"  # A19 is the head now
STAMP = "2026-11-02 10:00:00"
NEW_TABLES = {"match_decision", "match_rejection", "merge_record", "merge_record_item", "record_live", "field_conflict",
              "review_candidate"}
KEPT = {"match_decision_0034_kept", "match_rejection_0034_kept", "merge_record_0034_kept",
        "merge_record_item_0034_kept"}
NEW_COLUMNS = {"observation": {"payload_hash", "last_seen_at"}, "crosswalk": {"variant"}, "field_lock": {"cleared"},
               "review_item": {"reason", "evidence", "decision_id", "pair_key", "priority", "resolved_at"}}


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


def columns(engine, table) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def seed(engine):
    """What a 0033 node holds: c <- b <- a merged twice over, a live pair with two duplicate open review items."""
    explain = json.dumps({"title": {"strategy": "priority", "source": "steam"}})
    with engine.begin() as c:
        for cid, title in (("atlas:a", "Old A"), ("atlas:b", "Old B"), ("atlas:c", "Live C"), ("atlas:d", "Live D")):
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', :n, :v, :e, 2, :t, :t)"),
                      {"i": cid, "n": title.lower(), "v": json.dumps({"title": title, "sources": ["steam"]}),
                       "e": explain, "t": STAMP})
        c.execute(text("UPDATE canonical SET merged_into = 'atlas:b' WHERE id = 'atlas:a'"))
        c.execute(text("UPDATE canonical SET merged_into = 'atlas:c' WHERE id = 'atlas:b'"))
        for i, (cid, ext) in enumerate((("atlas:c", "1"), ("atlas:c", "2"), ("atlas:d", "3"))):
            c.execute(text("INSERT INTO observation (id, canonical_id, source_kind, source, source_scope, external_id, "
                           "payload, observed_at, is_current, redistributable) VALUES (:i, :c, 'metadata', 'steam', "
                           "'s', :x, :p, :t, true, false)"),
                      {"i": f"01JX{i:022d}", "c": cid, "x": ext, "p": json.dumps({"title": f"T{ext}"}), "t": STAMP})
            c.execute(text("INSERT INTO crosswalk (namespace, external_id, canonical_id) VALUES ('steam', :x, :c)"),
                      {"x": ext, "c": cid})
        c.execute(text("INSERT INTO field_lock (canonical_id, field, value, set_by, local_only, created_at) "
                       "VALUES ('atlas:c', 'series', :v, 'cur', true, :t)"), {"v": json.dumps("Series"), "t": STAMP})
        for i in range(2):
            c.execute(text("INSERT INTO review_item (id, kind, canonical_id, candidates, confidence, status, created_at) "
                           "VALUES (:i, 'possible_duplicate', 'atlas:d', :cand, 0.7, 'open', :t)"),
                      {"i": f"01JR{i:022d}", "cand": json.dumps([["atlas:c", 0.7]]), "t": f"2026-11-0{i + 1} 10:00:00"})


def maker(url, role="local"):
    settings = build_settings(database_url=url, role=role)
    return db.make_sessionmaker(db.make_engine(url), settings), settings


def test_upgrade_backfills_downgrade_keeps_and_reupgrade_restores(url):
    db.upgrade(url, "0033")
    engine = create_engine(url)
    seed(engine)

    db.upgrade(url)
    assert version(engine) == HEAD
    tables = set(inspect(engine).get_table_names())
    assert NEW_TABLES <= tables and not (KEPT & tables)
    for table, cols in NEW_COLUMNS.items():
        assert cols <= columns(engine, table)
    with engine.connect() as c:
        live = dict(c.execute(text("SELECT id, live_id FROM record_live")).all())
        reviews = {r.id: (r.status, r.pair_key, r.priority) for r in c.execute(text(
            "SELECT id, status, pair_key, priority FROM review_item"))}
        variants = {r[0] for r in c.execute(text("SELECT variant FROM crosswalk"))}
        cleared = {r[0] for r in c.execute(text("SELECT cleared FROM field_lock"))}
    assert live == {"atlas:a": "atlas:c", "atlas:b": "atlas:c"}  # merged ids only, chains followed to the end
    first, second = reviews[f"01JR{0:022d}"], reviews[f"01JR{1:022d}"]
    assert first[0] == "open" and second[0] == "superseded"  # one open item per pair; the newer duplicate goes
    assert first[1] and second[1] is None and first[2] == 17  # possible_duplicate: 10 + round(0.7 x 10)
    assert variants == {""} and cleared in ({False}, {0})

    mk, settings = maker(url)
    with mk() as s:  # the application uses every new construct
        ctx = Ctx(settings=settings, identity=load_identity(s))
        service.ingest(s, ctx, batch("psn", [{"external_id": "p5", "title": "Live D", "platform": "PS5"}]),
                       curator=True)
        service.set_lock(s, ctx, "atlas:d", "description", None, actor="cur", local_only=True, clear=True)
        service.merge_canonicals(s, ctx, "atlas:d", "atlas:c", actor="cur")
        s.commit()
        mid = s.scalars(select(MergeRecord.id)).one()
        explain_keys = set(s.get(Canonical, "atlas:c").explain)
    assert "_meta" in explain_keys
    with engine.connect() as c:
        counts = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in sorted(NEW_TABLES)}
    assert counts["match_decision"] >= 1 and counts["merge_record"] == 1 and counts["merge_record_item"] >= 1

    db.downgrade(url, "0033")
    assert version(engine) == "0033"
    tables = set(inspect(engine).get_table_names())
    assert KEPT <= tables and not (NEW_TABLES & tables)
    assert {"field_lock_0034_cleared_kept", "crosswalk_variant_0034_kept"} <= tables
    for table, cols in NEW_COLUMNS.items():
        assert not (cols & columns(engine, table))
    with engine.connect() as c:
        # the previous release would read a cleared lock's empty value as a pin: it is kept aside instead
        locks = {(r[0], r[1]) for r in c.execute(text("SELECT canonical_id, field FROM field_lock"))}
        assert ("atlas:c", "description") not in locks and ("atlas:c", "series") in locks
        explains = [r[0] for r in c.execute(text("SELECT explain FROM canonical"))]
        kept_items = c.execute(text("SELECT count(*) FROM merge_record_item_0034_kept")).scalar()
    for e in explains:
        assert "_meta" not in (json.loads(e) if isinstance(e, str) else e)
    assert kept_items == counts["merge_record_item"]

    db.upgrade(url)
    assert version(engine) == HEAD
    tables = set(inspect(engine).get_table_names())
    assert NEW_TABLES <= tables and not (KEPT & tables)
    assert not ({"field_lock_0034_cleared_kept", "crosswalk_variant_0034_kept"} & tables)
    with engine.connect() as c:
        again = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in sorted(NEW_TABLES)
                 if t not in ("field_conflict", "review_candidate")}
        variants = {r[0] for r in c.execute(text("SELECT variant FROM crosswalk WHERE namespace = 'psn'"))}
    assert again == {t: n for t, n in counts.items() if t not in ("field_conflict", "review_candidate")}
    assert variants == {"platform:ps5"}
    mk, settings = maker(url)
    with mk() as s:
        ctx = Ctx(settings=settings, identity=load_identity(s))
        lk = s.get(Lock, ("atlas:c", "description"))
        assert lk is not None and lk.cleared is True  # the clear came back (it moved with the merge)
        out = merges.unmerge(s, ctx, mid, actor="cur", remember_distinct=False)
        s.commit()
        assert out["status"] == "unmerged"
        assert s.get(Canonical, "atlas:d").merged_into is None
        assert s.get(Lock, ("atlas:d", "description")).cleared is True
    db.upgrade(url)  # idempotent
    assert version(engine) == HEAD
    engine.dispose()


def test_downgrade_of_an_empty_database_and_reupgrade(url):
    db.upgrade(url)
    engine = create_engine(url)
    db.downgrade(url, "0033")
    assert KEPT <= set(inspect(engine).get_table_names())
    db.upgrade(url)
    assert version(engine) == HEAD and NEW_TABLES <= set(inspect(engine).get_table_names())
    db.downgrade(url, "0032")  # through 0034 and 0033: both keep their tables aside
    assert KEPT | {"price_observation_0033_kept"} <= set(inspect(engine).get_table_names())
    db.upgrade(url)
    assert version(engine) == HEAD
    engine.dispose()


def test_review_columns_and_last_seen_survive_a_downgrade_and_reupgrade(url):
    """Amendment A17 #2: the review columns (dismissal windows need `resolved_at`), evidence and decision links,
    open items of kinds the previous release does not know, and `last_seen_at` come back after a downgrade and a
    re-upgrade; the candidate index is rebuilt. The previous release never sees the new kinds in its queue."""
    from atlas import matching
    from atlas.models import Observation, ReviewCandidate, ReviewItem, now

    db.upgrade(url, "0033")
    engine = create_engine(url)
    seed(engine)
    db.upgrade(url)
    mk, settings = maker(url)
    seen = now().replace(microsecond=0)
    with mk() as s:
        dismissed = s.scalars(select(ReviewItem).where(ReviewItem.status == "open")).one()
        dismissed.evidence = [{"signal": "title_equal", "value": "live d", "effect": 0.7}]
        dismissed.reason, dismissed.decision_id = "ambiguous", "01JD" + "0" * 22
        matching.close_review(s, dismissed, "dismissed", "cur")
        hidden = ReviewItem(kind="title_contradicted", canonical_id="atlas:d", candidates=[["atlas:c", 0.9]],
                            confidence=0.9, reason="contradicted", priority=20,
                            pair_key=matching.pair_key("title_contradicted", ["atlas:d", "atlas:c"]))
        still = ReviewItem(kind="id_conflict", canonical_id="atlas:c", candidates=[["atlas:d", 1.0]], confidence=1.0,
                           reason="conflict", priority=30, pair_key=matching.pair_key("id_conflict", ["atlas:c", "atlas:d"]))
        s.add_all([hidden, still])
        for o in s.scalars(select(Observation)):
            o.last_seen_at = seen
        s.commit()
        ids = {"dismissed": dismissed.id, "hidden": hidden.id, "still": still.id}
        before = {r.id: (r.status, r.reason, r.evidence, r.decision_id, r.pair_key, r.priority,
                         r.resolved_at.replace(tzinfo=None, microsecond=0) if r.resolved_at else None)
                  for r in s.scalars(select(ReviewItem).where(ReviewItem.id.in_(list(ids.values()))))}
        index = {(r.review_id, r.canonical_id) for r in s.scalars(select(ReviewCandidate))}
    assert {(ids["hidden"], "atlas:c"), (ids["still"], "atlas:d")} <= index

    db.downgrade(url, "0033")
    tables = set(inspect(engine).get_table_names())
    assert {"review_item_0034_kept", "observation_0034_kept"} <= tables and "review_candidate" not in tables
    with engine.begin() as c:
        old_queue = {r[0] for r in c.execute(text("SELECT id FROM review_item WHERE status = 'open'"))}
        assert ids["hidden"] not in old_queue and ids["still"] in old_queue  # 0033 lists only kinds it knows
        # the previous release resolves the id_conflict meanwhile
        c.execute(text("UPDATE review_item SET status = 'merged', decided_by = 'old' WHERE id = :i"),
                  {"i": ids["still"]})

    db.upgrade(url)
    assert not ({"review_item_0034_kept", "observation_0034_kept"} & set(inspect(engine).get_table_names()))
    with mk() as s:
        after = {r.id: (r.status, r.reason, r.evidence, r.decision_id, r.pair_key, r.priority,
                        r.resolved_at.replace(tzinfo=None, microsecond=0) if r.resolved_at else None)
                 for r in s.scalars(select(ReviewItem).where(ReviewItem.id.in_(list(ids.values()))))}
        assert after[ids["dismissed"]] == before[ids["dismissed"]]  # the dismissal window still holds
        assert after[ids["hidden"]] == before[ids["hidden"]]  # back in the queue, same pair
        resolved = after[ids["still"]]
        assert resolved[0] == "merged" and resolved[1:4] == before[ids["still"]][1:4]  # the old decision stands
        assert {o.last_seen_at.replace(tzinfo=None) for o in s.scalars(select(Observation))} == {seen.replace(tzinfo=None)}
        index = {(r.review_id, r.canonical_id) for r in s.scalars(select(ReviewCandidate))}
        assert {(ids["hidden"], "atlas:c"), (ids["still"], "atlas:d"), (ids["dismissed"], "atlas:c")} <= index
        # the dismissal still suppresses the same pair at the same strength (§11.5 rule 3)
        assert matching.open_review(s, settings, "possible_duplicate", "atlas:d", [("atlas:c", 0.7)], 0.7) is None
    engine.dispose()
