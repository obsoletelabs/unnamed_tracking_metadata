"""A22 evidence: a real running node (the code of the current working directory) on a throwaway SQLite file, with
the defaults of this release: Open Library on, no provider contact. The screenshots show Admin -> Integrations ->
Open Library before and after an administrator sets the contact. No request is made to Open Library.

Run from the worktree:  .venv/Scripts/python <this file> <out.json> <stop-file>
It stays up until <stop-file> exists.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from tools.multinode_drill import PASSWORD, Node  # noqa: E402

out, stop = Path(sys.argv[1]), Path(sys.argv[2])
root = Path(tempfile.mkdtemp(prefix="a22-lab-"))
node = Node(root, "node", "local")
node.start()
out.write_text(json.dumps({"node": node.url, "user": "drill-admin", "password": PASSWORD}), encoding="utf-8")
print("ready", node.url, flush=True)
try:
    while not stop.exists():
        time.sleep(0.5)
finally:
    node.close()
