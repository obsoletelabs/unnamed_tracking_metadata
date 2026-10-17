import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.asgi import app
from app.main import ApiToken, Game, SessionLocal, User


def test_mutating_api_rejects_oversized_declared_body():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/games",
            content=b"x" * (1024 * 1024 + 1),
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


def test_invalid_request_id_is_replaced_with_safe_id():
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "bad id"})
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert " " not in request_id
    assert len(request_id) <= 100


def test_idempotency_key_header_is_bounded():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/games",
            headers={
                "Authorization": "Bearer invalid",
                "Idempotency-Key": "x" * 129,
            },
            json={"title": "Not created"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"



def _write_token() -> str:
    raw = "atlas_idempotency_test_" + uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(
            email=f"{uuid.uuid4().hex}@example.test",
            display_name="idempotency-test",
            password_hash="not-used",
        )
        db.add(user)
        db.flush()
        db.add(
            ApiToken(
                user_id=user.id,
                label="idempotency test",
                token_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                token_prefix=raw[:16],
                scopes="read,write",
            )
        )
        db.commit()
    return raw


def test_idempotent_create_replays_successful_response():
    with TestClient(app) as client:
        raw = _write_token()
        headers = {
            "Authorization": f"Bearer {raw}",
            "Idempotency-Key": "create-game-" + uuid.uuid4().hex,
        }
        payload = {"title": "Idempotency " + uuid.uuid4().hex}
        first = client.post("/api/v1/games", headers=headers, json=payload)
        second = client.post("/api/v1/games", headers=headers, json=payload)
        assert first.status_code == 201
        assert second.status_code == 201
        assert second.headers.get("x-idempotent-replay") == "true"
        with SessionLocal() as db:
            count = db.scalar(select(func.count()).select_from(Game).where(Game.title == payload["title"]))
            assert count == 1


def test_idempotency_key_cannot_be_reused_for_different_body():
    with TestClient(app) as client:
        raw = _write_token()
        headers = {
            "Authorization": f"Bearer {raw}",
            "Idempotency-Key": "body-conflict-" + uuid.uuid4().hex,
        }
        first = client.post("/api/v1/games", headers=headers, json={"title": "First idempotent game"})
        second = client.post("/api/v1/games", headers=headers, json={"title": "Different body"})
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_key_reused"
