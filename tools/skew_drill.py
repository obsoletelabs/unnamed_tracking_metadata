"""skew drill: a real mixed network of this release (wire 1) and the previous v0 release (CONTRACT-C16).

The previous release is unpacked from git (`git archive`, nothing in the repository changes), gets its
own virtual environment through `uv sync --frozen`, and runs as real HTTP servers next to servers of
this checkout. Every node is disposable (temporary folder, SQLite, synthetic data, loopback only).

It checks the two C16 hard gates and the refusal:

* a wire-1 follower still syncs FROM a v0 authority and reports `in_sync` only after verifying and applying its
  head (the upstream is shown as v0);
* a v0 follower of a wire-1 authority is refused (HTTP 409 `wire_unsupported`, `peer_v0`): nothing is applied
  and the authority lists that follower as refused;
* a v0 node upgrades in place without losing data: `atlas export-verify export` before the upgrade,
  `atlas migrate`, `atlas export-verify verify` after it (every row from before present and unchanged), and
  the upgraded node then follows both its v0 authority and the wire-1 authority that refused it before.

The previous release is pinned to a commit (`DEFAULT_PREVIOUS`, the last v2 commit before protocol
negotiation), not a branch. Bump it when the next release ships.

`python -m tools.skew_drill [--previous-ref <commit or tag>] [--previous-dir <checkout>] [--output skew.json]`
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from atlas import feed, sync, wire
from atlas.ids import canonical_id, ulid
from atlas.signing import load_identity

from .multinode_drill import PASSWORD, Node, free_port

ROOT = Path(__file__).resolve().parents[1]
#: v2 at 62c73f3 ("feat: read large achievement catalogues in bounded pages (#95)"): the last release
#: without protocol negotiation (federation protocol 1.0, migration head 0020).
DEFAULT_PREVIOUS = "62c73f36e7de4a0bd6b814a7dd992eacc7a670e1"
#: The other real v0 build: A12 (federation protocol 1.1, migration head 0030), the build the owner's live follower
#: runs. Its exact commit c91b631 is no longer on any branch of `origin` (the A12 branch was rebased); CI runs the
#: rebased A12 head 485d0cc (same head 0030 and protocol 1.1) until the owner pins c91b631 with a tag (`skew/a12`).
A12_PREVIOUS = "485d0cc8aab972f7b1063f2a7b760570985212c1"

_SETUP = r"""
import json, sys
from atlas import accounts, auth, main
from atlas.settings import Settings
a = json.loads(sys.argv[1])
settings = Settings(_env_file=None, database_url=a["db"], role=a["role"], public_url=a["url"],
                    email_verification="optional", initial_admin="drill-admin", media_cache_enabled=False,
                    secret_key=a["key"], bootstrap_admin_token=a["admin_token"], auto_migrate=True,
                    media_dir=a["media"])
st = main.create_app(settings).state.atlas
with st.maker() as s:
    accounts.register(s, settings, "drill-admin", a["password"], "admin@example.test")
    _, reader = auth.create_token(s, "drill-reader", ["read"])
    _, follower = auth.create_token(s, "drill-follower", ["read", "sync"])
    s.commit()
print(json.dumps({"node_id": st.ctx.identity.node_id, "public_b64": st.ctx.identity.public_b64,
                  "reader": reader, "follower": follower}))
"""


#: The previous release's own pull, run out of process against a wire-1 authority it was never allowed to pin (a v0
#: build cannot pin a 64-digit node id through its form): what it does when it meets wire 1. A 1.1-1.5 build refuses
#: itself from the v0 tombstone (`min_follower_protocol` "2.0", "Upgrade Atlas"); any build that gets as far as
#: `/sync/changes` is refused there with 409 `wire_unsupported`.
_V0_PULL = r"""
import json, sys
import httpx
from atlas import main, sync
from atlas.settings import Settings
a = json.loads(sys.argv[1])
settings = Settings(_env_file=None, database_url=a["db"], role=a["role"], public_url=a["url"], secret_key=a["key"],
                    media_cache_enabled=False, media_dir=a["media"])
