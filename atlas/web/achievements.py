"""Website: the achievement editor (separate fields, per-row checks, a diff before anything is proposed) and
click-to-reveal for hidden achievements (signed-in members only)."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from .. import facets, proposals
from .. import media as images
from ..models import Achievement
from ..service import AtlasError
from .deps import Page, go, page, throttle, user_form, user_page
from .routes import live_game, render

router = APIRouter()

KIND_CHOICES = (("", "Not set"), ("progression", "Progression"), ("missable", "Missable"),
                ("win_condition", "Win condition"))
# Platinum (C7.1) marks completing the whole list. The choice approach follows the external contribution in
# obsoletelabs/unnamed_tracking_metadata#110; here it is also gated by the federation emit protocol (C1.6).
TIER_CHOICES = (("", "Not set"), ("bronze", "Bronze"), ("silver", "Silver"), ("gold", "Gold"),
                ("platinum", "Platinum"))
PLATINUM_GATED_NOTE = ("Platinum cannot be published by this node yet: its federation emit protocol is below 1.2. "
                       "An administrator raises it under Admin → Peers once every follower is upgraded.")
EXTRA_PLATINUM_NOTE = ("Another row is already platinum. Platinum usually marks completing the whole list; "
                       "keep more than one only if the game really has several.")
FIELDS = ("kind", "tier", "points", "hidden", "icon_url", "locked_icon_url")
FIELD_LABELS = {"description": "Description", "kind": "Kind", "tier": "Tier", "points": "Points", "hidden": "Hidden",
                "icon_url": "Icon", "locked_icon_url": "Locked icon", "global_percent": "Rarity (%)",
                "status": "Status"}
MAX_ROWS = 200
SOURCE = proposals.SOURCE


def _media_base(p: Page) -> str:
    return p.st.ctx.settings.public_url.rstrip("/") + "/media/"


def _known_blobs(p: Page, cid: str) -> dict[str, str]:
    """Icon address -> stored copy, for the icons of this game that this server already holds."""
    known: dict[str, str] = {}
    for a in p.session.scalars(select(Achievement).where(Achievement.canonical_id == cid)):
        if a.icon_url and a.icon_blob_hash:
            known[a.icon_url] = a.icon_blob_hash
        if a.locked_icon_url and a.locked_icon_blob_hash:
            known[a.locked_icon_url] = a.locked_icon_blob_hash
    return known


def _blob_for(p: Page, url: str, known: dict[str, str]) -> str | None:
    if not url:
        return None
    base = _media_base(p)
    if url.startswith(base):
        digest = url[len(base):]
        return digest if images.find_blob(p.st.ctx.settings, digest) is not None else None
    return known.get(url)


def _inherited(p: Page, cid: str) -> dict[str, dict[str, Any]]:
    """What other sources already say per achievement name, so empty fields can show where a value would come from."""
    out: dict[str, dict[str, Any]] = {}
    rows = [a for a in p.session.scalars(select(Achievement).where(
        Achievement.canonical_id == cid, Achievement.source != SOURCE))]
    rows.sort(key=lambda a: -p.st.ctx.settings.trust(a.source))
    for a in rows:
        slot = out.setdefault(a.norm_name, {})
        for field in ("kind", "tier", "points"):
            if slot.get(field) is None and getattr(a, field) is not None:
                slot[field] = (getattr(a, field), a.source)
        if a.hidden:
            slot.setdefault("hidden", (True, a.source))
        if (a.icon_url or a.icon_blob_hash) and "icon" not in slot:
            slot["icon"] = a.source
        if a.global_percent is not None and "rarity" not in slot:
            slot["rarity"] = (f"{a.global_percent:g}", a.source)
    return out


#: Item fields of C12 the editor does not show as controls; a row carries them through unchanged (JSON). The
#: rarity date is not among them: an unchanged rarity keeps its date, an edited one is dated today (§12.1.6).
EXTRA_FIELDS = ("set", "locale", "i18n", "icon_licence", "icon_attribution")
MAX_EXTRA = 20000


def _extra_of(item: dict[str, Any]) -> str:
    extra = {k: item[k] for k in EXTRA_FIELDS if item.get(k) not in (None, "", {})}
    return json.dumps(extra, sort_keys=True, separators=(",", ":")) if extra else ""


def _parse_extra(raw: str) -> dict[str, Any]:
    if not raw or len(raw) > MAX_EXTRA:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return {k: value[k] for k in EXTRA_FIELDS if isinstance(value, dict) and k in value}


def _row_from_item(item: dict[str, Any], key: str) -> dict[str, Any]:
    return {"key": key, "name": item.get("name", ""), "description": item.get("description", ""),
            "kind": item.get("kind", ""), "tier": item.get("tier", ""),
            "points": "" if item.get("points") is None else str(item["points"]), "hidden": bool(item.get("hidden")),
            "icon_url": item.get("icon_url", ""), "locked_icon_url": item.get("locked_icon_url", ""),
            "external_id": item.get("external_id", ""), "akey": item.get("key", ""),
            "global_percent": "" if item.get("global_percent") is None else f"{item['global_percent']:g}",
            "status": item.get("status", ""), "extra": _extra_of(item), "errors": [], "notes": []}


def _community_items(p: Page, cid: str) -> list[dict[str, Any]]:
    rows = list(p.session.scalars(select(Achievement).where(
        Achievement.canonical_id == cid, Achievement.source == SOURCE).order_by(
        Achievement.position.is_(None), Achievement.position, Achievement.name)))
    return facets.achievement_items(rows, p.session)  # retired rows are not part of the list (C12 §12.1.3)


def _decorate(p: Page, cid: str, rows: list[dict[str, Any]]) -> None:
    known, inherited = _known_blobs(p, cid), _inherited(p, cid)
    for r in rows:
        r["icon_blob"] = _blob_for(p, r["icon_url"], known)
        r["locked_blob"] = _blob_for(p, r["locked_icon_url"], known)
        r["inherited"] = inherited.get(facets._norm_name(r["name"]), {}) if r["name"] else {}


def _others(p: Page, cid: str) -> list[dict[str, Any]]:
    """Entries other sources provide, as the page shows them (hidden ones concealed)."""
    mine_only = {m["id"] for m in facets.merged_achievements(p.session, p.st.ctx, cid, reveal=True)
                 if set(m["sources"]) == {SOURCE}}
    return [m for m in facets.merged_achievements(p.session, p.st.ctx, cid) if m["id"] not in mine_only]


def _editor_ctx(p: Page, g: Any, rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    _decorate(p, g.id, rows)
    return {"g": g, "title": g.view.get("title"), "rows": rows, "kinds": KIND_CHOICES, "tiers": TIER_CHOICES,
            "platinum_gated": facets.platinum_gated(p.st.ctx), "others": _others(p, g.id), "max_rows": MAX_ROWS,
            **extra}


# ---- reading the submitted rows ----------------------------------------------------------------------

def _clean_row(row: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """The item a row stands for (None for an untouched blank row) and the problems found in it."""
    shown = ("name", "description", "kind", "tier", "points", "icon_url", "locked_icon_url", "global_percent", "status")
    if not any(str(row.get(f, "")).strip() for f in shown) and not row["hidden"]:
        return None, []
    item: dict[str, Any] = {"name": row["name"], "description": row["description"], "hidden": row["hidden"],
                            "external_id": row.get("external_id", ""), **_parse_extra(row.get("extra", ""))}
    if row.get("akey"):  # the stored key of the entry this row edits: a rename keeps the entry (C2.2)
        item["key"] = row["akey"]
    problems = []
    for field in ("kind", "tier", "icon_url", "locked_icon_url", "status"):
        if str(row.get(field, "")).strip():
            item[field] = str(row[field]).strip()
    points = str(row["points"]).strip()
    if points:
        if points.isdigit():
            item["points"] = int(points)
        else:
            problems.append("Points must be a whole number.")
    pct = str(row.get("global_percent", "")).strip().rstrip("%").replace(",", ".")
    if pct:
        try:
            value = float(pct)
        except ValueError:
            value = -1.0
        if not 0 <= value <= 100:
            problems.append("Rarity must be a number from 0 to 100.")
        else:
            item["global_percent"] = value
    if not str(row["name"]).strip():
        problems.append("A name is required.")
    if not problems:
        try:
            [item] = facets.clean_achievements([item])
        except AtlasError as exc:
            problems.append(str(exc).capitalize() + ("" if str(exc).endswith(".") else "."))
    return (None if problems else item), problems


async def _store_upload(p: Page, upload: Any) -> tuple[str | None, str | None]:
    """Check and keep an uploaded icon; returns its address on this server, or the reason it was refused."""
    s = p.st.ctx.settings
    data = await upload.read(s.media_max_bytes + 1)
    if len(data) > s.media_max_bytes:
        return None, f"The image may be at most {s.media_max_bytes // 1024 // 1024} MB."
    try:
        data, ext, _ctype, _w, _h = images.reencode_upload(data, s)  # metadata stripped (C12 §12.6.2)
        images.store(s, data, ext)
    except images.MediaError as exc:
        return None, f"That is not a usable image: {exc}."
    except OSError:
        return None, "This node cannot store images right now; the administrator should check ATLAS_MEDIA_DIR."
    return _media_base(p) + hashlib.sha256(data).hexdigest(), None


async def _read_rows(p: Page, form: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(rows as shown again in the editor, items for the rows that are valid and not removed)."""
    rows, items, seen = [], [], {}
    platinum_seen = False
    keys = [k for k in form.getlist("row") if isinstance(k, str)][:MAX_ROWS]
    uploads_left = 6
    for k in keys:
        if form.get(f"remove.{k}"):
            continue
        row = {"key": k, "external_id": str(form.get(f"external_id.{k}") or "")[:200],
               "akey": str(form.get(f"akey.{k}") or "")[:30],
               "global_percent": str(form.get(f"global_percent.{k}") or "")[:20], "errors": [], "notes": [],
               "hidden": bool(form.get(f"hidden.{k}")), "extra": str(form.get(f"extra.{k}") or "")[:MAX_EXTRA],
               "status": str(form.get(f"status.{k}") or "")[:16]}
        for f in ("name", "description", "kind", "tier", "points", "icon_url", "locked_icon_url"):
            row[f] = str(form.get(f"{f}.{k}") or "").strip()
        problems: list[str] = []
        for which, field in (("icon", "icon_url"), ("locked", "locked_icon_url")):
            upload = form.get(f"{which}_file.{k}")
            if upload is not None and getattr(upload, "filename", ""):
                if uploads_left <= 0:
                    problems.append("Too many images in one go; upload the rest after submitting.")
                    continue
                uploads_left -= 1
                throttle(p, f"ach-upload:{p.me.id}", 60, 3600, "Too many icon uploads")
                url, err = await _store_upload(p, upload)
                if err:
                    problems.append(err)
                else:
                    row[field] = url
                    row["notes"].append("Uploaded image will be used for the "
                                        + ("icon." if which == "icon" else "locked icon."))
        item, more = _clean_row(row)
        problems += more
        if row["tier"] == facets.PLATINUM:
            if facets.platinum_gated(p.st.ctx):
                problems.append(PLATINUM_GATED_NOTE)
            elif platinum_seen:
                row["notes"].append(EXTRA_PLATINUM_NOTE)  # a warning, not a refusal (C7.1)
            platinum_seen = True
        # two rows may share a name when they are different achievements (different external ids, C12 §12.1.4)
        ident = (row["external_id"], facets._norm_name(row["name"])) if row["name"] else None
        if ident:
            if ident in seen:
                problems.append("Another row already has this name." if not row["external_id"]
                                else "Another row already has this name and external id.")
                item = None
            seen[ident] = k
        row["errors"] = problems
        rows.append(row)
        if item is not None and not problems:
            items.append(item)
    return rows, items


