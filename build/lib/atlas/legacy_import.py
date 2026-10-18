"""One-way import from a demo-1 SQLite database. Reads it read-only; users and submissions are not imported.

Known losses: demo-1 stored only a release *year*, which the v1 contract cannot represent, so dates are dropped.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from sqlalchemy.orm import Session

from .contract import ObservationBatchV1
from .service import Ctx, ingest

_KEY = re.compile(r"^[a-z0-9._-]{1,50}$")


def import_legacy(session: Session, ctx: Ctx, path: str, chunk: int = 500) -> dict[str, int]:
    uri = f"file:{Path(path).resolve()}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        ids: dict[int, dict[str, str]] = {}
        for r in db.execute(
            "SELECT e.game_id, p.slug, e.external_id FROM game_external_ids e "
            "JOIN metadata_providers p ON p.id = e.provider_id"
        ):
            slug = r["slug"].strip().lower()
            if _KEY.match(slug) and r["external_id"].strip():
                ids.setdefault(r["game_id"], {})[slug] = r["external_id"].strip()
        tags: dict[int, list[str]] = {}
        for r in db.execute("SELECT gt.game_id, t.name FROM game_tags gt JOIN tags t ON t.id = gt.tag_id"):
            tags.setdefault(r["game_id"], []).append(r["name"][:128])

        items = []
        stats = {"games": 0, "created": 0, "updated": 0, "skipped": 0}
        rows = list(db.execute("SELECT id, canonical_id, title, developer, description FROM games ORDER BY id"))
    finally:
        db.close()

    def flush() -> None:
        if not items:
            return
        batch = ObservationBatchV1.model_validate({
            "connection": {"source_kind": "manual", "source": "manual", "source_scope": "legacy-demo-1"},
            "observations": items,
        })
        for res in ingest(session, ctx, batch, curator=True):
            stats["created" if res.status == "created" else "updated"] += 1
        items.clear()

    for g in rows:
        stats["games"] += 1
        title = (g["title"] or "").strip()
        if not title:
            stats["skipped"] += 1
            continue
        facts = {k: v for k, v in {
            "developer": (g["developer"] or "").strip()[:200] or None,
            "description": (g["description"] or "").strip()[:20000] or None,
            "tags": tags.get(g["id"], [])[:50] or None,
        }.items() if v}
        item = {"external_id": g["canonical_id"], "title": title[:500]}
        if ids.get(g["id"]):
            item["provider_ids"] = ids[g["id"]]
        if facts:
            item["facts"] = facts
        items.append(item)
        if len(items) >= chunk:
            flush()
    flush()
    return stats
