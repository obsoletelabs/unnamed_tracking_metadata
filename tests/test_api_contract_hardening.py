from fastapi.testclient import TestClient

from app.asgi import app


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
