"""Price observations (CONTRACTS-v1 C8, issue #38): a provider-agnostic model, stored apart from metadata.

A price observation says what one *offer* cost in one storefront region at one moment:

* the offer: `provider` + `product_id` (+ `package_id`, `edition`, `platform`), attached to the Atlas record
  it sells (`canonical_id`). A DLC or expansion is priced on its own record (the `from` side of its `dlc_of` /
  `expansion_of` relation, C6); an edition of a game is priced on the game's record with `edition` set to
  the edition's name (editions are not records, C6.1/H11); a bundle is priced on its Collection record.
* the money: integers in the ISO 4217 minor units of `currency` (`atlas.money`): `amount_minor` is what a
  buyer pays now, `base_amount_minor` the list price. An observation is *on sale* when the amount is below the
  list price; `discount_pct` is derived from the two and checked when a source sends it. Floats are refused.
* `region` (ISO 3166-1 alpha-2, never `ww`), `availability`, `observed_at`, `valid_until` (a sale's end).
* where it came from (`source_kind`, `provenance`) and whether its licence lets it be redistributed.

Prices are **not** observations and never enter `Canonical.view`, record versions or the signed feed. Each row
is kept for `retention_days` (default 30, at most 365) after `observed_at`; `purge()` deletes expired rows
(the housekeeping thread runs it hourly, `atlas prices purge` on demand) and reads hide them already.

Federation (C8.3, protocol 1.6 since the H52 shift) is designed in `feed_schema.PRICE_CHANNEL` and disabled in
this release: no capability is advertised, `/api/v2/sync/price-changes` answers 404 and nothing price-related is signed.
`channel_body` is the designed entry body; only tests sign it.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from . import feed_schema, money, vocab
from .models import PriceObservation
from .service import AtlasError, get_canonical, write_lock

DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 365
#: `POST /api/v2/prices` accepts at most this many observations per request (C8.2).
MAX_BATCH = 500
#: Latest-per-offer answers hold at most this many offers (`truncated` says more exist).
MAX_LATEST = 500
#: History pages hold at most this many observations.
MAX_HISTORY = 1000
#: An `observed_at` this far ahead of this node's clock is refused (clock skew allowance).
FUTURE_SKEW = timedelta(minutes=5)
#: Rows deleted per statement by `purge`.
PURGE_BATCH = 5000

_SLUG = r"^[a-z0-9][a-z0-9_.-]{0,31}$"
_TOKEN = r"^[\x21-\x7e]{1,128}$"  # printable ASCII, no spaces: store product and package ids
Minor = Annotated[int, Field(strict=True, ge=0, le=money.MAX_MINOR)]
Availability = Literal[vocab.PRICE_AVAILABILITY]  # type: ignore[valid-type]
SourceKind = Literal[vocab.PRICE_SOURCE_KINDS]  # type: ignore[valid-type]
_NEEDS_AMOUNT = frozenset({"available", "preorder"})


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str | None = Field(default=None, max_length=64, description="Node that observed the price.")
    method: str | None = Field(default=None, pattern=_SLUG, description="How: `api`, `manual`, `import`...")
    fetch_id: str | None = Field(default=None, max_length=128, description="Id of the fetch or import run.")


class License(BaseModel):
    model_config = ConfigDict(extra="forbid")
    redistributable: bool = Field(default=False, strict=True, description=(
        "Whether the source's terms allow passing this price on. Only redistributable prices are shown to "
        "read-only tokens and would enter the price channel (C8.3)."))
    terms: str = Field(default="", max_length=64, pattern=r"^[a-z0-9._-]*$",
                       description="Short name of the terms, e.g. `steam-web-api`.")


class PriceIn(BaseModel):
    """One price observation as `POST /api/v2/prices` accepts it (C8.1). Money is integer minor units."""

    model_config = ConfigDict(extra="forbid")
    canonical_id: str = Field(min_length=1, max_length=40, description="The Atlas record the offer sells.")
    provider: str = Field(pattern=_SLUG, description="Store or price source, e.g. `steam`.")
    product_id: str = Field(pattern=_TOKEN, description="The provider's id of the product (app).")
    package_id: str | None = Field(default=None, pattern=_TOKEN, description="The provider's package or SKU.")
    edition: str | None = Field(default=None, min_length=1, max_length=128,
                                description="Edition name when the offer is one edition (C6.1).")
    platform: str | None = Field(default=None, pattern=_SLUG)
    region: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2, lower case; no `ww`.")
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 alphabetic code.")
    amount_minor: Minor | None = Field(default=None, description=(
        "What a buyer pays now, in the currency's minor units (USD 999 = 9.99, JPY 1980 = 1980, KWD 1500 = "
        "1.500). Required when available or a pre-order; 0 when free."))
    base_amount_minor: Minor | None = Field(default=None, description="The list price, in minor units.")
    discount_pct: int | None = Field(default=None, strict=True, ge=0, le=100, description=(
        "Derived from the two amounts; when sent it must agree within one point."))
    availability: Availability
    observed_at: AwareDatetime
    valid_until: AwareDatetime | None = Field(default=None, description="When the price (a sale) ends.")
    source_kind: SourceKind = "manual"
    provenance: Provenance = Field(default_factory=Provenance)
    license: License = Field(default_factory=License)
    retention_days: int = Field(default=DEFAULT_RETENTION_DAYS, strict=True, ge=1, le=MAX_RETENTION_DAYS)

    @field_validator("region")
    @classmethod
    def _region(cls, value: str) -> str:
        if value == "ww":
            raise ValueError("a price is always regional: `ww` is not a region")
        if not money.valid_region(value):
            raise ValueError("region must be an ISO 3166-1 alpha-2 code in lower case, e.g. `us`")
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        if not money.valid_currency(value):
            raise ValueError("currency must be an active ISO 4217 alphabetic code in upper case, e.g. `USD`")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> PriceIn:
        amount, base = self.amount_minor, self.base_amount_minor
        if self.availability in _NEEDS_AMOUNT and amount is None:
            raise ValueError(f"amount_minor is required when availability is {self.availability!r}")
        if self.availability == "free":
            if amount not in (None, 0):
                raise ValueError("a free offer has amount_minor 0")
            self.amount_minor = amount = 0
        if amount is not None and base is not None and amount > base:
            raise ValueError("amount_minor cannot be above base_amount_minor (the list price)")
        if self.discount_pct is not None:
            if amount is None or base is None:
                raise ValueError("discount_pct needs both amount_minor and base_amount_minor")
            derived = money.discount_pct(amount, base)
            if abs(self.discount_pct - derived) > 1:
                raise ValueError(f"discount_pct {self.discount_pct} does not match the amounts ({derived})")
        elif amount is not None and base is not None:
            self.discount_pct = money.discount_pct(amount, base)
        if self.valid_until is not None and self.valid_until <= self.observed_at:
            raise ValueError("valid_until must be after observed_at")
        return self


class PriceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prices: list[PriceIn] = Field(min_length=1, max_length=MAX_BATCH)


# ---- helpers --------------------------------------------------------------------------------------------

def utc(dt: datetime | None) -> datetime | None:
    """Aware UTC. SQLite returns naive datetimes; everything is stored as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _iso(dt: datetime | None) -> str | None:
    value = utc(dt)
    return value.isoformat() if value is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def offer_key(item: PriceIn | PriceObservation) -> tuple[Any, ...]:
    return (item.provider, item.product_id, item.package_id, item.edition, item.platform, item.region,
            item.currency)


