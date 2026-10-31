"""The federation lab starts five real servers per scenario, so it is opt-in.

Run it with `python -m tools.federation_lab` (or set ATLAS_FEDERATION_LAB=1 and run pytest on this
folder). A plain `pytest` run collects the scenarios and skips them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.federation_lab import Lab

ENABLED = os.environ.get("ATLAS_FEDERATION_LAB") == "1"


def pytest_collection_modifyitems(config, items):
    if ENABLED:
        return
    skip = pytest.mark.skip(reason="federation lab is opt-in: python -m tools.federation_lab (ATLAS_FEDERATION_LAB=1)")
    for item in items:
        if "federation_lab" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def lab(request):
    """A fresh, wired five-node network per scenario; every credential is generated for the test."""
    log_dir = os.environ.get("ATLAS_LAB_LOG_DIR")
    keep = Path(log_dir) / request.node.name.replace("[", "_").replace("]", "") if log_dir else None
    with Lab(postgres_admin_url=os.environ.get("ATLAS_LAB_PG_URL") or None, keep_logs=keep) as network:
        network.wire()
        yield network
