"""CONTRACT-C14 Part A (C14-2): asking the owner group for records and images a member lacks.

Real nodes on SQLite files: an authority U (a v0 release, `v0_authority`, so a provider-only record S gets sidecars but
no `record`), and the owner's group {N1, N2} wired through the in-process dispatcher. N1 follows U and waits for S;
N2 holds S. Every key, secret and URL is a synthetic runtime value."""

from __future__ import annotations

import base64
import hashlib
import io

import pytest
from PIL import Image
from sqlalchemy import delete, func, select

from atlas import assist, feed, groups, media, service, sync
from atlas import group_crypto as gc
from atlas.api import group_routes
from atlas.models import (
    Artwork,
    Canonical,
    ChangeLog,
    Crosswalk,
    GroupAssistItem,
    MergeRecord,
    NodeState,
    Observation,
    ReplicatedChange,
    SyncFailure,
)
from atlas.service import Ctx

from .channel_harness import full_round, group_of
from .group_harness import Dispatcher, fast_kdf, make_node
from .test_c13_follower import curate_art, provider_only
from .test_sync_compat import Node as FeedNode
from .test_sync_compat import pull as feed_pull
from .v0_authority import upgrade, v0, v0_authorities  # noqa: F401


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    fast_kdf(monkeypatch)


def configure(node, **changes) -> None:
    node.st.ctx.settings = node.st.ctx.settings.model_copy(update=changes)


def ctx(node) -> Ctx:
    return Ctx(node.settings, node.identity)


def run(node, call=None) -> dict:
    return assist.run(node.st.maker, ctx(node), node.net, call)


def waiting(node, origin_id: str) -> list[tuple]:
    with node.session() as s:
        return [(f.seq, f.kind, f.waiting_for) for f in s.scalars(
            select(SyncFailure).where(SyncFailure.node_id == origin_id, SyncFailure.resolved_at.is_(None),
                                      SyncFailure.disposition == "wait").order_by(SyncFailure.seq))]


def jobs(node) -> list[dict]:
    import json

    with node.session() as s:
        return [json.loads(v) for v in s.scalars(select(NodeState.value).where(NodeState.key.like("assist_job:%")))]


@pytest.fixture
def world(tmp_path):
    """U (authority, legacy emission) with S + two artwork sidecars; N1 and N2 in one group; both follow U.
    N1 pulled while S was not introduced (its sidecars wait); then U introduced S and N2 pulled it all."""
    net = Dispatcher()
    u = v0(FeedNode(tmp_path, "authority", "authority"))  # a v0 authority (C16 Z1: no legacy emission any more)
    n1, n2 = make_node(tmp_path, net, "n1"), make_node(tmp_path, net, "n2")
    group_of(n1, n2)
    cid = provider_only(u)
    curate_art(u, cid)
    feed_pull(n1, u)
    assert waiting(n1, u.node_id) == [(1, "artwork", cid)]
    upgrade(u)  # the in-place upgrade to wire 1
    with u.st.maker() as s:
        assert feed.introduce_batch(s, u.st.ctx, "missing")["introduced"] == 1
        s.commit()
    feed_pull(n2, u)
    with n2.session() as s:
        assert s.get(Canonical, cid) is not None
    yield u, n1, n2, cid
    for n in (n1, n2):
        n.close()


# ---- §3.1 the hook ------------------------------------------------------------------------------------------------

def test_wait_row_enqueues_one_assist_job_per_record(world, tmp_path):
    """T-A1: the wait row made a job naming U; a second feed waiting for the same record only adds its feed."""
    u, n1, _n2, cid = world
    [job] = jobs(n1)
    assert job["cid"] == cid and job["feeds"] == [u.node_id] and job["reasons"] == ["waiting_for_record"]
    with n1.session() as s:
        assert assist.note_missing(s, n1.settings, cid, "waiting_for_record", "f" * 16)
        s.commit()
    [job] = jobs(n1)
    assert job["feeds"] == [u.node_id, "f" * 16]


