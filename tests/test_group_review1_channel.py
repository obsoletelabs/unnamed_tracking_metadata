"""C10 review #1 hardening of the private channel and sign-in (CONTRACT-C10 §C10.17: R1-4 expiry grace, R1-6 link
evidence witness, R1-7 mint binding, R1-12 remote factors). Each test runs the attack of its finding against real
nodes; every key and password is a synthetic runtime value."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from atlas import accounts, group_channel, group_schema, group_signin, groups, ids
from atlas import group_crypto as gc
from atlas.models import (
    AuditEntry,
    GroupAccountLink,
    GroupAccountNode,
    GroupChange,
    GroupStream,
    NodeState,
    Notification,
    ProfileConflict,
    User,
    UserMfa,
    UserSession,
)

from .channel_harness import (
    assertion,
    deliver,
    edit,
    entries_of,
    group_of,
    group_routes_pull,
    link,
    mint,
    rounds,
    shown,
    sync,
    uid_of,
    user,
)
from .group_harness import ROOT_PW, Dispatcher, fast_kdf, make_node


@pytest.fixture
def net(tmp_path, monkeypatch):
    fast_kdf(monkeypatch)
    group_channel._HELLO.clear()
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
    a, b = group_of(net.node("a"), net.node("b"))
    ua, ub = user(a, "alice"), user(b, "alicia")
    link(a, ua, b, ub)
    rounds(a, b)
    return a, b, ua, ub


@pytest.fixture
def trio(net):
    """a mints alice's account; c holds it too (linked through a); b is linked to nothing yet."""
    a, b, c = group_of(net.node("a"), net.node("b"), net.node("c"))
    ua, ub, uc = user(a, "alice"), user(b, "alicia"), user(c, "ally")
    uid = link(a, ua, c, uc)
    rounds(a, b, c)
    return a, b, c, ua, ub, uc, uid


def gid(node) -> str:
    with node.session() as s:
        return groups.load(s).group_id


def audited(node, action: str) -> list[dict]:
    with node.session() as s:
        return [r.detail or {} for r in s.scalars(select(AuditEntry).where(AuditEntry.action == action))]


def sign_into(node, uid: str, kind: str, body: dict) -> dict:
    with node.session() as s:
        group = groups.load(s)
        epoch = group_channel.ensure_epoch(s, group, node.identity)
        body = {"_v": 1, "issued_at": gc.stamp(groups.utcnow()), **body}
        ref = group_channel._append(s, group, node.identity, epoch, uid, kind, body, groups.utcnow())
        s.commit()
    origin, epoch, scope, seq = ref.split("/")
    return next(e for e in entries_of(node, origin, scope) if e["epoch"] == epoch and e["seq"] == int(seq))


def push(src, dst, uid: str, *extra, now=None) -> list:
    """Deliver `src`'s epoch entries and its stream of `uid` (plus `extra`) to `dst`, as a pull from `src` would."""
    return deliver(dst, [*entries_of(src, src.node_id, "_node"), *entries_of(src, src.node_id, uid), *extra],
                   now=now, served_by=src.node_id)


def held_by(node, uid: str, holder) -> GroupAccountNode | None:
    with node.session() as s:
        row = s.get(GroupAccountNode, (gid(node), uid, holder.node_id))
        if row is not None:
            s.expunge(row)
        return row


def witnesses(node, uid: str) -> list[dict]:
    with node.session() as s:
        return [r.body for r in s.scalars(select(GroupChange).where(GroupChange.scope == uid,
                                                                    GroupChange.kind == "account.assert_issued"))]


def link_without_witness(idp, idp_user: str, rp, rp_user: str, purpose: str = "link") -> dict:
    """What a pre-review IdP (or a stolen key) produces: a valid assertion with no witness in the IdP's stream."""
    a = assertion(idp, idp_user, rp, purpose=purpose, witness=False)
    with rp.session() as s:
        group = groups.load(s)
        if purpose == "link":
            group_channel.complete_link(s, rp.settings, group, rp.identity, s.get(User, rp_user),
                                        a["body"]["account_uid"], a)
        else:  # the auto-provisioned account at the relying party links with the sign-in assertion
            s.add(GroupAccountLink(group_id=group.group_id, account_uid=a["body"]["account_uid"], user_id=rp_user,
                                   minted_here=False, linked_at=groups.utcnow()))
            group_channel.emit_link(s, group, rp.identity, a["body"]["account_uid"], minted=False, evidence=a)
        s.commit()
    return a


