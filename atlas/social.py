"""User features: lists, follows, notifications, comments with moderation, profiles, export and deletion."""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import accounts, mail
from .models import (
    Canonical,
    Comment,
    Follow,
    ListItem,
    Notification,
    Proposal,
    Report,
    Token,
    User,
    UserList,
    UserSession,
    now,
)
from .service import AtlasError, Ctx, write_lock

MENTION = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_-]{2,31})")
EMAIL_PER_DAY = 20
EDIT_WINDOW = timedelta(minutes=15)
MAX_DEPTH = 3
BADGES = (("first", "First accepted edit", 1), ("ten", "10 accepted edits", 10), ("hundred", "100 accepted edits", 100))


# ---- lists --------------------------------------------------------------------------------------------

def _guard_lists(ctx: Ctx) -> None:
    if not ctx.settings.lists_enabled:
        raise AtlasError("lists are turned off on this server", "forbidden")


def create_list(session: Session, ctx: Ctx, user: User, name: str, description: str = "", public: bool = False,
                kind: str = "custom") -> UserList:
    _guard_lists(ctx)
    name = name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise AtlasError("a list needs a name of up to 80 characters")
    session.flush()
    count = session.scalar(select(func.count()).select_from(UserList).where(UserList.user_id == user.id)) or 0
    if count >= ctx.settings.max_lists_per_user:
        raise AtlasError(f"you can have at most {ctx.settings.max_lists_per_user} lists", "conflict")
    if session.scalars(select(UserList.id).where(UserList.user_id == user.id, UserList.name == name)).first():
        raise AtlasError("you already have a list with that name", "conflict")
    row = UserList(user_id=user.id, name=name, description=description.strip()[:300], public=public, kind=kind)
    session.add(row)
    session.flush()
    return row


def ensure_default_lists(session: Session, ctx: Ctx, user: User) -> list[UserList]:
    have = {lst.kind: lst for lst in session.scalars(select(UserList).where(UserList.user_id == user.id))}
    out = []
    for kind, name in (("favourites", "Favourites"), ("wishlist", "Wishlist")):
        out.append(have.get(kind) or create_list(session, ctx, user, name, kind=kind))
    return out


def get_list(session: Session, lid: str, viewer: User | None) -> UserList:
    row = session.get(UserList, lid)
    if row is None or not (row.public or (viewer and viewer.id == row.user_id)):
        raise AtlasError("no such list", "not_found")
    return row


def owned_list(session: Session, lid: str, user: User) -> UserList:
    row = session.get(UserList, lid)
    if row is None or row.user_id != user.id:
        raise AtlasError("no such list", "not_found")
    return row


def add_to_list(session: Session, ctx: Ctx, user: User, lid: str, cid: str, note: str = "") -> None:
    _guard_lists(ctx)
    row = owned_list(session, lid, user)
    game = session.get(Canonical, cid)
    if game is None or game.merged_into:
        raise AtlasError("unknown game", "not_found")
    session.flush()
    if session.get(ListItem, (lid, cid)):
        return
    count = session.scalar(select(func.count()).select_from(ListItem).where(ListItem.list_id == lid)) or 0
    if count >= ctx.settings.max_list_items:
        raise AtlasError(f"a list holds at most {ctx.settings.max_list_items} games", "conflict")
    session.add(ListItem(list_id=row.id, canonical_id=cid, note=note.strip()[:200]))


def remove_from_list(session: Session, user: User, lid: str, cid: str) -> None:
    owned_list(session, lid, user)
    session.execute(delete(ListItem).where(ListItem.list_id == lid, ListItem.canonical_id == cid))


def delete_list(session: Session, user: User, lid: str) -> None:
    row = owned_list(session, lid, user)
    session.execute(delete(ListItem).where(ListItem.list_id == lid))
    session.delete(row)


def move_list_items(session: Session, src: str, dst: str) -> None:
    """On merge of two games: lists follow the surviving record, without duplicates."""
    have = {(i.list_id) for i in session.scalars(select(ListItem).where(ListItem.canonical_id == dst))}
    for item in session.scalars(select(ListItem).where(ListItem.canonical_id == src)).all():
        if item.list_id in have:
            session.delete(item)
        else:
            item.canonical_id = dst
    mine = session.scalars(select(Follow).where(Follow.target_type == "game", Follow.target_id == src)).all()
    for f in mine:
        if session.get(Follow, (f.user_id, "game", dst)):
            session.delete(f)
        else:
            f.target_id = dst
    session.flush()