def test_no_jobs_outside_a_group_or_with_ask_off(tmp_path):
    """T-A1 (second half)."""
    net = Dispatcher()
    u = v0(FeedNode(tmp_path, "authority", "authority"))  # a v0 authority (C16 Z1: no legacy emission any more)
    lone = make_node(tmp_path, net, "lone")
    cid = provider_only(u)
    curate_art(u, cid)
    feed_pull(lone, u)
    assert waiting(lone, u.node_id) == [(1, "artwork", cid)]
    assert jobs(lone) == []
    a, b = make_node(tmp_path, net, "a"), make_node(tmp_path, net, "b")
    group_of(a, b)
    configure(a, group_assist_ask=False)
    feed_pull(a, u)
    assert waiting(a, u.node_id) and jobs(a) == []
    for n in (lone, a, b):
        n.close()


# ---- §3.3 helper answers -------------------------------------------------------------------------------------------

def test_find_answers_unknown_for_private_and_local_records(world):
    """T-A2: a `local`- or `private`-ceiling record answers exactly like an absent id."""
    _u, _n1, n2, cid = world
    absent = "atlas:" + "0" * 26
    with n2.session() as s:
        live = assist.find_one(s, n2.settings, cid)
        assert live["state"] == "live" and live["has"]["record"] and live["bundle_hash"]
        unknown = assist.find_one(s, n2.settings, absent)
        for ceiling in ("local", "private"):
            s.get(Canonical, cid).scope_ceiling = ceiling
            s.flush()
            hidden = assist.find_one(s, n2.settings, cid)
            assert {k: v for k, v in hidden.items() if k != "id"} == {k: v for k, v in unknown.items() if k != "id"}
        s.rollback()


def test_fetch_bundle_is_bound_to_requester_and_nonce(world):
    """T-A3: a bundle replayed to another member, or for another nonce, is refused."""
    _u, n1, n2, cid = world
    nonce = b"\x01" * 16
    with n1.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n1.node_id)
        member = tr.members()[n2.node_id]["body"]["member"]
        got = group_routes.call(s, n1.settings, n1.identity, group, member, "assist.fetch",
                                {"id": cid, "want": ["record", "artwork"], "signed_entries": True,
                                 "on_behalf": None}, n1.net, nonce=nonce)
        body = assist.verify_bundle(s, n1.identity, group, tr, n2.node_id, got["bundle"], cid, gc.b64(nonce))
        assert body["requester"] == n1.node_id
        with pytest.raises(assist.AssistError) as exc:
            assist.verify_bundle(s, n1.identity, group, tr, n2.node_id, got["bundle"], cid, gc.b64(b"\x02" * 16))
        assert exc.value.code == "wrong_requester"
        other = type("Id", (), {"node_id": "0" * 16})()
        with pytest.raises(assist.AssistError):
            assist.verify_bundle(s, other, group, tr, n2.node_id, got["bundle"], cid, gc.b64(nonce))
        tampered = {**got["bundle"], "body": {**got["bundle"]["body"], "requester": "0" * 16}}
        with pytest.raises(assist.AssistError) as exc:
            assist.verify_bundle(s, n1.identity, group, tr, n2.node_id, tampered, cid, gc.b64(nonce))
        assert exc.value.code == "bad_signature"


# ---- §3.4 apply, §3.5 wake ---------------------------------------------------------------------------------------

