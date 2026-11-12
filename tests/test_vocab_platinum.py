"""Shared vocabularies, the platinum tier and crossplay (CONTRACTS-v1 C7, atlas#98).

Platinum validation, editor choice and bulk import follow the approach of the external contribution
obsoletelabs/unnamed_tracking_metadata#110 (by hotzings); here they are also gated by the federation emit
protocol (C1.6, H13) and registered as protocol 1.2 so an older follower parks a platinum list.

Every scenario asserts persistent state: achievement rows, proposals, signed feed entries, retained
entries, `sync_failure` rows and custom values in the nodes' databases, not only HTTP answers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest
from sqlalchemy import select

from atlas import custom, facets, feed_schema, sync, vocab
from atlas.models import (
    Achievement,
    ChangeLog,
    CustomValue,
    FieldDef,
    ReplicatedChange,
    SyncFailure,
)
from atlas.service import AtlasError
from atlas.signing import body_hash
from tools import vocab_check

from .test_sync_compat import Node, _admin, pull
from .test_web import _ach_form, signup, token

ROOT = Path(__file__).resolve().parents[1]
VOCAB_FILE = ROOT / vocab.FILE


# ---- the shared file (C7.4) ------------------------------------------------------------------------------

def test_vocab_file_is_generated_from_the_registry_and_its_digest_matches():
    text = VOCAB_FILE.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert json.loads(text) == vocab.export()
    assert text == vocab.render(vocab.export()) == vocab_check.render(vocab.export())
    side = VOCAB_FILE.with_name(VOCAB_FILE.name + ".sha256").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert side == f"{vocab.digest(text)}  vocab-v1.json\n"
    assert vocab_check.cmd_check(ROOT) == 0


def test_the_registry_is_what_atlas_actually_validates_and_federates(session):
    data = vocab.export()
    assert data["version"] == 1
    assert data["achievement_tier"] == ["bronze", "silver", "gold", "platinum"]
    assert data["crossplay"] == ["yes", "partial", "no"] and vocab.NOT_APPLICABLE not in data["crossplay"]
    assert facets.ACH_TIERS == tuple(data["achievement_tier"]) and facets.ACH_KINDS == tuple(data["achievement_kind"])
    assert feed_schema.vocabularies()["achievement_tier"] == set(data["achievement_tier"])
    assert feed_schema.vocabularies()["achievement_kind"] == set(data["achievement_kind"])
    assert data["feed_kinds"] == list(feed_schema.FEED_KINDS)
    [crossplay] = [f for f in custom.MULTIPLAYER_FIELDS if f[0] == "crossplay"]
    assert crossplay[3] == data["crossplay"]
    # the database column holds every tier (V18): String(8), and "platinum" is exactly 8 characters
    assert max(map(len, vocab.ACHIEVEMENT_TIERS)) <= Achievement.__table__.c.tier.type.length == 8


def test_compare_reports_only_real_disagreements():
    atlas = vocab.export()
    host = {k: v for k, v in atlas.items() if k != "feed_kinds"}
    host = {**host, "crossplay": ["no", "partial", "yes"], "store_alias": ["steam"]}  # order and extra keys are fine
    assert vocab_check.compare({"atlas": atlas, "host": host}) == []
    old_plugin = {**host, "achievement_tier": ["bronze", "silver", "gold"]}
    [problem] = vocab_check.compare({"atlas": atlas, "plugin": old_plugin})
    assert problem.startswith("achievement_tier: only in atlas: ['platinum']")
    assert vocab_check.compare({"atlas": atlas, "v2": {**host, "version": 2}})[0].startswith("version differs")
    assert vocab_check.shape_errors({"version": 1, "achievement_tier": ["Platinum"], "achievement_kind": [],
                                     "crossplay": ["yes", "yes"]}) == [
        "achievement_tier: values must be lowercase tokens of at most 40 characters: ['Platinum']",
        "crossplay: repeated values"]


def test_the_script_runs_in_another_repository_without_atlas(tmp_path):
    """`verify` and `compare` use the standard library only: copied alone and run without site-packages
    (`-I -S`, so the installed `atlas` package is not importable), they still work."""
    script = tmp_path / "vocab_check.py"
    shutil.copy(ROOT / "tools" / "vocab_check.py", script)
    ours = tmp_path / "vocab-v1.json"
    shutil.copy(VOCAB_FILE, ours)
    shutil.copy(VOCAB_FILE.with_name("vocab-v1.json.sha256"), tmp_path / "vocab-v1.json.sha256")
    other = tmp_path / "plugin.json"
    other.write_text(json.dumps({"version": 1, "achievement_tier": ["bronze", "silver", "gold"],
                                 "achievement_kind": ["progression", "missable", "win_condition"],
                                 "crossplay": ["yes", "partial", "no"]}), encoding="utf-8")

    def run(*args):
        return subprocess.run([sys.executable, "-I", "-S", str(script), *args], cwd=tmp_path, capture_output=True,
                              text=True, timeout=60)

    ok = run("verify", str(ours))
    assert ok.returncode == 0 and "ok (" in ok.stdout, ok.stderr
    same = run("compare", str(ours), str(ours))
    assert same.returncode == 0 and "agree" in same.stdout
    differs = run("compare", str(ours), str(other))
    assert differs.returncode == 1 and "achievement_tier: only in" in differs.stdout and "platinum" in differs.stdout
    ours.write_text(ours.read_text(encoding="utf-8").replace('"no"', '"nope"'), encoding="utf-8")
    tampered = run("verify", str(ours))
    assert tampered.returncode == 1 and "hashes to" in tampered.stdout
    assert run("check").returncode != 0  # needs Atlas, which is not importable here


# ---- validation (C7.1) -----------------------------------------------------------------------------------

def test_platinum_is_a_tier_and_other_spellings_are_not():
    [item] = facets.clean_achievements([{"name": "Completionist", "tier": "platinum"}])
    assert item["tier"] == "platinum"
    for bad in ("Platinum", "diamond", "not_applicable", "plat", 8):
        with pytest.raises(AtlasError, match="tier must be one of"):
            facets.clean_achievements([{"name": "x", "tier": bad}])


def test_a_local_node_stores_and_serves_platinum_and_allows_more_than_one(tmp_path):
    local = Node(tmp_path, "local", "local")
    cid = local.publish("Long Haul")
    admin = {"Authorization": f"Bearer {_admin(local)}"}
    items = [{"name": "Finish", "tier": "gold"}, {"name": "Everything", "tier": "platinum"},
             {"name": "Everything, again", "tier": "platinum"}]  # C7.1: not limited to one per game
    r = local.client.put(f"/api/v2/games/{cid}/achievements", headers=admin, json={"items": items})
    assert r.status_code == 200, r.text
    with local.st.maker() as session:
        stored = {a.name: a.tier for a in session.scalars(select(Achievement).where(Achievement.canonical_id == cid))}
        assert stored == {"Finish": "gold", "Everything": "platinum", "Everything, again": "platinum"}
        assert session.scalars(select(ChangeLog).where(ChangeLog.kind == "achievements")).first() is None  # local
    out = local.client.get(f"/api/v2/games/{cid}/achievements", headers=admin).json()["results"]
    assert sorted(r["tier"] for r in out) == ["gold", "platinum", "platinum"]
    page = local.client.get(f"/game/{cid}").text
    assert '<span class="badge tier-platinum">platinum</span>' in page


# ---- the emission gate (C1.6, H13) -----------------------------------------------------------------------

def test_raising_emit_to_1_2_signs_platinum_with_its_requires_token(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.2")
    cid = authority.publish("Unlocked")
    admin = {"Authorization": f"Bearer {_admin(authority)}"}
    plain = [{"name": "Start", "tier": "bronze"}]
    assert authority.client.put(f"/api/v2/games/{cid}/achievements", headers=admin,
                                json={"items": plain}).status_code == 200
    r = authority.client.put(f"/api/v2/games/{cid}/achievements", headers=admin,
                             json={"items": [*plain, {"name": "All of it", "tier": "platinum"}]})
    assert r.status_code == 200, r.text
    with authority.st.maker() as session:
        first, second = session.scalars(select(ChangeLog).where(ChangeLog.kind == "achievements")
                                        .order_by(ChangeLog.seq)).all()
        assert "_requires" not in first.body  # a list without platinum is a version-1 body, signed as before
        assert second.body["_requires"] == ["tier-platinum"] and "_v" not in second.body
        assert [i["tier"] for i in second.body["items"]] == ["bronze", "platinum"]
        assert second.body_hash == body_hash(second.body)  # the token is inside the signed body
        assert session.scalars(select(Achievement.tier).where(Achievement.name == "All of it")).one() == "platinum"


def test_check_emit_adds_the_token_once_and_never_changes_the_callers_body():
    body = {"canonical_id": "atlas:x", "source": "curator",
            "items": [{"name": "A", "tier": "platinum"}, {"name": "B", "tier": "platinum"}]}
    signed = feed_schema.check_emit("achievements", body)
    assert signed["_requires"] == ["tier-platinum"] and "_requires" not in body
    again = feed_schema.check_emit("achievements", {**body, "_requires": ["tier-platinum"]})
    assert again["_requires"] == ["tier-platinum"]
    gold = {"canonical_id": "atlas:x", "source": "curator", "items": [{"name": "A", "tier": "gold"}]}
    assert feed_schema.check_emit("achievements", gold) == {**gold, "_wire": 1}


# ---- federation: 1.2 applies, 1.1 parks, upgrade converges (C7.5) ----------------------------------------

@pytest.fixture
def platinum_net(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.2")
    mirror = Node(tmp_path, "mirror", "mirror")
    cid = authority.publish("Trophy Hall")
    admin = {"Authorization": f"Bearer {_admin(authority)}"}

    def put(items):
        r = authority.client.put(f"/api/v2/games/{cid}/achievements", headers=admin, json={"items": items})
        assert r.status_code == 200, r.text
        return authority.head()
    return authority, mirror, cid, put


def _rows(node, cid):
    with node.st.maker() as session:
        return {(a.name, a.tier, a.key) for a in session.scalars(select(Achievement).where(
            Achievement.canonical_id == cid))}


def test_a_1_2_follower_applies_a_platinum_list(platinum_net):
    authority, mirror, cid, put = platinum_net
    put([{"name": "Start", "tier": "bronze"}, {"name": "Everything", "tier": "platinum"}])
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    assert _rows(mirror, cid) == _rows(authority, cid)  # same names, tiers and keys
    assert {t for _, t, _ in _rows(mirror, cid)} == {"bronze", "platinum"}
    status = mirror.heads(authority)
    assert status["state"] == "in_sync" and status["applied_head"] == authority.head()


def test_a_1_1_follower_parks_the_platinum_list_keeps_the_old_one_and_converges_after_upgrade(platinum_net, monkeypatch):
    authority, mirror, cid, put = platinum_net
    put([{"name": "Start", "tier": "bronze"}])
    platinum_seq = put([{"name": "Start", "tier": "bronze"}, {"name": "Everything", "tier": "platinum"}])
    authority.publish("Later Record")  # causally after the platinum list

    # Play the 1.1 release (see tests/test_sync_compat.py::release_1_1) for the follower only.
    old = {k: (v - {"platinum"} if k == "achievement_tier" else v) for k, v in feed_schema.vocabularies().items()}
    monkeypatch.setattr(feed_schema, "vocabularies", lambda: old)
    monkeypatch.setattr(feed_schema, "VOCAB_TOKENS", ())
    pull(mirror, authority)

    assert mirror.failures(authority) == [(platinum_seq, "achievements", "parked", "unsupported_capability")]
    with mirror.st.maker() as session:
        failure = session.get(SyncFailure, (authority.node_id, platinum_seq))
        assert failure.detail["requires"] == ["tier-platinum"]
        retained = {r.seq for r in session.scalars(select(ReplicatedChange))}
    assert retained == set(range(1, authority.head() + 1))  # later entries verified and retained, not dropped
    status = mirror.heads(authority)
    assert status["state"] == "parked" and status["applied_head"] == platinum_seq - 1
    assert status["parked"]["first_kind"] == "achievements" and status["parked"]["first_reason"] == "unsupported_capability"
    # Not a silent drop and not a partial list: the follower still shows the complete previous list.
    assert {(n, t) for n, t, _ in _rows(mirror, cid)} == {("Start", "bronze")}
    assert "Later Record" not in mirror.titles()  # causally later: held back

    monkeypatch.undo()  # the follower is upgraded in place to this release
    with mirror.st.maker() as session:
        result = sync.replay(session, mirror.st.ctx)
        session.commit()
    assert result[authority.node_id]["state"] == "in_sync", result
    assert mirror.failures(authority) == []
    assert _rows(mirror, cid) == _rows(authority, cid)
    assert "Later Record" in mirror.titles()


def test_a_relay_at_1_1_still_serves_the_platinum_entry_byte_for_byte(platinum_net, monkeypatch):
    authority, mirror, _, put = platinum_net
    seq = put([{"name": "Everything", "tier": "platinum"}])
    monkeypatch.setattr(feed_schema, "VOCAB_TOKENS", ())
    pull(mirror, authority)
    assert mirror.failures(authority)[0][3] == "unsupported_capability"
    assert mirror.retained(authority)[seq] == authority.own_feed()[seq]
    served = mirror.client.get("/api/v2/sync/changes", params={"node_id": authority.node_id, "since": seq - 1},
                               headers={"Authorization": f"Bearer {mirror.token}"}).json()
    [entry] = [e for e in served["entries"] if e["seq"] == seq]
    assert entry["body"]["_requires"] == ["tier-platinum"]


# ---- website: editor choices, warnings and the gate ------------------------------------------------------

def _site(tmp_path, role):
    from fastapi.testclient import TestClient

    from atlas import main, service

    from .conftest import batch, build_settings
    from .test_web import _wait_for_startup_replay

    s = build_settings(role=role, database_url=f"sqlite:///{tmp_path}/{role}.db", media_dir=str(tmp_path / "media"),
                       media_cache_enabled=False, public_url="http://localhost:8000")
    app = main.create_app(s)
    st = app.state.atlas
    with st.maker() as sess:
        [r] = service.ingest(sess, st.ctx, batch("igdb", [{"external_id": "9", "title": "Solo Lighthouse"}]),
                             system=True)
        sess.commit()
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        _wait_for_startup_replay(st)
        c.gid, c.st = r.canonical_id, st
        yield c


@pytest.fixture
def local_site(tmp_path):
    yield from _site(tmp_path, "local")


@pytest.fixture
def authority_site(tmp_path):
    """An authority (wire 1: platinum is signed, no gate)."""
    yield from _site(tmp_path, "authority")


def test_editor_offers_platinum_warns_about_a_second_one_and_saves(local_site):
    site = local_site
    signup(site, "alice")  # the first account administers this node: its proposals apply at once
    url = f"/game/{site.gid}/achievements/edit"
    page = site.get(url).text
    assert '<option value="platinum" >Platinum</option>' in page and "needs federation protocol" not in page
    t = token(site, url)
    rows = {"a": {"name": "Everything", "tier": "platinum"}, "b": {"name": "Everything twice", "tier": "platinum"}}
    preview = site.post(url, data=_ach_form(t, rows))
    text = unescape(preview.text)
    assert preview.status_code == 200 and "Another row is already platinum." in text and 'value="submit"' in text
    assert text.count("Another row is already platinum.") == 1  # on the second row only; a warning, not an error
    done = site.post(url, data=_ach_form(t, rows, action="submit"))
    assert done.status_code == 303 and "m=applied" in done.headers["location"]
    with site.st.maker() as s:
        assert sorted(s.scalars(select(Achievement.tier))) == ["platinum", "platinum"]
    bulk = site.post(f"/game/{site.gid}/achievements",
                     data={"csrf": token(site, f"/game/{site.gid}"), "lines": "Trilogy | All three | platinum | 100"})
    assert bulk.status_code == 303
    with site.st.maker() as s:
        assert s.scalars(select(Achievement).where(Achievement.name == "Trilogy")).one().tier == "platinum"


@pytest.mark.parametrize(("stored", "players_max", "shown"), [
    ("yes", None, "yes"), ("partial", 1, "partial"), ("no", 4, "no"), ("no", 1, "no"),
    (None, 1, "not_applicable"), (None, 1.0, "not_applicable"), (None, None, None), (None, 2, None),
    (None, True, None), (None, "1", None), ("maybe", None, None),
])
def test_crossplay_projection(stored, players_max, shown):
    assert vocab.crossplay_projection(stored, players_max) == shown


def test_not_applicable_is_never_stored_even_if_an_administrator_adds_it_as_a_choice(local_site):
    site = local_site
    with site.st.maker() as s:
        field = s.scalars(select(FieldDef).where(FieldDef.media_type == "game", FieldDef.key == "crossplay")).one()
        assert field.choices == ["yes", "partial", "no"]
        field.choices = [*field.choices, "not_applicable"]
        s.commit()
    with site.st.maker() as s:
        with pytest.raises(AtlasError, match="never stored"):
            custom.clean_values(s, "game", {"crossplay": "not_applicable"}, editable_only=False)
        assert custom.clean_values(s, "game", {"crossplay": "partial"}, editable_only=False) == {"crossplay": "partial"}
    admin = {"Authorization": f"Bearer {_admin_token(site)}"}
    r = site.put(f"/api/v2/games/{site.gid}/custom", headers=admin, json={"values": {"crossplay": "not_applicable"}})
    assert r.status_code == 400
    with site.st.maker() as s:
        assert s.scalars(select(CustomValue).where(CustomValue.key == "crossplay")).first() is None


def test_a_single_player_game_shows_single_player_but_the_api_never_sends_it(local_site):
    site = local_site
    admin = {"Authorization": f"Bearer {_admin_token(site)}"}
    assert site.put(f"/api/v2/games/{site.gid}/custom", headers=admin,
                    json={"values": {"players_max": 1}}).status_code == 200
    page = site.get(f"/game/{site.gid}").text
    assert '<span data-derived="crossplay">Single-player</span>' in page
    api = site.get(f"/api/v2/games/{site.gid}", headers=admin).json()
    assert "crossplay" not in api["custom"] and api["custom"]["players_max"] == 1  # derived, never sent
    with site.st.maker() as s:
        assert s.scalars(select(CustomValue).where(CustomValue.key == "crossplay")).first() is None
    # A stated answer always wins over the derived one, and "no" is shown as "no".
    assert site.put(f"/api/v2/games/{site.gid}/custom", headers=admin,
                    json={"values": {"crossplay": "no"}}).status_code == 200
    page = site.get(f"/game/{site.gid}").text
    assert "data-derived" not in page and "<dt>Crossplay</dt><dd>no</dd>" in page


def _admin_token(site):
    from atlas import auth

    with site.st.maker() as s:
        _, secret = auth.create_token(s, "vocab-admin", ["read", "curate", "admin"])
        s.commit()
    return secret