# ---- follows ------------------------------------------------------------------------------------------

def follow(session: Session, ctx: Ctx, user: User, target_type: str, target_id: str) -> None:
    _guard_lists(ctx)
    if target_type == "game":
        game = session.get(Canonical, target_id)
        if game is None or game.merged_into:
            raise AtlasError("unknown game", "not_found")
    elif target_type == "user":
        other = session.get(User, target_id)
        if other is None or other.deleted_at is not None or not other.profile_public:
            raise AtlasError("unknown user", "not_found")
        if other.id == user.id:
            raise AtlasError("you cannot follow yourself", "conflict")
    else:
        raise AtlasError("unknown follow target")
    session.flush()
    if session.get(Follow, (user.id, target_type, target_id)):
        return
    count = session.scalar(select(func.count()).select_from(Follow).where(Follow.user_id == user.id)) or 0
    if count >= ctx.settings.max_follows:
        raise AtlasError(f"you can follow at most {ctx.settings.max_follows} things", "conflict")
    session.add(Follow(user_id=user.id, target_type=target_type, target_id=target_id))


def unfollow(session: Session, user: User, target_type: str, target_id: str) -> None:
    session.execute(delete(Follow).where(Follow.user_id == user.id, Follow.target_type == target_type,
                                         Follow.target_id == target_id))


def followers(session: Session, target_type: str, target_id: str) -> list[str]:
    session.flush()
    return list(session.scalars(select(Follow.user_id).where(Follow.target_type == target_type,
                                                             Follow.target_id == target_id)))


# ---- notifications ------------------------------------------------------------------------------------

def notify(session: Session, ctx: Ctx, user_id: str, kind: str, title: str, url: str = "", key: str = "",
           actor_id: str | None = None) -> None:
    """Create (or fold into a recent unread) notification; optionally email it once."""
    if actor_id == user_id:
        return
    user = session.get(User, user_id)
    if user is None or user.deleted_at is not None or user.status != "active":
        return
    session.flush()
    if key:
        recent = session.scalars(select(Notification).where(
            Notification.user_id == user_id, Notification.kind == kind, Notification.key == key,
            Notification.read_at.is_(None), Notification.created_at > now() - timedelta(hours=1))).first()
        if recent is not None:
            recent.count += 1
            recent.title = title
            return
    session.add(Notification(user_id=user_id, kind=kind, key=key, title=title[:200], url=url[:300]))
    if user.email_notifications and user.email and mail.is_verified(ctx.settings, user):
        sent = session.scalar(select(func.count()).select_from(Notification).where(
            Notification.user_id == user_id, Notification.created_at > now() - timedelta(days=1))) or 0
        if sent <= EMAIL_PER_DAY:
            mail.queue(session, user.email, f"[{ctx.settings.app_name}] {title}"[:200],
                       f"{title}\n\n{ctx.settings.public_url}{url}\n\nYou can turn these emails off in your profile "
                       f"settings: {ctx.settings.public_url}/me/settings\n")


def unread_count(session: Session, user_id: str) -> int:
    return session.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == user_id, Notification.read_at.is_(None))) or 0


