"""CONTRACT-C13 §5 (A19): a follower waits for a record instead of quarantining what names it, and applies the
waiting entries when the record arrives.

Real nodes (the FastAPI app on SQLite files) talking through the real routes, as in `test_sync_compat`. Every
test asserts the follower's database: `sync_failure` rows, the cursor, observations, records and artwork."""

from __future__ import annotations

from sqlalchemy import select

from atlas import facets, feed, service, sync
from atlas.models import Artwork, Canonical, Observation, ReplicatedChange, SyncCursor, SyncFailure

from .conftest import batch
from .test_sync_compat import Node, pull

ART = [{"kind": "key_art", "url": "https://img.example.org/key.png"}]
ART_ROWS = [("key_art", "https://img.example.org/key.png")]


def configure(node: Node, **changes) -> None:
    node.st.ctx.settings = node.st.ctx.settings.model_copy(update=changes)


def provider_only(node: Node, title="Subnautica 2", steam="1962700") -> str:
    with node.st.maker() as s:
        [result] = service.ingest(s, node.st.ctx, batch("steam", [{"external_id": steam, "title": title,
                                                                    "provider_ids": {"steam": steam}}]), system=True)
        s.commit()
    return result.canonical_id


def curate_art(node: Node, cid: str, items=ART) -> None:
    with node.st.maker() as s:
        facets.set_artwork(s, node.st.ctx, cid, "curator", items, redistributable=True)
        s.commit()


def waiting(node: Node, origin: Node):
    with node.st.maker() as s:
        return [(f.seq, f.kind, f.disposition, f.reason, f.waiting_for, f.attempts) for f in s.scalars(
            select(SyncFailure).where(SyncFailure.node_id == origin.node_id, SyncFailure.resolved_at.is_(None))
            .order_by(SyncFailure.seq))]


def art(node: Node, cid: str):
    with node.st.maker() as s:
        return sorted((a.kind, a.url) for a in s.scalars(select(Artwork).where(Artwork.canonical_id == cid)))


def cursor(node: Node, origin: Node) -> int:
    with node.st.maker() as s:
        row = s.get(SyncCursor, origin.node_id)
        return row.last_seq if row else 0


def test_the_live_failure_waits_and_recovers_once_the_authority_introduces_the_record(tmp_path):
    """T-F1/T-F3: the observed shape. The 1.0-emitting authority signs artwork for a provider-only record. The
    follower records the entry as waiting (not quarantined, not counted as attempts), moves its cursor on, and
    reports `waiting`. Once the authority emits 1.5 and runs `introduce --missing`, the next pull applies the
    `record` and, in the same transaction, the waiting artwork."""
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    curate_art(authority, cid)
    pull(mirror, authority)
    assert waiting(mirror, authority) == [(1, "artwork", "wait", "waiting_for_record", cid, 1)]
    assert cursor(mirror, authority) == 1  # it does not block
    pull(mirror, authority)
    pull(mirror, authority)
    assert waiting(mirror, authority)[0][5] == 1  # waiting is not failing: no attempts counted
    status = mirror.heads(authority)
    assert status["state"] == "waiting" and status["waiting"]["count"] == 1 and status["waiting"]["records"] == 1
    assert status["quarantined"]["count"] == 0
    from .test_sync_compat import _admin

    listed = mirror.client.get("/api/v2/sync/failures", headers={"Authorization": f"Bearer {_admin(mirror)}"}).json()
    assert (listed["waiting"], listed["quarantined"]) == (1, 0)
    assert {k: listed["failures"][0][k] for k in ("disposition", "waiting_for", "next_try_at")} == {
        "disposition": "wait", "waiting_for": cid, "next_try_at": None}

    configure(authority, emit_protocol="1.5")
    with authority.st.maker() as s:
        assert feed.introduce_batch(s, authority.st.ctx, "missing")["introduced"] == 1
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    assert art(mirror, cid) == [("key_art", "https://img.example.org/key.png")]
    with mirror.st.maker() as s:
        record = s.get(Canonical, cid)
        assert record.view.get("title") == "Subnautica 2"
        obs = s.get(Observation, feed.record_obs_id(authority.node_id, cid))
        assert (obs.source, obs.source_scope, obs.origin_node, obs.origin_seq) == (
            "atlas-record", authority.node_id, authority.node_id, 2)
        failure = s.get(SyncFailure, (authority.node_id, 1))
        assert failure.resolved_at is not None
    with authority.st.maker() as s:
        assert s.get(Observation, feed.record_obs_id(authority.node_id, cid)).payload == obs.payload
    assert mirror.heads(authority)["state"] == "in_sync"


