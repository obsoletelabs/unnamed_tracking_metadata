"""Relationships between works (CONTRACTS-v1 C6, issue #86, protocol 1.4).

Covers the rules (self, cycles direct and 3-hop, one parent per family, membership targets, bounds), merges
(edges move, self/duplicate/conflict/cycle edges end, nothing points at a merged record), splits, redirects,
the emission gate, the API and the editor, the editions view, and federation: a 1.4 mirror converges, a
replicated cycle is quarantined without blocking, and 1.1 / 1.3 followers park (not drop) `relation` entries
and converge after upgrading; a 1.0 follower's silent drop is recovered by rescan + historical replay.
Every scenario asserts the persistent rows in the database, not only return values.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import custom, feed, feed_schema, main, relations, service, sync, vocab
from atlas.ids import canonical_id
from atlas.merge import Obs, merge
from atlas.models import Canonical, ChangeLog, Relation, SyncFailure
from atlas.service import AtlasError, Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings
from .test_sync_compat import Node, observation, pull
from .test_web import _wait_for_startup_replay, signup, token

# ---- helpers --------------------------------------------------------------------------------------------


def record(session, ctx, title, media_type="game"):
    custom.seed(session)  # the built-in types, as the application seeds them at start
    [r] = service.ingest(session, ctx, batch("manual", [{"external_id": title, "title": title}], kind="manual"),
                         curator=True, media_type=None if media_type == "game" else media_type)
    session.commit()
    return r.canonical_id


def bare(session, title, media_type="game"):
    """A record inserted directly (fast; for graphs with hundreds of records)."""
    custom.seed(session)
    cid = canonical_id()
    session.add(Canonical(id=cid, media_type=media_type, norm_title=title.lower(), view={"title": title}, explain={}))
    return cid


def rows(session, *, active=True):
    q = select(Relation)
    if active:
        q = q.where(Relation.ended_at.is_(None))
    return sorted((r.from_id, r.to_id, r.type) for r in session.scalars(q))


def ended(session):
    return {(r.from_id, r.to_id, r.type): r.end_reason
            for r in session.scalars(select(Relation).where(Relation.ended_at.is_not(None)))}


def version(session, cid):
    session.expire_all()
    return session.get(Canonical, cid).version


@pytest.fixture
def emitting(session):
    """An authority context that publishes relations (emit protocol 1.4)."""
    identity = load_identity(session)
    session.commit()
    return Ctx(settings=build_settings(emit_protocol="1.4"), identity=identity)


# ---- vocabulary and feed registry -----------------------------------------------------------------------

def test_relation_kind_is_registered_with_closed_vocabularies():
    spec = feed_schema.FEED_KINDS["relation"]
    assert (spec.since, spec.emit, spec.apply) == ("1.4", 1, 1)
    assert spec.fields == {"op", "from", "to", "type", "source"}
    assert feed_schema.PROTOCOL == "1.4" and feed_schema.RESERVED_KINDS == {"price": "1.5"}
    assert feed_schema.vocabularies()["relation_type"] == set(vocab.RELATION_TYPES)
    assert feed_schema.vocabularies()["relation_op"] == {"add", "end"}
    assert relations.TYPES == vocab.RELATION_TYPES
    assert set(relations.HIERARCHICAL) | set(relations.MEMBERSHIP) == set(vocab.RELATION_TYPES)
    assert "same_game" not in vocab.RELATION_TYPES and "custom" not in vocab.RELATION_TYPES
    assert "related_to" not in vocab.RELATION_TYPES and "edition_of" not in vocab.RELATION_TYPES  # C6.1, H11
    with open(vocab.FILE, encoding="utf-8") as fh:
        published = json.load(fh)
    assert published["relation_type"] == list(vocab.RELATION_TYPES) and "relation" in published["feed_kinds"]


ADD = {"op": "add", "from": "atlas:" + "a" * 26, "to": "atlas:" + "b" * 26, "type": "dlc_of", "source": "curator"}


@pytest.mark.parametrize(("body", "action", "reason"), [
    (ADD, "apply", ""),
    ({"op": "end", "from": ADD["from"], "to": ADD["to"], "type": "dlc_of"}, "apply", ""),
    ({**ADD, "type": "sequel_of"}, "parked", "unsupported_value"),  # a type a later release may add
    ({**ADD, "op": "replace"}, "parked", "unsupported_value"),
    ({**ADD, "weight": 1}, "parked", "unsupported_field"),
    ({**ADD, "_v": 2}, "parked", "unsupported_version"),
    ({**ADD, "_requires": ["relation-v2"]}, "parked", "unsupported_capability"),
    ({**ADD, "type": 7}, "parked", "unsupported_value"),
])
def test_classify_relation_entries(body, action, reason):
    outcome = feed_schema.classify("relation", body)
    assert (outcome.action, outcome.reason) == (action, reason)


def test_emission_gate_refuses_relation_below_1_4_and_signs_it_unchanged_at_1_4():
    with pytest.raises(feed_schema.EmitGateError, match="1.4"):
        feed_schema.check_emit("relation", ADD, "1.3")
    assert feed_schema.check_emit("relation", ADD, "1.4") is ADD  # version-1 body, no envelope added


# ---- the rules ------------------------------------------------------------------------------------------

def test_add_and_end_persist_rows_and_bump_both_versions(session, ctx):
    game, dlc = record(session, ctx, "Base Game"), record(session, ctx, "Base Game Extra")
    before = (version(session, game), version(session, dlc))
    row, created = relations.add(session, ctx, dlc, game, "dlc_of", actor="cur")
    session.commit()
    assert created and rows(session) == [(dlc, game, "dlc_of")]
    stored = session.get(Relation, row.id)
    assert (stored.source, stored.created_by, stored.redistributable, stored.feed_seq) == ("curator", "cur", True, None)
    assert (version(session, game), version(session, dlc)) == (before[0] + 1, before[1] + 1)
    again, created = relations.add(session, ctx, dlc, game, "dlc_of")  # idempotent
    assert not created and again.id == row.id
    assert relations.end(session, ctx, dlc, game, "dlc_of", actor="cur") is not None
    session.commit()
    assert rows(session) == [] and ended(session) == {(dlc, game, "dlc_of"): "removed"}
    assert relations.end(session, ctx, dlc, game, "dlc_of") is None  # nothing active: a no-op
    relations.add(session, ctx, dlc, game, "dlc_of")  # can be added again; the ended row stays as history
    session.commit()
    assert rows(session) == [(dlc, game, "dlc_of")] and len(rows(session, active=False)) == 2


def test_self_unknown_type_and_unknown_record_are_refused(session, ctx):
    game = record(session, ctx, "Lonely")
    with pytest.raises(AtlasError, match="itself") as exc:
        relations.add(session, ctx, game, game, "port_of")
    assert exc.value.code == "invalid"
    with pytest.raises(AtlasError, match="unknown relation type"):
        relations.add(session, ctx, game, record(session, ctx, "Other"), "edition_of")
    with pytest.raises(AtlasError) as exc:
        relations.add(session, ctx, game, "atlas:" + "z" * 26, "port_of")
    assert exc.value.code == "not_found"
    session.rollback()
    assert rows(session, active=False) == []


def test_membership_must_point_at_a_series_or_collection(session, ctx):
    game, other = record(session, ctx, "Saga One"), record(session, ctx, "Saga Two")
    series = record(session, ctx, "Saga", "series")
    bundle = record(session, ctx, "Saga Bundle", "collection")
    with pytest.raises(AtlasError, match="'series'"):
        relations.add(session, ctx, game, other, "series_member")
    with pytest.raises(AtlasError, match="'collection'"):
        relations.add(session, ctx, game, series, "collection_member")
    with pytest.raises(AtlasError, match="cannot point at a series"):
        relations.add(session, ctx, game, series, "dlc_of")
    relations.add(session, ctx, game, series, "series_member")
    relations.add(session, ctx, game, bundle, "collection_member")
    relations.add(session, ctx, other, bundle, "collection_member")
    session.commit()
    assert sorted(rows(session)) == sorted([(game, series, "series_member"), (game, bundle, "collection_member"),
                                            (other, bundle, "collection_member")])


def test_one_parent_per_family(session, ctx):
    a, b, c = (record(session, ctx, t) for t in ("Root A", "Root B", "Root C"))
    x = record(session, ctx, "Child X")
    relations.add(session, ctx, x, a, "dlc_of")
    for kind in ("dlc_of", "expansion_of", "standalone_expansion_of"):  # the addon family
        with pytest.raises(AtlasError, match="at most one") as exc:
            relations.add(session, ctx, x, b, kind)
        assert exc.value.code == "conflict"
    relations.add(session, ctx, x, b, "port_of")  # another family: allowed
    with pytest.raises(AtlasError, match="at most one"):
        relations.add(session, ctx, x, c, "port_of")
    relations.add(session, ctx, x, a, "remaster_of")  # same pair, another family
    session.commit()
    assert sorted(rows(session)) == sorted([(x, a, "dlc_of"), (x, b, "port_of"), (x, a, "remaster_of")])


def test_cycles_are_refused_direct_and_three_hop(session, ctx):
    a, b, c, d = (record(session, ctx, t) for t in ("Cy A", "Cy B", "Cy C", "Cy D"))
    relations.add(session, ctx, a, b, "dlc_of")
    with pytest.raises(AtlasError, match="cycle") as exc:
        relations.add(session, ctx, b, a, "mod_of")  # direct, across hierarchical types
    assert exc.value.code == "conflict"
    relations.add(session, ctx, b, c, "port_of")
    relations.add(session, ctx, c, d, "remake_of")
    with pytest.raises(AtlasError, match="cycle"):
        relations.add(session, ctx, d, a, "version_of")  # a -> b -> c -> d -> a
    session.commit()
    assert rows(session) == sorted([(a, b, "dlc_of"), (b, c, "port_of"), (c, d, "remake_of")])


def test_membership_cycles_are_refused_and_separate_from_hierarchy(session, ctx):
    s1, s2, s3 = (record(session, ctx, t, "series") for t in ("Ser 1", "Ser 2", "Ser 3"))
    relations.add(session, ctx, s1, s2, "series_member")
    relations.add(session, ctx, s2, s3, "series_member")
    with pytest.raises(AtlasError, match="cycle"):
        relations.add(session, ctx, s3, s1, "series_member")
    session.commit()
    assert len(rows(session)) == 2


def test_cycle_search_fails_closed_beyond_its_depth_bound(session, ctx):
    chain = [bare(session, f"Chain {i:02d}") for i in range(relations.MAX_DEPTH + 2)]
    session.commit()
    for child, parent in zip(chain, chain[1:], strict=False):
        session.add(Relation(from_id=child, to_id=parent, type="mod_of", source="curator"))
    session.commit()
    newcomer = bare(session, "Newcomer")
    session.commit()
    with pytest.raises(AtlasError, match="too deep"):
        relations.add(session, ctx, newcomer, chain[0], "mod_of")
    relations.add(session, ctx, newcomer, chain[-1], "mod_of")  # near the top: within the bound
    session.commit()
    assert (newcomer, chain[-1], "mod_of") in rows(session)


def test_outgoing_relations_are_capped(session, ctx):
    game = bare(session, "Collector")
    bundles = [bare(session, f"Bundle {i:03d}", "collection") for i in range(relations.MAX_OUTGOING + 1)]
    session.commit()
    for b in bundles[:-1]:
        session.add(Relation(from_id=game, to_id=b, type="collection_member", source="curator"))
    session.commit()
    with pytest.raises(AtlasError, match="already has 200 relations"):
        relations.add(session, ctx, game, bundles[-1], "collection_member")


def test_a_bundle_with_many_members_is_bounded_and_ordered(session, ctx):
    bundle = bare(session, "Mega Bundle", "collection")
    titles = {bare(session, f"Member {i:04d}"): f"Member {i:04d}" for i in range(450)}
    members = sorted(titles)
    session.commit()
    for m in members:
        relations.add(session, ctx, m, bundle, "collection_member")
    session.commit()
    block = relations.of(session, bundle)
    assert block["counts"]["members"] == 450 and block["truncated"] is True
    assert [m["id"] for m in block["members"]] == members[:200]
    assert block["members"][0] == {"id": members[0], "type": "collection_member", "title": titles[members[0]],
                                   "media_type": "game"}
    assert relations.of(session, bundle) == block  # deterministic
    fetched = []  # rows the database hands back while expanding: bounded by pages, not by the bundle's size

    def count_rows(conn, cursor, statement, parameters, context, executemany):
        if "FROM relation" in statement:
            fetched.append(statement)

    from sqlalchemy import event
    engine = session.get_bind()
    event.listen(engine, "after_cursor_execute", count_rows)
    try:
        graph = relations.expand(session, bundle, depth=4)
    finally:
        event.remove(engine, "after_cursor_execute", count_rows)
    assert len(graph["nodes"]) == relations.MAX_EXPAND_NODES and graph["truncated"] is True
    assert all("LIMIT" in q for q in fetched if "ORDER BY" in q)  # the walk reads pages
    assert sum("ORDER BY" in q for q in fetched) <= relations.MAX_EXPAND_NODES // relations._PAGE + 2
    assert graph["nodes"][0]["id"] == bundle and len(graph["edges"]) == relations.MAX_EXPAND_NODES - 1
    member = relations.of(session, members[0])
    assert member["members_of"] == [{"id": bundle, "type": "collection_member", "title": "Mega Bundle",
                                     "media_type": "collection"}]


def test_expand_follows_both_directions_with_a_depth_bound(session, ctx):
    game, dlc, port, remake = (record(session, ctx, t) for t in ("Ex Game", "Ex DLC", "Ex Port", "Ex Remake"))
    series = record(session, ctx, "Ex Series", "series")
    relations.add(session, ctx, dlc, game, "dlc_of")
    relations.add(session, ctx, port, game, "port_of")
    relations.add(session, ctx, remake, game, "remake_of")
    relations.add(session, ctx, game, series, "series_member")
    session.commit()
    one = relations.expand(session, dlc, depth=1)
    assert [n["id"] for n in one["nodes"]] == [dlc, game] and one["truncated"] is True
    two = relations.expand(session, dlc, depth=2)
    assert {n["id"] for n in two["nodes"]} == {dlc, game, port, remake, series} and two["truncated"] is False
    assert [e["type"] for e in two["edges"]] == ["remake_of", "port_of", "dlc_of", "series_member"]


# ---- merges, redirects, splits ---------------------------------------------------------------------------

def no_active_relation_names_a_merged_record(session):
    merged = set(session.scalars(select(Canonical.id).where(Canonical.merged_into.is_not(None))))
    return not any(f in merged or t in merged for f, t, _ in rows(session))


def test_merge_moves_relations_and_bumps_the_neighbours(session, ctx):
    game, dup = record(session, ctx, "Merge Game"), record(session, ctx, "Merge Game Duplicate")
    dlc, series = record(session, ctx, "Merge DLC"), record(session, ctx, "Merge Series", "series")
    relations.add(session, ctx, dlc, dup, "dlc_of")
    relations.add(session, ctx, dup, series, "series_member")
    session.commit()
    before = version(session, dlc), version(session, series)
    service.merge_canonicals(session, ctx, dup, game)
    session.commit()
    assert sorted(rows(session)) == sorted([(dlc, game, "dlc_of"), (game, series, "series_member")])
    assert no_active_relation_names_a_merged_record(session)
    assert (version(session, dlc), version(session, series)) == (before[0] + 1, before[1] + 1)
    # redirects: the merged id still works for reads and writes
    assert relations.end(session, ctx, dlc, dup, "dlc_of") is not None
    relations.add(session, ctx, dlc, dup, "expansion_of")
    session.commit()
    assert (dlc, game, "expansion_of") in rows(session)


def test_merge_ends_self_duplicate_conflicting_and_cyclic_relations(session, ctx):
    game, dup = record(session, ctx, "Survivor"), record(session, ctx, "Merged Away")
    a, b, c = (record(session, ctx, t) for t in ("Mg A", "Mg B", "Mg C"))
    s = record(session, ctx, "Mg Series", "series")
    relations.add(session, ctx, dup, game, "port_of")  # becomes game -> game: ended
    relations.add(session, ctx, game, s, "series_member")
    relations.add(session, ctx, dup, s, "series_member")  # duplicate of the survivor's: ended
    relations.add(session, ctx, game, a, "dlc_of")
    relations.add(session, ctx, dup, b, "expansion_of")  # second addon parent: ended
    relations.add(session, ctx, c, game, "mod_of")
    relations.add(session, ctx, dup, c, "remake_of")  # game -> c -> game would be a cycle: ended
    session.commit()
    service.merge_canonicals(session, ctx, dup, game)
    session.commit()
    assert sorted(rows(session)) == sorted([(game, s, "series_member"), (game, a, "dlc_of"), (c, game, "mod_of")])
    assert ended(session) == {(dup, game, "port_of"): "merge_self", (dup, s, "series_member"): "merge_duplicate",
                              (dup, b, "expansion_of"): "merge_conflict", (dup, c, "remake_of"): "merge_conflict"}
    assert no_active_relation_names_a_merged_record(session)


def test_merging_a_series_into_a_game_ends_its_memberships(session, ctx):
    series, game = record(session, ctx, "Wrong Series", "series"), record(session, ctx, "Real Game")
    member = record(session, ctx, "Member Game")
    relations.add(session, ctx, member, series, "series_member")
    session.commit()
    service.merge_canonicals(session, ctx, series, game)
    session.commit()
    assert rows(session) == [] and ended(session) == {(member, series, "series_member"): "merge_conflict"}


def test_split_after_merge_leaves_relations_on_the_original_record(session, ctx):
    game = record(session, ctx, "Split Game")
    [other] = service.ingest(session, ctx, batch("igdb", [{"external_id": "77", "title": "Split Game"}]), system=True)
    dlc = record(session, ctx, "Split DLC")
    relations.add(session, ctx, dlc, other.canonical_id, "dlc_of")
    session.commit()
    if other.canonical_id != game:
        service.merge_canonicals(session, ctx, other.canonical_id, game)
        session.commit()
    obs = session.scalars(select(service.Observation).where(service.Observation.source == "igdb")).one()
    new = service.detach_observation(session, ctx, obs.id)
    session.commit()
    assert rows(session) == [(dlc, game, "dlc_of")]  # stays with the record that keeps its id
    assert relations.of(session, new.id)["counts"] == {"parents": 0, "children": 0, "members_of": 0, "members": 0}
    assert no_active_relation_names_a_merged_record(session)


def test_set_outgoing_replaces_atomically(session, ctx):
    game, a, b = (record(session, ctx, t) for t in ("Put Game", "Put A", "Put B"))
    relations.add(session, ctx, game, a, "dlc_of")
    session.commit()
    assert relations.set_outgoing(session, ctx, game, [{"to": b, "type": "dlc_of"}]) == {"added": 1, "ended": 1}
    session.commit()
    assert rows(session) == [(game, b, "dlc_of")] and ended(session) == {(game, a, "dlc_of"): "replaced"}
    relations.add(session, ctx, a, b, "port_of")
    session.commit()
    with pytest.raises(AtlasError, match="cycle"):  # b -> a would close a -> b
        relations.set_outgoing(session, ctx, b, [{"to": a, "type": "mod_of"}])
    session.rollback()
    assert sorted(rows(session)) == sorted([(game, b, "dlc_of"), (a, b, "port_of")])


# ---- emission ---------------------------------------------------------------------------------------------

def test_gated_authority_stores_relations_but_signs_nothing(session, ctx):
    game, dlc = record(session, ctx, "Gated Game"), record(session, ctx, "Gated DLC")
    head = feed.head_seq(session)
    assert relations.gated(ctx)
    row, _ = relations.add(session, ctx, dlc, game, "dlc_of")
    relations.end(session, ctx, dlc, game, "dlc_of")
    session.commit()
    assert feed.head_seq(session) == head and row.feed_seq is None
    assert session.scalars(select(ChangeLog).where(ChangeLog.kind == "relation")).first() is None


def test_authority_at_1_4_signs_add_and_end(session, emitting):
    game, dlc = record(session, emitting, "Signed Game"), record(session, emitting, "Signed DLC")
    row, _ = relations.add(session, emitting, dlc, game, "dlc_of")
    relations.end(session, emitting, dlc, game, "dlc_of")
    session.commit()
    entries = [e.body for e in session.scalars(select(ChangeLog).where(ChangeLog.kind == "relation")
                                               .order_by(ChangeLog.seq))]
    assert entries == [{"op": "add", "from": dlc, "to": game, "type": "dlc_of", "source": "curator"},
                       {"op": "end", "from": dlc, "to": game, "type": "dlc_of"}]
    assert row.feed_seq is not None


def test_a_published_relation_is_not_ended_after_lowering_the_emit_protocol(session, emitting, ctx):
    game, dlc = record(session, emitting, "Lowered Game"), record(session, emitting, "Lowered DLC")
    relations.add(session, emitting, dlc, game, "dlc_of")
    session.commit()
    with pytest.raises(AtlasError, match="Raise federation emit protocol to 1.4") as exc:
        relations.end(session, ctx, dlc, game, "dlc_of")  # ctx emits 1.0
    assert exc.value.code == "emit_protocol"
    session.rollback()
    assert rows(session) == [(dlc, game, "dlc_of")]


def test_mirror_and_local_relations_are_never_signed(tmp_path):
    for role in ("mirror", "local"):
        app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/{role}.db", role=role,
                                             media_dir=str(tmp_path / role), emit_protocol="1.4"))
        st = app.state.atlas
        with st.maker() as s:
            a, b = record(s, st.ctx, f"{role} A"), record(s, st.ctx, f"{role} B")
            row, _ = relations.add(s, st.ctx, a, b, "port_of")
            s.commit()
            assert not row.redistributable and feed.head_seq(s) == 0


# ---- editions -----------------------------------------------------------------------------------------------

def test_editions_group_by_name_with_each_editions_own_ids():
    obs = [Obs("o1", "steam", "metadata", "100", {"title": "G", "edition": "Deluxe  Edition",
                                                  "provider_ids": {"gog": "base-game"}}, 5, 1),
           Obs("o2", "gog", "metadata", "g-dlx", {"title": "G", "edition": "deluxe edition"}, 3, 2),
           Obs("o3", "igdb", "metadata", "9", {"title": "G", "edition": "Anniversary"}, 1, 3),
           Obs("o4", "rawg", "metadata", "x", {"title": "G"}, 1, 4)]
    view, explain = merge(obs, {})
    assert view["editions"] == [
        {"name": "Anniversary", "provider_ids": {"igdb": "9"}, "sources": ["igdb"]},
        {"name": "Deluxe Edition", "provider_ids": {"gog": "g-dlx", "steam": "100"}, "sources": ["gog", "steam"]}]
    assert explain["editions"]["strategy"] == "group"
    assert "editions" not in merge([obs[3]], {})[0]  # absent means unknown


# ---- API and website ---------------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/api.db", media_dir=str(tmp_path / "m"),
                                         media_cache_enabled=False, emit_protocol="1.4"))
    st = app.state.atlas
    with TestClient(app) as client:
        _wait_for_startup_replay(st)
        with st.maker() as s:
            from atlas import auth
            _, curator = auth.create_token(s, "cur", ["curate"])
            _, reader = auth.create_token(s, "rd", ["read"])
            ids = {t: record(s, st.ctx, t, m) for t, m in (("Api Game", "game"), ("Api DLC", "game"),
                                                          ("Api Port", "game"), ("Api Series", "series"))}
            s.commit()
        client.st, client.ids = st, ids
        client.cur = {"Authorization": f"Bearer {curator}"}
        client.read = {"Authorization": f"Bearer {reader}"}
        yield client


def test_api_put_and_get_relations(api):
    ids = api.ids
    body = {"relations": [{"to": ids["Api Game"], "type": "dlc_of"}, {"to": ids["Api Series"], "type": "series_member"}]}
    assert api.put(f"/api/v2/games/{ids['Api DLC']}/relations", json=body, headers=api.read).status_code == 403
    r = api.put(f"/api/v2/games/{ids['Api DLC']}/relations", json=body, headers=api.cur)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == {"added": 2, "ended": 0} and r.json()["published"] is True
    game = api.get(f"/api/v2/games/{ids['Api Game']}", headers=api.read).json()
    assert game["relations"]["children"] == [{"id": ids["Api DLC"], "type": "dlc_of", "title": "Api DLC",
                                              "media_type": "game"}]
    assert game["relations"]["counts"] == {"parents": 0, "children": 1, "members_of": 0, "members": 0}
    assert game["title"] == "Api Game" and "id" in game  # additive: the old keys are unchanged
    dlc = api.get(f"/api/v2/games/{ids['Api DLC']}", headers=api.read).json()["relations"]
    assert [(p["id"], p["type"]) for p in dlc["parents"]] == [(ids["Api Game"], "dlc_of")]
    assert [(p["id"], p["type"]) for p in dlc["members_of"]] == [(ids["Api Series"], "series_member")]
    graph = api.get(f"/api/v2/games/{ids['Api Series']}/relations?depth=2", headers=api.read).json()
    assert {n["id"] for n in graph["nodes"]} == {ids["Api Series"], ids["Api DLC"], ids["Api Game"]}
    assert api.get(f"/api/v2/games/{ids['Api Series']}/relations?depth=9", headers=api.read).status_code == 422
    with api.st.maker() as s:
        kinds = [(e.body["op"], e.body["type"]) for e in s.scalars(select(ChangeLog).where(ChangeLog.kind == "relation"))]
    assert sorted(kinds) == [("add", "dlc_of"), ("add", "series_member")]
    changes = api.get("/api/v2/changes?since=0", headers=api.read).json()["results"]
    assert {ids["Api DLC"], ids["Api Game"], ids["Api Series"]} <= {c["id"] for c in changes}


def test_api_refusals_change_nothing(api):
    ids = api.ids
    ok = api.put(f"/api/v2/games/{ids['Api DLC']}/relations", headers=api.cur,
                 json={"relations": [{"to": ids["Api Game"], "type": "dlc_of"}]})
    assert ok.status_code == 200
    cycle = api.put(f"/api/v2/games/{ids['Api Game']}/relations", headers=api.cur,
                    json={"relations": [{"to": ids["Api DLC"], "type": "port_of"}]})
    assert cycle.status_code == 409 and cycle.json()["error"]["code"] == "conflict"
    bad = api.put(f"/api/v2/games/{ids['Api Game']}/relations", headers=api.cur,
                  json={"relations": [{"to": ids["Api DLC"], "type": "edition_of"}]})
    assert bad.status_code == 422
    target = api.put(f"/api/v2/games/{ids['Api Port']}/relations", headers=api.cur,
                     json={"relations": [{"to": ids["Api Game"], "type": "series_member"}]})
    assert target.status_code == 400
    too_many = api.put(f"/api/v2/games/{ids['Api Port']}/relations", headers=api.cur,
                       json={"relations": [{"to": ids["Api Game"], "type": "port_of"}] * 201})
    assert too_many.status_code == 422
    with api.st.maker() as s:
        assert rows(s) == [(ids["Api DLC"], ids["Api Game"], "dlc_of")]


def _reviewer_site(tmp_path, role="authority", **settings):
    s = build_settings(role=role, database_url=f"sqlite:///{tmp_path}/{role}-web.db", media_dir=str(tmp_path / "w"),
                       media_cache_enabled=False, public_url="http://localhost:8000", **settings)
    app = main.create_app(s)
    st = app.state.atlas
    client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
    client.__enter__()
    _wait_for_startup_replay(st)
    with st.maker() as sess:
        client.ids = {t: record(sess, st.ctx, t, m) for t, m in (("Web Game", "game"), ("Web DLC", "game"),
                                                                ("Web Series", "series"))}
        sess.commit()
    client.st = st
    signup(client, "alice")  # the first account administers this node
    return client


def test_editor_adds_searches_refuses_and_removes(tmp_path):
    site = _reviewer_site(tmp_path)
    ids = site.ids
    try:
        page = site.get(f"/game/{ids['Web DLC']}/relations?q=Web+Ga&type=dlc_of")
        assert page.status_code == 200 and "Web Game" in page.text and "data-relation-results" in page.text
        assert "Stays on this node for now" in page.text  # emit protocol 1.0: kept local
        t = token(site, f"/game/{ids['Web DLC']}/relations")
        r = site.post(f"/game/{ids['Web DLC']}/relations/add", data={"csrf": t, "to": ids["Web Game"], "type": "dlc_of"})
        assert r.status_code == 303
        with site.st.maker() as s:
            assert rows(s) == [(ids["Web DLC"], ids["Web Game"], "dlc_of")]
        game_page = site.get(f"/game/{ids['Web Game']}").text
        assert "data-relations" in game_page and "Web DLC" in game_page and 'data-rel-type="dlc_of"' in game_page
        refused = site.post(f"/game/{ids['Web Game']}/relations/add",
                            data={"csrf": t, "to": ids["Web DLC"], "type": "port_of"})
        assert refused.status_code == 409 and "would make a cycle" in refused.text
        assert "“Web DLC”" in refused.text  # named by title, not only by id
        assert 'value="port_of" selected' in refused.text  # the chosen type is kept
        wrong = site.post(f"/game/{ids['Web Game']}/relations/add",
                          data={"csrf": t, "to": ids["Web DLC"], "type": "series_member"})
        assert wrong.status_code == 400 and "data-relation-error" in wrong.text
        r = site.post(f"/game/{ids['Web DLC']}/relations/remove", data={"csrf": t, "to": ids["Web Game"], "type": "dlc_of"})
        assert r.status_code == 303
        with site.st.maker() as s:
            assert rows(s) == [] and ended(s) == {(ids["Web DLC"], ids["Web Game"], "dlc_of"): "removed"}
        assert "Ended relations" in site.get(f"/game/{ids['Web DLC']}/relations").text
    finally:
        site.__exit__(None, None, None)


def test_editor_needs_review_rights(tmp_path):
    site = _reviewer_site(tmp_path)
    try:
        other = TestClient(site.app, base_url="http://localhost:8000", follow_redirects=False)
        signup(other, "bob")  # an ordinary user
        assert other.get(f"/game/{site.ids['Web DLC']}/relations").status_code == 403
        t = token(other, "/me")
        r = other.post(f"/game/{site.ids['Web DLC']}/relations/add",
                       data={"csrf": t, "to": site.ids["Web Game"], "type": "dlc_of"})
        assert r.status_code == 403
        with site.st.maker() as s:
            assert rows(s) == []
    finally:
        site.__exit__(None, None, None)


# ---- federation ------------------------------------------------------------------------------------------------

@pytest.fixture
def fed(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.4")
    mirror = Node(tmp_path, "mirror", "mirror")
    return authority, mirror


def relation_state(node):
    with node.st.maker() as s:
        return sorted(rows(s)), ended(s)


def publish_typed(node, title, media_type):
    with node.st.maker() as s:
        cid = record(s, node.st.ctx, title, media_type)
    return cid


def relate(node, a, b, kind, op="add"):
    with node.st.maker() as s:
        if op == "add":
            relations.add(s, node.st.ctx, a, b, kind)
        else:
            relations.end(s, node.st.ctx, a, b, kind)
        s.commit()
        return feed.head_seq(s)


def test_a_1_4_mirror_applies_relations_merges_and_ends_like_the_authority(fed):
    authority, mirror = fed
    game, dup, dlc = (authority.publish(t) for t in ("Fed Game", "Fed Dup", "Fed DLC"))
    series = publish_typed(authority, "Fed Series", "series")
    relate(authority, dlc, dup, "dlc_of")
    relate(authority, dup, series, "series_member")
    relate(authority, game, series, "series_member")
    pull(mirror, authority)
    assert mirror.failures(authority) == [] and relation_state(mirror)[0] == relation_state(authority)[0]
    with authority.st.maker() as s:
        service.merge_canonicals(s, authority.st.ctx, dup, game)
        s.commit()
    relate(authority, game, series, "series_member", op="end")
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    mirror_active, mirror_ended = relation_state(mirror)
    authority_active, authority_ended = relation_state(authority)
    assert mirror_active == authority_active == [(dlc, game, "dlc_of")]
    assert mirror_ended == {**authority_ended, (game, series, "series_member"): "feed"}  # ended by the feed entry
    assert authority_ended[(game, series, "series_member")] == "removed"
    assert authority_ended[(dup, series, "series_member")] == "merge_duplicate"  # the same decision on both
    with mirror.st.maker() as s:
        stored = s.scalars(select(Relation).where(Relation.ended_at.is_(None))).one()
        assert stored.origin_node == authority.node_id and stored.redistributable
        assert no_active_relation_names_a_merged_record(s)
    pull(mirror, authority)  # idempotent
    assert relation_state(mirror) == (mirror_active, mirror_ended)


def at_protocol(node, protocol):
    """The node's context with another `emit_protocol` (the curator lowering or raising it)."""
    return Ctx(settings=node.st.ctx.settings.model_copy(update={"emit_protocol": protocol}),
               identity=node.st.ctx.identity)


