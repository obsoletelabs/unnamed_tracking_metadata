"""C11 §11.10 merge records and unmerge (A17): P4 merge -> unmerge round trips over seeded random records, rows
edited after the merge, last-in-first-out order, late chains, legacy merges, upstream-owned and signed merges, and
the "different works" memory an unmerge leaves. Every assertion reads the stored rows."""

from __future__ import annotations

import random

import pytest
from sqlalchemy import func, select

from atlas import facets, matching, merges, service
from atlas.models import (
    Achievement,
    Artwork,
    Canonical,
    Crosswalk,
    Lock,
    MatchRejection,
    MergeRecord,
    MergeRecordItem,
    Observation,
    RecordLive,
    ReviewItem,
)
from atlas.service import Ctx

from .conftest import batch, build_settings


@pytest.fixture
def lctx(session):
    """A local node: its merges are not in a signed feed, so they can be undone."""
    from atlas.signing import load_identity

    identity = load_identity(session)
    session.commit()
    return Ctx(settings=build_settings(role="local"), identity=identity)


def put(session, ctx, source, ext, title, **extra):
    [r] = service.ingest(session, ctx, batch(source, [{"external_id": ext, "title": title, **extra}]), curator=True)
    session.commit()
    return r.canonical_id


def state(session, *ids):
    """Everything an unmerge must give back, per record (versions excluded: they only move forward)."""
    out = {}
    for cid in ids:
        row = session.get(Canonical, cid)
        out[cid] = {
            "merged_into": row.merged_into, "view": row.view,
            "explain": {k: v for k, v in row.explain.items() if k != "_meta"},
            "obs": sorted((o.id, o.is_current) for o in session.scalars(
                select(Observation).where(Observation.canonical_id == cid))),
            "xwalk": sorted((c.namespace, c.external_id, c.variant) for c in session.scalars(
                select(Crosswalk).where(Crosswalk.canonical_id == cid))),
            "locks": sorted((lk.field, repr(lk.value), lk.cleared, lk.local_only) for lk in session.scalars(
                select(Lock).where(Lock.canonical_id == cid))),
            "art": sorted((a.id, a.source, a.kind, a.url) for a in session.scalars(
                select(Artwork).where(Artwork.canonical_id == cid))),
            "ach": sorted((a.id, a.source, a.name, a.key) for a in session.scalars(
                select(Achievement).where(Achievement.canonical_id == cid))),
        }
    return out


def seeded_pair(session, ctx, rng: random.Random, n: int):
    sources = rng.sample(["steam", "gog", "igdb", "epic", "rawg", "itch"], 4)
    a = put(session, ctx, sources[0], f"a{n}", f"Left Game {n}", release_date="2001-01-01",
            facts={"developer": "Dev", "genres": rng.sample(["RPG", "Action", "Puzzle"], 2)})
    b = put(session, ctx, sources[1], f"b{n}", f"Right Thing {n}", release_date="2015-06-06",
            facts={"description": "b side", "tags": rng.sample(["x", "y", "z"], 2)})
    if rng.random() < 0.7:  # a second chain on the merged record
        service.ingest(session, ctx, batch(sources[2], [{"external_id": f"c{n}", "title": f"Other {n}",
                                                          "provider_ids": {sources[1]: f"b{n}"}}]), curator=True)
    for cid, field, value in ((a, "series", "A series"), (b, "series", "B series"), (b, "website", "https://b.test")):
        if rng.random() < 0.6:
            service.set_lock(session, ctx, cid, field, value, actor="cur", local_only=True)
    if rng.random() < 0.5:
        service.set_lock(session, ctx, b, "publisher", None, actor="cur", local_only=True, clear=True)
    facets.set_artwork(session, ctx, b, sources[1], [{"kind": "key_art", "url": f"https://img.test/{n}/b.jpg"}])
    if rng.random() < 0.5:
        facets.set_artwork(session, ctx, a, sources[0], [{"kind": "key_art", "url": f"https://img.test/{n}/a.jpg"}])
    facets.set_achievements(session, ctx, b, sources[1], [{"name": "Winner", "external_id": "WIN"},
                                                          {"name": f"Only B {n}", "external_id": "ONLY"}])
    if rng.random() < 0.5:
        facets.set_achievements(session, ctx, a, sources[1], [{"name": "Winner", "external_id": "WIN"}])
    session.commit()
    return a, b