def test_the_bridge_lets_an_old_follower_apply_at_once(tmp_path):
    """T-F2: with the bridge on, the authority introduces the record with an `atlas-record` observation before the
    artwork, so the artwork applies on the first pull and nothing waits."""
    authority = Node(tmp_path, "authority", "authority", federation_bridge_record_obs=True)
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    curate_art(authority, cid)
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    assert art(mirror, cid) == [("key_art", "https://img.example.org/key.png")]
    with mirror.st.maker() as s:
        assert s.get(Observation, feed.record_obs_id(authority.node_id, cid)).source == "atlas-record"


def test_a_record_folds_in_the_followers_own_copy_of_the_same_game(tmp_path):
    """T-F6 (first half): the follower fetched the same Steam game itself. The record's store id folds its purely
    local record into the authority's, so the sidecars land on one record and the follower's provider data is kept."""
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    mirror = Node(tmp_path, "mirror", "mirror")
    local = provider_only(mirror, "Subnautica 2 (local fetch)")
    cid = provider_only(authority)
    curate_art(authority, cid)
    pull(mirror, authority)
    with mirror.st.maker() as s:
        assert s.get(Canonical, local).merged_into == cid
        sources = {o.source for o in s.scalars(select(Observation).where(Observation.canonical_id == cid))}
        assert sources == {"steam", "atlas-record"}
        assert s.get(Canonical, cid).view["title"] == "Subnautica 2 (local fetch)"  # a provider outranks it
    assert art(mirror, cid) == [("key_art", "https://img.example.org/key.png")]


def test_a_detach_of_an_unfederated_observation_is_a_noop_and_its_record_follows(tmp_path):
    """T-A8 (follower side): the follower never had the moved observation; the detach changes nothing, and the
    `record` right after it creates the new record."""
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority, "Pair", "10")
    with authority.st.maker() as s:
        service.ingest(s, authority.st.ctx, batch("gog", [{"external_id": "g1", "title": "Pair Two",
                                                            "provider_ids": {"steam": "10"}}]), system=True)
        moved = s.scalars(select(Observation).where(Observation.source == "gog")).one()
        new = service.detach_observation(s, authority.st.ctx, moved.id, actor="t").id
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    with mirror.st.maker() as s:
        assert s.get(Canonical, cid) is not None and s.get(Canonical, new) is not None
        kinds = [r.kind for r in s.scalars(select(ReplicatedChange).order_by(ReplicatedChange.seq))]
    assert kinds == ["record", "detach", "record"]


def test_wake_is_batched_and_the_retry_pass_applies_the_rest(tmp_path):
    """T-F14: more waiting entries than `sync_wake_batch`: the wake applies them batch by batch in the same pull,
    in feed order, so the last list is the one left."""
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror", sync_wake_batch=2)
    cid = provider_only(authority)
    for n in range(5):
        curate_art(authority, cid, [{"kind": "screenshot", "url": f"https://img.example.org/s{m}.png"}
                                    for m in range(n + 1)])
    pull(mirror, authority)
    assert len(waiting(mirror, authority)) == 5
    configure(authority, emit_protocol="1.5")
    with authority.st.maker() as s:
        feed.introduce_batch(s, authority.st.ctx, "missing")
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    assert len(art(mirror, cid)) == 5  # the last list, applied after the earlier ones (feed order)


