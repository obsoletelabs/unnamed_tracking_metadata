"""Source-observation contract v1.

Wire-compatible with the tracking app's `features/game_ingestion/contract.py`
(see docs/v2/observation-contract.md). An omitted or null field is unknown and
never clears a stored value.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import dates, normalize, vocab

CONTRACT_VERSION: Literal[1] = 1

ProviderKey = Annotated[str, Field(pattern=r"^[a-z0-9._-]{1,50}$")]
ProviderIdentity = Annotated[str, Field(min_length=1, max_length=256)]
ShortText = Annotated[str, Field(max_length=128)]

# Personal state that must never be stored in shared canonical data.
PERSONAL_FACTS = frozenset(
    {"playtime_seconds", "last_played_at", "owned", "wishlisted", "installed", "status", "favorite"}
)


class ObservedFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    playtime_seconds: int | None = Field(default=None, ge=0, le=2_000_000_000)
    last_played_at: int | None = Field(default=None, ge=0)
    owned: bool | None = None
    wishlisted: bool | None = None
    installed: bool | None = None
    status: str | None = Field(default=None, max_length=50)
    favorite: bool | None = None
    achievements_total: int | None = Field(default=None, ge=0)
    achievements_unlocked: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=20000)
    developer: str | None = Field(default=None, max_length=200)
    publisher: str | None = Field(default=None, max_length=200)
    series: str | None = Field(default=None, max_length=200)
    tags: list[ShortText] | None = Field(default=None, max_length=50)
    features: list[ShortText] | None = Field(default=None, max_length=50)
    # Atlas extensions (additive: the tracking app need not send them)
    genres: list[ShortText] | None = Field(default=None, max_length=50)
    themes: list[ShortText] | None = Field(default=None, max_length=50)
    languages: list[ShortText] | None = Field(default=None, max_length=60)
    aliases: list[ShortText] | None = Field(default=None, max_length=30)  # other names the game is known by
    age_rating: str | None = Field(default=None, max_length=50)
    website: str | None = Field(default=None, max_length=500)
    critic_score: int | None = Field(default=None, ge=0, le=100)
    user_score: int | None = Field(default=None, ge=0, le=100)

    @field_validator("website")
    @classmethod
    def https_only(cls, value: str | None) -> str | None:
        if value is not None and (not value.startswith("https://") or any(c.isspace() for c in value)):
            raise ValueError("website must be an https:// address")
        return value


class SourceConnectionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["host", "plugin", "manual", "metadata"]
    source: str = Field(min_length=1, max_length=128)
    source_scope: str = Field(min_length=1, max_length=512)
    label: str | None = Field(default=None, max_length=200)

    @field_validator("source", "source_scope")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Use a nonblank source and scope")
        return value

    @model_validator(mode="after")
    def manual_source(self) -> SourceConnectionRef:
        if self.source_kind == "manual" and self.source != "manual":
            raise ValueError("Manual entries use the source 'manual'")
        return self


class ReleaseDateV1(BaseModel):
    """C5.1 PartialDate plus its precision: `{"date": "2011-04", "precision": "month"}`."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(max_length=10)
    precision: Literal["year", "month", "day"]

    @model_validator(mode="after")
    def shape(self) -> ReleaseDateV1:
        try:
            dates.parse(self.date, self.precision)
        except dates.DateError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ReleaseEntryV1(ReleaseDateV1):
    """C5.1 ReleaseEntry: one dated release, keyed by platform, region, edition and category."""

    platform: str | None = Field(default=None, max_length=dates.MAX_PLATFORM)
    region: str | None = Field(default=None, pattern=r"^(ww|eu|na|asia|oce|latam|[a-z]{2})$")
    edition: str | None = Field(default=None, max_length=dates.MAX_EDITION)
    category: Literal["full", "early_access", "beta", "rerelease", "port", "demo"]

    @field_validator("platform", "edition", mode="after")
    @classmethod
    def one_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join("".join(c if c.isprintable() or c == " " else " " for c in value).split())
        if not cleaned:
            raise ValueError("Use printable text")
        return cleaned