@pytest.mark.parametrize("seed", range(12))
def test_p4_merge_then_unmerge_gives_back_both_records(session, lctx, seed):
    rng = random.Random(seed)
    a, b = seeded_pair(session, lctx, rng, seed)
    before = state(session, a, b)
    service.merge_canonicals(session, lctx, b, a, actor="cur")
    session.commit()
    rec = session.scalars(select(MergeRecord).where(MergeRecord.src == b)).one()
    assert rec.dst == a and rec.cause == "curator" and rec.reverted_at is None and rec.item_count > 0
    assert session.get(RecordLive, b).live_id == a
    assert session.scalar(select(func.count()).select_from(MergeRecordItem).where(
        MergeRecordItem.merge_id == rec.id)) == rec.item_count
    assert session.get(Canonical, b).merged_into == a

    preview = merges.unmerge(session, lctx, rec.id, actor="cur", dry_run=True)
    assert preview["status"] == "preview" and preview["moves"]["observations"] >= 1
    assert session.get(Canonical, b).merged_into == a  # a preview changes nothing

    out = merges.unmerge(session, lctx, rec.id, actor="cur", remember_distinct=False)
    session.commit()
    assert out["status"] == "unmerged" and out["conflicts"] == [] and out["review_id"] is None
    assert state(session, a, b) == before, f"seed {seed}"
    session.refresh(rec)
    assert rec.reverted_at is not None and rec.reverted_by == "cur"
    assert session.get(RecordLive, b) is None
    assert session.scalars(select(MatchRejection)).all() == []


def test_unmerge_remembers_the_pair_as_different_works(session, lctx):
    a, b = seeded_pair(session, lctx, random.Random(99), 99)
    service.merge_canonicals(session, lctx, b, a, actor="cur")
    rec = session.scalars(select(MergeRecord)).one()
    merges.unmerge(session, lctx, rec.id, actor="cur")
    session.commit()
    rej = session.scalars(select(MatchRejection)).one()
    assert (rej.scope, {rej.a, rej.b}, rej.reason, rej.ended_at) == ("record", {a, b}, "unmerge", None)
    with pytest.raises(service.AtlasError) as err:
        service.merge_canonicals(session, lctx, b, a, actor="cur")
    assert err.value.code == "distinct"


def test_rows_edited_after_the_merge_are_reported_not_overwritten(session, lctx):
    a = put(session, lctx, "steam", "1", "Edited Game", release_date="2001-01-01")
    b = put(session, lctx, "gog", "2", "Edited Other", release_date="2009-01-01")
    service.set_lock(session, lctx, b, "series", "B series", actor="cur", local_only=True)
    session.commit()
    service.merge_canonicals(session, lctx, b, a, actor="cur")
    session.commit()
    lk = session.get(Lock, (a, "series"))
    assert lk is not None and lk.value == "B series"  # the lock moved to the survivor
    service.set_lock(session, lctx, a, "series", "Edited after", actor="cur2", local_only=True)
    session.commit()
    rec = session.scalars(select(MergeRecord)).one()
    out = merges.unmerge(session, lctx, rec.id, actor="cur")
    session.commit()
    assert {(c["kind"], c["reason"]) for c in out["conflicts"]} == {("lock", "edited_after_merge")}
    assert session.get(Lock, (a, "series")).value == "Edited after"  # the curator's later edit stands
    assert session.get(Lock, (b, "series")) is None
    review = session.get(ReviewItem, out["review_id"])
    assert review.kind == "unmerge_check" and review.status == "open" and review.canonical_id == a
    assert review.priority == matching.REVIEW_PRIORITY["unmerge_check"]
    # the observations still went back
    assert {o.external_id for o in session.scalars(select(Observation).where(Observation.canonical_id == b))} == {"2"}


