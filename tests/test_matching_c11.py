"""C11 matching, merging and reconciliation (A17): explainable match decisions, vetoes, review queue rules,
trust classes, idempotent re-ingest, coalesced rebuilds, bounded explain and order-independent merging.

Property tests (P1, P3) run seeded random permutations: Hypothesis is not a dependency of this project
(CONTRACT-AMENDMENTS A17 #7), so each seed is a fixed, reproducible example and a failure names its seed.
Every scenario asserts the stored rows, not only the return values."""

from __future__ import annotations

import json
import random

import pytest
from sqlalchemy import func, select

from atlas import matching, provenance, service
from atlas.merge import CLASS_RANK, CLEARED, MAX_EXPLAIN_BYTES, Obs, effective_class, merge, source_class
from atlas.models import (
    Canonical,
    ChangeLog,
    Crosswalk,
    FieldConflict,
    MatchDecision,
    MatchRejection,
    Observation,
    ReviewItem,
)
from atlas.service import Ctx

from .conftest import batch, build_settings

SEEDS = range(25)


def ingest(session, ctx, source, items, *, kind="metadata", scope="test", curator=True):
    out = service.ingest(session, ctx, batch(source, items, kind=kind, scope=scope), curator=curator)
    session.commit()
    return out


def decisions(session, external_id=None):
    q = select(MatchDecision)
    if external_id is not None:
        q = q.where(MatchDecision.external_id == external_id)
    return session.scalars(q.order_by(MatchDecision.id)).all()


def live(session):
    return {c.id: c for c in session.scalars(select(Canonical).where(Canonical.merged_into.is_(None)))}


# ---- decisions are stored and explainable (§11.1, §11.4) --------------------------------------------------

def test_every_ingested_item_leaves_a_decision_with_tier_reason_and_evidence(session, ctx):
    [first] = ingest(session, ctx, "steam", [{"external_id": "620", "title": "Portal 2", "release_date": "2011-04-19",
                                             "facts": {"developer": "Valve"}}])
    [second] = ingest(session, ctx, "gog", [{"external_id": "g620", "title": "Portal 2", "release_date": "2011-04-19",
                                            "facts": {"developer": "Valve Corporation"}}])
    assert first.status == "created" and first.reason == "none"
    assert second.canonical_id == first.canonical_id and second.reason == "exact" and second.status == "updated"
    rows = decisions(session)
    assert [(d.external_id, d.outcome, d.tier, d.reason) for d in rows] == [
        ("620", "created", "none", "none"), ("g620", "assigned", "strong", "exact")]
    signals = {e["signal"] for e in rows[1].evidence}
    assert {"title_equal", "year_same", "developer_equal"} <= signals  # Valve == Valve Corporation (norm_company)
    assert rows[1].matcher == matching.MATCHER and rows[1].canonical_id == first.canonical_id
    assert rows[1].id == second.decision_id and rows[1].observation_id is not None
    assert rows[1].score == pytest.approx(second.confidence, abs=1e-3)


def test_a_crosswalk_hit_is_an_id_tier_decision(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Some Game"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "x", "title": "Totally Different", "provider_ids": {"steam": "1"}}])
    assert b.canonical_id == a.canonical_id
    d = decisions(session, "x")[0]
    assert (d.tier, d.reason, d.outcome) == ("id", "crosswalk", "assigned")


def test_sequel_guard_keeps_numbered_titles_apart(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "400", "title": "Portal", "release_date": "2007-10-10"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "p2", "title": "Portal 2", "release_date": "2007-10-10"}])
    assert a.canonical_id != b.canonical_id
    [c] = ingest(session, ctx, "epic", [{"external_id": "e3", "title": "Portal 3"}])
    assert c.canonical_id not in (a.canonical_id, b.canonical_id)
    assert len(live(session)) == 3


def test_placeholder_titles_never_match_by_title(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "9", "title": "Steam App 12345"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "y", "title": "Steam App 12345"}])
    assert a.canonical_id != b.canonical_id and b.reason == "placeholder"
    assert decisions(session, "y")[0].tier == "none"


