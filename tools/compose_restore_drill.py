"""Backup and restore drill on the real authority compose stack (issue #27).

`tools/restore_drill.py` proves the archive format in-process. This drill runs the shipped
`deploy/authority/compose.yaml` (PostgreSQL, the migrate job, the Atlas container and the `backup` service)
with Docker Compose and checks both documented restore paths end to end:

1. the Atlas archive (`atlas backup create` / `verify` / `restore`) into a stack whose volumes were destroyed;
2. the `pg_dump -Fc` file of the `backup` service, restored with `deploy/authority/restore.sh`.

After each restore it compares what an operator relies on: the node id and public key, a record, an API token,
a cached image served by `/media/<sha256>`, the feed head and the signing key still encrypted at rest.

Caddy is left out (a compose override puts it in a profile that is never started), so the drill needs no DNS,
no certificates and no host ports. Everything runs under a random compose project name and is removed with
`down -v` at the end, also on failure. Nothing here talks to the internet except to pull `postgres:16`.

    docker build -t atlas-drill:local .
    python tools/compose_restore_drill.py --image atlas-drill:local --output compose-restore.json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = ROOT / "deploy" / "authority"

# Runs inside the Atlas container with its own settings (PostgreSQL, ATLAS_SECRET_KEY): one record, a read
# token, an account and a cached image, the things a restore must bring back.
SEED = r"""
import hashlib, io, json
from PIL import Image
from atlas import accounts, auth, db, media, service
from atlas.contract import ObservationBatchV1
from atlas.models import Artwork, now
from atlas.settings import get_settings
from atlas.signing import load_identity
s = get_settings()
maker = db.make_sessionmaker(db.make_engine(s.database_url), s)
with maker() as session:
    batch = ObservationBatchV1.model_validate({"connection": {"source_kind": "manual", "source": "manual",
        "source_scope": "compose-drill"}, "observations": [{"external_id": "drill-1", "title": "Compose Drill Game",
        "release_date": "2026-11-07"}]})
    result, = service.ingest(session, service.Ctx(s, load_identity(session, s)), batch, curator=True)
    _, token = auth.create_token(session, "drill-read", ["read"])
    accounts.register(session, s, "drilladmin", "compose-drill-password", "drill@example.test")
    buf = io.BytesIO(); Image.new("RGB", (4, 4), "#3c4a78").save(buf, "PNG"); data = buf.getvalue()
    digest = media.store(s, data, "png")
    session.add(Artwork(canonical_id=result.canonical_id, source="manual", kind="key_art",
                        url="https://example.test/drill.png", blob_hash=digest, content_type="image/png",
                        size_bytes=len(data), cached_at=now(), redistributable=True))
    session.commit()
