"""skew-1.0 drill: a real mixed-version network of this release and the previous one (CONTRACTS-v1 C1.9).

The previous release is unpacked from git (`git archive`, nothing in the repository changes), gets its
own virtual environment through `uv sync --frozen`, and runs as real HTTP servers next to servers of
this checkout. Every node is disposable (temporary folder, SQLite, synthetic data, loopback only).

Checks, in both directions:

* a previous-release (1.0) follower converges on an authority of this release emitting 1.0;
* a follower of this release accepts a previous-release authority (no `protocol` advertised = 1.0);
* the authority records that its follower speaks 1.0, which is what keeps `emit_protocol` down;
* a newer construct (an unknown kind, an observation with an `ext` member) that the 1.0 follower
  skipped is recovered after that follower is upgraded in place: `atlas sync replay --rescan` records
  both (classification only), a plain `atlas sync replay` leaves them alone (never out of order), and
  `atlas sync replay --historical` re-applies the retained range in feed order; the unknown kind stays
  parked with its reason.

The previous release is pinned to a commit (`DEFAULT_PREVIOUS`, the last v2 commit before protocol
negotiation), not a branch: once this release is merged, the branch head would no longer be the previous
release. Bump it when the next release ships.

`python -m tools.skew_drill [--previous-ref <commit or tag>] [--output skew.json]`

`--scenario platinum` (skew-1.1, contract C7.5) runs a 1.1 release as the follower instead: an authority of
this release at emit protocol 1.2 publishes a platinum achievement list; the 1.1 follower must park it
(`unsupported_capability`, token `tier-platinum`), keep its previous complete list and hold back later
entries, and converge after an in-place upgrade. No 1.1 release is tagged yet, so `--previous-ref` (or
`--previous-dir`) is required for it.

`--scenario dates` (skew-1.2, contract C5.6) runs a 1.2 release as the follower: an authority of this release
at emit protocol 1.3 publishes an observation with partial and regional release dates (`ext.release`); the
1.2 follower must park it (`unsupported_capability`, token `dates-v1`) rather than strip the dates, keep the
record exactly as it was, hold back later entries, and converge after an in-place upgrade (which runs
migration 0031 on its database). `--previous-ref`/`--previous-dir` must name a 1.2 release.

`--scenario relations` (skew-1.4, contract C6) runs any earlier release as the follower: an authority of this
release at emit protocol 1.4 publishes a relation (`relation` entry) through the API. A 1.1, 1.2 or 1.3 follower
must park it (`unsupported_kind`), hold back later entries, and converge after an in-place upgrade (migration
0032) with a plain `atlas sync replay`. A 1.0 follower cannot park: it passes over it, and after the upgrade
`atlas sync replay --rescan` records it (`dropped_by_previous_release`) and `atlas sync replay --historical`
applies it. Without `--previous-ref`/`--previous-dir` it runs against the 1.0 release.
"""

from __future__ import annotations

import argparse
import io
import json
import os
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

from atlas import feed, feed_schema, sync
from atlas.ids import canonical_id, ulid
from atlas.models import ChangeLog
from atlas.signing import load_identity

from .multinode_drill import PASSWORD, Node, free_port