def test_contradicting_id_in_the_same_namespace_vetoes_a_title_match(session, ctx):
    ingest(session, ctx, "igdb", [{"external_id": "i1", "title": "Twin Game", "release_date": "2020-01-01",
                                   "provider_ids": {"steam": "111"}}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "g2", "title": "Twin Game", "release_date": "2020-01-01",
                                       "provider_ids": {"steam": "222"}}])
    assert b.reason == "contradicted" and b.status == "created"
    # the contradicting key was never registered on the first record
    rows = session.scalars(select(Crosswalk).where(Crosswalk.namespace == "steam")).all()
    first = session.scalars(select(Crosswalk).where(Crosswalk.namespace == "igdb")).one().canonical_id
    assert {(r.external_id, r.canonical_id) for r in rows} == {("111", first), ("222", b.canonical_id)}


def test_two_records_claimed_by_one_observation_open_an_id_conflict_and_never_bridge(session, ctx):
    """H33: no automatic bridge in this release. A multi-hit is a conflict and a review, both records stay."""
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Left"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Right"}])
    [c] = ingest(session, ctx, "igdb", [{"external_id": "3", "title": "Both", "provider_ids": {"steam": "1", "gog": "2"}}])
    assert c.reason == "conflict" and c.review
    assert set(live(session)) >= {a.canonical_id, b.canonical_id}
    [item] = session.scalars(select(ReviewItem).where(ReviewItem.kind == "id_conflict")).all()
    assert item.status == "open" and item.reason == "conflict" and item.priority == matching.REVIEW_PRIORITY["id_conflict"]
    assert item.decision_id == decisions(session, "3")[0].id
    bridged = [e for e in decisions(session, "3")[0].evidence if e["signal"] == "bridge_blocked"]
    assert bridged and bridged[0]["value"] == ["not_available"]


# ---- rejection memory and the review queue (§11.5, §11.6) -------------------------------------------------

def test_detach_remembers_the_chain_so_a_reingest_never_title_matches_it_back(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Shared Name", "release_date": "2015-01-01"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Shared Name", "release_date": "2015-01-01"}])
    assert a.canonical_id == b.canonical_id
    obs = session.scalars(select(Observation).where(Observation.external_id == "2")).one()
    new = service.detach_observation(session, ctx, obs.id, actor="cur")
    session.commit()
    rej = session.scalars(select(MatchRejection).where(MatchRejection.scope == "chain")).one()
    assert rej.b == a.canonical_id and rej.ended_at is None and rej.actor == "cur"
    [again] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Shared Name", "release_date": "2015-01-01",
                                           "facts": {"description": "changed"}}])
    assert again.canonical_id == new.id != a.canonical_id
    # a different chain with the same title still matches the original record
    # a third chain with the same title now sees two equal records: ambiguous, a review, never a guess
    [other] = ingest(session, ctx, "epic", [{"external_id": "3", "title": "Shared Name", "release_date": "2015-01-01"}])
    assert other.reason == "ambiguous" and other.review and other.canonical_id not in (a.canonical_id, new.id)


def test_a_distinct_decision_blocks_title_matching_and_merging(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Doom", "release_date": "1993-12-10"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Doom (2016)", "release_date": "2016-05-13"}])
    matching.add_rejection(session, "record", a.canonical_id, b.canonical_id, reason="curator", actor="cur")
    session.commit()
    with pytest.raises(service.AtlasError) as err:
        service.merge_canonicals(session, ctx, b.canonical_id, a.canonical_id, actor="cur", log=False)
    assert err.value.code == "distinct" and err.value.detail["rejection_id"]
    session.rollback()
    service.merge_canonicals(session, ctx, b.canonical_id, a.canonical_id, actor="cur", log=False,
                             override_distinct=True)
    session.commit()
    rej = session.scalars(select(MatchRejection)).one()
    assert rej.ended_at is not None and rej.end_reason == "overridden_by_merge"
    assert session.get(Canonical, b.canonical_id).merged_into == a.canonical_id


