from atlas import facets
from atlas.web.achievements import TIER_CHOICES
from atlas.web.routes import parse_achievement_line


def test_platinum_is_a_supported_achievement_tier():
    assert "platinum" in facets.ACH_TIERS
    assert ("platinum", "Platinum") in TIER_CHOICES

    [achievement] = facets.clean_achievements([{"name": "Completionist", "tier": "platinum"}])
    assert achievement["tier"] == "platinum"

    imported = parse_achievement_line("Completionist | Complete everything | platinum")
    assert imported["tier"] == "platinum"
