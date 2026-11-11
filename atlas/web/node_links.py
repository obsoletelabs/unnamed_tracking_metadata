"""Node link pages (CONTRACT-C14 §6.3, §6.6, §6.7, §7.2).

Main side (an authority with `link_enabled`):

* `/link` - enter (or open with a prefilled) user code. Approval is a separate POST with CSRF that needs the
  node's **confirmation code** from its own screen (C14-5) and a fresh password (+ second factor) entry; GET never
  approves. The page shows the node's full fingerprint, its name, the IP and time the flow was started.
* `/me/links` - the account's linked nodes: unlink, the H65 name mapping, withdraw everything pending.
* `/admin/links` - every link on this node: revoke, ban the key, and the unknown-code alarm.

Node side (`/admin/main`, never on an authority): link to a main Atlas (probe and pin, device flow with QR code,
user code and confirmation code), the queue, send/pull now, rotate the link key, unlink, report a stolen key.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from .. import accounts, link, link_client
from ..models import LinkDevice, NodeLink, Proposal, UpstreamContribution, UpstreamLink, User
from ..service import AtlasError
from .deps import LOCAL_ADMIN_ONLY, admin_page, go, local_admin_form, local_admin_page, throttle, user_form, user_page
from .deps import Page as Ctx
from .routes import render

router = APIRouter()


def _on(p: Ctx) -> None:
    if not link.active(p.st.ctx.settings):
        raise HTTPException(404)


def _no_store(resp: HTMLResponse) -> HTMLResponse:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ---- main side: approving a node (§6.3 step 3) --------------------------------------------------------------

def _device_view(device: LinkDevice) -> dict:
    node = device.node
    return {"name": node.get("name", ""), "fingerprint": link.fingerprint(node.get("node_id", "")),
            "key_fingerprint": link.fingerprint(node.get("key_id", "")), "ip": device.ip,
            "started": device.created_at, "relink": bool(node.get("relink")), "role": node.get("role") or "local",
            "public_url": node.get("public_url")}


def _link_page(p: Ctx, *, code: str = "", device: LinkDevice | None = None, error: str | None = None,
               status: int = 200, refusal: str | None = None) -> HTMLResponse:
    return _no_store(render(p, "link_approve.html", status=status, code=code, error=error, refusal=refusal,
                            device=_device_view(device) if device is not None else None,
                            user_code=link.show_code(link.normal_code(code) or "") if device is not None else "",
                            has_password=p.me.password_hash != "!", mfa=_has_mfa(p),
                            alarm=link.alarm(p.session) if p.is_admin else None))


def _has_mfa(p: Ctx) -> bool:
    from ..models import UserMfa

    factor = p.session.get(UserMfa, p.me.id)
    return factor is not None and factor.enabled_at is not None


@router.get("/link", response_class=HTMLResponse)
def link_enter(p: Annotated[Ctx, Depends(user_page)], code: str = ""):
    """The verification URI. A prefilled code is only shown; nothing is looked up or approved on GET (T-K15)."""
    _on(p)
    refusal = link.code_entry_refusal(p.st.ctx.settings, p.me, p.login)
    return _link_page(p, code=code[:12], refusal=refusal)


@router.post("/link", response_class=HTMLResponse)
def link_lookup(p: Annotated[Ctx, Depends(user_form)], code: Annotated[str, Form(max_length=20)] = ""):
    _on(p)
    if (refusal := link.code_entry_refusal(p.st.ctx.settings, p.me, p.login)) is not None:
        return _link_page(p, code=code, refusal=refusal, status=403)
    throttle(p, f"link-code:{p.request.client.host if p.request.client else '?'}", *link.CODE_ENTRY_PER_IP)
    device = link.find_device(p.session, p.st.ctx.settings, code)
    if device is None:
        miss = link.code_miss(p.session, p.st.ctx.settings, p.me)
        if miss["delay"]:
            import time

            time.sleep(miss["delay"])  # the main-wide alarm slows this account's misses; nobody is paused
        if miss["account_blocked"]:
            return _link_page(p, code=code, status=429,
                              error=f"Too many unknown codes. Try again in {miss['retry_after'] // 60 + 1} minutes.")
        return _link_page(p, code=code, status=400, error="That code is unknown or has expired. Check the code on "
                                                         "your node's screen, or start again there.")
    return _link_page(p, code=code, device=device)


@router.post("/link/decide", response_class=HTMLResponse)
def link_decide(p: Annotated[Ctx, Depends(user_form)], code: Annotated[str, Form(max_length=20)],
                decision: Annotated[str, Form(max_length=10)],
                confirm: Annotated[str, Form(max_length=20)] = "", password: Annotated[str, Form(max_length=256)] = "",
                mfa_code: Annotated[str, Form(max_length=20)] = "", show_name: Annotated[str, Form()] = ""):
    _on(p)
    if (refusal := link.code_entry_refusal(p.st.ctx.settings, p.me, p.login)) is not None:
        return _link_page(p, code=code, refusal=refusal, status=403)
    throttle(p, f"link-decide:{p.me.id}", 20, 3600)
    device = link.find_device(p.session, p.st.ctx.settings, code)
    if device is None:
        return _link_page(p, code="", status=400, error="That link request is no longer open. Start again on your "
                                                       "node.")
    if decision == "deny":
        link.deny(p.session, p.me, device)
        return go("/me/links", "link_denied", p)
    if decision != "approve":
        raise HTTPException(400, "Unknown decision.")
    if not link.recent_auth_ok(p.session, p.st.ctx.settings, p.me, p.login, password, mfa_code):
        return _link_page(p, code=code, device=device, status=400,
                          error="Confirm your password (and your second factor) to link a node.")
    try:
        link.approve(p.session, p.st.ctx, p.me, device, confirm, show_name=show_name == "on")
    except AtlasError as exc:
        left = exc.detail.get("left")
        p.session.commit()  # the wrong-code counter must persist
        if exc.detail.get("reason") == "confirm_mismatch" and left == 0:
            return _link_page(p, code="", status=400, error="Too many wrong confirmation codes: this link request "
                                                           "was refused. Start again on your node.")
        message = str(exc) + (f" {left} tries left." if left else "")
        fresh = link.find_device(p.session, p.st.ctx.settings, code)
        return _link_page(p, code=code, device=fresh, status=400, error=message)
    return go("/me/links", "link_approved", p)


# ---- main side: the account's linked nodes (§6.6, H65) --------------------------------------------------------

def _mine(p: Ctx, link_id: str) -> NodeLink:
    row = p.session.get(NodeLink, link_id)
    if row is None or row.user_id != p.me.id:
        raise HTTPException(404)
    return row


def _pending_by_link(p: Ctx, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    return dict(p.session.execute(select(Proposal.via_link_id, func.count()).where(
        Proposal.via_link_id.in_(ids), Proposal.status == "pending").group_by(Proposal.via_link_id)).all())


@router.get("/me/links", response_class=HTMLResponse)
def my_links(p: Annotated[Ctx, Depends(user_page)]):
    _on(p)
    rows = list(p.session.scalars(select(NodeLink).where(NodeLink.user_id == p.me.id)
                                  .order_by(NodeLink.created_at.desc())))
    pending = _pending_by_link(p, [r.id for r in rows])
    return render(p, "me_links.html", links=[{"l": r, "fp": link.fingerprint(r.node_id), "pending": pending.get(r.id, 0),
                                              "label": link.label(r, p.me)} for r in rows])


@router.post("/me/links/{link_id}/unlink")
def my_link_unlink(link_id: str, p: Annotated[Ctx, Depends(user_form)]):
    _on(p)
    row = link.lock(p.session, link_id)
    if row is None or row.user_id != p.me.id:
        raise HTTPException(404)
    link.revoke(p.session, p.st.ctx, row, by=p.me.id, reason="user_unlink")
    return go("/me/links", "link_removed", p)


@router.post("/me/links/{link_id}/name")
def my_link_name(link_id: str, p: Annotated[Ctx, Depends(user_form)], show: Annotated[str, Form()] = ""):
    """H65: show the account's display name on this link's contributions (off by default, reversible)."""
    _on(p)
    row = _mine(p, link_id)
    if row.status != "active":
        raise HTTPException(409, "Only an active link can change this.")
    row.show_name = show == "on"
    accounts.audit(p.session, p.me.id, "link_name_mapping", row.id, {"on": row.show_name})
    return go("/me/links", "saved", p)


@router.post("/me/links/{link_id}/withdraw")
def my_link_withdraw(link_id: str, p: Annotated[Ctx, Depends(user_form)]):
    _on(p)
    row = _mine(p, link_id)
    n = link.withdraw_all_pending(p.session, p.me, row)
    accounts.audit(p.session, p.me.id, "link_withdraw_all", row.id, {"count": n})
    return go("/me/links", "saved", p)


# ---- main side: administration (§6.7) ------------------------------------------------------------------------

@router.get("/admin/links", response_class=HTMLResponse)
def admin_links(p: Annotated[Ctx, Depends(admin_page)], status: str = ""):
    rows: list = []
    on = link.active(p.st.ctx.settings)
    if on:
        q = select(NodeLink, User).join(User, User.id == NodeLink.user_id)
        if status in ("active", "frozen", "revoked", "banned"):
            q = q.where(NodeLink.status == status)
        rows = list(p.session.execute(q.order_by(NodeLink.created_at.desc()).limit(200)).all())
    pending = _pending_by_link(p, [r[0].id for r in rows])
    return render(p, "admin_links.html", on=on, state=status,
                  links=[{"l": r[0], "user": r[1].username, "fp": link.fingerprint(r[0].node_id),
                          "pending": pending.get(r[0].id, 0)} for r in rows],
                  counts=link.counts(p.session) if on else {}, alarm=link.alarm(p.session) if on else None)


@router.post("/admin/links/{link_id}/revoke")
def admin_link_revoke(link_id: str, p: Annotated[Ctx, Depends(local_admin_form)]):
    _on(p)
    row = link.lock(p.session, link_id)
    if row is None:
        raise HTTPException(404)
    link.revoke(p.session, p.st.ctx, row, by=p.me.id, reason="admin")
    return go("/admin/links", "saved", p)


@router.post("/admin/links/{link_id}/ban")
def admin_link_ban(link_id: str, p: Annotated[Ctx, Depends(local_admin_form)],
                   compromised: Annotated[str, Form()] = ""):
    _on(p)
    row = link.lock(p.session, link_id)
    if row is None:
        raise HTTPException(404)
    link.ban(p.session, p.st.ctx, row, by=p.me.id, reason="compromised" if compromised == "on" else "admin")
    return go("/admin/links", "saved", p)


# ---- node side: Admin -> Main Atlas (§6.3 steps 0-2, §7.2) ------------------------------------------------------

def _main_page(p: Ctx, *, error: str | None = None, probe: dict | None = None, status: int = 200) -> HTMLResponse:
    settings = p.st.ctx.settings
    ups = list(p.session.scalars(select(UpstreamLink).order_by(UpstreamLink.created_at)))
    view = []
    for row in ups:
        recent = list(p.session.scalars(select(UpstreamContribution).where(
            UpstreamContribution.main_node_id == row.main_node_id)
            .order_by(UpstreamContribution.seq.desc()).limit(50)))
        view.append({"u": row, "main_fp": link.fingerprint(row.main_node_id),
                     "link_fp": link.fingerprint(link_client.link_node_id(row)) if row.link_root_public else "",
                     "key_fp": link.fingerprint(link_client.current_key_id(row)) if row.link_root_public else "",
                     "flow": link_client.flow_view(settings, row) if row.flow and "device_code" in row.flow else None,
                     "counts": link_client.counts(p.session, row.main_node_id), "recent": recent})
    return _no_store(render(p, "admin_main.html", status=status, ups=view, error=error, probe=probe,
                            refusal=link_client.refusal(settings), push=settings.link_push))


def _upstream_row(p: Ctx, main: str) -> UpstreamLink:
    row = p.session.get(UpstreamLink, main[:16])
    if row is None:
        raise HTTPException(404)
    return row


@router.get("/admin/main", response_class=HTMLResponse)
def main_atlas(p: Annotated[Ctx, Depends(local_admin_page)]):
    return _main_page(p)


@router.post("/admin/main/probe", response_class=HTMLResponse)
def main_probe(p: Annotated[Ctx, Depends(local_admin_form)], url: Annotated[str, Form(max_length=300)]):
    """Step one: read the main's discovery and show its fingerprint to compare before linking."""
    if (why := link_client.refusal(p.st.ctx.settings)) is not None:
        return _main_page(p, error=why, status=409)
    throttle(p, f"link-probe:{p.me.id}", 20, 3600)
    try:
        info = link_client.probe(p.st.http, url)
    except link_client.LinkClientError as exc:
        return _main_page(p, error=str(exc), status=400)
    return _main_page(p, probe=info)


