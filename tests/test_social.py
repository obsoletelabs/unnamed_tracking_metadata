import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from atlas import accounts, mail, proposals, service, social
from atlas.models import Comment, Follow, ListItem, Notification, Outbox, Proposal, User, now
from atlas.service import AtlasError, Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings
from .test_community import PW, user
from .test_web import fresh, login, signup, site, token  # noqa: F401


@pytest.fixture
def game(session, ctx):
    [r] = service.ingest(session, ctx, batch("igdb", [{"external_id": "1", "title": "Hades",
                                                       "release_date": "2020-09-17"}]), system=True)
    session.commit()
    return r.canonical_id


def users(session, ctx):
    return (user(session, ctx.settings, "alice"), user(session, ctx.settings, "bob"), user(session, ctx.settings, "carol"))


def inbox(session, u):
    session.flush()
    return list(session.scalars(select(Notification).where(Notification.user_id == u.id).order_by(Notification.created_at)))


# ---- lists --------------------------------------------------------------------------------------------

def test_lists_basics_and_limits(session, ctx, game):
    alice, bob, _ = users(session, ctx)
    fav, wish = social.ensure_default_lists(session, ctx, bob)
    assert (fav.kind, wish.kind) == ("favourites", "wishlist")
    assert [x.id for x in social.ensure_default_lists(session, ctx, bob)] == [fav.id, wish.id]  # idempotent
    lst = social.create_list(session, ctx, bob, "Backlog", "to play", public=True)
    with pytest.raises(AtlasError, match="already have"):
        social.create_list(session, ctx, bob, "Backlog")
    social.add_to_list(session, ctx, bob, lst.id, game, "soon")
    social.add_to_list(session, ctx, bob, lst.id, game)  # idempotent
    assert len(session.scalars(select(ListItem)).all()) == 1
    with pytest.raises(AtlasError, match="no such list"):
        social.add_to_list(session, ctx, alice, lst.id, game)  # someone else's list
    with pytest.raises(AtlasError, match="unknown game"):
        social.add_to_list(session, ctx, bob, lst.id, "atlas:nope")
    assert social.get_list(session, lst.id, None).name == "Backlog"  # public
    private = social.create_list(session, ctx, bob, "Secret")
    with pytest.raises(AtlasError):
        social.get_list(session, private.id, alice)
    assert social.get_list(session, private.id, bob)
    social.remove_from_list(session, bob, lst.id, game)
    social.delete_list(session, bob, lst.id)
    small = Ctx(build_settings(max_lists_per_user=2, max_list_items=1), load_identity(session))
    carol = user(session, small.settings, "carol2")
    social.create_list(session, small, carol, "a")
    social.create_list(session, small, carol, "b")
    with pytest.raises(AtlasError, match="at most 2"):
        social.create_list(session, small, carol, "c")


def test_lists_and_follows_follow_a_merge(session, ctx):
    _, bob, carol = users(session, ctx)
    [a] = service.ingest(session, ctx, batch("steam", [{"external_id": "1", "title": "Mystery"}]), system=True)
    [b] = service.ingest(session, ctx, batch("gog", [{"external_id": "2", "title": "Mystery"}]), system=True)
    lst = social.create_list(session, ctx, bob, "L")
    social.add_to_list(session, ctx, bob, lst.id, b.canonical_id)
    social.follow(session, ctx, carol, "game", b.canonical_id)
    social.follow(session, ctx, bob, "game", a.canonical_id)
    social.follow(session, ctx, bob, "game", b.canonical_id)  # bob follows both: must not collide
    service.merge_canonicals(session, ctx, b.canonical_id, a.canonical_id)
    session.flush()
    assert session.scalars(select(ListItem)).one().canonical_id == a.canonical_id
    followers = {f.user_id for f in session.scalars(select(Follow).where(Follow.target_id == a.canonical_id))}
    assert followers == {bob.id, carol.id}


# ---- follows ------------------------------------------------------------------------------------------

