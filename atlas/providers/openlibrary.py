"""Open Library (issue #54, books): the official keyless JSON API (https://openlibrary.org/developers/api).

Access: no key. Open Library asks clients to send an identifying User-Agent and to stay around one request per
second (more with an identified agent); bulk users should use the monthly data dumps instead. Atlas searches and
fetches one work at a time and caches each answer. Licence: Open Library's catalogue data is released CC0, so
the licence allows redistribution. Atlas still keeps these observations on this node unless the operator adds
`openlibrary` to ATLAS_REDISTRIBUTABLE_SOURCES (what reaches the change feed is decided by that setting, not by
the `redistributable` attribute, which records the licence). Cover images come from covers.openlibrary.org,
which limits lookups by cover id less strictly than by ISBN; Atlas only links cover ids.

Results are works (`OL…W`), the edition-independent record of a book. They only fit nodes with a `book` media
type (Admin → Media types), and are only offered for records of that type.
"""

from __future__ import annotations

import re
from typing import Any

from .base import ProviderResult, get_json, release_ext

API = "https://openlibrary.org"
_WORK = re.compile(r"^OL[1-9]\d{0,9}W\Z")
_SEARCH_FIELDS = "key,title,first_publish_year,author_name,cover_i,subject,language,publisher"


class OpenLibrary:
    name = "openlibrary"
    redistributable = True  # the licence (CC0); the feed follows ATLAS_REDISTRIBUTABLE_SOURCES
    requests_per_minute = 50
    media_types = ("book",)

    def search(self, client, query):
        data = get_json(client, "GET", f"{API}/search.json", params={"q": query, "limit": 10, "fields": _SEARCH_FIELDS})
        return [r for r in (self._result(d, None) for d in (data.get("docs") or [])[:10] if isinstance(d, dict)) if r]

    def fetch(self, client, external_id):
        if not _WORK.match(str(external_id)):
            return None
        data = get_json(client, "GET", f"{API}/search.json",
                        params={"q": f'key:"/works/{external_id}"', "limit": 1, "fields": _SEARCH_FIELDS})
        docs = [d for d in (data.get("docs") or []) if isinstance(d, dict)]
        if not docs:
            return None
        work = get_json(client, "GET", f"{API}/works/{external_id}.json")
        return self._result(docs[0], work if isinstance(work, dict) else None)

    @staticmethod
    def _result(doc: dict[str, Any], work: dict[str, Any] | None) -> ProviderResult | None:
        key = str(doc.get("key") or "").rsplit("/", 1)[-1]
        if not _WORK.match(key) or not doc.get("title"):
            return None
        year = doc.get("first_publish_year")
        first = (str(year), "year") if isinstance(year, int) and 1000 <= year <= 9999 else None
        day, release = release_ext(first)
        authors = [a for a in (doc.get("author_name") or []) if isinstance(a, str) and a.strip()]
        description = None
        if work:
            raw = work.get("description")
            description = raw.get("value") if isinstance(raw, dict) else raw
        cover = doc.get("cover_i")
        return ProviderResult(
            external_id=key, title=str(doc["title"])[:500], release_date=day, release=release,
            facts={"description": description[:20000] if isinstance(description, str) else None,
                   # the book's author fills the record's credit field, labelled per media type
                   "developer": authors[0][:200] if authors else None,
                   "publisher": next((str(p)[:200] for p in (doc.get("publisher") or []) if p), None),
                   "genres": [str(s)[:128] for s in (doc.get("subject") or [])][:20]},
            artwork=[{"kind": "key_art", "url": f"https://covers.openlibrary.org/b/id/{cover}-L.jpg", "priority": 30}]
            if isinstance(cover, int) and cover > 0 else [],
        )
