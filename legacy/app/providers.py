from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse


@dataclass(slots=True)
class ProviderResult:
    provider: str
    external_id: str
    title: str
    fields: dict[str, Any] = field(default_factory=dict)
    source_url: str = ""
    licence: str = ""
    confidence: float = 0.5
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class ProviderHealth:
    provider: str
    status: str = "not_configured"
    checked_at: datetime | None = None
    latency_ms: float | None = None
    error: str = ""
    consecutive_failures: int = 0


class ProviderAdapter(Protocol):
    name: str
    priority: int

    async def search(self, query: str, limit: int) -> list[ProviderResult]: ...

    async def enrich(self, external_id: str) -> ProviderResult | None: ...


class ProviderQuota:
    """Small local safety valve; distributed quotas are supplied by the cache layer later."""

    def __init__(self, requests: int = 60, period_seconds: float = 60.0) -> None:
        self.requests = max(1, requests)
        self.period_seconds = max(1.0, period_seconds)
        self._events: list[float] = []

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.period_seconds
        self._events = [event for event in self._events if event > cutoff]
        if len(self._events) >= self.requests:
            return False
        self._events.append(now)
        return True


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {}
        self._quotas: dict[str, ProviderQuota] = {}
        self._health: dict[str, ProviderHealth] = {}

    def register(self, provider: ProviderAdapter, quota: ProviderQuota | None = None) -> None:
        if provider.name in self._providers:
            raise ValueError(f"Provider already registered: {provider.name}")
        self._providers[provider.name] = provider
        self._quotas[provider.name] = quota or ProviderQuota()
        self._health[provider.name] = ProviderHealth(provider=provider.name)

    def health(self) -> list[ProviderHealth]:
        return list(self._health.values())

    async def search(self, query: str, limit: int = 3) -> list[ProviderResult]:
        if len(query.strip()) < 2:
            return []
        limit = max(1, min(limit, 10))
        providers = sorted(self._providers.values(), key=lambda item: (item.priority, item.name))
        results: list[ProviderResult] = []
        for provider in providers:
            quota = self._quotas[provider.name]
            if not quota.allow():
                continue
            started = time.monotonic()
            try:
                provider_results = await _with_retry(provider.search, query.strip(), min(limit, 5))
                latency = (time.monotonic() - started) * 1000
                health = self._health[provider.name]
                health.status = "healthy"
                health.checked_at = datetime.now(timezone.utc)
                health.latency_ms = latency
                health.error = ""
                health.consecutive_failures = 0
                results.extend(provider_results)
            except Exception as exc:  # provider failures must not take down the authority
                health = self._health[provider.name]
                health.status = "unhealthy"
                health.checked_at = datetime.now(timezone.utc)
                health.error = str(exc)[:500]
                health.consecutive_failures += 1
        return sorted((_sanitize_result(item) for item in results),
                      key=lambda item: (-item.confidence, item.provider, item.external_id))[:limit]

    async def enrich(self, provider_name: str, external_id: str) -> ProviderResult | None:
        provider = self._providers.get(provider_name)
        if provider is None:
            return None
        if not self._quotas[provider_name].allow():
            return None
        try:
            result = await _with_retry(provider.enrich, external_id)
            self._health[provider_name].status = "healthy"
            self._health[provider_name].checked_at = datetime.now(timezone.utc)
            return _sanitize_result(result) if result else None
        except Exception as exc:
            health = self._health[provider_name]
            health.status = "unhealthy"
            health.checked_at = datetime.now(timezone.utc)
            health.error = str(exc)[:500]
            health.consecutive_failures += 1
            return None


async def _with_retry(function, *args, attempts: int = 3):
    delay = 0.25
    for attempt in range(attempts):
        try:
            result = function(*args)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    return None


def _sanitize_result(result: ProviderResult | None) -> ProviderResult | None:
    if result is None:
        return None
    source_url = result.source_url.strip()
    if source_url:
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            source_url = ""
    return ProviderResult(
        provider=result.provider[:80],
        external_id=result.external_id[:200],
        title=result.title.strip()[:500],
        fields=result.fields,
        source_url=source_url[:1000],
        licence=result.licence[:200],
        confidence=max(0.0, min(1.0, float(result.confidence))),
        fetched_at=result.fetched_at,
    )
