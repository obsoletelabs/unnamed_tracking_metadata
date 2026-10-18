import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.asgi import app
from app.main import ApiToken, SessionLocal, User


def _token(*, scopes: str, expires_at=None) -> str:
    raw = "atlas_token_test_" + uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(
            email=f"{uuid.uuid4().hex}@example.test",
            display_name="token-test",
            password_hash="not-used",
        )
        db.add(user)
        db.flush()
        db.add(
            ApiToken(
                user_id=user.id,
                label="test token",
                token_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                token_prefix=raw[:16],
                scopes=scopes,
                expires_at=expires_at,
            )
        )
        db.commit()
    return raw


def test_expired_api_token_is_rejected():
    with TestClient(app) as client:
        raw = _token(scopes="read", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        response = client.get("/api/v1/games", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 401
    assert "expired" in response.json()["error"]["message"].lower()


def test_read_only_token_cannot_create_game():
    with TestClient(app) as client:
        raw = _token(scopes="read")
        response = client.post(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {raw}"},
            json={"title": "Must Not Be Created"},
        )
    assert response.status_code == 403


def test_api_token_last_used_is_recorded():
    with TestClient(app) as client:
        raw = _token(scopes="read")
        response = client.get("/api/v1/games", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with SessionLocal() as db:
        token = db.query(ApiToken).filter(ApiToken.token_hash == digest).one()
        assert token.last_used_at is not None
