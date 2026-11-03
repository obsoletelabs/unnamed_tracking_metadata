import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import custom, feed, main, proposals, service, sync
from atlas.models import Canonical, CustomValue, FieldDef, MediaType
from atlas.resolve import find
from atlas.service import AtlasError, Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings
from .test_community import user
from .test_sync import curate, pair  # noqa: F401
from .test_web import fresh, signup, site, token  # noqa: F401


@pytest.fixture
def schema(session, ctx):
    custom.seed(session)
    custom.save_type(session, ctx, "movie", "Movie", labels={"developer": "Director", "publisher": "Studio"})
    custom.save_field(session, ctx, "movie", "runtime_min", "Runtime (minutes)", "number")
    custom.save_field(session, ctx, "movie", "rating", "Rating", "choice", choices="G\nPG\nR")
    custom.save_field(session, ctx, "movie", "trailer", "Trailer", "url")
    custom.save_field(session, ctx, "movie", "cast", "Cast", "list")
    custom.save_field(session, ctx, "movie", "internal_note", "Internal note", "text", public=False, editable=False)
    session.commit()
    return ctx


@pytest.mark.parametrize("key", ["Title", "1abc", "has space", "x" * 41, "title", "developer", "custom", "id", ""])
def test_field_keys_are_validated(session, ctx, key):
    custom.seed(session)
    with pytest.raises(AtlasError):
        custom.save_field(session, ctx, "game", key, "Label", "text")


def test_field_rules(session, ctx):
    custom.seed(session)
    with pytest.raises(AtlasError, match="kind"):
        custom.save_field(session, ctx, "game", "x", "X", "weird")
    with pytest.raises(AtlasError, match="two options"):
        custom.save_field(session, ctx, "game", "x", "X", "choice", choices="only one")
    with pytest.raises(AtlasError, match="unknown media type"):
        custom.save_field(session, ctx, "nope", "x", "X", "text")
    custom.save_field(session, ctx, "game", "x", "X", "text")
    with pytest.raises(AtlasError, match="kind cannot change"):
        custom.save_field(session, ctx, "game", "x", "X", "number")
    for i in range(custom.MAX_FIELDS_PER_TYPE):
        try:
            custom.save_field(session, ctx, "game", f"f{i}", "F", "text")
        except AtlasError as exc:
            assert "at most" in str(exc)
            break
    else:
        raise AssertionError("no field limit")


def test_type_rules(session, ctx):
    custom.seed(session)
    for bad in ("1x", "a b", "x" * 40, "", "x-y"):
        with pytest.raises(AtlasError):
            custom.save_type(session, ctx, bad, "L")
    t = custom.save_type(session, ctx, "game", "Games", enabled=False)
    assert t.enabled is True  # the built-in type cannot be disabled
    with pytest.raises(AtlasError, match="cannot be removed"):
        custom.delete_type(session, ctx, "game")
    custom.save_type(session, ctx, "anime", "Anime", labels={"developer": "Studio", "bogus": "x", "series": ""})
    assert session.get(MediaType, "anime").labels == {"developer": "Studio"}  # unknown / empty labels dropped


def test_builtin_media_baselines_are_seeded_and_stable(session, ctx):
    custom.seed(session)
    assert {row.key for row in session.scalars(select(MediaType))} == {"game", "anime", "movie", "tv_show", "series",
                                                                       "collection"}
    assert session.get(MediaType, "movie").labels["developer"] == "Director"
    assert session.get(MediaType, "tv_show").labels["publisher"] == "Network"
    expected = {
        "anime": {"episode_count", "format", "status", "source_material"},
        "movie": {"runtime_minutes", "writer", "production_countries", "tagline"},
        "tv_show": {"episode_count", "season_count", "episode_runtime_minutes", "status"},
    }
    for media_type, keys in expected.items():
        fields = session.scalars(select(FieldDef).where(FieldDef.media_type == media_type)).all()
        assert {field.key for field in fields} == keys
        assert all(field.public and field.editable for field in fields)
    custom.seed(session)
    assert len(session.scalars(select(FieldDef).where(FieldDef.media_type == "movie")).all()) == 4
    with pytest.raises(AtlasError, match="built-in media types"):
        custom.delete_type(session, ctx, "movie")


