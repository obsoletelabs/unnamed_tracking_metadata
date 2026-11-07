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
from atlas.models import Achievement, Lock, ReplicatedChange, SyncFailure

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


def merge(node, src, dst):
    with node.st.maker() as s:
        service.merge_canonicals(s, node.st.ctx, src, dst, actor="curator")
        s.commit()


def all_achievements(node, cid):
    """Every row of the record, retired ones included, with the fields a list sets."""
    with node.st.maker() as s:
        return sorted((a.source, a.name, a.tier, a.key, a.retired_at is None) for a in s.scalars(
            select(Achievement).where(Achievement.canonical_id == cid)))


def test_a_dropped_entry_on_a_record_merged_since_lands_on_the_survivor(tmp_path, monkeypatch):
    """A platinum list and a releases lock on A, then A merged into B: the authority's merge carried both to B. A
    1.0 follower dropped both and its merge carried nothing. Rescan judges the entries on the survivor and parks
    them, and the historical replay lands them where the authority's merge did."""
    authority, mirror = fed(tmp_path)
    a, b = authority.publish("Merge Src"), authority.publish("Merge Dst")
    pull(mirror, authority)
    put_achievements(authority, a, [{"name": "All", "tier": "platinum"}])
    lock(authority, a, "releases", RELEASES)
    merge(authority, a, b)
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    assert achievements(mirror, b) == [] and locks(mirror, b) == {}  # the 1.0 symptom on the survivor
    assert mirror.heads(authority)["state"] == "in_sync"

    assert rescan(mirror, authority)["parked"] == 2
    assert mirror.heads(authority)["state"] != "in_sync"  # recorded: status no longer claims all is held
    out = historical(mirror)[authority.node_id]
    assert out["recovered_parked"] == 2 and out["state"] == "in_sync", out
    assert all_achievements(mirror, b) == all_achievements(authority, b) != []
    assert all_achievements(mirror, a) == all_achievements(authority, a) == []
    assert locks(mirror, b) == locks(authority, b) == {"releases": RELEASES}
    assert locks(mirror, a) == locks(authority, a) == {}
    assert view(mirror, b) == view(authority, b) and view(mirror, b)["releases"] == RELEASES
    assert mirror.failures(authority) == []
    assert rescan(mirror, authority)["parked"] == 0  # idempotent: the survivor now holds it
    historical(mirror)
    assert all_achievements(mirror, b) == all_achievements(authority, b)


def test_a_dropped_list_on_a_merged_record_reconciles_with_what_the_merge_carried(tmp_path, monkeypatch):
    """The follower applied A's first list and B's own list; A's second (platinum) list was dropped; the merge
    carried A's first list, and B's own duplicate won. The replay reconciles the carried rows in place (keys kept,
    the replaced item retired) and the survivor's duplicate still wins, exactly as on the authority."""
    authority, mirror = fed(tmp_path)
    a, b = authority.publish("Carried Src"), authority.publish("Carried Dst")
    put_achievements(authority, a, [{"name": "Start", "tier": "bronze"}, {"name": "Old", "tier": "silver"},
                                    {"name": "Shared", "tier": "gold"}])
    put_achievements(authority, b, [{"name": "Shared", "tier": "bronze"}])
    lock(authority, a, "releases", RELEASES)
    lock(authority, b, "release_date", "2031")
    pull(mirror, authority)
    put_achievements(authority, a, [{"name": "Start", "tier": "gold"}, {"name": "All", "tier": "platinum"},
                                    {"name": "Shared", "tier": "platinum"}])
    lock(authority, a, "releases", [{**RELEASES[0], "date": "2032"}])
    merge(authority, a, b)
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    assert all_achievements(mirror, b) != all_achievements(authority, b)
    assert rescan(mirror, authority)["parked"] == 2
    out = historical(mirror)[authority.node_id]
    assert out["recovered_parked"] == 2 and out["state"] == "in_sync", out
    assert all_achievements(mirror, b) == all_achievements(authority, b)
    assert ("curator", "Shared", "bronze") in {(s, n, t) for s, n, t, _, live in all_achievements(mirror, b) if live}
    assert locks(mirror, b) == locks(authority, b)
    assert locks(mirror, b)["releases"][0]["date"] == "2032"
    assert mirror.failures(authority) == []
    assert rescan(mirror, authority)["parked"] == 0


