"""Federation compatibility (CONTRACTS-v1 C1, atlas#33): negotiation, parking, quarantine, heads, replay.

Every scenario uses real nodes (the FastAPI app on SQLite files) talking through the real routes, and
asserts on the follower's database: retained signed entries, the applied cursor, `sync_failure` rows,
observations and audit entries, not only on return values. "A newer release" is played by signing
entries with the authority's real key that this release does not understand (`feed.append_unchecked`).
"""

from __future__ import annotations

import json
import random

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import auth, cli, feed, feed_schema, main, service, sync
from atlas.ids import canonical_id, ulid
from atlas.models import (
    Achievement,
    AuditEntry,
    FollowerSeen,
    NodeState,
    Observation,
    Peer,
    ReplicatedChange,
    SyncCursor,
    SyncFailure,
)

from .conftest import batch, build_settings

# ---- a small two-node network ---------------------------------------------------------------------------


class Node:
    def __init__(self, tmp_path, name, role, **settings):
        self.app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/{name}.db", role=role,
                                                  media_dir=str(tmp_path / name), media_cache_enabled=False,
                                                  **settings))
        self.st = self.app.state.atlas
        self.client = TestClient(self.app)
        with self.st.maker() as session:
            self.token_row, self.token = auth.create_token(session, f"{name}-follower", ["read", "sync"])
            session.commit()

    @property
    def node_id(self):
        return self.st.ctx.identity.node_id

    def publish(self, title):
        with self.st.maker() as session:
            [result] = service.ingest(session, self.st.ctx, batch("manual", [{"external_id": title, "title": title}],
                                                                   kind="manual"), curator=True)
            session.commit()
        return result.canonical_id

    def emit(self, kind, body):
        """Sign an entry this release may not understand, as a newer release would."""
        with self.st.maker() as session:
            seq = feed.append_unchecked(session, self.st.ctx.identity, kind, body)
            session.commit()
        return seq

    def head(self):
        with self.st.maker() as session:
            return feed.head_seq(session)

    def heads(self, origin):
        with self.st.maker() as session:
            return sync.upstream_status(session, self.st.ctx, origin.node_id)

    def failures(self, origin=None):
        with self.st.maker() as session:
            rows = session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None))
                                   .order_by(SyncFailure.seq))
            return [(f.seq, f.kind, f.disposition, f.reason) for f in rows
                    if origin is None or f.node_id == origin.node_id]

    def retained(self, origin):
        with self.st.maker() as session:
            return {r.seq: (r.kind, r.body_hash, r.signature) for r in
                    session.scalars(select(ReplicatedChange).where(ReplicatedChange.node_id == origin.node_id))}

    def own_feed(self):
        from atlas.models import ChangeLog
        with self.st.maker() as session:
            return {r.seq: (r.kind, r.body_hash, r.signature) for r in session.scalars(select(ChangeLog))}

    def observation_ids(self, origin):
        with self.st.maker() as session:
            return set(session.scalars(select(Observation.id).where(Observation.origin_node == origin.node_id)))

    def titles(self):
        with self.st.maker() as session:
            from atlas.models import Canonical
            return {c.view.get("title") for c in session.scalars(select(Canonical)) if c.merged_into is None}


class Wire:
    """httpx transport into a node's TestClient; `info`/`changes` rewrite answers, `seen` logs paths."""

    def __init__(self, upstream: Node, info=None, changes=None, fail=None):
        self.upstream, self.info, self.changes, self.fail, self.seen = upstream, info, changes, fail, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.url.path, request.headers.get("atlas-protocol")))
        if self.fail and self.fail(request):
            raise httpx.ConnectError("simulated network failure")
        response = self.upstream.client.request(request.method, request.url.raw_path.decode(),
                                                headers=dict(request.headers))
        body = response.json()
        if request.url.path.endswith("/sync/info") and self.info:
            body = self.info(body)
        if request.url.path.endswith("/sync/changes") and self.changes:
            body = self.changes(body)
        return httpx.Response(response.status_code, json=body)


def pull(follower: Node, upstream: Node, authority: Node | None = None, **wire):
    pinned = authority or upstream
    w = Wire(upstream, **wire)
    with httpx.Client(transport=httpx.MockTransport(w)) as http:
        n = sync.pull_all(follower.st.maker, follower.st.ctx, http, "http://upstream", upstream.token,
                          trusted={pinned.node_id: pinned.st.ctx.identity.trust_public_b64})
    return n, w


@pytest.fixture
def net(tmp_path):
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    local = Node(tmp_path, "local", "local")
    return authority, mirror, local


FUTURE = {"canonical_id": "atlas:" + "0" * 26, "type": "dlc_of", "target": "atlas:" + "1" * 26}


def observation(title, **payload):
    return {"canonical_id": canonical_id(), "media_type": "game",
            "observation": {"id": ulid(), "source_kind": "manual", "source": "manual", "source_scope": "compat",
                            "external_id": "compat-" + ulid().lower(), "payload": {"title": title, **payload},
                            "source_updated_at": None, "observed_at": "2026-01-01T00:00:00+00:00"}}


def around(authority, kind, body):
    """An ordinary entry, a newer-release entry, then another ordinary entry."""
    authority.publish("Before")
    seq = authority.emit(kind, body)
    authority.publish("After")
    return seq


# ---- classification (unit) ------------------------------------------------------------------------------

def test_registry_replaces_the_duplicated_kind_lists():
    from atlas import promotion

    assert promotion.KINDS == feed_schema.known_kinds()
    assert set(feed_schema.kinds_advertised()) == {"obs", "artwork", "achievements", "custom", "schema", "merge",
                                                   "detach", "lock", "unlock", "key_rotation", "relation"}
    assert feed_schema.OBS_PAYLOAD == set(sync._OBS_FIELDS)
    assert "x_future" not in feed_schema.FEED_KINDS and feed_schema.RESERVED_KINDS == {}
    assert feed_schema.FEED_KINDS["relation"].since == "1.4"  # reserved in 1.1-1.3, shipped by A15 (C6.3)


