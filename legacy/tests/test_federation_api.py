import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.asgi import app
from app.authority import config
from app.federation import AuthorityMapping
from app.main import ApiToken, Game, SessionLocal, User


def _write_token() -> tuple[str, str]:
    raw = "atlas_federation_test_" + uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(
            email=f"{uuid.uuid4().hex}@example.test",
            display_name="federation-test",
            password_hash="not-used",
        )
        db.add(user)
        db.flush()
        db.add(
            ApiToken(
                user_id=user.id,
                label="federation test",
                token_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                token_prefix=raw[:16],
                scopes="read,write",
            )
        )
        game = Game(canonical_id="gm_" + uuid.uuid4().hex[:12], title="Local Only Test Game")
        db.add(game)
        db.commit()
        return raw, game.canonical_id


def test_authority_status_does_not_expose_central_url_or_secrets():
    with TestClient(app) as client:
        response = client.get("/api/v1/authority/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] in {"local", "central", "hybrid"}
    assert "central_url" not in payload
    assert "token" not in payload


def test_mapping_proposal_preserves_local_canonical_identity():
    if config.is_central:
        return
    with TestClient(app) as client:
        raw, local_id = _write_token()
        response = client.post(
            "/api/v1/authority/mappings",
            headers={"Authorization": f"Bearer {raw}"},
            json={
                "local_canonical_id": local_id,
                "central_canonical_id": "gm_central123",
                "confidence": 0.8,
                "source": "manual-review",
            },
        )
    assert response.status_code == 202
    assert response.json()["status"] == "proposed"
    with SessionLocal() as db:
        mapping = db.scalar(
            select(AuthorityMapping).where(AuthorityMapping.local_canonical_id == local_id)
        )
        game = db.scalar(select(Game).where(Game.canonical_id == local_id))
        assert mapping is not None and mapping.status == "proposed"
        assert game is not None and game.canonical_id == local_id
