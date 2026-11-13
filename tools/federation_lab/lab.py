"""A disposable federation network: real HTTP nodes, isolated keys and users, database-level assertions.

Topology (built by `Lab.wire()`; any subset can be started):

    official (authority)   indie (authority, independent trust root)
          \\                  /
           mirror (relays both retained feeds)
              |            |
           local         local2        <- two plain nodes of the same owner

Every node is a real `uvicorn` subprocess with its own database, signing key, bootstrap token,
secret key and users, all generated for this run. Nothing is shared with a deployment. The nodes
talk over loopback HTTP only. A scenario drives them through the same admin forms and API routes
an operator would use, and then asserts on the *database* of each node, not on an HTTP status.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from atlas import feed, secrets_box, sync, wire
from atlas.ids import canonical_id, ulid
from atlas.models import (
    AuditEntry,
    Canonical,
    ChangeLog,
    Crosswalk,
    FeedIntroduced,
    NodeState,
    Observation,
    Peer,
    ReplicatedChange,
    SyncCursor,
    SyncFailure,
)
from atlas.signing import load_identity
from tools.drill_support import postgres_databases
from tools.multinode_drill import Node

ROLES = {"official": "authority", "indie": "authority", "mirror": "mirror", "local": "local", "local2": "local"}


class LabNode(Node):
    """A drill node plus the database-level views and fault injection the scenarios need."""

    # ---- reading the database ---------------------------------------------------------------------

    @property
    def node_id(self) -> str:
        return self.identity.node_id

    @property
    def root_key(self) -> str:
        """The pinned trust root. It never changes, even when the signing key rotates."""
        return self.identity.trust_public_b64

    def head(self) -> int:
        """Last sequence number of this node's own feed."""
        with self.state.maker() as session:
            return feed.head_seq(session)

    def cursor(self, origin: LabNode | str) -> int:
        with self.state.maker() as session:
            row = session.get(SyncCursor, _id(origin))
            return row.last_seq if row else 0

    def retained(self, origin: LabNode | str) -> dict[int, tuple[str, str, str]]:
        """Retained signed entries of an upstream feed: seq -> (kind, body hash, signature)."""
        with self.state.maker() as session:
            rows = session.scalars(select(ReplicatedChange).where(ReplicatedChange.node_id == _id(origin)))
            return {r.seq: (r.kind, r.body_hash, r.signature) for r in rows}

    def own_feed(self) -> dict[int, tuple[str, str, str]]:
        with self.state.maker() as session:
            rows = session.scalars(select(ChangeLog))
            return {r.seq: (r.kind, r.body_hash, r.signature) for r in rows}

    def open_failures(self) -> list[tuple[str, int, str]]:
        with self.state.maker() as session:
            rows = session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None)))
            return sorted((r.node_id, r.seq, r.kind) for r in rows)

    def observations(self, origin: LabNode | None = None) -> dict[str, tuple]:
        """Observation id -> the fields that must converge. With `origin`, only rows that arrived from
        that authority; on the authority itself, only the rows it publishes to its feed."""
        with self.state.maker() as session:
            query = select(Observation)
            if origin is None:
                query = query.where(Observation.origin_node.is_(None), Observation.redistributable.is_(True))
            else:
                query = query.where(Observation.origin_node == origin.node_id)
            return {o.id: (o.canonical_id, o.source_kind, o.source, o.source_scope, o.external_id,
                           _stable(o.payload), o.is_current) for o in session.scalars(query)}

    def canonicals(self, ids: set[str] | None = None) -> dict[str, tuple]:
        """Canonical id -> (merged_into, media type, merged view)."""
        with self.state.maker() as session:
            rows = session.scalars(select(Canonical))
            return {c.id: (c.merged_into, c.media_type, _stable(c.view)) for c in rows
                    if ids is None or c.id in ids}

    def digest(self) -> str:
        """One hash over everything federation can change on this node. Equal digests before and after
        an operation prove it changed nothing."""
        with self.state.maker() as session:
            parts = {
                "observations": sorted((o.id, o.canonical_id, o.is_current, o.origin_node, o.origin_seq,
                                        _stable(o.payload)) for o in session.scalars(select(Observation))),
                "canonicals": sorted((c.id, c.merged_into, _stable(c.view)) for c in session.scalars(select(Canonical))),
                "crosswalk": sorted((x.namespace, x.external_id, x.canonical_id)
                                    for x in session.scalars(select(Crosswalk))),
                "retained": sorted((r.node_id, r.seq, r.body_hash) for r in session.scalars(select(ReplicatedChange))),
                "cursors": sorted((c.upstream, c.last_seq) for c in session.scalars(select(SyncCursor))),
                "failures": sorted((f.node_id, f.seq, f.attempts, f.resolved_at is None)
                                   for f in session.scalars(select(SyncFailure))),
            }
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

    def sync_status(self, origin: LabNode | str) -> dict[str, Any]:
        """The follower's named heads for one upstream feed (observed, verified, applied, parked...)."""
        with self.state.maker() as session:
            return sync.upstream_status(session, self.state.ctx, _id(origin))

    def skip(self, origin: LabNode | str, seq: int, reason: str) -> None:
        """What `atlas sync skip` does, against this node's database."""
        with self.state.maker() as session:
            sync.skip(session, self.state.ctx, _id(origin), seq, reason)
            session.commit()

    def audit(self, action: str) -> list[str]:
        with self.state.maker() as session:
            return [a.target for a in session.scalars(select(AuditEntry).where(AuditEntry.action == action))]

    def peer_error(self, peer_id: str) -> str | None:
        with self.state.maker() as session:
            return session.get(Peer, peer_id).last_error

    # ---- writing --------------------------------------------------------------------------------

    def post(self, title: str, *, external_id: str | None = None, **facts: Any) -> str:
        """Publish (or update) a synthetic game through the observation API; returns its canonical id."""
        payload = {"connection": {"source_kind": "manual", "source": "manual", "source_scope": "federation-lab"},
                   "observations": [{"external_id": external_id or title, "title": title,
                                     "facts": {"genres": ["Puzzle"], **facts}}]}
        response = self.client.post("/api/v2/observations", headers=self.headers(self.admin_token), json=payload)
        assert response.status_code == 200, f"publish on {self.name}: {response.status_code} {response.text[:200]}"
        return response.json()["results"][0]["atlas"]["canonical_id"]

    def emit(self, kind: str, body: dict[str, Any], *, v0: bool = False) -> int:
        """Append an entry to this authority's feed, signed with its current key.

        This is how the lab plays "a newer release": it can sign feed entries of a kind or shape the
        code under test does not know. The node keeps running; it is a legitimate signed entry as far
        as every follower can tell. `v0=True` plays a v0 authority (C16 Z1): the records the entry names
        are marked `legacy_unintroduced`, as a v0 release did, so `atlas feed introduce --missing` finds them."""
        with self.state.maker() as session:
            seq = feed.append_unchecked(session, load_identity(session), kind, body)
            if v0:
                for cid in feed.named(kind, body):
                    if session.get(FeedIntroduced, cid) is None and session.get(Canonical, cid) is not None:
                        feed.mark(session, cid, None, feed.VIA_LEGACY)
            session.commit()
        return seq

    def tokens(self) -> dict[str, str]:
        response = self.client.get("/api/v2/tokens", headers=self.headers(self.admin_token))
        assert response.status_code == 200
        return {t["name"]: t["id"] for t in response.json()["results"]}

    def revoke_token(self, name: str) -> None:
        response = self.client.delete(f"/api/v2/tokens/{self.tokens()[name]}", headers=self.headers(self.admin_token))
        assert response.status_code == 204

    # ---- peers ----------------------------------------------------------------------------------

    def follow(self, remote: LabNode, authority: LabNode | None = None, *, token: str | None = None,
               public_key: str | None = None, node_id: str | None = None) -> str:
        """Register `remote` as an upstream, pinned to `authority`'s feed (default: remote's own).
        The keyword overrides let a scenario pin the wrong identity on purpose."""
        pinned = authority or remote
        # Stored as a peer added before A19 (`follow` NULL): its pulls infer own or relayed exactly as the scenarios
        # were written for. The C13 choose-feed form (probe, `follow`, acknowledgements) has its own scenario
        # (`test_c13_live_regression`); it cannot pin a wrong identity on purpose, which these scenarios need.
        with self.state.maker() as session:
            row = Peer(name=f"{pinned.name}-via-{remote.name}", url=remote.url, node_id=node_id or pinned.node_id,
                       public_key=public_key or pinned.root_key,
                       token_enc=secrets_box.encrypt(self.state.ctx.settings, token or remote.follower))
            session.add(row)
            session.commit()
            return row.id

    def set_peer_token(self, peer_id: str, token: str) -> None:
        with self.state.maker() as session:
            session.get(Peer, peer_id).token_enc = secrets_box.encrypt(self.state.ctx.settings, token)
            session.commit()

    def set_cursor(self, origin: LabNode | str, seq: int) -> None:
        with self.state.maker() as session:
            row = session.get(SyncCursor, _id(origin))
            if row is None:
                session.add(SyncCursor(upstream=_id(origin), last_seq=seq))
            else:
                row.last_seq = seq
            session.commit()

    def forget_retained(self, origin: LabNode | str) -> None:
        """Drop the retained copy of an upstream's feed, as after restoring an older backup."""
        with self.state.maker() as session:
            for row in session.scalars(select(ReplicatedChange).where(ReplicatedChange.node_id == _id(origin))):
                session.delete(row)
            session.commit()

    def truncate_feed(self, keep: int) -> None:
        """Rewind this authority's own feed to `keep` entries, as after restoring an older backup.
        Stop the node first."""
        with self.state.maker() as session:
            for row in session.scalars(select(ChangeLog).where(ChangeLog.seq > keep)):
                session.delete(row)
            session.get(NodeState, "feed_seq").value = str(keep)
            session.commit()

    # ---- in-process pulls through a hostile or faulty network ---------------------------------------

    def pull(self, upstream: LabNode, authority: LabNode | None = None,
             tamper: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
             token: str | None = None, pinned_key: str | None = None) -> int:
        """Run this node's real pull code against `upstream`, optionally rewriting the JSON the upstream
        sends. `tamper(path, body)` returns the body to deliver. `authority` and `pinned_key` choose what this
        node trusts (default: `upstream` and its real key). Raises whatever the pull raises."""
        pinned = authority or upstream
        real = httpx.Client(trust_env=False, timeout=30)

        def handler(request: httpx.Request) -> httpx.Response:
            # the follower's `Atlas-Wire` goes along (C16 Z1): a wire-1 upstream refuses a peer call without it
            forwarded = {"Authorization": request.headers.get("authorization", "")}
            if request.headers.get(wire.HEADER):
                forwarded[wire.HEADER] = request.headers[wire.HEADER]
            answer = real.get(str(request.url), headers=forwarded)
            if tamper is None or answer.status_code != 200:
                return httpx.Response(answer.status_code, content=answer.content)
            return httpx.Response(200, json=tamper(request.url.path, answer.json()))

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        try:
            return sync.pull_all(self.state.maker, self.state.ctx, client, upstream.url, token or upstream.follower,
                                 trusted={pinned.node_id: pinned_key or pinned.root_key})
        finally:
            client.close()
            real.close()


