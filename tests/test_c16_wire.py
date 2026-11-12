"""C16 Z1: one wire version with active negotiation (CONTRACT-C16 §22, `_notes/C16-VERSION-NEGOTIATION-DESIGN.md`).

Covers the negotiation (T-W1), the v0 refusal and the v0 tombstone (T-W2), the tolerant reader (T-W5), the header
grammar and the refusal body (T-W14), `wire_start`, renditions (the verifier's canonical-first and mirror rules),
the adapter registry on a synthetic registry, the wire high-water mark (T-W24), D5 widened ids, migration 0063 up
and down, and R2 (a pre-upgrade export verified after the upgrade: no data loss)."""

from __future__ import annotations

import base64
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from atlas import db, export_verify, feed_schema, rendition, signing, wire
from atlas.models import ChangeLog, NodeState, Setting

from .conftest import build_settings
from .test_sync_compat import Node, observation, pull

# ---- negotiation (T-W1) ------------------------------------------------------------------------------------------

A, B, C, D = wire.Range(1, 1), wire.Range(1, 2), wire.Range(2, 3), wire.Range(3, 4)


@pytest.mark.parametrize(("mine", "theirs", "use"), [(A, A, 1), (A, B, 1), (B, B, 2), (B, C, 2), (C, D, 3)])
def test_negotiate_picks_the_highest_common_wire(mine, theirs, use):
    assert wire.negotiate(mine, theirs) == use == wire.negotiate(theirs, mine)


def test_no_overlap_is_refused_naming_both_sides():
    with pytest.raises(wire.WireUnsupported) as err:
        wire.negotiate(C, A)  # the caller is too old for this side
    assert err.value.reason == "peer_too_old" and err.value.action == "upgrade_client"
    with pytest.raises(wire.WireUnsupported) as err:
        wire.negotiate(A, C)  # the caller only speaks newer wires
    assert err.value.reason == "peer_too_new" and err.value.action == "upgrade_server"
    body = err.value.body()
    assert body["code"] == "wire_unsupported" and body["server"]["wire"] == {"min": 1, "max": 1}
    assert body["client"]["wire"] == {"min": 2, "max": 3} and "wire 1" in body["detail"]


# ---- header grammar (T-W14) ------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "max=1", "min=1", "min=2, max=1", "min=0, max=1", "min=1, min=1, max=1",
                                 "min=1;max=1", "min=1, max=99999", "MIN=1, MAX=1"])
def test_malformed_wire_headers_are_invalid(bad):
    with pytest.raises(wire.InvalidWire):
        wire.parse_header(bad)


def test_unknown_header_keys_are_ignored_and_the_header_round_trips():
    assert wire.parse_header(" min=1 , max=3, future=7") == {"min": 1, "max": 3, "future": 7}
    assert wire.request_header() == "min=1, max=1"
    assert wire.response_header(1) == "use=1, min=1, max=1"


