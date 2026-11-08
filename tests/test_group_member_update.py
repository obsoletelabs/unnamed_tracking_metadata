"""#122: adding a node back after it left, and address or name changes reaching the group (C10 §1.3 member update).

Before this change a node that left stayed a member in everyone else's view (a node that left answered no call,
so its `left` CRL never travelled), so it could never be enrolled again ("that node is already a member"), and a
member whose address changed had no way to get it into its certificate, so the other members kept calling the
old one.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import group_crypto as gc
from atlas import group_enrol, groups
from atlas.api import group_routes
from atlas.models import AuditEntry, GroupMember, NodeState

from .group_harness import ROOT_PW, Dispatcher, create, enrol, fast_kdf, finish_creation, invite, make_node, members, pull
from .test_web import PW, signup, token


@pytest.fixture
def net(tmp_path, monkeypatch):
    fast_kdf(monkeypatch)
    d = Dispatcher()
    nodes = []

    def node(name, **kw):
        n = make_node(tmp_path, d, name, **kw)
        nodes.append(n)
        return n

    d.node = node
    yield d
    for n in nodes:
        n.close()


@pytest.fixture
def trio(net):
    a, b, c = net.node("a"), net.node("b"), net.node("c")
    create(a)
    finish_creation(a)
    enrol(a, b)
    enrol(a, c)
    pull(b)
    return a, b, c


def _leave(node, push=True):
    with node.session() as s:
        group = groups.load(s)
        groups.leave(s, node.settings, group, node.identity)
        out = group_routes.push_state(s, node.settings, node.identity, group, node.net) if push else None
        s.commit()
        return out


def _actions(node):
    with node.session() as s:
        return [a for (a,) in s.execute(select(AuditEntry.action))]


# ---- a node that left can be added again ----------------------------------------------------------------------

def test_leave_is_pushed_and_the_node_can_be_enrolled_again(net):
    a, b = net.node("a"), net.node("b")
    create(a)
    finish_creation(a)
    enrol(a, b)
    assert _leave(b) == {a.node_id: "ok"}
    assert b.node_id not in members(a)  # a learnt it at once, not on some later pull
    serial = enrol(a, b)  # the reported defect: this raised "that node is already a member"
    assert set(members(a)) == set(members(b)) == {a.node_id, b.node_id}
    with b.session() as s:
        assert groups.load(s).status == "active" and groups.load(s).self_serial == serial


def test_left_node_still_answers_state_so_unreached_members_learn(net):
    a, b = net.node("a"), net.node("b")
    create(a)
    finish_creation(a)
    enrol(a, b)
    net.down.add("a.test")
    assert _leave(b)[a.node_id].startswith("unreachable")
    net.down.discard("a.test")
    assert b.node_id in members(a)
    with a.session() as s:  # the inviter's refusal reaches the joiner with its reason and what to do
        code = invite(a)
    with b.session() as s, pytest.raises(groups.GroupError, match="already a member here.*Sync with members now"):
        group_enrol.start_join(s, b.settings, b.identity, "http://a.test", code, net)
    assert pull(a)[b.node_id] == "ok"  # b left but still serves `state`, which carries its `left` CRL
    assert b.node_id not in members(a)
    enrol(a, b)
    assert set(members(a)) == {a.node_id, b.node_id}


def test_left_node_answers_only_hello_and_state(net):
    a, b = net.node("a"), net.node("b")
    create(a)
    finish_creation(a)
    enrol(a, b)
    net.down.add("a.test")
    _leave(b)
    with a.session() as s:
        group = groups.load(s)
        member = groups.trust(s, group, own_node=a.node_id).in_force(b.node_id)["body"]["member"]
        assert "certs" in group_routes.call(s, a.settings, a.identity, group, member, "state", {}, net)
        for op in ("heads", "push", "member_update"):
            with pytest.raises(group_routes.CallError, match="not_found"):
                group_routes.call(s, a.settings, a.identity, group, member, op, {}, net)


# ---- address and name changes ---------------------------------------------------------------------------------

def _move(net, node, new_host):
    """The node now answers on a new host and the old one is gone."""
    old = node.settings.public_url.split("://", 1)[1]
    net.clients[new_host] = net.clients[old]
    net.down.add(old)
    node.settings.public_url = f"http://{new_host}"


def test_member_address_change_reaches_every_member(trio, net):
    a, b, c = trio
    _move(net, b, "b2.test")
    with b.session() as s:
        group = groups.load(s)
        assert groups.own_address(s, b.settings, group, b.identity)["changed"] is True
        update = groups.member_update(s, b.settings, group, b.identity, url="http://b2.test")
        assert update["member"]["rev"] == 2 and update["member"]["url"] == "http://b2.test"
        outcomes = group_routes.send_member_update(s, b.settings, b.identity, group, update, net)
        s.commit()
    assert outcomes[a.node_id] == "held" and "not_root" in outcomes[c.node_id]  # only the root holder keeps it
    with a.session() as s:
        group = groups.load(s)
        held = groups.held_member_updates(s, group, a.node_id)
        assert [(h["node_id"], h["old_url"], h["url"]) for h in held] == [(b.node_id, "http://b.test",
                                                                          "http://b2.test")]
        root = groups.unlock_root(s, a.settings, group, ROOT_PW)
        cert = groups.apply_member_update(s, a.settings, group, a.identity, json.loads(
            s.get(NodeState, groups.MEMBER_UPDATE_PREFIX + b.node_id).value), root=root)
        assert cert["body"]["member"]["url"] == "http://b2.test"
        assert cert["body"]["scopes"] == ["profile.sync", "group.relay", "identity.assert"]  # nothing else changed
        assert group_routes.push_state(s, a.settings, a.identity, group, net) == {b.node_id: "ok", c.node_id: "ok"}
        assert groups.held_member_updates(s, group, a.node_id) == []
        s.commit()
    for node in (a, b, c):
        with node.session() as s:
            assert s.get(GroupMember, (groups.load(s).group_id, b.node_id)).url == "http://b2.test"
    with b.session() as s:
        assert groups.own_address(s, b.settings, groups.load(s), b.identity)["changed"] is False
    assert pull(c)[b.node_id] == "ok"  # c reaches b at its new address; the old one is down


def test_root_holder_address_change_is_pushed(trio, net):
    a, b, c = trio
    _move(net, a, "a2.test")
    with a.session() as s:
        group = groups.load(s)
        update = groups.member_update(s, a.settings, group, a.identity, url="http://a2.test", name="Home server")
        root = groups.unlock_root(s, a.settings, group, ROOT_PW)
        groups.apply_member_update(s, a.settings, group, a.identity, update, root=root)
        assert group_routes.push_state(s, a.settings, a.identity, group, net) == {b.node_id: "ok", c.node_id: "ok"}
        s.commit()
    assert members(b)[a.node_id]["body"]["member"]["url"] == "http://a2.test"
    assert members(b)[a.node_id]["body"]["member"]["name"] == "Home server"
    assert pull(b)[a.node_id] == "ok"  # the old address is down, the new one answers


def test_member_update_is_only_accepted_from_the_member_itself(trio, net):
    a, b, c = trio
    with b.session() as s:
        update = groups.member_update(s, b.settings, groups.load(s), b.identity, url="http://b2.test")
    with c.session() as s:  # c relays b's genuinely signed update: the root holder refuses it
        group = groups.load(s)
        a_member = groups.trust(s, group, own_node=c.node_id).in_force(a.node_id)["body"]["member"]
        with pytest.raises(group_routes.CallError, match="only update its own"):
            group_routes.call(s, c.settings, c.identity, group, a_member, "member_update", {"update": update}, net)
    with a.session() as s:
        assert groups.held_member_updates(s, groups.load(s), a.node_id) == []


@pytest.mark.parametrize("change", ["forged_pop", "same_rev", "box_key", "root_key", "other_url", "extra_field"])
def test_member_update_checks(trio, change):
    a, b, c = trio
    with b.session() as s:
        group = groups.load(s)
        update = groups.member_update(s, b.settings, group, b.identity, url="http://b2.test")
    member = dict(update["member"])
    if change == "forged_pop":  # c signs b's member object
        update["member_pop"] = gc.member_pop(c.identity.private, group.group_id, member, c.identity.key_id)
    elif change == "same_rev":
        member["rev"] = 1
        update = {"member": member, "member_pop": gc.member_pop(b.identity.private, group.group_id, member,
                                                                 b.identity.key_id)}
    elif change == "box_key":
        member["box_public_key"] = gc.x_public_b64(gc.X25519PrivateKey.generate())
        update = {"member": member, "member_pop": gc.member_pop(b.identity.private, group.group_id, member,
                                                                 b.identity.key_id)}
    elif change == "root_key":
        member["root_public_key"] = c.identity.trust_public_b64
        update = {"member": member, "member_pop": update["member_pop"]}
    elif change == "other_url":  # c's address
        member["url"] = "http://C.test:80"
        update = {"member": member, "member_pop": gc.member_pop(b.identity.private, group.group_id, member,
                                                                 b.identity.key_id)}
    else:
        update = {**update, "scopes": ["group.enrol"]}
    with a.session() as s:
        group = groups.load(s)
        root = groups.unlock_root(s, a.settings, group, ROOT_PW)
        with pytest.raises(groups.GroupError):
            groups.hold_member_update(s, a.settings, group, a.node_id, update, b.node_id)
        with pytest.raises(groups.GroupError):
            groups.apply_member_update(s, a.settings, group, a.identity, update, root=root)
        assert members(a)[b.node_id]["body"]["member"]["url"] == "http://b.test"


def test_only_a_root_holder_keeps_an_update(trio):
    a, b, c = trio
    with b.session() as s:
        update = groups.member_update(s, b.settings, groups.load(s), b.identity, name="Laptop")
    with c.session() as s, pytest.raises(groups.GroupError, match="not_root"):
        groups.hold_member_update(s, c.settings, groups.load(s), c.node_id, update, b.node_id)


def test_member_update_refuses_no_change_and_bad_urls(trio):
    a, b, c = trio
    with b.session() as s:
        group = groups.load(s)
        with pytest.raises(groups.GroupError, match="already has"):
            groups.member_update(s, b.settings, group, b.identity, url="http://b.test")
        with pytest.raises(groups.GroupError, match="plain http"):
            groups.member_update(s, b.settings, group, b.identity, url="ftp://b2.test")
        with pytest.raises(groups.GroupError, match="already uses that address"):
            groups.member_update(s, b.settings, group, b.identity, url="http://c.test")


# ---- the admin pages ------------------------------------------------------------------------------------------

def _browser(node):
    c = TestClient(node.app, base_url=node.settings.public_url, follow_redirects=False)
    signup(c, f"admin{node.name}")
    return c


def test_admin_pages_add_a_node_elsewhere_and_address_update_flow(trio, net):
    a, b, c = trio
    ca, cb = _browser(a), _browser(b)
    page = cb.get("/admin/group").text
    # b cannot enrol: instead of no "Add a node" section at all, it says where to do it
    assert 'data-group-invite="elsewhere"' in page and "http://a.test/admin/group" in page and "group root" in page
    assert 'data-group-address="same"' in page
    assert 'action="/admin/group/invite"' in ca.get("/admin/group").text

    _move(net, b, "b2.test")
    page = cb.get("/admin/group").text
    assert 'data-group-address="changed"' in page and "http://b2.test" in page
    r = cb.post("/admin/group/address", data={"csrf": token(cb, "/admin/group"), "url": "http://b2.test",
                                              "name": "", "password": "wrong"})
    assert r.status_code == 400  # re-authentication first
    r = cb.post("/admin/group/address", data={"csrf": token(cb, "/admin/group"), "url": "http://b2.test",
                                              "name": "", "password": PW})
    assert r.status_code == 200 and "data-group-address-sent" in r.text

    page = ca.get("/admin/group").text
    assert f'data-held-update="{b.node_id}"' in page and "http://b2.test" in page
    r = ca.post(f"/admin/group/address/{b.node_id}/apply", data={"csrf": token(ca, "/admin/group"),
                                                                 "root_passphrase": "wrong passphrase here",
                                                                 "password": PW})
    assert r.status_code in (400, 403) and f'data-held-update="{b.node_id}"' in ca.get("/admin/group").text
    r = ca.post(f"/admin/group/address/{b.node_id}/apply", data={"csrf": token(ca, "/admin/group"),
                                                                 "root_passphrase": ROOT_PW, "password": PW})
    assert r.status_code == 200 and "data-group-address-applied" in r.text
    assert re.search(r'data-held-update="', r.text) is None
    assert 'data-group-address="same"' in cb.get("/admin/group").text
    with c.session() as s:
        assert s.get(GroupMember, (groups.load(s).group_id, b.node_id)).url == "http://b2.test"
    assert "group_member_updated" in _actions(a) and "group_member_update_sent" in _actions(b)


def test_dismiss_a_held_update(trio):
    a, b, c = trio
    with b.session() as s:
        update = groups.member_update(s, b.settings, groups.load(s), b.identity, name="Laptop")
    with a.session() as s:
        assert groups.hold_member_update(s, a.settings, groups.load(s), a.node_id, update, b.node_id) == "held"
        assert groups.hold_member_update(s, a.settings, groups.load(s), a.node_id, update, b.node_id) == "known"
        s.commit()
    ca = _browser(a)
    assert f'data-held-update="{b.node_id}"' in ca.get("/admin/group").text
    r = ca.post(f"/admin/group/address/{b.node_id}/dismiss", data={"csrf": token(ca, "/admin/group")})
    assert r.status_code == 303 and 'data-held-update' not in ca.get("/admin/group").text
    assert members(a)[b.node_id]["body"]["member"]["name"] == "Node B"


def test_leave_via_admin_page_tells_members(net):
    a, b = net.node("a"), net.node("b")
    create(a)
    finish_creation(a)
    enrol(a, b)
    cb = _browser(b)
    r = cb.post("/admin/group/leave", data={"csrf": token(cb, "/admin/group"), "password": PW})
    assert r.status_code == 303
    assert b.node_id not in members(a)


def test_ops_are_advertised():
    from atlas import group_channel
    assert {"push", "member_update"} <= set(group_routes.CALL_OPS)
    identity = type("Identity", (), {"node_id": "x", "key_id": "y"})
    assert {"push", "member_update"} <= set(group_channel.hello_result(identity)["ops"])
