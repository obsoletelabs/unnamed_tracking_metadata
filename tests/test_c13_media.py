"""CONTRACT-C13 §6 (A19): a follower asks the peer that delivered an artwork row for its bytes first, keeps why
each source failed, and a tile falls back to another cached image of the record.

Real nodes on SQLite files; the follower's outbound HTTP lands on the authority app in-process, and an origin URL
on a reserved `.invalid` host can never resolve, so "the origin is down" needs no network."""

from __future__ import annotations

import hashlib
import io

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from atlas import facets, media, secrets_box
from atlas.models import Artwork, Peer

from .test_c13_follower import provider_only
from .test_c13_peers import routed
from .test_sync_compat import Node, pull

ORIGIN = "https://img.invalid/key.png"


def png(colour=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 96), colour).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def pair(tmp_path):
    authority = Node(tmp_path, "authority", "authority", federation_bridge_record_obs=True)
    mirror = Node(tmp_path, "mirror", "mirror", secret_key="k" * 32)
    cid = provider_only(authority)
    data = png()
    with authority.st.maker() as s:  # the authority holds the bytes (cached earlier, or uploaded)
        facets.set_artwork(s, authority.st.ctx, cid, "curator", [{"kind": "key_art", "url": ORIGIN}],
                           redistributable=True)
        art = s.scalars(select(Artwork)).one()
        art.blob_hash, art.content_type = media.store(authority.st.ctx.settings, data, "png"), "image/png"
        art.licence = "cc0"  # a licence that may federate as bytes (C12 §12.5.5)
        s.commit()
        digest = art.blob_hash
    pull(mirror, authority)
    with mirror.st.maker() as s:
        s.add(Peer(name="Central", url="http://authority.test", node_id=authority.node_id, follow="own",
                   public_key=authority.st.ctx.identity.trust_public_b64,
                   token_enc=secrets_box.encrypt(mirror.st.ctx.settings, authority.token)))
        s.commit()
    return authority, mirror, cid, digest, data


def cache(mirror: Node, authority: Node) -> Artwork:
    with mirror.st.maker() as s, routed({"authority.test": authority}) as http:
        art = s.scalars(select(Artwork)).one()
        media.cache_one(s, mirror.st.ctx.settings, art, http)
        s.commit()
    with mirror.st.maker() as s:
        return s.scalars(select(Artwork)).one()


def test_the_peer_serves_only_federable_bytes(pair):
    """T-M1: `/api/v2/sync/media/<sha256>` needs the sync scope and serves only blobs of redistributable,
    not-local-only rows."""
    authority, mirror, cid, digest, data = pair
    auth = {"Authorization": f"Bearer {authority.token}"}
    ok = authority.client.get(f"/api/v2/sync/media/{digest}", headers=auth)
    assert ok.status_code == 200 and ok.content == data
    assert authority.client.get(f"/api/v2/sync/media/{digest}").status_code == 401
    assert authority.client.get(f"/api/v2/sync/media/{digest.upper()}", headers=auth).status_code == 422
    with authority.st.maker() as s:
        s.scalars(select(Artwork)).one().local_only = True
        s.commit()
    assert authority.client.get(f"/api/v2/sync/media/{digest}", headers=auth).status_code == 404
    assert authority.client.get(f"/api/v2/sync/media/{'0' * 64}", headers=auth).status_code == 404


@pytest.mark.parametrize("licence", [None, "cc-by", "provider_terms"])
def test_the_peer_serves_bytes_only_under_a_federable_licence(pair, licence):
    """T-M1b (C12 §12.5.5, H58 off): bytes of a curated URL row with an unknown licence (none stated), or one that
    keeps it out of the feed, are not handed to peers; with H58 on, the unknown-licence row is, the others never."""
    authority, mirror, cid, digest, data = pair
    auth = {"Authorization": f"Bearer {authority.token}"}
    with authority.st.maker() as s:
        s.scalars(select(Artwork)).one().licence = licence
        s.commit()
    assert authority.client.get(f"/api/v2/sync/media/{digest}", headers=auth).status_code == 404
    authority.st.ctx.settings = authority.st.ctx.settings.model_copy(update={"federation_media_unknown_licence": True})
    expected = 200 if licence is None else 404
    assert authority.client.get(f"/api/v2/sync/media/{digest}", headers=auth).status_code == expected


def test_a_follower_fetches_from_the_peer_first_and_checks_the_hash(pair):
    """T-M2: the row names its origin feed; discovery reads the digest from the peer's artwork list; the bytes come
    from the peer and must hash to that digest. The origin (which cannot resolve) is never asked."""
    authority, mirror, cid, digest, data = pair
    with mirror.st.maker() as s:
        art = s.scalars(select(Artwork)).one()
        assert art.origin_node == authority.node_id and art.blob_hash is None
    art = cache(mirror, authority)
    with mirror.st.maker() as s:
        peer = s.scalars(select(Peer)).one()
    assert art.blob_hash == hashlib.sha256(data).hexdigest() == digest
    assert (art.fetched_from, art.peer_digest, art.digest_provenance) == (f"peer:{peer.id}", digest, "peer_api")
    assert art.sources_tried is None and art.last_error is None
    assert media.find_blob(mirror.st.ctx.settings, digest) is not None


