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

from .. import instruments as m
from ..contract import ObservationBatchV1
from ..models import ProviderCache, now
from ..settings import Settings
from .base import CURRENT_PROVIDER, Provider, ProviderError, ProviderResult, user_agent
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
        # the operator's provider_contact identifies the node to Open Library (C15 §3.2); empty unless set
        reg["openlibrary"] = OpenLibrary(contact=settings.provider_contact or None)
    return reg


def build_crossref(settings: Settings) -> Wikidata | None:
    """The cross-store id source (issue #63), when the node opted in."""
    return Wikidata(settings.public_url) if settings.wikidata_enabled else None


def media_types(p: Provider) -> tuple[str, ...]:
    """The media types a provider describes; providers that do not say are game sources."""
    return tuple(getattr(p, "media_types", ("game",)))


def valid_id(p: Any, external_id: str) -> bool:
    """The Fetcher's id rule plus the adapter's own (`valid_id`, e.g. Open Library's work/edition/ISBN rule)."""
    if not _SAFE_ID.match(external_id):
        return False
    check = getattr(p, "valid_id", None)
    return True if check is None else bool(check(external_id))


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
        # no contact here: only providers that ask for one (Open Library) send `provider_contact`, per request
        self.client = client or httpx.Client(headers={"User-Agent": user_agent()})
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
        outcome = "error"
        try:
            results, outcome = self._call_inner(session, p, kind, arg)
            return results
        except ProviderError as exc:
            outcome = "unavailable" if getattr(exc, "held_back", False) else "error"
            raise
        finally:
            m.provider_calls.inc(provider=p.name, operation=kind, outcome=outcome)

    def _held_back(self, p: Provider, reason: str, message: str) -> ProviderError:
        m.provider_backoff.inc(provider=p.name, reason=reason)
        exc = ProviderError(message)
        exc.held_back = True  # type: ignore[attr-defined]
        return exc

    def _call_inner(self, session: Session, p: Provider, kind: str, arg: str
                    ) -> tuple[list[ProviderResult], str]:
        key = f"{kind}:{arg.casefold()}"
        row = self._cached(session, p.name, key)
        fresh = row is not None and row.expires_at.replace(tzinfo=now().tzinfo) > now()
        if fresh:
            assert row is not None
            m.provider_cache.inc(provider=p.name, result="negative_hit" if row.negative else "hit")
            if row.negative:
                return [], "negative_cache_hit"
            return [ProviderResult(**r) for r in row.payload], "cache_hit"
        m.provider_cache.inc(provider=p.name, result="miss")
        b = self._budget(p, kind)
        stale = None if row is None or row.negative else [ProviderResult(**r) for r in row.payload]
        if self._clock() < b.open_until:
            if stale is not None:
                m.provider_backoff.inc(provider=p.name, reason="circuit_open")
                m.provider_cache.inc(provider=p.name, result="stale")
                return stale, "stale_served"
            raise self._held_back(p, "circuit_open", f"{p.name} is temporarily unavailable (circuit open)")
        if not b.take():
            if stale is not None:
                m.provider_backoff.inc(provider=p.name, reason="budget_exhausted")
                m.provider_cache.inc(provider=p.name, result="stale")
                return stale, "stale_served"
            raise self._held_back(p, "budget_exhausted", f"{p.name} request budget exhausted; retry shortly")
        token = CURRENT_PROVIDER.set(p.name)
        try:
            if kind == "search":
                results = p.search(self.client, arg)
            else:
                one = p.fetch(self.client, arg)
                results = [one] if one else []
        except ProviderError as exc:
            if getattr(exc, "held_back", False):  # the adapter's own pacing said "not now": not an upstream failure
                m.provider_backoff.inc(provider=p.name, reason="budget_exhausted")
                if stale is not None:
                    m.provider_cache.inc(provider=p.name, result="stale")
                    return stale, "stale_served"
                raise
            with self._lock:
                self._failure_counts[p.name] = self._failure_counts.get(p.name, 0) + 1
            b.failures += 1
            if b.failures >= 5:
                b.open_until = self._clock() + 60
                b.failures = 0
                m.provider_backoff.inc(provider=p.name, reason="circuit_tripped")
            if stale is not None:
                m.provider_cache.inc(provider=p.name, result="stale")
                return stale, "stale_served"
            raise
        finally:
            CURRENT_PROVIDER.reset(token)
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
        return results, "ok" if results else "empty"

    def lookup(self, session: Session, provider: str, *, query: str | None = None,
               external_id: str | None = None) -> list[ProviderResult]:
        p = self.providers.get(provider)
        if p is None:
            raise ProviderError(f"provider {provider!r} is not configured on this node")
        if external_id is not None and not valid_id(p, str(external_id)):
            raise ProviderError("invalid provider id")  # before any request, and never a strike against the circuit
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
        outcome = "error"
        try:
            data, outcome = self._cached_call_inner(session, p, fn, key, empty, call)
            return data
        finally:
            m.provider_calls.inc(provider=p.name, operation=method, outcome=outcome)

    def _cached_call_inner(self, session: Session, p: Provider, fn: Any, key: str, empty: Any, call: Any
                           ) -> tuple[Any, str]:
        row = self._cached(session, p.name, key)
        if row is not None and row.expires_at.replace(tzinfo=now().tzinfo) > now():
            m.provider_cache.inc(provider=p.name, result="negative_hit" if row.negative else "hit")
            return (empty, "negative_cache_hit") if row.negative else (row.payload, "cache_hit")
        m.provider_cache.inc(provider=p.name, result="miss")
        usable = row is not None and not row.negative
        b = self._budget(p, "fetch")
        if self._clock() < b.open_until or not b.take():
            reason = "circuit_open" if self._clock() < b.open_until else "budget_exhausted"
            m.provider_backoff.inc(provider=p.name, reason=reason)
            if usable:
                m.provider_cache.inc(provider=p.name, result="stale")
                return row.payload, "stale_served"  # type: ignore[union-attr]
            return empty, "unavailable"
        token = CURRENT_PROVIDER.set(p.name)
        try:
            data = call(p, fn)
        except ProviderError:
            with self._lock:
                self._failure_counts[p.name] = self._failure_counts.get(p.name, 0) + 1
            b.failures += 1
            if usable:
                m.provider_cache.inc(provider=p.name, result="stale")
                return row.payload, "stale_served"  # type: ignore[union-attr]
            return empty, "error"
        finally:
            CURRENT_PROVIDER.reset(token)
        b.failures = 0
        ttl = self.settings.provider_ttl_seconds if data else self.settings.provider_negative_ttl_seconds
        if row is None:
            session.add(ProviderCache(provider=p.name, key=key, payload=data, negative=not data,
                                      expires_at=now() + timedelta(seconds=ttl)))
        else:
            row.payload, row.negative, row.fetched_at = data, not data, now()
            row.expires_at = now() + timedelta(seconds=ttl)
        session.flush()
        return data, "ok" if data else "empty"

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
        p = self.providers.get(provider)
        return ObservationBatchV1.model_validate({
            "connection": {"source_kind": "metadata", "source": provider, "source_scope": host},
            "media_type": media_types(p)[0] if p is not None else "game",  # C15 §3.3: the provider's type
            "observations": items,
        })
