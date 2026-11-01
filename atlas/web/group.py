"""Admin: the owner node group (CONTRACT-C10 §8.1): members, freshness, create, invite, join, SAS, revoke, leave,
and (A12) delegation grants and the local policy. Every route is `local_admin_only` (§6.3) except the grants page,
which a holder of a `group_admin` grant may also use for this node's delegated `grant.issue` power."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from .. import accounts, group_enrol, groups, runtime
from ..api import group_routes
from ..models import GroupAccountLink, GroupEnrolment, GroupMember, GroupRevoked, User
from .deps import LOCAL_ADMIN_ONLY, Page, admin_form, admin_page, go, local_admin_form, local_admin_page, throttle
from .routes import render, when

router = APIRouter()
PW = Annotated[str, Form(max_length=256)]
ROOT_LIMIT, ROOT_WINDOW = 5, 3600  # C10 §1.2: throttle(p, "group-root:<user>", 5, 3600)
REAUTH = "Confirm your current account password for this group operation."
ADMIN_CONFIRM = "grant administrator rights"  # §1.3 confused-deputy guard: `admin` in may_grant_roles is typed


def _root_gate(p: Page, password: str, uses_root: bool) -> str | None:
    """§1.2: a local admin (the dependency), `accounts.reauthenticated`, then the root passphrase under the
    group-root throttle. Returns an error message, or None when the operation may go on."""
    if uses_root:
        throttle(p, f"group-root:{p.me.id}", ROOT_LIMIT, ROOT_WINDOW, "Too many root operations")
    if not accounts.reauthenticated(p.me, password, p.login):
        return REAUTH
    return None


def _ctx(p: Page) -> tuple:
    return p.st.ctx.settings, p.st.ctx.identity


def _page(p: Page, error: str | None = None, status: int = 200, **extra):
    settings, identity = _ctx(p)
    group = groups.load(p.session)
    ctx: dict = {"group": group, "error": error, "fp": groups.gc_fp, "when": when, "identity": identity,
                 "settings": settings, "recovery_pending": groups.recovery_pending(p.session), **extra}
    if group is not None:
        tr = groups.refresh(p.session, group, identity.node_id, settings=settings)
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
            pending=groups.pending_successions(group), can_invite=_can_invite(p, group),
            policy=_policy_rows(settings), **_grants_ctx(p, group, tr))
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


def _policy_rows(settings) -> list[dict]:
    return [{"spec": spec, "value": runtime.from_attr(spec, runtime.current_value(spec, settings))}
            for spec in runtime.REGISTRY if spec.group == runtime.GROUP_POLICY]


def _group_admin_serial(p: Page) -> str | None:
    """The `group_admin` grant through which this (non-local) administrator uses the node's issuer power."""
    if p.rights is None or not p.rights.group_admin:
        return None
    return next((g["serial"] for g in p.rights.sources if g["role"] == "group_admin"), None)


def _grants_ctx(p: Page, group, tr) -> dict:
    settings, identity = _ctx(p)
    own = tr.may_grant_now(identity.node_id)
    delegate = own["body"]["delegate"] if own else None
    as_root = p.is_local_admin and group.root_state == "holder" and bool(group.root_sealed)
    roles = list(groups.GRANT_ALL_ROLES) if as_root else [r for r in groups.GRANT_ALL_ROLES
                                                          if delegate and r in delegate["may_grant_roles"]]
    accounts_here = [{"uid": link.account_uid, "username": u.username if u else "?"}
                     for link, u in p.session.execute(select(GroupAccountLink, User).join(
                         User, User.id == GroupAccountLink.user_id).where(GroupAccountLink.group_id == group.group_id)
                         .order_by(User.username))]
    members = {n: c["body"]["member"]["name"] for n, c in tr.members().items()}
    return {"grants": groups.grant_rows(p.session, group, identity.node_id), "grant_roles": roles,
            "grant_as_root": as_root, "grant_delegate": delegate, "grant_members": members,
            "grant_accounts": accounts_here, "grant_days": groups.GRANT_DAYS,
            "grant_max_days": group.genesis["body"]["policy"]["grant_max_days"],
            "may_grant": bool(roles) and (p.is_local_admin or _group_admin_serial(p) is not None),
            "cap": settings.group_max_delegated_role, "identity_node": identity.node_id,
            # §8.1 'effective here': the grants that add rights to this viewer's session right now
            "in_effect": {g["serial"] for g in (p.rights.sources if p.rights is not None else [])}}