def test_merges_are_undone_last_in_first_out(session, lctx):
    a = put(session, lctx, "steam", "1", "Chain A", release_date="2001-01-01")
    b = put(session, lctx, "gog", "2", "Chain B", release_date="2005-01-01")
    c = put(session, lctx, "epic", "3", "Chain C", release_date="2010-01-01")
    service.merge_canonicals(session, lctx, b, a, actor="cur")
    service.merge_canonicals(session, lctx, a, c, actor="cur")
    session.commit()
    first = session.scalars(select(MergeRecord).where(MergeRecord.src == b)).one()
    second = session.scalars(select(MergeRecord).where(MergeRecord.src == a)).one()
    assert session.get(RecordLive, b).live_id == c and session.get(RecordLive, a).live_id == c
    with pytest.raises(merges.MergeError) as err:
        merges.unmerge(session, lctx, first.id, actor="cur")
    assert err.value.code == "unmerge_blocked" and err.value.detail["blocked_by"] == [second.id]
    session.rollback()
    merges.unmerge(session, lctx, second.id, actor="cur", remember_distinct=False)
    merges.unmerge(session, lctx, first.id, actor="cur", remember_distinct=False)
    session.commit()
    assert {cid: session.get(Canonical, cid).merged_into for cid in (a, b, c)} == {a: None, b: None, c: None}
    assert {o.external_id: o.canonical_id for o in session.scalars(select(Observation))} == {"1": a, "2": b, "3": c}
    with pytest.raises(merges.MergeError) as err:
        merges.unmerge(session, lctx, first.id, actor="cur")
    assert err.value.code == "already_reverted"


def test_a_chain_that_arrived_after_the_merge_stays_or_goes_by_its_evidence(session, lctx):
    a = put(session, lctx, "steam", "1", "Late Game", release_date="2001-01-01")
    b = put(session, lctx, "gog", "2", "Late Other", release_date="2012-01-01")
    service.merge_canonicals(session, lctx, b, a, actor="cur")
    session.commit()
    # a new chain names the merged record's key: after the unmerge it belongs with that record
    late = put(session, lctx, "igdb", "9", "Something", provider_ids={"gog": "2"})
    assert late == a
    rec = session.scalars(select(MergeRecord)).one()
    out = merges.unmerge(session, lctx, rec.id, actor="cur", remember_distinct=False)
    session.commit()
    obs = session.scalars(select(Observation).where(Observation.external_id == "9")).one()
    assert obs.canonical_id == b and out["late_chains"]["to_src"] == 1
    assert session.get(Crosswalk, ("igdb", "9")).canonical_id == b  # its own key went with it


def test_if_match_must_name_the_current_survivor_version(session, lctx):
    a = put(session, lctx, "steam", "1", "Versioned", release_date="2001-01-01")
    b = put(session, lctx, "gog", "2", "Versioned Two", release_date="2011-01-01")
    service.merge_canonicals(session, lctx, b, a, actor="cur")
    session.commit()
    rec = session.scalars(select(MergeRecord)).one()
    v = session.get(Canonical, a).version
    with pytest.raises(merges.MergeError) as err:
        merges.unmerge(session, lctx, rec.id, actor="cur", if_match=f'"{a}-{v - 1}"')
    assert err.value.code == "stale_version" and err.value.detail["version"] == v
    session.rollback()
    assert merges.unmerge(session, lctx, rec.id, actor="cur", if_match=f'"{a}-{v}"')["status"] == "unmerged"


def test_a_merge_without_a_record_is_legacy(session, lctx):
    a = put(session, lctx, "steam", "1", "Legacy", release_date="2001-01-01")
    b = put(session, lctx, "gog", "2", "Legacy Two", release_date="2011-01-01")
    session.get(Canonical, b).merged_into = a  # merged before migration 0034
    session.commit()
    with pytest.raises(merges.MergeError) as err:
        merges.legacy_check(session, b)
    assert err.value.code == "legacy_merge"


def test_a_published_merge_cannot_be_undone_below_protocol_1_6(session, ctx):
    a = put(session, ctx, "steam", "1", "Published", release_date="2001-01-01")
    b = put(session, ctx, "gog", "2", "Published Two", release_date="2011-01-01")
    service.merge_canonicals(session, ctx, b, a, actor="cur")  # an authority: the merge enters the signed feed
    session.commit()
    rec = session.scalars(select(MergeRecord)).one()
    assert rec.feed_seq is not None
    with pytest.raises(merges.MergeError) as err:
        merges.unmerge(session, ctx, rec.id, actor="cur")
    assert err.value.code == "emit_protocol" and "1.7" in str(err.value)
    session.rollback()
    assert session.get(Canonical, b).merged_into == a