@pytest.mark.parametrize(("kind", "body", "action", "reason"), [
    ("x_future", FUTURE, "parked", "unsupported_kind"),
    ("obs", {**observation("x"), "_v": 2}, "parked", "unsupported_version"),
    ("obs", {**observation("x"), "_requires": ["x"]}, "parked", "unsupported_capability"),
    ("obs", {**observation("x"), "_requires": ["ext-v1"]}, "apply", ""),
    ("obs", observation("x", release_date_precision="month"), "parked", "unsupported_field"),
    ("obs", {**observation("x"), "surprise": 1}, "parked", "unsupported_field"),
    ("obs", {**observation("x"), "_future_envelope": 1}, "parked", "unsupported_field"),
    ("obs", observation("x", facts={"new_fact": 1}), "parked", "unsupported_field"),
    # An `ext` member this release does not know is optional unless the entry requires it (C1.4). `release`
    # is known since 1.3 (C5.2), so the examples use a member no release defines.
    ("obs", observation("x", ext={"x_future": {"precision": "month"}}), "apply", ""),
    ("obs", {**observation("x", ext={"x_future": {}}), "_requires": ["x-future-v1"]}, "parked",
     "unsupported_capability"),
    ("obs", {**observation("x", ext={"release": {"first": {"date": "2011", "precision": "year"}}}),
             "_requires": ["dates-v1"]}, "apply", ""),
    ("obs", observation("x", ext={"release": {"precision": "month"}}), "parked", "unsupported_field"),
    # platinum is known since 1.2 (C7.1); an unknown tier still parks (test_c covers a 1.1 follower)
    ("achievements", {"canonical_id": "atlas:x", "source": "curator", "items": [{"name": "A", "tier": "platinum"}]},
     "apply", ""),
    ("achievements", {"canonical_id": "atlas:x", "source": "curator", "items": [{"name": "A", "tier": "platinum"}],
                      "_requires": ["tier-platinum"]}, "apply", ""),
    ("achievements", {"canonical_id": "atlas:x", "source": "curator", "items": [{"name": "A", "tier": "diamond"}]},
     "parked", "unsupported_value"),
    ("achievements", {"canonical_id": "atlas:x", "source": "curator", "items": [{"name": "A", "key": "ak1:abc"}]},
     "apply", ""),
    ("artwork", {"canonical_id": "atlas:x", "source": "curator", "items": [{"kind": "hologram", "url": "https://a"}]},
     "parked", "unsupported_value"),
    ("schema", {"types": [], "fields": [{"media_type": "game", "key": "k", "label": "K", "kind": "rating"}]},
     "parked", "unsupported_value"),
    ("schema", {"types": [{"key": "game", "label": "Game", "icon": "x"}], "fields": []}, "parked",
     "unsupported_field"),
    ("lock", {"canonical_id": "atlas:x", "field": "mood", "value": "x"}, "parked", "unsupported_value"),
    # Envelope and vocabulary shapes this release does not know fail closed (park), never skip past.
    ("obs", {**observation("x"), "_v": "2"}, "parked", "unsupported_version"),
    ("obs", {**observation("x"), "_v": 1.5}, "parked", "unsupported_version"),
    ("obs", {**observation("x"), "_requires": "x"}, "parked", "unsupported_capability"),
    ("obs", observation("x", ext=["release"]), "parked", "unsupported_field"),
    ("artwork", {"canonical_id": "atlas:x", "source": "curator", "items": [{"kind": {"v": 2}, "url": "https://a"}]},
     "parked", "unsupported_value"),
    ("obs", "not an object", "quarantined", "invalid"),
    ("merge", {"src": "a", "dst": "b"}, "apply", ""),
])
def test_classification(kind, body, action, reason):
    outcome = feed_schema.classify(kind, body)
    assert (outcome.action, outcome.reason) == (action, reason), outcome


def test_ignorable_extension_is_removed_before_apply_but_not_from_the_signed_body():
    body = observation("Ext", ext={"x_future": {"precision": "month"}})
    original = json.dumps(body, sort_keys=True)
    outcome = feed_schema.classify("obs", body)
    assert outcome.action == "apply" and "ext" not in outcome.body["observation"]["payload"]
    assert json.dumps(body, sort_keys=True) == original


def test_protocol_parsing_is_strict():
    assert feed_schema.parse_protocol("1.10") == (1, 10)
    for bad in ("1", "1.x", " 1.1", "1.1.1", None, 11):
        with pytest.raises(ValueError):
            feed_schema.parse_protocol(bad)
    assert feed_schema.protocol_choices("1.0", "1.1") == ["1.0", "1.1"]


# ---- emission gate (H13) --------------------------------------------------------------------------------

def test_emission_gate_fails_closed_and_keeps_version_one_bodies_unchanged(net):
    authority, _, _ = net
    with authority.st.maker() as session:
        with pytest.raises(feed_schema.EmitGateError, match="1.4"):  # a kind newer than the emit protocol
            feed.append(session, authority.st.ctx.identity, "relation",
                        {"op": "add", "from": "atlas:a", "to": "atlas:b", "type": "dlc_of", "source": "curator"},
                        protocol="1.3")
        with pytest.raises(ValueError, match="unregistered"):
            feed.append(session, authority.st.ctx.identity, "mystery", {}, protocol="1.1")
        with pytest.raises(feed_schema.EmitGateError, match="Raise federation emit protocol to 1.2 first"):
            feed.append(session, authority.st.ctx.identity, "achievements",
                        {"canonical_id": "atlas:x", "source": "curator", "items": [{"name": "P", "tier": "platinum"}]},
                        protocol="1.1")
        with pytest.raises(feed_schema.EmitGateError, match="tier-platinum"):
            feed.append(session, authority.st.ctx.identity, "obs", {**observation("x"), "_requires": ["tier-platinum"]},
                        protocol="1.1")
        dated = observation("Dated", ext={"release": {"precision": "year", "value": "2026"}})
        seq = feed.append(session, authority.st.ctx.identity, "obs", dated, protocol="1.1")
        plain = observation("Plain")
        plain_seq = feed.emit(session, authority.st.ctx, "obs", plain)
        session.commit()
    with authority.st.maker() as session:
        from atlas.models import ChangeLog
        from atlas.signing import body_hash
        signed = session.get(ChangeLog, seq).body
        assert "ext" not in signed["observation"]["payload"]  # stored locally, omitted from the signed feed
        assert dated["observation"]["payload"]["ext"]  # the caller's copy is untouched
        row = session.get(ChangeLog, plain_seq)
        assert row.body == plain and row.body_hash == body_hash(plain)  # no envelope added to v1 bodies
    assert feed_schema.emit_protocol(authority.st.ctx.settings) == "1.0"  # default: min_follower_protocol


def test_platinum_stays_rejected_by_the_curator_api_while_emit_protocol_is_below_1_2(net):
    authority, _, _ = net
    cid = authority.publish("Trophies")
    r = authority.client.put(f"/api/v2/games/{cid}/achievements",
                             headers={"Authorization": f"Bearer {_admin(authority)}"},
                             json={"source": "curator", "items": [{"name": "All", "tier": "platinum"}]})
    assert r.status_code == 409  # valid request; this node's emit protocol is what refuses it (C1.6)
    assert r.json()["error"]["code"] == "emit_protocol"
    assert "Raise federation emit protocol to 1.2 first" in r.json()["error"]["message"]
    with authority.st.maker() as session:
        assert session.scalars(select(Achievement)).first() is None
        assert feed.head_seq(session) == 1  # only the observation; nothing signed for platinum


def _admin(node):
    with node.st.maker() as session:
        _, secret = auth.create_token(session, "admin", ["admin"])
        session.commit()
    return secret


# ---- /sync/info and follower telemetry ------------------------------------------------------------------

