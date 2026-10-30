import os
import sys
import tempfile
from pathlib import Path

# Make imports deterministic when pytest is launched from different working directories.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

_TEST_ROOT = tempfile.mkdtemp(prefix="atlas-metadata-tests-")
# SQLite ignores String(N), so a length bug only shows on PostgreSQL: CI also runs the suite with
# TEST_DATABASE_URL pointing at an empty PostgreSQL database.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite:///" + os.path.join(_TEST_ROOT, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_TEST_ROOT, "uploads")
os.environ["SESSION_SECRET"] = "test-only-session-secret-not-for-production"
os.environ["ADMIN_EMAILS"] = "admin@example.test"

import pytest


@pytest.fixture(scope="session", autouse=True)
def forget_applied_migrations_when_tables_are_dropped():
    """Tests reset the schema with Base.metadata.drop_all, which leaves the schema_migrations
    marker behind; the next app start would then skip the migrations and find no tables."""
    from sqlalchemy import event, text

    from app.main import Base

    def forget(target, connection, **kwargs):
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))

    event.listen(Base.metadata, "after_drop", forget)
    yield
    event.remove(Base.metadata, "after_drop", forget)


@pytest.fixture(autouse=True)
def disable_automatic_provider_network_calls(monkeypatch):
    # Provider discovery has dedicated mocked tests; API tests should not call third parties.
    from app import main as main_module
    monkeypatch.setattr(main_module, "discover_game_ids_background", lambda game_id: None)