def dedup_key(item: PriceIn | PriceObservation) -> str:
    """The offer plus `observed_at` (UTC, microseconds): the same observation sent twice has the same key."""
    parts = [p if p is not None else "\x00" for p in offer_key(item)]
    parts.append(utc(item.observed_at).strftime("%Y-%m-%dT%H:%M:%S.%f"))
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _facts(canonical_id: str, amount, base, availability, valid_until) -> tuple[Any, ...]:
    """What makes two observations of the same offer at the same moment the same: `discount_pct` is derived
    from the amounts and provenance may differ between two fetches of the same answer."""
    return canonical_id, amount, base, availability, utc(valid_until)


# ---- writing --------------------------------------------------------------------------------------------

def ingest(session: Session, items: list[PriceIn], *, actor: str = "") -> list[dict[str, Any]]:
    """Store a batch. Per item: `stored` (new row), `duplicate` (the same observation is already stored: nothing
    changes), `conflict` (the same offer and `observed_at` is stored with different facts: kept as it was),
    `unknown_record` or `expired` (already past its retention). Merged records are followed to the survivor.
    Serialised with the other writers, so concurrent batches cannot store an observation twice."""
    if len(items) > MAX_BATCH:
        raise AtlasError(f"at most {MAX_BATCH} prices per request", "too_large")
    write_lock(session)
    now = _now()
    keys = [dedup_key(i) for i in items]
    existing = {row.dedup_key: row for row in session.scalars(
        select(PriceObservation).where(PriceObservation.dedup_key.in_(set(keys))))}
    seen: dict[str, tuple[Any, ...]] = {key: _facts(r.canonical_id, r.amount_minor, r.base_amount_minor,
                                                    r.availability, r.valid_until)
                                        for key, r in existing.items()}
    ids = {key: r.id for key, r in existing.items()}
    results = []
    for index, (item, key) in enumerate(zip(items, keys, strict=True)):
        try:
            record, _redirected = get_canonical(session, item.canonical_id)
        except AtlasError:
            results.append({"index": index, "status": "unknown_record"})
            continue
        observed = utc(item.observed_at)
        if observed > now + FUTURE_SKEW:
            results.append({"index": index, "status": "invalid", "error": "observed_at is in the future"})
            continue
        purge_after = observed + timedelta(days=item.retention_days)
        if purge_after <= now:
            results.append({"index": index, "status": "expired"})
            continue
        facts = _facts(record.id, item.amount_minor, item.base_amount_minor, item.availability, item.valid_until)
        if key in seen:
            status = "duplicate" if seen[key] == facts else "conflict"
            results.append({"index": index, "status": status, "id": ids[key]})
            continue
        row = PriceObservation(
            canonical_id=record.id, provider=item.provider, product_id=item.product_id, package_id=item.package_id,
            edition=item.edition, platform=item.platform, region=item.region, currency=item.currency,
            amount_minor=item.amount_minor, base_amount_minor=item.base_amount_minor, discount_pct=item.discount_pct,
            availability=item.availability, observed_at=observed, valid_until=utc(item.valid_until),
            source_kind=item.source_kind, provenance=item.provenance.model_dump(exclude_none=True),
            redistributable=item.license.redistributable, license_terms=item.license.terms,
            retention_days=item.retention_days, purge_after=purge_after, dedup_key=key,
            created_by=actor[:128])
        session.add(row)
        session.flush()
        seen[key], ids[key] = facts, row.id
        results.append({"index": index, "status": "stored", "id": row.id, "canonical_id": record.id})
    return results