# Long values must not become test ids: pytest copies the id into PYTEST_CURRENT_TEST and Windows caps an
# environment variable at 32767 characters, which broke setup and teardown of the 20000-character case.
@pytest.mark.parametrize(("kind", "raw", "ok"), [
    ("text", "hello", "hello"), ("text", "x" * 501, None), ("text", "a\x00b", None),
    ("longtext", "x" * 20000, "x" * 20000), ("longtext", "x" * 20001, None),
    ("number", "12.5", 12.5), ("number", "7", 7), ("number", "abc", None), ("number", "nan", None), ("number", "1e99", None),
    ("date", "2020-09-17", "2020-09-17"), ("date", "17/09/2020", None), ("date", "2020-13-01", None),
    ("bool", "yes", True), ("bool", "no", False), ("bool", "maybe", None),
    ("choice", "PG", "PG"), ("choice", "XXX", None),
    ("list", "a\nb\n a ", ["a", "b", "a"]), ("list", "x" * 200, None),
    ("url", "https://x.test/a", "https://x.test/a"), ("url", "http://x.test", None), ("url", "javascript:alert(1)", None),
    ("url", "https://x.test/a b", None),
], ids=lambda v: f"{v[:12]}...len{len(v)}" if isinstance(v, str) and len(v) > 100 else None)  
def test_value_validation(kind, raw, ok):
    f = FieldDef(media_type="movie", key="k", label="K", kind=kind, choices=["G", "PG", "R"])
    if ok is None:
        with pytest.raises(AtlasError):
            custom.validate_value(f, raw)
    else:
        assert custom.validate_value(f, raw) == ok
    assert custom.validate_value(f, "") is None and custom.validate_value(f, None) is None


def test_a_record_can_hold_any_media_type_key_that_is_allowed(session, schema):
    from atlas.models import MediaType

    assert Canonical.__table__.c.media_type.type.length >= MediaType.__table__.c.key.type.length
    key = "a" * 32
    custom.save_type(session, schema, key, "Long")
    [r] = service.ingest(session, schema, batch("igdb", [{"external_id": "1", "title": "Odd"}]), system=True,
                         media_type=key)
    assert session.get(Canonical, r.canonical_id).media_type == key


def test_splitting_a_record_keeps_its_media_type(session, schema):
    from atlas.models import Observation

    [a] = service.ingest(session, schema, batch("igdb", [{"external_id": "1", "title": "Alien"}]), system=True,
                         media_type="movie")
    [b] = service.ingest(session, schema, batch("tmdb", [{"external_id": "2", "title": "Different",
                                                          "provider_ids": {"igdb": "1"}}]), system=True,
                         media_type="movie")
    assert b.canonical_id == a.canonical_id
    obs = session.scalars(select(Observation).where(Observation.external_id == "2")).one()
    new = service.detach_observation(session, schema, obs.id)
    session.commit()
    assert new.id != a.canonical_id
    assert session.get(Canonical, new.id).media_type == "movie"
    assert session.get(Canonical, a.canonical_id).media_type == "movie"


def test_merge_by_trust_hides_private_and_orphaned_values(session, schema):
    ctx = schema
    [r] = service.ingest(session, ctx, batch("igdb", [{"external_id": "1", "title": "Alien"}]), system=True,
                         media_type="movie")
    cid = r.canonical_id
    assert session.get(Canonical, cid).media_type == "movie"
    custom.set_values(session, ctx, cid, "community", {"rating": "PG", "runtime_min": 100})
    custom.set_values(session, ctx, cid, "curator", {"rating": "R"})
    custom.set_values(session, ctx, cid, "curator", {"internal_note": "hidden"})
    assert custom.merged(session, ctx, cid) == {"rating": "R", "runtime_min": 100}  # curator beats community
    assert custom.merged(session, ctx, cid, public_only=False)["internal_note"] == "hidden"
    custom.delete_field(session, ctx, "movie", "runtime_min")
    assert "runtime_min" not in custom.merged(session, ctx, cid)
    assert session.get(CustomValue, (cid, "runtime_min", "community")) is None
    with pytest.raises(AtlasError, match="built-in media types"):
        custom.delete_type(session, ctx, "movie")


