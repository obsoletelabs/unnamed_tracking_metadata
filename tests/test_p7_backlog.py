"""Phase 7 backlog (atlas-backlog lane): the evidence each open issue was reassessed against.

* #24  promote a mirror to authority: the `atlas recover-mirror` command end to end, including the follower cutover
       and the old trust pin refusing the new root (no silent split brain).
* #46  images: an assigned, cached image is viewable without a token through `public_url`; `media_url` needs one.
* #78  multiplayer sync: provider/curator multiplayer values reach the client API, the change feed and a mirror.
* #86  parent game: the issue's own examples expressed as C6 relations and read back from the record.
* #96  ID-linked resolution: a record renamed *and then* merged, request bounds and cross-type identities.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import backup, cli, custom, db, feed, main, media, relations, service, sync
from atlas.models import Artwork, Canonical, ChangeLog, FieldDef, ReplicatedChange, SyncFailure
from atlas.settings import Settings

from .conftest import batch, build_settings
from .test_media_facets import client_for, png, resolver  # noqa: F401
from .test_relay import node, publish, pull, transport
from .test_resolve_contract import observe, resolve
from .test_sync import pair  # noqa: F401
from .test_tracking_app_contract import atlas, seed  # noqa: F401

# ---- #46: viewing assigned images ------------------------------------------------------------------------------


def test_assigned_image_is_viewable_without_a_token_once_cached(atlas):  # noqa: F811
    cid = seed(atlas, ext="4601")
    assert atlas.put(f"/api/v2/games/{cid}/artwork", headers=atlas.write,
                     json={"items": [{"kind": "key_art", "url": "https://img.test/k.png"}]}).status_code == 200
    [pre] = atlas.get(f"/api/v2/games/{cid}/artwork", headers=atlas.read).json()["results"]
    assert pre["state"] == "not_fetched" and pre["public_url"] is None and pre["media_url"] is None

    media.cache_pending(atlas.state.maker, atlas.settings, client_for(lambda _r: httpx.Response(200, content=png())))
    [art] = atlas.get(f"/api/v2/games/{cid}/artwork", headers=atlas.read).json()["results"]
    digest = art["media_url"].rsplit("/", 1)[-1]
    assert art["public_url"] == f"/media/{digest}" and art["state"] == "cached"
    # what an <img src> does: no Authorization header
    assert atlas.get(art["media_url"]).status_code == 401  # the #46 symptom when a client used media_url
    assert atlas.get(art["media_url"], headers=atlas.read).status_code == 200
    got = atlas.get(art["public_url"])
    assert got.status_code == 200 and got.headers["content-type"] == "image/png" and got.content == png()


def test_public_url_disappears_when_the_source_stops_being_served(tmp_path, resolver):  # noqa: F811
    s = build_settings(database_url=f"sqlite:///{tmp_path}/r.db", media_dir=str(tmp_path / "m"),
                       media_cache_enabled=False, media_policy={"curator": {"fetch": "redirect"}})
    app = main.create_app(s)
    with TestClient(app) as c:
        st = app.state.atlas
        with st.maker() as session:
            from atlas import auth

            _, read = auth.create_token(session, "r", ["read"])
            [r] = service.ingest(session, st.ctx, batch("manual", [{"external_id": "x", "title": "Redirected"}],
                                                        kind="manual"), curator=True)
            session.add(Artwork(canonical_id=r.canonical_id, source="curator", kind="key_art",
                                url="https://img.test/a.png", blob_hash=media.store(s, png(), "png"),
                                content_type="image/png"))
            session.commit()
        [art] = c.get(f"/api/v2/games/{r.canonical_id}/artwork",
                      headers={"Authorization": f"Bearer {read}"}).json()["results"]
        assert art["serve"] == "redirect" and art["cached"] is True and art["public_url"] is None
        assert c.get(f"/media/{art['media_url'].rsplit('/', 1)[-1]}").status_code == 404


# ---- #78: multiplayer reaches clients and mirrors ---------------------------------------------------------------


def test_multiplayer_values_reach_the_record_and_the_client_change_feed(atlas):  # noqa: F811
    cid = seed(atlas, ext="7801", title="Overcooked 2")
    head = atlas.get("/api/v2/changes", params={"feed": "local"}, headers=atlas.read).json()
    put = atlas.put(f"/api/v2/games/{cid}/custom", headers=atlas.write,
                    json={"values": {"players_min": 1, "players_max": 4, "crossplay": "yes"}})
    assert put.status_code == 200, put.text
    game = atlas.get(f"/api/v2/games/{cid}", headers=atlas.read).json()
    # exactly the shape the metadata-atlas plugin's group_play() reads: ints and the shared crossplay vocabulary
    assert {k: game["custom"][k] for k in ("players_min", "players_max", "crossplay")} == {
        "players_min": 1, "players_max": 4, "crossplay": "yes"}
    page = atlas.get("/api/v2/changes", params={"feed": "local", "since": head["next_cursor"], "epoch": head["epoch"]},
                     headers=atlas.read).json()
    assert cid in {r["id"] for r in page["results"]}  # a client polling changes learns about it
    bad = atlas.put(f"/api/v2/games/{cid}/custom", headers=atlas.write, json={"values": {"crossplay": "maybe"}})
    assert bad.status_code in (400, 422)
    assert atlas.get(f"/api/v2/games/{cid}", headers=atlas.read).json()["custom"]["crossplay"] == "yes"


MP = {"players_min": 2, "players_max": 2, "crossplay": "partial"}


def _mp_record(maker, ctx, ext="mp", title="It Takes Two"):
    with maker() as s:
        custom.seed(s)
        custom.seed_multiplayer(s, ctx)  # as create_app does at start: no feed entry
        [r] = service.ingest(s, ctx, batch("curator", [{"external_id": ext, "title": title}], kind="metadata"),
                             curator=True)
        s.commit()
        return r.canonical_id


def _quarantined(maker):
    with maker() as s:
        return [(f.kind, f.reason) for f in s.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None)))]


def test_a_mirror_receives_the_authoritys_multiplayer_values(pair):  # noqa: F811
    """#78: before the fix the mirror quarantined this entry with `unknown field 'players_min' for game`."""
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    cid = _mp_record(a_maker, a_ctx)
    with a_maker() as s:
        custom.set_values(s, a_ctx, cid, "curator", MP, redistributable=True)
        s.commit()
        kinds = [e.kind for e in s.scalars(select(ChangeLog).order_by(ChangeLog.seq))]
    assert kinds.index("schema") < kinds.index("custom")  # the definitions travel before the first value
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    assert _quarantined(m_maker) == []
    with m_maker() as s:
        assert {k: custom.merged(s, m_ctx, cid).get(k) for k in MP} == MP
        assert {f.key for f in s.scalars(select(FieldDef).where(FieldDef.media_type == "game"))} >= set(MP)
    with a_maker() as s:  # published once, not on every value
        custom.set_values(s, a_ctx, cid, "curator", {"players_max": 3}, redistributable=True)
        s.commit()
        assert [e.kind for e in s.scalars(select(ChangeLog))].count("schema") == 1


