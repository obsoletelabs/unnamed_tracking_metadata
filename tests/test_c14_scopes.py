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

from atlas import accounts, feed, feed_schema, main, scope, service
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


def test_scope_derivation_table(session, ctx):
    """T-S1: four scopes, three ceilings; origin decides an item's scope (host, group and provider rows)."""
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


def test_group_ceiling_blocks_feed_emission_fail_closed(session, local_ctx):
    """T-S3 (and T-S9 on a local node): a ceiling'd record's edits are never signed (kept on this node), on every
    role; publishing lifts the ceiling."""
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
    assert exc.value.local_ok and isinstance(exc.value, feed_schema.FeedPolicyError)
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == before
    assert r.canonical_id not in {c.id for c in feed.candidates(session, "all")}
    scope.publish(session, ctx, r.canonical_id)
    session.commit()
    assert session.get(Canonical, r.canonical_id).scope_ceiling is None


def test_mixed_entry_fails_closed(session, local_ctx):
    """T-S3/T-S9: an entry naming a ceiling'd and a public record is refused outright (not kept locally)."""
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
    """T-S9: Keep-on-this-node is refused for an introduced record; followers keep what they have."""
    [r] = _ingest(session, ctx, "igdb", [{"external_id": "p1", "title": "Published Game"}])
    session.commit()
    feed.ensure_introduced(session, ctx, r.canonical_id, "test")
    session.commit()
    assert session.get(FeedIntroduced, r.canonical_id) is not None
    with pytest.raises(scope.CeilingRefused, match="already in your public feed"):
        scope.set_ceiling(session, ctx, r.canonical_id, "local")


def test_host_only_record_is_operator_private(session, ctx):
    """T-S10: a record that exists only through host/plugin data does not exist for visitors or tokens: same error
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
    """T-S10: a record with provider and host data shows visitors the provider values only."""
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
    """T-S10/D1: group-received data is absent while the switch is off; with it on, only public categories."""
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


def test_feed_emit_filters_group_and_local_fields(session, ctx):
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


def test_visibility_predicate_canaries_every_surface(node):
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


def test_anonymous_sees_no_group_badge(node):
    """T-S7/T-S10/D1: with the switch on, visitors see the public categories of group data, never a badge or the
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


def test_scope_filters_and_api_keys_additive(node):
    """T-S4: operator tokens act for the operator (detail): every record, with the additive `scope` key; old
    clients (a visitor, a follower's token) see the unchanged keys."""
    h = _mint(node, ["read", "curate"])
    g = node.get(f"/api/v2/games/{node.grp}", headers=h)
    assert g.status_code == 200 and g.json()["scope"]["widest"] == "group"
    assert node.get(f"/api/v2/games/{node.host}", headers=h).status_code == 200
    listed = node.get("/api/v2/games?q=Web&has=group", headers=h).json()
    assert listed is not None
    for headers in (_mint(node, ["read", "sync"]),):  # the API has no anonymous reader
        old = node.get(f"/api/v2/games/{node.pub}", headers=headers)
        assert old.status_code == 200 and "scope" not in old.json()


def test_self_registered_user_lacks_scope_detail(node):
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


