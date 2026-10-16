from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .identity import CanonicalField, GameAlias, GameIdentityRedirect, GameRelationship, MetadataObservation, ProviderMapping
from .main import Db, Game, require_api_token

router = APIRouter(prefix="/api/v1", tags=["identity"])


def normalize_identity(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


class MappingCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    external_id: str = Field(min_length=1, max_length=200)
    entity_type: str = Field(default="game", min_length=1, max_length=40)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_url: str = Field(default="", max_length=1000)


class ObservationCreate(BaseModel):
    field_name: str = Field(min_length=1, max_length=120)
    value: object
    source_type: str = Field(min_length=1, max_length=40)
    source_label: str = Field(default="", max_length=200)
    source_external_id: str = Field(default="", max_length=200)
    source_url: str = Field(default="", max_length=1000)
    licence: str = Field(default="", max_length=200)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class AliasCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=300)
    locale: str = Field(default="", max_length=20)
    alias_type: str = Field(default="alternate", max_length=40)


@router.get("/games/{canonical_id}/identity")
def game_identity(canonical_id: str, db: Db):
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        redirect = db.scalar(select(GameIdentityRedirect).where(GameIdentityRedirect.old_canonical_id == canonical_id))
        if redirect:
            target = db.get(Game, redirect.new_game_id)
            return {"canonical_id": canonical_id, "redirect_to": target.canonical_id if target else None}
        raise HTTPException(status_code=404, detail="Game not found")

    mappings = db.scalars(
        select(ProviderMapping).where(ProviderMapping.game_id == game.id, ProviderMapping.status.in_(["active", "verified"]))
    ).all()
    aliases = db.scalars(select(GameAlias).where(GameAlias.game_id == game.id)).all()
    relationships = db.scalars(
        select(GameRelationship).where(
            (GameRelationship.from_game_id == game.id) | (GameRelationship.to_game_id == game.id)
        )
    ).all()
    fields = db.scalars(select(CanonicalField).where(CanonicalField.game_id == game.id)).all()
    return {
        "canonical_id": game.canonical_id,
        "aliases": [
            {"alias": item.alias, "locale": item.locale, "type": item.alias_type, "normalized": item.normalized_alias}
            for item in aliases
        ],
        "provider_mappings": [
            {"provider": item.provider, "entity_type": item.entity_type, "external_id": item.external_id,
             "confidence": item.confidence, "status": item.status}
            for item in mappings
        ],
        "canonical_fields": [
            {"field": item.field_name, "value": json.loads(item.value_json), "observation_id": item.observation_id,
             "selection_reason": item.selection_reason}
            for item in fields
        ],
        "relationships": [
            {"game_id": db.get(Game, item.to_game_id if item.from_game_id == game.id else item.from_game_id).canonical_id,
             "type": item.relationship_type, "confidence": item.confidence}
            for item in relationships
            if db.get(Game, item.to_game_id if item.from_game_id == game.id else item.from_game_id)
        ],
    }


@router.get("/identities/{provider}/{external_id}")
def resolve_provider_identity(provider: str, external_id: str, db: Db):
    mapping = db.scalar(
        select(ProviderMapping).where(
            ProviderMapping.provider == provider,
            ProviderMapping.external_id == external_id,
            ProviderMapping.status.in_(["active", "verified"]),
        )
    )
    if not mapping:
        raise HTTPException(status_code=404, detail="No active canonical mapping exists")
    game = db.get(Game, mapping.game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Mapped game no longer exists")
    return {"canonical_id": game.canonical_id, "provider": provider, "external_id": external_id,
            "confidence": mapping.confidence, "status": mapping.status}


@router.post("/games/{canonical_id}/identity/mappings", status_code=202)
def propose_mapping(canonical_id: str, payload: MappingCreate, db: Db):
    token, _ = require_api_token(db, required_scope="write")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    existing = db.scalar(select(ProviderMapping).where(
        ProviderMapping.provider == payload.provider,
        ProviderMapping.entity_type == payload.entity_type,
        ProviderMapping.external_id == payload.external_id,
    ))
    if existing:
        if existing.game_id == game.id:
            existing.last_seen_at = datetime.now(timezone.utc)
            existing.confidence = max(existing.confidence, payload.confidence)
            db.commit()
            return {"status": "already-mapped", "mapping_id": existing.id}
        raise HTTPException(status_code=409, detail="Provider identity is already mapped to another canonical game")
    mapping = ProviderMapping(
        game_id=game.id,
        provider=payload.provider,
        entity_type=payload.entity_type,
        external_id=payload.external_id,
        confidence=payload.confidence,
        status="proposed",
        source_url=payload.source_url,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return {"status": "proposed", "mapping_id": mapping.id, "canonical_id": game.canonical_id, "token_id": token.id}


@router.post("/games/{canonical_id}/identity/observations", status_code=202)
def add_observation(canonical_id: str, payload: ObservationCreate, db: Db):
    token, _ = require_api_token(db, required_scope="write")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    observation = MetadataObservation(
        game_id=game.id,
        field_name=payload.field_name,
        value_json=json.dumps(payload.value, ensure_ascii=False, separators=(",", ":")),
        source_type=payload.source_type,
        source_label=payload.source_label,
        source_external_id=payload.source_external_id,
        source_url=payload.source_url,
        licence=payload.licence,
        confidence=payload.confidence,
    )
    db.add(observation)
    db.commit()
    db.refresh(observation)
    return {"status": "observed", "observation_id": observation.id, "token_id": token.id}


@router.post("/games/{canonical_id}/identity/aliases", status_code=202)
def add_alias(canonical_id: str, payload: AliasCreate, db: Db):
    token, _ = require_api_token(db, required_scope="write")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    normalized = normalize_identity(payload.alias)
    existing = db.scalar(select(GameAlias).where(GameAlias.game_id == game.id, GameAlias.normalized_alias == normalized))
    if existing:
        return {"status": "already-present", "alias_id": existing.id}
    alias = GameAlias(game_id=game.id, alias=payload.alias, normalized_alias=normalized,
                      locale=payload.locale, alias_type=payload.alias_type, source=f"api:{token.id}")
    db.add(alias)
    db.commit()
    db.refresh(alias)
    return {"status": "proposed", "alias_id": alias.id}
