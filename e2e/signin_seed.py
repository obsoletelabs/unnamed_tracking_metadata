"""Synthetic sign-in state for the A12 evidence screenshots (e2e/tests/signin.evidence.spec.js).

Run after e2e/profile_seed.py link (alice's account is linked with the synthetic second member). `federate` opens
a session for alice as the group sign-in finish step would (CONTRACT-C10 §5.5) with the other member as the
vouching node, writes the audit rows that flow leaves on both sides (`group_assertion_issued`, `group_signin`), and
prints the session cookie value for a fresh browser context. The synthetic member is not a running node, so it
cannot sign a real assertion; every value here is a synthetic runtime value and the cookie lives only in the
throwaway e2e database. Usage (repository root): uv run python e2e/signin_seed.py federate
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select

from atlas import accounts, groups, ids
from atlas.db import make_engine, make_sessionmaker
from atlas.models import AuditEntry, GroupAccountLink, NodeState, User
from atlas.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "e2e" / ".run" / "node" / "atlas.db"


def main(action: str) -> int:
    if action != "federate":
        raise SystemExit("usage: signin_seed.py federate")
    settings = Settings(_env_file=None, database_url=f"sqlite:///{DB.as_posix()}", role="local")
    engine = make_engine(settings.database_url)
    with make_sessionmaker(engine)() as s:
        alice = s.scalars(select(User).where(User.username == "alice")).one()
        own = s.get(NodeState, "node_id").value
        group = groups.load(s)
        other = next(m for m in s.scalars(select(groups.GroupMember).where(
            groups.GroupMember.group_id == group.group_id)) if m.node_id != own)
        link = s.scalars(select(GroupAccountLink).where(GroupAccountLink.user_id == alice.id)).one()
        jti = ids.ulid()
        secret, row = accounts.create_session(s, settings, alice)
        row.via_group, row.origin_node, row.assertion_id, row.amr = group.group_id, other.node_id, jti, ["pwd"]
        s.add(AuditEntry(actor_id=alice.id, action="group_assertion_issued", target=alice.id,
                         detail={"aud_node": other.node_id, "purpose": "signin", "jti": ids.ulid()}))
        s.add(AuditEntry(actor_id=alice.id, action="group_signin", target=alice.id,
                         detail={"via": "group", "group_id": group.group_id, "origin_node": other.node_id,
                                 "idp_node": other.node_id, "assertion_id": jti, "amr": ["pwd"],
                                 "provisioned": False, "mfa_trusted": False, "session": "federated",
                                 "account_uid": link.account_uid}))
        s.commit()
    engine.dispose()
    print(json.dumps({"cookie": secret, "other": other.node_id, "account_uid": link.account_uid}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "federate"))
