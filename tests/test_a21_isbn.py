"""CONTRACT-C15 §2.5 (A21): the ISBN normaliser (T-B1), the per-edition `isbn` crosswalk rows and the
`isbn_conflict` review (T-B2). Synthetic books only: no third-party catalogue data is used."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from atlas import books, metrics, service
from atlas.contract import BookEditionV1
from atlas.models import Crosswalk, ReviewItem
from atlas.normalize import isbn10, isbn13, looks_like_isbn

from .conftest import batch

# Synthetic but valid ISBNs (check digits computed by hand): a 978 book with its ISBN-10, and a 979 book.
ISBN13_A, ISBN10_A = "9780000000002", "0000000000"
ISBN13_B = "9781234567897"
ISBN10_B = "123456789X"  # -> 978-1-234-56789-7
ISBN13_979 = "9791234567896"


def test_t_b1_isbn13_check_digit_and_isbn10_conversion():
    assert isbn13(ISBN13_B) == ISBN13_B
    assert isbn13("978-1-234-56789-7") == ISBN13_B  # hyphens
    assert isbn13(" 978 1234 56789 7 ") == ISBN13_B  # spaces
    assert isbn13("978‑1234567897") == ISBN13_B  # Unicode dashes (here a non-breaking hyphen) as well
    assert isbn13("978/1234567897") is None  # anything else is not a separator
    assert isbn13("123456789x") == ISBN13_B  # ISBN-10 -> 978 + 9 digits + a recomputed check digit; x upper-cased
    assert isbn13("1-234-56789-X") == ISBN13_B
    assert isbn10(ISBN13_B) == ISBN10_B
    assert isbn13(ISBN10_A) == ISBN13_A and isbn10(ISBN13_A) == ISBN10_A


def test_t_b1_979_has_no_isbn10_and_bad_digits_are_never_guessed():
    assert isbn13(ISBN13_979) == ISBN13_979
    assert isbn10(ISBN13_979) is None
    assert isbn13("9781234567890") is None  # wrong check digit: dropped, never corrected
    assert isbn13("1234567890") is None  # wrong ISBN-10 check digit
    assert isbn13("9771234567898") is None  # 977 is an ISSN prefix, not an ISBN
    assert isbn13("97812345678") is None and isbn13("") is None and isbn13(None) is None and isbn13(978) is None
    assert looks_like_isbn("978-1-234-56789-7") and not looks_like_isbn("Dune")


@pytest.fixture
def counted(monkeypatch):
    from atlas import instruments

    monkeypatch.setattr(metrics, "ENABLED", True)
    instruments.book_isbn_rejected.series.clear()
    yield lambda field: instruments.book_isbn_rejected.series.get((field,), 0.0)
    instruments.book_isbn_rejected.series.clear()


def test_t_b1_edition_normalises_drops_and_counts(counted):
    e = BookEditionV1.model_validate({"isbn_13": "978-1-234-56789-7"})
    assert (e.isbn_13, e.isbn_10) == (ISBN13_B, ISBN10_B)  # the ISBN-10 is derived
    e = BookEditionV1.model_validate({"isbn_10": "123456789x"})
    assert (e.isbn_13, e.isbn_10) == (ISBN13_B, ISBN10_B)  # the ISBN-13 is derived
    e = BookEditionV1.model_validate({"isbn_13": ISBN13_979})
    assert (e.isbn_13, e.isbn_10) == (ISBN13_979, None)
    e = BookEditionV1.model_validate({"isbn_13": "9781234567890", "name": "Paperback"})
    assert (e.isbn_13, e.isbn_10, e.name) == (None, None, "Paperback")  # dropped, the edition is kept
    assert counted("isbn_13") == 1
    e = BookEditionV1.model_validate({"isbn_13": ISBN10_B})  # an ISBN-10 in the ISBN-13 field is not guessed at
    assert e.isbn_13 is None and counted("isbn_13") == 2
    e = BookEditionV1.model_validate({"isbn_13": ISBN13_B, "isbn_10": ISBN10_A})  # two different books
    assert (e.isbn_13, e.isbn_10) == (ISBN13_B, ISBN10_B) and counted("isbn_10") == 1


# ---- crosswalk rows and conflicts (T-B2) ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _types(request):
    if "session" in request.fixturenames:
        from atlas import custom

        session = request.getfixturevalue("session")
        custom.seed(session)
        session.commit()


def _book(external_id: str, title: str, editions: list[dict], **extra) -> dict:
    return {"external_id": external_id, "title": title, "ext": {"book": {"editions": editions, **extra}}}


def _ingest(session, ctx, source: str, items: list[dict]) -> list:
    out = service.ingest(session, ctx, batch(source, items), system=True, media_type="book")
    session.commit()
    return out


def _isbn_rows(session) -> dict[str, tuple[str, str]]:
    return {r.external_id: (r.canonical_id, r.variant)
            for r in session.scalars(select(Crosswalk).where(Crosswalk.namespace == books.ISBN_NAMESPACE))}


THREE = [{"name": "Paperback", "isbn_13": ISBN13_A, "format": "paperback"},
         {"name": "Hardcover", "isbn_13": ISBN13_B, "format": "hardcover"},
         {"name": "Ebook", "isbn_13": ISBN13_979, "format": "ebook"}]


def test_t_b2_three_edition_isbns_resolve_to_one_work(session, ctx):
    [res] = _ingest(session, ctx, "igdb", [_book("w1", "The Lantern Keeper", THREE)])
    rows = _isbn_rows(session)
    assert {k: v[0] for k, v in rows.items()} == {ISBN13_A: res.canonical_id, ISBN13_B: res.canonical_id,
                                                  ISBN13_979: res.canonical_id}
    assert rows[ISBN13_B][1] == "edition:hardcover"  # variant keys: many ISBNs point at one work
    from atlas.resolve import classify

    for isbn in (ISBN13_A, ISBN13_B, ISBN13_979):
        r = classify(session, ctx.settings, namespace="", external_id="", provider_ids={"isbn": isbn}, title=None,
                     release_date=None, platform=None, media_type="book")
        assert r.canonical_id == res.canonical_id, isbn


def test_t_b2_rows_follow_the_current_observation(session, ctx):
    [res] = _ingest(session, ctx, "igdb", [_book("w1", "The Lantern Keeper", THREE)])
    _ingest(session, ctx, "igdb", [_book("w1", "The Lantern Keeper", THREE[:1])])  # supersedes the first
    assert set(_isbn_rows(session)) == {ISBN13_A}
    assert _isbn_rows(session)[ISBN13_A][0] == res.canonical_id


def test_t_b2_host_reports_never_register_an_isbn(session, ctx):
    service.ingest(session, ctx, batch("someone", [_book("copy", "The Lantern Keeper", THREE)], kind="host"),
                   media_type="book")
    session.commit()
    assert _isbn_rows(session) == {}


def test_t_b2_conflicting_isbn_opens_a_review_and_never_merges(session, ctx):
    [first] = _ingest(session, ctx, "igdb", [_book("w1", "The Lantern Keeper", THREE[:2])])
    # another work (an omnibus) names one of the same ISBNs: publishers reuse ISBNs
    [second] = _ingest(session, ctx, "rawg", [_book("w9", "Tides of an Unlit Sea",
                                                    [{"name": "Omnibus", "isbn_13": ISBN13_B}],
                                                    authors=[{"name": "Ione Marsh"}])])
    assert second.canonical_id != first.canonical_id
    rows = _isbn_rows(session)
    assert rows[ISBN13_B][0] == first.canonical_id  # the key stays where it was
    items = session.scalars(select(ReviewItem).where(ReviewItem.reason == books.ISBN_CONFLICT)).all()
    assert len(items) == 1 and items[0].kind == "id_conflict" and items[0].status == "open"
    assert items[0].canonical_id == second.canonical_id and items[0].candidates[0][0] == first.canonical_id
    assert service.follow(session, second.canonical_id) == second.canonical_id  # nothing merged
    # the same claim again refreshes the one open item
    _ingest(session, ctx, "rawg", [_book("w9", "Tides of an Unlit Sea", [{"name": "Omnibus 2", "isbn_13": ISBN13_B}],
                                         authors=[{"name": "Ione Marsh"}])])
    assert len(session.scalars(select(ReviewItem).where(ReviewItem.reason == books.ISBN_CONFLICT)).all()) == 1


def test_record_carries_the_first_edition_isbn_only(session, ctx):
    """C15 §2.5: a `record` carries at most one `isbn`, the ISBN-13 of the first edition in `editions[]` order."""
    from atlas import feed
    from atlas.models import Canonical

    [res] = _ingest(session, ctx, "igdb", [_book("w1", "The Lantern Keeper", THREE)])
    row = session.get(Canonical, res.canonical_id)
    first = next(e["isbn_13"] for e in row.view["editions"] if e.get("isbn_13"))
    body = feed.record_body(session, ctx.settings, row, "publish")
    assert body["media_type"] == "book" and body["provider_ids"]["isbn"] == first
    assert sum(1 for k in body["provider_ids"] if k == "isbn") == 1 and body["provider_ids"]["igdb"] == "w1"


def test_resolve_endpoint_answers_the_work_for_any_edition_isbn(tmp_path):
    """T-B2 through the HTTP contract: `POST /resolve` with `provider_ids: {"isbn": ...}` (hyphens normalised by the
    caller or not) answers the work."""
    from fastapi.testclient import TestClient

    from atlas import main

    from .conftest import build_settings

    admin = "a" * 40
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/t.db", bootstrap_admin_token=admin))
    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {admin}"}
        body = {"connection": {"source_kind": "metadata", "source": "igdb", "source_scope": "s"}, "media_type": "book",
                "observations": [_book("w1", "The Lantern Keeper", THREE)]}
        r = client.post("/api/v2/observations", json=body, headers=h)
        assert r.status_code == 200, r.text
        cid = r.json()["results"][0]["atlas"]["canonical_id"]
        res = client.post("/api/v2/resolve", headers=h, json={"items": [
            {"provider_ids": {"isbn": isbn}, "media_type": "book"} for isbn in (ISBN13_A, ISBN13_B, ISBN13_979)
        ]}).json()["results"]
        assert [x["canonical_id"] for x in res] == [cid, cid, cid]
        assert res[0]["game"]["media_type"] == "book"


def test_detached_observation_takes_its_edition_isbns_along(session, ctx):
    [first] = _ingest(session, ctx, "igdb", [_book("w1", "The Lantern Keeper", THREE[:1])])
    [second] = _ingest(session, ctx, "rawg", [_book("r1", "The Lantern Keeper", THREE[1:2],
                                                    ) | {"provider_ids": {"igdb": "w1"}}])
    assert second.canonical_id == first.canonical_id  # matched through the igdb id: one work
    assert _isbn_rows(session)[ISBN13_B][0] == first.canonical_id
    from atlas.models import Observation

    moved = session.scalars(select(Observation).where(Observation.source == "rawg")).one()
    new = service.detach_observation(session, ctx, moved.id)
    session.commit()
    rows = _isbn_rows(session)
    assert rows[ISBN13_A][0] == first.canonical_id and rows[ISBN13_B][0] == new.id


def test_duplicate_isbn_inside_one_work_is_one_row(session, ctx):
    twice = [{"name": "Paperback", "isbn_13": ISBN13_A}, {"name": "Paperback (2nd printing)", "isbn_13": ISBN13_A}]
    [res] = _ingest(session, ctx, "igdb", [_book("w1", "The Lantern Keeper", twice)])
    assert _isbn_rows(session) == {ISBN13_A: (res.canonical_id, "edition:paperback")}
    view = service.get_canonical(session, res.canonical_id)[0].view
    assert [e["isbn_13"] for e in view["editions"]] == [ISBN13_A]
