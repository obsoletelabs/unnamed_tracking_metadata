"""Admin UI for the node group (CONTRACT-C10 §8.1, §11 A10 x): create with the recovery download, invite, join,
SAS confirmation on both sides, the members table and revoke, each checked against the database."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import groups
from atlas.models import GroupEnrolment, GroupRevoked, User

from .group_harness import REC_PW, ROOT_PW, Dispatcher, fast_kdf, make_node
from .test_web import signup, token


@pytest.fixture
def ui(tmp_path, monkeypatch):
    fast_kdf(monkeypatch)
    net = Dispatcher()
    a, b = make_node(tmp_path, net, "a"), make_node(tmp_path, net, "b")
    browsers = {}
    for node in (a, b):
        c = TestClient(node.app, base_url=node.settings.public_url, follow_redirects=False)
        signup(c, f"admin{node.name}")  # the first account is the administrator
        browsers[node.name] = c
    yield a, b, browsers["a"], browsers["b"]
    for node in (a, b):
        node.close()


def csrf(client, path):
    return token(client, path)


def create_via_ui(a, ca):
    r = ca.post("/admin/group/create", data={"csrf": csrf(ca, "/admin/group"), "name": "Home nodes",
                                              "root_passphrase": ROOT_PW, "root_passphrase2": ROOT_PW,
                                              "recovery_passphrase": REC_PW[0], "recovery_passphrase2": REC_PW[0]})
    assert r.status_code == 303
    page = ca.get("/admin/group").text
    assert 'data-group-recovery="pending"' in page
    download = ca.get("/admin/group/recovery.json")
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    assert download.json()["contains"] == ["recovery_key", "root_key"] and REC_PW[0] not in download.text
    r = ca.post("/admin/group/recovery/confirm", data={"csrf": csrf(ca, "/admin/group"), "downloaded": "on",
                                                       "passphrase": REC_PW[0]})
    assert r.status_code == 303
    assert 'data-group-recovery="pending"' not in ca.get("/admin/group").text


def test_group_admin_flow(ui):
    a, b, ca, cb = ui
    assert "Create a group on this node" in ca.get("/admin/group").text
    create_via_ui(a, ca)
    page = ca.get("/admin/group").text
    with a.session() as s:
        group = groups.load(s)
        assert group.name == "Home nodes" and group.root_state == "holder"
    assert groups.gc_fp(group.group_id) in page and 'data-member="' + a.node_id + '"' in page

    invite = ca.post("/admin/group/invite", data={"csrf": csrf(ca, "/admin/group"), "delegate_enrol": "0"})
    assert invite.status_code == 200
    code = re.search(r"data-invite-code>(AG1-[^<]+)<", invite.text).group(1)

    r = cb.post("/admin/group/join", data={"csrf": csrf(cb, "/admin/group/join"), "url": "http://a.test",
                                           "code": code, "name": "Laptop"})
    assert r.status_code == 303
    joiner_page = cb.get(r.headers["location"]).text
    sas_b = re.search(r"data-sas><strong>([0-9-]+)<", joiner_page).group(1)
    with a.session() as s:
        eid = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")).one().id
    inviter_page = ca.get(f"/admin/group/enrolment/{eid}").text
    assert re.search(r"data-sas><strong>([0-9-]+)<", inviter_page).group(1) == sas_b
    assert groups.gc_fp(b.node_id) in inviter_page and "Laptop" in inviter_page

    # The box must be ticked; the root passphrase must be right.
    no_tick = ca.post(f"/admin/group/enrolment/{eid}/confirm",
                      data={"csrf": csrf(ca, f"/admin/group/enrolment/{eid}"), "root_passphrase": ROOT_PW})
    assert no_tick.status_code == 400
    wrong = ca.post(f"/admin/group/enrolment/{eid}/confirm", data={
        "csrf": csrf(ca, f"/admin/group/enrolment/{eid}"), "matches": "on", "root_passphrase": "not the root phrase"})
    assert wrong.status_code == 403
    ok = ca.post(f"/admin/group/enrolment/{eid}/confirm", data={
        "csrf": csrf(ca, f"/admin/group/enrolment/{eid}"), "matches": "on", "root_passphrase": ROOT_PW})
    assert ok.status_code == 303
    done = cb.post(f"/admin/group/enrolment/{eid}/confirm",
                   data={"csrf": csrf(cb, f"/admin/group/enrolment/{eid}"), "matches": "on"})
    assert done.status_code == 303

    members_b = cb.get("/admin/group").text
    for node in (a, b):
        assert f'data-member="{node.node_id}"' in members_b and groups.gc_fp(node.node_id) in members_b
    assert "profile.sync" in members_b and 'data-member-status="active"' in members_b

    revoke = ca.post(f"/admin/group/members/{b.node_id}/revoke", data={
        "csrf": csrf(ca, "/admin/group"), "reason": "removed", "root_passphrase": ROOT_PW})
    assert revoke.status_code == 303
    with a.session() as s:
        assert s.scalars(select(GroupRevoked).where(GroupRevoked.node_id == b.node_id)).first().reason == "removed"
    assert f'data-member="{b.node_id}" data-member-status="revoked"' in ca.get("/admin/group").text


def test_group_pages_are_admin_only(ui):
    a, _, ca, _ = ui
    other = TestClient(a.app, base_url=a.settings.public_url, follow_redirects=False)
    signup(other, "curious")
    with a.session() as s:
        assert s.scalars(select(User).where(User.username == "curious")).one().role != "admin"
    assert other.get("/admin/group").status_code in (302, 303, 403, 404)
    assert other.get("/admin/group/recovery.json").status_code in (302, 303, 403, 404)


def test_weak_passphrase_rejected_in_ui(ui):
    _, _, ca, _ = ui
    r = ca.post("/admin/group/create", data={"csrf": csrf(ca, "/admin/group"), "name": "Home",
                                              "root_passphrase": "password12345678", "root_passphrase2": "password12345678",
                                              "recovery_passphrase": REC_PW[0], "recovery_passphrase2": REC_PW[0]})
    assert r.status_code == 400 and "passphrase refused" in r.text
