"""The client API v1 (CONTRACT-C15 §9-§10, A24): the documented subset of `/api/v2` that external tools use.

The atlas-connectors (Playnite, Jellyfin, later Emby and Plex) are written against this subset, not against Atlas
code. It is versioned on its own (`client-api` 1.0), separate from the federation protocol and the plugin API: it
starts at 1 with this release, so the single-version rule (C16) has nothing to collapse here, and nothing in this
module is a feed construct.

* `/api/v2/site` advertises `client_api` (`site_block`).
* The routes of the subset carry the FastAPI tag `client` (`CLIENT_TAG`) next to their usual tag; `tools/gen_docs.py
  --client-openapi` emits exactly those operations into `contracts/openapi-client-v1.json`, and
  `tests/test_client_api_stability.py` refuses a change that removes or narrows anything in it (§9.2 item 6).
* The `Client*` models below describe the answers for that OpenAPI file only (`responses={200: {"model": ...}}`):
  they never filter or validate what a route returns, and every one allows extra members, because the contract is
  additive (clients ignore unknown members, §9.2 item 1). The generated fixtures are validated against them.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from . import wire

#: The single version (C16): the client API major is the wire major, so a wire bump is a client-api bump.
VERSION = f"{wire.WIRE_MAX}.0"
MIN_SUPPORTED = VERSION
CLIENT_TAG = "client"
#: Items per `POST /games/batch` (and `/records/batch`) call; `/resolve` follows the `max_resolve` setting.
BATCH_ITEMS = 200
#: The lower cap for anonymous (`public_read`) batch and resolve calls (A24 review: a call is one rate-limit hit).
ANONYMOUS_ITEMS = 25
#: Announced deprecations of client routes or members: `{"what", "since", "sunset", "replacement"}`. Empty in v1.
DEPRECATIONS: list[dict[str, str]] = []

#: §10.2: the closed label set of `atlas_client_requests_total{client}`.
CONNECTORS = ("playnite", "jellyfin", "emby", "plex")
CLIENT_LABELS = (*CONNECTORS, "other")
_AGENT = re.compile(r"atlas-connector-([A-Za-z0-9_-]{1,32})(?:/|\s|$)")

#: §10.1 artwork `primary_by_role`: the kinds that may fill each role, best first (C12 §12.5.1 roles). A book's
#: cover is its `poster` (D15.5), as on record tiles (`web/routes.py`, C13 §6.4).
ART_ROLES = ("cover", "background", "icon", "logo")
_ROLE_KINDS = {"background": ("hero", "banner", "screenshot"), "icon": ("icon",), "logo": ("logo",)}


def client_label(user_agent: str | None) -> str:
    """The metric label for a request: the connector's host from `atlas-connector-<host>/<version>`, else `other`."""
    found = _AGENT.search(user_agent or "")
    host = found.group(1).lower() if found else ""
    return host if host in CONNECTORS else "other"


def capabilities(session: Session, settings: Any) -> list[str]:
    """What this node offers clients. `books` while the built-in book type is enabled; `public_read` while the
    admin lets anonymous clients read the subset (D15.15, off by default)."""
    from . import custom

    caps = []
    if any(t.key == "book" for t in custom.types(session, enabled_only=True)):
        caps.append("books")
    caps += ["pairing", "etag", "batch"]
    if getattr(settings, "public_read", False):
        caps.append("public_read")
    return caps


def site_block(session: Session, settings: Any) -> dict[str, Any]:
    """`/api/v2/site.client_api` (§9.1). Connectors refuse a node whose major version differs or which lacks a
    capability they need; `contract` (the general `/api/v2` marker) is independent and never read for this."""
    return {"version": VERSION, "min_supported": MIN_SUPPORTED, "capabilities": capabilities(session, settings),
            "deprecations": list(DEPRECATIONS),
            "limits": {"batch_items": BATCH_ITEMS, "anonymous_items": ANONYMOUS_ITEMS}}


def cover_kinds(media_type: str | None) -> tuple[str, ...]:
    return ("key_art", "poster") if media_type == "game" else ("poster", "key_art")


