"""The five C16 Z2 items PR #149 left for an owner decision (CONTRACT-AMENDMENTS.md, "Not built, needing an owner
decision"), now built:

1. `atlas/_build.json`, `atlas selfcheck` and `/health.build` (`atlas.build_info`, CONTRACT-C16 §10.4).
2. The remaining release gates and the wire snapshot (`tools/wire_snapshot.py`; G-W1, G-W5, G-W7, G-C1 in
   `tools/release_gates.py`). G-W2/G-W3/G-W4 still need a real wire-2 change to mean anything (recorded, not
   silently dropped, in `tools/release_gates.py`'s own module docstring).
3. The registry-level critical-member mark (`feed_schema.CRITICAL_MEMBERS`, generalised past `ext.*`).
4. The upgrade-stage metric (`atlas_upgrade_stage` and friends, `observability.Collectors._upgrade`).
5. The Admin -> Peers upgrade banner (`admin_peers.html`, fed by the same `upgrade.status()` already in
   admin `/status`).

Each test fails on the old behaviour (the feature absent) and passes with the new code, per the regression-style
request."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from atlas import build_info, cli, feed_schema, main, upgrade
from atlas.models import NodeState

from .conftest import build_settings
from .test_web import signup

ROOT = build_info.PATH.parent.parent


# ---- 1. _build.json, atlas selfcheck, /health.build -------------------------------------------------------

def test_compute_reflects_the_running_code():
    manifest = build_info.compute()
    assert manifest["version"] == __import__("atlas").__version__
    assert manifest["wire"] == {"min": 0, "max": 1}
    assert "upgrade-journal-v1" in manifest["capabilities"] and "ext-v1" in manifest["capabilities"]


def test_check_is_clean_with_no_snapshot_and_flags_a_mismatch_naming_both_values(tmp_path):
    missing = tmp_path / "absent.json"
    assert build_info.check(missing) == []  # a source checkout: nothing to disagree with
    stale = tmp_path / "_build.json"
    stale.write_text(json.dumps({"version": "0.0.1", "wire": {"min": 0, "max": 99}, "capabilities": []}),
                     encoding="utf-8")
    problems = build_info.check(stale)
    assert any("0.0.1" in p and build_info.compute()["version"] in p for p in problems)
    assert any("99" in p for p in problems)
    assert any("capabilities" in p for p in problems)


def test_write_then_check_round_trips_clean(tmp_path):
    target = tmp_path / "_build.json"
    written = build_info.write(target)
    assert written == build_info.compute()
    assert build_info.check(target) == []


def test_health_endpoint_exposes_build(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/h.db",
                                         media_dir=str(tmp_path / "media"), media_cache_enabled=False))
    with TestClient(app) as client:
        body = client.get("/api/v2/health").json()
    assert body["build"]["version"] == build_info.compute()["version"]
    assert body["build"]["wire"] == {"min": 0, "max": 1}
    assert "upgrade-journal-v1" in body["build"]["capabilities"]


def test_cli_selfcheck_passes_clean_and_fails_on_a_mismatch(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(build_info, "PATH", tmp_path / "absent.json")
    assert cli.main(["selfcheck"]) == 0
    capsys.readouterr()
    stale = tmp_path / "_build.json"
    stale.write_text(json.dumps({"version": "0.0.1", "wire": {"min": 0, "max": 1},
                                 "capabilities": build_info.compute()["capabilities"]}), encoding="utf-8")
    monkeypatch.setattr(build_info, "PATH", stale)
    assert cli.main(["selfcheck"]) == 1
    assert "0.0.1" in capsys.readouterr().err


def test_a_stale_build_refuses_to_start(tmp_path, monkeypatch):
    """The same check main.create_app runs before any thread starts (C16 §10.4): a stale `_build.json` is fatal,
    not merely logged (unlike the point-of-no-return backup failure, which never blocks start)."""
    stale = tmp_path / "_build.json"
    stale.write_text(json.dumps({"version": "0.0.1", "wire": {"min": 0, "max": 1}, "capabilities": []}),
                     encoding="utf-8")
    monkeypatch.setattr(build_info, "PATH", stale)
    with pytest.raises(build_info.BuildMismatch):
        main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/s.db",
                                       media_dir=str(tmp_path / "media"), media_cache_enabled=False))


# ---- 2. the wire snapshot and its gates --------------------------------------------------------------------

def test_wire_snapshot_matches_the_running_code():
    from tools import release_gates, wire_snapshot

    schemas = wire_snapshot.compute_schemas()
    assert "feed.kind.obs" in schemas and "wire.header" in schemas and "feed.critical_members" in schemas
    assert wire_snapshot.fingerprint(schemas)["feed.kind.obs"] == wire_snapshot.digest(schemas["feed.kind.obs"])
    assert release_gates.wire_fingerprint_matches(ROOT, []) == []
    assert release_gates.wire_vectors_present(ROOT, []) == []


def test_wire_snapshot_check_fails_when_the_committed_file_is_stale(tmp_path):
    """A planted hit for G-W1/G-W5: the committed snapshot disagrees with the running code."""
    from tools import release_gates, wire_snapshot

    (tmp_path / "atlas").mkdir()
    (tmp_path / "atlas" / "feed_schema.py").write_text("", encoding="utf-8")
    assert release_gates.wire_fingerprint_matches(tmp_path, []) != []  # no snapshot at all: a hit, not a crash

    schemas = wire_snapshot.compute_schemas()
    target_dir = tmp_path / wire_snapshot.DIR
    target_dir.mkdir(parents=True)
    (tmp_path / wire_snapshot.SCHEMA_FILE).write_text(wire_snapshot._render(schemas), encoding="utf-8")
    (tmp_path / wire_snapshot.FINGERPRINT_FILE).write_text(
        wire_snapshot._render(wire_snapshot.fingerprint(schemas)), encoding="utf-8")
    assert release_gates.wire_fingerprint_matches(tmp_path, []) == []
    assert release_gates.wire_vectors_present(tmp_path, []) == []

    # Tamper with one golden vector without updating its fingerprint: G-W1 catches it.
    schemas["feed.kind.obs"]["fields"] = ["tampered"]
    (tmp_path / wire_snapshot.SCHEMA_FILE).write_text(wire_snapshot._render(schemas), encoding="utf-8")
    hits = release_gates.wire_fingerprint_matches(tmp_path, [])
    assert hits and any("schema.json" in h[0] for h in hits)

    # Drop a fingerprint entry without dropping its vector: G-W5 catches it even though G-W1 would too.
    (tmp_path / wire_snapshot.SCHEMA_FILE).write_text(wire_snapshot._render(schemas), encoding="utf-8")  # reset
    fp = wire_snapshot.fingerprint(wire_snapshot.compute_schemas())
    del fp["feed.kind.obs"]
    (tmp_path / wire_snapshot.FINGERPRINT_FILE).write_text(wire_snapshot._render(fp), encoding="utf-8")
    hits = release_gates.wire_vectors_present(tmp_path, [])
    assert any("feed.kind.obs" in h[2] for h in hits)


def test_wire_snapshot_gates_say_nothing_outside_the_atlas_tree(tmp_path):
    """A throwaway repository with no atlas/feed_schema.py (what test_release_gates.py's own fixture builds) is
    not flagged just for lacking a wire snapshot: these gates are not applicable there."""
    from tools import release_gates

    assert release_gates.wire_fingerprint_matches(tmp_path, []) == []
    assert release_gates.wire_vectors_present(tmp_path, []) == []
    assert release_gates.wire_min_change_announced(tmp_path, []) == []
    assert release_gates.critical_members_are_registered(tmp_path, []) == []


def test_g_w7_passes_with_no_releases_and_fails_on_an_unannounced_min_raise(tmp_path):
    from tools import release_gates

    (tmp_path / "atlas").mkdir()
    (tmp_path / "atlas" / "feed_schema.py").write_text("", encoding="utf-8")
    released_dir = tmp_path / "contracts" / "wire"
    released_dir.mkdir(parents=True)
    (released_dir / "released.json").write_text("[]", encoding="utf-8")
    assert release_gates.wire_min_change_announced(tmp_path, []) == []  # nothing released yet

    (released_dir / "released.json").write_text(json.dumps([{"wire": 1, "min": 0, "min_raised": ""}]),
                                                 encoding="utf-8")
    assert release_gates.wire_min_change_announced(tmp_path, []) == []  # agrees with the real WIRE_MIN (0)

    (released_dir / "released.json").write_text(json.dumps([{"wire": 1, "min": 99, "min_raised": ""}]),
                                                 encoding="utf-8")
    hits = release_gates.wire_min_change_announced(tmp_path, [])
    assert hits and "min_raised" in hits[0][2]

    (released_dir / "released.json").write_text(
        json.dumps([{"wire": 1, "min": 99, "min_raised": "security fix, announced in the 1.0 release notes"}]),
        encoding="utf-8")
    assert release_gates.wire_min_change_announced(tmp_path, []) == []  # a non-empty note excuses the mismatch


def test_the_committed_wire_snapshot_files_exist_and_agree_with_tools_wire_snapshot():
    from tools import wire_snapshot

    have_schema = json.loads((ROOT / wire_snapshot.SCHEMA_FILE).read_text(encoding="utf-8"))
    have_fp = json.loads((ROOT / wire_snapshot.FINGERPRINT_FILE).read_text(encoding="utf-8"))
    assert set(have_schema) == set(have_fp) == set(wire_snapshot.compute_schemas())


# ---- 3. the registry-level critical-member mark ------------------------------------------------------------

def test_critical_members_replaces_the_ext_only_hardcoding_with_the_same_behaviour():
    assert feed_schema.CRITICAL_MEMBERS == {("obs", "observation.payload.ext.release"): "dates-v1",
                                            ("obs", "observation.payload.ext.book"): "book-v1"}
    body = {"canonical_id": "atlas:" + "a" * 26, "media_type": "game",
            "observation": {"id": "o1", "source_kind": "metadata", "source": "igdb", "source_scope": "default",
                            "external_id": "1", "payload": {"title": "x", "ext": {"release": {}}}}}
    out = feed_schema.check_emit("obs", body)
    assert "dates-v1" in out["_requires"]


def test_a_newly_registered_critical_member_is_picked_up_generically(monkeypatch):
    """Not only `ext.*`: check_emit walks whatever CRITICAL_MEMBERS names, for any kind and any path."""
    monkeypatch.setitem(feed_schema.CRITICAL_MEMBERS, ("schema", "notice"), "ext-v1")
    out = feed_schema.check_emit("schema", {"notice": "x"})
    assert "ext-v1" in out["_requires"]
    out_without = feed_schema.check_emit("schema", {})
    assert "ext-v1" not in (out_without.get("_requires") or [])


def test_g_c1_passes_for_the_real_registry_and_fails_when_a_token_is_unregistered(monkeypatch):
    from tools import release_gates

    assert release_gates.critical_members_are_registered(ROOT, []) == []
    bad = dict(feed_schema.CRITICAL_MEMBERS)
    bad[("obs", "observation.payload.ext.made_up")] = "not-a-real-token"
    monkeypatch.setattr(feed_schema, "CRITICAL_MEMBERS", bad)
    hits = release_gates.critical_members_are_registered(ROOT, [])
    assert hits and all("not-a-real-token" in h[2] for h in hits)


# ---- 4. the upgrade-stage metric -----------------------------------------------------------------------------

def test_upgrade_metrics_render_the_current_stage(tmp_path):
    from atlas import auth

    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/m.db", metrics_enabled=True,
                                         media_dir=str(tmp_path / "media"), media_cache_enabled=False))
    with app.state.atlas.maker() as session:
        _, scraper = auth.create_token(session, "scraper", ["metrics"])
        session.commit()
        upgrade.open_journal(app.state.atlas.maker, app.state.atlas.ctx)
    with TestClient(app) as client:
        text = client.get("/metrics", headers={"Authorization": f"Bearer {scraper}"}).text
    assert "# TYPE atlas_upgrade_complete gauge" in text and "# TYPE atlas_upgrade_stage gauge" in text
    for name in upgrade.STAGES:
        assert f'atlas_upgrade_stage{{stage="{name}"}}' in text
    assert "atlas_upgrade_complete 1" in text  # a fresh database: nothing to finish


def test_upgrade_metrics_show_a_pending_stage(session, ctx):
    from atlas import observability

    journal = upgrade.fresh_journal(session, ctx)
    journal["stages"]["introduce_missing"] = upgrade.PENDING
    journal["cursors"]["introduce_missing"] = 7
    session.add(NodeState(key=upgrade.KEY, value=json.dumps(journal)))
    session.commit()
    lines = observability.Stats()._upgrade(None, session)
    text = "\n".join(lines)
    assert 'atlas_upgrade_stage{stage="introduce_missing"} 1' in text
    assert 'atlas_upgrade_stage{stage="replay"} 0' in text
    assert "atlas_upgrade_stage_left 7" in text
    assert "atlas_upgrade_complete 0" in text


def test_upgrade_metric_names_are_documented():
    doc = (ROOT / "docs" / "guide" / "observability.md").read_text(encoding="utf-8")
    for name in ("atlas_upgrade_complete", "atlas_upgrade_stage", "atlas_upgrade_stage_left",
                "atlas_upgrade_skipped_scope_ceiling"):
        assert f"`{name}`" in doc, name


# ---- 5. the Admin -> Peers upgrade banner --------------------------------------------------------------------

def test_admin_peers_shows_the_upgrade_banner_until_complete(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/p.db",
                                         media_dir=str(tmp_path / "media"), media_cache_enabled=False))
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as site:
        signup(site, "alice")
        # `create_app` already opened the journal at startup (a fresh database: every stage done/n/a); mutate the
        # existing row rather than inserting a second one (NodeState.key is unique).
        with app.state.atlas.maker() as session:
            row = session.get(NodeState, upgrade.KEY)
            journal = json.loads(row.value)
            journal["stages"]["introduce_missing"] = upgrade.PENDING
            row.value = json.dumps(journal)
            session.commit()
        page = site.get("/admin/peers").text
        assert 'data-banner="upgrade-journal"' in page and "introduce_missing" in page
        with app.state.atlas.maker() as session:
            row = session.get(NodeState, upgrade.KEY)
            row.value = json.dumps({**journal, "stages": {k: "done" for k in journal["stages"]}})
            session.commit()
        done_page = site.get("/admin/peers").text
        assert 'data-banner="upgrade-journal"' not in done_page


def test_admin_peers_shows_no_banner_with_no_journal(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/q.db",
                                         media_dir=str(tmp_path / "media"), media_cache_enabled=False))
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as site:
        signup(site, "alice")
        assert 'data-banner="upgrade-journal"' not in site.get("/admin/peers").text