def test_sync_info_advertises_versions_and_records_follower_protocol(net):
    authority, mirror, _ = net
    authority.publish("Hello")
    info = authority.client.get("/api/v2/sync/info", headers={"Authorization": f"Bearer {authority.token}"}).json()
    assert info["contract"] == 1  # kept for pre-1.1 readers
    assert (info["protocol"], info["min_follower_protocol"], info["emit_protocol"]) == ("1.4", "1.0", "1.0")
    assert info["observation_schema"] == 1 and info["capabilities"] == ["ext-v1", "dates-v1"]
    assert info["kinds"]["achievements"] == {"emit": 1, "apply": 1}
    with authority.st.maker() as session:
        assert session.get(FollowerSeen, authority.token_row.id) is None  # a plain reader is not a follower

    _, wire = pull(mirror, authority)
    assert {protocol for _, protocol in wire.seen} == {"1.4"}
    with authority.st.maker() as session:
        assert session.get(FollowerSeen, authority.token_row.id).protocol == "1.4"
    # A pre-1.1 follower sends no header: recorded as 1.0 on /sync/changes.
    authority.client.get("/api/v2/sync/changes", headers={"Authorization": f"Bearer {authority.token}"})
    with authority.st.maker() as session:
        assert session.get(FollowerSeen, authority.token_row.id).protocol == "1.0"
    # The relay advertises what it saw the relayed authority speak.
    relay = mirror.client.get("/api/v2/sync/info", headers={"Authorization": f"Bearer {mirror.token}"}).json()
    [relayed] = relay["feeds"]
    assert relayed["node_id"] == authority.node_id and relayed["protocol"] == "1.4"


# ---- C1.11 (a)-(f): parking and quarantine --------------------------------------------------------------

def test_a_unknown_kind_parks_blocks_applied_and_later_entries_are_still_retained(net):
    authority, mirror, _ = net
    seq = around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    heads = mirror.heads(authority)
    assert heads["applied_head"] == seq - 1
    assert heads["verified_head"] == heads["downloaded_head"] == heads["observed_head"] == authority.head() == seq + 1
    assert heads["parked"]["first_seq"] == seq and heads["parked"]["first_reason"] == "unsupported_kind"
    assert heads["parked"]["first_kind"] == "x_future" and heads["parked"]["count"] == 2
    assert heads["state"] == "parked" and heads["in_sync"] is False
    assert mirror.retained(authority) == authority.own_feed()  # verified and retained byte-for-byte
    assert "After" not in mirror.titles()  # causally later: not applied
    assert mirror.failures(authority) == [(seq, "x_future", "parked", "unsupported_kind")]
    with mirror.st.maker() as session:
        assert session.get(SyncCursor, authority.node_id).last_seq == seq - 1
    # Pulling again changes nothing and never skips past it.
    pull(mirror, authority)
    assert mirror.heads(authority)["applied_head"] == seq - 1


def test_b_extra_observation_field_parks_instead_of_silent_drop(net):
    authority, mirror, _ = net
    entry = observation("Newer Field", release_date_precision="month")
    seq = around(authority, "obs", entry)
    pull(mirror, authority)
    assert mirror.failures(authority) == [(seq, "obs", "parked", "unsupported_field")]
    assert mirror.heads(authority)["applied_head"] == seq - 1
    with mirror.st.maker() as session:
        detail = session.get(SyncFailure, (authority.node_id, seq)).detail
    assert detail["unknown"] == ["observation.payload.release_date_precision"]


def test_c_platinum_tier_parks_the_whole_list_instead_of_dropping_it(net, release_1_1):
    """A 1.1 follower and a token-less platinum list: `unsupported_value` (the V4 regression). The 1.2
    authority's own entries also carry `_requires: [tier-platinum]`; see tests/test_vocab_platinum.py."""
    authority, mirror, _ = net
    release_1_1()
    cid = authority.publish("Trophy Game")
    seq = authority.emit("achievements", {"canonical_id": cid, "source": "curator",
                                          "items": [{"name": "Bronze one", "tier": "bronze"},
                                                    {"name": "All of them", "tier": "platinum"}]})
    pull(mirror, authority)
    assert mirror.failures(authority) == [(seq, "achievements", "parked", "unsupported_value")]
    with mirror.st.maker() as session:
        assert session.scalars(select(Achievement)).first() is None
        assert "items[1].tier=platinum" in session.get(SyncFailure, (authority.node_id, seq)).detail["unknown"]


def test_d_unknown_requires_token_parks(net):
    authority, mirror, _ = net
    seq = around(authority, "obs", {**observation("Needs X"), "_requires": ["x"]})
    pull(mirror, authority)
    assert mirror.failures(authority) == [(seq, "obs", "parked", "unsupported_capability")]


def test_e_reserved_optional_achievement_key_applies(net):
    authority, mirror, _ = net
    cid = authority.publish("Keyed")
    authority.emit("achievements", {"canonical_id": cid, "source": "curator",
                                    "items": [{"name": "Keyed One", "key": "ak1:" + "a" * 26}]})
    pull(mirror, authority)
    assert mirror.failures() == []
    heads = mirror.heads(authority)
    assert heads["applied_head"] == heads["verified_head"] == authority.head() and heads["in_sync"]
    with mirror.st.maker() as session:
        assert [a.name for a in session.scalars(select(Achievement))] == ["Keyed One"]


def test_f_quarantine_does_not_block(net):
    authority, mirror, _ = net
    authority.publish("First")
    bad = authority.emit("artwork", {"canonical_id": "atlas:" + "9" * 26, "source": "curator",
                                     "items": [{"kind": "key_art", "url": "https://example.test/a.png"}]})
    authority.publish("Last")
    pull(mirror, authority)
    assert mirror.failures(authority) == [(bad, "artwork", "quarantined", "apply_error")]
    heads = mirror.heads(authority)
    assert heads["applied_head"] == authority.head()  # quarantined entries count as passed (H6)
    assert heads["quarantined"] == {"count": 1, "oldest_seq": bad} and heads["state"] == "degraded"
    assert {"First", "Last"} <= mirror.titles()


def test_invalid_known_entry_is_quarantined_not_parked(net):
    authority, mirror, _ = net
    entry = observation("x" * 600)  # too long a title: a known field failing validation
    seq = around(authority, "obs", entry)
    pull(mirror, authority)
    assert mirror.failures(authority) == [(seq, "obs", "quarantined", "invalid")]
    assert mirror.heads(authority)["applied_head"] == authority.head()


def test_ignorable_extension_member_applies_on_this_release(net):
    authority, mirror, _ = net
    entry = observation("Year Only", ext={"x_future": {"precision": "year", "value": "2027"}})
    around(authority, "obs", entry)
    pull(mirror, authority)
    assert mirror.failures() == []
    with mirror.st.maker() as session:
        stored = session.get(Observation, entry["observation"]["id"])
        assert stored.payload == {"title": "Year Only"}  # the unknown optional member is not stored


# ---- C1.11 (g)-(h): refusal ------------------------------------------------------------------------------