@router.post("/admin/main/start", response_class=HTMLResponse)
def main_start(p: Annotated[Ctx, Depends(local_admin_form)], url: Annotated[str, Form(max_length=300)],
               node_id: Annotated[str, Form(max_length=16)] = "", relink: Annotated[str, Form()] = ""):
    """Step two: open the device flow. `node_id` is the main fingerprint the admin saw at the probe; a main that
    answers with another root key is refused (the pin)."""
    if (why := link_client.refusal(p.st.ctx.settings)) is not None:
        return _main_page(p, error=why, status=409)
    throttle(p, f"link-start:{p.me.id}", 10, 3600)
    try:
        info = link_client.probe(p.st.http, url)
        if node_id and info["node_id"] != node_id:
            raise link_client.LinkClientError("The main answered with another key than the one you checked. "
                                              "Nothing was linked.")
        link_client.start(p.session, p.st.ctx.settings, p.st.ctx.identity, p.st.http, url, relink=relink == "on")
    except link_client.LinkClientError as exc:
        p.session.rollback()
        return _main_page(p, error=str(exc), status=400)
    accounts.audit(p.session, p.me.id, "upstream_link_start", info["node_id"], {"relink": relink == "on"})
    return go("/admin/main", None, p)


def _act(p: Ctx, main: str, what: str):
    settings, identity, http = p.st.ctx.settings, p.st.ctx.identity, p.st.http
    row = _upstream_row(p, main)
    try:
        if what == "poll":
            link_client.poll(p.session, settings, http, row, force=True)
        elif what == "send":
            link_client.send(p.session, settings, identity, http, row, force=True)
            link_client.pull(p.session, settings, identity, http, row)
        elif what == "rotate":
            link_client.rotate_key(p.session, settings, identity, http, row)
        elif what in ("unlink", "compromised"):
            link_client.unlink(p.session, settings, identity, http, row, compromised=what == "compromised")
        elif what == "cancel":
            row.flow = None
            if row.status == "starting":
                row.status = "failed"
    except link_client.LinkClientError as exc:
        p.session.commit()  # a refusal (the link is gone) is kept
        return _main_page(p, error=str(exc), status=400)
    accounts.audit(p.session, p.me.id, f"upstream_link_{what}", row.main_node_id)
    return go("/admin/main", None, p)