def test_open_failures_are_bounded(tmp_path):
    """T-F13: at `sync_max_open_failures` open entries the follower stops applying (`backlog_full`) and keeps its
    cursor; nothing is lost: the entries stay retained."""
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror", sync_max_open_failures=2)
    for n in range(4):
        curate_art(authority, provider_only(authority, f"Game {n}", str(100 + n)))
    pull(mirror, authority)
    assert len(waiting(mirror, authority)) == 2
    assert cursor(mirror, authority) == 2
    status = mirror.heads(authority)
    assert status["state"] == "backlog_full" and status["verified_head"] == 4


def test_rescan_turns_an_old_unknown_record_quarantine_into_waiting(tmp_path):
    """T-F8: a row a previous release quarantined as "unknown canonical" waits for its record after a rescan."""
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    curate_art(authority, cid)
    pull(mirror, authority)
    with mirror.st.maker() as s:  # as the previous release left it
        row = s.get(SyncFailure, (authority.node_id, 1))
        row.disposition, row.reason, row.waiting_for, row.attempts = "quarantined", "apply_error", None, 20
        row.error = "AtlasError: unknown or merged canonical"
        s.commit()
    with mirror.st.maker() as s:
        counts = sync.rescan(s, authority.node_id)
        s.commit()
    assert counts["waiting"] == 1
    assert waiting(mirror, authority) == [(1, "artwork", "wait", "waiting_for_record", cid, 20)]


def as_a_1_0_release_left_it(node: Node, origin: Node, cid: str) -> None:
    """A 1.0 release swallowed the "unknown canonical" error of a sidecar: no `sync_failure` row, no artwork."""
    with node.st.maker() as s:
        s.execute(SyncFailure.__table__.delete().where(SyncFailure.node_id == origin.node_id))
        s.execute(Artwork.__table__.delete().where(Artwork.canonical_id == cid))
        s.commit()


def test_rescan_finds_sidecars_a_1_0_release_dropped_before_their_record(tmp_path):
    """T-F9 (C13 §5.6): a 1.0 release recorded nothing for artwork whose record had not arrived. After the upgrade
    the rescan makes it wait while the record is still unknown, and the record's arrival applies it."""
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    curate_art(authority, cid)
    pull(mirror, authority)
    as_a_1_0_release_left_it(mirror, authority, cid)
    with mirror.st.maker() as s:
        counts = sync.rescan(s, authority.node_id)
        s.commit()
    assert counts == {"scanned": 1, "parked": 0, "quarantined": 0, "waiting": 1}
    assert waiting(mirror, authority) == [(1, "artwork", "wait", "dropped_by_previous_release", cid, 1)]
    assert mirror.heads(authority)["state"] == "waiting"
    with mirror.st.maker() as s:
        assert "found by rescan" in s.get(SyncFailure, (authority.node_id, 1)).error
    configure(authority, emit_protocol="1.5")
    with authority.st.maker() as s:
        feed.introduce_batch(s, authority.st.ctx, "missing")
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    assert art(mirror, cid) == ART_ROWS
    with mirror.st.maker() as s:
        assert sync.rescan(s, authority.node_id) == {"scanned": 2, "parked": 0, "quarantined": 0}