def age_witness_waits(node, days: float) -> int:
    with node.session() as s:
        rows = s.scalars(select(NodeState).where(NodeState.key.like(group_channel.WITNESS_WAIT_PREFIX + "%"))).all()
        for row in rows:
            row.value = gc.stamp(groups.utcnow() - timedelta(days=days))
        s.commit()
        return len(rows)


def _cert_end(node, of) -> datetime:
    with node.session() as s:
        return gc.parse_time(groups.trust(s, groups.load(s)).in_force(of.node_id)["body"]["not_after"])


# ---- R1-4: entries inside the expiry grace are held ------------------------------------------------------------

def _held_in_grace(pair):
    a, b, ua, ub = pair
    edit(b, ub, "theme", "dark")
    uid = uid_of(b, ub)
    entries = entries_of(b, b.node_id, uid)
    theme = next(e for e in entries if e["subject"] == "theme")
    end = _cert_end(a, b)
    with a.session() as s:  # the head b announced before its certificate ended
        row = s.get(GroupStream, (gid(a), b.node_id, theme["epoch"], uid))
        row.advertised_head, row.advertised_at = theme["seq"], end - timedelta(hours=1)
        s.commit()
    outs = deliver(a, entries, now=end + timedelta(hours=1), served_by=b.node_id)
    assert outs == [(theme["seq"], "held", "origin_expired")]
    assert shown(a, ua, "theme") is None  # nothing from an expired origin is applied on the strength of the grace
    return a, b, ua, uid, theme, end


def _release(a, at: datetime) -> dict:
    with a.session() as s:
        group = groups.load(s)
        out = group_channel.release_expired_held(s, a.settings, a.identity, group,
                                                 groups.trust(s, group, at, own_node=a.node_id), at)
        s.commit()
        return out


def test_grace_entries_held_then_applied_after_renewal(pair):
    a, b, ua, uid, theme, end = _held_in_grace(pair)
    assert _release(a, end + timedelta(hours=2)) == {"applied": 0, "dropped": 0}  # still expired: still held
    with a.session() as s:  # the owner renews b (the renewal is in force past the old end)
        group = groups.load(s)
        groups.renew(s, a.settings, group, a.identity, [b.node_id], root=groups.unlock_root(s, a.settings, group,
                                                                                               ROOT_PW),
                     now=end - timedelta(hours=1))
        s.commit()
    assert _release(a, end + timedelta(hours=2))["applied"] == 1
    assert shown(a, ua, "theme") == "dark"
    with a.session() as s:
        row = s.get(GroupChange, (gid(a), b.node_id, theme["epoch"], uid, theme["seq"]))
        stream = s.get(GroupStream, (gid(a), b.node_id, theme["epoch"], uid))
        assert row.disposition == "applied" and stream.hwm == theme["seq"] and stream.blocked_seq is None


def test_grace_entries_dropped_after_window_with_notice(pair):
    a, b, ua, uid, theme, end = _held_in_grace(pair)
    assert _release(a, end + timedelta(hours=73)) == {"applied": 0, "dropped": 1}
    assert shown(a, ua, "theme") is None
    with a.session() as s:
        row = s.get(GroupChange, (gid(a), b.node_id, theme["epoch"], uid, theme["seq"]))
        stream = s.get(GroupStream, (gid(a), b.node_id, theme["epoch"], uid))
        assert (row.disposition, row.reason) == ("rejected", "origin_expired")
        assert stream.hwm == theme["seq"] and stream.blocked_seq is None
        notes = s.scalars(select(Notification).where(Notification.key == f"group-expired-held:{b.node_id}")).all()
        assert len(notes) == 1 and "were not applied" in notes[0].title
    assert audited(a, "group_expired_held_dropped") == [{"entries": 1}]


# ---- R1-6: link evidence needs the issuer's witness -------------------------------------------------------------

