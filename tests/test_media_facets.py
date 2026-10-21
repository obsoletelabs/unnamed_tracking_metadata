import io
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select, text

from atlas import db, facets, main, media, service, sync
from atlas.models import Achievement, Artwork, Canonical
from atlas.service import AtlasError

from .conftest import batch, build_settings
from .test_sync import curate, pair  # noqa: F401

PUBLIC = "93.184.216.34"


def png(w=4, h=3, fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def resolver(monkeypatch):
    table = {"img.test": PUBLIC, "evil.test": "10.0.0.5", "meta.test": "169.254.169.254", "lo.test": "127.0.0.1"}

    def fake(host, port, type=0, **kw):
        try:
            ip = table[host]
        except KeyError:
            ip = host  # literal addresses
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


@pytest.mark.parametrize("url", [
    "http://evil.test/a.png", "http://meta.test/latest", "http://lo.test/x.png", "http://127.0.0.1/x.png",
    "http://[::1]/x.png", "http://10.1.2.3/x.png", "http://img.test:8080/x.png", "https://user:pw@img.test/x.png",
    "ftp://img.test/x.png", "file:///etc/passwd", "https://img.test/x\\y.png", "https://img.test/a b.png",
])
def test_unsafe_urls_are_refused(resolver, url):
    with pytest.raises(media.MediaError):
        media.check_url(url, build_settings())


def test_public_url_is_accepted_and_pinned(resolver):
    assert media.check_url("https://img.test/a.png", build_settings()) == ("https", "img.test", 443, PUBLIC)


def test_host_allowlist(resolver):
    with pytest.raises(media.MediaError, match="host not allowed"):
        media.check_url("https://img.test/a.png", build_settings(media_allowed_hosts={"cdn.good.test"}))


def test_validate_image_rejects_non_images_and_bombs():
    s = build_settings()
    assert media.validate_image(png(), s)[0] == "png"
    assert media.validate_image(png(fmt="JPEG"), s)[:2] == ("jpg", "image/jpeg")
    for bad in (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", b"GIF89a" + b"x" * 5,
                b"", b"\x89PNG\r\n\x1a\n" + b"\0" * 20):
        with pytest.raises(media.MediaError):
            media.validate_image(bad, s)
    with pytest.raises(media.MediaError):
        media.validate_image(png(200, 200), build_settings(media_max_pixels=1000))


@pytest.mark.parametrize("fmt, ext, content_type", [
    ("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg"),
    ("WEBP", "webp", "image/webp"), ("GIF", "gif", "image/gif"),
])
def test_supported_image_formats_remain_cacheable(fmt, ext, content_type):
    assert media.validate_image(png(8, 6, fmt), build_settings()) == (ext, content_type, 8, 6)


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_follows_redirects_but_revalidates_every_hop(resolver, tmp_path):
    s = build_settings(media_dir=str(tmp_path))
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["host"]))
        if request.headers["host"] == "img.test" and request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://evil.test/private.png"})
        return httpx.Response(200, content=png())

    with pytest.raises(media.MediaError, match="routable"):
        media._request(client_for(handler), "https://img.test/hop", s)
    assert seen == [(PUBLIC, "img.test")]  # connected to the pinned IP, never to the private target
    assert media._request(client_for(handler), "https://img.test/ok.png", s)


def test_fetch_enforces_size_cap_even_without_content_length(resolver, tmp_path):
    s = build_settings(media_dir=str(tmp_path), media_max_bytes=2048)
    big = httpx.Response(200, content=b"\x89PNG" + b"0" * 5000)
    with pytest.raises(media.MediaError, match="too large"):
        media._request(client_for(lambda r: big), "https://img.test/x.png", s)


def seed(session, ctx):
    [r] = service.ingest(session, ctx, batch("steam", [{"external_id": "620", "title": "Portal 2",
                                                        "release_date": "2011-04-19"}]), system=True)
    session.commit()
    return r.canonical_id


