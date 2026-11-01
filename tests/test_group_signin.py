"""Cross-node sign-in (CONTRACT-C10 §5.4-§5.6, §11 A12-a/b/c/d/e/g/i/j/k/o/r/s/w/x and the email collision).

Node a is the identity provider (alice signs in there with her password); node b is the relying party (bob is
alice's linked account there). Every sign-in goes through the real browser flow: the start ticket or the login
button, the sealed authreq cross-site POST to a, the consent page, the sealed assertion cross-site POST back to b,
and the finish GET that checks the `atlas_grp_bind` cookie. Keys, passwords and accounts are synthetic."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import accounts, group_channel, groups, mfa, runtime
from atlas import group_crypto as gc
from atlas.models import (
    AuditEntry,
    GroupAccountLink,
    GroupAccountNode,
    GroupAssertionSeen,
    GroupChange,
    User,
    UserSession,
)

from .channel_harness import group_routes_pull, link, rounds
from .group_harness import ROOT_PW, Dispatcher, create, enrol, fast_kdf, finish_creation, make_node
from .test_group_link_web import hidden
from .test_web import PW, login, signup, token


def setp(node, **kv) -> None:
    with node.session() as s:
        runtime.save(s, node.st.base, None, kv)
        s.commit()
        node.st.refresh(s, force=True)


def relogin(client: TestClient, name: str) -> None:
    client.cookies.clear()
    r = login(client, name)
    assert r.status_code == 303, r.text


def browser(node, host: str) -> TestClient:
    return TestClient(node.app, base_url=f"http://{host}.test", follow_redirects=False)


def uid_by_name(node, name: str) -> str:
    with node.session() as s:
        return s.scalars(select(User).where(User.username == name)).one().id


@pytest.fixture
def fed(tmp_path, monkeypatch):
    """a and b in one group, alice@a linked to bob@b and synced (b knows a holds the account)."""
    fast_kdf(monkeypatch)
    group_channel._HELLO.clear()
    monkeypatch.setattr(mfa, "_counter", lambda: 100000)
    net = Dispatcher()
    a, b = make_node(tmp_path, net, "a"), make_node(tmp_path, net, "b")
    create(a)
    finish_creation(a)
    enrol(a, b)
    for n in (a, b):
        group_routes_pull(n)
    ca, cb = browser(a, "a"), browser(b, "b")
    signup(ca, "admina")  # the first account on each node is its local administrator
    signup(cb, "adminb")
    ca.cookies.clear()
    cb.cookies.clear()
    signup(ca, "alice")
    signup(cb, "bob")
    uid = link(a, uid_by_name(a, "alice"), b, uid_by_name(b, "bob"))
    rounds(a, b)
    with b.session() as s:  # start with no session for bob on b (signup opened one)
        accounts.end_all_sessions(s, uid_by_name(b, "bob"))
        s.commit()
    with b.session() as s:
        held = s.get(GroupAccountNode, (groups.load(s).group_id, uid, a.node_id))
        assert held is not None and held.status == "linked"
    yield net, a, b, ca, uid
    for n in (a, b):
        n.close()


def open_signed_in(ca, a, b, rp: TestClient) -> str:
    """alice on a clicks 'Open b signed in': the start ticket is auto-posted cross-site to b, which parks it and
    sends the sealed authreq back to a. Returns the sealed authreq."""
    r = ca.post("/me/linked-nodes/open", data={"csrf": token(ca, "/me/linked-nodes"), "node_id": b.node_id})
    assert r.status_code == 200 and 'action="http://b.test/group/start"' in r.text, r.text
    return hidden(r.text, "ticket")


def post_ticket(b, rp: TestClient, ticket: str):
    r = browser(b, "b").post("/group/start", data={"ticket": ticket})  # cross-site: no cookies
    if r.status_code != 303:
        return r
    return rp.get(r.headers["location"])


def consent(ca, request: str, decision: str = "approve"):
    r = TestClient(ca.app, base_url=str(ca.base_url)).post("/group/authorize", data={"request": request},
                                                          follow_redirects=False)  # cross-site: no cookies
    assert r.status_code == 303, r.text
    page = ca.get(r.headers["location"])
    if page.status_code != 200 or decision is None:
        return page
    r = ca.post("/group/authorize/continue", data={"csrf": hidden(page.text, "csrf"), "h": hidden(page.text, "h"),
                                                   "decision": decision})
    return r


def deliver(b, rp: TestClient, assertion: str):
    r = browser(b, "b").post("/group/callback", data={"assertion": assertion})  # cross-site: no cookies
    if r.status_code != 303:
        return r
    return rp.get(r.headers["location"])


def signin(ca, a, b, rp: TestClient | None = None):
    """The whole IdP-initiated flow; returns (rp browser, final response at b)."""
    rp = rp or browser(b, "b")
    started = post_ticket(b, rp, open_signed_in(ca, a, b, rp))
    assert started.status_code == 200 and f'action="http://{a.name}.test/group/authorize"' in started.text
    assert "atlas_grp_bind=" in started.headers["set-cookie"]
    approved = consent(ca, hidden(started.text, "request"))
    assert approved.status_code == 200 and 'action="http://b.test/group/callback"' in approved.text, approved.text
    return rp, deliver(b, rp, hidden(approved.text, "assertion"))


def sessions_of(node, user_id: str) -> list[UserSession]:
    with node.session() as s:
        return list(s.scalars(select(UserSession).where(UserSession.user_id == user_id)))


def test_a_signin_happy_path_creates_a_local_session_with_provenance(fed):
    net, a, b, ca, uid = fed
    relogin(ca, "alice")
    rp, done = signin(ca, a, b)
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert "atlas_session=" in done.headers["set-cookie"]
    bob = uid_by_name(b, "bob")
    [row] = sessions_of(b, bob)
    with b.session() as s:
        group_id = groups.load(s).group_id
        assert row.via_group == group_id and row.origin_node == a.node_id and row.amr == ["pwd"]
        assert row.assertion_id and row.mfa_verified_at is None
        assert s.scalar(select(func.count()).select_from(GroupAssertionSeen)) >= 2  # ticket nonce + jti
        e = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_signin")).one()
        assert e.target == bob and e.detail["origin_node"] == a.node_id and e.detail["assertion_id"] == row.assertion_id
        assert e.detail["provisioned"] is False
    me = rp.get("/me")
    assert me.status_code == 200 and "bob" in me.text
    assert "signed in through" in rp.get("/").text.lower() or "through Node A" in rp.get("/").text


def test_x_assertion_issued_is_audited_on_idp_and_security_lists_activity(fed):
    net, a, b, ca, uid = fed
    relogin(ca, "alice")
    rp, done = signin(ca, a, b)
    assert done.status_code == 303
    with a.session() as s:
        e = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_assertion_issued")).one()
        assert e.detail["aud_node"] == b.node_id and e.detail["purpose"] == "signin"
    page = ca.get("/me/security").text
    assert "This node confirmed you to another node (sign-in)" in page and "Node B" in page
    page_b = rp.get("/me/security").text
    assert "Signed in here through another node" in page_b and "Node A" in page_b


def test_b_mfa_on_b_is_required_unless_remote_otp_is_trusted_and_fresh(fed):
    net, a, b, ca, uid = fed
    bob = uid_by_name(b, "bob")
    local = browser(b, "b")
    relogin(local, "bob")
    started = local.post("/me/security/start", data={"csrf": token(local, "/me/security"), "password": PW})
    secret = re.search(r"Setup key: <code>([A-Z2-7]+)</code>", started.text).group(1)
    assert local.post("/me/security/enable", data={"csrf": token(local, "/me/security"),
                                                   "code": mfa.totp_code(secret, 100000)}).status_code == 200
    relogin(ca, "alice")
    rp, done = signin(ca, a, b)
    assert done.status_code == 303
    assert rp.get("/me").headers["location"] == "/mfa"  # b's own factor is still required
    # trust_remote_mfa: still required while a's session has no otp
    setp(b, group_trust_remote_mfa=True)
    rp, done = signin(ca, a, b)
    assert rp.get("/me").headers["location"] == "/mfa"
    # alice's session on a now carries otp: the fresh remote otp stands in for b's factor
    with a.session() as s:
        alice = uid_by_name(a, "alice")
        for row in s.scalars(select(UserSession).where(UserSession.user_id == alice)):
            row.mfa_verified_at = row.created_at
        s.commit()
    rp, done = signin(ca, a, b)
    assert rp.get("/me").status_code == 200
    rows = [r for r in sessions_of(b, bob) if r.via_group]
    assert any(r.mfa_verified_at is not None and "otp" in r.amr for r in rows)


def test_c_auto_provision_off_refuses_and_on_creates_a_plain_user_without_password(fed):
    net, a, b, ca, uid = fed
    signup(browser(a, "a"), "dora")
    dora_browser = browser(a, "a")
    relogin(dora_browser, "dora")
    with a.session() as s:  # dora's account becomes known as held by a (linked on a only)
        group_channel.mint_account(s, a.settings, groups.load(s), a.identity, s.get(User, uid_by_name(a, "dora")))
        s.commit()
    rounds(a, b)
    setp(b, group_signin_public=True)
    rp = browser(b, "b")
    page = rp.get("/login")
    assert "Node A" in page.text  # one button per member holding an account linked here; it names no account
    started = rp.post("/group/signin", data={"csrf": token(rp), "node_id": a.node_id})
    assert started.status_code == 200
    approved = consent(dora_browser, hidden(started.text, "request"))
    done = deliver(b, rp, hidden(approved.text, "assertion"))
    assert done.status_code == 400 and "No account here is linked" in done.text
    with b.session() as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "group_signin_refused")).first() is not None
        assert s.scalars(select(User).where(User.username.like("dora%"))).first() is None
    setp(b, group_auto_provision=True)
    rp = browser(b, "b")
    started = rp.post("/group/signin", data={"csrf": token(rp), "node_id": a.node_id})
    approved = consent(dora_browser, hidden(started.text, "request"))
    done = deliver(b, rp, hidden(approved.text, "assertion"))
    assert done.status_code == 303, done.text
    with b.session() as s:
        dora = s.scalars(select(User).where(User.username.like("dora%"))).one()
        assert dora.role == "user" and dora.password_hash == "!" and dora.email is None
        assert s.scalars(select(GroupAccountLink).where(GroupAccountLink.user_id == dora.id)).one().minted_here is False


def test_d_return_url_comes_only_from_the_certificate(fed):
    net, a, b, ca, uid = fed
    relogin(ca, "alice")
    rp = browser(b, "b")
    started = post_ticket(b, rp, open_signed_in(ca, a, b, rp))
    assert 'action="http://a.test/group/authorize"' in started.text
    approved = consent(ca, hidden(started.text, "request"))
    # the IdP posts back to b's certificate address, whatever the request or the browser says
    assert 'action="http://b.test/group/callback"' in approved.text
    with a.session() as s:
        cert = groups.trust(s, groups.load(s), own_node=a.node_id).in_force(b.node_id)
        assert cert["body"]["member"]["url"] == "http://b.test"
    done = deliver(b, rp, hidden(approved.text, "assertion"))
    assert done.headers["location"] == "/"  # next_url is fixed by b's own state row, never by a parameter
    r = browser(b, "b").get("/group/start/continue?h=x&next=https://evil.example.invalid/")
    assert r.status_code == 404


def test_e_compromised_idp_signin_gets_user_rights_only_by_default(fed):
    """a signs an assertion for adminb's account: under the defaults a federated session holds no admin rights."""
    net, a, b, ca, uid = fed
    with b.session() as s:  # make bob a local administrator: the session from a must still not be one
        s.get(User, uid_by_name(b, "bob")).role = "admin"
        s.commit()
    relogin(ca, "alice")
    rp, done = signin(ca, a, b)
    assert done.status_code == 303
    assert rp.get("/admin").status_code == 403
    assert "local password" in rp.get("/").text
    with b.session() as s:
        assert s.get(User, uid_by_name(b, "bob")).role == "admin"  # never changed, never auto-admin
    # federated_privilege=allow opts in (H15); local_mfa needs b's own factor
    setp(b, group_federated_privilege="local_mfa")
    assert rp.get("/admin").status_code == 403
    setp(b, group_federated_privilege="allow")
    assert rp.get("/admin").status_code == 200