def test_g_refusal_when_this_node_is_older_than_the_upstream_minimum(net):
    authority, mirror, _ = net
    authority.publish("Hidden Behind A Refusal")
    newer = {"protocol": "1.6", "min_follower_protocol": "1.5"}
    with pytest.raises(sync.SyncRefused, match=r"needs protocol ≥ 1.5; this node speaks 1.4. Upgrade Atlas"):
        _, wire = pull(mirror, authority, info=lambda body: {**body, **newer})
    with mirror.st.maker() as session:
        assert session.get(SyncCursor, authority.node_id) is None  # cursor untouched
        assert session.scalars(select(ReplicatedChange)).first() is None  # nothing downloaded
        refused = json.loads(session.get(NodeState, "sync_refused:" + authority.node_id).value)
    assert refused["upstream_protocol"] == "1.6" and "Upgrade Atlas" in refused["reason"]
    status = mirror.heads(authority)
    assert status["state"] == "refused" and status["upstream_protocol"] == "1.6"
    # Accepted again once the upstream allows this node: the refusal clears.
    pull(mirror, authority)
    assert mirror.heads(authority)["state"] == "in_sync"


def test_g_no_changes_request_is_made_when_refused(net):
    authority, mirror, _ = net
    wire = Wire(authority, info=lambda body: {**body, "min_follower_protocol": "1.9"})
    with httpx.Client(transport=httpx.MockTransport(wire)) as http, pytest.raises(sync.SyncRefused):
        sync.pull_once(mirror.st.maker, mirror.st.ctx, http, "http://upstream", authority.token,
                       trusted={authority.node_id: authority.st.ctx.identity.trust_public_b64})
    assert [path for path, _ in wire.seen] == ["/api/v2/sync/info"]


def test_h_n_minus_two_refusal(net, monkeypatch):
    authority, mirror, _ = net
    monkeypatch.setattr(sync, "PROTOCOL", "3.0")
    with pytest.raises(sync.SyncRefused, match="older than this node supports"):
        pull(mirror, authority)
    monkeypatch.setattr(sync, "PROTOCOL", "2.0")
    pull(mirror, authority)  # N-1 is still accepted


def test_pre_1_1_upstream_is_accepted(net):
    authority, mirror, _ = net
    authority.publish("From An Old Authority")
    legacy = ("protocol", "min_follower_protocol", "emit_protocol", "kinds", "capabilities", "observation_schema")
    pull(mirror, authority, info=lambda body: {k: v for k, v in body.items() if k not in legacy})
    assert "From An Old Authority" in mirror.titles()
    assert mirror.heads(authority)["upstream_protocol"] == "1.0"


def test_malformed_upstream_protocol_is_refused(net):
    authority, mirror, _ = net
    with pytest.raises(sync.SyncRefused, match="malformed"):
        pull(mirror, authority, info=lambda body: {**body, "protocol": "one point four"})


# ---- C1.11 (i)-(j): replay and skip ---------------------------------------------------------------------

@pytest.fixture
def release_1_1(monkeypatch):
    """Play a follower of the 1.1 release: it speaks 1.1, knows no `tier-platinum` token and no platinum tier.
    Everything else is this release's code, which is what 1.1 had for these paths (classify, park, replay)."""
    def install():
        old = {k: (v - {"platinum"} if k == "achievement_tier" else v) for k, v in feed_schema.vocabularies().items()}
        monkeypatch.setattr(feed_schema, "vocabularies", lambda: old)
        monkeypatch.setattr(feed_schema, "VOCAB_TOKENS", ())
        monkeypatch.setattr(feed_schema, "PROTOCOL", "1.1")
        monkeypatch.setattr(sync, "PROTOCOL", "1.1")
    return install


@pytest.fixture
def future_kind_support(monkeypatch):
    """Play "this node was upgraded to a release that knows `x_future`"."""
    def install():
        kinds = dict(feed_schema.FEED_KINDS)
        kinds["x_future"] = feed_schema.KindSpec("x_future", "1.4", 1, 1, frozenset(FUTURE), "test")
        monkeypatch.setattr(feed_schema, "FEED_KINDS", kinds)
        real = sync._apply

        def apply(session, ctx, node_id, seq, kind, body):
            if kind == "x_future":
                session.add(NodeState(key=f"x_future:{seq}", value=json.dumps(body)))
                session.flush()
                return None
            return real(session, ctx, node_id, seq, kind, body)

        monkeypatch.setattr(sync, "_apply", apply)
    return install


def test_i_replay_after_upgrade_converges_to_the_authority_view(net, future_kind_support):
    authority, mirror, _ = net
    seq = around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    assert mirror.heads(authority)["state"] == "parked"
    with mirror.st.maker() as session, authority.st.maker() as upstream:
        assert sync.view_digest(session, authority.node_id) != sync.view_digest(upstream, None)

    future_kind_support()
    with mirror.st.maker() as session:
        result = sync.replay(session, mirror.st.ctx)
        session.commit()
    assert result[authority.node_id]["applied_head"] == authority.head()
    assert result[authority.node_id]["state"] == "in_sync"
    with mirror.st.maker() as session, authority.st.maker() as upstream:
        assert sync.view_digest(session, authority.node_id) == sync.view_digest(upstream, None)
        assert json.loads(session.get(NodeState, f"x_future:{seq}").value) == FUTURE
        assert session.get(SyncFailure, (authority.node_id, seq)).resolved_at is not None
    assert "After" in mirror.titles() and mirror.failures() == []
    with mirror.st.maker() as session:  # idempotent
        assert sync.replay(session, mirror.st.ctx)[authority.node_id]["applied"] == 0


def test_pull_after_upgrade_also_converges_without_running_replay(net, future_kind_support):
    authority, mirror, _ = net
    around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    future_kind_support()
    pull(mirror, authority)
    assert mirror.heads(authority)["in_sync"] and "After" in mirror.titles()


def _persisted(node, origin):
    """What a dry run or a rolled-back apply must leave exactly as it was, read from the database file."""
    with node.st.maker() as session:
        return {"view": sync.view_digest(session, origin.node_id),
                "node_state": dict(session.execute(select(NodeState.key, NodeState.value)).all()),
                "observations": session.scalar(select(func.count()).select_from(Observation)),
                "cursor": [(c.upstream, c.last_seq) for c in session.scalars(select(SyncCursor))],
                "failures": sorted((f.node_id, f.seq, f.disposition, f.reason, f.resolved_at is None)
                                   for f in session.scalars(select(SyncFailure)))}


