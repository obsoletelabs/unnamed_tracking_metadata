"""Signed administrative delegation (CONTRACT-C10 §6, §11 A12-f/h/l/m/n/t/u): grants issued by the root or by a
`grant.issue` delegate, distributed through `state`, capped and gated by local policy, revoked with a cascade, and
recorded in the audit with their provenance. Every key, passphrase and account is a synthetic runtime value."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import accounts, cli, group_channel, group_cli, groups, runtime
from atlas.models import AuditEntry, GroupCrl, GroupGrant, User, UserSession

from .channel_harness import group_routes_pull, link, user
from .group_harness import ROOT_PW, Dispatcher, create, enrol, fast_kdf, finish_creation, make_node
from .test_web import PW, signup, token


def setp(node, **kv) -> None:
    """Change this node's local policy the way /admin/group/policy does (runtime settings, then refresh)."""
    with node.session() as s:
        runtime.save(s, node.st.base, None, kv)
        s.commit()
        node.st.refresh(s, force=True)


def uid_of_user(node, user_id: str) -> str | None:
    with node.session() as s:
        link_ = group_channel.local_link(s, groups.load(s).group_id, user_id)
        return link_.account_uid if link_ else None


def grant(node, uid: str, role: str, nodes: list[str], *, as_root: bool = True, days: int = 30, **kw) -> dict:
    with node.session() as s:
        group = groups.load(s)
        root = groups.unlock_root(s, node.settings, group, ROOT_PW) if as_root else None
        obj = groups.issue_grant(s, node.settings, group, node.identity, account_uid=uid, role=role, nodes=nodes,
                                 days=days, root=root, **kw)
        s.commit()
        return obj


def revoke(node, serial: str, *, as_root: bool = True) -> dict:
    with node.session() as s:
        group = groups.load(s)
        root = groups.unlock_root(s, node.settings, group, ROOT_PW) if as_root else None
        crl = groups.revoke_grant(s, node.settings, group, node.identity, serial, root=root)
        s.commit()
        return crl


def rights(node, user_id: str, login: UserSession | None = None, mfa_done: bool = True) -> accounts.Rights:
    """What `deps.page` pins for this user on this node, for a local password session (MFA step done or not)."""
    with node.session() as s:
        u = s.get(User, user_id)
        if login is None:
            login = UserSession(id="x", user_id=user_id, created_at=groups.utcnow(),
                                expires_at=groups.utcnow() + timedelta(days=1),
                                mfa_verified_at=groups.utcnow() if mfa_done else None)
        return accounts.effective_rights(s, node.settings, u, login, own_node=node.node_id)


@pytest.fixture
def trio(tmp_path, monkeypatch):
    """a: the root holder. b: an ordinary member that may sign `moderator`/`curator` grants for b and c. c: an
    ordinary member. `bob` on b and `carol` on c are linked to `alice`'s group account on a."""
    fast_kdf(monkeypatch)
    group_channel._HELLO.clear()
    net = Dispatcher()
    a, b, c = (make_node(tmp_path, net, n) for n in "abc")
    create(a)
    finish_creation(a)
    enrol(a, b, [*groups.DEFAULT_SCOPES, "grant.issue"],
          groups.default_delegate(0, ["moderator", "curator"], [b.node_id, c.node_id]))
    enrol(a, c)
    for n in (a, b, c):
        group_routes_pull(n)
    for n in (a, b, c):
        user(n, "owner")  # the first account on a node is its local administrator
    alice, bob, carol = user(a, "alice"), user(b, "bob"), user(c, "carol")
    uid = link(a, alice, b, bob)
    assert link(a, alice, c, carol) == uid
    yield a, b, c, uid, {"alice": alice, "bob": bob, "carol": carol}
    for n in (a, b, c):
        n.close()


def sync_state(*nodes) -> None:
    for _ in range(2):
        for n in nodes:
            group_routes_pull(n)


