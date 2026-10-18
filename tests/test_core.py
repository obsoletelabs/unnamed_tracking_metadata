from sqlalchemy import select

from atlas import service
from atlas.models import Canonical, Crosswalk, Observation, ReviewItem

from .conftest import batch


def ingest(session, ctx, source, items, **kw):
    res = service.ingest(session, ctx, batch(source, items, **kw.pop("b", {})), **kw)
    session.commit()
    return res


def test_creates_canonical_and_crosswalk(session, ctx):
    [r] = ingest(session, ctx, "steam", [
        {"external_id": "620", "title": "Portal 2", "release_date": "2011-04-19", "platform": "pc",
         "facts": {"developer": "Valve", "tags": ["Puzzle", "Co-op"]}},
    ])
    assert r.status == "created" and r.canonical_id.startswith("atlas:")
    row = session.get(Canonical, r.canonical_id)
    assert row.view["title"] == "Portal 2" and row.view["developer"] == "Valve"
    assert row.view["provider_ids"] == {"steam": "620"}
    assert session.get(Crosswalk, ("steam", "620")).canonical_id == r.canonical_id


def test_same_game_from_two_providers_merges(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "620", "title": "Portal 2", "release_date": "2011-04-19"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "1207658", "title": "Portal 2",
                                        "release_date": "2011-04-18", "facts": {"tags": ["puzzle"]}}])
    assert b.canonical_id == a.canonical_id and b.method == "exact"
    view = session.get(Canonical, a.canonical_id).view
    assert view["provider_ids"] == {"gog": "1207658", "steam": "620"}
    assert view["sources"] == ["gog", "steam"]


def test_explicit_cross_reference_wins(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Alpha"}])
    [b] = ingest(session, ctx, "igdb", [{"external_id": "9", "title": "Totally Different Name",
                                         "provider_ids": {"steam": "1"}}])
    assert a.canonical_id == b.canonical_id and b.method == "crosswalk"


def test_missing_year_creates_new_record_and_review(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Mystery Game"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Mystery Game"}])
    assert a.canonical_id != b.canonical_id
    assert b.review and b.candidates[0][0] == a.canonical_id
    item = session.scalars(select(ReviewItem)).one()
    assert item.kind == "possible_duplicate" and item.canonical_id == b.canonical_id


def test_sequels_never_auto_match(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Resident Evil 2", "release_date": "2019-01-25"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Resident Evil 3", "release_date": "2020-04-03"}])
    assert a.canonical_id != b.canonical_id and not b.review


def test_different_year_same_title_is_distinct(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Prey", "release_date": "2006-07-11"}])
    [b] = ingest(session, ctx, "steam", [{"external_id": "2", "title": "Prey", "release_date": "2017-05-05"}])
    assert a.canonical_id != b.canonical_id


def test_personal_facts_are_never_stored(session, ctx):
    ingest(session, ctx, "playnite", [{"external_id": "g1", "title": "Hades", "release_date": "2020-09-17",
                                       "facts": {"playtime_seconds": 9999, "owned": True, "tags": ["roguelike"]}}],
           b={"kind": "plugin"})
    obs = session.scalars(select(Observation)).one()
    assert obs.payload["facts"] == {"tags": ["roguelike"]}


def test_update_supersedes_and_stale_is_ignored(session, ctx):
    ingest(session, ctx, "steam", [{"external_id": "1", "title": "Old", "source_updated_at": 100}])
    [r] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "New", "source_updated_at": 200}])
    assert r.status == "updated"
    [stale] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Older", "source_updated_at": 50}])
    assert stale.status == "stale"
    row = session.get(Canonical, r.canonical_id)
    assert row.view["title"] == "New"
    cur = session.scalars(select(Observation).where(Observation.is_current.is_(True))).all()
    assert len(cur) == 1 and len(session.scalars(select(Observation)).all()) == 2


def test_omitted_field_does_not_clear(session, ctx):
    ingest(session, ctx, "steam", [{"external_id": "1", "title": "Game", "facts": {"developer": "Dev"}}])
    [r] = ingest(session, ctx, "gog", [{"external_id": "5", "title": "Game", "provider_ids": {"steam": "1"}}])
    assert session.get(Canonical, r.canonical_id).view["developer"] == "Dev"


def test_trust_priority_and_vote(session, ctx):
    ingest(session, ctx, "rawg", [{"external_id": "a", "title": "Foo", "release_date": "2020-01-01",
                                   "facts": {"developer": "Low"}}])
    [r] = ingest(session, ctx, "igdb", [{"external_id": "b", "title": "Foo", "release_date": "2020-01-01",
                                         "facts": {"developer": "High"}}])
    view = session.get(Canonical, r.canonical_id).view
    assert view["developer"] == "High"  # igdb trust 70 > rawg 50


def test_lock_beats_merge_and_clears(session, ctx):
    [r] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Wrong Name"}])
    service.set_lock(session, ctx, r.canonical_id, "title", "Right Name", actor="alice")
    session.commit()
    assert session.get(Canonical, r.canonical_id).view["title"] == "Right Name"
    ingest(session, ctx, "steam", [{"external_id": "1", "title": "Another"}])
    assert session.get(Canonical, r.canonical_id).view["title"] == "Right Name"
    service.clear_lock(session, ctx, r.canonical_id, "title")
    session.commit()
    assert session.get(Canonical, r.canonical_id).view["title"] == "Another"


def test_merge_leaves_redirect_and_repoints_ids(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Mystery Game"}])
    [b] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Mystery Game"}])
    service.merge_canonicals(session, ctx, b.canonical_id, a.canonical_id, actor="alice")
    session.commit()
    live, redirected = service.get_canonical(session, b.canonical_id)
    assert live.id == a.canonical_id and redirected == b.canonical_id
    assert session.get(Crosswalk, ("gog", "2")).canonical_id == a.canonical_id
    assert set(live.view["provider_ids"]) == {"gog", "steam"}
    assert session.scalars(select(ReviewItem)).one().status == "merged"
    # later claims for the merged id land on the survivor
    [c] = ingest(session, ctx, "gog", [{"external_id": "2", "title": "Mystery Game"}])
    assert c.canonical_id == a.canonical_id


def test_detach_splits_wrong_merge(session, ctx):
    [a] = ingest(session, ctx, "steam", [{"external_id": "1", "title": "Prey", "release_date": "2017-05-05"}])
    [b] = ingest(session, ctx, "igdb", [{"external_id": "2", "title": "Different", "provider_ids": {"steam": "1"}}])
    obs = session.scalars(select(Observation).where(Observation.external_id == "2")).one()
    new = service.detach_observation(session, ctx, obs.id)
    session.commit()
    assert new.id != a.canonical_id
    assert session.get(Crosswalk, ("igdb", "2")).canonical_id == new.id
    assert session.get(Crosswalk, ("steam", "1")).canonical_id == a.canonical_id


def test_search_ranks_exact_first(session, ctx):
    ingest(session, ctx, "steam", [
        {"external_id": "1", "title": "Hades", "release_date": "2020-09-17"},
        {"external_id": "2", "title": "Hades II", "release_date": "2024-05-06"},
        {"external_id": "3", "title": "Hades' Star", "release_date": "2016-01-01"},
    ])
    names = [r.view["title"] for r in service.search(session, "hades")]
    assert names[0] == "Hades" and len(names) == 3
