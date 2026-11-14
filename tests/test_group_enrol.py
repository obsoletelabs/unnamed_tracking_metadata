"""Enrolment (CONTRACT-C10 §2, §11 A10): mutual verification, SAS, limits, body caps, uniform 404, sealed calls."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from atlas import group_crypto as gc
from atlas import group_enrol, groups, key_rotation
from atlas.api import group_routes
from atlas.models import AuditEntry, GroupCert, GroupEnrolment, GroupMember, OwnerGroup
from atlas.signing import canonical_json

from .group_harness import (
    REC_PW,
    ROOT_PW,
    Dispatcher,
    create,
    enrol,
    fast_kdf,
    finish_creation,
    invite,
    make_node,
    members,
    pull,
)


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
def pair(net):
    a, b = net.node("a"), net.node("b")
    create(a)
    finish_creation(a)
    return a, b


def actions(node, prefix="group_"):
    with node.session() as s:
        return [a for (a,) in s.execute(select(AuditEntry.action).where(AuditEntry.action.like(prefix + "%")))]


def test_full_enrolment_both_sides_persist_membership(pair):
    a, b = pair
    serial = enrol(a, b)
    with b.session() as s:  # persistent state on the joiner
        group = s.scalars(select(OwnerGroup)).one()
        assert group.status == "active" and group.root_state == "none" and group.root_sealed is None
        assert group.self_serial == serial and group.box_sealed.startswith("enc:v1:")
        assert {m.node_id for m in s.scalars(select(GroupMember))} == {a.node_id, b.node_id}
    with a.session() as s:  # and on the inviter
        row = s.get(GroupCert, (groups.load(s).group_id, serial))
        assert row.node_id == b.node_id and row.body["body"]["issuer"] == "root"
        assert row.body["body"]["scopes"] == ["profile.sync", "group.relay", "identity.assert"]  # H16 default
        assert row.body["body"]["member"]["url_verified"] is True  # the ping reached b
        enrolment = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")).one()
        assert enrolment.state == "active" and enrolment.cert_serial == serial
    assert set(members(a)) == set(members(b)) == {a.node_id, b.node_id}
    assert "group_cert_issued" in actions(a) and "group_joined" in actions(b)


def test_sas_matches_on_both_sides_and_differs_per_enrolment(pair, net):
    a, b = pair
    c = net.node("c")
    sas = []
    for joiner in (b, c):
        code = invite(a)
        with joiner.session() as s:
            row, value, inviter = group_enrol.start_join(s, joiner.settings, joiner.identity, "http://a.test", code,
                                                         net)
            s.commit()
            eid = row.id
        assert inviter["node_id"] == a.node_id
        with a.session() as s:
            assert group_enrol.sas_of(a.settings, s.get(GroupEnrolment, eid)) == value
        assert len(value) == 14 and value[4] == "-" and value[9] == "-"
        sas.append(value)
    assert sas[0] != sas[1]


def test_code_round_trip_and_typo_detection():
    invite_ = gc.Invite("9c1e4b7a2f30d811", "3f2a9c0d1e2b4a55", "b5c0e1f2a3b4c5d6", bytes(range(16)))
    code = gc.encode_code(invite_)
    assert code.startswith("AG1-") and len(code.replace("-", "")) == 3 + 69
    assert gc.decode_code(code.lower()) == invite_
    broken = code[:-3] + ("A" if code[-3] != "A" else "B") + code[-2:]
    with pytest.raises(gc.GroupCryptoError):
        gc.decode_code(broken)
    qr = gc.qr_payload("https://home.example.net", code)
    assert set(qr) == {"v", "type", "url", "code"}  # H21: code-only, no personal data


def test_complete_is_pending_until_inviter_confirms_and_is_sealed(pair, net):
    a, b = pair
    code = invite(a)
    with b.session() as s:
        row, _, _ = group_enrol.start_join(s, b.settings, b.identity, "http://a.test", code, net)
        eid = row.id
        s.commit()
    with b.session() as s:
        assert group_enrol.confirm_joiner(s, b.settings, b.identity, eid, net) == "pending"
        s.commit()
        assert groups.load(s) is None  # nothing activated before the inviter's admin confirms the SAS
    with a.session() as s:
        group_enrol.confirm_inviter(s, a.settings, groups.load(s), a.identity, eid, ROOT_PW, http=net)
        s.commit()
    # The raw complete answer is sealed: it holds no certificate or group id in clear.
    with b.session() as s:
        row = s.get(GroupEnrolment, eid)
        k = gc.unb64(json.loads(groups.open_secret(b.settings, row.secret_enc))["k"], 16)
        payload = {"enrolment_id": eid, "pop": gc.enrol_pop(b.identity.private, "joiner-complete",
                                                            row.transcript_hash, eid),
                   "mac": gc.enrol_mac(k, "joiner-complete", f"{row.transcript_hash}\n{eid}".encode())}
    raw = net.post("http://a.test/api/v2/group/enrol/complete", json=payload)
    assert raw.status_code == 200 and set(raw.json()) == {"sealed", "epk", "nonce", "ct"}
    assert a.node_id not in raw.text and "group.cert" not in raw.text
    with b.session() as s:
        assert group_enrol.confirm_joiner(s, b.settings, b.identity, eid, net) == "active"
        s.commit()


def test_unknown_invite_and_malformed_bodies_answer_the_route_404(pair, net):
    a, _ = pair
    unknown = net.post("http://a.test/api/v2/group/enrol/hello", json={"invite_id": "0" * 16})
    route = net.post("http://a.test/api/v2/group/enrol/nothing-here", json={})
    assert unknown.status_code == route.status_code == 404
    assert unknown.json() == route.json() == {"detail": "Not Found"}
    for step in ("reveal", "complete", "abort"):
        r = net.post(f"http://a.test/api/v2/group/enrol/{step}", json={"enrolment_id": "0" * 26})
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert net.get("http://a.test/api/v2/group/enrol/ping", params={"e": "x"}).json() == {"detail": "Not Found"}


def _forged_hello(b, code, mutate=None):
    inv = gc.decode_code(code)
    member = groups.own_member(b.settings, b.identity, gc.x_public_b64(gc.X25519PrivateKey.generate()))
    body = {"v": 1, "invite_id": inv.invite_id, "group_id": inv.group_id, "issued_at": gc.stamp(),
            "joiner": {"node_id": b.node_id, "root_public_key": b.identity.trust_public_b64,
                       "signing_key_id": b.identity.key_id, "rotations": [], "member": member,
                       "member_pop": gc.member_pop(b.identity.private, inv.group_id, member, b.identity.key_id),
                       "atlas_protocol": "1.1", "group_protocol": "1.0"},
            "commit_j": gc.commitment(b"\x01" * 32)}
    if mutate:
        mutate(body)
    t1 = canonical_json(body)
    return {**body, "pop": gc.enrol_pop(b.identity.private, "joiner-hello", gc.sha256_hex(t1)),
            "mac": gc.enrol_mac(inv.k, "joiner-hello", t1)}, inv


def test_invite_burns_after_failures(pair, net):
    a, b = pair
    code = invite(a)
    body, _ = _forged_hello(b, code)
    for _ in range(5):
        r = net.post("http://a.test/api/v2/group/enrol/hello", json={**body, "mac": "0" * 64})
        assert r.status_code == 404
    with a.session() as s:
        row = s.scalars(select(GroupEnrolment)).one()
        assert row.state == "burned" and row.failures == 5
    assert net.post("http://a.test/api/v2/group/enrol/hello", json=body).status_code == 404  # even a good MAC now
    assert "group_invite_burned" in actions(a)


def test_hello_with_a_pop_by_another_key_is_refused(pair, net):
    a, b = pair
    code = invite(a)
    body, inv = _forged_hello(b, code)
    other = gc.Ed25519PrivateKey.generate()
    body["pop"] = gc.enrol_pop(other, "joiner-hello", gc.sha256_hex(canonical_json(
        {k: v for k, v in body.items() if k not in ("pop", "mac")})))
    assert net.post("http://a.test/api/v2/group/enrol/hello", json=body).status_code == 404
    with a.session() as s:
        assert s.scalars(select(GroupEnrolment)).one().failures == 1


def test_enrolment_key_swap_detected_by_pop_i(pair, net):
    """A relay that holds the code but substitutes its own hello gets a transcript J never computed (§2.4)."""
    a, b = pair
    code = invite(a)
    inv = gc.decode_code(code)
    mallory = net.node("m")
    honest, _ = _forged_hello(b, code)
    evil, _ = _forged_hello(mallory, code)
    reply = net.post("http://a.test/api/v2/group/enrol/hello", json=evil).json()
    t1_honest = canonical_json({k: v for k, v in honest.items() if k not in ("pop", "mac")})
    with pytest.raises(groups.GroupError):
        group_enrol._verify_reply(inv, t1_honest, reply)


def test_reflection_of_a_mac_between_roles_fails(pair, net):
    a, b = pair
    code = invite(a)
    with b.session() as s:
        row, _, _ = group_enrol.start_join(s, b.settings, b.identity, "http://a.test", code, net)
        s.commit()
        eid, th = row.id, row.transcript_hash
        k = gc.unb64(json.loads(groups.open_secret(b.settings, row.secret_enc))["k"], 16)
    data = f"{th}\n{eid}".encode()
    pop = gc.enrol_pop(b.identity.private, "joiner-abort", th, eid)
    wrong = {"enrolment_id": eid, "pop": pop, "mac": gc.enrol_mac(k, "joiner-abort", data)}
    assert net.post("http://a.test/api/v2/group/enrol/complete", json=wrong).status_code == 404
    with a.session() as s:
        assert s.get(GroupEnrolment, eid).failures == 1


def test_sas_commitment_enforced(pair, net):
    a, b = pair
    code = invite(a)
    body, inv = _forged_hello(b, code)
    reply = net.post("http://a.test/api/v2/group/enrol/hello", json=body).json()
    eid, th = reply["enrolment_id"], reply["transcript_hash"]
    nonce = gc.b64(b"\x02" * 32)  # not the committed nonce
    mac = gc.enrol_mac(inv.k, "joiner-reveal", f"{th}\n{eid}\n{nonce}".encode())
    r = net.post("http://a.test/api/v2/group/enrol/reveal", json={"enrolment_id": eid, "nonce_j": nonce, "mac": mac})
    assert r.status_code == 404
    with a.session() as s:
        row = s.get(GroupEnrolment, eid)
        assert row.failures == 1 and row.state == "hello" and row.nonce_j is None


def test_invite_is_single_use(pair, net):
    a, b = pair
    code = invite(a)
    body, _ = _forged_hello(b, code)
    assert net.post("http://a.test/api/v2/group/enrol/hello", json=body).status_code == 200
    assert net.post("http://a.test/api/v2/group/enrol/hello", json=body).status_code == 404


def test_hello_outside_clock_skew_is_refused(pair, net):
    a, b = pair
    code = invite(a)
    body, _ = _forged_hello(b, code, lambda bd: bd.update(issued_at=gc.stamp(groups.utcnow() - groups.SKEW * 2)))
    assert net.post("http://a.test/api/v2/group/enrol/hello", json=body).status_code == 404


def test_at_most_three_open_invites(pair):
    a, _ = pair
    for _ in range(3):
        invite(a)
    with pytest.raises(groups.GroupError, match="at most 3"):
        invite(a)


def test_ip_throttle_on_enrol_endpoints(pair, net):
    a, _ = pair
    codes = [net.post("http://a.test/api/v2/group/enrol/hello", json={"invite_id": "x"}).status_code
             for _ in range(11)]
    assert codes[:10] == [404] * 10 and codes[10] == 429


def test_enrol_body_caps(pair, net):
    a, b = pair
    big = {"invite_id": "x", "pad": "a" * (64 * 1024)}
    assert net.post("http://a.test/api/v2/group/enrol/hello", json=big).status_code == 413
    small = {"enrolment_id": "x", "pad": "a" * 4096}
    for step in ("reveal", "complete", "abort"):
        assert net.post(f"http://a.test/api/v2/group/enrol/{step}", json=small).status_code == 413
    # A full MAX_ROTATIONS-sized history (128 entries of realistic size) still fits the 64 KiB hello cap.
    entry = {"body": {"key_id": "0" * 16, "public_key": "A" * 44, "previous_key_id": "0" * 16, "seq": 1,
                      "issued_at": "2026-11-01T10:00:00.000Z"}, "sig": "A" * 88, "root_sig": "A" * 88}
    assert len(canonical_json([entry] * 128)) < 48 * 1024


def test_url_and_name_uniqueness(pair, net):
    a, b = pair
    enrol(a, b)
    c = net.node("c")
    # c claims b's address
    code = invite(a)
    with c.session() as s, pytest.raises(groups.GroupError):
        group_enrol.start_join(s, c.settings, c.identity, "http://a.test", code, net, member_url="http://B.test:80/")
    # and b's name, case-folded
    code = invite(a)
    with c.session() as s, pytest.raises(groups.GroupError):
        group_enrol.start_join(s, c.settings, c.identity, "http://a.test", code, net, name="node b")


def test_banned_node_cannot_reenrol(pair, net):
    a, b = pair
    enrol(a, b)
    with a.session() as s:
        group = groups.load(s)
        root = groups.unlock_root(s, a.settings, group, ROOT_PW)
        groups.revoke_node(s, a.settings, group, a.identity, b.node_id, "removed", root=root)
        s.commit()
    with b.session() as s:  # b wipes its group locally and tries again
        s.delete(groups.load(s))
        s.commit()
    code = invite(a)
    with b.session() as s, pytest.raises(groups.GroupError):
        group_enrol.start_join(s, b.settings, b.identity, "http://a.test", code, net)


def test_abort_after_issue_by_delegate_revokes(net):
    a, d, j = net.node("a"), net.node("d"), net.node("j")
    create(a)
    finish_creation(a)
    enrol(a, d, scopes=["profile.sync", "group.relay", "identity.assert", "group.enrol"],
          delegate=groups.default_delegate(2))
    code = invite(d)
    with j.session() as s:
        row, _, _ = group_enrol.start_join(s, j.settings, j.identity, "http://d.test", code, net)
        eid = row.id
        s.commit()
    with d.session() as s:
        group_enrol.confirm_inviter(s, d.settings, groups.load(s), d.identity, eid, None, http=net)
        s.commit()
    with j.session() as s:
        row = s.get(GroupEnrolment, eid)
        k = gc.unb64(json.loads(groups.open_secret(j.settings, row.secret_enc))["k"], 16)
        body = {"enrolment_id": eid, "pop": gc.enrol_pop(j.identity.private, "joiner-abort", row.transcript_hash, eid),
                "mac": gc.enrol_mac(k, "joiner-abort", f"{row.transcript_hash}\n{eid}".encode())}
    assert net.post("http://d.test/api/v2/group/enrol/abort", json=body).status_code == 200
    with d.session() as s:
        group = groups.load(s)
        serial = s.get(GroupEnrolment, eid).cert_serial
        assert s.get(groups.GroupRevoked, (group.group_id, serial)).reason == "enrolment_aborted"
    assert j.node_id not in members(d)


# ---- the sealed call (§3.2) ---------------------------------------------------------------------------------

def _member(node):
    with node.session() as s:
        group = groups.load(s)
        return groups.trust(s, group, own_node=node.node_id).in_force(node.node_id)["body"]["member"]


def test_call_state_between_members_and_uniform_404(pair, net):
    a, b = pair
    enrol(a, b)
    with b.session() as s:
        state = group_routes.call(s, b.settings, b.identity, groups.load(s), _member(a), "state", {}, net)
        assert state["genesis"]["group_id"] == groups.load(s).group_id
        hello = group_routes.call(s, b.settings, b.identity, groups.load(s), _member(a), "hello", {}, net)
        assert hello["group_protocol"] == "1.0"
    # non-members, garbage and a node in no group all look the same
    stranger = net.node("s")
    garbage = net.post("http://a.test/api/v2/group/call", content=b'{"sealed_req":"x"}',
                       headers={"Content-Type": "application/json"})
    empty = net.post("http://s.test/api/v2/group/call", content=b'{}', headers={"Content-Type": "application/json"})
    with a.session() as s:
        group_id = groups.load(s).group_id
    with stranger.session():
        fake = OwnerGroup(group_id=group_id, genesis={}, succession=[])
        inner = {"v": 1, "g": fake.group_id, "n": stranger.node_id, "aud": a.node_id, "k": stranger.identity.key_id,
                 "t": int(groups.utcnow().timestamp()), "r": gc.b64(b"\0" * 16), "proto": "1.0", "op": "state",
                 "params": {}}
        sealed = gc.seal_request(_member(a)["box_public_key"], fake.group_id, a.node_id,
                                 gc.sign_request(stranger.identity.private, inner))
    outsider = net.post("http://a.test/api/v2/group/call", content=canonical_json(sealed),
                        headers={"Content-Type": "application/json"})
    for r in (garbage, empty, outsider):
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert "group_request_auth_failed" in actions(a)


def test_call_replay_and_audience_binding(pair, net):
    a, b = pair
    enrol(a, b)
    with b.session() as s:
        group = groups.load(s)
        inner = {"v": 1, "g": group.group_id, "n": b.node_id, "aud": a.node_id, "k": b.identity.key_id,
                 "t": int(groups.utcnow().timestamp()), "r": gc.b64(b"\7" * 16), "proto": "1.0", "op": "hello",
                 "params": {}}
        sealed = canonical_json(gc.seal_request(_member(a)["box_public_key"], group.group_id, a.node_id,
                                                gc.sign_request(b.identity.private, inner)))
        keys = groups.box_keys(b.settings, group)
    headers = {"Content-Type": "application/json"}
    first = net.post("http://a.test/api/v2/group/call", content=sealed, headers=headers)
    second = net.post("http://a.test/api/v2/group/call", content=sealed, headers=headers)
    opened = [gc.open_response(keys, b"\7" * 16, group.group_id, a.node_id, b.node_id, r.json()) for r in (first, second)]
    assert opened[0]["error"] is None and opened[1]["error"] == "replay"
    # A request addressed to another member (aud) is not accepted here.
    with b.session() as s:
        inner2 = {**inner, "aud": "0" * 16, "r": gc.b64(b"" * 16)}
        sealed2 = canonical_json(gc.seal_request(_member(a)["box_public_key"], group.group_id, a.node_id,
                                                 gc.sign_request(b.identity.private, inner2)))
    assert net.post("http://a.test/api/v2/group/call", content=sealed2, headers=headers).status_code == 404


def test_pull_ingests_state_and_views(pair, net):
    a, b = pair
    enrol(a, b)
    assert pull(a) == {b.node_id: "ok"}
    assert pull(b) == {a.node_id: "ok"}
    with a.session() as s:
        assert s.scalar(select(func.count()).select_from(groups.GroupView)) == 2


def test_restore_sets_resyncing_and_secret_check_covers_group_secrets(pair, tmp_path):
    """§3.6: a restored node pulls every member's state before any privileged action."""
    from sqlalchemy import create_engine

    from atlas import backup, db

    a, b = pair
    enrol(a, b)
    archive = tmp_path / "group.tar.gz"
    backup.create(a.settings, a.st.maker.kw["bind"], archive, include_media=False)
    checked = backup.secret_check(a.settings, archive)
    assert checked["secrets"] >= 3  # sealed root, box key and the enrolment secret
    url = f"sqlite:///{tmp_path}/restored.db"
    db.upgrade(url)
    engine = create_engine(url)
    backup.restore(a.settings.model_copy(update={"database_url": url}), engine, archive, include_media=False)
    with engine.connect() as c:
        from sqlalchemy import text

        assert c.execute(text("SELECT status FROM owner_group")).scalar() == "resyncing"
    engine.dispose()
    with a.session() as s:  # the source is untouched
        assert groups.load(s).status == "active"