def move(session: Session, src: str, dst: str) -> int:
    """A merge (`service.merge_canonicals`): the surviving record takes over the merged one's prices."""
    return session.execute(update(PriceObservation).where(PriceObservation.canonical_id == src)
                           .values(canonical_id=dst)).rowcount or 0


def purge(session: Session, now: datetime | None = None, batch: int = PURGE_BATCH) -> int:
    """Delete every observation whose retention has passed, `batch` rows per statement. Returns the count."""
    cutoff = utc(now) or _now()
    total = 0
    while True:
        ids = list(session.scalars(select(PriceObservation.id).where(PriceObservation.purge_after <= cutoff)
                                   .order_by(PriceObservation.purge_after).limit(batch)))
        if not ids:
            return total
        session.execute(delete(PriceObservation).where(PriceObservation.id.in_(ids)))
        total += len(ids)
        if len(ids) < batch:
            return total


# ---- reading --------------------------------------------------------------------------------------------

def out(row: PriceObservation, now: datetime | None = None) -> dict[str, Any]:
    """The C8.1 shape plus read-only conveniences: exact decimal strings, the exponent, `sale` (below the list
    price) and `stale` (`valid_until` has passed)."""
    now = now or _now()
    valid_until = utc(row.valid_until)
    return {
        **channel_body(row),
        "exponent": money.exponent(row.currency),
        "amount": money.decimal(row.amount_minor, row.currency),
        "base_amount": money.decimal(row.base_amount_minor, row.currency),
        "sale": (row.amount_minor is not None and row.base_amount_minor is not None
                 and row.amount_minor < row.base_amount_minor),
        "stale": valid_until is not None and valid_until <= now,
    }


