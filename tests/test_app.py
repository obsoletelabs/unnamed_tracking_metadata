import hashlib
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jinja2 import TemplateSyntaxError
from sqlalchemy import select

from app.main import (ApiToken, Base, EmailVerification, Game, GameAchievement, GameProvenance, GameRevision, GameSubmission, MetadataProvider, SchemaDefinition, SessionLocal, User, app, engine, hash_password, validate_schema_document, discover_provider_ids, utcnow)


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
        if not db.scalar(select(MetadataProvider).where(MetadataProvider.slug == "igdb")):
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




def test_schema_validated_custom_fields_round_trip_and_required_fields(client):
    token = create_api_token()
    with SessionLocal() as db:
        schema = db.scalar(select(SchemaDefinition).where(SchemaDefinition.name == "game"))
        schema.schema_json = json.dumps([
            {"key": "edition", "label": "Edition", "type": "enum", "options": ["standard", "deluxe"], "required": True},
            {"key": "player_count", "label": "Maximum players", "type": "integer"},
            {"key": "is_remake", "label": "Is a remake", "type": "boolean"},
            {"key": "genres", "label": "Genres", "type": "string", "multiple": True},
        ])
        db.commit()
    headers = {"Authorization": f"Bearer {token}"}
    missing = client.post("/api/v1/games", headers=headers, json={"title": "Missing required edition"})
    assert missing.status_code == 422
    response = client.post("/api/v1/games", headers=headers, json={
        "title": "Schema fields round trip",
        "custom_fields": {"edition": "deluxe", "player_count": 4, "is_remake": False, "genres": ["RPG", "Indie"]},
    })
    assert response.status_code == 201, response.text
    assert response.json()["custom_fields"] == {
        "edition": "deluxe", "player_count": 4, "is_remake": False, "genres": ["RPG", "Indie"]
    }


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



