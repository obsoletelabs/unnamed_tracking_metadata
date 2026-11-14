"""Linking an account across the owner group (CONTRACT-C10 §5.3-§5.4, link purpose; D10.19).

B (where the user is signed in) starts; A (where the account already lives) authenticates the user, asks for
consent and signs a `group.assertion`; B verifies it and runs the link wizard. Every browser payload (the authreq
and the assertion) is sealed to the receiving node's box key. Every cross-site POST only parks its payload under a
random single-use handle and answers 303 to a same-origin GET, where the checks run against the dedicated
`atlas_grp_bind` cookie (SameSite=Lax cookies never ride a cross-site POST). Unknown handles, garbage and
non-members get exactly the 404 of an unknown route.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.exc import IntegrityError

from .. import accounts, group_channel, groups, ids, profile, profile_schema
from .. import group_crypto as gc
from ..api.group_routes import NOT_FOUND
from ..models import GroupAssertionSeen, GroupIdpConsent, GroupSigninState, GroupWebHandle
from ..signing import body_hash, verify_message
from . import CSP
from .deps import Page as Ctx
from .deps import go, page, secure_cookies, throttle, user_form, user_page
from .profile_pages import describe, linked_context
from .routes import render

router = APIRouter()
BIND_COOKIE = "atlas_grp_bind"
STATE_TTL = timedelta(seconds=600)
HANDLE_TTL = timedelta(seconds=120)  # authorize / callback handles (§5.3)
WIZARD_TTL = timedelta(seconds=600)  # the linkform wizard, capped by the evidence window (see `_wizard_deadline`)
WIZARD_MARGIN = timedelta(seconds=30)
AUTHREQ_TTL = timedelta(seconds=600)
FORM_CAP = 64 * 1024


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def not_found() -> JSONResponse:
    """Byte for byte the answer of an unknown route (§5.3, A11-k2)."""
    return JSONResponse(NOT_FOUND, status_code=404)


def _park(session, kind: str, payload: dict, expires_at=None) -> str:
    handle = secrets.token_urlsafe(32)
    session.add(GroupWebHandle(id=_h(handle), kind=kind, payload=payload,
                               expires_at=expires_at or groups.utcnow() + HANDLE_TTL))
    session.flush()
    return handle


def _peek(session, kind: str, handle: str | None) -> GroupWebHandle | None:
    if not handle or len(handle) > 100:
        return None
    row = session.get(GroupWebHandle, _h(handle))
    if row is None or row.kind != kind:
        return None
    if groups.aware(row.expires_at) < groups.utcnow():
        session.delete(row)
        return None
    return row


def _purge(session) -> None:
    from sqlalchemy import delete

    now = groups.utcnow()
    session.execute(delete(GroupWebHandle).where(GroupWebHandle.expires_at < now))
    session.execute(delete(GroupSigninState).where(GroupSigninState.expires_at < now))
    session.execute(delete(GroupAssertionSeen).where(GroupAssertionSeen.expires_at < now))


def _autopost(p: Ctx, url: str, field: str, value: str, title: str, text: str) -> HTMLResponse:
    """An auto-submitting form to another member (the only cross-site navigation). `form-action` is widened to
    exactly that member's origin, taken from its in-force certificate, never from a request."""
    resp = render(p, "group_post.html", action=url, field=field, value=value, title=title, text=text)
    parts = urlsplit(url)
    origin = parts.scheme + "://" + quote(parts.netloc, safe=":[]")
    resp.headers["Content-Security-Policy"] = CSP.replace("form-action 'self';", f"form-action 'self' {origin};", 1)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _sealed_field(raw: str | None, session, settings, group, own: str) -> dict | None:
    if not raw or len(raw) > FORM_CAP:
        return None
    try:
        return gc.open_request(groups.box_keys(settings, group), group.group_id, own, json.loads(raw))
    except (ValueError, TypeError, gc.GroupCryptoError, groups.GroupError):
        return None