def test_review_items_are_unique_per_pair_and_a_dismissal_is_remembered(session, ctx):
    s = build_settings(review_dismiss_days=30)
    ctx = Ctx(settings=s, identity=ctx.identity)
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "The Witcher 3 Wild Hunt"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "The Witcher 3 Wild Hunts"}])
    opened = session.scalars(select(ReviewItem).where(ReviewItem.status == "open")).all()
    if b.canonical_id == a.canonical_id:  # the fuzzy match was strong and clear enough to assign
        assert b.reason == "fuzzy" and not opened
        return
    assert len(opened) == 1 and opened[0].kind == "possible_duplicate" and opened[0].pair_key
    item = opened[0]
    assert item.evidence and item.reason and item.priority >= matching.REVIEW_PRIORITY["possible_duplicate"]
    # the same pair again (a second chain of the same record) refreshes the item instead of adding one
    ingest(session, ctx, "gog", [{"external_id": "2", "title": "The Witcher 3 Wild Hunts", "facts": {"genres": ["RPG"]}}])
    assert session.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")) == 1
    matching.close_review(session, item, "dismissed", "cur")
    session.commit()
    assert item.resolved_at is not None and item.decided_by == "cur"
    ingest(session, ctx, "gog", [{"external_id": "2", "title": "The Witcher 3 Wild Hunts", "facts": {"genres": ["X"]}}])
    assert session.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")) == 0


def test_a_merge_supersedes_review_items_that_collapse_to_one_record(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Left"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Right"}])
    item = matching.open_review(session, ctx.settings, "possible_duplicate", b.canonical_id, [(a.canonical_id, 0.8)],
                                0.8, reason="fuzzy", evidence=[matching.ev("title_fuzzy", "x", 0.8)])
    session.commit()
    assert item.pair_key and item.priority == matching.REVIEW_PRIORITY["possible_duplicate"] + 8
    service.merge_canonicals(session, ctx, b.canonical_id, a.canonical_id, actor="cur", log=False,
                             cause="review", review_id=item.id)
    session.commit()
    session.refresh(item)
    assert item.status in ("merged", "superseded") and item.resolved_at is not None


# ---- trust classes (§11.7.1) -----------------------------------------------------------------------------

def test_trust_class_ceiling_by_origin():
    assert source_class("curator") == "curator" and source_class("community") == "community"
    assert source_class("steam") == "provider"
    assert effective_class("curator", "provider") == "provider"  # an untrusted origin cannot claim curator
    assert effective_class("steam", "curator") == "provider"
    s = build_settings(trusted_keys={"node-up": "a" * 43}, sync_origin_class_cap={"node-cap": "community"})
    assert provenance.origin_ceiling(s, None) == "curator"
    assert provenance.origin_ceiling(s, "node-up") == "curator"
    assert provenance.origin_ceiling(s, "node-cap") == "community"
    assert provenance.origin_ceiling(s, "stranger") == "provider"


def obs(i, source, payload, *, trust=50, stamp=1000, cls="provider", kind="metadata"):
    return Obs(id=f"o{i:03d}", source=source, source_kind=kind, external_id=f"e{i}", payload=payload, trust=trust,
               stamp=stamp, cls=CLASS_RANK[cls], source_cls=CLASS_RANK[source_class(source)])


def test_class_beats_trust_and_preference_only_reorders_providers():
    rows = [obs(1, "igdb", {"title": "Provider Title"}, trust=99),
            obs(2, "community", {"title": "Community Title"}, trust=1, cls="community"),
            obs(3, "steam", {"title": "Steam Title"}, trust=10)]
    view, explain = merge(rows, {})
    assert view["title"] == "Community Title"
    assert explain["title"]["class"] == "community" and explain["title"]["reason"] == "class"
    view, explain = merge(rows[::2], {}, prefs={"title": ["steam"]})
    assert view["title"] == "Steam Title" and explain["title"]["reason"] == "preference"
    view, explain = merge(rows, {}, prefs={"title": ["steam"]})
    assert view["title"] == "Community Title"  # a preference never lifts a provider over community