def test_root_grant_reaches_member_and_is_capped_by_default(trio):
    a, b, c, uid, who = trio
    obj = grant(a, uid, "admin", [b.node_id], account_label="Alice", note="synthetic")
    sync_state(a, b, c)
    with b.session() as s:
        row = s.get(GroupGrant, (groups.load(s).group_id, obj["body"]["serial"]))
        assert row is not None and row.account_uid == uid and row.role == "admin" and row.held_reason is None
        assert row.issuer == "root" and row.nodes == [b.node_id]
    with a.session() as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "group_grant_issued",
                                                  AuditEntry.target == obj["body"]["serial"])).one()
    # H15: group_max_delegated_role defaults to none, so the grant changes nothing on b
    r = rights(b, who["bob"])
    assert not r.admin and not r.review and not r.moderate and not r.sources
    setp(b, group_max_delegated_role="admin")
    r = rights(b, who["bob"])
    assert r.admin and r.review and r.moderate and not r.local_admin and not r.group_admin
    assert r.sources == [{"serial": obj["body"]["serial"], "role": "admin", "issuer": "root"}]
    # nodes scoping: the grant names b only, so carol on c gets nothing even with the cap open
    setp(c, group_max_delegated_role="admin")
    assert not rights(c, who["carol"]).admin


def test_l_role_conflicts_union_cap_denied_and_local_admin_never_reduced(trio):
    a, b, c, uid, who = trio
    mod = grant(a, uid, "moderator", [b.node_id])
    adm = grant(a, uid, "admin", [b.node_id])
    sync_state(a, b)
    setp(b, group_max_delegated_role="admin")
    r = rights(b, who["bob"])
    assert r.admin and {g["serial"] for g in r.sources} == {mod["body"]["serial"], adm["body"]["serial"]}
    # the cap clamps the union
    setp(b, group_max_delegated_role="curator")
    r = rights(b, who["bob"])
    assert r.review and r.moderate and not r.admin
    setp(b, group_max_delegated_role="moderator")
    r = rights(b, who["bob"])
    assert r.moderate and not r.review and not r.admin
    # two grants, one revoked: the other one still applies
    setp(b, group_max_delegated_role="admin")
    revoke(a, adm["body"]["serial"])
    sync_state(a, b)
    r = rights(b, who["bob"])
    assert r.moderate and not r.admin and [g["serial"] for g in r.sources] == [mod["body"]["serial"]]
    # group_denied_accounts wins over every grant
    setp(b, group_denied_accounts=uid)
    r = rights(b, who["bob"])
    assert not r.moderate and not r.sources
    # a local admin is never reduced by the group, even when denied
    with b.session() as s:
        s.get(User, who["bob"]).role = "admin"
        s.commit()
    r = rights(b, who["bob"])
    assert r.admin and r.local_admin
    with b.session() as s:
        assert s.get(User, who["bob"]).role == "admin"


def test_delegated_rights_need_this_nodes_own_mfa_by_default(trio):
    a, b, c, uid, who = trio
    grant(a, uid, "admin", [b.node_id])
    sync_state(a, b)
    setp(b, group_max_delegated_role="admin")
    r = rights(b, who["bob"], mfa_done=False)
    assert not r.admin and "two-factor" in r.banner
    setp(b, group_delegated_requires_mfa=False)
    assert rights(b, who["bob"], mfa_done=False).admin


def test_h_revoked_or_expired_grant_replay_has_no_effect(trio, monkeypatch):
    a, b, c, uid, who = trio
    obj = grant(a, uid, "admin", [b.node_id], days=2)
    sync_state(a, b)
    setp(b, group_max_delegated_role="admin")
    assert rights(b, who["bob"]).admin
    revoke(a, obj["body"]["serial"])
    sync_state(a, b)
    assert not rights(b, who["bob"]).admin
    # replaying the original signed grant changes nothing: it is known, and the CRL wins (monotone)
    with b.session() as s:
        group = groups.load(s)
        assert groups.ingest_grant(s, group, obj, groups.utcnow()) == "known"
        groups.rebuild(s, group, b.node_id)
        s.commit()
        assert s.scalars(select(GroupCrl).where(GroupCrl.group_id == group.group_id)).first() is not None
    assert not rights(b, who["bob"]).admin
    # expiry: a fresh grant stops counting once its not_after passes, with no CRL at all
    fresh = grant(a, uid, "admin", [b.node_id], days=1)
    sync_state(a, b)
    assert rights(b, who["bob"]).admin
    future = groups.utcnow() + timedelta(days=2)
    monkeypatch.setattr(groups, "utcnow", lambda: future)
    with b.session() as s:
        tr = groups.trust(s, groups.load(s), future, own_node=b.node_id)
        assert tr.grant_valid(fresh).reason == "expired"
        assert tr.grants_for(uid, b.node_id) == []


