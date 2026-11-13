"""Play a v0 authority in-process (C16 Z1).

A wire-1 authority introduces every record before it signs an entry that names it, carries `_wire` and starts its
feed with `wire_start`. The follower still reads v0 feeds, which named records they never introduced (the C13 live
failure) and carry no `_wire`. These tests keep that follower path covered by letting a node of this release sign
exactly what a v0 (emit protocol below 1.5, bridge off) authority signed: the sidecar alone, the record marked
`legacy_unintroduced`. `upgrade(node)` turns it into the wire-1 authority it becomes after the in-place upgrade.
"""

from __future__ import annotations

import pytest

from atlas import feed
from atlas.models import Canonical, FeedIntroduced

V0: set[str] = set()


def v0(node):
    """Mark `node` (a test `Node`) as a v0 authority; returns it."""
    V0.add(node.node_id)
    return node


def upgrade(node) -> None:
    """The in-place upgrade to wire 1: from now on it introduces, signs `_wire` and starts with `wire_start`."""
    V0.discard(node.node_id)


@pytest.fixture(autouse=True)
def v0_authorities(monkeypatch):
    real_emit = feed.emit

    def emit(session, ctx, kind, body):
        if ctx.identity.node_id not in V0:
            return real_emit(session, ctx, kind, body)
        names = feed.named(kind, body)
        feed.scope_gate(session, kind, body, names)
        seq = feed.append_unchecked(session, ctx.identity, kind, body)  # no `_wire`, no `wire_start`
        own = body.get("canonical_id") if kind == "obs" else None
        for cid in names:
            if cid == own or session.get(FeedIntroduced, cid) is not None or session.get(Canonical, cid) is None:
                continue
            feed.mark(session, cid, None, feed.VIA_LEGACY)
        if isinstance(own, str):
            feed.mark(session, own, seq, feed.VIA_OBS)
        session.flush()
        return seq

    real_introduce = feed.ensure_introduced

    def ensure_introduced(session, ctx, cid, because):
        if ctx.identity.node_id in V0:
            return None  # a v0 authority could not introduce (emit below 1.5, bridge off)
        return real_introduce(session, ctx, cid, because)

    monkeypatch.setattr(feed, "emit", emit)
    monkeypatch.setattr(feed, "ensure_introduced", ensure_introduced)
    yield
    V0.clear()
