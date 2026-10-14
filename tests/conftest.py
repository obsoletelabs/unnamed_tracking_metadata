import os
import tempfile
import uuid

_TEST_ROOT = tempfile.mkdtemp(prefix="atlas-metadata-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TEST_ROOT, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_TEST_ROOT, "uploads")
os.environ["SESSION_SECRET"] = "test-only-session-secret-not-for-production"
os.environ["ADMIN_EMAILS"] = "admin@example.test"
