"""Request limits must fit the stored columns.

SQLite does not enforce String(N), so these cases only prove something when the suite runs on
PostgreSQL (TEST_DATABASE_URL); CI does both. On SQLite they still check the 422 boundary.
"""

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.asgi import app
from app.main import ApiToken, SessionLocal, User, engine


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def write_headers():
    raw = "atlas_test_" + uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="Limits", password_hash="x")
        db.add(user)
        db.flush()
        db.add(ApiToken(user_id=user.id, label="limits", token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                        token_prefix=raw[:16]))
        db.commit()
    return {"Authorization": f"Bearer {raw}"}


def new_game(client, headers):
    response = client.post("/api/v1/games", headers=headers, json={"title": "Limits", "custom_fields": {}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


CASES = [
    ("mappings", "provider", 80, {"external_id": "unique"}),
    ("mappings", "external_id", 200, {"provider": "steam"}),
    ("mappings", "entity_type", 40, {"provider": "steam", "external_id": "unique"}),
    ("mappings", "source_url", 1000, {"provider": "steam", "external_id": "unique"}),
    ("observations", "field_name", 120, {"value": 1, "source_type": "user"}),
    ("observations", "source_type", 40, {"field_name": "x", "value": 1}),
    ("observations", "source_label", 200, {"field_name": "x", "value": 1, "source_type": "user"}),
    ("observations", "source_external_id", 200, {"field_name": "x", "value": 1, "source_type": "user"}),
    ("observations", "source_url", 1000, {"field_name": "x", "value": 1, "source_type": "user"}),
    ("observations", "licence", 200, {"field_name": "x", "value": 1, "source_type": "user"}),
    ("aliases", "alias", 300, {}),
    ("aliases", "locale", 20, {"alias": "Name"}),
    ("aliases", "alias_type", 40, {"alias": "Name"}),
]


@pytest.mark.parametrize(("route", "field", "limit", "rest"), CASES)
def test_identity_fields_accept_their_limit_and_refuse_one_more(client, route, field, limit, rest):
    headers = write_headers()
    canonical_id = new_game(client, headers)
    url = f"/api/v1/games/{canonical_id}/identity/{route}"
    rest = {key: (uuid.uuid4().hex if value == "unique" else value) for key, value in rest.items()}
    fits = client.post(url, headers=headers, json={**rest, field: "a" * limit})
    assert fits.status_code == 202, fits.text
    over = client.post(url, headers=headers, json={**rest, field: "b" * (limit + 1)})
    assert over.status_code == 422, over.text


def test_the_test_database_enforces_declared_lengths():
    """Guards the guard: on PostgreSQL an oversized value must be rejected by the database."""
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite does not enforce String(N); run with TEST_DATABASE_URL on PostgreSQL")
    from sqlalchemy.exc import DataError

    with SessionLocal() as db:
        db.add(User(email="x" * 400 + "@example.test", display_name="d", password_hash="h"))
        with pytest.raises(DataError):
            db.flush()
