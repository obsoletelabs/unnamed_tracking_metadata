"""CONTRACT-C13 §4 (A19): an authority introduces every record before an entry names it ("no orphans").

The live failure: a record built from a provider fetch only (its observation is not redistributable, so it is
never signed) gets curated artwork and achievements, which are signed and name a record no follower can know.
Every test asserts the authority's persistent state: its `change_log`, `feed_introduced` and observations."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from atlas import cli, facets, feed, feed_schema, service
from atlas.models import Canonical, ChangeLog, Crosswalk, FeedIntroduced, NodeState, Observation
from atlas.service import AtlasError, Ctx

from .conftest import batch, build_settings

ART = [{"kind": "key_art", "url": "https://img.example.org/key.png"}]


def at(ctx: Ctx, **settings) -> Ctx:
    return Ctx(settings=build_settings(**settings), identity=ctx.identity)


def provider_only(session, ctx, title="Subnautica 2", steam="1962700"):
    """A record whose only observation came from a provider fetch: stored here, never signed."""
    [result] = service.ingest(session, ctx, batch("steam", [{"external_id": steam, "title": title,
                                                              "provider_ids": {"steam": steam}}]), system=True)
    session.flush()
    obs = session.scalars(select(Observation).where(Observation.canonical_id == result.canonical_id,
                                                    Observation.source == "steam")).one()
    assert obs.redistributable is False
    return result.canonical_id


def log(session):
    return [(r.seq, r.kind, r.body) for r in session.scalars(select(ChangeLog).order_by(ChangeLog.seq))]


def introduced(session):
    return {r.canonical_id: (r.seq, r.via, r.because) for r in session.scalars(select(FeedIntroduced))}


def test_below_1_5_a_sidecar_is_signed_as_before_and_the_record_is_marked_legacy(session, ctx):
    """T-A1: emit 1.0, bridge off (the safe default). Nothing can introduce the record; the artwork is signed
    unchanged (what older releases did) and the record is remembered as `legacy_unintroduced`."""
    cid = provider_only(session, ctx)
    facets.set_artwork(session, ctx, cid, "curator", ART, redistributable=True)
    session.commit()
    assert [(seq, kind) for seq, kind, _ in log(session)] == [(1, "artwork")]
    assert introduced(session) == {cid: (None, "legacy", "sidecar")}
    assert feed.records_summary(session) == {"live": 1, "introduced": 0, "not_federated": 1}
    assert feed.introduced_through(session) is None


def test_at_1_5_a_record_entry_precedes_the_first_sidecar(session, ctx):
    """T-A2: at emit 1.5 the artwork is preceded, in the same transaction, by a `record` carrying the record's id,
    type, display title and store ids, and nothing else. The second sidecar signs no second record."""
    c15 = at(ctx, emit_protocol="1.5")
    cid = provider_only(session, c15)
    facets.set_artwork(session, c15, cid, "curator", ART, redistributable=True)
    facets.set_achievements(session, c15, cid, "curator", [], redistributable=True)
    session.commit()
    entries = log(session)
    assert entries[0][:2] == (1, "record") and entries[1][:2] == (2, "artwork")
    assert entries[0][2] == {"canonical_id": cid, "media_type": "game", "title": "Subnautica 2",
                             "provider_ids": {"steam": "1962700"}, "because": "sidecar"}
    assert [kind for _, kind, _ in entries].count("record") == 1
    assert introduced(session) == {cid: (1, "record", "sidecar")}
    # the authority keeps the identity observation its followers will store (same id, same payload)
    obs = session.get(Observation, feed.record_obs_id(ctx.identity.node_id, cid))
    assert (obs.source, obs.source_scope, obs.canonical_id, obs.redistributable) == (
        "atlas-record", ctx.identity.node_id, cid, True)
    assert obs.payload == {"title": "Subnautica 2", "provider_ids": {"steam": "1962700"}}
    # it ranks below every provider: the view still comes from the Steam observation
    assert c15.settings.trust("atlas-record") < c15.settings.trust("steam")
    assert feed.records_summary(session) == {"live": 1, "introduced": 1, "not_federated": 0}


def test_the_bridge_introduces_with_an_observation_old_followers_apply(session, ctx):
    """T-A3: bridge on with followers at 1.0: the introduction is a plain `obs` of source `atlas-record` with the
    deterministic id, which every release since 1.0 applies."""
    bridged = at(ctx, federation_bridge_record_obs=True)
    cid = provider_only(session, bridged)
    facets.set_artwork(session, bridged, cid, "curator", ART, redistributable=True)
    session.commit()
    entries = log(session)
    assert [kind for _, kind, _ in entries] == ["obs", "artwork"]
    body = entries[0][2]
    assert body["canonical_id"] == cid and body["observation"]["source"] == "atlas-record"
    assert body["observation"]["id"] == feed.record_obs_id(ctx.identity.node_id, cid)
    assert len(body["observation"]["id"]) == 26
    assert feed_schema.classify("obs", body).action == feed_schema.APPLY  # a 1.0 shape
    assert introduced(session)[cid][:2] == (1, "bridge")
    # once every follower speaks 1.5 the same settings emit `record` instead (C13 §4.1, first matching row)
    assert feed.introduction_mode(at(ctx, federation_bridge_record_obs=True, min_follower_protocol="1.5").settings) \
        == "record"


def test_record_obs_id_is_deterministic_and_keyed_on_the_signer():
    a = feed.record_obs_id("356cae19243bb510", "atlas:01aaaaaaaaaaaaaaaaaaaaaaaa")
    assert a == feed.record_obs_id("356cae19243bb510", "atlas:01aaaaaaaaaaaaaaaaaaaaaaaa")
    assert a != feed.record_obs_id("0000000000000000", "atlas:01aaaaaaaaaaaaaaaaaaaaaaaa")
    assert len(a) == 26 and a.isalnum()


def test_feed_introduce_missing_publishes_legacy_records_resumably(session, ctx, monkeypatch, capsys, maker):
    """T-A4: `atlas feed introduce --missing` is refused at emit 1.0 without the bridge; at 1.5 it signs a `record`
    for every legacy record, in batches, and then sets `introduced_through` to the feed head."""
    cids = [provider_only(session, ctx, f"Game {n}", str(1000 + n)) for n in range(3)]
    for cid in cids:
        facets.set_artwork(session, ctx, cid, "curator", ART, redistributable=True)
    session.commit()
    with pytest.raises(feed.IntroduceRefused):
        feed.introduce_batch(session, ctx, "missing")
    c15 = at(ctx, emit_protocol="1.5")
    first = feed.introduce_batch(session, c15, "missing", limit=2)
    session.commit()
    assert (first["introduced"], first["left"], first["finished"]) == (2, 1, False)
    assert session.get(NodeState, "introduce_cursor:missing") is not None
    second = feed.introduce_batch(session, c15, "missing", limit=2)
    session.commit()
    assert (second["introduced"], second["left"], second["finished"]) == (1, 0, True)
    kinds = [kind for _, kind, _ in log(session)]
    assert kinds == ["artwork"] * 3 + ["record"] * 3
    assert {r[1] for r in introduced(session).values()} == {"record"}
    assert {b["because"] for _, k, b in log(session) if k == "record"} == {"backfill"}
    assert feed.introduced_through(session) == 6
    assert session.get(NodeState, "introduce_cursor:missing") is None
    again = feed.introduce_batch(session, c15, "missing")
    assert again["introduced"] == 0 and len(log(session)) == 6  # idempotent


def test_feed_introduce_cli(tmp_path, monkeypatch, capsys):
    """The CLI on a real database: dry run, refusal (exit 2), then a run."""
    url = f"sqlite:///{tmp_path}/a.db"
    monkeypatch.setenv("ATLAS_DATABASE_URL", url)
    monkeypatch.setenv("ATLAS_ROLE", "authority")
    monkeypatch.setenv("ATLAS_ENVIRONMENT", "dev")
    from atlas import db
    from atlas.settings import get_settings

    get_settings.cache_clear()
    db.upgrade(url)
    settings, maker = cli._maker()
    from atlas.signing import load_identity

    with maker() as s:
        c = Ctx(settings, load_identity(s, settings))
        cid = provider_only(s, c)
        facets.set_artwork(s, c, cid, "curator", ART, redistributable=True)
        s.commit()
    assert cli.main(["feed", "introduce", "--missing", "--dry-run"]) == 0
    assert '"would_introduce": 1' in capsys.readouterr().out
    assert cli.main(["feed", "introduce", "--missing"]) == 2
    assert "refused" in capsys.readouterr().err
    monkeypatch.setenv("ATLAS_EMIT_PROTOCOL", "1.5")
    get_settings.cache_clear()
    assert cli.main(["feed", "introduce", "--missing"]) == 0
    assert '"finished": true' in capsys.readouterr().out
    with maker() as s:
        assert [kind for _, kind, _ in log(s)] == ["artwork", "record"]
        assert introduced(s)[cid][1] == "record"
    get_settings.cache_clear()


def test_a_merge_introduces_both_records_while_they_still_have_their_identity(session, ctx):
    """T-A5: both sides of a merge are introduced before the merge entry, with their own titles."""
    c15 = at(ctx, emit_protocol="1.5")
    src, dst = provider_only(session, c15, "Old Name", "1"), provider_only(session, c15, "New Name", "2")
    service.merge_canonicals(session, c15, src, dst, actor="t")
    session.commit()
    entries = log(session)
    assert [kind for _, kind, _ in entries] == ["record", "record", "merge"]
    assert [(b["canonical_id"], b["title"], b["because"]) for _, k, b in entries if k == "record"] == [
        (src, "Old Name", "merge"), (dst, "New Name", "merge")]


def test_a_detach_of_an_unsigned_observation_introduces_the_new_record_after_it(session, ctx):
    """T-A6: a follower cannot create the detached record from an observation it never had, so the authority
    signs a `record` for it right after the detach. A detach of a signed observation introduces it itself."""
    c15 = at(ctx, emit_protocol="1.5")
    cid = provider_only(session, c15, "Pair", "10")
    service.ingest(session, c15, batch("gog", [{"external_id": "g1", "title": "Pair",
                                                 "provider_ids": {"steam": "10"}}]), system=True)
    moved = session.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                      Observation.source == "gog")).one()
    new = service.detach_observation(session, c15, moved.id, actor="t")
    session.commit()
    entries = log(session)
    assert [kind for _, kind, _ in entries] == ["record", "detach", "record"]
    assert entries[0][2]["canonical_id"] == cid
    assert entries[2][2]["canonical_id"] == new.id and entries[2][2]["because"] == "detach"
    assert introduced(session)[new.id][:2] == (3, "record")

    signed = service.ingest(session, c15, batch("manual", [{"external_id": "m1", "title": "Signed"}],
                                                 kind="manual"), curator=True)[0].canonical_id
    service.ingest(session, c15, batch("manual", [{"external_id": "m2", "title": "Signed"}], kind="manual"),
                   curator=True, force_canonical=signed)
    obs = session.scalars(select(Observation).where(Observation.external_id == "m2")).one()
    second = service.detach_observation(session, c15, obs.id, actor="t")
    session.commit()
    assert introduced(session)[second.id][1] == "detach"
    assert log(session)[-1][1] == "detach"


def test_the_gate_fails_closed_for_a_record_that_does_not_exist(session, ctx):
    """T-A8: an entry naming a record this node does not hold is never signed."""
    c15 = at(ctx, emit_protocol="1.5")
    with pytest.raises(feed.UnintroducedCanonical) as err:
        feed.emit(session, c15, "lock", {"canonical_id": "atlas:01zzzzzzzzzzzzzzzzzzzzzzzz", "field": "title",
                                         "value": "x"})
    assert isinstance(err.value, feed_schema.EmitGateError)
    assert log(session) == []


def test_atlas_record_is_reserved_for_record_identities(session, ctx):
    """T-A9: no connector, curator or system, writes observations of source `atlas-record`."""
    with pytest.raises(AtlasError, match="reserved"):
        service.ingest(session, ctx, batch("atlas-record", [{"external_id": "x", "title": "Forged"}]), system=True)


def test_introduce_all_publishes_every_record_at_ingest(session, ctx):
    """T-A10 (H55, off by default): with `federation_introduce=all`, a provider-only record is introduced when it is
    created; with the default `referenced` it is introduced only when something names it."""
    cid = provider_only(session, at(ctx, emit_protocol="1.5"), "Quiet", "20")
    assert log(session) == []
    loud = provider_only(session, at(ctx, emit_protocol="1.5", federation_introduce="all"), "Loud", "21")
    session.commit()
    assert [(kind, b["canonical_id"], b["because"]) for _, kind, b in log(session)] == [("record", loud, "publish")]
    assert cid not in introduced(session)


def test_record_provider_ids_never_carry_reserved_namespaces(session, ctx):
    """T-A11: only valid store ids enter a `record`; never `atlas`, `curator` or another reserved namespace, and
    the snapshot fields setting (H53) can leave out the title and the ids."""
    assert feed.strip_ids({"steam": "1", "atlas": "atlas:01x", "curator": "c", "gog": "g", "bad key!": "1",
                           "manual": "m"}) == {"gog": "g", "steam": "1"}
    assert len(feed.strip_ids({f"p{n:02d}": str(n) for n in range(40)})) == 32
    c15 = at(ctx, emit_protocol="1.5", federation_snapshot_fields=[])
    cid = provider_only(session, c15, "Bare", "30")
    session.add(Crosswalk(namespace="curator", external_id="c-30", canonical_id=cid))
    facets.set_artwork(session, c15, cid, "curator", ART, redistributable=True)
    session.commit()
    body = log(session)[0][2]
    assert body == {"canonical_id": cid, "media_type": "game", "title": "", "provider_ids": {}, "because": "sidecar"}
    row = session.get(Canonical, cid)
    full = feed.record_body(session, build_settings(), row, "sidecar")
    assert full["provider_ids"] == {"steam": "30"}  # the curator crosswalk is reserved and stays out


def test_raising_the_emit_protocol_into_1_5_upgrades_legacy_marks(session, ctx):
    """A record marked legacy is introduced by the next entry that names it once 1.5 is available."""
    cid = provider_only(session, ctx)
    facets.set_artwork(session, ctx, cid, "curator", ART, redistributable=True)
    session.commit()
    c15 = at(ctx, emit_protocol="1.5")
    facets.set_artwork(session, c15, cid, "curator", ART + [{"kind": "logo", "url": "https://img.example.org/l.png"}],
                       redistributable=True)
    session.commit()
    assert [kind for _, kind, _ in log(session)] == ["artwork", "record", "artwork"]
    assert introduced(session)[cid][:2] == (2, "record")


def test_promotion_rebuilds_introductions_from_the_log(session, ctx):
    c15 = at(ctx, emit_protocol="1.5")
    cid = provider_only(session, c15)
    facets.set_artwork(session, c15, cid, "curator", ART, redistributable=True)
    session.commit()
    session.execute(FeedIntroduced.__table__.delete())
    assert feed.rebuild_introduced(session) == 1
    assert introduced(session) == {cid: (1, "record", None)}


def test_artwork_clash_emits_what_was_stored(session, ctx):
    """T-A6 (R4e): the curator's list names an image the record's Steam fetch already holds (same kind and address).
    It stays Steam's and is not stored for the curator, so the signed entry leaves it out too: a follower stores
    exactly what this node stores for the curator."""
    from atlas.models import Artwork

    c15 = at(ctx, emit_protocol="1.5")
    cid = provider_only(session, c15)
    shared = {"kind": "key_art", "url": "https://cdn.steam.example/1962700/header.jpg"}
    facets.set_artwork(session, c15, cid, "steam", [shared])
    skipped: list[dict] = []
    shot = {"kind": "screenshot", "url": "https://img.example.org/s.png"}
    facets.set_artwork(session, c15, cid, "curator", [shared, shot], redistributable=True, skipped=skipped)
    session.commit()
    stored = sorted((a.source, a.kind, a.url) for a in session.scalars(select(Artwork).where(
        Artwork.canonical_id == cid)))
    assert stored == [("curator", "screenshot", "https://img.example.org/s.png"), ("steam", "key_art", shared["url"])]
    assert skipped == [{**shared, "held_by": "steam", "reason": "held_by_another_source"}]
    [(_, kind, body)] = [e for e in log(session) if e[1] == "artwork"]
    assert [(i["kind"], i["url"]) for i in body["items"]] == [("screenshot", "https://img.example.org/s.png")]


def test_bridge_chosen_while_min_follower_below_1_5(session, ctx):
    """T-A9: emit 1.5 with `min_follower_protocol` 1.1 and the bridge on: a follower at 1.1 may still be served, and
    it would park a `record`, so the introduction is the bridge `obs` (a 1.0 shape), not `record`. Without the
    bridge, the same settings introduce with `record`."""
    mixed = at(ctx, emit_protocol="1.5", min_follower_protocol="1.1", federation_bridge_record_obs=True)
    assert feed.introduction_mode(mixed.settings) == "bridge"
    cid = provider_only(session, mixed)
    facets.set_artwork(session, mixed, cid, "curator", ART, redistributable=True)
    session.commit()
    entries = log(session)
    assert [kind for _, kind, _ in entries] == ["obs", "artwork"]
    assert entries[0][2]["observation"]["source"] == "atlas-record"
    for _, kind, body in entries:  # nothing a 1.1 follower would park
        assert feed_schema.parse_protocol(feed_schema.KIND_SINCE.get(kind, "1.0")) <= feed_schema.parse_protocol("1.1")
        assert feed_schema.classify(kind, body).action == feed_schema.APPLY
    assert introduced(session)[cid][1] == "bridge"
    assert feed.introduction_mode(at(ctx, emit_protocol="1.5", min_follower_protocol="1.1").settings) == "record"
