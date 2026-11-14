"""Synthetic ask-the-group state for the C14-2 evidence screenshots (e2e/tests/assist.evidence.spec.js).

The e2e node must already be in a group with the synthetic joiner (e2e/group_joiner.py). This writes, directly into
the e2e database (the joiner is not a running node, so no real answer can arrive):

* a synthetic followed feed, "Lantern Authority", and five records whose changes wait for that feed;
* one state per record, as the assist runner would leave it: an offer from the other member (class 2 without the
  preset), a merge hint, "none of your nodes has it", asked and backing off, and queued;
* two contribution states the other member shares (H67): waiting for review and applied.

Every title, id and key is synthetic. `reset` removes what `seed` wrote.
Usage (repository root): uv run python e2e/assist_seed.py seed|reset
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from atlas import groups, ids
from atlas.db import make_engine, make_sessionmaker
from atlas.models import Canonical, GroupAssistItem, GroupContribState, NodeState, Peer, SyncFailure, now
from atlas.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "e2e" / ".run" / "node" / "atlas.db"
FEED = "7e2a91c04b5d3f68"  # the synthetic followed feed
MARK = "e2e-assist"


def cid(n: int) -> str:
    return "atlas:" + hashlib.sha256(f"{MARK}-{n}".encode()).hexdigest()[:26].translate(
        str.maketrans("abcdef", "mnpqrs"))


def key(prefix: str, c: str) -> str:
    return prefix + hashlib.sha256(c.encode()).hexdigest()[:16]


def reset(s) -> None:
    cids = [cid(n) for n in range(1, 8)]
    s.execute(delete(SyncFailure).where(SyncFailure.node_id == FEED))
    s.execute(delete(Peer).where(Peer.node_id == FEED))
    s.execute(delete(GroupAssistItem).where(GroupAssistItem.cid.in_(cids)))
    s.execute(delete(Canonical).where(Canonical.id.in_(cids)))
    s.execute(delete(GroupContribState).where(GroupContribState.contribution_id.like(MARK + "%")))
    for c in cids:
        s.execute(delete(NodeState).where(NodeState.key.in_([key("assist_job:", c), key("assist_done:", c)])))


def seed(s) -> None:
    reset(s)
    own = s.get(NodeState, "node_id").value
    group = groups.load(s)
    other = next(m for m in s.scalars(select(groups.GroupMember).where(
        groups.GroupMember.group_id == group.group_id)) if m.node_id != own)
    t = now()
    s.add(Peer(name="Lantern Authority", url="https://lantern-authority.invalid", node_id=FEED,
               public_key="e2e-synthetic-public-key", follow="all"))
    titles = {1: None, 2: None, 3: None, 4: None, 5: None}
    seq = 0
    for n in titles:
        for kind in ("artwork", "custom") if n % 2 else ("achievements",):
            seq += 1
            s.add(SyncFailure(node_id=FEED, seq=seq, kind=kind, error="waiting for its record",
                              disposition="wait", reason="waiting_for_record", waiting_for=cid(n),
                              first_failed_at=t - timedelta(days=3, hours=n), last_attempt_at=t - timedelta(hours=n)))
    # 1: the other member holds it and applied this feed's changes there (class 2, one click without the preset)
    s.add(GroupAssistItem(id=ids.ulid(), helper=other.node_id, cid=cid(1), kind="offer",
                          body={"feed": FEED, "helper": other.node_id}, body_hash="0" * 64,
                          received_at=t - timedelta(minutes=20)))
    # 2: the other member says the record was merged into another one (a hint, never a redirect)
    s.add(Canonical(id=cid(6), view={"title": "Brinewatch Harbour: Complete Edition"}, explain={}, media_type="game"))
    s.add(GroupAssistItem(id=ids.ulid(), helper=other.node_id, cid=cid(2), kind="merge_hint",
                          body={"survivor": cid(6)}, body_hash="0" * 64, received_at=t - timedelta(hours=2)))
    # 3: nobody in the group has it (gave up after the backoff)
    s.add(NodeState(key=key("assist_done:", cid(3)), value=json.dumps(
        {"cid": cid(3), "outcome": "none_has_it", "at": (t - timedelta(days=1)).isoformat(), "asked": [other.node_id]})))
    # 4: asked once, nobody answered yet, asking again later
    s.add(NodeState(key=key("assist_job:", cid(4)), value=json.dumps(
        {"cid": cid(4), "reasons": ["waiting_for_record"], "feeds": [FEED], "first_at": (t - timedelta(hours=3)).isoformat(),
         "next_at": (t + timedelta(hours=5)).isoformat(), "tries": 1, "last_asked": (t - timedelta(hours=1)).isoformat(),
         "asked": [other.node_id]})))
    # 5: queued for the next group round
    s.add(NodeState(key=key("assist_job:", cid(5)), value=json.dumps(
        {"cid": cid(5), "reasons": ["waiting_for_record"], "feeds": [FEED], "first_at": t.isoformat(),
         "next_at": t.isoformat(), "tries": 0, "asked": []})))
    # H67: contribution states the other member shares (no payload values, no account data)
    for i, (state, kind, fields, decided) in enumerate((
            ("pending", "edit", ["release"], None),
            ("approved", "edit", ["developer", "genres"], (t - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")))):
        body = {"main": "c0ffee0123456789", "link_subject_hint": "a1b2", "contribution_id": f"{MARK}-{i}",
                "kind": kind, "local_cid": cid(7), "main_cid": None, "fields": fields, "state": state,
                "decided_at": decided, "feed_seq": None, "updated_at": t.strftime("%Y-%m-%dT%H:%M:%SZ")}
        s.add(GroupContribState(origin_node=other.node_id, contribution_id=f"{MARK}-{i}",
                                envelope={"t": "group.contrib_state", "body": body},
                                updated_at=t - timedelta(hours=i * 30)))


def main(action: str) -> int:
    settings = Settings(_env_file=None, database_url=f"sqlite:///{DB.as_posix()}", role="local")
    engine = make_engine(settings.database_url)
    with make_sessionmaker(engine)() as s:
        (seed if action == "seed" else reset)(s)
        s.commit()
    engine.dispose()
    print('{"ok": true}')
    return 0


def run(argv: list[str]) -> int:
    """main(), retried while the running e2e node holds the SQLite write lock (as e2e/profile_seed.py)."""
    for attempt in range(20):
        try:
            return main(argv[0] if argv else "seed")
        except OperationalError as exc:
            if "locked" not in str(exc) or attempt == 19:
                raise
            time.sleep(1)
    return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
