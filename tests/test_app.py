import hashlib
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jinja2 import TemplateSyntaxError
from sqlalchemy import select

from app.main import (ApiToken, Base, EmailVerification, Game, GameAchievement, MetadataProvider, SessionLocal, User, app, engine, hash_password, validate_schema_document, discover_provider_ids, utcnow)


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    yield


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


def test_image_reads_also_require_an_api_key(client):
    assert client.get("/api/v1/images/unknown.png").status_code == 401


def test_authenticated_search_returns_catalogue_records(client):
    token = create_api_token()
    response = client.get("/api/v1/games?q=Hollow", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert any(item["title"] == "Hollow Knight" for item in response.json()["items"])


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



def test_registration_page_is_disabled_without_smtp(client):
    response = client.get("/register", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_game_achievements_are_api_key_protected_and_supported(client):
    token = create_api_token()
    create = client.post("/api/v1/games", headers={"Authorization": f"Bearer {token}"},
                         json={"title": "Achievement test game"})
    assert create.status_code == 201, create.text
    game_id = create.json()["id"]
    assert client.get(f"/api/v1/games/{game_id}/achievements").status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(f"/api/v1/games/{game_id}/achievements", headers=headers,
                           json={"name": "First steps", "description": "Complete the tutorial"})
    assert response.status_code == 201, response.text
    items = client.get(f"/api/v1/games/{game_id}/achievements", headers=headers).json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "First steps"


def test_admin_page_and_account_creation_require_admin(client):
    with SessionLocal() as db:
        db.add(User(email="admin@example.test", display_name="Admin",
                    password_hash=hash_password("admin-test-password-123"), email_verified=True))
        db.commit()
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    login = client.post("/auth/login", data={"email": "admin@example.test", "password": "admin-test-password-123"},
                        follow_redirects=False)
    assert login.status_code == 303
    admin_page = client.get("/admin")
    assert admin_page.status_code == 200
    created = client.post("/admin/users/create", data={
        "email": "new-user@example.test", "display_name": "New User", "password": "user-test-password-123"
    }, follow_redirects=False)
    assert created.status_code == 303
    with SessionLocal() as db:
        new_user = db.scalar(select(User).where(User.email == "new-user@example.test"))
        assert new_user is not None and new_user.email_verified is True



def test_first_run_setup_is_available_before_any_accounts_exist(client):
    response = client.get("/setup")
    assert response.status_code == 200
    assert "Create administrator" in response.text


def test_email_verification_activates_a_pending_account(client):
    raw_token = "verify-" + uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(email="pending@example.test", display_name="Pending",
                    password_hash=hash_password("pending-password-123"), email_verified=False)
        db.add(user)
        db.flush()
        db.add(EmailVerification(user_id=user.id, token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                                 expires_at=utcnow().replace(tzinfo=None) + __import__("datetime").timedelta(hours=1)))
        db.commit()
    response = client.get("/auth/verify", params={"token": raw_token}, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "pending@example.test"))
        assert user.email_verified is True


def test_provider_discovery_attaches_only_an_exact_title_match(client, monkeypatch):
    from app import main as main_module
    with TestClient(app) as test_client:
        token = create_api_token()
        with SessionLocal() as db:
            provider = db.scalar(select(MetadataProvider).where(MetadataProvider.slug == "igdb"))
            provider.adapter = "steam_store"
            game = Game(canonical_id="gm_DISCOVERY", title="Discovery Exact Game", developer="Test",
                        custom_fields="{}")
            db.add(game)
            db.commit()
            game_id = game.id
        class FakeResponse:
            def raise_for_status(self):
                return None
            def json(self):
                return {"items": [{"id": 98765, "name": "Discovery Exact Game"}]}
        monkeypatch.setattr(main_module.httpx, "get", lambda *args, **kwargs: FakeResponse())
        with SessionLocal() as db:
            game = db.get(Game, game_id)
            result = discover_provider_ids(db, game)
            assert result["added"][0]["external_id"] == "98765"
            assert not result["suggestions"]


def test_templates_parse():
    from app.main import templates
    for name in ("index.html", "admin.html", "auth.html", "dashboard.html", "catalogue.html", "game_detail.html", "achievements.html", "admin_home.html", "providers.html", "users.html", "game_form.html", "setup.html"):
        try:
            templates.env.get_template(name)
        except TemplateSyntaxError as exc:
            pytest.fail(f"{name} has invalid template syntax: {exc}")