def test_unknown_or_disabled_media_types_are_refused_on_ingest(session, schema):
    ctx = schema
    with pytest.raises(AtlasError, match="media type"):
        service.ingest(session, ctx, batch("igdb", [{"external_id": "9", "title": "X"}]), system=True,
                       media_type="podcast")


def test_ingest_and_resolution_keep_same_title_separate_by_media_type(session):
    custom.seed(session)
    ctx = Ctx(settings=build_settings(), identity=load_identity(session))
    game_batch = batch("gamefeed", [{"external_id": "game-1", "title": "Dune", "release_date": "2021-10-22"}])
    movie_batch = batch("moviedb", [{"external_id": "movie-1", "title": "Dune", "release_date": "2021-10-22"}])
    [game] = service.ingest(session, ctx, game_batch, system=True, media_type="game")
    [movie] = service.ingest(session, ctx, movie_batch, system=True, media_type="movie")
    assert game.canonical_id != movie.canonical_id
    assert session.get(Canonical, game.canonical_id).media_type == "game"
    assert session.get(Canonical, movie.canonical_id).media_type == "movie"
    match = find(session, ctx.settings, namespace="", external_id="", provider_ids=None, title="Dune",
                 release_date="2021-10-22", platform=None, media_type="movie")
    assert match.canonical_id == movie.canonical_id
    custom.save_type(session, ctx, "podcast", "Podcast", enabled=False)
    with pytest.raises(AtlasError, match="media type"):
        service.ingest(session, ctx, batch("podcast", [{"external_id": "9", "title": "X"}]), system=True,
                       media_type="podcast")


def test_custom_values_move_on_merge(session, schema):
    ctx = schema
    [a] = service.ingest(session, ctx, batch("igdb", [{"external_id": "1", "title": "Mystery"}]), system=True,
                         media_type="movie")
    [b] = service.ingest(session, ctx, batch("gogplugin", [{"external_id": "2", "title": "Mystery"}]),
                         media_type="movie")
    custom.set_values(session, ctx, b.canonical_id, "curator", {"rating": "G"})
    service.merge_canonicals(session, ctx, b.canonical_id, a.canonical_id)
    assert custom.merged(session, ctx, a.canonical_id) == {"rating": "G"}


# ---- proposals ----------------------------------------------------------------------------------------

def test_proposals_carry_custom_values_apply_and_revert(session, schema):
    ctx = schema
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    [r] = service.ingest(session, ctx, batch("igdb", [{"external_id": "1", "title": "Alien"}]), system=True,
                         media_type="movie")
    cid = r.canonical_id
    p = proposals.create(session, ctx, bob, "edit", cid,
                         {"custom": {"rating": "R", "cast": "Sigourney Weaver\nTom Skerritt"}})
    assert p.payload["custom"]["cast"] == ["Sigourney Weaver", "Tom Skerritt"]
    assert custom.merged(session, ctx, cid) == {}  # pending
    proposals.review(session, ctx, admin, p.id, approve=True)
    assert custom.merged(session, ctx, cid)["rating"] == "R"
    trusted = user(session, ctx.settings, "carol", rep=100)
    q = proposals.create(session, ctx, trusted, "edit", cid, {"custom": {"rating": "PG", "runtime_min": "117"}})
    cast = ["Sigourney Weaver", "Tom Skerritt"]
    assert custom.merged(session, ctx, cid) == {"rating": "PG", "runtime_min": 117, "cast": cast}
    proposals.revert(session, ctx, admin, q.id)
    assert custom.merged(session, ctx, cid) == {"rating": "R", "cast": cast}


