from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from .authority import config
from .federation import AuthorityMapping
from .main import Db

router = APIRouter()


@router.get("/api/v1/authority")
def authority_info():
    """Public capability metadata; never exposes credentials or private source configuration."""
    return {
        "mode": config.mode,
        "instance_id": config.instance_id,
        "canonical_identity": "local" if config.is_local else "central",
        "offline_capable": config.is_local or config.mode == "hybrid",
        "central_sync": config.can_sync,
        "publish_contributions": config.publish_contributions,
        "features": {
            "provider_mappings": True,
            "provenance": True,
            "community_curation": True,
            "content_addressed_media": True,
            "local_overrides": config.mode in {"local", "hybrid"},
        },
    }


@router.get("/api/v1/authority/mappings")
def authority_mappings(db: Db, limit: int = 100):
    """Return local-to-central mappings for sync clients without exposing private fields."""
    limit = max(1, min(limit, 500))
    rows = db.scalars(select(AuthorityMapping).order_by(AuthorityMapping.id).limit(limit)).all()
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
        "mode": config.mode,
    }