def test_forged_grant_is_not_stored(trio):
    a, b, c, uid, who = trio
    obj = grant(a, uid, "moderator", [b.node_id])
    forged = json.loads(json.dumps(obj))
    forged["body"]["role"] = "admin"  # the signature no longer covers the body
    forged["body"]["serial"] = forged["body"]["serial"].rsplit("-", 1)[0] + "-" + "0" * 16
    forged["id"] = f"grant:{forged['body']['serial']}"
    with b.session() as s:
        group = groups.load(s)
        assert groups.ingest_grant(s, group, forged, groups.utcnow()) in ("bad_signature", "malformed")
        assert s.get(GroupGrant, (group.group_id, forged["body"]["serial"])) is None
        # a node without grant.issue cannot sign one either (c is an ordinary member)
        from atlas import group_crypto as gc

        body = {**obj["body"], "serial": f"{c.node_id}/9", "issuer": c.node_id,
                "parent_serial": groups.load(s).self_serial}
        bogus = gc.make_object("group.grant", group.group_id, f"grant:{body['serial']}", body,
                               {"kind": "node", "id": c.node_id, "key_id": c.identity.key_id}, c.identity.private)
        assert groups.ingest_grant(s, group, bogus, groups.utcnow()) == "issuer_cannot_grant"
        s.commit()


def test_u_delegate_cannot_grant_group_admin_or_star_or_outside_its_nodes(trio):
    a, b, c, uid, who = trio
    for role, nodes, msg in (("group_admin", [c.node_id], "only the group root"),
                             ("moderator", ["*"], "only the group root"),
                             ("admin", [c.node_id], "may grant only"),
                             ("moderator", [a.node_id], "may not grant for")):
        with pytest.raises(groups.GroupError, match=msg):
            grant(b, uid, role, nodes, as_root=False)
    ok = grant(b, uid, "moderator", [c.node_id], as_root=False)
    assert ok["body"]["issuer"] == b.node_id and ok["body"]["parent_serial"]
    sync_state(b, a, c)
    setp(c, group_max_delegated_role="admin")
    r = rights(c, who["carol"])
    assert r.moderate and not r.review and r.sources[0]["issuer"] == b.node_id
    # a delegate-signed grant edited to escalate is refused by every reader (signature or scope)
    from atlas import group_crypto as gc

    with c.session() as s:
        group = groups.load(s)
        tr = groups.trust(s, group, own_node=c.node_id)
        body = {**ok["body"], "serial": f"{b.node_id}/{groups.parse_serial(ok['body']['serial']).n + 1}",
                "role": "admin"}
        esc = gc.make_object("group.grant", group.group_id, f"grant:{body['serial']}", body,
                             {"kind": "node", "id": b.node_id, "key_id": b.identity.key_id}, b.identity.private)
        assert tr.grant_valid(esc).reason == "role_escalation"
        body = {**body, "role": "moderator", "nodes": [a.node_id]}
        esc = gc.make_object("group.grant", group.group_id, f"grant:{body['serial']}", body,
                             {"kind": "node", "id": b.node_id, "key_id": b.identity.key_id}, b.identity.private)
        assert tr.grant_valid(esc).reason == "node_escalation"


