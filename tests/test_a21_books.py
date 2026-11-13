"""CONTRACT-C15 §2-§4 (A21): the book model. The built-in `book` type, the `ext.book` contract and its feed
registration under the unified release version (T-B8), the merge rules (T-B5), the ISBN-first editions view
(T-B6), achievements for books (T-B7, the book part), `relation.position` on `series_member` (T-B11) and the
type-aware title key. All books here are synthetic (made-up titles, authors and ISBNs)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from atlas import auth, custom, facets, feed_schema, matching, merge, relations, service, vocab
from atlas.contract import BookInfoV1, SourceObservationV1
from atlas.feed_schema import EmitGateError, check_emit, classify
from atlas.models import BookCreator, Canonical, ChangeLog, MediaType, Relation
from atlas.normalize import norm_title

from .conftest import batch
from .test_sync_compat import Node, pull

ISBN_A, ISBN_B, ISBN_C = "9780000000002", "9781234567897", "9791234567896"
AUTHORS = [{"name": "Ione Marsh", "role": "author", "ids": {"openlibrary": "OL1A"}},
           {"name": "Teo Varga", "role": "translator"}]
BOOK = {"authors": AUTHORS, "subtitle": "A Novel of Small Lights", "original_language": "en", "page_count": 412,
        "series": {"name": "The Lamp Cycle", "position": "1"},
        "editions": [{"key": "E1", "name": "Paperback, Harbour 2005", "isbn_13": ISBN_A, "format": "paperback",
                      "publisher": "Harbour Press", "published": {"date": "2005-08", "precision": "month"},
                      "pages": 528, "language": "en"}]}


def obs_body(book: dict | None = None, **payload) -> dict:
    return {"canonical_id": "atlas:01J00000000000000000000001", "media_type": "book",
            "observation": {"id": "01J00000000000000000000002", "source_kind": "manual", "source": "manual",
                            "source_scope": "default", "external_id": "x",
                            "payload": {"title": "The Lantern Keeper", **payload,
                                        **({"ext": {"book": book}} if book is not None else {})},
                            "observed_at": "2026-11-10T10:00:00+00:00"}}


# ---- the built-in type -------------------------------------------------------------------------------------

def test_book_is_a_seeded_builtin_without_achievements(session):
    custom.seed(session)
    row = session.get(MediaType, "book")
    assert (row.label, row.position, row.origin, row.achievements) == ("Book", 6, "local", False)
    assert row.labels == {"developer": "Author", "publisher": "Publisher", "series": "Series",
                          "release_date": "First published", "platforms": "Formats", "features": "Subjects"}
    assert custom.fields_for(session, "book") == []  # no custom fields: page_count etc. are ext.book view fields
    others = {t.key: t.achievements for t in session.scalars(select(MediaType)) if t.key != "book"}
    assert others and all(others.values())  # only book changes (C15 §2.7 critic)


def test_seed_leaves_an_existing_book_type_alone(session):
    session.add(MediaType(key="book", label="Novels", position=9, labels={"developer": "Writer"}, origin="local"))
    session.flush()
    custom.seed(session)
    row = session.get(MediaType, "book")
    assert (row.label, row.position, row.labels) == ("Novels", 9, {"developer": "Writer"})


# ---- ext.book contract (C15 §2.3) --------------------------------------------------------------------------

def test_ext_book_contract_rules():
    info = BookInfoV1.model_validate(BOOK)
    assert info.reported()["authors"][0] == AUTHORS[0]
    with pytest.raises(ValidationError):
        BookInfoV1.model_validate({"authors": [{"name": f"A{i}"} for i in range(31)]})  # at most 30
    with pytest.raises(ValidationError):
        BookInfoV1.model_validate({"authors": [{"name": "X", "role": "ghostwriter"}]})  # book_creator_role
    with pytest.raises(ValidationError):
        BookInfoV1.model_validate({"authors": [{"name": "X", "ids": {"atlas": "atlas:1"}}]})  # reserved namespace
    with pytest.raises(ValidationError):
        BookInfoV1.model_validate({"editions": [{"format": "scroll"}]})  # book_format
    with pytest.raises(ValidationError):
        BookInfoV1.model_validate({"page_count": 100_001})
    with pytest.raises(ValidationError):
        BookInfoV1.model_validate({"series": {"name": "S", "position": "x" * 17}})
    with pytest.raises(ValidationError):
        BookInfoV1.model_validate({"subtitle": "\n\t"})  # printable only
    big = {"editions": [{"name": f"Edition {i}", "publisher": "P" * 200, "key": "k" * 64,
                         "provider_ids": {f"ns{j}": "v" * 256 for j in range(8)}} for i in range(50)]}
    with pytest.raises(ValidationError, match="64 KiB"):
        BookInfoV1.model_validate(big)
    assert BookInfoV1.model_validate({"authors": [{"name": " Ione\u0007 Marsh "}]}).authors[0].name == "Ione Marsh"


def test_observation_carries_ext_book_and_drops_empty_members():
    obs = SourceObservationV1.model_validate({"external_id": "x", "title": "T", "ext": {"book": BOOK}})
    assert obs.reported()["ext"]["book"]["page_count"] == 412
    assert "ext" not in SourceObservationV1.model_validate({"external_id": "x", "ext": {"book": {"authors": []}}}).reported()


def test_vocab_file_lists_the_book_vocabularies():
    exported = vocab.export()
    assert exported["book_format"] == list(vocab.BOOK_FORMATS) == ["paperback", "hardcover", "ebook", "audiobook",
                                                                   "other"]
    assert exported["book_creator_role"] == list(vocab.BOOK_CREATOR_ROLES)
    from pathlib import Path

    on_disk = json.loads((Path(__file__).parents[1] / vocab.FILE).read_text(encoding="utf-8"))
    assert on_disk == exported


# ---- feed registration (T-B8) ------------------------------------------------------------------------------

def test_t_b8_follower_of_this_release_applies_ext_book():
    out = classify("obs", {**obs_body(BOOK), "_requires": ["book-v1"]})
    assert out.action == "apply", out.message
    assert out.body["observation"]["payload"]["ext"]["book"]["editions"][0]["isbn_13"] == ISBN_A


@pytest.mark.parametrize(("book", "reason"), [
    ({"authors": [{"name": "X", "pseudonym": True}]}, "unsupported_field"),
    ({"editions": [{"name": "E", "published": {"date": "2005", "precision": "year", "era": "ce"}}]},
     "unsupported_field"),
    ({"illustrations": 3}, "unsupported_field"),
    ({"editions": [{"format": "scroll"}]}, "unsupported_value"),
    ({"authors": [{"name": "X", "role": "ghostwriter"}]}, "unsupported_value"),
    ({"editions": [{"published": {"date": "2005-08", "precision": "week"}}]}, "unsupported_value"),
])
def test_t_b8_unknown_nested_members_and_values_park(book, reason):
    out = classify("obs", {**obs_body(book), "_requires": ["book-v1"]})
    assert (out.action, out.reason) == ("parked", reason), out.message


def test_t_b8_signed_with_book_v1_and_refused_below_unified():
    assert "book-v1" in feed_schema.CAPABILITIES and "book-v1" in feed_schema.capabilities()
    signed = check_emit("obs", obs_body(BOOK), feed_schema.UNIFIED)
    assert signed["_requires"] == ["book-v1"]
    with pytest.raises(EmitGateError, match="needs federation protocol 1.5"):
        check_emit("obs", obs_body(BOOK), "1.4")
    plain = obs_body()
    assert check_emit("obs", plain, "1.0") is plain  # a body without ext.book is signed exactly as before
    both = obs_body(BOOK, ext=None)
    both["observation"]["payload"]["ext"] = {"book": BOOK, "release": {"first": {"date": "1999", "precision": "year"}}}
    assert check_emit("obs", both, feed_schema.UNIFIED)["_requires"] == ["book-v1", "dates-v1"]


def test_t_b8_a_follower_without_book_v1_parks_instead_of_stripping(monkeypatch):
    monkeypatch.setattr(feed_schema, "CAPABILITIES", ("ext-v1", "dates-v1"))
    monkeypatch.setattr(feed_schema, "EXT_APPLY", frozenset({"release"}))
    signed = check_emit("obs", obs_body(BOOK), "1.5")
    out = classify("obs", signed)
    assert (out.action, out.reason, out.detail["requires"]) == ("parked", "unsupported_capability", ["book-v1"])
    # without the token it would have been stripped to a bare record: the token is what makes it explicit
    unsigned = classify("obs", obs_body(BOOK))
    assert unsigned.action == "apply" and "ext" not in unsigned.body["observation"]["payload"]


def test_t_b8_moving_unified_moves_every_c15_gate(monkeypatch):
    later = feed_schema.UnifiedVersion("1.6")
    monkeypatch.setattr(feed_schema, "UNIFIED", later)
    for name, key in feed_schema.UNIFIED_CONSTRUCTS:
        monkeypatch.setitem(getattr(feed_schema, name), key, later)
    with pytest.raises(EmitGateError, match="1.6"):
        check_emit("obs", obs_body(BOOK), "1.5")
    with pytest.raises(EmitGateError, match="1.6"):
        check_emit("lock", {"canonical_id": "atlas:x", "field": "authors", "value": AUTHORS}, "1.5")
    rel = {"op": "add", "from": "atlas:a", "to": "atlas:s", "type": "series_member", "source": "curator",
           "position": "2"}
    assert "position" not in check_emit("relation", rel, "1.5")
    monkeypatch.setattr(feed_schema, "PROTOCOL", "1.6")
    assert check_emit("obs", obs_body(BOOK), "1.6")["_requires"] == ["book-v1"]
    assert check_emit("relation", rel, "1.6")["position"] == "2"


def test_t_b8_book_locks_and_position_are_registered_under_unified():
    for f in merge.BOOK_LOCKS:
        assert feed_schema.VALUE_SINCE[("lock_field", f)] is feed_schema.UNIFIED
        assert f in feed_schema.vocabularies()["lock_field"]
    assert feed_schema.BOOK_LOCK_FIELDS == merge.BOOK_LOCKS
    assert feed_schema.MEMBER_SINCE[("relation", "position")] is feed_schema.UNIFIED
    assert feed_schema.TOKEN_SINCE["book-v1"] is feed_schema.UNIFIED
    with pytest.raises(EmitGateError):
        check_emit("lock", {"canonical_id": "atlas:x", "field": "page_count", "value": 3}, "1.4")
    assert check_emit("lock", {"canonical_id": "atlas:x", "field": "page_count", "value": 3}, "1.5")["value"] == 3


# ---- merge (T-B5) and the editions view (T-B6) -------------------------------------------------------------

def mk(oid: str, source: str, trust: int, book: dict | None = None, *, cls: str = "provider", **payload) -> merge.Obs:
    if book is not None:
        payload["ext"] = {"book": book}
    return merge.Obs(id=oid, source=source, source_kind="metadata", external_id=f"{source}-{oid}", payload=payload,
                     trust=trust, stamp=1, cls=merge.CLASS_RANK[cls], source_cls=merge.CLASS_RANK[cls])


def test_t_b5_highest_ranked_author_list_wins_whole():
    high = mk("1", "igdb", 70, {"authors": [{"name": "Ione Marsh"}, {"name": "Teo Varga"}], "page_count": 400})
    low = mk("2", "rawg", 50, {"authors": [{"name": "I. Marsh"}, {"name": "Pell Okafor"}, {"name": "Teo Varga"}],
                               "page_count": 412, "subtitle": "A Novel"})
    view, explain = merge.merge([low, high], {})
    assert view["authors"] == [{"name": "Ione Marsh"}, {"name": "Teo Varga"}]  # never interleaved
    assert explain["authors"]["source"] == "igdb" and explain["authors"]["runners_up"] == ["2"]
    assert (view["page_count"], view["subtitle"]) == (400, "A Novel")  # each field by priority on its own
    assert view["developer"] == "Ione Marsh"  # derived from the first author
    assert explain["developer"] == {"strategy": "derived", "reason": "derived", "from": "authors"}


def test_t_b5_lock_beats_the_merge_and_a_stated_developer_beats_the_derivation():
    o = mk("1", "igdb", 70, {"authors": [{"name": "Ione Marsh"}]}, facts={"developer": "Marsh Estate"})
    view, _ = merge.merge([o], {"authors": [{"name": "Pell Okafor", "role": "editor"}]})
    assert view["authors"] == [{"name": "Pell Okafor", "role": "editor"}]
    assert view["developer"] == "Marsh Estate"
    view, explain = merge.merge([mk("1", "igdb", 70, {"authors": AUTHORS})], {"authors": merge.CLEARED})
    assert "authors" not in view and "developer" not in view and explain["authors"]["strategy"] == "cleared"
    view, _ = merge.merge([mk("1", "igdb", 70, {"authors": AUTHORS})], {"developer": merge.CLEARED})
    assert "developer" not in view  # a cleared developer stays cleared


def test_t_b5_curator_class_beats_provider_trust():
    prov = mk("1", "igdb", 70, {"authors": [{"name": "Wrong Name"}]})
    cur = mk("2", "curator", 100, {"authors": [{"name": "Ione Marsh"}]}, cls="curator")
    assert merge.merge([prov, cur], {})[0]["authors"] == [{"name": "Ione Marsh"}]


def test_t_b6_same_isbn_under_two_names_is_one_edition():
    a = mk("1", "igdb", 70, {"editions": [{"name": "Paperback", "isbn_13": ISBN_A, "format": "paperback"}]})
    b = mk("2", "rawg", 50, {"editions": [{"name": "Trade paperback", "isbn_13": ISBN_A, "pages": 528, "key": "M1"}]})
    [ed] = merge.merge([a, b], {})[0]["editions"]
    assert ed == {"name": "Paperback", "provider_ids": {"rawg_edition": "M1"}, "sources": ["igdb", "rawg"],
                  "isbn_13": ISBN_A, "format": "paperback", "pages": 528}


def test_t_b6_two_paperbacks_with_different_isbns_stay_two():
    a = mk("1", "igdb", 70, {"editions": [{"name": "Paperback", "isbn_13": ISBN_A},
                                          {"name": "Paperback", "isbn_13": ISBN_B}]})
    eds = merge.merge([a], {})[0]["editions"]
    assert [e["isbn_13"] for e in eds] == [ISBN_A, ISBN_B]


def test_t_b6_name_only_joins_the_single_isbn_group_of_that_name():
    a = mk("1", "igdb", 70, {"editions": [{"name": "Hardcover", "isbn_13": ISBN_B}]})
    b = mk("2", "rawg", 50, edition="hardcover")  # the C6.1 path: an observation's own edition name
    [ed] = merge.merge([a, b], {})[0]["editions"]
    assert ed["isbn_13"] == ISBN_B and ed["provider_ids"] == {"rawg": "rawg-2"} and ed["sources"] == ["igdb", "rawg"]
    # with two ISBN groups of that name, a name-only entry joins neither
    c = mk("3", "igdb", 70, {"editions": [{"name": "Hardcover", "isbn_13": ISBN_B},
                                          {"name": "Hardcover", "isbn_13": ISBN_C}]})
    eds = merge.merge([c, b], {})[0]["editions"]
    assert len(eds) == 3 and [e.get("isbn_13") for e in eds].count(None) == 1


def test_t_b6_synthesised_name_cap_and_game_editions_unchanged():
    a = mk("1", "igdb", 70, {"editions": [{"isbn_13": ISBN_A, "format": "hardcover", "publisher": "Harbour Press",
                                           "published": {"date": "2005-08", "precision": "month"}}]})
    [ed] = merge.merge([a], {})[0]["editions"]
    assert ed["name"] == "Hardcover, Harbour Press 2005"
    many = mk("1", "igdb", 70, {"editions": [{"name": f"Printing {i:02d}"} for i in range(50)]})
    more = mk("2", "rawg", 50, {"editions": [{"name": "Printing 99"}]})
    assert len(merge.merge([many, more], {})[0]["editions"]) == merge.MAX_EDITIONS
    game = merge.merge([mk("1", "steam", 65, title="Game", edition="Deluxe Edition")], {})[0]
    assert game["editions"] == [{"name": "Deluxe Edition", "provider_ids": {"steam": "steam-1"}, "sources": ["steam"]}]


# ---- the type-aware title key (C15 §2.5) ----------------------------------------------------------------------

def test_norm_title_keeps_edition_words_for_books_only():
    assert norm_title("The Second Edition", "book") == "second edition"
    assert norm_title("The Second Edition") == "second edition"  # "second" is no edition word: both keep it
    assert norm_title("Hades: Definitive Edition") == "hades"
    assert norm_title("Hades: Definitive Edition", "game") == "hades"
    assert norm_title("A Special Edition", "book") == "a special edition"
    assert norm_title("A Special Edition") == "a"
    assert norm_title("Letters (Collected)", "book") == "letters collected"
    assert norm_title("Letters (Collected)") == "letters"


def ingest_book(session, ctx, source: str, external_id: str, title: str, book: dict | None = None,
                **payload) -> service.IngestResult:
    item = {"external_id": external_id, "title": title, **payload}
    if book is not None:
        item["ext"] = {"book": book}
    [res] = service.ingest(session, ctx, batch(source, [item]), system=True, media_type="book")
    session.commit()
    return res


@pytest.fixture
def seeded(session, ctx):
    custom.seed(session)
    session.commit()
    return session, ctx


def test_book_record_stores_the_type_aware_key_and_its_creators(seeded):
    session, ctx = seeded
    res = ingest_book(session, ctx, "igdb", "1", "A Special Edition", {"authors": AUTHORS})
    row = session.get(Canonical, res.canonical_id)
    assert row.norm_title == "a special edition" and row.media_type == "book"
    assert row.view["developer"] == "Ione Marsh"
    creators = session.scalars(select(BookCreator).where(BookCreator.canonical_id == row.id)
                               .order_by(BookCreator.position)).all()
    assert [(c.position, c.name, c.norm_name, c.role) for c in creators] == [
        (0, "Ione Marsh", "ione marsh", "author"), (1, "Teo Varga", "teo varga", "translator")]


def test_book_matching_uses_first_author_and_year_and_ignores_platform(seeded):
    session, ctx = seeded
    first = ingest_book(session, ctx, "igdb", "1", "The Lantern Keeper", {"authors": [{"name": "Ione Marsh"}]},
                        platforms=["Paperback"], ext=None)
    session.get(Canonical, first.canonical_id)
    payload = {"title": "The Lantern Keeper", "platforms": ["Ebook"],
               "ext": {"book": {"authors": [{"name": "Ione Marsh"}]},
                       "release": {"first": {"date": "1999", "precision": "year"}}}}
    facts = matching.Facts.of_payload("rawg", "9", payload, "book")
    assert facts.developer == "Ione Marsh" and facts.release_date == "1999"
    cands = matching.title_candidates(session, ctx.settings, facts)
    assert cands and cands[0].id == first.canonical_id
    names = [e["signal"] for e in cands[0].evidence]
    assert "developer_equal" in names and "platform_overlap" not in names
    other = matching.Facts.of_payload("rawg", "9", {**payload, "ext": {"book": {"authors": [{"name": "Pell Okafor"}]}}},
                                      "book")
    worse = matching.title_candidates(session, ctx.settings, other)  # another first author counts against it
    assert not worse or ("developer_mismatch" in [e["signal"] for e in worse[0].evidence]
                         and worse[0].score < cands[0].score)


# ---- achievements (T-B7, the book part) -------------------------------------------------------------------

ITEMS = [{"name": "First page", "description": "Open it."}]


def curator(node: Node) -> dict[str, str]:
    with node.st.maker() as s:
        _, secret = auth.create_token(s, "curator", ["read", "contribute", "curate"])
        s.commit()
    return {"Authorization": f"Bearer {secret}"}


def publish_book(node: Node, title: str, book: dict | None = BOOK, external_id: str | None = None) -> str:
    with node.st.maker() as s:
        item = {"external_id": external_id or title, "title": title}
        if book is not None:
            item["ext"] = {"book": book}
        [res] = service.ingest(s, node.st.ctx, batch("manual", [item], kind="manual"), curator=True,
                               media_type="book")
        s.commit()
    return res.canonical_id


def test_t_b7_achievements_do_not_apply_to_books(tmp_path):
    node = Node(tmp_path, "n", "authority", emit_protocol="1.5")
    cid = publish_book(node, "The Lantern Keeper")
    headers = curator(node)
    r = node.client.put(f"/api/v2/games/{cid}/achievements", json={"items": ITEMS}, headers=headers)
    assert r.status_code == 400 and "achievements do not apply to book records" in r.text
    got = node.client.get(f"/api/v2/games/{cid}/achievements", headers=headers).json()
    assert got["applicable"] is False and got["results"] == []
    with node.st.maker() as s, pytest.raises(facets.AchievementsNotApplicable):
        facets.set_achievements(s, node.st.ctx, cid, "curator", ITEMS, redistributable=True)


# ---- federation of a curated book and its locks ------------------------------------------------------------

def test_curated_book_federates_with_book_v1_and_the_follower_holds_it(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    follower = Node(tmp_path, "follower", "mirror")
    cid = publish_book(authority, "The Lantern Keeper")
    with authority.st.maker() as s:
        entry = s.scalars(select(ChangeLog).where(ChangeLog.kind == "obs")).one()
        assert entry.body["_requires"] == ["book-v1"] and entry.body["media_type"] == "book"
    pull(follower, authority)
    assert follower.failures() == []
    with follower.st.maker() as s:
        row = s.get(Canonical, cid)
        assert row.media_type == "book" and row.view["authors"][0]["name"] == "Ione Marsh"
        assert row.view["editions"][0]["isbn_13"] == ISBN_A and row.view["developer"] == "Ione Marsh"
        from atlas.models import Crosswalk

        assert s.get(Crosswalk, ("isbn", ISBN_A)).canonical_id == cid
        assert s.scalar(select(func.count()).select_from(BookCreator).where(BookCreator.canonical_id == cid)) == 2


def test_curated_book_data_is_refused_below_unified(tmp_path):
    node = Node(tmp_path, "n", "authority")  # emit protocol 1.0 (the default)
    r = node.client.post("/api/v2/observations", headers=curator(node), json={
        "connection": {"source_kind": "manual", "source": "manual", "source_scope": "default"},
        "media_type": "book", "observations": [{"external_id": "x", "title": "T", "ext": {"book": BOOK}}]})
    assert r.status_code == 409 and "needs federation protocol 1.5" in r.text
    with node.st.maker() as s:
        assert s.scalar(select(func.count()).select_from(Canonical)) == 0  # nothing stored
    # a local node never signs anything: no gate
    local = Node(tmp_path, "l", "local")
    publish_book(local, "The Lantern Keeper")


def test_book_locks_validate_sign_and_replicate(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    follower = Node(tmp_path, "follower", "mirror")
    cid = publish_book(authority, "The Lantern Keeper")
    headers = curator(authority)
    locked = [{"name": "Pell Okafor", "role": "editor"}]
    r = authority.client.put(f"/api/v2/games/{cid}/locks/authors", json={"value": locked}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["authors"] == locked and r.json()["developer"] == "Pell Okafor"
    r = authority.client.put(f"/api/v2/games/{cid}/locks/page_count", json={"value": 300}, headers=headers)
    assert r.status_code == 200 and r.json()["page_count"] == 300
    for field, value in (("authors", [{"name": "X", "role": "ghostwriter"}]), ("authors", "Pell Okafor"),
                         ("page_count", 0), ("page_count", "300"), ("original_language", "English"),
                         ("subtitle", 12)):
        r = authority.client.put(f"/api/v2/games/{cid}/locks/{field}", json={"value": value}, headers=headers)
        assert r.status_code in (400, 422), (field, value, r.text)
    pull(follower, authority)
    assert follower.failures() == []
    with follower.st.maker() as s:
        view = s.get(Canonical, cid).view
        assert view["authors"] == locked and view["page_count"] == 300


def test_book_lock_refused_below_unified(tmp_path):
    node = Node(tmp_path, "n", "authority", emit_protocol="1.5")
    cid = publish_book(node, "The Lantern Keeper")
    with node.st.maker() as s:
        node.st.ctx.settings.emit_protocol = "1.4"
        try:
            with pytest.raises(service.AtlasError, match="needs federation protocol 1.5") as exc:
                service.set_lock(s, node.st.ctx, cid, "subtitle", "Another")
            assert exc.value.code == "emit_protocol"
            service.set_lock(s, node.st.ctx, cid, "subtitle", "Another", local_only=True)  # never signed: allowed
        finally:
            node.st.ctx.settings.emit_protocol = "1.5"


# ---- relation.position (T-B11) -----------------------------------------------------------------------------

def make_series(node: Node, title: str) -> str:
    with node.st.maker() as s:
        [res] = service.ingest(s, node.st.ctx, batch("manual", [{"external_id": title, "title": title}],
                                                      kind="manual"), curator=True, media_type="series")
        s.commit()
    return res.canonical_id


def test_t_b11_series_position_is_signed_applied_and_shown(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    follower = Node(tmp_path, "follower", "mirror")
    book, series = publish_book(authority, "The Lantern Keeper"), make_series(authority, "The Lamp Cycle")
    headers = curator(authority)
    r = authority.client.put(f"/api/v2/games/{book}/relations", headers=headers,
                             json={"relations": [{"to": series, "type": "series_member", "position": "1"}]})
    assert r.status_code == 200, r.text
    assert r.json()["relations"]["members_of"][0]["position"] == "1"
    members = authority.client.get(f"/api/v2/games/{series}", headers=headers).json()["relations"]["members"]
    assert members == [{"id": book, "type": "series_member", "position": "1", "title": "The Lantern Keeper",
                        "media_type": "book"}]
    # a new position: `end` + `add` of the same pair, no new op
    r = authority.client.put(f"/api/v2/games/{book}/relations", headers=headers,
                             json={"relations": [{"to": series, "type": "series_member", "position": "2.5"}]})
    assert r.status_code == 200, r.text
    with authority.st.maker() as s:
        bodies = [c.body for c in s.scalars(select(ChangeLog).where(ChangeLog.kind == "relation")
                                            .order_by(ChangeLog.seq))]
    assert [(b["op"], b.get("position")) for b in bodies] == [("add", "1"), ("end", None), ("add", "2.5")]
    pull(follower, authority)
    assert follower.failures() == []
    with follower.st.maker() as s:
        rows = s.scalars(select(Relation).where(Relation.from_id == book).order_by(Relation.created_at)).all()
        assert [(r.position, r.ended_at is None) for r in rows] == [("1", False), ("2.5", True)]
    edges = follower.client.get(f"/api/v2/games/{book}/relations", headers=curator(follower)).json()["edges"]
    assert edges == [{"from": book, "to": series, "type": "series_member", "position": "2.5"}]


def test_t_b11_position_refused_on_other_types_and_quarantined_on_followers(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    follower = Node(tmp_path, "follower", "mirror")
    a, b = publish_book(authority, "The Lantern Keeper"), publish_book(authority, "The Lantern Keeper Abridged",
                                                                       external_id="abr")
    r = authority.client.put(f"/api/v2/games/{b}/relations", headers=curator(authority),
                             json={"relations": [{"to": a, "type": "version_of", "position": "1"}]})
    assert r.status_code == 400 and "only a series_member relation has a position" in r.text
    with pytest.raises(service.AtlasError):
        relations.clean_position("series_member", "x" * 17)
    authority.emit("relation", {"op": "add", "from": b, "to": a, "type": "version_of", "source": "curator",
                                "position": "1"})
    pull(follower, authority)
    assert [(k, d, why) for _seq, k, d, why in follower.failures()] == [("relation", "quarantined", "invalid")]


def test_t_b11_relations_without_position_are_signed_as_before(tmp_path):
    node = Node(tmp_path, "n", "authority", emit_protocol="1.5")
    book, series = publish_book(node, "The Lantern Keeper"), make_series(node, "The Lamp Cycle")
    with node.st.maker() as s:
        relations.add(s, node.st.ctx, book, series, "series_member")
        s.commit()
        body = s.scalars(select(ChangeLog.body).where(ChangeLog.kind == "relation")).one()
    assert body == {"op": "add", "from": book, "to": series, "type": "series_member", "source": "curator"}


def test_web_add_keeps_a_series_position_and_can_set_one(tmp_path):
    """Review fix: re-adding a positioned series_member from the web form (position left blank) keeps its position
    instead of silently clearing it; the form can set one, and the list shows it."""
    from .test_relations import _reviewer_site
    from .test_web import token

    site = _reviewer_site(tmp_path)
    ids = site.ids
    member, series = ids["Web Game"], ids["Web Series"]
    try:
        t = token(site, f"/game/{member}/relations")
        r = site.post(f"/game/{member}/relations/add",
                      data={"csrf": t, "to": series, "type": "series_member", "position": "3"})
        assert r.status_code == 303, r.text
        page = site.get(f"/game/{member}/relations").text
        assert "data-relation-position>#3<" in page
        r = site.post(f"/game/{member}/relations/add", data={"csrf": t, "to": series, "type": "series_member"})
        assert r.status_code == 303, r.text
        with site.st.maker() as s:
            rows = s.scalars(select(Relation).where(Relation.from_id == member)).all()
            assert [(row.position, row.ended_at is None) for row in rows] == [("3", True)]  # kept, nothing ended
        r = site.post(f"/game/{member}/relations/add",
                      data={"csrf": t, "to": series, "type": "series_member", "position": "4"})
        assert r.status_code == 303
        with site.st.maker() as s:
            active = s.scalars(select(Relation).where(Relation.from_id == member, Relation.ended_at.is_(None))).all()
            assert [row.position for row in active] == ["4"]
    finally:
        site.__exit__(None, None, None)