def replicated_state(node):
    with node.st.maker() as s:
        return sorted((r.from_id, r.to_id, r.type) for r in s.scalars(
            select(Relation).where(Relation.ended_at.is_(None))) if relations.is_replicated(r))


def test_a_gated_local_relation_does_not_change_how_a_merge_collapses(fed):
    """Review finding: a relation kept local below emit 1.4 must not decide a replicated merge."""
    authority, mirror = fed
    g1, g2, d, s_ = (authority.publish(t) for t in ("Gm G1", "Gm G2", "Gm D", "Gm S"))
    with authority.st.maker() as s:  # emit 1.3: stored locally, nothing signed (the documented upgrade path)
        local, _ = relations.add(s, at_protocol(authority, "1.3"), s_, g2, "dlc_of")
        s.commit()
        assert local.feed_seq is None and not relations.is_replicated(local)
    relate(authority, d, g1, "dlc_of")  # emit 1.4: published
    pull(mirror, authority)
    with authority.st.maker() as s:
        before = version(s, s_), version(s, g2)
        service.merge_canonicals(s, authority.st.ctx, d, s_)
        s.commit()
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    # the published D -> G1 moved on both nodes; the authority's local S -> G2 gave way to it
    assert replicated_state(authority) == replicated_state(mirror) == [(s_, g1, "dlc_of")]
    assert relation_state(authority)[0] == relation_state(mirror)[0] == [(s_, g1, "dlc_of")]
    assert relation_state(authority)[1] == {(s_, g2, "dlc_of"): "superseded"}
    with authority.st.maker() as s:
        assert version(s, g2) > before[1] and version(s, s_) > before[0]
        assert no_active_relation_names_a_merged_record(s)
    relate(authority, s_, g1, "dlc_of", op="end")  # a later end reaches the mirror's row
    pull(mirror, authority)
    assert relation_state(mirror)[0] == relation_state(authority)[0] == []


