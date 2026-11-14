"""A quarantine retry never regresses newer state (PR #102 review, olie-ob).

A known entry that fails is quarantined and retried later, after the entries behind it have applied. For an entry
that replaces state (artwork, achievements, a lock, the schema) or upserts part of it (custom values), re-running
the old entry then would overwrite what a later entry of the same feed already set, and the failure would still be
reported resolved. These tests check the final follower state after the retry, not only the failure rows.
"""

from __future__ import annotations

from sqlalchemy import select

from atlas import custom, facets, sync
from atlas.models import Achievement, Artwork, Canonical, Lock, ReplicatedChange, ReplicatedFeed, SyncFailure

from .test_sync_compat import net, pull  # noqa: F401  (fixture)

NODE = "upstream"
CID = "atlas:" + "G" * 26


def _fail_once(monkeypatch, module, name):
    """Make `module.name` raise ValueError on its first call only (a transient apply error)."""
    real, calls = getattr(module, name), []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("transient failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, flaky)
    return calls


def _feed(session, *entries):
    """Retain `entries` (kind, body) as seq 1..n of NODE's feed, as a verified pull would."""
    session.add(ReplicatedFeed(node_id=NODE, public_key="test"))
    session.flush()
    for seq, (kind, body) in enumerate(entries, start=1):
        session.add(ReplicatedChange(node_id=NODE, seq=seq, kind=kind, body=body, body_hash="h" * 64,
                                     signature="s" * 64, key_id=NODE))
    session.flush()


def _game(session):
    session.add(Canonical(id=CID, view={}, explain={}))
    session.flush()


def _open_failures(session):
    return [(f.seq, f.kind) for f in session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None)))]


def _pull_then_retry(session, ctx):
    processed, head = sync._apply_pending(session, ctx, NODE)
    result = sync.retry_failures(session, ctx, NODE)
    session.flush()
    return head, result


# ---- the reviewer's reproduction, through a real pull ---------------------------------------------------------

def test_retried_artwork_does_not_overwrite_the_newer_replacement(net, monkeypatch):  # noqa: F811
    authority, mirror, _ = net
    cid = authority.publish("Art")
    old = authority.emit("artwork", {"canonical_id": cid, "source": "curator",
                                     "items": [{"kind": "key_art", "url": "https://example.test/old.png"}]})
    authority.emit("artwork", {"canonical_id": cid, "source": "curator",
                               "items": [{"kind": "key_art", "url": "https://example.test/new.png"}]})
    _fail_once(monkeypatch, facets, "set_artwork")
    pull(mirror, authority)
    assert mirror.failures() == []  # the old entry is resolved: a later entry superseded it
    heads = mirror.heads(authority)
    assert heads["applied_head"] == authority.head() and heads["in_sync"]
    with mirror.st.maker() as session:
        assert [a.url for a in session.scalars(select(Artwork).where(Artwork.canonical_id == cid))] == [
            "https://example.test/new.png"]
        row = session.get(SyncFailure, (authority.node_id, old))
        assert row.resolved_at is not None and row.detail["superseded_by"] == old + 1


# ---- every replacing kind ----------------------------------------------------------------------------------

def test_retried_achievements_do_not_overwrite_the_newer_list(session, ctx, monkeypatch):
    _game(session)
    _feed(session,
          ("achievements", {"canonical_id": CID, "source": "curator", "items": [{"name": "Old"}]}),
          ("achievements", {"canonical_id": CID, "source": "curator", "items": [{"name": "New"}]}))
    _fail_once(monkeypatch, facets, "set_achievements")
    head, result = _pull_then_retry(session, ctx)
    assert head == 2 and result["resolved"] == 1 and _open_failures(session) == []
    assert [a.name for a in session.scalars(select(Achievement))] == ["New"]


