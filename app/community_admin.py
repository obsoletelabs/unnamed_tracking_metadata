from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from . import community, main
from .main import Db, Game

router = APIRouter()


def admin_user(request: Request, db: Db):
    user = main.current_user(request, db)
    if not user or not user.email_verified or user.email.lower() not in main.ADMIN_EMAILS:
        raise HTTPException(403, "Administrator access required")
    return user


@router.get("/admin/community", response_class=HTMLResponse)
def community_admin_page(request: Request, db: Db):
    user = admin_user(request, db)
    image_rows = db.scalars(select(main.GameImage).order_by(main.GameImage.created_at.desc()).limit(250)).all()
    image_items = []
    for image in image_rows:
        game = db.get(Game, image.game_id)
        if not game:
            continue
        variant = community.variant_for(db, image.id)
        up, down = community.image_scores(db, image.id)
        image_items.append({
            "id": image.id, "game_id": game.canonical_id, "game_title": game.title,
            "url": image.url, "role": variant.role if variant else "other",
            "aspect_ratio": variant.aspect_ratio if variant else "unknown",
            "upvotes": up, "downvotes": down, "score": up - down,
        })
    image_items.sort(key=lambda item: (-item["score"], -item["upvotes"], item["id"]))
    candidates = db.scalars(select(community.MetadataCandidate)
                            .where(community.MetadataCandidate.status == "active")
                            .order_by(community.MetadataCandidate.created_at.desc()).limit(250)).all()
    metadata_items = []
    for candidate in candidates:
        game = db.get(Game, candidate.game_id)
        if not game:
            continue
        up, down = community.metadata_score(db, candidate.id)
        metadata_items.append({
            "id": candidate.id, "game_id": game.canonical_id, "game_title": game.title,
            "field": candidate.field_name, "value": candidate.value_json,
            "upvotes": up, "downvotes": down, "score": up - down,
        })
    metadata_items.sort(key=lambda item: (-item["score"], -item["upvotes"], item["id"]))
    return main.templates.TemplateResponse(request, "community_admin.html", {
        "request": request, "app_name": main.APP_NAME, "user": user,
        "images": image_items[:100], "metadata": metadata_items[:100],
        "image_count": len(image_items), "metadata_count": len(metadata_items),
    })
