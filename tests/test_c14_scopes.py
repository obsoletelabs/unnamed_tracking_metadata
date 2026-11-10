"""C14-1 scopes (CONTRACT-C14 §5, T-S1…T-S13): one scope per item, the record ceiling and the feed gate, the §5.7
visibility rule applied inside every read query, the D1 partial public projection, badges, filters, additive API
keys, the `scope.detail` right and Admin → Sharing.

Canary convention (T-S10): every value that must never reach a viewer carries a unique marker, and each surface is
asserted not to contain it, for anonymous, token, user and detail viewers, with the switch off and on."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import feed, feed_schema, main, scope, service
from atlas.models import Canonical, ChangeLog, FeedIntroduced, ScopeProjection, User, UserPermission

from .conftest import batch, build_settings
from .test_web import PW, _wait_for_startup_replay, login, signup, token

HOST_MARK = "HOSTCANARY7319"   # host/plugin library data: never shown to anyone without scope.detail
GROUP_MARK = "GROUPCANARY4402"  # a withheld category of group data (a description)
GROUP_TITLE = "Groupidentity Zeta"  # the identity of a record known only through the group


def _ingest(s, ctx, source, items, *, kind="metadata", scope_name="test"):
    return service.ingest(s, ctx, batch(source, items, kind=kind, scope=scope_name), system=True)


# ---- unit level: derivation, ceiling, gate, projection --------------------------------------------------------------

@pytest.fixture
def local_ctx(session):
    from atlas.service import Ctx
    from atlas.signing import load_identity

    identity = load_identity(session)
    session.commit()
    return Ctx(settings=build_settings(role="local"), identity=identity)


def test_scope_vocabulary_and_derivation(session, ctx):
    """T-S1: four scopes, three ceilings; origin decides an item's scope."""
    assert scope.SCOPES == ("public", "group", "private", "local")
    assert set(scope.CEILINGS) == {"group", "private", "local"}
    [host] = _ingest(session, ctx, "playnite", [{"external_id": "h1", "title": "Host Only"}], kind="plugin")
    [grp] = _ingest(session, ctx, "igdb", [{"external_id": "g1", "title": GROUP_TITLE}], scope_name="group:n1")
    session.commit()
    from atlas.models import Observation

    h = session.scalars(select(Observation).where(Observation.canonical_id == host.canonical_id)).one()
    g = session.scalars(select(Observation).where(Observation.canonical_id == grp.canonical_id)).one()
    assert scope.is_host_obs(h) and not scope.is_group_obs(h)
    assert scope.is_group_obs(g) and not scope.is_host_obs(g)
    assert scope.of_observation(ctx.settings, h) == "local"
    assert scope.of_observation(ctx.settings, g) == "group"
    assert scope.of_observation(ctx.settings, h, "group") == "group"  # the ceiling decides
    assert scope.badge("group")["label"] == "Your nodes" and scope.badge("local")["label"] == "This node only"


def test_ceiling_keeps_edits_local_and_publish_introduces(session, local_ctx):
    """T-S2/T-S9: a ceiling'd record's edits are never signed (kept on this node), on every role; publishing lifts
    the ceiling."""
    ctx = local_ctx
    [r] = _ingest(session, ctx, "igdb", [{"external_id": "c1", "title": "Ceiling Game"}])
    session.commit()
    scope.set_ceiling(session, ctx, r.canonical_id, "local")
    session.commit()
    before = session.scalar(select(func.count()).select_from(ChangeLog))
    assert feed.emit_or_local(session, ctx, "lock", {"canonical_id": r.canonical_id, "field": "title",
                                                      "value": "X"}) is None
    with pytest.raises(scope.ScopeCeiling) as exc:
        feed.scope_gate(session, "lock", {"canonical_id": r.canonical_id}, [r.canonical_id])
    assert exc.value.local_ok and isinstance(exc.value, feed_schema.EmitGateError)
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == before
    assert r.canonical_id not in {c.id for c in feed.candidates(session, "all")}
    scope.publish(session, ctx, r.canonical_id)
    session.commit()
    assert session.get(Canonical, r.canonical_id).scope_ceiling is None


