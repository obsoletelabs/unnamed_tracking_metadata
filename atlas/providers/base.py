"""Provider adapter interface. Adapters only translate; caching, budgets and retries live in Fetcher."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx


class ProviderError(Exception):
    """Upstream failure (network, 5xx, bad payload)."""


@dataclass
class ProviderResult:
    external_id: str
    title: str
    platform: str | None = None
    release_date: str | None = None
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


def parse_date(text: str | None) -> str | None:
    """Return an ISO date or None. Month-only or year-only dates are not guessed."""
    if not text:
        return None
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


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


def get_json(client: httpx.Client, method: str, url: str, **kw: Any) -> Any:
    try:
        resp = client.request(method, url, timeout=kw.pop("timeout", 10), **kw)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError(f"{url}: {exc}") from exc