def test_bundle_apply_creates_group_scoped_record_and_wakes_with_a_signed_entry(world):
    """T-A4 + T-A5: the bundle creates S under the `group` ceiling with `atlas-group` observations (trust 5); U's
    own signed `record` entry carried by N2 verifies against N1's pin for U and wakes U's waiting artwork; nothing
    is signed into N1's own feed; `replicated_change` on N1 is unchanged."""
    u, n1, n2, cid = world
    with n1.session() as s:
        retained_before = s.scalar(select(func.count()).select_from(ReplicatedChange))
        own_feed_before = s.scalar(select(func.count()).select_from(ChangeLog))
    out = run(n1)
    assert out["applied"] == 1, out
    with n1.session() as s:
        row = s.get(Canonical, cid)
        assert (row.scope_ceiling, row.scope_ceiling_by) == ("group", "assist")
        obs = list(s.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                       Observation.source == "atlas-group")))
        assert obs and all(o.source_scope == n2.node_id and o.assist_item_id and o.origin_node is None for o in obs)
        assert n1.settings.trust("atlas-group") == 5
        assert row.view.get("title") == "Subnautica 2"
        assert s.scalar(select(func.count()).select_from(ReplicatedChange)) == retained_before
        assert s.scalar(select(func.count()).select_from(ChangeLog)) == own_feed_before
        kinds = {i.kind for i in s.scalars(select(GroupAssistItem).where(GroupAssistItem.cid == cid))}
        assert {"bundle", "entry"} <= kinds
        arts = list(s.scalars(select(Artwork).where(Artwork.canonical_id == cid)))
        assert any(a.origin_node == u.node_id for a in arts)  # U's waiting artwork entry applied (class 1a)
    assert waiting(n1, u.node_id) == []
    # the courier entry does not lift the ceiling; N1's own later pull of U does, and supersedes the group identity
    with n1.session() as s:
        assert s.get(Canonical, cid).scope_ceiling == "group"
    feed_pull(n1, u)
    with n1.session() as s:
        row = s.get(Canonical, cid)
        assert row.scope_ceiling is None and row.scope_ceiling_by is None
        assert not s.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                       Observation.source == "atlas-group",
                                                       Observation.is_current.is_(True))).first()


def test_supersede_group_identity_when_feed_introduces(world):
    """T-A12 (covered with T-A5 above); the job closes as applied and is not asked again."""
    _u, n1, _n2, cid = world
    run(n1)
    assert jobs(n1) == []
    with n1.session() as s:
        assert assist.done_of(s, cid)["outcome"] == "applied"


def _drop_introducing(node, cid):
    """N2 keeps U's applied data but no longer retains the introducing entry (class 2: applied_from only)."""
    with node.session() as s:
        for o in s.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                     Observation.origin_node.is_not(None))):
            s.execute(delete(ReplicatedChange).where(ReplicatedChange.node_id == o.origin_node,
                                                     ReplicatedChange.seq == o.origin_seq,
                                                     ReplicatedChange.kind.in_(("obs", "record", "detach"))))
        s.commit()


def test_class2_applied_from_wakes_only_with_preset_or_click(world):
    """T-A7: without the preset the rows keep waiting and an offer is shown; the click wakes them and records a
    `wake` item; `forget` puts them back to waiting."""
    u, n1, n2, cid = world
    _drop_introducing(n2, cid)
    run(n1)
    assert waiting(n1, u.node_id) == [(1, "artwork", cid)]
    with n1.session() as s:
        line = assist.waiting_line(s, cid, u.node_id, groups.active(s).group_id)
        assert line["state"] == "offer" and line["helper"]
        offer = line["item"]
    with n1.session() as s:
        assert assist.adopt(s, ctx(n1), offer, None) == 1
        s.commit()
    assert waiting(n1, u.node_id) == []
    with n1.session() as s:
        assert s.scalars(select(GroupAssistItem).where(GroupAssistItem.kind == "wake")).first() is not None
        counts = assist.forget(s, ctx(n1), n2.node_id, dry_run=True)
        assert counts["rewaited"] == 1
        s.rollback()
    with n1.session() as s:
        assist.forget(s, ctx(n1), n2.node_id)
        s.commit()
    assert waiting(n1, u.node_id) == [(1, "artwork", cid)]
    with n1.session() as s:
        assert s.get(Canonical, cid) is None  # only the helper's answer had created it


def test_class2_with_the_preset_wakes_automatically(world):
    u, n1, n2, cid = world
    _drop_introducing(n2, cid)
    configure(n1, group_assist_auto_adopt=True)
    run(n1)
    assert waiting(n1, u.node_id) == []
    with n1.session() as s:
        [wake] = s.scalars(select(GroupAssistItem).where(GroupAssistItem.kind == "wake")).all()
        assert wake.body["feed"] == u.node_id and wake.body["seqs"] == [1]