def test_follow_rules(session, ctx, game):
    alice, bob, carol = users(session, ctx)
    social.follow(session, ctx, bob, "game", game)
    social.follow(session, ctx, bob, "game", game)  # idempotent
    social.follow(session, ctx, bob, "user", alice.id)
    with pytest.raises(AtlasError, match="yourself"):
        social.follow(session, ctx, bob, "user", bob.id)
    with pytest.raises(AtlasError):
        social.follow(session, ctx, bob, "game", "atlas:nope")
    with pytest.raises(AtlasError):
        social.follow(session, ctx, bob, "thing", "x")
    alice.profile_public = False
    with pytest.raises(AtlasError, match="unknown user"):
        social.follow(session, ctx, carol, "user", alice.id)  # private profiles cannot be followed
    social.unfollow(session, bob, "game", game)
    assert social.followers(session, "game", game) == []
    tiny = Ctx(build_settings(max_follows=1), load_identity(session))
    dave = user(session, tiny.settings, "dave")
    social.follow(session, tiny, dave, "user", bob.id)
    with pytest.raises(AtlasError, match="at most 1"):
        social.follow(session, tiny, dave, "user", carol.id)


# ---- notifications ------------------------------------------------------------------------------------

def test_decisions_and_changes_notify_the_right_people(session, ctx, game):
    admin, bob, carol = users(session, ctx)
    social.follow(session, ctx, carol, "game", game)
    social.follow(session, ctx, carol, "user", bob.id)
    p = proposals.create(session, ctx, bob, "edit", game, {"developer": "Supergiant"})
    assert inbox(session, bob) == [] and inbox(session, carol) == []  # pending: nothing yet
    proposals.review(session, ctx, admin, p.id, approve=True)
    mine = inbox(session, bob)
    assert [n.kind for n in mine] == ["proposal"] and "was approved" in mine[0].title
    kinds = sorted(n.kind for n in inbox(session, carol))
    assert kinds == ["followed_user", "game_changed"]
    assert inbox(session, admin) == []  # the actor is never told about their own action
    q = proposals.create(session, ctx, bob, "edit", game, {"series": "S"})
    proposals.review(session, ctx, admin, q.id, approve=False, note="no source")
    assert "was rejected" in inbox(session, bob)[-1].title


def test_repeated_changes_fold_into_one_unread_notification(session, ctx, game):
    admin, bob, carol = users(session, ctx)
    social.follow(session, ctx, carol, "game", game)
    for i in range(3):
        p = proposals.create(session, ctx, bob, "edit", game, {"series": f"S{i}"})
        proposals.review(session, ctx, admin, p.id, approve=True)
    rows = [n for n in inbox(session, carol) if n.kind == "game_changed"]
    assert len(rows) == 1 and rows[0].count == 3
    social.mark_read(session, carol.id, rows[0].id)
    assert social.unread_count(session, carol.id) == 0
    p = proposals.create(session, ctx, bob, "edit", game, {"series": "again"})
    proposals.review(session, ctx, admin, p.id, approve=True)
    assert social.unread_count(session, carol.id) == 1  # a read one is not reused


def test_email_notifications_are_opt_in_and_need_a_confirmed_address(session, ctx, game):
    admin, bob, carol = users(session, ctx)
    social.follow(session, ctx, carol, "game", game)
    p = proposals.create(session, ctx, bob, "edit", game, {"series": "a"})
    proposals.review(session, ctx, admin, p.id, approve=True)
    assert session.scalars(select(Outbox)).first() is None  # opt-in: nothing sent
    social.mark_read(session, carol.id)  # an unread one would absorb the next change
    carol.email_notifications, carol.email = True, "carol@example.org"
    p = proposals.create(session, ctx, bob, "edit", game, {"series": "b"})
    proposals.review(session, ctx, admin, p.id, approve=True)
    mails = [m for m in session.scalars(select(Outbox)) if m.to_address == "carol@example.org"]
    strict = ctx.settings.model_copy(update={"email_verification": "required"})
    assert len(mails) == 1 and "/me/settings" in mails[0].body  # optional policy: unverified is fine
    carol.email_notifications = True
    n_before = len(session.scalars(select(Outbox)).all())
    social.notify(session, Ctx(strict, ctx.identity), carol.id, "x", "Hello", "/", key="")
    assert len(session.scalars(select(Outbox)).all()) == n_before  # required + unverified: no email
    carol.email_verified_at = now()
    social.notify(session, Ctx(strict, ctx.identity), carol.id, "x", "Hello again", "/", key="")
    assert len(session.scalars(select(Outbox)).all()) == n_before + 1