def test_conflict_counts_only_within_the_winning_class():
    rows = [obs(1, "igdb", {"facts": {"developer": "A"}}, trust=90), obs(2, "steam", {"facts": {"developer": "B"}}),
            obs(3, "gog", {"facts": {"developer": "A"}})]
    _view, explain = merge(rows, {})
    e = explain["developer"]
    assert e["conflict"] is True and (e["agree"], e["disagree"]) == (2, 1) and e["runners_up"] == ["o002"]


def test_cleared_lock_empties_the_field_and_says_so():
    rows = [obs(1, "igdb", {"title": "T", "facts": {"description": "text"}, "genres": ["x"]})]
    view, explain = merge(rows, {"description": CLEARED, "genres": CLEARED})
    assert "description" not in view and explain["description"] == {"strategy": "cleared", "reason": "lock"}
    assert "genres" not in view and explain["genres"]["strategy"] == "cleared"


def test_explain_is_bounded():
    rows = [obs(i, f"source-number-{i:03d}-" + "s" * 40, {"title": "T", "facts": {"tags": [f"tag-{j}" for j in range(100)]}})
            for i in range(60)]
    _view, explain = merge(rows, {}, meta={"explain": 2})
    assert len(json.dumps(explain, separators=(",", ":"))) <= MAX_EXPLAIN_BYTES
    assert explain["tags"]["truncated"] is True and explain["tags"]["members"] == {}


# ---- P1: merging is order-independent -------------------------------------------------------------------

FIELD_POOL = {"title": ["Alpha", "Alpha Remastered", "Steam App 1"], "release_date": ["2001", "2001-03", "2001-03-04"],
              "developer": ["Dev A", "Dev B"], "genres": [["RPG"], ["rpg", "Action"], ["Puzzle"]],
              "platforms": [["PC"], ["pc", "Switch"]]}
SOURCES = ["steam", "gog", "igdb", "epic", "community", "curator", "rawg"]


def random_obs(rng: random.Random, n: int) -> list[Obs]:
    out = []
    for i in range(n):
        source = rng.choice(SOURCES)
        payload: dict = {"facts": {}}
        for field, values in FIELD_POOL.items():
            if rng.random() < 0.7:
                v = rng.choice(values)
                if field in ("title", "release_date"):
                    payload[field] = v
                elif field == "platforms":
                    payload["platforms"] = v
                else:
                    payload["facts"][field] = v
        if rng.random() < 0.3:
            payload["provider_ids"] = {"igdb": rng.choice(["1", "2"])}
        cls = source_class(source)
        out.append(Obs(id=f"{i:026d}", source=source, source_kind=rng.choice(["metadata", "plugin"]),
                       external_id=f"x{i}", payload=payload, trust=rng.choice([10, 50, 90]),
                       stamp=rng.choice([100, 200, 300]), cls=CLASS_RANK[cls], source_cls=CLASS_RANK[cls]))
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_p1_view_and_explain_do_not_depend_on_observation_order(seed):
    rng = random.Random(seed)
    rows = random_obs(rng, rng.randint(1, 12))
    locks = {"series": "Locked"} if rng.random() < 0.3 else {}
    if rng.random() < 0.3:
        locks["genres"] = CLEARED
    prefs = {"title": ["gog", "steam"]} if rng.random() < 0.5 else None
    expected = merge(rows, locks, prefs=prefs, meta={"explain": 2})
    for _ in range(8):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        assert merge(shuffled, locks, prefs=prefs, meta={"explain": 2}) == expected, f"seed {seed}"