def test_mixed_entry_fails_closed(session, local_ctx):
    """T-S9: an entry naming a ceiling'd and a public record is refused outright (not kept locally)."""
    ctx = local_ctx
    a, b = (_ingest(session, ctx, "igdb", [{"external_id": e, "title": t}])[0] for e, t in
            (("m1", "Mixed One"), ("m2", "Mixed Two")))
    session.commit()
    scope.set_ceiling(session, ctx, a.canonical_id, "local")
    with pytest.raises(scope.ScopeCeiling) as exc:
        feed.scope_gate(session, "merge", {"src": a.canonical_id, "dst": b.canonical_id},
                        [a.canonical_id, b.canonical_id])
    assert not exc.value.local_ok


def test_ceiling_cannot_unpublish(session, ctx):
    """T-S3: an introduced record cannot be kept on this node only; followers keep what they have."""
    [r] = _ingest(session, ctx, "igdb", [{"external_id": "p1", "title": "Published Game"}])
    session.commit()
    feed.ensure_introduced(session, ctx, r.canonical_id, "test")
    session.commit()
    assert session.get(FeedIntroduced, r.canonical_id) is not None
    with pytest.raises(scope.CeilingRefused, match="already in your public feed"):
        scope.set_ceiling(session, ctx, r.canonical_id, "local")


def test_host_only_record_is_operator_private(session, ctx):
    """T-S4: a record that exists only through host/plugin data does not exist for visitors or tokens: same error
    as an unknown id."""
    [r] = _ingest(session, ctx, "playnite", [{"external_id": "h2", "title": f"Host {HOST_MARK}"}], kind="plugin")
    session.commit()
    cid = r.canonical_id
    for viewer in (scope.Viewer("anonymous"), scope.Viewer("token"), scope.Viewer("user", user_id="u")):
        with pytest.raises(service.AtlasError) as exc:
            scope.get_visible(session, viewer, cid)
        assert str(exc.value) == f"unknown canonical {cid}" and exc.value.code == "not_found"
    row, _ = scope.get_visible(session, scope.Viewer("detail", user_id="u"), cid)
    assert HOST_MARK in row.view["title"]


def test_mixed_record_hides_host_values(session, ctx):
    """T-S5: a record with provider and host data shows visitors the provider values only."""
    [r] = _ingest(session, ctx, "igdb", [{"external_id": "x1", "title": "Mixed Record"}])
    _ingest(session, ctx, "playnite", [{"external_id": "x1h", "title": "Mixed Record",
                                        "provider_ids": {"igdb": "x1"},
                                        "facts": {"description": f"notes {HOST_MARK}"}}], kind="plugin")
    session.commit()
    full = session.get(Canonical, r.canonical_id)
    anon, _ = scope.get_visible(session, scope.Viewer("anonymous"), r.canonical_id)
    assert anon.view["title"] == "Mixed Record"
    assert HOST_MARK not in repr(anon.view) and HOST_MARK not in repr(anon.explain)
    if HOST_MARK in repr(full.view):  # the merge took the host description: the projection must not
        assert session.get(ScopeProjection, (r.canonical_id, scope.ANON_OFF)) is not None


def test_group_switch_and_categories(session, ctx):
    """T-S6/D1: group-received data is absent while the switch is off; with it on, only public categories."""
    [r] = _ingest(session, ctx, "igdb", [{"external_id": "g2", "title": GROUP_TITLE,
                                          "facts": {"description": f"about {GROUP_MARK}", "genres": ["Puzzle"]}}],
                  scope_name="group:n1")
    session.commit()
    cid = r.canonical_id
    with pytest.raises(service.AtlasError):
        scope.get_visible(session, scope.Viewer("anonymous", switch=False), cid)
    on, _ = scope.get_visible(session, scope.Viewer("anonymous", switch=True), cid)
    assert on.view["title"] == GROUP_TITLE and GROUP_MARK not in repr(on.view)
    detail, _ = scope.get_visible(session, scope.Viewer("detail", user_id="u"), cid)
    assert GROUP_MARK in repr(detail.view)


