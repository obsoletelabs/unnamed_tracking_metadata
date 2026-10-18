from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .authority import config
from .federation import AuthorityMapping
from .main import Db, Game, require_api_token

router = APIRouter(prefix="/api/v1/authority", tags=["authority"])


class MappingProposal(BaseModel):
    local_canonical_id: str = Field(min_length=1, max_length=32)
    central_canonical_id: str | None = Field(default=None, min_length=1, max_length=32)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: str = Field(default="manual", min_length=1, max_length=80)


@router.get("/status")
def authority_status():
    return {
        "mode": config.mode,
        "instance_id": config.instance_id,
        "central_configured": bool(config.central_url),
        "sync_enabled": config.can_sync,
        "publishing_enabled": config.publish_contributions,
        "sync_interval_seconds": config.sync_interval_seconds,
        "sync_implementation": "not yet enabled",
    }


@router.get("/mappings")
def list_authority_mappings(
    db: Db,
    limit: int = 50,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    require_api_token(db, authorization, "read")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    if offset < 0 or offset > 1_000_000:
        raise HTTPException(status_code=422, detail="offset must be between 0 and 1000000")
    rows = db.scalars(
        select(AuthorityMapping)
        .order_by(AuthorityMapping.local_canonical_id)
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                "local_canonical_id": row.local_canonical_id,
                "central_canonical_id": row.central_canonical_id,
                "status": row.status,
                "confidence": row.confidence,
                "source": row.source,
                "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
                "etag": row.etag,
            }
            for row in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.post("/mappings", status_code=202)
def propose_authority_mapping(
    payload: MappingProposal,
    db: Db,
    authorization: str | None = Header(default=None),
):
    require_api_token(db, authorization, "write")
    local_game = db.scalar(select(Game).where(Game.canonical_id == payload.local_canonical_id))
    if local_game is None:
        raise HTTPException(status_code=404, detail="Local canonical game not found")
    if config.is_central:
        raise HTTPException(status_code=409, detail="Central authority does not accept local-to-central mapping proposals")
    existing = db.scalar(
        select(AuthorityMapping).where(AuthorityMapping.local_canonical_id == payload.local_canonical_id)
    )
    if existing and existing.status == "verified":
        if existing.central_canonical_id == payload.central_canonical_id:
            return {"status": "already-verified", "local_canonical_id": existing.local_canonical_id,
                    "central_canonical_id": existing.central_canonical_id}
        raise HTTPException(status_code=409, detail="Verified identity mappings cannot be overwritten by an API proposal")
    if existing is None:
        existing = AuthorityMapping(
            local_canonical_id=payload.local_canonical_id,
            central_canonical_id=payload.central_canonical_id,
            status="proposed",
            confidence=payload.confidence,
            source=payload.source,
        )
        db.add(existing)
    else:
        existing.central_canonical_id = payload.central_canonical_id
        existing.status = "proposed"
        existing.confidence = payload.confidence
        existing.source = payload.source
        existing.last_synced_at = None
        existing.etag = ""
    db.commit()
    return {
        "status": "proposed",
        "local_canonical_id": payload.local_canonical_id,
        "central_canonical_id": payload.central_canonical_id,
        "confidence": payload.confidence,
        "note": "Proposal only; it does not merge or replace either canonical identity.",
    }
