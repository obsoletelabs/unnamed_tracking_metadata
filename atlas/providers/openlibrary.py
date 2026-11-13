"""Open Library (CONTRACT-C15 §3; issue #54): the keyless JSON API (https://openlibrary.org/developers/api), the first
book provider. On by default (D15.7): a fresh node can find and import books with no configuration.

Etiquette and terms (checked 2026-11-11 against openlibrary.org/developers/api and /developers/licensing):
* Identify the client. Every request sends `atlas-metadata/<version> (+<project URL>[; <contact>])`; the contact is
  the operator's `provider_contact` (an e-mail or https URL, empty by default), never `node_operator`.
* About one request a second unidentified (three identified). Atlas paces itself at one a second per process
  whatever the identification (`Pacer`, `requests_per_minute = 60`); the Fetcher's per-provider budget, circuit
  breaker and cache sit in front of it, so a cached work costs nothing.
* No bulk downloads: Atlas never walks the catalogue. Searches, imports and H56 enrichment are driven by a person or
  a record; a node that wants a catalogue uses Open Library's monthly dumps offline (D15.8).
* Licence: the Internet Archive asserts no new rights over the catalogue, but descriptions can be publisher text and
  covers belong to their publishers. Open Library observations therefore stay on this node (D15.2: `openlibrary` is
  not in ATLAS_REDISTRIBUTABLE_SOURCES); followers fetch books themselves (H56).

Requests (C15 §3.1): `search.json` always with `fields=` (never `isbn`: popular works list hundreds); `/isbn/{isbn}.json`
answers a redirect to its edition, followed once and only within openlibrary.org; `/works/{id}.json` (a merged work is
a `/type/redirect`, followed once); `/works/{id}/editions.json?limit=50`. Author names and keys come from one
`search.json?q=key:"/works/…"` request (in the work's order) instead of one `/authors/…` request each. Covers are
linked by cover id only (`covers.openlibrary.org/b/id/<id>-L.jpg`): lookups by ISBN or OLID are limited much more.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from .. import dates, normalize
from .base import ProviderError, ProviderResult, get_response, release_ext, user_agent

API = "https://openlibrary.org"
COVER = "https://covers.openlibrary.org/b/id/{id}-L.jpg"
_HOST = "openlibrary.org"
_WORK = re.compile(r"^OL[1-9]\d{0,9}W\Z", re.ASCII)
_EDITION = re.compile(r"^OL[1-9]\d{0,9}M\Z", re.ASCII)
_AUTHOR = re.compile(r"^OL[1-9]\d{0,9}A\Z", re.ASCII)
_LANGUAGE = re.compile(r"^[a-z]{2,3}\Z", re.ASCII)
SEARCH_FIELDS = "key,title,subtitle,author_name,author_key,first_publish_year,cover_i,edition_count,language"
SEARCH_LIMIT, EDITIONS_LIMIT, MAX_AUTHORS, MAX_SUBJECTS = 20, 50, 30, 50

#: `physical_format` is free text; anything else that is stated is `other` (vocab `book_format`)
_FORMATS = (
    ("audiobook", ("audio", "cassette", "mp3", "audiobook")),
    ("ebook", ("ebook", "e-book", "electronic", "kindle", "epub", "digital")),
    ("hardcover", ("hardcover", "hardback", "hard cover", "library binding", "board book")),
    ("paperback", ("paperback", "softcover", "soft cover", "paper back", "pocket", "trade paper")),
)


class Pacer:
    """At most one request every `interval` seconds in this process, shared by every Open Library adapter (a
    rebuilt registry keeps the pace). A request whose turn is more than `max_wait` away is refused as held back
    (it never trips the circuit breaker): the caller serves a stale answer or asks again shortly."""

    def __init__(self, interval: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, max_wait: float = 10.0) -> None:
        self.interval, self.clock, self.sleep, self.max_wait = interval, clock, sleep, max_wait
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            t = self.clock()
            slot = max(t, self._next)
            if slot - t > self.max_wait:
                exc = ProviderError("openlibrary: too many requests waiting; retry shortly")
                exc.held_back = True  # type: ignore[attr-defined]
                raise exc
            self._next = slot + self.interval
        if slot > t:
            self.sleep(slot - t)


_NOT_THERE = (301, 302, 303, 307, 308, 404)
REQUESTS_PER_MINUTE = 60
_SHARED = Pacer(60 / REQUESTS_PER_MINUTE)


def _isbn(value: Any) -> str | None:
    return normalize.isbn13(value) if isinstance(value, str) and len(value) <= 32 else None


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    if not isinstance(value, str):
        return None
    cleaned = " ".join("".join(c if c.isprintable() or c == " " else " " for c in value).split())
    return cleaned[:limit].rstrip() or None


def _description(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value.replace("\r\n", "\n") if c.isprintable() or c in "\n\t").strip()
    return cleaned[:20000] or None


def _format(value: Any) -> str | None:
    text = _text(value, 100)
    if not text:
        return None
    low = text.casefold()
    for name, words in _FORMATS:
        if any(w in low for w in words):
            return name
    return "other"


def _language(entry: Any) -> str | None:
    """`{"key": "/languages/eng"}` -> `eng` (Open Library uses MARC codes)."""
    key = entry.get("key") if isinstance(entry, dict) else None
    code = key.rsplit("/", 1)[-1] if isinstance(key, str) else None
    return code if code and _LANGUAGE.match(code) else None


def _first(values: Any) -> Any:
    return next((v for v in values if v), None) if isinstance(values, list) else None


def _published(value: Any) -> dict[str, str] | None:
    got = dates.parse_loose(value)
    return {"date": got.value, "precision": got.precision} if got else None


def _key_id(value: Any, pattern: re.Pattern[str]) -> str | None:
    ident = value.rsplit("/", 1)[-1] if isinstance(value, str) else None
    return ident if ident and pattern.match(ident) else None


class OpenLibrary:
    name = "openlibrary"
    redistributable = False  # D15.2: observations stay on this node; followers enrich themselves (H56)
    requests_per_minute = REQUESTS_PER_MINUTE
    media_types = ("book",)

    def __init__(self, contact: str | None = None, *, pacer: Pacer | None = None) -> None:
        self.contact = contact or None
        self.pacer = pacer or _SHARED

    # ---- ids ----------------------------------------------------------------------------------------------------

    @staticmethod
    def valid_id(external_id: str) -> bool:
        """A work (`OL…W`), an edition (`OL…M`) or a valid ISBN-10/13 (C15 §3.3). An author id is not a record."""
        text = str(external_id)
        return bool(_WORK.match(text) or _EDITION.match(text)) or _isbn(text) is not None

    @staticmethod
    def alias_of(requested: str) -> bool:
        """True when a fetch by `requested` that answered another work id came through a work redirect, so the old
        id is kept as an alias of the record (an ISBN or an edition id is not a work key)."""
        return bool(_WORK.match(str(requested)))

    # ---- HTTP ---------------------------------------------------------------------------------------------------

    def _get(self, client, path: str, *, params: dict[str, Any] | None = None, allow: tuple[int, ...] = (404,)):
        self.pacer.wait()
        return get_response(client, "GET", f"{API}{path}", params=params, allow=allow,
                            headers={"User-Agent": user_agent(self.contact), "Accept": "application/json"})

    def _json(self, client, path: str, **kw) -> dict[str, Any] | None:
        """The JSON object at `path`, or None for a 404 or an HTTP redirect (never followed here)."""
        resp = self._get(client, path, allow=_NOT_THERE, **kw)
        if resp.status_code in _NOT_THERE:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None

    def _search(self, client, q: str, limit: int) -> list[dict[str, Any]]:
        data = self._json(client, "/search.json", params={"q": q, "fields": SEARCH_FIELDS, "limit": limit}) or {}
        return [d for d in (data.get("docs") or [])[:limit] if isinstance(d, dict)]

    # ---- the adapter ----------------------------------------------------------------------------------------------

    def search(self, client, query):
        isbn = _isbn(query.strip())
        if isbn is not None:  # an ISBN is an identifier lookup, never a title search
            got = self.fetch(client, isbn)
            return [got] if got else []
        return [r for r in (self._result(d) for d in self._search(client, query, SEARCH_LIMIT)) if r]

    def fetch(self, client, external_id):
        ident = str(external_id)
        if not self.valid_id(ident):
            return None
        isbn = None
        if not _WORK.match(ident):
            isbn = _isbn(ident) if not _EDITION.match(ident) else None
            edition = self._edition(client, ident, isbn)
            if edition is None:
                return None
            ident = _key_id(_first([w.get("key") for w in edition.get("works") or [] if isinstance(w, dict)]), _WORK)
            if ident is None:
                return None
        work = self._json(client, f"/works/{ident}.json")
        if work is not None and (work.get("type") or {}).get("key") == "/type/redirect":
            # a merged work (C15 §3.3): follow one redirect within openlibrary.org; a chain is not followed
            ident = self._redirect_target(work.get("location"))
            work = self._json(client, f"/works/{ident}.json") if ident else None
            if work is not None and (work.get("type") or {}).get("key") == "/type/redirect":
                return None
        if work is None or not isinstance(work.get("title"), str):
            return None
        docs = self._search(client, f'key:"/works/{ident}"', 1)
        doc = docs[0] if docs and str(docs[0].get("key")) == f"/works/{ident}" else {}
        editions = self._json(client, f"/works/{ident}/editions.json", params={"limit": EDITIONS_LIMIT}) or {}
        result = self._result({**doc, "key": f"/works/{ident}"}, work,
                              [e for e in (editions.get("entries") or []) if isinstance(e, dict)][:EDITIONS_LIMIT])
        if result is not None and isbn is not None:
            result.provider_ids["isbn"] = isbn
        return result

    def _redirect_target(self, location: Any) -> str | None:
        if not isinstance(location, str):
            return None
        parts = urlsplit(location)
        if parts.netloc and (parts.scheme != "https" or parts.netloc != _HOST):
            return None
        m = re.fullmatch(r"/works/(OL[1-9]\d{0,9}W)(?:\.json)?", parts.path)
        return m[1] if m else None

    def _edition(self, client, ident: str, isbn: str | None) -> dict[str, Any] | None:
        if isbn is None:
            return self._json(client, f"/books/{ident}.json")
        resp = self._get(client, f"/isbn/{isbn}.json", allow=(301, 302, 303, 307, 308, 404))
        if resp.status_code == 404:
            return None
        if resp.status_code < 300:
            data = resp.json()
            return data if isinstance(data, dict) else None
        parts = urlsplit(resp.headers.get("location", ""))  # one redirect, to an edition on openlibrary.org only
        if (parts.scheme or parts.netloc) and (parts.scheme != "https" or parts.netloc != _HOST) or parts.query:
            return None
        m = re.fullmatch(r"/books/(OL[1-9]\d{0,9}M)\.json", parts.path)
        if m is None:
            return None
        return self._json(client, f"/books/{m[1]}.json")

    # ---- mapping --------------------------------------------------------------------------------------------------

    def _result(self, doc: dict[str, Any], work: dict[str, Any] | None = None,
                editions: list[dict[str, Any]] | None = None) -> ProviderResult | None:
        key = _key_id(doc.get("key"), _WORK)
        title = _text((work or {}).get("title") or doc.get("title"), 500)
        if key is None or not title:
            return None
        first = dates.parse_loose((work or {}).get("first_publish_date")) if work else None
        year = doc.get("first_publish_year")
        if first is None and isinstance(year, int):
            first = dates.try_parse(str(year), "year")
        day, release = release_ext((first.value, first.precision) if first else None)
        authors = self._authors(doc)
        cover = doc.get("cover_i")
        if not (isinstance(cover, int) and cover > 0) and work:
            cover = next((c for c in work.get("covers") or [] if isinstance(c, int) and c > 0), None)
        facts: dict[str, Any] = {"developer": authors[0]["name"] if authors else None}
        book: dict[str, Any] | None = None
        if work is not None:
            facts["description"] = _description(work.get("description"))
            subjects = [_text(s, 128) for s in work.get("subjects") or []]
            facts["features"] = list(dict.fromkeys(s for s in subjects if s))[:MAX_SUBJECTS]
            eds = self._editions(editions or [])
            facts["languages"] = list(dict.fromkeys(e["language"] for e in eds if e.get("language")))[:60]
            book = {"authors": authors} if authors else {}
            subtitle = _text(work.get("subtitle") or doc.get("subtitle"), 300)
            if subtitle:
                book["subtitle"] = subtitle
            if eds:
                book["editions"] = eds
            book = self._valid_book(book)
        return ProviderResult(
            external_id=key, title=title, release_date=day, release=release, provider_ids={"openlibrary": key},
            facts={k: v for k, v in facts.items() if v not in (None, "", [])}, book=book,
            artwork=[{"kind": "poster", "url": COVER.format(id=cover)}] if isinstance(cover, int) and cover > 0 else [],
        )

    @staticmethod
    def _authors(doc: dict[str, Any]) -> list[dict[str, Any]]:
        names = doc.get("author_name") or []
        keys = doc.get("author_key") or []
        out = []
        for i, name in enumerate(names if isinstance(names, list) else []):
            clean = _text(name, 200)
            if not clean:
                continue
            author: dict[str, Any] = {"name": clean, "role": "author"}  # Open Library states no role
            ident = keys[i] if isinstance(keys, list) and i < len(keys) and isinstance(keys[i], str) else None
            if ident and _AUTHOR.match(ident):
                author["ids"] = {"openlibrary": ident}
            out.append(author)
        return out[:MAX_AUTHORS]

    @staticmethod
    def _editions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each edition validated on its own (an invalid ISBN is dropped and counted there, once); an edition that
        still does not validate is left out. ISBN editions first, then newest first (C15 §3.1)."""
        from ..contract import BookEditionV1

        out = []
        for e in entries:
            key = _key_id(e.get("key"), _EDITION)
            if key is None:
                continue
            published = _published(e.get("publish_date"))
            pages = e.get("number_of_pages")
            raw: dict[str, Any] = {
                "key": key, "name": _text(e.get("edition_name"), 100),
                "isbn_13": _first(e.get("isbn_13")) if isinstance(_first(e.get("isbn_13")), str) else None,
                "isbn_10": _first(e.get("isbn_10")) if isinstance(_first(e.get("isbn_10")), str) else None,
                "format": _format(e.get("physical_format")),
                "publisher": _text(_first(e.get("publishers")), 200),
                "published": published,
                "pages": pages if isinstance(pages, int) and not isinstance(pages, bool) and 1 <= pages <= 100_000
                else None,
                "language": _language(_first(e.get("languages"))),
            }
            if raw["isbn_13"] is not None and len(raw["isbn_13"]) > 32:
                raw["isbn_13"] = None
            if raw["isbn_10"] is not None and len(raw["isbn_10"]) > 32:
                raw["isbn_10"] = None
            try:
                edition = BookEditionV1.model_validate({k: v for k, v in raw.items() if v is not None})
            except ValidationError:
                continue
            out.append(edition.model_dump(mode="json", exclude_none=True))

        def newest(e: dict[str, Any]) -> str:
            return (e.get("published") or {}).get("date", "")

        out.sort(key=lambda e: (e["key"]))
        out.sort(key=newest, reverse=True)
        out.sort(key=lambda e: "isbn_13" not in e)
        return out[:EDITIONS_LIMIT]

    @staticmethod
    def _valid_book(book: dict[str, Any]) -> dict[str, Any] | None:
        from ..contract import BookInfoV1

        if not book:
            return None
        try:
            return BookInfoV1.model_validate(book).reported() or None
        except ValidationError:  # over the 64 KiB cap: keep the authors and the first editions
            while book.get("editions"):
                book["editions"] = book["editions"][: len(book["editions"]) // 2]
                try:
                    return BookInfoV1.model_validate(book).reported() or None
                except ValidationError:
                    continue
            return None