# ---- comments -----------------------------------------------------------------------------------------

def test_comment_gates(session, game):
    s = build_settings(email_verification="required", comment_min_reputation=5, comment_daily_limit=2)
    c = Ctx(s, load_identity(session))
    admin = user(session, s, "alice")
    bob = user(session, s, "bob")
    with pytest.raises(AtlasError, match="confirm your email"):
        social.add_comment(session, c, bob, "hi", canonical_id=game)
    bob.email_verified_at = now()
    with pytest.raises(AtlasError, match="reputation"):
        social.add_comment(session, c, bob, "hi", canonical_id=game)
    bob.reputation = 5
    social.add_comment(session, c, bob, "one", canonical_id=game)
    social.add_comment(session, c, bob, "two", canonical_id=game)
    with pytest.raises(AtlasError, match="daily"):
        social.add_comment(session, c, bob, "three", canonical_id=game)
    social.add_comment(session, c, admin, "reviewers are not capped", canonical_id=game)
    off = Ctx(build_settings(comments_enabled=False), load_identity(session))
    with pytest.raises(AtlasError, match="turned off"):
        social.add_comment(session, off, admin, "x", canonical_id=game)


@pytest.mark.parametrize("body", ["", "   ", "x" * 2001])
def test_comment_body_limits(session, ctx, game, body):
    _, bob, _ = users(session, ctx)
    with pytest.raises(AtlasError):
        social.add_comment(session, ctx, bob, body, canonical_id=game)


def test_comment_text_is_cleaned(session, ctx, game):
    _, bob, _ = users(session, ctx)
    row = social.add_comment(session, ctx, bob, "a\x00b\x07c\r\n\r\n\r\n\r\nd", canonical_id=game)
    assert row.body == "abc\n\nd"


def test_threads_replies_edits_and_deletes(session, ctx, game):
    _, bob, carol = users(session, ctx)
    top = social.add_comment(session, ctx, bob, "top", canonical_id=game)
    r1 = social.add_comment(session, ctx, carol, "reply", canonical_id=game, parent_id=top.id)
    r2 = social.add_comment(session, ctx, bob, "deeper", canonical_id=game, parent_id=r1.id)
    r3 = social.add_comment(session, ctx, carol, "deepest", canonical_id=game, parent_id=r2.id)
    r4 = social.add_comment(session, ctx, bob, "too deep", canonical_id=game, parent_id=r3.id)
    t = social.thread(session, canonical_id=game, viewer=bob, ctx=ctx)
    assert [x["c"].body for x in t] == ["top", "reply", "deeper", "deepest", "too deep"]
    assert max(x["depth"] for x in t) <= social.MAX_DEPTH
    assert r4.parent_id == r1.id  # replies beyond the depth limit attach to the last visible level
    with pytest.raises(AtlasError, match="unknown comment"):
        social.add_comment(session, ctx, bob, "x", canonical_id="atlas:other", parent_id=top.id)
    social.edit_comment(session, bob, top.id, "top (edited)")
    with pytest.raises(AtlasError):
        social.edit_comment(session, carol, top.id, "hijack")
    top.created_at = now() - timedelta(minutes=16)
    with pytest.raises(AtlasError, match="15 minutes"):
        social.edit_comment(session, bob, top.id, "too late")
    social.delete_comment(session, bob, top.id)
    t = social.thread(session, canonical_id=game, viewer=None, ctx=ctx)
    assert t[0]["c"].status == "deleted" and t[0]["c"].body == ""
    with pytest.raises(AtlasError):
        social.delete_comment(session, carol, r2.id)


