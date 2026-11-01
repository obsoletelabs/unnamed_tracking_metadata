"""Certificates, CRLs, cascade, succession and freshness (CONTRACT-C10 §1.3-§1.9, §11 A10 f-p, y-ff, i2-r2)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from atlas import group_crypto as gc
from atlas import groups
from atlas.models import AuditEntry, GroupBannedNode, GroupCert, GroupMember, GroupRevoked, GroupView
from atlas.signing import body_hash

from .group_world import World, rotation, synth


@pytest.fixture
def w(monkeypatch):
    world = World(monkeypatch)
    yield world
    world.s.close()
    world.engine.dispose()


def audited(w, action):
    return w.s.scalars(select(AuditEntry).where(AuditEntry.action == action)).all()


def revoked(w, serial):
    return w.s.get(GroupRevoked, (w.gid, serial))


# ---- f: every step of §1.4 ----------------------------------------------------------------------------------

def test_root_issued_certificate_is_valid_and_persisted(w):
    x = synth()
    cert = w.enrol(x)
    assert w.verdict(cert) == "ok"
    row = w.s.get(GroupCert, (w.gid, cert["body"]["serial"]))
    assert row.node_id == x.node_id and row.held_reason is None
    member = w.s.get(GroupMember, (w.gid, x.node_id))
    assert member.status == "active" and member.serial == cert["body"]["serial"]


def test_step1_malformed_and_id_mismatch(w):
    x = synth()
    cert = w.root_cert(x, 9)
    bad = {**cert, "id": "cert:root/10-0123456789abcdef"}
    assert w.verdict(bad) == "malformed"
    assert w.verdict({**cert, "t": "group.crl"}) == "malformed"


def test_step2_tampered_body_and_moved_signature(w):
    x, y = synth(), synth()
    cert = w.root_cert(x, 9)
    tampered = {**cert, "body": {**cert["body"], "scopes": ["profile.sync"]}}
    assert w.verdict(tampered) == "malformed"  # body_hash no longer matches
    tampered["body_hash"] = body_hash(tampered["body"])
    assert w.verdict(tampered) == "bad_signature"
    d = synth()
    w.delegate(d)
    good = w.node_cert(d, y, 1, w.trust().in_force(d.node_id)["body"]["serial"])
    moved = {**good, "signer": {**good["signer"], "id": x.node_id}}  # cc: signer.id is in the message
    assert w.verdict(moved) != "ok"


def test_step2_member_pop_required_url_swap_refused(w):
    """n4: an issuer cannot change a member's url (or box key) without a new member_pop by that member."""
    x = synth()
    member = w.member(x)
    pop = w.pop(x, member)
    swapped = {**member, "url": "http://evil.test"}
    with pytest.raises(groups.GroupError, match="bad_member_pop"):
        groups.issue_cert(w.s, w.group, w.me, swapped, pop, list(groups.DEFAULT_SCOPES), None, signer_root=w.root(),
                          days=365)


def test_step3_expiry_and_skew_edges(w):
    x = synth()
    start = groups.utcnow() - timedelta(days=10)
    cert = w.root_cert(x, 9, days=5, start=start)
    w.ingest(cert)
    end = gc.parse_time(cert["body"]["not_after"])
    assert w.verdict(cert, end + timedelta(seconds=300)) == "ok"  # exact edge: +SKEW still valid
    assert w.verdict(cert, end + timedelta(seconds=301)) == "expired"
    begin = gc.parse_time(cert["body"]["not_before"])
    assert w.verdict(cert, begin - timedelta(seconds=300)) == "ok"
    assert w.verdict(cert, begin - timedelta(seconds=301)) == "not_yet_valid"


def test_step4_member_without_delegate_cannot_enrol(w):
    """k / g: a member key signs a certificate for another node; every member rejects it (T1, T4)."""
    m, x = synth(), synth()
    parent = w.enrol(m)["body"]["serial"]
    forged = w.node_cert(m, x, 1, parent)
    w.ingest(forged)
    assert w.verdict(forged) == "issuer_cannot_enrol"
    assert x.node_id not in w.trust().members()


