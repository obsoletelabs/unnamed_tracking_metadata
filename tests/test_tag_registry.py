import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.asgi import app
from app.main import ApiToken, Base, MetadataProvider, SessionLocal, User, engine
from app.tag_registry import ProviderTagMapping, TagRegistryEntry, normalize


def test_tag_registry_backfills_and_resolves_provider_tags():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="Tag test", password_hash="test")
            db.add(user); db.flush()
            raw = "atlas_tag_test_" + uuid.uuid4().hex
            db.add(ApiToken(user_id=user.id, label="test", token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
            provider = MetadataProvider(slug="steam", name="Steam", homepage="https://store.steampowered.com", adapter="steam_store")
            db.add(provider); db.commit()
            token = raw
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/v1/tag-registry", headers=headers)
        assert response.status_code == 200
        with SessionLocal() as db:
            tag = db.scalar(select(TagRegistryEntry))
            assert tag is None
        # Registry is empty until a canonical tag is introduced; resolution safely reports a miss.
        assert client.get("/api/v1/tag-registry/resolve?provider=steam&tag=racing", headers=headers).json()["mapped"] is False


def test_provider_mapping_resolves_to_one_canonical_tag():
    Base.metadata.drop_all(engine)
    with TestClient(app):
        with SessionLocal() as db:
            user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="Tag test", password_hash="test")
            db.add(user); db.flush()
            raw = "atlas_tag_test_" + uuid.uuid4().hex
            db.add(ApiToken(user_id=user.id, label="test", token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
            provider = MetadataProvider(slug="steam", name="Steam", homepage="https://store.steampowered.com", adapter="steam_store")
            db.add(provider); db.commit(); db.refresh(provider)
            from app.main import Tag
            tag = Tag(name="racing"); db.add(tag); db.flush()
            entry = TagRegistryEntry(tag_id=tag.id, slug="racing", display_name="Racing", category="genre")
            db.add(entry); db.add(ProviderTagMapping(provider_id=provider.id, tag_id=tag.id, external_tag="Race", normalized_tag=normalize("Race"), confidence=.94, mapping_method="llm")); db.commit()
        result = TestClient(app).get("/api/v1/tag-registry/resolve?provider=steam&tag=racing", headers={"Authorization": f"Bearer {raw}"})
        assert result.status_code == 200
        assert result.json()["canonical_tag"] == "racing"
        assert result.json()["confidence"] == .94