@pytest.mark.parametrize("payload", [
    {"custom": {"internal_note": "x"}}, {"custom": {"nope": "x"}}, {"custom": {"rating": "X"}},
    {"custom": {"trailer": "http://insecure.test"}}, {"custom": {}},
])
def test_invalid_custom_proposals_are_rejected(session, schema, payload):
    ctx = schema
    user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    [r] = service.ingest(session, ctx, batch("igdb", [{"external_id": "1", "title": "Alien"}]), system=True,
                         media_type="movie")
    with pytest.raises(AtlasError):
        proposals.create(session, ctx, bob, "edit", r.canonical_id, payload)


def test_new_game_proposal_with_a_media_type_and_custom_fields(session, schema):
    ctx = schema
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    p = proposals.create(session, ctx, bob, "new_game", None, {
        "title": "Blade Runner", "media_type": "movie", "release_date": "1982-06-25", "developer": "Ridley Scott",
        "custom": {"rating": "R"}})
    proposals.review(session, ctx, admin, p.id, approve=True)
    row = session.get(Canonical, p.canonical_id)
    assert row.media_type == "movie" and row.view["developer"] == "Ridley Scott"
    assert custom.merged(session, ctx, row.id) == {"rating": "R"}
    with pytest.raises(AtlasError, match="media type"):
        proposals.create(session, ctx, bob, "new_game", None, {"title": "X", "media_type": "podcast"})
    with pytest.raises(AtlasError, match="cannot be edited"):
        proposals.create(session, ctx, bob, "new_game", None, {"title": "X", "media_type": "movie",
                                                                "custom": {"internal_note": "y"}})


# ---- API ----------------------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/a.db", bootstrap_admin_token="m" * 40,
                       media_cache_enabled=False)
    app = main.create_app(s)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer " + "m" * 40
        c.state = app.state.atlas
        yield c


def test_api_exposes_schema_custom_values_and_type_filter(api):
    with api.state.maker() as s:
        ctx = api.state.ctx
        custom.save_type(s, ctx, "movie", "Movie", labels={"developer": "Director"})
        custom.save_field(s, ctx, "movie", "rating", "Rating", "choice", choices="G\nPG\nR")
        custom.save_field(s, ctx, "movie", "secret", "Secret", "text", public=False)
        s.commit()
    schema = api.get("/api/v2/schema").json()
    movie = next(t for t in schema["media_types"] if t["key"] == "movie")
    assert movie["labels"] == {"developer": "Director"}
    assert {f["key"] for f in movie["fields"]} == {
        "rating", "runtime_minutes", "writer", "production_countries", "tagline"
    }
    with api.state.maker() as s:
        [r] = service.ingest(s, api.state.ctx, batch("igdb", [{"external_id": "1", "title": "Alien"}]), system=True,
                             media_type="movie")
        [g] = service.ingest(s, api.state.ctx, batch("igdb", [{"external_id": "2", "title": "Alien Isolation"}]),
                             system=True)
        s.commit()
    cid = r.canonical_id
    put = api.put(f"/api/v2/games/{cid}/custom", json={"values": {"rating": "R", "secret": "x"}})
    assert put.status_code == 200 and put.json()["custom"] == {"rating": "R"}
    assert api.put(f"/api/v2/games/{cid}/custom", json={"values": {"rating": "XXX"}}).status_code == 400
    assert api.put(f"/api/v2/games/{cid}/custom", json={"values": {"nope": "x"}}).status_code == 400
    game = api.get(f"/api/v2/games/{cid}").json()
    assert game["media_type"] == "movie" and game["custom"] == {"rating": "R"}
    assert api.put(f"/api/v2/games/{cid}/custom", json={"clear": ["rating"]}).json()["custom"] == {}
    both = api.get("/api/v2/games?q=alien").json()["results"]
    assert len(both) == 2
    only = api.get("/api/v2/games?q=alien&type=movie").json()["results"]
    assert [x["id"] for x in only] == [cid]
    assert "movie" in api.get("/api/v2/site").json()["media_types"]
    assert g.canonical_id != cid


