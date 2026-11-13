"""Curator page: search a provider, preview candidates and import one with its artwork and achievements.

The same page is served twice (#65): `/import` for curators (the top navigation), and `/admin/import` inside the
administration area, so the Administration tab "Import" keeps the admin tabs and its own address.

C15 §5 (A23): a book provider (Open Library) gets an ISBN box next to its search box. An ISBN goes straight to the
provider's fetch (no search): `POST /import/isbn` (also used by the local first-run card on the home page). Typing an
ISBN into the search box is an identifier lookup too, so either box finds the one book."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse

from .. import importer
from ..normalize import isbn13
from ..providers import ProviderError
from ..providers.registry import media_types
from ..service import AtlasError
from .deps import Page as Ctx
from .deps import admin_form, admin_page, go, review_form, review_page, throttle
from .routes import render

router = APIRouter()


ADMIN_PATH, PUBLIC_PATH = "/admin/import", "/import"
Provider = Annotated[str, Query(max_length=40)]
Search = Annotated[str, Query(max_length=200)]
ExternalId = Annotated[str, Form(min_length=1, max_length=128)]


def book_providers(p: Ctx) -> list[str]:
    """The enabled providers that describe books (C15 §3.3 `Provider.media_types`)."""
    return sorted(n for n, src in p.st.providers.providers.items() if "book" in media_types(src))


def _view(p: Ctx, base: str, *, provider: str = "", q: str = "", results: list | None = None,
          error: str | None = None, done=None, isbn: str = "", status: int = 200):
    names = sorted(p.st.providers.providers)
    provider = provider if provider in names else (names[0] if names else "")
    return render(p, "admin_import.html", status=status, names=names, provider=provider, q=q, results=results,
                  error=error, done=done, base=base, in_admin=base == ADMIN_PATH, books=book_providers(p),
                  isbn=isbn)


def _search(p: Ctx, base: str, provider: str, q: str):
    if not (provider and q.strip()):
        return _view(p, base, provider=provider)
    throttle(p, f"import-search:{p.user.id}", 30, 60, "Too many searches")
    try:
        found = p.st.providers.lookup(p.session, provider, query=q)
    except ProviderError as exc:
        return _view(p, base, provider=provider, q=q, results=[], error=f"{provider} could not be searched: {exc}")
    return _view(p, base, provider=provider, q=q, results=[r.to_item() for r in found[:25]])


def _run(p: Ctx, base: str, provider: str, external_id: str, q: str):
    throttle(p, f"import:{p.user.id}", 20, 60, "Too many imports")
    try:
        got = importer.import_from_provider(p.st, p.session, provider, external_id)
    except (ProviderError, AtlasError) as exc:
        return _view(p, base, provider=provider, q=q, error=f"Could not import: {exc}")
    return _view(p, base, provider=provider, done=got)


def _run_isbn(p: Ctx, base: str, provider: str, isbn: str, after: str):
    """Import the book an ISBN names, straight through the provider's fetch. `after="record"` (the first-run card)
    goes to the new record; otherwise the import page shows what was imported."""
    provider = provider or "openlibrary"
    found = isbn13(isbn)
    if provider not in book_providers(p):
        error = ("Open Library is switched off on this node: turn it on under Administration → Integrations."
                 if provider == "openlibrary" else f"{provider} is not a book provider on this node.")
        return _view(p, base, provider=provider, isbn=isbn, error=error, status=400)
    if found is None:
        return _view(p, base, provider=provider, isbn=isbn, status=400,
                     error=f"“{isbn.strip()}” is not a valid ISBN: check the digits (10 or 13, hyphens allowed).")
    throttle(p, f"import:{p.user.id}", 20, 60, "Too many imports")
    try:
        got = importer.import_from_provider(p.st, p.session, provider, found)
    except (ProviderError, AtlasError) as exc:
        return _view(p, base, provider=provider, isbn=isbn, error=f"Could not import ISBN {found}: {exc}",
                     status=404 if isinstance(exc, AtlasError) and exc.code == "not_found" else 502)
    if after == "record":
        return go(f"/game/{got.row.id}", "imported")
    return _view(p, base, provider=provider, done=got)


Isbn = Annotated[str, Form(max_length=40)]
After = Annotated[str, Form(max_length=16)]


@router.post(PUBLIC_PATH + "/isbn", response_class=HTMLResponse)
def import_isbn(p: Annotated[Ctx, Depends(review_form)], isbn: Isbn,
                provider: Annotated[str, Form(max_length=40)] = "openlibrary", next: After = ""):
    return _run_isbn(p, PUBLIC_PATH, provider, isbn, next)


@router.post(ADMIN_PATH + "/isbn", response_class=HTMLResponse)
def admin_import_isbn(p: Annotated[Ctx, Depends(admin_form)], isbn: Isbn,
                      provider: Annotated[str, Form(max_length=40)] = "openlibrary", next: After = ""):
    return _run_isbn(p, ADMIN_PATH, provider, isbn, next)


@router.get(PUBLIC_PATH, response_class=HTMLResponse)
def import_form(p: Annotated[Ctx, Depends(review_page)], provider: Provider = "", q: Search = ""):
    return _search(p, PUBLIC_PATH, provider, q)


@router.post(PUBLIC_PATH, response_class=HTMLResponse)
def import_run(p: Annotated[Ctx, Depends(review_form)], provider: Annotated[str, Form(max_length=40)],
               external_id: ExternalId, q: Annotated[str, Form(max_length=200)] = ""):
    return _run(p, PUBLIC_PATH, provider, external_id, q)


@router.get(ADMIN_PATH, response_class=HTMLResponse)
def admin_import_form(p: Annotated[Ctx, Depends(admin_page)], provider: Provider = "", q: Search = ""):
    return _search(p, ADMIN_PATH, provider, q)


@router.post(ADMIN_PATH, response_class=HTMLResponse)
def admin_import_run(p: Annotated[Ctx, Depends(admin_form)], provider: Annotated[str, Form(max_length=40)],
                     external_id: ExternalId, q: Annotated[str, Form(max_length=200)] = ""):
    return _run(p, ADMIN_PATH, provider, external_id, q)