ROOT = Path(__file__).resolve().parents[1]
#: v2 at 62c73f3 ("feat: read large achievement catalogues in bounded pages (#95)"): the last release
#: without protocol negotiation (federation protocol 1.0).
DEFAULT_PREVIOUS = "62c73f36e7de4a0bd6b814a7dd992eacc7a670e1"

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
        from .multinode_drill import csrf

        identity = (authority or remote).identity
        response = self.admin.post("/admin/peers", data={"csrf": csrf(self.admin), "name": (authority or remote).name,
                    "url": remote.url, "node_id": identity.node_id, "public_key": identity.public_b64,
                    "token": remote.follower})
        assert response.status_code == 303, f"add peer on {self.name}: {response.status_code}"
        return self._peer(identity.node_id)[0]

    def sync(self, peer_id):
        from .multinode_drill import csrf

        response = self.admin.post(f"/admin/peers/{peer_id}/sync", data={"csrf": csrf(self.admin)})
        assert response.status_code == 200, f"sync response on {self.name}: {response.status_code}"
        with sqlite3.connect(self.db_path) as db:
            return db.execute("SELECT last_error FROM peer WHERE id = ?", (peer_id,)).fetchone()[0]

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
    report: dict[str, Any] = {"drill": "skew-1.0", "started_at": datetime.now(UTC).isoformat(), "checks": [],
                              "transport": "isolated loopback HTTP, development mode, SQLite"}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="atlas-skew-", ignore_cleanup_errors=True) as folder, ExitStack() as stack:
        root = Path(folder)
        if previous_dir is None:
            (root / "previous").mkdir()
            source, python, commit = prepare_previous(previous_ref, root / "previous")
        else:
            source, python, commit = previous_dir, _venv_python(previous_dir), "local"
        report["previous"] = {"ref": previous_ref if previous_dir is None else str(previous_dir), "commit": commit}
        report["current"] = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                                      text=True).stdout.strip()}
        new_authority = Node(root, "authority-1.1", "authority")
        new_local = Node(root, "local-1.1", "local")
        old_authority = PreviousNode(root, "authority-1.0", "authority", source, python)
        old_local = PreviousNode(root, "local-1.0", "local", source, python)
        for node in (new_authority, new_local, old_authority, old_local):
            stack.callback(node.close)
            node.start()

        info = new_authority.client.get("/api/v2/sync/info", headers=new_authority.headers()).json()
        old_info = old_authority.client.get("/api/v2/sync/info", headers=old_authority.headers()).json()
        assert info["protocol"] == feed_schema.PROTOCOL and info["emit_protocol"] == "1.0" and info["contract"] == 1
        assert "protocol" not in old_info and old_info["contract"] == 1
        checks.append(f"this release advertises protocol {feed_schema.PROTOCOL} / emit 1.0; the previous release "
                      "advertises none (= 1.0)")

        # 1.0 follower <- 1.1 authority.
        first = new_authority.publish("Skew New Authority Game")
        peer = old_local.add_peer(new_authority)
        assert old_local.sync(peer) is None
        assert old_local.game(first)["title"] == "Skew New Authority Game"
        followers = new_authority.client.get("/api/v2/sync/status",
                                             headers=new_authority.headers(new_authority.admin_token)).json()["followers"]
        assert [f["protocol"] for f in followers] == ["1.0"], followers
        checks.append("a previous-release follower converges on this release's authority at emit protocol 1.0, "
                      "and the authority records that follower as protocol 1.0")

        # 1.1 follower <- 1.0 authority.
        old_game = old_authority.publish("Skew Old Authority Game")
        new_peer = new_local.add_peer(old_authority)
        assert new_local.sync(new_peer) is None
        assert new_local.game(old_game)["title"] == "Skew Old Authority Game"
        with new_local.state.maker() as session:
            heads = sync.upstream_status(session, new_local.state.ctx, old_authority.identity.node_id)
        assert heads["state"] == "in_sync" and heads["upstream_protocol"] == "1.0", heads
        checks.append("a follower of this release accepts a previous-release authority (N-1) and reports in_sync "
                      "only after verifying and applying its head")

        # A newer construct, the 1.0 follower, then an in-place upgrade.
        unknown = _sign(new_authority, "relationship", {"canonical_id": "atlas:" + "0" * 26, "kind": "dlc_of"})
        # A valid 1.3 extension (C5.2): the previous release rejects the unknown `ext` key; this one applies it.
        extended = _observation("Skew Extended Game",
                                ext={"release": {"first": {"date": "2026", "precision": "year"}}})
        ext_seq = _sign(new_authority, "obs", extended)
        after = new_authority.publish("Skew After Newer Entries")
        assert old_local.sync(peer) is None  # the previous release passes over both silently (atlas#33)
        assert old_local.client.get(f"/api/v2/games/{extended['canonical_id']}",
                                    headers=old_local.headers()).status_code == 404
        assert old_local.game(after)["title"] == "Skew After Newer Entries"
        checks.append(f"the previous release skips entries {unknown} (unknown kind) and {ext_seq} (observation with "
                      "an ext member) without recording them: the documented 1.0 limitation")

        old_local.upgrade()
        rescan = old_local.cli("sync", "replay", "--rescan")
        assert rescan.returncode == 3 and "recorded 2 parked" in rescan.stdout, (rescan.stdout, rescan.stderr)
        status = old_local.cli("sync", "status", "--json")
        upstream = json.loads(status.stdout)["upstreams"][new_authority.identity.node_id]
        assert status.returncode == 3 and upstream["parked"]["first_seq"] == unknown, upstream
        assert upstream["parked"]["first_reason"] == "unsupported_kind"
        assert upstream["applied_head"] == upstream["verified_head"]  # the cursor never moved back
        replay = old_local.cli("sync", "replay")
        assert replay.returncode == 3 and "replay --historical" in replay.stdout, (replay.stdout, replay.stderr)
        assert old_local.client.get(f"/api/v2/games/{extended['canonical_id']}",
                                    headers=old_local.headers()).status_code == 404  # never out of order
        replay = old_local.cli("sync", "replay", "--historical")
        assert replay.returncode == 3 and "unsupported kind" in replay.stdout + replay.stderr, (replay.stdout, replay.stderr)
        recovered = old_local.game(extended["canonical_id"])
        assert recovered["title"] == "Skew Extended Game", recovered
        assert recovered["first_release"] == {"date": "2026", "precision": "year"}, recovered
        assert recovered["release_year"] == 2026 and "release_date" not in recovered, recovered
        assert old_local.game(after)["title"] == "Skew After Newer Entries"
        assert old_local.sync(peer) is None
        checks.append("after upgrading that follower in place, `atlas sync replay --rescan` records both (exit 3), "
                      "a plain `atlas sync replay` applies nothing below the cursor, and `atlas sync replay "
                      "--historical` re-applies the retained range in feed order: the dropped observation is "
                      "applied while the unknown kind stays parked with its reason shown")
    report["status"] = "passed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def _prepare(root: Path, previous_ref: str, previous_dir: Path | None) -> tuple[Path, Path, str]:
    if previous_dir is None:
        (root / "previous").mkdir()
        return prepare_previous(previous_ref, root / "previous")
    return previous_dir, _venv_python(previous_dir), "local"


