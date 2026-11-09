"""Admin: the owner node group (CONTRACT-C10 §8.1): members, freshness, create, invite, join, SAS, revoke, leave,
and (A12) delegation grants and the local policy. Every route is `local_admin_only` (§6.3) except the grants page,
which a holder of a `group_admin` grant may also use for this node's delegated `grant.issue` power."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import Markup
from sqlalchemy import select

from .. import accounts, group_enrol, group_presets, groups, qr, runtime
from ..api import group_routes
from ..models import GroupAccountLink, GroupEnrolment, GroupMember, GroupRevoked, User
from .deps import LOCAL_ADMIN_ONLY, Page, admin_form, admin_page, go, local_admin_form, local_admin_page, throttle
from .routes import render

router = APIRouter()
PW = Annotated[str, Form(max_length=256)]
ROOT_LIMIT, ROOT_WINDOW = 5, 3600  # C10 §1.2: throttle(p, "group-root:<user>", 5, 3600)
REAUTH = "Confirm your current account password for this group operation."
PRESET = Annotated[str, Form(max_length=16)]
#: SPEC-UI-POLISH-PASS §3.4: a member not heard from for this many sync intervals counts as "not seen lately".
STALE_INTERVALS = 5
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
    ctx: dict = {"group": group, "error": error, "fp": groups.gc_fp, "identity": identity,
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
            policy=_policy_rows(settings), policy_groups=_policy_groups(settings),
            address=groups.own_address(p.session, settings, group, identity),
            health=_health(rows, settings), preset_now=group_presets.current(settings),
            preset_diffs={k: group_presets.diff(settings, k) for k in (group_presets.SAME_OWNER,
                                                                         group_presets.CAUTIOUS)},
            preset_labels=group_presets.LABELS, admin_confirm=ADMIN_CONFIRM,
            held_updates=groups.held_member_updates(p.session, group, identity.node_id)
            if group.root_state == "holder" else [],
            enrollers=_enrollers(p, group, tr, identity.node_id), **_grants_ctx(p, group, tr))
    else:
        ctx["joins"] = list(p.session.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "joiner")
                                              .order_by(GroupEnrolment.created_at.desc()).limit(5)))
    return render(p, "admin_group.html", status=status, **ctx)


def _health(rows: list, settings) -> dict:
    """§3.4: members by status, and those not seen for `STALE_INTERVALS` sync rounds (this node excluded)."""
    from datetime import UTC, datetime, timedelta

    counts: dict[str, int] = {}
    for m in rows:
        counts[m.status] = counts.get(m.status, 0) + 1
    limit = datetime.now(UTC) - timedelta(seconds=STALE_INTERVALS * max(int(settings.group_sync_interval), 1))
    stale = sum(1 for m in rows if m.status in ("active", "expiring") and m.last_seen_at is not None
                and (m.last_seen_at if m.last_seen_at.tzinfo else m.last_seen_at.replace(tzinfo=UTC)) < limit)
    worst = "bad" if counts.get("fork") or counts.get("expired") else (
        "warn" if counts.get("expiring") or stale else "ok")
    soon = datetime.now(UTC) + timedelta(days=14)
    expiring = {m.node_id for m in rows if m.not_after is not None and m.status in ("active", "expiring")
                and (m.not_after if m.not_after.tzinfo else m.not_after.replace(tzinfo=UTC)) < soon}
    return {"counts": counts, "stale": stale, "tone": worst, "total": len(rows), "expiring_soon": expiring}


def _can_invite(p: Page, group) -> bool:
    try:
        group_enrol.may_invite(p.session, group, p.st.ctx.identity)
        return True
    except groups.GroupError:
        return False


def _enrollers(p: Page, group, tr, own_node: str) -> list[dict]:
    """Where a node can be added from when this node cannot enrol (#122): the member that most likely holds the
    root, and every member with a `group.enrol` delegation. A hint for the administrator; enrolment itself checks."""
    hint = groups.root_holder_hint(p.session, group)
    out = []
    for node, cert in tr.members().items():
        b = cert["body"]
        delegated = bool(b["delegate"]) and "group.enrol" in b["scopes"]
        if node != own_node and (node == hint or delegated):
            out.append({"node_id": node, "name": b["member"]["name"], "url": b["member"]["url"],
                        "how": "group root" if node == hint else "enrol delegation"})
    return out


def _status(exc: groups.GroupError) -> int:
    return {"forbidden": 403, "conflict": 409, "not_found": 404}.get(exc.code, 400)


def _policy_rows(settings) -> list[dict]:
    return [{"spec": spec, "value": runtime.from_attr(spec, runtime.current_value(spec, settings))}
            for spec in runtime.REGISTRY if spec.group == runtime.GROUP_POLICY]


#: §3.3 item 4: the policy rows in three fieldsets (any row not named here is shown under "Other").
POLICY_GROUPS = (("Sign-in", ("group_signin", "group_signin_public", "group_auto_provision", "group_trust_remote_mfa")),
                 ("Rights", ("group_max_delegated_role", "group_federated_privilege", "group_delegated_requires_mfa",
                             "group_denied_accounts")),
                 ("Sync", ("group_profile_sync", "group_profile_fields", "group_view_max_age_hours",
                           "group_hold_timeout_days")))


def _policy_groups(settings) -> list[tuple[str, list[dict]]]:
    rows = {r["spec"].key: r for r in _policy_rows(settings)}
    out = [(title, [rows.pop(k) for k in keys if k in rows]) for title, keys in POLICY_GROUPS]
    if rows:
        out.append(("Other", list(rows.values())))
    return out


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
                      error=error or "This node is not in an active group.", fp=groups.gc_fp)
    tr = groups.refresh(p.session, group, identity.node_id, settings=settings)
    return render(p, "admin_group_grants.html", status=status, group=group, error=error, fp=groups.gc_fp,
                  **_grants_ctx(p, group, tr))


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
    preset = str(form.get("preset") or "")
    if preset:  # §4.3: "Apply" on a preset ignores the other fields
        with_admin = form.get("with_admin") == "on"
        if preset not in (group_presets.SAME_OWNER, group_presets.CAUTIOUS):
            return _page(p, "Choose a preset.", 400)
        if with_admin and str(form.get("confirm_admin") or "").strip() != ADMIN_CONFIRM:
            return _page(p, f"To let administrators of your other nodes act as administrators here, type: "
                            f"{ADMIN_CONFIRM}", 400)
        changed = group_presets.apply(p.session, p.st.ctx.settings, preset, with_admin and preset == "same_owner",
                                      p.me.id)
        p.st.refresh(p.session, force=True)
        return go("/admin/group#policy", "saved" if changed else "nochange")
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
           confirm_admin: Annotated[str, Form(max_length=40)] = "", preset: PRESET = group_presets.CUSTOM,
           with_admin: Annotated[str, Form(max_length=4)] = ""):
    """§4.3: `preset` (same_owner | cautious | custom, the default and today's behaviour). A preset ignores the
    delegate and grant fields: it never changes the invite's scopes, and it sets this node's own policy when the
    enrolment becomes active. `with_admin` (same_owner only) needs the typed confirmation."""
    settings, identity = _ctx(p)
    group = groups.load(p.session)
    if preset not in group_presets.PRESETS:
        return _page(p, "Choose how much this node should trust the new one.", 400)
    admin = with_admin == "on" and preset == group_presets.SAME_OWNER
    if preset != group_presets.CUSTOM:
        if admin and confirm_admin.strip() != ADMIN_CONFIRM:
            return _page(p, f"To let administrators of your other nodes act as administrators here, type: "
                            f"{ADMIN_CONFIRM}", 400)
        roles, delegate_enrol, grant_nodes = [], 0, []
    else:
        roles = [r for r in groups.GRANT_ROLES if r in grant_roles]
        if "admin" in roles and confirm_admin.strip() != ADMIN_CONFIRM:
            return _page(p, f"To let the new node grant administrator rights, type: {ADMIN_CONFIRM}", 400)
    scopes = [*groups.DEFAULT_SCOPES, *(["group.enrol"] if delegate_enrol else []), *(["grant.issue"] if roles else [])]
    delegate = groups.default_delegate(delegate_enrol, roles, grant_nodes) if delegate_enrol or roles else None
    try:
        row, code = group_enrol.create_invite(p.session, settings, group, identity, scopes, delegate, p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    if preset != group_presets.CUSTOM:
        row.preset, row.preset_admin = preset, admin
    return _invite_page(p, row, code, group)


def invite_svg(payload: str) -> Markup | None:
    """The invite as an inline SVG QR code (§5.1), or None when it cannot be drawn: the page then shows the
    address and the code only. Inline markup, never a data: URI (img-src 'self') or a second URL holding the
    secret."""
    try:
        return Markup(qr.svg(payload, label="QR code with this node's address and the invite code"))
    except Exception:  # noqa: BLE001 - too long, or a broken build without segno: the code is always shown
        return None


def _invite_page(p: Page, row, code: str, group):
    settings, _ = _ctx(p)
    payload = json.dumps(group_enrol.gc.qr_payload(settings.public_url, code))
    return render(p, "admin_group_enrol.html", step="invite", code=code, address=settings.public_url, qr=payload,
                  qr_svg=invite_svg(payload), row=row, fp=groups.gc_fp, group=group,
                  preset_label=group_presets.LABELS.get(row.preset or group_presets.CUSTOM))


def _join_page(p: Page, error: str | None = None, status: int = 200, **form):
    settings, _ = _ctx(p)
    return render(p, "admin_group_enrol.html", step="join", fp=groups.gc_fp, error=error, status=status,
                  form=form, preset_diffs={k: group_presets.diff(settings, k) for k in (group_presets.SAME_OWNER,
                                                                                        group_presets.CAUTIOUS)},
                  preset_labels=group_presets.LABELS, admin_confirm=ADMIN_CONFIRM)


@router.get("/admin/group/join", response_class=HTMLResponse)
def join_form(p: Annotated[Page, Depends(local_admin_page)]):
    return _join_page(p)


def split_payload(url: str, code: str) -> tuple[str, str]:
    """§5.3: the Address field also takes the pasted QR payload {"v":1,"type":"atlas-group-invite","url","code"};
    its url and code then replace the fields. The url keeps the 300-character limit of the field it replaces."""
    text = url.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("type") == "atlas-group-invite" and data.get("v") == 1:
            url, code = str(data.get("url") or ""), str(data.get("code") or "")
            if not url or not code:
                raise groups.GroupError("the pasted invite has no address or no code")
            if len(url) > 300 or len(code) > 120:
                raise groups.GroupError("the pasted invite's address or code is too long")
    return url.strip(), code


@router.post("/admin/group/join", response_class=HTMLResponse)
def join(p: Annotated[Page, Depends(local_admin_form)], url: Annotated[str, Form(max_length=600)],
         code: Annotated[str, Form(max_length=120)] = "", name: Annotated[str, Form(max_length=80)] = "",
         preset: PRESET = group_presets.CUSTOM, with_admin: Annotated[str, Form(max_length=4)] = "",
         confirm_admin: Annotated[str, Form(max_length=40)] = ""):
    settings, identity = _ctx(p)
    throttle(p, f"group-join:{p.me.id}", 10, 600)
    keep = {"name": name, "preset": preset, "with_admin": with_admin == "on"}
    if preset not in group_presets.PRESETS:
        return _join_page(p, "Choose how much this node should trust the group.", 400, **keep)
    admin = with_admin == "on" and preset == group_presets.SAME_OWNER
    if admin and confirm_admin.strip() != ADMIN_CONFIRM:
        return _join_page(p, f"To let administrators of your other nodes act as administrators here, type: "
                             f"{ADMIN_CONFIRM}", 400, **keep)
    try:
        url, code = split_payload(url, code)
        if len(url) > 300:
            raise groups.GroupError("the address is longer than 300 characters")
        if not code.strip():
            raise groups.GroupError("enter the invite code (or paste the whole invite into the address field)")
        row, _, _ = group_enrol.start_join(p.session, settings, identity, url, code, p.st.http, name=name or None,
                                           actor=p.me.id)
    except groups.GroupError as exc:
        return _join_page(p, str(exc), _status(exc), **keep)
    if preset != group_presets.CUSTOM:
        row.preset, row.preset_admin = preset, admin
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
    group = groups.load(p.session)
    done = row.state == "active" or (row.side == "inviter" and row.state == "issued")
    return render(p, "admin_group_enrol.html", step="done" if done else "sas", row=row,
                  sas=group_enrol.sas_of(settings, row), peer=peer, fp=groups.gc_fp, error=error, status=status,
                  needs_root=row.side == "inviter" and group is not None and group.root_state == "holder",
                  group=group, preset_label=group_presets.LABELS.get(row.preset or group_presets.CUSTOM),
                  preset_changes=group_presets.diff(settings, row.preset, bool(row.preset_admin)),
                  preset_applied=_applied(p, row) if done else [])


def _applied(p: Page, row) -> list[str]:
    """The policy rows the preset changed when this enrolment became active (from its audit line)."""
    from sqlalchemy import desc

    from ..models import AuditEntry

    if not row.preset:
        return []
    entry = p.session.scalars(select(AuditEntry).where(AuditEntry.action == "group_policy_preset")
                              .where(AuditEntry.created_at >= row.created_at).order_by(desc(AuditEntry.created_at))
                              .limit(1)).first()
    changed = (entry.detail or {}).get("changed") if entry is not None else None
    return [runtime.BY_KEY[k].label for k in changed or [] if k in runtime.BY_KEY]


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
    p.st.refresh(p.session, force=True)  # a preset applied on activation (joiner side)
    return go(f"/admin/group/enrolment/{eid}", "saved")


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
    group = groups.load(p.session)
    try:
        groups.leave(p.session, settings, group, identity, actor=p.me.id)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    # Tell the members now (#122): until they see the `left` CRL they still count this node as a member, and
    # it could not be added back. Members that cannot be reached learn it on their next pull.
    group_routes.push_state(p.session, settings, identity, group, p.st.http)
    return go("/admin/group", "saved")


@router.post("/admin/group/address", response_class=HTMLResponse)
def address_update(p: Annotated[Page, Depends(local_admin_form)], url: Annotated[str, Form(max_length=300)] = "",
                   name: Annotated[str, Form(max_length=80)] = "", root_passphrase: PW = "", password: PW = ""):
    """This node's new address or name (#122, §1.3 member update). The node signs the new member object; a root
    holder re-issues its own certificate at once and pushes it, any other member sends it to the root holder."""
    settings, identity = _ctx(p)
    group = groups.active(p.session)
    if group is None:
        return _page(p, "This node is not in an active group.", 409)
    holder = group.root_state == "holder" and bool(group.root_sealed)
    if (why := _root_gate(p, password, uses_root=holder)):
        return _page(p, why, 400)
    try:
        update = groups.member_update(p.session, settings, group, identity, url=url.strip() or None,
                                      name=name.strip() or None)
        if holder:
            root = groups.unlock_root(p.session, settings, group, root_passphrase)
            groups.apply_member_update(p.session, settings, group, identity, update, root=root, actor=p.me.id)
            del root
            outcomes = group_routes.push_state(p.session, settings, identity, group, p.st.http)
            return _page(p, _unreached(outcomes), 200, address_sent="applied")
        groups.audit(p.session, p.me.id, "group_member_update_sent", identity.node_id,
                     {"url": update["member"]["url"], "name": update["member"]["name"], "rev": update["member"]["rev"]})
        outcomes = group_routes.send_member_update(p.session, settings, identity, group, update, p.st.http)
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    if not any(v in ("held", "known") for v in outcomes.values()):
        return _page(p, "No member holding the group root accepted the update. Ask the administrator of the node "
                     "that holds the root to sync, then send it again. " + _unreached(outcomes, all_=True), 409)
    return _page(p, None, 200, address_sent="sent")


def _unreached(outcomes: dict[str, str], all_: bool = False) -> str | None:
    failed = {groups.gc_fp(n): v for n, v in outcomes.items() if all_ or v not in ("ok", "held", "known")}
    if not failed:
        return None
    lead = "Answers" if all_ else "Done here; these members were not reached now and learn it on their next sync"
    return f"{lead}: " + "; ".join(f"{k}: {v}" for k, v in failed.items())


@router.post("/admin/group/address/{node_id}/apply", response_class=HTMLResponse)
def address_apply(node_id: str, p: Annotated[Page, Depends(local_admin_form)], root_passphrase: PW = "",
                  password: PW = ""):
    """Root holder: apply a member's held update (its own signature was checked when it arrived and is checked
    again here), then push the new certificate to every member, the updated one at its new address."""
    settings, identity = _ctx(p)
    group = groups.active(p.session)
    if group is None:
        return _page(p, "This node is not in an active group.", 409)
    if (why := _root_gate(p, password, uses_root=True)):
        return _page(p, why, 400)
    row = p.session.get(groups.NodeState, groups.MEMBER_UPDATE_PREFIX + node_id[:16])
    if row is None:
        return _page(p, "No update from that node is waiting.", 404)
    try:
        root = groups.unlock_root(p.session, settings, group, root_passphrase)
        groups.apply_member_update(p.session, settings, group, identity, json.loads(row.value), root=root,
                                   actor=p.me.id)
        del root
    except groups.GroupError as exc:
        return _page(p, str(exc), _status(exc))
    outcomes = group_routes.push_state(p.session, settings, identity, group, p.st.http)
    return _page(p, _unreached(outcomes), 200, address_sent="applied")


@router.post("/admin/group/address/{node_id}/dismiss")
def address_dismiss(node_id: str, p: Annotated[Page, Depends(local_admin_form)]):
    groups.drop_member_update(p.session, node_id[:16], p.me.id)
    return go("/admin/group", "saved")


@router.post("/admin/group/sync", response_class=HTMLResponse)
def sync_now(p: Annotated[Page, Depends(local_admin_form)]):
    settings, identity = _ctx(p)
    throttle(p, f"group-sync:{p.me.id}", 20, 600)
    group = groups.active(p.session)
    if group is None:
        return _page(p, "This node is not in an active group.", 409)
    outcomes = group_routes.pull(p.session, settings, identity, group, p.st.http)
    # Then send this node's state the other way (#122): a member that cannot reach this node at the address it
    # has on file (this node moved) gets the re-issued certificate here.
    for node, outcome in group_routes.push_state(p.session, settings, identity, group, p.st.http).items():
        if outcome not in ("ok", "not_found") and outcomes.get(node) == "ok":  # not_found: an older member
            outcomes[node] = f"pulled, but the push failed: {outcome}"
    failed = {groups.gc_fp(n): v for n, v in outcomes.items() if v != "ok"}
    return _page(p, "Some members could not be reached: " + "; ".join(f"{k}: {v}" for k, v in failed.items())
                 if failed else None, 200, synced=True)


@router.post("/admin/group/resync/skip", response_class=HTMLResponse)
def resync_skip(p: Annotated[Page, Depends(local_admin_form)], password: PW = ""):
    if not accounts.reauthenticated(p.me, password, p.login):
        return _page(p, "Confirm your current password to skip the resync.", 400)
    group = groups.load(p.session)
    if group is not None and group.status == "resyncing":
        groups.skip_resync(p.session, group, p.me.id)  # a new trust epoch (C10 R1-3)
    return go("/admin/group", "saved")
