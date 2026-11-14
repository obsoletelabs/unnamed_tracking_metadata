"""Boot a throwaway Atlas node for the end-to-end tests.

Everything lives in e2e/.run/node (git-ignored; SQLite database and media), the node listens on
127.0.0.1 only, and the directory is wiped at the start of every run. It is also removed when this
process exits normally, but Playwright kills the process tree on shutdown (always on Windows), so
the cleanup at exit is best effort and the wipe at the next start is what guarantees a clean slate.
Nothing here talks to a real service: provider integrations are disabled and all credentials are synthetic test values.

Run from the repository root:  uv run python e2e/server.py
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("E2E_PORT", "8765"))
# Synthetic, test-only values. The node is bound to loopback; its database sits in a git-ignored folder
# that is wiped on the next start.
BOOTSTRAP_TOKEN = os.environ.get("E2E_BOOTSTRAP_TOKEN", "e2e-synthetic-bootstrap-token-0123456789abcdef")
ADMIN_USER = os.environ.get("E2E_ADMIN_USER", "alice")


def main() -> int:
    data = ROOT / "e2e" / ".run" / "node"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PYTHONUTF8": "1",
        # E2E_ROLE=authority plays an authority (emit protocol 1.0: platinum is gated); the default is a local node.
        "ATLAS_ROLE": os.environ.get("E2E_ROLE", "local"),
        "ATLAS_PUBLIC_URL": f"http://127.0.0.1:{PORT}",
        "ATLAS_DATABASE_URL": f"sqlite:///{(data / 'atlas.db').as_posix()}",
        "ATLAS_MEDIA_DIR": (data / "media").as_posix(),
        "ATLAS_INITIAL_ADMIN": ADMIN_USER,
        "ATLAS_BOOTSTRAP_ADMIN_TOKEN": BOOTSTRAP_TOKEN,
        "ATLAS_SECRET_KEY": secrets.token_urlsafe(32),
        "ATLAS_STEAM_ENABLED": "false",
        "ATLAS_MEDIA_CACHE_ENABLED": "false",  # no outbound image fetches during tests
    }
    if env["ATLAS_ROLE"] == "authority":  # an authority requires verified email by default; there is no mail here
        env.setdefault("ATLAS_EMAIL_VERIFICATION", "optional")
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "atlas.cli", "serve", "--host", "127.0.0.1", "--port", str(PORT)], cwd=ROOT, env=env
        )
        return proc.wait()
    except KeyboardInterrupt:
        if proc is not None:
            proc.terminate()
            return proc.wait()
        return 1
    finally:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
