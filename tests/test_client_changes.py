"""Local client change log (CONTRACTS-v1 C3, #78 part 2, #62).

Provider imports and other non-redistributable writes bump the record version and reach this node's own
clients through `GET /api/v2/changes?feed=local`, while the signed feed, relay and every follower stay
exactly as before. Scenarios run real nodes (the FastAPI app on SQLite files) through the real routes and
assert on database state: `client_change` rows, `change_log` / `replicated_change` content and versions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from atlas import client_changes, custom, service
from atlas.models import (
    Achievement,
    Artwork,
    Canonical,
    ClientChange,
    CustomValue,
    NodeState,
    Observation,
    ReplicatedChange,
)
from atlas.providers.base import ProviderResult

from .conftest import batch
from .test_sync_compat import Node, pull

PROVIDER = "fakeprov"
SECRET_NAME = "Provider-only Achievement Name"  # licensed provider text that must never leave the node
SECRET_ART = "https://img.example/provider-only-key-art.jpg"


class FakeProvider:
    """A non-redistributable provider: metadata, artwork, crossplay and achievements."""

    name = PROVIDER
    redistributable = False
    requests_per_minute = 600

    def __init__(self, title="Fez", achievement=SECRET_NAME, crossplay="yes"):
        self.title, self.achievement, self.crossplay = title, achievement, crossplay

    def search(self, client, query):
        return []

    def fetch(self, client, external_id):
        return ProviderResult(external_id=external_id, title=self.title, platform="pc", release_date="2018-01-25",
                              provider_ids={PROVIDER: external_id},
                              artwork=[{"kind": "key_art", "url": SECRET_ART}],
                              custom={"crossplay": self.crossplay, "players_max": 4})

    def achievements(self, client, external_id):
        return [{"name": self.achievement, "description": "Provider description."}]


def H(node):
    return {"Authorization": f"Bearer {node.token}"}


def curator(node):
    from atlas import auth

    with node.st.maker() as session:
        _, secret = auth.create_token(session, "curator", ["read", "contribute", "curate"])
        session.commit()
    return {"Authorization": f"Bearer {secret}"}


def local_page(node, since=0, limit=100):
    r = node.client.get("/api/v2/changes", params={"feed": "local", "since": since, "limit": limit}, headers=H(node))
    assert r.status_code == 200, r.text
    return r.json()


def v1_page(node, since=0, **params):
    r = node.client.get("/api/v2/changes", params={"since": since, **params}, headers=H(node))
    assert r.status_code == 200, r.text
    return r.json()


def signed_feed(node, since=0):
    r = node.client.get("/api/v2/sync/changes", params={"since": since, "limit": 1000}, headers=H(node))
    assert r.status_code == 200, r.text
    return r.json()


def rows(node, cid=None):
    with node.st.maker() as session:
        q = select(ClientChange).order_by(ClientChange.seq)
        if cid:
            q = q.where(ClientChange.canonical_id == cid)
        return [(r.seq, r.canonical_id, r.version, r.cause, r.origin_node, r.origin_seq) for r in session.scalars(q)]


def version(node, cid):
    with node.st.maker() as session:
        return session.get(Canonical, cid).version


def provider_import(node, external_id="77", provider=None, headers=None):
    node.st.providers.providers[PROVIDER] = provider or FakeProvider()
    r = node.client.post(f"/api/v2/providers/{PROVIDER}/fetch", json={"external_id": external_id},
                         headers=headers or curator(node))
    assert r.status_code == 200, r.text
    return r.json()["game"]["id"]


@pytest.fixture
def authority(tmp_path):
    node = Node(tmp_path, "authority", "authority")
    with node.st.maker() as session:
        custom.seed_multiplayer(session, node.st.ctx)
        session.commit()
    return node


def test_provider_import_reaches_local_feed_and_leaves_signed_feed_untouched(authority):
    a = authority
    before_feed = a.own_feed()
    before_signed = signed_feed(a)
    before_v1 = v1_page(a)
    before_local = local_page(a)

    cid = provider_import(a)

    # The data really is stored, and none of it is redistributable.
    with a.st.maker() as session:
        assert [x.name for x in session.scalars(select(Achievement).where(Achievement.canonical_id == cid))] \
            == [SECRET_NAME]
        assert all(not x.redistributable for x in session.scalars(select(Achievement)))
        assert all(not x.redistributable for x in session.scalars(select(Artwork)))
        assert {v.key: v.value for v in session.scalars(select(CustomValue).where(CustomValue.canonical_id == cid))} \
            == {"crossplay": "yes", "players_max": 4}
        assert all(not o.redistributable for o in session.scalars(select(Observation)))

    # A polling client sees the record at its new version, attributed to the provider.
    page = local_page(a, before_local["head"])
    assert page["feed"] == "local" and page["node_id"] == a.node_id and page["reset"] is False
    assert page["results"] == [{"id": cid, "version": version(a, cid), "merged_into": None, "causes": ["provider"]}]
    assert page["next_cursor"] == page["head"] > before_local["head"] and page["has_more"] is False
    # Observation, artwork, crossplay and achievements in one request share one row (one transaction).
    [(_, _, stored_version, cause, origin_node, origin_seq)] = rows(a, cid)
    assert (stored_version, cause, origin_node, origin_seq) == (version(a, cid), "provider", None, None)
    assert version(a, cid) == 5  # new record (1), rebuilt (2), then artwork, custom values and achievements

    # The signed feed is byte-for-byte unchanged: same rows, same head, nothing provider-sourced.
    assert a.own_feed() == before_feed
    after_signed = signed_feed(a)
    assert after_signed == before_signed
    blob = json.dumps(after_signed)
    assert SECRET_NAME not in blob and SECRET_ART not in blob and PROVIDER not in blob and cid not in blob
    # And /changes v1 (no `feed`) is unchanged apart from its additive hint fields.
    after_v1 = v1_page(a, before_v1["head"])
    assert after_v1["results"] == [] and after_v1["head"] == before_v1["head"]
    assert after_v1["feed"] == "signed" and after_v1["local_feed"] is True

    # A second refresh is a new change with a higher version.
    cursor = page["next_cursor"]
    provider_import(a, provider=FakeProvider(achievement="Renamed by the provider"))
    again = local_page(a, cursor)
    assert [(x["id"], x["causes"]) for x in again["results"]] == [(cid, ["provider"])]
    assert again["results"][0]["version"] > page["results"][0]["version"]
    assert a.own_feed() == before_feed


def test_followers_never_see_provider_imports_or_the_local_log(tmp_path, authority):
    a = authority
    mirror = Node(tmp_path, "mirror", "mirror")
    downstream = Node(tmp_path, "downstream", "local")
    with a.st.maker() as session:  # a curated record that also names the provider's id
        [res] = service.ingest(session, a.st.ctx, batch("manual", [
            {"external_id": "shared", "title": "Shared Game", "provider_ids": {PROVIDER: "88"}}], kind="manual"),
            curator=True)
        session.commit()
    shared = res.canonical_id
    pull(mirror, a)
    mirror_version = version(mirror, shared)
    mirror_rows = rows(mirror)

    # The authority imports from a provider onto a record its followers also hold.
    signed_before = a.own_feed()
    assert provider_import(a, "88", FakeProvider(title="Shared Game")) == shared
    assert any(r[3] == "provider" for r in rows(a, shared))
    with a.st.maker() as session:  # the authority holds the provider data on the shared record
        names = [x.name for x in session.scalars(select(Achievement).where(Achievement.canonical_id == shared))]
        assert names == [SECRET_NAME]

    assert a.own_feed() == signed_before  # nothing new was signed
    assert pull(mirror, a)[0] == 0
    # The mirror's retained copy of the signed feed holds no provider content.
    with mirror.st.maker() as session:
        bodies = [r.body for r in session.scalars(select(ReplicatedChange))]
        assert session.scalars(select(Achievement)).all() == []
        assert session.scalars(select(Artwork)).all() == []
        assert session.scalars(select(CustomValue).where(CustomValue.source == PROVIDER)).all() == []
        assert session.scalars(select(Observation).where(Observation.source == PROVIDER)).all() == []
    text = json.dumps(bodies)
    assert SECRET_NAME not in text and SECRET_ART not in text
    assert all((b.get("observation") or {}).get("source") != PROVIDER and b.get("source") != PROVIDER for b in bodies)
    assert version(mirror, shared) == mirror_version and rows(mirror) == mirror_rows

    # Relay: a downstream node pulling through the mirror gets the same signed entries and no local log.
    pull(downstream, mirror, authority=a)
    assert downstream.retained(a) == mirror.retained(a)
    assert all(r[3] == "replicated" for r in rows(downstream))
    relayed = mirror.client.get("/api/v2/sync/changes", params={"since": 0, "node_id": a.node_id}, headers=H(mirror))
    assert relayed.status_code == 200
    assert SECRET_NAME not in relayed.text and SECRET_ART not in relayed.text

    # The mirror's own local edits and provider imports reach the mirror's clients only.
    cursor = local_page(mirror)["head"]
    local_cid = provider_import(mirror, "99", FakeProvider(title="Mirror Only"))
    assert [x["id"] for x in local_page(mirror, cursor)["results"]] == [local_cid]
    assert mirror.own_feed() == {}  # a mirror signs nothing
    assert pull(downstream, mirror, authority=a)[0] == 0
    with downstream.st.maker() as session:
        assert session.get(Canonical, local_cid) is None


def test_replicated_entries_are_attributed_to_their_origin(tmp_path, authority):
    a = authority
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = a.publish("Portal")
    cursor = local_page(mirror)["head"]
    pull(mirror, a)
    page = local_page(mirror, cursor)
    assert [(x["id"], x["causes"], x["version"]) for x in page["results"]] == [(cid, ["replicated"], version(mirror, cid))]
    obs_seq = next(seq for seq, (kind, *_rest) in sorted(mirror.retained(a).items()) if kind == "obs")
    assert {(r[3], r[4], r[5]) for r in rows(mirror, cid)} == {("replicated", a.node_id, obs_seq)}
    # On the authority the same change was signed into its own feed: cause `feed`.
    assert {r[3] for r in rows(a, cid)} == {"feed"}

    # A replicated merge is a `merge` change that keeps its upstream origin; both ids reach clients.
    other = a.publish("Half-Life")
    with a.st.maker() as session:
        service.merge_canonicals(session, a.st.ctx, other, cid)
        session.commit()
    cursor = local_page(mirror)["head"]
    pull(mirror, a)
    page = local_page(mirror, cursor)
    by_id = {x["id"]: x for x in page["results"]}
    assert set(by_id) == {other, cid}
    assert by_id[other]["merged_into"] == cid and "merge" in by_id[other]["causes"]
    merge_seq = max(seq for seq, (kind, *_rest) in mirror.retained(a).items() if kind == "merge")
    assert (other, "merge", a.node_id, merge_seq) in {(r[1], r[3], r[4], r[5]) for r in rows(mirror)}


def test_merge_and_detach_show_both_ids(authority):
    a = authority
    w = curator(a)
    src, dst = a.publish("Alpha"), a.publish("Beta")
    cursor = local_page(a)["head"]
    assert a.client.post(f"/api/v2/games/{src}/merge", json={"into": dst}, headers=w).status_code == 200
    page = local_page(a, cursor)
    assert {x["id"]: (x["merged_into"], x["causes"]) for x in page["results"]} == {
        src: (dst, ["merge"]), dst: (None, ["merge"])}
    assert {(r[1], r[3]) for r in rows(a) if r[0] > cursor} == {(src, "merge"), (dst, "merge")}

    with a.st.maker() as session:
        obs = session.scalars(select(Observation).where(Observation.canonical_id == dst,
                                                        Observation.external_id == "Alpha")).one()
        oid = obs.id
    cursor = local_page(a)["head"]
    detached = a.client.post(f"/api/v2/observations/{oid}/detach", headers=w)
    assert detached.status_code == 200, detached.text
    new = detached.json()["id"]
    page = local_page(a, cursor)
    assert {x["id"]: x["causes"] for x in page["results"]} == {dst: ["detach"], new: ["detach"]}


def test_local_only_writes_are_local_changes(tmp_path):
    mirror = Node(tmp_path, "mirror", "mirror")
    with mirror.st.maker() as session:
        [res] = service.ingest(session, mirror.st.ctx, batch("manual", [{"external_id": "x", "title": "Local"}],
                                                             kind="manual"), curator=True)
        session.commit()
    assert {r[3] for r in rows(mirror, res.canonical_id)} == {"local"}
    cursor = local_page(mirror)["head"]
    w = curator(mirror)
    r = mirror.client.put(f"/api/v2/games/{res.canonical_id}/achievements", json={"items": [{"name": "Mine"}]},
                          headers=w)
    assert r.status_code == 200, r.text
    page = local_page(mirror, cursor)
    assert page["results"] == [{"id": res.canonical_id, "version": version(mirror, res.canonical_id),
                                "merged_into": None, "causes": ["local"]}]


def test_polling_client_pages_through_every_change(authority):
    a = authority
    ids = [a.publish(t) for t in ("Hades", "Outer Wilds", "Tunic", "Hollow Knight", "Braid")]
    cid = provider_import(a)
    cursor, seen, pages = 0, [], 0
    while True:
        page = local_page(a, cursor, limit=1)
        assert page["reset"] is False
        seen += [x["id"] for x in page["results"]]
        pages += 1
        if not page["has_more"]:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
        assert pages < 50
    assert set(seen) == {*ids, cid}
    assert local_page(a, page["next_cursor"])["results"] == []


def test_parameters_are_validated(authority):
    a = authority
    r = a.client.get("/api/v2/changes", params={"feed": "local", "node_id": a.node_id}, headers=H(a))
    assert r.status_code == 400
    assert a.client.get("/api/v2/changes", params={"feed": "everything"}, headers=H(a)).status_code == 422
    assert a.client.get("/api/v2/changes", params={"feed": "local", "limit": 501}, headers=H(a)).status_code == 422
    assert a.client.get("/api/v2/changes", params={"feed": "local"}).status_code == 401
    signed = a.client.get("/api/v2/changes", params={"feed": "signed"}, headers=H(a)).json()
    assert signed["feed"] == "signed" and signed == v1_page(a)


def _age(node, days):
    with node.st.maker() as session:
        for row in session.scalars(select(ClientChange)):
            row.created_at = datetime.now(UTC) - timedelta(days=days)
        session.commit()


def test_retention_keeps_the_newest_row_per_record_and_stale_cursors_reset(authority):
    a = authority
    one, two = a.publish("One"), a.publish("Two")
    w = curator(a)
    for name in ("first", "second"):
        assert a.client.put(f"/api/v2/games/{one}/achievements", json={"items": [{"name": name}]},
                            headers=w).status_code == 200
    stale = 0
    before = rows(a)
    assert [r[1] for r in before] == [one, two, one, one]
    _age(a, 91)
    assert a.client.put(f"/api/v2/games/{two}/achievements", json={"items": [{"name": "x"}]},
                        headers=w).status_code == 200  # a recent row: never pruned

    with a.st.maker() as session:
        assert client_changes.maybe_prune(session, a.st.ctx.settings.client_changes_retain_days) == 3
        session.commit()
        assert client_changes.maybe_prune(session, 90) is None  # daily: not due again yet
        assert session.get(NodeState, client_changes.PRUNED_AT) is not None
    kept = rows(a)
    # Every old row with a newer row of the same record is gone; One's newest row is kept although it is old.
    assert [(r[0], r[1]) for r in kept] == [(before[3][0], one), (kept[-1][0], two)] and kept[-1][0] > before[3][0]

    page = local_page(a, stale)
    assert page["reset"] is True and page["results"] == [] and page["has_more"] is False
    assert page["next_cursor"] == page["head"] == kept[-1][0]
    # A cursor at the oldest kept row - 1 continues normally and still sees every record changed since.
    ok = local_page(a, kept[0][0] - 1)
    assert ok["reset"] is False and {x["id"] for x in ok["results"]} == {one, two}
    # A cursor above the head (the log was recreated by a rollback or restore) also resets.
    ahead = local_page(a, page["head"] + 5)
    assert ahead["reset"] is True and ahead["next_cursor"] == page["head"]


def test_prune_is_due_again_after_a_day(session, ctx):
    [res] = service.ingest(session, ctx, batch("manual", [{"external_id": "a", "title": "A"}], kind="manual"),
                           curator=True)
    service.touch_version(session, res.canonical_id)
    session.commit()
    t0 = datetime.now(UTC) + timedelta(days=200)
    assert [r.cause for r in session.scalars(select(ClientChange))] == ["feed", "local"]
    assert client_changes.maybe_prune(session, 90, at=t0) == 1  # the older duplicate goes
    assert client_changes.maybe_prune(session, 90, at=t0 + timedelta(hours=23)) is None
    assert client_changes.maybe_prune(session, 90, at=t0 + timedelta(hours=25)) == 0
    assert [r.cause for r in session.scalars(select(ClientChange))] == ["local"]


def test_bump_version_is_the_single_writer_and_rolls_back_with_its_transaction(session, ctx):
    [res] = service.ingest(session, ctx, batch("manual", [{"external_id": "a", "title": "A"}], kind="manual"),
                           curator=True)
    session.commit()
    cid = res.canonical_id
    start = len(session.scalars(select(ClientChange)).all())
    try:
        with session.begin_nested():
            service.bump_version(session, cid, "local")
            raise RuntimeError("roll this back")
    except RuntimeError:
        pass
    session.commit()
    assert len(session.scalars(select(ClientChange)).all()) == start  # no orphan row for a rolled-back bump
    row = service.bump_version(session, cid, "provider")
    session.commit()
    last = session.scalars(select(ClientChange).order_by(ClientChange.seq.desc())).first()
    assert (last.canonical_id, last.version, last.cause) == (cid, row.version, "provider")
    with pytest.raises(ValueError):
        service.bump_version(session, cid, "gossip")
    session.rollback()
    with pytest.raises(ValueError), client_changes.cause(session, "gossip"):
        pass
