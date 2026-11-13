"""A23 evidence: a real node (the code of the current working directory, served by uvicorn) on a throwaway SQLite
file, role `local`, Open Library on by default. Open Library itself is replaced by the synthetic A22 fixtures (the
fake transport of tests/test_a22_openlibrary.py), so no request leaves the machine and every book is invented.

Run from the worktree:  .venv/Scripts/python <this file> <out.json> <stop-file>
It stays up until <stop-file> exists.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import uvicorn  # noqa: E402

from atlas import main  # noqa: E402
from tests.conftest import build_settings  # noqa: E402
from tests.test_a22_openlibrary import fixture_client, ol  # noqa: E402

out, stop = Path(sys.argv[1]), Path(sys.argv[2])
root = Path(tempfile.mkdtemp(prefix="a23-lab-"))
PORT = 8791
settings = build_settings(role="local", database_url=f"sqlite:///{(root / 'atlas.db').as_posix()}",
                          media_dir=str(root / "media"), media_cache_enabled=False, environment="dev",
                          public_url=f"http://127.0.0.1:{PORT}", initial_admin="curator")
app = main.create_app(settings)
st = app.state.atlas
st.providers.client = fixture_client()
st.providers.providers["openlibrary"] = ol()
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
while not server.started:
    time.sleep(0.1)
out.write_text(json.dumps({"node": f"http://127.0.0.1:{PORT}", "user": "curator",
                           "password": "lab-Synthetic-Pass-12345"}), encoding="utf-8")
print("ready", PORT, flush=True)
try:
    while not stop.exists():
        time.sleep(0.5)
finally:
    server.should_exit = True
    thread.join(10)
