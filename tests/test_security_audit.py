import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.asgi import app
from app.main import ApiToken, Base, SessionLocal, User, engine
from app.security_audit import SecurityAuditEvent


def test_api_mutations_create_audit_events():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            raw = "atlas_audit_test_" + uuid.uuid4().hex
            user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="audit", password_hash="test")
            db.add(user)
            db.flush()
            db.add(ApiToken(user_id=user.id, label="audit", token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
            db.commit()
        response = client.post("/api/v1/games", headers={"Authorization": f"Bearer {raw}"}, json={"title": "Audit Test"})
        assert response.status_code == 201
        with SessionLocal() as db:
            event = db.scalar(select(SecurityAuditEvent).where(SecurityAuditEvent.path == "/api/v1/games").order_by(SecurityAuditEvent.id.desc()))
            assert event is not None
            assert event.method == "POST"
            assert event.status_code == 201
            assert event.principal_hash