def _grants_page(p: Page, error: str | None = None, status: int = 200):
    settings, identity = _ctx(p)
    group = groups.active(p.session)
    if group is None:
        return render(p, "admin_group_grants.html", status=409 if error is None else status, group=None,
                      error=error or "This node is not in an active group.", fp=groups.gc_fp, when=when)
    tr = groups.refresh(p.session, group, identity.node_id, settings=settings)
    return render(p, "admin_group_grants.html", status=status, group=group, error=error, fp=groups.gc_fp,
                  when=when, **_grants_ctx(p, group, tr))


def _grants_gate(p: Page) -> None:
    if not (p.is_local_admin or _group_admin_serial(p)):
        from fastapi import HTTPException

        raise HTTPException(403, LOCAL_ADMIN_ONLY)


@router.get("/admin/group/grants", response_class=HTMLResponse)
def grants_page(p: Annotated[Page, Depends(admin_page)]):
    _grants_gate(p)
    return _grants_page(p)


@router.post("/admin/group/grants", response_class=HTMLResponse)
def grant_issue(p: Annotated[Page, Depends(admin_form)], role: Annotated[str, Form(max_length=16)],
                days: Annotated[int, Form(ge=1, le=90)] = groups.GRANT_DAYS,
                account_uid: Annotated[str, Form(max_length=26)] = "", nodes: Annotated[list[str], Form()] = [],  # noqa: B006
                note: Annotated[str, Form(max_length=200)] = "", signer: Annotated[str, Form(max_length=8)] = "node",
                root_passphrase: PW = "", password: PW = ""):
    """§6.1: a local admin signs as root (passphrase) or with this node's `grant.issue` delegation; a group admin
    only with the delegation, and the grant then carries `authorized_by_grant` (it falls with theirs)."""
    _grants_gate(p)
    settings, identity = _ctx(p)
    group = groups.active(p.session)
    if group is None:
        return _grants_page(p, "This node is not in an active group.", 409)
    use_root = signer == "root"
    if use_root and not p.is_local_admin:
        return _grants_page(p, LOCAL_ADMIN_ONLY, 403)
    if (why := _root_gate(p, password, use_root)):
        return _grants_page(p, why, 400)
    label = ""
    link = p.session.get(GroupAccountLink, (group.group_id, account_uid.strip()))
    if link is not None:
        user = p.session.get(User, link.user_id)
        label = f"{user.username} ({settings.app_name})" if user else ""
    try:
        root = groups.unlock_root(p.session, settings, group, root_passphrase) if use_root else None
        groups.issue_grant(p.session, settings, group, identity, account_uid=account_uid.strip(), role=role,
                           nodes=nodes, days=days, root=root, account_label=label, note=note,
                           authorized_by_grant=None if p.is_local_admin else _group_admin_serial(p), actor=p.me.id)
    except groups.GroupError as exc:
        return _grants_page(p, str(exc), _status(exc))
    return go("/admin/group/grants", "saved")


@router.post("/admin/group/grants/revoke", response_class=HTMLResponse)
def grant_revoke(p: Annotated[Page, Depends(admin_form)], serial: Annotated[str, Form(max_length=40)],
                 root_passphrase: PW = "", password: PW = ""):
    _grants_gate(p)
    settings, identity = _ctx(p)
    group = groups.active(p.session)
    if group is None:
        return _grants_page(p, "This node is not in an active group.", 409)
    issued_here = (groups.parse_serial(serial) or groups.Serial("", 0, None)).issuer == identity.node_id
    use_root = not issued_here
    if use_root and not p.is_local_admin:
        return _grants_page(p, LOCAL_ADMIN_ONLY, 403)
    if not p.is_local_admin:  # a group admin revokes only what was minted on their authority
        tr = groups.trust(p.session, group, own_node=identity.node_id)
        g = tr.grants.get(serial)
        if g is None or g["body"]["authorized_by_grant"] != _group_admin_serial(p):
            return _grants_page(p, "You may revoke only the grants you issued through this node.", 403)
    if (why := _root_gate(p, password, use_root)):
        return _grants_page(p, why, 400)
    try:
        root = groups.unlock_root(p.session, settings, group, root_passphrase) if use_root else None
        groups.revoke_grant(p.session, settings, group, identity, serial, root=root, actor=p.me.id)
    except groups.GroupError as exc:
        return _grants_page(p, str(exc), _status(exc))
    return go("/admin/group/grants", "saved")


