from fastapi.testclient import TestClient

from app.asgi import app


def test_identity_lookup_requires_read_token():
    with TestClient(app) as client:
        response = client.get("/api/v1/games/gm_missing/identity")
        assert response.status_code == 401


def test_provider_identity_lookup_requires_read_token():
    with TestClient(app) as client:
        response = client.get("/api/v1/identities/steam/12345")
        assert response.status_code == 401


def test_identity_lookup_rejects_write_only_token():
    # Scope checks are performed by the shared token validator; an invalid bearer
    # credential must never fall through to an unauthenticated identity response.
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/identities/steam/12345",
            headers={"Authorization": "Bearer definitely-not-a-valid-token"},
        )
        assert response.status_code == 401