def test_values_a_mirror_quarantined_before_the_fix_converge_after_it(pair):  # noqa: F811
    """An authority that already emitted multiplayer values without a schema entry (older release): the next value
    publishes the schema and the mirror's automatic retry applies what it had quarantined."""
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    cid = _mp_record(a_maker, a_ctx)
    with a_maker() as s:  # what the previous release signed: a value, no schema
        custom.set_values(s, a_ctx, cid, "curator", MP, redistributable=True, log=False)
        feed.emit(s, a_ctx, "custom", {"canonical_id": cid, "source": "curator", "values": MP, "clear": []})
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    assert _quarantined(m_maker) == [("custom", "apply_error")]  # the #78 symptom, reproduced

    other = _mp_record(a_maker, a_ctx, "mp2", "Overcooked")
    with a_maker() as s:
        custom.set_values(s, a_ctx, other, "curator", {"players_max": 4}, redistributable=True)
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        sync.retry_failures(s, m_ctx, due_only=False)  # what the scheduled retry does once the backoff passed
        s.commit()
    assert _quarantined(m_maker) == []
    with m_maker() as s:
        assert {k: custom.merged(s, m_ctx, cid).get(k) for k in MP} == MP
        assert custom.merged(s, m_ctx, other)["players_max"] == 4


# ---- #86: the issue's "parent game" examples ---------------------------------------------------------------------