@router.post("/admin/group/policy", response_class=HTMLResponse)
async def policy_save(p: Annotated[Page, Depends(local_admin_form)], request: Request):
    """The local policy form (§8.1): only a local administrator, never a delegated one (§6.3)."""
    form = await request.form()
    values = {}
    for spec in runtime.REGISTRY:
        if spec.group != runtime.GROUP_POLICY:
            continue
        values[spec.key] = form.get(spec.key) == "on" if spec.kind == "bool" else form.get(spec.key, "")
    try:
        changed = runtime.save(p.session, p.st.base, p.me.id, values)  # type: ignore[arg-type]
    except runtime.SettingError as exc:
        p.session.rollback()
        return _page(p, str(exc), 400)
    if changed:
        groups.audit(p.session, p.me.id, "group_policy_changed", "local", {"changed": changed})
    p.st.refresh(p.session, force=True)
    return go("/admin/group", "saved" if changed else "nochange")


@router.get("/admin/group", response_class=HTMLResponse)
def group_page(p: Annotated[Page, Depends(local_admin_page)]):
    return _page(p)


@router.post("/admin/group/create", response_class=HTMLResponse)
def create(p: Annotated[Page, Depends(local_admin_form)], name: Annotated[str, Form(max_length=80)], root_passphrase: PW,
           root_passphrase2: PW, recovery_passphrase: PW, recovery_passphrase2: PW, password: PW = ""):
    settings, identity = _ctx(p)
    throttle(p, f"group-create:{p.me.id}", 5, 3600)
    if (why := _root_gate(p, password, uses_root=False)):
        return _page(p, why, 400)
    if root_passphrase != root_passphrase2 or recovery_passphrase != recovery_passphrase2:
        return _page(p, "The two entries of a passphrase differ.", 400)
    try:
        groups.create_group(p.session, settings, identity, name, root_passphrase, [recovery_passphrase], user=p.me)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.get("/admin/group/recovery.json")
def recovery_download(p: Annotated[Page, Depends(local_admin_page)]):
    settings, _ = _ctx(p)
    files = groups.pending_recovery_files(p.session, settings)
    group = groups.load(p.session)
    if not files or group is None:
        return go("/admin/group")
    groups.audit(p.session, p.me.id, "group_root_exported", group.group_id,
                 {"download": True, "contains": files[0].get("contains")})
    p.session.commit()
    return Response(json.dumps(files[0], indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="atlas-group-{group.group_id}-recovery.json"',
                             "Cache-Control": "no-store"})