def test_custom_writes_need_curate(api):
    with api.state.maker() as s:
        [r] = service.ingest(s, api.state.ctx, batch("igdb", [{"external_id": "1", "title": "X"}]), system=True)
        s.commit()
    tok = api.post("/api/v2/tokens", json={"name": "r", "scopes": ["read", "contribute"]}).json()["token"]
    weak = TestClient(api.app)
    assert weak.put(f"/api/v2/games/{r.canonical_id}/custom", json={"values": {}},
                    headers={"Authorization": "Bearer " + tok}).status_code == 403


# ---- website ------------------------------------------------------------------------------------------

def test_admin_builds_a_media_type_and_contributors_use_it(site):  # noqa: F811
    signup(site, "alice")
    t = token(site, "/admin/schema")
    assert site.post("/admin/schema/types", data={"csrf": t, "key": "movie", "label": "Movie", "enabled": "on",
                                                  "label_developer": "Director", "position": "5"}).status_code == 303
    assert site.post("/admin/schema/movie/fields", data={
        "csrf": t, "key": "rating", "label": "Age rating", "kind": "choice", "choices": "G\nPG\nR", "public": "on",
        "editable": "on", "help": "Classification board"}).status_code == 303
    assert site.post("/admin/schema/movie/fields", data={
        "csrf": t, "key": "runtime_min", "label": "Runtime", "kind": "number", "public": "on",
        "editable": "on"}).status_code == 303
    bad = site.post("/admin/schema/movie/fields", data={"csrf": t, "key": "title", "label": "T", "kind": "text"})
    assert bad.status_code == 400
    assert "Age rating" in site.get("/admin/schema/movie").text
    form = site.get("/new?type=movie").text
    assert 'name="cf_rating"' in form and "Classification board" in form and ">Director</label>" in form
    assert 'name="media_type" value="movie"' in form
    r = site.post("/new", data={"csrf": t, "media_type": "movie", "title": "Blade Runner", "developer": "Ridley Scott",
                                "release_date": "1982-06-25", "cf_rating": "R", "cf_runtime_min": "117"})
    assert r.status_code == 303 and r.headers["location"].startswith("/game/atlas:")  # admin: applied at once
    page = site.get(r.headers["location"].split("?")[0]).text
    assert "Age rating" in page and ">R<" in page and "117" in page and "Director" in page and "Ridley Scott" in page
    gid = r.headers["location"].split("?")[0].split("/game/")[1]
    bob = fresh(site)
    signup(bob, "bob")
    edit = bob.get(f"/game/{gid}/edit")
    assert 'name="cf_runtime_min" value="117"' in edit.text.replace(" ", " ") or 'value="117"' in edit.text
    t2 = token(bob, f"/game/{gid}/edit")
    r2 = bob.post(f"/game/{gid}/edit", data={"csrf": t2, "cf_rating": "PG"})
    assert r2.status_code == 303 and "m=proposed" in r2.headers["location"]
    queue = site.get("/review").text
    assert "Age rating" in queue and "PG" in queue and "<del>R</del>" in queue
    bad = bob.post(f"/game/{gid}/edit", data={"csrf": t2, "cf_rating": "XXX"})
    assert bad.status_code == 400 and "choose one of" in bad.text
    assert site.get("/search?q=blade&type=movie").text.count("Blade Runner") >= 1
    assert "Blade Runner" not in site.get("/search?q=blade&type=game").text
    # disabling the type hides it from the chooser but keeps records
    site.post("/admin/schema/types", data={"csrf": t, "key": "movie", "label": "Movie", "position": "5"})
    assert "Movie" not in re.sub(r"<[^>]+>", " ", site.get("/new").text).split("Type:")[-1][:40]