def test_parent_game_examples_from_the_issue_are_relations(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/p.db", media_dir=str(tmp_path / "m"),
                                         media_cache_enabled=False))
    st = app.state.atlas
    with TestClient(app) as c:
        with st.maker() as s:
            from atlas import auth

            _, read = auth.create_token(s, "r", ["read"])
            ids = {}
            for title in ("Minecraft", "GregTech: New Horizons", "Factorio", "Space Exploration", "Space Age"):
                [r] = service.ingest(s, st.ctx, batch("manual", [{"external_id": title, "title": title}], kind="manual"),
                                     curator=True)
                ids[title] = r.canonical_id
            relations.add(s, st.ctx, ids["GregTech: New Horizons"], ids["Minecraft"], "modpack_of")
            relations.add(s, st.ctx, ids["Space Exploration"], ids["Factorio"], "mod_of")
            relations.add(s, st.ctx, ids["Space Age"], ids["Factorio"], "dlc_of")
            s.commit()
        h = {"Authorization": f"Bearer {read}"}
        gtnh = c.get(f"/api/v2/games/{ids['GregTech: New Horizons']}", headers=h).json()["relations"]
        assert [(p["id"], p["type"], p["title"]) for p in gtnh["parents"]] == [
            (ids["Minecraft"], "modpack_of", "Minecraft")]
        factorio = c.get(f"/api/v2/games/{ids['Factorio']}", headers=h).json()["relations"]
        assert sorted((ch["title"], ch["type"]) for ch in factorio["children"]) == [
            ("Space Age", "dlc_of"), ("Space Exploration", "mod_of")]
        assert factorio["parents"] == []


# ---- #96: renamed and merged, bounds, cross-type ---------------------------------------------------------------


def test_a_renamed_record_that_is_then_merged_resolves_by_its_old_ids(atlas):  # noqa: F811
    old = observe(atlas, {"source": "steamsync", "external_id": "9601", "title": "Codename Kite",
                          "release_date": "2021-01-01"})["canonical_id"]
    assert observe(atlas, {"source": "steamsync", "external_id": "9601", "title": "Kite Runner Tactics",
                           "release_date": "2021-01-01"})["canonical_id"] == old  # renamed in place
    survivor = observe(atlas, {"source": "igdbsync", "external_id": "9602", "title": "Kite Runner Tactics: Complete",
                               "release_date": "2021-01-01"})["canonical_id"]
    assert atlas.post(f"/api/v2/games/{old}/merge", json={"into": survivor}, headers=atlas.write).status_code == 200
    by_provider, by_atlas, by_old_title = resolve(
        atlas, {"title": "Codename Kite", "provider_ids": {"steamsync": "9601"}},
        {"namespace": "atlas", "external_id": old, "title": "Codename Kite"},
        {"title": "Codename Kite"})
    assert (by_provider["reason"], by_provider["canonical_id"]) == ("crosswalk", survivor)
    assert by_provider["game"]["title"] == "Kite Runner Tactics: Complete"
    assert (by_atlas["reason"], by_atlas["canonical_id"], by_atlas["redirected_from"]) == ("redirect", survivor, old)
    assert by_old_title["canonical_id"] is None  # an old title alone is never identity evidence
    record = atlas.get(f"/api/v2/games/{old}", headers=atlas.read).json()
    assert record["id"] == survivor and record["redirected_from"] == old


def test_resolve_is_bounded_and_read_only(atlas):  # noqa: F811
    limit = atlas.settings.max_resolve
    too_many = atlas.post("/api/v2/resolve", headers=atlas.read, json={"items": [{"title": "x"}] * (limit + 1)})
    assert too_many.status_code == 413
    assert atlas.post("/api/v2/resolve", json={"items": [{"title": "x"}]}).status_code == 401
    with atlas.state.maker() as s:
        before = s.scalar(select(Canonical.id).limit(1))
    resolve(atlas, {"namespace": "steamsync", "external_id": "never-seen", "title": "Never Seen"})
    with atlas.state.maker() as s:
        assert s.scalar(select(Canonical.id).limit(1)) == before  # nothing was created


