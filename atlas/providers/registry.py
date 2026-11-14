"""Provider registry and the Fetcher: cache, per-provider budgets, circuit breaker, serve-stale-on-error."""

from __future__ import annotations

import re
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
from .openlibrary import OpenLibrary
from .rawg import Rawg
from .steam import Steam
from .tgdb import TheGamesDb
from .wikidata import Wikidata

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}\Z")
_SAFE_LINK_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}(/[A-Za-z0-9_.-]{1,100})?\Z")  # itch ids are `user/game`


def build_registry(settings: Settings) -> dict[str, Provider]:
    reg: dict[str, Provider] = {}
    if settings.steam_enabled:
        reg["steam"] = Steam()
    if settings.rawg_api_key:
        reg["rawg"] = Rawg(settings.rawg_api_key)
    if settings.igdb_client_id and settings.igdb_client_secret:
        reg["igdb"] = Igdb(settings.igdb_client_id, settings.igdb_client_secret)
    if settings.tgdb_api_key:
        reg["tgdb"] = TheGamesDb(settings.tgdb_api_key)
    if settings.openlibrary_enabled:
        reg["openlibrary"] = OpenLibrary()
    return reg


def build_crossref(settings: Settings) -> Wikidata | None:
    """The cross-store id source (issue #63), when the node opted in."""
    return Wikidata(settings.public_url) if settings.wikidata_enabled else None


def media_types(p: Provider) -> tuple[str, ...]:
    """The media types a provider describes; providers that do not say are game sources."""
    return tuple(getattr(p, "media_types", ("game",)))


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
        self._budgets: dict[tuple[str, str], _Budget] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._failure_counts: dict[str, int] = {}
        self.crossref_source: Wikidata | None = build_crossref(settings)

    def rebuild(self, settings: Settings) -> None:
        """Re-create the built-in providers from changed credentials; custom or replaced providers are kept."""
        built_in = (Steam, Rawg, Igdb, TheGamesDb, OpenLibrary)
        kept = {name: p for name, p in self.providers.items() if not isinstance(p, built_in)}
        self.providers = {**build_registry(settings), **kept}
        if self.crossref_source is None or isinstance(self.crossref_source, Wikidata):
            self.crossref_source = build_crossref(settings)

    def failure_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._failure_counts)

    def _budget(self, p: Provider, kind: str = "fetch") -> _Budget:
        """Searches get a third of the budget so they cannot starve fetches."""
        cap = max(1, p.requests_per_minute // 3) if kind == "search" else max(1, p.requests_per_minute * 2 // 3)
        with self._lock:
            return self._budgets.setdefault((p.name, kind), _Budget(cap, cap))

    def _cached(self, session: Session, provider: str, key: str) -> ProviderCache | None:
        return session.get(ProviderCache, (provider, key))

    def _call(self, session: Session, p: Provider, kind: str, arg: str) -> list[ProviderResult] | None:
        key = f"{kind}:{arg.casefold()}"
        row = self._cached(session, p.name, key)
        fresh = row is not None and row.expires_at.replace(tzinfo=now().tzinfo) > now()
        if fresh:
            return [] if row.negative else [ProviderResult(**r) for r in row.payload]
        b = self._budget(p, kind)
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
            with self._lock:
                self._failure_counts[p.name] = self._failure_counts.get(p.name, 0) + 1
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
        if external_id is not None and not _SAFE_ID.match(str(external_id)):
            raise ProviderError("invalid provider id")
        if bool(query) == bool(external_id):
            raise ProviderError("give exactly one of query or external_id")
        if query:
            return self._call(session, p, "search", query.strip()[:200]) or []
        return self._call(session, p, "fetch", str(external_id)[:128]) or []

    def achievements(self, session: Session, provider: str, external_id: str) -> list[dict[str, Any]]:
        """Achievement list for providers that offer one (cached like any other lookup)."""
        def call(_p: Any, fn: Any) -> Any:
            if provider == "steam":
                return fn(self.client, str(external_id), self.settings.steam_api_key)
            return fn(self.client, str(external_id))

        return self._cached_call(session, provider, external_id, "achievements", f"ach:{external_id}", [], call)

    def achievement_stats(self, session: Session, provider: str, external_id: str) -> dict[str, float]:
        """Global unlock percentages by external id, for providers that publish them without their definitions
        (keyless Steam, C12 §12.1.6). Cached like any other lookup; `{}` when there are none."""
        return self._cached_call(session, provider, external_id, "achievement_stats", f"achstat:{external_id}", {},
                                 lambda _p, fn: fn(self.client, str(external_id)))

    def crossref(self, session: Session, namespace: str, ident: str) -> dict[str, list[str]]:
        """Other stores' ids for a record's `namespace:ident` (Wikidata, issue #63), cached like a lookup.
        `{}` when the node has no cross-reference source, the id cannot be looked up, or the source is down."""
        src = self.crossref_source
        if src is None or not _SAFE_LINK_ID.match(str(ident)):
            return {}
        return self._cached_call(session, src.name, f"{namespace}:{ident}", "crossref", f"xref:{namespace}:{ident}",
                                 {}, lambda _p, fn: fn(self.client, namespace, str(ident)), provider_obj=src)

    def _cached_call(self, session: Session, provider: str, external_id: str, method: str, key: str, empty: Any,
                     call: Any, provider_obj: Any = None) -> Any:
        p = provider_obj if provider_obj is not None else self.providers.get(provider)
        fn = getattr(p, method, None)
        if p is None or fn is None or not (provider_obj is not None or _SAFE_ID.match(str(external_id))):
            return empty
        row = self._cached(session, p.name, key)
        if row is not None and row.expires_at.replace(tzinfo=now().tzinfo) > now():
            return empty if row.negative else row.payload
        b = self._budget(p, "fetch")
        if self._clock() < b.open_until or not b.take():
            return empty if row is None or row.negative else row.payload
        try:
            data = call(p, fn)
        except ProviderError:
            with self._lock:
                self._failure_counts[p.name] = self._failure_counts.get(p.name, 0) + 1
            b.failures += 1
            return empty if row is None or row.negative else row.payload
        b.failures = 0
        ttl = self.settings.provider_ttl_seconds if data else self.settings.provider_negative_ttl_seconds
        if row is None:
            session.add(ProviderCache(provider=p.name, key=key, payload=data, negative=not data,
                                      expires_at=now() + timedelta(seconds=ttl)))
        else:
            row.payload, row.negative, row.fetched_at = data, not data, now()
            row.expires_at = now() + timedelta(seconds=ttl)
        session.flush()
        return data

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