class ReleaseInfoV1(BaseModel):
    """`ext.release` (protocol 1.3): the source's first release and up to five dated releases."""

    model_config = ConfigDict(extra="forbid")

    first: ReleaseDateV1 | None = None
    entries: list[ReleaseEntryV1] = Field(default_factory=list, max_length=dates.MAX_RELEASES)

    @model_validator(mode="after")
    def distinct(self) -> ReleaseInfoV1:
        if self.first is None and not self.entries:
            raise ValueError("ext.release needs a first release or at least one entry")
        keys = [dates.entry_key(e.model_dump(exclude_none=True)) for e in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("release entries must differ in platform, region, edition or category")
        return self

    def reported(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.first is not None:
            out["first"] = self.first.model_dump()
        if self.entries:
            out["entries"] = [e.model_dump(exclude_none=True) for e in self.entries]
        return out


# ---- ext.book (CONTRACT-C15 §2.3, registered under feed_schema.UNIFIED) ------------------------------------

MAX_AUTHORS, MAX_BOOK_EDITIONS, MAX_PAGES = 30, 50, 100_000
#: The whole `ext.book` member, JSON-encoded, is at most this large (C15 §2.3).
MAX_BOOK_BYTES = 64 * 1024
#: Namespaces an author id or an edition id never uses (C13 §3.2): an Atlas id or a curated namespace there could
#: claim someone else's record.
RESERVED_ID_NAMESPACES = frozenset({"atlas", "atlas-record", "curator", "manual", "community"})
_LANGUAGE = r"^[a-z]{2,3}(-[A-Za-z0-9]{1,8})?$"


def _one_line(value: str | None) -> str | None:
    """Printable text on one line (the `ReleaseEntryV1.one_line` rule); blank is refused."""
    if value is None:
        return None
    cleaned = " ".join("".join(c if c.isprintable() or c == " " else " " for c in value).split())
    if not cleaned:
        raise ValueError("Use printable text")
    return cleaned


def _no_reserved(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    reserved = sorted(set(value) & RESERVED_ID_NAMESPACES)
    if reserved:
        raise ValueError(f"reserved namespace in ids: {', '.join(reserved)}")
    return value


def _isbn_rejected(field: str) -> None:
    """Count an ISBN that failed its check (`atlas_book_isbn_rejected_total`). Imported lazily: the contract module is
    imported by the metrics registry's own dependencies."""
    from . import instruments

    instruments.book_isbn_rejected.inc(field=field)


def _digits(value: str) -> int:
    return sum(1 for c in value if c.isalnum())


class BookAuthorV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    role: Literal[vocab.BOOK_CREATOR_ROLES] = "author"  # type: ignore[valid-type]
    ids: dict[ProviderKey, ProviderIdentity] | None = Field(default=None, max_length=8)

    @field_validator("name", mode="after")
    @classmethod
    def printable(cls, value: str) -> str | None:
        return _one_line(value)

    @field_validator("ids", mode="after")
    @classmethod
    def not_reserved(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _no_reserved(value)


class BookSeriesV1(BaseModel):
    """A series hint: C15 §2.4 turns it into a `series_member` relation once a curator confirms the series record."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    position: str | None = Field(default=None, min_length=1, max_length=16)

    @field_validator("name", "position", mode="after")
    @classmethod
    def printable(cls, value: str | None) -> str | None:
        return _one_line(value)


class BookEditionV1(BaseModel):
    """One edition (C15 §2.3): never a record (H11), only an entry of the work's `editions[]` view."""

    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    isbn_13: str | None = Field(default=None, max_length=32)
    isbn_10: str | None = Field(default=None, max_length=32)
    format: Literal[vocab.BOOK_FORMATS] | None = None  # type: ignore[valid-type]
    publisher: str | None = Field(default=None, min_length=1, max_length=200)
    published: ReleaseDateV1 | None = None
    pages: int | None = Field(default=None, ge=1, le=MAX_PAGES)
    language: str | None = Field(default=None, max_length=12, pattern=_LANGUAGE)
    provider_ids: dict[ProviderKey, ProviderIdentity] | None = Field(default=None, max_length=8)

    @field_validator("key", "name", "publisher", mode="after")
    @classmethod
    def printable(cls, value: str | None) -> str | None:
        return _one_line(value)

    @field_validator("provider_ids", mode="after")
    @classmethod
    def not_reserved(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _no_reserved(value)

    @model_validator(mode="after")
    def isbns(self) -> BookEditionV1:
        """Normalise and check both ISBNs (C15 §2.5). An invalid one is dropped and counted, never guessed and never
        a reason to refuse the observation. A missing ISBN-13 is derived from a valid ISBN-10; an ISBN-10 that names
        another book than the ISBN-13 is dropped (the ISBN-13 wins); a `979` ISBN has no ISBN-10."""
        thirteen = None
        if self.isbn_13 is not None:
            thirteen = normalize.isbn13(self.isbn_13) if _digits(self.isbn_13) == 13 else None
            if thirteen is None:
                _isbn_rejected("isbn_13")
        ten = None
        if self.isbn_10 is not None:
            ten = normalize.isbn10(self.isbn_10) if _digits(self.isbn_10) == 10 else None
            if ten is None:
                _isbn_rejected("isbn_10")
        if ten is not None:
            from_ten = normalize.isbn13(ten)
            if thirteen is None:
                thirteen = from_ten
            elif from_ten != thirteen:
                _isbn_rejected("isbn_10")
                ten = None
        if thirteen is not None and ten is None:
            ten = normalize.isbn10(thirteen)
        self.isbn_13, self.isbn_10 = thirteen, ten
        return self


class BookInfoV1(BaseModel):
    """`ext.book`: what a book observation adds (C15 §2.3). Subjects travel as `facts.features` and the first
    publication as `ext.release.first`: one path each, never a second copy here."""

    model_config = ConfigDict(extra="forbid")

    authors: list[BookAuthorV1] | None = Field(default=None, max_length=MAX_AUTHORS)
    subtitle: str | None = Field(default=None, min_length=1, max_length=300)
    original_language: str | None = Field(default=None, max_length=12, pattern=_LANGUAGE)
    page_count: int | None = Field(default=None, ge=1, le=MAX_PAGES)
    series: BookSeriesV1 | None = None
    editions: list[BookEditionV1] | None = Field(default=None, max_length=MAX_BOOK_EDITIONS)

    @field_validator("subtitle", mode="after")
    @classmethod
    def printable(cls, value: str | None) -> str | None:
        return _one_line(value)

    @model_validator(mode="after")
    def bounded(self) -> BookInfoV1:
        import json

        if len(json.dumps(self.reported(), separators=(",", ":"), ensure_ascii=False).encode()) > MAX_BOOK_BYTES:
            raise ValueError(f"ext.book is larger than {MAX_BOOK_BYTES // 1024} KiB")
        return self

    def reported(self) -> dict[str, Any]:
        out = self.model_dump(mode="json", exclude_none=True)
        for key in ("authors", "editions"):
            if not out.get(key):
                out.pop(key, None)
        return out


class ObservationExtV1(BaseModel):
    """Optional observation extensions (C1.4 `ext`). `release` shipped with protocol 1.3; `book` (C15 §2.3) is
    registered under the unified release version (`feed_schema.UNIFIED`) and always signed with `_requires: book-v1`."""

    model_config = ConfigDict(extra="forbid")

    release: ReleaseInfoV1 | None = None
    book: BookInfoV1 | None = None


class SourceObservationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    platform: str | None = Field(default=None, max_length=50)
    platforms: list[Annotated[str, Field(max_length=50)]] | None = Field(default=None, max_length=20)  # Atlas extension
    edition: str | None = Field(default=None, max_length=100)
    # Day precision only (unchanged meaning). A partial date goes in `ext.release.first` (C5.2).
    release_date: date | None = None
    provider_ids: dict[ProviderKey, ProviderIdentity] | None = Field(default=None, max_length=32)
    facts: ObservedFacts | None = None
    source_updated_at: int | None = Field(default=None, ge=0, le=4_102_444_800)
    available: bool | None = None
    ext: ObservationExtV1 | None = None  # protocol 1.3 (`ext.release`); older followers park it

    @model_validator(mode="after")
    def release_consistent(self) -> SourceObservationV1:
        """C5.2: `release_date` is set only when the first release is known to the day, and then equals it.
        An entry's edition, when given, is this observation's edition (C5.1)."""
        release = self.ext.release if self.ext else None
        if release is None:
            return self
        first = release.first
        if first is not None and self.release_date is not None and (
                first.precision != "day" or first.date != self.release_date.isoformat()):
            raise ValueError("release_date is the first release when that is known to the day; "
                             "send a partial first release without release_date")
        # C5.1: the first release is the earliest full or early-access release, so it cannot start after one
        # this observation lists itself (it may be earlier: only five entries fit).
        earliest = dates.first_of_entries(e.model_dump(exclude_none=True) for e in release.entries)
        if earliest is not None:
            stated = dates.parse(first.date, first.precision) if first is not None else (
                dates.PartialDate(self.release_date.isoformat(), "day") if self.release_date is not None else None)
            if stated is not None and stated.start() > earliest.end():
                raise ValueError(f"the first release ({stated.value}) is after this observation's own "
                                 f"{earliest.value} full or early-access release")
        if self.edition:
            for e in release.entries:
                if e.edition and e.edition != self.edition:
                    raise ValueError(f"release edition {e.edition!r} is not this observation's edition")
        return self

    @field_validator("external_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Use a nonblank external identity")
        return value

    @field_validator("title", "platform", "edition", mode="after")
    @classmethod
    def one_line(cls, value: str | None) -> str | None:
        """Titles are shown in pages and emails: no control characters or line breaks."""
        if value is None:
            return None
        cleaned = " ".join("".join(c if c.isprintable() or c == " " else " " for c in value).split())
        if not cleaned:
            raise ValueError("Use printable text")
        return cleaned

    def reported(self, *, strip_personal: bool = True) -> dict[str, Any]:
        """Only what the source reported, JSON-ready; personal facts removed by default."""
        out: dict[str, Any] = {}
        if self.platforms:
            out["platforms"] = list(self.platforms)
        for name in ("title", "platform", "edition"):
            if getattr(self, name) is not None:
                out[name] = getattr(self, name)
        if self.release_date is not None:
            out["release_date"] = self.release_date.isoformat()
        if self.provider_ids:
            out["provider_ids"] = dict(self.provider_ids)
        if self.facts is not None:
            facts = self.facts.model_dump(mode="json", exclude_none=True)
            if strip_personal:
                facts = {k: v for k, v in facts.items() if k not in PERSONAL_FACTS}
            if facts:
                out["facts"] = facts
        if self.source_updated_at is not None:
            out["source_updated_at"] = self.source_updated_at
        if self.available is not None:
            out["available"] = self.available
        if self.ext is not None:
            ext: dict[str, Any] = {}
            if self.ext.release is not None:
                ext["release"] = self.ext.release.reported()
            if self.ext.book is not None and (book := self.ext.book.reported()):
                ext["book"] = book
            if ext:
                out["ext"] = ext
        return out


class ObservationBatchV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = CONTRACT_VERSION
    connection: SourceConnectionRef
    media_type: str = Field(default="game", min_length=1, max_length=32)
    observations: list[SourceObservationV1] = Field(min_length=1, max_length=5000)

    @field_validator("observations")
    @classmethod
    def distinct_identities(cls, items: list[SourceObservationV1]) -> list[SourceObservationV1]:
        if len({item.external_id for item in items}) != len(items):
            raise ValueError("Each batch item must have a distinct external identity")
        return items