def test_reply_and_mention_notifications(session, ctx, game):
    admin, bob, carol = users(session, ctx)
    top = social.add_comment(session, ctx, bob, "hello", canonical_id=game)
    social.add_comment(session, ctx, carol, "hi @bob and @alice and @nobody and @carol", canonical_id=game,
                       parent_id=top.id)
    assert [n.kind for n in inbox(session, bob)] == ["reply"]  # replied to: one notice, not two
    assert [n.kind for n in inbox(session, admin)] == ["mention"]
    assert inbox(session, carol) == []  # no self-mention notice


def test_proposal_threads_are_private_to_proposer_and_reviewers(session, ctx, game):
    admin, bob, carol = users(session, ctx)
    p = proposals.create(session, ctx, bob, "edit", game, {"series": "x"})
    social.add_comment(session, ctx, bob, "why was this slow?", proposal_id=p.id)
    social.add_comment(session, ctx, admin, "reviewing now, @carol", proposal_id=p.id)
    with pytest.raises(AtlasError, match="unknown proposal"):
        social.add_comment(session, ctx, carol, "can I see this?", proposal_id=p.id)
    assert inbox(session, carol) == []  # mentions never leak a private thread
    assert any(n.kind == "comment" for n in inbox(session, bob))


def test_reports_autohide_and_moderation(session, ctx, game):
    admin, bob, carol = users(session, ctx)
    dave, erin = user(session, ctx.settings, "dave"), user(session, ctx.settings, "erin")
    row = social.add_comment(session, ctx, bob, "spam spam", canonical_id=game)
    with pytest.raises(AtlasError):
        social.report_comment(session, ctx, bob, row.id, "my own")
    social.report_comment(session, ctx, carol, row.id, "spam")
    social.report_comment(session, ctx, carol, row.id, "again")  # same reporter: ignored
    assert row.status == "visible"
    social.report_comment(session, ctx, dave, row.id, "")
    social.report_comment(session, ctx, erin, row.id, "")
    assert row.status == "hidden"  # three distinct reporters
    t = social.thread(session, canonical_id=game, viewer=carol, ctx=ctx)
    assert t[0]["show"] is False  # ordinary readers see a placeholder
    assert social.thread(session, canonical_id=game, viewer=admin, ctx=ctx)[0]["show"] is True
    before = bob.reputation
    with pytest.raises(AtlasError, match="review rights"):
        social.moderate(session, ctx, carol, row.id, "restore")
    social.moderate(session, ctx, admin, row.id, "hide")
    assert bob.reputation == before - 3
    social.moderate(session, ctx, admin, row.id, "restore")
    assert row.status == "visible"


# ---- profile, export, deletion ------------------------------------------------------------------------

def test_profile_stats_and_badges(session, ctx, game):
    admin, bob, _ = users(session, ctx)
    p = proposals.create(session, ctx, bob, "edit", game, {"series": "x"})
    proposals.review(session, ctx, admin, p.id, approve=True)
    stats = social.profile_stats(session, bob)
    assert stats["accepted"] == 1 and "First accepted edit" in stats["badges"]
    assert "Reviewer" in social.profile_stats(session, admin)["badges"]


def test_export_contains_the_users_data(session, ctx, game):
    _, bob, _ = users(session, ctx)
    lst = social.create_list(session, ctx, bob, "Mine")
    social.add_to_list(session, ctx, bob, lst.id, game)
    social.follow(session, ctx, bob, "game", game)
    social.add_comment(session, ctx, bob, "my words", canonical_id=game)
    proposals.create(session, ctx, bob, "edit", game, {"series": "x"})
    data = json.loads(json.dumps(social.export_data(session, bob), default=str))
    assert data["account"]["username"] == "bob" and data["lists"][0]["items"][0]["game"] == game
    assert data["follows"][0]["target"] == game and data["comments"][0]["body"] == "my words"
    assert data["proposals"][0]["kind"] == "edit" and "password" not in json.dumps(data)