def test_step4_delegate_bounds(w):
    """l: max_members, depth 1 and not_after bounds."""
    d, x, y, z = synth(), synth(), synth(), synth()
    parent = w.delegate(d, max_members=1)["body"]["serial"]
    first = w.node_cert(d, x, 1, parent)
    w.ingest(first)
    assert w.verdict(first) == "ok"
    second = w.node_cert(d, y, 2, parent)
    w.ingest(second)
    assert w.verdict(second) == "delegate_members_exhausted"
    third = w.node_cert(d, z, 3, parent)
    assert w.verdict(third) == "delegate_serials_exhausted"  # max_serials = 2 * max_members
    long = w.node_cert(d, x, 2, parent, days=60)  # outlives the 30-day delegate certificate
    assert w.verdict(long) in ("outlives_issuer", "bad_lifetime")
    deeper = w.node_cert(d, x, 2, parent, scopes=[*groups.DEFAULT_SCOPES, "group.enrol"],
                         delegate=groups.default_delegate(1))
    assert w.verdict(deeper) in ("scope_escalation", "bad_lifetime")


def test_step5_chain_depth_is_one(w):
    d, e, x = synth(), synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    w.ingest(w.node_cert(d, e, 1, parent))
    sub = w.node_cert(e, x, 1, f"{d.node_id}/1")
    w.ingest(sub)
    assert w.verdict(sub) in ("issuer_cannot_enrol", "chain_too_deep")


def test_step6_revoked_serial_invalid(w):
    x = synth()
    cert = w.enrol(x)
    groups.revoke_node(w.s, w.settings, w.group, w.me, x.node_id, "removed", root=w.root())
    assert w.verdict(cert) == "revoked"
    assert w.s.get(GroupBannedNode, (w.gid, x.node_id)).reason == "removed"


def test_step8_new_root_key_for_existing_node_refused(w):
    """n: a certificate for an existing node id with another root key is refused (T5)."""
    x = synth()
    w.enrol(x)
    impostor_key = Ed25519PrivateKey.generate()
    member = w.member(x)
    member["root_public_key"] = gc.ed_public_b64(impostor_key)
    impostor = groups.NodeIdentity(node_id=x.node_id, private=impostor_key)
    with pytest.raises(Exception):  # noqa: B017 - malformed (node_id is not the key id) or root_key_changed
        groups.issue_cert(w.s, w.group, w.me, member, gc.member_pop(impostor_key, w.gid, member, impostor.key_id),
                          list(groups.DEFAULT_SCOPES), None, signer_root=w.root(), days=365)


def test_delegate_cannot_reissue_other_issuers_node(w):
    """n3: a node held by a root certificate cannot be re-certified by a delegate."""
    d, x = synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    w.enrol(x)
    stolen = w.node_cert(d, x, 1, parent)
    w.ingest(stolen)
    assert w.verdict(stolen) == "other_issuer"


def test_member_rev_cannot_regress(w):
    d, x = synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    w.ingest(w.node_cert(d, x, 1, parent, member=w.member(x, rev=3)))
    older = w.node_cert(d, x, 2, parent, member=w.member(x, rev=2))
    w.ingest(older)
    assert w.verdict(older) == "member_rev_regressed"


# ---- g, h: CRL scope ----------------------------------------------------------------------------------------

def test_member_crl_revoking_others_is_invalid(w):
    m, x = synth(), synth()
    w.enrol(m)
    victim = w.enrol(x)
    entry = groups.crl_entry(victim, "removed", groups.utcnow())
    assert w.apply(w.crl(m, [entry], 1)) == "invalid"
    assert w.verdict(victim) == "ok" and revoked(w, victim["body"]["serial"]) is None


def test_self_leave_crl_and_stolen_key_cannot_undo(w):
    """p3: self CRL `left`, then a backdated empty self CRL at seq+1: the node stays revoked (T18)."""
    m = synth()
    cert = w.enrol(m)
    left = w.crl(m, [groups.crl_entry(cert, "left", groups.utcnow())], 1)
    assert w.apply(left) == "stored"
    assert revoked(w, cert["body"]["serial"]).reason == "left"
    empty = w.crl(m, [], 2, issued=groups.utcnow() - timedelta(days=3))
    w.apply(empty)
    assert revoked(w, cert["body"]["serial"]).reason == "left"
    assert w.trust().status_of(m.node_id) == "left"
    assert audited(w, "group_crl_shrink")


# ---- m, ee: cascade ------------------------------------------------------------------------------------------

