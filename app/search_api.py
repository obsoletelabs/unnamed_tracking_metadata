from __future__ import annotations

import re
from difflib import SequenceMatcher

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import select

from .identity import GameAlias
from .main import Db, Game, require_api_token

router = APIRouter()
MAX_CANDIDATES = 500
MAX_RESULTS = 100
MIN_PROVIDER_QUERY_LENGTH = 5


def normalize_query(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def search_score(query: str, candidate: str) -> float:
    normalized = normalize_query(candidate)
    if not normalized:
        return 0.0
    if normalized == query:
        return 1.0
    if normalized.startswith(query):
        return 0.95
    if query in normalized:
        return 0.9
    return SequenceMatcher(None, query, normalized).ratio()


def local_search(db: Db, query: str, limit: int) -> list[dict]:
    normalized = normalize_query(query)
    if len(normalized) < 2:
        raise HTTPException(422, "Search query must contain at least two characters")
    tokens = normalized.split()
    broad = "%" + "%".join(tokens[:8]) + "%"
    games = db.scalars(
        select(Game).where(Game.title.ilike(broad) | Game.developer.ilike(broad)).order_by(Game.title, Game.canonical_id).limit(MAX_CANDIDATES)
    ).all()
    aliases = db.scalars(
        select(GameAlias).where(GameAlias.normalized_alias.ilike(broad)).order_by(GameAlias.id).limit(MAX_CANDIDATES)
    ).all()
    by_game: dict[int, dict] = {}
    for game in games:
        by_game[game.id] = {"game": game, "best": search_score(normalized, game.title), "matched_by": "title"}
    for alias in aliases:
        game = db.get(Game, alias.game_id)
        if game is None:
            continue
        score = search_score(normalized, alias.alias)
        existing = by_game.get(game.id)
        if existing is None or score > existing["best"]:
            by_game[game.id] = {"game": game, "best": score, "matched_by": "alias"}
    ranked = sorted(by_game.values(), key=lambda row: (-row["best"], row["game"].title.casefold(), row["game"].canonical_id))
    return [
        {"id": row["game"].canonical_id, "title": row["game"].title, "developer": row["game"].developer,
         "release_year": row["game"].release_year, "score": round(row["best"], 4), "matched_by": row["matched_by"]}
        for row in ranked[:limit]
    ]


@router.get("/api/v1/search")
def search(query: str = "", limit: int = 20, db: Db = None,
           authorization: str | None = Header(default=None)):
    require_api_token(db, authorization, "read")
    if limit < 1 or limit > MAX_RESULTS:
        raise HTTPException(422, f"limit must be between 1 and {MAX_RESULTS}")
    return {
        "query": query,
        "normalized_query": normalize_query(query),
        "items": local_search(db, query, limit),
        "search_only": True,
        "provider_enrichment": "not performed",
    }


@router.get("/api/v1/search/budget")
def search_budget():
    return {
        "max_candidates": MAX_CANDIDATES,
        "max_results": MAX_RESULTS,
        "provider_query_minimum": MIN_PROVIDER_QUERY_LENGTH,
        "enrichment_top_n": 3,
        "provider_fanout": "bounded by provider registry quotas",
    }
