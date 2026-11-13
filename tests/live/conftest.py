"""Live provider checks (issue #26) call the real Steam, RAWG, IGDB, TheGamesDB, Open Library and Wikidata APIs,
so they are opt-in: set ATLAS_LIVE_PROVIDERS=1. A plain `pytest` run collects them and skips them.

Each keyed provider also needs its credentials in the environment (the same variables a node reads:
ATLAS_RAWG_API_KEY, ATLAS_IGDB_CLIENT_ID + ATLAS_IGDB_CLIENT_SECRET, ATLAS_TGDB_API_KEY, optional
ATLAS_STEAM_API_KEY); without them that provider's checks skip and name the missing variable. Keyless providers
(Steam storefront, Wikidata) run whenever the switch is on. Open Library also needs ATLAS_LIVE_OPENLIBRARY=1: it is
checked on manual dispatch only (CONTRACT-C15 §3.4, the `Live Open Library` workflow).

ATLAS_LIVE_REPORT names a JSON file that receives, per provider, which fields each answer filled (never the
values). Comparing two reports shows response-shape drift (the genre/theme/language/score/alias fields of #26).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

ENABLED = os.environ.get("ATLAS_LIVE_PROVIDERS") == "1"
_REPORT: dict[str, dict] = {}


def pytest_collection_modifyitems(config, items):
    if ENABLED:
        return
    skip = pytest.mark.skip(reason="live provider checks are opt-in: ATLAS_LIVE_PROVIDERS=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("ATLAS_LIVE_REPORT")
    if ENABLED and path and _REPORT:
        Path(path).write_text(json.dumps(_REPORT, indent=2, sort_keys=True), encoding="utf-8")


def need(*names: str) -> dict[str, str]:
    """The named environment variables, or skip the test naming the missing ones."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} (a repository secret in the live-providers workflow)")
    return {n: os.environ[n] for n in names}


@pytest.fixture
def report():
    return _REPORT


@pytest.fixture(scope="session")
def client():
    with httpx.Client(headers={"User-Agent": "atlas-metadata/2 (live provider checks; "
                                             "https://github.com/obsoletelabs/unnamed_tracking_metadata)"},
                      timeout=20) as c:
        yield c
