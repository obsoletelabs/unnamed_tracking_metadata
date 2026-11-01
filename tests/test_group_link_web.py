"""The link web flow (CONTRACT-C10 §5.3-§5.4, §11 A11-cc/k2/p2/v): POST-park / 303-GET with `atlas_grp_bind`, the
consent page on the asserting node, the wizard diff, a foreign assertion refused, and the uniform 404."""

from __future__ import annotations

import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import group_channel, groups, profile
from atlas import group_crypto as gc
from atlas.models import (
    AuditEntry,
    GroupAccountLink,
    GroupAssertionSeen,
    GroupSigninState,
    ProfileConflict,
    ProfileField,
    User,
)

from .channel_harness import group_of, heads, rounds, shown
from .group_harness import Dispatcher, fast_kdf, make_node
from .test_web import PW, signup, token


@pytest.fixture
def web(tmp_path, monkeypatch):
    fast_kdf(monkeypatch)
    group_channel._HELLO.clear()
    net = Dispatcher()
    a, b = make_node(tmp_path, net, "a"), make_node(tmp_path, net, "b")
    group_of(a, b)
    ca = TestClient(a.app, base_url="http://a.test", follow_redirects=False)
    cb = TestClient(b.app, base_url="http://b.test", follow_redirects=False)
    signup(ca, "alice")
    signup(cb, "alicia")
    yield net, a, b, ca, cb
    for n in (a, b):
        n.close()


def uid_of_name(node, name):
    with node.session() as s:
        u = s.scalars(select(User).where(User.username == name)).one()
        link = s.scalars(select(GroupAccountLink).where(GroupAccountLink.user_id == u.id)).first()
        return u.id, (link.account_uid if link else None)


def hidden(page: str, name: str) -> str:
    return html.unescape(re.search(rf'name="{name}" value="([^"]+)"', page).group(1))


def start(cb, a) -> str:
    r = cb.post("/me/linked-nodes/link", data={"csrf": token(cb, "/me/linked-nodes"), "node_id": a.node_id,
                                               "password": PW})
    assert r.status_code == 200, r.text
    assert 'action="http://a.test/group/authorize"' in r.text and "data-autosubmit" in r.text
    assert "form-action 'self' http://a.test;" in r.headers["content-security-policy"]
    assert "atlas_grp_bind=" in r.headers["set-cookie"] and "Path=/group/" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "samesite=lax" in r.headers["set-cookie"].lower()
    return hidden(r.text, "request")


def authorize(ca, request: str, fields=("theme", "display_name")) -> str:
    r = TestClient(ca.app, base_url="http://a.test").post("/group/authorize", data={"request": request},
                                                          follow_redirects=False)  # a cross-site POST: no cookies
    assert r.status_code == 303 and r.headers["location"].startswith("/group/authorize/continue?h=")
    page = ca.get(r.headers["location"])
    assert page.status_code == 200
    data = {"csrf": hidden(page.text, "csrf"), "h": hidden(page.text, "h"), "decision": "approve",
            **{f"c.{f}": "on" for f in fields}}
    r = ca.post("/group/authorize/continue", data=data)
    assert r.status_code == 200 and 'action="http://b.test/group/callback"' in r.text
    return hidden(r.text, "assertion")


def callback(b, cb, assertion: str):
    r = TestClient(b.app, base_url="http://b.test").post("/group/callback", data={"assertion": assertion},
                                                         follow_redirects=False)  # no cookies on the POST
    assert r.status_code == 303 and r.headers["location"].startswith("/group/callback/finish?h=")
    return cb.get(r.headers["location"])


def finish(cb, wizard: str, **extra):
    data = {"csrf": hidden(wizard, "csrf"), "h": hidden(wizard, "h"), "decision": "link",
            "c.theme": "on", "c.display_name": "on", "signout": "on", **extra}
    return cb.post("/group/link/finish", data=data)


def test_link_flow_end_to_end_with_consent_and_binding(web):
    net, a, b, ca, cb = web
    request = start(cb, a)
    assert a.node_id not in request and "alice" not in request  # sealed: no identifiers in the payload
    assertion = authorize(ca, request)
    wizard = callback(b, cb, assertion)
    assert wizard.status_code == 200 and "Finish linking with Node A" in wizard.text
    assert "atlas_grp_bind=" in wizard.headers["set-cookie"]  # cleared at the finish step
    r = finish(cb, wizard.text)
    assert r.status_code == 303 and r.headers["location"].startswith("/me/linked-nodes")
    ua, uid = uid_of_name(a, "alice")
    ub, uid_b = uid_of_name(b, "alicia")
    assert uid is not None and uid_b == uid
    with b.session() as s:
        assert s.scalar(select(func.count()).select_from(GroupSigninState)) == 0
        assert s.scalar(select(func.count()).select_from(GroupAssertionSeen)) == 1
        assert {a.action for a in s.scalars(select(AuditEntry).where(AuditEntry.action.like("group_link%")))} >= {
            "group_link_started", "group_link_wizard"}
    with a.session() as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "group_assertion_issued")).first() is not None
        assert set(profile.consent_fields(s, ua, groups.load(s).group_id)) == {"theme", "display_name"}
    rounds(a, b)
    edit_page = ca.get("/me/settings")
    assert "synced" in edit_page.text
    r = ca.post("/me/preferences", data={"csrf": hidden(edit_page.text, "csrf"), "f.theme": "dark",
                                         "f.display_name": "Alice"})
    assert r.status_code == 303
    rounds(a, b)
    assert shown(b, ub, "theme") == "dark" and shown(b, ub, "display_name") == "Alice"
    page = cb.get("/me/settings").text
    assert 'data-theme="dark"' in page and "set on Node A" in page