def test_retried_lock_does_not_undo_a_later_unlock(session, ctx, monkeypatch):
    from atlas import service

    _game(session)
    _feed(session,
          ("lock", {"canonical_id": CID, "field": "title", "value": "Pinned"}),
          ("unlock", {"canonical_id": CID, "field": "title"}))
    _fail_once(monkeypatch, service, "set_lock")
    head, result = _pull_then_retry(session, ctx)
    assert head == 2 and result["resolved"] == 1 and _open_failures(session) == []
    assert session.get(Lock, (CID, "title")) is None


def test_retried_custom_values_keep_newer_keys_and_apply_the_rest(session, ctx, monkeypatch):
    custom.seed(session)
    custom.save_field(session, ctx, "game", "site", "Website", "url")
    custom.save_field(session, ctx, "game", "forum", "Forum", "url")
    _game(session)
    _feed(session,
          ("custom", {"canonical_id": CID, "source": "curator",
                      "values": {"site": "https://old.example", "forum": "https://forum.example"}}),
          ("custom", {"canonical_id": CID, "source": "curator", "values": {"site": "https://new.example"}}))
    _fail_once(monkeypatch, custom, "set_values")
    head, result = _pull_then_retry(session, ctx)
    assert head == 2 and result["resolved"] == 1 and _open_failures(session) == []
    # `site` keeps the later value; `forum`, which no later entry touched, is not lost
    assert custom.merged(session, ctx, CID) == {"site": "https://new.example", "forum": "https://forum.example"}