def test_sync_changes_negotiates_after_authentication(tmp_path):
    node = Node(tmp_path, "n", "authority")
    raw = TestClient(node.app)  # no default Atlas-Wire: plays v0 callers and malformed ones
    assert raw.get("/api/v2/sync/changes").status_code == 401  # authentication runs first
    auth_h = {"Authorization": f"Bearer {node.token}"}
    r = raw.get("/api/v2/sync/changes", headers={**auth_h, "Atlas-Wire": "min=2, max=1"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_wire"
    r = raw.get("/api/v2/sync/changes", headers={**auth_h, "Atlas-Wire": "min=2, max=5"})
    assert r.status_code == 409 and r.json()["error"]["reason"] == "peer_too_new"
    assert r.headers["Atlas-Wire"] == "min=1, max=1" and r.headers["Cache-Control"] == "no-store"
    r = raw.get("/api/v2/sync/changes", headers=auth_h)
    assert r.status_code == 409 and r.json()["error"]["reason"] == "peer_v0"
    assert r.json()["error"]["client"] == {"legacy_protocol": "1.0"}  # a pre-A1 build sends nothing at all
    r = raw.get("/api/v2/sync/changes", headers={**auth_h, "Atlas-Wire": "min=1, max=9"})
    assert r.status_code == 200 and r.headers["Atlas-Wire"] == "use=1, min=1, max=1"
    assert r.headers["Vary"] == "Atlas-Wire"


# ---- v0 (T-W2) ---------------------------------------------------------------------------------------------------

def test_sync_info_keeps_the_v0_tombstone_and_never_refuses(tmp_path):
    node = Node(tmp_path, "n", "authority")
    info = TestClient(node.app).get("/api/v2/sync/info", headers={"Authorization": f"Bearer {node.token}"})
    assert info.status_code == 200
    body = info.json()
    assert (body["protocol"], body["min_follower_protocol"]) == ("2.0", "2.0")
    assert body["wire"] == {"min": 1, "max": 1, "reads_v0": True, "serves_v0": False}
    assert node.client.get("/api/v2/health").json()["wire"]["max"] == 1


def test_release_with_min_1_refuses_a_v0_upstream():
    newer = wire.WireProfile(max=2, min=1, min_peer=1)
    with pytest.raises(wire.WireUnsupported):
        wire.upstream_wire({"protocol": "1.5"}, newer)
    assert wire.upstream_wire({"protocol": "1.5"}) == (0, None)  # this release reads v0 (WIRE_MIN == 0)
    assert wire.upstream_wire({"wire": {"min": 1, "max": 3}}) == (1, wire.Range(1, 3))


def test_a_peer_seen_on_wire_is_not_silently_read_as_v0(tmp_path):
    from atlas import sync
    from atlas.models import Peer

    authority, mirror = Node(tmp_path, "a", "authority"), Node(tmp_path, "m", "mirror")
    authority.publish("Seen")
    with mirror.st.maker() as s:
        s.add(Peer(name="a", url="http://upstream", node_id=authority.node_id,
                   public_key=authority.st.ctx.identity.trust_public_b64))
        s.commit()
    pull(mirror, authority)
    with pytest.raises(sync.SyncRefused, match="looks like v0"):
        pull(mirror, authority, info=lambda body: {k: v for k, v in body.items() if k != "wire"})


# ---- feed content: wire_start, `_wire`, tolerant reader (T-W5) ---------------------------------------------------

def test_wire_start_is_signed_first_and_a_follower_applies_past_it(tmp_path):
    authority, mirror = Node(tmp_path, "a", "authority"), Node(tmp_path, "m", "mirror")
    authority.publish("After The Boundary")
    with authority.st.maker() as s:
        rows = list(s.scalars(select(ChangeLog).order_by(ChangeLog.seq)))
        assert rows[0].kind == "wire_start" and rows[0].body["first_seq"] == 1
        assert all(r.body.get("_wire") == 1 for r in rows)
        assert s.get(NodeState, "wire_start_seq").value == "1"
    pull(mirror, authority)
    assert "After The Boundary" in mirror.titles() and mirror.heads(authority)["state"] == "in_sync"


def test_unknown_member_is_ignored_not_parked_and_a_critical_member_parks_an_old_reader():
    body = {**observation("Additive"), "_wire": 1, "x_new_member": {"a": 1}}
    out = feed_schema.classify("obs", body)
    assert (out.action, out.reason) == ("apply", "ignored_member") and out.detail["ignored"] == ["x_new_member"]
    assert "x_new_member" not in out.body and "_wire" not in out.body and "x_new_member" in body
    critical = observation("Critical", ext={"x_scope": {"tier": "private"}})
    critical["_requires"] = ["x-scope-v1"]
    assert feed_schema.classify("obs", critical).action == "parked"
    assert feed_schema.classify("obs", {**observation("Later"), "_wire": 2}).reason == "unsupported_version"
    assert feed_schema.classify("obs", {**observation("Later"), "_wire": 2}, wire_max=2).action == "apply"


# ---- renditions ------------------------------------------------------------------------------------------------

def _identity(tmp_path, name):
    node = Node(tmp_path, name, "authority")
    return node, node.st.ctx.identity


def test_rendition_verifier_is_canonical_first_and_refuses_mirror_renditions_of_critical_entries(tmp_path):
    _, origin = _identity(tmp_path, "o")
    _, mirror = _identity(tmp_path, "m")
    canonical = {"node_id": origin.node_id, "seq": 9, "kind": "obs", "key_id": origin.key_id,
                 "body_hash": "h" * 64, "signature": "s", "body": {"_wire": 2}}
    adapter = {"id": "obs.2-1", "from": 2, "to": 1, "version": 1}
    keys = {origin.node_id: origin.public_b64, mirror.node_id: mirror.public_b64}
    by_origin = rendition.build(canonical, version=1, adapter=adapter, kind="obs", body={"title": "t"}, omitted=["x"],
                                signer_kind="origin", identity=origin)
    by_mirror = rendition.build(canonical, version=1, adapter=adapter, kind="obs", body={"title": "t"}, omitted=[],
                                signer_kind="mirror", identity=mirror)
    common = {"use": 1, "signer_keys": keys, "mirror_standing": {mirror.node_id}}
    assert rendition.check({**canonical, "body": {"_wire": 1}}, by_origin, own_max=1, critical=False,
                           **common).result == rendition.UNNEEDED  # rule 0: the canonical body is readable
    assert rendition.check(canonical, by_origin, own_max=1, critical=False, **common).result == "apply"
    verdict = rendition.check(canonical, by_mirror, own_max=1, critical=False, **common)
    assert verdict.result == "apply" and verdict.label.startswith("rendition by ")
    assert rendition.check(canonical, by_mirror, own_max=1, critical=True, **common).result == rendition.BAD
    forked = {**canonical, "body_hash": "f" * 64}
    assert rendition.check(forked, by_origin, own_max=1, critical=False, **common).result == "fork"
    tampered = {**by_origin, "body": {"title": "other"}}
    assert rendition.check(canonical, tampered, own_max=1, critical=False, **common).result == rendition.BAD


def test_adapter_registry_completeness_is_checked_on_a_synthetic_registry():
    toy = wire.WireProfile(max=3, min=1, min_peer=1)
    reg = wire.AdapterRegistry()

    @reg.adapter("sync.info", 2, 1)
    def _down(body, omitted):
        omitted.append("feeds[].x")
        return {k: v for k, v in body.items() if k != "x"}

    @reg.adapter("sync.info", 1, 2, "request")
    def _up(body):
        return body

    reg.no_wire_change("sync.info", 3, "optional member; old readers ignore it")
    assert reg.missing(toy) == []
    assert reg.down("sync.info", {"x": 1, "y": 2}, 3, 1) == ({"y": 2}, ["feeds[].x"])
    reg.no_wire_change("group.hello", 2, "additive")
    assert any(gap.startswith("group.hello") for gap in reg.missing(toy))  # wire 3 is not covered
    with pytest.raises(RuntimeError):
        reg.check(toy)
    with pytest.raises(ValueError):
        reg.adapter("sync.info", 3, 1)
    wire.REGISTRY.check()  # this build (wire 1) has no steps to adapt


# ---- start-up (T-W24) and ignored settings ---------------------------------------------------------------------

def test_build_below_wire_high_water_refuses_to_start(tmp_path):
    url = f"sqlite:///{tmp_path}/hw.db"
    db.upgrade(url)
    engine = create_engine(url)
    maker = db.make_sessionmaker(engine, build_settings(database_url=url))
    with maker() as s:
        assert wire.check_high_water(s) == 1
        s.get(NodeState, wire.HIGH_WATER).value = "2"  # a wire-2 build ran on this database
        s.commit()
    with maker() as s, pytest.raises(wire.WireHighWater, match="wire-2 build"):
        wire.check_high_water(s)
    with maker() as s:
        assert wire.check_high_water(s, restoring=True) == 2  # `atlas restore` may go back
        assert s.get(NodeState, wire.HIGH_WATER).value == "1"
    engine.dispose()


def test_leftover_version_settings_are_ignored_and_logged_once(tmp_path, caplog):
    url = f"sqlite:///{tmp_path}/ig.db"
    db.upgrade(url)
    engine = create_engine(url)
    maker = db.make_sessionmaker(engine, build_settings(database_url=url))
    with maker() as s:
        s.add(Setting(key="emit_protocol", value="1.5"))
        s.commit()
        with caplog.at_level(logging.WARNING):
            found = wire.log_ignored(s, {"ATLAS_EMIT_PROTOCOL": "1.4"}, logging.getLogger("atlas.test"))
    assert found == ["ATLAS_EMIT_PROTOCOL", "setting.emit_protocol"]
    assert sum("ignored:" in r.message for r in caplog.records) == 2
    engine.dispose()


# ---- D5: 256-bit ids, explicit legacy mapping ------------------------------------------------------------------

def test_new_identities_are_wide_and_v0_ids_keep_their_form(tmp_path):
    node = Node(tmp_path, "d5", "authority")
    ident = node.st.ctx.identity
    assert len(ident.node_id) == 64 and signing.id_form(ident.node_id) == "w1"
    pub = base64.b64decode(ident.trust_public_b64)
    legacy = signing.key_id_for(pub, legacy=True)
    assert len(legacy) == 16 and ident.node_id.startswith(legacy)
    assert signing.id_matches(legacy, pub) and signing.id_matches(ident.node_id, pub)
    assert not signing.id_matches("0" * 16, pub) and not signing.id_matches(legacy[:15], pub)
    assert signing.key_id_like(pub, legacy) == legacy and signing.key_id_like(pub, None) == ident.node_id


# ---- migration 0063 ----------------------------------------------------------------------------------------------

def test_migration_0063_up_and_down(tmp_path):
    url = f"sqlite:///{tmp_path}/m63.db"
    db.upgrade(url, "0062")
    engine = create_engine(url)
    assert "follower_wire" not in inspect(engine).get_table_names()
    db.upgrade(url)
    names = set(inspect(engine).get_table_names())
    assert {"follower_wire", "feed_rendition", "rendition_received"} <= names
    assert "remote_wire" in {c["name"] for c in inspect(engine).get_columns("peer")}
    with engine.connect() as c:
        assert c.execute(text("SELECT value FROM node_state WHERE key = 'wire_high_water'")).scalar() == "1"
    engine.dispose()
    db.downgrade(url, "0062")
    engine = create_engine(url)
    names = set(inspect(engine).get_table_names())
    assert not {"follower_wire", "feed_rendition", "rendition_received"} & names
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM node_state WHERE key = 'wire_high_water'")).scalar() == 0
    engine.dispose()


# ---- R2: a v0 node upgrades in place with no data loss -----------------------------------------------------------

def test_pre_upgrade_export_verifies_after_the_upgrade_and_catches_a_lost_row(tmp_path):
    url = f"sqlite:///{tmp_path}/v0.db"
    db.upgrade(url, "0062")  # the last v0 schema
    engine = create_engine(url)
    with engine.begin() as c:
        for i in range(25):
            c.execute(text("INSERT INTO node_state (key, value) VALUES (:k, :v)"), {"k": f"v0:{i}", "v": str(i)})
        c.execute(text("INSERT INTO setting (key, value, updated_at) VALUES ('emit_protocol', :v, "
                       "'2026-11-01 00:00:00')"), {"v": '"1.5"'})
    manifest = export_verify.export(engine)
    export_verify.write(tmp_path / "m.json.gz", manifest)
    assert manifest["revision"] == "0062" and manifest["tables"]["node_state"]["rows"] >= 25
    engine.dispose()
    db.upgrade(url)  # the wire-1 upgrade, in place
    engine = create_engine(url)
    report = export_verify.verify(engine, export_verify.read(tmp_path / "m.json.gz"))
    assert report["ok"], report["problems"]
    assert report["revision_after"] == "0063" and "follower_wire" in report["added_tables"]
    assert "remote_wire" in report["tables"]["peer"]["added_columns"]
    with engine.begin() as c:
        c.execute(text("DELETE FROM node_state WHERE key = 'v0:3'"))
    report = export_verify.verify(engine, manifest)
    assert not report["ok"] and report["tables"]["node_state"]["missing_or_changed"] == 1
    engine.dispose()


def test_a_row_a_migration_rewrites_on_purpose_is_checked_by_key_not_content(tmp_path):
    """Migration 0024 encrypts `node_state.signing_key`: a real v0 node (the skew drill) changes that row by design.
    It is reported as transformed while it exists, and as lost when it is gone."""
    url = f"sqlite:///{tmp_path}/v0.db"
    db.upgrade(url, "0062")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO node_state (key, value) VALUES ('signing_key', 'plain')"))
    manifest = export_verify.export(engine)
    assert manifest["tables"]["node_state"]["transformed"] == ["signing_key"]
    with engine.begin() as c:
        c.execute(text("UPDATE node_state SET value = 'encrypted' WHERE key = 'signing_key'"))
    report = export_verify.verify(engine, manifest)
    assert report["ok"], report["problems"]
    assert "0024" in report["tables"]["node_state"]["transformed"]["signing_key"]
    with engine.begin() as c:
        c.execute(text("DELETE FROM node_state WHERE key = 'signing_key'"))
    report = export_verify.verify(engine, manifest)
    assert not report["ok"] and "signing_key" in report["problems"][0]
    engine.dispose()


# ---- every signed entry carries `_wire`, key rotations included -------------------------------------------------

def test_a_rotation_proof_signed_in_wire_1_validates_and_a_bad_wire_does_not(tmp_path):
    """The rotation entry is signed through the feed like every other one, so its body carries `_wire`. The trust
    chain (`key_rotation.validate_history`, used by followers and by the group's member keys) must accept it."""
    from atlas import key_rotation
    from atlas.signing import load_identity

    node = Node(tmp_path, "a", "authority")
    with node.st.maker() as s:
        original = load_identity(s)
        successor = key_rotation.rotate(s, original.key_id)
        s.commit()
    info = node.client.get("/api/v2/sync/info", headers={"Authorization": f"Bearer {node.token}"}).json()
    [proof] = info["rotations"]
    assert proof["body"]["_wire"] == 1
    history, keys = key_rotation.validate_history(original.node_id, original.public_b64, info["rotations"],
                                                  successor.key_id)
    assert successor.key_id in keys and len(history) == 1
    bad = {**proof, "body": {**proof["body"], "_wire": "1"}}
    with pytest.raises(key_rotation.RotationError):
        key_rotation.validate_history(original.node_id, original.public_b64, [bad], successor.key_id)
