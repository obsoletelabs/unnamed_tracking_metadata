"""Provider adapter interface. Adapters only translate; caching, budgets and retries live in Fetcher."""

from __future__ import annotations

import contextvars
import html
import re
import time
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
    # C15 §2.3 `ext.book` (Open Library): authors, subtitle, editions. Already validated by the adapter.
    book: dict[str, Any] | None = None

    def to_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {"external_id": self.external_id, "title": self.title}
        if self.platform:
            item["platform"] = self.platform
        if self.release_date:
            item["release_date"] = self.release_date
        if self.release:
            item["ext"] = {"release": self.release}
        if self.book:
            item.setdefault("ext", {})["book"] = self.book
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
    #: the media types the provider describes (C15 §3.3); the importer files its records under the first one
    media_types: tuple[str, ...]

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


#: Where Atlas lives; providers that ask clients to identify themselves get it in the User-Agent (C15 §3.2).
PROJECT_URL = "https://github.com/obsoletelabs/unnamed_tracking_metadata"


def user_agent(contact: str | None = None) -> str:
    """`atlas-metadata/<version> (+<project URL>[; <contact>])` (C15 §3.2). The contact is the operator's
    `provider_contact`, passed only to the providers that ask for one (Open Library); never `node_operator`."""
    from .. import __version__

    return f"atlas-metadata/{__version__} (+{PROJECT_URL}" + (f"; {contact})" if contact else ")")


#: The provider an adapter call belongs to, set by the Fetcher; labels upstream-request metrics (#71).
CURRENT_PROVIDER: contextvars.ContextVar[str] = contextvars.ContextVar("atlas_provider", default="other")


def _record_quota(provider: str, headers: Any) -> None:
    """Record quota headers only when the provider sent them; nothing is invented for providers that do not."""
    from ..instruments import provider_quota_remaining, provider_quota_reset

    remaining = headers.get("x-ratelimit-remaining") or headers.get("ratelimit-remaining")
    if remaining is not None and remaining.strip().isdigit():
        provider_quota_remaining.set(int(remaining.strip()), provider=provider)
    reset = headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset")
    after = headers.get("retry-after")
    try:
        if reset is not None and reset.strip().isdigit():
            value = int(reset.strip())
            # RateLimit-Reset (IETF draft) is seconds from now; X-RateLimit-Reset is usually a Unix time
            provider_quota_reset.set(value if value > 1_000_000_000 else time.time() + value, provider=provider)
        elif after is not None and after.strip().isdigit():
            provider_quota_reset.set(time.time() + int(after.strip()), provider=provider)
    except ValueError:
        pass


def _outcome(exc: BaseException | None, status: int | None) -> str:
    if exc is None:
        return "ok"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError) and status is not None:
        return "http_429" if status == 429 else "http_5xx" if status >= 500 else "http_4xx"
    if isinstance(exc, ValueError):
        return "invalid_payload"
    return "transport"


def get_json(client: httpx.Client, method: str, url: str, **kw: Any) -> Any:
    return get_response(client, method, url, **kw).json()


def get_response(client: httpx.Client, method: str, url: str, *, allow: tuple[int, ...] = (), **kw: Any
                 ) -> httpx.Response:
    """One request, counted and timed. Redirects are never followed here (an adapter decides, C15 §3.1); a status in
    `allow` (a redirect, a 404 meaning "no such record") is returned instead of raised. A 2xx body must be JSON."""
    from ..instruments import provider_request_duration, provider_requests

    provider = CURRENT_PROVIDER.get()
    started, status, failure = time.monotonic(), None, None
    try:
        resp = client.request(method, url, timeout=kw.pop("timeout", 10), follow_redirects=False, **kw)
        status = resp.status_code
        _record_quota(provider, resp.headers)
        if status in allow:
            return resp
        resp.raise_for_status()
        resp.json()  # a 2xx that is not JSON is a bad payload, counted as such
        return resp
    except httpx.HTTPStatusError as exc:
        failure = exc
        raise ProviderError(f"{redact_urls(url)}: HTTP {exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError) as exc:
        failure = exc
        raise ProviderError(redact_urls(f"{url}: {type(exc).__name__}: {exc}")) from None
    finally:
        outcome = _outcome(failure, status)
        provider_requests.inc(provider=provider, outcome=outcome)
        provider_request_duration.observe(time.monotonic() - started, provider=provider, outcome=outcome)