def test_self_registration_requires_email_verification_before_login(client, monkeypatch):
    from app import main as main_module
    sent = {}
    monkeypatch.setattr(main_module, "ALLOW_SELF_REGISTRATION", True)
    monkeypatch.setattr(main_module, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(main_module, "SMTP_FROM", "noreply@example.test")
    monkeypatch.setattr(main_module, "send_email", lambda recipient, subject, body: sent.update(body=body))
    response = client.post("/auth/register", data={
        "email": "new-person@example.test", "display_name": "New Person", "password": "long-test-password-123"
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "new-person@example.test"))
        assert user is not None and user.email_verified is False
    login = client.post("/auth/login", data={
        "email": "new-person@example.test", "password": "long-test-password-123"
    }, follow_redirects=False)
    assert login.status_code == 303
    assert "verify" in login.headers["location"].lower()
    token = sent["body"].split("link within 24 hours:\n", 1)[1].splitlines()[0].rsplit("token=", 1)[1]
    verified = client.get("/auth/verify", params={"token": token}, follow_redirects=False)
    assert verified.status_code == 303
    login = client.post("/auth/login", data={
        "email": "new-person@example.test", "password": "long-test-password-123"
    }, follow_redirects=False)
    assert login.headers["location"].startswith("/dashboard")


def test_provider_discovery_attaches_only_an_exact_title_match(client, monkeypatch):
    from app import main as main_module
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
    for name in ("admin.html", "auth.html", "dashboard.html", "catalogue.html", "game_detail.html", "achievements.html", "admin_home.html", "providers.html", "users.html", "game_form.html", "setup.html", "submissions.html", "reviews.html", "game_history.html", "game_edit.html"):
        try:
            templates.env.get_template(name)
        except TemplateSyntaxError as exc:
            pytest.fail(f"{name} has invalid template syntax: {exc}")


def test_root_redirects_to_first_run_setup_when_database_is_empty(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_browser_game_submission_requires_review_and_records_provenance(client, monkeypatch):
    from app import main as main_module
    monkeypatch.setattr(main_module, "ADMIN_EMAILS", {"admin@example.test"})
    with SessionLocal() as db:
        db.add(User(email="contributor@example.test", display_name="Contributor",
                    password_hash=hash_password("contributor-password-123"), email_verified=True))
        db.add(User(email="admin@example.test", display_name="Admin",
                    password_hash=hash_password("admin-password-123"), email_verified=True))
        db.commit()
    login = client.post("/auth/login", data={
        "email": "contributor@example.test", "password": "contributor-password-123"
    }, follow_redirects=False)
    assert login.status_code == 303
    submitted = client.post("/games", data={
        "title": "Reviewed Example Game", "developer": "Example Studio", "release_year": "2024",
        "description": "A record that must be reviewed.", "tags": "demo, adventure",
        "external_ids_json": "[]", "evidence_url": "https://example.test/game",
        "evidence_note": "Official publisher page",
    }, follow_redirects=False)
    assert submitted.status_code == 303
    assert submitted.headers["location"].startswith("/submissions")
    with SessionLocal() as db:
        proposal = db.scalar(select(GameSubmission).where(GameSubmission.proposed_title == "Reviewed Example Game"))
        assert proposal is not None and proposal.status == "pending"
        assert db.scalar(select(Game).where(Game.title == "Reviewed Example Game")) is None
        proposal_id = proposal.id
    client.post("/auth/logout", follow_redirects=False)
    client.post("/auth/login", data={
        "email": "admin@example.test", "password": "admin-password-123"
    }, follow_redirects=False)
    review = client.post(f"/admin/reviews/{proposal_id}/decision", data={
        "decision": "approve", "review_note": "Evidence checked."
    }, follow_redirects=False)
    assert review.status_code == 303
    assert review.headers["location"].startswith("/games/")
    with SessionLocal() as db:
        game = db.scalar(select(Game).where(Game.title == "Reviewed Example Game"))
        proposal = db.get(GameSubmission, proposal_id)
        assert game is not None
        assert proposal.status == "approved"
        assert db.scalar(select(GameProvenance).where(GameProvenance.game_id == game.id)) is not None
        assert db.scalar(select(GameRevision).where(GameRevision.game_id == game.id)) is not None

    client.post("/auth/logout", follow_redirects=False)
    client.post("/auth/login", data={
        "email": "contributor@example.test", "password": "contributor-password-123"
    }, follow_redirects=False)
    canonical_id = game.canonical_id
    proposed_edit = client.post(f"/games/{canonical_id}/propose-edit", data={
        "title": "Reviewed Example Game — Updated", "developer": "Example Studio",
        "release_year": "2025", "description": "Updated after source review.",
        "custom_fields_json": "{}", "tags": "demo, adventure, verified",
        "external_ids_json": "[]", "evidence_url": "https://example.test/updated",
        "evidence_note": "Updated official publisher page",
    }, follow_redirects=False)
    assert proposed_edit.status_code == 303
    with SessionLocal() as db:
        edit_proposal = db.scalar(select(GameSubmission).where(
            GameSubmission.proposed_title == "Reviewed Example Game — Updated"))
        assert edit_proposal is not None and edit_proposal.status == "pending"
        assert db.get(Game, game.id).title == "Reviewed Example Game"
        edit_id = edit_proposal.id
    client.post("/auth/logout", follow_redirects=False)
    client.post("/auth/login", data={
        "email": "admin@example.test", "password": "admin-password-123"
    }, follow_redirects=False)
    approved_edit = client.post(f"/admin/reviews/{edit_id}/decision", data={
        "decision": "approve", "review_note": "Updated source checked."
    }, follow_redirects=False)
    assert approved_edit.status_code == 303
    with SessionLocal() as db:
        edited_game = db.get(Game, game.id)
        assert edited_game.title == "Reviewed Example Game — Updated"
        revisions = db.scalars(select(GameRevision).where(GameRevision.game_id == game.id)
                                .order_by(GameRevision.created_at)).all()
        assert any(item.action == "updated" and '"before"' in item.snapshot_json and '"after"' in item.snapshot_json
                   for item in revisions)