def test_a_mirror_overlay_never_blocks_an_upstream_relation(fed):
    """Review finding: an upstream entry that conflicts only with a local overlay is applied, not quarantined;
    the overlay is ended (`superseded`) and an identical overlay is taken over by the upstream's row."""
    authority, mirror = fed
    x, p1, p2, a, b, c, d = (authority.publish(t) for t in ("Ov X", "Ov P1", "Ov P2", "Ov A", "Ov B", "Ov C",
                                                             "Ov D"))
    pull(mirror, authority)
    with mirror.st.maker() as s:  # mirror curators' overlays (local; never signed)
        relations.add(s, mirror.st.ctx, x, p1, "dlc_of", local_only=True)  # second parent once X dlc_of P2 lands
        relations.add(s, mirror.st.ctx, a, b, "remake_of", local_only=True)  # cycle once B port_of A lands
        relations.add(s, mirror.st.ctx, c, d, "mod_of", local_only=True)  # identical to the upstream's
        s.commit()
    relate(authority, x, p2, "dlc_of")
    relate(authority, b, a, "port_of")
    add_seq = relate(authority, c, d, "mod_of")
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    assert replicated_state(mirror) == replicated_state(authority) == sorted(
        [(x, p2, "dlc_of"), (b, a, "port_of"), (c, d, "mod_of")])
    assert relation_state(mirror)[1] == {(x, p1, "dlc_of"): "superseded", (a, b, "remake_of"): "superseded"}
    with mirror.st.maker() as s:
        taken = relations.active_between(s, c, d, "mod_of")
        assert (taken.origin_node, taken.origin_seq) == (authority.node_id, add_seq)
        assert sync.rescan(s, authority.node_id)["parked"] == 0
    relate(authority, c, d, "mod_of", op="end")
    pull(mirror, authority)
    assert relation_state(mirror)[0] == relation_state(authority)[0]


