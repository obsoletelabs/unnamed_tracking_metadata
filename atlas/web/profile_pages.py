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


#: C14-4 (super group, "invisible topology"): the same review cards without naming any node
HIDDEN_TITLES = {"concurrent": "Changed in two places at once", "privacy_widen": "Made public elsewhere",
                 "suspect": "Set by a removed device", "suspect_link": "Linked with the help of a removed device",
                 "missing_base": "Arrived without its history"}


def topology_hidden(p: Ctx) -> bool:
    """In a super group (C14-4) user pages never name a node, its fingerprint or which node set a value."""
    from .. import super_group

    return super_group.enabled(p.st.ctx.settings)


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
    policy = set(profile.policy_fields(settings))
    names = node_names(s, group)
    hidden = topology_hidden(p)
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
                     # in a group but this account is not linked: an actionable badge, not "this node only"
                     "unlinked": group is not None and not linked,
                     "why": None if name in accepted else ("not shared by this node's administrator"
                                                           if name not in policy else "you chose not to share it"),
                     "stored": row is not None and row.value is not None,
                     # C14-4: in a super group no user page names a node (the value is simply synced)
                     "origin": None if origin in (None, own) or hidden else names.get(origin, groups.gc_fp(origin)),
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
    hidden = topology_hidden(p)
    titles = HIDDEN_TITLES if hidden else KIND_TITLES

    def where(node):
        if hidden:
            return None
        if node is None:
            return "unknown"
        if node == p.st.ctx.identity.node_id:
            return "this node"
        return names.get(node, groups.gc_fp(node))

    cards = []
    for row in profile.open_conflicts(s, me.id):
        spec = profile_schema.REGISTRY.get(row.field)
        card = {"row": row, "label": spec.label if spec else ("Linked sign-ins" if hidden else "Linked nodes"),
                "title": titles.get(row.kind, row.kind), "spec": spec, "options": []}
        if row.kind == "concurrent":
            for choice, hlc in (("winner", row.winner_hlc), ("loser", row.loser_hlc)):
                v = profile.version(s, me.id, row.field, hlc)
                if v is not None:
                    card["options"].append({"choice": choice, "value": describe(None if v.cleared else v.value),
                                            "where": where(v.origin_node), "at": profile.hlc_time(v.hlc),
                                            "shown": choice == "winner"})
        elif row.kind == "privacy_widen":
            card["note"] = ("Your profile was set to public elsewhere. It stays private here until you decide."
                            if hidden else
                            f"{where((row.detail or {}).get('origin'))} set your profile to public. This node keeps "
                            "it private until you decide.")
        elif row.kind in ("suspect", "missing_base"):
            v = profile.version(s, me.id, row.field, row.loser_hlc)
            shown = describe(None if v is None or v.cleared else v.value)
            card["note"] = (f"The current value ({shown}) needs your confirmation." if hidden else
                            f"The current value ({shown}) came from {where((row.detail or {}).get('origin'))}.")
        elif row.kind == "suspect_link":
            card["note"] = ("A sign-in was linked to your account with the help of a device that was later removed "
                            "as compromised. Nothing is synced with it until you decide." if hidden else
                            f"{where(row.loser_hlc)} was linked to your account with the help of "
                            f"{where((row.detail or {}).get('evidenced_by'))}, which was later revoked as "
                            "compromised. Nothing is synced with it until you decide.")
        cards.append(card)
    return cards


@router.get("/me/conflicts", response_class=HTMLResponse)
def conflicts_page(p: Annotated[Ctx, Depends(user_page)]):
    resolved = list(p.session.scalars(select(ProfileConflict).where(
        ProfileConflict.user_id == p.me.id, ProfileConflict.status != "open")
        .order_by(ProfileConflict.resolved_at.desc()).limit(10)))
    return render(p, "me_conflicts.html", cards=conflict_cards(p), resolved=resolved,
                  titles=HIDDEN_TITLES if topology_hidden(p) else KIND_TITLES,
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
               resyncing=group.status == "resyncing", strip=sync_strip(p, group, me))
    return ctx


def sync_strip(p: Ctx, group, user=None) -> dict:
    """The "Group sync" strip (SPEC-UI-POLISH-PASS addendum item 3), on /me/linked-nodes (`user` given: what this
    account shares and its open decisions) and on the admin group page (no user: the node's totals). Per member:
    when this node last heard from it, its status and the last error. Read only; computed from rows already kept."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    from ..models import GroupAccountLink, User

    s, settings = p.session, p.st.ctx.settings
    own = p.st.ctx.identity.node_id
    peers = catalogue_peers(s)
    interval = max(int(settings.group_sync_interval), 1)
    limit = datetime.now(UTC) - timedelta(seconds=5 * interval)
    members = []
    for m in s.scalars(select(groups.GroupMember).where(groups.GroupMember.group_id == group.group_id)
                       .order_by(groups.GroupMember.name)):
        if m.node_id == own or m.status in ("revoked", "left"):
            continue
        seen = groups.aware(m.last_seen_at) if m.last_seen_at is not None else None
        members.append({"name": m.name, "node": m.node_id, "status": m.status, "seen": seen,
                        "stale": seen is None or seen < limit, "error": m.last_error,
                        "catalogue": catalogue_state(peers, m.node_id, m.url)})
    policy = profile.policy_fields(settings)
    out: dict[str, Any] = {"members": members, "interval": interval, "sync_on": bool(policy),
                           "last": max((m["seen"] for m in members if m["seen"]), default=None)}
    if user is not None:
        linked = group_channel.local_link(s, group.group_id, user.id) is not None
        shared = profile.accept(s, settings, user.id, group.group_id) if linked else []
        out.update(linked=linked, shared=[profile_schema.REGISTRY[n].label for n in shared],
                   conflicts=profile.open_count(s, user.id))
    else:
        people = s.scalar(select(func.count()).select_from(User).where(User.deleted_at.is_(None),
                                                                       User.status == "active")) or 0
        linked_n = s.scalar(select(func.count(func.distinct(GroupAccountLink.user_id))).where(
            GroupAccountLink.group_id == group.group_id)) or 0
        out.update(shared=[profile_schema.REGISTRY[n].label for n in policy], people=people, linked_n=linked_n,
                   unlinked_n=max(people - linked_n, 0),
                   conflicts=s.scalar(select(func.count()).select_from(ProfileConflict).where(
                       ProfileConflict.status == "open")) or 0)
    return out


def catalogue_peers(session) -> list:
    from ..models import Peer

    return list(session.scalars(select(Peer)))


def _same_url(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.rstrip("/").lower() == b.rstrip("/").lower()


def catalogue_state(peers: list, node_id: str, url: str | None) -> dict:
    """SPEC-UI-POLISH-PASS §4.2 "catalogue federation with members" [dep], as far as this release's backend goes:
    group membership never creates a peer by itself (no auto-peer exists yet), so this only reports whether the
    administrator follows that member's catalogue on Peers. `on`: an enabled peer for the member's node id (or its
    address) with no error; `error`: enabled but its last pull failed; `paused`: the peer is disabled; `off`: none.
    """
    peer = next((x for x in peers if x.node_id == node_id), None) or next(
        (x for x in peers if _same_url(x.url, url)), None)
    if peer is None:
        return {"state": "off", "peer_id": None}
    if not peer.enabled:
        return {"state": "paused", "peer_id": peer.id}
    return {"state": "error" if peer.last_error else "on", "peer_id": peer.id, "error": peer.last_error}


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