async def _form_field(request: Request, name: str) -> str | None:
    if int(request.headers.get("content-length") or 0) > FORM_CAP:
        return None
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - garbage gets the uniform 404
        return None
    value = form.get(name)
    return value if isinstance(value, str) else None


# ---- B: start (§5.3 steps 1-3) --------------------------------------------------------------------------------

@router.post("/me/linked-nodes/link")
def link_start(p: Annotated[Ctx, Depends(user_form)], node_id: Annotated[str, Form(max_length=16)],
               password: Annotated[str, Form(max_length=256)] = ""):
    throttle(p, f"group-link:{p.me.id}", 10, 3600)
    s, identity = p.session, p.st.ctx.identity

    def refuse(message: str):
        return render(p, "me_linked_nodes.html", status=400, **linked_context(p, message))

    if not accounts.reauthenticated(p.me, password, p.login):
        return refuse("Confirm your current password to link another node.")
    group = groups.active(s)
    if group is None or group.status != "active":
        return refuse("This node is not an active member of a group right now.")
    if group_channel.local_link(s, group.group_id, p.me.id) is not None:
        return refuse("This account is already linked in the group. Link the other node from there, or unlink "
                      "first.")
    tr = groups.trust(s, group, own_node=identity.node_id)
    cert = tr.in_force(node_id)
    if cert is None or node_id == identity.node_id or "identity.assert" not in cert["body"]["scopes"]:
        return refuse("That node cannot confirm accounts in this group.")
    _purge(s)
    member = cert["body"]["member"]
    state, bind, nonce, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), ids.ulid(), groups.utcnow()
    s.add(GroupSigninState(id=_h(state), group_id=group.group_id, idp_node=node_id, purpose="link", nonce=nonce,
                           binding=_h(bind), link_user_id=p.me.id, next_url="/me/linked-nodes",
                           expires_at=now + STATE_TTL))
    body = {"rp_node": identity.node_id, "idp_node": node_id, "purpose": "link", "state_hash": _h(state),
            "nonce": nonce, "issued_at": gc.stamp(now), "not_after": gc.stamp(now + AUTHREQ_TTL)}
    env = gc.make_object("group.authreq", group.group_id, f"authreq:{nonce}", body,
                         {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, identity.private)
    sealed = gc.seal_request(member["box_public_key"], group.group_id, node_id, env)
    resp = _autopost(p, member["url"].rstrip("/") + "/group/authorize", "request", json.dumps(sealed),
                     f"Continue on {member['name']}", f"Sign in on {member['name']} to confirm it is your account.")
    resp.set_cookie(BIND_COOKIE, bind, httponly=True, samesite="lax", secure=secure_cookies(p), path="/group/",
                    max_age=int(STATE_TTL.total_seconds()))
    groups.audit(s, p.me.id, "group_link_started", p.me.id, {"idp_node": node_id})
    return resp


# ---- A: authorize (§5.3 steps 4-7) ----------------------------------------------------------------------------

def _authreq_ok(env: Any, tr: groups.Trust, group, own: str) -> dict | None:
    """The authreq envelope, verified: signed by an in-force member's current key, for this node, in its lifetime."""
    try:
        if not isinstance(env, dict) or env.get("t") != "group.authreq" or env.get("_v") != 1 \
                or env.get("group_id") != group.group_id or not isinstance(env.get("body"), dict):
            return None
        b, signer = env["body"], env["signer"]
        if set(b) != {"rp_node", "idp_node", "purpose", "state_hash", "nonce", "issued_at", "not_after"}:
            return None
        if b["idp_node"] != own or b["rp_node"] == own or b["purpose"] != "link" or signer.get("kind") != "node" \
                or signer.get("id") != b["rp_node"] or env["id"] != f"authreq:{b['nonce']}":
            return None
        if body_hash(b) != env["body_hash"] or tr.in_force(b["rp_node"]) is None:
            return None
        key = tr.current_key(b["rp_node"])
        if key is None or key[0] != signer["key_id"] or not verify_message(
                key[1], gc.DOMAINS["group.authreq"], gc.object_parts(env), env["sig"]):
            return None
        now = groups.utcnow()
        ia, na = gc.parse_time(b["issued_at"]), gc.parse_time(b["not_after"])
        if ia > now + groups.SKEW or now > na or na - ia > AUTHREQ_TTL:
            return None
        return b
    except (KeyError, TypeError, AttributeError, gc.GroupCryptoError, ValueError):
        return None


@router.post("/group/authorize")
async def authorize_park(request: Request, p: Annotated[Ctx, Depends(page)]):
    raw = await _form_field(request, "request")
    s, settings, identity = p.session, p.st.ctx.settings, p.st.ctx.identity
    group = groups.active(s)
    if group is None:
        return not_found()
    env = _sealed_field(raw, s, settings, group, identity.node_id)
    tr = groups.trust(s, group, own_node=identity.node_id)
    if env is None or _authreq_ok(env, tr, group, identity.node_id) is None:
        return not_found()
    _purge(s)
    handle = _park(s, "authorize", {"env": env})
    return go(f"/group/authorize/continue?h={handle}")


def _authorize_context(p: Ctx, handle: str | None):
    s, identity = p.session, p.st.ctx.identity
    group = groups.active(s)
    row = _peek(s, "authorize", handle) if group is not None else None
    if row is None:
        return None
    tr = groups.trust(s, group, own_node=identity.node_id)
    b = _authreq_ok(row.payload["env"], tr, group, identity.node_id)
    own = tr.in_force(identity.node_id)
    if b is None or own is None or "identity.assert" not in own["body"]["scopes"] or group.status != "active":
        return None
    return group, tr, row, b


@router.get("/group/authorize/continue", response_class=HTMLResponse)
def authorize_consent(request: Request, p: Annotated[Ctx, Depends(user_page)]):
    found = _authorize_context(p, request.query_params.get("h"))
    if found is None:
        return not_found()
    group, tr, row, b = found
    member = tr.in_force(b["rp_node"])["body"]["member"]
    consent = set(profile.consent_fields(p.session, p.me.id, group.group_id))
    return render(p, "group_link_consent.html", handle=request.query_params["h"], member=member,
                  fp=groups.gc_fp(b["rp_node"]), fields=profile_schema.REGISTRY, consent=consent,
                  policy=set(profile.policy_fields(p.st.ctx.settings)))


@router.post("/group/authorize/continue")
async def authorize_decide(request: Request, p: Annotated[Ctx, Depends(user_form)]):
    form = await request.form()
    handle = form.get("h") if isinstance(form.get("h"), str) else None
    found = _authorize_context(p, handle)
    if found is None:
        return not_found()
    group, tr, row, b = found
    s, settings, identity = p.session, p.st.ctx.settings, p.st.ctx.identity
    s.delete(row)  # single use
    if form.get("decision") != "approve":
        groups.audit(s, p.me.id, "group_link_declined", p.me.id, {"rp_node": b["rp_node"]})
        return go("/me/linked-nodes", "dismissed")
    chosen = [n for n in profile_schema.REGISTRY if form.get(f"c.{n}") == "on"]
    profile.set_consent(s, p.me.id, group.group_id, chosen)
    link = group_channel.mint_account(s, settings, group, identity, p.me)
    s.merge(GroupIdpConsent(user_id=p.me.id, group_id=group.group_id, rp_node=b["rp_node"],
                            granted_at=groups.utcnow()))
    amr = ["pwd"] + (["otp"] if p.login is not None and p.login.mfa_verified_at is not None else [])
    auth_time = groups.aware(p.login.created_at) if p.login is not None else groups.utcnow()
    assertion = group_channel.sign_assertion(s, group, identity, aud_node=b["rp_node"], uid=link.account_uid,
                                             nonce=b["nonce"], state_hash=b["state_hash"], auth_time=auth_time,
                                             amr=amr, username_hint=p.me.username)
    groups.audit(s, p.me.id, "group_assertion_issued", p.me.id, {"aud_node": b["rp_node"], "purpose": "link",
                                                                 "jti": assertion["body"]["jti"]})
    member = tr.in_force(b["rp_node"])["body"]["member"]
    sealed = gc.seal_request(member["box_public_key"], group.group_id, b["rp_node"], assertion)
    return _autopost(p, member["url"].rstrip("/") + "/group/callback", "assertion", json.dumps(sealed),
                     f"Back to {member['name']}", f"Returning to {member['name']} to finish linking.")


# ---- B: callback and wizard (§5.3 steps 8-9, §5.4) -------------------------------------------------------------

@router.post("/group/callback")
async def callback_park(request: Request, p: Annotated[Ctx, Depends(page)]):
    raw = await _form_field(request, "assertion")
    s, settings, identity = p.session, p.st.ctx.settings, p.st.ctx.identity
    group = groups.active(s)
    if group is None:
        return not_found()
    a = _sealed_field(raw, s, settings, group, identity.node_id)
    if a is None or not group_channel.assertion_shape_ok(a, group.group_id) \
            or a["body"]["aud_node"] != identity.node_id:
        return not_found()
    _purge(s)
    handle = _park(s, "callback", {"a": a})
    return go(f"/group/callback/finish?h={handle}")


class LinkRefused(Exception):
    pass


def verify_link_assertion(s, settings, identity, group, a: dict, binding_cookie: str | None, user_id: str) -> None:
    """The relying-party checks of §5.4 for `purpose: link`. Consumes the state row and the `jti`."""
    b = a["body"]
    state = s.get(GroupSigninState, b["state_hash"]) if isinstance(b.get("state_hash"), str) else None
    if state is not None:
        s.delete(state)  # single use, success or not
        s.flush()
    if state is None or groups.aware(state.expires_at) < groups.utcnow():
        raise LinkRefused("This link request expired or was already used. Start again from Linked nodes.")
    if not binding_cookie or _h(binding_cookie) != state.binding:
        raise LinkRefused("This link was not started in this browser.")
    if state.link_user_id != user_id or state.purpose != "link" or b["purpose"] != "link":
        raise LinkRefused("This link was started by another account.")
    if b["iss_node"] != state.idp_node or b["nonce"] != state.nonce or state.group_id != group.group_id:
        raise LinkRefused("The answer does not belong to this link request.")
    if group.status != "active":
        raise LinkRefused("This node is resynchronizing with the group; try again in a minute.")
    tr = groups.trust(s, group, own_node=identity.node_id)
    cert = tr.in_force(b["iss_node"])
    key = tr.current_key(b["iss_node"])
    if cert is None or "identity.assert" not in cert["body"]["scopes"] or key is None \
            or a["signer"]["key_id"] != key[0] or not group_channel.verify_assertion_sig(a, key[1]):
        raise LinkRefused("The other node's confirmation could not be verified.")
    fresh = groups.freshness(s, group, identity.node_id, b["iss_node"], settings.group_view_max_age_hours)
    if not fresh.ok:
        raise LinkRefused("This node has no recent view of the group; try again after the next sync.")
    now = groups.utcnow()
    ia, na = gc.parse_time(b["issued_at"]), gc.parse_time(b["not_after"])
    if ia > now + groups.SKEW or now > na or na - ia > group_channel.ASSERTION_TTL:
        raise LinkRefused("The confirmation expired. Start again from Linked nodes.")
    try:
        with s.begin_nested():
            s.add(GroupAssertionSeen(group_id=group.group_id, jti=b["jti"], iss_node=b["iss_node"],
                                     expires_at=na + groups.SKEW))
    except IntegrityError:
        raise LinkRefused("This confirmation was already used.") from None


@router.get("/group/callback/finish", response_class=HTMLResponse)
def callback_finish(request: Request, p: Annotated[Ctx, Depends(user_page)]):
    s, settings, identity = p.session, p.st.ctx.settings, p.st.ctx.identity
    group = groups.active(s)
    row = _peek(s, "callback", request.query_params.get("h")) if group is not None else None
    if row is None:
        return not_found()
    a = row.payload["a"]
    s.delete(row)
    try:
        verify_link_assertion(s, settings, identity, group, a, request.cookies.get(BIND_COOKIE), p.me.id)
        if group_channel.local_link(s, group.group_id, p.me.id) is not None:
            raise LinkRefused("This account is already linked in the group.")
        if group_channel.link_by_uid(s, group.group_id, a["body"]["account_uid"]) is not None:
            raise LinkRefused("That account is already linked to another account on this node.")
    except LinkRefused as exc:
        groups.audit(s, p.me.id, "group_link_refused", p.me.id, {"reason": str(exc)[:120]})
        resp = render(p, "me_linked_nodes.html", status=400, **linked_context(p, str(exc)))
        resp.delete_cookie(BIND_COOKIE, path="/group/")
        return resp
    resp = _wizard(p, group, a)
    resp.delete_cookie(BIND_COOKIE, path="/group/")
    return resp


def _wizard_deadline(a: dict):
    """The latest moment the wizard may emit `account.link`: every receiver's evidence check requires the link's
    `issued_at ≤ A.not_after + SKEW` (§4.3), so a slower wizard would produce a link the group refuses."""
    return gc.parse_time(a["body"]["not_after"]) + groups.SKEW - WIZARD_MARGIN


def _snapshot(p: Ctx, group, a: dict, accept: list[str]) -> tuple[dict, dict]:
    """A's signed snapshot of the fields in `accept` only: operator policy ∩ this user's consent here (the default
    consent before the user has chosen), so values the user has not agreed to receive never land on this node."""
    s, settings, identity = p.session, p.st.ctx.settings, p.st.ctx.identity
    if not accept:
        return {}, {}
    try:
        snap = group_channel.fetch_snapshot(s, settings, identity, group, a["body"]["iss_node"],
                                            a["body"]["account_uid"], accept, p.st.http)
    except Exception:  # noqa: BLE001 - the link still works; the values arrive with the next sync
        snap = {}
    rx = group_channel.Rx(s, settings, identity, group, groups.trust(s, group, own_node=identity.node_id),
                          groups.utcnow(), a["body"]["iss_node"])
    return snap if isinstance(snap, dict) else {}, group_channel.snapshot_values(
        rx, a["body"]["account_uid"], snap, set(accept))


def _merge_snapshots(first: dict, second: dict) -> dict:
    out = {"entries": list(first.get("entries") or []), "epochs": list(first.get("epochs") or [])}
    for key in ("entries", "epochs"):
        extra = second.get(key) if isinstance(second, dict) else None
        if isinstance(extra, list):
            out[key] += [e for e in extra if e not in out[key]]
    return out


def _wizard(p: Ctx, group, a: dict, error: str | None = None) -> HTMLResponse:
    """The link wizard: per field, this node's value against the other node's, and this node's consent."""
    accept = profile.accept(p.session, p.st.ctx.settings, p.me.id, group.group_id)
    snap, theirs = _snapshot(p, group, a, accept)
    expires = min(groups.utcnow() + WIZARD_TTL, _wizard_deadline(a))
    handle = _park(p.session, "linkform", {"a": a, "snap": snap, "fetched": accept, "user": p.me.id}, expires)
    return render(p, "group_link_wizard.html", handle=handle, rows=_diff(p, theirs), error=error,
                  idp=_member_name(p, group, a["body"]["iss_node"]), fields=profile_schema.REGISTRY,
                  consent=set(profile.consent_fields(p.session, p.me.id, group.group_id)),
                  policy=set(profile.policy_fields(p.st.ctx.settings)))


def _member_name(p: Ctx, group, node: str) -> str:
    row = p.session.get(groups.GroupMember, (group.group_id, node))
    return row.name if row is not None else groups.gc_fp(node)


def _diff(p: Ctx, theirs: dict) -> list[dict]:
    mine = profile.values(p.session, p.st.ctx.settings, p.me)
    rows = []
    for name, spec in profile_schema.REGISTRY.items():
        if name not in theirs:
            continue
        value, cleared, _ = theirs[name]
        other = None if cleared else value
        here = p.session.get(profile.ProfileField, (p.me.id, name))
        mine_value = here.value if here is not None else None
        if name in ("bio", "privacy.profile_public"):
            mine_value = mine[name]
        rows.append({"name": name, "label": spec.label, "mine": describe(mine_value), "theirs": describe(other),
                     "same": mine_value == other})
    return rows


@router.post("/group/link/finish")
async def wizard_finish(request: Request, p: Annotated[Ctx, Depends(user_form)]):
    form = await request.form()
    s, settings, identity = p.session, p.st.ctx.settings, p.st.ctx.identity
    group = groups.active(s)
    handle = form.get("h") if isinstance(form.get("h"), str) else None
    row = None
    if group is not None and handle and len(handle) <= 100:
        row = s.get(GroupWebHandle, _h(handle))
    if row is None or row.kind != "linkform" or row.payload.get("user") != p.me.id:
        return not_found()
    payload = row.payload
    s.delete(row)
    a = payload["a"]
    now = groups.utcnow()
    if form.get("decision") != "link":
        groups.audit(s, p.me.id, "group_link_declined", p.me.id, {"idp_node": a["body"]["iss_node"]})
        return go("/me/linked-nodes", "dismissed")
    if now > groups.aware(row.expires_at) or now > _wizard_deadline(a):
        # the confirmation from the other node is too old for the group to accept this link (§4.3)
        groups.audit(s, p.me.id, "group_link_refused", p.me.id, {"reason": "wizard expired"})
        return render(p, "me_linked_nodes.html", status=400, **linked_context(
            p, "This link page was open too long and the other node's confirmation has expired. Nothing was "
               "linked. Start again from Linked nodes."))
    try:
        link = group_channel.complete_link(s, settings, group, identity, p.me, a["body"]["account_uid"], a, now=now)
    except group_channel.ChannelError as exc:
        return render(p, "me_linked_nodes.html", status=409, **linked_context(p, str(exc)))
    chosen = [n for n in profile_schema.REGISTRY if form.get(f"c.{n}") == "on"]
    profile.set_consent(s, p.me.id, group.group_id, chosen, form.get("signout") == "on")
    accept = set(profile.accept(s, settings, p.me.id, group.group_id))
    snap = payload["snap"]
    extra = sorted(accept - set(payload.get("fetched") or []))
    if extra:  # fields the user ticked in the wizard: fetched only now that they are consented
        snap = _merge_snapshots(snap, _snapshot(p, group, a, extra)[0])
    rx = group_channel.Rx(s, settings, identity, group, groups.trust(s, group, own_node=identity.node_id),
                          groups.utcnow(), a["body"]["iss_node"])
    mine = {n: s.get(profile.ProfileField, (p.me.id, n)) for n in accept}
    mine_values = {n: (r.value if r is not None else None) for n, r in mine.items()}
    group_channel.install_snapshot(rx, link.account_uid, snap, accept)
    resolved = 0
    for name in accept:
        pick = form.get(f"k.{name}")
        if pick not in ("mine", "theirs"):
            continue
        if len(profile.heads(s, p.me.id, name)) < 2 and pick == "theirs":
            continue
        current = s.get(profile.ProfileField, (p.me.id, name))
        value = mine_values[name] if pick == "mine" else (current.value if current is not None else None)
        if pick == "theirs":
            heads = profile.heads(s, p.me.id, name)
            theirs = [h for h in heads if h.origin_node != identity.node_id]
            if theirs:
                value = profile.CLEAR if theirs[-1].cleared else theirs[-1].value
        # a resolving version: its base is every current head, so the choice never opens a conflict (A11-v)
        profile.set_local(s, settings, identity, p.me, name, profile.CLEAR if value is None else value)
        resolved += 1
    group_channel.emit_pending(s, settings, identity, p.me)
    groups.audit(s, p.me.id, "group_link_wizard", p.me.id, {"resolved": resolved})
    return go("/me/linked-nodes", "saved")


__all__ = ["router", "verify_link_assertion", "BIND_COOKIE"]
