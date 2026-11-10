"""The group must feel like ONE node (SPEC-UI-POLISH-PASS addendum 2026-11-09, items 1-5), and the U3 review fixes
to the enrolment preset. Two in-process lab nodes joined through the real enrolment pages with "Same owner, full
trust"; synthetic names only.

- item 1: the enroller links their own account as the last enrolment step (password here, consent and fingerprint
  on the other node, wizard back here), with the non-sensitive fields pre-ticked;
- item 5: the enroller is linked at the end, a preference change reaches the other node within a sync round, and a
  second person on the node stays unlinked;
- items 2-4: the actionable "Not linked" badge, the Group sync strip on both pages, the "Invite users to link"
  notice (never an automatic link).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from atlas import group_presets, runtime
from atlas.models import AuditEntry, GroupAccountLink, GroupEnrolment, Peer, User

from .channel_harness import rounds, shown
from .test_group_link_web import authorize, callback, hidden
from .test_group_polish import code_of, finish, invite, join, policy
from .test_group_ui import create_via_ui, csrf, ui  # noqa: F401  (fixture)
from .test_web import PW, signup


def linked_uid(node, username):
    with node.session() as s:
        u = s.scalars(select(User).where(User.username == username)).one()
        link = s.scalars(select(GroupAccountLink).where(GroupAccountLink.user_id == u.id)).first()
        return u.id, (link.account_uid if link else None)


def joined(a, b, ca, cb, inviter="same_owner", joiner="same_owner"):
    create_via_ui(a, ca)
    code = code_of(invite(ca, preset=inviter).text)
    assert join(cb, code=code, preset=joiner).status_code == 303
    return finish(a, ca, cb)


def link_from_enrolment(a, b, ca, cb, eid, fields=None):
    """The joiner's administrator presses "Link my account on both nodes" on the done step of enrolment `eid`."""
    done = cb.get(f"/admin/group/enrolment/{eid}").text
    ticked = fields if fields is not None else re.findall(r'name="c\.([\w.]+)" checked', done)
    r = cb.post(f"/admin/group/enrolment/{eid}/link", data={
        "csrf": csrf(cb, f"/admin/group/enrolment/{eid}"), "password": PW, **{f"c.{f}": "on" for f in ticked}})
    assert r.status_code == 200, r.text
    assert 'action="http://a.test/group/authorize"' in r.text  # on to the other node: sign-in and fingerprint there
    assertion = authorize(ca, hidden(r.text, "request"), fields=ticked)
    wizard = callback(b, cb, assertion)
    assert wizard.status_code == 200 and "Finish linking" in wizard.text, wizard.text
    r = cb.post("/group/link/finish", data={"csrf": hidden(wizard.text, "csrf"), "h": hidden(wizard.text, "h"),
                                            "decision": "link", **{f"c.{f}": "on" for f in ticked}})
    assert r.status_code == 303 and r.headers["location"].startswith("/me/linked-nodes"), r.text
    return ticked


def test_enrolment_offers_link_with_non_sensitive_fields_preticked(ui):  # noqa: F811
    a, b, ca, cb = ui
    eid = joined(a, b, ca, cb)
    done = cb.get(f"/admin/group/enrolment/{eid}").text
    assert 'data-enrol-link="offer"' in done and "Link my account on both nodes" in done
    ticked = set(re.findall(r'name="c\.([\w.]+)" checked', done))
    offered = set(re.findall(r'name="c\.([\w.]+)"', done))
    for name in ("display_name", "theme", "locale", "time_zone"):
        assert name in ticked, name
    for name in ("bio", "privacy.profile_public", "identity.refs"):  # one click away, never pre-ticked
        assert name in offered and name not in ticked, name
    assert 'autocomplete="current-password"' in done and "never matched by name or email" in done
    # the inviter's done step offers the same, the other way
    assert 'data-enrol-link="offer"' in ca.get(f"/admin/group/enrolment/{eid}").text


def test_enrolment_link_needs_the_password(ui):  # noqa: F811
    a, b, ca, cb = ui
    eid = joined(a, b, ca, cb)
    r = cb.post(f"/admin/group/enrolment/{eid}/link", data={"csrf": csrf(cb, f"/admin/group/enrolment/{eid}"),
                                                            "password": "wrong", "c.theme": "on"})
    assert r.status_code == 400 and "Confirm your current password" in r.text
    assert linked_uid(b, "adminb")[1] is None