def test_backdated_evidence_with_rotated_key_rejected(trio):
    """The finding: a's old key K1 (rotated away, then stolen) signs an assertion dated before the rotation for a
    compromised b. Its signature verifies under K1 and no rotation epoch predates it, so it used to link b."""
    from atlas import key_rotation

    a, b, c, ua, ub, uc, uid = trio
    before = groups.utcnow()
    old = a.identity
    with a.session() as s:
        successor = key_rotation.rotate(s, a.identity.key_id, a.settings)
        s.commit()
    a.st.ctx.identity = successor
    with a.session() as s:
        group = groups.load(s)
        groups.rebuild(s, group, a.node_id, settings=a.settings)
        group_channel.ensure_epoch(s, group, successor)
        s.commit()
    group_routes_pull(c)
    sync(c)
    with a.session() as s:  # only the signing: the thief holds K1, not a's database
        forged = group_channel.sign_assertion(s, groups.load(s), old, aud_node=b.node_id, uid=uid, nonce="n" * 32,
                                              state_hash="s" * 43, auth_time=before, amr=["pwd"],
                                              username_hint="mallory", now=before, witness=False)
        s.rollback()
    claim = sign_into(b, uid, "account.link", {"account_uid": uid, "node_id": b.node_id, "minted": None,
                                               "evidence": forged,
                                               "issued_at": gc.stamp(before + timedelta(seconds=30))})
    outs = push(b, c, uid, claim)
    assert outs[-1][1:] == (None, "evidence_pending")
    assert held_by(c, uid, b) is None


def test_link_with_witness_applies(trio):
    a, b, c, ua, ub, uc, uid = trio
    evidence = assertion(a, ua, b)
    with b.session() as s:
        group_channel.complete_link(s, b.settings, groups.load(s), b.identity, s.get(User, ub), uid, evidence)
        s.commit()
    rounds(a, b, c)
    assert held_by(c, uid, b).status == "linked"
    assert any(w["jti"] == evidence["body"]["jti"] and w["aud_node"] == b.node_id
               and w["assertion_hash"] == evidence["body_hash"] for w in witnesses(c, uid))


def test_snapshot_carries_witness(trio):
    a, b, c, ua, ub, uc, uid = trio
    evidence = assertion(a, ua, b)
    with b.session() as s:
        group_channel.complete_link(s, b.settings, groups.load(s), b.identity, s.get(User, ub), uid, evidence)
        s.commit()
    rounds(a, b, c)
    with c.session() as s:
        group = groups.load(s)
        snap = group_channel.serve_snapshot(s, group, c.node_id, b.node_id, {"account_uid": uid, "accept": []})
        kinds = [e["kind"] for e in snap["entries"]]
        assert any(e["kind"] == "account.assert_issued" and e["body"]["jti"] == evidence["body"]["jti"]
                   for e in snap["entries"])
        rx = group_channel.Rx(s, c.settings, c.identity, group, groups.trust(s, group, own_node=c.node_id),
                              groups.utcnow(), b.node_id)
        ordered = [e["kind"] for e, body, _ in group_channel.verified_snapshot(rx, uid, snap)[0]
                   if e["kind"] != "account.link" or not body.get("minted")]
        # the witnesses come before the links whose evidence they back
        assert ordered.index("account.assert_issued") < ordered.index("account.link")
        assert "account.assert_issued" in kinds


def test_missing_witness_turns_suspect_after_hold_timeout(trio):
    a, b, c, ua, ub, uc, uid = trio
    link_without_witness(a, ua, b, ub)
    assert push(b, c, uid)[-1][1:] == (None, "evidence_pending")
    assert held_by(c, uid, b) is None
    assert push(b, c, uid)[-1][1:] == (None, "evidence_pending")  # still inside group_hold_timeout_days
    assert age_witness_waits(c, 8) == 1
    assert push(b, c, uid)[-1][1:] == ("applied", "")
    assert held_by(c, uid, b).status == "suspect"
    with c.session() as s:
        row = s.scalars(select(ProfileConflict).where(ProfileConflict.kind == "suspect_link")).one()
        assert (row.user_id, row.loser_hlc) == (uc, b.node_id)
    assert audited(c, "account_link_witness_missing")[0]["node"] == b.node_id