def test_revoking_enroller_cascades(w):
    d, x, y = synth(), synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    issued = [w.node_cert(d, x, 1, parent), w.node_cert(d, y, 2, parent)]
    for c in issued:
        w.ingest(c)
    assert all(w.verdict(c) == "ok" for c in issued)
    groups.revoke_node(w.s, w.settings, w.group, w.me, d.node_id, "removed", root=w.root())
    assert {w.verdict(c) for c in issued} == {"issuer_revoked"}
    assert set(w.trust().members()) == {w.me.node_id}


def test_superseded_revocation_does_not_cascade(w):
    d, x = synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    child = w.node_cert(d, x, 1, parent)
    w.ingest(child)
    renewed = groups.renew(w.s, w.settings, w.group, w.me, [d.node_id], root=w.root())
    assert len(renewed) == 1 and revoked(w, parent).reason == "superseded"
    assert w.verdict(child) == "ok"  # superseded never cascades
    assert w.trust().in_force(d.node_id)["body"]["serial"] == renewed[0]["body"]["serial"]


def test_rekey_box_renews_and_supersedes(w):
    """ff: a member-signed update with a new box key and rev+1; the old serial superseded at once."""
    x = synth()
    old = w.enrol(x)
    member = {k: v for k, v in old["body"]["member"].items() if k != "url_verified"}
    member = {**member, "box_public_key": gc.x_public_b64(gc.X25519PrivateKey.generate()), "rev": member["rev"] + 1}
    update = {"member": member, "member_pop": w.pop(x, member)}
    out = groups.renew(w.s, w.settings, w.group, w.me, [x.node_id], root=w.root(), member_update=update)
    assert out[0]["body"]["member"]["box_public_key"] == member["box_public_key"]
    assert revoked(w, old["body"]["serial"]).reason == "superseded"
    assert w.s.get(GroupMember, (w.gid, x.node_id)).box_public_key == member["box_public_key"]
    forged = {"member": {**member, "rev": member["rev"] + 1}, "member_pop": update["member_pop"]}
    with pytest.raises(groups.GroupError):
        groups.renew(w.s, w.settings, w.group, w.me, [x.node_id], root=w.root(), member_update=forged)


def test_revoked_node_not_reissued_by_bulk_renew_or_delegate(w):
    """n2."""
    d, x = synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    w.enrol(x)
    groups.revoke_node(w.s, w.settings, w.group, w.me, x.node_id, "removed", root=w.root())
    renewed = groups.renew(w.s, w.settings, w.group, w.me, None, root=w.root())
    assert x.node_id not in {c["body"]["member"]["node_id"] for c in renewed}
    by_delegate = w.node_cert(d, x, 1, parent)
    w.ingest(by_delegate)
    assert w.verdict(by_delegate) == "banned"
    ban = w.s.get(GroupBannedNode, (w.gid, x.node_id))
    readmit = w.enrol(x, readmit=ban.crl_serial)  # only a root-issued readmission
    assert w.verdict(readmit) == "ok"


def test_delegate_cap_deterministic(w):
    """dd: whatever order members see a delegate's certificates in, they agree; renewals do not use up room."""
    d, x, y, z = synth(), synth(), synth(), synth()
    parent = w.delegate(d, max_members=2)["body"]["serial"]
    certs = [w.node_cert(d, x, 1, parent), w.node_cert(d, y, 2, parent),
             w.node_cert(d, x, 3, parent, member=w.member(x, rev=2)), w.node_cert(d, z, 4, parent)]
    for c in reversed(certs):  # arrival order is not issue order
        w.ingest(c)
    verdicts = [w.verdict(c) for c in certs]
    assert verdicts == ["ok", "ok", "ok", "delegate_members_exhausted"]
    tr = w.trust()
    assert tr.in_force(x.node_id)["body"]["serial"] == f"{d.node_id}/3"
    fresh = groups.Trust(w.group, dict(reversed(list(tr.certs.items()))), tr.revoked, tr.banned, tr.rotations, tr.now)
    assert [fresh.valid(c).ok for c in certs] == [True, True, True, False]


# ---- p: CRL monotonicity ------------------------------------------------------------------------------------

def test_crl_rewind_changes_nothing(w):
    x, y = synth(), synth()
    cx, cy = w.enrol(x), w.enrol(y)
    root = w.root()
    now = groups.utcnow()
    w.apply(w.crl(None, [groups.crl_entry(cx, "removed", now)], 5, root=root))
    w.apply(w.crl(None, [], 3, root=root))  # rewind: a lower seq
    assert revoked(w, cx["body"]["serial"]).reason == "removed"
    assert audited(w, "group_crl_rewind_refused")
    assert w.verdict(cy) == "ok"