def test_a_feed_end_never_ends_a_local_overlay(fed):
    authority, mirror = fed
    a, b = authority.publish("Fe A"), authority.publish("Fe B")
    pull(mirror, authority)
    with mirror.st.maker() as s:
        relations.add(s, mirror.st.ctx, a, b, "port_of", local_only=True)
        s.commit()
    authority.emit("relation", {"op": "end", "from": a, "to": b, "type": "port_of"})
    pull(mirror, authority)
    assert mirror.failures(authority) == [] and relation_state(mirror)[0] == [(a, b, "port_of")]


def test_restore_after_a_downgrade_converges_with_a_follower_that_applied_the_merge(fed, monkeypatch):
    """Review finding: an authority rolled back to 0031 merges records (that release does not move relations);
    its 1.4 follower applies the merge with `move`. Re-upgrading replays the same `move`, so both agree, and the
    versions of the records whose relations changed advance."""
    from atlas import db

    authority, mirror = fed
    game, dup, dlc, port, other, original = (authority.publish(t) for t in ("Rs Game", "Rs Dup", "Rs DLC",
                                                                            "Rs Port", "Rs Other", "Rs Original"))
    relate(authority, dlc, dup, "dlc_of")
    relate(authority, port, dup, "port_of")
    relate(authority, dup, other, "remaster_of")  # a second remaster_of parent for the survivor: ended
    relate(authority, game, original, "remaster_of")
    pull(mirror, authority)
    url = authority.st.ctx.settings.database_url
    db.downgrade(url, "0031")
    with monkeypatch.context() as m:  # the previous release: merges sign a `merge`, relations stay where they are
        m.setattr(relations, "move", lambda session, src, dst: set())
        with authority.st.maker() as s:
            service.merge_canonicals(s, authority.st.ctx, dup, game)
            s.commit()
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    with authority.st.maker() as s:
        before = {cid: version(s, cid) for cid in (game, dup, dlc, port, other)}  # not `original`: unchanged
    db.upgrade(url)
    expected = sorted([(dlc, game, "dlc_of"), (port, game, "port_of"), (game, original, "remaster_of")])
    assert relation_state(authority)[0] == relation_state(mirror)[0] == expected
    assert relation_state(authority)[1] == relation_state(mirror)[1] == {(dup, other, "remaster_of"): "merge_conflict"}
    with authority.st.maker() as s:
        assert all(version(s, cid) > v for cid, v in before.items())
        assert no_active_relation_names_a_merged_record(s)
        assert s.get(relations.NodeState, relations.RESTORE_KEY) is None
    relate(authority, dlc, game, "dlc_of", op="end")  # and they keep converging
    pull(mirror, authority)
    assert relation_state(mirror)[0] == relation_state(authority)[0]