# ---- o, o4, q: MITM, PoP reflection, wrong group or inviter ---------------------------------------------------

def test_pop_reflection_between_roles_refused(pair, net):
    """o4 (T21): a joiner PoP made for another role or step (abort, the url ping) is refused at complete even
    with a valid complete MAC; it counts as a failure and delivers nothing."""
    a, b = pair
    code = invite(a)
    with b.session() as s:
        row, _, _ = group_enrol.start_join(s, b.settings, b.identity, "http://a.test", code, net)
        s.commit()
        eid, th = row.id, row.transcript_hash
        k = gc.unb64(json.loads(groups.open_secret(b.settings, row.secret_enc))["k"], 16)
    with a.session() as s:
        group_enrol.confirm_inviter(s, a.settings, groups.load(s), a.identity, eid, ROOT_PW, http=net)
        s.commit()
    mac = gc.enrol_mac(k, "joiner-complete", f"{th}\n{eid}".encode())
    reflected = [gc.enrol_pop(b.identity.private, "joiner-abort", th, eid),
                 gc.enrol_pop(b.identity.private, "joiner-url", th),
                 gc.enrol_pop(b.identity.private, "joiner-hello", th, eid)]
    for pop in reflected:
        r = net.post("http://a.test/api/v2/group/enrol/complete", json={"enrolment_id": eid, "pop": pop, "mac": mac})
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    with a.session() as s:
        row = s.get(GroupEnrolment, eid)
        assert row.failures == 3 and row.state == "issued"  # nothing delivered
    good = gc.enrol_pop(b.identity.private, "joiner-complete", th, eid)
    assert net.post("http://a.test/api/v2/group/enrol/complete",
                    json={"enrolment_id": eid, "pop": good, "mac": mac}).status_code == 200