def test_unrevoke_via_higher_seq_crl_refused(w):
    """p2 (T18)."""
    x = synth()
    cx = w.enrol(x)
    root = w.root()
    w.apply(w.crl(None, [groups.crl_entry(cx, "removed", groups.utcnow())], 1, root=root))
    w.apply(w.crl(None, [], 2, root=root))
    assert revoked(w, cx["body"]["serial"]) is not None and w.verdict(cx) == "revoked"
    assert audited(w, "group_crl_shrink")


def test_crl_fork_applies_union(w):
    """p4: two different CRLs with the same seq: both apply, an alarm is raised (T6, T18)."""
    x, y = synth(), synth()
    cx, cy = w.enrol(x), w.enrol(y)
    root = w.root()
    now = groups.utcnow()
    w.apply(w.crl(None, [groups.crl_entry(cx, "removed", now)], 7, root=root))
    w.apply(w.crl(None, [groups.crl_entry(cy, "removed", now)], 7, root=root))
    assert w.verdict(cx) == w.verdict(cy) == "revoked"
    assert audited(w, "group_crl_fork")


def test_reason_upgrade_never_downgrade(w):
    x = synth()
    cx = w.enrol(x)
    root = w.root()
    now = groups.utcnow()
    w.apply(w.crl(None, [groups.crl_entry(cx, "key_compromise", now, now - timedelta(days=1))], 1, root=root))
    w.apply(w.crl(None, [groups.crl_entry(cx, "superseded", now)], 2, root=root))
    assert revoked(w, cx["body"]["serial"]).reason == "key_compromise"


# ---- r: feed signatures are not group objects ---------------------------------------------------------------

def test_feed_signature_is_not_a_group_object(w):
    x = synth()
    cert = w.root_cert(x, 9)
    feed_sig = w.me.sign(1, "group.cert", cert["body_hash"])
    assert not gc.verify_object({**cert, "sig": feed_sig}, w.trust().chain()[0].public)


def test_rotation_to_other_members_key_refused(w):
    """r2: a member's rotation may not adopt a key another member already uses."""
    x, y = synth(), synth()
    w.enrol(x)
    w.enrol(y)
    proof = rotation(x, x.node_id, 1, y.private)
    assert groups.ingest_rotations(w.s, w.group, x.node_id, [proof], groups.utcnow()) == "refused"
    good = rotation(x, x.node_id, 1, Ed25519PrivateKey.generate())
    assert groups.ingest_rotations(w.s, w.group, x.node_id, [good], groups.utcnow()) == "stored"
    assert w.s.get(GroupMember, (w.gid, x.node_id)).rotations == [good]


# ---- i, i2, i3, y, z, j: root succession ---------------------------------------------------------------------

def test_succession_needs_recovery_quorum(monkeypatch):
    w = World(monkeypatch, recovery=3)
    keys = w.recovery_keys()
    new = Ed25519PrivateKey.generate()
    assert groups.ingest_succession(w.s, w.group, w.succession(1, new, keys[:1]), groups.utcnow()) == "invalid"
    g_only = w.succession(1, new, keys[:2])
    g_only["signer"] = {"kind": "root", "id": w.gid, "key_id": w.gid}
    assert groups.ingest_succession(w.s, w.group, g_only, groups.utcnow()) == "invalid"
    assert groups.ingest_succession(w.s, w.group, w.succession(1, new, keys[:2]), groups.utcnow()) == "pending"


