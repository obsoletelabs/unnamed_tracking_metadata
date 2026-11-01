"""Exercise four real HTTP nodes, two authorities, a relay and an ordinary user."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from atlas import accounts, auth, main
from atlas.models import Peer
from atlas.settings import Settings

from .drill_support import postgres_databases

PASSWORD = "isolated-release-drill-password"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def csrf(client, path="/me"):
    response = client.get(path)
    assert response.status_code == 200, f"CSRF page {path}: {response.status_code}"
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


class Node:
    def __init__(self, root, name, role, database_url=None, *, password=PASSWORD, extra_env=None, python=None):
        self.root, self.name, self.role = root, name, role
        # `password` and `extra_env` let other harnesses (tools.federation_lab) generate per-run
        # credentials and tune a node; `python` runs the server from another interpreter, for
        # example a virtualenv holding an older release, to build a real mixed-version network
        # (tools.skew_drill, which also sets `cwd` so `python -m uvicorn` imports that release).
        self.password, self.extra_env, self.python = password, dict(extra_env or {}), python or sys.executable
        self.url = f"http://127.0.0.1:{free_port()}"
        self.database_url = database_url or f"sqlite:///{root}/{name}.db"
        self.admin_token, self.encryption_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        settings = Settings(_env_file=None, database_url=self.database_url, role=role, public_url=self.url,
                            email_verification="optional", initial_admin="drill-admin", media_cache_enabled=False,
                            secret_key=self.encryption_key, bootstrap_admin_token=self.admin_token, auto_migrate=True,
                            media_dir=str(root / f"{name}-media"))
        self.app = main.create_app(settings)
        self.state = self.app.state.atlas
        with self.state.maker() as session:
            accounts.register(session, settings, "drill-admin", self.password, "admin@example.test")
            accounts.register(session, settings, "drill-user", self.password, "user@example.test")
            accounts.register(session, settings, "drill-other", self.password, "other@example.test")
            _, self.reader = auth.create_token(session, "drill-reader", ["read"])
            _, self.follower = auth.create_token(session, "drill-follower", ["read", "sync"])
            _, self.scraper = auth.create_token(session, "drill-scraper", ["metrics"])
            session.commit()
        self.process, self.client, self.admin = None, None, None
        self.log = (root / f"{name}.log").open("ab")

    def start(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("ATLAS_")}
        env.update(ATLAS_DATABASE_URL=self.database_url, ATLAS_ROLE=self.role, ATLAS_PUBLIC_URL=self.url,
                   ATLAS_EMAIL_VERIFICATION="optional", ATLAS_INITIAL_ADMIN="drill-admin", ATLAS_AUTO_MIGRATE="true",
                   ATLAS_SECRET_KEY=self.encryption_key, ATLAS_BOOTSTRAP_ADMIN_TOKEN=self.admin_token,
                   ATLAS_MEDIA_DIR=str(self.root / f"{self.name}-media"), ATLAS_MEDIA_CACHE_ENABLED="false",
                   ATLAS_SYNC_INTERVAL_SECONDS="3600", ATLAS_METRICS_ENABLED="true", ATLAS_JSON_LOGS="true")
        env.update(self.extra_env)
        self.process = subprocess.Popen([self.python, "-m", "uvicorn", "atlas.main:app_factory", "--factory",
                                        "--host", "127.0.0.1", "--port", self.url.rsplit(":", 1)[1]],
                                       env=env, stdout=self.log, stderr=self.log, cwd=getattr(self, "cwd", None))
        self.client = httpx.Client(base_url=self.url, timeout=20, follow_redirects=False, trust_env=False)
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.name} did not start; inspect {self.root / (self.name + '.log')}")
            try:
                if self.client.get("/api/v2/ready").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"{self.name} did not become ready")
        self.admin = self.signin("drill-admin")

    def signin(self, username):
        client = httpx.Client(base_url=self.url, timeout=20, follow_redirects=False, trust_env=False)
        form = {"csrf": csrf(client, "/login"), "username": username, "password": self.password}
        response = client.post("/login", data=form)
        assert response.status_code == 303, f"sign-in on {self.name}"
        return client

    def stop(self):
        if self.admin:
            self.admin.close()
            self.admin = None
        if self.client:
            self.client.close()
            self.client = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process:
            self._wait_until_gone()

    def _wait_until_gone(self, timeout=10.0):
        """On Windows a virtualenv's python.exe is a launcher: the server is its child and can still hold the
        database (and the port) for a moment after `wait()` returns. Wait until nothing listens any more."""
        port, deadline = int(self.url.rsplit(":", 1)[1]), time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    pass
            except OSError:
                return
            time.sleep(0.05)
        raise RuntimeError(f"{self.name} still listens on port {port} after it was stopped")

    def close(self):
        self.stop()
        self.log.close()
        self.state.http.close()
        self.state.providers.client.close()
        self.state.maker.kw["bind"].dispose()

    @property
    def identity(self):
        return self.state.ctx.identity

    def headers(self, token=None):
        return {"Authorization": f"Bearer {token or self.reader}"}

    def publish(self, title):
        payload = {"connection": {"source_kind": "manual", "source": "manual", "source_scope": "release-drill"},
                   "observations": [{"external_id": title, "title": title, "facts": {"genres": ["Puzzle"]}}]}
        response = self.client.post("/api/v2/observations", headers=self.headers(self.admin_token), json=payload)
        assert response.status_code == 200, f"publish on {self.name}: {response.status_code}"
        return response.json()["results"][0]["atlas"]["canonical_id"]

    def add_peer(self, remote, authority=None):
        identity = (authority or remote).identity
        response = self.admin.post("/admin/peers", data={"csrf": csrf(self.admin), "name": (authority or remote).name,
                    "url": remote.url, "node_id": identity.node_id, "public_key": identity.public_b64,
                    "token": remote.follower})
        assert response.status_code == 303, f"add peer on {self.name}: {response.status_code}"
        with self.state.maker() as session:
            return session.scalars(select(Peer.id).where(Peer.node_id == identity.node_id)).one()

    def sync(self, peer_id):
        response = self.admin.post(f"/admin/peers/{peer_id}/sync", data={"csrf": csrf(self.admin)})
        assert response.status_code == 200, f"sync response on {self.name}: {response.status_code}"
        with self.state.maker() as session:
            peer = session.get(Peer, peer_id)
            return peer.last_error

    def game(self, cid):
        response = self.client.get(f"/api/v2/games/{cid}", headers=self.headers())
        assert response.status_code == 200, f"game read on {self.name}: {response.status_code}"
        return response.json()


def ordinary_workflows(node, ordinary, other, cid):
    created = ordinary.post("/me/lists", data={"csrf": csrf(ordinary), "name": "private-list-canary"})
    assert created.status_code == 303
    path = created.headers["location"].split("?")[0]
    assert ordinary.post(path + "/add", data={"csrf": csrf(ordinary), "game": cid,
                                              "note": "private-note-canary"}).status_code == 303
    assert "private-note-canary" in ordinary.get(path).text
    assert other.get(path).status_code == 404
    assert other.post(path + "/settings", data={"csrf": csrf(other), "name": "stolen"}).status_code == 404
    assert ordinary.post("/follow", data={"csrf": csrf(ordinary), "type": "game", "target": cid}).status_code == 303
    assert cid in ordinary.get("/me/following").text
    comment = ordinary.post(f"/game/{cid}/comments", data={"csrf": csrf(ordinary),
                                                        "body": "local-comment-canary <b>plain</b>"})
    assert comment.status_code == 303
    comment_id = comment.headers["location"].split("#c-")[1]
    assert "&lt;b&gt;plain&lt;/b&gt;" in ordinary.get(f"/game/{cid}").text
    assert other.post(f"/comments/{comment_id}/edit", data={"csrf": csrf(other), "body": "stolen"}).status_code == 404
    assert other.post(f"/comments/{comment_id}/moderate", data={"csrf": csrf(other), "action": "hide"}).status_code == 403
    report = other.post(f"/comments/{comment_id}/report", data={"csrf": csrf(other), "reason": "test report"})
    assert report.status_code == 303
    edited = ordinary.post(f"/comments/{comment_id}/edit", data={"csrf": csrf(ordinary), "body": "local-comment-canary"})
    assert edited.status_code == 303
    proposal = ordinary.post(f"/game/{cid}/edit", data={"csrf": csrf(ordinary), "title": "Authority Alpha Game",
                            "developer": "Community Drill Developer", "genres": "Puzzle", "note": "proposal-note-canary"})
    assert proposal.status_code == 303 and "m=proposed" in proposal.headers["location"]
    review = node.admin.get("/review")
    proposal_id = re.search(r'action="/review/([A-Za-z0-9]{26})"', review.text).group(1)
    assert other.get(f"/proposal/{proposal_id}").status_code == 404
    assert ordinary.post(f"/review/{proposal_id}", data={"csrf": csrf(ordinary), "decision": "approve"}).status_code == 403
    accepted = node.admin.post(f"/review/{proposal_id}", data={"csrf": csrf(node.admin), "decision": "approve"})
    assert accepted.status_code == 303
    assert node.game(cid)["developer"] == "Community Drill Developer"
    assert "was approved" in ordinary.get("/notifications").text
    assert ordinary.post("/notifications/read", data={"csrf": csrf(ordinary)}).status_code == 303
    exported = ordinary.get("/me/export.json").json()
    assert any(row["name"] == "private-list-canary" and row["items"][0]["note"] == "private-note-canary"
               for row in exported["lists"])
    assert exported["proposals"][0]["status"] == "approved"
    assert ordinary.post(f"/comments/{comment_id}/delete", data={"csrf": csrf(ordinary)}).status_code == 303
    assert ordinary.post("/unfollow", data={"csrf": csrf(ordinary), "type": "game", "target": cid}).status_code == 303
    assert ordinary.post(path + "/remove", data={"csrf": csrf(ordinary), "game": cid}).status_code == 303
    assert ordinary.post(path + "/delete", data={"csrf": csrf(ordinary)}).status_code == 303


def remove_tree(folder, timeout=10.0):
    """Remove the drill's folder once every node is closed. Windows refuses to delete a file another process
    still has open (the antivirus scanner, or a server process that is still exiting), so retry for a short
    while. Returns None, or the error when the folder is left behind: that is reported, not a failed drill."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            shutil.rmtree(folder)
            return None
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                return f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)