def test_standalone_node_sharing_page(node):
    """T-S6: a standalone node's Sharing page: the table, the standalone line, the push column greyed, the
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


# ---- review fixes: federated sessions, mixed merges, every introduction mode, own data inside a group ---------------

def test_federated_session_detail_follows_c10_caps(session):
    """T-S13 (critic): a session opened through the owner group gets `scope.detail` only per its C10 caps (the
    request's effective rights) or an explicit grant, never from `via_group` alone: an account that registered itself
    (here or on an open sibling) and was linked stays a visitor."""
    from types import SimpleNamespace

    from atlas.accounts import Rights

    s = build_settings()  # registration open
    user = User(username="linked", password_hash="!", role="user")
    curator = User(username="capped", password_hash="!", role="curator")
    session.add_all([user, curator])
    session.flush()
    federated = SimpleNamespace(via_group="g1")
    assert not scope.holds_detail(session, s, user)
    assert not scope.holds_detail(session, s, user, federated)
    assert not scope.holds_detail(session, s, user, federated, Rights(federated=True))
    assert scope.viewer_for_user(session, s, user, federated, Rights(federated=True)).kind == "user"
    # a curator whose review rights the group policy capped in this session (group_federated_privilege): a visitor
    assert not scope.holds_detail(session, s, curator, federated, Rights(federated=True))
    assert scope.holds_detail(session, s, curator)  # signed in locally: the local role applies
    # rights the session holds (the local role passed the caps, or a valid delegated grant) do give it
    assert scope.holds_detail(session, s, user, federated, Rights(federated=True, review=True))
    scope.grant(session, user, scope.DETAIL, "admin")
    assert scope.holds_detail(session, s, user, federated, Rights(federated=True))


def test_mixed_merge_is_a_409_with_the_publish_message(node):
    """T-S9 (critic), through the API and the site: merging a ceiling'd record with a public one is refused with the
    §5.4 message (409, not a 500), nothing is signed, and both records stay as they were."""
    with node.st.maker() as s:
        scope.set_ceiling(s, node.st.ctx, node.grp, "local")
        s.commit()
        before = s.scalar(select(func.count()).select_from(ChangeLog))
    h = _mint(node, ["read", "curate"])
    for src, dst in ((node.grp, node.pub), (node.pub, node.grp)):
        r = node.post(f"/api/v2/games/{src}/merge", headers=h, json={"into": dst})
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["code"] == "scope_ceiling" and "Publish it" in err["message"]
    with node.st.maker() as s:
        assert s.scalar(select(func.count()).select_from(ChangeLog)) == before
        assert s.get(Canonical, node.grp).merged_into is None and s.get(Canonical, node.pub).merged_into is None
        s.add(User(username="cur9", password_hash=accounts.hash_password(PW), role="curator"))
        s.commit()
    c = _fresh(node)
    assert login(c, "cur9").status_code == 303
    csrf = token(c, f"/game/{node.pub}")
    r = c.post(f"/game/{node.grp}/merge", data={"csrf": csrf, "into": node.pub})
    assert r.status_code == 409 and "Publish it" in r.text


MODES = {"record": {}}  # C16 Z1: the v0 `legacy` and `bridge` introduction modes are gone


@pytest.mark.parametrize("mode", sorted(MODES))
def test_ceiling_gate_holds_in_legacy_bridge_and_record_modes(session, mode):
    """T-S9 (critic): on an authority, in every introduction mode and with `federation_introduce=all`: a ceiling'd
    record is never introduced or marked; an observation ingested into it, a lock, an unlock and a relation to a
    public record stay on this node (or are refused) without signing; a mixed merge fails closed with the "Publish
    it" refusal; Publish lifts the ceiling."""
    from atlas import relations
    from atlas.service import Ctx
    from atlas.signing import load_identity

    identity = load_identity(session)
    session.commit()
    ctx = Ctx(settings=build_settings(role="authority", federation_introduce="all", **MODES[mode]), identity=identity)
    assert feed.introduction_mode(ctx.settings) == mode
    [pub] = _ingest(session, ctx, "igdb", [{"external_id": f"p-{mode}", "title": f"Gatepublic {mode}"}])
    session.commit()
    [cap] = _ingest(session, ctx, "playnite", [{"external_id": f"c-{mode}", "title": f"Gatekept {mode}"}],
                    kind="plugin")  # host data only: `all` does not introduce it on ingest
    session.commit()
    cid = cap.canonical_id
    assert session.get(FeedIntroduced, cid) is None
    scope.set_ceiling(session, ctx, cid, "local")
    session.commit()
    before = session.scalar(select(func.count()).select_from(ChangeLog))
    obs = {"id": "o-" + mode, "source_kind": "metadata", "source": "manual", "source_scope": "test",
           "external_id": "c2", "payload": {"title": "new"}}
    assert feed.emit_or_local(session, ctx, "obs", {"canonical_id": cid, "media_type": "game",
                                                    "observation": obs}) is None
    assert feed.emit_or_local(session, ctx, "lock", {"canonical_id": cid, "field": "title", "value": "Y"}) is None
    assert feed.emit_or_local(session, ctx, "unlock", {"canonical_id": cid, "field": "title"}) is None
    assert feed.ensure_introduced(session, ctx, cid, "publish") is None
    assert cid not in {c.id for c in feed.candidates(session, "all")}
    assert session.get(FeedIntroduced, cid) is None
    session.commit()
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == before
    with pytest.raises(service.AtlasError) as exc:
        service.merge_canonicals(session, ctx, cid, pub.canonical_id, actor="cur")
    assert exc.value.code == "scope_ceiling" and "Publish it" in str(exc.value)
    session.rollback()
    try:  # gated below emit 1.4 (kept local, never signed) or refused at 1.5: never signed either way
        relations.add(session, ctx, cid, pub.canonical_id, "remake_of", actor="cur")
        session.commit()
    except service.AtlasError as refused:
        assert refused.code == "scope_ceiling"
        session.rollback()
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == before
    assert session.get(FeedIntroduced, cid) is None
    scope.publish(session, ctx, cid)
    session.commit()
    assert session.get(Canonical, cid).scope_ceiling is None


def _blob(node, colour) -> str:
    import hashlib
    import io
    from pathlib import Path

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(buf, "PNG")
    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    path = Path(node.st.ctx.settings.media_dir) / digest[:2] / f"{digest}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return digest


def test_standalone_node_public_pages_unchanged(node, monkeypatch):
    """T-S12 (Review #1 + critic): a node's own provider data and art stay public with today's cache headers, also
    for a node in a group with `group_assist_share_provider_data = fields+media` and `group_data_public_pages` off.
    Bytes only group data references are refused to visitors while the switch is off and carry `private` headers
    (never `immutable`) otherwise: `private, max-age=300` for a visitor, `private, no-store` for a signed-in user."""
    from atlas.models import Artwork, Observation

    own, grp = _blob(node, (10, 120, 30)), _blob(node, (200, 10, 10))
    with node.st.maker() as s:
        s.add(Artwork(canonical_id=node.pub, source="igdb", kind="key_art", url="https://img.invalid/own.png",
                      blob_hash=own, redistributable=False))
        s.add(Artwork(canonical_id=node.grp, source="igdb", kind="key_art", url="https://img.invalid/grp.png",
                      blob_hash=grp, fetched_from="group:n1", redistributable=False))
        s.commit()

    def check_own():
        anon = _fresh(node)
        page = anon.get(f"/game/{node.pub}")
        assert page.status_code == 200 and "Webpublic Alpha" in page.text
        r = anon.get(f"/media/{own}")
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

    def in_group(settings_obj):
        monkeypatch.setattr(scope, "in_group", lambda session: True)
        object.__setattr__(settings_obj, "group_assist_share_provider_data", "fields+media")

    check_own()  # standalone
    in_group(node.st.ctx.settings)
    with node.st.maker() as s:
        o = s.scalars(select(Observation).where(Observation.canonical_id == node.pub)).first()
        assert scope.of_observation(node.st.ctx.settings, o, grouped=True) == "group"  # it travels to the group…
    check_own()  # …and its display is unchanged: visibility follows origin, not travel scope
    assert _fresh(node).get(f"/media/{grp}").status_code == 404  # switch off: group-only bytes do not exist
    _set_switch(node, True)
    in_group(node.st.ctx.settings)
    r = _fresh(node).get(f"/media/{grp}")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=300"
    assert "immutable" not in r.headers["cache-control"] and "public" not in r.headers["cache-control"]
    with node.st.maker() as s:
        s.add(User(username="cur12", password_hash=accounts.hash_password(PW), role="curator"))
        s.commit()
    c = _fresh(node)
    assert login(c, "cur12").status_code == 303
    r = c.get(f"/media/{grp}")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    check_own()


# ---- T-S10, widened (second review): every surface, every viewer, the switch off and on -----------------------------

def _seed_wide(node):
    """Facets that must follow their record's visibility: a group observation (a withheld description) on the public
    record, relations from the public record to the hidden ones, an achievement on the group record, an image of
    the host record, and a public list holding all three records. Third review: a group-received achievement on the
    public record (the editor's "From other sources") and an accepted proposal on the host-only record (a profile)."""
    from datetime import UTC, datetime

    from atlas.models import Achievement, Artwork, ListItem, Proposal, Relation, UserList

    with node.st.maker() as s:
        _ingest(s, node.st.ctx, "igdb", [{"external_id": "w1", "title": "Webpublic Alpha",
                                          "facts": {"description": f"mixed {GROUP_MARK}"}}], scope_name="group:n1")
        s.add_all([Relation(from_id=node.pub, to_id=node.host, type="remake_of", source="curator"),
                   Relation(from_id=node.pub, to_id=node.grp, type="port_of", source="curator")])
        ach = Achievement(canonical_id=node.grp, source="group:n1", external_id="a1", name=f"Ach {GROUP_MARK}",
                          norm_name="ach", description=f"Ach {GROUP_MARK}")
        art = Artwork(canonical_id=node.host, source="playnite", kind="cover", url=f"https://img.invalid/{HOST_MARK}.png")
        s.add_all([ach, art, Achievement(canonical_id=node.pub, source="group:n1", external_id="a2",
                                         name=f"Pubach {GROUP_MARK}", norm_name="pubach", tier="gold", points=90)])
        owner = User(username="lister", password_hash=accounts.hash_password(PW), role="user")
        s.add(owner)
        s.flush()
        s.add(Proposal(kind="edit", canonical_id=node.host, proposer_id=owner.id, payload={"title": "x"},
                       status="approved", decided_at=datetime.now(UTC)))
        lst = UserList(user_id=owner.id, name="Mixed shelf", public=True)
        s.add(lst)
        s.flush()
        s.add_all([ListItem(list_id=lst.id, canonical_id=cid) for cid in (node.pub, node.host, node.grp)])
        s.commit()
        scope.reproject_pending(node.st.maker, node.st.ctx)
        return {"ach": ach.id, "art": art.id, "list": lst.id}


def _web_wide(client, node, ids, extra=()):
    parts = []
    paths = ["/", "/search?q=Web", "/search?q=Groupidentity", f"/game/{node.pub}", f"/game/{node.pub}/relations",
             f"/game/{node.pub}/links", f"/game/{node.host}/relations", f"/game/{node.host}/links",
             f"/game/{node.grp}/achievements/{ids['ach']}", f"/game/{node.host}/artwork/{ids['art']}",
             f"/list/{ids['list']}", "/u/lister", f"/game/{node.pub}/achievements/edit",
             f"/game/{node.grp}/achievements/edit", *extra]
    for path in paths:
        r = client.get(path)
        parts.append(f"{path} {r.status_code} {r.text}")
    return "\n".join(parts)


def _api_wide(client, node, headers):
    parts = [_api_surfaces(client, node, headers)]
    for path in (f"/api/v2/games/{node.pub}/relations", f"/api/v2/games/{node.pub}/provenance",
                 f"/api/v2/games/{node.pub}/explain", f"/api/v2/games/{node.grp}/achievements",
                 f"/api/v2/games/{node.host}/artwork", f"/api/v2/games/{node.pub}/artwork"):
        r = client.get(path, headers=headers)
        parts.append(f"{path} {r.status_code} {r.text}")
    return "\n".join(parts)


def _personal_token(node, username):
    from atlas import auth

    with node.st.maker() as s:
        user = s.scalars(select(User).where(User.username == username)).one()
        row, secret = auth.create_token(s, f"{username}-app", ["read"])
        row.user_id = user.id
        s.commit()
    return {"Authorization": "Bearer " + secret}


def _changes_ids(client, headers):
    r = client.get("/api/v2/changes?feed=local&limit=500", headers=headers)
    assert r.status_code == 200, r.text
    return {e["id"] for e in r.json()["results"]}


@pytest.mark.parametrize("switch", [False, True])
def test_canaries_every_surface_every_viewer(node, switch):
    """T-S10 (second review): record page, lists, profiles, relations, links, achievements, artwork, provenance,
    explain, resolve, the local change feed, the proposal page and the edit form, for anonymous, token and user
    viewers with the switch off and on; hidden records answer like unknown ones everywhere."""
    ids = _seed_wide(node)
    _set_switch(node, switch)
    signup(_fresh(node), "boss")  # the first account is the administrator; the next one registers itself
    u = _fresh(node)
    signup(u, "stranger")

    def check(blob):
        leaks = [chunk.split(" ", 2)[:2] for chunk in re.split(r"\n(?=/|resolve )", blob)
                 if HOST_MARK in chunk or GROUP_MARK in chunk or "group:n1" in chunk]
        assert not leaks, f"a withheld value leaked on {leaks}"
        assert (GROUP_TITLE in blob) if switch else (GROUP_TITLE not in blob)
        assert "Your nodes" not in blob

    check(_web_wide(_fresh(node), node, ids))
    # the self-registered user also has the edit form and their own proposal on a record holding group data
    csrf = token(u, f"/game/{node.pub}/edit")
    r = u.post(f"/game/{node.pub}/edit", data={"csrf": csrf, "title": "Webpublic Alpha", "release_date": "2020-01-02",
                                                "description": "my words", "note": "probe"})
    assert r.status_code == 303, r.text
    with node.st.maker() as s:
        from atlas.models import Proposal

        pid = s.scalars(select(Proposal.id).where(Proposal.canonical_id == node.pub)).first()
    assert pid
    check(_web_wide(u, node, ids, extra=(f"/proposal/{pid}", f"/game/{node.pub}/edit", "/me/export.json")))
    for headers in (_mint(node, ["read", "sync"]), _personal_token(node, "stranger")):
        check(_api_wide(node, node, headers))
        seen = _changes_ids(node, headers)
        assert node.pub in seen and node.host not in seen
        assert (node.grp in seen) == switch
    # the operator's tools still see everything
    op = _mint(node, ["read", "curate"])
    assert {node.pub, node.host, node.grp} <= _changes_ids(node, op)


def test_group_admin_cannot_change_scope_detail(node, monkeypatch):
    """C10 §6.3/§6.4: `scope.detail` is a standing right, so an administrator through a group grant (not a local
    administrator) can neither grant nor revoke it, and the Users page shows them no checkbox."""
    from atlas.web.deps import Page

    signup(_fresh(node), "boss")
    signup(_fresh(node), "stranger")
    with node.st.maker() as s:
        sid = s.scalars(select(User.id).where(User.username == "stranger")).one()
    a = _fresh(node)
    assert login(a, "boss").status_code == 303
    # the same administrator, but holding the right through a group grant (C10: `rights.local_admin` is False)
    monkeypatch.setattr(Page, "is_local_admin", property(lambda self: False))
    page = a.get("/admin/users")
    assert page.status_code == 200 and 'name="detail"' not in page.text
    csrf = token(a, "/admin/users")
    r = a.post(f"/admin/users/{sid}", data={"csrf": csrf, "role": "user", "status": "active", "has_detail": "1",
                                            "detail": "on"})
    assert r.status_code == 409 and "local administrator" in r.text
    with node.st.maker() as s:
        assert s.get(UserPermission, (sid, scope.DETAIL)) is None
    # a save that leaves the right as it is still changes role and status
    r = a.post(f"/admin/users/{sid}", data={"csrf": csrf, "role": "user", "status": "active"})
    assert r.status_code == 303


@pytest.mark.parametrize("switch", [False, True])
def test_hidden_record_actions_and_profile(node, switch):
    """Third review (§5.7, identical 404): a profile never names a hidden record (not even by its id); following,
    listing or commenting on one answers exactly like an unknown id; the achievement editor leaves out rows
    received from the group."""
    ids = _seed_wide(node)
    _set_switch(node, switch)
    signup(_fresh(node), "boss")
    u = _fresh(node)
    signup(u, "stranger")
    for client in (_fresh(node), u):
        r = client.get("/u/lister")
        assert r.status_code == 200 and node.host not in r.text
    for path in (f"/game/{node.pub}/achievements/edit", f"/game/{node.grp}/achievements/edit"):
        r = u.get(path)
        assert GROUP_MARK not in r.text and "group:n1" not in r.text
    csrf = token(u, "/me/lists")
    r = u.post("/me/lists", data={"csrf": csrf, "name": "Probe shelf"})
    assert r.status_code == 303, r.text
    with node.st.maker() as s:
        from atlas.models import Follow, ListItem, UserList

        lid = s.scalars(select(UserList.id).where(UserList.name == "Probe shelf")).one()
    hidden = [node.host] if switch else [node.host, node.grp]
    unknown = "0" * 26

    def answers(target):
        follow = u.post("/follow", data={"csrf": csrf, "type": "game", "target": target})
        listed = u.post(f"/list/{lid}/add", data={"csrf": csrf, "game": target})
        return (follow.status_code, follow.text.replace(target, "X"), listed.status_code,
                listed.text.replace(target, "X"))

    expected = answers(unknown)
    assert expected[0] == 404 and expected[2] == 404
    for cid in hidden:
        assert answers(cid) == expected, f"{cid} answers unlike an unknown id"
    with node.st.maker() as s:
        assert not s.scalars(select(Follow).where(Follow.target_id.in_(hidden))).all()
        assert not s.scalars(select(ListItem).where(ListItem.list_id == lid)).all()
    # the visible record still works
    assert u.post("/follow", data={"csrf": csrf, "type": "game", "target": node.pub}).status_code == 303
    assert u.post(f"/list/{lid}/add", data={"csrf": csrf, "game": node.pub}).status_code == 303
    assert ids


def test_notifications_follow_the_projection(node):
    """Third review: a follower without scope.detail is never told the title of a record that does not exist for
    them (host-only), and a proposer is told the projected title, never a host/plugin value."""
    from atlas import social
    from atlas.models import Follow, Notification, Proposal

    signup(_fresh(node), "boss")
    signup(_fresh(node), "stranger")
    with node.st.maker() as s:
        sid = s.scalars(select(User.id).where(User.username == "stranger")).one()
        bid = s.scalars(select(User.id).where(User.username == "boss")).one()
        s.add_all([Follow(user_id=sid, target_type="game", target_id=node.host),
                   Follow(user_id=sid, target_type="game", target_id=node.pub)])
        s.flush()
        for cid in (node.host, node.pub):
            theirs = Proposal(kind="edit", canonical_id=cid, proposer_id=bid, payload={}, status="approved")
            mine = Proposal(kind="edit", canonical_id=cid, proposer_id=sid, payload={}, status="approved")
            s.add_all([theirs, mine])
            s.flush()
            social.on_proposal_applied(s, node.st.ctx, theirs, None)  # the follower is told
            social.on_proposal_decided(s, node.st.ctx, mine, "approved", None)  # the proposer is told
        s.commit()
        texts = [n.title for n in s.scalars(select(Notification).where(Notification.user_id == sid))]
    assert texts and not any(HOST_MARK in t for t in texts), texts
    assert not any(node.host in t for t in texts)
    assert any("Webpublic Alpha was updated" in t for t in texts)
    assert any("for Webpublic Alpha was approved" in t for t in texts)
    assert sum("was updated" in t for t in texts) == 1  # nothing about the host-only record


def test_introduce_projects_before_signing(session):
    """Third review (T-S11) while the post-upgrade reprojection is still running: a record with host data but no
    projection row yet is projected before it is signed, so `introduce --all` neither signs a host value nor
    introduces a host-only record."""
    from atlas.service import Ctx
    from atlas.signing import load_identity

    identity = load_identity(session)
    session.commit()
    ctx = Ctx(settings=build_settings(role="authority", **MODES["record"]),
              identity=identity)
    [pub] = _ingest(session, ctx, "igdb", [{"external_id": "i1", "title": "Introduce Public"}])
    [host] = _ingest(session, ctx, "playnite", [{"external_id": "i2", "title": f"Introhost {HOST_MARK}"}],
                     kind="plugin")
    session.commit()
    session.execute(ScopeProjection.__table__.delete())  # as migration 0038 leaves it, before the worker ran
    session.commit()
    row = session.get(Canonical, host.canonical_id)
    assert feed.project_now(session, ctx, row) is False
    assert HOST_MARK not in repr(feed.record_body(session, ctx.settings, row, "t"))
    session.execute(ScopeProjection.__table__.delete())
    session.commit()
    while not feed.introduce_batch(session, ctx, "all")["finished"]:
        pass
    session.commit()
    assert session.get(FeedIntroduced, pub.canonical_id) is not None
    assert session.get(FeedIntroduced, host.canonical_id) is None
    assert HOST_MARK not in repr([e.body for e in session.scalars(select(ChangeLog))])