def test_a_dropped_entry_on_a_merged_record_that_a_later_survivor_entry_replaced_is_not_resurrected(
        tmp_path, monkeypatch):
    """After the merge the authority replaced the survivor's list and lock for the same source and field: those
    later entries decide, so A's dropped list and lock are not recovered (nothing parked, the follower already
    holds what the authority holds)."""
    authority, mirror = fed(tmp_path)
    a, b = authority.publish("Replaced Src"), authority.publish("Replaced Dst")
    pull(mirror, authority)
    put_achievements(authority, a, [{"name": "All", "tier": "platinum"}])
    lock(authority, a, "releases", RELEASES)
    merge(authority, a, b)
    put_achievements(authority, b, [{"name": "Only", "tier": "gold"}])
    lock(authority, b, "releases", [{**RELEASES[0], "date": "2033"}])
    unlock(authority, b, "releases")
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    assert rescan(mirror, authority)["parked"] == 0
    assert [(n, t) for n, t, _ in achievements(mirror, b)] == [(n, t) for n, t, _ in achievements(authority, b)]
    assert locks(mirror, b) == locks(authority, b) == {}


def test_a_dropped_lock_on_a_merged_record_whose_survivor_had_its_own_lock_is_left_alone(tmp_path, monkeypatch):
    """The authority's merge dropped A's releases lock because B had its own (`lock_dropped`): the follower's
    survivor keeps B's lock too, so that entry is not parked and B's value stays (also under a historical replay)."""
    authority, mirror = fed(tmp_path)
    a, b = authority.publish("Own Lock Src"), authority.publish("Own Lock Dst")
    own = [{**RELEASES[0], "date": "2029"}]
    lock(authority, b, "releases", own)
    pull(mirror, authority)
    lock(authority, a, "releases", RELEASES)
    put_achievements(authority, a, [{"name": "All", "tier": "platinum"}])  # parked, so the replay runs
    merge(authority, a, b)
    with monkeypatch.context() as m:
        as_1_0(m)
        pull(mirror, authority)
    assert locks(authority, b) == locks(mirror, b) == {"releases": own}
    assert rescan(mirror, authority)["parked"] == 1  # the list only
    historical(mirror)
    assert all_achievements(mirror, b) == all_achievements(authority, b) != []
    assert locks(mirror, b) == locks(authority, b)
    assert locks(mirror, a) == locks(authority, a) == {}
    assert mirror.failures(authority) == []
    assert rescan(mirror, authority)["parked"] == 0


def test_a_historical_replay_over_a_merge_leaves_what_the_merge_carried_where_it_is(tmp_path, monkeypatch):
    """The replay starts before both lists and the merge (an early dropped lock elsewhere is parked). Re-running
    B's own list must not retire the rows A's later merge carried onto B, and re-running A's lists must not
    duplicate them: the survivor ends exactly as on the authority, and a second replay changes nothing."""
    authority, mirror = fed(tmp_path)
    x, a, b = authority.publish("Early"), authority.publish("Replayed Src"), authority.publish("Replayed Dst")
    pull(mirror, authority)
    with monkeypatch.context() as m:
        as_1_0(m)
        lock(authority, x, "releases", RELEASES)
        pull(mirror, authority)
    put_achievements(authority, a, [{"name": "Start", "tier": "bronze"}, {"name": "Shared", "tier": "gold"}])
    put_achievements(authority, b, [{"name": "Shared", "tier": "silver"}, {"name": "Own", "tier": "gold"}])
    put_achievements(authority, a, [{"name": "Start", "tier": "gold"}, {"name": "Shared", "tier": "gold"},
                                    {"name": "New", "tier": "bronze"}])
    lock(authority, a, "release_date", "2030")
    merge(authority, a, b)
    put_achievements(authority, authority.publish("After"), [{"name": "Late", "tier": "bronze"}])
    pull(mirror, authority)
    before = all_achievements(mirror, b)
    assert before == all_achievements(authority, b)
    assert rescan(mirror, authority)["parked"] == 1  # the early lock only
    out = historical(mirror)[authority.node_id]
    assert out["recovered_parked"] == 1 and out["state"] == "in_sync", out
    assert all_achievements(mirror, b) == all_achievements(authority, b) == before
    assert locks(mirror, b) == locks(authority, b) and locks(mirror, x) == locks(authority, x)
    assert mirror.failures(authority) == []
    with mirror.st.maker() as s:  # a second replay from the start changes nothing either
        for seq in range(1, authority.head() + 1):
            entry = s.get(ReplicatedChange, (authority.node_id, seq))
            with sync.entry_at(entry.created_at):
                assert sync.apply_entry(s, mirror.st.ctx, authority.node_id, seq, entry.kind, entry.body,
                                        may_park=False) in ("apply", "noop")
        s.commit()
    assert all_achievements(mirror, b) == all_achievements(authority, b)
    assert locks(mirror, b) == locks(authority, b)
    assert mirror.failures(authority) == []


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
