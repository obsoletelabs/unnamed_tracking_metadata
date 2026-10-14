import hashlib
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jinja2 import TemplateSyntaxError

from app.main import ApiToken, MetadataProvider, SessionLocal, app, validate_schema_document


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def create_api_token():
    raw = "atlas_test_" + uuid.uuid4().hex
    with SessionLocal() as db:
        from app.main import User
        user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="API test", password_hash="test-hash")
        db.add(user)
        db.flush()
        db.add(ApiToken(user_id=user.id, label="test", token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
        db.add(MetadataProvider(slug="igdb", name="IGDB", homepage="https://igdb.com"))
        db.commit()
    return raw


def test_schema_definition_accepts_supported_fields():
    fields = [
        {"key": "genres", "label": "Genres", "type": "string", "multiple": True},
        {"key": "edition", "label": "Edition", "type": "enum", "options": ["standard", "deluxe"]},
        {"key": "is_remake", "label": "Is a remake", "type": "boolean"},
    ]
    assert validate_schema_document(json.dumps(fields)) == fields


def test_schema_definition_rejects_duplicate_keys():
    raw = json.dumps([
        {"key": "genres", "label": "Genres", "type": "string"},
        {"key": "genres", "label": "Other genres", "type": "string"},
    ])
    with pytest.raises(HTTPException) as error:
        validate_schema_document(raw)
    assert error.value.status_code == 422


def test_schema_definition_rejects_non_string_enum_options():
    raw = json.dumps([{"key": "edition", "label": "Edition", "type": "enum", "options": [1, 2]}])
    with pytest.raises(HTTPException):
        validate_schema_document(raw)


def test_api_reads_require_a_real_key(client):
    assert client.get("/api/v1/games").status_code == 401
    assert client.get("/api/v1/games", headers={"Authorization": "Bearer not-a-real-key"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_browser_search_requires_login(client):
    assert client.get("/catalogue/search?q=minecraft").status_code == 401


def test_authenticated_api_can_create_tagged_game_with_provider_id(client):
    token = create_api_token()
    response = client.post(
        "/api/v1/games",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Schema test game", "tags": ["Indie", "Puzzle"],
              "external_ids": [{"provider": "igdb", "external_id": "test-game-001"}], "custom_fields": {}},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["title"] == "Schema test game"
    assert record["tags"] == ["indie", "puzzle"]
    assert record["external_ids"][0]["provider"] == "igdb"
    assert record["external_ids"][0]["external_id"] == "test-game-001"


def test_templates_parse():
    from app.main import templates
    for name in ("index.html", "admin.html"):
        try:
            templates.env.get_template(name)
        except TemplateSyntaxError as exc:
            pytest.fail(f"{name} has invalid template syntax: {exc}")