def test_g_revocation_unlink_and_signin_off_end_federated_sessions(fed):
    net, a, b, ca, uid = fed
    bob = uid_by_name(b, "bob")
    relogin(ca, "alice")
    rp, _ = signin(ca, a, b)
    local = browser(b, "b")
    relogin(local, "bob")
    assert rp.get("/me").status_code == 200
    # unlink on a: the account.unlink entry reaches b and ends a's sessions for bob; the local one stays
    with a.session() as s:
        group_channel.unlink(s, a.settings, a.identity, s.get(User, uid_by_name(a, "alice")))
        s.commit()
    rounds(a, b)
    assert [r.via_group for r in sessions_of(b, bob)] == [None]
    assert rp.get("/me").status_code in (303, 401)
    assert local.get("/me").status_code == 200
    with b.session() as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "group_sessions_ended")).first() is not None


def test_g_signin_turned_off_ends_federated_sessions(fed):
    net, a, b, ca, uid = fed
    bob = uid_by_name(b, "bob")
    relogin(ca, "alice")
    rp, _ = signin(ca, a, b)
    assert rp.get("/me").status_code == 200
    setp(b, group_signin=False)
    assert rp.get("/me").status_code in (303, 401)
    assert [r for r in sessions_of(b, bob) if r.via_group] == []
    with b.session() as s:
        e = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_sessions_ended")).one()
        assert e.detail["reason"] == "signin_off"