def test_only_admins_edit_the_schema(site):  # noqa: F811
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    t = token(bob, "/me")
    for method, path in (("get", "/admin/schema"), ("get", "/admin/schema/game"), ("post", "/admin/schema/types"),
                         ("post", "/admin/schema/game/fields"), ("post", "/admin/schema/game/fields/x/delete")):
        r = getattr(bob, method)(path, **({"data": {"csrf": t}} if method == "post" else {}))
        assert r.status_code == 403, path
    assert site.post("/admin/schema/types", data={"key": "movie", "label": "Movie"}).status_code in (403, 422)


def test_deleting_a_field_from_the_ui(site):  # noqa: F811
    signup(site, "alice")
    t = token(site, "/admin/schema")
    site.post("/admin/schema/game/fields", data={"csrf": t, "key": "engine", "label": "Engine", "kind": "text",
                                                 "public": "on", "editable": "on"})
    assert "Engine" in site.get("/admin/schema/game").text
    assert site.post("/admin/schema/game/fields/engine/delete", data={"csrf": t}).status_code == 303
    assert "Engine" not in site.get("/admin/schema/game").text
    assert site.post("/admin/schema/game/fields/engine/delete", data={"csrf": t}).status_code == 404


# ---- federation ---------------------------------------------------------------------------------------

def test_schema_and_values_replicate_and_local_definitions_win(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    with m_maker() as s:  # the mirror's own definition of "rating" must survive
        custom.seed(s)
        custom.save_type(s, m_ctx, "movie", "My Movies")
        custom.save_field(s, m_ctx, "movie", "rating", "My rating", "text")
        s.commit()
    with a_maker() as s:
        custom.seed(s)
        custom.save_type(s, a_ctx, "movie", "Movie", labels={"developer": "Director"})
        custom.save_type(s, a_ctx, "miniseries", "Miniseries")
        custom.save_field(s, a_ctx, "movie", "rating", "Rating", "choice", choices="G\nPG")
        custom.save_field(s, a_ctx, "movie", "runtime_min", "Runtime", "number")
        s.commit()
    with a_maker() as s:  # a record created as a movie (the type travels with the first observation)
        [r] = service.ingest(s, a_ctx, batch("curator", [{"external_id": "c1", "title": "Alien",
                                                          "release_date": "1979-05-25"}], kind="metadata"),
                             curator=True, media_type="movie")
        custom.set_values(s, a_ctx, r.canonical_id, "curator", {"runtime_min": 117}, redistributable=True)
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        types = {t.key: t for t in s.scalars(select(MediaType))}
        assert types["movie"].origin == "local" and types["movie"].label == "My Movies"  # local wins
        assert types["anime"].origin == "local"
        assert types["miniseries"].origin == "upstream"
        fields = {(f.media_type, f.key): f for f in s.scalars(select(FieldDef))}
        assert fields[("movie", "rating")].origin == "local" and fields[("movie", "runtime_min")].origin == "upstream"
        assert s.get(CustomValue, (r.canonical_id, "runtime_min", "curator")).value == 117
    # the authority drops a type and a field: the mirror follows for upstream-origin definitions only
    with a_maker() as s:
        custom.delete_field(s, a_ctx, "movie", "runtime_min")
        custom.delete_type(s, a_ctx, "miniseries")
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert s.get(MediaType, "miniseries") is None
        assert s.scalars(select(FieldDef).where(FieldDef.key == "runtime_min")).first() is None
        assert s.scalars(select(FieldDef).where(FieldDef.key == "rating", FieldDef.origin == "local")).first()


def test_inherit_schema_can_be_switched_off(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    m_ctx.settings.inherit_schema = False
    with a_maker() as s:
        custom.seed(s)
        custom.save_type(s, a_ctx, "movie", "Movie")
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert s.get(MediaType, "movie") is None


def test_schema_changes_are_published_only_by_authorities(session, ctx):
    custom.seed(session)
    custom.save_type(session, ctx, "movie", "Movie")
    session.commit()
    assert [e.kind for e in feed.read(session, 0, 10)] == ["schema"]
    local = Ctx(build_settings(role="local"), load_identity(session))
    custom.save_type(session, local, "anime", "Anime")
    session.commit()
    assert feed.head_seq(session) == 1
