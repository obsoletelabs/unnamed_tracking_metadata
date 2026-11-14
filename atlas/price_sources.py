"""Price sources (issue #38): provider-agnostic fetching of store prices into price observations (C8).

A `PriceSource` turns a store's answer into `Quote`s for a list of the store's product ids in one region; `collect`
finds the records linked to that store, asks the source in batches under a request budget, stops when the source
keeps failing, and stores what it got through `prices.ingest` (deduplicated, validated, retention-bound).

Nothing here touches the price channel: observations stay on this node (C8.3 stays disabled, decision H52), and a
source whose terms do not allow redistribution stores `license.redistributable = false`, which keeps its prices
from read-only tokens. Fetching is off until an operator lists the source in `ATLAS_PRICE_SOURCES`, after reading
its terms (docs/guide/prices.md), and runs `atlas prices fetch`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import money, prices
from .models import Canonical, Crosswalk
from .providers.base import ProviderError, get_json
from .settings import Settings


@dataclass
class Quote:
    """One store's answer for one product in one region. Money is integer minor units (`atlas.money`)."""

    product_id: str
    availability: str
    currency: str | None = None
    amount_minor: int | None = None
    base_amount_minor: int | None = None
    package_id: str | None = None


class PriceSource(Protocol):
    name: str  # also the store namespace the products are linked under, e.g. `steam`
    terms: str  # short name of the terms the prices are stored under (`license.terms`)
    redistributable: bool
    requests_per_minute: int
    batch_size: int  # product ids per request

    def quotes(self, client: httpx.Client, product_ids: list[str], region: str) -> list[Quote]: ...


class SteamPrices:
    """The Steam storefront's `appdetails` with `filters=price_overview`, which answers several apps per request.

    Terms: the storefront endpoints are public but undocumented, and Valve's Steam Web API terms allow use but do
    not grant redistribution of store data; prices are therefore stored as not redistributable. Valve throttles
    `appdetails` (about 200 requests per five minutes per address), so the budget stays at 20 per minute.
    A free app answers with no `price_overview`: that is not evidence of a price, so no observation is made.
    An app the store does not sell in the region answers `success: false` with no currency; it is reported as
    unavailable and not stored, because an observation needs the region's currency and Steam does not say it."""

    name = "steam"
    terms = "steam-storefront"
    redistributable = False
    requests_per_minute = 20
    batch_size = 50

    def quotes(self, client, product_ids, region):
        ids = [i for i in product_ids if i.isdigit()][: self.batch_size]
        if not ids:
            return []
        data = get_json(client, "GET", "https://store.steampowered.com/api/appdetails",
                        params={"appids": ",".join(ids), "filters": "price_overview", "cc": region})
        if not isinstance(data, dict):
            raise ProviderError("steam: unexpected answer")
        out = []
        for app in ids:
            entry = data.get(app)
            if not isinstance(entry, dict):
                continue
            if not entry.get("success"):
                out.append(Quote(app, "unavailable"))  # no currency: counted, not stored
                continue
            po = (entry.get("data") or {}).get("price_overview") if isinstance(entry.get("data"), dict) else None
            if not isinstance(po, dict):
                continue  # free, or no price shown: no observation
            currency, final, initial = po.get("currency"), po.get("final"), po.get("initial")
            if not (isinstance(currency, str) and money.valid_currency(currency) and _minor(final)):
                continue
            base = initial if _minor(initial) and initial >= final else None
            out.append(Quote(app, "available", currency, final, base))
        return out