@pytest.mark.parametrize("seed", range(6))
def test_p1_stored_views_converge_whatever_the_ingest_order(seed, tmp_path):
    """The same observations ingested in two random orders into two databases give the same view."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from atlas.db import make_sessionmaker
    from atlas.models import Base
    from atlas.signing import load_identity

    rng = random.Random(1000 + seed)
    items = []
    for i, source in enumerate(rng.sample(["steam", "gog", "igdb", "epic", "rawg"], 4)):
        items.append((source, {"external_id": f"{source}-{seed}", "title": "Convergent Game",
                               "release_date": rng.choice(["2010-05-06", "2010-05-07"]),
                               "source_updated_at": 1_700_000_000 + i * 10 + rng.randint(0, 5),
                               "facts": {"developer": "Dev A", "description": rng.choice(["one", "two"]),
                                         "genres": rng.sample(["RPG", "Action", "Puzzle"], 2)}}))
    views = []
    for _ in range(2):
        rng.shuffle(items)
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        with make_sessionmaker(engine)() as s:
            c = Ctx(settings=build_settings(), identity=load_identity(s))
            for source, item in items:
                service.ingest(s, c, batch(source, [item]), curator=True)
            s.commit()
            [row] = s.scalars(select(Canonical).where(Canonical.merged_into.is_(None))).all()
            views.append(row.view)
    assert views[0] == views[1]


# ---- P3: re-ingest is idempotent; one rebuild per record per batch (§11.9) --------------------------------

@pytest.mark.parametrize("seed", range(5))
def test_p3_reingesting_the_same_batch_changes_nothing(session, ctx, seed):
    rng = random.Random(seed)
    items = [{"external_id": f"k{i}", "title": rng.choice(["One", "Two", "Three"]) + f" {i % 3}",
              "facts": {"genres": [rng.choice(["RPG", "Action"])]}} for i in range(6)]
    ingest(session, ctx, "steam", items)
    before_obs = session.scalar(select(func.count()).select_from(Observation))
    before = {c.id: (c.version, c.view) for c in live(session).values()}
    feed_before = session.scalar(select(func.count()).select_from(ChangeLog))
    results = ingest(session, ctx, "steam", items)
    assert {r.status for r in results} == {"unchanged"}
    assert session.scalar(select(func.count()).select_from(Observation)) == before_obs
    assert {c.id: (c.version, c.view) for c in live(session).values()} == before
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == feed_before
    assert all(o.payload_hash for o in session.scalars(select(Observation)))
    assert all(o.last_seen_at is not None for o in session.scalars(select(Observation)))


def test_a_changed_payload_is_not_unchanged(session, ctx):
    ingest(session, ctx, "steam", [{"external_id": "1", "title": "Same"}])
    [r] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Same", "facts": {"developer": "New"}}])
    assert r.status == "updated"
    assert session.scalar(select(func.count()).select_from(Observation)) == 2


def test_a_batch_rebuilds_each_touched_record_once(session, ctx):
    [first] = ingest(session, ctx, "steam", [{"external_id": "base", "title": "Batch Game", "release_date": "2012-02-02"}])
    v0 = session.get(Canonical, first.canonical_id).version
    # one chain per source: two ids of one namespace on one record would contradict each other (V4)
    results = []
    from atlas.contract import ObservationBatchV1

    for i, source in enumerate(["gog", "epic", "igdb", "rawg", "itch"]):
        results += service.ingest(session, ctx, ObservationBatchV1.model_validate({
            "connection": {"source_kind": "metadata", "source": source, "source_scope": "test"},
            "observations": [{"external_id": f"g{i}", "title": "Batch Game", "release_date": "2012-02-02",
                              "facts": {"tags": [f"t{i}"]}}]}), curator=True)
    session.commit()
    assert {r.canonical_id for r in results} == {first.canonical_id}
    assert session.get(Canonical, first.canonical_id).version == v0 + 5  # five batches: five rebuilds
    v0 += 5
    # one batch that reaches the record three times: a platform-scoped store holds one id per platform (variant),
    # so the three ids do not contradict each other. One rebuild at the end of the batch.
    results = ingest(session, ctx, "psn", [
        {"external_id": f"psn-{p}", "title": "Batch Game", "release_date": "2012-02-02", "platform": p,
         "facts": {"tags": [f"s{i}"]}} for i, p in enumerate(["PS3", "PS4", "PS5"])])
    assert {r.canonical_id for r in results} == {first.canonical_id}
    row = session.get(Canonical, first.canonical_id)
    assert row.version == v0 + 1  # three items, one rebuild
    assert {"s0", "s1", "s2"} <= set(row.view["tags"])
    keys = session.scalars(select(Crosswalk).where(Crosswalk.namespace == "psn"))
    assert {(c.external_id, c.variant) for c in keys} == {(f"psn-PS{n}", f"platform:ps{n}") for n in (3, 4, 5)}


# ---- field conflicts index and explain v2 meta (§11.7.2) --------------------------------------------------

def test_field_conflict_index_and_explain_meta(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Conflicted", "release_date": "2019-09-09",
                                          "facts": {"description": "X"}}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Conflicted", "release_date": "2019-09-09",
                                       "facts": {"description": "Y"}}])
    assert b.canonical_id == a.canonical_id
    row = session.get(Canonical, a.canonical_id)
    assert row.explain["_meta"]["explain"] == 2 and row.explain["_meta"]["matcher"] == matching.MATCHER
    assert row.explain["_meta"]["trust_digest"].startswith("sha256:")
    fields = set(session.scalars(select(FieldConflict.field).where(FieldConflict.canonical_id == a.canonical_id)))
    assert "description" in fields
    service.set_lock(session, ctx, a.canonical_id, "description", "X", actor="cur", local_only=True)
    session.commit()
    fields = set(session.scalars(select(FieldConflict.field).where(FieldConflict.canonical_id == a.canonical_id)))
    assert "description" not in fields


def test_a_local_clear_lock_is_stored_and_a_signed_one_needs_protocol_1_6(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Clear Me", "facts": {"description": "d"}}])
    with pytest.raises(service.AtlasError) as err:
        service.set_lock(session, ctx, a.canonical_id, "description", None, actor="cur", clear=True)
    assert err.value.code == "emit_protocol" and "1.7" in str(err.value)
    session.rollback()
    with pytest.raises(service.AtlasError) as err:
        service.set_lock(session, ctx, a.canonical_id, "description", None, actor="cur", local_only=True)
    assert err.value.code == "invalid_lock"
    session.rollback()
    service.set_lock(session, ctx, a.canonical_id, "description", None, actor="cur", local_only=True, clear=True)
    session.commit()
    from atlas.models import Lock

    lk = session.get(Lock, (a.canonical_id, "description"))
    assert lk.cleared is True and lk.value is None and lk.local_only is True
    row = session.get(Canonical, a.canonical_id)
    assert "description" not in row.view and row.explain["description"]["strategy"] == "cleared"


def test_decision_retention_keeps_referenced_and_newest_rows(session, ctx):
    from datetime import timedelta

    from atlas.models import now

    for i in range(3):
        ingest(session, ctx, "steam", [{"external_id": "1", "title": f"Version {i}"}])
    rows = decisions(session, "1")
    assert len(rows) == 3
    for d in rows:
        d.created_at = now() - timedelta(days=400)
    session.commit()
    pruned = matching.prune_decisions(session, build_settings(match_decision_retain_days=180))
    session.commit()
    left = decisions(session, "1")
    assert pruned == 2 and [d.id for d in left] == [rows[-1].id]  # the newest per chain stays


# ---- bounded work (§11.11, §11.12) ----------------------------------------------------------------------

def test_provenance_answers_are_bounded_and_host_chains_are_counted_not_listed(session, ctx):
    [first] = ingest(session, ctx, "steam", [{"external_id": "base", "title": "Crowded", "release_date": "2018-08-08"}])
    for i in range(30):  # thirty installations of one plugin report the same game
        ingest(session, ctx, "playnite", [{"external_id": "base", "title": "Crowded", "provider_ids": {"steam": "base"}}],
               kind="plugin", scope=f"host-{i}")
    for i, source in enumerate(["gog", "epic", "igdb", "rawg"]):
        ingest(session, ctx, source, [{"external_id": f"m{i}", "title": "Crowded", "release_date": "2018-08-08"}])
    row = session.get(Canonical, first.canonical_id)
    assert session.scalar(select(func.count()).select_from(Observation).where(
        Observation.canonical_id == row.id)) == 35
    out = provenance.provenance(session, ctx.settings, row, curate=False, values=False)
    listed = [o for o in out["observations"] if "id" in o]
    grouped = [o for o in out["observations"] if "count" in o]
    assert len(listed) == 5 and grouped == [{"source": "playnite", "source_kind": "plugin", "count": 30}]
    assert "decisions" not in out
    full = provenance.provenance(session, ctx.settings, row, curate=True, values=True)
    assert len(full["decisions"]) <= provenance.MAX_EMBEDDED and full["decisions_next"]
    assert len(full["fields"]["title"]["candidates"]) <= provenance.MAX_GROUPS
    page = provenance.observations_page(session, row.id, limit=1000, cursor=None)
    assert len(page["results"]) == 35 and page["next"] is None  # limit is capped at MAX_PAGE (100)


def test_decision_retention_is_not_stalled_by_old_rows_it_keeps(session, ctx):
    """More than four thousand old chains whose only decision is their newest (kept), then one chain changed
    three times: its two older decisions are found and deleted behind the keepers, in batches."""
    from datetime import timedelta

    from atlas.models import now

    old = now() - timedelta(days=400)
    rows = [MatchDecision(source="steam", source_scope="test", external_id=f"k{i}", canonical_id=f"atlas:k{i}",
                          outcome="created", tier="none", reason="none", score=0.0, evidence=[], candidates=[],
                          matcher=matching.MATCHER, created_at=old - timedelta(days=1, seconds=i)) for i in range(4100)]
    session.add_all(rows)
    session.commit()
    for i in range(3):
        ingest(session, ctx, "gog", [{"external_id": "g1", "title": f"Changed {i}"}])
    chain = decisions(session, "g1")
    assert len(chain) == 3
    for d in chain:
        d.created_at = old
    session.commit()
    pruned = matching.prune_decisions(session, build_settings(match_decision_retain_days=180), batch=1000)
    session.commit()
    assert pruned == 2 and [d.id for d in decisions(session, "g1")] == [chain[-1].id]
    assert session.scalar(select(func.count()).select_from(MatchDecision)) == 4101  # every keeper is still there
    many = [MatchDecision(source="epic", source_scope="test", external_id="busy", canonical_id="atlas:busy",
                          outcome="assigned", tier="id", reason="crosswalk", score=1.0, evidence=[], candidates=[],
                          matcher=matching.MATCHER, created_at=old + timedelta(seconds=i)) for i in range(2500)]
    session.add_all(many)
    session.commit()
    assert matching.prune_decisions(session, build_settings(match_decision_retain_days=180), batch=1000) == 2499
    session.commit()
    left = decisions(session, "busy")
    assert [d.id for d in left] == [many[-1].id]  # several batches in one call; the newest stays


def test_a_contradicting_key_on_the_own_record_is_listed_as_a_provider_ids_conflict(session, ctx):
    """§11.2.1 step 4 with only one record involved: the observation's own key names the record, a provider id it
    sends contradicts the record. There is no other record for an id_conflict item, so the contradiction is in
    the decision's evidence and the record is in the `field_conflict` index for `provider_ids`."""
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Own Record"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Own Record", "provider_ids": {"steam": "1"}}])
    assert b.canonical_id == a.canonical_id
    [c] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Own Record", "provider_ids": {"steam": "999"}}])
    assert c.canonical_id == a.canonical_id and c.reason == "conflict"
    d = decisions(session, "2")[-1]
    assert any(e["signal"] == "namespace_conflict" and "steam:999" in json.dumps(e["value"]) for e in d.evidence)
    assert session.get(Crosswalk, ("steam", "999")) is None  # never registered
    assert session.scalars(select(ReviewItem)).all() == []
    row = session.get(Canonical, a.canonical_id)
    assert row.explain["provider_ids"]["conflict"] is True
    assert {v["value"] for v in row.explain["provider_ids"]["conflicts"]["steam"]} == {"1", "999"}
    assert session.get(FieldConflict, (a.canonical_id, "provider_ids")) is not None