def test_digests_are_discovered_only_from_the_feed_signer(pair):
    """T-M2b (C13 §6.3): a relay did not sign the rows, so its artwork list is never asked for a digest; the row
    falls back to the origin."""
    authority, mirror, cid, digest, data = pair
    with mirror.st.maker() as s:
        s.scalars(select(Peer)).one().follow = "relayed"
        s.commit()
    art = cache(mirror, authority)
    assert art.blob_hash is None and art.peer_digest is None and art.digest_provenance is None
    assert [(e["source"].split(":")[0], e["error"]) for e in art.sources_tried] == [("peer", "relay_unsigned"),
                                                                                  ("origin", "dns_failed")]


def test_failure_reasons_are_kept_per_source(pair):
    """T-M3: the peer does not have it (local-only there) and the origin cannot be resolved: both reasons are kept,
    the row backs off as before."""
    authority, mirror, cid, digest, data = pair
    with authority.st.maker() as s:
        s.scalars(select(Artwork)).one().local_only = True
        s.commit()
    art = cache(mirror, authority)
    assert art.blob_hash is None and art.fail_count == 1 and art.last_error == "dns_failed"
    assert [(e["source"].split(":")[0], e["error"]) for e in art.sources_tried] == [("peer", "status_404"),
                                                                                  ("origin", "dns_failed")]


def test_a_tampering_peer_is_refused(pair, monkeypatch):
    """T-M4: bytes that do not hash to the digest are not stored."""
    authority, mirror, cid, digest, data = pair
    with mirror.st.maker() as s:
        s.scalars(select(Artwork)).one().peer_digest = "f" * 64  # what a lying artwork list would claim
        s.commit()
    art = cache(mirror, authority)
    assert art.blob_hash is None
    assert ("peer", "status_404") in [(e["source"].split(":")[0], e["error"]) for e in art.sources_tried]

    def lying(*args, **kwargs):
        return httpx.Response(200, content=png((0, 0, 0)), headers={"content-type": "image/png"})
    with mirror.st.maker() as s:
        row = s.scalars(select(Artwork)).one()
        row.peer_digest, row.next_try_at, row.sources_tried = digest, None, None
        s.commit()
    with mirror.st.maker() as s, httpx.Client(transport=httpx.MockTransport(lying)) as http:
        row = s.scalars(select(Artwork)).one()
        media.cache_one(s, mirror.st.ctx.settings, row, http)
        s.commit()
        assert row.blob_hash is None
        assert row.sources_tried[0]["error"] == "bad_image"


def test_a_tile_falls_back_to_another_cached_image(pair):
    """T-M5: no cover cached, a screenshot is: the tile shows the screenshot."""
    from atlas.web.routes import TILE_KINDS, primary_art

    authority, mirror, cid, digest, data = pair
    assert TILE_KINDS[0] == "key_art"

    class P:  # the bits of a page `primary_art` reads
        def __init__(self, node, session):
            self.session, self.st = session, node.st

    with authority.st.maker() as s:
        s.add(Artwork(canonical_id=cid, source="curator", kind="screenshot", url="https://img.invalid/s.png",
                      priority=10, blob_hash="a" * 64, redistributable=True))
        s.scalars(select(Artwork).where(Artwork.kind == "key_art")).one().blob_hash = None
        s.commit()
        assert primary_art(P(authority, s), [cid]) == {cid: "a" * 64}


def recording(authority: Node, answers: dict | None = None):
    """An outbound client that records every request and lets `answers[path]` (a callable) stand in for the peer;
    anything else on `authority.test` lands on the authority app, and any other host fails as unresolvable."""
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path))
        for path, answer in (answers or {}).items():
            if request.url.path == path:
                return answer(request)
        if request.url.host != "authority.test":
            raise httpx.ConnectError("unresolvable", request=request)
        r = authority.client.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get(
            "content-type", "application/json")})
    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def cache_with(mirror: Node, http: httpx.Client) -> Artwork:
    with mirror.st.maker() as s:
        art = s.scalars(select(Artwork)).one()
        art.next_try_at, art.sources_tried = None, None
        media.cache_one(s, mirror.st.ctx.settings, art, http)
        s.commit()
    with mirror.st.maker() as s:
        return s.scalars(select(Artwork)).one()


