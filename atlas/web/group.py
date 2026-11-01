"""Admin: the owner node group (CONTRACT-C10 §8.1): members, freshness, create, invite, join, SAS, revoke, leave."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from .. import accounts, group_enrol, groups
from ..api import group_routes
from ..models import GroupEnrolment, GroupMember, GroupRevoked
from .deps import Page, admin_form, admin_page, go, throttle
from .routes import render, when

router = APIRouter()
PW = Annotated[str, Form(max_length=256)]


def _ctx(p: Page) -> tuple:
    return p.st.ctx.settings, p.st.ctx.identity


def _page(p: Page, error: str | None = None, status: int = 200, **extra):
    settings, identity = _ctx(p)
    group = groups.load(p.session)
    ctx: dict = {"group": group, "error": error, "fp": groups.gc_fp, "when": when, "identity": identity,
                 "settings": settings, "recovery_pending": groups.recovery_pending(p.session), **extra}
    if group is not None:
        tr = groups.rebuild(p.session, group, identity.node_id, settings=settings)
        rows = list(p.session.scalars(select(GroupMember).where(GroupMember.group_id == group.group_id)
                                      .order_by(GroupMember.name)))
        certs = {m.node_id: tr.certs.get(m.serial) for m in rows}
        ctx.update(
            members=rows, certs=certs, me_id=identity.node_id,
            fresh=groups.freshness(p.session, group, identity.node_id, None, settings.group_view_max_age_hours),
            crl_seq=groups.crl_seqs(p.session, group.group_id),
            revoked=list(p.session.scalars(select(GroupRevoked).where(GroupRevoked.group_id == group.group_id)
                                           .order_by(GroupRevoked.revoked_at.desc()).limit(20))),
            enrolments=list(p.session.scalars(select(GroupEnrolment).where(GroupEnrolment.group_id == group.group_id)
                                              .order_by(GroupEnrolment.created_at.desc()).limit(10))),
            pending=groups.pending_successions(group), can_invite=_can_invite(p, group))
    else:
        ctx["joins"] = list(p.session.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "joiner")
                                              .order_by(GroupEnrolment.created_at.desc()).limit(5)))
    return render(p, "admin_group.html", status=status, **ctx)


def _can_invite(p: Page, group) -> bool:
    try:
        group_enrol.may_invite(p.session, group, p.st.ctx.identity)
        return True
    except groups.GroupError:
        return False


def _status(exc: groups.GroupError) -> int:
    return {"forbidden": 403, "conflict": 409, "not_found": 404}.get(exc.code, 400)


@router.get("/admin/group", response_class=HTMLResponse)
def group_page(p: Annotated[Page, Depends(admin_page)]):
    return _page(p)


@router.post("/admin/group/create", response_class=HTMLResponse)
def create(p: Annotated[Page, Depends(admin_form)], name: Annotated[str, Form(max_length=80)], root_passphrase: PW,
           root_passphrase2: PW, recovery_passphrase: PW, recovery_passphrase2: PW):
    settings, identity = _ctx(p)
    throttle(p, f"group-create:{p.me.id}", 5, 3600)
    if root_passphrase != root_passphrase2 or recovery_passphrase != recovery_passphrase2:
        return _page(p, "The two entries of a passphrase differ.", 400)
    try:
        groups.create_group(p.session, settings, identity, name, root_passphrase, [recovery_passphrase], user=p.me)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.get("/admin/group/recovery.json")
def recovery_download(p: Annotated[Page, Depends(admin_page)]):
    settings, _ = _ctx(p)
    files = groups.pending_recovery_files(p.session, settings)
    group = groups.load(p.session)
    if not files or group is None:
        return go("/admin/group")
    return Response(json.dumps(files[0], indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="atlas-group-{group.group_id}-recovery.json"',
                             "Cache-Control": "no-store"})


@router.post("/admin/group/recovery/confirm", response_class=HTMLResponse)
def recovery_confirm(p: Annotated[Page, Depends(admin_form)], passphrase: PW,
                     downloaded: Annotated[str, Form()] = ""):
    settings, _ = _ctx(p)
    throttle(p, f"group-root:{p.me.id}", 10, 900)
    if downloaded != "on":
        return _page(p, "Download the recovery file and keep it offline first.", 400)
    try:
        groups.confirm_recovery(p.session, settings, groups.load(p.session), passphrase, p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/invite", response_class=HTMLResponse)
def invite(p: Annotated[Page, Depends(admin_form)], delegate_enrol: Annotated[int, Form(ge=0, le=32)] = 0):
    settings, identity = _ctx(p)
    group = groups.load(p.session)
    scopes = list(groups.DEFAULT_SCOPES) + (["group.enrol"] if delegate_enrol else [])
    delegate = groups.default_delegate(delegate_enrol) if delegate_enrol else None
    try:
        row, code = group_enrol.create_invite(p.session, settings, group, identity, scopes, delegate, p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    qr = json.dumps(group_enrol.gc.qr_payload(settings.public_url, code))
    return render(p, "admin_group_enrol.html", step="invite", code=code, address=settings.public_url, qr=qr,
                  row=row, fp=groups.gc_fp, group=group)


@router.get("/admin/group/join", response_class=HTMLResponse)
def join_form(p: Annotated[Page, Depends(admin_page)]):
    return render(p, "admin_group_enrol.html", step="join", fp=groups.gc_fp, error=None)


@router.post("/admin/group/join", response_class=HTMLResponse)
def join(p: Annotated[Page, Depends(admin_form)], url: Annotated[str, Form(max_length=300)],
         code: Annotated[str, Form(max_length=120)], name: Annotated[str, Form(max_length=80)] = ""):
    settings, identity = _ctx(p)
    throttle(p, f"group-join:{p.me.id}", 10, 600)
    try:
        row, _, _ = group_enrol.start_join(p.session, settings, identity, url, code, p.st.http, name=name or None,
                                           actor=p.me.id)
    except groups.GroupError as exc:
        return render(p, "admin_group_enrol.html", step="join", fp=groups.gc_fp, error=str(exc),
                      status=_status(exc))
    return go(f"/admin/group/enrolment/{row.id}")


def _enrolment(p: Page, eid: str, error: str | None = None, status: int = 200):
    settings, _ = _ctx(p)
    row = p.session.get(GroupEnrolment, eid)
    if row is None:
        return _page(p, "No such enrolment.", 404)
    peer = None
    if row.transcript:
        if row.side == "inviter":
            j = row.transcript["hello"]["joiner"]["member"]
            peer = {"node_id": j["node_id"], "name": j["name"], "url": j["url"]}
        else:
            i = row.transcript["reply"]["inviter"]
            peer = {"node_id": i["node_id"], "name": i["name"], "url": i["url"]}
    return render(p, "admin_group_enrol.html", step="sas", row=row, sas=group_enrol.sas_of(settings, row), peer=peer,
                  fp=groups.gc_fp, error=error, status=status,
                  needs_root=row.side == "inviter" and groups.load(p.session) is not None
                  and groups.load(p.session).root_state == "holder")


@router.get("/admin/group/enrolment/{eid}", response_class=HTMLResponse)
def enrolment(eid: str, p: Annotated[Page, Depends(admin_page)]):
    return _enrolment(p, eid)


@router.post("/admin/group/enrolment/{eid}/confirm", response_class=HTMLResponse)
def enrolment_confirm(eid: str, p: Annotated[Page, Depends(admin_form)], matches: Annotated[str, Form()] = "",
                      root_passphrase: PW = ""):
    settings, identity = _ctx(p)
    if matches != "on":
        return _enrolment(p, eid, "Tick the box only when the numbers match on both screens.", 400)
    row = p.session.get(GroupEnrolment, eid)
    if row is None:
        return _page(p, "No such enrolment.", 404)
    try:
        if row.side == "inviter":
            throttle(p, f"group-root:{p.me.id}", 10, 900)
            group_enrol.confirm_inviter(p.session, settings, groups.load(p.session), identity, eid,
                                        root_passphrase or None, http=p.st.http, actor=p.me.id)
        else:
            outcome = group_enrol.confirm_joiner(p.session, settings, identity, eid, p.st.http, actor=p.me.id)
            if outcome != "active":
                p.session.commit()
                return _enrolment(p, eid, "Waiting for the inviter to confirm. Confirm here again once they have."
                                  if outcome == "pending" else "The inviter aborted this enrolment.", 202)
    except groups.GroupError as exc:
        return _enrolment(p, eid, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/enrolment/{eid}/cancel")
def enrolment_cancel(eid: str, p: Annotated[Page, Depends(admin_form)]):
    settings, identity = _ctx(p)
    try:
        group_enrol.cancel(p.session, settings, identity, eid, p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/members/{node_id}/revoke", response_class=HTMLResponse)
def revoke(node_id: str, p: Annotated[Page, Depends(admin_form)], reason: Annotated[str, Form(max_length=20)],
           root_passphrase: PW = "", suspect_since: Annotated[str, Form(max_length=40)] = ""):
    settings, identity = _ctx(p)
    throttle(p, f"group-root:{p.me.id}", 10, 900)
    group = groups.load(p.session)
    try:
        root = groups.unlock_root(p.session, settings, group, root_passphrase) if group.root_state == "holder" else None
        suspect = groups.gc.parse_time(suspect_since + ("" if suspect_since.endswith("Z") else ":00Z")) \
            if suspect_since else None
        groups.revoke_node(p.session, settings, group, identity, node_id, reason, root=root, suspect_since=suspect,
                           actor=p.me.id)
    except (groups.GroupError, groups.gc.GroupCryptoError) as exc:
        return _page(p, str(exc), _status(exc) if isinstance(exc, groups.GroupError) else 400)
    return go("/admin/group", "saved")


@router.post("/admin/group/renew", response_class=HTMLResponse)
def renew(p: Annotated[Page, Depends(admin_form)], root_passphrase: PW,
          node_id: Annotated[str, Form(max_length=16)] = ""):
    settings, identity = _ctx(p)
    throttle(p, f"group-root:{p.me.id}", 10, 900)
    group = groups.load(p.session)
    try:
        root = groups.unlock_root(p.session, settings, group, root_passphrase)
        groups.renew(p.session, settings, group, identity, [node_id] if node_id else None, root=root, actor=p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/leave", response_class=HTMLResponse)
def leave(p: Annotated[Page, Depends(admin_form)], password: PW = ""):
    settings, identity = _ctx(p)
    if not accounts.reauthenticated(p.me, password, p.login):
        return _page(p, "Confirm your current password to leave the group.", 400)
    try:
        groups.leave(p.session, settings, groups.load(p.session), identity, actor=p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/sync", response_class=HTMLResponse)
def sync_now(p: Annotated[Page, Depends(admin_form)]):
    settings, identity = _ctx(p)
    throttle(p, f"group-sync:{p.me.id}", 20, 600)
    group = groups.active(p.session)
    if group is None:
        return _page(p, "This node is not in an active group.", 409)
    outcomes = group_routes.pull(p.session, settings, identity, group, p.st.http)
    failed = {groups.gc_fp(n): v for n, v in outcomes.items() if v != "ok"}
    return _page(p, "Some members could not be reached: " + "; ".join(f"{k}: {v}" for k, v in failed.items())
                 if failed else None, 200, synced=True)


@router.post("/admin/group/resync/skip", response_class=HTMLResponse)
def resync_skip(p: Annotated[Page, Depends(admin_form)], password: PW = ""):
    if not accounts.reauthenticated(p.me, password, p.login):
        return _page(p, "Confirm your current password to skip the resync.", 400)
    group = groups.load(p.session)
    if group is not None and group.status == "resyncing":
        group.status = "active"
        groups.audit(p.session, p.me.id, "group_resync_skipped", group.group_id)
    return go("/admin/group", "saved")
