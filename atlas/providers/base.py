"""Provider adapter interface. Adapters only translate; caching, budgets and retries live in Fetcher."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from .. import dates


class ProviderError(Exception):
    """Upstream failure (network, 5xx, bad payload)."""


@dataclass
class ProviderResult:
    external_id: str
    title: str
    platform: str | None = None
    release_date: str | None = None  # day precision only (legacy key)
    # C5.2 `ext.release`: {"first": {date, precision}, "entries": [...]} when the source knows more than a day
    release: dict[str, Any] | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    custom: dict[str, Any] = field(default_factory=dict)  # values for admin-defined fields, e.g. crossplay
    artwork: list[dict[str, Any]] = field(default_factory=list)
    achievements: list[dict[str, Any]] = field(default_factory=list)

    def to_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {"external_id": self.external_id, "title": self.title}
        if self.platform:
            item["platform"] = self.platform
        if self.release_date:
            item["release_date"] = self.release_date
        if self.release:
            item["ext"] = {"release": self.release}
        if self.provider_ids:
            item["provider_ids"] = self.provider_ids
        facts = {k: v for k, v in self.facts.items() if v not in (None, "", [])}
        if facts:
            item["facts"] = facts
        return item


class Provider(Protocol):
    name: str
    redistributable: bool
    requests_per_minute: int

    def search(self, client: httpx.Client, query: str) -> list[ProviderResult]: ...

    def fetch(self, client: httpx.Client, external_id: str) -> ProviderResult | None: ...


_DATE_FORMATS = ("%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%d %B, %Y", "%Y-%m-%d")
_MONTH_FORMATS = ("%b %Y", "%B %Y", "%Y-%m")
_QUARTER = re.compile(r"Q[1-4]\s+\d{4}\Z", re.IGNORECASE | re.ASCII)


def parse_date(text: str | None) -> str | None:
    """Return an ISO day or None. Month-only or year-only dates are not guessed (see `parse_release`)."""
    got = parse_release(text)
    return got[0] if got and got[1] == "day" else None


def parse_release(text: str | None) -> tuple[str, str] | None:
    """A store's release text as (PartialDate, precision) (C5.2): "19 Apr, 2011" -> day, "Apr 2011" ->
    month, "2011" -> year. A quarter ("Q3 2025") is None until the reserved `quarter` precision ships; so is
    anything outside the release year range or not a date ("Coming soon", "TBA")."""
    if not text:
        return None
    text = " ".join(text.split())
    if _QUARTER.fullmatch(text):
        return None
    candidates: list[tuple[str, str]] = []
    for fmt in _DATE_FORMATS:
        try:
            candidates.append((datetime.strptime(text, fmt).date().isoformat(), "day"))
            break
        except ValueError:
            continue
    if not candidates:
        for fmt in _MONTH_FORMATS:
            try:
                candidates.append((datetime.strptime(text, fmt).strftime("%Y-%m"), "month"))
                break
            except ValueError:
                continue
    if not candidates and re.fullmatch(r"\d{4}", text, re.ASCII):
        candidates.append((text, "year"))
    for value, precision in candidates:
        # a day is the legacy `release_date` and keeps any year (C5.2); a year or a month uses the C5.1 range
        if dates.try_parse(value, precision, any_year=precision == "day") is not None:
            return value, precision
    return None


def release_ext(first: tuple[str, str] | None, entries: list[dict[str, Any]] | None = None
                ) -> tuple[str | None, dict[str, Any] | None]:
    """(`release_date`, `ext.release`) for a provider result: the legacy key only for a day, and the
    extension only when it says more than that key (a partial first release, or dated entries)."""
    entries = entries or []
    if first is None and entries:
        derived = dates.first_of_entries(entries)
        first = (derived.value, derived.precision) if derived else None
    day = first[0] if first and first[1] == "day" else None
    if first is None or (first[1] == "day" and not entries):
        return day, None
    release: dict[str, Any] = {}
    if dates.try_parse(first[0], first[1]) is not None:  # a pre-1950 day lives in release_date alone
        release["first"] = {"date": first[0], "precision": first[1]}
    if entries:
        release["entries"] = entries
    return day, release or None


def epoch_to_date(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(?:br|p|li|ul|ol|div|h[1-6])\b[^>]*>", re.IGNORECASE)


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    text = _BLOCK.sub(" ", text)
    text = html.unescape(_TAG.sub("", text))
    return re.sub(r"\s+", " ", text).strip() or None


_QUERY = re.compile(r"(https?://[^\s'\"?#]*)[?#][^\s'\"]*")


def redact_urls(text: str) -> str:
    """`text` with every URL's query string and fragment removed. Keys travel as query parameters (RAWG `key`,
    TheGamesDB `apikey`), and httpx puts the full request URL in its error text, which members see on the link
    page and operators in logs."""
    return _QUERY.sub(r"\1", text)


def get_json(client: httpx.Client, method: str, url: str, **kw: Any) -> Any:
    try:
        resp = client.request(method, url, timeout=kw.pop("timeout", 10), **kw)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        raise ProviderError(f"{redact_urls(url)}: HTTP {exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError(redact_urls(f"{url}: {type(exc).__name__}: {exc}")) from None
