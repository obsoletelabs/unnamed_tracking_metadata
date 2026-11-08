"""Import a record from a configured provider: merge its facts, then attach its artwork and achievements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from . import client_changes, custom, facets, service
from .models import Canonical
from .providers import ProviderError

if TYPE_CHECKING:
    from .api.deps import AppState


@dataclass
class Imported:
    row: Canonical
    method: str
    confidence: float | None
    review: bool
    artwork: int
    achievements: int


def import_from_provider(st: AppState, session: Session, provider: str, external_id: str, *,
                         force_canonical: str | None = None) -> Imported:
    """Fetch full details (cached), merge them into the canonical record and keep its media and achievements.

    `force_canonical` attaches the provider's record to that existing record instead of matching (C13 §5.1 step 4:
    a follower enriching a record its upstream introduced, `atlas.enrich`).

    Raises ProviderError when the provider cannot be reached and service.AtlasError when it has no such record.
    Every version bump it causes is a `provider` client change: visible to this node's clients through
    `/changes?feed=local`, never signed into the feed (the data is not redistributable).
    """
    from . import instruments

    outcome = "failed"
    try:
        with client_changes.cause(session, "provider"):
            got = _import(st, session, provider, external_id, force_canonical)
        outcome = "review" if got.review else "created" if got.method == "none" else "matched"
        return got
    except service.AtlasError as exc:
        outcome = "not_found" if exc.code == "not_found" else "failed"
        raise
    finally:
        instruments.enrichment.inc(operation="provider_import", outcome=outcome)


def _import(st: AppState, session: Session, provider: str, external_id: str,
            force_canonical: str | None = None) -> Imported:
    from .providers.registry import media_types

    source = st.providers.providers.get(provider)
    # a provider of another media type (Open Library: books) imports records of that type; the node must define it
    kind = media_types(source)[0] if source is not None and media_types(source) else "game"
    results = st.providers.lookup(session, provider, external_id=external_id)
    batch = st.providers.as_batch(provider, results)
    if batch is None:
        raise service.AtlasError("provider has no such record", "not_found")
    res = service.ingest(session, st.ctx, batch, system=True, force_canonical=force_canonical,
                         media_type=None if kind == "game" else kind)[0]
    row, _redirect = service.get_canonical(session, res.canonical_id)  # type: ignore[arg-type]
    art = ach = 0
    if row.id == res.canonical_id and not res.review:  # an uncertain match must not attach media to a guess
        got = results[0]
        if got.artwork:
            facets.set_artwork(session, st.ctx, row.id, provider, got.artwork)
            art = len(got.artwork)
        _apply_custom(st, session, row, provider, got)
        try:
            items = st.providers.achievements(session, provider, external_id)
        except ProviderError:
            items = []
        today = datetime.now(UTC).date()
        if items:  # a provider's percentages were measured when fetched (C12 §12.1.6)
            facets.set_achievements(session, st.ctx, row.id, provider, items, measured_at=today)
            ach = len(items)
        else:  # no definitions (keyless Steam): keep the public percentages for the rows other sources list
            try:
                stats = st.providers.achievement_stats(session, provider, external_id)
            except ProviderError:
                stats = {}
            if stats:
                facets.store_stats(session, row.id, provider, stats, as_of=today)
    return Imported(row, res.method, res.confidence, res.review, art, ach)


def _apply_custom(st: AppState, session: Session, row: Canonical, provider: str, got) -> None:
    """Store provider-reported values for admin-defined fields; fields this node does not define are skipped."""
    values = {}
    for key, raw in (got.custom or {}).items():
        try:
            values.update(custom.clean_values(session, row.media_type, {key: raw}, editable_only=False))
        except service.AtlasError:
            continue
    if values:
        custom.set_values(session, st.ctx, row.id, provider, values, redistributable=False)
