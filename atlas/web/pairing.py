"""The approval side of client pairing (CONTRACT-C15 §10.1 item 1, D15.15, A24): `/pair`.

A connector (Jellyfin, Playnite, ...) shows a code and a QR code from `POST /api/v2/client/pair`. The person opens
`/pair` here, signed in, enters the code (the QR pre-fills it; a GET never approves anything), sees which client asks
(its label, User-Agent and the address it asked from), and approves or denies with a CSRF-checked POST. Five wrong
codes in one sign-in session lock the form for ten minutes. On approval the connector's next poll receives a personal
read token of this account (see `atlas.pairing`)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from .. import pairing, service
from ..models import Token
from .deps import Page, user_form, user_page
from .routes import render

router = APIRouter()


def _prefill(raw: str | None) -> str:
    code = pairing.normalise_code(raw)
    return pairing.display_code(code) if code else ""


def _check_lock(p: Page) -> None:
    left = pairing.locked_for(p.session, p.login.id if p.login else p.me.id)
    if left:
        raise HTTPException(429, f"Too many wrong codes. Try again in {left} seconds.",
                            headers={"Retry-After": str(left)})


def _wrong(p: Page, code: str) -> HTMLResponse:
    service.write_lock(p.session)
    locked = pairing.wrong_code(p.session, p.login.id if p.login else p.me.id)
    p.session.commit()
    error = "That code is not valid. Check it on your device; codes expire after 10 minutes."
    if locked:
        error = f"That code is not valid, and that was the fifth wrong code: this form is locked for {locked // 60} " \
                "minutes."
    return render(p, "pair.html", status=400, step="enter", code=code, error=error)


def _details(pending: pairing.Pending) -> dict:
    left = max(0, int(pending.expires - pairing.clock()))
    return {"label": pending.label, "user_agent": pending.user_agent or "(none sent)", "ip": pending.ip,
            "minutes_left": max(1, (left + 59) // 60)}


@router.get("/pair", response_class=HTMLResponse)
def pair_page(p: Annotated[Page, Depends(user_page)], code: Annotated[str | None, Query(max_length=32)] = None):
    return render(p, "pair.html", step="enter", code=_prefill(code), error=None)


@router.post("/pair", response_class=HTMLResponse)
def pair_check(p: Annotated[Page, Depends(user_form)], code: Annotated[str, Form(max_length=32)] = ""):
    _check_lock(p)
    pending = pairing.find(p.session, code)
    if pending is None:
        return _wrong(p, code)
    return render(p, "pair.html", step="confirm", code=_prefill(code), client=_details(pending), error=None)


@router.post("/pair/decide", response_class=HTMLResponse)
def pair_decide(p: Annotated[Page, Depends(user_form)], code: Annotated[str, Form(max_length=32)] = "",
                decision: Annotated[Literal["approve", "deny"], Form()] = "deny"):
    _check_lock(p)
    service.write_lock(p.session)
    pending = pairing.find(p.session, code)
    if pending is None:
        return _wrong(p, code)
    if decision == "approve":
        active = p.session.scalar(select(func.count()).select_from(Token).where(
            Token.user_id == p.me.id, Token.revoked_at.is_(None))) or 0
        if active >= pairing.TOKEN_CAP:
            raise HTTPException(409, "You already have 10 active tokens; revoke one first (Profile → API tokens), "
                                     "then pair again.")
    pairing.decide(p.session, pending, p.me.id, decision == "approve")
    return render(p, "pair.html", step="done", approved=decision == "approve", client=_details(pending), error=None)
