"""Enrolment (CONTRACT-C10 §2, §11 A10): mutual verification, SAS, limits, body caps, uniform 404, sealed calls."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from atlas import group_crypto as gc
from atlas import group_enrol, groups
from atlas.api import group_routes
from atlas.models import AuditEntry, GroupCert, GroupEnrolment, GroupMember, OwnerGroup
from atlas.signing import canonical_json

from .group_harness import (
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


def test_five_mac_failures_burn_the_invite(pair, net):
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


def test_mitm_swapping_joiner_keys_breaks_the_inviter_pop(pair, net):
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


def test_wrong_reveal_commitment_counts_as_failure(pair, net):
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
