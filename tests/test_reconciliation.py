from datetime import datetime, timezone

from app.identity import MetadataObservation
from app.reconciliation import observation_score


def _observation(source_type: str, confidence: float) -> MetadataObservation:
    return MetadataObservation(
        game_id=1,
        field_name="title",
        value_json='"Example"',
        source_type=source_type,
        confidence=confidence,
        observed_at=datetime.now(timezone.utc),
    )


def test_curated_observation_outscores_unknown_source_at_equal_confidence():
    assert observation_score(_observation("curated", 0.8)) > observation_score(_observation("unknown", 1.0))


def test_provider_confidence_contributes_to_canonical_selection_score():
    assert observation_score(_observation("provider", 1.0)) > observation_score(_observation("provider", 0.5))