def _pair(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[int | None]:
    """For each new item, the index of the old entry it edits: same key first, then same name."""
    by_key = {i["key"]: n for n, i in enumerate(old) if i.get("key")}
    by_name = {facets._norm_name(i["name"]): n for n, i in enumerate(old)}
    pairs: list[int | None] = [None] * len(new)
    used: set[int] = set()
    for idx, i in enumerate(new):
        n = by_key.get(i.get("key", ""))
        if n is not None and n not in used:
            pairs[idx] = n
            used.add(n)
    for idx, i in enumerate(new):
        n = by_name.get(facets._norm_name(i["name"]))
        if pairs[idx] is None and n is not None and n not in used:
            pairs[idx] = n
            used.add(n)
    return pairs


def _diff(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, Any]:
    """What will change in the community list, entry by entry. A renamed row is a change, not a removal."""
    pairs = _pair(old, new)
    used = {n for n in pairs if n is not None}
    added = [i["name"] for i, n in zip(new, pairs, strict=True) if n is None]
    removed = [i["name"] for n, i in enumerate(old) if n not in used]
    changed = []
    for b, n in zip(new, pairs, strict=True):
        if n is None:
            continue
        a = old[n]
        deltas = []
        for f in ("name", "description", "kind", "tier", "points", "global_percent", "status", "hidden", "icon_url",
                  "locked_icon_url"):
            x, y = a.get(f) or ("" if f != "hidden" else False), b.get(f) or ("" if f != "hidden" else False)
            if x != y:
                deltas.append((FIELD_LABELS.get(f, f.replace("_", " ").capitalize()), _show(x), _show(y)))
        if deltas:
            changed.append({"name": b["name"], "deltas": deltas})
    order_changed = sorted(used) != [n for n in pairs if n is not None]
    return {"added": added, "removed": removed, "changed": changed, "reordered": order_changed,
            "none": not (added or removed or changed or order_changed)}


def _show(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False or value in ("", None):
        return "(empty)"
    return str(value)


# ---- pages -------------------------------------------------------------------------------------------

@router.get("/game/{cid}/achievements/edit", response_class=HTMLResponse)
def editor(cid: str, p: Annotated[Page, Depends(user_page)]):
    g, _ = live_game(p, cid)
    rows = [_row_from_item(i, f"r{n}") for n, i in enumerate(_community_items(p, g.id))]
    if not rows:
        rows = [_row_from_item({}, "n0")]
    return render(p, "achievement_edit.html", **_editor_ctx(p, g, rows, diff=None, can_submit=False))


@router.post("/game/{cid}/achievements/edit", response_class=HTMLResponse)
async def editor_post(cid: str, request: Request, p: Annotated[Page, Depends(user_form)]):
    g, _ = live_game(p, cid)
    form = await request.form()
    action = str(form.get("action") or "preview")
    rows, items = await _read_rows(p, form)
    if action == "add":
        rows.append(_row_from_item({}, f"n{len(rows) + 1}{len(form.getlist('row'))}"))
        return render(p, "achievement_edit.html", **_editor_ctx(p, g, rows, diff=None, can_submit=False))
    note = str(form.get("note") or "")[:2000]
    bad = [r for r in rows if r["errors"]]
    diff = _diff(_community_items(p, g.id), items) if not bad else None
    if action == "submit" and not bad:
        if diff and diff["none"]:
            return render(p, "achievement_edit.html", **_editor_ctx(p, g, rows, diff=diff, can_submit=False, note=note))
        try:
            pr = proposals.create(p.session, p.st.ctx, p.me, "achievements", g.id,
                                  {"items": items, "mode": "replace"}, note)
        except AtlasError as exc:
            raise HTTPException(400, str(exc)) from exc
        return go(f"/game/{g.id}#achievements", "applied" if pr.auto_applied else "proposed")
    return render(p, "achievement_edit.html", status=400 if bad else 200,
                  **_editor_ctx(p, g, rows, diff=diff, can_submit=bool(diff) and not diff["none"], note=note))


@router.get("/game/{cid}/achievements/{aid}")
def reveal(cid: str, aid: str, p: Annotated[Page, Depends(page)], json: int = 0):
    """The real name and description of one achievement, for a signed-in member who chose to look.

    Never part of the public page or any shared cache: it needs a session and is sent `no-store`."""
    if p.user is None:
        if json:
            return JSONResponse({"error": "sign in"}, status_code=401, headers={"Cache-Control": "no-store"})
        return go(f"/login?next=/game/{cid}/achievements/{aid}")
    g, _ = live_game(p, cid)
    found = next((m for m in facets.merged_achievements(p.session, p.st.ctx, g.id, reveal={aid})
                  if m["id"] == aid or aid in m["keys"]), None)
    if found is None:
        raise HTTPException(404, "No such achievement.")
    if json:
        return JSONResponse({"name": found["name"], "description": found["description"]},
                            headers={"Cache-Control": "no-store"})
    return render(p, "achievement.html", g=g, a=found, title=g.view.get("title"))