def test_an_upstream_merge_is_owned_by_the_upstream(session, lctx):
    a = put(session, lctx, "steam", "1", "Upstream", release_date="2001-01-01")
    b = put(session, lctx, "gog", "2", "Upstream Two", release_date="2011-01-01")
    service.merge_canonicals(session, lctx, b, a, actor="sync", log=False, cause="sync", origin=("node-up", 7))
    session.commit()
    rec = session.scalars(select(MergeRecord)).one()
    assert (rec.cause, rec.origin_node, rec.origin_seq) == ("sync", "node-up", 7)
    with pytest.raises(merges.MergeError) as err:
        merges.unmerge(session, lctx, rec.id, actor="cur")
    assert err.value.code == "upstream_owned"


def test_merge_preview_changes_nothing(session, lctx):
    a = put(session, lctx, "steam", "1", "Preview", release_date="2001-01-01")
    b = put(session, lctx, "gog", "2", "Preview Two", release_date="2011-01-01", facts={"developer": "Dev"})
    before = state(session, a, b)
    out = service.merge_preview(session, lctx, b, a)
    assert out["status"] == "preview" and out["undoable"] is True
    assert {"field": "developer", "before": None, "after": "Dev", "winner_source": "gog"} in out["changes"]
    assert state(session, a, b) == before
    assert session.scalar(select(func.count()).select_from(MergeRecord)) == 0


# ---- review items across a merge and its unmerge (P4, §11.5 rule 7, §11.9.2) ------------------------------

def bare(session, cid, title):
    session.add(Canonical(id=cid, view={"title": title}, explain={}, norm_title=title.lower()))


def reviews(session):
    """Every review item's identity, from the stored rows, with its candidate index rows."""
    from atlas.models import ReviewCandidate

    index = {}
    for row in session.scalars(select(ReviewCandidate)):
        index.setdefault(row.review_id, set()).add(row.canonical_id)
    return {r.id: (r.status, r.canonical_id, [list(c) for c in r.candidates], r.pair_key, r.resolved_at is None,
                   index.get(r.id, set()))
            for r in session.scalars(select(ReviewItem))}


def open_item(session, cid, *cands, score=0.7):
    item = ReviewItem(kind="possible_duplicate", canonical_id=cid, candidates=[[c, score] for c in cands],
                      confidence=score, pair_key=matching.pair_key("possible_duplicate", [cid, *cands]))
    session.add(item)
    session.flush()
    return item


def test_unmerge_gives_back_reviews_the_reevaluation_superseded_or_repointed(session, lctx):
    """X~A and X~B collapse to one pair when A goes into B (X~A superseded); Y~A is re-pointed at B. The unmerge
    restores both, with their pair keys and candidate index rows."""
    for cid, title in (("atlas:x", "X"), ("atlas:a", "A"), ("atlas:b", "B"), ("atlas:y", "Y")):
        bare(session, cid, title)
    session.flush()
    xa = open_item(session, "atlas:x", "atlas:a")
    xb = open_item(session, "atlas:x", "atlas:b")
    ya = open_item(session, "atlas:y", "atlas:a")
    session.commit()
    before = reviews(session)
    service.merge_canonicals(session, lctx, "atlas:a", "atlas:b", actor="cur")
    session.commit()
    merged = reviews(session)
    assert merged[xa.id][0] == "superseded" and merged[xb.id] == before[xb.id]
    assert merged[ya.id][:3] == ("open", "atlas:y", [["atlas:b", 0.7]]) and merged[ya.id][5] == {"atlas:b"}
    rec = session.scalars(select(MergeRecord)).one()
    assert {i.ref for i in merges.items_of(session, rec) if i.kind == "review"} == {xa.id, ya.id}
    merges.unmerge(session, lctx, rec.id, actor="cur", remember_distinct=False)
    session.commit()
    assert reviews(session) == before


def test_a_review_a_curator_resolved_after_the_merge_is_not_reopened(session, lctx):
    for cid, title in (("atlas:x", "X"), ("atlas:a", "A"), ("atlas:b", "B")):
        bare(session, cid, title)
    session.flush()
    xa = open_item(session, "atlas:x", "atlas:a")
    session.commit()
    service.merge_canonicals(session, lctx, "atlas:a", "atlas:b", actor="cur")
    session.commit()
    item = session.get(ReviewItem, xa.id)
    assert item.status == "open" and item.candidates == [["atlas:b", 0.7]]
    matching.close_review(session, item, "dismissed", "cur2")  # decided on the merged pair
    session.commit()
    rec = session.scalars(select(MergeRecord)).one()
    merges.unmerge(session, lctx, rec.id, actor="cur", remember_distinct=False)
    session.commit()
    session.refresh(item)
    assert (item.status, item.decided_by, item.canonical_id) == ("dismissed", "cur2", "atlas:x")