def test_rescan_parks_sidecars_a_1_0_release_dropped_when_their_record_came_later(tmp_path):
    """T-F10 (C13 §5.6): the 1.0 release already received the record (the bridge observation, signed after the
    artwork) before the upgrade. The rescan parks the artwork as dropped_by_previous_release; `replay --historical`
    applies it in feed order; a second rescan records nothing."""
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    curate_art(authority, cid)
    configure(authority, federation_bridge_record_obs=True)
    with authority.st.maker() as s:
        assert feed.introduce_batch(s, authority.st.ctx, "missing")["introduced"] == 1
        s.commit()
    pull(mirror, authority)
    assert art(mirror, cid) == ART_ROWS  # this release waited, then applied it
    as_a_1_0_release_left_it(mirror, authority, cid)
    with mirror.st.maker() as s:
        counts = sync.rescan(s, authority.node_id)
        s.commit()
    assert counts == {"scanned": 2, "parked": 1, "quarantined": 0}
    with mirror.st.maker() as s:
        row = s.get(SyncFailure, (authority.node_id, 1))
        assert (row.disposition, row.reason) == ("parked", "dropped_by_previous_release")
        sync.replay(s, mirror.st.ctx, authority.node_id, historical=True)
        s.commit()
    assert art(mirror, cid) == ART_ROWS
    with mirror.st.maker() as s:
        assert sync.rescan(s, authority.node_id) == {"scanned": 2, "parked": 0, "quarantined": 0}
        assert s.get(SyncFailure, (authority.node_id, 1)).resolved_at is not None


def test_one_feed_cannot_supersede_another_feeds_observation(tmp_path):
    """R5: two authorities sign an observation of the same source record. On a follower of both, the second does
    not retire the first; each feed still supersedes its own."""
    from atlas.ids import canonical_id, ulid

    a, b = Node(tmp_path, "a", "authority"), Node(tmp_path, "b", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")

    def obs(title):
        return {"canonical_id": canonical_id(), "media_type": "game",
                "observation": {"id": ulid(), "source_kind": "manual", "source": "manual", "source_scope": "shared",
                                "external_id": "same-record", "payload": {"title": title},
                                "source_updated_at": None, "observed_at": "2026-01-01T00:00:00+00:00"}}

    first, second, third = obs("From A"), obs("From B"), obs("From A again")
    a.emit("obs", first)
    pull(mirror, a)
    b.emit("obs", second)
    pull(mirror, b)
    with mirror.st.maker() as s:
        current = {o.id: o.is_current for o in s.scalars(select(Observation).where(
            Observation.external_id == "same-record"))}
    assert current == {first["observation"]["id"]: True, second["observation"]["id"]: True}
    a.emit("obs", third)
    pull(mirror, a)
    with mirror.st.maker() as s:
        current = {o.id: o.is_current for o in s.scalars(select(Observation).where(
            Observation.external_id == "same-record"))}
    assert current == {first["observation"]["id"]: False, second["observation"]["id"]: True,
                       third["observation"]["id"]: True}


def test_sync_info_reports_what_the_feed_introduced(tmp_path):
    """C13 §3.4: the additive `/sync/info` fields an operator and a prober read."""
    authority = Node(tmp_path, "authority", "authority")
    cid = provider_only(authority)
    curate_art(authority, cid)
    info = authority.client.get("/api/v2/sync/info", headers={"Authorization": f"Bearer {authority.token}"}).json()
    assert info["own_head"] == 1 and info["introduced_through"] is None
    assert info["records"] == {"live": 1, "introduced": 0, "not_federated": 1}
    assert info["media_withheld"] == 0 and info["protocol"] == "1.5" and "record" in info["kinds"]
    mirror = Node(tmp_path, "mirror", "mirror")
    pull(mirror, authority)
    relayed = mirror.client.get("/api/v2/sync/info", headers={"Authorization": f"Bearer {mirror.token}"}).json()
    assert "records" not in relayed and relayed["own_head"] == 0
    assert [(f["node_id"], f["empty"]) for f in relayed["feeds"]] == [(authority.node_id, False)]


def test_wake_batches_large_groups_in_order(tmp_path):
    """T-F14: more waiting entries than `sync_wake_batch`, and a newer entry for the same record in the same page.
    Every waiting entry applies (in batches) before the next entry of the feed, so the newest list wins, as on the
    authority, and nothing is left open."""
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror", sync_wake_batch=2)
    cid = provider_only(authority)
    for n in range(5):
        curate_art(authority, cid, [{"kind": "screenshot", "url": f"https://img.example.org/s{n}.png"}])
    pull(mirror, authority)
    assert len(waiting(mirror, authority)) == 5
    configure(authority, emit_protocol="1.5")
    with authority.st.maker() as s:
        feed.introduce_batch(s, authority.st.ctx, "missing")
        s.commit()
    curate_art(authority, cid, [{"kind": "screenshot", "url": "https://img.example.org/newest.png"}])
    pull(mirror, authority)
    assert art(mirror, cid) == art(authority, cid) == [("screenshot", "https://img.example.org/newest.png")]
    assert waiting(mirror, authority) == []
    with mirror.st.maker() as s:
        assert s.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None))).first() is None
    assert mirror.heads(authority)["state"] == "in_sync"