def test_replay_dry_run_changes_nothing(net, future_kind_support, tmp_path, monkeypatch, capsys):
    authority, mirror, _ = net
    around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    future_kind_support()
    before = _persisted(mirror, authority)
    _cli_env(monkeypatch, mirror)
    assert cli.main(["sync", "replay", "--dry-run"]) == cli.EXIT_CONVERGED
    assert "would apply 2 entries" in capsys.readouterr().out
    assert mirror.heads(authority)["state"] == "parked"
    # The x_future apply runs inside a savepoint that opens the SQLite transaction. pysqlite's legacy
    # mode committed it on RELEASE, so the dry run persisted `x_future:<seq>` (then `relation:<seq>`) (PHASE2 D1).
    after = _persisted(mirror, authority)
    assert not any(k.startswith("x_future:") for k in after["node_state"])
    assert after == before
    assert "After" not in mirror.titles()
    assert cli.main(["sync", "replay"]) == cli.EXIT_CONVERGED  # the real replay still has everything to do
    assert "applied 2 entries" in capsys.readouterr().out
    assert mirror.heads(authority)["in_sync"] and "After" in mirror.titles()


def test_a_pull_that_rolls_back_after_applying_leaves_nothing_behind(net, future_kind_support, monkeypatch):
    authority, mirror, _ = net
    seq = around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    future_kind_support()
    before = _persisted(mirror, authority)

    def fail(*_args, **_kw):
        raise RuntimeError("simulated failure after the parked entries were applied")

    monkeypatch.setattr(sync, "retry_failures", fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        pull(mirror, authority)
    assert _persisted(mirror, authority) == before
    assert f"x_future:{seq}" not in before["node_state"]
    monkeypatch.undo()
    future_kind_support()
    pull(mirror, authority)
    assert mirror.heads(authority)["in_sync"] and "After" in mirror.titles()


def test_j_skip_is_audited_and_lets_later_entries_apply(net):
    authority, mirror, _ = net
    seq = around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    with mirror.st.maker() as session:
        with pytest.raises(ValueError, match="give a reason"):
            sync.skip(session, mirror.st.ctx, authority.node_id, seq, "  ")
        with pytest.raises(ValueError, match="not parked"):
            sync.skip(session, mirror.st.ctx, authority.node_id, seq - 1, "wrong entry")
        result = sync.skip(session, mirror.st.ctx, authority.node_id, seq, "accepted: x_future not needed here")
        session.commit()
    assert result["applied_head"] == authority.head()
    assert mirror.failures(authority) == [(seq, "x_future", "quarantined", "skipped")]
    assert "After" in mirror.titles()
    with mirror.st.maker() as session:
        [audit] = session.scalars(select(AuditEntry).where(AuditEntry.action == "sync_skip")).all()
        assert audit.target == f"{authority.node_id}:{seq}"
        assert audit.detail["reason"] == "accepted: x_future not needed here"
        assert audit.detail["parked_reason"] == "unsupported_kind"
    pull(mirror, authority)  # a skipped entry is never parked again
    assert mirror.failures(authority) == [(seq, "x_future", "quarantined", "skipped")]
    assert mirror.heads(authority)["applied_head"] == authority.head()


# ---- C1.11 (k): the head invariant under random page failures -------------------------------------------

@pytest.mark.parametrize("seed", range(6))
def test_k_head_invariant_holds_after_random_page_failures(tmp_path, seed):
    rng = random.Random(seed)
    authority, mirror = Node(tmp_path, "a", "authority", sync_batch_size=3), Node(tmp_path, "m", "mirror",
                                                                                   sync_batch_size=3)
    for index in range(14):
        roll = rng.random()
        if roll < 0.15:
            authority.emit("x_future", FUTURE)
        elif roll < 0.3:
            authority.emit("obs", observation("x" * 600))  # quarantined
        else:
            authority.publish(f"Game {seed}-{index}")

    def corrupt(page):
        if page["entries"] and rng.random() < 0.3:
            entries = [dict(e) for e in page["entries"]]
            entries[-1]["signature"] = entries[0]["signature"] if len(entries) > 1 else "AAAA"
            return {**page, "entries": entries}
        return page

    for _ in range(12):
        try:
            pull(mirror, authority, changes=corrupt, fail=lambda r: rng.random() < 0.2)
        except (sync.SyncError, httpx.HTTPError):
            pass
        h = mirror.heads(authority)
        assert h["applied_head"] <= h["verified_head"] <= h["downloaded_head"] <= h["observed_head"], h
        assert h["verified_head"] == max(mirror.retained(authority) or [0])
        assert set(mirror.retained(authority)) == set(range(1, h["verified_head"] + 1))  # contiguous
        parked = [seq for seq, _, disposition, _ in mirror.failures(authority) if disposition == "parked"]
        if parked:
            assert h["applied_head"] == min(parked) - 1  # never applied past a parked entry
        assert not (h["in_sync"] and h["verified_head"] < authority.head())


# ---- backlog cap (H10), rewinds, rescan -----------------------------------------------------------------

def test_downloading_pauses_when_the_parked_backlog_is_full(tmp_path):
    authority = Node(tmp_path, "a", "authority")
    mirror = Node(tmp_path, "m", "mirror", sync_max_parked_backlog=3, sync_batch_size=2)
    seq = authority.emit("x_future", FUTURE)
    for index in range(6):
        authority.publish(f"Behind {index}")
    pull(mirror, authority)
    heads = mirror.heads(authority)
    assert heads["applied_head"] == seq - 1 and heads["verified_head"] == seq + 2  # 3 held back, then it stops
    assert heads["parked"]["backlog_full"] and heads["state"] == "backlog_full"
    n, wire = pull(mirror, authority)
    assert n == 0 and [p for p, _ in wire.seen] == ["/api/v2/sync/info"]  # no download while full


def test_follower_never_reports_in_sync_from_an_unverified_head(net):
    authority, mirror, _ = net
    authority.publish("One")
    authority.publish("Two")
    pull(mirror, authority, changes=lambda page: {**page, "entries": page["entries"][:1]})
    heads = mirror.heads(authority)
    assert heads["applied_head"] == heads["verified_head"] == 1 and heads["observed_head"] == 2
    assert heads["state"] == "behind" and not heads["in_sync"]
    pull(mirror, authority, changes=lambda page: {k: v for k, v in page.items() if k != "head"})
    with mirror.st.maker() as session:
        session.execute(NodeState.__table__.delete().where(NodeState.key.like("sync_head:%")))
        session.commit()
    assert mirror.heads(authority)["state"] == "unknown"  # no head ever reported: not "in sync"


def test_rewound_authority_is_reported_not_silently_ignored(net):
    authority, mirror, _ = net
    for title in ("A", "B", "C"):
        authority.publish(title)
    pull(mirror, authority)
    with pytest.raises(sync.SyncError, match="fork or rewind"):
        pull(mirror, authority, changes=lambda page: {**page, "head": 1})
    heads = mirror.heads(authority)
    assert heads["applied_head"] == 3
    assert heads["state"] == "rewound" and not heads["in_sync"], heads  # never "in sync" from a forked feed
    assert (heads["rewound"]["kind"], heads["rewound"]["advertised_head"], heads["rewound"]["verified_head"]) == \
        ("rewind", 1, 3)
    with mirror.st.maker() as session:  # persisted in node state, also without a Peer row
        assert session.get(NodeState, "sync_rewound:" + authority.node_id) is not None
    # A pull that again sees a short head stays rewound; one that re-serves the verified tip unchanged clears it.
    with pytest.raises(sync.SyncError, match="fork or rewind"):
        pull(mirror, authority, changes=lambda page: {**page, "head": 2})
    pull(mirror, authority)  # the genuine feed again: the verified tip matches, so the record clears
    assert mirror.heads(authority)["state"] == "in_sync" and mirror.heads(authority)["rewound"] is None


def test_forked_tip_keeps_the_upstream_rewound_and_the_cli_exits_5(net, monkeypatch, capsys):
    """After a rewind the follower re-reads its last verified entry. If the authority now serves a
    different signed entry under that sequence number, that is a fork: still not in sync."""
    authority, mirror, _ = net
    for title in ("A", "B"):
        authority.publish(title)
    pull(mirror, authority)
    with pytest.raises(sync.SyncError, match="fork or rewind"):
        pull(mirror, authority, changes=lambda page: {**page, "head": 1})

    from atlas.signing import load_identity

    with authority.st.maker() as session:  # the authority's real key signs a different entry 2
        signer = load_identity(session)
    other = observation("Forked History")

    def forked(page):
        entries = [dict(e) for e in page["entries"]]
        for e in entries:
            if e["seq"] == 2:
                digest = feed.body_hash(other)
                e.update(kind="obs", body=other, body_hash=digest, signature=signer.sign(2, "obs", digest))
        return {**page, "entries": entries}

    with pytest.raises(sync.SyncError, match="already retained entry 2 changed"):
        pull(mirror, authority, changes=forked)
    heads = mirror.heads(authority)
    assert heads["state"] == "rewound" and heads["rewound"]["kind"] == "fork", heads
    assert other["observation"]["id"] not in mirror.observation_ids(authority)
    assert mirror.retained(authority) == authority.own_feed()  # the retained copy never changed
    _cli_env(monkeypatch, mirror)
    assert cli.main(["sync", "status"]) == cli.EXIT_REWOUND
    assert "rewound: authority feed" in capsys.readouterr().out
    pull(mirror, authority)
    assert mirror.heads(authority)["state"] == "in_sync"
    assert cli.main(["sync", "status"]) == cli.EXIT_CONVERGED


def test_relay_that_is_behind_is_not_an_error(net):
    authority, mirror, local = net
    authority.publish("A")
    pull(mirror, authority)
    pull(local, mirror, authority)
    authority.publish("B")
    pull(local, authority)  # local gets ahead of the relay directly from the authority
    pull(local, mirror, authority)  # the relay serves an older head: fine, and reported
    heads = local.heads(authority)
    assert heads["applied_head"] == 2 and heads["in_sync"]


def test_rescan_records_entries_an_older_release_silently_skipped(net):
    authority, mirror, _ = net
    authority.publish("Before")
    unknown = authority.emit("x_future", FUTURE)
    invalid = authority.emit("obs", observation("x" * 600))
    extended = observation("Extended", ext={"x_future": {"date": "2026"}})
    skipped = authority.emit("obs", extended)
    authority.publish("After")
    pull(mirror, authority)
    # Simulate a pre-1.1 mirror: it advanced past the entries and recorded nothing; it had rejected the
    # observation with an `ext` member (a field it did not know) without storing it.
    obs_id = extended["observation"]["id"]
    with mirror.st.maker() as session:
        for row in session.scalars(select(SyncFailure)):
            session.delete(row)
        assert session.get(Observation, obs_id) is None  # held back behind the parked entry, never applied
        session.get(SyncCursor, authority.node_id).last_seq = authority.head()
        session.commit()
    with mirror.st.maker() as session:
        counts = sync.rescan(session, authority.node_id)
        session.commit()
    # "After" was held back behind the parked entry, so the old release never applied it either.
    assert counts == {"scanned": 5, "parked": 3, "quarantined": 1}
    assert mirror.failures(authority) == [(unknown, "x_future", "parked", "unsupported_kind"),
                                          (invalid, "obs", "quarantined", "invalid"),
                                          (skipped, "obs", "parked", "dropped_by_previous_release"),
                                          (authority.head(), "obs", "parked", "dropped_by_previous_release")]
    heads = mirror.heads(authority)
    assert heads["applied_head"] == authority.head()  # the cursor never moves back
    assert heads["parked"]["count"] == 3 and heads["state"] == "parked"
    with mirror.st.maker() as session:  # idempotent
        assert sync.rescan(session, authority.node_id) == {"scanned": 5, "parked": 0, "quarantined": 0}
    with mirror.st.maker() as session:  # a plain replay never applies them out of order (C1.7, C1.8)
        result = sync.replay(session, mirror.st.ctx, authority.node_id)[authority.node_id]
        session.commit()
        assert session.get(Observation, obs_id) is None
    assert result["historical_parked"] == unknown and result["reapplied"] == 0
    with mirror.st.maker() as session:  # --historical re-applies the retained range in feed order
        result = sync.replay(session, mirror.st.ctx, authority.node_id, historical=True)[authority.node_id]
        session.commit()
        assert session.get(Observation, obs_id) is not None
    assert result["reapplied"] == authority.head() - unknown + 1 and result["recovered_parked"] == 2
    assert [f[0] for f in mirror.failures(authority)] == [unknown, invalid]
    assert "After" in mirror.titles()


def _old_release(monkeypatch, skip_seqs=()):
    """Play a release before 1.1 on this node: unknown kinds (and `skip_seqs`) are passed over silently,
    any open failure row for them is marked resolved, and the cursor moves on. Returns an undo."""
    real = sync.apply_entry

    def apply_entry(session, ctx, node_id, seq, kind, body, *, may_park=True):
        if kind not in feed_schema.FEED_KINDS or seq in skip_seqs:
            failure = session.get(SyncFailure, (node_id, seq))
            if failure is not None and failure.resolved_at is None:
                from datetime import UTC, datetime
                failure.resolved_at = datetime.now(UTC)
            return sync.NOOP
        return real(session, ctx, node_id, seq, kind, body, may_park=may_park)

    monkeypatch.setattr(sync, "apply_entry", apply_entry)
    return lambda: monkeypatch.setattr(sync, "apply_entry", real)


def _old_release_retry_and_catch_up(node, origin):
    """What the older release's pull does with the retained copy: retry every open row, then apply on."""
    with node.st.maker() as session:
        for row in session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None))).all():
            entry = session.get(ReplicatedChange, (row.node_id, row.seq))
            sync.apply_entry(session, node.st.ctx, row.node_id, row.seq, entry.kind, entry.body)
        sync._apply_pending(session, node.st.ctx, origin.node_id)
        session.commit()