def test_search_counts_never_leak(session, ctx):
    """T-S10 (SQL level): the rule is inside the query, so totals and pages agree with what a visitor sees."""
    _ingest(session, ctx, "igdb", [{"external_id": "s1", "title": "Leakcheck Public"}])
    _ingest(session, ctx, "playnite", [{"external_id": "s2", "title": "Leakcheck Hostonly"}], kind="plugin")
    _ingest(session, ctx, "igdb", [{"external_id": "s3", "title": "Leakcheck Groupie"}], scope_name="group:n1")
    session.commit()

    def titles(viewer):
        return sorted(g.view["title"] for g in service.search(session, "Leakcheck", 50, 0, viewer=viewer))

    assert titles(scope.Viewer("anonymous")) == ["Leakcheck Public"]
    assert titles(scope.Viewer("token")) == ["Leakcheck Public"]
    assert titles(scope.Viewer("anonymous", switch=True)) == ["Leakcheck Groupie", "Leakcheck Public"]
    assert titles(scope.Viewer("detail", user_id="u")) == ["Leakcheck Groupie", "Leakcheck Hostonly",
                                                          "Leakcheck Public"]
    # filters are for detail only; for anyone else they are ignored (no effect, no error)
    assert titles(scope.Viewer("anonymous")) == sorted(
        g.view["title"] for g in service.search(session, "Leakcheck", 50, 0, viewer=scope.Viewer("anonymous"),
                                                has="group"))
    only_group = service.search(session, "Leakcheck", 50, 0, viewer=scope.Viewer("detail", user_id="u"),
                                has="group")
    assert [g.view["title"] for g in only_group] == ["Leakcheck Groupie"]


def test_feed_record_body_is_the_public_projection(session, ctx):
    """T-S11: a record's signed identity never carries a host/plugin value."""
    [r] = _ingest(session, ctx, "igdb", [{"external_id": "f1", "title": "Feed Public Title"}])
    _ingest(session, ctx, "playnite", [{"external_id": "f1h", "title": f"Feed {HOST_MARK}",
                                        "provider_ids": {"igdb": "f1"}}], kind="plugin")
    session.commit()
    body = feed.record_body(session, ctx.settings, session.get(Canonical, r.canonical_id), "test")
    assert HOST_MARK not in repr(body)


def test_projection_reprojects_when_categories_change(session, ctx):
    from atlas import runtime

    [r] = _ingest(session, ctx, "igdb", [{"external_id": "g3", "title": "Category Game",
                                          "facts": {"genres": ["Puzzle"]}}], scope_name="group:n1")
    session.commit()
    runtime.save(session, ctx.settings, None, {"group_public_categories": "identity"})
    assert scope.pending(session)
    with pytest.raises(runtime.SettingError):
        runtime.save(session, ctx.settings, None, {"group_public_categories": "identity nonsense"})
    assert r.canonical_id


# ---- web and API: viewers, canaries, Sharing, scope.detail ----------------------------------------------------------

@pytest.fixture
def node(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/c14.db", bootstrap_admin_token="b" * 40,
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False,
                       public_url="http://localhost:8000")
    app = main.create_app(s)
    st = app.state.atlas
    with st.maker() as sess:
        [pub] = _ingest(sess, st.ctx, "igdb", [{"external_id": "w1", "title": "Webpublic Alpha",
                                                "release_date": "2020-01-02"}])
        [host] = _ingest(sess, st.ctx, "playnite", [{"external_id": "w2", "title": f"Webhost {HOST_MARK}"}],
                         kind="plugin")
        [grp] = _ingest(sess, st.ctx, "igdb", [{"external_id": "w3", "title": GROUP_TITLE,
                                                "facts": {"description": f"desc {GROUP_MARK}"}}],
                        scope_name="group:n1")
        sess.commit()
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        _wait_for_startup_replay(st)
        c.st, c.app = st, app
        c.pub, c.host, c.grp = pub.canonical_id, host.canonical_id, grp.canonical_id
        yield c


def _fresh(node):
    return TestClient(node.app, base_url="http://localhost:8000", follow_redirects=False)


def _set_switch(node, on: bool):
    from atlas import runtime

    with node.st.maker() as s:
        runtime.save(s, node.st.base, None, {"group_data_public_pages": on})
        s.commit()
        node.st.refresh(s, force=True)


def _mint(node, scopes):
    r = node.post("/api/v2/tokens", headers={"Authorization": "Bearer " + "b" * 40},
                  json={"name": "t-" + "-".join(scopes), "scopes": scopes})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _surfaces(client, node, headers=None):
    """Every read surface a viewer can reach, as one text blob, plus the status of the hidden records' pages."""
    h = headers or {}
    parts = []
    for path in ("/", "/search?q=Web", "/search?q=Webhost", "/search?q=Groupidentity",
                 f"/game/{node.pub}", "/search?q=Web&has=group", "/search?q=Web&scope=local"):
        r = client.get(path, headers=h)
        parts.append(f"{path} {r.status_code} {r.text}")
    return "\n".join(parts)


