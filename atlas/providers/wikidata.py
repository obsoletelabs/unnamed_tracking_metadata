"""Wikidata cross-store ids (issue #63): from one id a record already has, the ids other stores use for it.

Access: the public SPARQL endpoint (https://query.wikidata.org/), no key. Wikimedia's policy asks for a
User-Agent naming the tool and a contact, and limits each client to 60 seconds of query time per minute and five
concurrent queries; one lookup here is a single indexed triple pattern that answers in milliseconds, and answers
are cached. Licence: Wikidata's structured data is CC0.

Wikidata is not a metadata source here: Atlas only reads the store ids, and only suggests them. A member still
confirms each link, and it is reviewed like any other link proposal.
"""

from __future__ import annotations

import re
from typing import Any

from .base import ProviderError, get_json

SPARQL = "https://query.wikidata.org/sparql"
# Atlas store namespace -> Wikidata property. Only properties whose values are the id Atlas stores for that
# namespace are listed (IGDB and RAWG are not: Wikidata holds their slugs, Atlas their numeric ids).
PROPERTIES: dict[str, str] = {"steam": "P1733", "gog": "P2725", "epic": "P6278", "tgdb": "P7622",
                              "itch": "P7294", "openlibrary": "P648"}
# The media type each namespace's ids describe; every other listed namespace is a game store.
_MEDIA: dict[str, str] = {"openlibrary": "book"}
# Seconds one lookup may take. Suggestions are made while the Link a store page loads, so a slow Wikidata must
# not hold the page for the general 10 s provider timeout.
TIMEOUT = 4.0
_STEAM = re.compile(r"^[1-9]\d{0,9}\Z")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}\Z")
_ITCH = re.compile(r"^https?://([a-z0-9][a-z0-9-]{0,62})\.itch\.io/([a-z0-9][a-z0-9_-]{0,99})/?\Z")
_ITCH_ID = re.compile(r"^([a-z0-9][a-z0-9-]{0,62})/([a-z0-9][a-z0-9_-]{0,99})\Z")
_OL_WORK = re.compile(r"^OL[1-9]\d{0,9}W\Z")


def media_type_of(namespace: str) -> str:
    """The media type a namespace's ids describe (`book` for Open Library, `game` for the stores)."""
    return _MEDIA.get(namespace, "game")


def to_wikidata(namespace: str, ident: str) -> str | None:
    """The Wikidata value for an Atlas id, or None when it cannot be one."""
    ident = str(ident)
    if namespace in ("steam", "tgdb"):
        return ident if _STEAM.match(ident) else None
    if namespace == "gog":
        return f"game/{ident}" if _SLUG.match(ident) else None
    if namespace == "epic":
        return ident if _SLUG.match(ident) else None
    if namespace == "itch":
        m = _ITCH_ID.match(ident)
        return f"https://{m[1]}.itch.io/{m[2]}" if m else None
    if namespace == "openlibrary":
        return ident if _OL_WORK.match(ident) else None
    return None


def from_wikidata(namespace: str, value: str) -> str | None:
    """The Atlas id for a Wikidata value, or None when the value is not one Atlas can link (a GOG movie, an
    Open Library edition or author, a malformed id)."""
    value = str(value).strip()
    if namespace in ("steam", "tgdb"):
        return value if _STEAM.match(value) else None
    if namespace == "gog":
        slug = value.removeprefix("game/")
        return slug if slug != value and _SLUG.match(slug) else None
    if namespace == "epic":
        slug = value.removeprefix("p/")
        return slug if _SLUG.match(slug) else None
    if namespace == "itch":
        m = _ITCH.match(value.lower())
        return f"{m[1]}/{m[2]}" if m else None
    if namespace == "openlibrary":
        return value if _OL_WORK.match(value) else None
    return None


def _literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Wikidata:
    name = "wikidata"
    redistributable = True  # CC0
    requests_per_minute = 30

    def __init__(self, contact: str = "") -> None:
        self.user_agent = "atlas-metadata/2 (https://github.com/obsoletelabs/unnamed_tracking_metadata" + (
            f"; {contact}" if contact else "") + ")"

    def query(self, namespace: str, ident: str) -> str | None:
        prop, value = PROPERTIES.get(namespace), to_wikidata(namespace, ident)
        if prop is None or value is None:
            return None
        others = list(PROPERTIES.items())
        optional = " ".join(f"OPTIONAL {{ ?item wdt:{p} ?{ns} }}" for ns, p in others)
        return f"SELECT ?item {' '.join('?' + ns for ns, _ in others)} WHERE {{ ?item wdt:{prop} {_literal(value)} . {optional} }} LIMIT 20"  # noqa: E501

    def crossref(self, client, namespace: str, ident: str) -> dict[str, list[str]]:
        """Ids by namespace for the item(s) holding this id. One id can map to several items (a game and its
        edition both carrying a Steam id): every value is kept, and the caller decides what to suggest."""
        sparql = self.query(namespace, ident)
        if sparql is None:
            return {}
        data: Any = get_json(client, "GET", SPARQL, params={"query": sparql, "format": "json"}, timeout=TIMEOUT,
                             headers={"User-Agent": self.user_agent, "Accept": "application/sparql-results+json"})
        try:
            rows = data["results"]["bindings"]
        except (KeyError, TypeError) as exc:
            raise ProviderError("wikidata: unexpected answer") from exc
        out: dict[str, list[str]] = {}
        for row in rows[:20] if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            for ns in PROPERTIES:
                cell = row.get(ns)
                raw = cell.get("value") if isinstance(cell, dict) else None
                got = from_wikidata(ns, raw) if isinstance(raw, str) else None
                if got and got not in out.setdefault(ns, []):
                    out[ns].append(got)
        return {ns: vals[:5] for ns, vals in out.items() if vals and ns != namespace}