def channel_body(row: PriceObservation) -> dict[str, Any]:
    """One observation exactly as C8.1 shapes it: the designed price channel entry body (C8.3)."""
    return {
        "id": row.id, "canonical_id": row.canonical_id, "provider": row.provider, "product_id": row.product_id,
        "package_id": row.package_id, "edition": row.edition, "platform": row.platform, "region": row.region,
        "currency": row.currency, "amount_minor": row.amount_minor, "base_amount_minor": row.base_amount_minor,
        "discount_pct": row.discount_pct, "availability": row.availability, "observed_at": _iso(row.observed_at),
        "valid_until": _iso(row.valid_until), "source_kind": row.source_kind, "provenance": dict(row.provenance or {}),
        "license": {"redistributable": bool(row.redistributable), "terms": row.license_terms},
        "retention_days": row.retention_days,
    }


def _visible(query, cid: str, now: datetime, restricted: bool):
    query = query.where(PriceObservation.canonical_id == cid, PriceObservation.purge_after > now)
    if not restricted:
        query = query.where(PriceObservation.redistributable.is_(True))
    return query


def latest(session: Session, cid: str, *, region: str | None = None, currency: str | None = None,
           provider: str | None = None, restricted: bool = False, limit: int = MAX_LATEST) -> dict[str, Any]:
    """The newest observation of every offer of a record (C8.2: latest per provider, region and currency; an
    offer also names its product, package, edition and platform), ordered by provider, region, currency, offer.
    Rows past retention are never returned; non-redistributable rows only when `restricted`."""
    now = _now()
    limit = max(1, min(limit, MAX_LATEST))
    rank = func.row_number().over(
        partition_by=(PriceObservation.provider, PriceObservation.product_id, PriceObservation.package_id,
                      PriceObservation.edition, PriceObservation.platform, PriceObservation.region,
                      PriceObservation.currency),
        order_by=(PriceObservation.observed_at.desc(), PriceObservation.id.desc())).label("rank")
    inner = _visible(select(PriceObservation.id, rank), cid, now, restricted)
    if region:
        inner = inner.where(PriceObservation.region == region)
    if currency:
        inner = inner.where(PriceObservation.currency == currency)
    if provider:
        inner = inner.where(PriceObservation.provider == provider)
    ranked = inner.subquery()
    rows = list(session.scalars(
        select(PriceObservation).join(ranked, ranked.c.id == PriceObservation.id).where(ranked.c.rank == 1)
        .order_by(PriceObservation.provider, PriceObservation.region, PriceObservation.currency,
                  PriceObservation.product_id, func.coalesce(PriceObservation.package_id, ""),
                  func.coalesce(PriceObservation.edition, ""), func.coalesce(PriceObservation.platform, ""))
        .limit(limit + 1)))
    return {"results": [out(r, now) for r in rows[:limit]], "truncated": len(rows) > limit}


def _cursor(row: PriceObservation) -> str:
    return base64.urlsafe_b64encode(f"{_iso(row.observed_at)}|{row.id}".encode()).decode().rstrip("=")