def _id(origin: LabNode | str) -> str:
    return origin if isinstance(origin, str) else origin.node_id


def _stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def observation_entry(title: str, *, extra_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """The body of an "obs" feed entry for a new synthetic game, in the format the current release
    writes. `extra_payload` adds fields to the observation payload, as a newer release would."""
    external_id = "lab-" + ulid().lower()
    return {
        "canonical_id": canonical_id(), "media_type": "game",
        "observation": {
            "id": ulid(), "source_kind": "manual", "source": "manual", "source_scope": "federation-lab",
            "external_id": external_id, "payload": {"title": title, **(extra_payload or {})},
            "source_updated_at": None, "observed_at": "2026-01-01T00:00:00+00:00",
        },
    }


def disposition(follower: LabNode, origin: LabNode, seq: int, observation_id: str | None = None) -> str:
    """What became of feed entry `seq` of `origin` on `follower`. One of:

    * "applied"   the observation it carried is stored (only knowable for observation entries)
    * "blocked"   the cursor stayed before it (a parked entry), so it will be applied after an upgrade
    * "recorded"  an open sync failure names it (parked or quarantined), so an operator can see it
    * "dropped"   the cursor moved past it and nothing records that it was not applied: silent loss"""
    if observation_id is not None:
        with follower.state.maker() as session:
            if session.get(Observation, observation_id) is not None:
                return "applied"
    if follower.cursor(origin) < seq:
        return "blocked"
    if (origin.node_id, seq) in {(n, s) for n, s, _ in follower.open_failures()}:
        return "recorded"
    return "dropped"


@dataclass
class Lab:
    """Owns the temporary folder, the generated credentials and the nodes of one lab run."""

    postgres_admin_url: str | None = None
    keep_logs: Path | None = None
    pythons: dict[str, str] = field(default_factory=dict)  # node name -> interpreter of another release
    env: dict[str, dict[str, str]] = field(default_factory=dict)  # node name -> extra ATLAS_* settings
    password: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    nodes: dict[str, LabNode] = field(default_factory=dict)
    peers: dict[tuple[str, str], str] = field(default_factory=dict)
    _stack: ExitStack = field(default_factory=ExitStack)
    _folder: Path | None = None
    _databases: list[str | None] = field(default_factory=lambda: [None, None])

    def __enter__(self) -> Lab:
        self._stack.__enter__()
        folder = self._stack.enter_context(tempfile.TemporaryDirectory(prefix="atlas-lab-", ignore_cleanup_errors=True))
        self._folder = Path(folder)
        self._databases = self._stack.enter_context(postgres_databases(self.postgres_admin_url, 2))
        return self

    def __exit__(self, *exc) -> None:
        if self.keep_logs is not None and self._folder is not None:
            self.keep_logs.mkdir(parents=True, exist_ok=True)
            for log in self._folder.glob("*.log"):
                (self.keep_logs / log.name).write_bytes(log.read_bytes())
        self._stack.__exit__(*exc)

    def start(self, *names: str) -> list[LabNode]:
        """Create and start the named nodes (default: all five). Already running nodes are reused."""
        started = []
        for name in names or tuple(ROLES):
            if name not in self.nodes:
                database = {"official": self._databases[0], "indie": self._databases[1]}.get(name)
                python = self.pythons.get(name) or os.environ.get(f"ATLAS_LAB_PYTHON_{name.upper()}")
                node = LabNode(self._folder, name, ROLES[name], database, password=self.password, python=python,
                               extra_env=self.env.get(name))
                self._stack.callback(node.close)
                self.nodes[name] = node
                node.start()
            started.append(self.nodes[name])
        return started

    def wire(self) -> dict[tuple[str, str], str]:
        """Pin the topology: the mirror follows both authorities; local and local2 follow both
        authorities' feeds through the mirror. Peer ids are kept in `self.peers[(follower, authority)]`."""
        official, indie, mirror, local, local2 = self.start("official", "indie", "mirror", "local", "local2")
        for authority in (official, indie):
            self.peers[("mirror", authority.name)] = mirror.follow(authority)
            for follower in (local, local2):
                self.peers[(follower.name, authority.name)] = follower.follow(mirror, authority)
        return self.peers

    def sync(self, follower: str, authority: str) -> str | None:
        """Run the follower's admin "sync now" for one pinned authority; returns the recorded error."""
        return self.nodes[follower].sync(self.peers[(follower, authority)])

    def sync_all(self) -> None:
        """One full propagation round, authorities to mirror to both plain nodes. Fails on any error."""
        for authority in ("official", "indie"):
            assert self.sync("mirror", authority) is None, self.nodes["mirror"].peer_error(self.peers[("mirror", authority)])
            for follower in ("local", "local2"):
                error = self.sync(follower, authority)
                assert error is None, f"{follower} <- {authority}: {error}"

    # ---- convergence ------------------------------------------------------------------------------

    def assert_converged(self, *, followers: tuple[str, ...] = ("mirror", "local", "local2")) -> None:
        """Every follower holds exactly each authority's published observations, the same canonical
        views, the same signed entries and a cursor at the authority's head."""
        for authority in (self.nodes["official"], self.nodes["indie"]):
            published = authority.observations()
            canonicals = authority.canonicals({v[0] for v in published.values()})
            for name in followers:
                follower = self.nodes[name]
                assert follower.observations(authority) == published, f"{name}: observations diverge from {authority.name}"
                assert follower.canonicals(set(canonicals)) == canonicals, f"{name}: canonical views diverge"
                assert follower.cursor(authority) == authority.head(), f"{name}: cursor is not at {authority.name}'s head"
                assert follower.retained(authority) == authority.own_feed(), f"{name}: retained signed entries differ"
        for name in followers:
            assert not self.nodes[name].open_failures(), f"{name} has open sync failures"


__all__ = ["LabNode", "Lab", "ROLES", "disposition", "observation_entry"]