def test_mirror_curators_cannot_end_an_upstream_relation(fed):
    authority, mirror = fed
    game, dlc = authority.publish("Up Game"), authority.publish("Up DLC")
    relate(authority, dlc, game, "dlc_of")
    pull(mirror, authority)
    with mirror.st.maker() as s:
        with pytest.raises(AtlasError, match="upstream") as exc:
            relations.end(s, mirror.st.ctx, dlc, game, "dlc_of")
        assert exc.value.code == "conflict"


def test_a_replicated_cycle_is_quarantined_without_blocking(fed):
    authority, mirror = fed
    a, b = authority.publish("Qa A"), authority.publish("Qa B")
    relate(authority, a, b, "dlc_of")
    bad = authority.emit("relation", {"op": "add", "from": b, "to": a, "type": "mod_of", "source": "curator"})
    later = authority.publish("Qa Later")
    pull(mirror, authority)
    assert mirror.failures(authority) == [(bad, "relation", "quarantined", "invalid")]
    assert mirror.heads(authority)["applied_head"] == authority.head()  # not blocked
    assert "Qa Later" in mirror.titles() and later
    assert relation_state(mirror)[0] == [(a, b, "dlc_of")]


def test_a_relation_to_a_record_not_here_yet_is_retried(fed):
    authority, mirror = fed
    a = authority.publish("Rt A")
    ghost = canonical_id()
    seq = authority.emit("relation", {"op": "add", "from": a, "to": ghost, "type": "port_of", "source": "curator"})
    pull(mirror, authority)
    assert mirror.failures(authority) == [(seq, "relation", "quarantined", "apply_error")]
    authority.emit("obs", {**observation("Rt Ghost"), "canonical_id": ghost})
    pull(mirror, authority)
    with mirror.st.maker() as s:
        sync.retry_failures(s, mirror.st.ctx)
        s.commit()
    assert mirror.failures(authority) == [] and relation_state(mirror)[0] == [(a, ghost, "port_of")]


