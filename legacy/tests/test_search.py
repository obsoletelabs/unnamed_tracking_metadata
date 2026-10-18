import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.asgi import app
from app.identity import GameAlias
from app.main import ApiToken, Base, Game, SessionLocal, User, engine


def token_for(db):
    raw = "atlas_search_test_" + uuid.uuid4().hex
    user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="search", password_hash="test")
    db.add(user)
    db.flush()
    db.add(ApiToken(user_id=user.id, label="search", token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
    db.commit()
    return raw


def test_search_normalizes_punctuation_and_uses_aliases():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            token = token_for(db)
            game = db.scalar(select(Game).where(Game.title == "Hollow Knight"))
            db.add(GameAlias(game_id=game.id, alias="Hollow-Knight", normalized_alias="hollow knight", alias_type="alternate", source="test"))
            db.commit()
        headers = {"Authorization": f"Bearer {token}"}
        result = client.get("/api/v1/search?query=hollow-knight&limit=5", headers=headers)
        assert result.status_code == 200
        assert result.json()["items"][0]["title"] == "Hollow Knight"
        assert result.json()["normalized_query"] == "hollow knight"


def test_search_budget_is_explicit_and_bounded():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            token = token_for(db)
        response = client.get("/api/v1/search/budget", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["max_results"] == 100
        assert response.json()["enrichment_top_n"] == 3