def test_discovery_never_requests_media_url_host(pair):
    """T-M9: the peer's artwork list names an internal host in `media_url`. Only the trailing digest is taken from
    it; the only requests go to the configured peer: its artwork list, then `/api/v2/sync/media/<digest>`."""
    authority, mirror, cid, digest, data = pair
    listing = {"results": [{"kind": "key_art", "url": ORIGIN, "media_url": f"http://10.0.0.1/media/{digest}"}]}
    http, seen = recording(authority, {f"/api/v2/games/{cid}/artwork": lambda r: httpx.Response(200, json=listing)})
    art = cache_with(mirror, http)
    assert art.blob_hash == digest and art.digest_provenance == "peer_api"
    assert {host for host, _ in seen} == {"authority.test"}
    assert [path for _, path in seen] == [f"/api/v2/games/{cid}/artwork", f"/api/v2/sync/media/{digest}"]


def test_peer_fetch_refuses_redirect(pair):
    """T-M10: the peer answers the media request with a redirect to a private address. It is not followed: the
    source is recorded as `too_many_redirects` and nothing is requested from the redirect target."""
    authority, mirror, cid, digest, data = pair
    bounce = {f"/api/v2/sync/media/{digest}": lambda r: httpx.Response(302, headers={
        "location": "http://10.0.0.1/admin"})}
    http, seen = recording(authority, bounce)
    art = cache_with(mirror, http)
    assert art.blob_hash is None
    assert ("peer", "too_many_redirects") in [(e["source"].split(":")[0], e["error"]) for e in art.sources_tried]
    assert all(host != "10.0.0.1" for host, _ in seen)
    assert seen.count(("authority.test", f"/api/v2/sync/media/{digest}")) == 1


def test_peer_reported_digest_does_not_block_origin(pair, monkeypatch):
    """T-M11: a digest the peer reported (unsigned) is a hint. The peer no longer has those bytes and the origin
    serves an updated image with another digest: the origin's bytes are stored, recorded as the origin's digest."""
    authority, mirror, cid, digest, data = pair
    newer = png((10, 200, 10))
    monkeypatch.setattr(media, "check_url", lambda url, settings: ("https", "img.invalid", 443, "93.184.216.34"))
    with mirror.st.maker() as s:
        row = s.scalars(select(Artwork)).one()
        row.peer_digest, row.digest_provenance = "e" * 64, "peer_api"
        s.commit()
    http, _ = recording(authority, {"/key.png": lambda r: httpx.Response(200, content=newer,
                                                                         headers={"content-type": "image/png"})})
    art = cache_with(mirror, http)
    assert art.blob_hash == hashlib.sha256(newer).hexdigest() != "e" * 64
    assert (art.fetched_from, art.peer_digest, art.digest_provenance) == ("origin", art.blob_hash, "origin")


def test_no_discovery_through_relay(pair):
    """T-M12: a peer that relays the feed (`follow = relayed`) did not sign the rows, so it is never asked for a
    digest (no artwork-list request at all); the row goes to its origin."""
    authority, mirror, cid, digest, data = pair
    with mirror.st.maker() as s:
        s.scalars(select(Peer)).one().follow = "relayed"
        s.commit()
    http, seen = recording(authority)
    art = cache_with(mirror, http)
    assert art.peer_digest is None and not any("/games/" in path for _, path in seen)
    assert art.sources_tried[0]["error"] == "relay_unsigned"


def test_own_provider_rows_never_fetched_from_peer(pair):
    """T-M13: a row this node imported from its own provider (no feed delivered it, as with C13 §5.1 step 4
    enrichment) is fetched from its origin only: no peer is asked, even one that holds the same image."""
    authority, mirror, cid, digest, data = pair
    with mirror.st.maker() as s:
        row = s.scalars(select(Artwork)).one()
        row.origin_node, row.source, row.redistributable = None, "steam", False
        s.commit()
    http, seen = recording(authority)
    art = cache_with(mirror, http)
    assert seen == [] and art.blob_hash is None  # the origin cannot resolve; the peer was never contacted
    assert [e["source"] for e in art.sources_tried] == ["origin"]


def test_sync_media_follows_the_c14_ceiling_and_group_rule(pair):
    """C14 §5.4/§5.7 (third review): the bytes of a record under a ceiling, or of a row received from the group,
    are the same 404 as an unknown digest on `/sync/media`."""
    from atlas.models import Canonical

    authority, mirror, cid, digest, data = pair
    auth = {"Authorization": f"Bearer {authority.token}"}
    assert authority.client.get(f"/api/v2/sync/media/{digest}", headers=auth).status_code == 200
    with authority.st.maker() as s:
        s.scalars(select(Artwork)).one().fetched_from = "group:n1"
        s.commit()
    assert authority.client.get(f"/api/v2/sync/media/{digest}", headers=auth).status_code == 404
    with authority.st.maker() as s:
        s.scalars(select(Artwork)).one().fetched_from = None
        s.get(Canonical, cid).scope_ceiling = "group"
        s.commit()
    assert authority.client.get(f"/api/v2/sync/media/{digest}", headers=auth).status_code == 404