def test_join_refuses_wrong_group_or_inviter(pair, net):
    """q (T7): the code names the inviter and the group; an answer from another node or for another group is
    refused before any SAS is shown, and nothing is stored on the joiner."""
    a, b = pair
    c = net.node("c")
    code = invite(a)
    inv = gc.decode_code(code)
    wrong_inviter = gc.encode_code(gc.Invite(inv.group_id, c.node_id, inv.invite_id, inv.k))
    with b.session() as s, pytest.raises(groups.GroupError, match="another node or group"):
        group_enrol.start_join(s, b.settings, b.identity, "http://a.test", wrong_inviter, net)
    with b.session() as s:
        assert s.scalars(select(GroupEnrolment)).all() == [] and groups.load(s) is None
    code = invite(a)
    inv = gc.decode_code(code)
    body, _ = _forged_hello(b, code)
    reply = net.post("http://a.test/api/v2/group/enrol/hello", json=body).json()
    t1 = canonical_json({k: v for k, v in body.items() if k not in ("pop", "mac")})
    assert group_enrol._verify_reply(inv, t1, reply)["node_id"] == a.node_id  # the genuine pair verifies
    for fake in (gc.Invite("0123456789abcdef", inv.inviter, inv.invite_id, inv.k),
                 gc.Invite(inv.group_id, c.node_id, inv.invite_id, inv.k)):
        with pytest.raises(groups.GroupError, match="another node or group"):
            group_enrol._verify_reply(fake, t1, reply)
    # J also checks the inviter's own certificate: a root holder cannot pose as a delegate, nor show another cert
    shadow = OwnerGroup(group_id=inv.group_id, genesis=reply["group"]["genesis"], succession=[],
                        recovery=reply["group"]["genesis"]["body"]["recovery"])
    with pytest.raises(groups.GroupError, match="not delegated"):
        group_enrol._inviter_cert_ok(shadow, reply["inviter"], {**reply, "inviter_kind": "delegate"})
    with pytest.raises(groups.GroupError):
        group_enrol._inviter_cert_ok(shadow, {**reply["inviter"], "url": "http://elsewhere.test"}, reply)


