import hashlib
import uuid

from fastapi.testclient import TestClient

from app.asgi import app
from app.main import ApiToken, Base, SessionLocal, User, engine


def token_for(db, label):
    raw = "atlas_presentation_test_" + uuid.uuid4().hex
    user = User(email=f"{uuid.uuid4().hex}@example.test", display_name=label, password_hash="test")
    db.add(user)
    db.flush()
    db.add(ApiToken(user_id=user.id, label=label, token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
    db.commit()
    return raw


def test_presentation_selects_best_positive_image_for_requested_ratio():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            token = token_for(db, "selector")
        headers = {"Authorization": f"Bearer {token}"}
        game_id = client.get("/api/v1/games?q=Hollow%20Knight", headers=headers).json()["items"][0]["id"]
        portrait = client.post(f"/api/v1/games/{game_id}/images/add", headers=headers, json={
            "url": "https://example.com/portrait.jpg", "role": "cover", "aspect_ratio": "2:3",
        }).json()
        landscape = client.post(f"/api/v1/games/{game_id}/images/add", headers=headers, json={
            "url": "https://example.com/landscape.jpg", "role": "cover", "aspect_ratio": "16:9",
        }).json()
        client.post(f"/api/v1/images/{portrait['id']}/vote", headers=headers, json={"value": 1})
        client.post(f"/api/v1/images/{landscape['id']}/vote", headers=headers, json={"value": 1})
        result = client.get(f"/api/v1/games/{game_id}/presentation?role=cover&aspect_ratio=16:9", headers=headers).json()
        assert result["preferred"]["id"] == landscape["id"]
        assert result["preferred"]["aspect_ratio"] == "16:9"


def test_presentation_requires_positive_community_consensus():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            token = token_for(db, "selector")
        headers = {"Authorization": f"Bearer {token}"}
        game_id = client.get("/api/v1/games?q=Hollow%20Knight", headers=headers).json()["items"][0]["id"]
        image = client.post(f"/api/v1/games/{game_id}/images/add", headers=headers, json={
            "url": "https://example.com/unvoted.jpg", "role": "cover", "aspect_ratio": "2:3",
        }).json()
        result = client.get(f"/api/v1/games/{game_id}/presentation?role=cover&aspect_ratio=2:3", headers=headers).json()
        assert result["preferred"] is None
        assert result["alternatives"][0]["id"] == image["id"]
