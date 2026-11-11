"""Synthetic achievement rows for the C14-1 evidence screenshots (e2e/tests/scopes.evidence.spec.js).

`achievements <cid>` stores two invented achievements on a record of the throwaway e2e node: one from a provider
(`igdb`, shown to everyone) and one stored under the `atlas-group` source (received from your other node, shown only to
accounts that see data from your nodes). Nothing here talks to a real service; the database is the git-ignored e2e
node's. Usage (repository root): uv run python e2e/scopes_seed.py achievements <cid>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from atlas import facets
from atlas.db import make_engine, make_sessionmaker
from atlas.service import Ctx
from atlas.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "e2e" / ".run" / "node" / "atlas.db"


def main(action: str, cid: str) -> int:
    if action != "achievements":
        raise SystemExit("usage: scopes_seed.py achievements <cid>")
    settings = Settings(_env_file=None, database_url=f"sqlite:///{DB.as_posix()}", role="local")
    with make_sessionmaker(make_engine(settings.database_url))() as s:
        ctx = Ctx(settings=settings, identity=None)  # log=False: nothing is signed, so no key is needed
        facets.set_achievements(s, ctx, cid, "igdb", [
            {"external_id": "SC-ACH-1", "name": "First Light", "description": "Relight the harbour lamp.",
             "tier": "bronze", "points": 15}], log=False)
        facets.set_achievements(s, ctx, cid, "atlas-group", [  # scope.GROUP_SOURCE: a row received from the group
            {"external_id": "SC-ACH-2", "name": "Tidewatch Beacon",
             "description": "Received from your other node.", "tier": "gold", "points": 90}], log=False)
        s.commit()
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:3]))