def test_g_crl_with_cascading_reason_ends_sessions_asserted_by_that_node(fed, tmp_path):
    """c is the identity provider here; the root (a) removes c and b ends c's sessions on CRL apply."""
    net, a, b, ca, uid = fed
    c = make_node(tmp_path, net, "c")
    try:
        enrol(a, c)
        for n in (a, b, c):
            group_routes_pull(n)
        cc = browser(c, "c")
        signup(cc, "adminc")
        cc.cookies.clear()
        signup(cc, "carol")
        bob = uid_by_name(b, "bob")
        # carol@c joins alice's group account (link c to a), so c also holds it and may assert for bob
        link(a, uid_by_name(a, "alice"), c, uid_by_name(c, "carol"))
        rounds(a, b, c)
        relogin(cc, "carol")
        rp, done = signin(cc, c, b)
        assert done.status_code == 303, done.text
        [row] = [r for r in sessions_of(b, bob) if r.via_group]
        assert row.origin_node == c.node_id
        with a.session() as s:
            group = groups.load(s)
            groups.revoke_node(s, a.settings, group, a.identity, c.node_id, "removed",
                               root=groups.unlock_root(s, a.settings, group, ROOT_PW))
            s.commit()
        group_routes_pull(b)
        assert [r for r in sessions_of(b, bob) if r.via_group] == []
        assert rp.get("/me").status_code in (303, 401)
    finally:
        c.close()


