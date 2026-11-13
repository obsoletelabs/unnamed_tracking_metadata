"""Curator pages for books (CONTRACT-C15 §5, A23): the authors editor and the series picker.

* **Authors** (`/game/{cid}/authors`): an ordered list of creators with their roles, saved as a *curator observation*
  (source `curator`, external id = the record) that states only `ext.book.authors`, so it outranks provider lists
  under the priority rule (§2.3) without pinning any other field. "Pin" also writes the `authors` lock (`BOOK_LOCKS`),
  which beats every observation; unticking it removes the lock.
* **Series** (`/game/{cid}/series`): links the book to a `series` record with a `series_member` relation and its
  position (§2.4, D15.4), creating the series record when none of that name exists. The provider's
  `ext.book.series` hint fills the form; it is never turned into a relation without a curator.

Both follow the node's role like the other curator forms: on an authority the change is signed (and refused below
the unified release, A21), elsewhere it stays on this node. Scopes (C14) are not on this base: book data uses the
ordinary record, observation, lock and relation paths, so whatever C14 applies to those applies here."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from .. import books, relations, service, vocab
from ..contract import MAX_AUTHORS, ObservationBatchV1
from ..ids import ulid
from ..models import Canonical, Lock, Observation
from ..normalize import norm_title
from ..service import AtlasError
from .deps import Page, form_dict, go, review_form, review_page
from .routes import _guard_upstream, live_game, render

router = APIRouter()

CURATOR = "curator"
SCOPE = "web"
#: Empty rows the editor offers below the current list.
BLANK_ROWS = 3
#: Form rows read back (a list holds at most MAX_AUTHORS; a few more so a too-long list is reported, not cut).
MAX_ROWS = MAX_AUTHORS + BLANK_ROWS


def _book(p: Page, cid: str) -> tuple[Canonical, str | None]:
    try:
        g, redirected = live_game(p, cid)
    except AtlasError as exc:
        raise HTTPException(404, "No such record.") from exc
    if g.media_type != books.BOOK:
        raise HTTPException(404, "This record is not a book.")
    return g, redirected


def _local(p: Page) -> bool:
    return p.st.ctx.settings.role != "authority"


def _status(exc: AtlasError) -> int:
    return 409 if exc.code in ("emit_protocol", "conflict") else 404 if exc.code == "not_found" else 400


# ---- authors -----------------------------------------------------------------------------------------------

def _rows(authors: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = [{"name": str(a.get("name") or ""), "role": str(a.get("role") or "author"),
             "olid": str((a.get("ids") or {}).get("openlibrary") or "")} for a in authors[:MAX_AUTHORS]
            if isinstance(a, dict)]
    return rows + [{"name": "", "role": "author", "olid": ""} for _ in range(BLANK_ROWS)]


def _authors_page(p: Page, g: Canonical, *, rows: list[dict[str, str]] | None = None, pinned: bool | None = None,
                  error: str | None = None, status: int = 200):
    lock = p.session.get(Lock, (g.id, "authors"))
    return render(p, "book_authors.html", status=status, g=g, v=g.view,
                  rows=rows if rows is not None else _rows(g.view.get("authors") or []),
                  pinned=(lock is not None) if pinned is None else pinned, locked=lock is not None,
                  roles=vocab.BOOK_CREATOR_ROLES, phrases=dict(books.ROLE_PHRASES), error=error, max=MAX_AUTHORS)


def authors_from_form(f: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """(rows to show again, the ordered list): rows with a name, sorted by their "order" box (blank or unreadable =
    where the row is), then by row. An Open Library id kept in the row's hidden field travels with its name."""
    rows, picked = [], []
    for i in range(MAX_ROWS):
        if f"name.{i}" not in f and f"role.{i}" not in f:
            continue
        row = {"name": " ".join((f.get(f"name.{i}") or "").split()), "role": (f.get(f"role.{i}") or "author").strip(),
               "olid": (f.get(f"olid.{i}") or "").strip(), "order": (f.get(f"order.{i}") or "").strip()}
        rows.append(row)
        if not row["name"]:
            continue
        try:
            order = float(row["order"]) if row["order"] else float(i + 1)
        except ValueError:
            order = float(i + 1)
        entry: dict[str, Any] = {"name": row["name"], "role": row["role"]}
        if row["olid"]:
            entry["ids"] = {"openlibrary": row["olid"]}
        picked.append((order, i, entry))
    picked.sort(key=lambda t: (t[0], t[1]))
    return rows, [e for _, _, e in picked]


def _curator_payload(p: Page, cid: str) -> dict[str, Any]:
    """The record's current curator observation from this editor, as an observation item (or a new one)."""
    obs = p.session.scalars(select(Observation).where(
        Observation.canonical_id == cid, Observation.source == CURATOR, Observation.external_id == cid,
        Observation.is_current.is_(True))).first()
    payload = dict(obs.payload) if obs is not None else {}
    item = {k: v for k, v in payload.items() if k in ("title", "facts", "ext")}
    item["external_id"] = cid
    return item


def save_authors(p: Page, g: Canonical, authors: list[dict[str, Any]]) -> None:
    """A curator observation stating the ordered list (and keeping what this editor's earlier observation said)."""
    item = _curator_payload(p, g.id)
    ext = dict(item.get("ext") or {})
    book = dict(ext.get("book") or {})
    book["authors"] = authors
    ext["book"] = book
    item["ext"] = ext
    batch = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "metadata", "source": CURATOR, "source_scope": SCOPE}, "observations": [item]})
    service.ingest(p.session, p.st.ctx, batch, curator=True, force_canonical=g.id, media_type=g.media_type)