def test_no_offer_without_full_trust(ui):  # noqa: F811
    a, b, ca, cb = ui
    eid = joined(a, b, ca, cb, inviter="cautious", joiner="cautious")
    done = cb.get(f"/admin/group/enrolment/{eid}").text
    assert "data-enrol-link" not in done and "/me/linked-nodes" in done
    r = cb.post(f"/admin/group/enrolment/{eid}/link", data={"csrf": csrf(cb, f"/admin/group/enrolment/{eid}"),
                                                            "password": PW})
    assert r.status_code == 303 and linked_uid(b, "adminb")[1] is None


def test_lab_two_nodes_enroller_linked_preference_propagates_second_user_unlinked(ui):  # noqa: F811
    """Addendum item 5."""
    a, b, ca, cb = ui
    carol = TestClient(b.app, base_url=b.settings.public_url, follow_redirects=False)
    signup(carol, "carol")  # a second person on the joining node, never linked by anyone else
    eid = joined(a, b, ca, cb)
    link_from_enrolment(a, b, ca, cb, eid)
    ua, uid_a = linked_uid(a, "admina")
    ub, uid_b = linked_uid(b, "adminb")
    assert uid_a is not None and uid_a == uid_b  # the enroller's account is linked at the end of enrolment
    with b.session() as s:
        line = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_link_started")).one()
        assert line.detail["via"] == "enrolment"
    assert 'data-enrol-link="linked"' in cb.get(f"/admin/group/enrolment/{eid}").text
    # a preference change on A reaches B within the sync interval (one round of the group sync job)
    page = ca.get("/me/settings").text
    r = ca.post("/me/preferences", data={"csrf": hidden(page, "csrf"), "f.theme": "dark", "f.display_name": "Ada"})
    assert r.status_code == 303
    rounds(a, b)
    assert shown(b, ub, "theme") == "dark" and shown(b, ub, "display_name") == "Ada"
    # the second user stays unlinked, and sees why with a way to fix it
    assert linked_uid(b, "carol")[1] is None
    settings_page = carol.get("/me/settings").text
    assert "data-pref-unlinked" in settings_page and "Not linked to your other nodes" in settings_page
    assert 'href="/me/linked-nodes#link"' in settings_page
    linked_page = cb.get("/me/settings").text  # the linked enroller: synced badges, no "Not linked"
    assert "data-pref-unlinked" not in linked_page and ">synced<" in linked_page


def test_this_node_only_kept_for_fields_not_shared(ui):  # noqa: F811
    a, b, ca, cb = ui
    eid = joined(a, b, ca, cb)
    link_from_enrolment(a, b, ca, cb, eid, fields=["theme"])
    page = cb.get("/me/settings").text
    assert "data-pref-unlinked" not in page
    assert 'title="Not shared: you chose not to share it">this node only' in page


def test_group_sync_strip_on_linked_nodes_and_admin_page(ui):  # noqa: F811
    a, b, ca, cb = ui
    eid = joined(a, b, ca, cb)
    before = cb.get("/me/linked-nodes").text
    assert 'data-group-sync="me"' in before and "Nothing yet: this account is not linked" in before
    link_from_enrolment(a, b, ca, cb, eid)
    rounds(a, b)
    me = cb.get("/me/linked-nodes").text
    assert re.search(f'data-sync-member="{a.node_id}" data-sync-state="ok"', me)
    assert "last sync" in me and "Theme" in me[me.index("data-group-sync-shared"):]
    assert 'data-group-sync-conflicts="0"' in me
    admin = cb.get("/admin/group").text
    assert 'data-group-sync="admin"' in admin and 'data-group-sync-accounts="1/1"' in admin


def test_catalogue_sync_chip_reports_the_peer_and_never_creates_one(ui):  # noqa: F811
    """§4.2 "catalogue federation with members" [dep]: no auto-peer exists yet, so joining creates no peer and the
    chip says "off"; once the administrator follows the member on Peers, the strip and the done step say "on"."""
    a, b, ca, cb = ui
    eid = joined(a, b, ca, cb)
    with b.session() as s:
        assert s.scalars(select(Peer)).first() is None  # the join never set up catalogue federation by itself
    assert re.search(r'data-catalogue-sync="off"', cb.get("/admin/group").text)
    assert 'data-enrol-catalogue="off"' in cb.get(f"/admin/group/enrolment/{eid}").text
    with b.session() as s:
        s.add(Peer(name="Node A", url="http://a.test", node_id=a.node_id, public_key="k"))
        s.commit()
    assert 'data-catalogue-sync="on"' in cb.get("/admin/group").text
    assert 'data-enrol-catalogue="on"' in cb.get(f"/admin/group/enrolment/{eid}").text
    with b.session() as s:
        peer = s.scalars(select(Peer)).one()
        peer.last_error = "connection refused"
        s.commit()
    assert 'data-catalogue-sync="error"' in cb.get("/admin/group").text
    assert "data-catalogue-sync" not in cb.get("/me/linked-nodes").text  # an admin matter, not on the person's page