def test_t_revoking_issuer_grant_cascades_to_child_grants(trio):
    a, b, c, uid, who = trio
    with a.session() as s:
        boss_uid = group_channel.mint_account(s, a.settings, groups.load(s), a.identity, s.get(User, who["alice"]))
        s.commit()
        boss_uid = boss_uid.account_uid
    assert boss_uid == uid
    ga = grant(a, uid, "group_admin", [b.node_id])
    sync_state(a, b, c)
    child = grant(b, uid, "curator", [c.node_id], as_root=False, authorized_by_grant=ga["body"]["serial"])
    assert child["body"]["authorized_by_grant"] == ga["body"]["serial"]
    assert child["body"]["not_after"] <= ga["body"]["not_after"]
    sync_state(b, a, c)
    setp(c, group_max_delegated_role="admin")
    assert rights(c, who["carol"]).review
    revoke(a, ga["body"]["serial"])
    sync_state(a, b, c)
    with c.session() as s:
        tr = groups.trust(s, groups.load(s), own_node=c.node_id)
        assert tr.grant_valid(tr.grants[child["body"]["serial"]]).reason == "authorizer_revoked"
    assert not rights(c, who["carol"]).review
    with a.session() as s:
        e = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_grant_revoked")).one()
        assert e.detail["cascaded"] == [child["body"]["serial"]]


def test_revoking_the_delegate_node_cascades_to_its_grants(trio):
    a, b, c, uid, who = trio
    child = grant(b, uid, "moderator", [c.node_id], as_root=False)
    sync_state(b, a, c)
    setp(c, group_max_delegated_role="admin")
    assert rights(c, who["carol"]).moderate
    with a.session() as s:
        group = groups.load(s)
        groups.revoke_node(s, a.settings, group, a.identity, b.node_id, "removed",
                           root=groups.unlock_root(s, a.settings, group, ROOT_PW))
        s.commit()
    sync_state(a, c)
    with c.session() as s:
        tr = groups.trust(s, groups.load(s), own_node=c.node_id)
        assert tr.grant_valid(tr.grants[child["body"]["serial"]]).reason in ("issuer_revoked", "parent_revoked")
    assert not rights(c, who["carol"]).moderate


def test_s_stale_view_pauses_delegated_rights(trio, monkeypatch):
    a, b, c, uid, who = trio
    grant(a, uid, "admin", [b.node_id])
    sync_state(a, b)
    setp(b, group_max_delegated_role="admin")
    assert rights(b, who["bob"]).admin
    future = groups.utcnow() + timedelta(hours=25)  # every view b holds is now older than 24 h (H28)
    monkeypatch.setattr(groups, "utcnow", lambda: future)
    r = rights(b, who["bob"])
    assert not r.admin and "paused" in r.banner


@pytest.fixture
def web_pair(tmp_path, monkeypatch):
    """Two nodes; `bob` on b holds an admin grant from the root on a; b's cap is admin and its MFA rule is off."""
    fast_kdf(monkeypatch)
    group_channel._HELLO.clear()
    net = Dispatcher()
    a, b = make_node(tmp_path, net, "a"), make_node(tmp_path, net, "b")
    create(a)
    finish_creation(a)
    enrol(a, b)
    for n in (a, b):
        group_routes_pull(n)
    cb = TestClient(b.app, base_url="http://b.test", follow_redirects=False)
    signup(cb, "boss")  # the first account is the local administrator
    cb.cookies.clear()
    signup(cb, "bob")
    alice = user(a, "alice")
    with b.session() as s:
        bob = s.scalars(select(User).where(User.username == "bob")).one().id
    uid = link(a, alice, b, bob)
    yield a, b, cb, uid, bob
    for n in (a, b):
        n.close()