def test_an_identity_of_another_media_type_is_never_assigned_or_shown(atlas):  # noqa: F811
    observe(atlas, {"source": "steamsync", "external_id": "9603", "title": "Shared Name", "release_date": "2020-01-01"})
    [got] = resolve(atlas, {"namespace": "steamsync", "external_id": "9603", "title": "Shared Name",
                            "media_type": "book"})
    assert got["reason"] == "type_mismatch" and got["canonical_id"] is None and got["assigned"] is False
    assert got["game"] is None and got["tier"] == "id"


# ---- #24: promote a mirror (recover-mirror) end to end ----------------------------------------------------------


def _env_for(monkeypatch, st, tmp_path):
    s = st.ctx.settings
    for key, value in {"ATLAS_DATABASE_URL": s.database_url, "ATLAS_ROLE": "mirror", "ATLAS_ENVIRONMENT": "dev",
                       "ATLAS_MEDIA_DIR": str(tmp_path / "cli-media"), "ATLAS_EMAIL_VERIFICATION": "optional"}.items():
        monkeypatch.setenv(key, value)
    for key in ("ATLAS_SECRET_KEY", "ATLAS_UPSTREAM_URL", "ATLAS_TRUSTED_KEYS"):
        monkeypatch.delenv(key, raising=False)
    from atlas.settings import get_settings

    get_settings.cache_clear()


def test_recover_mirror_cli_fences_builds_and_followers_cut_over_to_the_new_root(tmp_path, monkeypatch, capsys):
    authority, mirror = node(tmp_path, "a", "authority"), node(tmp_path, "m", "mirror")
    cid = publish(authority[2], "Survives The Outage", "dr_note")
    pull(mirror[2], authority)
    old_root = authority[2].ctx.identity
    out = tmp_path / "candidate.tar.gz"
    _env_for(monkeypatch, mirror[2], tmp_path)
    args = ["recover-mirror", "--from-authority", old_root.node_id,
            "--confirm-mirror", mirror[2].ctx.identity.node_id, "--output", str(out)]

    assert cli.main(args) == 2 and not out.exists()  # no fence assertion, nothing built
    assert "--old-authority-fenced" in capsys.readouterr().err
    assert cli.main([*args, "--old-authority-fenced"]) == 0 and out.exists()
    new_node_id = __import__("json").loads(capsys.readouterr().out)["node_id"]
    assert new_node_id not in (old_root.node_id, mirror[2].ctx.identity.node_id)
    assert cli.main([*args, "--old-authority-fenced"]) == 1  # an existing archive is never overwritten
    from atlas.settings import get_settings

    get_settings.cache_clear()

    # restore the candidate as the replacement authority
    url = f"sqlite:///{tmp_path}/replacement.db"
    target = Settings(_env_file=None, database_url=url, media_dir=str(tmp_path / "rm"))
    db.upgrade(url)
    engine = db.make_engine(url)
    try:
        backup.restore(target, engine, out)
    finally:
        engine.dispose()
    app = main.create_app(build_settings(database_url=url, role="authority", media_dir=str(tmp_path / "rm"),
                                         media_cache_enabled=False))
    st = app.state.atlas
    with st.maker() as s:
        from atlas import auth

        _, token = auth.create_token(s, "sync", ["read", "sync"])
        s.commit()
    replacement = (app, TestClient(app), st, token)
    assert st.ctx.identity.node_id == new_node_id

    # the replacement accepts a write: it is signed by the NEW root only
    after = publish(st, "Written After Cutover", "post_note")

    # a follower still pinned to the OLD root keeps only what the old root really signed (the replacement relays
    # those original entries verbatim as evidence); it never accepts anything the new root signs, so a forgotten
    # follower cannot silently mix two writers (#24 split brain)
    stale = node(tmp_path, "stale", "mirror")
    with transport(replacement[1]) as http:
        sync.pull_all(stale[2].maker, stale[2].ctx, http, "http://upstream", token,
                      trusted={old_root.node_id: old_root.public_b64})
    with stale[2].maker() as s:
        assert s.get(Canonical, after) is None
        assert {r.node_id for r in s.scalars(select(ReplicatedChange))} <= {old_root.node_id}

    # a follower that re-pinned to the new root converges: recovered history under the same ids, then new writes
    fresh = node(tmp_path, "fresh", "mirror")
    assert pull(fresh[2], replacement) > 0
    with fresh[2].maker() as s:
        assert s.get(Canonical, cid).view["title"] == "Survives The Outage"
        assert s.get(Canonical, after).view["title"] == "Written After Cutover"