print(json.dumps({"cid": result.canonical_id, "token": token, "digest": digest}))
"""

# Runs inside the Atlas container after a restore: what an operator and a client would check.
PROBE = r"""
import json, sys, urllib.request
from sqlalchemy import func, select
from atlas import accounts, db, signing
from atlas.models import ChangeLog
from atlas.settings import get_settings
from atlas.signing import load_identity
cid, token, digest = sys.argv[1:4]
s = get_settings()
def get(path, auth=None):
    req = urllib.request.Request("http://127.0.0.1:8000" + path, headers={"Authorization": f"Bearer {auth}"} if auth else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
status, body = get(f"/api/v2/games/{cid}", token)
image_status, image = get(f"/media/{digest}")
with db.make_sessionmaker(db.make_engine(s.database_url), s)() as session:
    ident = load_identity(session, s)
    out = {"node_id": ident.node_id, "public_key": ident.trust_public_b64,
           "key_at_rest": signing.signing_key_at_rest(session),
           "feed_head": session.scalar(select(func.max(ChangeLog.seq))),
           "password_ok": accounts.authenticate(session, "drilladmin", "compose-drill-password") is not None}
out.update(record_status=status, title=json.loads(body)["title"] if status == 200 else None,
           image_status=image_status, image_bytes=len(image))
print(json.dumps(out))
"""

OVERRIDE = """\
# Written by tools/compose_restore_drill.py: no Caddy (no DNS, ports or certificates needed).
services:
  caddy:
    profiles: ["never"]
  migrate:
    volumes: ["media:/media"]   # so `run migrate atlas backup restore` puts cached images where Atlas serves them
"""


class DrillError(RuntimeError):
    pass


class Stack:
    def __init__(self, folder: Path, project: str, image: str, log: list[str]):
        self.folder, self.project, self.image, self.log = folder, project, image, log
        self.env = {**os.environ, "COMPOSE_PROJECT_NAME": project}
        self.secrets: list[str] = []  # redacted from the command log (the report is uploaded as a CI artifact)

    def _logged(self, cmd: list[str]) -> str:
        text = " ".join(cmd)
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def run(self, *args: str, stdin: str | None = None, check: bool = True, timeout: int = 600) -> str:
        cmd = ["docker", "compose", *args]
        self.log.append("$ " + self._logged(cmd))
        p = subprocess.run(cmd, cwd=self.folder, env=self.env, input=stdin, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        if check and p.returncode != 0:
            raise DrillError(f"{self._logged(cmd)} failed ({p.returncode}):\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
        return p.stdout

    def python(self, script: str, *args: str) -> dict:
        out = self.run("exec", "-T", "atlas", "python", "-", *args, stdin=script)
        return json.loads(out.strip().splitlines()[-1])

    def wait_healthy(self, deadline: float = 180) -> None:
        end = time.monotonic() + deadline
        probe = "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v2/health', timeout=4)"
        while time.monotonic() < end:
            p = subprocess.run(["docker", "compose", "exec", "-T", "atlas", "python", "-c", probe], cwd=self.folder,
                               env=self.env, capture_output=True, text=True)
            if p.returncode == 0:
                return
            time.sleep(2)
        raise DrillError("atlas did not become healthy:\n" + self.run("logs", "--tail", "60", check=False))


def prepare(folder: Path, image: str) -> None:
    for name in ("compose.yaml", "Caddyfile", "restore.sh"):
        shutil.copy(AUTHORITY / name, folder / name)
    (folder / "compose.override.yaml").write_text(OVERRIDE, encoding="utf-8")
    (folder / "backups").mkdir()
    repo, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "latest")
    env = {
        "ATLAS_IMAGE": repo, "ATLAS_TAG": tag, "ATLAS_DOMAIN": "drill.invalid",
        "ATLAS_PUBLIC_URL": "https://drill.invalid", "ATLAS_INITIAL_ADMIN": "drilladmin",
        "ATLAS_BOOTSTRAP_ADMIN_TOKEN": secrets.token_hex(24), "ATLAS_SECRET_KEY": secrets.token_urlsafe(32),
        "POSTGRES_PASSWORD": secrets.token_hex(16), "ATLAS_MAIL_BACKEND": "outbox",
    }
    (folder / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")


def run(image: str) -> dict:
    project = "atlasdrill" + secrets.token_hex(3)
    report: dict = {"started_at": datetime.now(UTC).isoformat(), "image": image, "project": project, "checks": []}
    checks, log = report["checks"], []
    with tempfile.TemporaryDirectory(prefix="atlas-compose-drill-") as tmp:
        folder = Path(tmp)
        prepare(folder, image)
        stack = Stack(folder, project, image, log)
        try:
            stack.run("up", "-d", "atlas", "backup")
            stack.wait_healthy()
            seed = stack.python(SEED)
            stack.secrets.append(seed["token"])
            before = stack.python(PROBE, seed["cid"], seed["token"], seed["digest"])
            if not (before["record_status"] == 200 and before["image_status"] == 200 and before["password_ok"]):
                raise DrillError(f"the seeded stack is not healthy: {before}")
            if before["key_at_rest"] != "encrypted":
                raise DrillError(f"signing key at rest is {before['key_at_rest']}, expected encrypted")
            checks.append("authority stack up (PostgreSQL, migrate job, Atlas, backup service); seeded and probed")

            stack.run("exec", "-T", "atlas", "atlas", "backup", "create", "/data/drill.tar.gz")
            stack.run("exec", "-T", "atlas", "atlas", "backup", "verify", "/data/drill.tar.gz", "--secret-check")
            archive = folder / "backups" / "drill.tar.gz"
            stack.run("cp", "atlas:/data/drill.tar.gz", str(archive))
            # `compose cp` keeps the archive's 0600 mode but gives it to the host user, so on Linux the container's
            # uid 10001 could not read it back through the bind mount (Permission denied). Same step an operator
            # takes (docs/guide/backup.md); the drill's temporary folder itself is private (0700).
            os.chmod(archive, 0o644)
            stack.run("exec", "-T", "backup", "pg_dump", "-Fc", "-f", "/backups/drill.dump")
            if not (folder / "backups" / "drill.dump").stat().st_size:
                raise DrillError("pg_dump wrote an empty file")
            checks.append("Atlas archive created and verified with --secret-check; "
                          "pg_dump -Fc written by the backup service")

            # 1. total loss: every volume goes, then the Atlas archive comes back on a fresh stack
            stack.run("down", "-v")
            stack.run("up", "-d", "db")
            stack.run("run", "--rm", "migrate")
            stack.run("run", "--rm", "-v", f"{(folder / 'backups').as_posix()}:/restore:ro",
                      "migrate",
                      "atlas", "backup", "restore", "/restore/drill.tar.gz")
            stack.run("up", "-d", "atlas")
            stack.wait_healthy()
            after = stack.python(PROBE, seed["cid"], seed["token"], seed["digest"])
            if after != before:
                raise DrillError(f"Atlas archive restore differs:\nbefore {before}\nafter  {after}")
            stack.run("exec", "-T", "atlas", "atlas", "check")  # exit 1 on any consistency finding
            checks.append("after down -v, migrate + `atlas backup restore` gave the same node id, key, record, token, "
                          "password, cached image and feed head; the key is still encrypted at rest; `atlas check` is clean")

            # 2. the pg_dump path with the shipped restore.sh, onto a stack that has drifted since the dump
            stack.run("exec", "-T", "db", "psql", "-U", "atlas", "-d", "atlas", "-c", "DELETE FROM api_token")
            drifted = stack.python(PROBE, seed["cid"], seed["token"], seed["digest"])
            if drifted["record_status"] != 401:
                raise DrillError(f"drift did not take: {drifted}")
            shell = shutil.which("sh") or shutil.which("bash")
            if shell is None:
                raise DrillError("restore.sh needs a POSIX shell (sh)")
            p = subprocess.run([shell, "restore.sh", "backups/drill.dump"], cwd=folder, env=stack.env,
                               capture_output=True, text=True, timeout=600)
            log.append(f"$ sh restore.sh backups/drill.dump -> {p.returncode}")
            if p.returncode != 0:
                raise DrillError(f"restore.sh failed:\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
            stack.wait_healthy()
            again = stack.python(PROBE, seed["cid"], seed["token"], seed["digest"])
            if again != before:
                raise DrillError(f"pg_dump restore differs:\nbefore {before}\nafter  {again}")
            checks.append("restore.sh replaced a drifted database (tokens deleted) from the pg_dump file; "
                          "the same checks pass")
            report.update(status="passed", fingerprint={k: before[k] for k in ("node_id", "feed_head", "title")})
        except Exception as exc:  # the report says what failed; the stack is still removed
            report.update(status="failed", error=str(exc)[-4000:])
        finally:
            stack.run("--profile", "never", "down", "-v", "--remove-orphans", check=False)
            report["commands"] = log
            report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True, help="the Atlas image to drill, e.g. atlas-drill:local")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.image)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main_cli())