def test_rollback_then_reupgrade_rescan_recovers_rows_the_old_release_resolved(net, monkeypatch, future_kind_support):
    """Upgrade, park, roll the code back (the old release resolves the parked row and moves past it),
    upgrade again: rescan must re-classify the resolved row instead of skipping it (C1.8, C1.10)."""
    authority, mirror, _ = net
    seq = around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    assert mirror.failures(authority) == [(seq, "x_future", "parked", "unsupported_kind")]

    undo = _old_release(monkeypatch)
    _old_release_retry_and_catch_up(mirror, authority)
    undo()
    with mirror.st.maker() as session:
        row = session.get(SyncFailure, (authority.node_id, seq))
        assert row.resolved_at is not None  # the old release marked it resolved...
    assert mirror.heads(authority)["applied_head"] == authority.head()  # ...and moved past it
    assert mirror.heads(authority)["in_sync"]  # what the old release would claim: the silent loss

    with mirror.st.maker() as session:
        counts = sync.rescan(session, authority.node_id)
        session.commit()
    assert counts == {"scanned": 3, "parked": 1, "quarantined": 0}
    heads = mirror.heads(authority)
    assert mirror.failures(authority) == [(seq, "x_future", "parked", "unsupported_kind")]
    assert heads["state"] == "parked" and not heads["in_sync"]  # no longer silently "in sync"
    with mirror.st.maker() as session:
        row = session.get(SyncFailure, (authority.node_id, seq))
        assert row.resolved_at is None and row.attempts >= 2
        assert session.get(NodeState, f"x_future:{seq}") is None  # nothing applied by rescan

    future_kind_support()  # the upgraded release understands it: --historical applies it in place
    with mirror.st.maker() as session:
        result = sync.replay(session, mirror.st.ctx, historical=True)[authority.node_id]
        session.commit()
        assert json.loads(session.get(NodeState, f"x_future:{seq}").value) == FUTURE
    assert result["recovered_parked"] == 1 and result["state"] == "in_sync"
    assert mirror.failures(authority) == []