def mark_read(session: Session, user_id: str, nid: str | None = None) -> None:
    stmt = update(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    if nid:
        stmt = stmt.where(Notification.id == nid)
    session.execute(stmt.values(read_at=now()))


def on_proposal_applied(session: Session, ctx: Ctx, p: Proposal, actor: User | None) -> None:
    """A change to a game went live: tell the people following the game and the contributor."""
    if not p.canonical_id:
        return
    game = session.get(Canonical, p.canonical_id)
    title = (game.view.get("title") if game and game.view else None) or "a game"
    who = session.get(User, p.proposer_id)
    for uid in followers(session, "game", p.canonical_id):
        notify(session, ctx, uid, "game_changed", f"{title} was updated", f"/game/{p.canonical_id}",
               key=f"g:{p.canonical_id}", actor_id=p.proposer_id)
    if who is not None:
        for uid in followers(session, "user", who.id):
            notify(session, ctx, uid, "followed_user", f"{who.username} contributed to {title}",
                   f"/game/{p.canonical_id}", key=f"u:{who.id}", actor_id=who.id)


def on_proposal_decided(session: Session, ctx: Ctx, p: Proposal, outcome: str, reviewer: User | None) -> None:
    game = session.get(Canonical, p.canonical_id) if p.canonical_id else None
    title = (game.view.get("title") if game and game.view else None) or p.payload.get("title") or "your proposal"
    words = {"approved": "was approved", "rejected": "was rejected", "reverted": "was reverted"}
    notify(session, ctx, p.proposer_id, "proposal", f"Your {p.kind.replace('_', ' ')} for {title} {words[outcome]}",
           f"/game/{p.canonical_id}" if p.canonical_id else "/me", key=f"p:{p.id}",
           actor_id=reviewer.id if reviewer else None)


# ---- comments -----------------------------------------------------------------------------------------

def clean_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    text = "".join(c for c in text if c == "\n" or ord(c) >= 32)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text or len(text) > 2000:
        raise AtlasError("a comment needs 1-2000 characters")
    return text


def _may_comment(session: Session, ctx: Ctx, user: User) -> None:
    s = ctx.settings
    if not s.comments_enabled:
        raise AtlasError("comments are turned off on this server", "forbidden")
    if user.status != "active" or user.deleted_at is not None:
        raise AtlasError("account suspended", "forbidden")
    if not mail.is_verified(s, user):
        raise AtlasError("confirm your email address before commenting (see your profile)", "forbidden")
    if user.reputation < s.comment_min_reputation and not accounts.can_review(s, user):
        raise AtlasError(f"you need {s.comment_min_reputation} reputation to comment here", "forbidden")
    session.flush()
    today = session.scalar(select(func.count()).select_from(Comment).where(
        Comment.user_id == user.id, Comment.created_at > now() - timedelta(days=1))) or 0
    if today >= s.comment_daily_limit and not accounts.can_review(s, user):
        raise AtlasError("daily comment limit reached", "conflict")


def _depth(session: Session, parent: Comment) -> int:
    depth, cur = 1, parent
    while cur.parent_id and depth < 10:
        cur = session.get(Comment, cur.parent_id) or cur
        depth += 1
        if cur.parent_id is None:
            break
    return depth


def can_view_proposal_thread(ctx: Ctx, user: User | None, p: Proposal) -> bool:
    return bool(user and (user.id == p.proposer_id or accounts.can_review(ctx.settings, user)))


def add_comment(session: Session, ctx: Ctx, user: User, body: str, *, canonical_id: str | None = None,
                proposal_id: str | None = None, parent_id: str | None = None) -> Comment:
    write_lock(session)
    _may_comment(session, ctx, user)
    body = clean_body(body)
    if bool(canonical_id) == bool(proposal_id):
        raise AtlasError("a comment belongs to a game or a proposal")
    parent = None
    if parent_id:
        parent = session.get(Comment, parent_id)
        if parent is None or parent.status == "deleted" or parent.canonical_id != canonical_id \
                or parent.proposal_id != proposal_id:
            raise AtlasError("unknown comment to reply to", "not_found")
        if _depth(session, parent) >= MAX_DEPTH:
            parent = session.get(Comment, parent.parent_id) if parent.parent_id else parent  # flatten deep threads
    prop = None
    if canonical_id:
        game = session.get(Canonical, canonical_id)
        if game is None or game.merged_into:
            raise AtlasError("unknown game", "not_found")
    else:
        prop = session.get(Proposal, proposal_id)
        if prop is None or not can_view_proposal_thread(ctx, user, prop):
            raise AtlasError("unknown proposal", "not_found")
    row = Comment(canonical_id=canonical_id, proposal_id=proposal_id, parent_id=parent.id if parent else None,
                  user_id=user.id, body=body)
    session.add(row)
    session.flush()
    _comment_notifications(session, ctx, user, row, parent, prop)
    return row


def _comment_notifications(session: Session, ctx: Ctx, user: User, row: Comment, parent: Comment | None,
                           prop: Proposal | None) -> None:
    told: set[str] = {user.id}
    url = f"/game/{row.canonical_id}#c-{row.id}" if row.canonical_id else f"/proposal/{row.proposal_id}#c-{row.id}"
    if parent is not None and parent.user_id not in told:
        told.add(parent.user_id)
        notify(session, ctx, parent.user_id, "reply", f"{user.username} replied to your comment", url,
               key=f"r:{parent.id}", actor_id=user.id)
    for name in list(dict.fromkeys(MENTION.findall(row.body.lower())))[:5]:
        target = session.scalars(select(User).where(User.username == name)).first()
        if target is None or target.id in told:
            continue
        if prop is not None and not can_view_proposal_thread(ctx, target, prop):
            continue
        told.add(target.id)
        notify(session, ctx, target.id, "mention", f"{user.username} mentioned you in a comment", url,
               key=f"m:{row.id}", actor_id=user.id)
    if row.canonical_id:
        for uid in followers(session, "game", row.canonical_id):
            if uid not in told:
                told.add(uid)
                notify(session, ctx, uid, "comment", "New comments on a game you follow", url,
                       key=f"c:{row.canonical_id}", actor_id=user.id)
    elif prop is not None:
        for uid in {prop.proposer_id}:
            if uid not in told:
                notify(session, ctx, uid, "comment", "New comment on your proposal", url, key=f"pc:{prop.id}",
                       actor_id=user.id)


def edit_comment(session: Session, user: User, cid: str, body: str) -> Comment:
    row = session.get(Comment, cid)
    if row is None or row.user_id != user.id or row.status != "visible":
        raise AtlasError("no such comment", "not_found")
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=now().tzinfo)
    if now() - created > EDIT_WINDOW:
        raise AtlasError("comments can only be edited for 15 minutes", "conflict")
    row.body, row.edited_at = clean_body(body), now()
    return row


