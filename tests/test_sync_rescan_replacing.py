"""`atlas sync replay --rescan` for entries that replace state (Phase 4 defect D1, CONTRACTS-v1 C1.8).

A 1.0 release had no park state: an `achievements` entry with a tier it did not know (`platinum`, C7) or a `lock`
on a field it did not know (`releases`, C5) failed inside its apply, was swallowed and the cursor moved on. After an
in-place upgrade, `rescan` must record such entries as `dropped_by_previous_release` and `replay --historical` must
apply them, idempotently; the newest entry per list or lock decides, so an older dropped entry that a later one
replaced is not resurrected. The 1.0 behaviour is reproduced in process exactly as `atlas/sync.py` at 62c73f3 did
it (the apply fails, the error is swallowed); `tools/skew_drill.py --scenario rescan` drives the real 1.0 release.
"""

from __future__ import annotations

from sqlalchemy import select

from atlas import facets, service, sync
from atlas.merge import RELEASE_LOCKS
from atlas.models import Achievement, Lock, SyncFailure

from .test_sync_compat import Node, pull

V10_TIERS = ("bronze", "silver", "gold")  # atlas/facets.py ACH_TIERS at 62c73f3
RELEASES = [{"date": "2030", "precision": "year", "platform": "pc", "region": "ww", "category": "full"}]


def fed(tmp_path):
    return Node(tmp_path, "authority", "authority", emit_protocol="1.4"), Node(tmp_path, "mirror", "mirror")


def as_1_0(monkeypatch):
    """Apply as the 1.0 release did: a list with a tier it did not know and a lock or unlock of a field it did not
    know raised AtlasError inside its apply, which it swallowed (`except AtlasError: return`)."""
    real = sync._apply

    def old_release(session, ctx, node_id, seq, kind, body):
        if kind == "achievements" and any(i.get("tier") not in (None, *V10_TIERS) for i in body["items"]):
            return None
        if kind in ("lock", "unlock") and body["field"] in RELEASE_LOCKS:
            return None
        return real(session, ctx, node_id, seq, kind, body)

    monkeypatch.setattr(sync, "_apply", old_release)


def put_achievements(node, cid, items):
    with node.st.maker() as s:
        facets.set_achievements(s, node.st.ctx, cid, "curator", items, redistributable=True)
        s.commit()


def lock(node, cid, field, value):
    with node.st.maker() as s:
        service.set_lock(s, node.st.ctx, cid, field, value, actor="curator")
        s.commit()


def unlock(node, cid, field):
    with node.st.maker() as s:
        service.clear_lock(s, node.st.ctx, cid, field)
        s.commit()


def achievements(node, cid):
    with node.st.maker() as s:
        return sorted((a.name, a.tier, a.key) for a in s.scalars(select(Achievement).where(
            Achievement.canonical_id == cid, Achievement.retired_at.is_(None))))


def locks(node, cid):
    with node.st.maker() as s:
        return {lk.field: lk.value for lk in s.scalars(select(Lock).where(Lock.canonical_id == cid))}


def view(node, cid):
    with node.st.maker() as s:
        v = service.get_canonical(s, cid)[0].view
        return {k: v.get(k) for k in ("release_date", "first_release", "releases", "release_year")}


def rescan(node, origin):
    with node.st.maker() as s:
        counts = sync.rescan(s, origin.node_id)
        s.commit()
    return counts


def historical(node):
    with node.st.maker() as s:
        out = sync.replay(s, node.st.ctx, historical=True)
        s.commit()
    return out


