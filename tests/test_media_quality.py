"""C12 media quality (A18): measured dimensions and aspect classes, exact duplicates by content hash, near
duplicates reported, regional and language variants, licence and attribution with the feed downgrade, per-source
media policy, metadata-free uploads, and the safe fetch limits (https only, no https -> http redirect, one deadline,
Content-Type pre-check, frame caps, per-host limits, error codes). Every image is generated here."""

from __future__ import annotations

import io
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageSequence
from sqlalchemy import select

from atlas import facets, main, media, service
from atlas.models import Artwork, ChangeLog
from atlas.service import AtlasError

from .conftest import batch, build_settings
from .test_media_facets import PUBLIC, client_for, png, resolver  # noqa: F401  (fixture)


def seed(session, ctx, ext="77", title="Lantern Bay"):
    [r] = service.ingest(session, ctx, batch("steam", [{"external_id": ext, "title": title}]), system=True)
    session.commit()
    return r.canonical_id


def image(w, h, fmt="PNG", color=(200, 30, 30), **save) -> bytes:
    out = io.BytesIO()
    img = Image.new("RGB", (w, h), color)
    for x in range(0, w, max(1, w // 4)):  # some structure, so the difference hash has something to read
        for y in range(h):
            img.putpixel((x, y), (10, 10, 10))
    img.save(out, fmt, **save)
    return out.getvalue()


def serve(data_by_url, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for url, data in data_by_url.items():
            if url.endswith(path):
                return httpx.Response(200, content=data, headers=headers or {"content-type": "image/png"})
        return httpx.Response(404)
    return client_for(handler)


def arts(session, cid):
    session.expire_all()
    return {a.url: a for a in session.scalars(select(Artwork).where(Artwork.canonical_id == cid))}


# ---- dimensions, aspect, role fit (§12.5.2, §12.5.3) -------------------------------------------------------

@pytest.mark.parametrize(("w", "h", "cls"), [(100, 200, "tall"), (600, 900, "portrait"), (100, 100, "square"),
                                             (1920, 1080, "landscape"), (460, 215, "capsule"), (3840, 1240, "wide"),
                                             (80, 100, "other"), (None, 10, None)])
def test_aspect_classes(w, h, cls):
    assert media.aspect_class(w, h) == cls


def test_measured_size_wins_and_the_declaration_is_kept(session, ctx, resolver, tmp_path, maker):  # noqa: F811
    cid = seed(session, ctx)
    facets.set_artwork(session, ctx, cid, "curator", [
        {"kind": "key_art", "url": "https://img.test/k.png", "width": 600, "height": 900},
        {"kind": "icon", "url": "https://img.test/i.png"}])
    session.commit()
    s = ctx.settings.model_copy(update={"media_dir": str(tmp_path)})
    media.cache_pending(maker, s, serve({"https://img.test/k.png": image(320, 180),
                                         "https://img.test/i.png": image(64, 64)}))
    k, i = arts(session, cid)["https://img.test/k.png"], arts(session, cid)["https://img.test/i.png"]
    assert (k.width, k.height, k.declared_width, k.declared_height, k.aspect) == (320, 180, 600, 900, "landscape")
    assert facets.role_fit(k) is False and facets.role_fit(i) is True and len(k.dhash) == 16
    # a later declaration changes declared_* only: the measurement stays
    facets.set_artwork(session, ctx, cid, "curator", [
        {"kind": "key_art", "url": "https://img.test/k.png", "width": 700, "height": 1000},
        {"kind": "icon", "url": "https://img.test/i.png"}])
    session.commit()
    k = arts(session, cid)["https://img.test/k.png"]
    assert (k.width, k.height, k.declared_width, k.declared_height) == (320, 180, 700, 1000)
    assert facets.artwork_items([k])[0]["width"] == 700  # a snapshot round-trips the declaration


def test_backfill_measures_images_cached_before_the_upgrade(session, ctx, tmp_path, maker):
    cid = seed(session, ctx)
    s = ctx.settings.model_copy(update={"media_dir": str(tmp_path)})
    data = image(200, 300)
    digest = media.store(s, data, "png")
    session.add(Artwork(canonical_id=cid, source="curator", kind="poster", url="https://img.test/p.png",
                        width=10, height=10, blob_hash=digest, content_type="image/png", size_bytes=len(data)))
    session.add(Artwork(canonical_id=cid, source="curator", kind="logo", url="https://img.test/gone.png",
                        blob_hash="0" * 64))
    session.commit()
    assert media.pending_backfill(session) == 2
    assert media.backfill(maker, s) == 1
    rows = arts(session, cid)
    p = rows["https://img.test/p.png"]
    assert (p.width, p.height, p.declared_width, p.declared_height, p.aspect) == (200, 300, 10, 10, "portrait")
    # a remote, proxy-fetched image whose blob went missing is recoverable: the stale hash is cleared so
    # `cache_pending` treats it as uncached and fetches it again, rather than being stuck forever (review
    # finding: a recoverable remote image stayed permanently uncached after the missing-file detection).
    gone = rows["https://img.test/gone.png"]
    assert gone.blob_hash is None and gone.last_error is None and gone.fail_count == 0
    assert media.pending_backfill(session) == 0


def test_backfill_cannot_recover_a_missing_local_upload(session, ctx, tmp_path, maker):
    """A local-only upload (this node's own `/media/<sha256>`) has no remote source to refetch: if its file is
    gone, it stays a terminal `storage` error instead of being retried forever."""
    cid = seed(session, ctx)
    s = ctx.settings.model_copy(update={"media_dir": str(tmp_path)})
    own_url = media.own_media_base(s) + "0" * 64
    session.add(Artwork(canonical_id=cid, source="manual", kind="poster", url=own_url, blob_hash="0" * 64))
    session.commit()
    assert media.backfill(maker, s) == 0
    rows = arts(session, cid)
    art = rows[own_url]
    assert art.last_error == "storage" and art.blob_hash == "0" * 64
    assert media.pending_backfill(session) == 0


# ---- duplicates (§12.5.4) ------------------------------------------------------------------------------------

def test_exact_duplicates_point_at_the_best_row_and_are_left_out_on_request(session, ctx, resolver, tmp_path,  # noqa: F811
                                                                            maker):
    cid = seed(session, ctx)
    same = image(300, 450)
    facets.set_artwork(session, ctx, cid, "steam", [{"kind": "key_art", "url": "https://img.test/a.png"}])
    facets.set_artwork(session, ctx, cid, "curator", [{"kind": "key_art", "url": "https://img.test/b.png"}])
    session.commit()
    s = ctx.settings.model_copy(update={"media_dir": str(tmp_path)})
    media.cache_pending(maker, s, serve({"https://img.test/a.png": same, "https://img.test/b.png": same}))
    rows = arts(session, cid)
    a, b = rows["https://img.test/a.png"], rows["https://img.test/b.png"]
    assert a.blob_hash == b.blob_hash and b.duplicate_of is None and a.duplicate_of == b.id  # the curator's stays
    assert [x.url for x in facets.merged_artwork(session, ctx, cid, include_duplicates=False)] == [b.url]
    assert len(facets.merged_artwork(session, ctx, cid)) == 2  # never deleted for being a duplicate
    facets.set_artwork(session, ctx, cid, "curator", [])  # the best one goes: the other is no longer a duplicate
    session.commit()
    assert arts(session, cid)["https://img.test/a.png"].duplicate_of is None


def test_unmerge_and_detach_recompute_exact_duplicates(session, ctx, resolver, tmp_path, maker):  # noqa: F811
    """§12.5.4: duplicates are recomputed when rows part again. After an unmerge or a detach no row may keep a
    `duplicate_of` that points at a row of the other record, or the gallery and `primary` lose its image."""
    from atlas import merges
    from atlas.service import Ctx

    lctx = Ctx(settings=ctx.settings.model_copy(update={"role": "local", "media_dir": str(tmp_path)}),
               identity=ctx.identity)
    same = image(300, 450)
    a, b = seed(session, lctx, "1", "Lantern Bay"), seed(session, lctx, "2", "Harbour Lights")
    facets.set_artwork(session, lctx, a, "curator", [{"kind": "key_art", "url": "https://img.test/a.png"}])
    facets.set_artwork(session, lctx, b, "steam", [{"kind": "key_art", "url": "https://img.test/b.png"}])
    session.commit()
    media.cache_pending(maker, lctx.settings, serve({"https://img.test/a.png": same, "https://img.test/b.png": same}))
    service.merge_canonicals(session, lctx, a, b, actor="cur")
    session.commit()
    on_b = arts(session, b)
    assert on_b["https://img.test/b.png"].duplicate_of == on_b["https://img.test/a.png"].id  # the curator's wins
    from atlas.models import MergeRecord

    rec = session.scalars(select(MergeRecord).where(MergeRecord.src == a)).one()
    merges.unmerge(session, lctx, rec.id, actor="cur", remember_distinct=False)
    session.commit()
    steam_row, curated_row = arts(session, b)["https://img.test/b.png"], arts(session, a)["https://img.test/a.png"]
    assert steam_row.duplicate_of is None and curated_row.duplicate_of is None
    assert [x.url for x in facets.merged_artwork(session, lctx, b, include_duplicates=False)] == [steam_row.url]

    # detach: a gog chain on b with the same bytes; splitting it off clears the pointer on whichever row had it
    service.ingest(session, lctx, batch("gog", [{"external_id": "g2", "title": "Harbour Lights",
                                                  "provider_ids": {"steam": "2"}}]), curator=True)
    facets.set_artwork(session, lctx, b, "gog", [{"kind": "key_art", "url": "https://img.test/g.png"}])
    session.commit()
    media.cache_pending(maker, lctx.settings, serve({"https://img.test/g.png": same}))
    pair = arts(session, b)
    assert sum(1 for x in pair.values() if x.duplicate_of is not None) == 1
    from atlas.models import Observation

    gog_obs = session.scalars(select(Observation).where(Observation.source == "gog",
                                                        Observation.canonical_id == b)).one()
    split = service.detach_observation(session, lctx, gog_obs.id, actor="cur")
    session.commit()
    assert arts(session, b)["https://img.test/b.png"].duplicate_of is None
    assert arts(session, split.id)["https://img.test/g.png"].duplicate_of is None


def test_near_duplicates_are_reported_and_never_hidden(session, ctx):
    cid = seed(session, ctx)
    for n, h in enumerate(("ffff0000ffff0000", "ffff0000ffff0001", "0123456789abcdef")):
        session.add(Artwork(canonical_id=cid, source="curator", kind="screenshot", url=f"https://img.test/s{n}.png",
                            priority=n, width=1920, height=1080, aspect="landscape", dhash=h, blob_hash=f"{n}" * 64))
    session.commit()
    rows = facets.merged_artwork(session, ctx, cid)
    near = facets.near_duplicates(rows)
    by_url = {a.url: a.id for a in rows}
    assert near == {by_url["https://img.test/s1.png"]: by_url["https://img.test/s0.png"]}
    assert len(facets.merged_artwork(session, ctx, cid, include_duplicates=False)) == 3


def test_the_legacy_order_of_one_batch_does_not_depend_on_the_clock(session, ctx, monkeypatch):
    """The images one `set_artwork` call adds share one age, so the legacy order inside the batch is url order on a
    fine clock (Linux: every read differs) as on a coarse one (Windows: equal reads); the client fixtures broke on
    exactly that (A24)."""
    import atlas.models as models

    ticks = iter(range(1, 10_000))

    class FineClock(models.datetime):
        @classmethod
        def now(cls, tz=None):
            return models.datetime(2026, 1, 1, tzinfo=tz) + timedelta(microseconds=next(ticks))

    monkeypatch.setattr(models, "datetime", FineClock)
    cid = seed(session, ctx)
    kinds = ("key_art", "hero", "icon", "logo")  # insertion order differs from url order
    facets.set_artwork(session, ctx, cid, "curator", [{"kind": k, "url": f"https://img.test/g-{k}.png"} for k in kinds])
    session.commit()
    session.expire_all()
    rows = facets.merged_artwork(session, ctx, cid, order="legacy")
    assert [a.kind for a in rows] == sorted(kinds)
    assert len({a.created_at for a in rows}) == 1


# ---- ranking: role fit, region and language (§12.5.6, §12.5.7) ---------------------------------------------

def test_region_and_language_variants_rank_for_the_asked_region(session, ctx):
    cid = seed(session, ctx)
    facets.set_artwork(session, ctx, cid, "curator", [
        {"kind": "key_art", "url": "https://img.test/ww.png", "region": "ww", "priority": 100},
        {"kind": "key_art", "url": "https://img.test/jp.png", "region": "jp", "locale": "ja", "priority": 100},
        {"kind": "key_art", "url": "https://img.test/de.png", "region": "eu", "locale": "de", "priority": 100}])
    session.commit()
    first = lambda **kw: facets.merged_artwork(session, ctx, cid, **kw)[0].url  # noqa: E731
    assert first(region="jp") == "https://img.test/jp.png"
    assert first(region="eu", lang="de-at") == "https://img.test/de.png"
    assert first(region="us") == "https://img.test/ww.png"  # worldwide before another region
    for bad in ({"kind": "cover", "url": "https://img.test/c.png", "region": "Europe"},
                {"kind": "logo", "url": "https://img.test/c.png", "locale": "!!"},
                {"kind": "logo", "url": "https://img.test/c.png", "licence": "all-rights"},
                {"kind": "logo", "url": "https://img.test/c.png", "source_page_url": "http://img.test/page"}):
        with pytest.raises(AtlasError):
            facets.set_artwork(session, ctx, cid, "curator", [bad])
    assert facets.clean_artwork([{"kind": "cover", "url": "https://img.test/c.png"}])[0]["kind"] == "key_art"


# ---- licences and the feed (§12.5.5, H47) ------------------------------------------------------------------

def test_withheld_licences_and_local_only_images_stay_out_of_the_signed_feed(session, ctx, caplog):
    cid = seed(session, ctx)
    with caplog.at_level("INFO"):
        facets.set_artwork(session, ctx, cid, "curator", [
            {"kind": "hero", "url": "https://img.test/press.jpg", "licence": "publisher_press",
             "attribution": "Example Press"},
            {"kind": "logo", "url": "https://img.test/fan.png", "licence": "cc0", "local_only": True},
            {"kind": "banner", "url": "https://img.test/free.png", "licence": "cc0", "region": "eu", "width": 920,
             "height": 430}], redistributable=True)
    session.commit()
    [entry] = session.scalars(select(ChangeLog).where(ChangeLog.kind == "artwork")).all()
    assert entry.body["items"] == [{"kind": "banner", "url": "https://img.test/free.png", "priority": 100,
                                    "width": 920, "height": 430}]  # v1 fields only
    assert "licence_withheld" in caplog.text
    assert {a.url: a.licence for a in arts(session, cid).values()} == {
        "https://img.test/press.jpg": "publisher_press", "https://img.test/fan.png": "cc0",
        "https://img.test/free.png": "cc0"}  # everything is kept here


def test_an_upload_is_owner_upload_and_a_non_federable_licence_must_be_local_only(session, ctx):
    cid = seed(session, ctx)
    own = media.own_media_base(ctx.settings) + "a" * 64
    facets.set_artwork(session, ctx, cid, "curator", [{"kind": "logo", "url": own}], redistributable=True)
    session.commit()
    assert arts(session, cid)[own].licence == "owner_upload"
    with pytest.raises(AtlasError, match="licence_not_federable"):
        facets.set_artwork(session, ctx, cid, "curator", [{"kind": "logo", "url": own, "licence": "cc-by"}],
                           redistributable=True)
    facets.set_artwork(session, ctx, cid, "curator", [{"kind": "logo", "url": own, "licence": "cc-by",
                                                       "local_only": True}], redistributable=True)
    session.commit()
    assert arts(session, cid)[own].local_only is True


# ---- media policy (§12.5.5, H39, H50) ----------------------------------------------------------------------

def test_api_lists_exact_duplicates_only_on_request(tmp_path, resolver):  # noqa: F811
    """§12.5.4: exact duplicates are omitted from the API list unless ?include_duplicates=1."""
    s = build_settings(database_url=f"sqlite:///{tmp_path}/a.db", bootstrap_admin_token="m" * 40, role="local",
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False)
    with TestClient(main.create_app(s)) as api:
        api.headers["Authorization"] = "Bearer " + "m" * 40
        st = api.app.state.atlas
        with st.maker() as session:
            cid = seed(session, st.ctx)
            facets.set_artwork(session, st.ctx, cid, "steam", [{"kind": "key_art", "url": "https://img.test/a.png"}])
            facets.set_artwork(session, st.ctx, cid, "curator", [{"kind": "key_art", "url": "https://img.test/b.png"}])
            session.commit()
        same = image(300, 450)
        media.cache_pending(st.maker, s, serve({"https://img.test/a.png": same, "https://img.test/b.png": same}))
        plain = api.get(f"/api/v2/games/{cid}/artwork").json()
        assert [r["url"] for r in plain["results"]] == ["https://img.test/b.png"]
        assert plain["primary"]["key_art"]["url"] == "https://img.test/b.png"
        full = api.get(f"/api/v2/games/{cid}/artwork?include_duplicates=1").json()
        assert [r["url"] for r in full["results"]] == ["https://img.test/b.png", "https://img.test/a.png"]
        assert full["results"][1]["duplicate_of"] == full["results"][0]["id"]


def test_api_keeps_the_legacy_order_unless_quality_is_asked(tmp_path, resolver):  # noqa: F811
    """D2 (amendment A18 #18): a client that takes the first image keeps getting it. The default list keeps the
    pre-C12 order (priority across kinds), `best` is the §12.5.7 pick per kind, and `order=quality` opts in."""
    s = build_settings(database_url=f"sqlite:///{tmp_path}/a.db", bootstrap_admin_token="m" * 40, role="local",
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False)
    with TestClient(main.create_app(s)) as api:
        api.headers["Authorization"] = "Bearer " + "m" * 40
        st = api.app.state.atlas
        with st.maker() as session:
            gid = seed(session, st.ctx)
            session.commit()
        assert api.put(f"/api/v2/games/{gid}/artwork", json={"items": [
            {"kind": "key_art", "url": "https://img.test/square.png", "width": 1, "height": 1, "priority": 10},
            {"kind": "key_art", "url": "https://img.test/tall.png", "width": 600, "height": 900, "priority": 15},
            {"kind": "logo", "url": "https://img.test/logo.png", "priority": 20}]}).status_code == 200
        media.cache_pending(st.maker, s, serve({"https://img.test/square.png": image(40, 40),
                                                "https://img.test/tall.png": image(60, 90),
                                                "https://img.test/logo.png": image(90, 30)}))
        with st.maker() as session:  # what was stored, not only what the route says
            stored = {a.url: a for a in session.scalars(select(Artwork).where(Artwork.canonical_id == gid))}
            assert facets.role_fit(stored["https://img.test/square.png"]) is False
            assert facets.role_fit(stored["https://img.test/tall.png"]) is True

        legacy = api.get(f"/api/v2/games/{gid}/artwork").json()
        assert legacy["order"] == "legacy"
        assert [r["url"] for r in legacy["results"]] == [
            "https://img.test/square.png", "https://img.test/tall.png", "https://img.test/logo.png"]
        assert legacy["primary"]["key_art"]["url"] == "https://img.test/square.png"  # unchanged for old clients
        assert legacy["best"]["key_art"]["url"] == "https://img.test/tall.png"  # the misfit ranks lower
        assert legacy["best"]["logo"]["url"] == "https://img.test/logo.png"
        assert api.get(f"/api/v2/games/{gid}/artwork?order=legacy").json() == legacy

        quality = api.get(f"/api/v2/games/{gid}/artwork?order=quality").json()
        assert quality["order"] == "quality"
        assert [r["url"] for r in quality["results"]] == [
            "https://img.test/tall.png", "https://img.test/logo.png", "https://img.test/square.png"]
        assert quality["primary"] == quality["best"] == legacy["best"]
        assert api.get(f"/api/v2/games/{gid}/artwork?order=best").status_code == 422


def test_policy_none_or_redirect_stops_downloads_and_serving(tmp_path, resolver):  # noqa: F811
    s = build_settings(database_url=f"sqlite:///{tmp_path}/a.db", bootstrap_admin_token="m" * 40, role="local",
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False)
    with TestClient(main.create_app(s)) as api:
        api.headers["Authorization"] = "Bearer " + "m" * 40
        st = api.app.state.atlas
        with st.maker() as session:
            cid = seed(session, st.ctx)
            facets.set_artwork(session, st.ctx, cid, "steam", [{"kind": "key_art", "url": "https://img.test/s.png"}])
            session.commit()
        media.cache_pending(st.maker, s, serve({"https://img.test/s.png": image(60, 90)}))
        item = api.get(f"/api/v2/games/{cid}/artwork").json()["results"][0]
        assert item["state"] == "cached" and api.get(item["media_url"]).status_code == 200
        assert api.get(item["media_url"].replace("/api/v2", "")).status_code == 200  # the web route
        st.ctx.settings = s.model_copy(update={"media_policy": {"steam": {"fetch": "none"}}})
        assert api.get(item["media_url"]).status_code == 404  # its cached bytes are no longer served
        assert api.get(item["media_url"].replace("/api/v2", "")).status_code == 404
        with st.maker() as session:
            facets.set_artwork(session, st.ctx, cid, "steam", [{"kind": "key_art", "url": "https://img.test/s.png"},
                                                               {"kind": "hero", "url": "https://img.test/h.png"}])
            session.commit()
        calls = []
        media.cache_pending(st.maker, st.ctx.settings, client_for(lambda r: calls.append(r) or httpx.Response(500)))
        assert calls == []  # never downloaded under `none`
    with pytest.raises(ValueError, match="fetch must be one of"):
        build_settings(media_policy={"steam": {"fetch": "sometimes"}})
    with pytest.raises(ValueError, match="an object with"):
        build_settings(media_policy={"steam": {"cache": True}})


# ---- uploads (§12.6.2, H41) ----------------------------------------------------------------------------------

def test_uploads_are_re_encoded_without_metadata(settings):
    exif = Image.Exif()
    exif[0x010E] = "secret location note"  # ImageDescription
    jpeg = image(40, 30, "JPEG", exif=exif.tobytes())
    assert b"secret location note" in jpeg
    data, ext, ctype, w, h = media.reencode_upload(jpeg, settings)
    assert (ext, ctype, w, h) == ("jpg", "image/jpeg", 40, 30) and b"secret location note" not in data
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "private text chunk")
    with_text = image(20, 20, "PNG", pnginfo=info)
    data, ext, *_ = media.reencode_upload(with_text, settings)
    assert ext == "png" and b"private text chunk" not in data and b"private text chunk" in with_text
    frames = [Image.new("RGB", (8, 8), (80 * n, 40, 200 - 60 * n)) for n in range(3)]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=50, comment=b"gif comment")
    data, ext, *_ = media.reencode_upload(out.getvalue(), settings)
    with Image.open(io.BytesIO(data)) as img:
        assert ext == "gif" and img.n_frames == 3 and b"gif comment" not in data
    with pytest.raises(media.MediaError):
        media.reencode_upload(b"<svg xmlns='http://www.w3.org/2000/svg'/>", settings)


def test_re_encoding_keeps_what_is_drawn(settings):
    """Stripping metadata must not change the picture: palette transparency, the EXIF orientation (applied before
    the EXIF goes), WebP and GIF frame timing and loop, and every APNG frame survive."""
    def encode(img, fmt, **save):
        out = io.BytesIO()
        img.save(out, fmt, **save)
        return out.getvalue()

    pal = Image.new("P", (10, 10), 0)
    pal.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    pal.putpixel((5, 5), 1)
    data, ext, *_ = media.reencode_upload(encode(pal, "PNG", transparency=0), settings)
    with Image.open(io.BytesIO(data)) as img:
        assert ext == "png" and img.mode == "P" and img.info.get("transparency") == 0  # a logo stays see-through

    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 degrees clockwise to display
    data, ext, _ctype, w, h = media.reencode_upload(image(40, 30, "JPEG", exif=exif.tobytes()), settings)
    with Image.open(io.BytesIO(data)) as img:
        assert (ext, w, h) == ("jpg", 30, 40) and not img.getexif()  # shown upright, and no EXIF left

    frames = [Image.new("RGBA", (8, 8), (80 * n, 40, 200 - 60 * n, 255)) for n in range(2)]
    webp = encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=[70, 130], loop=3,
                  lossless=True)
    data, ext, *_ = media.reencode_upload(webp, settings)
    with Image.open(io.BytesIO(data)) as img:
        timing = []
        for frame in ImageSequence.Iterator(img):
            frame.load()
            timing.append(frame.info.get("duration"))
        assert ext == "webp" and img.n_frames == 2 and timing == [70, 130] and img.info.get("loop") == 3

    gif = encode(frames[0].convert("RGB"), "GIF", save_all=True, append_images=[frames[1].convert("RGB")],
                 duration=[40, 90], loop=2)
    data, *_ = media.reencode_upload(gif, settings)
    with Image.open(io.BytesIO(data)) as img:
        timing = []
        for frame in ImageSequence.Iterator(img):
            frame.load()
            timing.append(frame.info.get("duration"))
        assert timing == [40, 90] and img.info.get("loop") == 2

    apng = encode(frames[0], "PNG", save_all=True, append_images=frames[1:], duration=[50, 60])
    data, ext, *_ = media.reencode_upload(apng, settings)
    with Image.open(io.BytesIO(data)) as img:
        assert ext == "png" and getattr(img, "n_frames", 1) == 2 and getattr(img, "is_animated", False)