def _achievements(node: Node, cid: str) -> set[tuple[str, str | None, str]]:
    response = node.client.get(f"/api/v2/games/{cid}/achievements", headers=node.headers())
    assert response.status_code == 200, f"achievements on {node.name}: {response.status_code}"
    return {(a["name"], a["tier"], a["key"]) for a in response.json()["results"]}


def run_platinum(previous_ref: str, previous_dir: Path | None = None) -> dict[str, Any]:
    """skew-1.1: a real 1.1 follower meets platinum from a 1.2 authority (C7.5)."""
    report: dict[str, Any] = {"drill": "skew-1.1-platinum", "started_at": datetime.now(UTC).isoformat(),
                              "checks": [], "transport": "isolated loopback HTTP, development mode, SQLite"}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="atlas-skew11-", ignore_cleanup_errors=True) as folder, ExitStack() as stack:
        root = Path(folder)
        source, python, commit = _prepare(root, previous_ref, previous_dir)
        report["previous"] = {"ref": previous_ref if previous_dir is None else str(previous_dir), "commit": commit}
        report["current"] = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                                      text=True).stdout.strip()}
        authority = Node(root, "authority-1.2", "authority", extra_env={"ATLAS_EMIT_PROTOCOL": "1.2"})
        follower = PreviousNode(root, "local-1.1", "local", source, python)
        for node in (authority, follower):
            stack.callback(node.close)
            node.start()
        old_info = follower.client.get("/api/v2/sync/info", headers=follower.headers()).json()
        info = authority.client.get("/api/v2/sync/info", headers=authority.headers()).json()
        assert old_info.get("protocol") == "1.1", old_info.get("protocol")
        assert (info["protocol"], info["emit_protocol"]) == (feed_schema.PROTOCOL, "1.2"), info
        checks.append(f"the follower runs a 1.1 release; the authority speaks {info['protocol']} and emits 1.2")

        cid = authority.publish("Skew Trophy Hall")
        curator = authority.headers(authority.admin_token)
        start = [{"name": "Start", "tier": "bronze"}]
        assert authority.client.put(f"/api/v2/games/{cid}/achievements", headers=curator,
                                    json={"items": start}).status_code == 200
        peer = follower.add_peer(authority)
        assert follower.sync(peer) is None
        before = _achievements(follower, cid)
        assert {(n, t) for n, t, _ in before} == {("Start", "bronze")}, before
        response = authority.client.put(f"/api/v2/games/{cid}/achievements", headers=curator,
                                        json={"items": [*start, {"name": "Everything", "tier": "platinum"}]})
        assert response.status_code == 200, response.text
        with authority.state.maker() as session:
            platinum_seq = feed.head_seq(session)
        later = authority.publish("Skew After Platinum")
        follower.sync(peer)
        status = follower.client.get("/api/v2/sync/status", headers=follower.headers(follower.admin_token)).json()
        up = status["upstreams"][authority.identity.node_id]
        assert up["state"] == "parked" and up["parked"]["first_seq"] == platinum_seq, up
        assert up["parked"]["first_reason"] == "unsupported_capability" and up["parked"]["first_kind"] == "achievements"
        assert up["applied_head"] == platinum_seq - 1 and up["verified_head"] == platinum_seq + 1, up
        assert _achievements(follower, cid) == before  # not dropped, not partial: the previous list stays
        assert follower.client.get(f"/api/v2/games/{later}", headers=follower.headers()).status_code == 404
        checks.append(f"the 1.1 follower parks the platinum list at seq {platinum_seq} (unsupported_capability), "
                      "keeps its previous complete list, holds back the later record and still verifies and "
                      "retains the later entry")

        follower.upgrade()
        replay = follower.cli("sync", "replay")
        assert replay.returncode == 0, (replay.stdout, replay.stderr)
        assert _achievements(follower, cid) == _achievements(authority, cid)
        assert {t for _, t, _ in _achievements(follower, cid)} == {"bronze", "platinum"}
        assert follower.game(later)["title"] == "Skew After Platinum"
        status = follower.cli("sync", "status", "--json")
        assert status.returncode == 0, (status.stdout, status.stderr)
        checks.append("after upgrading the follower in place, replay applies the parked list and the later record: "
                      "names, tiers and keys equal the authority's, and `atlas sync status` exits 0")
    report["status"] = "passed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


