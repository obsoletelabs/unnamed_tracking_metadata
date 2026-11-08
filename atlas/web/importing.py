"""Curator page: search a provider, preview candidates and import one with its artwork and achievements.

The same page is served twice (#65): `/import` for curators (the top navigation), and `/admin/import` inside the
administration area, so the Administration tab "Import" keeps the admin tabs and its own address."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse

from .. import importer
from ..providers import ProviderError
from ..service import AtlasError
from .deps import Page as Ctx
from .deps import admin_form, admin_page, review_form, review_page, throttle
from .routes import render

router = APIRouter()


ADMIN_PATH, PUBLIC_PATH = "/admin/import", "/import"
Provider = Annotated[str, Query(max_length=40)]
Search = Annotated[str, Query(max_length=200)]
ExternalId = Annotated[str, Form(min_length=1, max_length=128)]


def _view(p: Ctx, base: str, *, provider: str = "", q: str = "", results: list | None = None,
          error: str | None = None, done=None):
    names = sorted(p.st.providers.providers)
    return render(p, "admin_import.html", names=names, provider=provider or (names[0] if names else ""), q=q,
                  results=results, error=error, done=done, base=base, in_admin=base == ADMIN_PATH)


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