# ---- safe fetch (§12.6) --------------------------------------------------------------------------------------

def test_plain_http_and_https_to_http_redirects_are_refused(resolver, settings):  # noqa: F811
    with pytest.raises(media.MediaError, match="https required") as exc:
        media.check_url("http://img.test/a.png", settings)
    assert exc.value.code == "https_required"
    assert media.check_url("http://img.test/a.png", settings.model_copy(update={"media_https_only": False}))
    hop = client_for(lambda r: httpx.Response(302, headers={"location": "http://img.test/b.png"}))
    with pytest.raises(media.MediaError) as exc:
        media._request(hop, "https://img.test/a.png", settings.model_copy(update={"media_https_only": False}))
    assert exc.value.code == "https_required"  # a downgrade inside a redirect chain, even when http is allowed


@pytest.mark.parametrize(("url", "code"), [
    ("https://evil.test/a.png", "not_public"), ("https://meta.test/latest", "not_public"),
    ("https://lo.test/a.png", "not_public"), ("https://127.0.0.1/a.png", "not_public"),
    ("https://[::1]/a.png", "not_public"), ("https://[64:ff9b::a00:5]/a.png", "not_public"),
    ("https://img.test:8443/a.png", "port_not_allowed"), ("https://u:p@img.test/a.png", "invalid_url"),
    ("ftp://img.test/a.png", "invalid_url"), ("https://img.test/a b.png", "invalid_url")])
