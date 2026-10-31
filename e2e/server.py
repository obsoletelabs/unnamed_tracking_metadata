"""Boot a throwaway Atlas node for the end-to-end tests.

Everything lives in a fresh temporary directory (SQLite database and media), the node listens on
127.0.0.1 only, and the directory is removed when the process stops. Nothing here talks to a real
service: provider integrations are disabled and all credentials are synthetic test values.

Run from the repository root:  uv run python e2e/server.py
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("E2E_PORT", "8765"))
# Synthetic, test-only values. The node is bound to loopback and its database is deleted on exit.
BOOTSTRAP_TOKEN = os.environ.get("E2E_BOOTSTRAP_TOKEN", "e2e-synthetic-bootstrap-token-0123456789abcdef")
ADMIN_USER = os.environ.get("E2E_ADMIN_USER", "alice")


def main() -> int:
    data = Path(tempfile.mkdtemp(prefix="atlas-e2e-"))
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PYTHONUTF8": "1",
        "ATLAS_ROLE": "local",
        "ATLAS_PUBLIC_URL": f"http://127.0.0.1:{PORT}",
        "ATLAS_DATABASE_URL": f"sqlite:///{(data / 'atlas.db').as_posix()}",
        "ATLAS_MEDIA_DIR": (data / "media").as_posix(),
        "ATLAS_INITIAL_ADMIN": ADMIN_USER,
        "ATLAS_BOOTSTRAP_ADMIN_TOKEN": BOOTSTRAP_TOKEN,
        "ATLAS_SECRET_KEY": secrets.token_urlsafe(32),
        "ATLAS_STEAM_ENABLED": "false",
        "ATLAS_MEDIA_CACHE_ENABLED": "false",  # no outbound image fetches during tests
    }
    try:
        proc = subprocess.Popen([sys.executable, "-m", "atlas.cli", "serve", "--host", "127.0.0.1", "--port", str(PORT)],
                                cwd=ROOT, env=env)
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return proc.wait()
    finally:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