def test_enrolment_mitm_changes_sas(pair, net):
    """o (T5): someone who learned the code can only enrol itself in J's place. The inviter's screen then shows
    the intruder's fingerprint and a SAS the real joiner never sees; nothing is issued without the inviter's
    confirmation, and cancelling ends it."""
    a, b = pair
    mallory = net.node("m")
    code = invite(a)
    with mallory.session() as s:
        row_m, sas_m, _ = group_enrol.start_join(s, mallory.settings, mallory.identity, "http://a.test", code, net)
        s.commit()
        eid = row_m.id
    with b.session() as s, pytest.raises(groups.GroupError, match="does not know this code"):
        group_enrol.start_join(s, b.settings, b.identity, "http://a.test", code, net)
    with b.session() as s:
        assert s.scalars(select(GroupEnrolment)).all() == []  # the real joiner has no SAS that could match
    with a.session() as s:
        row = s.get(GroupEnrolment, eid)
        assert group_enrol.sas_of(a.settings, row) == sas_m and row.peer_node_id == mallory.node_id != b.node_id
    with mallory.session() as s:
        assert group_enrol.confirm_joiner(s, mallory.settings, mallory.identity, eid, net) == "pending"
        s.commit()
    with a.session() as s:
        assert s.scalars(select(GroupCert).where(GroupCert.node_id == mallory.node_id)).all() == []
        group_enrol.cancel(s, a.settings, a.identity, eid)
        s.commit()
    with mallory.session() as s:
        assert group_enrol.confirm_joiner(s, mallory.settings, mallory.identity, eid, net) == "aborted"
        s.commit()
    with a.session() as s:
        assert s.scalars(select(GroupCert).where(GroupCert.node_id == mallory.node_id)).all() == []
        assert s.get(GroupEnrolment, eid).state == "aborted"