def test_ssrf_refusals_carry_their_error_code(resolver, settings, url, code):  # noqa: F811
    with pytest.raises(media.MediaError) as exc:
        media.check_url(url, settings)
    assert exc.value.code == code


def test_a_redirect_to_a_private_address_is_refused_at_the_hop(resolver, settings):  # noqa: F811
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.test/x.png"})

    with pytest.raises(media.MediaError) as exc:
        media._request(client_for(handler), "https://img.test/a.png", settings)
    assert exc.value.code == "not_public" and seen == [PUBLIC]  # the private host was never contacted


@pytest.mark.parametrize(("headers", "status", "code"), [
    ({"content-type": "text/html"}, 200, "bad_type"), ({"content-type": "image/png"}, 404, "status_404"),
    ({"content-type": "image/png"}, 410, "status_410"), ({"content-type": "image/png"}, 403, "status_4xx"),
    ({"content-type": "image/png"}, 503, "status_5xx"), ({"content-length": str(10 ** 9)}, 200, "too_large")])
def test_responses_are_checked_before_the_body_is_used(resolver, settings, headers, status, code):  # noqa: F811
    client = client_for(lambda r: httpx.Response(status, content=png(), headers=headers))
    with pytest.raises(media.MediaError) as exc:
        media._request(client, "https://img.test/a.png", settings)
    assert exc.value.code == code