# ---- container base image (Trivy: zlib1g HIGH CVEs in the Debian base) ---------------------------------------


def test_container_base_is_digest_pinned_and_takes_debian_security_updates():
    from pathlib import Path

    text = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
    stages = [line for line in text.splitlines() if line.startswith("FROM ")]
    assert len(stages) == 2 and stages[0].endswith(" AS build")
    bases = {line.split()[1] for line in stages}
    assert len(bases) == 1  # both stages on the same base: venv scripts embed the interpreter path
    [base] = bases
    # immutable and literal (no ARG indirection), so Dependabot's docker updater sees and bumps it
    assert base.startswith("python:3.12-slim@sha256:") and len(base.rsplit(":", 1)[-1]) == 64
    runtime = text.split(stages[1] + "\n", 1)[1]
    assert "apt-get upgrade -y" in runtime and "rm -rf /var/lib/apt/lists/*" in runtime
    assert runtime.index("apt-get upgrade") < runtime.index("USER atlas")  # upgraded as root, run as atlas


# ---- #19: `atlas check` (read-only consistency checks) -----------------------------------------------------------


def test_atlas_check_is_clean_on_a_healthy_node_and_reports_seeded_problems(tmp_path, monkeypatch, capsys):
    from atlas import consistency
    from atlas.models import Crosswalk

    st = node(tmp_path, "chk", "authority")[2]
    s_media = st.ctx.settings
    with st.maker() as s:
        [a] = service.ingest(s, st.ctx, batch("steamsync", [{"external_id": "1", "title": "Kept"}], kind="metadata"),
                             curator=True)
        [b] = service.ingest(s, st.ctx, batch("steamsync", [{"external_id": "2", "title": "Merged Away"}],
                                              kind="metadata"), curator=True)
        s.commit()
    with st.maker() as s:
        assert consistency.run(s, s_media)["ok"] is True
        clean_counts = {n: r["count"] for n, r in consistency.run(s, s_media)["checks"].items()}
    assert set(clean_counts.values()) == {0}

    with st.maker() as s:  # damage the kind of state the checks exist for, behind the services' back
        s.get(Canonical, b.canonical_id).merged_into = a.canonical_id
        s.add(Artwork(canonical_id=a.canonical_id, source="curator", kind="key_art", url="https://img.test/x.png",
                      blob_hash="ab" * 32, content_type="image/png"))
        s.commit()
    orphan = media.store(s_media, png(), "png")
    with st.maker() as s:
        before = {r.namespace + r.external_id: r.canonical_id for r in s.scalars(select(Crosswalk))}
        report = consistency.run(s, s_media, sample=5)
        assert report["ok"] is False
        c = report["checks"]
        assert c["crosswalk_to_merged_record"]["count"] == 1 and c["crosswalk_to_merged_record"]["examples"] == [
            "steamsync:2"]
        assert c["artwork_cached_file_missing"]["count"] == 1 and "hint" in c["artwork_cached_file_missing"]
        assert c["media_file_unreferenced"]["examples"] == [f"{orphan}.png"]
        assert c["merge_chain_cycle_or_too_long"]["count"] == 0
        after = {r.namespace + r.external_id: r.canonical_id for r in s.scalars(select(Crosswalk))}
    assert after == before  # read-only

    for key, value in {"ATLAS_DATABASE_URL": s_media.database_url, "ATLAS_ROLE": "authority",
                       "ATLAS_ENVIRONMENT": "dev", "ATLAS_MEDIA_DIR": s_media.media_dir}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ATLAS_SECRET_KEY", raising=False)
    from atlas.settings import get_settings

    get_settings.cache_clear()
    try:
        assert cli.main(["check"]) == 1
        text = capsys.readouterr().out
        assert "FOUND crosswalk_to_merged_record: 1" in text and "note  media_file_unreferenced: 1" in text
        assert cli.main(["check", "--only", "merge_target_missing", "--json"]) == 0
        assert '"ok": true' in capsys.readouterr().out
        assert cli.main(["check", "--only", "nope"]) == 2
    finally:
        get_settings.cache_clear()