def test_admin_invite_users_to_link_shows_notice_and_never_links(ui):  # noqa: F811
    a, b, ca, cb = ui
    carol = TestClient(b.app, base_url=b.settings.public_url, follow_redirects=False)
    signup(carol, "carol")
    joined(a, b, ca, cb)
    admin = cb.get("/admin/group").text
    assert 'data-group-sync-accounts="0/2"' in admin
    assert 'data-group-link-invite="off"' in admin and "2 accounts here are not linked" in admin
    assert "data-link-notice" not in carol.get("/").text
    r = cb.post("/admin/group/link-invite", data={"csrf": csrf(cb, "/admin/group"), "action": "on"})
    assert r.status_code == 303
    home = carol.get("/").text
    assert "data-link-notice" in home and 'href="/me/linked-nodes#link"' in home
    assert linked_uid(b, "carol")[1] is None and linked_uid(b, "adminb")[1] is None  # nobody was linked
    assert 'data-group-link-invite="on"' in cb.get("/admin/group").text
    r = cb.post("/admin/group/link-invite", data={"csrf": csrf(cb, "/admin/group"), "action": "off"})
    assert r.status_code == 303 and "data-link-notice" not in carol.get("/").text
    # a plain user cannot switch it
    r = carol.post("/admin/group/link-invite", data={"csrf": csrf(carol, "/"), "action": "on"})
    assert r.status_code in (403, 404)


# ---- U3 review: the preset on activation -----------------------------------------------------------------------

def test_done_step_shows_only_its_own_preset_changes(ui):  # noqa: F811
    a, b, ca, cb = ui
    eid = joined(a, b, ca, cb, inviter="same_owner", joiner="custom")
    done = ca.get(f"/admin/group/enrolment/{eid}").text
    assert "Trust remote" in done or "two-factor" in done.lower()
    before = re.findall(r"<ul class=\"small\">(.*?)</ul>", done, re.S)
    assert before and "<li>" in before[0]  # the two rows this activation changed
    # a later manual apply on the policy page must not show up on this enrolment's done step
    r = ca.post("/admin/group/policy", data={"csrf": csrf(ca, "/admin/group"), "preset": "cautious"})
    assert r.status_code == 303
    after = re.findall(r"<ul class=\"small\">(.*?)</ul>", ca.get(f"/admin/group/enrolment/{eid}").text, re.S)
    assert after == before
    with a.session() as s:
        targets = [e.target for e in s.scalars(select(AuditEntry).where(AuditEntry.action == "group_policy_preset"))]
        assert group_presets.activation_target(s.get(GroupEnrolment, eid)) in targets and "local" in targets


def test_activation_keeps_rows_the_preset_does_not_decide(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    with a.session() as s:  # the administrator turned public group sign-in on after inviting
        runtime.save(s, a.settings, None, {"group_signin_public": True})
        s.commit()
    code = code_of(invite(ca, preset="same_owner").text)
    assert join(cb, code=code, preset="custom").status_code == 303
    finish(a, ca, cb)
    with a.session() as s:
        eff = runtime.effective(a.settings, runtime.load(s))
    assert eff.group_signin_public is True and eff.group_auto_provision is True


@pytest.mark.parametrize("error", [runtime.SettingError("the local policy store refused the write"),
                                   OperationalError("UPDATE setting", {}, Exception("database is locked: refused"))])
def test_failing_policy_write_never_breaks_the_enrolment(ui, monkeypatch, error):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    code = code_of(invite(ca, preset="same_owner").text)
    assert join(cb, code=code, preset="custom").status_code == 303

    def broken(*_a, **_k):
        raise error

    monkeypatch.setattr(group_presets.runtime, "save", broken)
    eid = finish(a, ca, cb)
    with a.session() as s:
        row = s.get(GroupEnrolment, eid)
        assert row.state == "active"
        failed = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_policy_preset_failed")).one()
        assert failed.target == f"enrolment:{eid}" and "refused" in failed.detail["error"]
    monkeypatch.undo()
    assert policy(a)["group_auto_provision"] is False  # nothing half-written
