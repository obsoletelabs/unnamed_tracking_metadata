"""CONTRACT-C13 §5.1 step 4 (owner decision H56, OFF by default): a follower that receives a `record` may build the
game from its *own* providers, by the store ids the record carries, so art, description and achievements arrive
without anything being redistributed between nodes.

Real nodes on SQLite files talking through the real routes (`test_sync_compat.Node`); the follower's Steam adapter
is a fake that answers like the real one. Assertions are on the follower's database: observations, artwork,
achievements, the crosswalk, its own feed and the queued jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from atlas import enrich, feed
from atlas.models import Achievement, Artwork, Canonical, Crosswalk, NodeState, Observation
from atlas.providers.base import ProviderResult

from .test_c13_follower import curate_art, provider_only
from .test_sync_compat import Node, pull

STEAM_ART = "https://cdn.steam.example/apps/{id}/header.jpg"


class FakeSteam:
    """Answers like the Steam adapter: metadata, a header image and an achievement list."""

    name = "steam"
    redistributable = False
    requests_per_minute = 600

    def __init__(self):
        self.fetched: list[str] = []

    def search(self, client, query):
        return []

    def fetch(self, client, external_id):
        self.fetched.append(external_id)
        return ProviderResult(external_id=external_id, title=f"Steam game {external_id}", platform="pc",
                              provider_ids={"steam": external_id}, release_date="2026-05-14",
                              artwork=[{"kind": "hero", "url": STEAM_ART.format(id=external_id)}])

    def achievements(self, client, external_id, api_key=None):
        return [{"name": "First Dive", "description": "Reach 100 m."}]


def follower(tmp_path, name="mirror", **settings) -> tuple[Node, FakeSteam]:
    node = Node(tmp_path, name, "mirror", sync_enrich_from_own_providers=True, **settings)
    steam = FakeSteam()
    node.st.providers.providers["steam"] = steam
    return node, steam


def jobs(node: Node) -> list[tuple[str, str, str]]:
    with node.st.maker() as s:
        return sorted((j["ns"], j["id"], j["cid"]) for _, j in enrich.pending(s))


def test_record_enriches_from_own_provider(tmp_path):
    """T-F7: the record arrives with its Steam id; the follower fetches the game from its own Steam and attaches it
    to the record (art and achievements present here), signs nothing, and asks nothing of the authority."""
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    cid = provider_only(authority)
    curate_art(authority, cid)
    mirror, steam = follower(tmp_path)
    pull(mirror, authority)
    assert jobs(mirror) == [("steam", "1962700", cid)]
    assert enrich.run(mirror.st.maker, mirror.st) == {"fetched": 1, "dropped": 0, "deferred": 0, "failed": 0}
    assert steam.fetched == ["1962700"] and jobs(mirror) == []
    with mirror.st.maker() as s:
        steam_obs = s.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                        Observation.source == "steam")).one()
        assert steam_obs.origin_node is None and steam_obs.redistributable is False
        assert s.get(Crosswalk, ("steam", "1962700")).canonical_id == cid
        kinds = {(a.source, a.kind) for a in s.scalars(select(Artwork).where(Artwork.canonical_id == cid))}
        assert kinds == {("curator", "key_art"), ("steam", "hero")}
        assert [a.name for a in s.scalars(select(Achievement).where(Achievement.canonical_id == cid))] == [
            "First Dive"]
        assert s.get(Canonical, cid).merged_into is None
        assert feed.head_seq(s) == 0  # a follower signs nothing
    assert mirror.heads(authority)["state"] == "in_sync"


def test_enrichment_is_off_by_default(tmp_path):
    """H56 is the owner's decision: without the setting nothing is queued or fetched."""
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    curate_art(authority, provider_only(authority))
    mirror = Node(tmp_path, "mirror", "mirror")
    pull(mirror, authority)
    assert jobs(mirror) == []
    assert enrich.run(mirror.st.maker, mirror.st) == {"fetched": 0, "dropped": 0, "deferred": 0, "failed": 0}


def test_enrich_skips_key_owned_by_other_record(tmp_path):
    """T-F17: a second authority's record carries a Steam id that already belongs to the first authority's record
    here (a cross-origin duplicate under review). Fetching it with `force_canonical` would move the first record's
    observation, so it is never queued; the first record keeps its store id."""
    a = Node(tmp_path, "a", "authority", emit_protocol="1.5")
    b = Node(tmp_path, "b", "authority", emit_protocol="1.5")
    first, second = provider_only(a, "Subnautica 2", "1962700"), provider_only(b, "Subnautica 2", "1962700")
    curate_art(a, first)
    curate_art(b, second)
    mirror, steam = follower(tmp_path)
    pull(mirror, a)
    assert jobs(mirror) == [("steam", "1962700", first)]  # a's record: the store id points at it
    enrich.run(mirror.st.maker, mirror.st)
    pull(mirror, b)
    assert jobs(mirror) == []  # b's record found the key taken by a's record
    with mirror.st.maker() as s:
        assert s.get(Crosswalk, ("steam", "1962700")).canonical_id == first
        assert enrich.queue(s, mirror.st.ctx.settings, b.node_id, second, {"steam": "1962700"})["claimed"] == 1
        assert s.scalars(select(Observation.canonical_id).where(Observation.source == "steam")).all() == [first]
    assert steam.fetched == ["1962700"]