def test_cache_pending_stores_validates_and_backs_off(session, ctx, resolver, tmp_path, maker):
    cid = seed(session, ctx)
    s = ctx.settings.model_copy(update={"media_dir": str(tmp_path)})
    facets.set_artwork(session, ctx, cid, "steam", [
        {"kind": "key_art", "url": "https://img.test/good.png"}, {"kind": "banner", "url": "https://img.test/bad.png"}])
    session.commit()

    def handler(request):
        return httpx.Response(200, content=png() if "good" in request.url.path else b"<html>nope</html>")

    assert media.cache_pending(maker, s, client_for(handler)) == 1
    good, bad = (session.scalars(select(Artwork).where(Artwork.kind == k)).one() for k in ("key_art", "banner"))
    session.refresh(good), session.refresh(bad)
    assert good.blob_hash and good.width == 4 and media.find_blob(s, good.blob_hash)[1] == "image/png"
    assert bad.blob_hash is None and bad.fail_count == 1 and bad.next_try_at is not None
    assert media.cache_pending(maker, s, client_for(handler)) == 0  # backed off, not retried immediately
    assert media.find_blob(s, "../../etc/passwd") is None


@pytest.mark.parametrize("bad_url", [
    "https://img.test:invalid/bad.png", "https://img.test:65536/bad.png", "https://[::1/bad.png",
])
def test_malformed_artwork_does_not_block_the_cache_queue(session, ctx, resolver, tmp_path, maker, bad_url):
    cid = seed(session, ctx)
    settings = ctx.settings.model_copy(update={"media_dir": str(tmp_path)})
    facets.set_artwork(session, ctx, cid, "curator", [
        {"kind": "banner", "url": bad_url, "priority": 0},
        {"kind": "key_art", "url": "https://img.test/good.png", "priority": 1},
    ])
    session.commit()

    assert media.cache_pending(maker, settings, client_for(lambda _: httpx.Response(200, content=png()))) == 1
    session.expire_all()
    bad = session.scalars(select(Artwork).where(Artwork.url == bad_url)).one()
    good = session.scalars(select(Artwork).where(Artwork.kind == "key_art")).one()
    assert bad.blob_hash is None and bad.fail_count == 1 and bad.next_try_at is not None
    assert good.blob_hash and media.find_blob(settings, good.blob_hash)[1] == "image/png"


def test_set_artwork_replaces_per_source_and_merge_moves_facets(session, ctx):
    a = seed(session, ctx)
    [b] = service.ingest(session, ctx, batch("gogplugin", [{"external_id": "1", "title": "Other Thing"}]))
    session.commit()
    facets.set_artwork(session, ctx, a, "steam", [{"kind": "banner", "url": "https://x.test/1.jpg"},
                                                  {"kind": "logo", "url": "https://x.test/2.png"}])
    facets.set_artwork(session, ctx, a, "steam", [{"kind": "banner", "url": "https://x.test/1.jpg"}])
    assert [r.kind for r in session.scalars(select(Artwork))] == ["banner"]
    facets.set_artwork(session, ctx, b.canonical_id, "gogplugin", [{"kind": "banner", "url": "https://x.test/1.jpg"},
                                                                   {"kind": "icon", "url": "https://x.test/i.png"}])
    facets.set_achievements(session, ctx, b.canonical_id, "gogplugin", [{"name": "First Win"}])
    service.merge_canonicals(session, ctx, b.canonical_id, a)
    session.commit()
    kinds = sorted(r.kind for r in session.scalars(select(Artwork).where(Artwork.canonical_id == a)))
    assert kinds == ["banner", "icon"]  # duplicate image kept once
    assert session.scalars(select(Achievement)).one().canonical_id == a


@pytest.mark.parametrize("bad", [
    {"kind": "weird", "url": "https://x.test/a.png"}, {"kind": "banner", "url": "javascript:alert(1)"},
    {"kind": "banner", "url": "https://x.test/a b.png"}, {"kind": "banner", "url": "https://x.test/a.png", "width": 0},
])
def test_artwork_validation(session, ctx, bad):
    cid = seed(session, ctx)
    with pytest.raises(AtlasError):
        facets.set_artwork(session, ctx, cid, "curator", [bad])