def _minor(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= money.MAX_MINOR


SOURCES: dict[str, Callable[[], PriceSource]] = {"steam": SteamPrices}


def enabled(settings: Settings) -> list[str]:
    """The sources this node's operator switched on (`ATLAS_PRICE_SOURCES`), in the order given."""
    names = [n.strip().lower() for n in (settings.price_sources or "").split(",") if n.strip()]
    return [n for n in dict.fromkeys(names) if n in SOURCES]


def linked_products(session: Session, store: str, limit: int) -> Iterator[tuple[str, str]]:
    """(product id, canonical id) for live records linked to the store, in a stable order (by record id)."""
    rows = session.execute(
        select(Crosswalk.external_id, Crosswalk.canonical_id).join(Canonical, Canonical.id == Crosswalk.canonical_id)
        .where(Crosswalk.namespace == store, Crosswalk.variant == "", Canonical.merged_into.is_(None))
        .order_by(Crosswalk.canonical_id, Crosswalk.external_id).limit(limit))
    yield from ((str(pid), str(cid)) for pid, cid in rows)


@dataclass
class Report:
    source: str
    region: str
    requested: int = 0
    requests: int = 0
    stored: int = 0
    duplicate: int = 0
    skipped: int = 0  # no price shown (free, or not answered) or refused by validation
    failures: int = 0
    stopped: str | None = None
    statuses: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, {})}


def collect(session: Session, source: PriceSource, region: str, *, client: httpx.Client, limit: int = 500,
            max_failures: int = 3, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
            now: Callable[[], datetime] = lambda: datetime.now(UTC), node: str = "", dry_run: bool = False) -> Report:
    """Fetch prices for up to `limit` linked products and store them. Requests are spaced to the source's
    `requests_per_minute`; after `max_failures` failed requests in a row the run stops (the store is down or
    refusing us) and reports why. A product the store does not answer for is skipped, never guessed."""
    if not money.valid_region(region) or region == "ww":
        raise ValueError("region must be an ISO 3166-1 alpha-2 code in lower case, e.g. `us`")
    report = Report(source.name, region)
    products = list(linked_products(session, source.name, limit))
    report.requested = len(products)
    by_product: dict[str, list[str]] = {}
    for pid, cid in products:
        by_product.setdefault(pid, []).append(cid)
    ids = list(by_product)
    gap = 60.0 / max(1, source.requests_per_minute)
    last = None
    failures = 0
    fetch_id = f"{source.name}-{region}-{now().strftime('%Y%m%dT%H%M%SZ')}"
    for start in range(0, len(ids), source.batch_size):
        if last is not None and (wait := gap - (clock() - last)) > 0:
            sleep(wait)
        last = clock()
        chunk = ids[start:start + source.batch_size]
        report.requests += 1
        try:
            quotes = source.quotes(client, chunk, region)
        except ProviderError as exc:
            failures += 1
            report.failures += 1
            if failures >= max_failures:
                report.stopped = f"{failures} failed requests in a row: {str(exc)[:200]}"
                break
            continue
        failures = 0
        observed = now()
        items: list[prices.PriceIn] = []
        answered = set()
        for q in quotes:
            if q.currency is None:
                report.statuses[q.availability] = report.statuses.get(q.availability, 0) + 1
                answered.add(q.product_id)
                continue
            for cid in by_product.get(q.product_id, []):
                try:
                    items.append(prices.PriceIn(
                        canonical_id=cid, provider=source.name, product_id=q.product_id, package_id=q.package_id,
                        region=region, currency=q.currency, amount_minor=q.amount_minor,
                        base_amount_minor=q.base_amount_minor, availability=q.availability, observed_at=observed,
                        source_kind="provider",
                        provenance=prices.Provenance(node=node[:64] or None, method="api", fetch_id=fetch_id),
                        license=prices.License(redistributable=source.redistributable, terms=source.terms)))
                    answered.add(q.product_id)
                except ValueError:
                    report.skipped += 1
        report.skipped += len([p for p in chunk if p not in answered])
        if items and not dry_run:
            for r in prices.ingest(session, items, actor=f"price-source:{source.name}"):
                report.statuses[r["status"]] = report.statuses.get(r["status"], 0) + 1
            session.flush()
        elif items:
            report.statuses["would_store"] = report.statuses.get("would_store", 0) + len(items)
    report.stored = report.statuses.get("stored", 0)
    report.duplicate = report.statuses.get("duplicate", 0)
    return report