def delete_comment(session: Session, user: User, cid: str) -> None:
    row = session.get(Comment, cid)
    if row is None or row.user_id != user.id:
        raise AtlasError("no such comment", "not_found")
    row.status, row.body = "deleted", ""


def moderate(session: Session, ctx: Ctx, moderator: User, cid: str, action: str) -> Comment:
    if not accounts.can_review(ctx.settings, moderator):
        raise AtlasError("review rights required", "forbidden")
    row = session.get(Comment, cid)
    if row is None or row.status == "deleted":
        raise AtlasError("no such comment", "not_found")
    if action == "hide":
        row.status = "hidden"
        from .proposals import adjust_reputation

        author = session.get(User, row.user_id)
        if author is not None and author.id != moderator.id:
            adjust_reputation(session, author, -3, "comment_hidden")
    elif action == "restore":
        row.status = "visible"
    else:
        raise AtlasError("unknown action")
    session.execute(update(Report).where(Report.comment_id == cid, Report.status == "open").values(
        status="actioned" if action == "hide" else "dismissed"))
    accounts.audit(session, moderator.id, "comment_" + action, cid)
    return row


def report_comment(session: Session, ctx: Ctx, user: User, cid: str, reason: str) -> None:
    write_lock(session)
    if user.status != "active" or not mail.is_verified(ctx.settings, user):
        raise AtlasError("confirm your email address before reporting", "forbidden")
    row = session.get(Comment, cid)
    if row is None or row.status != "visible" or row.user_id == user.id:
        raise AtlasError("no such comment", "not_found")
    session.flush()
    if session.scalars(select(Report.id).where(Report.comment_id == cid, Report.reporter_id == user.id)).first():
        return
    session.add(Report(comment_id=cid, reporter_id=user.id, reason=(reason or "").strip()[:300]))
    session.flush()
    open_reports = session.scalar(select(func.count()).select_from(Report).where(
        Report.comment_id == cid, Report.status == "open")) or 0
    if open_reports >= ctx.settings.comment_autohide_reports:
        row.status = "hidden"  # held until a moderator decides; reports stay open


def thread(session: Session, *, canonical_id: str | None = None, proposal_id: str | None = None,
           viewer: User | None, ctx: Ctx) -> list[dict[str, Any]]:
    """Comments in display order with depth; hidden ones only expose their text to moderators."""
    stmt = select(Comment).where(Comment.canonical_id == canonical_id if canonical_id
                                 else Comment.proposal_id == proposal_id).order_by(Comment.created_at)
    rows = list(session.scalars(stmt))
    by_parent: dict[str | None, list[Comment]] = {}
    for c in rows:
        by_parent.setdefault(c.parent_id, []).append(c)
    users = {u.id: u for u in session.scalars(select(User).where(User.id.in_({c.user_id for c in rows})))} if rows else {}
    mod = bool(viewer and accounts.can_review(ctx.settings, viewer))
    out: list[dict[str, Any]] = []

    def walk(parent: str | None, depth: int) -> None:
        for c in by_parent.get(parent, []):
            author = users.get(c.user_id)
            show = c.status == "visible" or (c.status == "hidden" and mod)
            out.append({"c": c, "depth": min(depth, MAX_DEPTH), "author": author.username if author else "?",
                        "deleted_author": bool(author and author.deleted_at), "show": show,
                        "mine": bool(viewer and viewer.id == c.user_id),
                        "editable": bool(viewer and viewer.id == c.user_id and c.status == "visible"
                                         and now() - (c.created_at if c.created_at.tzinfo
                                                      else c.created_at.replace(tzinfo=now().tzinfo)) <= EDIT_WINDOW)})
            walk(c.id, depth + 1)

    walk(None, 0)
    return out