@router.post("/admin/group/recovery/confirm", response_class=HTMLResponse)
def recovery_confirm(p: Annotated[Page, Depends(local_admin_form)], passphrase: PW,
                     downloaded: Annotated[str, Form()] = ""):
    settings, _ = _ctx(p)
    throttle(p, f"group-recovery:{p.me.id}", ROOT_LIMIT, ROOT_WINDOW)
    if downloaded != "on":
        return _page(p, "Download the recovery file and keep it offline first.", 400)
    try:
        groups.confirm_recovery(p.session, settings, groups.load(p.session), passphrase, p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/invite", response_class=HTMLResponse)
def invite(p: Annotated[Page, Depends(local_admin_form)], delegate_enrol: Annotated[int, Form(ge=0, le=32)] = 0,
           grant_roles: Annotated[list[str], Form()] = [], grant_nodes: Annotated[list[str], Form()] = [],  # noqa: B006
           confirm_admin: Annotated[str, Form(max_length=40)] = ""):
    settings, identity = _ctx(p)
    group = groups.load(p.session)
    roles = [r for r in groups.GRANT_ROLES if r in grant_roles]
    if "admin" in roles and confirm_admin.strip() != ADMIN_CONFIRM:
        return _page(p, f"To let the new node grant administrator rights, type: {ADMIN_CONFIRM}", 400)
    scopes = [*groups.DEFAULT_SCOPES, *(["group.enrol"] if delegate_enrol else []), *(["grant.issue"] if roles else [])]
    delegate = groups.default_delegate(delegate_enrol, roles, grant_nodes) if delegate_enrol or roles else None
    try:
        row, code = group_enrol.create_invite(p.session, settings, group, identity, scopes, delegate, p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    qr = json.dumps(group_enrol.gc.qr_payload(settings.public_url, code))
    return render(p, "admin_group_enrol.html", step="invite", code=code, address=settings.public_url, qr=qr,
                  row=row, fp=groups.gc_fp, group=group)


@router.get("/admin/group/join", response_class=HTMLResponse)
def join_form(p: Annotated[Page, Depends(local_admin_page)]):
    return render(p, "admin_group_enrol.html", step="join", fp=groups.gc_fp, error=None)


@router.post("/admin/group/join", response_class=HTMLResponse)
def join(p: Annotated[Page, Depends(local_admin_form)], url: Annotated[str, Form(max_length=300)],
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
def enrolment(eid: str, p: Annotated[Page, Depends(local_admin_page)]):
    return _enrolment(p, eid)


@router.post("/admin/group/enrolment/{eid}/confirm", response_class=HTMLResponse)
def enrolment_confirm(eid: str, p: Annotated[Page, Depends(local_admin_form)], matches: Annotated[str, Form()] = "",
                      root_passphrase: PW = "", password: PW = ""):
    settings, identity = _ctx(p)
    if matches != "on":
        return _enrolment(p, eid, "Tick the box only when the numbers match on both screens.", 400)
    row = p.session.get(GroupEnrolment, eid)
    if row is None:
        return _page(p, "No such enrolment.", 404)
    try:
        if row.side == "inviter":
            group = groups.load(p.session)
            uses_root = group is not None and group.root_state == "holder"
            if (why := _root_gate(p, password, uses_root)):
                return _enrolment(p, eid, why, 400)
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
def enrolment_cancel(eid: str, p: Annotated[Page, Depends(local_admin_form)]):
    settings, identity = _ctx(p)
    try:
        group_enrol.cancel(p.session, settings, identity, eid, p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/members/{node_id}/revoke", response_class=HTMLResponse)
def revoke(node_id: str, p: Annotated[Page, Depends(local_admin_form)], reason: Annotated[str, Form(max_length=20)],
           root_passphrase: PW = "", suspect_since: Annotated[str, Form(max_length=40)] = "", password: PW = ""):
    settings, identity = _ctx(p)
    group = groups.load(p.session)
    if group is None:
        return _page(p, "This node is not in a group.", 409)
    if (why := _root_gate(p, password, group.root_state == "holder")):
        return _page(p, why, 400)
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
def renew(p: Annotated[Page, Depends(local_admin_form)], root_passphrase: PW,
          node_id: Annotated[str, Form(max_length=16)] = "", password: PW = ""):
    settings, identity = _ctx(p)
    if (why := _root_gate(p, password, uses_root=True)):
        return _page(p, why, 400)
    group = groups.load(p.session)
    try:
        root = groups.unlock_root(p.session, settings, group, root_passphrase)
        groups.renew(p.session, settings, group, identity, [node_id] if node_id else None, root=root, actor=p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/leave", response_class=HTMLResponse)
def leave(p: Annotated[Page, Depends(local_admin_form)], password: PW = ""):
    settings, identity = _ctx(p)
    if not accounts.reauthenticated(p.me, password, p.login):
        return _page(p, "Confirm your current password to leave the group.", 400)
    try:
        groups.leave(p.session, settings, groups.load(p.session), identity, actor=p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    return go("/admin/group", "saved")


@router.post("/admin/group/sync", response_class=HTMLResponse)
def sync_now(p: Annotated[Page, Depends(local_admin_form)]):
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
def resync_skip(p: Annotated[Page, Depends(local_admin_form)], password: PW = ""):
    if not accounts.reauthenticated(p.me, password, p.login):
        return _page(p, "Confirm your current password to skip the resync.", 400)
    group = groups.load(p.session)
    if group is not None and group.status == "resyncing":
        group.status = "active"
        groups.audit(p.session, p.me.id, "group_resync_skipped", group.group_id)
    return go("/admin/group", "saved")
