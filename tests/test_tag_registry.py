import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.asgi import app
from app.main import ApiToken, Base, MetadataProvider, SessionLocal, User, engine, Tag
from app.tag_registry import ProviderTagMapping, TagRegistryEntry, normalize


def test_tag_registry_backfills_and_resolves_provider_tags():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="Tag test", password_hash="test")
            db.add(user); db.flush()
            raw = "atlas_tag_test_" + uuid.uuid4().hex
            db.add(ApiToken(user_id=user.id, label="test", token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
            db.commit()
        headers = {"Authorization": f"Bearer {raw}"}
        assert client.get("/api/v1/tag-registry", headers=headers).status_code == 200
        assert client.get("/api/v1/tag-registry/resolve?provider=steam&tag=racing", headers=headers).json()["mapped"] is False


def test_provider_mapping_resolves_to_one_canonical_tag():
    Base.metadata.drop_all(engine)
    with TestClient(app):
        with SessionLocal() as db:
            user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="Tag test", password_hash="test")
            db.add(user); db.flush()
            raw = "atlas_tag_test_" + uuid.uuid4().hex
            db.add(ApiToken(user_id=user.id, label="test", token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
            provider = db.scalar(select(MetadataProvider).where(MetadataProvider.slug == "steam"))
            assert provider is not None
            tag = Tag(name="racing"); db.add(tag); db.flush()
            db.add(TagRegistryEntry(tag_id=tag.id, slug="racing", display_name="Racing", category="genre"))
            db.add(ProviderTagMapping(provider_id=provider.id, tag_id=tag.id, external_tag="Race", normalized_tag=normalize("Race"), confidence=.94, mapping_method="llm"))
            db.commit()
        result = TestClient(app).get("/api/v1/tag-registry/resolve?provider=steam&tag=racing", headers={"Authorization": f"Bearer {raw}"})
        assert result.status_code == 200
        assert result.json()["canonical_tag"] == "racing"
        assert result.json()["confidence"] == .94
