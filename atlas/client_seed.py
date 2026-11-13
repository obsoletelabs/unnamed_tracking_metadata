"""The synthetic `connectors-v1` catalogue (CONTRACT-C15 §9.3, §12, A24): `atlas seed --fixture connectors-v1`.

What the atlas-connectors test against, on a real Atlas (their live lane) and in the generated fixtures
(`tools/export_client_fixtures.py`): 3 games, 3 books, 1 series, 1 merge redirect and 1 record of a media type no
connector knows (`comic`, §9.2 item 2). Every title, person, company, id and ISBN here is invented (the ISBNs only
carry valid check digits); no provider data is used, so the fixtures can be CC0. Ids are in the namespaces
`fixturegames` and `fixturebooks`, which no provider uses.

Seeding is idempotent: a second run answers the ids of the first (stored under `node_state` `client_seed:<name>`).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import custom, facets, relations, service
from .contract import ObservationBatchV1
from .models import Canonical, NodeState
from .service import Ctx

FIXTURE = "connectors-v1"
FIXTURES = (FIXTURE,)
MARK = "client_seed:" + FIXTURE
GAMES_NS = "fixturegames"
BOOKS_NS = "fixturebooks"
ART_HOST = "https://media.example.org/atlas-fixtures"
NAMES = ("lanternfall", "copper-tide", "quiet-orbit", "lanternfall-duplicate", "the-lamp-keeper", "salt-and-signal",
         "the-far-light", "series-lamp-cycle", "unknown-type")


def isbn13(stem12: str) -> str:
    """A 13-digit ISBN with a valid check digit (the stems below are invented)."""
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(stem12))
    return stem12 + str((10 - total % 10) % 10)


ISBN_LAMP_PB = isbn13("978000000010")
ISBN_LAMP_HC = isbn13("978000000011")
ISBN_SALT_EB = isbn13("978000000020")


def _batch(source: str, items: list[dict[str, Any]]) -> ObservationBatchV1:
    return ObservationBatchV1.model_validate({
        "connection": {"source_kind": "metadata", "source": source, "source_scope": FIXTURE}, "observations": items})


def _one(session: Session, ctx: Ctx, source: str, item: dict[str, Any], media_type: str | None = None) -> str:
    [result] = service.ingest(session, ctx, _batch(source, [item]), system=True, media_type=media_type)
    return result.canonical_id


GAMES: dict[str, dict[str, Any]] = {
    "lanternfall": {
        "external_id": "LF-1001", "title": "Lanternfall", "release_date": "2019-03-14",
        "provider_ids": {GAMES_NS: "LF-1001"},
        "facts": {"developer": "Glowmoth Studio", "publisher": "Harbour Lights Interactive",
                  "description": "A short synthetic puzzle game for the Atlas client fixtures.",
                  "genres": ["Puzzle", "Adventure"], "features": ["Single-player"], "languages": ["English"]}},
    "copper-tide": {
        "external_id": "CT-2002", "title": "Copper Tide", "provider_ids": {GAMES_NS: "CT-2002"},
        "facts": {"developer": "Brassfin Games", "genres": ["Strategy"]},
        "ext": {"release": {"first": {"date": "2021-11", "precision": "month"}}}},
    "quiet-orbit": {
        "external_id": "QO-3003", "title": "Quiet Orbit", "provider_ids": {GAMES_NS: "QO-3003"},
        "facts": {"developer": "Stillwater Labs", "genres": ["Simulation"]},
        "ext": {"release": {"first": {"date": "2023", "precision": "year"}}}},
    "lanternfall-duplicate": {
        "external_id": "LF-1001-DUP", "title": "Lanternfall (duplicate entry)",
        "facts": {"developer": "Glowmoth Studio"}},
}

BOOKS: dict[str, dict[str, Any]] = {
    "the-lamp-keeper": {
        "external_id": "LK-0001", "title": "The Lamp Keeper", "provider_ids": {BOOKS_NS: "LK-0001"},
        "facts": {"description": "A synthetic novel about a lighthouse, for the Atlas client fixtures.",
                  "features": ["Lighthouses", "Fiction"]},
        "ext": {"release": {"first": {"date": "2005-08-14", "precision": "day"}},
                "book": {"authors": [{"name": "Ada Quillfeather", "role": "author"},
                                     {"name": "Tomas Vell", "role": "translator"}],
                         "subtitle": "A Novel of Small Lights", "original_language": "en", "page_count": 412,
                         "series": {"name": "The Lamp Cycle", "position": "1"},
                         "editions": [
                             {"key": "LK-0001-PB", "name": "Paperback, Harbour Press 2006", "isbn_13": ISBN_LAMP_PB,
                              "format": "paperback", "publisher": "Harbour Press",
                              "published": {"date": "2006-02", "precision": "month"}, "pages": 428, "language": "en"},
                             {"key": "LK-0001-HC", "name": "Hardcover, Harbour Press 2005", "isbn_13": ISBN_LAMP_HC,
                              "format": "hardcover", "publisher": "Harbour Press",
                              "published": {"date": "2005", "precision": "year"}, "pages": 412, "language": "en"}]}}},
    "salt-and-signal": {
        "external_id": "SS-0002", "title": "Salt and Signal", "provider_ids": {BOOKS_NS: "SS-0002"},
        "facts": {"features": ["Radio", "Essays"]},
        "ext": {"release": {"first": {"date": "2008", "precision": "year"}},
                "book": {"authors": [{"name": "Ines Morrow", "role": "author"},
                                     {"name": "Pell Arden", "role": "narrator"}],
                         "original_language": "en",
                         "editions": [{"key": "SS-0002-EB", "name": "Ebook, Lowtide 2009", "isbn_13": ISBN_SALT_EB,
                                       "format": "ebook", "publisher": "Lowtide Books",
                                       "published": {"date": "2009-05-02", "precision": "day"}, "language": "en"}]}}},
    "the-far-light": {
        "external_id": "FL-0003", "title": "The Far Light", "provider_ids": {BOOKS_NS: "FL-0003"},
        "ext": {"book": {"authors": [{"name": "Ada Quillfeather", "role": "author"}],
                         "series": {"name": "The Lamp Cycle", "position": "2"}}}},
}

ARTWORK = {
    "lanternfall": ("key_art", "hero", "icon", "logo"),
    "the-lamp-keeper": ("poster",),
}


def _art(name: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{"kind": kind, "url": f"{ART_HOST}/{name}-{kind}.png"} for kind in kinds]


ACHIEVEMENTS = [
    {"external_id": "LF-ACH-1", "name": "First Light", "description": "Light the first lantern."},
    {"external_id": "LF-ACH-2", "name": "Keeper of Flames", "description": "Light every lantern.", "hidden": True},
]


def seeded(session: Session) -> dict[str, str] | None:
    row = session.get(NodeState, MARK)
    return json.loads(row.value) if row is not None else None


def refusal(session: Session, settings: Any) -> str | None:
    """Why `atlas seed` should not run here without `--force` (None when it may): the catalogue is invented, and
    seeding writes it to the change log, so only a fresh, non-authority database is seeded by default. Re-running
    on a database this seed already filled is fine (it is idempotent)."""
    if seeded(session) is not None:
        return None
    if getattr(settings, "role", None) == "authority":
        return "refusing to seed an authority node"
    if session.scalar(select(Canonical.id).limit(1)) is not None:
        return "refusing to seed a database that already holds records"
    return None


def seed(session: Session, ctx: Ctx, fixture: str = FIXTURE) -> tuple[dict[str, str], bool]:
    """Seed the catalogue once. Returns (name -> Atlas id, created). The caller commits."""
    if fixture not in FIXTURES:
        raise ValueError(f"unknown fixture {fixture!r} (known: {', '.join(FIXTURES)})")
    if (done := seeded(session)) is not None:
        return done, False
    custom.seed(session)  # the built-in types (book, series) on a database that only ran `atlas migrate`
    ids: dict[str, str] = {}
    for name, item in GAMES.items():
        ids[name] = _one(session, ctx, GAMES_NS, item, "game")
    for name, item in BOOKS.items():
        ids[name] = _one(session, ctx, BOOKS_NS, item, "book")
    ids["series-lamp-cycle"] = _one(session, ctx, BOOKS_NS, {
        "external_id": "SER-LAMP", "title": "The Lamp Cycle", "provider_ids": {BOOKS_NS: "SER-LAMP"}}, "series")
    if not any(t.key == "comic" for t in custom.types(session)):
        custom.save_type(session, ctx, "comic", "Comic", position=50)
    ids["unknown-type"] = _one(session, ctx, GAMES_NS, {
        "external_id": "TT-0001", "title": "Tidepool Tales #1", "provider_ids": {GAMES_NS: "TT-0001"},
        "facts": {"publisher": "Kelp Street Press"}}, "comic")
    for name, kinds in ARTWORK.items():
        facets.set_artwork(session, ctx, ids[name], "curator", _art(name, kinds))
    facets.set_achievements(session, ctx, ids["lanternfall"], "curator", [dict(a) for a in ACHIEVEMENTS])
    for book, position in (("the-lamp-keeper", "1"), ("the-far-light", "2")):
        relations.add(session, ctx, ids[book], ids["series-lamp-cycle"], "series_member", position=position,
                      actor="seed")
    service.merge_canonicals(session, ctx, ids["lanternfall-duplicate"], ids["lanternfall"], actor="seed")
    session.add(NodeState(key=MARK, value=json.dumps(ids, sort_keys=True)))
    session.flush()
    return ids, True
