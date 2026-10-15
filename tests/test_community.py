import hashlib
import uuid

from fastapi.testclient import TestClient

from app.asgi import app
from app.main import ApiToken, Base, SessionLocal, User, engine


def token_for(db, label):
    raw = "atlas_community_test_" + uuid.uuid4().hex
    user = User(email=f"{uuid.uuid4().hex}@example.test", display_name=label, password_hash="test")
    db.add(user)
    db.flush()
    db.add(ApiToken(user_id=user.id, label=label, token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
    db.commit()
    return raw


def test_multiple_image_ratios_vote_and_preferred_selection():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            first = token_for(db, "first")
            second = token_for(db, "second")
        h1 = {"Authorization": f"Bearer {first}"}
        h2 = {"Authorization": f"Bearer {second}"}
        game = client.get("/api/v1/games?q=Hollow%20Knight", headers=h1).json()["items"][0]
        game_id = game["id"]
        cover = client.post("/api/v1/games/%s/images" % game_id, headers=h1, json={
            "url": "https://example.com/cover.jpg", "content_type": "image/jpeg", "role": "cover",
            "width": 1600, "height": 900, "alt_text": "Cover art",
        })
        banner = client.post("/api/v1/games/%s/images" % game_id, headers=h1, json={
            "url": "https://example.com/banner.jpg", "content_type": "image/jpeg", "role": "banner",
            "aspect_ratio": "21:9",
        })
        assert cover.status_code == 201 and cover.json()["aspect_ratio"] == "16:9"
        assert banner.status_code == 201 and banner.json()["aspect_ratio"] == "21:9"
        image_id = cover.json()["id"]
        assert client.post(f"/api/v1/images/{image_id}/vote", headers=h1, json={"value": 1}).status_code == 200
        result = client.get(f"/api/v1/games/{game_id}/images", headers=h2).json()
        assert result["items"]["cover"][0]["preferred"] is True
        assert result["items"]["banner"][0]["preferred"] is False
        assert result["items"]["cover"][0]["upvotes"] == 1
        assert client.post(f"/api/v1/images/{image_id}/vote", headers=h2, json={"value": -1}).status_code == 200
        result = client.get(f"/api/v1/games/{game_id}/images", headers=h2).json()
        assert result["items"]["cover"][0]["score"] == 0
        assert result["items"]["cover"][0]["preferred"] is False


def test_metadata_candidates_are_ranked_by_community_votes_and_exposed_on_game():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            first = token_for(db, "first")
            second = token_for(db, "second")
        h1 = {"Authorization": f"Bearer {first}"}
        h2 = {"Authorization": f"Bearer {second}"}
        game_id = client.get("/api/v1/games?q=Hollow%20Knight", headers=h1).json()["items"][0]["id"]
        a = client.post(f"/api/v1/games/{game_id}/metadata-options", headers=h1, json={
            "field_name": "developer", "value": "Team Cherry Pty Ltd", "source_type": "community", "confidence": 0.9,
        }).json()
        b = client.post(f"/api/v1/games/{game_id}/metadata-options", headers=h2, json={
            "field_name": "developer", "value": "Team Cherry Studio", "source_type": "community", "confidence": 0.8,
        }).json()
        assert a["id"] != b["id"]
        assert client.post(f"/api/v1/metadata-options/{a['id']}/vote", headers=h1, json={"value": 1}).status_code == 200
        assert client.post(f"/api/v1/metadata-options/{a['id']}/vote", headers=h2, json={"value": 1}).status_code == 200
        options = client.get(f"/api/v1/games/{game_id}/metadata-options", headers=h1).json()
        assert options["preferred"]["developer"] == a["id"]
        assert next(item for item in options["items"] if item["id"] == a["id"])["preferred"] is True
        game = client.get(f"/api/v1/games/{game_id}", headers=h1).json()
        assert game["preferred_metadata"]["developer"] == "Team Cherry Pty Ltd"
        assert len(game["images"]) >= 0