def test_account_deletion_removes_personal_data_but_keeps_contributions(session, ctx, game):
    admin, bob, carol = users(session, ctx)
    p = proposals.create(session, ctx, bob, "edit", game, {"series": "x"})
    proposals.review(session, ctx, admin, p.id, approve=True)
    lst = social.create_list(session, ctx, bob, "Mine")
    social.add_to_list(session, ctx, bob, lst.id, game)
    social.follow(session, ctx, carol, "user", bob.id)
    social.add_comment(session, ctx, bob, "bye", canonical_id=game)
    bob.email, bob.bio = "bob@example.org", "about me"
    accounts.create_session(session, ctx.settings, bob)
    social.delete_account(session, bob)
    session.flush()
    assert bob.username.startswith("deleted-") and bob.email is None and bob.bio == "" and bob.deleted_at
    assert not accounts.verify_password(PW, bob.password_hash) and accounts.authenticate(session, "bob", PW) is None
    assert session.scalars(select(ListItem)).first() is None and social.followers(session, "user", bob.id) == []
    assert session.scalars(select(Comment)).one().body == ""
    assert session.get(Proposal, p.id).status == "approved"  # the (CC0) contribution stays
    with pytest.raises(AtlasError, match="only administrator"):
        social.delete_account(session, admin)


# ---- website ------------------------------------------------------------------------------------------

def make_game(site):  # noqa: F811
    with site.st.maker() as s:
        [r] = service.ingest(s, site.st.ctx, batch("igdb", [{"external_id": "1", "title": "Hades",
                             "release_date": "2020-09-17"}]), system=True)
        s.commit()
    return r.canonical_id


def csrf_for(client, path="/me"):
    return token(client, path)


def test_profiles_public_private_and_following(site):  # noqa: F811
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    page = fresh(site).get("/u/bob")
    assert page.status_code == 200 and "bob" in page.text and "reputation" in page.text
    t = csrf_for(bob, "/me/settings")
    bob.post("/me/settings", data={"csrf": t, "bio": "I like <b>bold</b> games"})  # profile_public unchecked => private
    assert fresh(site).get("/u/bob").status_code == 404
    assert bob.get("/u/bob").status_code == 200 and site.get("/u/bob").status_code == 200  # self and reviewers
    t = csrf_for(bob, "/me/settings")
    bob.post("/me/settings", data={"csrf": t, "bio": "I like <b>bold</b> games", "profile_public": "on"})
    shown = fresh(site).get("/u/bob").text
    assert "&lt;b&gt;bold&lt;/b&gt;" in shown and "<b>bold</b>" not in shown
    ta = csrf_for(site, "/u/bob")
    with site.st.maker() as s:
        bob_id = s.scalars(select(User).where(User.username == "bob")).one().id
    assert site.post("/follow", data={"csrf": ta, "type": "user", "target": bob_id}).status_code == 303
    assert "Following" in site.get("/u/bob").text
    assert site.post("/unfollow", data={"csrf": ta, "type": "user", "target": bob_id}).status_code == 303
    assert "Following" not in site.get("/u/bob").text.replace("Following</a>", "")


def test_lists_flow_on_the_site(site):  # noqa: F811
    gid = make_game(site)
    signup(site, "alice")
    t = csrf_for(site, "/me/lists")
    r = site.post("/me/lists", data={"csrf": t, "name": "Backlog", "public": "on"})
    lid = r.headers["location"].split("?")[0].split("/list/")[1]
    assert site.post(f"/list/{lid}/add", data={"csrf": t, "game": gid, "note": "soon"}).status_code == 303
    page = site.get(f"/list/{lid}").text
    assert "Hades" in page and "soon" in page
    assert "Hades" in fresh(site).get(f"/list/{lid}").text  # public
    assert "Remove from Backlog" in site.get(f"/game/{gid}").text
    assert site.post("/me/lists", data={"csrf": t, "name": "Backlog"}).status_code == 409
    site.post(f"/list/{lid}/settings", data={"csrf": t, "name": "Backlog", "description": "d"})  # now private
    assert fresh(site).get(f"/list/{lid}").status_code == 404
    other = fresh(site)
    signup(other, "bob")
    assert other.get(f"/list/{lid}").status_code == 404
    t2 = csrf_for(other, "/me")
    assert other.post(f"/list/{lid}/delete", data={"csrf": t2}).status_code == 404
    assert site.post(f"/list/{lid}/delete", data={"csrf": t}).status_code == 303