def test_succession_pending_then_cancel(w):
    """i3: pending for the delay; old-root objects are held meanwhile; a recovery-quorum cancel ends it."""
    (rec,) = w.recovery_keys()
    new = Ed25519PrivateKey.generate()
    succ = w.succession(1, new, [rec])
    assert groups.ingest_succession(w.s, w.group, succ, groups.utcnow()) == "pending"
    with pytest.raises(groups.GroupError, match="succession is pending"):
        w.root()
    cancel_body = {"index": 1, "succession_body_hash": succ["body_hash"], "issued_at": gc.stamp()}
    g_cancel = gc.make_object("group.succession_cancel", w.gid, "succession_cancel:1", cancel_body,
                              {"kind": "root", "id": w.gid, "key_id": w.gid}, Ed25519PrivateKey.generate())
    assert groups.ingest_cancel(w.s, w.group, g_cancel, groups.utcnow()) == "invalid"  # G-signed: never honoured
    cancel = gc.make_object("group.succession_cancel", w.gid, "succession_cancel:1", cancel_body,
                            {"kind": "recovery", "id": w.gid, "key_id": gc.key_id_of(gc.ed_public_b64(rec))}, rec)
    assert groups.ingest_cancel(w.s, w.group, cancel, groups.utcnow()) == "cancelled"
    assert groups.activate_successions(w.s, w.group, groups.utcnow() + timedelta(days=2)) == 0
    assert w.root()  # usable again


def test_old_root_objects_held_while_pending(w):
    x = synth()
    root = w.root()
    (rec,) = w.recovery_keys()
    groups.ingest_succession(w.s, w.group, w.succession(1, Ed25519PrivateKey.generate(), [rec]), groups.utcnow())
    cert = w.root_cert(x, 40, root=root)
    assert groups.ingest_cert(w.s, w.group, cert, groups.utcnow()) == "held"
    groups.rebuild(w.s, w.group, w.me.node_id)
    assert x.node_id not in w.trust().members()


def test_conflicting_successions_fatal(w):
    """i2: two valid successions with the same index halt the group."""
    (rec,) = w.recovery_keys()
    a = w.succession(1, Ed25519PrivateKey.generate(), [rec])
    b = w.succession(1, Ed25519PrivateKey.generate(), [rec])
    groups.ingest_succession(w.s, w.group, a, groups.utcnow())
    assert groups.ingest_succession(w.s, w.group, b, groups.utcnow()) == "conflict"
    assert w.group.status == "halted" and audited(w, "group_succession_conflict")


def _activated(w):
    x = synth()
    carried = w.enrol(x)
    root = w.root()
    (rec,) = w.recovery_keys()
    new = Ed25519PrivateKey.generate()
    succ = w.succession(1, new, [rec])
    groups.ingest_succession(w.s, w.group, succ, groups.utcnow())
    later = groups.utcnow() + timedelta(hours=25)
    assert groups.activate_successions(w.s, w.group, later) == 1
    return x, carried, root, new, later


def test_succession_activates_and_carries_members(w):
    x, carried, _, new, later = _activated(w)
    assert w.group.root_key_id == gc.key_id_of(gc.ed_public_b64(new)) and w.group.root_state == "moved"
    assert w.verdict(carried, later) == "ok"
    y = synth()
    fresh = w.root_cert(y, 99, root=new, root_key_id=w.group.root_key_id)
    assert w.verdict(fresh, later) == "ok"  # new-root objects are not bound by the carry


def test_old_root_forgery_reusing_carried_serial_refused(w):
    """y (T17)."""
    x, carried, old_root, _, later = _activated(w)
    serial = carried["body"]["serial"]
    n, r = serial.split("/")[1].split("-")
    forged = w.root_cert(x, int(n), r=r, root=old_root, root_key_id=w.group.genesis["group_id"],
                         start=groups.utcnow() - timedelta(days=1), member=w.member(x, rev=5))
    assert forged["body"]["serial"] == serial
    assert w.verdict(forged, later) in ("carry_mismatch", "not_carried")
    assert w.verdict(carried, later) == "ok"
    beyond = w.root_cert(synth(), int(n) + 50, root=old_root, root_key_id=w.group.genesis["group_id"])
    assert w.verdict(beyond, later) == "old_root_bound"


def test_old_root_backdated_crl_after_succession_refused(w):
    """z (T17)."""
    x, carried, old_root, _, later = _activated(w)
    crl = w.crl(None, [groups.crl_entry(carried, "removed", groups.utcnow() - timedelta(days=5))], 1, root=old_root,
                root_key_id=w.group.genesis["group_id"], issued=groups.utcnow() - timedelta(days=5))
    assert groups.ingest_crl(w.s, w.group, crl, later) == "invalid"
    assert w.verdict(carried, later) == "ok"


def test_genesis_replacement_refused(w, monkeypatch):
    """j (T2): a member serving another genesis for the same group id is refused."""
    other = World(monkeypatch)
    state = {"genesis": other.group.genesis}
    with pytest.raises(groups.GroupError, match="different genesis"):
        groups.ingest_state(w.s, w.group, state, w.me.node_id)