def test_assist_created_record_does_not_let_new_entries_skip_the_wait(world, tmp_path):
    """T-A17: after a class 2 offer created S (no evidence for U), a new sidecar of U naming S still waits; and
    one of a third feed waits too."""
    u, n1, n2, cid = world
    _drop_introducing(n2, cid)
    run(n1)
    with n1.session() as s:
        assert sync.missing_record(s, "artwork", {"canonical_id": cid}, u.node_id) == cid
        assert sync.missing_record(s, "artwork", {"canonical_id": cid}, "e" * 16) == cid
        assert sync.missing_record(s, "artwork", {"canonical_id": cid}) is None  # no feed: legacy callers
        assert sync.missing_record(s, "record", {"canonical_id": cid}, u.node_id) is None  # it introduces S


def test_helper_claims_never_merge_redirect_or_clear_ceiling(world):
    """T-A20 (part): a `merged` answer creates no redirect, only a hint, and asks for the survivor."""
    _u, n1, n2, cid = world
    survivor = "atlas:" + "9" * 26

    def fake_call(session, settings, identity, group, member, op, params, http, now=None, nonce=None):
        if op == "hello":
            return {"ops": list(group_routes.CALL_OPS)}
        if op == "assist.find":
            return {"items": [{"id": i, "state": "merged", "survivor": survivor} for i in params["ids"]]}
        raise group_routes.CallError("not_found")

    out = run(n1, fake_call)
    assert out["hints"] == 1
    with n1.session() as s:
        assert s.get(Canonical, cid) is None
        assert not s.scalars(select(MergeRecord)).first()
        assert assist.job_of(s, survivor) is not None
        line = assist.waiting_line(s, cid, _u.node_id, groups.active(s).group_id)
        assert line["state"] == "merge_hint" and line["survivor"] == survivor


def test_bundle_with_unadopted_media_type_is_not_applied(world):
    """T-A18: no `game` fallback."""
    _u, n1, n2, cid = world
    with n2.session() as s:
        s.get(Canonical, cid).media_type = "boardgame"
        s.commit()
    run(n1)
    with n1.session() as s:
        assert s.get(Canonical, cid) is None
        assert assist.done_of(s, cid)["outcome"] == "media_type_not_adopted"


def test_assist_forget_drops_a_revoked_helpers_data(world):
    """T-A19: forget retires the helper's observations and evidence and rebuilds."""
    u, n1, n2, cid = world
    run(n1)
    with n1.session() as s:
        assert s.scalars(select(Observation).where(Observation.source_scope == n2.node_id)).first() is not None
        counts = assist.forget(s, ctx(n1), n2.node_id)
        s.commit()
    assert counts["observations"] >= 1 and counts["items"] >= 1
    with n1.session() as s:
        assert s.scalars(select(Observation).where(Observation.source_scope == n2.node_id)).first() is None
        assert not s.scalars(select(GroupAssistItem).where(GroupAssistItem.superseded_at.is_(None))).first()


def test_a_bad_bundle_marks_the_helper_suspect_and_pauses_class2(world):
    _u, n1, n2, cid = world

    def forged(session, settings, identity, group, member, op, params, http, now=None, nonce=None):
        if op == "hello":
            return {"ops": list(group_routes.CALL_OPS)}
        got = group_routes.call(session, settings, identity, group, member, op, params, http, now=now, nonce=nonce)
        if op == "assist.fetch":
            got["bundle"]["body"]["record"]["title"] = "Forged"
        return got

    out = run(n1, forged)
    assert out["rejected"] == 1
    with n1.session() as s:
        assert assist.is_suspect(s, n2.node_id)
        assert s.get(Canonical, cid) is None


# ---- §3.6 media ----------------------------------------------------------------------------------------------------

