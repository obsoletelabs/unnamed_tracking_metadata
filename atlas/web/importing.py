"""Curator page: search a provider, preview candidates and import one with its artwork and achievements."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse

from .. import importer
from ..providers import ProviderError
from ..service import AtlasError
from .deps import Page as Ctx
from .deps import review_form, review_page, throttle
from .routes import render

router = APIRouter()


def _view(p: Ctx, *, provider: str = "", q: str = "", results: list | None = None, error: str | None = None, done=None):
    names = sorted(p.st.providers.providers)
    return render(p, "admin_import.html", names=names, provider=provider or (names[0] if names else ""), q=q,
                  results=results, error=error, done=done)


@router.get("/import", response_class=HTMLResponse)
def import_form(p: Annotated[Ctx, Depends(review_page)], provider: Annotated[str, Query(max_length=40)] = "",
                q: Annotated[str, Query(max_length=200)] = ""):
    if not (provider and q.strip()):
        return _view(p, provider=provider)
    throttle(p, f"import-search:{p.user.id}", 30, 60, "Too many searches")
    try:
        found = p.st.providers.lookup(p.session, provider, query=q)
    except ProviderError as exc:
        return _view(p, provider=provider, q=q, results=[], error=f"{provider} could not be searched: {exc}")
    return _view(p, provider=provider, q=q, results=[r.to_item() for r in found[:25]])


@router.post("/import", response_class=HTMLResponse)
def import_run(p: Annotated[Ctx, Depends(review_form)], provider: Annotated[str, Form(max_length=40)],
               external_id: Annotated[str, Form(min_length=1, max_length=128)], q: Annotated[str, Form(max_length=200)] = ""):  # noqa: E501
    throttle(p, f"import:{p.user.id}", 20, 60, "Too many imports")
    try:
        got = importer.import_from_provider(p.st, p.session, provider, external_id)
    except (ProviderError, AtlasError) as exc:
        return _view(p, provider=provider, q=q, error=f"Could not import: {exc}")
    return _view(p, provider=provider, done=got)