RELEASE = {"first": {"date": "2027", "precision": "year"},
           "entries": [{"date": "2027", "precision": "year", "platform": "pc", "region": "ww", "category": "full"},
                       {"date": "2026-11", "precision": "month", "platform": "pc", "region": "ww",
                        "category": "beta"},
                       {"date": "2027-03-01", "precision": "day", "platform": "Switch", "region": "jp",
                        "category": "port"}]}


def _post_release(node: Node, title: str, release: dict[str, Any]) -> str:
    payload = {"connection": {"source_kind": "manual", "source": "manual", "source_scope": "release-drill"},
               "observations": [{"external_id": title, "title": title, "facts": {"genres": ["Puzzle"]},
                                 "ext": {"release": release}}]}
    response = node.client.post("/api/v2/observations", headers=node.headers(node.admin_token), json=payload)
    assert response.status_code == 200, f"publish on {node.name}: {response.status_code} {response.text[:300]}"
    return response.json()["results"][0]["atlas"]["canonical_id"]


def run_dates(previous_ref: str, previous_dir: Path | None = None) -> dict[str, Any]:
    """skew-1.2: a real 1.2 follower meets partial and regional release dates from a 1.3 authority (C5.6)."""
    report: dict[str, Any] = {"drill": "skew-1.2-dates", "started_at": datetime.now(UTC).isoformat(),
                              "checks": [], "transport": "isolated loopback HTTP, development mode, SQLite"}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="atlas-skew12-", ignore_cleanup_errors=True) as folder, ExitStack() as stack:
        root = Path(folder)
        source, python, commit = _prepare(root, previous_ref, previous_dir)
        report["previous"] = {"ref": previous_ref if previous_dir is None else str(previous_dir), "commit": commit}
        report["current"] = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                                      text=True).stdout.strip()}
        authority = Node(root, "authority-1.3", "authority", extra_env={"ATLAS_EMIT_PROTOCOL": "1.3"})
        follower = PreviousNode(root, "local-1.2", "local", source, python)
        for node in (authority, follower):
            stack.callback(node.close)
            node.start()
        old_info = follower.client.get("/api/v2/sync/info", headers=follower.headers()).json()
        info = authority.client.get("/api/v2/sync/info", headers=authority.headers()).json()
        assert old_info.get("protocol") == "1.2", old_info.get("protocol")
        assert (info["protocol"], info["emit_protocol"]) == (feed_schema.PROTOCOL, "1.3"), info
        assert "dates-v1" in info["capabilities"] and "dates-v1" not in old_info.get("capabilities", []), info
        checks.append(f"the follower runs a 1.2 release; the authority speaks {info['protocol']} and emits 1.3")

        title = "Skew Dated Game"
        cid = authority.publish(title)
        peer = follower.add_peer(authority)
        assert follower.sync(peer) is None
        before = follower.game(cid)
        assert before["title"] == title and "first_release" not in before, before
        assert _post_release(authority, title, RELEASE) == cid
        with authority.state.maker() as session:
            dated_seq = feed.head_seq(session)
            body = session.get(ChangeLog, dated_seq).body
        assert body["_requires"] == ["dates-v1"], body
        assert body["observation"]["payload"]["ext"]["release"] == RELEASE, body
        later = authority.publish("Skew After Dates")
        follower.sync(peer)
        status = follower.client.get("/api/v2/sync/status", headers=follower.headers(follower.admin_token)).json()
        up = status["upstreams"][authority.identity.node_id]
        assert up["state"] == "parked" and up["parked"]["first_seq"] == dated_seq, up
        assert up["parked"]["first_reason"] == "unsupported_capability" and up["parked"]["first_kind"] == "obs", up
        assert up["applied_head"] == dated_seq - 1 and up["verified_head"] == dated_seq + 1, up
        kept = follower.game(cid)
        assert kept == before, (kept, before)  # not stripped, not partial: the record stays as it was
        assert follower.client.get(f"/api/v2/games/{later}", headers=follower.headers()).status_code == 404
        checks.append(f"the 1.2 follower parks the dated observation at seq {dated_seq} (unsupported_capability, "
                      "token dates-v1) instead of stripping the dates, keeps the record exactly as it was, holds "
                      "back the later record and still verifies and retains the later entry")

        follower.upgrade()
        replay = follower.cli("sync", "replay")
        assert replay.returncode == 0, (replay.stdout, replay.stderr)
        got, want = follower.game(cid), authority.game(cid)
        assert got["first_release"] == want["first_release"] == {"date": "2027", "precision": "year"}, (got, want)
        assert got["release_year"] == 2027 and "release_date" not in got, got  # never a fake 1 January
        assert got["releases"] == want["releases"] and len(got["releases"]) == 3, got["releases"]
        assert follower.game(later)["title"] == "Skew After Dates"
        status = follower.cli("sync", "status", "--json")
        assert status.returncode == 0, (status.stdout, status.stderr)
        checks.append("after upgrading the follower in place (migration 0031 on its database), replay applies the "
                      "parked observation and the later record: first release, year and the three regional "
                      "entries equal the authority's, `release_date` stays absent, and `atlas sync status` exits 0")
    report["status"] = "passed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def _relations(node: Node, cid: str) -> dict[str, Any]:
    response = node.client.get(f"/api/v2/games/{cid}", headers=node.headers())
    assert response.status_code == 200, f"game on {node.name}: {response.status_code}"
    return response.json().get("relations", {})


