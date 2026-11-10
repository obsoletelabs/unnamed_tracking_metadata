"""CONTRACT-C15 §2.2 (A20, T-B10): a follower given an entry that names a media type it does not have waits for the
type (`waiting_for = "type:<key>"`) instead of filing the record as a game. The same feed's `schema` wakes it, another
feed's does not; with `inherit_schema` off it is quarantined `media_type_not_adopted` (no endless wait); an entry
that names no type still files as a game. Plus the safety net, the operator actions and `atlas doctor media-types`.

Real nodes on SQLite files through the real routes (`test_sync_compat.Node`)."""

from __future__ import annotations

import json

from sqlalchemy import select

from atlas import cli, custom, doctor, sync
from atlas.ids import canonical_id
from atlas.models import Artwork, AuditEntry, Canonical, MediaType, NodeState, Observation, SyncFailure

from .test_c13_follower import ART, ART_ROWS, art, configure, cursor, waiting
from .test_sync_compat import Node, _admin, observation, pull

COMIC = {"key": "comic", "label": "Comic", "enabled": True, "position": 7, "labels": {"developer": "Writer"},
         "achievements": False}


def comic_obs(title="Watchmen", media_type="comic"):
    body = observation(title)
    if media_type is None:
        body.pop("media_type")
    else:
        body["media_type"] = media_type
    return body


def schema(*types):
    return {"types": list(types), "fields": []}


def record_of(node: Node, cid: str) -> Canonical | None:
    with node.st.maker() as s:
        row = s.get(Canonical, cid)
        if row is not None:
            s.expunge(row)
        return row


def test_t_b10_waits_for_the_type_and_the_same_feeds_schema_wakes_it_in_order(tmp_path):
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    body = comic_obs()
    cid = body["canonical_id"]
    authority.emit("obs", body)
    authority.emit("artwork", {"canonical_id": cid, "source": "curator", "items": ART})
    pull(mirror, authority)
    assert waiting(mirror, authority) == [(1, "obs", "wait", "waiting_for_media_type", "type:comic", 1),
                                          (2, "artwork", "wait", "waiting_for_record", cid, 1)]
    assert record_of(mirror, cid) is None  # never filed as a game
    assert cursor(mirror, authority) == 2  # waiting does not block
    pull(mirror, authority)
    assert waiting(mirror, authority)[0][5] == 1  # waiting is not failing: no attempt counted

    with mirror.st.maker() as s:
        [group] = [g for g in sync.waiting_groups(s) if g["cause"] == sync.WAITING_FOR_MEDIA_TYPE]
        assert (group["media_type"], group["title"], group["seqs"], group["canonical_id"]) == (
            "comic", "Watchmen", [1, 1], None)
    listed = mirror.client.get("/api/v2/sync/failures", headers={"Authorization": f"Bearer {_admin(mirror)}"}).json()
    assert {f["waiting_for"] for f in listed["failures"]} == {"type:comic", cid}

    authority.emit("schema", schema(COMIC))
    pull(mirror, authority)
    assert waiting(mirror, authority) == []
    row = record_of(mirror, cid)
    assert row.media_type == "comic" and row.view.get("title") == "Watchmen"
    assert art(mirror, cid) == ART_ROWS  # the artwork waiting for the record applied right after it
    with mirror.st.maker() as s:
        assert s.get(MediaType, "comic").achievements is False  # schema.types[].achievements applied
        assert s.get(NodeState, f"sync_woken:schema:{authority.node_id}").value == "1"
    assert mirror.heads(authority)["state"] == "in_sync"


def test_t_b10_a_record_entry_waits_too(tmp_path):
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    cid = canonical_id()
    authority.emit("record", {"canonical_id": cid, "media_type": "comic", "title": "Saga", "provider_ids": {},
                              "because": "publish"})
    pull(mirror, authority)
    assert waiting(mirror, authority) == [(1, "record", "wait", "waiting_for_media_type", "type:comic", 1)]
    assert record_of(mirror, cid) is None
    authority.emit("schema", schema(COMIC))
    pull(mirror, authority)
    assert record_of(mirror, cid).media_type == "comic" and waiting(mirror, authority) == []


