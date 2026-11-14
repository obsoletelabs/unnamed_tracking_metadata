"""Source-observation contract v1.

Wire-compatible with the tracking app's `features/game_ingestion/contract.py`
(see docs/v2/observation-contract.md). An omitted or null field is unknown and
never clears a stored value.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import dates

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


class ObservationExtV1(BaseModel):
    """Optional observation extensions (C1.4 `ext`). Each member ships with a protocol minor."""

    model_config = ConfigDict(extra="forbid")

    release: ReleaseInfoV1 | None = None


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
        if self.ext is not None and self.ext.release is not None:
            out["ext"] = {"release": self.ext.release.reported()}
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