def play_older(monkeypatch, protocol):
    """Play a follower of a release before 1.4 for this process: `relation` is not a kind it knows (1.1-1.3 had
    it reserved, so classify parks it as `unsupported_kind`)."""
    kinds = {k: v for k, v in feed_schema.FEED_KINDS.items() if k != "relation"}
    monkeypatch.setattr(feed_schema, "FEED_KINDS", kinds)
    monkeypatch.setattr(feed_schema, "RESERVED_KINDS", {"relation": "1.4"})
    monkeypatch.setattr(feed_schema, "PROTOCOL", protocol)
    monkeypatch.setattr(sync, "PROTOCOL", protocol)


@pytest.mark.parametrize("protocol", ["1.3", "1.1"])
def test_an_older_follower_parks_relations_and_converges_after_upgrade(fed, monkeypatch, protocol):
    authority, mirror = fed
    game, dlc = authority.publish("Old Game"), authority.publish("Old DLC")
    pull(mirror, authority)
    seq = relate(authority, dlc, game, "dlc_of")
    authority.publish("Old Later")
    with monkeypatch.context() as m:
        play_older(m, protocol)
        pull(mirror, authority)
        heads = mirror.heads(authority)
        assert mirror.failures(authority) == [(seq, "relation", "parked", "unsupported_kind")]
        assert heads["applied_head"] == seq - 1 and heads["verified_head"] == authority.head()
        assert heads["parked"]["first_kind"] == "relation"
        assert "Old Later" not in mirror.titles()  # held back, retained, not dropped
        assert seq in mirror.retained(authority) and relation_state(mirror)[0] == []
    with mirror.st.maker() as s:  # upgraded: replay applies in order
        result = sync.replay(s, mirror.st.ctx)
        s.commit()
    assert result[authority.node_id]["applied_head"] == authority.head()
    assert mirror.failures(authority) == [] and "Old Later" in mirror.titles()
    assert relation_state(mirror) == relation_state(authority)


