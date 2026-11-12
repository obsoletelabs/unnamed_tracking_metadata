"""CONTRACT-C15 §2.7 (A20, the achievements part of T-B7): `MediaType.achievements` is a per-type flag, driven by
data and never by a type's name. Off: the curator API and the emitter refuse, a follower quarantines the entry as
`invalid` (it does not wait), the API answers `applicable: false`; stored rows are kept and come back. Every existing
type keeps the flag on (migration 0060), so games, anime and the rest behave as before."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from atlas import auth, custom, facets, feed_schema, service
from atlas.models import Achievement, ChangeLog, MediaType

from .conftest import batch
from .test_sync_compat import Node, pull

ITEMS = [{"name": "First page", "description": "Open it."}, {"name": "Last page", "description": "Finish it."}]


def make_record(node: Node, title: str, media_type: str | None = None) -> str:
    with node.st.maker() as s:
        [res] = service.ingest(s, node.st.ctx, batch("manual", [{"external_id": title, "title": title}],
                                                      kind="manual"), curator=True, media_type=media_type)
        s.commit()
    return res.canonical_id


def curator(node: Node) -> dict[str, str]:
    with node.st.maker() as s:
        _, secret = auth.create_token(s, "curator", ["read", "curate"])
        s.commit()
    return {"Authorization": f"Bearer {secret}"}


def listing(node: Node, cid: str, headers) -> dict:
    r = node.client.get(f"/api/v2/games/{cid}/achievements", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def stored(node: Node, cid: str) -> int:
    with node.st.maker() as s:
        return s.scalar(select(func.count()).select_from(Achievement).where(Achievement.canonical_id == cid))


def set_type(node: Node, key: str, label: str, achievements: bool) -> None:
    with node.st.maker() as s:
        custom.save_type(s, node.st.ctx, key, label, achievements=achievements)
        s.commit()


def test_seeded_types_default_on(tmp_path):
    node = Node(tmp_path, "n", "authority")
    with node.st.maker() as s:
        types = {t.key: t.achievements for t in s.scalars(select(MediaType))}
    # game, anime, movie, tv_show, series, collection; only A21's built-in `book` is seeded off (C15 §2.7)
    assert types.pop("book") is False
    assert types and all(types.values()), types


def test_curator_api_refuses_and_listing_says_not_applicable(tmp_path):
    node = Node(tmp_path, "n", "authority")
    set_type(node, "comic", "Comic", achievements=False)
    cid = make_record(node, "Watchmen", "comic")
    headers = curator(node)
    r = node.client.put(f"/api/v2/games/{cid}/achievements", json={"items": ITEMS}, headers=headers)
    assert r.status_code == 400 and "achievements do not apply to comic records" in r.text
    got = listing(node, cid, headers)
    assert (got["applicable"], got["results"], got["total"], got["sets"]) == (False, [], 0, [])
    with node.st.maker() as s:  # the emitter never signs one
        assert s.scalar(select(func.count()).select_from(ChangeLog).where(ChangeLog.kind == "achievements")) == 0
        with pytest.raises(facets.AchievementsNotApplicable):
            facets.set_achievements(s, node.st.ctx, cid, "curator", ITEMS, redistributable=True)


def test_anime_and_games_still_apply(tmp_path):
    node = Node(tmp_path, "n", "authority")
    headers = curator(node)
    for media_type in (None, "anime", "movie"):
        cid = make_record(node, f"Title {media_type}", media_type)
        r = node.client.put(f"/api/v2/games/{cid}/achievements", json={"items": ITEMS}, headers=headers)
        assert r.status_code == 200, (media_type, r.text)
        got = listing(node, cid, headers)
        assert got["applicable"] is True and got["total"] == 2


def test_turning_the_flag_off_hides_and_never_deletes(tmp_path):
    """Retyping (here: the type's flag changes under a record) hides the stored rows; turning it back shows them."""
    node = Node(tmp_path, "n", "authority")
    set_type(node, "comic", "Comic", achievements=True)
    cid = make_record(node, "Saga", "comic")
    headers = curator(node)
    assert node.client.put(f"/api/v2/games/{cid}/achievements", json={"items": ITEMS},
                           headers=headers).status_code == 200
    set_type(node, "comic", "Comic", achievements=False)
    assert listing(node, cid, headers)["applicable"] is False and stored(node, cid) == 2
    with node.st.maker() as s:  # a record retyped to a type without achievements behaves the same
        row = service.get_canonical(s, make_record(node, "Other", None))[0]
        row.media_type = "comic"
        s.commit()
        other = row.id
    assert listing(node, other, headers)["applicable"] is False
    set_type(node, "comic", "Comic", achievements=True)
    got = listing(node, cid, headers)
    assert got["applicable"] is True and got["total"] == 2 and stored(node, cid) == 2


def test_the_flag_travels_in_the_schema_and_a_follower_quarantines_invalid_not_wait(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    mirror = Node(tmp_path, "mirror", "mirror")
    set_type(authority, "comic", "Comic", achievements=False)  # an authority publishes its schema
    cid = make_record(authority, "Watchmen", "comic")
    with authority.st.maker() as s:
        [schema] = [r.body for r in s.scalars(select(ChangeLog).where(ChangeLog.kind == "schema"))][-1:]
        assert {t["key"]: t["achievements"] for t in schema["types"]}["comic"] is False
    # a hostile or buggy upstream signs achievements anyway
    seq = authority.emit("achievements", {"canonical_id": cid, "source": "curator", "items": ITEMS})
    pull(mirror, authority)
    with mirror.st.maker() as s:
        assert s.get(MediaType, "comic").achievements is False and s.get(MediaType, "comic").origin == "upstream"
    assert (seq, "achievements", "quarantined", "invalid") in mirror.failures(authority)
    assert stored(mirror, cid) == 0
    assert mirror.heads(authority)["waiting"]["count"] == 0


def test_an_older_authority_without_the_member_means_on(tmp_path):
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    authority.emit("schema", {"types": [{"key": "comic", "label": "Comic", "enabled": True, "position": 7,
                                         "labels": {}}], "fields": []})
    pull(mirror, authority)
    with mirror.st.maker() as s:
        assert s.get(MediaType, "comic").achievements is True


def test_admin_type_page_toggles_the_flag_and_the_new_type_form_keeps_it_on(tmp_path):
    from fastapi.testclient import TestClient

    from .test_web import signup, token

    node = Node(tmp_path, "n", "authority")
    web = TestClient(node.app, base_url="http://localhost:8000", follow_redirects=False)
    signup(web, "admin")
    csrf = token(web, "/admin/schema")
    assert web.post("/admin/schema/types", data={"csrf": csrf, "key": "comic", "label": "Comic"}).status_code == 303
    with node.st.maker() as s:
        assert s.get(MediaType, "comic").achievements is True
    page = web.get("/admin/schema/comic").text
    assert 'name="achievements"' in page and "Achievements apply to this type" in page
    csrf = token(web, "/admin/schema/comic")
    assert web.post("/admin/schema/types", data={"csrf": csrf, "key": "comic", "label": "Comic", "enabled": "on",
                                                 "achievements_shown": "1"}).status_code == 303
    with node.st.maker() as s:
        assert s.get(MediaType, "comic").achievements is False
    assert feed_schema.classify("schema", {"types": [{"key": "comic", "label": "Comic", "achievements": False}],
                                           "fields": []}).action == "apply"
