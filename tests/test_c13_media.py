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