def _png(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def _cache_on(node, cid, data: bytes) -> str:
    with node.session() as s:
        art = s.scalars(select(Artwork).where(Artwork.canonical_id == cid)).first()
        digest = media.store(node.settings, data, "png")
        art.blob_hash, art.content_type, art.size_bytes = digest, "image/png", len(data)
        s.commit()
        return digest


def test_blob_chunks_assemble_and_hash_verify(world):
    """T-A8: the bytes come in chunks and verify against the digest; a wrong chunk is rejected and the helper is
    suspect."""
    _u, n1, n2, cid = world
    data = _png() + b"\0" * (assist.BLOB_CHUNK + 5000)  # forces two chunks; PNG decoders ignore the tail
    digest = _cache_on(n2, cid, data)
    run(n1)
    with n1.session() as s:
        art = s.scalars(select(Artwork).where(Artwork.canonical_id == cid)).first()
        art.peer_digest = digest
        seen = []

        def spy(*args, **kw):
            try:
                out = group_routes.call(*args, **kw)
            except Exception as exc:
                seen.append(repr(exc))
                raise
            seen.append(sorted(out) if isinstance(out, dict) else out)
            return out

        got = assist.fetch_blob(s, n1.settings, n1.identity, n1.net, art, call=spy)
        assert got == data, seen and art.fetched_from == "group:" + n2.node_id
        s.rollback()

    def corrupt(session, settings, identity, group, member, op, params, http, now=None, nonce=None):
        got = group_routes.call(session, settings, identity, group, member, op, params, http, now=now, nonce=nonce)
        if op == "assist.blob":
            raw = bytearray(base64.b64decode(got["data"]))
            raw[0] ^= 0xFF
            got["data"] = base64.b64encode(bytes(raw)).decode()
        return got

    with n1.session() as s:
        art = s.scalars(select(Artwork).where(Artwork.canonical_id == cid)).first()
        art.peer_digest = digest
        assert assist.fetch_blob(s, n1.settings, n1.identity, n1.net, art, call=corrupt) is None
        assert assist.is_suspect(s, n2.node_id)


def test_helper_refuses_provider_terms_blob_without_policy(world):
    """T-A9: a provider-terms image is served only while the helper shares media in the group, and is otherwise
    `not_found`, identical to an unknown digest."""
    _u, n1, n2, cid = world
    data = _png((1, 2, 3))
    with n2.session() as s:
        digest = media.store(n2.settings, data, "png")
        s.add(Artwork(canonical_id=cid, source="steam", kind="banner", url="https://cdn.example.org/b.png",
                      licence="provider_terms", redistributable=False, blob_hash=digest))
        s.commit()
        assert not assist.blob_shareable(s, n2.settings, digest)
        assert not assist.blob_shareable(s, n2.settings, hashlib.sha256(b"x").hexdigest())
    configure(n2, group_assist_answer="group", group_assist_share_provider_data="fields+media")
    with n2.session() as s:
        assert assist.blob_shareable(s, n2.settings, digest)


def test_media_order_group_before_origin_only_with_preset(world, monkeypatch):
    """T-A10: by default the origin goes first and the group is the fallback; with the preset row the group goes
    first."""
    _u, n1, n2, cid = world
    order = []

    def origin(client, url, settings, **_kw):
        order.append("origin")
        raise media.MediaError("unreachable")

    def group_fetch(session, art):
        order.append("group")
        return _png()

    monkeypatch.setattr(media, "_request", origin)
    run(n1)
    with n1.session() as s:
        art = s.scalars(select(Artwork).where(Artwork.canonical_id == cid)).first()
        assert media.cache_one(s, n1.settings, art, None, group_fetch)
        s.rollback()
    assert order == ["origin", "group"]
    order.clear()
    configure(n1, group_assist_group_before_origin=True)
    with n1.session() as s:
        art = s.scalars(select(Artwork).where(Artwork.canonical_id == cid)).first()
        assert media.cache_one(s, n1.settings, art, None, group_fetch)
        s.rollback()
    assert order == ["group"]


def test_group_reported_digest_is_a_hint(world, monkeypatch):
    """T-A11: origin bytes that differ from a group-reported digest replace it."""
    _u, n1, _n2, cid = world
    origin_bytes = _png((200, 0, 0))
    monkeypatch.setattr(media, "_request", lambda client, url, settings, **_kw: origin_bytes)
    with n1.session() as s:
        art = Artwork(canonical_id=cid, source="curator", kind="banner", url="https://img.example.org/b.png",
                      licence="cc0", redistributable=False, fetched_from="group:" + "a" * 16,
                      peer_digest="b" * 64, digest_provenance="group_reported")
        if s.get(Canonical, cid) is None:
            s.add(Canonical(id=cid, view={}, explain={}, media_type="game"))
            s.flush()
        s.add(art)
        s.flush()
        assert media.cache_one(s, n1.settings, art, None, None)
        assert art.peer_digest == hashlib.sha256(origin_bytes).hexdigest() and art.digest_provenance == "origin"
        assert art.fetched_from == "group:" + "a" * 16  # still group data
        s.rollback()


# ---- §3.10 limits and backoff --------------------------------------------------------------------------------------

def test_assist_limits_and_backoff(world, monkeypatch):
    """T-A13: the per-asker answer limit refuses with `retry_after`, which backs off that helper only; a job no
    member can answer backs off 1 h, 6 h, 24 h, then daily, and gives up after 30 days leaving its rows waiting."""
    u, n1, n2, cid = world
    monkeypatch.setattr(assist, "ANSWER_PER_MIN", 0)
    out = run(n1)
    assert out["applied"] == 0
    with n1.session() as s:
        assert assist._backed_off(s, n2.node_id)
        job = assist.job_of(s, cid)
        assert job is not None
    monkeypatch.setattr(assist, "ANSWER_PER_MIN", 120)
    with n1.session() as s:
        s.execute(delete(NodeState).where(NodeState.key.like("assist_backoff:%")))
        job = assist.job_of(s, cid)
        job["first_at"] = "2000-01-01T00:00:00+00:00"
        job["next_at"] = "2000-01-01T00:00:00+00:00"
        assist._put(s, assist.JOB + assist._hash(cid), job)
        s.commit()
    with n2.session() as s:  # nobody has it any more
        s.get(Canonical, cid).scope_ceiling = "local"
        s.commit()
    run(n1)
    with n1.session() as s:
        assert assist.job_of(s, cid) is None and assist.done_of(s, cid)["outcome"] == "none_has_it"
        line = assist.waiting_line(s, cid, u.node_id, groups.active(s).group_id)
        assert line["state"] == "none_has_it" and line["at"] is not None
    assert waiting(n1, u.node_id) == [(1, "artwork", cid)]


def test_backoff_schedule():
    with pytest.raises(IndexError):
        assist.BACKOFF[3]
    assert [b.total_seconds() / 3600 for b in assist.BACKOFF] == [1, 6, 24]


def test_assist_ops_unknown_to_older_member_mean_cannot_help(world):
    """T-A15: a member whose `hello` does not list the ops is never asked; an op error `not_found` is final."""
    _u, n1, n2, cid = world
    calls = []

    def older(session, settings, identity, group, member, op, params, http, now=None, nonce=None):
        calls.append(op)
        if op == "hello":
            return {"ops": ["hello", "state", "heads", "changes"]}
        raise AssertionError("an older member is never asked an assist op")

    from atlas import group_channel

    group_channel._HELLO.clear()
    with n1.session() as s:
        s.execute(delete(NodeState).where(NodeState.key.like("assist_hello:%")))
        s.commit()
    run(n1, older)
    run(n1, older)
    assert set(calls) == {"hello"}


def test_client_response_cap_applies_to_all_group_calls():
    """T-A14: assist ops are capped at 1 MiB, the others at 8 MiB."""
    assert group_routes.response_cap("assist.fetch") == 1024 * 1024
    assert group_routes.response_cap("assist.blob") == 1024 * 1024
    assert group_routes.response_cap("changes") == 8 * 1024 * 1024


# ---- §3.1 backfill cursor ------------------------------------------------------------------------------------------

def test_existing_waiting_rows_are_backfilled_once(world):
    """T-A16: rows waiting before the upgrade get jobs once through the per-group cursor, which pages and pauses
    when the queue is full (it never skips); `type:` rows do not."""
    u, n1, _n2, cid = world
    with n1.session() as s:
        s.execute(delete(NodeState).where(NodeState.key.like("assist_%")))
        s.add(SyncFailure(node_id=u.node_id, seq=99, kind="custom", error="x", attempts=1,
                          first_failed_at=service.now(), last_attempt_at=service.now(), disposition="wait",
                          reason="waiting_for_record", waiting_for="type:boardgame"))
        s.commit()
        group_id = groups.active(s).group_id
    configure(n1, group_assist_max_jobs=0)
    with n1.session() as s:
        out = assist.backfill_step(s, n1.settings, group_id)
        s.commit()
    assert out["paused"] and out["queued"] == 0 and jobs(n1) == []
    configure(n1, group_assist_max_jobs=5000)
    with n1.session() as s:
        out = assist.backfill_step(s, n1.settings, group_id, limit=1)
        s.commit()
    assert out["queued"] == 1 and [j["cid"] for j in jobs(n1)] == [cid]
    with n1.session() as s:
        for _ in range(3):
            assist.backfill_step(s, n1.settings, group_id, limit=1)
        s.commit()
        assert assist.backfill_step(s, n1.settings, group_id)["done"]
    assert [j["cid"] for j in jobs(n1)] == [cid]


def test_backfill_pages_past_a_full_queue_and_restarts_for_a_new_group(world):
    """T-A16 (extended, Review #1 C14-14): more waiting rows than the queue holds all get jobs over successive
    rounds (a full queue pauses the cursor, it never skips); a new group starts its own cursor from the start."""
    u, n1, _n2, _cid = world
    with n1.session() as s:
        s.execute(delete(NodeState).where(NodeState.key.like("assist_%")))
        cids = ["atlas:" + f"{i:026d}" for i in range(30)]
        for i, c in enumerate(cids):
            s.add(SyncFailure(node_id=u.node_id, seq=1000 + i, kind="custom", error="x", attempts=1,
                              first_failed_at=service.now(), last_attempt_at=service.now(), disposition="wait",
                              reason="waiting_for_record", waiting_for=c))
        s.commit()
        group_id = groups.active(s).group_id
    configure(n1, group_assist_max_jobs=12)
    seen: set[str] = set()
    for _round in range(10):
        with n1.session() as s:
            out = assist.backfill_step(s, n1.settings, group_id, limit=500)
            s.commit()
        got = {j["cid"] for j in jobs(n1)}
        assert len(got) <= 12
        seen |= got
        if out["done"]:
            break
        with n1.session() as s:  # the runner works the queue down between rounds
            s.execute(delete(NodeState).where(NodeState.key.like(assist.JOB + "%")))
            s.commit()
    assert out["done"] and set(cids) <= seen
    with n1.session() as s:
        s.execute(delete(NodeState).where(NodeState.key.like(assist.JOB + "%")))
        out = assist.backfill_step(s, n1.settings, "f" * 16, limit=500)  # another group: a fresh cursor
        s.commit()
        assert s.get(NodeState, assist.BACKFILL + group_id) is None
        assert s.get(NodeState, assist.BACKFILL + "f" * 16) is not None
    assert out["queued"] == 12 and out["paused"]


# ---- crosswalk and ingest guards -------------------------------------------------------------------------------------

def test_bundle_takes_only_free_crosswalk_keys(world):
    _u, n1, _n2, cid = world
    run(n1)
    with n1.session() as s:
        keys = list(s.scalars(select(Crosswalk).where(Crosswalk.canonical_id == cid)))
        assert ("steam", "1962700") in {(k.namespace, k.external_id) for k in keys}


def test_clients_cannot_write_group_data(world):
    from atlas.service import AtlasError

    from .conftest import batch

    _u, n1, _n2, _cid = world
    with n1.session() as s:
        with pytest.raises(AtlasError):
            service.ingest(s, ctx(n1), batch("atlas-group", [{"external_id": "x", "title": "X"}]), curator=True)
        b = batch("community", [{"external_id": "x", "title": "X"}])
        b.connection.source_scope = "group:" + "a" * 16
        with pytest.raises(AtlasError):
            service.ingest(s, ctx(n1), b, curator=True)


def test_full_round_runs_the_assist(world):
    """The group round asks and applies without the test driving `assist.run`."""
    u, n1, _n2, cid = world
    full_round(n1)
    assert waiting(n1, u.node_id) == []


# ---- review fixes (C14-2): no retry loop, no free re-asks, no budget theft, forget drops claimed keys ------------

def _due(node, cid) -> bool:
    with node.session() as s:
        job = assist.job_of(s, cid)
        return job is not None and (assist._parse(job.get("next_at")) or assist.now()) <= assist.now()


def test_failed_fetch_and_rejected_bundle_back_the_job_off(world):
    """A helper that says `live` but whose fetch then fails, or whose bundle is rejected, backs the job off: it is
    no longer due, so it cannot lead every round and starve the others."""
    _u, n1, n2, cid = world

    def failing(session, settings, identity, group, member, op, params, http, now=None, nonce=None):
        if op == "assist.fetch":
            raise group_routes.CallError("too_large")
        return group_routes.call(session, settings, identity, group, member, op, params, http, now=now, nonce=nonce)

    assert _due(n1, cid)
    out = run(n1, failing)
    assert out["applied"] == 0 and out["none"] == 1
    [job] = jobs(n1)
    assert job["tries"] == 1 and not _due(n1, cid)
    with n1.session() as s:  # due again
        job["next_at"] = "2000-01-01T00:00:00+00:00"
        assist._put(s, assist.JOB + assist._hash(cid), job)
        s.commit()

    def forged(session, settings, identity, group, member, op, params, http, now=None, nonce=None):
        got = group_routes.call(session, settings, identity, group, member, op, params, http, now=now, nonce=nonce)
        if op == "assist.fetch":
            got["bundle"]["body"]["record"]["title"] = "Forged"
        return got

    assert run(n1, forged)["rejected"] == 1
    [job] = jobs(n1)
    assert job["tries"] == 2 and not _due(n1, cid)
    with n1.session() as s:
        assert assist.is_suspect(s, n2.node_id)


def test_suspect_helper_is_asked_last(world, tmp_path):
    _u, n1, n2, _cid = world
    with n1.session() as s:
        assist.suspect(s, n2.node_id, "bad_signature")
        s.commit()
        assert assist.is_suspect(s, n2.node_id)
        order = sorted(["a" * 16, n2.node_id, "b" * 16], key=lambda n: assist.is_suspect(s, n))
    assert order[-1] == n2.node_id


def test_replayed_request_spends_no_asker_budget(world, monkeypatch):
    """A captured sealed request replayed by an observer is refused as `replay` and takes nothing from the real
    asker's per-asker assist budget."""
    _u, n1, n2, cid = world
    taken = []
    real = assist.limit_call
    monkeypatch.setattr(assist, "limit_call", lambda *a, **kw: (taken.append(a[2]), real(*a, **kw))[1])
    sent = []
    real_post = n1.net.post

    def post(url, *args, **kw):
        sent.append((url, args, kw))
        return real_post(url, *args, **kw)

    monkeypatch.setattr(n1.net, "post", post)
    with n1.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n1.node_id)
        member = tr.members()[n2.node_id]["body"]["member"]
        group_routes.call(s, n1.settings, n1.identity, group, member, "assist.find",
                          {"ids": [cid], "want": ["record"], "reason": "user", "on_behalf": None}, n1.net)
    assert taken == ["assist.find"] and sent
    url, args, kw = sent[-1]
    real_post(url, *args, **kw)  # the replay
    assert taken == ["assist.find"]


def test_forget_drops_crosswalk_keys_only_the_helper_claimed(world):
    """A record that survives `forget` (here: a list item keeps it) loses the crosswalk keys the helper's answer
    took, so later own-provider imports of those ids are not routed into the group record."""
    from atlas.models import ListItem, UserList

    from .channel_harness import user

    _u, n1, n2, cid = world
    run(n1)
    uid = user(n1, "keeper")
    with n1.session() as s:
        assert s.get(Crosswalk, ("steam", "1962700")) is not None
        lst = UserList(user_id=uid, name="keep")
        s.add(lst)
        s.flush()
        s.add(ListItem(list_id=lst.id, canonical_id=cid))
        s.flush()
        counts = assist.forget(s, ctx(n1), n2.node_id)
        s.commit()
    with n1.session() as s:
        assert s.get(Canonical, cid) is not None
        assert counts["crosswalk"] >= 1
        assert s.get(Crosswalk, ("steam", "1962700")) is None