def test_historical_replay_applies_skipped_entries_and_their_dependants_in_feed_order(net, monkeypatch):
    """An older release skipped an observation, so it also skipped the merge that needed its record.
    Re-applying from the skipped entry in feed order brings both back; a later observation of the same
    source record stays current."""
    authority, mirror, _ = net

    def ingest(title):
        with authority.st.maker() as session:
            [result] = service.ingest(session, authority.st.ctx, batch("manual", [{"external_id": "edition",
                                                                                   "title": title}], kind="manual"),
                                      curator=True)
            session.commit()
        return result.canonical_id, authority.head()

    source, source_seq = ingest("Edition One")
    target = authority.publish("Merge Target")
    with authority.st.maker() as session:
        service.merge_canonicals(session, authority.st.ctx, source, target, actor="test")
        session.commit()
    merge_seq = authority.head()
    assert authority.own_feed()[merge_seq][0] == "merge"
    _, newer_seq = ingest("Edition Two")
    authority.publish("Tail")

    def by_seq(session, seq):
        return session.scalars(select(Observation).where(Observation.origin_node == authority.node_id,
                                                         Observation.origin_seq == seq)).one_or_none()

    undo = _old_release(monkeypatch, skip_seqs={source_seq, merge_seq})
    pull(mirror, authority)
    undo()
    with mirror.st.maker() as session:
        assert by_seq(session, source_seq) is None
        assert by_seq(session, newer_seq).is_current
    assert mirror.heads(authority)["applied_head"] == authority.head() and mirror.failures() == []

    with mirror.st.maker() as session:
        assert sync.rescan(session, authority.node_id)["parked"] == 1
        session.commit()
    assert mirror.failures(authority) == [(source_seq, "obs", "parked", "dropped_by_previous_release")]

    # The startup replay never applies rows below the cursor.
    with mirror.st.maker() as session:
        session.execute(NodeState.__table__.delete().where(NodeState.key == "sync_replayed_for"))
        session.commit()
    assert sync.startup_replay(mirror.st.maker, mirror.st.ctx) is not None
    with mirror.st.maker() as session:
        assert by_seq(session, source_seq) is None
    assert mirror.heads(authority)["state"] == "parked"

    with mirror.st.maker() as session:
        result = sync.replay(session, mirror.st.ctx, authority.node_id, historical=True)[authority.node_id]
        session.commit()
    assert result["reapplied"] == authority.head() - source_seq + 1 and result["state"] == "in_sync"
    assert mirror.failures() == []
    with mirror.st.maker() as session, authority.st.maker() as upstream:
        from atlas.models import Canonical

        old, new = by_seq(session, source_seq), by_seq(session, newer_seq)
        assert old is not None and not old.is_current  # superseded by a later entry: history
        assert new.is_current  # the later entry still wins
        assert session.get(Canonical, source).merged_into == target  # the merge ran after it
        assert sync.view_digest(session, authority.node_id) == sync.view_digest(upstream, None)
    with mirror.st.maker() as session:  # idempotent
        again = sync.replay(session, mirror.st.ctx, authority.node_id, historical=True)[authority.node_id]
        assert again["reapplied"] == 0 and again["applied"] == 0


def test_kinds_table_in_the_guide_matches_the_registry():
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[1] / "docs" / "guide" / "federation.md").read_text(encoding="utf-8")
    assert feed_schema.markdown_table() in guide.replace("\r\n", "\n")


def test_relay_at_this_release_relays_newer_entries_byte_for_byte(net):
    authority, mirror, local = net
    around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    pull(local, mirror, authority)
    assert mirror.retained(authority) == local.retained(authority) == authority.own_feed()
    assert local.heads(authority)["state"] == "parked"


# ---- operator surfaces: CLI, API, admin page, metrics ---------------------------------------------------

def _cli_env(monkeypatch, node):
    from atlas.settings import get_settings

    monkeypatch.setenv("ATLAS_DATABASE_URL", node.st.ctx.settings.database_url)
    monkeypatch.setenv("ATLAS_ROLE", node.st.ctx.settings.role)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings():
    from atlas.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_status_replay_and_skip_exit_codes(net, monkeypatch, capsys, future_kind_support):
    authority, mirror, _ = net
    seq = around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    _cli_env(monkeypatch, mirror)
    assert cli.main(["sync", "status"]) == cli.EXIT_PARKED
    out = capsys.readouterr().out
    assert f"parked: 2 entries from seq {seq} (unsupported_kind, kind 'x_future')" in out
    assert cli.main(["sync", "status", "--json", "--node", authority.node_id]) == cli.EXIT_PARKED
    data = json.loads(capsys.readouterr().out)
    assert data["upstreams"][authority.node_id]["parked"]["first_seq"] == seq
    assert cli.main(["sync", "replay"]) == cli.EXIT_PARKED
    assert "still parked at seq" in capsys.readouterr().out
    assert cli.main(["sync", "skip", "--node", authority.node_id, "--seq", str(seq - 1), "--reason", "x"]) == 1
    future_kind_support()
    assert cli.main(["sync", "replay", "--node", authority.node_id]) == cli.EXIT_CONVERGED
    assert cli.main(["sync", "status"]) == cli.EXIT_CONVERGED