def _parse_cursor(value: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        when, _, row_id = raw.partition("|")
        parsed = utc(datetime.fromisoformat(when))
        if not row_id or len(row_id) > 26 or parsed is None:
            raise ValueError
        return parsed, row_id
    except ValueError as exc:
        raise AtlasError("invalid cursor") from exc


def history(session: Session, cid: str, *, region: str, currency: str | None = None, provider: str | None = None,
            product_id: str | None = None, edition: str | None = None, platform: str | None = None,
            since: datetime | None = None, until: datetime | None = None, limit: int = 100,
            cursor: str | None = None, restricted: bool = False) -> dict[str, Any]:
    """Every retained observation of a record in one region, newest first (`observed_at`, then id, so the order
    is total and stable across pages). `next_cursor` continues the walk; None at the end. With a `currency`,
    `lowest` is the lowest retained price matching the filters (all pages; the newest one on a tie): the
    historical low as far as retention reaches."""
    now = _now()
    limit = max(1, min(limit, MAX_HISTORY))
    query = _visible(select(PriceObservation), cid, now, restricted).where(PriceObservation.region == region)
    for column, value in ((PriceObservation.currency, currency), (PriceObservation.provider, provider),
                          (PriceObservation.product_id, product_id), (PriceObservation.edition, edition),
                          (PriceObservation.platform, platform)):
        if value is not None:
            query = query.where(column == value)
    if since is not None:
        query = query.where(PriceObservation.observed_at >= utc(since))
    if until is not None:
        query = query.where(PriceObservation.observed_at < utc(until))
    lowest = None
    if currency is not None:  # amounts compare only within one currency
        low = session.scalars(query.where(PriceObservation.amount_minor.is_not(None))
                              .order_by(PriceObservation.amount_minor, PriceObservation.observed_at.desc(),
                                        PriceObservation.id.desc()).limit(1)).first()
        lowest = None if low is None else {"amount_minor": low.amount_minor,
                                           "amount": money.decimal(low.amount_minor, low.currency),
                                           "observed_at": _iso(low.observed_at), "id": low.id}
    if cursor:
        when, row_id = _parse_cursor(cursor)
        query = query.where(or_(PriceObservation.observed_at < when,
                                and_(PriceObservation.observed_at == when, PriceObservation.id < row_id)))
    rows = list(session.scalars(query.order_by(PriceObservation.observed_at.desc(), PriceObservation.id.desc())
                                .limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    return {"region": region, "results": [out(r, now) for r in rows],
            "next_cursor": _cursor(rows[-1]) if more and rows else None, "lowest": lowest}


def status(session: Session) -> dict[str, Any]:
    """Admin view: how much is stored, what the purge will remove, and the channel's state."""
    now = _now()
    total, redistributable, oldest, newest = session.execute(select(
        func.count(PriceObservation.id),
        func.count(PriceObservation.id).filter(PriceObservation.redistributable.is_(True)),
        func.min(PriceObservation.observed_at), func.max(PriceObservation.observed_at))).one()
    due = session.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.purge_after <= now))
    records = session.scalar(select(func.count(func.distinct(PriceObservation.canonical_id))))
    providers = dict(session.execute(select(PriceObservation.provider, func.count(PriceObservation.id))
                                     .group_by(PriceObservation.provider).order_by(PriceObservation.provider)
                                     .limit(100)).all())
    channel = feed_schema.PRICE_CHANNEL
    return {"observations": total, "redistributable": redistributable, "records": records,
            "providers": providers, "due_for_purge": due,
            "oldest_observed_at": _iso(oldest), "newest_observed_at": _iso(newest),
            "retention_days": {"default": DEFAULT_RETENTION_DAYS, "max": MAX_RETENTION_DAYS},
            "channel": {"protocol": channel.since, "capability": channel.capability, "endpoint": channel.endpoint,
                        "enabled": feed_schema.CHANNEL_ENABLED}}