def test_existing_links_grandfathered(trio, monkeypatch):
    a, b, c, ua, ub, uc, uid = trio
    evidence = assertion(a, ua, b)
    with b.session() as s:
        group_channel.complete_link(s, b.settings, groups.load(s), b.identity, s.get(User, ub), uid, evidence)
        s.commit()
    rounds(a, b, c)
    assert held_by(c, uid, b).status == "linked"
    with c.session() as s:  # a database from before the upgrade holds no witnesses
        for row in s.scalars(select(GroupChange).where(GroupChange.kind == "account.assert_issued")):
            s.delete(row)
        s.commit()
    rounds(a, b, c)
    assert held_by(c, uid, b).status == "linked"  # a link already evaluated is not evaluated again
    # evidence issued before the review #1 rules (an IdP that could not witness) is suspect, never rejected
    monkeypatch.setattr(group_channel, "R1_SINCE", datetime(2100, 1, 1, tzinfo=UTC))
    bea = user(b, "bea")
    link_without_witness(a, user(a, "anna"), b, bea)
    uid2 = uid_of(b, bea)
    outs = push(b, a, uid2)
    assert outs[-1][1:] == ("applied", "")
    assert held_by(a, uid2, b).status == "suspect"
    with a.session() as s:
        assert s.scalar(select(func.count()).select_from(ProfileConflict).where(
            ProfileConflict.kind == "suspect_link")) == 1


# ---- R1-7: the account uid is bound to its minter ---------------------------------------------------------------

def _minted_claim(node, uid: str, **extra) -> dict:
    return sign_into(node, uid, "account.link", {"account_uid": uid, "node_id": node.node_id, "minted": True,
                                                 "evidence": None, **extra})


def test_minted_claim_by_non_minter_refused(net):
    """The finding: b claims to have minted a's new uid and its claim reaches c first; first-writer-wins then
    refused the real minter."""
    a, b, c = group_of(net.node("a"), net.node("b"), net.node("c"))
    uid = mint(a, user(a, "alice"))
    assert push(b, c, uid, _minted_claim(b, uid))[-1][1:] == ("rejected", "not_minter")
    assert push(b, c, uid, _minted_claim(b, uid, mint_nonce="0" * 32))[-1][1:] == ("rejected", "not_minter")
    assert held_by(c, uid, b) is None
    assert push(a, c, uid)[-1][1:] == ("applied", "")
    assert held_by(c, uid, a).status == "linked"


def test_bound_uid_is_valid_ulid(net):
    (a,) = group_of(net.node("a"))
    started = groups.utcnow()
    uid = mint(a, user(a, "alice"))
    assert group_schema.ULID.match(uid)
    assert abs(group_channel._ulid_time(uid) - started) < timedelta(minutes=1)
    body = entries_of(a, a.node_id, uid)[0]["body"]
    assert body["minted"] is True and group_schema.MINT_NONCE.match(body["mint_nonce"])
    assert group_channel.bound_uid(gid(a), a.node_id, body["mint_nonce"], group_channel._ulid_ms(uid)) == uid
    assert group_channel.bound_uid(gid(a), "0" * 16, body["mint_nonce"], group_channel._ulid_ms(uid)) != uid
    assert ids.ulid_bound(0, b"\x00" * 10) == "0" * 26
    with pytest.raises(ValueError):
        ids.ulid_bound(0, b"short")


def test_legacy_conflicting_mint_claims_both_suspect(net, monkeypatch):
    monkeypatch.setattr(group_channel, "R1_SINCE", datetime(2100, 1, 1, tzinfo=UTC))
    a, b, c = group_of(net.node("a"), net.node("b"), net.node("c"))
    uc = user(c, "ally")
    uid = ids.ulid()  # minted before the binding: random bits, no nonce
    with c.session() as s:
        s.add(GroupAccountLink(group_id=gid(c), account_uid=uid, user_id=uc, minted_here=False,
                               linked_at=groups.utcnow()))
        s.commit()
    assert push(a, c, uid, _minted_claim(a, uid))[-1][1:] == ("applied", "")
    assert push(b, c, uid, _minted_claim(b, uid))[-1][1:] == ("applied", "")
    assert held_by(c, uid, a).status == held_by(c, uid, b).status == "suspect"
    with c.session() as s:
        losers = sorted(r.loser_hlc for r in s.scalars(select(ProfileConflict).where(
            ProfileConflict.kind == "suspect_link", ProfileConflict.user_id == uc)))
    assert losers == sorted([a.node_id, b.node_id])
    assert audited(c, "account_mint_conflict")[0]["legacy"] is True