def test_cli_reports_a_refused_upstream(net, monkeypatch, capsys):
    authority, mirror, _ = net
    with pytest.raises(sync.SyncRefused):
        pull(mirror, authority, info=lambda body: {**body, "min_follower_protocol": "1.5"})
    _cli_env(monkeypatch, mirror)
    assert cli.main(["sync", "status"]) == cli.EXIT_REFUSED
    assert "refused: upstream needs protocol" in capsys.readouterr().out
    assert cli.main(["sync", "replay"]) == cli.EXIT_REFUSED


def test_cli_rescan_and_plain_sync_without_upstream(net, monkeypatch, capsys):
    authority, mirror, _ = net
    authority.publish("A")
    pull(mirror, authority)
    _cli_env(monkeypatch, mirror)
    assert cli.main(["sync", "replay", "--rescan"]) == cli.EXIT_CONVERGED
    assert "recorded 0 parked and 0 quarantined" in capsys.readouterr().out
    assert cli.main(["sync"]) == 2  # unchanged: needs ATLAS_UPSTREAM_URL


def test_reconcile_compares_digests_with_the_upstream(net):
    authority, mirror, local = net
    authority.publish("A")
    authority.publish("B")
    pull(mirror, authority)
    w = Wire(authority)
    with httpx.Client(transport=httpx.MockTransport(w)) as http, mirror.st.maker() as session:
        same = sync.reconcile(session, http, "http://upstream", authority.token, authority.node_id)
    assert same["match"] is True and same["local"]["observations"] == 2
    # Through a relay: the relay answers for the retained authority feed.
    pull(local, mirror, authority)
    with httpx.Client(transport=httpx.MockTransport(Wire(mirror))) as http, local.st.maker() as session:
        relayed = sync.reconcile(session, http, "http://upstream", mirror.token, authority.node_id)
    assert relayed["match"] is True and relayed["upstream"]["node_id"] == authority.node_id
    with mirror.st.maker() as session:  # diverge locally
        session.delete(session.scalars(select(Observation).where(Observation.origin_node == authority.node_id)).first())
        session.commit()
    with httpx.Client(transport=httpx.MockTransport(Wire(authority))) as http, mirror.st.maker() as session:
        assert sync.reconcile(session, http, "http://upstream", authority.token, authority.node_id)["match"] is False


def test_status_api_exposes_named_heads(net):
    authority, mirror, _ = net
    seq = around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    admin = {"Authorization": f"Bearer {_admin(mirror)}"}
    status = mirror.client.get("/api/v2/status", headers=admin).json()
    up = status["upstreams"][authority.node_id]
    assert (up["applied_head"], up["verified_head"], up["parked"]["first_seq"]) == (seq - 1, seq + 1, seq)
    detail = mirror.client.get("/api/v2/sync/status", headers=admin).json()
    assert detail["own_protocol"] == feed_schema.PROTOCOL and detail["upstreams"][authority.node_id]["state"] == "parked"
    failures = mirror.client.get("/api/v2/sync/failures", headers=admin).json()
    assert failures["parked"] == 1 and failures["failures"][0]["reason"] == "unsupported_kind"
    assert mirror.client.get("/api/v2/sync/status", headers={"Authorization": f"Bearer {mirror.token}"}
                             ).status_code == 403


def test_metrics_expose_heads_and_parked_counts(tmp_path):
    authority = Node(tmp_path, "a", "authority")
    mirror = Node(tmp_path, "m", "mirror", metrics_enabled=True)
    around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    with mirror.st.maker() as session:
        _, scrape = auth.create_token(session, "scrape", ["metrics"])
        session.commit()
    text = mirror.client.get("/metrics", headers={"Authorization": f"Bearer {scrape}"}).text
    label = json.dumps(authority.node_id)
    assert f'atlas_sync_upstream_head{{node_id={label},head="applied"}} 1' in text
    assert f'atlas_sync_upstream_head{{node_id={label},head="verified"}} 3' in text
    assert f"atlas_sync_parked_entries{{node_id={label}}} 2" in text
    assert f"atlas_sync_in_sync{{node_id={label}}} 0" in text


def test_startup_replay_runs_once_per_release(net, future_kind_support):
    authority, mirror, _ = net
    around(authority, "x_future", FUTURE)
    pull(mirror, authority)
    assert sync.startup_replay(mirror.st.maker, mirror.st.ctx) is not None
    assert sync.startup_replay(mirror.st.maker, mirror.st.ctx) is None  # same release: skipped
    future_kind_support()  # the registry changed: the marker differs, so it runs and converges
    assert sync.startup_replay(mirror.st.maker, mirror.st.ctx)[authority.node_id]["state"] == "in_sync"


def test_peer_records_the_remote_protocol(net):
    authority, mirror, _ = net
    with mirror.st.maker() as session:
        session.add(Peer(name="a", url="http://upstream", node_id=authority.node_id,
                         public_key=authority.st.ctx.identity.trust_public_b64))
        session.commit()
    pull(mirror, authority)
    with mirror.st.maker() as session:
        assert session.scalars(select(Peer)).one().remote_protocol == feed_schema.PROTOCOL


def test_follower_telemetry_upsert_tolerates_a_row_another_request_inserted(net):
    """Two requests with the same token can both see no row; the second insert must not fail the sync."""
    from datetime import UTC, datetime, timedelta

    from atlas.api import routes

    authority, _, _ = net
    token_id = authority.token_row.id
    with authority.st.maker() as first, authority.st.maker() as second:
        assert first.get(FollowerSeen, token_id) is None and second.get(FollowerSeen, token_id) is None
        routes.upsert_follower_seen(first, token_id, "1.0", datetime.now(UTC) - timedelta(minutes=5))
        first.commit()
        routes.upsert_follower_seen(second, token_id, "1.1", datetime.now(UTC))  # would be a duplicate insert
        second.commit()
    with authority.st.maker() as session:
        assert session.get(FollowerSeen, token_id).protocol == "1.1"
    headers = {"Authorization": f"Bearer {authority.token}", "Atlas-Protocol": "1.1"}
    for _ in range(3):  # repeated reads within a minute: no rewrite, no error
        assert authority.client.get("/api/v2/sync/changes", headers=headers).status_code == 200
    with authority.st.maker() as session:
        assert session.scalars(select(FollowerSeen)).all()[0].protocol == "1.1"


def test_sync_digest_is_cached_per_feed(net):
    authority, _, _ = net
    authority.publish("A")
    headers = {"Authorization": f"Bearer {authority.token}"}
    first = authority.client.get("/api/v2/sync/digest", headers=headers).json()
    authority.publish("B")
    second = authority.client.get("/api/v2/sync/digest", headers=headers).json()
    assert first["cached_seconds"] == 0.0 and second["digest"] == first["digest"]  # served from the cache
    assert first["observations"] == 1
    authority.st.__dict__["_digest_cache"].clear()
    fresh = authority.client.get("/api/v2/sync/digest", headers=headers).json()
    assert fresh["observations"] == 2 and fresh["digest"] != first["digest"]