def test_i_assertion_replay_wrong_aud_state_and_ttl_refused(fed):
    net, a, b, ca, uid = fed
    relogin(ca, "alice")
    rp = browser(b, "b")
    started = post_ticket(b, rp, open_signed_in(ca, a, b, rp))
    approved = consent(ca, hidden(started.text, "request"))
    assertion = hidden(approved.text, "assertion")
    assert deliver(b, rp, assertion).status_code == 303
    # replay: the state row is consumed and the jti seen
    replay = deliver(b, rp, assertion)
    assert replay.status_code == 400
    # wrong aud: an assertion sealed to b but naming another audience never parks
    with a.session() as s:
        group = groups.load(s)
        a_body = group_channel.sign_assertion(s, group, a.identity, aud_node="0" * 16, uid=uid, nonce="n" * 26,
                                              state_hash="s" * 43, auth_time=groups.utcnow(), amr=["pwd"],
                                              username_hint="x", purpose="signin")
        member = groups.trust(s, group, own_node=a.node_id).in_force(b.node_id)["body"]["member"]
        sealed = gc.seal_request(member["box_public_key"], group.group_id, b.node_id, a_body)
        s.rollback()
    assert browser(b, "b").post("/group/callback", data={"assertion": json.dumps(sealed)}).status_code == 404
    # state mismatch: a valid signin assertion for a state b never issued
    with a.session() as s:
        group = groups.load(s)
        forged = group_channel.sign_assertion(s, group, a.identity, aud_node=b.node_id, uid=uid, nonce="n" * 26,
                                              state_hash="s" * 43, auth_time=groups.utcnow(), amr=["pwd"],
                                              username_hint="x", purpose="signin")
        sealed = gc.seal_request(member["box_public_key"], group.group_id, b.node_id, forged)
        s.rollback()
    r = deliver(b, rp, json.dumps(sealed))
    assert r.status_code == 400 and "expired or was already used" in r.text
    # binding mismatch: a fresh flow finished in a browser without the binding cookie
    started = post_ticket(b, rp, open_signed_in(ca, a, b, rp))
    approved = consent(ca, hidden(started.text, "request"))
    stranger = browser(b, "b")
    r = deliver(b, stranger, hidden(approved.text, "assertion"))
    assert r.status_code == 400 and "not started in this browser" in r.text
    # TTL > 120 s: an over-long assertion is refused even with valid state and binding
    started = post_ticket(b, rp, open_signed_in(ca, a, b, rp))
    with a.session() as s:
        group = groups.load(s)
        orig = group_channel.ASSERTION_TTL
        group_channel.ASSERTION_TTL = timedelta(seconds=600)
        try:
            approved = consent(ca, hidden(started.text, "request"))
        finally:
            group_channel.ASSERTION_TTL = orig
    r = deliver(b, rp, hidden(approved.text, "assertion"))
    assert r.status_code == 400 and "expired" in r.text
    with b.session() as s:
        assert s.scalar(select(func.count()).select_from(UserSession).where(UserSession.via_group.isnot(None))) == 1