def test_achievement_merge_prefers_trusted_source_and_fills_gaps(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "rawg", [{"name": "Wake Up Call!", "global_percent": 80.5}])
    facets.set_achievements(session, ctx, cid, "steam", [
        {"name": "wake up call", "description": "Finish the intro", "external_id": "ACH_1", "hidden": False,
         "icon_url": "https://x.test/i.jpg"}, {"name": "Secret", "hidden": True}])
    merged = {m["name"]: m for m in facets.merged_achievements(session, ctx, cid)}
    assert merged["wake up call"]["global_percent"] == 80.5 and merged["wake up call"]["description"]
    assert merged["wake up call"]["sources"] == ["steam", "rawg"] and merged["Secret"]["hidden"]
    with pytest.raises(AtlasError):
        facets.set_achievements(session, ctx, cid, "curator", [{"name": "x", "global_percent": 120}])


def test_artwork_and_achievements_replicate_but_provider_art_does_not(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [r] = curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "Hades", "release_date": "2020-09-17"}])
    with a_maker() as s:
        facets.set_artwork(s, a_ctx, r.canonical_id, "curator", [{"kind": "key_art", "url": "https://x.test/h.png"}],
                           redistributable=True)
        facets.set_artwork(s, a_ctx, r.canonical_id, "steam", [{"kind": "banner", "url": "https://x.test/p.jpg"}])
        facets.set_achievements(s, a_ctx, r.canonical_id, "curator", [{"name": "Escape"}], redistributable=True)
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert [a.url for a in s.scalars(select(Artwork))] == ["https://x.test/h.png"]
        assert s.scalars(select(Achievement)).one().name == "Escape"


@pytest.fixture
def api(tmp_path, resolver):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/a.db", bootstrap_admin_token="m" * 40,
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False)
    app = main.create_app(s)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer " + "m" * 40
        c.settings = s
        c.state = app.state.atlas
        yield c


def test_api_artwork_achievements_and_media(api, tmp_path):
    body = {"connection": {"source_kind": "manual", "source": "manual", "source_scope": "ops"},
            "observations": [{"external_id": "1", "title": "Hades", "release_date": "2020-09-17"}]}
    cid = api.post("/api/v2/observations", json=body).json()["results"][0]["atlas"]["canonical_id"]
    assert api.put(f"/api/v2/games/{cid}/artwork", json={"items": [
        {"kind": "key_art", "url": "https://img.test/c.png"}, {"kind": "screenshot", "url": "https://img.test/s.png"}
    ]}).json() == {"count": 2}
    assert api.put(f"/api/v2/games/{cid}/artwork", json={"items": [{"kind": "x", "url": "https://a.b/c"}]}
                   ).status_code == 400
    assert api.put(f"/api/v2/games/{cid}/achievements", json={"items": [
        {"name": "Escape", "description": "Leave", "global_percent": 12.5}]}).json() == {"count": 1}
    art = api.get(f"/api/v2/games/{cid}/artwork").json()
    assert art["primary"]["key_art"]["cached"] is False and len(art["results"]) == 2
    assert api.get(f"/api/v2/games/{cid}/achievements").json()["results"][0]["global_percent"] == 12.5

    media.cache_pending(api.state.maker, api.settings, client_for(lambda r: httpx.Response(200, content=png())))
    art = api.get(f"/api/v2/games/{cid}/artwork").json()
    url = art["primary"]["key_art"]["media_url"]
    img = api.get(url)
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert "immutable" in img.headers["cache-control"] and img.headers["x-content-type-options"] == "nosniff"
    assert api.get("/api/v2/media/" + "0" * 64).status_code == 404
    assert api.get("/api/v2/media/..%2f..%2fetc%2fpasswd").status_code in (404, 422)


