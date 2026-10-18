import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.asgi import app
from app.main import ApiToken, Base, MetadataProvider, SessionLocal, User, engine, Tag
from app.tag_registry import ProviderTagMapping, TagAlias, TagRegistryEntry, normalize


def token_for(db):
    raw = "atlas_tag_test_" + uuid.uuid4().hex
    user = User(email=f"{uuid.uuid4().hex}@example.test", display_name="Tag test", password_hash="test")
    db.add(user); db.flush()
    db.add(ApiToken(user_id=user.id, label="test", token_hash=hashlib.sha256(raw.encode()).hexdigest(), token_prefix=raw[:16]))
    db.commit()
    return raw


def test_tag_registry_suggests_aliases_and_normalized_names():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            raw = token_for(db)
            tag = Tag(name="racing"); db.add(tag); db.flush()
            db.add(TagRegistryEntry(tag_id=tag.id, slug="racing", display_name="Racing", category="genre"))
            db.add(TagAlias(tag_id=tag.id, alias="race", normalized_alias=normalize("race"), source="manual")); db.commit()
        response = client.get("/api/v1/tag-registry/suggest?tag=race", headers={"Authorization": f"Bearer {raw}"})
        assert response.status_code == 200
        assert response.json()["items"][0]["canonical_tag"] == "racing"
        assert response.json()["items"][0]["reason"] == "alias"


def test_provider_mapping_resolves_and_unmapped_values_include_suggestions():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        with SessionLocal() as db:
            raw = token_for(db)
            provider = db.scalar(select(MetadataProvider).where(MetadataProvider.slug == "steam"))
            assert provider is not None
            tag = Tag(name="racing"); db.add(tag); db.flush()
            db.add(TagRegistryEntry(tag_id=tag.id, slug="racing", display_name="Racing", category="genre"))
            db.add(ProviderTagMapping(provider_id=provider.id, tag_id=tag.id, external_tag="Race", normalized_tag=normalize("Race"), confidence=.94, mapping_method="llm")); db.commit()
        headers = {"Authorization": f"Bearer {raw}"}
        result = client.get("/api/v1/tag-registry/resolve?provider=steam&tag=Race", headers=headers)
        assert result.status_code == 200 and result.json()["canonical_tag"] == "racing"
        miss = client.get("/api/v1/tag-registry/resolve?provider=steam&tag=racing-like", headers=headers)
        assert miss.status_code == 200 and miss.json()["mapped"] is False
        assert miss.json()["suggestions"][0]["canonical_tag"] == "racing"