def test_an_upstream_configured_as_a_peer_is_an_authority_origin(session, ctx):
    """§11.7.1 / H48: every pinned upstream whose merges and locks this node applies is a configured authority:
    ATLAS_TRUSTED_KEYS and Admin → Peers alike. Other origins stay capped at `provider`."""
    from atlas.models import Peer

    session.add(Peer(name="up", url="https://up.test", node_id="node-peer", public_key="k" * 43))
    session.commit()
    s = build_settings(trusted_keys={"node-env": "a" * 43})
    authorities = provenance.authority_origins(session, s)
    assert authorities == {"node-env", "node-peer"}
    assert provenance.origin_ceiling(s, "node-peer", authorities) == "curator"
    assert provenance.origin_ceiling(s, "node-env", authorities) == "curator"
    assert provenance.origin_ceiling(s, "relayed", authorities) == "provider"
    capped = build_settings(sync_origin_class_cap={"node-peer": "community"})
    assert provenance.origin_ceiling(capped, "node-peer", provenance.authority_origins(session, capped)) == "community"
    # a replicated curator observation from the Peer upstream keeps its class in the stored view
    [mine] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Peer Title", "facts": {"developer": "Mine"}}])
    session.add(Observation(id="01JPEER0000000000000000000", canonical_id=mine.canonical_id, source_kind="metadata",
                            source="curator", source_scope="up", external_id="c1", origin_node="node-peer",
                            payload={"title": "Peer Title", "facts": {"developer": "Upstream Curator"}},
                            redistributable=True))
    session.commit()
    row = service.rebuild(session, ctx, mine.canonical_id)
    session.commit()
    assert row.view["developer"] == "Upstream Curator" and row.explain["developer"]["class"] == "curator"


