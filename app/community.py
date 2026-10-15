from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from . import main
from .main import Base, Db, Game, GameImage, require_api_token

router = APIRouter()
IMAGE_ROLES = {"cover", "banner", "background", "screenshot", "logo", "icon", "thumbnail", "other"}
METADATA_FIELDS = {"title", "developer", "description", "release_year", "custom_fields"}
RATIO_RE = re.compile(r"^(\d{1,4}):(\d{1,4})$")


class ImageVariant(Base):
    __tablename__ = "game_image_variants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("game_images.id", ondelete="CASCADE"), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(30), default="other", index=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    aspect_ratio: Mapped[str] = mapped_column(String(12), default="unknown")
    alt_text: Mapped[str] = mapped_column(String(500), default="")
    source_type: Mapped[str] = mapped_column(String(40), default="community")
    source_label: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ImageVote(Base):
    __tablename__ = "game_image_votes"
    __table_args__ = (UniqueConstraint("image_id", "user_id", name="uq_game_image_vote"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("game_images.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    value: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MetadataCandidate(Base):
    __tablename__ = "metadata_candidates"
    __table_args__ = (UniqueConstraint("game_id", "field_name", "value_json", name="uq_metadata_candidate_value"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    field_name: Mapped[str] = mapped_column(String(80), index=True)
    value_json: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40), default="community")
    source_label: Mapped[str] = mapped_column(String(200), default="")
    confidence: Mapped[float] = mapped_column(default=0.5)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MetadataVote(Base):
    __tablename__ = "metadata_candidate_votes"
    __table_args__ = (UniqueConstraint("candidate_id", "user_id", name="uq_metadata_candidate_vote"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("metadata_candidates.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    value: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ImageCreate(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    content_type: str = Field(default="image/unknown", max_length=80)
    role: str = Field(default="other", max_length=30)
    width: int | None = Field(default=None, ge=1, le=50000)
    height: int | None = Field(default=None, ge=1, le=50000)
    aspect_ratio: str | None = Field(default=None, max_length=12)
    alt_text: str = Field(default="", max_length=500)
    source_type: str = Field(default="community", max_length=40)
    source_label: str = Field(default="", max_length=200)


class VoteCreate(BaseModel):
    value: int = Field(ge=-1, le=1)


class MetadataCandidateCreate(BaseModel):
    field_name: str = Field(min_length=1, max_length=80)
    value: object
    source_type: str = Field(default="community", max_length=40)
    source_label: str = Field(default="", max_length=200)
    confidence: float = Field(default=0.5, ge=0, le=1)


def normalize_ratio(value: str | None, width: int | None, height: int | None) -> str:
    if width and height:
        a, b = width, height
        while b:
            a, b = b, a % b
        return f"{width // a}:{height // a}"
    if value:
        match = RATIO_RE.fullmatch(value.strip())
        if not match or int(match.group(1)) == 0 or int(match.group(2)) == 0:
            raise HTTPException(422, "aspect_ratio must use a positive W:H format")
        return f"{int(match.group(1))}:{int(match.group(2))}"
    return "unknown"


def validate_image_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(422, "Image URL must be an HTTPS URL")
    return url.strip()


def image_scores(db, image_id: int) -> tuple[int, int]:
    up = db.scalar(select(func.count()).select_from(ImageVote).where(ImageVote.image_id == image_id, ImageVote.value == 1)) or 0
    down = db.scalar(select(func.count()).select_from(ImageVote).where(ImageVote.image_id == image_id, ImageVote.value == -1)) or 0
    return int(up), int(down)


def variant_for(db, image_id: int) -> ImageVariant | None:
    return db.scalar(select(ImageVariant).where(ImageVariant.image_id == image_id))


def serialize_image(db, image: GameImage, user_id: int | None = None) -> dict:
    variant = variant_for(db, image.id)
    up, down = image_scores(db, image.id)
    return {
        "id": image.id, "url": image.url, "content_type": image.content_type,
        "role": variant.role if variant else "other", "width": variant.width if variant else None,
        "height": variant.height if variant else None, "aspect_ratio": variant.aspect_ratio if variant else "unknown",
        "alt_text": variant.alt_text if variant else "", "source_type": variant.source_type if variant else "legacy",
        "source_label": variant.source_label if variant else "", "upvotes": up, "downvotes": down, "score": up - down,
        "my_vote": (db.scalar(select(ImageVote.value).where(ImageVote.image_id == image.id, ImageVote.user_id == user_id)) if user_id else None),
        "created_at": image.created_at.isoformat() if image.created_at else None,
    }


def ranked_images(db, game_id: int, role: str = "") -> list[GameImage]:
    statement = select(GameImage).where(GameImage.game_id == game_id)
    images = db.scalars(statement.order_by(GameImage.created_at, GameImage.id)).all()
    if role:
        images = [image for image in images if (variant_for(db, image.id) and variant_for(db, image.id).role == role)]
    return sorted(images, key=lambda image: (-(image_scores(db, image.id)[0] - image_scores(db, image.id)[1]),
                                               -image_scores(db, image.id)[0], image.created_at, image.id))


def metadata_score(db, candidate_id: int) -> tuple[int, int]:
    up = db.scalar(select(func.count()).select_from(MetadataVote).where(MetadataVote.candidate_id == candidate_id, MetadataVote.value == 1)) or 0
    down = db.scalar(select(func.count()).select_from(MetadataVote).where(MetadataVote.candidate_id == candidate_id, MetadataVote.value == -1)) or 0
    return int(up), int(down)


def serialize_candidate(db, candidate: MetadataCandidate, user_id: int | None = None, preferred: bool = False) -> dict:
    up, down = metadata_score(db, candidate.id)
    return {
        "id": candidate.id, "field": candidate.field_name, "value": json.loads(candidate.value_json),
        "source_type": candidate.source_type, "source_label": candidate.source_label, "confidence": candidate.confidence,
        "status": candidate.status, "upvotes": up, "downvotes": down, "score": up - down,
        "my_vote": (db.scalar(select(MetadataVote.value).where(MetadataVote.candidate_id == candidate.id, MetadataVote.user_id == user_id)) if user_id else None),
        "preferred": preferred, "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
    }


def preferred_candidates(db, game_id: int) -> dict[str, MetadataCandidate | None]:
    result: dict[str, MetadataCandidate | None] = {}
    fields = db.scalars(select(MetadataCandidate.field_name).where(MetadataCandidate.game_id == game_id, MetadataCandidate.status == "active").distinct()).all()
    for field in fields:
        rows = db.scalars(select(MetadataCandidate).where(MetadataCandidate.game_id == game_id, MetadataCandidate.field_name == field, MetadataCandidate.status == "active")).all()
        ranked = []
        for candidate in rows:
            up, down = metadata_score(db, candidate.id)
            ranked.append((candidate, up - down, up, candidate.confidence, candidate.created_at, candidate.id))
        ranked.sort(key=lambda row: (-row[1], -row[2], -row[3], row[4], row[5]))
        result[field] = ranked[0][0] if ranked and ranked[0][1] > 0 else None
    return result


def community_game_data(db, game: Game, user_id: int | None = None) -> dict:
    images = [serialize_image(db, image, user_id) for image in ranked_images(db, game.id)]
    preferred_images: dict[str, int | None] = {}
    for image in images:
        role = image["role"]
        if role not in preferred_images:
            preferred_images[role] = image["id"] if image["score"] > 0 else None
    candidates = db.scalars(select(MetadataCandidate).where(MetadataCandidate.game_id == game.id, MetadataCandidate.status == "active").order_by(MetadataCandidate.field_name, MetadataCandidate.id)).all()
    preferred = preferred_candidates(db, game.id)
    options = [serialize_candidate(db, row, user_id, preferred.get(row.field_name) is row) for row in candidates]
    preferred_metadata = {field: json.loads(candidate.value_json) for field, candidate in preferred.items() if candidate}
    return {"images": images, "preferred_images": preferred_images, "metadata_options": options, "preferred_metadata": preferred_metadata}


# Extend the existing game representation without changing its stable core schema.
_original_serialize_game = main.serialize_game

def serialize_game_with_community(game, db=None):
    result = _original_serialize_game(game, db)
    if db is not None:
        result.update(community_game_data(db, game))
    return result

main.serialize_game = serialize_game_with_community


@router.get("/api/v1/games/{canonical_id}/images")
def list_images(canonical_id: str, db: Db, role: str = "", authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "read")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game: raise HTTPException(404, "Game not found")
    if role and role not in IMAGE_ROLES: raise HTTPException(422, "Invalid image role")
    items = [serialize_image(db, image, user.id) for image in ranked_images(db, game.id, role)]
    grouped: dict[str, list[dict]] = {}
    for item in items: grouped.setdefault(item["role"], []).append(item)
    for values in grouped.values():
        for index, item in enumerate(values): item["preferred"] = index == 0 and item["score"] > 0
    return {"game_id": canonical_id, "roles": sorted(grouped), "items": grouped, "flat": items,
            "aspect_ratios": sorted({item["aspect_ratio"] for item in items})}


@router.post("/api/v1/games/{canonical_id}/images", status_code=201)
def add_image(canonical_id: str, payload: ImageCreate, db: Db, authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "images")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game: raise HTTPException(404, "Game not found")
    role = payload.role.strip().lower()
    if role not in IMAGE_ROLES: raise HTTPException(422, "Invalid image role")
    content_type = payload.content_type.strip().lower()
    if not content_type.startswith("image/"): raise HTTPException(422, "content_type must be an image MIME type")
    item = GameImage(game_id=game.id, url=validate_image_url(payload.url), content_type=content_type)
    db.add(item); db.flush()
    db.add(ImageVariant(image_id=item.id, role=role, width=payload.width, height=payload.height,
                        aspect_ratio=normalize_ratio(payload.aspect_ratio, payload.width, payload.height),
                        alt_text=payload.alt_text.strip(), source_type=payload.source_type.strip()[:40] or "community",
                        source_label=payload.source_label.strip()[:200], created_by=user.id))
    db.commit(); db.refresh(item)
    return serialize_image(db, item, user.id)


@router.post("/api/v1/images/{image_id}/vote")
def vote_image(image_id: int, payload: VoteCreate, db: Db, authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "write")
    image = db.get(GameImage, image_id)
    if not image: raise HTTPException(404, "Image not found")
    existing = db.scalar(select(ImageVote).where(ImageVote.image_id == image.id, ImageVote.user_id == user.id))
    if payload.value == 0:
        if existing: db.delete(existing)
    elif existing:
        existing.value = payload.value; existing.updated_at = datetime.now(timezone.utc)
    else:
        db.add(ImageVote(image_id=image.id, user_id=user.id, value=payload.value))
    db.commit()
    return serialize_image(db, image, user.id)


@router.get("/api/v1/games/{canonical_id}/metadata-options")
def list_metadata_options(canonical_id: str, db: Db, field: str = "", authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "read")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game: raise HTTPException(404, "Game not found")
    if field and field not in METADATA_FIELDS: raise HTTPException(422, "Unsupported metadata field")
    statement = select(MetadataCandidate).where(MetadataCandidate.game_id == game.id, MetadataCandidate.status == "active")
    if field: statement = statement.where(MetadataCandidate.field_name == field)
    rows = db.scalars(statement.order_by(MetadataCandidate.field_name, MetadataCandidate.id)).all()
    preferred = preferred_candidates(db, game.id)
    return {"game_id": canonical_id, "fields": sorted({row.field_name for row in rows}),
            "items": [serialize_candidate(db, row, user.id, preferred.get(row.field_name) is row) for row in rows],
            "preferred": {key: (value.id if value else None) for key, value in preferred.items()}}


@router.post("/api/v1/games/{canonical_id}/metadata-options", status_code=201)
def add_metadata_option(canonical_id: str, payload: MetadataCandidateCreate, db: Db, authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "write")
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game: raise HTTPException(404, "Game not found")
    field = payload.field_name.strip().lower()
    if field not in METADATA_FIELDS: raise HTTPException(422, "Unsupported metadata field")
    value = payload.value
    if field in {"title", "developer", "description"} and (not isinstance(value, str) or not value.strip()):
        raise HTTPException(422, f"{field} must be a non-empty string")
    if field == "release_year" and (not isinstance(value, int) or isinstance(value, bool) or not 1950 <= value <= 2100):
        raise HTTPException(422, "release_year must be an integer between 1950 and 2100")
    if field == "custom_fields" and not isinstance(value, dict): raise HTTPException(422, "custom_fields must be an object")
    value_json = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(value_json) > 10000: raise HTTPException(422, "Metadata option is too large")
    existing = db.scalar(select(MetadataCandidate).where(MetadataCandidate.game_id == game.id, MetadataCandidate.field_name == field, MetadataCandidate.value_json == value_json))
    if existing: return serialize_candidate(db, existing, user.id, False)
    item = MetadataCandidate(game_id=game.id, field_name=field, value_json=value_json,
                             source_type=payload.source_type.strip()[:40] or "community", source_label=payload.source_label.strip()[:200],
                             confidence=payload.confidence, created_by=user.id)
    db.add(item); db.commit(); db.refresh(item)
    return serialize_candidate(db, item, user.id, False)


@router.post("/api/v1/metadata-options/{candidate_id}/vote")
def vote_metadata(candidate_id: int, payload: VoteCreate, db: Db, authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "write")
    candidate = db.get(MetadataCandidate, candidate_id)
    if not candidate or candidate.status != "active": raise HTTPException(404, "Metadata option not found")
    existing = db.scalar(select(MetadataVote).where(MetadataVote.candidate_id == candidate.id, MetadataVote.user_id == user.id))
    if payload.value == 0:
        if existing: db.delete(existing)
    elif existing:
        existing.value = payload.value; existing.updated_at = datetime.now(timezone.utc)
    else:
        db.add(MetadataVote(candidate_id=candidate.id, user_id=user.id, value=payload.value))
    db.commit()
    preferred = preferred_candidates(db, candidate.game_id).get(candidate.field_name)
    return serialize_candidate(db, candidate, user.id, preferred is candidate)
