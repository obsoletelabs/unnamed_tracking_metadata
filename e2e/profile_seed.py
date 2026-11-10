"""Synthetic profile-sync state for the A11 evidence screenshots (e2e/tests/profile.evidence.spec.js).

`reset` removes alice's preferences, review rows and group account link. `link` links alice's account with the
other member of the e2e node's group (enrolled by e2e/group_joiner.py), sets a few preferences and receives a
concurrent display-name edit from that member, which opens a review row. It writes the e2e database directly
(the synthetic joiner is not a running node); every value is synthetic.
Usage (repository root): uv run python e2e/profile_seed.py reset|link [theme]
The optional theme (default dark) is alice's synced theme; pass `auto` so the emulated colour scheme decides, for a
real light/dark pair of shots. `profile-sync on|off` sets this node's "Profile sync" policy row (the U3 done-step
note for preset rows left as the administrator set them).
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from atlas import groups, ids, profile, runtime
from atlas.db import make_engine, make_sessionmaker
from atlas.models import (
    GroupAccountLink,
    GroupAccountNode,
    NodeState,
    ProfileConflict,
    ProfileField,
    ProfileSyncConsent,
    ProfileVersion,
    User,
)
from atlas.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "e2e" / ".run" / "node" / "atlas.db"


@dataclass
class Ident:
    node_id: str


def main(action: str, theme: str = "dark") -> int:
    settings = Settings(_env_file=None, database_url=f"sqlite:///{DB.as_posix()}", role="local")
    engine = make_engine(settings.database_url)
    with make_sessionmaker(engine)() as s:
        if action == "profile-sync":
            runtime.save(s, settings, None, {"group_profile_sync": theme == "on"})
            s.commit()
            engine.dispose()
            print('{"ok": true}')
            return 0
        alice = s.scalars(select(User).where(User.username == "alice")).one()
        own = s.get(NodeState, "node_id").value
        for model in (ProfileConflict, ProfileVersion, ProfileField, ProfileSyncConsent, GroupAccountLink):
            s.execute(delete(model).where(model.user_id == alice.id))
        s.execute(delete(GroupAccountNode))
        if action == "link":
            group = groups.load(s)
            other = next(m for m in s.scalars(select(groups.GroupMember).where(
                groups.GroupMember.group_id == group.group_id)) if m.node_id != own)
            now = groups.utcnow()
            uid = ids.ulid()
            s.add(GroupAccountLink(group_id=group.group_id, account_uid=uid, user_id=alice.id, minted_here=True,
                                   linked_at=now))
            for node, info in ((own, {"minted": True, "evidence": None}), (other.node_id, {"minted": False})):
                s.add(GroupAccountNode(group_id=group.group_id, account_uid=uid, node_id=node, status="linked",
                                       evidence={**info, "linked_at": groups.gc.stamp(now)}, entry_ref="-",
                                       updated_at=now))
            me = Ident(own)
            for field, value in (("theme", theme), ("display_name", "Alice Example"), ("date_format", "iso"),
                                 ("a11y.font_scale", 1.1)):
                profile.set_local(s, settings, me, alice, field, value, emit=False, now=now - timedelta(minutes=5))
            later = profile.fmt_hlc(int(now.timestamp() * 1000), 0, other.node_id)
            profile.ingest_remote(s, settings, own, alice, "display_name", hlc=later, value="Alice (laptop)",
                                  cleared=False, base=[], cause="user", origin=other.node_id, entry_ref="-", now=now)
            remote_theme = profile.fmt_hlc(int(now.timestamp() * 1000) + 1, 0, other.node_id)
            base = [h.hlc for h in profile.heads(s, alice.id, "time_format")]
            profile.ingest_remote(s, settings, own, alice, "time_format", hlc=remote_theme, value="24h",
                                  cleared=False, base=base, cause="user", origin=other.node_id, entry_ref="-", now=now)
        s.commit()
    engine.dispose()
    print('{"ok": true}')
    return 0


def run(argv: list[str]) -> int:
    """main(), retried while the running e2e node holds the SQLite write lock (its background group sync writes the
    same file, so a seed write can meet "database is locked"; every action is idempotent)."""
    for attempt in range(20):
        try:
            return main(argv[0] if argv else "link", *argv[1:2])
        except OperationalError as exc:
            if "locked" not in str(exc) or attempt == 19:
                raise
            time.sleep(1)
    return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
