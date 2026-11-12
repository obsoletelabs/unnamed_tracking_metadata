"""Answers that say what happened (Phase 4 notes O1 and O3).

O1: a relation an authority stored while gated (emit below 1.4) used to stay unsigned after the protocol was raised,
and putting it again answered `published: true` while signing nothing. Now that answer is honest while gated
(`published: false`, `reason`, `pending`), and putting it again once the authority may sign publishes it, once.

O3: a curator `PUT /artwork` listing an image another source of the record already lists used to skip it silently
while `count` included it. Now the response lists each skipped image and who holds it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import auth, facets, main
from atlas.models import Artwork, Canonical, ChangeLog

from .conftest import build_settings
from .test_relations import record
from .test_web import _wait_for_startup_replay


def app_at(tmp_path, emit):
    """The same database served at an emit protocol (the operator raises it and restarts)."""
    return main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/o1.db", media_dir=str(tmp_path / "m"),
                                          media_cache_enabled=False, emit_protocol=emit))


def version(st, cid):
    with st.maker() as s:
        return s.get(Canonical, cid).version


def signed_relations(st):
    with st.maker() as s:
        return [e.body for e in s.scalars(select(ChangeLog).where(ChangeLog.kind == "relation")
                                          .order_by(ChangeLog.seq))]


@pytest.fixture
def ids_and_token(tmp_path):
    app = app_at(tmp_path, "1.0")
    st = app.state.atlas
    with TestClient(app):
        _wait_for_startup_replay(st)
        with st.maker() as s:
            _, curator = auth.create_token(s, "cur", ["curate"])
            ids = {t: record(s, st.ctx, t) for t in ("O1 Game", "O1 DLC", "O1 Port")}
            s.commit()
    return ids, {"Authorization": f"Bearer {curator}"}


def test_a_mirror_answers_that_its_relations_are_local(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/mirror.db", role="mirror",
                                         media_dir=str(tmp_path / "m"), media_cache_enabled=False))
    st = app.state.atlas
    with TestClient(app) as client:
        _wait_for_startup_replay(st)
        with st.maker() as s:
            _, curator = auth.create_token(s, "cur", ["curate"])
            a, b = record(s, st.ctx, "Mirror A"), record(s, st.ctx, "Mirror B")
            s.commit()
        r = client.put(f"/api/v2/games/{a}/relations", headers={"Authorization": f"Bearer {curator}"},
                       json={"relations": [{"to": b, "type": "port_of"}]})
        assert r.status_code == 200, r.text
        assert (r.json()["published"], r.json()["reason"]) == (False, "local_node")


def test_curator_artwork_put_reports_the_images_another_source_holds(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/art.db", media_dir=str(tmp_path / "m"),
                                         media_cache_enabled=False))
    st = app.state.atlas
    held = "https://img.example.org/cover-a.png"
    fresh = "https://img.example.org/cover-b.png"
    with TestClient(app) as client:
        _wait_for_startup_replay(st)
        with st.maker() as s:
            _, curator = auth.create_token(s, "cur", ["curate"])
            cid = record(s, st.ctx, "Art Game")
            facets.set_artwork(s, st.ctx, cid, "steam", [{"kind": "key_art", "url": held}])
            s.commit()
        cur = {"Authorization": f"Bearer {curator}"}
        r = client.put(f"/api/v2/games/{cid}/artwork", headers=cur,
                       json={"items": [{"kind": "key_art", "url": held}, {"kind": "key_art", "url": fresh}]})
        assert r.status_code == 200, r.text
        assert r.json() == {"count": 2, "stored": 1, "skipped": [
            {"kind": "key_art", "url": held, "held_by": "steam", "reason": "held_by_another_source"}]}
        with st.maker() as s:
            rows = sorted((a.source, a.url) for a in s.scalars(select(Artwork).where(Artwork.canonical_id == cid)))
            signed = s.scalars(select(ChangeLog).where(ChangeLog.kind == "artwork").order_by(ChangeLog.seq.desc())
                               ).first()
        assert rows == [("curator", fresh), ("steam", held)]  # persisted: the image stays the other source's
        # the signed entry says what was stored: a follower does not file the other source's image as the curator's
        assert signed is not None and [i["url"] for i in signed.body["items"]] == [fresh]
        r = client.put(f"/api/v2/games/{cid}/artwork", headers=cur, json={"items": [{"kind": "key_art", "url": fresh}]})
        assert r.json() == {"count": 1, "stored": 1, "skipped": []}
