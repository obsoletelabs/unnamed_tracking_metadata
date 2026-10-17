from __future__ import annotations

from fastapi import APIRouter

from .authority import config

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