def test_mint_nonce_required_for_new_links(net):
    a, b, c = group_of(net.node("a"), net.node("b"), net.node("c"))
    uid = ids.ulid()  # a uid of today with no nonce: no honest node of this release mints one
    assert push(b, c, uid, _minted_claim(b, uid))[-1][1:] == ("rejected", "not_minter")
    assert held_by(c, uid, b) is None
    base = {"account_uid": uid, "node_id": b.node_id, "issued_at": gc.stamp()}
    assert group_schema.classify("account.link", {**base, "minted": True, "evidence": None,
                                                  "mint_nonce": "xyz"}).action == group_schema.QUARANTINE
    assert group_schema.classify("account.link", {**base, "minted": None, "evidence": {},
                                                  "mint_nonce": "0" * 32}).action == group_schema.QUARANTINE
    assert group_schema.classify("account.link", {**base, "minted": True, "evidence": None,
                                                  "mint_nonce": "0" * 32}).action == group_schema.APPLY


# ---- R1-6 (sign-in half) ----------------------------------------------------------------------------------------

def test_signin_assertion_witnessed_for_auto_provision_evidence(net):
    a, b = group_of(net.node("a"), net.node("b"))
    ua = user(a, "alice")
    a_signin = assertion(a, ua, b, purpose="signin")
    uid = a_signin["body"]["account_uid"]
    (w,) = witnesses(a, uid)
    assert w == {**w, "jti": a_signin["body"]["jti"], "aud_node": b.node_id, "purpose": "signin",
                 "assertion_hash": a_signin["body_hash"], "account_uid": uid}


def test_auto_provision_link_without_witness_suspect_elsewhere(trio):
    a, b, c, ua, ub, uc, uid = trio
    link_without_witness(a, ua, b, ub, purpose="signin")
    assert push(b, c, uid)[-1][1:] == (None, "evidence_pending")
    age_witness_waits(c, 8)
    assert push(b, c, uid)[-1][1:] == ("applied", "")
    assert held_by(c, uid, b).status == "suspect"


def test_signin_at_linked_node_not_witnessed(pair):
    a, b, ua, ub = pair
    uid = uid_of(a, ua)
    linked = len(witnesses(a, uid))
    assert linked == 1  # the link itself was witnessed
    assertion(a, ua, b, purpose="signin")  # b holds a live link: a sign-in there is no link evidence
    assert len(witnesses(a, uid)) == linked


def test_live_signin_does_not_wait_for_witness(pair):
    a, b, ua, ub = pair
    a_signin = assertion(a, ua, b, purpose="signin", witness=False)
    with b.session() as s:
        group = groups.load(s)
        found, provisioned = group_signin.signin_user(s, b.settings, b.identity, group, a_signin)
        assert (found.id, provisioned) == (ub, False)


# ---- R1-12: remote factors (tests only) --------------------------------------------------------------------------

def test_future_auth_time_never_counts_as_remote_otp(pair):
    a, b, ua, ub = pair
    settings = b.settings.model_copy(update={"group_trust_remote_mfa": True})
    with b.session() as s:
        s.add(UserMfa(user_id=ub, secret_enc="synthetic", setup_session_id="x", enabled_at=groups.utcnow()))
        s.flush()
        group = groups.load(s)
        for auth_time, counted in ((groups.utcnow() + timedelta(hours=1), False), (groups.utcnow(), True)):
            body = {"iss_node": a.node_id, "jti": ids.ulid(), "amr": ["pwd", "otp"], "auth_time": gc.stamp(auth_time)}
            _, row = group_signin.open_session(s, settings, s.get(User, ub), group, {"body": body})
            assert (row.mfa_verified_at is not None) is counted
        s.rollback()


def test_remote_otp_never_grants_privilege():
    now = groups.utcnow()
    remote = UserSession(via_group="g", amr=["pwd", "otp"], mfa_verified_at=now)
    assert not accounts.local_mfa_done(remote)  # another node's otp never stands in for this node's own factor
    assert accounts.local_mfa_done(UserSession(via_group="g", amr=["pwd", accounts.LOCAL_OTP], mfa_verified_at=now))
    assert accounts.local_mfa_done(UserSession(via_group=None, amr=None, mfa_verified_at=now))