def test_j_assertion_from_another_group_is_refused(fed, tmp_path):
    net, a, b, ca, uid = fed
    x, y = make_node(tmp_path, net, "x"), make_node(tmp_path, net, "y")
    try:
        create(x, name="Other group")
        finish_creation(x)
        enrol(x, y)
        with x.session() as s:
            other = groups.load(s)
            alien = group_channel.sign_assertion(s, other, x.identity, aud_node=b.node_id, uid=uid, nonce="n" * 26,
                                                 state_hash="s" * 43, auth_time=groups.utcnow(), amr=["pwd"],
                                                 username_hint="x", purpose="signin")
            s.rollback()
        with b.session() as s:
            group = groups.load(s)
            assert alien["group_id"] != group.group_id
            box = groups.trust(s, group, own_node=b.node_id).in_force(b.node_id)["body"]["member"]["box_public_key"]
        sealed = gc.seal_request(box, alien["group_id"], b.node_id, alien)
        assert browser(b, "b").post("/group/callback", data={"assertion": json.dumps(sealed)}).status_code == 404
        sealed = gc.seal_request(box, group.group_id, b.node_id, alien)  # re-labelled outside: still refused
        assert browser(b, "b").post("/group/callback", data={"assertion": json.dumps(sealed)}).status_code == 404
        assert [r for r in sessions_of(b, uid_by_name(b, "bob")) if r.via_group] == []
    finally:
        x.close()
        y.close()


