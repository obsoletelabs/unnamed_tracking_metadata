from app.identity_api import normalize_identity
from app.main import Base


def test_identity_normalization_is_case_and_punctuation_insensitive():
    assert normalize_identity("The Witcher 3: Wild Hunt") == "the witcher 3 wild hunt"
    assert normalize_identity("THE-WITCHER 3 / Wild Hunt") == "the witcher 3 wild hunt"


def test_identity_schema_is_registered():
    expected = {
        "provider_mappings",
        "game_aliases",
        "metadata_observations",
        "canonical_fields",
        "game_relationships",
        "game_identity_redirects",
    }
    assert expected.issubset(Base.metadata.tables)