def test_wizard_keep_the_other_node_creates_resolving_versions_and_no_conflicts(web):
    net, a, b, ca, cb = web
    ua, _ = uid_of_name(a, "alice")
    ub, _ = uid_of_name(b, "alicia")
    with a.session() as s:
        profile.set_local(s, a.settings, a.identity, s.get(User, ua), "theme", "dark")
        s.commit()
    with b.session() as s:
        profile.set_local(s, b.settings, b.identity, s.get(User, ub), "theme", "light")
        profile.set_local(s, b.settings, b.identity, s.get(User, ub), "display_name", "Alicia")
        s.commit()
    wizard = callback(b, cb, authorize(ca, start(cb, a)))
    assert "Where the two nodes differ" in wizard.text and 'name="k.theme"' in wizard.text
    finish(cb, wizard.text, **{"k.theme": "theirs"})
    assert shown(b, ub, "theme") == "dark"
    rounds(a, b)
    assert shown(a, ua, "theme") == shown(b, ub, "theme") == "dark"
    assert shown(a, ua, "display_name") == "Alicia"  # only on b before: now shared
    assert len(heads(a, ua, "theme")) == len(heads(b, ub, "theme")) == 1
    for node in (a, b):
        with node.session() as s:
            assert s.scalar(select(func.count()).select_from(ProfileConflict).where(
                ProfileConflict.status == "open")) == 0


def test_wizard_keep_this_node_wins_everywhere(web):
    net, a, b, ca, cb = web
    ua, _ = uid_of_name(a, "alice")
    ub, _ = uid_of_name(b, "alicia")
    with a.session() as s:
        profile.set_local(s, a.settings, a.identity, s.get(User, ua), "theme", "dark")
        s.commit()
    with b.session() as s:
        profile.set_local(s, b.settings, b.identity, s.get(User, ub), "theme", "light")
        s.commit()
    finish(cb, callback(b, cb, authorize(ca, start(cb, a))).text, **{"k.theme": "mine"})
    rounds(a, b)
    assert shown(a, ua, "theme") == shown(b, ub, "theme") == "light"
    with a.session() as s:
        assert s.scalar(select(func.count()).select_from(ProfileConflict).where(ProfileConflict.status == "open")) == 0


def test_callback_binding_survives_a_cross_site_post_and_foreign_assertion_is_refused(web, tmp_path):
    """The victim's browser receives the attacker's valid assertion: its state matches no state bound to the
    victim's `atlas_grp_bind`, so nothing is linked (login CSRF / session swap, T28)."""
    net, a, b, ca, cb = web
    attacker_a = TestClient(a.app, base_url="http://a.test", follow_redirects=False)
    signup(attacker_a, "mallory")
    attacker_b = TestClient(b.app, base_url="http://b.test", follow_redirects=False)
    signup(attacker_b, "mallorie")
    foreign = authorize(attacker_a, start(attacker_b, a))
    start(cb, a)  # the victim has a flow of its own going
    page = callback(b, cb, foreign)
    assert page.status_code == 400 and ("not started in this browser" in page.text or "another account" in page.text)
    _, uid = uid_of_name(b, "alicia")
    assert uid is None
    with b.session() as s:
        assert s.scalar(select(func.count()).select_from(GroupAccountLink)) == 0
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "group_link_refused")).first() is not None
    # and the attacker cannot use it afterwards either: the state row was consumed
    page = callback(b, attacker_b, foreign)
    assert page.status_code == 400


def test_replayed_assertion_is_refused(web):
    net, a, b, ca, cb = web
    assertion = authorize(ca, start(cb, a))
    wizard = callback(b, cb, assertion)
    assert wizard.status_code == 200
    assert callback(b, cb, assertion).status_code == 400  # state consumed, jti seen


def _uniform(resp) -> tuple:
    return resp.status_code, resp.content, resp.headers.get("content-type")