def test_wake_is_scoped_to_introducing_feed(tmp_path):
    """T-F12: feed B signs artwork for a record only feed A introduces. When A introduces it, B's entry keeps
    waiting: not on A's pull, not on B's retry pass, not on the startup wake. B cannot stage changes for A's record."""
    a = Node(tmp_path, "a", "authority", emit_protocol="1.5")
    b = Node(tmp_path, "b", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = provider_only(a)
    b.emit("artwork", {"canonical_id": cid, "source": "curator",
                       "items": [{"kind": "key_art", "url": "https://img.example.org/staged.png"}]})
    pull(mirror, b)
    assert waiting(mirror, b) == [(1, "artwork", "wait", "waiting_for_record", cid, 1)]
    curate_art(a, cid)  # A introduces the record (a `record` entry) before its own artwork
    pull(mirror, a)
    assert art(mirror, cid) == ART_ROWS
    pull(mirror, b)
    with mirror.st.maker() as s:
        assert sync.retry_failures(s, mirror.st.ctx, b.node_id, due_only=False)["retried"] == 0
        assert sync._wake_ready(s, mirror.st.ctx, None, 100) == 0
        s.commit()
    assert waiting(mirror, b) == [(1, "artwork", "wait", "waiting_for_record", cid, 1)]
    assert art(mirror, cid) == ART_ROWS  # the staged list never landed
    assert mirror.heads(b)["state"] == "waiting"


def test_sidecar_for_locally_merged_record_applies_to_survivor(tmp_path):
    """T-F4: the follower's operator merged two records here. A later artwork entry for the merged-away record
    applies to the survivor (C13 §5.2 redirects) instead of failing for ever."""
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    x, y = authority.publish("Hades"), authority.publish("Hades (duplicate)")
    pull(mirror, authority)
    with mirror.st.maker() as s:
        service.merge_canonicals(s, mirror.st.ctx, x, y, actor="admin")
        s.commit()
    curate_art(authority, x)
    pull(mirror, authority)
    assert art(mirror, y) == ART_ROWS
    with mirror.st.maker() as s:
        assert s.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None))).first() is None
    assert mirror.heads(authority)["state"] == "in_sync"


def test_record_apply_cross_origin_opens_review(tmp_path):
    """T-F6 (second half): two authorities each publish a record for the same Steam game. On a follower of both,
    the second record is not merged into the first: a `cross_origin_duplicate` review names both, with the shared
    store id as evidence. Applying the record again opens nothing new."""
    from atlas.models import ReviewItem

    a = Node(tmp_path, "a", "authority", emit_protocol="1.5")
    b = Node(tmp_path, "b", "authority", emit_protocol="1.5")
    mirror = Node(tmp_path, "mirror", "mirror")
    first, second = provider_only(a, "Subnautica 2", "1962700"), provider_only(b, "Subnautica 2", "1962700")
    curate_art(a, first)
    curate_art(b, second)
    pull(mirror, a)
    pull(mirror, b)
    with mirror.st.maker() as s:
        assert s.get(Canonical, first).merged_into is None and s.get(Canonical, second).merged_into is None
        [item] = s.scalars(select(ReviewItem).where(ReviewItem.kind == "cross_origin_duplicate")).all()
        assert (item.canonical_id, item.candidates, item.status) == (second, [[first, 1.0]], "open")
        assert item.evidence == [{"kind": "shared_store_id", "namespace": "steam", "external_id": "1962700"}]
        sync.replay(s, mirror.st.ctx, b.node_id)
        s.commit()
    with mirror.st.maker() as s:
        assert len(s.scalars(select(ReviewItem).where(ReviewItem.kind == "cross_origin_duplicate")).all()) == 1