def test_a_platinum_list_and_a_releases_lock_a_1_0_follower_dropped_are_recovered(tmp_path, monkeypatch):
    """The Phase 4 repro (`lab/repro_v10_rescan.py`) in process: both entries come back, nothing else moves."""
    authority, mirror = fed(tmp_path)
    game, locked = authority.publish("Repro Game"), authority.publish("Repro Locked Game")
    put_achievements(authority, game, [{"name": "Start", "tier": "bronze"}])
    pull(mirror, authority)
    put_achievements(authority, game, [{"name": "Start", "tier": "bronze"}, {"name": "All", "tier": "platinum"}])
    platinum_seq = authority.head()
    lock(authority, locked, "release_date", "2030")
    lock(authority, locked, "releases", RELEASES)
    releases_seq = authority.head()
    later = authority.publish("Repro Later")
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    heads = mirror.heads(authority)
    assert heads["applied_head"] == authority.head() and heads["state"] == "in_sync"  # the 1.0 symptom
    assert mirror.failures(authority) == []  # nothing recorded: the drop was silent
    assert [(n, t) for n, t, _ in achievements(mirror, game)] == [("Start", "bronze")]
    assert locks(mirror, locked) == {"release_date": "2030"} and view(mirror, locked)["releases"] is None
    assert "Repro Later" in mirror.titles() and later

    counts = rescan(mirror, authority)  # the upgraded release
    assert counts["parked"] == 2 and counts["quarantined"] == 0, counts
    with mirror.st.maker() as s:
        rows = {seq: s.get(SyncFailure, (authority.node_id, seq)) for seq in (platinum_seq, releases_seq)}
        assert {seq: (f.disposition, f.reason, f.kind) for seq, f in rows.items()} == {
            platinum_seq: ("parked", "dropped_by_previous_release", "achievements"),
            releases_seq: ("parked", "dropped_by_previous_release", "lock")}
    assert mirror.heads(authority)["applied_head"] == authority.head()  # classification only: the cursor stays

    with mirror.st.maker() as s:  # a plain replay never applies below the cursor (C1.7)
        sync.replay(s, mirror.st.ctx)
        s.commit()
    assert [(n, t) for n, t, _ in achievements(mirror, game)] == [("Start", "bronze")]

    out = historical(mirror)[authority.node_id]
    assert out["recovered_parked"] == 2 and out["historical_parked"] is None and out["state"] == "in_sync", out
    assert achievements(mirror, game) == achievements(authority, game)  # names, tiers and keys
    assert {t for _, t, _ in achievements(mirror, game)} == {"bronze", "platinum"}
    assert locks(mirror, locked) == locks(authority, locked)
    assert view(mirror, locked) == view(authority, locked)
    assert view(mirror, locked)["releases"] == RELEASES
    assert mirror.failures(authority) == []

    assert rescan(mirror, authority)["parked"] == 0  # idempotent: the state now holds
    assert historical(mirror)[authority.node_id]["reapplied"] == 0
    assert achievements(mirror, game) == achievements(authority, game)


def test_only_the_newest_entry_per_list_or_lock_decides(tmp_path, monkeypatch):
    """A dropped list that a later list replaced, and a dropped lock that a later unlock removed, are not
    recovered: the follower already holds what the authority holds."""
    authority, mirror = fed(tmp_path)
    game, locked = authority.publish("Newest Game"), authority.publish("Newest Locked")
    pull(mirror, authority)
    put_achievements(authority, game, [{"name": "Start", "tier": "bronze"}, {"name": "All", "tier": "platinum"}])
    put_achievements(authority, game, [{"name": "Start", "tier": "gold"}])
    lock(authority, locked, "releases", RELEASES)
    unlock(authority, locked, "releases")
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    assert achievements(mirror, game) == achievements(authority, game)
    assert locks(mirror, locked) == locks(authority, locked) == {}
    assert rescan(mirror, authority) == {"scanned": authority.head(), "parked": 0, "quarantined": 0}
    assert mirror.failures(authority) == []


def test_a_lock_this_node_pinned_itself_is_not_taken_for_a_drop(tmp_path, monkeypatch):
    authority, mirror = fed(tmp_path)
    cid = authority.publish("Pinned Here")
    pull(mirror, authority)
    with mirror.st.maker() as s:  # the mirror's own pin wins over the feed, as when the entry is applied
        service.set_lock(s, mirror.st.ctx, cid, "releases", RELEASES, actor="me", local_only=True)
        s.commit()
    lock(authority, cid, "releases", [{**RELEASES[0], "date": "2031"}])
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    assert rescan(mirror, authority)["parked"] == 0
    assert locks(mirror, cid)["releases"] == RELEASES


def test_a_dropped_entry_on_a_record_merged_since_is_left_alone(tmp_path, monkeypatch):
    """Re-applying a list to a record that was merged since would not land where it did on the authority; rescan
    does not judge it (no false work), and the merge itself carried the survivor's state."""
    authority, mirror = fed(tmp_path)
    a, b = authority.publish("Merge Src"), authority.publish("Merge Dst")
    pull(mirror, authority)
    put_achievements(authority, a, [{"name": "All", "tier": "platinum"}])
    with authority.st.maker() as s:
        service.merge_canonicals(s, authority.st.ctx, a, b, actor="curator")
        s.commit()
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    assert rescan(mirror, authority)["parked"] == 0


def test_an_achievements_entry_this_release_refuses_is_quarantined_by_the_replay(tmp_path, monkeypatch):
    """A list the upgraded release cannot apply either is surfaced (parked by rescan, quarantined with its reason
    by the historical replay) instead of staying invisible; it does not block anything."""
    authority, mirror = fed(tmp_path)
    cid = authority.publish("Bad List")
    pull(mirror, authority)
    seq = authority.emit("achievements", {"canonical_id": cid, "source": "curator",
                                          "items": [{"name": "x" * 201, "tier": "platinum"}]})
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    assert rescan(mirror, authority)["parked"] == 1
    historical(mirror)
    assert mirror.failures(authority) == [(seq, "achievements", "quarantined", "apply_error")]
    assert mirror.heads(authority)["applied_head"] == authority.head()
    assert rescan(mirror, authority)["parked"] == 0  # tracked: not recorded twice