def test_k_no_assertion_from_a_federated_session(fed):
    """A session opened through another node cannot vouch for the user anywhere (no chaining, T16)."""
    net, a, b, ca, uid = fed
    relogin(ca, "alice")
    rp, _ = signin(ca, a, b)
    r = rp.post("/me/linked-nodes/open", data={"csrf": token(rp, "/me/linked-nodes"), "node_id": a.node_id})
    assert r.status_code == 403 and "opened through another node" in r.text
    # the IdP side: alice's own session on a is marked as group-created, and a refuses to assert from it
    rp2 = browser(b, "b")
    started = post_ticket(b, rp2, open_signed_in(ca, a, b, rp2))
    with a.session() as s:
        for row in s.scalars(select(UserSession).where(UserSession.user_id == uid_by_name(a, "alice"))):
            row.via_group, row.origin_node = groups.load(s).group_id, b.node_id
        s.commit()
    page = consent(ca, hidden(started.text, "request"), decision=None)
    assert page.status_code == 403 and "opened through another node" in page.text
    with a.session() as s:
        assert s.scalar(select(func.count()).select_from(AuditEntry).where(
            AuditEntry.action == "group_assertion_issued")) == 1  # only the first, from the password session


def test_o_signout_all_propagates_only_where_consented(fed):
    from atlas import profile

    net, a, b, ca, uid = fed
    bob = uid_by_name(b, "bob")
    with b.session() as s:
        gid = groups.load(s).group_id
        profile.set_consent(s, bob, gid, profile.consent_fields(s, bob, gid), signout_propagation=False)
        s.commit()
    local_b = browser(b, "b")
    relogin(local_b, "bob")
    relogin(ca, "alice")
    r = ca.post("/me/security/signout-all", data={"csrf": token(ca, "/me/security")})
    assert r.status_code == 303
    assert sessions_of(a, uid_by_name(a, "alice")) == []
    rounds(a, b)
    assert len(sessions_of(b, bob)) == 1  # bob's consent on b says no: b keeps its session
    with b.session() as s:  # ...although the entry arrived and was applied
        [entry] = s.scalars(select(GroupChange).where(GroupChange.kind == "account.signout_all"))
        assert entry.disposition == "applied"
    with a.session() as s:
        e = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_signout_all")).one()
        assert e.detail["emitted"] is True
    with b.session() as s:
        profile.set_consent(s, bob, gid, profile.consent_fields(s, bob, gid), signout_propagation=True)
        s.commit()
    relogin(ca, "alice")
    assert ca.post("/me/security/signout-all", data={"csrf": token(ca, "/me/security")}).status_code == 303
    rounds(a, b)
    assert sessions_of(b, bob) == []
    assert local_b.get("/me").status_code in (303, 401)


def test_r_login_page_shows_no_group_by_default_and_tickets_are_single_use(fed):
    net, a, b, ca, uid = fed
    page = browser(b, "b").get("/login").text
    assert "/group/signin" not in page and "Node A" not in page
    rp = browser(b, "b")
    assert rp.post("/group/signin", data={"csrf": token(rp), "node_id": a.node_id}).status_code == 404
    setp(b, group_signin_public=True)
    page = browser(b, "b").get("/login").text
    assert 'action="/group/signin"' in page and "Node A" in page
    relogin(ca, "alice")
    ticket = open_signed_in(ca, a, b, rp)
    assert post_ticket(b, rp, ticket).status_code == 200
    replay = browser(b, "b").post("/group/start", data={"ticket": ticket})
    assert replay.status_code == 404 and replay.json() == browser(b, "b").post(
        "/group/start", data={"ticket": "{}"}).json()
    # forged: the sealed ticket body altered (signature broken) gives the same 404
    forged = json.loads(ticket)
    forged["ct"] = forged.get("ct", "")[:-4] + "AAAA" if "ct" in forged else forged
    assert browser(b, "b").post("/group/start", data={"ticket": json.dumps(forged)}).status_code == 404
    assert browser(b, "b").post("/group/start", data={}).status_code == 404


