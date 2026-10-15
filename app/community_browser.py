from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from . import community, main
from .main import Db, Game

router = APIRouter()


def signed_in(request: Request, db):
    user = main.current_user(request, db)
    if not user or not user.email_verified:
        return None
    return user


@router.post("/games/{canonical_id}/community/image-vote")
def browser_image_vote(canonical_id: str, request: Request, db: Db,
                       image_id: int = Form(...), value: int = Form(...)):
    user = signed_in(request, db)
    if not user:
        return RedirectResponse("/login?error=Sign+in+to+vote", status_code=303)
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    image = db.get(main.GameImage, image_id)
    if not game or not image or image.game_id != game.id or value not in {-1, 0, 1}:
        return RedirectResponse(f"/games/{canonical_id}?error=Invalid+community+vote", status_code=303)
    existing = db.scalar(select(community.ImageVote).where(
        community.ImageVote.image_id == image_id,
        community.ImageVote.user_id == user.id,
    ))
    if value == 0:
        if existing:
            db.delete(existing)
    elif existing:
        existing.value = value
        existing.updated_at = main.utcnow()
    else:
        db.add(community.ImageVote(image_id=image_id, user_id=user.id, value=value))
    db.commit()
    return RedirectResponse(f"/games/{canonical_id}?notice=Image+vote+updated", status_code=303)


@router.post("/games/{canonical_id}/community/metadata-vote")
def browser_metadata_vote(canonical_id: str, request: Request, db: Db,
                           candidate_id: int = Form(...), value: int = Form(...)):
    user = signed_in(request, db)
    if not user:
        return RedirectResponse("/login?error=Sign+in+to+vote", status_code=303)
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    candidate = db.get(community.MetadataCandidate, candidate_id)
    if not game or not candidate or candidate.game_id != game.id or candidate.status != "active" or value not in {-1, 0, 1}:
        return RedirectResponse(f"/games/{canonical_id}?error=Invalid+community+vote", status_code=303)
    existing = db.scalar(select(community.MetadataVote).where(
        community.MetadataVote.candidate_id == candidate_id,
        community.MetadataVote.user_id == user.id,
    ))
    if value == 0:
        if existing:
            db.delete(existing)
    elif existing:
        existing.value = value
        existing.updated_at = main.utcnow()
    else:
        db.add(community.MetadataVote(candidate_id=candidate_id, user_id=user.id, value=value))
    db.commit()
    return RedirectResponse(f"/games/{canonical_id}?notice=Metadata+vote+updated", status_code=303)
