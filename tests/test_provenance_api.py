"""C11 HTTP surface (A17): provenance, decisions, conflicts, merge preview, merge records and unmerge, "different
works", the review queue fields, explicit clears and `report_unchanged`. Additive only: old fields stay. Every
scenario also reads the database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import main
from atlas.models import Canonical, Lock, MatchDecision, MatchRejection, MergeRecord, ReviewItem

from .conftest import build_settings

ADMIN = "y" * 40


def H(token):
    return {"Authorization": f"Bearer {token}"}


def make(tmp_path, role):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/{role}.db", role=role,
                                         bootstrap_admin_token=ADMIN))
    client = TestClient(app)
    client.__enter__()
    client.st = app.state.atlas
    r = client.post("/api/v2/tokens", json={"name": "c", "scopes": ["read", "contribute", "curate"]}, headers=H(ADMIN))
    client.cur = r.json()["token"]
    r = client.post("/api/v2/tokens", json={"name": "r", "scopes": ["read"]}, headers=H(ADMIN))
    client.ro = r.json()["token"]
    return client


@pytest.fixture
def local(tmp_path):
    c = make(tmp_path, "local")
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def authority(tmp_path):
    c = make(tmp_path, "authority")
    yield c
    c.__exit__(None, None, None)


def submit(client, source, ext, title, *, query="", **extra):
    body = {"connection": {"source_kind": "metadata", "source": source, "source_scope": "api"},
            "observations": [{"external_id": ext, "title": title, **extra}]}
    r = client.post(f"/api/v2/observations{query}", json=body, headers=H(client.cur))
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


def db(client):
    return client.st.maker()


def test_ingest_answers_carry_reason_and_decision_and_unchanged_is_opt_in(local):
    first = submit(local, "steam", "1", "Api Game", release_date="2020-02-02")
    assert first["atlas"]["reason"] == "none" and first["atlas"]["decision_id"]
    again = submit(local, "steam", "1", "Api Game", release_date="2020-02-02")
    assert again["status"] == "updated"  # pre-C11 clients see what they always saw
    third = submit(local, "steam", "1", "Api Game", release_date="2020-02-02", query="?report_unchanged=1")
    assert third["status"] == "unchanged" and third["atlas"]["canonical_id"] == first["atlas"]["canonical_id"]
    with db(local) as s:
        assert len(s.scalars(select(MatchDecision)).all()) == 1  # an unchanged item records nothing


def test_provenance_explains_each_field_and_hides_values_without_curate(local):
    a = submit(local, "steam", "1", "Prov Game", release_date="2020-02-02", facts={"description": "from steam"})
    submit(local, "gog", "2", "Prov Game", release_date="2020-02-02", facts={"description": "from gog"})
    cid = a["atlas"]["canonical_id"]
    r = local.get(f"/api/v2/games/{cid}/provenance", headers=H(local.ro))
    assert r.status_code == 200, r.text
    body = r.json()
    desc = body["fields"]["description"]
    assert desc["winner"]["source"] in ("steam", "gog") and desc["winner"]["class"] == "provider"
    assert desc["conflict"] is True and desc["reason"] in ("trust", "freshness", "tie_break")
    assert desc["value"] == f"from {desc['winner']['source']}"
    assert body["matcher"] == "m2" and body["trust_digest"].startswith("sha256:") and "decisions" not in body
    assert {o["source"] for o in body["observations"]} == {"steam", "gog"}
    assert local.get(f"/api/v2/games/{cid}/provenance?detail=values", headers=H(local.ro)).status_code == 403
    full = local.get(f"/api/v2/games/{cid}/provenance?detail=values", headers=H(local.cur)).json()
    assert full["fields"]["description"]["candidates"]
    obs = local.get(f"/api/v2/games/{cid}/provenance/observations?limit=1", headers=H(local.cur)).json()
    assert len(obs["results"]) == 1 and obs["next"]
    rest = local.get(f"/api/v2/games/{cid}/provenance/observations?cursor={obs['next']}", headers=H(local.cur)).json()
    assert len(rest["results"]) == 1 and rest["next"] is None
    decisions = local.get(f"/api/v2/games/{cid}/decisions", headers=H(local.cur)).json()["results"]
    assert {d["reason"] for d in decisions} == {"none", "exact"}
    one = local.get(f"/api/v2/observations/{obs['results'][0]['id']}/decision", headers=H(local.cur)).json()
    assert one["canonical_id"] == cid
    conflicts = local.get("/api/v2/conflicts?field=description", headers=H(local.cur)).json()
    assert [r["canonical_id"] for r in conflicts["results"]] == [cid]
    assert local.get("/api/v2/conflicts?field=description", headers=H(local.ro)).status_code == 403
    # the plain explain keeps its pre-C11 shape per field, with `_meta` added
    explain = local.get(f"/api/v2/games/{cid}/explain", headers=H(local.ro)).json()["explain"]
    assert explain["description"]["strategy"] == "priority" and explain["_meta"]["explain"] == 2


def test_merge_preview_merge_records_and_unmerge_over_http(local):
    a = submit(local, "steam", "1", "Http Left", release_date="2001-01-01")["atlas"]["canonical_id"]
    b = submit(local, "gog", "2", "Http Right", release_date="2011-01-01", facts={"developer": "Dev"})["atlas"][
        "canonical_id"]
    preview = local.post(f"/api/v2/games/{b}/merge?dry_run=1", json={"into": a}, headers=H(local.cur))
    assert preview.status_code == 200 and preview.json()["status"] == "preview"
    with db(local) as s:
        assert s.get(Canonical, b).merged_into is None
    assert local.post(f"/api/v2/games/{b}/merge", json={"into": a}, headers=H(local.cur)).status_code == 200
    listed = local.get(f"/api/v2/merges?canonical_id={a}", headers=H(local.cur)).json()["results"]
    assert [(m["src"], m["dst"], m["cause"], m["signed"]) for m in listed] == [(b, a, "curator", False)]
    mid = listed[0]["merge_id"]
    detail = local.get(f"/api/v2/merges/{mid}", headers=H(local.cur)).json()
    assert detail["items"]["chain"] == 1 and detail["src_view"]["title"] == "Http Right"
    etag = local.get(f"/api/v2/games/{a}", headers=H(local.ro)).headers["etag"]
    stale = local.post(f"/api/v2/merges/{mid}/unmerge", headers={**H(local.cur), "If-Match": '"x-1"'})
    assert stale.status_code == 412 and stale.json()["error"]["code"] == "stale_version"
    dry = local.post(f"/api/v2/merges/{mid}/unmerge", json={"dry_run": True}, headers=H(local.cur)).json()
    assert dry["status"] == "preview"
    done = local.post(f"/api/v2/merges/{mid}/unmerge", headers={**H(local.cur), "If-Match": etag})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "unmerged"
    again = local.post(f"/api/v2/merges/{mid}/unmerge", headers=H(local.cur))
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_reverted"
    game = local.get(f"/api/v2/games/{a}", headers=H(local.ro)).json()
    assert [u["id"] for u in game["unmerged"]] == [b]
    with db(local) as s:
        assert s.get(Canonical, b).merged_into is None and s.get(MergeRecord, mid).reverted_at is not None
        assert s.scalars(select(MatchRejection)).one().reason == "unmerge"
    blocked = local.post(f"/api/v2/games/{b}/merge", json={"into": a}, headers=H(local.cur))
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "distinct"
    assert blocked.json()["error"]["rejection_id"]
    forced = local.post(f"/api/v2/games/{b}/merge", json={"into": a, "override_distinct": True}, headers=H(local.cur))
    assert forced.status_code == 200


def test_a_published_merge_is_refused_an_unmerge_on_an_authority(authority):
    a = submit(authority, "steam", "1", "Signed Left", release_date="2001-01-01")["atlas"]["canonical_id"]
    b = submit(authority, "gog", "2", "Signed Right", release_date="2011-01-01")["atlas"]["canonical_id"]
    assert authority.post(f"/api/v2/games/{b}/merge", json={"into": a}, headers=H(authority.cur)).status_code == 200
    mid = authority.get(f"/api/v2/merges?canonical_id={a}", headers=H(authority.cur)).json()["results"][0]["merge_id"]
    r = authority.post(f"/api/v2/merges/{mid}/unmerge", headers=H(authority.cur))
    assert r.status_code == 409 and r.json()["error"]["code"] == "emit_protocol"
    with db(authority) as s:
        assert s.get(Canonical, b).merged_into == a


def test_distinct_endpoints_and_review_resolutions(local):
    a = submit(local, "steam", "1", "Left", release_date="2001-01-01")["atlas"]["canonical_id"]
    b = submit(local, "gog", "2", "Right", release_date="2001-01-01")["atlas"]["canonical_id"]
    c = submit(local, "igdb", "3", "Both", provider_ids={"steam": "1", "gog": "2"})
    assert c["atlas"]["review"] is True and c["atlas"]["reason"] == "conflict"
    items = local.get("/api/v2/review?order=priority&kind=id_conflict", headers=H(local.cur)).json()["results"]
    assert len(items) == 1
    item = items[0]
    assert item["reason"] == "conflict" and item["priority"] == 30 and item["decision_id"] and item["evidence"]
    r = local.post(f"/api/v2/review/{item['id']}/distinct", headers=H(local.cur))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "distinct"
    with db(local) as s:
        row = s.get(ReviewItem, item["id"])
        assert row.status == "distinct" and row.resolved_at is not None
        assert len(s.scalars(select(MatchRejection).where(MatchRejection.ended_at.is_(None))).all()) == 2
    marks = local.get(f"/api/v2/distinct?canonical_id={a}", headers=H(local.cur)).json()["results"]
    assert len(marks) == 1
    pair = local.post("/api/v2/distinct", json={"a": a, "b": b}, headers=H(local.cur)).json()
    assert pair["created"] is True
    assert local.post("/api/v2/distinct", json={"a": a, "b": b}, headers=H(local.cur)).json()["created"] is False
    ended = local.delete(f"/api/v2/distinct/{pair['id']}", headers=H(local.cur)).json()
    assert ended["ended_at"] and ended["end_reason"] == "curator"
    with db(local) as s:
        assert s.get(MatchRejection, pair["id"]).ended_at is not None


def test_review_detach_and_dismiss(local):
    a = submit(local, "steam", "1", "Split Me", release_date="2003-03-03")["atlas"]["canonical_id"]
    assert submit(local, "gog", "5", "Split Me", release_date="2003-03-03")["atlas"]["canonical_id"] == a
    submit(local, "epic", "2", "Left Alone", release_date="1999-03-03")
    submit(local, "igdb", "3", "Both", provider_ids={"steam": "1", "epic": "2"})
    item = local.get("/api/v2/review", headers=H(local.cur)).json()["results"][0]
    with db(local) as s:
        from atlas.models import Observation

        oid = s.scalars(select(Observation.id).where(Observation.external_id == "5")).one()
    r = local.post(f"/api/v2/review/{item['id']}/detach", json={"observation_id": oid}, headers=H(local.cur))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved" and r.json()["game"]["id"] != a
    with db(local) as s:
        assert s.get(ReviewItem, item["id"]).status == "resolved"
        assert s.get(Observation, oid).canonical_id == r.json()["game"]["id"]
    assert local.post(f"/api/v2/review/{item['id']}/detach", json={"observation_id": oid},
                      headers=H(local.cur)).status_code == 404  # closed
    submit(local, "rawg", "7", "Again Both", provider_ids={"steam": "1", "epic": "2"})
    open_items = local.get("/api/v2/review", headers=H(local.cur)).json()["results"]
    assert len(open_items) == 1
    rid = open_items[0]["id"]
    assert local.post(f"/api/v2/review/{rid}/dismiss", headers=H(local.cur)).json()["status"] == "dismissed"
    with db(local) as s:
        assert s.get(ReviewItem, rid).resolved_at is not None


def test_lock_clear_over_http(local, authority):
    cid = submit(local, "steam", "1", "Clear Api", facts={"description": "text"})["atlas"]["canonical_id"]
    bad = local.put(f"/api/v2/games/{cid}/locks/description", json={}, headers=H(local.cur))
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_lock"
    both = local.put(f"/api/v2/games/{cid}/locks/description", json={"value": "x", "clear": True},
                     headers=H(local.cur))
    assert both.status_code == 400
    ok = local.put(f"/api/v2/games/{cid}/locks/description", json={"clear": True}, headers=H(local.cur))
    assert ok.status_code == 200 and "description" not in ok.json()
    with db(local) as s:
        lk = s.get(Lock, (cid, "description"))
        assert lk.cleared is True and lk.local_only is True
    assert local.delete(f"/api/v2/games/{cid}/locks/description", headers=H(local.cur)).status_code == 200
    assert local.get(f"/api/v2/games/{cid}", headers=H(local.ro)).json()["description"] == "text"
    # on an authority a clear is kept on the node itself: it is never signed into the feed in this release
    cid2 = submit(authority, "steam", "1", "Clear Auth", facts={"description": "text"})["atlas"]["canonical_id"]
    r = authority.put(f"/api/v2/games/{cid2}/locks/description", json={"clear": True}, headers=H(authority.cur))
    assert r.status_code == 200
    with db(authority) as s:
        from atlas.models import ChangeLog

        assert s.get(Lock, (cid2, "description")).local_only is True
        assert not [e for e in s.scalars(select(ChangeLog)) if e.kind == "lock"]


def test_an_unmerge_check_is_never_merged_over_http_or_the_web(local):
    """An `unmerge_check` (C11 §11.10.3) names the survivor and the record an unmerge split off it: merging them
    would redo the merge that was just undone. The API refuses it and the web queue offers no Merge button."""
    a = submit(local, "steam", "1", "Check Left", release_date="2001-01-01")["atlas"]["canonical_id"]
    b = submit(local, "gog", "2", "Check Right", release_date="2011-01-01")["atlas"]["canonical_id"]
    assert local.put(f"/api/v2/games/{b}/locks/series", json={"value": "B series", "local_only": True},
                     headers=H(local.cur)).status_code == 200
    assert local.post(f"/api/v2/games/{b}/merge", json={"into": a}, headers=H(local.cur)).status_code == 200
    assert local.put(f"/api/v2/games/{a}/locks/series", json={"value": "Edited after", "local_only": True},
                     headers=H(local.cur)).status_code == 200
    mid = local.get(f"/api/v2/merges?canonical_id={a}", headers=H(local.cur)).json()["results"][0]["merge_id"]
    done = local.post(f"/api/v2/merges/{mid}/unmerge", json={"remember_distinct": False}, headers=H(local.cur))
    assert done.status_code == 200, done.text
    rid = done.json()["review_id"]
    queued = local.get("/api/v2/review?kind=unmerge_check", headers=H(local.cur)).json()["results"]
    assert [(r["id"], r["canonical_id"], r["candidates"][0][0]) for r in queued] == [(rid, a, b)]
    r = local.post(f"/api/v2/review/{rid}/merge", json={"into": b}, headers=H(local.cur))
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    with db(local) as s:
        assert s.get(Canonical, a).merged_into is None and s.get(Canonical, b).merged_into is None
        assert s.get(ReviewItem, rid).status == "open"
    r = local.post(f"/api/v2/review/{rid}/dismiss", headers=H(local.cur))
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
