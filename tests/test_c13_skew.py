"""CONTRACT-C13 §10.1 version skew, in-process (T-V1, T-V4): an A19 authority and a follower that plays an older
release by knowing only the feed kinds that release knew.

Real nodes on SQLite files through the real routes (`test_sync_compat.Node`). The older follower is simulated: while it
pulls, `feed_schema.FEED_KINDS` holds only the kinds its release understood, so it classifies exactly as that release's
compatibility gate does (park what it does not know). The real 1.0 and 1.1 releases are exercised by
`python -m tools.skew_drill --scenario records`; the lab's T-V2/T-V3 are in `tests/federation_lab/test_version_skew.py`.
"""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import select

from atlas import feed_schema, sync
from atlas.models import Artwork, ChangeLog, ReplicatedChange, SyncFailure

from .test_c13_follower import ART_ROWS, art, curate_art, provider_only
from .test_sync_compat import Node, pull


@contextmanager
def as_release(monkeypatch, *missing: str):
    """The follower is a build whose registry lacks the kinds `missing` (C16 Z1: there are no "1.x" releases to
    pick any more; a wire-1 build that does not know a kind parks it)."""
    known = {k: spec for k, spec in feed_schema.FEED_KINDS.items() if k not in missing}
    with monkeypatch.context() as m:
        m.setattr(feed_schema, "FEED_KINDS", known)
        yield


def kinds(node: Node) -> list[str]:
    with node.st.maker() as s:
        return [r.kind for r in s.scalars(select(ChangeLog).order_by(ChangeLog.seq))]


def open_rows(node: Node, origin: Node) -> list[tuple[int, str, str, str]]:
    with node.st.maker() as s:
        return [(f.seq, f.kind, f.disposition, f.reason) for f in s.scalars(select(SyncFailure).where(
            SyncFailure.node_id == origin.node_id, SyncFailure.resolved_at.is_(None)).order_by(SyncFailure.seq))]


def test_v4_a_1_4_follower_parks_a_record_entry_and_keeps_it(tmp_path, monkeypatch):
    """T-V4: a follower whose registry does not know `record` parks it as `unsupported_kind`: retained, verified,
    nothing after it applied, nothing dropped. After the upgrade the startup replay applies the record and then the
    artwork, in feed order."""
    authority = Node(tmp_path, "authority", "authority")
    cid = provider_only(authority)
    curate_art(authority, cid)
    assert kinds(authority) == ["wire_start", "record", "artwork"]
    follower = Node(tmp_path, "follower", "mirror")
    with as_release(monkeypatch, "record"):
        pull(follower, authority)
        assert open_rows(follower, authority) == [(2, "record", "parked", "unsupported_kind")]
        status = follower.heads(authority)
        assert (status["state"], status["applied_head"], status["verified_head"]) == ("parked", 1, 3)
    with follower.st.maker() as s:
        assert [r.kind for r in s.scalars(select(ReplicatedChange).order_by(ReplicatedChange.seq))] == [
            "wire_start", "record", "artwork"]
        assert s.scalars(select(Artwork)).first() is None
        sync.replay(s, follower.st.ctx, authority.node_id)  # the startup replay of the upgraded release
        s.commit()
    assert open_rows(follower, authority) == []
    assert art(follower, cid) == ART_ROWS
    assert follower.heads(authority)["state"] == "in_sync"