def test_a_1_0_follower_drop_is_recovered_by_rescan_and_historical_replay(fed, monkeypatch):
    authority, mirror = fed
    game, dlc = authority.publish("Drop Game"), authority.publish("Drop DLC")
    add_seq = relate(authority, dlc, game, "dlc_of")
    authority.publish("Drop Later")
    real = sync._apply

    def old_release(session, ctx, node_id, seq, kind, body):  # a 1.0 release passes over unknown kinds
        return None if kind == "relation" else real(session, ctx, node_id, seq, kind, body)

    with monkeypatch.context() as m:
        m.setattr(sync, "_apply", old_release)
        pull(mirror, authority)
    assert mirror.heads(authority)["applied_head"] == authority.head() and relation_state(mirror)[0] == []
    with mirror.st.maker() as s:
        counts = sync.rescan(s, authority.node_id)
        s.commit()
        assert counts["parked"] == 1
        f = s.get(SyncFailure, (authority.node_id, add_seq))
        assert (f.disposition, f.reason) == ("parked", "dropped_by_previous_release")
    with mirror.st.maker() as s:
        sync.replay(s, mirror.st.ctx, historical=True)
        s.commit()
    assert relation_state(mirror) == relation_state(authority) and mirror.failures(authority) == []
    with mirror.st.maker() as s:  # a second rescan finds nothing: the relation now carries its origin
        assert sync.rescan(s, authority.node_id)["parked"] == 0


def test_a_relay_serves_relation_entries_verbatim(fed, tmp_path):
    authority, mirror = fed
    game, dlc = authority.publish("Relay Game"), authority.publish("Relay DLC")
    seq = relate(authority, dlc, game, "dlc_of")
    pull(mirror, authority)
    assert mirror.retained(authority)[seq] == authority.own_feed()[seq]


def test_view_digest_and_count_unchanged_by_relations(fed):
    authority, mirror = fed
    a, b = authority.publish("Dg A"), authority.publish("Dg B")
    with authority.st.maker() as s:
        before = sync.view_digest(s, None)
    relate(authority, a, b, "port_of")
    with authority.st.maker() as s:
        assert sync.view_digest(s, None) == before  # scope atlas-view-v1: observations only (documented)
        assert s.scalar(select(func.count()).select_from(Relation)) == 1