def run_relations(previous_ref: str = DEFAULT_PREVIOUS, previous_dir: Path | None = None) -> dict[str, Any]:
    """skew-1.4: a real follower of an earlier release meets `relation` entries from a 1.4 authority (C6)."""
    report: dict[str, Any] = {"drill": "skew-1.4-relations", "started_at": datetime.now(UTC).isoformat(),
                              "checks": [], "transport": "isolated loopback HTTP, development mode, SQLite"}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="atlas-skew14-", ignore_cleanup_errors=True) as folder, ExitStack() as stack:
        root = Path(folder)
        source, python, commit = _prepare(root, previous_ref, previous_dir)
        report["previous"] = {"ref": previous_ref if previous_dir is None else str(previous_dir), "commit": commit}
        report["current"] = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                                      text=True).stdout.strip()}
        authority = Node(root, "authority-1.4", "authority", extra_env={"ATLAS_EMIT_PROTOCOL": "1.4"})
        follower = PreviousNode(root, "local-previous", "local", source, python)
        for node in (authority, follower):
            stack.callback(node.close)
            node.start()
        old_info = follower.client.get("/api/v2/sync/info", headers=follower.headers()).json()
        info = authority.client.get("/api/v2/sync/info", headers=authority.headers()).json()
        old_protocol = old_info.get("protocol", "1.0")
        report["previous"]["protocol"] = old_protocol
        assert old_protocol in ("1.0", "1.1", "1.2", "1.3"), old_protocol
        assert (info["protocol"], info["emit_protocol"]) == (feed_schema.PROTOCOL, "1.4"), info
        checks.append(f"the follower runs a {old_protocol} release; the authority speaks {info['protocol']} and "
                      "emits 1.4")

        game, dlc = authority.publish("Skew Base Game"), authority.publish("Skew Base Game Expansion")
        peer = follower.add_peer(authority)
        assert follower.sync(peer) is None
        assert follower.game(dlc)["title"] == "Skew Base Game Expansion"
        response = authority.client.put(f"/api/v2/games/{dlc}/relations",
                                        headers=authority.headers(authority.admin_token),
                                        json={"relations": [{"to": game, "type": "dlc_of"}]})
        assert response.status_code == 200 and response.json()["published"] is True, response.text
        with authority.state.maker() as session:
            relation_seq = feed.head_seq(session)
            body = session.get(ChangeLog, relation_seq).body
        assert body == {"op": "add", "from": dlc, "to": game, "type": "dlc_of", "source": "curator"}, body
        later = authority.publish("Skew After Relation")
        want = _relations(authority, dlc)
        assert [(p["id"], p["type"]) for p in want["parents"]] == [(game, "dlc_of")], want

        follower.sync(peer)
        if old_protocol == "1.0":
            assert follower.game(later)["title"] == "Skew After Relation"
            assert "relations" not in follower.game(dlc)
            checks.append(f"the 1.0 follower passes over the relation at seq {relation_seq} without recording it "
                          "(the documented 1.0 limitation) and applies the later record")
            follower.upgrade()
            rescan = follower.cli("sync", "replay", "--rescan")
            assert rescan.returncode == 3 and "recorded 1 parked" in rescan.stdout, (rescan.stdout, rescan.stderr)
            with sqlite3.connect(follower.db_path) as db:
                reason = db.execute("SELECT reason FROM sync_failure WHERE seq = ?", (relation_seq,)).fetchone()
            assert reason == ("dropped_by_previous_release",), reason
            replay = follower.cli("sync", "replay", "--historical")
            assert replay.returncode == 0, (replay.stdout, replay.stderr)
            checks.append("after upgrading it in place (migration 0032), `atlas sync replay --rescan` records the "
                          "relation as dropped_by_previous_release (exit 3) and `atlas sync replay --historical` "
                          "applies it")
        else:
            status = follower.client.get("/api/v2/sync/status", headers=follower.headers(follower.admin_token)).json()
            up = status["upstreams"][authority.identity.node_id]
            assert up["state"] == "parked" and up["parked"]["first_seq"] == relation_seq, up
            assert up["parked"]["first_reason"] == "unsupported_kind" and up["parked"]["first_kind"] == "relation", up
            assert up["applied_head"] == relation_seq - 1 and up["verified_head"] == relation_seq + 1, up
            assert follower.client.get(f"/api/v2/games/{later}", headers=follower.headers()).status_code == 404
            assert "relations" not in follower.game(dlc)
            checks.append(f"the {old_protocol} follower parks the relation at seq {relation_seq} (unsupported_kind), "
                          "holds back the later record and still verifies and retains the later entry")
            follower.upgrade()
            replay = follower.cli("sync", "replay")
            assert replay.returncode == 0, (replay.stdout, replay.stderr)
            checks.append("after upgrading it in place (migration 0032), a plain `atlas sync replay` applies the "
                          "parked relation and the later record in feed order")
        assert _relations(follower, dlc) == want, (_relations(follower, dlc), want)
        assert _relations(follower, game) == _relations(authority, game)
        assert follower.game(later)["title"] == "Skew After Relation"
        assert follower.sync(peer) is None
        status = follower.cli("sync", "status", "--json")
        assert status.returncode == 0, (status.stdout, status.stderr)
        checks.append("the upgraded follower's relations on both records equal the authority's and `atlas sync "
                      "status` exits 0")
    report["status"] = "passed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def main_cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--previous-ref", default=DEFAULT_PREVIOUS, help="git ref of the previous release")
    parser.add_argument("--previous-dir", type=Path,
                        help="an existing checkout of the previous release with its own .venv (skips git and uv)")
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    parser.add_argument("--scenario", choices=("1.0", "platinum", "dates", "relations"), default="1.0",
                        help="1.0: the previous 1.0 release (default); platinum: a 1.1 release meets platinum; "
                             "dates: a 1.2 release meets partial and regional release dates; relations: any "
                             "earlier release meets relations from a 1.4 authority")
    args = parser.parse_args()
    if args.scenario == "platinum":
        if args.previous_dir is None and args.previous_ref == DEFAULT_PREVIOUS:
            parser.error("--scenario platinum needs --previous-ref or --previous-dir of a 1.1 release")
        report = run_platinum(args.previous_ref, args.previous_dir)
    elif args.scenario == "dates":
        if args.previous_dir is None and args.previous_ref == DEFAULT_PREVIOUS:
            parser.error("--scenario dates needs --previous-ref or --previous-dir of a 1.2 release")
        report = run_dates(args.previous_ref, args.previous_dir)
    elif args.scenario == "relations":
        report = run_relations(args.previous_ref, args.previous_dir)
    else:
        report = run(args.previous_ref, args.previous_dir)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main_cli()