def test_t_b10_another_feeds_schema_does_not_wake_it(tmp_path):
    a, b = Node(tmp_path, "a", "authority"), Node(tmp_path, "b", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    body = comic_obs()
    a.emit("obs", body)
    pull(mirror, a)
    b.emit("schema", schema(COMIC))
    pull(mirror, b)  # b's schema creates `comic` here, owned by b
    with mirror.st.maker() as s:
        assert s.get(MediaType, "comic").origin_node == b.node_id
        sync.retry_failures(s, mirror.st.ctx, due_only=False)  # the safety net respects the feed scope too
        s.commit()
    assert waiting(mirror, a) == [(1, "obs", "wait", "waiting_for_media_type", "type:comic", 1)]
    assert record_of(mirror, body["canonical_id"]) is None
    a.emit("schema", schema(COMIC))  # a's own schema names the type: it applies (the type b owns is used as is)
    pull(mirror, a)
    assert waiting(mirror, a) == [] and record_of(mirror, body["canonical_id"]).media_type == "comic"


def test_t_b10_without_inherit_schema_it_is_quarantined_not_adopted(tmp_path):
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror", inherit_schema=False)
    body = comic_obs()
    authority.emit("obs", body)
    pull(mirror, authority)
    with mirror.st.maker() as s:
        row = s.get(SyncFailure, (authority.node_id, 1))
        assert (row.disposition, row.reason, row.waiting_for) == ("quarantined", "media_type_not_adopted", None)
        assert row.detail == {"kind": "obs", "media_type": "comic"}
        assert "uses media type 'comic', which this node does not adopt" in row.error
    assert record_of(mirror, body["canonical_id"]) is None
    assert cursor(mirror, authority) == 1  # it does not block
    authority.emit("schema", schema(COMIC))
    pull(mirror, authority)  # the schema is a no-op here; the row stays quarantined
    with mirror.st.maker() as s:
        assert s.get(MediaType, "comic") is None
        # "Create type locally": an ordinary local type; the next retry applies the entry
        custom.save_type(s, mirror.st.ctx, "comic", "Comic")
        assert sync.retry_failures(s, mirror.st.ctx, due_only=False)["resolved"] == 1
        s.commit()
    assert record_of(mirror, body["canonical_id"]).media_type == "comic"


def test_t_b10_a_later_schema_that_drops_the_type_quarantines_instead_of_an_endless_wait(tmp_path):
    """C15 §2.2 "no endless wait" with `inherit_schema` on: the feed's own later schema is its whole set of types, so
    an older entry whose type it leaves out (deleted upstream) is quarantined; a newer entry keeps waiting."""
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    old = comic_obs("Watchmen")
    authority.emit("obs", old)  # seq 1: names `comic`
    pull(mirror, authority)
    assert waiting(mirror, authority)[0][4] == "type:comic"
    authority.emit("schema", schema())  # seq 2: the authority no longer has `comic`
    newer = comic_obs("Saga")
    authority.emit("obs", newer)  # seq 3: after the schema, so the next schema may still bring it
    pull(mirror, authority)
    with mirror.st.maker() as s:
        row = s.get(SyncFailure, (authority.node_id, 1))
        assert (row.disposition, row.reason, row.waiting_for) == ("quarantined", "media_type_not_adopted", None)
        assert "later schema (entry 2) does not carry it" in row.error
    assert waiting(mirror, authority) == [(1, "obs", "quarantined", "media_type_not_adopted", None, 2),
                                          (3, "obs", "wait", "waiting_for_media_type", "type:comic", 1)]
    assert record_of(mirror, old["canonical_id"]) is None
    authority.emit("schema", schema(COMIC))
    pull(mirror, authority)  # the newer entry applies; the quarantined one waits for an administrator
    assert waiting(mirror, authority) == [(1, "obs", "quarantined", "media_type_not_adopted", None, 2)]
    assert record_of(mirror, newer["canonical_id"]).media_type == "comic"


def test_t_b10_no_media_type_still_files_as_game_and_a_named_one_never_does(tmp_path):
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    legacy, empty, named = comic_obs("Old", None), comic_obs("Blank", ""), comic_obs("Named", "comic")
    for body in (legacy, empty, named):
        authority.emit("obs", body)
    pull(mirror, authority)
    assert record_of(mirror, legacy["canonical_id"]).media_type == "game"  # a 1.0 emitter: unchanged
    assert record_of(mirror, empty["canonical_id"]).media_type == "game"
    assert record_of(mirror, named["canonical_id"]) is None
    with mirror.st.maker() as s:
        assert s.scalars(select(Canonical.media_type).where(Canonical.id == named["canonical_id"])).first() is None


def test_an_invalid_media_type_key_is_quarantined_invalid(tmp_path):
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    authority.emit("obs", comic_obs("Bad", "Comic Books!"))
    pull(mirror, authority)
    assert mirror.failures(authority) == [(1, "obs", "quarantined", "invalid")]


def test_safety_net_wakes_for_a_local_type_and_quarantines_when_inherit_is_turned_off(tmp_path):
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    first, second = comic_obs("One"), comic_obs("Two", "manga")
    authority.emit("obs", first)
    authority.emit("obs", second)
    pull(mirror, authority)
    with mirror.st.maker() as s:
        custom.save_type(s, mirror.st.ctx, "comic", "Comic")  # a type created here (or a built-in an upgrade seeds)
        s.commit()
    configure(mirror, inherit_schema=False)
    with mirror.st.maker() as s:
        result = sync.retry_failures(s, mirror.st.ctx)
        s.commit()
    assert result.get("woken") == 1
    assert record_of(mirror, first["canonical_id"]).media_type == "comic"
    assert mirror.failures(authority) == [(2, "obs", "quarantined", "media_type_not_adopted")]


def test_dismiss_keeps_the_administrators_decision_across_retries(tmp_path):
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror", inherit_schema=False)
    authority.emit("obs", comic_obs())
    pull(mirror, authority)
    with mirror.st.maker() as s:
        sync.dismiss_not_adopted(s, authority.node_id, 1, via="test")
        sync.retry_failures(s, mirror.st.ctx, due_only=False)
        s.commit()
        row = s.get(SyncFailure, (authority.node_id, 1))
        assert (row.disposition, row.reason) == ("quarantined", "skipped")
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "sync_skip")).first() is not None


