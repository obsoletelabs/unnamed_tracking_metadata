"""Synthetic profile-sync state for the A11 evidence screenshots (e2e/tests/profile.evidence.spec.js).

`reset` removes alice's preferences, review rows and group account link. `link` links alice's account with the
other member of the e2e node's group (enrolled by e2e/group_joiner.py), sets a few preferences and receives a
concurrent display-name edit from that member, which opens a review row. It writes the e2e database directly
(the synthetic joiner is not a running node); every value is synthetic.
Usage (repository root): uv run python e2e/profile_seed.py reset|link
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from atlas import groups, ids, profile
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


def main(action: str) -> int:
    settings = Settings(_env_file=None, database_url=f"sqlite:///{DB.as_posix()}", role="local")
    engine = make_engine(settings.database_url)
    with make_sessionmaker(engine)() as s:
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
            for field, value in (("theme", "dark"), ("display_name", "Alice Example"), ("date_format", "iso"),
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "link"))