@router.get("/game/{cid}/authors", response_class=HTMLResponse)
def authors_form(cid: str, p: Annotated[Page, Depends(review_page)]):
    g, redirected = _book(p, cid)
    if redirected:
        return RedirectResponse(f"/game/{g.id}/authors", status_code=301)
    return _authors_page(p, g)


@router.post("/game/{cid}/authors", response_class=HTMLResponse)
def authors_save(cid: str, p: Annotated[Page, Depends(review_form)], f: Annotated[dict[str, str], Depends(form_dict)]):
    g, _ = _book(p, cid)
    rows, authors = authors_from_form(f)
    pin = f.get("pin") == "1"
    shown = rows + [{"name": "", "role": "author", "olid": "", "order": ""}
                    for _ in range(max(0, BLANK_ROWS - sum(1 for r in rows if not r["name"])))]
    if not authors:
        return _authors_page(p, g, rows=shown, pinned=pin, status=400,
                             error="Give at least one author: a book's list cannot be emptied here.")
    try:
        cleaned = books.clean_lock("authors", authors)
    except ValueError as exc:
        return _authors_page(p, g, rows=shown, pinned=pin, error=str(exc), status=400)
    local = _local(p)
    try:
        if not local:
            _guard_upstream(p.session, p.st, g.id)
        save_authors(p, g, cleaned)
        lock = p.session.get(Lock, (g.id, "authors"))
        if pin:
            service.set_lock(p.session, p.st.ctx, g.id, "authors", cleaned, actor=p.me.username, local_only=local)
        elif lock is not None:
            if not lock.local_only and local:
                raise AtlasError("This list is pinned by the upstream authority; it cannot be unpinned here.",
                                 "conflict")
            service.clear_lock(p.session, p.st.ctx, g.id, "authors")
    except HTTPException as exc:
        p.session.rollback()
        g, _ = _book(p, cid)
        return _authors_page(p, g, rows=shown, pinned=pin, error=str(exc.detail), status=exc.status_code)
    except AtlasError as exc:
        p.session.rollback()
        g, _ = _book(p, cid)
        return _authors_page(p, g, rows=shown, pinned=pin, error=str(exc), status=_status(exc))
    return go(f"/game/{g.id}", "authors-pinned" if pin else "authors-saved")


# ---- series ------------------------------------------------------------------------------------------------

def _series_page(p: Page, g: Canonical, *, q: str = "", name: str | None = None, position: str | None = None,
                 error: str | None = None, status: int = 200):
    s = p.session
    hint = books.series_hint(s, g.id)
    current = books.series_of(relations.of(s, g.id))
    q = q.strip()[:200]
    results = service.search(s, q, 20, media_type="series") if q else None
    first = current[0] if current else {}
    return render(p, "book_series.html", status=status, g=g, v=g.view, hint=hint, current=current, q=q,
                  results=results, error=error,
                  series_name=name if name is not None else (hint.get("name") or ""),
                  position=position if position is not None else (first.get("position") or hint.get("position") or ""))


def find_or_create_series(p: Page, name: str) -> str:
    """The live `series` record of that name (title key equal, oldest first), or a new one from a curator
    observation. Never a second series of the same name."""
    key = norm_title(name, "series")
    if not key:
        raise AtlasError("Give the series a name.")
    found = p.session.scalars(select(Canonical.id).where(
        Canonical.media_type == "series", Canonical.merged_into.is_(None), Canonical.norm_title == key)
        .order_by(Canonical.created_at, Canonical.id)).first()
    if found:
        return found
    batch = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "metadata", "source": CURATOR, "source_scope": SCOPE},
        "observations": [{"external_id": f"series-{ulid()}", "title": name}]})
    res = service.ingest(p.session, p.st.ctx, batch, curator=True, media_type="series")[0]
    if res.canonical_id is None or res.review:
        raise AtlasError("The series record could not be created; try again or pick it from the search.")
    return res.canonical_id


@router.get("/game/{cid}/series", response_class=HTMLResponse)
def series_form(cid: str, p: Annotated[Page, Depends(review_page)], q: Annotated[str, Query(max_length=200)] = ""):
    g, redirected = _book(p, cid)
    if redirected:
        return RedirectResponse(f"/game/{g.id}/series", status_code=301)
    return _series_page(p, g, q=q)


@router.post("/game/{cid}/series", response_class=HTMLResponse)
def series_save(cid: str, p: Annotated[Page, Depends(review_form)], f: Annotated[dict[str, str], Depends(form_dict)]):
    g, _ = _book(p, cid)
    series_id = (f.get("series") or "").strip()[:40]
    name = " ".join((f.get("name") or "").split())[:500]
    position = (f.get("position") or "").strip()[:64]
    local = _local(p)
    try:
        if not series_id and not name:
            raise AtlasError("Pick a series from the search or give the name of a new one.")
        if not local:
            _guard_upstream(p.session, p.st, g.id)
        target = series_id or find_or_create_series(p, name)
        relations.add(p.session, p.st.ctx, g.id, target, "series_member", actor=p.me.username, local_only=local,
                      position=position or None, keep_position=True)
    except HTTPException as exc:
        p.session.rollback()
        g, _ = _book(p, cid)
        return _series_page(p, g, name=name, position=position, error=str(exc.detail), status=exc.status_code)
    except AtlasError as exc:
        p.session.rollback()
        g, _ = _book(p, cid)
        return _series_page(p, g, name=name, position=position, error=str(exc), status=_status(exc))
    return go(f"/game/{g.id}", "series-set")