def test_upstream_defect_needs_introduced_through(tmp_path):
    """T-F15: without `introduced_through` the group reads `upstream_unpublished`; once the upstream says it
    introduced every record through a seq this node has applied, the same group is reported as the upstream's
    defect. The copy-a-note text names the record and the fix."""
    from atlas.models import NodeState

    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    curate_art(authority, cid)
    pull(mirror, authority)
    with mirror.st.maker() as s:
        [group] = sync.waiting_groups(s, authority.node_id)
    assert (group["subreason"], group["canonical_id"], group["kinds"], group["seqs"]) == (
        "upstream_unpublished", cid, {"artwork": 1}, [1, 1])
    assert "atlas feed introduce --missing" in group["note"] and cid in group["note"]
    with authority.st.maker() as s:  # a broken upstream: it claims everything is introduced
        s.add(NodeState(key=feed.INTRODUCED_THROUGH, value="1"))
        s.commit()
    pull(mirror, authority)
    with mirror.st.maker() as s:
        [group] = sync.waiting_groups(s, authority.node_id)
        assert group["subreason"] == "upstream_defect"
        assert s.get(NodeState, "sync_introduced_through:" + authority.node_id).value == "1"


def test_waiting_has_no_attempt_cap(tmp_path):
    """T-F3: thirty pulls later the entry still waits, with its single attempt, and nothing says retries stopped."""
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    curate_art(authority, cid)
    for _ in range(30):
        pull(mirror, authority)
    assert waiting(mirror, authority) == [(1, "artwork", "wait", "waiting_for_record", cid, 1)]
    with mirror.st.maker() as s:
        assert "stopped" not in s.get(SyncFailure, (authority.node_id, 1)).error


def test_removing_a_peer_abandons_its_waiting_entries(tmp_path):
    """C13 §5.2: removing a peer resolves its open waits as `abandoned` (they stop counting toward the bound); a
    rescan after following the feed again re-flags them."""
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    curate_art(authority, cid)
    pull(mirror, authority)
    with mirror.st.maker() as s:
        assert sync.abandon_waiting(s, authority.node_id) == 1
        s.commit()
    assert waiting(mirror, authority) == []
    with mirror.st.maker() as s:
        row = s.get(SyncFailure, (authority.node_id, 1))
        assert (row.disposition, row.reason, row.resolved_at is not None) == ("wait", "abandoned", True)
        assert sync.open_waiting(s, authority.node_id)["count"] == 0
        sync.rescan(s, authority.node_id)
        s.commit()
    assert waiting(mirror, authority) == [(1, "artwork", "wait", "dropped_by_previous_release", cid, 2)]


