"""Import a record from a configured provider: merge its facts, then attach its artwork and achievements."""

from __future__ import annotations

from dataclasses import dataclass
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


def import_from_provider(st: AppState, session: Session, provider: str, external_id: str) -> Imported:
    """Fetch full details (cached), merge them into the canonical record and keep its media and achievements.

    Raises ProviderError when the provider cannot be reached and service.AtlasError when it has no such record.
    Every version bump it causes is a `provider` client change: visible to this node's clients through
    `/changes?feed=local`, never signed into the feed (the data is not redistributable).
    """
    with client_changes.cause(session, "provider"):
        return _import(st, session, provider, external_id)


def _import(st: AppState, session: Session, provider: str, external_id: str) -> Imported:
    results = st.providers.lookup(session, provider, external_id=external_id)
    batch = st.providers.as_batch(provider, results)
    if batch is None:
        raise service.AtlasError("provider has no such record", "not_found")
    res = service.ingest(session, st.ctx, batch, system=True)[0]
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
        if items:
            facets.set_achievements(session, st.ctx, row.id, provider, items)
            ach = len(items)
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
