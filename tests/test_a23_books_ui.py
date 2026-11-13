"""CONTRACT-C15 §5 (A23): books in the Atlas web interface and search.

T-B14 (ISBN search and the author filter), T-B15 (book dates through the shared formatter) and the page tests of the
book record page, the curator import with an ISBN box, the authors editor, the series picker, the local first-run
"Add a book by ISBN" and the Open Library attribution. Every book, author and ISBN here is invented; Open Library is a
fake transport over the synthetic A22 fixtures (no network)."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import main, service
from atlas.models import BookCreator, Canonical, Lock, Observation, Relation
from atlas.normalize import isbn10

from .conftest import batch, build_settings
from .test_a22_openlibrary import ISBN as OL_ISBN
from .test_a22_openlibrary import W1, fixture_client, ol
from .test_web import _wait_for_startup_replay, csrf_in, fresh, signup

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "atlas" / "web" / "templates"
ISBN_A = "9781234567897"   # synthetic; its ISBN-10 is 123456789X
ISBN_B = "9780000000002"
AUTHORS = [{"name": "Maren Hérbertsdóttir", "role": "author", "ids": {"openlibrary": "OL9100001A"}},
           {"name": "Ilse Varn", "role": "author"},
           {"name": "Teo Varga", "role": "translator"}]


def _site(tmp_path, **kw):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/books.db", bootstrap_admin_token="b" * 40,
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False,
                       public_url="http://localhost:8000", **kw)
    app = main.create_app(s)
    st = app.state.atlas
    st.providers.client = fixture_client(calls := [])
    st.providers.providers["openlibrary"] = ol()
    client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
    client.st, client.st_app, client.calls = st, app, calls
    return client


@pytest.fixture
def books(tmp_path):
    """A local node (the role a personal Atlas has), with Open Library on through the fake transport."""
    with _site(tmp_path, role="local") as c:
        _wait_for_startup_replay(c.st)
        yield c


def add_book(st, ext_id: str, title: str, *, authors=AUTHORS, first=None, editions=(), source="openlibrary",
             subtitle=None, pages=None, language=None, subjects=(), series=None) -> str:
    book: dict = {"authors": list(authors)}
    if subtitle:
        book["subtitle"] = subtitle
    if pages:
        book["page_count"] = pages
    if language:
        book["original_language"] = language
    if series:
        book["series"] = series
    if editions:
        book["editions"] = list(editions)
    ext: dict = {"book": book}
    if first:
        ext["release"] = {"first": first}
    item = {"external_id": ext_id, "title": title, "provider_ids": {source: ext_id}, "ext": ext}
    if subjects:
        item["facts"] = {"features": list(subjects)}
    with st.maker() as s:
        [r] = service.ingest(s, st.ctx, batch(source, [item]), system=True, media_type="book")
        s.commit()
    return r.canonical_id


def add_game(st, ext_id: str, title: str, developer: str) -> str:
    with st.maker() as s:
        [r] = service.ingest(s, st.ctx, batch("igdb", [{"external_id": ext_id, "title": title,
                                                        "facts": {"developer": developer}}]), system=True)
        s.commit()
    return r.canonical_id


def lantern(st) -> str:
    return add_book(st, "OL9100001W", "The Lantern Keeper", subtitle="A Novel of Small Lights", pages=412,
                    language="en", subjects=("Lighthouses", "Fiction"), first={"date": "2005-08-14", "precision": "day"},
                    series={"name": "The Lamp Cycle", "position": "2"},
                    editions=[{"key": "OL9100001M", "name": "Paperback, Harbour 2006", "isbn_13": ISBN_A,
                               "format": "paperback", "publisher": "Harbour Press",
                               "published": {"date": "2006-02", "precision": "month"}, "pages": 528, "language": "en"},
                              {"key": "OL9100002M", "name": "Hardcover, Harbour 2007", "isbn_13": ISBN_B,
                               "format": "hardcover", "publisher": "Harbour Press",
                               "published": {"date": "2007", "precision": "year"}, "pages": 512}])


def text_of(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def ids_of(results) -> list[str]:
    return [r.id for r in results]


# ---- T-B14: ISBN search and the author filter -------------------------------------------------------------------

def test_isbn_search_and_author_filter(books):
    st = books.st
    cid = lantern(st)
    other = add_book(st, "OL9100009W", "Harbour Lights", authors=[{"name": "Pell Arden", "role": "author"}])
    game = add_game(st, "777", "Herbert Island", "Herb Studio")
    with st.maker() as s:
        # q = a valid ISBN-13 or ISBN-10 (hyphens and spaces allowed) -> that work alone, through the crosswalk
        for q in (ISBN_A, "978-1-234-56789-7", "123456789X", "1-23-456789-x", ISBN_B, " 978 0 00 000000 2 "):
            assert ids_of(service.search(s, q)) == [cid], q
        assert isbn10(ISBN_A) == "123456789X"
        assert ids_of(service.search(s, ISBN_A, media_type="book")) == [cid]
        assert service.search(s, ISBN_A, media_type="game") == []  # the type filter still applies
        assert service.search(s, ISBN_A, offset=1) == []  # one work, one page
        # the ISBN short-circuit validates like any search and applies the author and release filters (A23 fix pass)
        with pytest.raises(service.AtlasError, match="type=book"):
            service.search(s, ISBN_A, media_type="game", author="herb")
        with pytest.raises(service.AtlasError, match="sort"):
            service.search(s, ISBN_A, sort="nonsense")
        with pytest.raises(service.AtlasError):
            service.search(s, ISBN_A, released_from="not-a-date")
        assert ids_of(service.search(s, ISBN_A, author="herb")) == [cid]
        assert service.search(s, ISBN_A, author="pell") == []  # the author does not match
        assert ids_of(service.search(s, ISBN_A, released_from="2005", released_to="2005")) == [cid]
        assert service.search(s, ISBN_A, released_from="2010") == []
        # an invalid check digit is not an ISBN: an ordinary title search (which finds nothing here)
        assert service.search(s, "9781234567898") == []
        # author= prefix-matches any word of a creator's name, casefold, without diacritics
        for author in ("herb", "HERB", "hérb", "Herbertsdottir", "maren h", "Maren Hérbertsdóttir", "varga"):
            assert ids_of(service.search(s, "", media_type="book", author=author)) == [cid], author
        assert service.search(s, "", media_type="book", author="erb") == []  # a prefix, not a substring
        assert ids_of(service.search(s, "", media_type="book", author="pell")) == [other]
        # only for books: no type means books; another type is refused
        assert ids_of(service.search(s, "", author="herb")) == [cid]
        with pytest.raises(service.AtlasError, match="type=book"):
            service.search(s, "", media_type="game", author="herb")
        # a free q also matches authors, for type=book only
        assert ids_of(service.search(s, "herbertsdottir", media_type="book")) == [cid]
        assert ids_of(service.search(s, "herbert")) == [game]  # titles only without type=book
        assert set(ids_of(service.search(s, "lantern", media_type="book", author="maren"))) == {cid}
        rows = s.scalars(select(BookCreator).where(BookCreator.canonical_id == cid)
                         .order_by(BookCreator.position)).all()
        assert [(r.position, r.norm_name, r.role) for r in rows] == [
            (0, "maren herbertsdottir", "author"), (1, "ilse varn", "author"), (2, "teo varga", "translator")]

    # API: the same rules over HTTP, and author alone is enough of a filter
    tok = books.post("/api/v2/tokens", headers={"Authorization": "Bearer " + "b" * 40},
                     json={"name": "t", "scopes": ["read"]}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert [g["id"] for g in books.get("/api/v2/games", params={"q": "978-1-234-56789-7"}, headers=h)
            .json()["results"]] == [cid]
    assert [g["id"] for g in books.get("/api/v2/games", params={"type": "book", "author": "herb"}, headers=h)
            .json()["results"]] == [cid]
    assert books.get("/api/v2/games", params={"type": "game", "author": "herb"}, headers=h).status_code == 400
    assert books.get("/api/v2/games", params={"q": ISBN_A, "type": "game", "author": "x"}, headers=h).status_code == 400
    assert books.get("/api/v2/games", params={"q": ISBN_A, "author": "pell"}, headers=h).json()["results"] == []
    # web: the author box appears on the Books listing and filters it
    page = books.get("/search", params={"type": "book", "author": "varn"})
    assert page.status_code == 200 and 'name="author"' in page.text and "The Lantern Keeper" in page.text
    assert "Harbour Lights" not in page.text
    assert 'name="author"' not in books.get("/search", params={"type": "game"}).text
    assert "The Lantern Keeper" in books.get("/search", params={"q": "123456789X"}).text


def test_book_creator_rebuilt_with_the_view_and_bounded(books):
    st = books.st
    thirty = [{"name": f"Synthetic Writer {i:02d}", "role": "author"} for i in range(30)]
    cid = add_book(st, "OL9100003W", "Thirty Hands", authors=thirty)
    with st.maker() as s:
        assert s.scalar(select(func.count()).select_from(BookCreator).where(BookCreator.canonical_id == cid)) == 30
        # a curator lock replaces the list; the rows follow the rebuilt view
        service.set_lock(s, st.ctx, cid, "authors", [{"name": "Ona Quill", "role": "editor"}], local_only=True)
        s.commit()
        rows = s.scalars(select(BookCreator).where(BookCreator.canonical_id == cid)).all()
        assert [(r.name, r.role) for r in rows] == [("Ona Quill", "editor")]
        assert ids_of(service.search(s, "", media_type="book", author="quill")) == [cid]
        assert service.search(s, "", media_type="book", author="synthetic") == []


# ---- T-B15: dates through the shared formatter -------------------------------------------------------------------

def test_book_page_dates_use_shared_formatter(books):
    cid = lantern(books.st)
    url = f"/game/{cid}"

    def shown(client) -> str:
        r = client.get(url)
        assert r.status_code == 200
        return text_of(r.text)

    page = shown(books)  # default: dd/mm/yyyy, a month as "February 2006", a year as the year
    assert "First published 14/08/2005" in page
    assert "February 2006" in page and "2007" in page
    for style, first, month in (("long", "14 August 2005", "February 2006"), ("iso", "2005-08-14", "2006-02"),
                                ("mdy", "08/14/2005", "February 2006"), ("dmy", "14/08/2005", "February 2006")):
        anon = fresh(books)
        anon.cookies.set("atlas_date", style)
        page = shown(anon)
        assert f"First published {first}" in page, style
        assert month in page, style
    # a signed-in viewer's own preference wins
    signup(books, "alice")
    t = csrf_in(books, "/me/settings")
    assert books.post("/prefs/date-format", data={"csrf": t, "value": "iso"}).status_code in (200, 303)
    assert "First published 2005-08-14" in shown(books)
    # a year-only first publication is the year
    year_only = add_book(books.st, "OL9100004W", "Tidewater", first={"date": "1999", "precision": "year"})
    assert "First published 1999" in text_of(fresh(books).get(f"/game/{year_only}").text)


def test_book_templates_hardcode_no_date_format():
    for name in ("_book.html", "_book_editions.html", "book_authors.html", "book_series.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "strftime" not in text and "%Y" not in text and "%d" not in text, name
        assert not re.search(r"dd/mm|mm/dd|yyyy", text, re.I), name
        # an edition date or a first publication is never printed as stored
        assert not re.search(r"\{\{\s*[\w.]*published[\w.]*\s*\}\}", text), name
        assert not re.search(r"\{\{\s*[\w.]*first_release[\w.]*\s*\}\}", text), name


# ---- the book record page ----------------------------------------------------------------------------------------

def test_book_record_page(books):
    st = books.st
    cid = lantern(st)
    html = books.get(f"/game/{cid}").text
    page = text_of(html)
    assert "The Lantern Keeper" in page and "A Novel of Small Lights" in page
    assert re.search(r"by Maren Hérbertsdóttir and Ilse Varn\s*;?\s*translated by Teo Varga", page), page
    assert "Pages 412" in page and "Original language English" in page
    assert "Subjects" in page and "Lighthouses" in page
    # the editions table: format, publisher, date, pages, language and ISBN-13 with a copy button
    assert "data-book-editions" in html
    table = html[html.index("data-book-editions"):]
    table = table[:table.index("</table>")]
    assert "Paperback" in table and "Hardcover" in table and "Harbour Press" in table and "528" in table
    assert f'id="isbn-{ISBN_A}"' in table and f'data-copy="isbn-{ISBN_A}"' in table
    assert ISBN_A in table and ISBN_B in table
    # mobile cards show no label for a value an edition does not have: the empty cell is marked and hidden
    assert '<td data-label="Language" class="empty">' in table  # the hardcover has no language
    paperback = next(row for row in table.split("<tr ") if "528" in row)
    assert '<td data-label="Language" class="empty">' not in paperback and 'class="empty"' not in paperback
    # the header search box on every page advertises author and ISBN search
    assert 'placeholder="Search by title, author or ISBN"' in html
    # attribution, linked to the work page
    assert "Book data from" in page and 'href="https://openlibrary.org/works/OL9100001W"' in html
    # no achievements for books
    assert 'id="achievements"' not in html and "Achievements" not in page
    # the type pill: books are browsable from the catalogue tabs and the home page
    assert 'href="/search?type=book"' in books.get("/search").text
    assert 'href="/search?type=book"' in books.get("/").text


def test_book_page_without_open_library_data_has_no_attribution(books):
    cid = add_book(books.st, "c1", "Quiet Ledger", source="manual", authors=[{"name": "Ona Quill"}])
    page = books.get(f"/game/{cid}").text
    assert "Book data from" not in page and "by Ona Quill" in text_of(page)


# ---- curator import with an ISBN box -----------------------------------------------------------------------------

def test_import_page_has_an_isbn_box_that_imports_straight_away(books):
    signup(books, "alice")  # the first account administers the node
    page = books.get("/import", params={"provider": "openlibrary"})
    assert page.status_code == 200 and 'name="isbn"' in page.text and 'action="/import/isbn"' in page.text
    assert "Title, author or ISBN" in page.text
    t = csrf_in(books, "/import")
    bad = books.post("/import/isbn", data={"csrf": t, "provider": "openlibrary", "isbn": "978-0-00-990011-3"})
    assert bad.status_code == 400 and "not a valid ISBN" in bad.text
    assert not books.calls  # refused before any request
    got = books.post("/import/isbn", data={"csrf": t, "provider": "openlibrary", "isbn": OL_ISBN[:3] + "-" + OL_ISBN[3:]})
    assert got.status_code == 200, got.text
    assert "Imported" in got.text and "The Salt Cartographer" in got.text
    with books.st.maker() as s:
        row = s.scalars(select(Canonical).where(Canonical.media_type == "book")).one()
        assert row.view["provider_ids"]["openlibrary"] == W1
    # a search with an ISBN in the one box is an identifier lookup: one row to import
    found = books.get("/import", params={"provider": "openlibrary", "q": OL_ISBN})
    assert found.status_code == 200 and found.text.count('name="external_id"') == 1
    assert "Ione Marsh" in found.text  # the author column
    # a game provider has no ISBN box, and its search box asks for a game title
    assert "steam" in books.st.providers.providers
    steam = books.get("/import", params={"provider": "steam"}).text
    assert 'name="isbn"' not in steam and "Game title" in steam


def test_isbn_import_needs_a_book_provider_and_review_rights(books):
    signup(books, "alice")
    bob = fresh(books)
    signup(bob, "bob")
    t = csrf_in(bob, "/me")
    assert bob.post("/import/isbn", data={"csrf": t, "provider": "openlibrary", "isbn": OL_ISBN}).status_code == 403
    del books.st.providers.providers["openlibrary"]
    t = csrf_in(books, "/import")
    r = books.post("/import/isbn", data={"csrf": t, "provider": "openlibrary", "isbn": OL_ISBN})
    assert r.status_code == 400 and "Open Library" in r.text


# ---- authors editor ----------------------------------------------------------------------------------------------

def test_authors_editor_saves_a_curator_observation_and_can_pin(books):
    st = books.st
    cid = lantern(st)
    signup(books, "alice")
    form = books.get(f"/game/{cid}/authors")
    assert form.status_code == 200 and "Maren Hérbertsdóttir" in form.text and 'name="role.2"' in form.text
    t = csrf_in(books, f"/game/{cid}/authors")
    # reorder (Ilse first), change a role, add one, drop the empty rows
    data = {"csrf": t, "name.0": "Maren Hérbertsdóttir", "role.0": "author", "order.0": "2",
            "olid.0": "OL9100001A",
            "name.1": "Ilse Varn", "role.1": "author", "order.1": "1",
            "name.2": "Teo Varga", "role.2": "translator", "order.2": "3",
            "name.3": "Bryn Oake", "role.3": "illustrator", "order.3": "4",
            "name.4": "", "role.4": "author", "order.4": "5"}
    r = books.post(f"/game/{cid}/authors", data=data)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/game/{cid}")
    with st.maker() as s:
        row = s.get(Canonical, cid)
        assert [(a["name"], a["role"]) for a in row.view["authors"]] == [
            ("Ilse Varn", "author"), ("Maren Hérbertsdóttir", "author"), ("Teo Varga", "translator"),
            ("Bryn Oake", "illustrator")]
        assert row.view["authors"][1]["ids"] == {"openlibrary": "OL9100001A"}  # kept through the editor
        assert row.view["developer"] == "Ilse Varn"
        obs = s.scalars(select(Observation).where(Observation.canonical_id == cid, Observation.source == "curator",
                                                  Observation.is_current.is_(True))).one()
        assert obs.payload["ext"]["book"]["authors"][0]["name"] == "Ilse Varn"
        assert "title" not in obs.payload  # the editor states the authors only
        assert s.get(Lock, (cid, "authors")) is None
    # pin it: the lock beats any provider list
    t = csrf_in(books, f"/game/{cid}/authors")
    r = books.post(f"/game/{cid}/authors", data={**data, "csrf": t, "pin": "1", "name.3": ""})
    assert r.status_code == 303
    with st.maker() as s:
        lk = s.get(Lock, (cid, "authors"))
        assert lk is not None and [a["name"] for a in lk.value] == ["Ilse Varn", "Maren Hérbertsdóttir", "Teo Varga"]
    assert 'checked' in books.get(f"/game/{cid}/authors").text
    # unpinning removes the lock and keeps the curator list
    t = csrf_in(books, f"/game/{cid}/authors")
    assert books.post(f"/game/{cid}/authors", data={**data, "csrf": t, "name.3": ""}).status_code == 303
    with st.maker() as s:
        assert s.get(Lock, (cid, "authors")) is None
    # invalid input is shown back, nothing saved
    t = csrf_in(books, f"/game/{cid}/authors")
    bad = books.post(f"/game/{cid}/authors", data={"csrf": t, "name.0": "", "role.0": "author"})
    assert bad.status_code == 400 and "at least one" in bad.text
    bad = books.post(f"/game/{cid}/authors", data={"csrf": t, "name.0": "X", "role.0": "ghostwriter"})
    assert bad.status_code == 400


def test_authors_editor_is_for_curators_and_books_only(books):
    st = books.st
    cid = lantern(st)
    game = add_game(st, "778", "Some Game", "Studio")
    signup(books, "alice")
    assert books.get(f"/game/{game}/authors").status_code == 404
    bob = fresh(books)
    signup(bob, "bob")
    assert bob.get(f"/game/{cid}/authors").status_code == 403
    # the book page links the editors for curators only
    assert f'href="/game/{cid}/authors"' in books.get(f"/game/{cid}").text
    assert f'href="/game/{cid}/authors"' not in bob.get(f"/game/{cid}").text


def test_authors_editor_on_an_authority_below_the_unified_release(tmp_path):
    with _site(tmp_path, role="authority") as c:  # emit protocol 1.0: book data is refused, not stripped (A21)
        _wait_for_startup_replay(c.st)
        cid = lantern(c.st)
        signup(c, "alice")
        t = csrf_in(c, f"/game/{cid}/authors")
        r = c.post(f"/game/{cid}/authors", data={"csrf": t, "name.0": "Ilse Varn", "role.0": "author"})
        assert r.status_code == 409 and "book data" in r.text
        with c.st.maker() as s:
            assert not s.scalars(select(Observation).where(Observation.source == "curator")).all()


# ---- series picker -----------------------------------------------------------------------------------------------

def test_series_picker_creates_or_links_a_series_with_a_position(books):
    st = books.st
    cid = lantern(st)
    two = add_book(st, "OL9100005W", "The Lamp Wardens", authors=[{"name": "Ilse Varn"}])
    signup(books, "alice")
    page = books.get(f"/game/{cid}/series")
    assert page.status_code == 200
    # the provider's hint fills the form
    assert 'value="The Lamp Cycle"' in page.text and 'value="2"' in page.text
    t = csrf_in(books, f"/game/{cid}/series")
    r = books.post(f"/game/{cid}/series", data={"csrf": t, "name": "The Lamp Cycle", "position": "2"})
    assert r.status_code == 303, r.text
    with st.maker() as s:
        series = s.scalars(select(Canonical).where(Canonical.media_type == "series")).one()
        rel = s.scalars(select(Relation).where(Relation.from_id == cid, Relation.ended_at.is_(None))).one()
        assert (rel.to_id, rel.type, rel.position) == (series.id, "series_member", "2")
    # a second book links the existing record (picked by id) at its own position
    found = books.get(f"/game/{two}/series", params={"q": "lamp"})
    assert f'value="{series.id}"' in found.text
    t = csrf_in(books, f"/game/{two}/series")
    assert books.post(f"/game/{two}/series", data={"csrf": t, "series": series.id, "position": "1"}).status_code == 303
    # creating by the same name again links, never duplicates
    t = csrf_in(books, f"/game/{two}/series")
    assert books.post(f"/game/{two}/series", data={"csrf": t, "name": "the lamp cycle", "position": "1.5"}
                      ).status_code == 303
    with st.maker() as s:
        assert s.scalar(select(func.count()).select_from(Canonical).where(Canonical.media_type == "series")) == 1
        rel = s.scalars(select(Relation).where(Relation.from_id == two, Relation.ended_at.is_(None))).one()
        assert rel.position == "1.5"
    # the book page names the series with the position, linked to the series record
    html = books.get(f"/game/{cid}").text
    assert f'href="/game/{series.id}"' in html and re.search(r"#2\s+in\s*The Lamp Cycle", text_of(html))
    # an invalid position is refused and shown
    t = csrf_in(books, f"/game/{cid}/series")
    bad = books.post(f"/game/{cid}/series", data={"csrf": t, "series": series.id, "position": "x" * 17})
    assert bad.status_code == 400
    assert books.post(f"/game/{cid}/series", data={"csrf": t, "position": "1"}).status_code == 400  # no series


# ---- local first run ---------------------------------------------------------------------------------------------

def test_local_first_run_offers_add_a_book_by_isbn(books):
    anon = books.get("/")
    assert "Add a book by ISBN" in anon.text and 'action="/import/isbn"' not in anon.text  # signed out: sign in
    signup(books, "alice")
    home = books.get("/").text
    assert "Add a game" in home and "Add a book by ISBN" in home and 'action="/import/isbn"' in home
    t = csrf_in(books, "/")
    r = books.post("/import/isbn", data={"csrf": t, "provider": "openlibrary", "isbn": OL_ISBN, "next": "record"})
    assert r.status_code == 303 and r.headers["location"].startswith("/game/atlas:")
    page = books.get(r.headers["location"].split("?")[0]).text
    assert "The Salt Cartographer" in page and "Book data from" in page
    assert "Add a book by ISBN" not in books.get("/").text  # first run only


def test_first_run_book_box_is_local_only(tmp_path):
    with _site(tmp_path, role="authority") as c:
        _wait_for_startup_replay(c.st)
        signup(c, "alice")
        assert "Add a book by ISBN" not in c.get("/").text
