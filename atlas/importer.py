"""Import a record from a configured provider: merge its facts, then attach its artwork and achievements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from . import facets, service
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
    """
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
        try:
            items = st.providers.achievements(session, provider, external_id)
        except ProviderError:
            items = []
        if items:
            facets.set_achievements(session, st.ctx, row.id, provider, items)
            ach = len(items)
    return Imported(row, res.method, res.confidence, res.review, art, ach)