# ---- T3 (A10 side), t: removed callers and the skew edge of the sealed call -----------------------------------

def test_revoked_member_call_gets_404(pair, net):
    a, b = pair
    enrol(a, b)
    with a.session() as s:
        group = groups.load(s)
        groups.revoke_node(s, a.settings, group, a.identity, b.node_id, "removed",
                           root=groups.unlock_root(s, a.settings, group, ROOT_PW))
        s.commit()
    with b.session() as s, pytest.raises(group_routes.CallError, match="HTTP 404"):
        group_routes.call(s, b.settings, b.identity, groups.load(s), _member(a), "state", {}, net)
    assert "group_request_auth_failed" in actions(a)
    with a.session() as s:
        assert s.get(GroupMember, (groups.load(s).group_id, b.node_id)).status == "revoked"


def _sealed_call(b, a_member, group, t, nonce):
    inner = {"v": 1, "g": group.group_id, "n": b.node_id, "aud": a_member["node_id"], "k": b.identity.key_id,
             "t": t, "r": gc.b64(nonce), "proto": "1.0", "op": "hello", "params": {}}
    return gc.seal_request(a_member["box_public_key"], group.group_id, a_member["node_id"],
                           gc.sign_request(b.identity.private, inner))


def test_call_clock_skew_exact_edges(pair, net):
    """t: a request exactly SKEW (300 s) away is served; one second more answers `clock_skew` (signed, sealed)."""
    a, b = pair
    enrol(a, b)
    a_member = _member(a)
    base = int(groups.utcnow().timestamp())
    with b.session() as s:
        group = groups.load(s)
        keys = groups.box_keys(b.settings, group)
        cases = [(base, base + gc.SKEW, None), (base, base - gc.SKEW, None),
                 (base, base + gc.SKEW + 1, "clock_skew"), (base, base - gc.SKEW - 1, "clock_skew")]
        for i, (t, now, expected) in enumerate(cases):
            nonce = bytes([i + 1]) * 16
            resp = group_routes.serve_call(a.st, _sealed_call(b, a_member, group, t, nonce), now=now)
            opened = gc.open_response(keys, nonce, group.group_id, a.node_id, b.node_id, json.loads(resp.body))
            assert opened["error"] == expected, (t, now)
            assert (opened["result"] is None) == (expected is not None)