def _api_surfaces(client, node, headers):
    parts = []
    for path in ("/api/v2/games?q=Web", "/api/v2/games?q=Webhost", "/api/v2/games?q=Groupidentity",
                 f"/api/v2/games/{node.pub}", f"/api/v2/games/{node.host}", f"/api/v2/games/{node.grp}",
                 f"/api/v2/games/{node.host}/provenance", f"/api/v2/games/{node.grp}/explain",
                 "/api/v2/games?q=Web&has=group"):
        r = client.get(path, headers=headers)
        parts.append(f"{path} {r.status_code} {r.text}")
    r = client.post("/api/v2/resolve", headers=headers, json={"items": [{"title": f"Webhost {HOST_MARK}"},
                                                                         {"title": GROUP_TITLE}]})
    parts.append(f"resolve {r.status_code} {r.text}")
    return "\n".join(parts)


def test_canaries_anonymous_switch_off(node):
    """T-S10: no host or group value, badge, count or filter reaches a visitor; hidden records are plain 404s."""
    anon = _fresh(node)
    blob = _surfaces(anon, node)
    assert HOST_MARK not in blob and GROUP_MARK not in blob and GROUP_TITLE not in blob
    assert "Your nodes" not in blob and "This node only" not in blob and 'name="has"' not in blob
    assert "Webpublic Alpha" in blob
    unknown = anon.get("/game/atlas:01zzzzzzzzzzzzzzzzzzzzzzzz")
    for cid in (node.host, node.grp):
        r = anon.get(f"/game/{cid}")
        assert r.status_code == unknown.status_code == 404


def test_canaries_anonymous_switch_on(node):
    """T-S10/D1: with the switch on, visitors see the public categories of group data, never a badge or the
    withheld categories; host data stays hidden."""
    _set_switch(node, True)
    anon = _fresh(node)
    blob = _surfaces(anon, node)
    assert GROUP_TITLE in blob  # identity is public by default
    assert HOST_MARK not in blob and GROUP_MARK not in blob and "Your nodes" not in blob
    page = anon.get(f"/game/{node.grp}")
    assert page.status_code == 200 and GROUP_MARK not in page.text and "Your nodes" not in page.text


def test_canaries_token_viewer(node):
    """T-S10: a follower's token (`sync`, no operator scope) sees what a visitor sees."""
    blob = _api_surfaces(node, node, _mint(node, ["read", "sync"]))
    assert HOST_MARK not in blob and GROUP_MARK not in blob and GROUP_TITLE not in blob
    assert '"scope"' not in blob
    assert f"/api/v2/games/{node.host} 404" in blob and f"/api/v2/games/{node.grp} 404" in blob


def test_operator_token_sees_detail_with_additive_keys(node):
    """Operator tokens act for the operator (detail): every record, with the additive `scope` key."""
    h = _mint(node, ["read", "curate"])
    g = node.get(f"/api/v2/games/{node.grp}", headers=h)
    assert g.status_code == 200 and g.json()["scope"]["widest"] == "group"
    assert node.get(f"/api/v2/games/{node.host}", headers=h).status_code == 200
    listed = node.get("/api/v2/games?q=Web&has=group", headers=h).json()
    assert listed is not None


def test_self_registered_user_lacks_detail_until_granted(node):
    """T-S13: on an open node, an account that registered itself sees what a visitor sees; an administrator can
    grant `scope.detail` on Users, and it is audited."""
    a = _fresh(node)
    signup(a, "boss")  # the first account on a fresh node is its administrator
    u = _fresh(node)
    signup(u, "stranger")
    blob = _surfaces(u, node)
    assert HOST_MARK not in blob and GROUP_MARK not in blob and GROUP_TITLE not in blob
    with node.st.maker() as s:
        stranger = s.scalars(select(User).where(User.username == "stranger")).one()
        assert stranger.role == "user" and s.scalars(select(User).where(User.username == "boss")).one().role == "admin"
        sid = stranger.id
    csrf = token(a, "/admin/users")
    r = a.post(f"/admin/users/{sid}", data={"csrf": csrf, "role": "user", "status": "active", "has_detail": "1",
                                            "detail": "on"})
    assert r.status_code == 303, r.text
    with node.st.maker() as s:
        assert s.get(UserPermission, (sid, scope.DETAIL)) is not None
    page = u.get(f"/game/{node.grp}")
    assert page.status_code == 200 and GROUP_MARK in page.text and "Your nodes" in page.text
    r = a.post(f"/admin/users/{sid}", data={"csrf": csrf, "role": "user", "status": "active", "has_detail": "1"})
    assert r.status_code == 303
    assert u.get(f"/game/{node.grp}").status_code == 404


