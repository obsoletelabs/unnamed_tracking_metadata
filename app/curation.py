from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import func, select

from . import community
from .main import Db, Game, require_api_token

router = APIRouter()


def ratio_parts(value: str | None) -> tuple[int, int] | None:
    if not value or ":" not in value:
        return None
    try:
        width, height = (int(part) for part in value.split(":", 1))
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def ratio_distance(actual: str, requested: str | None) -> tuple[int, float]:
    if not requested:
        return 0, 0.0
    want = ratio_parts(requested)
    got = ratio_parts(actual)
    if not want or not got:
        return 2, 999.0
    if want == got:
        return 0, 0.0
    want_value = want[0] / want[1]
    got_value = got[0] / got[1]
    # Prefer the same orientation before choosing a merely numerically closer ratio.
    orientation_penalty = 0 if (want_value >= 1) == (got_value >= 1) else 1
    return 1 + orientation_penalty, abs(want_value - got_value)


def presentation_images(db, game_id: int, role: str, requested_ratio: str | None, user_id: int | None) -> list[dict]:
    rows = community.ranked_images(db, game_id, role)
    serialized = [community.serialize_image(db, row, user_id) for row in rows]
    return sorted(
        serialized,
        key=lambda item: (
            ratio_distance(item["aspect_ratio"], requested_ratio),
            -(item["score"]),
            -(item["upvotes"]),
            item["created_at"] or "",
            item["id"],
        ),
    )


@router.get("/api/v1/games/{canonical_id}/presentation")
def presentation(canonical_id: str, db: Db, role: str = "cover", aspect_ratio: str | None = None,
                 authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "read")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        raise HTTPException(404, "Game not found")
    if role not in community.IMAGE_ROLES:
        raise HTTPException(422, "Invalid image role")
    if aspect_ratio is not None and community.normalize_ratio(aspect_ratio, None, None) == "unknown":
        raise HTTPException(422, "aspect_ratio must use a positive W:H format")
    candidates = presentation_images(db, game.id, role, aspect_ratio, user.id)
    preferred = candidates[0] if candidates and candidates[0]["score"] > 0 else None
    return {
        "game_id": canonical_id,
        "role": role,
        "requested_aspect_ratio": aspect_ratio,
        "preferred": preferred,
        "alternatives": candidates,
        "selection": "community_score_then_aspect_ratio" if preferred else "none",
    }


@router.get("/api/v1/games/{canonical_id}/community-summary")
def community_summary(canonical_id: str, db: Db, authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "read")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        raise HTTPException(404, "Game not found")
    data = community.community_game_data(db, game, user.id)
    image_votes = sum(item["upvotes"] + item["downvotes"] for item in data["images"])
    metadata_votes = sum(item["upvotes"] + item["downvotes"] for item in data["metadata_options"])
    return {
        "game_id": canonical_id,
        "image_count": len(data["images"]),
        "image_vote_count": image_votes,
        "metadata_option_count": len(data["metadata_options"]),
        "metadata_vote_count": metadata_votes,
        "preferred_images": data["preferred_images"],
        "preferred_metadata": data["preferred_metadata"],
        "roles": data["images"] and sorted({item["role"] for item in data["images"]}) or [],
    }


@router.get("/api/v1/games/{canonical_id}/community-leaderboard")
def community_leaderboard(canonical_id: str, db: Db, field: str = "", role: str = "",
                          authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "read")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        raise HTTPException(404, "Game not found")
    if role and role not in community.IMAGE_ROLES:
        raise HTTPException(422, "Invalid image role")
    if field and field not in community.METADATA_FIELDS:
        raise HTTPException(422, "Unsupported metadata field")
    images = [community.serialize_image(db, image, user.id) for image in community.ranked_images(db, game.id, role)] if role or not field else []
    metadata = []
    if field or not role:
        rows = db.scalars(select(community.MetadataCandidate).where(
            community.MetadataCandidate.game_id == game.id,
            community.MetadataCandidate.status == "active",
            *( [community.MetadataCandidate.field_name == field] if field else [] ),
        )).all()
        preferred = community.preferred_candidates(db, game.id)
        metadata = [community.serialize_candidate(db, row, user.id, preferred.get(row.field_name) is row) for row in rows]
        metadata.sort(key=lambda item: (-item["score"], -item["upvotes"], item["id"]))
    return {
        "game_id": canonical_id,
        "images": images,
        "metadata": metadata,
    }