# ---- §1.7 end to end: rotate, and the node stays a member on itself and on its peers ---------------------------

def test_rotation_keeps_membership_on_self_and_peers(pair, net):
    a, b = pair
    enrol(a, b)
    with b.session() as s:
        successor = key_rotation.rotate(s, b.identity.key_id, b.settings)
        s.commit()
    b.st.ctx.identity = successor
    with b.session() as s:
        group = groups.load(s)
        groups.rebuild(s, group, b.node_id, settings=b.settings)
        s.commit()
        assert group.status == "active"
        assert s.get(GroupMember, (group.group_id, b.node_id)).status == "active"
    assert pull(a) == {b.node_id: "ok"}  # a learns b's rotation from b's own history and verifies the answer
    with a.session() as s:
        group = groups.load(s)
        tr = groups.trust(s, group, own_node=a.node_id)
        assert tr.current_key(b.node_id)[0] == successor.key_id
        assert b.node_id in tr.members()
        assert s.get(GroupMember, (group.group_id, b.node_id)).status == "active"
    assert pull(b) == {a.node_id: "ok"}  # b's requests, signed by its new key, are accepted by a
    assert set(members(a)) == set(members(b)) == {a.node_id, b.node_id}


# ---- §1.8: a halted node keeps pulling revocations ---------------------------------------------------------------