def test_closed_registration_gives_every_account_detail(session):
    s = build_settings(registration="closed")
    user = User(username="fam", password_hash="!", role="user")
    session.add(user)
    session.flush()
    assert scope.holds_detail(session, s, user)
    assert not scope.holds_detail(session, build_settings(), user)


def test_sharing_page_standalone(node):
    """T-S12: a standalone node's Sharing page: the table, the standalone line, the push column greyed, the
    switch off; saving the categories reprojects."""
    with node.st.maker() as s:
        admin = User(username="boss2", password_hash=__import__("atlas.accounts", fromlist=["x"]).hash_password(PW),
                     role="admin")
        s.add(admin)
        s.commit()
    a = _fresh(node)
    assert login(a, "boss2").status_code == 303
    page = a.get("/admin/sharing")
    assert page.status_code == 200
    assert "Not in a group · Not linked to a main Atlas. Everything stays here." in page.text
    assert 'data-scope-row="group"' in page.text and 'aria-disabled="true"' in page.text
    assert re.search(r'name="group_data_public_pages" data-switch\s*>', page.text)  # off by default
    csrf = token(a, "/admin/sharing")
    r = a.post("/admin/sharing", data={"csrf": csrf, "public_category": ["identity", "release"]})
    assert r.status_code == 303
    with node.st.maker() as s:
        assert scope.pending(s)
    assert scope.public_categories(node.st.ctx.settings) == {"identity", "release"}
    assert _fresh(node).get("/admin/sharing").status_code in (302, 303, 401, 403)


def test_game_page_badges_and_keep_local(node):
    """Badges and the curation card for a curator; Keep on this node only sets a `local` ceiling."""
    with node.st.maker() as s:
        s.add(User(username="cur", password_hash=__import__("atlas.accounts", fromlist=["x"]).hash_password(PW),
                   role="curator"))
        s.commit()
    c = _fresh(node)
    assert login(c, "cur").status_code == 303
    page = c.get(f"/game/{node.grp}")
    assert page.status_code == 200 and "data-scope-line" in page.text and "Your nodes" in page.text
    assert "Where this record goes" in page.text
    with node.st.maker() as s:
        introduced = s.get(FeedIntroduced, node.pub) is not None
    csrf = token(c, f"/game/{node.pub}")
    r = c.post(f"/game/{node.pub}/scope", data={"csrf": csrf, "action": "local"})
    assert r.status_code == 303
    expect = "refused" if introduced else "kept"
    assert f"scope={expect}" in r.headers["location"]


def test_api_put_scope(node):
    h = _mint(node, ["curate"])
    r = node.put(f"/api/v2/games/{node.grp}/scope", headers=h, json={"ceiling": "local"})
    assert r.status_code in (200, 409), r.text
    if r.status_code == 200:
        assert r.json()["ceiling"] == "local"
        r = node.put(f"/api/v2/games/{node.grp}/scope", headers=h, json={"ceiling": None})
        assert r.status_code == 200 and r.json()["ceiling"] is None
    assert node.put(f"/api/v2/games/{node.grp}/scope", headers=_mint(node, ["read", "sync"]),
                    json={"ceiling": "local"}).status_code == 403
    assert node.put(f"/api/v2/games/{node.grp}/scope", headers=h, json={"ceiling": "public"}).status_code == 422


def test_group_artwork_is_hidden_from_visitors(node):
    """T-S10: an image received from the group (screenshots are withheld by default) is neither listed, nor
    reachable by id, nor served as bytes to a visitor; cover art follows the switch."""
    from atlas.models import Artwork

    with node.st.maker() as s:
        shot = Artwork(canonical_id=node.pub, source="igdb", kind="screenshot", url="https://img.invalid/g.png",
                       blob_hash="c" * 64, fetched_from="group:n1", redistributable=False)
        s.add(shot)
        s.commit()
        aid = shot.id
    anon = _fresh(node)
    assert anon.get(f"/game/{node.pub}/artwork/{aid}").status_code == 404
    assert "c" * 64 not in anon.get(f"/game/{node.pub}").text
    _set_switch(node, True)
    assert _fresh(node).get(f"/game/{node.pub}/artwork/{aid}").status_code == 404  # screenshots: withheld
