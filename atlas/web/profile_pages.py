"""Preferences, the review queue and linked nodes (CONTRACT-C10 §4.3, §4.6, §5.3, §7).

`/me/settings` shows every registry preference with where its value came from and whether it is synced;
`/me/conflicts` is the review queue; `/me/linked-nodes` lists the nodes holding this account, the per-node consent
and the link / unlink controls. Everything works without a group: preferences are local first.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .. import accounts, group_channel, groups, profile, profile_schema
from ..models import GroupAccountNode, ProfileConflict
from .deps import Page as Ctx
from .deps import form_dict, go, throttle, user_form, user_page
from .routes import render

router = APIRouter()

EDITABLE = [n for n in profile_schema.REGISTRY if n not in ("bio", "privacy.profile_public", "identity.refs")]
KIND_TITLES = {"concurrent": "Changed on two nodes at once", "privacy_widen": "Another node made your profile public",
               "suspect": "Set by a node revoked as compromised", "suspect_link": "Linked by a revoked node",
               "missing_base": "Arrived without its history"}


def node_names(session, group) -> dict[str, str]:
    if group is None:
        return {}
    return {m.node_id: m.name for m in session.scalars(select(groups.GroupMember).where(
        groups.GroupMember.group_id == group.group_id))}


def describe(value: Any) -> str:
    if value is None:
        return "the default"
    if value is True:
        return "on"
    if value is False:
        return "off"
    if isinstance(value, list):
        return ", ".join(str(v.get("issuer_name", v)) if isinstance(v, dict) else str(v) for v in value) or "none"
    return str(value)


def preference_rows(p: Ctx) -> list[dict]:
    """One row per registry field for the settings form: value, origin, sync state and an open conflict."""
    s, me = p.session, p.me
    settings, own = p.st.ctx.settings, p.st.ctx.identity.node_id
    group = groups.active(s)
    linked = group is not None and group_channel.local_link(s, group.group_id, me.id) is not None
    accepted = set(profile.accept(s, settings, me.id, group.group_id)) if linked else set()
    names = node_names(s, group)
    values = profile.values(s, settings, me)
    shown = {r.field: r for r in s.scalars(select(profile.ProfileField).where(profile.ProfileField.user_id == me.id))}
    open_rows = {r.field: r for r in profile.open_conflicts(s, me.id)}
    rows = []
    for name, spec in profile_schema.REGISTRY.items():
        row = shown.get(name)
        origin = row.origin_node if row is not None else None
        conflict = open_rows.get(name)
        competing = None
        if conflict is not None and conflict.kind == "concurrent":
            v = profile.version(s, me.id, name, conflict.loser_hlc)
            competing = describe(None if v is None or v.cleared else v.value) if v else None
        rows.append({"name": name, "spec": spec, "value": values[name], "synced": name in accepted,
                     "stored": row is not None and row.value is not None,
                     "origin": None if origin in (None, own) else names.get(origin, groups.gc_fp(origin)),
                     "conflict": conflict, "competing": competing, "editable": name in EDITABLE})
    return rows


def _parse(spec: profile_schema.Field, raw: str | None, present: bool) -> Any:
    """Form input to a value, or profile.CLEAR for an empty choice; None means "leave as it is"."""
    if spec.kind == "bool":
        return raw == "on"
    if not present:
        return None
    raw = (raw or "").strip()
    if raw == "":
        return profile.CLEAR
    if spec.kind == "number":
        try:
            return float(raw)
        except ValueError:
            raise profile.ProfileError(f"{spec.label}: must be a number") from None
    return raw


@router.post("/me/preferences")
async def preferences_save(request: Request, p: Annotated[Ctx, Depends(user_form)]):
    form = await form_dict(request)
    s, me, settings = p.session, p.me, p.st.ctx.settings
    current = profile.values(s, settings, me)
    changed = 0
    try:
        for name in EDITABLE:
            spec = profile_schema.REGISTRY[name]
            if spec.kind == "bool" and f"present.{name}" not in form:
                continue
            value = _parse(spec, form.get(f"f.{name}"), f"f.{name}" in form)
            if value is None:
                continue
            row = s.get(profile.ProfileField, (me.id, name))
            stored = row.value if row is not None else None  # None: unset or reset, the form shows "Default"
            if value is profile.CLEAR:
                if stored is None:
                    continue
            elif profile.validate(name, value) == (current[name] if spec.kind == "bool" else stored):
                continue
            profile.set_local(s, settings, p.st.ctx.identity, me, name, value)
            changed += 1
    except profile.ProfileError as exc:
        s.rollback()
        raise HTTPException(400, str(exc)) from None
    return go("/me/settings#preferences", "saved" if changed else "nochange")


# ---- review queue ---------------------------------------------------------------------------------------------

def conflict_cards(p: Ctx) -> list[dict]:
    s, me = p.session, p.me
    group = groups.load(s)
    names = node_names(s, group)

    def where(node):
        if node is None:
            return "unknown"
        if node == p.st.ctx.identity.node_id:
            return "this node"
        return names.get(node, groups.gc_fp(node))

    cards = []
    for row in profile.open_conflicts(s, me.id):
        spec = profile_schema.REGISTRY.get(row.field)
        card = {"row": row, "label": spec.label if spec else "Linked nodes", "title": KIND_TITLES.get(row.kind, row.kind),
                "spec": spec, "options": []}
        if row.kind == "concurrent":
            for choice, hlc in (("winner", row.winner_hlc), ("loser", row.loser_hlc)):
                v = profile.version(s, me.id, row.field, hlc)
                if v is not None:
                    card["options"].append({"choice": choice, "value": describe(None if v.cleared else v.value),
                                            "where": where(v.origin_node), "at": profile.hlc_time(v.hlc),
                                            "shown": choice == "winner"})
        elif row.kind == "privacy_widen":
            card["note"] = (f"{where((row.detail or {}).get('origin'))} set your profile to public. This node keeps "
                            "it private until you decide.")
        elif row.kind in ("suspect", "missing_base"):
            v = profile.version(s, me.id, row.field, row.loser_hlc)
            card["note"] = (f"The current value ({describe(None if v is None or v.cleared else v.value)}) came from "
                            f"{where((row.detail or {}).get('origin'))}.")
        elif row.kind == "suspect_link":
            card["note"] = (f"{where(row.loser_hlc)} was linked to your account with the help of "
                            f"{where((row.detail or {}).get('evidenced_by'))}, which was later revoked as "
                            "compromised. Nothing is synced with it until you decide.")
        cards.append(card)
    return cards


@router.get("/me/conflicts", response_class=HTMLResponse)
def conflicts_page(p: Annotated[Ctx, Depends(user_page)]):
    resolved = list(p.session.scalars(select(ProfileConflict).where(
        ProfileConflict.user_id == p.me.id, ProfileConflict.status != "open")
        .order_by(ProfileConflict.resolved_at.desc()).limit(10)))
    return render(p, "me_conflicts.html", cards=conflict_cards(p), resolved=resolved, titles=KIND_TITLES,
                  labels={n: f.label for n, f in profile_schema.REGISTRY.items()})


@router.post("/me/conflicts/{cid}")
def conflict_resolve(cid: str, p: Annotated[Ctx, Depends(user_form)], choice: Annotated[str, Form(max_length=16)],
                     custom: Annotated[str, Form(max_length=600)] = ""):
    if choice not in ("winner", "loser", "custom", "accept", "dismiss", "remove", "keep"):
        raise HTTPException(400, "Choose one of the offered options.")
    row = p.session.get(ProfileConflict, cid)
    value: Any = custom
    if row is not None and choice == "custom" and row.field in profile_schema.REGISTRY:
        spec = profile_schema.REGISTRY[row.field]
        try:
            value = _parse(spec, custom, True) if spec.kind != "bool" else custom == "on"
        except profile.ProfileError as exc:
            raise HTTPException(400, str(exc)) from None
    try:
        profile.resolve(p.session, p.st.ctx.settings, p.st.ctx.identity, p.me, cid,
                        "dismiss" if choice == "keep" else choice, value)
    except profile.ProfileError as exc:
        p.session.rollback()
        raise HTTPException(400, str(exc)) from None
    return go("/me/conflicts", "saved")


# ---- linked nodes ---------------------------------------------------------------------------------------------

def linked_context(p: Ctx, error: str | None = None) -> dict:
    s, me, settings = p.session, p.me, p.st.ctx.settings
    own = p.st.ctx.identity.node_id
    group = groups.active(s)
    ctx: dict[str, Any] = {"group": group, "error": error, "fp": groups.gc_fp}
    if group is None:
        return ctx
    tr = groups.trust(s, group, own_node=own)
    link = group_channel.local_link(s, group.group_id, me.id)
    names = node_names(s, group)
    holders = []
    if link is not None:
        for row in s.scalars(select(GroupAccountNode).where(GroupAccountNode.group_id == group.group_id,
                                                            GroupAccountNode.account_uid == link.account_uid)):
            holders.append({"node": row.node_id, "name": "This node" if row.node_id == own
                            else names.get(row.node_id, "Unknown node"), "status": row.status,
                            "since": (row.evidence or {}).get("linked_at"), "self": row.node_id == own})
    linkable = []
    for node, cert in sorted(tr.members().items(), key=lambda x: x[1]["body"]["member"]["name"]):
        member = cert["body"]["member"]
        if node == own or "identity.assert" not in cert["body"]["scopes"]:
            continue
        if any(h["node"] == node and h["status"] == "linked" for h in holders):
            continue
        linkable.append({"node": node, "name": member["name"], "url": member["url"],
                         "verified": member.get("url_verified", False)})
    consent_row = profile.consent(s, me.id, group.group_id)
    from .. import group_signin

    ctx.update(open_targets=group_signin.open_targets(s, p.st.ctx.identity, me),
               chained=p.login is not None and p.login.via_group is not None)
    ctx.update(link=link, holders=sorted(holders, key=lambda h: (not h["self"], h["name"])), linkable=linkable,
               policy=set(profile.policy_fields(settings)), consent=set(profile.consent_fields(s, me.id,
                                                                                               group.group_id)),
               signout=consent_row.signout_propagation if consent_row else True, fields=profile_schema.REGISTRY,
               resyncing=group.status == "resyncing")
    return ctx


@router.get("/me/linked-nodes", response_class=HTMLResponse)
def linked_nodes(p: Annotated[Ctx, Depends(user_page)]):
    return render(p, "me_linked_nodes.html", **linked_context(p))


@router.post("/me/linked-nodes/consent")
async def linked_consent(request: Request, p: Annotated[Ctx, Depends(user_form)]):
    form = await form_dict(request)
    s, me = p.session, p.me
    group = groups.active(s)
    if group is None:
        raise HTTPException(404, "This node is not in a group.")
    before = set(profile.accept(s, p.st.ctx.settings, me.id, group.group_id))
    chosen = [n for n in profile_schema.REGISTRY if form.get(f"c.{n}") == "on"]
    profile.set_consent(s, me.id, group.group_id, chosen, form.get("signout") == "on")
    accounts.audit(s, me.id, "profile_consent_changed", me.id, {"fields": chosen})
    added = set(profile.accept(s, p.st.ctx.settings, me.id, group.group_id)) - before
    if added and group_channel.local_link(s, group.group_id, me.id) is not None:
        group_channel.emit_pending(s, p.st.ctx.settings, p.st.ctx.identity, me)
    return go("/me/linked-nodes", "saved")


@router.post("/me/linked-nodes/unlink")
def linked_unlink(p: Annotated[Ctx, Depends(user_form)], password: Annotated[str, Form(max_length=256)] = ""):
    throttle(p, f"group-unlink:{p.me.id}", 5, 3600)
    if not accounts.reauthenticated(p.me, password, p.login):
        return render(p, "me_linked_nodes.html", status=400,
                      **linked_context(p, "Confirm your current password to unlink."))
    if not group_channel.unlink(p.session, p.st.ctx.settings, p.st.ctx.identity, p.me):
        raise HTTPException(404, "This account is not linked.")
    return go("/me/linked-nodes", "saved")


__all__ = ["router", "preference_rows", "linked_context"]