def run(postgres_admin_url=None):
    report = {"started_at": datetime.now(UTC).isoformat(), "checks": [],
              "transport": "isolated loopback HTTP, development mode; production TLS is not exercised"}
    folder = tempfile.mkdtemp(prefix="atlas-topology-")
    try:
        _run(folder, postgres_admin_url, report)
    finally:
        # Every node (server process, in-process engine, clients, log file) is closed by now.
        left = remove_tree(folder)
        if left:
            report["cleanup"] = f"temporary folder {folder} was left behind: {left}"
            print(f"warning: {report['cleanup']}", file=sys.stderr)
    report["status"] = "passed"
    return report


def _run(folder, postgres_admin_url, report):
    checks = report["checks"]
    with ExitStack() as stack:
        databases = stack.enter_context(postgres_databases(postgres_admin_url, 2))
        nodes = [Node(Path(folder), "authority-a", "authority", databases[0]),
                 Node(Path(folder), "authority-b", "authority", databases[1]),
                 Node(Path(folder), "mirror", "mirror"), Node(Path(folder), "local", "local")]
        for node in nodes:
            stack.callback(node.close)
            node.start()
        a, b, mirror, local = nodes
        report["nodes"] = [{"name": n.name, "role": n.role, "node_id": n.identity.node_id,
                             "database": "PostgreSQL" if n.database_url.startswith("postgresql") else "SQLite"}
                            for n in nodes]
        assert len({n.identity.node_id for n in nodes}) == 4
        checks.append("four real HTTP servers, distinct node identities and healthy readiness endpoints")
        aid, bid = a.publish("Authority Alpha Game"), b.publish("Authority Beta Game")
        pa, pb = mirror.add_peer(a), mirror.add_peer(b)
        assert mirror.sync(pa) is None and mirror.sync(pb) is None
        assert mirror.game(aid)["title"] == "Authority Alpha Game"
        assert mirror.game(bid)["title"] == "Authority Beta Game"
        checks.append("mirror combines two independently pinned authorities")
        probe = local.admin.post("/admin/peers/probe", data={"csrf": csrf(local.admin), "url": mirror.url,
                                                           "token": mirror.follower})
        assert "Authority feeds available" in probe.text
        assert a.identity.node_id in probe.text and b.identity.node_id in probe.text
        la, lb = local.add_peer(mirror, a), local.add_peer(mirror, b)
        assert local.sync(la) is None and local.sync(lb) is None
        assert local.game(aid)["title"] == mirror.game(aid)["title"]
        assert local.game(bid)["title"] == mirror.game(bid)["title"]
        version = local.game(aid)["version"]
        assert local.sync(la) is None and local.game(aid)["version"] == version
        checks.append("local follows both original authority feeds through a mirror; replay is idempotent")
        before = a.client.get("/api/v2/sync/info", headers=a.headers()).json()
        rotated = a.admin.post("/admin/keys/rotate", data={"csrf": csrf(a.admin), "password": PASSWORD,
                         "confirm": before["signing_key_id"], "followers_upgraded": "on"})
        assert rotated.status_code == 303, "administrator signing-key rotation"
        after = a.client.get("/api/v2/sync/info", headers=a.headers()).json()
        assert after["node_id"] == before["node_id"] and after["signing_key_id"] != before["signing_key_id"]
        rotated_game = a.publish("After Signing Key Rotation")
        assert mirror.sync(pa) is None and local.sync(la) is None
        assert local.game(rotated_game)["title"] == "After Signing Key Rotation"
        checks.append("signing-key rotation preserves node identity and continues through a relay without re-pinning")

        ordinary = a.signin("drill-user")
        stack.callback(ordinary.close)
        for path in ("/", "/search?q=Alpha", f"/game/{aid}", "/me", "/me/settings", "/me/export.json", "/new"):
            assert ordinary.get(path).status_code == 200, f"ordinary user {path}"
        for path in ("/admin", "/admin/schema", "/admin/keys", "/admin/tokens", "/admin/peers", "/admin/backup", "/review"):
            assert ordinary.get(path).status_code == 403, f"ordinary user must not access {path}"
        assert ordinary.post("/me/settings", data={"bio": "forged"}).status_code == 403
        assert ordinary.post("/me/settings", data={"csrf": csrf(ordinary), "bio": "private-bio-canary"}).status_code == 303
        exported = ordinary.get("/me/export.json").json()
        assert exported["account"]["username"] == "drill-user" and exported["account"]["bio"] == "private-bio-canary"
        created = ordinary.post("/me/tokens", data={"csrf": csrf(ordinary), "name": "personal drill"})
        assert created.status_code == 200, f"personal token creation: {created.status_code}"
        personal = re.search(r"<strong>(atl_[A-Za-z0-9_-]+)</strong>", created.text).group(1)
        personal_read = a.client.get(f"/api/v2/games/{aid}", headers=a.headers(personal))
        assert personal_read.status_code == 200, f"personal token read: {personal_read.status_code}"
        forged = a.client.post("/api/v2/tokens", headers=a.headers(personal),
                               json={"name": "forged", "scopes": ["admin"]})
        assert forged.status_code == 403
        checks.append("ordinary-user browse/profile/export/read-only tokens; CSRF and admin/reviewer boundaries hold")
        other = a.signin("drill-other")
        stack.callback(other.close)
        ordinary_workflows(a, ordinary, other, aid)
        assert mirror.sync(pa) is None and local.sync(la) is None
        assert local.game(aid)["developer"] == "Community Drill Developer"
        checks.append("ordinary-user private lists, follows, comments, reports, proposals, review and notifications work")
        assert b.client.get(f"/api/v2/games/{aid}", headers=b.headers(personal)).status_code == 401
        isolated = httpx.Client(base_url=b.url, follow_redirects=False, trust_env=False)
        stack.callback(isolated.close)
        isolated.cookies.set("atlas_session", ordinary.cookies.get("atlas_session"))
        assert isolated.get("/me").status_code == 303
        for source in (a, b):
            entries = source.client.get("/api/v2/sync/changes", headers=source.headers(source.follower)).text
            for private in ("private-bio-canary", "user@example.test", personal, "private-list-canary",
                            "private-note-canary", "local-comment-canary", "proposal-note-canary"):
                assert private not in entries
        checks.append("accounts, session cookies, personal tokens and private profile data do not federate")
        a.stop()
        assert mirror.sync(pa) is not None
        assert mirror.game(aid)["title"] == "Authority Alpha Game"
        new_b = b.publish("Beta During Alpha Outage")
        assert mirror.sync(pb) is None and local.sync(lb) is None
        assert local.game(new_b)["title"] == "Beta During Alpha Outage"
        assert local.sync(la) is None
        a.start()
        new_a = a.publish("Alpha After Reconnect")
        assert mirror.sync(pa) is None and local.sync(la) is None
        assert local.game(new_a)["title"] == "Alpha After Reconnect"
        checks.append("one authority can fail and restart while other-authority updates and cached relay reads continue")
        assert local.client.get("/metrics").status_code == 401
        assert local.client.get("/metrics", headers=local.headers()).status_code == 403
        metrics = local.client.get("/metrics", headers=local.headers(local.scraper))
        assert metrics.status_code == 200 and "atlas_sync_cursor" in metrics.text
        checks.append("live metrics require a dedicated scope and expose node synchronization state")
        deleted = ordinary.post("/me/delete", data={"csrf": csrf(ordinary), "password": PASSWORD, "confirm": "drill-user"})
        assert deleted.status_code == 303
        assert a.client.get(f"/api/v2/games/{aid}", headers=a.headers(personal)).status_code == 401
        checks.append("ordinary-user deletion revokes personal credentials without deleting canonical games")


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-admin-url", help="create two disposable databases on this test PostgreSQL server")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.postgres_admin_url)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main_cli()