def test_upstream_defect_subreason_on_1_5_feed(tmp_path):
    """T-F5: a feed at 1.5 that has published every record it names through `introduced_through` still signs a
    sidecar for a record it never introduced (an upstream bug, signed here past the gate). The follower keeps it
    waiting, but names it as the upstream's defect, not as "will arrive"."""
    from atlas.models import Peer

    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    mirror = Node(tmp_path, "mirror", "mirror")
    authority.publish("Hades")
    with authority.st.maker() as s:
        assert feed.introduce_batch(s, authority.st.ctx, "missing")["finished"] is True
        assert feed.introduced_through(s) == 1
        s.commit()
    cid = provider_only(authority)
    seq = authority.emit("artwork", {"canonical_id": cid, "source": "curator", "items": ART})
    pull(mirror, authority)
    assert waiting(mirror, authority) == [(seq, "artwork", "wait", "waiting_for_record", cid, 1)]
    with mirror.st.maker() as s:
        s.add(Peer(name="Central", url="http://upstream", node_id=authority.node_id, follow="own",
                   public_key=authority.st.ctx.identity.trust_public_b64))
        s.commit()
        [group] = sync.waiting_groups(s, authority.node_id, name="Central")
        assert (group["subreason"], group["label"]) == ("upstream_defect",
                                                        "Waiting for its game: Central's feed is missing it")
        assert group["title"] is None and "never published the record itself" in group["note"]
    assert mirror.heads(authority)["state"] == "waiting"


def test_unlock_waits_behind_waiting_lock(tmp_path):
    """T-F16: a lock and the unlock after it both name a record the follower does not hold. The unlock waits too
    (it is not an idempotent no-op on an unknown record), so when the record arrives the lock applies and then the
    unlock: the field ends up unlocked, as on the authority."""
    from atlas.models import Lock

    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    cid = provider_only(authority)
    with authority.st.maker() as s:
        service.set_lock(s, authority.st.ctx, cid, "title", "Subnautica 2 (Curated)", actor="curator")
        service.clear_lock(s, authority.st.ctx, cid, "title")
        s.commit()
    pull(mirror, authority)
    assert [(r[1], r[2], r[4]) for r in waiting(mirror, authority)] == [("lock", "wait", cid), ("unlock", "wait", cid)]
    configure(authority, emit_protocol="1.5")
    with authority.st.maker() as s:
        feed.introduce_batch(s, authority.st.ctx, "missing")
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    with mirror.st.maker() as s:
        assert s.get(Lock, (cid, "title")) is None
        assert s.get(Canonical, cid).view["title"] == "Subnautica 2"
    with authority.st.maker() as s:
        assert s.get(Lock, (cid, "title")) is None


def test_merge_rewaits_for_second_canonical(tmp_path):
    """T-F16: a merge names two records the follower does not hold. It waits for the first; when that arrives and
    the second is still missing, the same row waits again, for the second (no attempt counted); the second's
    arrival applies the merge. Each arrival that applied something counts once in `atlas_sync_woken_total`."""
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    mirror = Node(tmp_path, "mirror", "mirror")
    a, b = provider_only(authority, "Alpha", "201"), provider_only(authority, "Beta", "202")
    seq = authority.emit("merge", {"src": a, "dst": b})  # as a release before C13 signed it: nothing introduced
    pull(mirror, authority)
    assert waiting(mirror, authority) == [(seq, "merge", "wait", "waiting_for_record", a, 1)]
    with authority.st.maker() as s:
        feed.ensure_introduced(s, authority.st.ctx, a, "backfill")
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == [(seq, "merge", "wait", "waiting_for_record", b, 1)]
    with mirror.st.maker() as s:
        assert s.get(Canonical, a).merged_into is None
    with authority.st.maker() as s:
        feed.ensure_introduced(s, authority.st.ctx, b, "backfill")
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    with mirror.st.maker() as s:
        assert s.get(Canonical, a).merged_into == b
        assert sync.woken_counts(s) == {(authority.node_id, "record"): 1}  # the first arrival applied nothing


