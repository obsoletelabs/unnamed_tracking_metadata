"""CONTRACT-C15 §4.1 (A20, T-B16 for any non-game type): the H54 compatibility bridge introduces records with an
ordinary 1.0 observation, which a 1.0 follower files as a game whatever `media_type` says (its old fallback). So the
bridge carries only `federation_bridge_media_types` (default `["game"]`): a record of another type gets a `record`
(which a 1.0-1.4 follower parks) when the emit protocol allows one, and is otherwise left out. Admin → Peers counts
the skipped records. (A21's `book` is such a type; A20 has no `book`, so a custom `comic` type stands in.)"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import custom, feed, service
from atlas.models import Canonical, ChangeLog, MediaType

from .conftest import batch
from .test_c13_follower import ART_ROWS, art, curate_art
from .test_c13_skew import as_release, kinds, open_rows
from .test_sync_compat import Node, pull


def provider_only(node: Node, title: str, steam: str, media_type: str | None = None) -> str:
    with node.st.maker() as s:
        if media_type and s.get(MediaType, media_type) is None:
            custom.save_type(s, node.st.ctx, media_type, media_type.capitalize())
        [result] = service.ingest(s, node.st.ctx, batch("steam", [{"external_id": steam, "title": title,
                                                                    "provider_ids": {"steam": steam}}]),
                                  system=True, media_type=media_type)
        s.commit()
    return result.canonical_id


def bridged_obs(node: Node) -> list[str]:
    with node.st.maker() as s:
        return [r.body["canonical_id"] for r in s.scalars(select(ChangeLog).where(ChangeLog.kind == "obs"))]


def test_t_b16_a_non_game_record_is_not_bridged_by_default(tmp_path, monkeypatch):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5", min_follower_protocol="1.0",
                     federation_bridge_record_obs=True)
    settings = authority.st.ctx.settings
    assert feed.introduction_mode(settings) == "bridge"
    assert feed.introduction_mode(settings, "game") == "bridge" and feed.introduction_mode(settings, "comic") == "record"
    game = provider_only(authority, "Hades", "1145360")
    comic = provider_only(authority, "Watchmen", "9000001", "comic")
    curate_art(authority, game)
    curate_art(authority, comic)
    assert bridged_obs(authority) == [game]  # the game is bridged; the comic is not
    assert kinds(authority)[-2:] == ["record", "artwork"]
    with authority.st.maker() as s:
        assert feed.bridge_skipped(s, authority.st.ctx.settings) == 1

    follower = Node(tmp_path, "follower", "mirror")
    with as_release(monkeypatch, "1.4"):  # a follower that knows no `record`: it parks it, never files a game
        pull(follower, authority)
        parked = [r for r in open_rows(follower, authority) if r[2] == "parked"]
        assert parked and parked[0][1:] == ("record", "parked", "unsupported_kind")
    assert art(follower, game) == ART_ROWS
    with follower.st.maker() as s:
        assert s.get(Canonical, comic) is None


def test_t_b16_the_operator_can_bridge_another_type(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5", federation_bridge_record_obs=True,
                     federation_bridge_media_types=["game", "comic"])
    comic = provider_only(authority, "Watchmen", "9000001", "comic")
    curate_art(authority, comic)
    assert bridged_obs(authority) == [comic] and "record" not in kinds(authority)
    with authority.st.maker() as s:
        assert feed.bridge_skipped(s, authority.st.ctx.settings) == 0


def test_t_b16_below_1_5_a_non_game_record_stays_legacy_and_introduce_still_finishes(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.4", federation_bridge_record_obs=True)
    game = provider_only(authority, "Hades", "1145360")
    comic = provider_only(authority, "Watchmen", "9000001", "comic")
    curate_art(authority, comic)  # signed as before (legacy): nothing can introduce a comic to a 1.0 follower
    curate_art(authority, game)
    assert bridged_obs(authority) == [game]
    with authority.st.maker() as s:
        result = feed.introduce_batch(s, authority.st.ctx, "all")
        s.commit()
    assert result["finished"] and result["left"] == 0
    with authority.st.maker() as s:
        assert feed.unintroduced_count(s, "all") == 1  # the comic, counted where no settings narrow it
        assert feed.bridge_skipped(s, authority.st.ctx.settings) == 1


def test_admin_peers_counts_the_skipped_records(tmp_path):
    from .test_web import signup

    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5", federation_bridge_record_obs=True)
    provider_only(authority, "Watchmen", "9000001", "comic")
    web = TestClient(authority.app, base_url="http://localhost:8000", follow_redirects=False)
    signup(web, "admin")
    page = web.get("/admin/peers").text
    assert 'data-bridge-skipped="1"' in page and "carries only game records" in page
