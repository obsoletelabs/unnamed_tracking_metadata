from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, Request, UploadFile
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


@router.post("/games/{canonical_id}/community/image-upload")
async def browser_community_image_upload(canonical_id: str, request: Request, db: Db,
                                         file: UploadFile = File(...),
                                         role: str = Form("other"),
                                         aspect_ratio: str = Form(""),
                                         alt_text: str = Form(""),
                                         source_label: str = Form("Browser upload")):
    user = signed_in(request, db)
    if not user:
        return RedirectResponse("/login?error=Sign+in+to+upload+an+image", status_code=303)
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        return RedirectResponse(f"/games/{canonical_id}?error=Game+not+found", status_code=303)
    from urllib.parse import quote_plus
    role = role.strip().lower()
    if role not in community.IMAGE_ROLES:
        return RedirectResponse(f"/games/{canonical_id}?error={quote_plus('Invalid image role')}", status_code=303)
    allowed = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
    signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff",
                  "image/gif": (b"GIF87a", b"GIF89a"), "image/webp": b"RIFF"}
    try:
        if file.content_type not in allowed:
            return RedirectResponse(f"/games/{canonical_id}?error={quote_plus('Unsupported image type')}", status_code=303)
        content = await file.read(5 * 1024 * 1024 + 1)
        signature = signatures[file.content_type]
        valid_signature = any(content.startswith(sig) for sig in signature) if isinstance(signature, tuple) else content.startswith(signature)
        if not content or len(content) > 5 * 1024 * 1024 or not valid_signature:
            return RedirectResponse(f"/games/{canonical_id}?error={quote_plus('Image is empty, too large, or does not match its declared type')}", status_code=303)
        if file.content_type == "image/webp" and content[8:12] != b"WEBP":
            return RedirectResponse(f"/games/{canonical_id}?error={quote_plus('Invalid WebP image')}", status_code=303)
        ratio = community.normalize_ratio(aspect_ratio or None, None, None)
        filename = uuid.uuid4().hex + allowed[file.content_type]
        (main.UPLOAD_DIR / filename).write_bytes(content)
        item = main.GameImage(game_id=game.id, url="/api/v1/images/" + filename, content_type=file.content_type)
        db.add(item)
        db.flush()
        db.add(community.ImageVariant(
            image_id=item.id, role=role, aspect_ratio=ratio,
            alt_text=alt_text.strip()[:500], source_type="browser", source_label=source_label.strip()[:200] or "Browser upload",
            created_by=user.id,
        ))
        db.commit()
    finally:
        await file.close()
    return RedirectResponse(f"/games/{canonical_id}?notice={quote_plus('Image uploaded with community metadata')}", status_code=303)


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