def test_follow_a_game_and_read_the_inbox(site):  # noqa: F811
    gid = make_game(site)
    signup(site, "alice")  # admin
    bob = fresh(site)
    signup(bob, "bob")
    tb = csrf_for(bob, f"/game/{gid}")
    bob.post("/follow", data={"csrf": tb, "type": "game", "target": gid, "to": f"/game/{gid}"})
    assert "Following" in bob.get(f"/game/{gid}").text
    t = csrf_for(site, f"/game/{gid}/edit")
    site.post(f"/game/{gid}/edit", data={"csrf": t, "developer": "Supergiant Games"})  # admin edit applies at once
    inbox_page = bob.get("/notifications").text
    assert "Hades was updated" in inbox_page and "Inbox <span class=\"badge warn\">1</span>" in bob.get("/").text
    nid = re.search(r"/go/([0-9A-Z]{26})", inbox_page).group(1)
    assert site.get(f"/go/{nid}").status_code == 404  # someone else's notification
    r = bob.get(f"/go/{nid}")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/game/{gid}")
    assert "badge warn" not in bob.get("/notifications").text.split("<main>")[1].split("</h1>")[0]


def test_comment_flow_with_moderation(site):  # noqa: F811
    gid = make_game(site)
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    carol = fresh(site)
    signup(carol, "carol")
    tb = csrf_for(bob, f"/game/{gid}")
    r = bob.post(f"/game/{gid}/comments", data={"csrf": tb, "body": "Great <script>alert(1)</script> game"})
    assert r.status_code == 303
    page = fresh(site).get(f"/game/{gid}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page
    assert "Sign in</a> to join the discussion" in page
    cid = re.search(r'id="c-([0-9A-Z]{26})"', page).group(1)
    tc = csrf_for(carol, f"/game/{gid}")
    assert carol.post(f"/game/{gid}/comments", data={"csrf": tc, "body": "I agree", "parent": cid}).status_code == 303
    assert "I agree" in site.get(f"/game/{gid}").text
    assert carol.post(f"/comments/{cid}/report", data={"csrf": tc, "to": f"/game/{gid}"}).status_code == 303
    assert "Reported comments" in site.get("/review/reports").text and "Great" in site.get("/review/reports").text
    assert carol.get("/review/reports").status_code == 403
    ta = csrf_for(site, "/review/reports")
    assert carol.post(f"/comments/{cid}/moderate", data={"csrf": tc, "action": "hide"}).status_code == 403
    hide = {"csrf": ta, "action": "hide", "to": f"/game/{gid}"}
    assert site.post(f"/comments/{cid}/moderate", data=hide).status_code == 303
    assert "hidden by a moderator" in fresh(site).get(f"/game/{gid}").text
    assert "Great" not in fresh(site).get(f"/game/{gid}").text
    # the author cannot delete it while it waits for a moderator
    assert bob.post(f"/comments/{cid}/delete", data={"csrf": tb, "to": f"/game/{gid}"}).status_code == 409
    restore = {"csrf": ta, "action": "restore", "to": f"/game/{gid}"}
    assert site.post(f"/comments/{cid}/moderate", data=restore).status_code == 303
    assert bob.post(f"/comments/{cid}/delete", data={"csrf": tb, "to": f"/game/{gid}"}).status_code == 303
    assert carol.post(f"/comments/{cid}/edit", data={"csrf": tc, "body": "hijack"}).status_code == 404


def test_comment_rate_limit_and_csrf(site):  # noqa: F811
    gid = make_game(site)
    signup(site, "alice")
    t = csrf_for(site, f"/game/{gid}")
    codes = [site.post(f"/game/{gid}/comments", data={"csrf": t, "body": f"c{i}"}).status_code for i in range(9)]
    assert codes.count(303) == 6 and 429 in codes
    assert site.post(f"/game/{gid}/comments", data={"body": "no token"}).status_code in (403, 422)
    for path in ("/follow", "/unfollow", "/me/lists", "/me/settings", "/notifications/read", "/me/delete"):
        assert site.post(path, data={"type": "game", "target": gid}).status_code in (403, 422), path


def test_proposal_page_is_private_and_threaded(site):  # noqa: F811
    gid = make_game(site)
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    carol = fresh(site)
    signup(carol, "carol")
    t = csrf_for(bob, f"/game/{gid}/edit")
    bob.post(f"/game/{gid}/edit", data={"csrf": t, "developer": "Dev"})
    with site.st.maker() as s:
        pid = s.scalars(select(Proposal)).one().id
    assert carol.get(f"/proposal/{pid}").status_code == 404
    assert bob.get(f"/proposal/{pid}").status_code == 200 and site.get(f"/proposal/{pid}").status_code == 200
    ts = csrf_for(site, f"/proposal/{pid}")
    assert site.post(f"/proposal/{pid}/comments", data={"csrf": ts, "body": "please add a source"}).status_code == 303
    assert "please add a source" in bob.get(f"/proposal/{pid}").text
    tc = csrf_for(carol, "/me")
    assert carol.post(f"/proposal/{pid}/comments", data={"csrf": tc, "body": "hi"}).status_code == 404


def test_export_and_account_deletion_on_the_site(site):  # noqa: F811
    gid = make_game(site)
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    assert fresh(site).get("/me/export.json").status_code == 303
    t = csrf_for(bob, "/me/settings")
    bob.post("/me/lists", data={"csrf": t, "name": "Mine"})
    bob.post(f"/game/{gid}/comments", data={"csrf": t, "body": "hello"})
    export = bob.get("/me/export.json")
    assert export.status_code == 200 and export.headers["cache-control"] == "no-store"
    data = export.json()
    assert data["account"]["username"] == "bob" and data["comments"][0]["body"] == "hello"
    bad = bob.post("/me/delete", data={"csrf": t, "confirm": "bob", "password": "wrong wrong wrong"})
    assert bad.status_code == 400
    assert bob.post("/me/delete", data={"csrf": t, "confirm": "someone", "password": PW}).status_code == 400
    ok = bob.post("/me/delete", data={"csrf": t, "confirm": "bob", "password": PW})
    assert ok.status_code == 303
    assert bob.get("/me").status_code == 303
    assert login(fresh(site), "bob").status_code == 400
    assert fresh(site).get("/u/bob").status_code == 404
    assert "hello" not in fresh(site).get(f"/game/{gid}").text
    t_admin = csrf_for(site, "/me/settings")
    last = site.post("/me/delete", data={"csrf": t_admin, "confirm": "alice", "password": PW})
    assert last.status_code == 409 and "only administrator" in last.text


def test_social_features_can_be_turned_off(tmp_path):
    from fastapi.testclient import TestClient

    from atlas import main

    s = build_settings(database_url=f"sqlite:///{tmp_path}/off.db", comments_enabled=False, lists_enabled=False,
                       media_cache_enabled=False)
    app = main.create_app(s)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        with app.state.atlas.maker() as sess:
            [r] = service.ingest(sess, app.state.atlas.ctx, batch("igdb", [{"external_id": "1", "title": "Hades"}]),
                                 system=True)
            sess.commit()
        signup(c, "alice")
        page = c.get(f"/game/{r.canonical_id}").text
        assert "Discussion" not in page and "Add to list" not in page and ">Follow<" not in page
        t = token(c, "/me")
        assert c.post(f"/game/{r.canonical_id}/comments", data={"csrf": t, "body": "x"}).status_code == 403
        assert c.post("/follow", data={"csrf": t, "type": "game", "target": r.canonical_id}).status_code == 403


_ = (mail, Ctx)
