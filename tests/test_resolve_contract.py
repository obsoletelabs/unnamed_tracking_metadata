"""Contract C4: `POST /api/v2/resolve` semantics (#96).

The shared fixture `tests/fixtures/resolve-contract-v1.json` is what the metadata-atlas plugin vendors (C4.5): every
case in it is replayed here against a real node, and its digest is pinned so a change to it is deliberate. The other
tests check the database state behind the answers (crosswalk rows, merge redirects, versions, reviews) and that
`/resolve` writes nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from atlas import models
from atlas.resolve import SAFE_REASONS, TIERS, is_placeholder_title

from .test_media_facets import resolver  # noqa: F401
from .test_tracking_app_contract import atlas  # noqa: F401

FIXTURE = Path(__file__).parent / "fixtures" / "resolve-contract-v1.json"
DIGEST = FIXTURE.with_name(FIXTURE.name + ".sha256")
SPEC = json.loads(FIXTURE.read_text(encoding="utf-8"))


def observe(atlas, seed):  # noqa: F811
    item = {"external_id": seed["external_id"], "title": seed["title"]}
    for key in ("release_date", "provider_ids"):
        if key in seed:
            item[key] = seed[key]
    if "developer" in seed:
        item["facts"] = {"developer": seed["developer"]}
    body = {"connection": {"source_kind": "plugin", "source": seed["source"], "source_scope": "fixture"},
            "observations": [item]}
    r = atlas.post("/api/v2/observations", json=body, headers=atlas.write)
    assert r.status_code == 200, r.text
    return r.json()["results"][0]["atlas"]


def sub(value, ids):
    if isinstance(value, str) and value.startswith("$"):
        return ids[value[1:]]
    if isinstance(value, dict):
        return {k: sub(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [sub(v, ids) for v in value]
    return value


def build(atlas):  # noqa: F811
    ids = {}
    for seed in SPEC["seeds"]:
        ids[seed["name"]] = observe(atlas, seed)["canonical_id"]
    for src, dst in SPEC["merges"]:
        r = atlas.post(f"/api/v2/games/{sub(src, ids)}/merge", json={"into": sub(dst, ids)}, headers=atlas.write)
        assert r.status_code == 200, r.text
    return ids


def resolve(atlas, *items, include_view=True):  # noqa: F811
    r = atlas.post("/api/v2/resolve", headers=atlas.read, json={"items": list(items), "include_view": include_view})
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_fixture_digest_is_pinned():
    """The plugin vendors this file with its digest; changing a case means bumping both on purpose."""
    expected = DIGEST.read_text(encoding="utf-8").split()[0]
    # hashed with LF line endings, so a CRLF checkout (core.autocrlf) gives the same digest
    assert hashlib.sha256(FIXTURE.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == expected
    assert {c["expect"]["reason"] for c in SPEC["cases"]} <= set(TIERS)


def test_fixture_seeds_build_the_intended_records(atlas):  # noqa: F811
    ids = build(atlas)
    assert ids["halflife_claims_portal_id"] == ids["halflife"]  # ingest keeps its own semantics: assigns `own`
    assert ids["renamed_update"] == ids["renamed"]
    assert len({ids[n] for n in ("portal", "halflife", "doom1993", "doom2016", "placeholder", "renamed",
                                 "merge_a", "merge_b", "merge_c")}) == 9


@pytest.mark.parametrize("case", SPEC["cases"], ids=[c["name"] for c in SPEC["cases"]])
def test_fixture_case(atlas, case):  # noqa: F811
    ids = build(atlas)
    want = sub(case["expect"], ids)
    got = resolve(atlas, sub(case["request"], ids))[0]
    assert got["reason"] == want["reason"] and got["tier"] == want["tier"] == TIERS[want["reason"]]
    assert got["method"] == want["method"]
    assert got["canonical_id"] == want["canonical_id"]
    assert got["assigned"] is (want["canonical_id"] is not None)
    # a client may assign exactly what Atlas assigns, except a capped fuzzy match the admin opted into
    assert (got["reason"] in SAFE_REASONS) is got["assigned"]
    if "redirected_from" in want:
        assert got["redirected_from"] == want["redirected_from"]
    if "game_title" in want:
        assert got["game"]["id"] == want["canonical_id"] and got["game"]["title"] == want["game_title"]
    if want["canonical_id"] is None:
        assert got["game"] is None
    if want["reason"] == "conflict":
        assert got["conflict"]["own"] == want["conflict_own"]
        assert sorted(got["conflict"]["canonical_ids"]) == sorted(want["conflict_ids"])
        assert sorted(c["id"] for c in got["candidates"]) == sorted(want["conflict_ids"])
    else:
        assert got["conflict"] is None


def test_every_result_carries_the_c4_keys(atlas):  # noqa: F811
    build(atlas)
    for got in resolve(atlas, {"title": "Doom"}, {"namespace": "steamsync", "external_id": "620"}, {},
                       include_view=False):
        assert {"canonical_id", "method", "confidence", "candidates", "assigned", "reason", "tier",
                "redirected_from", "matched_keys", "conflict"} <= set(got)
        assert "game" not in got  # include_view=false still omits the record


def test_conflict_reports_keys_and_the_review_ingest_opened_without_writing(atlas):  # noqa: F811
    ids = build(atlas)
    portal, halflife = ids["portal"], ids["halflife"]
    with atlas.state.maker() as session:
        review = session.scalars(select(models.ReviewItem).where(
            models.ReviewItem.kind == "id_conflict", models.ReviewItem.status == "open",
            models.ReviewItem.canonical_id == halflife)).one()
        assert [c for c, _ in review.candidates] == [portal]
        # the crosswalk still says two different records own the two ids
        assert session.get(models.Crosswalk, ("steamsync", "620")).canonical_id == portal
        assert session.get(models.Crosswalk, ("igdbsync", "72")).canonical_id == halflife
        before = {t.__tablename__: session.scalar(select(func.count()).select_from(t))
                  for t in (models.ReviewItem, models.Crosswalk, models.Canonical, models.Observation,
                            models.ChangeLog)}
        versions = dict(session.execute(select(models.Canonical.id, models.Canonical.version)).all())

    got = resolve(atlas, {"namespace": "igdbsync", "external_id": "72", "title": "Half-Life 2",
                          "provider_ids": {"steamsync": "620"}})[0]
    assert got["canonical_id"] is None and got["method"] == "none" and got["reason"] == "conflict"
    assert got["confidence"] == 0.9 and got["conflict"]["own"] == halflife
    assert got["conflict"]["review_id"] == review.id
    assert sorted((k["namespace"], k["external_id"], k["canonical_id"]) for k in got["matched_keys"]) == [
        ("igdbsync", "72", halflife), ("steamsync", "620", portal)]

    # the same claim from the other side names the other record as `own`
    other = resolve(atlas, {"namespace": "steamsync", "external_id": "620", "provider_ids": {"igdbsync": "72"}})[0]
    assert other["reason"] == "conflict" and other["conflict"]["own"] == portal

    with atlas.state.maker() as session:  # /resolve is read-only: no review, crosswalk or version change
        after = {t.__tablename__: session.scalar(select(func.count()).select_from(t))
                 for t in (models.ReviewItem, models.Crosswalk, models.Canonical, models.Observation,
                           models.ChangeLog)}
        assert after == before
        assert dict(session.execute(select(models.Canonical.id, models.Canonical.version)).all()) == versions


def test_conflict_without_an_open_review_has_null_review_id(atlas):  # noqa: F811
    ids = build(atlas)
    got = resolve(atlas, {"namespace": "atlas", "external_id": ids["doom1993"],
                          "provider_ids": {"steamsync": "379720"}})[0]
    assert got["reason"] == "conflict" and got["conflict"]["review_id"] is None
    assert got["conflict"]["own"] == ids["doom1993"] and got["confidence"] == 0.9


def test_merge_chain_redirects_by_id_and_keeps_old_ids_resolving(atlas):  # noqa: F811
    ids = build(atlas)
    a, b, c = ids["merge_a"], ids["merge_b"], ids["merge_c"]
    with atlas.state.maker() as session:
        assert session.get(models.Canonical, a).merged_into == b
        assert session.get(models.Canonical, b).merged_into == c
        assert session.get(models.Canonical, c).merged_into is None
        # merge moved every crosswalk row to the survivor
        for ext in ("5001", "5002", "5003"):
            assert session.get(models.Crosswalk, ("steamsync", ext)).canonical_id == c

    for old in (a, b):
        got = resolve(atlas, {"namespace": "atlas", "external_id": old, "title": "Unrelated Title"})[0]
        assert (got["reason"], got["canonical_id"], got["redirected_from"]) == ("redirect", c, old)
        assert got["matched_keys"] == [{"namespace": "atlas", "external_id": old, "canonical_id": c}]
        assert got["game"]["id"] == c and got["game"]["title"] == "Silent Orchard"
        record = atlas.get(f"/api/v2/games/{old}", headers=atlas.read).json()
        assert record["id"] == c and record["redirected_from"] == old

    # the bare ULID (no `atlas:` prefix) is the same id
    bare = resolve(atlas, {"provider_ids": {"atlas": a.removeprefix("atlas:")}})[0]
    assert bare["reason"] == "redirect" and bare["canonical_id"] == c

    # an atlas id and a provider id that agree after the redirect are one identity, not a conflict
    both = resolve(atlas, {"namespace": "atlas", "external_id": a, "provider_ids": {"steamsync": "5003"}})[0]
    assert both["reason"] == "redirect" and both["canonical_id"] == c and both["conflict"] is None


def test_rename_keeps_the_id_bumps_the_version_and_reaches_changes(atlas):  # noqa: F811
    first = observe(atlas, {"source": "steamsync", "external_id": "400", "title": "Working Title",
                            "release_date": "2019-06-01"})["canonical_id"]
    with atlas.state.maker() as session:
        v1 = session.get(models.Canonical, first).version
    head = atlas.get("/api/v2/changes", params={"feed": "local", "since": 0}, headers=atlas.read).json()
    renamed = observe(atlas, {"source": "steamsync", "external_id": "400", "title": "Final Name",
                              "release_date": "2019-06-01"})
    assert renamed["canonical_id"] == first and renamed["match"] == "crosswalk"
    with atlas.state.maker() as session:
        row = session.get(models.Canonical, first)
        assert row.version > v1 and row.norm_title == "final name" and row.merged_into is None
        assert session.get(models.Crosswalk, ("steamsync", "400")).canonical_id == first
    page = atlas.get("/api/v2/changes", params={"feed": "local", "since": head["next_cursor"],
                                                "epoch": head["epoch"]}, headers=atlas.read).json()
    assert first in {r["id"] for r in page["results"]}

    # the client still holds the old title and the provider id: identity wins, the title is not consulted
    got = resolve(atlas, {"title": "Working Title", "provider_ids": {"steamsync": "400"}},
                  {"namespace": "atlas", "external_id": first, "title": "Working Title"})
    assert [(g["reason"], g["canonical_id"], g["game"]["title"]) for g in got] == [
        ("crosswalk", first, "Final Name"), ("id", first, "Final Name")]


def test_placeholder_title_record_is_refreshed_by_id_only(atlas):  # noqa: F811
    cid = observe(atlas, {"source": "steamsync", "external_id": "999001", "title": "Steam app 999001",
                          "release_date": "2020-03-01"})["canonical_id"]
    with atlas.state.maker() as session:
        assert session.get(models.Canonical, cid).norm_title == "steam app 999001"
    by_id, by_title = resolve(atlas, {"namespace": "steamsync", "external_id": "999001", "title": "Real Name"},
                              {"title": "Steam App 999001", "release_date": "2020-03-01"})
    assert by_id["reason"] == "crosswalk" and by_id["canonical_id"] == cid
    assert by_title["reason"] == "none" and by_title["canonical_id"] is None and by_title["candidates"] == []


@pytest.mark.parametrize(("title", "placeholder"), [
    ("Steam app 12345", True), ("steam APP 12345", True), ("GOG game 1207658924", True), ("Epic app id 77", True),
    ("Steam App #12345", True), ("Game 2", False), ("Portal 2", False), ("Doom game 1993", False),
    ("Steam", False), (None, False), ("", False),
])
def test_placeholder_titles(title, placeholder):
    assert is_placeholder_title(title) is placeholder


def test_identical_titles_list_both_candidates_and_assign_neither(atlas):  # noqa: F811
    ids = build(atlas)
    got = resolve(atlas, {"title": "DOOM", "platform": "PC"})[0]
    assert got["reason"] == "ambiguous" and got["tier"] == "weak" and got["canonical_id"] is None
    assert {c["id"] for c in got["candidates"]} == {ids["doom1993"], ids["doom2016"]}
    assert got["matched_keys"] == []


def test_openapi_documents_the_c4_response(atlas):  # noqa: F811
    spec = atlas.app.openapi()
    op = spec["paths"]["/api/v2/resolve"]["post"]
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    schemas = spec["components"]["schemas"]
    item = schemas[schemas[ref]["properties"]["results"]["items"]["$ref"].rsplit("/", 1)[-1]]
    assert set(item["properties"]["reason"]["enum"]) == set(TIERS)
    assert set(item["properties"]["tier"]["enum"]) == {"id", "strong", "weak", "none"}
    for key in ("assigned", "redirected_from", "matched_keys", "conflict", "canonical_id", "method"):
        assert key in item["properties"], key


def test_unknown_atlas_id_never_lets_the_title_assign(atlas):  # noqa: F811
    """A client holding an Atlas id this node does not know (another authority, a reset database, a typo) is not
    told to adopt a different record on title alone: the title candidates are reported, nothing is assigned."""
    ids = build(atlas)
    unknown = "atlas:01ZZZZZZZZZZZZZZZZZZZZZZZZ"
    with atlas.state.maker() as session:
        assert session.get(models.Canonical, unknown) is None
        assert session.get(models.Crosswalk, ("atlas", unknown)) is None
    plain, held, as_provider = resolve(
        atlas,
        {"title": "Portal 2", "release_date": "2011"},
        {"namespace": "atlas", "external_id": unknown, "title": "Portal 2", "release_date": "2011"},
        {"provider_ids": {"atlas": unknown}, "title": "Portal 2", "release_date": "2011"})
    assert (plain["reason"], plain["canonical_id"]) == ("exact", ids["portal"])
    for got in (held, as_provider):
        assert (got["reason"], got["tier"], got["canonical_id"], got["assigned"]) == ("ambiguous", "weak", None, False)
        assert got["candidates"][0]["id"] == ids["portal"] and got["confidence"] == plain["confidence"]
        assert got["matched_keys"] == [] and got["game"] is None

    # a known provider id is still identity evidence on its own
    known = resolve(atlas, {"provider_ids": {"atlas": unknown, "steamsync": "620"}, "title": "Unrelated"})[0]
    assert (known["reason"], known["canonical_id"]) == ("crosswalk", ids["portal"])


def test_merge_chain_beyond_the_hop_limit_is_never_a_redirect(atlas):  # noqa: F811
    from atlas.resolve import MAX_REDIRECT_HOPS

    chain = [observe(atlas, {"source": "steamsync", "external_id": str(7000 + i), "title": f"Chain Link {i:02d}",
                             "release_date": "2010-01-01"})["canonical_id"] for i in range(MAX_REDIRECT_HOPS + 2)]
    for src, dst in zip(chain[:-1], chain[1:], strict=True):
        r = atlas.post(f"/api/v2/games/{src}/merge", json={"into": dst}, headers=atlas.write)
        assert r.status_code == 200, r.text
    with atlas.state.maker() as session:
        hops, cid = 0, chain[0]
        while (row := session.get(models.Canonical, cid)).merged_into is not None:
            hops, cid = hops + 1, row.merged_into
        assert hops == MAX_REDIRECT_HOPS + 1 and cid == chain[-1]

    deep, near = resolve(atlas, {"namespace": "atlas", "external_id": chain[0], "title": "Chain Link 21"},
                         {"namespace": "atlas", "external_id": chain[1]})
    # the followed record is still merged: no live id is known, so nothing is assigned, and the title is capped
    assert (deep["reason"], deep["canonical_id"], deep["assigned"]) == ("ambiguous", None, False)
    assert deep["redirected_from"] is None and deep["matched_keys"] == []
    assert (near["reason"], near["canonical_id"], near["redirected_from"]) == ("redirect", chain[-1], chain[1])


def test_conflict_own_is_the_items_own_atlas_id(atlas):  # noqa: F811
    ids = build(atlas)
    first, second = sorted((ids["doom1993"], ids["doom2016"]))
    for own, other in ((first, second), (second, first)):
        got = resolve(atlas, {"namespace": "atlas", "external_id": own, "provider_ids": {"atlas": other}})[0]
        assert got["reason"] == "conflict" and got["canonical_id"] is None
        assert got["conflict"]["own"] == own and sorted(got["conflict"]["canonical_ids"]) == [first, second]
    # without an item-level Atlas id there is no `own`, whatever provider_ids.atlas says
    got = resolve(atlas, {"provider_ids": {"atlas": first, "steamsync": "379720"}})[0]
    assert got["reason"] == "conflict" and got["conflict"]["own"] is None and got["confidence"] == 0.0


def _set_media_type(atlas, cid, media_type):  # noqa: F811
    with atlas.state.maker() as session:
        session.get(models.Canonical, cid).media_type = media_type
        session.commit()


def test_ids_pointing_at_records_of_different_types_are_a_conflict_not_a_crosswalk(atlas):  # noqa: F811
    """Review on #105: filtering by `media_type` before the conflict check hid the contradicting id and assigned
    the game with `reason: crosswalk`. Any supplied id that names a different record, of any type, is a conflict."""
    game = observe(atlas, {"source": "steamsync", "external_id": "10", "title": "Crossed Wires"})["canonical_id"]
    movie = observe(atlas, {"source": "imdb", "external_id": "20", "title": "Crossed Wires: The Film"})["canonical_id"]
    assert game != movie
    _set_media_type(atlas, movie, "movie")

    got = resolve(atlas, {"namespace": "steamsync", "external_id": "10", "media_type": "game",
                          "title": "Crossed Wires", "provider_ids": {"imdb": "20"}})[0]
    assert got["reason"] == "conflict" and got["canonical_id"] is None and got["method"] == "none"
    assert got["assigned"] is False and got["game"] is None
    # the contradicting evidence is kept, not filtered out by type
    assert sorted(got["conflict"]["canonical_ids"]) == sorted([game, movie])
    assert got["conflict"]["own"] == game and got["confidence"] == 0.9
    assert sorted((k["namespace"], k["external_id"], k["canonical_id"]) for k in got["matched_keys"]) == sorted([
        ("steamsync", "10", game), ("imdb", "20", movie)])

    # the same with no media_type, and with the type of the other record: still a conflict, never an assignment
    for media_type in (None, "movie"):
        item = {"namespace": "steamsync", "external_id": "10", "provider_ids": {"imdb": "20"}}
        if media_type:
            item["media_type"] = media_type
        other = resolve(atlas, item)[0]
        assert other["reason"] == "conflict" and other["canonical_id"] is None
        assert sorted(other["conflict"]["canonical_ids"]) == sorted([game, movie])

    # an Atlas id of the other type contradicts a provider id just the same
    mixed = resolve(atlas, {"media_type": "game", "provider_ids": {"steamsync": "10", "atlas": movie}})[0]
    assert mixed["reason"] == "conflict" and mixed["canonical_id"] is None and mixed["conflict"]["own"] is None

    # one record of another type, no contradiction: still a type_mismatch with its key reported
    only = resolve(atlas, {"namespace": "imdb", "external_id": "20", "media_type": "game"})[0]
    assert only["reason"] == "type_mismatch" and only["canonical_id"] is None
    assert [(k["namespace"], k["canonical_id"]) for k in only["matched_keys"]] == [("imdb", movie)]