def test_provider_fetch_attaches_artwork_and_achievements(api):
    from .test_providers import STEAM_DETAILS

    def handler(request):
        if "appdetails" in request.url.path:
            data = {"620": {"success": True, "data": {**STEAM_DETAILS["620"]["data"],
                    "header_image": "https://cdn.test/h.jpg", "screenshots": [{"path_full": "https://cdn.test/s1.jpg"}]}}}
            return httpx.Response(200, json=data)
        if "GlobalAchievementPercentages" in request.url.path:
            return httpx.Response(200, json={"achievementpercentages": {"achievements": [
                {"name": "ACH_1", "percent": "45.5"}]}})
        if "GetSchemaForGame" in request.url.path:
            return httpx.Response(200, json={"game": {"availableGameStats": {"achievements": [
                {"name": "ACH_1", "displayName": "Wake Up Call", "hidden": 0, "description": "Wake up",
                 "icon": "https://cdn.test/a.jpg"}]}}})
        return httpx.Response(404)

    api.state.providers.client = client_for(handler)
    api.state.base = api.state.base.model_copy(update={"steam_api_key": "k"})  # live settings derive from base
    api.state.providers.settings = api.state.base
    api.state.ctx.settings = api.state.base
    r = api.post("/api/v2/providers/steam/fetch", json={"external_id": "620"})
    assert r.status_code == 200, r.text
    cid = r.json()["game"]["id"]
    kinds = {a["kind"] for a in api.get(f"/api/v2/games/{cid}/artwork").json()["results"]}
    assert {"banner", "key_art", "screenshot"} <= kinds
    ach = api.get(f"/api/v2/games/{cid}/achievements").json()["results"]
    assert ach[0]["name"] == "Wake Up Call" and ach[0]["global_percent"] == 45.5


def test_upgrade_preserves_existing_rows(tmp_path):
    url = f"sqlite:///{tmp_path}/u.db"
    db.upgrade(url, "0001")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO canonical (id, norm_title, view, explain, version, created_at, updated_at) "
                       "VALUES ('atlas:old', 'old', '{}', '{}', 1, '2024-01-01', '2024-01-01')"))
    db.upgrade(url)
    with engine.connect() as c:
        assert c.execute(text("SELECT media_type FROM canonical")).scalar() == "game"
    assert Canonical.__table__.c.media_type.server_default is not None


def test_unwritable_media_dir_is_reported_and_does_not_penalise_artwork(
        session, ctx, resolver, tmp_path, maker, monkeypatch):
    cid = seed(session, ctx)
    s = ctx.settings.model_copy(update={"media_dir": str(tmp_path)})
    facets.set_artwork(session, ctx, cid, "steam", [{"kind": "key_art", "url": "https://img.test/good.png"}])
    session.commit()

    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(media.Path, "mkdir", refuse)
    with pytest.raises(media.MediaStorageError, match="ATLAS_MEDIA_DIR"):
        media.cache_pending(maker, s, client_for(lambda _: httpx.Response(200, content=png())))
    session.expire_all()
    art = session.scalars(select(Artwork)).one()
    # The image is fine; the node's storage is not. It must stay eligible for the next pass.
    assert art.blob_hash is None and art.fail_count == 0 and art.next_try_at is None


def test_storage_problem_detects_unwritable_directory(tmp_path):
    assert media.storage_problem(build_settings(media_dir=str(tmp_path / "new" / "dir"))) is None
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert "ATLAS_MEDIA_DIR" in media.storage_problem(build_settings(media_dir=str(blocker / "media")))


def test_worker_logs_an_actionable_storage_message(monkeypatch, maker, ctx, caplog):
    from .test_media_worker import StopAfterWaits

    def broken(*_):
        raise media.MediaStorageError("cannot write to ATLAS_MEDIA_DIR (/media)")

    monkeypatch.setattr(media, "cache_pending", broken)
    main._cache_media(maker, ctx, StopAfterWaits(1))
    assert "ATLAS_MEDIA_DIR" in caplog.text


def test_health_reports_media_storage(tmp_path, resolver):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = build_settings(database_url=f"sqlite:///{tmp_path}/h.db", media_dir=str(blocker / "media"),
                       media_cache_enabled=False)
    with TestClient(main.create_app(s)) as c:
        body = c.get("/api/v2/health").json()
    assert body["status"] == "ok" and body["media"] == "unwritable"
