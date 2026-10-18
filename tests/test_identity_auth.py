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


def test_identity_lookup_rejects_invalid_bearer_token():
    # Invalid bearer credentials must never fall through to an unauthenticated response.
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/identities/steam/12345",
            headers={"Authorization": "Bearer definitely-not-a-valid-token"},
        )
        assert response.status_code == 401


def _mapped_game(provider_pairs):
    """A canonical game with active provider mappings, and a read token."""
    import hashlib
    import uuid

    from app.identity import ProviderMapping
    from app.main import ApiToken, Game, SessionLocal, User, new_canonical_id

    raw = "atlas_test_" + uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="t", password_hash="x")
        db.add(user)
        db.flush()
        db.add(ApiToken(user_id=user.id, label="t", token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                        token_prefix=raw[:16]))
        game = Game(canonical_id=new_canonical_id(), title="Hades " + uuid.uuid4().hex[:6])
        db.add(game)
        db.flush()
        for provider, external_id in provider_pairs:
            db.add(ProviderMapping(game_id=game.id, provider=provider, external_id=external_id, status="active"))
        db.commit()
        return raw, game.canonical_id


def test_provider_identity_lookup_reads_the_token_from_the_header_only():
    import uuid

    steam = uuid.uuid4().hex[:10]
    token, canonical = _mapped_game([("steam", steam)])
    with TestClient(app) as client:
        found = client.get(f"/api/v1/identities/steam/{steam}", headers={"Authorization": f"Bearer {token}"})
        assert found.status_code == 200, found.text
        assert found.json()["canonical_id"] == canonical
        in_url = client.get(f"/api/v1/identities/steam/{steam}", params={"authorization": f"Bearer {token}"})
        assert in_url.status_code == 401


def test_batch_resolve_names_one_canonical_game_for_copies_from_several_stores():
    import uuid

    steam, epic = uuid.uuid4().hex[:10], uuid.uuid4().hex[:10]
    token, canonical = _mapped_game([("steam", steam), ("epic", epic)])
    body = {"items": [{"provider": "steam", "external_id": steam},
                      {"provider": "epic", "external_id": epic},
                      {"provider": "gog", "external_id": "unknown"}]}
    with TestClient(app) as client:
        assert client.post("/api/v1/identities/resolve", json=body).status_code == 401
        resolved = client.post("/api/v1/identities/resolve", json=body, headers={"Authorization": f"Bearer {token}"})
        assert resolved.status_code == 200, resolved.text
        items = resolved.json()["items"]
        assert [item["canonical_id"] for item in items] == [canonical, canonical, None]
        assert items[2]["status"] == "unmapped"
        too_many = {"items": [{"provider": "steam", "external_id": str(n)} for n in range(201)]}
        assert client.post("/api/v1/identities/resolve", json=too_many,
                           headers={"Authorization": f"Bearer {token}"}).status_code == 422
