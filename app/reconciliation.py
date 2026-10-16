from __future__ import annotations

import math
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .identity import CanonicalField, MetadataObservation

METADATA_MODE = os.getenv("METADATA_MODE", "central").strip().lower()
if METADATA_MODE not in {"central", "local"}:
    raise RuntimeError("METADATA_MODE must be 'central' or 'local'")

# These are deliberately conservative defaults. Provider-specific trust can be
# configured later without changing the canonical data model.
SOURCE_TRUST = {
    "curated": 1.00,
    "user": 0.98,
    "manual": 0.90,
    "api": 0.85,
    "provider": 0.75,
    "community": 0.60,
    "unknown": 0.25,
}


def _freshness(observed_at: datetime) -> float:
    now = datetime.now(timezone.utc)
    timestamp = observed_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - timestamp).total_seconds() / 86400)
    # Never reaches zero; old evidence remains useful and auditable.
    return 0.5 + (0.5 * math.exp(-age_days / 365.0))


def observation_score(observation: MetadataObservation) -> float:
    trust = SOURCE_TRUST.get(observation.source_type, SOURCE_TRUST["unknown"])
    return float(observation.confidence) * trust * _freshness(observation.observed_at)


def choose_observation(db: Session, game_id: int, field_name: str) -> MetadataObservation | None:
    observations = db.scalars(
        select(MetadataObservation)
        .where(MetadataObservation.game_id == game_id, MetadataObservation.field_name == field_name)
        .order_by(MetadataObservation.observed_at.desc(), MetadataObservation.id.desc())
    ).all()
    if not observations:
        return None

    # User-local overrides are intentionally strongest only for a local
    # authority. Central mode still records them but does not let one private
    # override silently become shared canonical truth.
    if METADATA_MODE == "local":
        local = [item for item in observations if item.source_type == "user" and item.confidence >= 0.5]
        if local:
            return max(local, key=lambda item: (observation_score(item), item.observed_at, item.id))

    return max(observations, key=lambda item: (observation_score(item), item.observed_at, item.id))


def reconcile_field(db: Session, game_id: int, field_name: str) -> CanonicalField | None:
    observation = choose_observation(db, game_id, field_name)
    if observation is None:
        return None

    existing = db.scalar(select(CanonicalField).where(
        CanonicalField.game_id == game_id,
        CanonicalField.field_name == field_name,
    ))
    if existing is None:
        existing = CanonicalField(game_id=game_id, field_name=field_name, value_json=observation.value_json,
                                  observation_id=observation.id,
                                  selection_reason=f"highest scored {observation.source_type} observation")
        db.add(existing)
    else:
        existing.value_json = observation.value_json
        existing.observation_id = observation.id
        existing.selection_reason = f"highest scored {observation.source_type} observation"
        existing.selected_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(existing)
    return existing
