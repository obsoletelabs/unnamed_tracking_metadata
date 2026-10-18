import hashlib
import uuid

from fastapi.testclient import TestClient

from app.asgi import app
from app.main import ApiToken, Base, SessionLocal, User, engine


def token_for(db, label):
    raw = "atlas_browser_vote_test_" + uuid.uuid4().hex
    user = User(email=f"{uuid.uuid4().hex}@example.test", display_name=label, password_hash="test")
    db.add(user)
    db.flush()
    db.add(ApiToken(user_id=user.id, label=label, token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
    db.commit()
    return raw


def test_browser_community_vote_requires_a_signed_session():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            token = token_for(db, "browser")
        headers = {"Authorization": f"Bearer {token}"}
        game_id = client.get("/api/v1/games?q=Hollow%20Knight", headers=headers).json()["items"][0]["id"]
        image = client.post(f"/api/v1/games/{game_id}/images/add", headers=headers, json={
            "url": "https://example.com/community.jpg", "role": "cover", "aspect_ratio": "2:3",
        }).json()
        response = client.post(
            f"/games/{game_id}/community/image-vote",
            data={"image_id": image["id"], "value": 1},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