def test_a_missing_content_type_is_decided_by_the_bytes(resolver, settings):  # noqa: F811
    data = media._request(client_for(lambda r: httpx.Response(200, content=png())), "https://img.test/a.png",
                          settings)
    assert media.validate_image(data, settings)[0] == "png"


def test_one_deadline_covers_the_whole_download(resolver, settings, monkeypatch):  # noqa: F811
    clock = iter(range(0, 1000, 15))  # every reading of the clock is 15 s later
    monkeypatch.setattr(media.time, "monotonic", lambda: float(next(clock)))
    big = client_for(lambda r: httpx.Response(200, content=png(), headers={"content-type": "image/png"}))
    with pytest.raises(media.MediaError) as exc:
        media._request(big, "https://img.test/a.png", settings.model_copy(update={"media_fetch_deadline_s": 20}))
    assert exc.value.code == "timeout"


def test_animated_images_are_capped_by_frames_and_total_pixels(settings):
    frames = [Image.new("RGB", (16, 16), (20 * n, 255 - 20 * n, 90)) for n in range(12)]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=20)
    data = out.getvalue()
    assert media.validate_image(data, settings)[0] == "gif"
    with pytest.raises(media.MediaError) as exc:
        media.validate_image(data, settings.model_copy(update={"media_max_frames": 10}))
    assert exc.value.code == "dimensions"
    with pytest.raises(media.MediaError) as exc:
        media.validate_image(data, settings.model_copy(update={"media_max_pixels": 16 * 16 * 2}))
    assert exc.value.code == "dimensions"


