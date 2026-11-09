"""C10 review #1 hardening of trust, revocation, succession, freshness and the revocation journal (CONTRACT-C10
§C10.17: R1-1, R1-2, R1-3, R1-5, R1-9, R1-10, R1-11 pin, R1-13). Each test runs the attack of its finding against a
single-database world whose keys are generated at runtime; passphrases are synthetic test values."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, select

from atlas import group_crypto as gc
from atlas import groups
from atlas.models import AuditEntry, GroupBannedNode, GroupCrl, GroupRevoked, NodeState
from atlas.signing import body_hash, canonical_json, sign_message

from .conftest import build_settings
from .group_harness import REC_PW, ROOT_PW
from .group_world import World, rotation, synth

VECTORS = Path(__file__).parent / "fixtures" / "group_canonical_vectors.json"


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


def later(hours: float = 25):
    return groups.utcnow() + timedelta(hours=hours)


def view(w, ident, at, **body) -> dict:
    issued = gc.stamp(at)
    b = {"node_id": ident.node_id, "state_hash": "0" * 64, "crl_seq": {}, "revoked_count": 0, "succession_index": 0,
         "succession_pending": False, "issued_at": issued, **body}
    return gc.make_object("group.view", w.gid, f"view:{ident.node_id}/{issued}", b,
                          {"kind": "node", "id": ident.node_id, "key_id": ident.key_id}, ident.private)


def see(w, ident, at=None, **body) -> str:
    at = at or groups.utcnow()
    return groups.ingest_view(w.s, w.group, view(w, ident, at, **body), at, own_node=w.me.node_id)


def carry_of(*certs) -> list[dict]:
    return [{"serial": c["body"]["serial"], "body_hash": c["body_hash"]} for c in certs]


def own_cert(w) -> dict:
    return w.trust().in_force(w.me.node_id)


def succeed(w, *, delivered_by, last_crl=None, carry=None, at=None) -> tuple[Ed25519PrivateKey, str, object]:
    """Ingest succession 1 (delivered by `delivered_by`) and try to activate it after the delay."""
    (rec,) = w.recovery_keys()
    old_kid = w.group.root_key_id
    new = Ed25519PrivateKey.generate()
    obj = w.succession(1, new, [rec], last_crl=last_crl, carry=carry)
    assert groups.ingest_succession(w.s, w.group, obj, groups.utcnow(), delivered_by=delivered_by) == "pending"
    at = at or later()
    groups.activate_successions(w.s, w.group, at, w.me.node_id)
    return new, old_kid, at


def last_of(crl: dict) -> dict:
    return {"crl_seq": crl["body"]["crl_seq"], "body_hash": crl["body_hash"]}


# ---- R1-1: the old root's revocation state is exactly old_root_last_crl ------------------------------------------

def test_old_root_lower_seq_crl_after_activation_ignored(w):
    """The finding: a thief of the old G signs a CRL with a seq below old_root_last_crl after the succession."""
    x = synth()
    cx = w.enrol(x)
    old_root = w.root()
    honest = w.crl(None, [], 5, root=old_root)
    w.apply(honest)
    _, old_kid, at = succeed(w, delivered_by=x.node_id, last_crl=last_of(honest))
    assert w.group.succession
    forged = w.crl(None, [groups.crl_entry(cx, "key_compromise", groups.utcnow(), groups.utcnow())], 3,
                   root=old_root, root_key_id=old_kid)
    assert groups.ingest_crl(w.s, w.group, forged, at) == "invalid"
    groups.rebuild(w.s, w.group, w.me.node_id, at)
    assert revoked(w, cx["body"]["serial"]) is None and w.verdict(cx, at) == "ok"
    assert w.s.get(GroupBannedNode, (w.gid, x.node_id)) is None


def test_old_root_crl_held_while_succession_pending(w):
    x = synth()
    cx = w.enrol(x)
    old_root = w.root()
    (rec,) = w.recovery_keys()
    succ = w.succession(1, Ed25519PrivateKey.generate(), [rec])
    groups.ingest_succession(w.s, w.group, succ, groups.utcnow(), delivered_by=x.node_id)
    ban = w.crl(None, [groups.crl_entry(cx, "removed", groups.utcnow())], 1, root=old_root)
    assert w.apply(ban) == "held"
    assert revoked(w, cx["body"]["serial"]) is None and w.verdict(cx) == "ok"
    assert w.s.get(NodeState, groups.CRL_HELD_PREFIX + ban["body_hash"][:40]) is not None
    at = later()
    groups.rebuild(w.s, w.group, w.me.node_id, at)  # activates: the held CRL is not old_root_last_crl
    assert w.group.succession and w.verdict(cx, at) == "ok"
    assert [a.detail["outcome"] for a in audited(w, "group_crl_released")] == ["invalid"]


def test_pre_succession_forged_ban_retired_on_activation(w):
    """A thief of G bans x before the owner notices; the succession names the last honest CRL, so the ban goes."""
    x, y = synth(), synth()
    cx, cy = w.enrol(x), w.enrol(y)
    root = w.root()
    honest = w.crl(None, [groups.crl_entry(cy, "removed", groups.utcnow())], 1, root=root)
    w.apply(honest)
    w.apply(w.crl(None, [*honest["body"]["entries"], groups.crl_entry(cx, "removed", groups.utcnow())], 2,
                  root=root))
    assert w.verdict(cx) == "revoked" and w.s.get(GroupBannedNode, (w.gid, x.node_id)) is not None
    _, _, at = succeed(w, delivered_by=x.node_id, last_crl=last_of(honest), carry=carry_of(own_cert(w), cx))
    assert w.group.succession
    assert revoked(w, cx["body"]["serial"]) is None and w.s.get(GroupBannedNode, (w.gid, x.node_id)) is None
    assert w.verdict(cx, at) == "ok"
    assert revoked(w, cy["body"]["serial"]).reason == "removed"  # the honest revocation stays
    (entry,) = audited(w, "group_revocation_retired_by_succession")
    assert cx["body"]["serial"] in entry.detail["retired"]


def test_activation_without_last_crl_blocks_freshness_until_it_arrives(w):
    x = synth()
    w.enrol(x)
    last = w.crl(None, [], 2, root=w.root())  # signed, but never reached this node
    _, _, at = succeed(w, delivered_by=x.node_id, last_crl=last_of(last))
    assert w.group.succession
    fresh = groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24, at)
    assert not fresh.ok and fresh.reason == "succession_crl_missing"
    assert groups.ingest_crl(w.s, w.group, last, at) == "stored"
    assert w.s.get(NodeState, groups.SUCCESSION_CRL_MISSING) is None
    assert groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24, at).ok


def test_root_crl_not_compacted_by_covering_crl(w):
    x, y = synth(), synth()
    cx, cy = w.enrol(x), w.enrol(y)
    root = w.root()
    first = w.crl(None, [groups.crl_entry(cx, "removed", groups.utcnow())], 1, root=root)
    w.apply(first)
    w.apply(w.crl(None, [*first["body"]["entries"], groups.crl_entry(cy, "removed", groups.utcnow())], 2, root=root))
    seqs = sorted(r.crl_seq for r in w.s.scalars(select(GroupCrl).where(GroupCrl.issuer == "root")))
    assert seqs == [1, 2]  # either may become a succession's old_root_last_crl


def test_forged_old_root_reason_upgrade_retired(w):
    x = synth()
    cx = w.enrol(x)
    root = w.root()
    now = groups.utcnow()
    honest = w.crl(None, [groups.crl_entry(cx, "removed", now)], 1, root=root)
    w.apply(honest)
    w.apply(w.crl(None, [groups.crl_entry(cx, "key_compromise", now, now - timedelta(days=30))], 2, root=root))
    assert revoked(w, cx["body"]["serial"]).reason == "key_compromise"
    succeed(w, delivered_by=x.node_id, last_crl=last_of(honest), carry=carry_of(own_cert(w)))
    assert w.group.succession
    row = revoked(w, cx["body"]["serial"])
    assert row.reason == "removed" and row.suspect_since is None
    assert w.s.get(GroupBannedNode, (w.gid, x.node_id)).reason == "removed"  # readmissible again


# ---- R1-2: freshness compares CRL content, not only seq ---------------------------------------------------------

def test_same_seq_crl_fork_blocks_freshness_until_pulled(w):
    x, y = synth(), synth()
    w.enrol(x)
    w.enrol(y)
    root = w.root()
    mine = w.crl(None, [], 3, root=root)
    theirs = w.crl(None, [], 3, root=root, issued=groups.utcnow() - timedelta(minutes=5))
    assert mine["body_hash"] != theirs["body_hash"]
    w.apply(mine)
    see(w, y, crl_seq={"root": 3}, crl_top={"root": [3, [theirs["body_hash"]]]})
    fresh = groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24)
    assert not fresh.ok and fresh.behind_from == y.node_id
    w.apply(theirs)
    assert groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24).ok


def test_view_without_crl_top_still_accepted(w):
    x, y = synth(), synth()
    w.enrol(x)
    w.enrol(y)
    w.apply(w.crl(None, [], 3))
    assert see(w, y, crl_seq={"root": 3}) == "stored"  # an A12 member's view
    assert groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24).ok


def test_waiver_is_audited_once_per_action(w):
    x = synth()
    w.enrol(x)
    groups._WAIVED_SEEN.clear()
    for action in ("revoke", "revoke", "issue"):
        fresh = groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24, action=action)
        assert fresh.ok and fresh.waived
    assert sorted(a.detail["action"] for a in audited(w, "group_freshness_waived")) == ["issue", "revoke"]


def test_unbacked_view_ignored_after_pull(w):
    x, y = synth(), synth()
    w.enrol(x)
    w.enrol(y)
    see(w, x)
    see(w, y, crl_seq={"root": 9})  # y announces a CRL it does not serve
    assert groups.freshness(w.s, w.group, w.me.node_id, None, 24).behind_from == y.node_id
    assert groups.check_view_backed(w.s, w.group, y.node_id) is False  # what `pull` runs after y's state
    assert groups.freshness(w.s, w.group, w.me.node_id, None, 24).ok
    assert len(audited(w, "group_view_unbacked")) == 1
    see(w, y, groups.utcnow() + timedelta(seconds=2))  # a newer, backed view of y counts again
    assert groups.check_view_backed(w.s, w.group, y.node_id) is True
    assert w.s.get(NodeState, groups.VIEW_UNBACKED_PREFIX + y.node_id) is None


# ---- R1-3: the revocation journal survives a restore -------------------------------------------------------------

def test_restore_replays_journal_revocations(w, tmp_path):
    from atlas import backup

    x = synth()
    cx = w.enrol(x)
    w.s.commit()
    archive = tmp_path / "before.tar"
    backup.create(w.settings, w.engine, archive, include_media=False)
    w.apply(w.crl(None, [groups.crl_entry(cx, "removed", groups.utcnow())], 1))  # after the backup
    w.s.commit()
    w.s.close()
    backup.restore(w.settings, w.engine, archive, force=True, include_media=False)
    with w.maker() as s:
        assert s.get(GroupRevoked, (w.gid, cx["body"]["serial"])) is not None
        assert s.get(NodeState, "group_journal_replay_due") is None
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "group_journal_replayed")).first()
    w.s = w.maker()


def test_resync_not_done_by_revoked_only_peer(w):
    from atlas.api import group_routes

    x, y = synth(), synth()
    w.enrol(x)
    cy = w.enrol(y)
    w.apply(w.crl(None, [groups.crl_entry(cy, "removed", groups.utcnow())], 1))
    w.group.status = "resyncing"
    group_routes._finish_resync(w.s, w.settings, w.me, w.group, {y.node_id: "ok"})
    assert w.group.status == "resyncing"  # only the revoked node answered
    group_routes._finish_resync(w.s, w.settings, w.me, w.group, {x.node_id: "ok"})
    assert w.group.status == "active"


def test_resync_skip_disables_two_member_waiver(w):
    x = synth()
    w.enrol(x)
    w.group.status = "resyncing"
    groups.skip_resync(w.s, w.group)
    fresh = groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24)
    assert w.group.status == "active" and not fresh.ok and fresh.reason == "resync_skipped"
    groups.confirm_trust_epoch(w.s, w.settings, w.group, ROOT_PW)
    assert groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24).waived
    assert audited(w, "group_trust_epoch_skipped") and audited(w, "group_resync_confirmed")


def test_journal_dir_inside_media_refused(tmp_path):
    with pytest.raises(ValueError, match="must not be inside media_dir"):
        build_settings(media_dir=str(tmp_path / "media"), group_journal_dir=str(tmp_path / "media" / "journal"))
    with pytest.raises(ValueError, match="must not be inside backup_dir"):
        build_settings(backup_dir=str(tmp_path / "b"), group_journal_dir=str(tmp_path / "b"))
    assert build_settings(media_dir=str(tmp_path / "media"), group_journal_dir=str(tmp_path / "journal"))


def test_tampered_journal_line_ignored(w):
    from atlas import group_journal

    x, y = synth(), synth()
    cx, cy = w.enrol(x), w.enrol(y)
    crl = w.crl(None, [groups.crl_entry(cx, "removed", groups.utcnow())], 1)
    w.apply(crl)
    path = group_journal.path_for(w.settings, w.gid)
    retarget = json.loads(json.dumps(crl))
    retarget["body"]["entries"] = [groups.crl_entry(cy, "removed", groups.utcnow())]  # the hash no longer matches
    rehashed = json.loads(json.dumps(retarget))
    rehashed["body_hash"] = body_hash(rehashed["body"])  # the hash matches, the root signature does not
    with open(path, "a", encoding="utf-8") as fh:
        for obj in (retarget, rehashed):
            fh.write(json.dumps({"kind": "crl", "obj": obj}) + "\n")
        fh.write("{torn line\n")
    w.s.execute(delete(GroupCrl))
    w.s.execute(delete(GroupRevoked))
    w.s.execute(delete(GroupBannedNode))
    counts = group_journal.replay(w.s, w.group, w.me.node_id)
    assert counts == {"crl:stored": 1, "crl:invalid": 2}
    assert revoked(w, cx["body"]["serial"]) is not None and revoked(w, cy["body"]["serial"]) is None


def test_journal_keeps_activated_succession_active(w):
    from atlas import group_journal

    x = synth()
    w.enrol(x)
    before = (w.group.root_key_id, w.group.recovery, w.group.root_state)
    new, _, at = succeed(w, delivered_by=x.node_id)
    new_kid = gc.key_id_of(gc.ed_public_b64(new))
    assert w.group.root_key_id == new_kid
    kinds = [line["kind"] for line in group_journal.read(w.settings, w.gid)]
    assert kinds == ["succession", "activation"]
    # a restore from a backup taken before the succession
    w.group.succession, w.group.succession_pending = [], []
    w.group.root_key_id, w.group.recovery, w.group.root_state = before
    w.s.merge(NodeState(key=group_journal.REPLAY_DUE, value="1"))
    w.s.flush()
    group_journal.replay(w.s, w.group, w.me.node_id)
    assert w.group.root_key_id == new_kid and len(w.group.succession) == 1  # active again, no second delay
    assert not groups.pending_successions(w.group)


def test_resync_completes_on_journal_when_no_member_left(w):
    from atlas.api import group_routes

    x = synth()
    cx = w.enrol(x)
    w.apply(w.crl(None, [groups.crl_entry(cx, "removed", groups.utcnow())], 1))
    w.group.status = "resyncing"
    group_routes._finish_resync(w.s, w.settings, w.me, w.group, {})
    w.s.flush()
    assert w.group.status == "active"
    assert audited(w, "group_resync_done")[-1].detail["journal_only"] is True


# ---- R1-5: recovery file v2 and confirmed activation --------------------------------------------------------------

def test_recovery_file_v2_needs_root_passphrase_for_g(w):
    keys = gc.open_recovery_file(w.files[0], REC_PW[0])
    assert keys.version == 2 and keys.root is None and keys.root_wrapped is not None
    with pytest.raises(gc.WrongPassphrase):
        gc.recovery_root(keys, None)
    with pytest.raises(gc.WrongPassphrase):
        gc.recovery_root(keys, REC_PW[0])  # the file passphrase does not unwrap G
    assert gc.key_id_of(gc.ed_public_b64(gc.recovery_root(keys, ROOT_PW))) == w.group.root_key_id
    assert groups.verify_recovery_root(keys, w.group, ROOT_PW)


def test_recovery_file_v1_still_opens(w):
    rec, root = w.recovery_keys()[0], w.root()
    rid = gc.key_id_of(gc.ed_public_b64(rec))
    plain = canonical_json({"recovery_private": gc.b64(gc.ed_private_raw(rec)),
                            "root_private": gc.b64(gc.ed_private_raw(root))})
    v1 = {"format": gc.RECOVERY_FORMAT, "v": 1, "group_id": w.gid, "name": "World", "created_at": gc.stamp(),
          "genesis": w.group.genesis, "succession": [], "recovery_key_id": rid, "contains": ["recovery_key", "root_key"],
          "wrapped": gc.wrap(plain, REC_PW[0], gc.recovery_aad(w.gid, rid))}
    keys = gc.open_recovery_file(v1, REC_PW[0])
    assert keys.version == 1 and keys.legacy_raw_root and keys.recovery is not None
    assert groups.verify_recovery_root(keys, w.group, None)


def test_stolen_file_without_root_passphrase_only_allows_succession(w):
    x = synth()
    w.enrol(x)
    keys = gc.open_recovery_file(w.files[0], REC_PW[0])  # the thief cracked the file passphrase only
    with pytest.raises(groups.GroupError, match="wrong root passphrase"):
        groups.import_root(w.s, w.settings, w.group, w.me, keys, None, "running")
    with pytest.raises(groups.GroupError, match="wrong root passphrase"):
        groups.verify_recovery_root(keys, w.group, "a guess at the root passphrase")
    succ = w.succession(1, Ed25519PrivateKey.generate(), [keys.recovery])  # visible, delayed and cancellable
    assert groups.ingest_succession(w.s, w.group, succ, groups.utcnow()) == "pending"


def test_export_recovery_v2_wraps_g(w):
    phrase = "synthetic export phrase 55aa"
    root = w.root()
    with pytest.raises(gc.GroupCryptoError, match="root passphrase"):
        gc.make_recovery_file(w.group.genesis, [], None, root, phrase)
    data = gc.make_recovery_file(w.group.genesis, [], None, root, phrase, root_passphrase=ROOT_PW)
    assert data["v"] == 2 and data["contains"] == ["root_key_wrapped"]
    inner = json.loads(gc.unwrap(data["wrapped"], phrase, gc.recovery_aad(w.gid, "root")))
    assert "root_private" not in inner and inner["root_wrapped"]
    keys = gc.open_recovery_file(data, phrase)
    assert keys.root is None and gc.key_id_of(gc.ed_public_b64(gc.recovery_root(keys, ROOT_PW))) == w.gid


def test_isolated_member_does_not_activate_without_post_delay_view(w):
    """The finding: a member cut off from the others activates a succession that was cancelled elsewhere."""
    x, y = synth(), synth()
    w.enrol(x)
    w.enrol(y)
    (rec,) = w.recovery_keys()
    succ = w.succession(1, Ed25519PrivateKey.generate(), [rec])
    groups.ingest_succession(w.s, w.group, succ, groups.utcnow(), delivered_by=x.node_id)
    see(w, y, succession_pending=True)  # before the delay ended: no confirmation
    at = later()
    assert groups.activate_successions(w.s, w.group, at, w.me.node_id) == 0
    assert audited(w, "group_succession_awaiting_confirmation")
    see(w, x, at, succession_pending=True)  # the deliverer never confirms its own delivery
    assert groups.activate_successions(w.s, w.group, at, w.me.node_id) == 0
    see(w, y, at + timedelta(minutes=1), succession_pending=True)
    assert groups.activate_successions(w.s, w.group, at + timedelta(minutes=1), w.me.node_id) == 1


def test_two_member_group_activation_waived(w):
    x = synth()
    w.enrol(x)
    (rec,) = w.recovery_keys()
    succ = w.succession(1, Ed25519PrivateKey.generate(), [rec])
    groups.ingest_succession(w.s, w.group, succ, groups.utcnow(), delivered_by=x.node_id)
    assert groups.activate_successions(w.s, w.group, later(), w.me.node_id) == 1


# ---- R1-9: member PoP and rotation ------------------------------------------------------------------------------

def test_higher_rev_member_object_by_superseded_key_refused(w):
    """The finding: with x's rotated-out key and a compromised issuer, a higher-rev member object (a new URL) is
    signed by the old key; non-strict PoP checks accepted it."""
    d, x = synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    m1 = w.member(x)
    w.ingest(w.node_cert(d, x, 1, parent, member=m1))
    k2 = Ed25519PrivateKey.generate()
    assert groups.ingest_rotations(w.s, w.group, x.node_id, [rotation(x, x.node_id, 1, k2)],
                                   groups.utcnow()) == "stored"
    groups.rebuild(w.s, w.group, w.me.node_id)
    k2_id = gc.key_id_of(gc.ed_public_b64(k2))
    m2 = {**m1, "rev": 2, "url": "http://x-moved.test"}
    c2 = w.node_cert(d, x, 2, parent, member=m2, pop=gc.member_pop(k2, w.gid, m2, k2_id))
    w.ingest(c2)
    assert w.verdict(c2) == "ok"
    m3 = {**m1, "rev": 3, "url": "http://attacker.test"}
    forged = w.node_cert(d, x, 3, parent, member=m3, pop=gc.member_pop(x.private, w.gid, m3, x.key_id))
    assert w.verdict(forged) == "member_pop_superseded"


def test_renewal_after_rotation_repops_with_current_key(w):
    from atlas import key_rotation

    successor = key_rotation.rotate(w.s, w.me.key_id, w.settings)
    groups.rebuild(w.s, w.group, w.me.node_id, settings=w.settings)
    (renewed,) = groups.renew(w.s, w.settings, w.group, successor, [w.me.node_id], root=w.root())
    assert renewed["body"]["member_pop"]["key_id"] == successor.key_id != w.me.node_id
    assert w.verdict(renewed) == "ok"


# ---- R1-10: envelope version and canonical bodies ----------------------------------------------------------------

def test_v2_envelope_not_verified_under_v1_domain(w, monkeypatch):
    """A later build that accepts `_v: 2` must never verify a v2 envelope under the v1 domain table."""
    x = synth()
    v1 = view(w, x, groups.utcnow())
    v2 = {**v1, "_v": 2}
    v2["sig"] = sign_message(x.private, gc.DOMAINS["group.view"], gc.object_parts(v2))
    monkeypatch.setattr(gc, "SUPPORTED_VERSION", 2)
    public = gc.ed_public_b64(x.private)
    assert gc.verify_object(v1, public)
    assert not gc.verify_object(v2, public)
    with pytest.raises(gc.GroupCryptoError):
        gc.co_sign(v2, x.private)


def test_canonical_body_vectors():
    """Fixed vectors for the R1-10 body rules (a new file: `group_vectors.json` stays byte-identical)."""
    cases = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert len(cases) >= 6
    key = Ed25519PrivateKey.generate()
    for case in cases:
        assert gc.canonical_ok(case["body"]) is case["canonical"], case["name"]
        if case["canonical"]:
            assert body_hash(case["body"]) == case["body_hash"], case["name"]
        else:
            with pytest.raises(gc.GroupCryptoError):
                gc.make_object("group.view", "0" * 16, "view:x", case["body"],
                               {"kind": "node", "id": "0" * 16, "key_id": "0" * 16}, key)


# ---- R1-11 (pinned; the id width changes in the unification PR) ------------------------------------------------------

def test_forced_key_id_collision_refused(w):
    """A member object that reuses another member's node id with a different root key is never valid."""
    x, y = synth(), synth()
    w.enrol(x)
    member = {**w.member(y), "node_id": x.node_id}
    forged = w.root_cert(y, 60, member=member)
    assert w.verdict(forged) in ("malformed", "bad_member_pop", "root_key_changed")
    assert set(w.trust().members()) == {w.me.node_id, x.node_id}