def test_ids_naming_records_of_different_types_are_an_id_conflict_at_ingest(session, ctx):
    """Review on #105, carried into the C11 matcher: the conflict is decided on every record the ids name, before
    the media-type filter, so the id naming a record of another type is never dropped and the item is never
    assigned by crosswalk. Ingest keeps the item on its own key's record only when that record has the item's
    type; an item is never assigned to a record of another media type."""
    [game] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Crossed Wires"}])
    [movie] = ingest(session, ctx, "imdb", [{"external_id": "2", "title": "Crossed Wires: The Film"}])
    session.get(Canonical, movie.canonical_id).media_type = "movie"
    session.commit()

    def typed_ingest(source, item):
        [out] = service.ingest(session, ctx, batch(source, [item]), curator=True, media_type="game")
        session.commit()
        return out

    both = typed_ingest("igdb", {"external_id": "3", "title": "Crossed Wires", "provider_ids": {"steam": "1", "imdb": "2"}})
    assert both.reason == "conflict" and both.canonical_id not in (game.canonical_id, movie.canonical_id)
    assert session.get(Canonical, both.canonical_id).media_type == "game"

    own_game = typed_ingest("steam", {"external_id": "1", "title": "Crossed Wires", "provider_ids": {"imdb": "2"}})
    assert own_game.reason == "conflict" and own_game.canonical_id == game.canonical_id

    own_movie = typed_ingest("imdb", {"external_id": "2", "title": "Crossed Wires", "provider_ids": {"steam": "1"}})
    assert own_movie.reason == "conflict" and own_movie.canonical_id != movie.canonical_id
    assert session.get(Canonical, own_movie.canonical_id).media_type == "game"
    kinds = {i.kind for i in session.scalars(select(ReviewItem).where(ReviewItem.status == "open"))}
    assert "id_conflict" in kinds