def test_the_per_host_rate_limit_defers_without_counting_a_failure(session, ctx, resolver, tmp_path, maker):  # noqa: F811
    cid = seed(session, ctx)
    facets.set_artwork(session, ctx, cid, "curator", [{"kind": "screenshot", "url": f"https://img.test/{n}.png"}
                                                       for n in range(3)])
    session.commit()
    s = ctx.settings.model_copy(update={"media_dir": str(tmp_path), "media_host_rpm": 2})
    data = {f"https://img.test/{n}.png": image(64 + n, 36) for n in range(3)}
    assert media.cache_pending(maker, s, serve(data)) == 2
    waiting = [a for a in arts(session, cid).values() if a.blob_hash is None]
    assert len(waiting) == 1 and waiting[0].fail_count == 0 and waiting[0].next_try_at is not None
    assert waiting[0].last_error == "timeout"


def test_failures_record_their_code_and_the_api_reports_the_state(session, ctx, resolver, tmp_path, maker):  # noqa: F811
    cid = seed(session, ctx)
    facets.set_artwork(session, ctx, cid, "curator", [{"kind": "logo", "url": "https://img.test/missing.png"}])
    session.commit()
    s = ctx.settings.model_copy(update={"media_dir": str(tmp_path)})
    media.cache_pending(maker, s, serve({}))
    art = arts(session, cid)["https://img.test/missing.png"]
    assert (art.last_error, art.fail_count) == ("status_404", 1)
    assert media.describe_state(s, art) == "failing"


@pytest.mark.parametrize("release", ["this", "1.1"])
def test_followers_park_an_artwork_v2_body(tmp_path, monkeypatch, release):

    from .test_prices import play
    from .test_sync_compat import Node, pull

    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    mirror = Node(tmp_path, "mirror", "mirror")
    gid = authority.publish("Park Art")
    seq = authority.emit("artwork", {"_v": 2, "canonical_id": gid, "source": "curator", "_requires": ["art-v2"],
                                     "items": [{"kind": "trailer", "url": "https://img.test/t.mp4",
                                                "licence": "cc0"}]})
    with monkeypatch.context() as m:
        play(m, release)
        pull(mirror, authority)
        [(fseq, kind, disposition, _reason)] = mirror.failures(authority)
        assert (fseq, kind, disposition) == (seq, "artwork", "parked")
    assert seq in mirror.retained(authority)
    with mirror.st.maker() as s:
        assert s.scalars(select(Artwork)).all() == []
