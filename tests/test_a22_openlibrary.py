"""CONTRACT-C15 §3 (A22): the Open Library provider. T-B3 (adapter against the fixtures), T-B4 (the provider's media
type reaches ingest), T-B13 (budget, pacing, User-Agent and `provider_contact`, redirects, media policy).

Offline: every answer comes from `tests/fixtures/openlibrary/` (synthetic books written by
`tools/record_openlibrary_fixtures.py`; Open Library data is not redistributable here, D15.2)."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from atlas import dates, enrich, importer, instruments, media, metrics, service
from atlas.models import Crosswalk
from atlas.providers import Fetcher, ProviderError, build_registry
from atlas.providers.base import PROJECT_URL, ProviderResult, user_agent
from atlas.providers.igdb import Igdb
from atlas.providers.media_policy import policy
from atlas.providers.openlibrary import OpenLibrary, Pacer
from atlas.providers.rawg import Rawg
from atlas.providers.registry import media_types
from atlas.providers.steam import Steam
from atlas.providers.tgdb import TheGamesDb
from tools import record_openlibrary_fixtures as recorder

from . import test_web
from .conftest import build_settings

site = test_web.site  # the web fixture

FIX = Path(__file__).parent / "fixtures" / "openlibrary"
ROUTES = json.loads((FIX / "routes.json").read_text(encoding="utf-8"))
W1, W2, W3, W4, W5 = (f"OL990000000{n}W" for n in range(1, 6))
ISBN, ISBN10_HARD, ISBN_OLD = ROUTES["isbn_paperback"], ROUTES["isbn10_hardcover"], ROUTES["isbn_old"]
FIELDS = "key,title,subtitle,author_name,author_key,first_publish_year,cover_i,edition_count,language"
VERSION_UA = f"atlas-metadata/2.0.0.dev0 (+{PROJECT_URL})"


def answer(request: httpx.Request) -> httpx.Response:
    url = urlsplit(str(request.url))
    if url.netloc != "openlibrary.org":
        return httpx.Response(404)
    q = parse_qs(url.query).get("q", [None])[0]
    for r in ROUTES["routes"]:
        if r["path"] == url.path and r.get("q") == q:
            body = json.loads((FIX / r["file"]).read_text(encoding="utf-8")) if r.get("file") else None
            return httpx.Response(r.get("status", 200), headers=r.get("headers", {}), json=body)
    return httpx.Response(404, json={"error": "notfound"})


def fixture_client(calls: list[httpx.Request] | None = None, extra=None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if extra is not None and (got := extra(request)) is not None:
            return got
        return answer(request)
    return httpx.Client(transport=httpx.MockTransport(handler))


def ol(**kw) -> OpenLibrary:
    return OpenLibrary(pacer=Pacer(0), **kw)


@pytest.fixture
def rejected(monkeypatch):
    monkeypatch.setattr(metrics, "ENABLED", True)
    instruments.book_isbn_rejected.series.clear()
    yield lambda field: instruments.book_isbn_rejected.series.get((field,), 0.0)
    instruments.book_isbn_rejected.series.clear()


# ---- fixtures ---------------------------------------------------------------------------------------------------

def test_fixtures_are_what_the_recorder_writes_and_synthetic():
    assert recorder.drift() == []
    for path in FIX.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        for key in json.loads(text).get("docs", []) + [json.loads(text)]:
            ident = str(key.get("key", "")) if isinstance(key, dict) else ""
            assert not ident or ident.split("/")[-1].startswith("OL99"), (path.name, ident)  # invented ids only
    assert (FIX / "README.md").exists()


# ---- T-B3: the adapter --------------------------------------------------------------------------------------------

def test_search_lists_works_with_small_responses():
    calls: list[httpx.Request] = []
    found = ol().search(fixture_client(calls), "salt cartographer")
    assert [r.external_id for r in found] == [W1, W3]
    first = found[0]
    assert first.title == "The Salt Cartographer" and first.facts["developer"] == "Ione Marsh"
    assert first.release_date is None and first.release == {"first": {"date": "1987", "precision": "year"}}
    assert first.provider_ids == {"openlibrary": W1}
    assert found[1].release is None and found[1].release_date is None
    params = calls[0].url.params
    assert params["fields"] == FIELDS and params["limit"] == "20" and "isbn" not in params["fields"].split(",")
    assert ol().search(fixture_client(), "no such book anywhere") == []


def test_isbn_query_goes_to_the_isbn_lookup():
    calls: list[httpx.Request] = []
    found = ol().search(fixture_client(calls), "978-0-00-990011-2")
    assert [r.external_id for r in found] == [W1] and found[0].provider_ids == {"openlibrary": W1, "isbn": ISBN}
    assert not any(c.url.path == "/search.json" and c.url.params.get("q") == ISBN for c in calls)
    assert calls[0].url.path == f"/isbn/{ISBN}.json"


def test_fetch_builds_the_book(rejected):
    got = ol().fetch(fixture_client(), W1)
    assert got.external_id == W1 and got.title == "The Salt Cartographer"
    assert got.provider_ids == {"openlibrary": W1}
    assert got.release_date is None and got.release == {"first": {"date": "1987-03", "precision": "month"}}
    assert got.facts["description"].startswith("An invented novel") and got.facts["developer"] == "Ione Marsh"
    assert got.facts["features"] == ["Maps", "Coasts", "Fiction", "Surveyors"]  # subjects travel as features
    assert got.facts["languages"] == ["eng", "fre"]
    assert got.artwork == [{"kind": "poster", "url": "https://covers.openlibrary.org/b/id/9900001-L.jpg"}]
    book = got.book
    assert book["subtitle"] == "Maps of a Drowned Coast"
    assert book["authors"] == [{"name": "Ione Marsh", "role": "author", "ids": {"openlibrary": "OL9900000001A"}},
                               {"name": "Pell Arden", "role": "author", "ids": {"openlibrary": "OL9900000002A"}}]
    eds = {e["key"]: e for e in book["editions"]}
    # ISBN editions first (newest first), then the rest (newest first)
    assert [e["key"] for e in book["editions"]] == ["OL9900000011M", "OL9900000012M", "OL9900000015M",
                                                    "OL9900000013M", "OL9900000014M"]
    assert eds["OL9900000011M"] == {"key": "OL9900000011M", "isbn_13": ISBN, "isbn_10": ISBN[3:12] + "X",
                                    "format": "paperback", "publisher": "Gullwing Press", "pages": 412,
                                    "published": {"date": "2005-08-01", "precision": "day"}, "language": "eng"}
    hard = eds["OL9900000012M"]
    assert hard["isbn_10"] == ISBN10_HARD and hard["isbn_13"].startswith("978") and hard["format"] == "hardcover"
    assert hard["name"] == "First edition" and hard["published"] == {"date": "1987", "precision": "year"}
    assert "published" not in eds["OL9900000015M"]  # 1948 is outside the C5.1 range: dropped, never moved
    assert "isbn_13" not in eds["OL9900000013M"] and eds["OL9900000013M"]["format"] == "audiobook"
    assert eds["OL9900000013M"]["published"] == {"date": "2019-05", "precision": "month"}
    ebook = eds["OL9900000014M"]
    assert ebook["format"] == "ebook" and ebook["language"] == "fre" and "pages" not in ebook
    assert ebook["published"] == {"date": "2011", "precision": "year"}  # "c2011": a year, never a day
    assert rejected("isbn_13") == 1 and rejected("isbn_10") == 1  # the bad check digit; the other book's ISBN-10
    item = got.to_item()
    assert item["ext"]["book"] == book and item["ext"]["release"] == got.release
    batch = Fetcher(build_settings(), {}).as_batch("openlibrary", [got])
    assert batch is not None and batch.observations[0].ext.book.editions[0].isbn_13 == ISBN
    assert rejected("isbn_13") == 1  # validated once; the batch does not count again


def test_fetch_by_isbn_or_edition_resolves_the_work():
    calls: list[httpx.Request] = []
    by_isbn = ol().fetch(fixture_client(calls), ISBN)
    assert by_isbn.external_id == W1 and by_isbn.provider_ids == {"openlibrary": W1, "isbn": ISBN}
    assert [c.url.path for c in calls[:2]] == [f"/isbn/{ISBN}.json", "/books/OL9900000011M.json"]
    assert ol().fetch(fixture_client(), "OL9900000012M").external_id == W1
    assert ol().fetch(fixture_client(), "9780306406157") is None  # a valid ISBN Open Library does not know: 404


@pytest.mark.parametrize("location", [
    "https://evil.example/books/OL9900000011M.json",  # another host
    "http://openlibrary.org/books/OL9900000011M.json",  # not https
    "https://openlibrary.org/authors/OL9900000001A.json",  # not an edition
    "https://openlibrary.org.evil.example/books/OL9900000011M.json",
])
def test_isbn_redirect_followed_only_within_openlibrary(location):
    calls: list[httpx.Request] = []

    def redirect(request):
        if request.url.path == f"/isbn/{ISBN}.json":
            return httpx.Response(302, headers={"Location": location})
        return None

    assert ol().fetch(fixture_client(calls, redirect), ISBN) is None
    assert [str(c.url.host) for c in calls] == ["openlibrary.org"]  # the target was never requested


def test_isbn_redirect_is_followed_once():
    def chain(request):
        if request.url.path == "/books/OL9900000011M.json":
            return httpx.Response(302, headers={"Location": "https://openlibrary.org/books/OL9900000012M.json"})
        return None

    calls: list[httpx.Request] = []
    assert ol().fetch(fixture_client(calls, chain), ISBN) is None
    assert [c.url.path for c in calls] == [f"/isbn/{ISBN}.json", "/books/OL9900000011M.json"]


def test_work_redirect_is_followed_once():
    calls: list[httpx.Request] = []
    got = ol().fetch(fixture_client(calls), W2)
    assert got.external_id == W1 and got.provider_ids == {"openlibrary": W1}
    assert [c.url.path for c in calls[:2]] == [f"/works/{W2}.json", f"/works/{W1}.json"]

    def loop(request):
        if request.url.path == f"/works/{W1}.json":
            return httpx.Response(200, json={"key": f"/works/{W1}", "type": {"key": "/type/redirect"},
                                             "location": f"/works/{W3}"})
        return None

    assert ol().fetch(fixture_client(extra=loop), W2) is None  # a chain is not followed

    def away(request):
        if request.url.path == f"/works/{W2}.json":
            return httpx.Response(200, json={"key": f"/works/{W2}", "type": {"key": "/type/redirect"},
                                             "location": "https://evil.example/works/OL9900000001W"})
        return None

    assert ol().fetch(fixture_client(extra=away), W2) is None


def test_work_without_dates_or_editions():
    got = ol().fetch(fixture_client(), W3)
    assert got.title == "An Undated Ledger" and got.release is None and got.release_date is None
    assert got.facts["description"] == "Plain text description." and got.artwork == []
    assert "editions" not in got.book and got.book["authors"][0]["name"] == "Oswin Fell"


def test_free_text_dates_keep_only_the_precision_they_prove():
    got = ol().fetch(fixture_client(), W4)
    assert got.release == {"first": {"date": "1972", "precision": "year"}}  # "c1972"
    eds = {e["key"]: e for e in got.book["editions"]}
    assert eds["OL9900000041M"]["published"] == {"date": "1999", "precision": "year"}  # "Spring 1999"
    assert eds["OL9900000042M"]["published"] == {"date": "1999", "precision": "year"}  # "1/2/1999" is ambiguous
    assert eds["OL9900000042M"]["format"] == "other"  # "Large print"


@pytest.mark.parametrize("text, expected", [
    ("1965", ("1965", "year")), ("August 1965", ("1965-08", "month")), ("Aug 01, 1965", ("1965-08-01", "day")),
    ("c1965", ("1965", "year")), ("c. 1965", ("1965", "year")), ("[1965?]", ("1965", "year")),
    ("2005-08", ("2005-08", "month")), ("August 2, 2005", ("2005-08-02", "day")), ("2 August 2005", ("2005-08-02", "day")),
    ("13/02/1999", ("1999-02-13", "day")), ("1/2/1999", ("1999", "year")), ("Spring 1999", ("1999", "year")),
    ("1965-1970", None), ("1948", None), ("undated", None), ("", None), (None, None), ("31 Februar 1999", ("1999", "year")),
])
def test_parse_loose(text, expected):
    got = dates.parse_loose(text)
    assert (None if got is None else (got.value, got.precision)) == expected


def test_many_authors_are_cut_to_thirty():
    got = ol().fetch(fixture_client(), W5)
    assert len(got.book["authors"]) == 30 and got.book["authors"][0]["name"] == "Contributor Number 01"
    assert got.facts["developer"] == "Contributor Number 01"
    assert Fetcher(build_settings(), {}).as_batch("openlibrary", [got]) is not None


def test_rate_limited_answer_is_a_provider_error():
    with pytest.raises(ProviderError, match="429"):
        ol().search(fixture_client(), "busy")


@pytest.mark.parametrize("ident, ok", [
    (W1, True), ("OL9900000011M", True), (ISBN, True), ("978-0-00-990011-2", True), (ISBN10_HARD, True),
    ("OL9900000001A", False), ("OL0W", False), ("OL12345678901W", False), ("../etc", False), ("12345", False),
    (ISBN[:-1] + str((int(ISBN[-1]) + 1) % 10), False), ("ol1w", False), ("OL1W\n", False),
])
def test_ids_are_checked_before_any_request(session, ident, ok):
    assert OpenLibrary.valid_id(ident) is ok
    if ok:
        return
    calls: list[httpx.Request] = []
    f = Fetcher(build_settings(), {"openlibrary": ol()}, client=fixture_client(calls))
    with pytest.raises(ProviderError, match="invalid provider id"):
        f.lookup(session, "openlibrary", external_id=ident)
    assert calls == [] and f.failure_counts() == {}


# ---- T-B4: the provider's media type reaches ingest ---------------------------------------------------------------

def test_every_built_in_provider_declares_its_media_types():
    assert Steam.media_types == Rawg.media_types == TheGamesDb.media_types == ("game",)
    assert Igdb.media_types == ("game",) and OpenLibrary.media_types == ("book",)

    class Custom:  # a provider that does not say is a game source
        name = "custom"

    assert media_types(Custom()) == ("game",)


class FakeSteam:
    name, redistributable, requests_per_minute, media_types = "steam", False, 600, ("game",)

    def search(self, client, query):
        return []

    def fetch(self, client, external_id):
        return ProviderResult(external_id=external_id, title=f"Steam game {external_id}", platform="pc")


def test_provider_media_type_reaches_ingest(site):
    st = site.st
    st.providers.client = fixture_client()
    st.providers.providers["openlibrary"] = ol()
    st.providers.providers["steam"] = FakeSteam()
    with st.maker() as s:
        book = importer.import_from_provider(st, s, "openlibrary", W1)
        game = importer.import_from_provider(st, s, "steam", "4242")
        s.commit()
        assert book.row.media_type == "book" and game.row.media_type == "game"
        view = book.row.view
        assert view["title"] == "The Salt Cartographer" and view["developer"] == "Ione Marsh"
        assert [a["name"] for a in view["authors"]] == ["Ione Marsh", "Pell Arden"]
        assert any(e.get("isbn_13") == ISBN for e in view["editions"])
        assert s.get(Crosswalk, ("isbn", ISBN)).canonical_id == book.row.id
        assert book.artwork == 1 and book.achievements == 0  # books have no achievements (flag off)


def test_imported_redirect_keeps_the_old_id_as_an_alias(site):
    st = site.st
    st.providers.client = fixture_client()
    st.providers.providers["openlibrary"] = ol()
    with st.maker() as s:
        first = importer.import_from_provider(st, s, "openlibrary", W1)
        again = importer.import_from_provider(st, s, "openlibrary", W2)  # the merged-away id
        s.commit()
        assert again.row.id == first.row.id
        assert s.get(Crosswalk, ("openlibrary", W2)).canonical_id == first.row.id
        assert s.get(Crosswalk, ("openlibrary", W1)).canonical_id == first.row.id


# ---- T-B13: budget, pacing, identification ------------------------------------------------------------------------

def test_budget_is_sixty_a_minute():
    p = OpenLibrary()
    assert p.requests_per_minute == 60
    f = Fetcher(build_settings(), {"openlibrary": p})
    assert f._budget(p, "search").capacity == 20 and f._budget(p, "fetch").capacity == 40


def test_pacer_spaces_requests_and_refuses_a_long_wait():
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        now[0] += seconds

    pacer = Pacer(1.0, clock=lambda: now[0], sleep=sleep, max_wait=2.5)
    pacer.wait()
    pacer.wait()
    pacer.wait()
    assert slept == [1.0, 1.0]  # the first goes at once, then one a second
    busy = Pacer(1.0, clock=lambda: now[0], sleep=lambda s: None, max_wait=2.5)
    for _ in range(3):
        busy.wait()  # sleeps are not taken here: slots pile up
    with pytest.raises(ProviderError) as exc:
        busy.wait()
    assert getattr(exc.value, "held_back", False)


def test_a_paced_out_request_does_not_trip_the_circuit(session):
    class Busy(OpenLibrary):
        def search(self, client, query):
            exc = ProviderError("busy")
            exc.held_back = True  # type: ignore[attr-defined]
            raise exc

    f = Fetcher(build_settings(), {"openlibrary": Busy()}, client=fixture_client())
    for n in range(6):
        with pytest.raises(ProviderError):
            f.lookup(session, "openlibrary", query=f"anything {n}")
    assert f.failure_counts() == {} and f._budget(f.providers["openlibrary"], "search").open_until == 0


def test_user_agent_carries_the_contact_only_when_set():
    assert user_agent() == VERSION_UA
    calls: list[httpx.Request] = []
    ol().search(fixture_client(calls), "salt cartographer")
    assert calls[0].headers["User-Agent"] == VERSION_UA
    calls.clear()
    ol(contact="ops@example.org").search(fixture_client(calls), "salt cartographer")
    assert calls[0].headers["User-Agent"] == f"atlas-metadata/2.0.0.dev0 (+{PROJECT_URL}; ops@example.org)"
    # the operator's name on /site is never sent; only provider_contact is
    reg = build_registry(build_settings(node_operator="Jane Doe <jane@example.org>"))
    assert reg["openlibrary"].contact is None
    reg = build_registry(build_settings(provider_contact="https://example.org/contact"))
    assert reg["openlibrary"].contact == "https://example.org/contact"
    # other providers never get the contact
    f = Fetcher(build_settings(provider_contact="ops@example.org"), {})
    assert f.client.headers["User-Agent"] == VERSION_UA


@pytest.mark.parametrize("value", ["ops@example.org", "https://example.org/contact", ""])
def test_provider_contact_accepts_an_email_or_https_url(value):
    assert build_settings(provider_contact=value).provider_contact == value


@pytest.mark.parametrize("value", ["a b@example.org", "http://example.org", "javascript:alert(1)", "x<y>@example.org",
                                   "ops@example.org\r\nX: 1", "é@example.org", "nobody", "a@" + "b" * 200 + ".org",
                                   "https://ex(ample).org"])
def test_provider_contact_refuses_anything_else(value):
    with pytest.raises(ValidationError):
        build_settings(provider_contact=value)


def test_settings_defaults_for_open_library():
    s = build_settings()
    assert s.openlibrary_enabled is True and s.provider_contact == ""  # D15.7: on, no contact
    assert s.source_trust["openlibrary"] == 55 and "openlibrary" not in s.redistributable_sources  # D15.2
    assert "openlibrary" in build_registry(s) and OpenLibrary.redistributable is False


def test_admin_sets_the_contact_on_the_integrations_page(site):
    from atlas.models import AuditEntry, Setting

    from .test_web import signup, token

    signup(site, "alice")
    page = site.get("/admin/integrations").text
    assert "Open Library asks programs that use its API to identify themselves" in page
    assert 'name="provider_contact"' in page and "Not set: nothing identifies this node" in page
    t = token(site, "/admin/integrations")
    bad = site.post("/admin/integrations/openlibrary", data={"csrf": t, "openlibrary_enabled": "on",
                                                            "provider_contact": "ops@example.org X-Evil: 1"})
    assert bad.status_code == 400
    ok = site.post("/admin/integrations/openlibrary", data={"csrf": t, "openlibrary_enabled": "on",
                                                           "provider_contact": "ops@example.org"})
    assert ok.status_code == 303
    page = site.get("/admin/integrations").text
    assert 'value="ops@example.org"' in page
    assert site.st.providers.providers["openlibrary"].contact == "ops@example.org"
    assert site.st.ctx.settings.provider_contact == "ops@example.org"
    with site.st.maker() as s:
        assert s.get(Setting, "integration.provider_contact").value == "ops@example.org"
        audit = s.scalars(select(AuditEntry).where(AuditEntry.action == "integration")).all()[-1]
        assert audit.detail == {"fields": ["provider_contact"]}  # the field name, never the value
    site.post("/admin/integrations/openlibrary", data={"csrf": t, "openlibrary_enabled": "on"})  # no box: kept
    site.get("/admin/integrations")
    assert site.st.providers.providers["openlibrary"].contact == "ops@example.org"
    site.post("/admin/integrations/openlibrary", data={"csrf": t, "openlibrary_enabled": "on", "provider_contact": ""})
    site.get("/admin/integrations")
    assert site.st.providers.providers["openlibrary"].contact is None


def test_enrichment_accepts_only_fetchable_open_library_ids():
    assert enrich.valid_id("openlibrary", W1) and enrich.valid_id("openlibrary", ISBN)
    assert not enrich.valid_id("openlibrary", "OL9900000001A") and not enrich.valid_id("openlibrary", "OL0W")


# ---- media policy and cover fetching --------------------------------------------------------------------------------

def test_media_policy_for_open_library():
    pol = policy(build_settings(), "openlibrary")
    assert (pol.federate, pol.licence, pol.attribution, pol.fetch) == (False, "provider_terms", "Open Library", "proxy")
    assert pol.redirect_hosts == ("covers.openlibrary.org",)  # browsers are sent to the cover host only
    assert pol.follow_hosts == ("covers.openlibrary.org", "archive.org")
    assert policy(build_settings(), "steam").follow_hosts == ()  # other sources: unchanged


@pytest.fixture
def resolver(monkeypatch):
    def fake(host, port, type=0, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    media.reset_host_limits()


def _cover_client(seen, last_hop="https://ia800505.us.archive.org/view_archive.php?file=x-L.jpg"):
    from .test_media_facets import png

    def handler(request):
        seen.append((request.headers["Host"], request.headers["User-Agent"]))
        host = request.headers["Host"]
        if host == "covers.openlibrary.org":
            return httpx.Response(302, headers={"Location": "https://archive.org/download/l_covers/x-L.jpg"})
        if host == "archive.org":
            return httpx.Response(302, headers={"Location": last_hop})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png())
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cover_fetch_follows_archive_org_and_identifies_itself(resolver):
    settings = build_settings(provider_contact="ops@example.org")
    ua, follow = media.fetch_options(settings, "openlibrary")
    assert ua.endswith("; ops@example.org)") and follow == ("covers.openlibrary.org", "archive.org")
    seen: list[tuple[str, str]] = []
    data = media._request(_cover_client(seen), "https://covers.openlibrary.org/b/id/9900001-L.jpg", settings,
                          user_agent=ua, follow_hosts=follow)
    assert data.startswith(b"\x89PNG")
    assert [h for h, _ in seen] == ["covers.openlibrary.org", "archive.org", "ia800505.us.archive.org"]
    assert {u for _, u in seen} == {ua}
    assert media.fetch_options(settings, "steam") == (user_agent(), ())  # no contact for other sources


def test_cover_redirect_to_another_host_is_refused(resolver):
    seen: list[tuple[str, str]] = []
    settings = build_settings()
    ua, follow = media.fetch_options(settings, "openlibrary")
    with pytest.raises(media.MediaError, match="redirect"):
        media._request(_cover_client(seen, last_hop="https://images.evil.example/x.png"),
                       "https://covers.openlibrary.org/b/id/9900001-L.jpg", settings, user_agent=ua,
                       follow_hosts=follow)
    assert "images.evil.example" not in [h for h, _ in seen]


def test_cover_rows_are_kept_local(site):
    st = site.st
    st.providers.client = fixture_client()
    st.providers.providers["openlibrary"] = ol()
    with st.maker() as s:
        got = importer.import_from_provider(st, s, "openlibrary", W1)
        s.commit()
        from atlas.models import Artwork

        [art] = s.scalars(select(Artwork).where(Artwork.canonical_id == got.row.id)).all()
        assert (art.kind, art.source, art.redistributable) == ("poster", "openlibrary", False)
        assert service.get_canonical(s, got.row.id)[0].media_type == "book"