# ---- R1-13: delegate bans are derived ---------------------------------------------------------------------------

def _delegate_world(w, reason="removed"):
    d, x = synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    cx = w.node_cert(d, x, 1, parent)
    w.ingest(cx)
    now = groups.utcnow()
    crl = w.crl(d, [groups.crl_entry(cx, reason, now, now - timedelta(days=1) if reason == "key_compromise" else None)],
                1)
    return d, x, cx, crl


def test_delegate_crl_before_root_reissue_does_not_ban_forever(w):
    d, x, cx, crl = _delegate_world(w)
    w.apply(crl)
    assert w.s.get(GroupBannedNode, (w.gid, x.node_id)) is None  # never stored
    assert w.trust().banned[x.node_id].reason == "removed"
    reissued = w.root_cert(x, 70)
    w.ingest(reissued)
    assert w.verdict(reissued) == "ok" and x.node_id not in w.trust().banned
    assert x.node_id in w.trust().members()


def test_delegate_key_compromise_is_readmissible_by_root(w):
    d, x, cx, crl = _delegate_world(w, "key_compromise")
    w.apply(crl)
    assert w.trust().banned[x.node_id].reason == "removed"  # a delegate never bans permanently
    reissued = w.root_cert(x, 71)
    w.ingest(reissued)
    assert w.verdict(reissued) == "ok"