def test_s_stale_view_blocks_signin(fed, monkeypatch, tmp_path):
    """With a third member, b needs a view newer than 24 h from someone other than itself and the issuer."""
    net, a, b, ca, uid = fed
    c = make_node(tmp_path, net, "c")
    try:
        enrol(a, c)
        for n in (a, b, c):
            group_routes_pull(n)
        relogin(ca, "alice")
        later = groups.utcnow() + timedelta(hours=25)
        real = groups.utcnow
        rp = browser(b, "b")
        started = post_ticket(b, rp, open_signed_in(ca, a, b, rp))
        approved = consent(ca, hidden(started.text, "request"))
        assertion = hidden(approved.text, "assertion")
        with b.session() as s:  # age every third-party view on b past the limit
            from atlas.models import GroupView

            for v in s.scalars(select(GroupView)):
                v.issued_at = real() - timedelta(hours=25)
            s.commit()
        r = deliver(b, rp, assertion)
        assert r.status_code == 400 and "no recent view" in r.text
        assert later > real()
    finally:
        c.close()


def test_s_two_member_group_waives_freshness(fed):
    net, a, b, ca, uid = fed
    with b.session() as s:
        group = groups.load(s)
        f = groups.freshness(s, group, b.node_id, a.node_id, b.settings.group_view_max_age_hours)
        assert f.ok and f.waived
    relogin(ca, "alice")
    _, done = signin(ca, a, b)
    assert done.status_code == 303


def test_w_login_csrf_foreign_assertion_refused(fed):
    """mallory's valid sign-in assertion posted into the victim's browser does not sign the victim in as anyone."""
    net, a, b, ca, uid = fed
    relogin(ca, "alice")  # the attacker controls alice's account for this test
    attacker = browser(b, "b")
    started = post_ticket(b, attacker, open_signed_in(ca, a, b, attacker))
    approved = consent(ca, hidden(started.text, "request"))
    victim = browser(b, "b")
    victim.get("/login")
    r = deliver(b, victim, hidden(approved.text, "assertion"))
    assert r.status_code == 400 and "not started in this browser" in r.text
    assert "atlas_session" not in victim.cookies
    assert [r for r in sessions_of(b, uid_by_name(b, "bob")) if r.via_group] == []


def test_email_collision_never_links_or_signs_in(fed):
    """A local account on b with alice's email address is never matched: only (group, account_uid) links."""
    net, a, b, ca, uid = fed
    with a.session() as s:
        s.get(User, uid_by_name(a, "alice")).email = "shared@example.invalid"
        s.commit()
    eve_b = browser(b, "b")
    signup(eve_b, "eve")
    with b.session() as s:
        eve = s.get(User, uid_by_name(b, "eve"))
        eve.email = "shared@example.invalid"
        s.commit()
        eve_id = eve.id
    setp(b, group_auto_provision=True)
    relogin(ca, "alice")
    rp, done = signin(ca, a, b)
    assert done.status_code == 303
    with b.session() as s:
        [row] = [r for r in s.scalars(select(UserSession).where(UserSession.via_group.isnot(None)))]
        assert row.user_id == uid_by_name(b, "bob") != eve_id
        assert s.scalars(select(GroupAccountLink).where(GroupAccountLink.user_id == eve_id)).first() is None
    # the assertion never carries an email, so nothing about it can be matched
    with a.session() as s:
        e = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_assertion_issued")).one()
        assert "shared@example.invalid" not in json.dumps(e.detail)


def test_session_scoping_federated_session_is_valid_on_b_only(fed):
    net, a, b, ca, uid = fed
    relogin(ca, "alice")
    rp, done = signin(ca, a, b)
    cookie = rp.cookies.get("atlas_session")
    other = browser(a, "a")
    other.cookies.set("atlas_session", cookie)
    assert other.get("/me").status_code in (303, 401)  # b's session token means nothing on a
    with b.session() as s:
        row = s.scalars(select(UserSession).where(UserSession.via_group.isnot(None))).one()
        assert row.id != cookie and cookie not in json.dumps([r.detail for r in s.scalars(select(AuditEntry))])
    assert accounts.LOCAL_OTP not in (row.amr or [])
