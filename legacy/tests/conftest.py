import os
import sys
import tempfile
from pathlib import Path

# Make imports deterministic when pytest is launched from different working directories.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

_TEST_ROOT = tempfile.mkdtemp(prefix="atlas-metadata-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TEST_ROOT, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_TEST_ROOT, "uploads")
os.environ["SESSION_SECRET"] = "test-only-session-secret-not-for-production"
os.environ["ADMIN_EMAILS"] = "admin@example.test"

import pytest


@pytest.fixture(autouse=True)
def disable_automatic_provider_network_calls(monkeypatch):
    # Provider discovery has dedicated mocked tests; API tests should not call third parties.
    from app import main as main_module
    monkeypatch.setattr(main_module, "discover_game_ids_background", lambda game_id: None)