def test_group_web_routes_answer_the_uniform_404(web, tmp_path):
    net, a, b, ca, cb = web
    reference = _uniform(ca.get("/definitely/not/a/route"))
    assert reference[0] == 404
    garbage = {"request": "not json"}
    unsealed = {"request": json.dumps({"t": "group.authreq", "body": {}})}
    with b.session() as s:  # sealed to a, but signed by a key that is not b's
        group = groups.load(s)
        member = groups.trust(s, group, own_node=b.node_id).in_force(a.node_id)["body"]["member"]
        from tests.group_world import synth

        stranger = synth()
        body = {"rp_node": b.node_id, "idp_node": a.node_id, "purpose": "link", "state_hash": "0" * 64,
                "nonce": "01JBA4AAAAAAAAAAAAAAAAAAAA", "issued_at": gc.stamp(groups.utcnow()),
                "not_after": gc.stamp(groups.utcnow())}
        env = gc.make_object("group.authreq", group.group_id, "authreq:01JBA4AAAAAAAAAAAAAAAAAAAA", body,
                             {"kind": "node", "id": b.node_id, "key_id": stranger.key_id}, stranger.private)
        unsigned = {"request": json.dumps(gc.seal_request(member["box_public_key"], group.group_id, a.node_id, env))}
    outsider = make_node(tmp_path, net, "c")  # in no group at all
    co = TestClient(outsider.app, base_url="http://c.test", follow_redirects=False)
    try:
        answers = [ca.post("/group/authorize", data=garbage), ca.post("/group/authorize", data=unsealed),
                   ca.post("/group/authorize", data=unsigned), ca.post("/group/authorize", data={}),
                   ca.get("/group/authorize/continue?h=bogus"), cb.post("/group/callback", data={"assertion": "x"}),
                   cb.get("/group/callback/finish?h=bogus"), co.post("/group/authorize", data=unsigned),
                   co.post("/group/callback", data={"assertion": "x"}),
                   cb.post("/group/link/finish", data={"csrf": token(cb, "/me/linked-nodes"), "h": "bogus"})]
        for r in answers:
            assert _uniform(r) == reference
    finally:
        outsider.close()


def test_link_requires_reauthentication(web):
    net, a, b, ca, cb = web
    r = cb.post("/me/linked-nodes/link", data={"csrf": token(cb, "/me/linked-nodes"), "node_id": a.node_id,
                                               "password": "wrong"})
    assert r.status_code == 400 and "Confirm your current password" in r.text
    with b.session() as s:
        assert s.scalar(select(func.count()).select_from(GroupSigninState)) == 0


def set_status(node, status: str) -> None:
    with node.session() as s:
        groups.load(s).status = status
        s.commit()


def test_restore_resyncs_before_privileged_actions(web):
    """A11-p2 for the link: a node that is `resyncing` (after a restore) neither asserts, finishes nor starts a link
    until it has pulled every member's state (§3.6, §5.3 step 5, RP verification)."""
    net, a, b, ca, cb = web
    request = start(cb, a)
    set_status(a, "resyncing")
    r = TestClient(ca.app, base_url="http://a.test").post("/group/authorize", data={"request": request},
                                                          follow_redirects=False)
    page = ca.get(r.headers["location"])
    assert page.status_code == 404 and page.json() == {"detail": "Not Found"}
    set_status(a, "active")
    assertion = authorize(ca, start(cb, a))
    set_status(b, "resyncing")
    r = callback(b, cb, assertion)
    assert r.status_code == 400 and "resynchronizing" in r.text
    r = cb.post("/me/linked-nodes/link", data={"csrf": token(cb, "/me/linked-nodes"), "node_id": a.node_id,
                                               "password": PW})
    assert r.status_code == 400 and "not an active member" in r.text
    ub, _ = uid_of_name(b, "alicia")
    with b.session() as s:
        assert s.scalars(select(GroupAccountLink).where(GroupAccountLink.user_id == ub)).first() is None
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "group_link_refused")).first() is not None


def test_preferences_form_saves_versions_and_applies_theme_and_a11y(web):
    net, a, b, ca, cb = web
    page = ca.get("/me/settings").text
    r = ca.post("/me/preferences", data={"csrf": hidden(page, "csrf"), "f.theme": "light", "f.a11y.font_scale": "1.25",
                                         "present.a11y.underline_links": "1", "f.a11y.underline_links": "on",
                                         "f.date_format": "dmy"})
    assert r.status_code == 303
    ua, _ = uid_of_name(a, "alice")
    with a.session() as s:
        stored = {r.field: r.value for r in s.scalars(select(ProfileField).where(ProfileField.user_id == ua))}
        assert stored.items() >= {"theme": "light", "a11y.font_scale": 1.25, "a11y.underline_links": True,
                                  "date_format": "dmy"}.items()
    page = ca.get("/me/settings").text
    assert 'data-theme="light"' in page and "font-scale-125" in page and "a11y-underline-links" in page
    again = ca.post("/me/preferences", data={"csrf": hidden(page, "csrf"), "f.theme": "light",
                                             "f.a11y.font_scale": "1.25", "present.a11y.underline_links": "1",
                                             "f.a11y.underline_links": "on", "f.date_format": "dmy"})
    assert "m=nochange" in again.headers["location"]  # an unchanged form writes no new version
    bad = ca.post("/me/preferences", data={"csrf": hidden(page, "csrf"), "f.time_zone": "Not/AZone!"})
    assert bad.status_code == 400