@pytest.mark.parametrize("seed", range(10))
def test_p4_review_queue_round_trips(session, lctx, seed):
    """P4 over random review queues: items between the merged pair, items naming only `src` or only `dst` as the
    record or as a candidate, items that collapse to one pair, and unrelated ones. After merge -> unmerge every
    item's status, record, candidates, pair key and candidate index rows are as before."""
    rng = random.Random(1000 + seed)
    ids = [f"atlas:r{seed}n{n}" for n in range(6)]
    for n, cid in enumerate(ids):
        bare(session, cid, f"Queue {seed} {n}")
    session.flush()
    src, dst = ids[0], ids[1]
    pairs = set()
    wanted = [(ids[2], [src]), (ids[2], [dst]), (src, [ids[3]])]  # always: a collapse and a re-point
    for _ in range(rng.randint(2, 8)):
        cid = rng.choice(ids)
        wanted.append((cid, rng.sample([i for i in ids if i != cid], rng.choice([1, 1, 2]))))
    for cid, cands in wanted:
        key = matching.pair_key("possible_duplicate", [cid, *cands])
        if key not in pairs:
            pairs.add(key)
            open_item(session, cid, *cands, score=round(rng.uniform(0.6, 0.9), 2))
    session.commit()
    before = reviews(session)
    service.merge_canonicals(session, lctx, src, dst, actor="cur")
    session.commit()
    after_merge = reviews(session)
    assert all(s[0] != "open" or src not in {s[1], *(c for c, _ in s[2])} for s in after_merge.values())
    assert after_merge != before
    rec = session.scalars(select(MergeRecord)).one()
    merges.unmerge(session, lctx, rec.id, actor="cur", remember_distinct=False)
    session.commit()
    assert reviews(session) == before, f"seed {seed}"


def test_a_merge_reads_only_the_review_items_it_touches(session, lctx):
    """§11.9.2: the review work of a merge and its unmerge is bounded by the items naming the pair, not by the
    size of the queue."""
    from sqlalchemy import event

    for cid, title in (("atlas:qa", "Qa"), ("atlas:qb", "Qb"), ("atlas:qx", "Qx")):
        bare(session, cid, title)
    session.flush()
    for i in range(300):
        c1, c2 = f"atlas:u{i:04d}a", f"atlas:u{i:04d}b"
        bare(session, c1, f"Unrelated {i} a")
        bare(session, c2, f"Unrelated {i} b")
        session.flush()
        open_item(session, c1, c2)
    near = open_item(session, "atlas:qx", "atlas:qa")
    session.commit()
    session.expunge_all()  # nothing cached: every item a merge reads is loaded from the database
    loaded: list[str] = []

    @event.listens_for(session, "loaded_as_persistent")
    def _loaded(_s, inst):
        if isinstance(inst, ReviewItem):
            loaded.append(inst.id)

    service.merge_canonicals(session, lctx, "atlas:qa", "atlas:qb", actor="cur")
    session.commit()
    assert set(loaded) == {near.id}
    rec = session.scalars(select(MergeRecord)).one()
    session.expunge_all()
    loaded.clear()
    merges.unmerge(session, lctx, rec.id, actor="cur", remember_distinct=False)
    session.commit()
    assert set(loaded) == {near.id}
    assert session.get(ReviewItem, near.id).candidates == [["atlas:qa", 0.7]]


def test_a_published_merge_stays_refused_after_the_node_changes_role(session, ctx):
    """The refusal follows the merge (`feed_seq`), not the node's current ATLAS_ROLE: every mirror has it."""
    a = put(session, ctx, "steam", "1", "Published Role", release_date="2001-01-01")
    b = put(session, ctx, "gog", "2", "Published Role Two", release_date="2011-01-01")
    service.merge_canonicals(session, ctx, b, a, actor="cur")
    session.commit()
    rec = session.scalars(select(MergeRecord)).one()
    assert rec.feed_seq is not None
    relabelled = Ctx(settings=build_settings(role="local"), identity=ctx.identity)
    with pytest.raises(merges.MergeError) as err:
        merges.unmerge(session, relabelled, rec.id, actor="cur")
    assert err.value.code == "emit_protocol" and "1.7" in str(err.value)
    session.rollback()
    assert session.get(Canonical, b).merged_into == a
    assert session.get(MergeRecord, rec.id).reverted_at is None