def test_a_later_entry_that_is_itself_still_failing_does_not_supersede(session, ctx, monkeypatch):
    """Both entries fail once: retried oldest first, the feed's final state wins and nothing is left open."""
    _game(session)
    _feed(session,
          ("artwork", {"canonical_id": CID, "source": "curator",
                       "items": [{"kind": "key_art", "url": "https://example.test/old.png"}]}),
          ("artwork", {"canonical_id": CID, "source": "curator",
                       "items": [{"kind": "key_art", "url": "https://example.test/new.png"}]}))
    real, calls = facets.set_artwork, []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) <= 2:
            raise ValueError("transient failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(facets, "set_artwork", flaky)
    head, result = _pull_then_retry(session, ctx)
    assert head == 2 and result == {"retried": 2, "resolved": 2, "still_failing": 0}
    assert [a.url for a in session.scalars(select(Artwork))] == ["https://example.test/new.png"]


def test_retry_with_no_later_entry_for_the_target_still_applies(session, ctx, monkeypatch):
    _game(session)
    other = "atlas:" + "H" * 26
    session.add(Canonical(id=other, view={}, explain={}))
    _feed(session,
          ("artwork", {"canonical_id": CID, "source": "curator",
                       "items": [{"kind": "key_art", "url": "https://example.test/a.png"}]}),
          ("artwork", {"canonical_id": other, "source": "curator",
                       "items": [{"kind": "key_art", "url": "https://example.test/b.png"}]}),
          ("artwork", {"canonical_id": CID, "source": "someone-else",
                       "items": [{"kind": "key_art", "url": "https://example.test/c.png"}]}))
    _fail_once(monkeypatch, facets, "set_artwork")
    head, result = _pull_then_retry(session, ctx)
    assert head == 3 and result["resolved"] == 1 and _open_failures(session) == []
    assert sorted(a.url for a in session.scalars(select(Artwork).where(Artwork.canonical_id == CID))) == [
        "https://example.test/a.png", "https://example.test/c.png"]


# ---- a retried merge (second review pass) --------------------------------------------------------------------
#
# A merge moves the source record's locks, artwork, achievements and custom values onto the survivor. Run late,
# after later entries for the survivor applied, it would overwrite them. The retry applies the merge and then
# re-applies, in feed order, the later applied entries that write the two records.

SRC, DST = "atlas:" + "A" * 26, "atlas:" + "B" * 26


def _two_games(session):
    session.add_all([Canonical(id=SRC, view={}, explain={}), Canonical(id=DST, view={}, explain={})])
    session.flush()


def _merge_fails_once(monkeypatch):
    from atlas import service

    return _fail_once(monkeypatch, service, "merge_canonicals")


def test_retried_merge_does_not_bring_back_a_lock_a_later_unlock_removed(session, ctx, monkeypatch):
    _two_games(session)
    _feed(session,
          ("lock", {"canonical_id": SRC, "field": "title", "value": "Pinned"}),
          ("merge", {"src": SRC, "dst": DST}),
          ("unlock", {"canonical_id": DST, "field": "title"}))
    _merge_fails_once(monkeypatch)
    head, result = _pull_then_retry(session, ctx)
    assert head == 3 and result == {"retried": 1, "resolved": 1, "still_failing": 0}
    assert _open_failures(session) == []
    assert session.get(Canonical, SRC).merged_into == DST
    assert session.get(Lock, (DST, "title")) is None and session.get(Lock, (SRC, "title")) is None
    assert session.get(SyncFailure, (NODE, 2)).detail["replayed"] == [3]


def test_retried_merge_does_not_keep_artwork_a_later_entry_replaced(session, ctx, monkeypatch):
    _two_games(session)
    _feed(session,
          ("artwork", {"canonical_id": SRC, "source": "curator",
                       "items": [{"kind": "key_art", "url": "https://example.test/old.png"}]}),
          ("merge", {"src": SRC, "dst": DST}),
          ("artwork", {"canonical_id": DST, "source": "curator",
                       "items": [{"kind": "key_art", "url": "https://example.test/new.png"}]}))
    _merge_fails_once(monkeypatch)
    head, result = _pull_then_retry(session, ctx)
    assert head == 3 and result["resolved"] == 1 and _open_failures(session) == []
    assert session.get(Canonical, SRC).merged_into == DST
    assert [a.url for a in session.scalars(select(Artwork))] == ["https://example.test/new.png"]


def test_retried_merge_keeps_later_custom_values_and_moves_the_rest(session, ctx, monkeypatch):
    custom.seed(session)
    custom.save_field(session, ctx, "game", "site", "Website", "url")
    custom.save_field(session, ctx, "game", "forum", "Forum", "url")
    _two_games(session)
    _feed(session,
          ("custom", {"canonical_id": SRC, "source": "curator",
                      "values": {"site": "https://old.example", "forum": "https://forum.example"}}),
          ("merge", {"src": SRC, "dst": DST}),
          ("custom", {"canonical_id": DST, "source": "curator", "values": {"site": "https://new.example"}}))
    _merge_fails_once(monkeypatch)
    head, result = _pull_then_retry(session, ctx)
    assert head == 3 and result["resolved"] == 1 and _open_failures(session) == []
    assert custom.merged(session, ctx, DST) == {"site": "https://new.example", "forum": "https://forum.example"}


def test_retried_merge_with_a_later_merge_of_the_same_records_stays_quarantined(session, ctx, monkeypatch):
    """Two merges into one survivor move state in an order-dependent way; a late one is not auto-resolved."""
    _two_games(session)
    third = "atlas:" + "C" * 26
    session.add(Canonical(id=third, view={}, explain={}))
    _feed(session,
          ("merge", {"src": SRC, "dst": DST}),
          ("merge", {"src": third, "dst": DST}))
    _merge_fails_once(monkeypatch)
    head, result = _pull_then_retry(session, ctx)
    assert head == 2 and result == {"retried": 1, "resolved": 0, "still_failing": 1}
    row = session.get(SyncFailure, (NODE, 1))
    assert row.resolved_at is None and row.reason == "out_of_order" and row.detail["later"] == [2]
    assert session.get(Canonical, SRC).merged_into is None  # nothing half-applied


def test_retried_merge_with_no_later_entries_for_its_records_still_applies(session, ctx, monkeypatch):
    _two_games(session)
    _game(session)
    _feed(session,
          ("lock", {"canonical_id": SRC, "field": "title", "value": "Pinned"}),
          ("merge", {"src": SRC, "dst": DST}),
          ("unlock", {"canonical_id": CID, "field": "title"}))
    _merge_fails_once(monkeypatch)
    head, result = _pull_then_retry(session, ctx)
    assert head == 3 and result["resolved"] == 1 and _open_failures(session) == []
    assert session.get(Lock, (DST, "title")).value == "Pinned"