# ---- bb: root_moved ------------------------------------------------------------------------------------------

def test_two_root_holders_no_destructive_collision(w):
    """bb (T26): two holders issue concurrently; both objects survive; root_moved disables the old holder."""
    x, y = synth(), synth()
    root = w.root()
    first = w.root_cert(x, 20, root=root, r="aaaaaaaaaaaaaaaa")
    second = w.root_cert(y, 20, root=root, r="bbbbbbbbbbbbbbbb")  # same n from another holder
    w.ingest(first)
    w.ingest(second)
    assert w.verdict(first) == w.verdict(second) == "ok"
    other = synth()
    moved = gc.make_object("group.root_moved", w.gid, f"root_moved:{other.node_id}",
                           {"to_node": other.node_id, "issued_at": gc.stamp()},
                           {"kind": "root", "id": w.gid, "key_id": w.group.root_key_id}, root)
    assert groups.ingest_root_moved(w.s, w.group, moved, w.me.node_id, groups.utcnow()) == "stored"
    assert w.group.root_state == "moved"
    with pytest.raises(groups.GroupError, match="not on this node"):
        w.root()


# ---- aa: freshness ---------------------------------------------------------------------------------------------

def test_joiner_detects_withheld_crl_via_view(w):
    """aa (T24): a third member's view announcing a newer CRL blocks privileged actions until pulled."""
    x, y = synth(), synth()
    w.enrol(x)
    w.enrol(y)
    fresh = groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24)
    assert not fresh.ok and fresh.reason.startswith("no third-party view")
    issued = gc.stamp()
    body = {"node_id": y.node_id, "state_hash": "0" * 64, "crl_seq": {"root": 3}, "revoked_count": 1,
            "succession_index": 0, "succession_pending": False, "issued_at": issued}
    view = gc.make_object("group.view", w.gid, f"view:{y.node_id}/{issued}", body,
                          {"kind": "node", "id": y.node_id, "key_id": y.key_id}, y.private)
    assert groups.ingest_view(w.s, w.group, view, groups.utcnow()) == "stored"
    behind = groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24)
    assert not behind.ok and behind.behind_from == y.node_id
    body2 = {**body, "crl_seq": {}, "issued_at": gc.stamp(groups.utcnow() + timedelta(seconds=1))}
    view2 = gc.make_object("group.view", w.gid, f"view:{y.node_id}/{body2['issued_at']}", body2,
                           {"kind": "node", "id": y.node_id, "key_id": y.key_id}, y.private)
    groups.ingest_view(w.s, w.group, view2, groups.utcnow() + timedelta(seconds=2))
    assert groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24).ok
    assert w.s.get(GroupView, (w.gid, y.node_id)).body["body"]["crl_seq"] == {}


def test_two_member_waiver(w):
    x = synth()
    w.enrol(x)
    fresh = groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24)
    assert fresh.ok and fresh.waived


def test_view_signer_must_match_body(w):
    x, y = synth(), synth()
    w.enrol(x)
    w.enrol(y)
    issued = gc.stamp()
    body = {"node_id": y.node_id, "state_hash": "0" * 64, "crl_seq": {}, "revoked_count": 0,
            "succession_index": 0, "succession_pending": False, "issued_at": issued}
    forged = gc.make_object("group.view", w.gid, f"view:{y.node_id}/{issued}", body,
                            {"kind": "node", "id": x.node_id, "key_id": x.key_id}, x.private)
    assert groups.ingest_view(w.s, w.group, forged, groups.utcnow()) == "invalid"


# ---- hh: leave guard -------------------------------------------------------------------------------------------

def test_leave_refused_while_root_sealed_without_verified_recovery(monkeypatch):
    w = World(monkeypatch, recovery=3)  # 2-of-3 files hold no G
    assert groups.leave_allowed(w.s, w.group)
    with pytest.raises(groups.GroupError, match="holds the group root"):
        groups.leave(w.s, w.settings, w.group, w.me)
    one = World(monkeypatch)  # a 1-of-1 file holds G and was verified at creation
    assert groups.leave_allowed(one.s, one.group) is None
    crl = groups.leave(one.s, one.settings, one.group, one.me)
    assert crl["body"]["entries"][0]["reason"] == "left"
    assert one.group.status == "left" and one.group.root_sealed is None