def test_merge_does_not_follow_redirect(tmp_path):
    """T-F16: the follower merged X into Y here. The authority later merges X into Z. A sidecar would follow X to Y,
    but a merge never does: re-targeting it would join Y and Z, which nobody decided. It is recorded for the
    operator, and Y stays a record of its own."""
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    x, y, z = authority.publish("Doom"), authority.publish("Doom (1993)"), authority.publish("Doom Classic")
    pull(mirror, authority)
    with mirror.st.maker() as s:
        service.merge_canonicals(s, mirror.st.ctx, x, y, actor="admin")
        s.commit()
    with authority.st.maker() as s:
        service.merge_canonicals(s, authority.st.ctx, x, z, actor="curator")
        s.commit()
    pull(mirror, authority)
    with mirror.st.maker() as s:
        assert s.get(Canonical, y).merged_into is None and s.get(Canonical, z).merged_into is None
        assert s.get(Canonical, x).merged_into == y
    [row] = waiting(mirror, authority)
    assert (row[1], row[2], row[4]) == ("merge", "quarantined", None)


def test_a_legacy_merge_of_a_record_never_published_applies_after_introduce(tmp_path):
    """A legacy (pre-1.5) authority set artwork on a provider-only record A and then merged A into another
    provider-only record B. Followers never heard of either: the artwork and the merge wait for A. `introduce
    --missing` publishes A as well, although it is merged now (signed entries name it), so the follower creates A,
    applies the artwork, merges A into B and ends with what the authority holds."""
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    a, b = provider_only(authority, "Alpha", "301"), provider_only(authority, "Beta", "302")
    curate_art(authority, a)
    with authority.st.maker() as s:
        service.merge_canonicals(s, authority.st.ctx, a, b, actor="curator")
        s.commit()
    pull(mirror, authority)
    assert [(r[1], r[4]) for r in waiting(mirror, authority)] == [("artwork", a), ("merge", a)]
    configure(authority, emit_protocol="1.5")
    with authority.st.maker() as s:
        assert feed.unintroduced_count(s, "missing") == 2
        assert feed.introduce_batch(s, authority.st.ctx, "missing")["introduced"] == 2
        s.commit()
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    with mirror.st.maker() as s:
        assert s.get(Canonical, a).merged_into == b
    assert art(mirror, b) == art(authority, b) == ART_ROWS
    assert mirror.heads(authority)["state"] == "in_sync"


def test_waiting_groups_carry_a_title_when_one_is_known(tmp_path):
    """C13 §8.1: a waiting group names its game when this node knows a title: the record is already here from
    another feed (`here`), or the entry of the same feed that would bring it carries one but did not apply
    (`observation`). Otherwise the title is None and the page says "A game ... hasn't published yet"."""
    from atlas.ids import ulid

    a = Node(tmp_path, "a", "authority", emit_protocol="1.5")
    b = Node(tmp_path, "b", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = provider_only(a)
    b.emit("artwork", {"canonical_id": cid, "source": "curator", "items": ART})
    pull(mirror, b)
    with mirror.st.maker() as s:
        [group] = sync.waiting_groups(s, b.node_id)
        assert (group["title"], group["title_provenance"]) == (None, None)
    curate_art(a, cid)  # A introduces the record; B's entry keeps waiting for B (§5.3 scope)
    pull(mirror, a)
    with mirror.st.maker() as s:
        [group] = sync.waiting_groups(s, b.node_id)
        assert (group["title"], group["title_provenance"]) == ("Subnautica 2", "here")

    broken = "atlas:01m5a1vhdrabwv215gqg1g8nx9"
    bad = b.emit("obs", {"canonical_id": broken, "media_type": "game",
                         "observation": {"id": "not-a-ulid", "source_kind": "manual", "source": "manual",
                                         "source_scope": "b", "external_id": ulid(),
                                         "payload": {"title": "Outer   Wilds"}, "source_updated_at": None,
                                         "observed_at": "2026-01-01T00:00:00+00:00"}})
    b.emit("artwork", {"canonical_id": broken, "source": "curator", "items": ART})
    pull(mirror, b)
    with mirror.st.maker() as s:
        group = next(g for g in sync.waiting_groups(s, b.node_id) if g["canonical_id"] == broken)
    assert (group["subreason"], group["blocked_by"]) == ("blocked_by_entry", bad)
    assert (group["title"], group["title_provenance"]) == ("Outer Wilds", "observation")