def test_halted_node_keeps_receiving_revocations(net):
    from atlas import main

    a, b, c = net.node("a"), net.node("b"), net.node("c")
    create(a)
    finish_creation(a)
    enrol(a, b)
    enrol(a, c)
    with b.session() as s:
        groups.load(s).status = "halted"  # as after conflicting successions
        s.commit()
    with a.session() as s:
        group = groups.load(s)
        groups.revoke_node(s, a.settings, group, a.identity, c.node_id, "removed",
                           root=groups.unlock_root(s, a.settings, group, ROOT_PW))
        s.commit()
    assert main.group_round(b.st.maker, b.st.ctx, net).get(a.node_id) == "ok"
    with b.session() as s:
        group = groups.load(s)
        assert group.status == "halted"
        assert s.scalars(select(groups.GroupRevoked).where(groups.GroupRevoked.node_id == c.node_id)).first() \
            is not None
        assert c.node_id not in groups.trust(s, group, own_node=b.node_id).members()


# ---- §1.9: moving the root with a recovery file ------------------------------------------------------------------

NEW_ROOT_PW = "synthetic imported root phrase 3f2a"


def test_root_import_moves_root_and_disables_old_holder(net):
    a, b, c = net.node("a"), net.node("b"), net.node("c")
    created = create(a)
    finish_creation(a)
    enrol(a, b)
    keys = gc.open_recovery_file(created.recovery_files[0], REC_PW[0])
    with b.session() as s:
        group = groups.load(s)
        # a v2 file (C10 R1-5) holds G under the root passphrase; a new one needs a re-exported file
        with pytest.raises(groups.GroupError, match="wrong root passphrase"):
            groups.import_root(s, b.settings, group, b.identity, keys, NEW_ROOT_PW, "running")
        with pytest.raises(groups.GroupError, match="re-exported recovery file"):
            groups.import_root(s, b.settings, group, b.identity, keys, NEW_ROOT_PW, "running", root_passphrase=ROOT_PW)
        done = groups.import_root(s, b.settings, group, b.identity, keys, NEW_ROOT_PW, "running",
                                  root_passphrase=ROOT_PW, reexport_passphrase=REC_PW[1])
        s.commit()
        moved = done.moved
        assert moved["body"]["to_node"] == b.node_id
        again = gc.open_recovery_file(done.reexport, REC_PW[1])
        assert again.root is None and gc.recovery_root(again, NEW_ROOT_PW) is not None
        assert group.root_state == "holder" and group.status == "resyncing"
        with pytest.raises(groups.GroupError, match="resyncing"):
            groups.unlock_root(s, b.settings, group, NEW_ROOT_PW)
    assert pull(b) == {a.node_id: "ok"}
    with b.session() as s:
        assert groups.load(s).status == "active"
        assert "group_root_imported" in actions(b)
    assert pull(a) == {b.node_id: "ok"}
    with a.session() as s:
        group = groups.load(s)
        assert group.root_state == "moved"
        with pytest.raises(groups.GroupError, match="not on this node"):
            groups.unlock_root(s, a.settings, group, ROOT_PW)
    enrol(b, c, passphrase=NEW_ROOT_PW)  # the new holder issues
    assert c.node_id in members(b)
    with b.session() as s, pytest.raises(groups.GroupError, match="already holds"):
        groups.import_root(s, b.settings, groups.load(s), b.identity, keys, NEW_ROOT_PW, "running",
                           root_passphrase=ROOT_PW, reexport_passphrase=REC_PW[1])