def test_root_key_compromise_still_permanent(w):
    x = synth()
    cx = w.enrol(x)
    now = groups.utcnow()
    w.apply(w.crl(None, [groups.crl_entry(cx, "key_compromise", now, now - timedelta(days=1))], 1))
    ban = w.s.get(GroupBannedNode, (w.gid, x.node_id))
    assert ban is not None and ban.reason == "key_compromise"
    again = w.root_cert(x, 72)
    assert w.verdict(again) == "banned"


def _arrival(monkeypatch, root_first: bool) -> tuple:
    world = World(monkeypatch)
    try:
        d, x, cx, crl = _delegate_world(world)
        reissued = world.root_cert(x, 73)
        steps = [lambda: world.ingest(reissued), lambda: world.apply(crl)]
        for step in steps if root_first else reversed(steps):
            step()
        tr = world.trust()
        return (world.verdict(cx), world.verdict(reissued), x.node_id in tr.banned, x.node_id in tr.members(),
                world.s.scalars(select(GroupBannedNode)).all() == [])
    finally:
        world.s.close()
        world.engine.dispose()


def test_derived_ban_independent_of_arrival_order(monkeypatch):
    assert _arrival(monkeypatch, True) == _arrival(monkeypatch, False) == ("revoked", "ok", False, True, True)


def test_root_reissue_before_delegate_crl_needs_no_readmit(w):
    d, x, cx, crl = _delegate_world(w)
    reissued = w.root_cert(x, 74)
    w.ingest(reissued)
    w.apply(crl)
    assert w.verdict(cx) == "revoked" and w.verdict(reissued) == "ok"
    assert x.node_id not in w.trust().banned