def test_admin_peers_shows_the_wait_and_the_not_adopted_actions(tmp_path):
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    authority.emit("obs", comic_obs())
    pull(mirror, authority)
    from fastapi.testclient import TestClient

    from .test_web import signup, token

    web = TestClient(mirror.app, base_url="http://localhost:8000", follow_redirects=False)
    signup(web, "admin")  # the first account administers the node
    page = web.get("/admin/peers").text
    assert 'data-waiting-for="type:comic"' in page and "Waiting for the media type" in page
    # review fix: one entry reads as one entry, and type waits are not counted as unpublished records
    assert "1 entry from" in page and "(for <strong>Watchmen</strong>) describes records" in page
    assert "Entries waiting for their media type" in page and "the upstream feed has not published yet" not in page
    assert "Admin → Fields" in page
    # fix pass: the per-peer badge and the line under it name the media type, not a game
    with mirror.st.maker() as s:
        heads = {"waiting": sync.open_waiting(s, authority.node_id), "quarantined": {"count": 0},
                 "observed_head": 1, "applied_head": 1}
    assert sync.status_label("caught_up_waiting", heads) == "Caught up · 1 change waiting for their media type"
    line = sync.status_detail("caught_up_waiting", heads)
    assert "1 media type hasn't arrived from the upstream yet" in line and "game" not in line

    configure(mirror, inherit_schema=False)
    with mirror.st.maker() as s:
        sync.retry_failures(s, mirror.st.ctx)
        s.commit()
    page = web.get("/admin/peers").text
    assert 'data-not-adopted="comic"' in page and "Create type locally" in page and "Dismiss" in page
    csrf = token(web, "/admin/peers")
    answer = web.post("/admin/peers/failures/create-type", data={"key": "comic", "csrf": csrf})
    assert answer.status_code == 200 and "Created the media type comic" in answer.text
    assert mirror.failures(authority) == []
    with mirror.st.maker() as s:
        assert s.get(MediaType, "comic").origin == "local"
        assert s.scalars(select(Observation).where(Observation.origin_node == authority.node_id)).first() is not None


def test_doctor_lists_and_retypes_records_the_old_fallback_misfiled(tmp_path, monkeypatch, capsys):
    """The upgrade story: a record an older release filed as a game stays a game until an administrator retypes it
    per peer; the startup scan only counts and logs."""
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    body = comic_obs()
    cid = body["canonical_id"]
    authority.emit("obs", body)
    authority.emit("artwork", {"canonical_id": cid, "source": "curator", "items": ART})

    def old_fallback(session, cid, media_type=None):  # the pre-A20 behaviour
        if session.get(Canonical, cid) is None:
            known = media_type and session.get(MediaType, media_type) is not None
            session.add(Canonical(id=cid, view={}, explain={}, media_type=media_type if known else "game"))
            session.flush()

    with monkeypatch.context() as m:
        m.setattr(sync, "_ensure_canonical", old_fallback)
        pull(mirror, authority)
    assert record_of(mirror, cid).media_type == "game"
    with mirror.st.maker() as s:
        assert doctor.misfiled(s) == []  # `comic` is not known here yet: nothing to retype it to
        custom.save_type(s, mirror.st.ctx, "comic", "Comic")
        s.commit()
    with mirror.st.maker() as s:
        assert doctor.startup_scan(s) == 1 and doctor.startup_scan(s) is None  # once
        [item] = doctor.misfiled(s)
        assert (item["canonical_id"], item["node_id"], item["seq"], item["named"]) == (cid, authority.node_id, 1,
                                                                                      "comic")
        s.commit()
    assert record_of(mirror, cid).media_type == "game"  # never retyped automatically

    monkeypatch.setenv("ATLAS_DATABASE_URL", f"sqlite:///{tmp_path}/mirror.db")
    monkeypatch.setenv("ATLAS_ROLE", "mirror")
    monkeypatch.setenv("ATLAS_MEDIA_DIR", str(tmp_path / "mirror"))
    from atlas.settings import get_settings

    get_settings.cache_clear()
    try:
        assert cli.main(["doctor", "media-types"]) == 3
        assert "1 record(s) filed as game (comic ×1)" in capsys.readouterr().out
        assert cli.main(["doctor", "media-types", "--retype", authority.node_id]) == 0
        out = capsys.readouterr().out
        assert json.loads(out.splitlines()[0]) == {"retyped": 1, "node_id": authority.node_id}
        assert "no misfiled records" in out
    finally:
        get_settings.cache_clear()
    row = record_of(mirror, cid)
    assert row.media_type == "comic" and row.view.get("title") == "Watchmen"
    with mirror.st.maker() as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "doctor_retype")).first().detail["count"] == 1
        assert s.scalars(select(Artwork).where(Artwork.canonical_id == cid)).first() is not None  # nothing lost