st = main.create_app(settings).state.atlas
try:
    with httpx.Client() as client:
        n = sync.pull_once(st.maker, st.ctx, client, a["upstream"], a["token"], {a["node_id"]: a["public"]})
    print(json.dumps({"refused": None, "applied": n}))
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"refused": f"{type(exc).__name__}: {exc}"}))
"""


def _venv_python(folder: Path) -> Path:
    return folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare_previous(ref: str, folder: Path) -> tuple[Path, Path, str]:
    """Unpack `ref` into `folder` and install it into its own virtual environment."""
    commit = subprocess.run(["git", "rev-parse", "--verify", ref + "^{commit}"], cwd=ROOT, check=True,
                            capture_output=True, text=True).stdout.strip()
    archive = subprocess.run(["git", "archive", "--format=tar", commit], cwd=ROOT, check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(folder, filter="data")
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}
    subprocess.run(["uv", "sync", "--frozen", "--no-dev"], cwd=folder, check=True, env=env,
                   stdout=subprocess.DEVNULL)
    return folder, _venv_python(folder), commit


class PreviousNode(Node):
    """A node running the previous release. It is set up and inspected out of process (its schema is
    older than this checkout's models) and through its own HTTP API."""

    def __init__(self, root: Path, name: str, role: str, source: Path, python: Path):
        self.root, self.name, self.role, self.password, self.extra_env = root, name, role, PASSWORD, {}
        self.python, self.cwd = str(python), str(source)
        self.url = f"http://127.0.0.1:{free_port()}"
        self.db_path = root / f"{name}.db"
        self.database_url = f"sqlite:///{self.db_path.as_posix()}"
        self.admin_token, self.encryption_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        args = {"db": self.database_url, "role": role, "url": self.url, "key": self.encryption_key,
                "admin_token": self.admin_token, "media": str(root / f"{name}-media"), "password": PASSWORD}
        out = subprocess.run([self.python, "-c", _SETUP, json.dumps(args)], cwd=self.cwd, check=True,
                             capture_output=True, text=True, env=_clean_env())
        made = json.loads(out.stdout.strip().splitlines()[-1])
        self._identity = SimpleNamespace(node_id=made["node_id"], public_b64=made["public_b64"])
        self.reader, self.follower = made["reader"], made["follower"]
        self.process, self.client, self.admin = None, None, None
        self.log = (root / f"{name}.log").open("ab")

    @property
    def identity(self):
        return self._identity

    def _peer(self, node_id: str) -> tuple[str, str | None]:
        with sqlite3.connect(self.db_path) as db:
            return db.execute("SELECT id, last_error FROM peer WHERE node_id = ?", (node_id,)).fetchone()

    def add_peer(self, remote, authority=None):
        from .multinode_drill import add_peer_form

        identity = (authority or remote).identity
        response = add_peer_form(self.admin, remote.url, remote.follower, identity.node_id, identity.public_b64,
                                 (authority or remote).name)
        assert response.status_code == 303, f"add peer on {self.name}: {response.status_code}"
        return self._peer(identity.node_id)[0]

    def sync(self, peer_id):
        from .multinode_drill import csrf

        response = self.admin.post(f"/admin/peers/{peer_id}/sync", data={"csrf": csrf(self.admin)})
        assert response.status_code == 200, f"sync response on {self.name}: {response.status_code}"
        with sqlite3.connect(self.db_path) as db:
            return db.execute("SELECT last_error FROM peer WHERE id = ?", (peer_id,)).fetchone()[0]

    def v0_pull(self, upstream: Node) -> dict[str, Any]:
        """Run this previous release's own pull against `upstream`, pinning it in code (see `_V0_PULL`)."""
        args = {"db": self.database_url, "role": self.role, "url": self.url, "key": self.encryption_key,
                "media": str(self.root / f"{self.name}-media"), "upstream": upstream.url, "token": upstream.follower,
                "node_id": upstream.identity.node_id, "public": upstream.identity.public_b64}
        out = subprocess.run([self.python, "-c", _V0_PULL, json.dumps(args)], cwd=self.cwd, capture_output=True,
                             text=True, env=_clean_env())
        assert out.returncode == 0, (out.stdout[-2000:], out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def publish_dated(self, title: str, release_date: str) -> str:
        """A record with a release date: migration 0031 rewrites its view, the case R2 must not report as lost."""
        payload = {"connection": {"source_kind": "manual", "source": "manual", "source_scope": "release-drill"},
                   "observations": [{"external_id": title, "title": title, "release_date": release_date,
                                     "facts": {"genres": ["Puzzle"]}}]}
        response = self.client.post("/api/v2/observations", headers=self.headers(self.admin_token), json=payload)
        assert response.status_code == 200, f"publish on {self.name}: {response.status_code} {response.text[:300]}"
        return response.json()["results"][0]["atlas"]["canonical_id"]

    def upgrade(self) -> None:
        """Stop the previous release and run this checkout against the same database (auto-migrates)."""
        self.stop()
        self.python, self.cwd = sys.executable, str(ROOT)
        self.start()

    def cli(self, *argv: str) -> subprocess.CompletedProcess:
        env = _clean_env() | {"ATLAS_DATABASE_URL": self.database_url, "ATLAS_ROLE": self.role,
                              "ATLAS_PUBLIC_URL": self.url, "ATLAS_SECRET_KEY": self.encryption_key,
                              "ATLAS_MEDIA_DIR": str(self.root / f"{self.name}-media")}
        return subprocess.run([self.python, "-c", "import sys; from atlas.cli import main; sys.exit(main(sys.argv[1:]))",
                               *argv], cwd=self.cwd, env=env, capture_output=True, text=True)

    def close(self):
        self.stop()
        self.log.close()


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_") and k != "VIRTUAL_ENV"}


def _sign(node: Node, kind: str, body: dict[str, Any]) -> int:
    """Sign an entry as a newer release would (bypassing this release's emission gate)."""
    with node.state.maker() as session:
        seq = feed.append_unchecked(session, load_identity(session), kind, body)
        session.commit()
    return seq


def _observation(title: str, **payload: Any) -> dict[str, Any]:
    return {"canonical_id": canonical_id(), "media_type": "game",
            "observation": {"id": ulid(), "source_kind": "manual", "source": "manual", "source_scope": "skew-drill",
                            "external_id": "skew-" + ulid().lower(), "payload": {"title": title, **payload},
                            "source_updated_at": None, "observed_at": "2026-01-01T00:00:00+00:00"}}


def run(previous_ref: str = DEFAULT_PREVIOUS, previous_dir: Path | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"drill": "skew-v0-wire1", "started_at": datetime.now(UTC).isoformat(), "checks": [],
                              "transport": "isolated loopback HTTP, development mode, SQLite"}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="atlas-skew-", ignore_cleanup_errors=True) as folder, ExitStack() as stack:
        root = Path(folder)
        source, python, commit = _prepare(root, previous_ref, previous_dir)
        report["previous"] = {"ref": previous_ref if previous_dir is None else str(previous_dir), "commit": commit}
        report["current"] = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                                      text=True).stdout.strip()}
        new_authority = Node(root, "authority-w1", "authority")
        new_local = Node(root, "local-w1", "local")
        old_authority = PreviousNode(root, "authority-v0", "authority", source, python)
        old_local = PreviousNode(root, "local-v0", "local", source, python)
        for node in (new_authority, new_local, old_authority, old_local):
            stack.callback(node.close)
            node.start()

        info = new_authority.client.get("/api/v2/sync/info", headers=new_authority.headers()).json()
        old_info = old_authority.client.get("/api/v2/sync/info", headers=old_authority.headers()).json()
        assert (info["wire"]["min"], info["wire"]["max"]) == (wire.WIRE_MIN_PEER, wire.WIRE_MAX), info
        assert info["wire"]["reads_v0"] is True and info["contract"] == 1, info
        assert info["protocol"] == info["min_follower_protocol"] == wire.V0_TOMBSTONE["protocol"], info
        assert "wire" not in old_info and old_info["contract"] == 1
        checks.append(f"this release advertises wire {wire.WIRE_MAX} and the v0 tombstone protocol "
                      f"{wire.V0_TOMBSTONE["protocol"]}; the previous release advertises no wire (v0)")

        # Hard gate 1: a wire-1 follower <- a v0 authority.
        old_game = old_authority.publish("Skew Old Authority Game")
        new_peer = new_local.add_peer(old_authority)
        assert new_local.sync(new_peer) is None
        assert new_local.game(old_game)["title"] == "Skew Old Authority Game"
        with new_local.state.maker() as session:
            heads = sync.upstream_status(session, new_local.state.ctx, old_authority.identity.node_id)
        assert heads["state"] == "in_sync" and heads["upstream_wire"] is None, heads
        checks.append("a wire-1 follower syncs from a v0 authority and reports in_sync only after verifying and "
                      "applying its head (upstream shown as v0)")

        # The refusal: a v0 follower <- a wire-1 authority.
        first = new_authority.publish("Skew New Authority Game")
        from .multinode_drill import add_peer_form

        identity = new_authority.identity
        response = add_peer_form(old_local.admin, new_authority.url, new_authority.follower, identity.node_id,
                                 identity.public_b64, new_authority.name)
        assert response.status_code == 400, f"a v0 follower must not pin a wire-1 authority: {response.status_code}"
        assert old_local._peer(identity.node_id) is None
        assert old_local.client.get(f"/api/v2/games/{first}", headers=old_local.headers()).status_code == 404
        refusal = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.text))
        assert "does not match the public key" in refusal, refusal[:400]  # D5: a wide node id is not a v0 id
        changes = new_authority.client.get("/api/v2/sync/changes", params={"after": 0},  # what a v0 pull sends
                                           headers=new_authority.headers(new_authority.follower))
        assert changes.status_code == 409, changes.status_code
        error = changes.json()["error"]
        assert (error["code"], error["reason"]) == ("wire_unsupported", "peer_v0"), error
        followers = new_authority.client.get("/api/v2/sync/status",
                                             headers=new_authority.headers(new_authority.admin_token)).json()["followers"]
        assert [f["refused"] for f in followers] == ["peer_v0"], followers
        # The previous release's own pull code, pinned to the wire-1 authority in code (the case the form cannot
        # reach): a 1.1-1.5 build refuses itself from the v0 tombstone; whatever it does, nothing is applied.
        tried = old_local.v0_pull(new_authority)
        assert tried["refused"], tried
        assert old_local.client.get(f"/api/v2/games/{first}", headers=old_local.headers()).status_code == 404
        self_refused = "Upgrade Atlas" in tried["refused"] and wire.V0_TOMBSTONE["protocol"] in tried["refused"]
        report["v0_pull"] = {"previous_protocol": old_info.get("protocol"), "refused": tried["refused"][:300],
                             "self_refused_from_tombstone": self_refused}
        if old_info.get("protocol") not in (None, "1.0"):
            assert self_refused, tried  # 1.1-1.5 (A12): the tombstone makes it refuse before /sync/changes
        checks.append("the previous release's own pull, pinned to the wire-1 authority in code, is refused and applies "
                      f"nothing ({'self-refusal from the v0 tombstone' if self_refused else 'refused'}: "
                      f"{tried['refused'][:120]})")
        checks.append("a v0 follower cannot pin a wire-1 authority (its 64-digit node id is not a v0 id: the v0 release "
                      "answers 400 and pins nothing), and a v0-shaped pull of /sync/changes is refused with 409 "
                      "wire_unsupported (peer_v0), applies nothing, and the authority lists that follower as refused")

        # Hard gate 2: the v0 follower upgrades in place without losing data.
        old_peer = old_local.add_peer(old_authority)
        second = old_authority.publish("Skew Before Upgrade")
        dated = old_authority.publish_dated("Skew Dated Before Upgrade", "2011-04-19")
        assert old_local.sync(old_peer) is None
        assert old_local.game(second)["title"] == "Skew Before Upgrade"
        assert old_local.game(dated)["title"] == "Skew Dated Before Upgrade"
        old_local.stop()
        manifest = root / "local-v0-manifest.json"
        exported = _current_cli(old_local, "export-verify", "export", str(manifest))
        assert exported.returncode == 0, (exported.stdout, exported.stderr)
        migrated = _current_cli(old_local, "migrate")
        assert migrated.returncode == 0, (migrated.stdout, migrated.stderr)
        verified = _current_cli(old_local, "export-verify", "verify", str(manifest), "--json")
        assert verified.returncode == 0, (verified.stdout, verified.stderr)
        rewritten = json.loads(verified.stdout)["tables"]["canonical"]["derived_rewritten"]
        assert rewritten >= 1, verified.stdout[:2000]  # the dated record's view was rewritten by 0031, not lost
        report["export_verify"] = {"revision_before": json.loads(verified.stdout)["revision_before"],
                                   "canonical_derived_rewritten": rewritten}
        old_local.upgrade()
        assert old_local.game(second)["title"] == "Skew Before Upgrade"
        assert old_local.game(dated)["title"] == "Skew Dated Before Upgrade"
        checks.append("the v0 follower upgrades in place: export-verify before, `atlas migrate`, export-verify "
                      f"after reports every row present and unchanged ({exported.stdout.strip()}), a dated record "
                      f"included ({rewritten} record view(s) rewritten on purpose by migration 0031, reported as "
                      "derived)")

        third = old_authority.publish("Skew After Upgrade")
        assert old_local.sync(old_peer) is None
        assert old_local.game(third)["title"] == "Skew After Upgrade"
        new_peer_on_upgraded = old_local.add_peer(new_authority)
        assert old_local.sync(new_peer_on_upgraded) is None
        assert old_local.game(first)["title"] == "Skew New Authority Game"
        checks.append("after the upgrade the same node keeps following its v0 authority and now follows the wire-1 "
                      "authority that refused it before")
    report["status"] = "passed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def _prepare(root: Path, previous_ref: str, previous_dir: Path | None) -> tuple[Path, Path, str]:
    if previous_dir is None:
        (root / "previous").mkdir()
        return prepare_previous(previous_ref, root / "previous")
    return previous_dir, _venv_python(previous_dir), "local"


def _current_cli(node: Node, *argv: str) -> subprocess.CompletedProcess:
    """Run this checkout's CLI against `node`'s database, with the node's settings."""
    env = _clean_env() | {"ATLAS_DATABASE_URL": node.database_url, "ATLAS_ROLE": node.role,
                          "ATLAS_PUBLIC_URL": node.url, "ATLAS_SECRET_KEY": node.encryption_key,
                          "ATLAS_MEDIA_DIR": str(node.root / f"{node.name}-media")} | node.extra_env
    return subprocess.run([sys.executable, "-c", "import sys; from atlas.cli import main; sys.exit(main(sys.argv[1:]))",
                           *argv], cwd=ROOT, env=env, capture_output=True, text=True)


def main_cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--previous-ref", default=DEFAULT_PREVIOUS, help="git ref of the previous (v0) release")
    parser.add_argument("--previous-dir", type=Path,
                        help="an existing checkout of the previous release with its own .venv (skips git and uv)")
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    args = parser.parse_args()
    report = run(args.previous_ref, args.previous_dir)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main_cli()