# ---- profiles, export, deletion -----------------------------------------------------------------------

def badges(accepted: int, user: User) -> list[str]:
    out = [label for _k, label, n in BADGES if accepted >= n]
    if user.role in ("curator", "admin"):
        out.append("Reviewer")
    return out


def profile_stats(session: Session, user: User) -> dict[str, Any]:
    accepted = session.scalar(select(func.count()).select_from(Proposal).where(
        Proposal.proposer_id == user.id, Proposal.status == "approved")) or 0
    total = session.scalar(select(func.count()).select_from(Proposal).where(Proposal.proposer_id == user.id)) or 0
    return {"accepted": accepted, "total": total, "badges": badges(accepted, user)}


def export_data(session: Session, user: User) -> dict[str, Any]:
    def iso(dt):
        return dt.isoformat() if dt else None

    lists = []
    for lst in session.scalars(select(UserList).where(UserList.user_id == user.id)):
        items = [{"game": i.canonical_id, "note": i.note, "added": iso(i.added_at)}
                 for i in session.scalars(select(ListItem).where(ListItem.list_id == lst.id))]
        lists.append({"name": lst.name, "kind": lst.kind, "public": lst.public, "description": lst.description,
                      "items": items})
    return {
        "account": {"username": user.username, "email": user.email, "role": user.role, "reputation": user.reputation,
                    "created": iso(user.created_at), "bio": user.bio, "profile_public": user.profile_public,
                    "email_verified": iso(user.email_verified_at)},
        "lists": lists,
        "follows": [{"type": f.target_type, "target": f.target_id, "since": iso(f.created_at)}
                    for f in session.scalars(select(Follow).where(Follow.user_id == user.id))],
        "proposals": [{"kind": p.kind, "game": p.canonical_id, "status": p.status, "payload": p.payload,
                       "note": p.note, "created": iso(p.created_at)}
                      for p in session.scalars(select(Proposal).where(Proposal.proposer_id == user.id))],
        "comments": [{"game": c.canonical_id, "proposal": c.proposal_id, "body": c.body if c.status != "deleted" else "",
                      "status": c.status, "created": iso(c.created_at)}
                     for c in session.scalars(select(Comment).where(Comment.user_id == user.id))],
        "notice": "Accepted contributions are released under CC0 and stay on the site after account deletion.",
    }


def delete_account(session: Session, user: User) -> None:
    """Remove personal data. Contributions stay (CC0) but are detached from the person."""
    if user.role == "admin":
        others = session.scalar(select(func.count()).select_from(User).where(
            User.role == "admin", User.id != user.id, User.deleted_at.is_(None), User.status == "active")) or 0
        if not others:
            raise AtlasError("you are the only administrator; promote someone else first", "conflict")
    accounts.end_all_sessions(session, user.id)
    session.execute(delete(Token).where(Token.user_id == user.id))
    session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    for lst in session.scalars(select(UserList).where(UserList.user_id == user.id)).all():
        session.execute(delete(ListItem).where(ListItem.list_id == lst.id))
        session.delete(lst)
    session.execute(delete(Follow).where((Follow.user_id == user.id) | ((Follow.target_type == "user")
                                                                         & (Follow.target_id == user.id))))
    session.execute(delete(Notification).where(Notification.user_id == user.id))
    session.execute(update(Comment).where(Comment.user_id == user.id).values(body="", status="deleted"))
    from .models import EmailToken

    session.execute(delete(EmailToken).where(EmailToken.user_id == user.id))
    user.username = "deleted-" + secrets.token_hex(4)
    user.email, user.email_verified_at, user.bio = None, None, ""
    user.password_hash, user.status, user.role = "!", "suspended", "user"
    user.profile_public, user.email_notifications, user.deleted_at = False, False, now()
    accounts.audit(session, None, "account_deleted", user.id)