def test_f_delegated_admin_cannot_change_group_policy_keys_restore_or_tokens(web_pair):
    a, b, cb, uid, bob = web_pair
    obj = grant(a, uid, "admin", [b.node_id])
    sync_state(a, b)
    setp(b, group_max_delegated_role="admin", group_delegated_requires_mfa=False)
    assert cb.get("/admin").status_code == 200  # the grant opens ordinary administration
    r = cb.get("/admin/group")
    assert r.status_code == 403 and "Only an administrator of this node" in r.text
    assert cb.get("/admin/group/grants").status_code == 403  # grants are for local and group admins only
    csrf = token(cb, "/admin")
    for path, data in (("/admin/group/policy", {"group_max_delegated_role": "admin"}),
                       ("/admin/keys/rotate", {"password": PW, "confirm": "rotate", "followers_upgraded": "on"}),
                       ("/admin/tokens", {"name": "x", "scopes": ["admin"]})):
        r = cb.post(path, data={"csrf": csrf, **data})
        assert r.status_code == 403, (path, r.status_code)
    with b.session() as s:
        from atlas.models import Setting, Token

        assert s.get(Setting, "group_max_delegated_role").value == "admin"  # unchanged
        assert all("admin" not in t.scopes for t in s.scalars(select(Token)))
        assert b.identity.key_id == b.st.ctx.identity.key_id
    # and the delegated admin cannot make anyone a local administrator
    with b.session() as s:
        boss = s.scalars(select(User).where(User.username == "boss")).one()
        me = s.get(User, bob)
        accounts.pin_rights(s, me, rights(b, bob), None)
        with pytest.raises(accounts.AtlasError, match="local administrator"):
            accounts.set_role(s, me, boss, "user")
    assert obj["body"]["role"] == "admin"


def test_m_audit_provenance_on_delegated_actions(web_pair):
    a, b, cb, uid, bob = web_pair
    obj = grant(a, uid, "admin", [b.node_id])
    sync_state(a, b)
    setp(b, group_max_delegated_role="admin", group_delegated_requires_mfa=False)
    with b.session() as s:
        victim = accounts.register(s, b.settings, "victim", "synthetic-Pass-for-tests-4821")
        s.commit()
        victim_id = victim.id
    r = cb.post(f"/admin/users/{victim_id}", data={"csrf": token(cb, "/admin"), "role": "user", "status": "suspended"})
    assert r.status_code in (200, 303), r.text
    with b.session() as s:
        assert s.get(User, victim_id).status == "suspended"
        e = s.scalars(select(AuditEntry).where(AuditEntry.actor_id == bob, AuditEntry.target == victim_id)).one()
        assert e.detail["via"] == "group" and e.detail["grant_serial"] == obj["body"]["serial"]
        assert e.detail["session"] == "local" and e.detail["origin_node"] is None
    page = cb.get("/admin").text
    assert "Via group" in page


def test_n_moderator_grant_opens_reports_only(web_pair):
    a, b, cb, uid, bob = web_pair
    grant(a, uid, "moderator", [b.node_id])
    sync_state(a, b)
    assert cb.get("/review/reports").status_code in (403, 303)
    setp(b, group_max_delegated_role="admin", group_delegated_requires_mfa=False)
    assert cb.get("/review/reports").status_code == 200
    assert cb.get("/review").status_code in (403, 303)  # edits review stays with curators
    assert cb.get("/admin").status_code in (403, 303)


def test_cli_grant_grants_and_revoke_grant(trio, monkeypatch, capsys):
    a, b, c, uid, who = trio
    monkeypatch.setattr(group_cli, "_ask", lambda prompt, confirm=False: ROOT_PW)
    monkeypatch.setattr(group_cli, "_run", lambda fn: _run_on(a, fn))
    assert cli.main(["group", "grant", "--account", uid, "--role", "curator", "--nodes", b.node_id,
                           "--days", "7", "--label", "Alice"]) == 0
    out = capsys.readouterr().out
    serial = out.strip().rsplit(": ", 1)[1]
    assert serial.startswith("root/")
    assert cli.main(["group", "grants", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["serial"] for r in rows] == [serial] and rows[0]["status"] == "valid"
    assert cli.main(["group", "revoke-grant", serial]) == 0
    capsys.readouterr()
    with a.session() as s:
        tr = groups.trust(s, groups.load(s), own_node=a.node_id)
        assert serial in tr.revoked
        assert {e.action for e in s.scalars(select(AuditEntry).where(AuditEntry.target == serial))} == {
            "group_grant_issued", "group_grant_revoked"}


def _run_on(node, fn) -> int:
    with node.session() as s:
        code = fn(s, node.settings, node.identity)
        s.commit()
        return code
