"""Test harness for owner groups: several real Atlas apps (own SQLite files, own synthetic secrets) wired together
by an in-process HTTP dispatcher that routes absolute URLs by host to each app's TestClient.

Every key is generated at runtime; passphrases and secrets here are synthetic test values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from atlas import group_crypto as gc
from atlas import group_enrol, groups, main
from atlas.api import group_routes

from .conftest import build_settings

ROOT_PW = "synthetic root passphrase for tests 7f3a"
REC_PW = ["synthetic recovery phrase one 9c1e", "synthetic recovery phrase two 4b7a",
          "synthetic recovery phrase three 2f30"]


class Dispatcher:
    """`httpx.Client`-shaped: routes `http://<host>/...` to the TestClient registered for that host."""

    def __init__(self) -> None:
        self.clients: dict[str, TestClient] = {}
        self.down: set[str] = set()
        self.log: list[tuple[str, str]] = []

    def _client(self, url: str) -> tuple[TestClient, str]:
        parts = urlsplit(url)
        if parts.hostname in self.down or parts.hostname not in self.clients:
            raise ConnectionError(f"{parts.hostname} unreachable")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.log.append((parts.hostname, parts.path))
        return self.clients[parts.hostname], path

    def post(self, url: str, json=None, content=None, headers=None, timeout=None):
        client, path = self._client(url)
        if content is not None:
            return client.post(path, content=content, headers=headers or {})
        return client.post(path, json=json)

    def get(self, url: str, params=None, timeout=None):
        client, path = self._client(url)
        return client.get(path, params=params or {})

    def close(self) -> None:
        pass


@dataclass
class Node:
    name: str
    app: object
    st: object
    net: Dispatcher

    @property
    def settings(self):
        return self.st.ctx.settings

    @property
    def identity(self):
        return self.st.ctx.identity

    @property
    def node_id(self) -> str:
        return self.identity.node_id

    def session(self):
        return self.st.maker()

    def group(self, session):
        return groups.load(session)

    def close(self) -> None:
        self.st.http.close()
        self.st.providers.client.close()
        self.st.maker.kw["bind"].dispose()


def make_node(tmp_path, net: Dispatcher, name: str, **kw) -> Node:
    settings = build_settings(database_url=f"sqlite:///{tmp_path}/{name}.db",
                              secret_key=f"synthetic-test-secret-{name}-0123456789abcdef",
                              media_dir=str(tmp_path / f"{name}-media"), media_cache_enabled=False,
                              public_url=f"http://{name}.test", app_name=f"Node {name.upper()}", **kw)
    app = main.create_app(settings)
    net.clients[f"{name}.test"] = TestClient(app, base_url=f"http://{name}.test")
    app.state.atlas.http = net
    return Node(name, app, app.state.atlas, net)


def fast_kdf(monkeypatch) -> None:
    monkeypatch.setattr(gc, "SCRYPT_N", 2 ** 10)  # the stored parameters are what unwrap uses
    monkeypatch.setattr(gc, "SCRYPT_MIN_N", 2 ** 10)


def create(node: Node, recovery: int = 1, **kw):
    with node.session() as s:
        created = groups.create_group(s, node.settings, node.identity, kw.pop("name", "Home group"), ROOT_PW,
                                      REC_PW[:recovery], threshold=1 if recovery == 1 else 2, **kw)
        s.commit()
        return created


def finish_creation(node: Node) -> None:
    with node.session() as s:
        groups.confirm_recovery(s, node.settings, groups.load(s), REC_PW[0])
        s.commit()


def invite(node: Node, scopes=None, delegate=None) -> str:
    with node.session() as s:
        _, code = group_enrol.create_invite(s, node.settings, groups.load(s), node.identity, scopes, delegate)
        s.commit()
        return code


def enrol(inviter: Node, joiner: Node, scopes=None, delegate=None, passphrase: str | None = ROOT_PW,
          name: str | None = None) -> str:
    """The full flow with both SAS confirmations. Returns the joiner's serial."""
    code = invite(inviter, scopes, delegate)
    with joiner.session() as s:
        row, sas_j, _ = group_enrol.start_join(s, joiner.settings, joiner.identity, inviter.settings.public_url, code,
                                               joiner.net, name=name)
        eid = row.id
        s.commit()
    with inviter.session() as s:
        from atlas.models import GroupEnrolment

        assert group_enrol.sas_of(inviter.settings, s.get(GroupEnrolment, eid)) == sas_j
        group_enrol.confirm_inviter(s, inviter.settings, groups.load(s), inviter.identity, eid, passphrase,
                                    http=inviter.net)
        s.commit()
    with joiner.session() as s:
        assert group_enrol.confirm_joiner(s, joiner.settings, joiner.identity, eid, joiner.net) == "active"
        s.commit()
        return groups.load(s).self_serial


def pull(node: Node) -> dict:
    with node.session() as s:
        out = group_routes.pull(s, node.settings, node.identity, groups.load(s), node.net)
        s.commit()
        return out


def members(node: Node) -> dict:
    with node.session() as s:
        group = groups.load(s)
        return groups.trust(s, group, own_node=node.node_id).members()


def later(days: float = 0, hours: float = 0, minutes: float = 0):
    return groups.utcnow() + timedelta(days=days, hours=hours, minutes=minutes)