def test_enrich_rate_limited_and_validates_ids(tmp_path):
    """T-F17: an id the provider would refuse is never requested (counted instead), and at most
    `sync_enrich_per_peer_per_hour` fetches run per upstream feed; the rest wait for the next hour."""
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    for n in range(3):
        curate_art(authority, provider_only(authority, f"Game {n}", str(500 + n)))
    curate_art(authority, provider_only(authority, "Odd id", "12ab"))
    mirror, steam = follower(tmp_path, sync_enrich_per_peer_per_hour=2)
    pull(mirror, authority)
    assert [j[1] for j in jobs(mirror)] == ["500", "501", "502"]
    with mirror.st.maker() as s:
        assert s.get(NodeState, enrich.INVALID).value == "1"
    now = datetime(2026, 11, 7, 12, 5, tzinfo=UTC)
    assert enrich.run(mirror.st.maker, mirror.st, now=now) == {"fetched": 2, "dropped": 0, "deferred": 1,
                                                               "failed": 0}
    assert enrich.run(mirror.st.maker, mirror.st, now=now + timedelta(minutes=10))["deferred"] == 1
    assert enrich.run(mirror.st.maker, mirror.st, now=now + timedelta(hours=1))["fetched"] == 1
    assert sorted(steam.fetched) == ["500", "501", "502"] and jobs(mirror) == []


def test_a_late_record_for_a_record_the_follower_already_holds(tmp_path):
    """`introduce --missing` signs a legacy record X that was merged since (`feed.candidates`), after its merge
    entry. A follower that already holds X from another feed gets that late `record` too: it lands on X as one more
    identity observation (no second record, no stolen store id), the waiting merge then moves it to the survivor,
    and the queued enrichment follows the redirect. Once X is merged here, a late `record` X is a no-op: nothing is
    written and nothing is queued."""
    from atlas import sync

    from .test_c13_follower import ART_ROWS, art, waiting

    authority = Node(tmp_path, "authority", "authority")  # a legacy authority: nothing introduced
    a, b = provider_only(authority, "Alpha", "301"), provider_only(authority, "Beta", "302")
    curate_art(authority, a)
    with authority.st.maker() as s:
        from atlas import service

        service.merge_canonicals(s, authority.st.ctx, a, b, actor="curator")
        s.commit()
    other = Node(tmp_path, "other", "authority", emit_protocol="1.5")
    other.emit("record", {"canonical_id": a, "media_type": "game", "title": "Alpha", "provider_ids": {"steam": "301"},
                          "because": "publish"})
    mirror, steam = follower(tmp_path)
    pull(mirror, other)
    assert jobs(mirror) == [("steam", "301", a)]
    pull(mirror, authority)
    assert waiting(mirror, authority)  # the merge waits for B, which nobody has introduced
    authority.st.ctx.settings = authority.st.ctx.settings.model_copy(update={"emit_protocol": "1.5"})
    with authority.st.maker() as s:
        assert feed.introduce_batch(s, authority.st.ctx, "missing")["introduced"] == 2
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    assert mirror.heads(authority)["state"] == "in_sync"
    with mirror.st.maker() as s:
        assert s.get(Canonical, a).merged_into == b
        assert s.scalars(select(Canonical.id).where(Canonical.merged_into.is_(None))).all() == [b]
        assert s.get(Crosswalk, ("steam", "301")).canonical_id in (a, b)
        record_obs = s.scalars(select(Observation.origin_node).where(
            Observation.source == "atlas-record", Observation.canonical_id == b)).all()
        assert sorted(record_obs) == sorted([other.node_id, authority.node_id, authority.node_id])
    assert art(mirror, b) == ART_ROWS
    # the late `record` A is signed merged (no title, no ids) and B's carries steam 301 (B holds both store ids now):
    # the job queued from `other`'s record stays the only one, and runs against the survivor
    assert jobs(mirror) == [("steam", "301", a)]
    assert enrich.run(mirror.st.maker, mirror.st)["fetched"] == 1
    with mirror.st.maker() as s:
        assert {o.canonical_id for o in s.scalars(select(Observation).where(Observation.source == "steam"))} == {b}

    with mirror.st.maker() as s:  # X merged here: a late `record` X changes nothing and queues nothing
        before = s.scalars(select(Observation.id)).all()
        late = {"canonical_id": a, "media_type": "game", "title": "Alpha", "provider_ids": {"steam": "301"},
                "because": "backfill"}
        assert sync._apply_record(s, mirror.st.ctx, authority.node_id, 10_000, late) == sync.NOOP
        assert sync._apply_record(s, mirror.st.ctx, other.node_id, 10_000, late) == sync.NOOP
        assert s.scalars(select(Observation.id)).all() == before
        s.commit()
    assert jobs(mirror) == []


def test_the_hourly_allowance_is_counted_with_compare_and_set(tmp_path):
    """Two workers that read the same allowance cannot both spend its last fetch: the second write sees that the
    count moved and defers (reviewer finding on `_take`)."""
    mirror, _ = follower(tmp_path, sync_enrich_per_peer_per_hour=2)
    settings, now = mirror.st.ctx.settings, datetime(2026, 11, 7, 16, tzinfo=UTC)
    with mirror.st.maker() as s:
        assert enrich._take(s, settings, "peer", now)
        s.commit()
    with mirror.st.maker() as first, mirror.st.maker() as second:
        first.get(NodeState, enrich.HOUR + "peer")  # both workers read "1 used"
        second.get(NodeState, enrich.HOUR + "peer")
        assert enrich._take(second, settings, "peer", now)
        second.commit()
        assert not enrich._take(first, settings, "peer", now)  # the count moved under it: deferred
        first.commit()
    with mirror.st.maker() as s:
        assert s.get(NodeState, enrich.HOUR + "peer").value == "2026110716:2"
        assert not enrich._take(s, settings, "peer", now)
