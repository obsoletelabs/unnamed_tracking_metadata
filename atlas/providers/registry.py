"""Provider registry and the Fetcher: cache, per-provider budgets, circuit breaker, serve-stale-on-error."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..contract import ObservationBatchV1
from ..models import ProviderCache, now
from ..settings import Settings
from .base import Provider, ProviderError, ProviderResult
from .igdb import Igdb
from .rawg import Rawg
from .steam import Steam


def build_registry(settings: Settings) -> dict[str, Provider]:
    reg: dict[str, Provider] = {}
    if settings.steam_enabled:
        reg["steam"] = Steam()
    if settings.rawg_api_key:
        reg["rawg"] = Rawg(settings.rawg_api_key)
    if settings.igdb_client_id and settings.igdb_client_secret:
        reg["igdb"] = Igdb(settings.igdb_client_id, settings.igdb_client_secret)
    return reg


@dataclass
class _Budget:
    capacity: int
    tokens: float
    updated: float = field(default_factory=time.monotonic)
    failures: int = 0
    open_until: float = 0.0

    def take(self) -> bool:
        t = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (t - self.updated) * self.capacity / 60)
        self.updated = t
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Fetcher:
    """Budgets are per process: with N workers a provider may see up to N x its declared rate."""

    def __init__(self, settings: Settings, providers: dict[str, Provider], client: httpx.Client | None = None,
                 clock=time.monotonic) -> None:
        self.settings, self.providers = settings, providers
        self.client = client or httpx.Client(headers={"User-Agent": "atlas-metadata/2"})
        self._budgets: dict[str, _Budget] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def _budget(self, p: Provider) -> _Budget:
        with self._lock:
            return self._budgets.setdefault(p.name, _Budget(p.requests_per_minute, p.requests_per_minute))

    def _cached(self, session: Session, provider: str, key: str) -> ProviderCache | None:
        return session.get(ProviderCache, (provider, key))

    def _call(self, session: Session, p: Provider, kind: str, arg: str) -> list[ProviderResult] | None:
        key = f"{kind}:{arg.casefold()}"
        row = self._cached(session, p.name, key)
        fresh = row is not None and row.expires_at.replace(tzinfo=now().tzinfo) > now()
        if fresh:
            return [] if row.negative else [ProviderResult(**r) for r in row.payload]
        b = self._budget(p)
        stale = None if row is None or row.negative else [ProviderResult(**r) for r in row.payload]
        if self._clock() < b.open_until:
            if stale is not None:
                return stale
            raise ProviderError(f"{p.name} is temporarily unavailable (circuit open)")
        if not b.take():
            if stale is not None:
                return stale
            raise ProviderError(f"{p.name} request budget exhausted; retry shortly")
        try:
            if kind == "search":
                results = p.search(self.client, arg)
            else:
                one = p.fetch(self.client, arg)
                results = [one] if one else []
        except ProviderError:
            b.failures += 1
            if b.failures >= 5:
                b.open_until = self._clock() + 60
                b.failures = 0
            if stale is not None:
                return stale
            raise
        b.failures = 0
        ttl = self.settings.provider_ttl_seconds if results else self.settings.provider_negative_ttl_seconds
        payload: list[dict[str, Any]] = [r.__dict__ for r in results]
        if row is None:
            session.add(ProviderCache(provider=p.name, key=key, payload=payload, negative=not results,
                                      expires_at=now() + timedelta(seconds=ttl)))
        else:
            row.payload, row.negative, row.fetched_at = payload, not results, now()
            row.expires_at = now() + timedelta(seconds=ttl)
        session.flush()
        return results

    def lookup(self, session: Session, provider: str, *, query: str | None = None,
               external_id: str | None = None) -> list[ProviderResult]:
        p = self.providers.get(provider)
        if p is None:
            raise ProviderError(f"provider {provider!r} is not configured on this node")
        if bool(query) == bool(external_id):
            raise ProviderError("give exactly one of query or external_id")
        if query:
            return self._call(session, p, "search", query.strip()[:200]) or []
        return self._call(session, p, "fetch", str(external_id)[:128]) or []

    def as_batch(self, provider: str, results: list[ProviderResult]) -> ObservationBatchV1 | None:
        if not results:
            return None
        seen: set[str] = set()
        items = []
        for r in results:
            if r.external_id not in seen:
                seen.add(r.external_id)
                items.append(r.to_item())
        host = self.settings.public_url.split("://", 1)[-1]
        return ObservationBatchV1.model_validate({
            "connection": {"source_kind": "metadata", "source": provider, "source_scope": host},
            "observations": items,
        })
