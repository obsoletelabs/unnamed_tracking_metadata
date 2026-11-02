"""Profiles, lists, follows, notifications, comments, moderation, export and deletion."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .. import accounts, social
from ..models import Canonical, Comment, Follow, ListItem, Notification, Proposal, Report, User, UserList
from ..service import AtlasError
from . import release_year
from .deps import (
    SESSION_COOKIE,
    client_ip,
    csrf_form,
    go,
    moderate_form,
    moderate_page,
    page,
    throttle,
    user_form,
    user_page,
)
from .deps import Page as Ctx
from .routes import _proposal_card, render, safe_next, when

router = APIRouter()
_STATUS = {"not_found": 404, "forbidden": 403, "conflict": 409}


def fail(exc: AtlasError) -> HTTPException:
    return HTTPException(_STATUS.get(exc.code, 400), str(exc))


def back(default: str, to: str | None = None) -> str:
    return safe_next(to) if to else default


def game_title(p: Ctx, cid: str) -> str:
    row = p.session.get(Canonical, cid)
    return (row.view.get("title") if row and row.view else None) or cid


# ---- profiles -----------------------------------------------------------------------------------------

@router.get("/u/{username}", response_class=HTMLResponse)
def profile(username: str, p: Annotated[Ctx, Depends(page)]):
    u = p.session.scalars(select(User).where(User.username == username.lower())).first()
    mine = bool(p.user and u and p.user.id == u.id)
    if u is None or u.deleted_at is not None or not (u.profile_public or mine or p.can_review):
        raise HTTPException(404, "No such user.")
    accepted = []
    for pr in p.session.scalars(select(Proposal).where(Proposal.proposer_id == u.id, Proposal.status == "approved")
                                .order_by(Proposal.decided_at.desc()).limit(15)):
        accepted.append({"kind": pr.kind.replace("_", " "), "cid": pr.canonical_id, "when": when(pr.decided_at),
                         "title": game_title(p, pr.canonical_id) if pr.canonical_id else "(new game)"})
    lists = list(p.session.scalars(select(UserList).where(UserList.user_id == u.id, UserList.public.is_(True))))
    counts = {lst.id: len(p.session.scalars(select(ListItem.canonical_id).where(ListItem.list_id == lst.id)).all())
              for lst in lists}
    following = bool(p.user and p.session.get(Follow, (p.user.id, "user", u.id)))
    return render(p, "profile.html", u=u, stats=social.profile_stats(p.session, u), accepted=accepted, lists=lists,
                  counts=counts, mine=mine, following=following, hidden=not u.profile_public)


@router.get("/me/settings", response_class=HTMLResponse)
def settings_form(p: Annotated[Ctx, Depends(user_page)]):
    return render(p, "me_settings.html", error=None)


@router.post("/me/settings")
def settings_save(p: Annotated[Ctx, Depends(user_form)], bio: Annotated[str, Form(max_length=500)] = "",
                  profile_public: Annotated[str, Form()] = "", email_notifications: Annotated[str, Form()] = ""):
    if any(ord(c) < 32 and c not in "\n\r" for c in bio):
        raise HTTPException(400, "The bio contains characters that are not allowed.")
    u = p.me
    # Versioned (CONTRACT-C10 §4): `profile.set_local` dual-writes app_user.bio / profile_public, removes followers
    # when the profile turns private, and sends the change to linked nodes when the user consented to that.
    from .. import profile

    for field, value in (("bio", bio.strip()), ("privacy.profile_public", profile_public == "on")):
        current = u.bio or "" if field == "bio" else bool(u.profile_public)
        if value != current:
            try:
                profile.set_local(p.session, p.st.ctx.settings, p.st.ctx.identity, u, field, value)
            except profile.ProfileError as exc:
                raise HTTPException(400, str(exc)) from None
    if not u.profile_public:  # a private profile has no followers and sends no activity to anyone
        p.session.execute(Follow.__table__.delete().where(Follow.target_type == "user", Follow.target_id == u.id))
    u.email_notifications = email_notifications == "on"
    return go("/me/settings", "saved")


@router.get("/me/export.json")
def export(p: Annotated[Ctx, Depends(user_page)]):
    data = json.dumps(social.export_data(p.session, p.me), indent=2, default=str)
    return Response(data, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{p.st.ctx.settings.app_name.lower()}-{p.me.username}.json"',
        "Cache-Control": "no-store"})


@router.post("/me/delete")
def delete_account(p: Annotated[Ctx, Depends(user_form)], confirm: Annotated[str, Form(max_length=64)],
                   password: Annotated[str, Form(max_length=256)] = ""):
    throttle(p, f"delete:user:{p.me.id}", 5, 3600, "Too many attempts")
    if confirm.strip().lower() != p.me.username or not accounts.reauthenticated(p.me, password, p.login):
        raise HTTPException(400, "Type your username and your current password to confirm.")
    try:
        social.delete_account(p.session, p.me, p.st.ctx)
    except AtlasError as exc:
        raise fail(exc) from exc
    resp = go("/", "bye")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---- lists --------------------------------------------------------------------------------------------

@router.get("/me/lists", response_class=HTMLResponse)
def my_lists(p: Annotated[Ctx, Depends(user_page)]):
    try:
        social.ensure_default_lists(p.session, p.st.ctx, p.me)
    except AtlasError:
        pass
    rows = list(p.session.scalars(select(UserList).where(UserList.user_id == p.me.id).order_by(UserList.created_at)))
    counts = {lst.id: len(p.session.scalars(select(ListItem.canonical_id).where(ListItem.list_id == lst.id)).all())
              for lst in rows}
    return render(p, "lists.html", rows=rows, counts=counts, enabled=p.st.ctx.settings.lists_enabled)


@router.post("/me/lists")
def list_create(p: Annotated[Ctx, Depends(user_form)], name: Annotated[str, Form(max_length=80)],
                description: Annotated[str, Form(max_length=300)] = "", public: Annotated[str, Form()] = ""):
    try:
        row = social.create_list(p.session, p.st.ctx, p.me, name, description, public == "on")
    except AtlasError as exc:
        raise fail(exc) from exc
    return go(f"/list/{row.id}", "saved")


@router.get("/list/{lid}", response_class=HTMLResponse)
def view_list(lid: str, p: Annotated[Ctx, Depends(page)]):
    try:
        row = social.get_list(p.session, lid, p.user)
    except AtlasError as exc:
        raise fail(exc) from exc
    owner = p.session.get(User, row.user_id)
    items = []
    for it in p.session.scalars(select(ListItem).where(ListItem.list_id == lid).order_by(ListItem.added_at.desc())):
        g = p.session.get(Canonical, it.canonical_id)
        if g is not None and g.merged_into:
            g = p.session.get(Canonical, g.merged_into)
        items.append({"cid": g.id if g else it.canonical_id, "title": (g.view.get("title") if g and g.view else "?"),
                      "year": (release_year(g.view) if g and g.view else ""), "note": it.note})
    return render(p, "list.html", row=row, owner=owner, items=items, mine=bool(p.user and p.user.id == row.user_id))


@router.post("/list/{lid}/settings")
def list_settings(lid: str, p: Annotated[Ctx, Depends(user_form)], name: Annotated[str, Form(max_length=80)],
                  description: Annotated[str, Form(max_length=300)] = "", public: Annotated[str, Form()] = ""):
    try:
        row = social.owned_list(p.session, lid, p.me)
    except AtlasError as exc:
        raise fail(exc) from exc
    name = name.strip()
    if not name:
        raise HTTPException(400, "A list needs a name.")
    clash = p.session.scalars(select(UserList.id).where(UserList.user_id == p.me.id, UserList.name == name,
                                                        UserList.id != lid)).first()
    if clash:
        raise HTTPException(409, "You already have a list with that name.")
    row.name, row.description, row.public = name, description.strip()[:300], public == "on"
    return go(f"/list/{lid}", "saved")


@router.post("/list/{lid}/delete")
def list_delete(lid: str, p: Annotated[Ctx, Depends(user_form)]):
    try:
        social.delete_list(p.session, p.me, lid)
    except AtlasError as exc:
        raise fail(exc) from exc
    return go("/me/lists", "saved")


@router.post("/list/{lid}/add")
def list_add(lid: str, p: Annotated[Ctx, Depends(user_form)], game: Annotated[str, Form(max_length=40)],
             note: Annotated[str, Form(max_length=200)] = "", to: Annotated[str, Form()] = ""):
    try:
        social.add_to_list(p.session, p.st.ctx, p.me, lid, game, note)
    except AtlasError as exc:
        raise fail(exc) from exc
    return go(back(f"/list/{lid}", to), "saved")


@router.post("/list/{lid}/remove")
def list_remove(lid: str, p: Annotated[Ctx, Depends(user_form)], game: Annotated[str, Form(max_length=40)]):
    try:
        social.remove_from_list(p.session, p.me, lid, game)
    except AtlasError as exc:
        raise fail(exc) from exc
    return go(f"/list/{lid}", "saved")


# ---- follows ------------------------------------------------------------------------------------------

@router.post("/follow")
def follow(p: Annotated[Ctx, Depends(user_form)], type: Annotated[str, Form(max_length=8)],
           target: Annotated[str, Form(max_length=40)], to: Annotated[str, Form()] = ""):
    try:
        social.follow(p.session, p.st.ctx, p.me, type, target)
    except AtlasError as exc:
        raise fail(exc) from exc
    return go(back("/me/following", to), "saved")


@router.post("/unfollow")
def unfollow(p: Annotated[Ctx, Depends(user_form)], type: Annotated[str, Form(max_length=8)],
             target: Annotated[str, Form(max_length=40)], to: Annotated[str, Form()] = ""):
    social.unfollow(p.session, p.me, type, target)
    return go(back("/me/following", to), "saved")


@router.get("/me/following", response_class=HTMLResponse)
def following(p: Annotated[Ctx, Depends(user_page)]):
    games, people = [], []
    for f in p.session.scalars(select(Follow).where(Follow.user_id == p.me.id).order_by(Follow.created_at.desc())):
        if f.target_type == "game":
            games.append({"id": f.target_id, "title": game_title(p, f.target_id)})
        else:
            u = p.session.get(User, f.target_id)
            if u is not None and u.deleted_at is None:
                people.append({"id": u.id, "name": u.username})
    return render(p, "following.html", games=games, people=people)


# ---- notifications ------------------------------------------------------------------------------------

@router.get("/notifications", response_class=HTMLResponse)
def notifications(p: Annotated[Ctx, Depends(user_page)]):
    rows = list(p.session.scalars(select(Notification).where(Notification.user_id == p.me.id)
                                  .order_by(Notification.created_at.desc()).limit(100)))
    return render(p, "notifications.html", rows=rows, when=when)


@router.post("/notifications/read")
def notifications_read(p: Annotated[Ctx, Depends(user_form)], id: Annotated[str, Form(max_length=26)] = ""):
    social.mark_read(p.session, p.me.id, id or None)
    return go("/notifications")


@router.get("/go/{nid}")
def open_notification(nid: str, p: Annotated[Ctx, Depends(user_page)]):
    """Marks one notification read and continues to its target (a GET is fine: only the owner's own flag changes)."""
    n = p.session.get(Notification, nid)
    if n is None or n.user_id != p.me.id:
        raise HTTPException(404, "No such notification.")
    social.mark_read(p.session, p.me.id, nid)
    return go(safe_next(n.url))


# ---- comments -----------------------------------------------------------------------------------------

@router.post("/game/{cid}/comments")
def comment_game(cid: str, p: Annotated[Ctx, Depends(user_form)], body: Annotated[str, Form(max_length=4000)],
                 parent: Annotated[str, Form(max_length=26)] = ""):
    throttle(p, f"comment:{p.me.id}", 6, 60, "You are commenting too quickly")
    try:
        row = social.add_comment(p.session, p.st.ctx, p.me, body, canonical_id=cid, parent_id=parent or None)
    except AtlasError as exc:
        p.session.rollback()
        raise fail(exc) from exc
    return go(f"/game/{cid}#c-{row.id}", "saved")


@router.post("/comments/{cid}/edit")
def comment_edit(cid: str, p: Annotated[Ctx, Depends(user_form)], body: Annotated[str, Form(max_length=4000)],
                 to: Annotated[str, Form()] = "/"):
    try:
        social.edit_comment(p.session, p.me, cid, body)
    except AtlasError as exc:
        raise fail(exc) from exc
    return go(back("/", to), "saved")


@router.post("/comments/{cid}/delete")
def comment_delete(cid: str, p: Annotated[Ctx, Depends(user_form)], to: Annotated[str, Form()] = "/"):
    try:
        social.delete_comment(p.session, p.me, cid)
    except AtlasError as exc:
        raise fail(exc) from exc
    return go(back("/", to), "saved")


@router.post("/comments/{cid}/report")
def comment_report(cid: str, p: Annotated[Ctx, Depends(user_form)], reason: Annotated[str, Form(max_length=300)] = "",
                   to: Annotated[str, Form()] = "/"):
    throttle(p, f"report:{p.me.id}", 10, 3600, "Too many reports")
    try:
        social.report_comment(p.session, p.st.ctx, p.me, cid, reason)
    except AtlasError as exc:
        raise fail(exc) from exc
    return go(back("/", to), "saved")


@router.post("/comments/{cid}/moderate")
def comment_moderate(cid: str, p: Annotated[Ctx, Depends(moderate_form)], action: Annotated[str, Form()],
                     to: Annotated[str, Form()] = "/review/reports"):
    try:
        social.moderate(p.session, p.st.ctx, p.me, cid, action)
    except AtlasError as exc:
        raise fail(exc) from exc
    return go(back("/review/reports", to), "saved")


@router.get("/review/reports", response_class=HTMLResponse)
def reports(p: Annotated[Ctx, Depends(moderate_page)]):
    p.session.flush()
    rows = []
    ids = {r.comment_id for r in p.session.scalars(select(Report).where(Report.status == "open"))}
    hidden = {c.id for c in p.session.scalars(select(Comment).where(Comment.status == "hidden"))}
    for c in p.session.scalars(select(Comment).where(Comment.id.in_(ids | hidden)).order_by(Comment.created_at)):
        author = p.session.get(User, c.user_id)
        reps = list(p.session.scalars(select(Report).where(Report.comment_id == c.id, Report.status == "open")))
        rows.append({"c": c, "author": author.username if author else "?", "reports": [r.reason for r in reps if r.reason],
                     "n": len(reps), "where": f"/game/{c.canonical_id}#c-{c.id}" if c.canonical_id
                     else f"/proposal/{c.proposal_id}#c-{c.id}"})
    return render(p, "reports.html", rows=rows)


# ---- proposal pages (with discussion) -----------------------------------------------------------------

@router.get("/proposal/{pid}", response_class=HTMLResponse)
def proposal_page(pid: str, p: Annotated[Ctx, Depends(user_page)]):
    row = p.session.get(Proposal, pid)
    if row is None or not social.can_view_proposal_thread(p.st.ctx, p.me, row):
        raise HTTPException(404, "No such proposal.")
    card: dict[str, Any] = _proposal_card(p, row)
    thread = social.thread(p.session, proposal_id=pid, viewer=p.me, ctx=p.st.ctx)
    return render(p, "proposal.html", r=row, card=card, thread=thread, target={"proposal": pid},
                  action=f"/proposal/{pid}/comments", where=f"/proposal/{pid}")


@router.post("/proposal/{pid}/comments")
def comment_proposal(pid: str, p: Annotated[Ctx, Depends(user_form)], body: Annotated[str, Form(max_length=4000)],
                     parent: Annotated[str, Form(max_length=26)] = ""):
    throttle(p, f"comment:{p.me.id}", 6, 60, "You are commenting too quickly")
    try:
        row = social.add_comment(p.session, p.st.ctx, p.me, body, proposal_id=pid, parent_id=parent or None)
    except AtlasError as exc:
        p.session.rollback()
        raise fail(exc) from exc
    return go(f"/proposal/{pid}#c-{row.id}", "saved")


_ = (client_ip, csrf_form)