def test_safety_net_reaches_rows_behind_ones_that_keep_waiting(tmp_path):
    # Review fix: `_wake_ready_types` read only the first `limit` rows; rows still waiting hid the ones behind them.
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    for i in range(3):
        authority.emit("obs", comic_obs(f"Manga {i}", "manga"))  # stays missing (inherit on): keeps waiting
    last = comic_obs("Comic", "comic")
    authority.emit("obs", last)
    pull(mirror, authority)
    with mirror.st.maker() as s:
        custom.save_type(s, mirror.st.ctx, "comic", "Comic")
        s.commit()
    with mirror.st.maker() as s:
        assert sync._wake_ready_types(s, mirror.st.ctx, None, 2) == 1  # limit 2 < the 3 manga rows ahead
        s.commit()
    assert record_of(mirror, last["canonical_id"]).media_type == "comic"
    assert [r[4] for r in waiting(mirror, authority)] == ["type:manga"] * 3


def test_a_dismissed_entry_is_left_alone_by_automatic_retries(tmp_path):
    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror", inherit_schema=False)
    authority.emit("obs", comic_obs())
    pull(mirror, authority)
    with mirror.st.maker() as s:
        sync.dismiss_not_adopted(s, authority.node_id, 1, via="test")
        s.commit()
    with mirror.st.maker() as s:
        before = s.get(SyncFailure, (authority.node_id, 1)).attempts
        row = s.get(SyncFailure, (authority.node_id, 1))
        row.last_attempt_at = row.last_attempt_at.replace(year=2000)  # its backoff has long passed
        s.commit()
    with mirror.st.maker() as s:
        assert sync.retry_failures(s, mirror.st.ctx)["retried"] == 0
        assert s.get(SyncFailure, (authority.node_id, 1)).attempts == before
        assert "or dismiss it" in s.get(SyncFailure, (authority.node_id, 1)).error


def test_doctor_scan_runs_again_when_a_type_arrives_later(tmp_path, monkeypatch):
    # Review fix: the scan ran once per node, before A21 seeds `book` or a schema brings a type; it now keys on the
    # set of known types.
    authority, mirror = Node(tmp_path, "authority", "authority"), Node(tmp_path, "mirror", "mirror")
    body = comic_obs()
    authority.emit("obs", body)

    def old_fallback(session, cid, media_type=None):
        if session.get(Canonical, cid) is None:
            session.add(Canonical(id=cid, view={}, explain={}, media_type="game"))
            session.flush()

    with monkeypatch.context() as m:
        m.setattr(sync, "_ensure_canonical", old_fallback)
        pull(mirror, authority)
    with mirror.st.maker() as s:
        assert doctor.startup_scan(s) == 0  # the A20 upgrade: `comic` unknown, nothing reportable
        assert doctor.startup_scan(s) is None  # unchanged types: no rescan
        s.commit()
    with mirror.st.maker() as s:
        custom.save_type(s, mirror.st.ctx, "comic", "Comic")  # arrives later (seeded, schema, or created)
        s.commit()
    with mirror.st.maker() as s:
        assert doctor.startup_scan(s) == 1
        assert doctor.startup_scan(s) is None
        s.add(NodeState(key="legacy_probe", value="x"))
        s.get(NodeState, doctor.SCANNED).value = "1"  # a marker the first A20 build wrote (count only)
        s.commit()
    with mirror.st.maker() as s:
        assert doctor.startup_scan(s) == 1  # re-keyed once
