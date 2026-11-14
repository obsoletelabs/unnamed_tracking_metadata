"""U1 evidence: a real authority and a real follower (both the code of the current working directory), so the
follower's Admin -> Peers page has a real caught-up peer. Synthetic extras are written into the follower's own
SQLite file: two `wait` rows for a record the authority never published (the C13 waiting shape), an unreachable
peer (a dead loopback port), and a paused peer added before A19 (follow not confirmed).

Run from the worktree to show:  .venv/Scripts/python <this file> <out.json> <stop-file>
It stays up until <stop-file> exists. Everything is synthetic and throwaway (temp dir).
"""
from __future__ import annotations

import json
import secrets
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from atlas.models import Peer, SyncFailure  # noqa: E402
from tools.multinode_drill import PASSWORD, Node, free_port  # noqa: E402

out, stop = Path(sys.argv[1]), Path(sys.argv[2])
root = Path(tempfile.mkdtemp(prefix="u1-peers-"))
auth = Node(root, "authority", "authority")
fol = Node(root, "follower", "local")
other = Node(root, "tidewater", "authority")
auth.start()
other.start()
other.publish("Quiet Orchard")
for t in ("Lamplight Keeper", "Harbour of Echoes", "Signal Tower"):
    auth.publish(t)
fol.start()
pid = fol.add_peer(auth)
fol.sync(pid)
now = datetime.now(UTC)
with fol.state.maker() as s:
    for seq, kind in ((901, "artwork"), (902, "achievements")):
        s.add(SyncFailure(node_id=auth.identity.node_id, seq=seq, kind=kind, disposition="wait",
                          reason="waiting_for_record", waiting_for="atlas:01u1synthwaitingrecord00000",
                          error="waiting for record atlas:01u1synthwaitingrecord00000: the feed has not introduced it yet",
                          first_failed_at=now - timedelta(minutes=42), last_attempt_at=now - timedelta(minutes=42)))
    s.add(Peer(name="Harbour mirror", url=f"http://127.0.0.1:{free_port()}", node_id=secrets.token_hex(32),
               public_key=secrets.token_urlsafe(32), follow="own", last_ok_at=now - timedelta(hours=7),
               last_error="ConnectError: [WinError 10061] No connection could be made (synthetic lab peer)"))
    s.add(Peer(name="Old workshop node", url="http://127.0.0.1:9", node_id=secrets.token_hex(32),
               public_key=secrets.token_urlsafe(32), enabled=False, follow=None))
    s.commit()
out.write_text(json.dumps({"follower": fol.url, "authority": auth.url, "authority_token": auth.follower, "probe": other.url, "probe_token": other.follower,
                           "user": "drill-admin", "password": PASSWORD}), encoding="utf-8")
print("ready", fol.url, flush=True)
try:
    while not stop.exists():
        time.sleep(0.5)
finally:
    fol.close()
    auth.close()
    other.close()
