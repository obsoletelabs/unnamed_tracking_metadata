"""Regression tests for the issues found in the independent review."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import main, ratelimit, service, signing, sync
from atlas.models import Canonical, Crosswalk, Lock, Observation, ReviewItem
from atlas.normalize import norm_title
from atlas.service import AtlasError

from .conftest import batch, build_settings
from .test_sync import curate, pair  # noqa: F401

ADMIN = "x" * 40


def put(session, ctx, source, items, **kw):
    kw.setdefault("system", False)
    res = service.ingest(session, ctx, batch(source, items, kind=kw.pop("kind", "plugin")), **kw)
    session.commit()
    return res


def test_uncurated_token_cannot_use_trusted_source_names(session, ctx):
    with pytest.raises(AtlasError, match="reserved"):
        put(session, ctx, "curator", [{"external_id": "1", "title": "Pwned"}])
    with pytest.raises(AtlasError, match="reserved"):
        put(session, ctx, "IGDB", [{"external_id": "1", "title": "Pwned"}])


def test_uncurated_claims_cannot_register_foreign_ids_or_override_trusted_fields(session, ctx):
    [real] = put(session, ctx, "igdb", [{"external_id": "5", "title": "Dota 2", "provider_ids": {"steam": "570"},
                                         "release_date": "2013-07-09"}], system=True)
    [evil] = put(session, ctx, "mallory", [{"external_id": "x", "title": "Hacked", "provider_ids": {
        "steam": "570", "igdb": "999"}}])
    # C11 §11.2.1 step 4: the claim contradicts the record (igdb 999 vs 5), so it is a conflict, not a match: the
    # observation goes to its own record and an id_conflict review is opened
    assert evil.canonical_id != real.canonical_id and evil.reason == "conflict" and evil.review
    assert session.get(Crosswalk, ("igdb", "999")) is None  # it cannot squat someone else's namespace
    assert session.get(Crosswalk, ("steam", "570")).canonical_id == real.canonical_id
    assert session.get(Canonical, real.canonical_id).view["title"] == "Dota 2"  # trust 40 < 70


def test_detach_with_history_moves_crosswalk_and_reingest_follows(session, ctx):
    put(session, ctx, "gogplugin", [{"external_id": "g1", "title": "Alpha", "release_date": "2020-01-01"}])
    [b] = put(session, ctx, "gogplugin", [{"external_id": "g1", "title": "Alpha", "release_date": "2020-01-01"}])
    [a] = put(session, ctx, "other", [{"external_id": "o1", "title": "Alpha", "release_date": "2020-01-01"}])
    assert a.canonical_id == b.canonical_id
    obs = session.scalars(select(Observation).where(Observation.source == "gogplugin", Observation.is_current)).one()
    new = service.detach_observation(session, ctx, obs.id)
    session.commit()
    assert session.get(Crosswalk, ("gogplugin", "g1")).canonical_id == new.id
    [again] = put(session, ctx, "gogplugin",
                  [{"external_id": "g1", "title": "Alpha Two", "release_date": "2020-01-01"}])
    assert again.canonical_id == new.id
    assert session.get(Canonical, new.id).view["title"] == "Alpha Two"


def test_reassignment_rebuilds_the_record_left_behind(session, ctx):
    [a] = put(session, ctx, "p1", [{"external_id": "1", "title": "Zeta Quest", "release_date": "2019-05-05"}])
    [b] = put(session, ctx, "p2", [{"external_id": "2", "title": "Different Title", "release_date": "2019-05-05"}])
    session.delete(session.get(Crosswalk, ("p1", "1")))  # simulate an identity that now routes elsewhere
    session.commit()
    [r] = put(session, ctx, "p1", [{"external_id": "1", "title": "Different Title", "release_date": "2019-05-05"}])
    assert r.canonical_id == b.canonical_id and a.canonical_id != b.canonical_id
    assert session.get(Canonical, a.canonical_id).view.get("sources") == []  # no stale view left behind
    assert session.get(Canonical, b.canonical_id).view["sources"] == ["p1", "p2"]


def test_feed_kind_is_signed(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    with a_maker() as s:
        [r] = service.ingest(s, a_ctx, batch("curator", [{"external_id": "1", "title": "T"}], kind="metadata"),
                             curator=True)
        service.set_lock(s, a_ctx, r.canonical_id, "title", "Locked")
        s.commit()
        from atlas import feed
        entry = feed.read(s, 1, 10)[0]
    pub = sync_pub(a_ctx)
    ok = signing.verify(pub, entry.key_id, entry.seq, entry.kind, entry.body, entry.body_hash, entry.signature)
    forged = signing.verify(pub, entry.key_id, entry.seq, "unlock", entry.body, entry.body_hash, entry.signature)
    assert ok and not forged


def sync_pub(ctx):
    return ctx.identity.public_b64


def test_future_timestamps_do_not_freeze_a_record(session, ctx):
    put(session, ctx, "p1", [{"external_id": "1", "title": "Game", "source_updated_at": 4_000_000_000}])
    [r] = put(session, ctx, "p1", [{"external_id": "1", "title": "Game v2", "source_updated_at": 1_700_000_000}])
    assert r.status == "updated"
    assert session.get(Canonical, r.canonical_id).view["title"] == "Game v2"


def test_source_case_cannot_duplicate_an_observation(session, ctx):
    put(session, ctx, "PluginX", [{"external_id": "1", "title": "Game"}])
    [r] = put(session, ctx, "pluginx", [{"external_id": "1", "title": "Game"}])
    assert r.status == "unchanged"  # same chain, same payload (C11 §11.9.1); the API reports it as `updated`
    current = session.scalars(select(Observation).where(Observation.is_current)).all()
    assert len(current) == 1


def test_oversized_titles_are_bounded(session, ctx):
    assert len(norm_title("ﷺ" * 500)) <= 400
    [r] = put(session, ctx, "p1", [{"external_id": "1", "title": "ﷺ" * 500}])
    assert session.get(Canonical, r.canonical_id).norm_title


def test_mirror_does_not_repoint_between_authoritative_records(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [a] = curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "Alpha Game"}])
    [b] = curate(a_maker, a_ctx, "curator", [{"external_id": "c2", "title": "Beta Game"}])
    curate(a_maker, a_ctx, "curator", [{"external_id": "c2", "title": "Beta Game",
                                        "provider_ids": {"curator": "c1"}}])
    with a_maker() as s:
        assert s.get(Crosswalk, ("curator", "c1")).canonical_id == a.canonical_id
        assert s.scalars(select(ReviewItem).where(ReviewItem.kind == "id_conflict")).first()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert s.get(Crosswalk, ("curator", "c1")).canonical_id == a.canonical_id
    assert b.canonical_id != a.canonical_id


def test_upstream_lock_does_not_overwrite_local_override(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [a] = curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "Alpha Game"}])
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        service.set_lock(s, m_ctx, a.canonical_id, "title", "My Title", local_only=True)
        s.commit()
    with a_maker() as s:
        service.set_lock(s, a_ctx, a.canonical_id, "title", "Authority Title")
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert s.get(Canonical, a.canonical_id).view["title"] == "My Title"
        assert s.get(Lock, (a.canonical_id, "title")).local_only


@pytest.fixture
def app_factory(tmp_path):
    def make(**kw):
        s = build_settings(database_url=f"sqlite:///{tmp_path}/r.db", **kw)
        return main.create_app(s)
    return make


def H(t):
    return {"Authorization": f"Bearer {t}"}


def test_forged_forwarded_for_does_not_evade_the_limit(app_factory, monkeypatch):
    # The limiter counts in a fixed 60-second wall-clock window. On a slow runner the 70 requests can straddle a minute
    # boundary and never put 61 into one window (a flake, not a defect). Freeze the clock the limiter reads; the
    # assertion is unchanged.
    real_hit = ratelimit.hit
    monkeypatch.setattr(ratelimit, "hit", lambda *a, **kw: real_hit(*a, clock=lambda: 1_700_000_010.0, **kw))
    with TestClient(app_factory(bootstrap_admin_token=ADMIN)) as c:
        codes = [c.get("/api/v2/games?q=x", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
                 for i in range(70)]
    assert 429 in codes


def test_rate_counters_are_purged(app_factory):
    app = app_factory(bootstrap_admin_token=ADMIN)
    maker = app.state.atlas.maker
    ratelimit.hit(maker, "old", 5, clock=lambda: 0.0)
    ratelimit.purge(maker, clock=lambda: 10_000.0)
    from atlas.models import RateCounter
    with maker() as s:
        assert s.get(RateCounter, ("old", 0)) is None


def test_bootstrap_token_rotation_and_removal(tmp_path):
    url = f"sqlite:///{tmp_path}/b.db"
    old, new = "o" * 40, "n" * 40
    with TestClient(main.create_app(build_settings(database_url=url, bootstrap_admin_token=old))) as c:
        assert c.get("/api/v2/tokens", headers=H(old)).status_code == 200
    with TestClient(main.create_app(build_settings(database_url=url, bootstrap_admin_token=new))) as c:
        assert c.get("/api/v2/tokens", headers=H(old)).status_code == 401
        assert c.get("/api/v2/tokens", headers=H(new)).status_code == 200
    with TestClient(main.create_app(build_settings(database_url=url))) as c:
        assert c.get("/api/v2/tokens", headers=H(new)).status_code == 401


def test_resolve_cap_and_minimal_ready(app_factory):
    with TestClient(app_factory(bootstrap_admin_token=ADMIN, max_resolve=5)) as c:
        too_many = {"items": [{"title": f"g{i}"} for i in range(6)]}
        assert c.post("/api/v2/resolve", json=too_many, headers=H(ADMIN)).status_code == 413
        r = c.get("/api/v2/ready")
        assert r.status_code == 200 and r.json() == {"status": "ready"}
        assert c.get("/api/v2/status").status_code == 401
        assert "feed_head" in c.get("/api/v2/status", headers=H(ADMIN)).json()


def test_lock_and_timestamp_bounds_are_rejected_not_500(app_factory):
    with TestClient(app_factory(bootstrap_admin_token=ADMIN)) as c:
        body = {"connection": {"source_kind": "plugin", "source": "p", "source_scope": "s"},
                "observations": [{"external_id": "1", "title": "t", "source_updated_at": 2**62}]}
        assert c.post("/api/v2/observations", json=body, headers=H(ADMIN)).status_code == 422
        assert c.put("/api/v2/games/atlas:x/locks/title", json={"value": "x" * 30000},
                     headers=H(ADMIN)).status_code == 422


def test_provider_ids_are_validated(app_factory):
    with TestClient(app_factory(bootstrap_admin_token=ADMIN, rawg_api_key="k")) as c:
        r = c.post("/api/v2/providers/rawg/fetch", json={"external_id": "../x?key=1"}, headers=H(ADMIN))
        assert r.status_code == 502 and "invalid provider id" in r.text


def test_search_budget_cannot_starve_fetch(session):
    from atlas.providers import Fetcher, build_registry
    s = build_settings()
    f = Fetcher(s, build_registry(s))
    assert f._budget(f.providers["steam"], "search").capacity < f._budget(f.providers["steam"], "fetch").capacity