def primary_by_role(media_type: str | None, best: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    """One artwork id per role (or null) from the C12 §12.5.7 pick of each kind (`best`), so a client does not
    re-implement the ranking (§10.1)."""
    kinds = {"cover": cover_kinds(media_type), **_ROLE_KINDS}
    out: dict[str, str | None] = {}
    for role in ART_ROLES:
        out[role] = next((best[k]["id"] for k in kinds[role] if k in best), None)
    return out


# ---- documentation models (OpenAPI only; see the module notes) ----------------------------------------------------

class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ClientApiInfo(_Open):
    version: str = Field(description="`major.minor`. A client refuses a node whose major differs from its own.")
    min_supported: str = Field(description="The oldest client-api version this node still answers for.")
    capabilities: list[str] = Field(description="`books`, `pairing`, `etag`, `batch`, `public_read`; more may "
                                                "appear. Unknown ones are ignored.")
    deprecations: list[dict[str, Any]] = Field(description="Announced deprecations; empty in v1.")
    limits: dict[str, int] | None = Field(default=None, description=(
        "`batch_items`: ids per batch call (200); `anonymous_items`: the cap on batch ids and resolve items for a "
        "call without a token under `public_read` (25). Absent on older nodes."))


class ClientSite(_Open):
    name: str
    role: str
    kind: str
    node_id: str
    media_types: list[str] = Field(description="Enabled media type keys. A client maps an unknown one to "
                                               "\"other\" (§9.2 item 2).")
    contract: int = Field(description="The general `/api/v2` contract marker (1). Clients read `client_api`.")
    client_api: ClientApiInfo


class ClientRelationRef(_Open):
    id: str
    type: str
    title: str | None = None
    media_type: str | None = None
    position: str | None = Field(default=None, description="Place in a series (`series_member` only).")


class ClientRelations(_Open):
    parents: list[ClientRelationRef] = []
    children: list[ClientRelationRef] = []
    members_of: list[ClientRelationRef] = []
    members: list[ClientRelationRef] = []
    counts: dict[str, int] = {}
    truncated: bool = False


class ClientEdition(_Open):
    name: str | None = None
    provider_ids: dict[str, str] = {}
    isbn_13: str | None = None
    isbn_10: str | None = None
    format: str | None = Field(default=None, description="`book_format`; may grow (§9.2 item 2).")
    publisher: str | None = None
    published: dict[str, Any] | None = None
    pages: int | None = None
    language: str | None = None


class ClientCreator(_Open):
    name: str
    role: str = Field(description="`book_creator_role`; may grow (§9.2 item 2).")
    ids: dict[str, str] = {}


class ClientRecord(_Open):
    """One record, as `GET /games/{id}` (and each `game` of a batch or resolve answer) returns it. Members with no
    value are left out; clients read them with defaults."""

    id: str = Field(description="`atlas:<ulid>`. Permanent; a merged id keeps resolving (`redirected_from`).")
    version: int = Field(description="Moves with every client-visible change; the ETag is `\"<id>-<version>\"`.")
    redirected_from: str | None = Field(description="The id asked for, when it was merged into this record.")
    media_type: str
    updated_at: str | None = None
    title: str | None = None
    description: str | None = None
    developer: str | None = None
    publisher: str | None = None
    release_date: str | None = Field(default=None, description="Day-precise first release only (`YYYY-MM-DD`).")
    first_release: dict[str, Any] | None = Field(default=None, description="C5 partial date `{date, precision}`.")
    release_year: int | None = None
    platforms: list[str] | None = None
    genres: list[str] | None = None
    features: list[str] | None = None
    provider_ids: dict[str, str] | None = None
    editions: list[ClientEdition] | None = None
    authors: list[ClientCreator] | None = Field(default=None, description="Books: ordered creators.")
    subtitle: str | None = None
    page_count: int | None = None
    original_language: str | None = None
    sources: list[str] | None = None
    custom: dict[str, Any] | None = None
    relations: ClientRelations | None = Field(default=None, description="`GET /games/{id}` and batch only.")


class ClientSearch(_Open):
    results: list[ClientRecord]


class ClientBatchItem(_Open):
    requested: str = Field(description="The id as sent.")
    status: Literal["ok", "not_found"]
    game: ClientRecord | None = Field(description="As `GET /games/{id}` answers it (redirects followed).")


class ClientBatch(_Open):
    results: list[ClientBatchItem] = Field(description="One per requested id, in request order.")


class ClientArtwork(_Open):
    id: str
    kind: str = Field(description="`key_art`, `banner`, `logo`, `icon`, `screenshot`, `hero`, `poster`; may grow.")
    url: str
    width: int | None = None
    height: int | None = None
    source: str
    cached: bool
    media_url: str | None = Field(description="`/api/v2/media/<sha256>` when cached here (read token, immutable).")
    public_url: str | None = Field(default=None, description="The same bytes without a token, when served.")
    image_url: str = Field(description="`/api/v2/artwork/<id>/image`: the bytes, or a 307 to the provider. Follow "
                                       "that redirect without the Authorization header (§8.4).")
    duplicate_of: str | None = None


class ClientArtworkList(_Open):
    id: str
    redirected_from: str | None
    order: str
    primary: dict[str, ClientArtwork] = Field(description="Kind → the first image of that kind in `results`.")
    best: dict[str, ClientArtwork] = Field(description="Kind → the C12 §12.5.7 pick.")
    primary_by_role: dict[str, str | None] = Field(description=(
        "Role (`cover`, `background`, `icon`, `logo`) → the artwork id to show, from the C12 ranking, or null."))
    results: list[ClientArtwork]


class ClientAchievements(_Open):
    id: str
    redirected_from: str | None
    applicable: bool = Field(description="False when achievements do not apply to the record's type (books).")
    total: int
    results: list[dict[str, Any]]


class ClientRelationGraph(_Open):
    id: str
    redirected_from: str | None
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]] = Field(description="`{from, to, type, position?}`.")
    truncated: bool
    editions: list[ClientEdition] = []