@router.post("/admin/main/{main}/poll", response_class=HTMLResponse)
def main_poll(main: str, p: Annotated[Ctx, Depends(local_admin_form)]):
    return _act(p, main, "poll")


@router.post("/admin/main/{main}/send", response_class=HTMLResponse)
def main_send(main: str, p: Annotated[Ctx, Depends(local_admin_form)]):
    return _act(p, main, "send")


@router.post("/admin/main/{main}/rotate", response_class=HTMLResponse)
def main_rotate(main: str, p: Annotated[Ctx, Depends(local_admin_form)]):
    return _act(p, main, "rotate")


@router.post("/admin/main/{main}/unlink", response_class=HTMLResponse)
def main_unlink(main: str, p: Annotated[Ctx, Depends(local_admin_form)],
                compromised: Annotated[str, Form()] = ""):
    return _act(p, main, "compromised" if compromised == "on" else "unlink")


@router.post("/admin/main/{main}/cancel", response_class=HTMLResponse)
def main_cancel(main: str, p: Annotated[Ctx, Depends(local_admin_form)]):
    return _act(p, main, "cancel")


@router.post("/admin/main/{main}/withdraw/{cid}", response_class=HTMLResponse)
def main_withdraw(main: str, cid: str, p: Annotated[Ctx, Depends(local_admin_form)]):
    row = _upstream_row(p, main)
    c = p.session.get(UpstreamContribution, cid[:26])
    if c is None or c.main_node_id != row.main_node_id:
        raise HTTPException(404)
    try:
        link_client.withdraw(p.session, p.st.ctx.settings, p.st.ctx.identity, p.st.http, row, c)
    except link_client.LinkClientError as exc:
        return _main_page(p, error=str(exc), status=400)
    return go("/admin/main", None, p)


_ = LOCAL_ADMIN_ONLY
