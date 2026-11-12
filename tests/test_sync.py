import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from atlas import feed, service, sync
from atlas.db import make_sessionmaker
from atlas.models import Base, Canonical, Crosswalk, NodeState, Observation, SyncCursor, SyncFailure
from atlas.service import Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings


def make_node(**kw):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = make_sessionmaker(engine)
    with maker() as s:
        ident = load_identity(s)
        s.commit()
    return maker, ident, build_settings(**kw)


def pub_of(ident):
    return base64.b64encode(
        ident.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()


@pytest.fixture
def pair():
    a_maker, a_id, a_set = make_node(role="authority", redistributable_sources={"curator", "manual"})
    a_ctx = Ctx(a_set, a_id)
    m_maker, m_id, _ = make_node()
    m_set = build_settings(
        role="mirror", upstream_url="http://upstream", trusted_keys={a_id.node_id: pub_of(a_id)}
    )
    m_ctx = Ctx(m_set, m_id)

    def handler(request: httpx.Request) -> httpx.Response:
        with a_maker() as s:
            if request.url.path.endswith("/sync/info"):
                return httpx.Response(200, json={"node_id": a_id.node_id, "public_key": pub_of(a_id)})
            since = int(request.url.params["since"])
            rows = feed.read(s, since, int(request.url.params["limit"]))
            return httpx.Response(200, json={"entries": [
                {"seq": r.seq, "kind": r.kind, "body": r.body, "body_hash": r.body_hash,
                 "signature": r.signature, "key_id": r.key_id, "node_id": r.key_id} for r in rows]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return a_maker, a_ctx, m_maker, m_ctx, client


def curate(maker, ctx, source, items):
    with maker() as s:
        res = service.ingest(s, ctx, batch(source, items, kind="manual" if source == "manual" else "metadata"),
                             curator=True)
        s.commit()
        return res


def test_mirror_converges_and_preserves_ids(pair):
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [r] = curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "Hades", "release_date": "2020-09-17"}])
    assert sync.pull_all(m_maker, m_ctx, client, "http://upstream", None) == 2  # wire_start, the observation
    with m_maker() as s:
        row = s.get(Canonical, r.canonical_id)
        assert row.view["title"] == "Hades"
        assert s.get(Crosswalk, ("curator", "c1")).canonical_id == r.canonical_id
        assert s.get(SyncCursor, a_ctx.identity.node_id).last_seq == 2
    # idempotent when nothing new
    assert sync.pull_all(m_maker, m_ctx, client, "http://upstream", None) == 0


def test_merge_lock_detach_replicate(pair):
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [a] = curate(a_maker, a_ctx, "curator", [{"external_id": "1", "title": "Mystery"}])
    [b] = curate(a_maker, a_ctx, "curator", [{"external_id": "2", "title": "Mystery"}])
    with a_maker() as s:
        service.merge_canonicals(s, a_ctx, b.canonical_id, a.canonical_id)
        service.set_lock(s, a_ctx, a.canonical_id, "title", "Mystery Game")
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        live, redirected = service.get_canonical(s, b.canonical_id)
        assert live.id == a.canonical_id and live.view["title"] == "Mystery Game"
        obs = s.scalars(select(Observation).where(Observation.external_id == "2")).one()
        oid = obs.id
    with a_maker() as s:
        new = service.detach_observation(s, a_ctx, oid)
        s.commit()
        new_id = new.id
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert s.get(Crosswalk, ("curator", "2")).canonical_id == new_id


def test_provider_data_is_not_published(pair):
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    curate(a_maker, a_ctx, "steam", [{"external_id": "620", "title": "Portal 2"}])
    with a_maker() as s:
        assert feed.head_seq(s) == 0


def test_local_record_folds_into_authoritative_twin(pair):
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    with m_maker() as s:  # mirror cached a provider locally first
        [local] = service.ingest(s, m_ctx, batch("steam", [{"external_id": "620", "title": "Portal 2",
                                                            "release_date": "2011-04-19"}]), system=True)
        s.commit()
    [auth] = curate(a_maker, a_ctx, "curator", [{"external_id": "p2", "title": "Portal 2",
                                                 "provider_ids": {"steam": "620"}}])
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        live, redirected = service.get_canonical(s, local.canonical_id)
        assert live.id == auth.canonical_id and redirected
        assert set(live.view["sources"]) == {"curator", "steam"}


def test_rejects_untrusted_signature_and_gaps(pair):
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "A"}])
    curate(a_maker, a_ctx, "curator", [{"external_id": "c2", "title": "B"}])

    def tamper(request):
        resp = client._transport.handle_request(request)
        data = resp.read()
        import json
        j = json.loads(data)
        if "entries" in j:
            j["entries"][0]["body"]["canonical_id"] = "atlas:evil"
        return httpx.Response(200, json=j)

    evil = httpx.Client(transport=httpx.MockTransport(tamper))
    with pytest.raises(sync.SyncError, match="bad signature"):
        sync.pull_once(m_maker, m_ctx, evil, "http://upstream", None)

    def gap(request):
        resp = client._transport.handle_request(request)
        import json
        j = json.loads(resp.read())
        if "entries" in j:
            j["entries"] = j["entries"][1:]
        return httpx.Response(200, json=j)

    with pytest.raises(sync.SyncError, match="gap"):
        sync.pull_once(m_maker, m_ctx, httpx.Client(transport=httpx.MockTransport(gap)), "http://upstream", None)
    with m_maker() as s:
        assert s.scalars(select(Observation)).first() is None  # nothing applied


def test_untrusted_upstream_key_is_refused(pair):
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    m_ctx.settings.trusted_keys.clear()
    m_ctx.settings.trusted_keys["deadbeef"] = "AAAA"
    with pytest.raises(sync.SyncError, match="is not trusted"):
        sync.pull_once(m_maker, m_ctx, client, "http://upstream", None)


# --- failed entries are reported and retried (#90) ---------------------------------------------


@pytest.fixture
def flaky(monkeypatch):
    """Make applying one feed sequence number fail until `fail.clear()` is called."""
    fail: set[int] = set()
    real = sync._apply

    def apply(session, ctx, node_id, seq, kind, body):
        if seq in fail:
            # Write something first, to prove a failed entry is rolled back rather than half-applied.
            session.add(NodeState(key=f"partial:{seq}", value="x"))
            session.flush()
            raise ValueError("simulated apply failure")
        real(session, ctx, node_id, seq, kind, body)

    monkeypatch.setattr(sync, "_apply", apply)
    return fail


def test_failed_entry_is_recorded_rolled_back_and_does_not_block_healthy_ones(pair, flaky):
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [first] = curate(a_maker, a_ctx, "curator", [{"external_id": "1", "title": "Failing"}])
    [second] = curate(a_maker, a_ctx, "curator", [{"external_id": "2", "title": "Healthy"}])
    flaky.add(2)  # 1 is wire_start
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert s.get(Canonical, second.canonical_id).view["title"] == "Healthy"
        assert s.get(Canonical, first.canonical_id) is None
        assert s.get(NodeState, "partial:2") is None  # the half-applied write was rolled back
        assert s.get(SyncCursor, a_ctx.identity.node_id).last_seq == 3
        [failure] = s.scalars(select(SyncFailure)).all()
        assert (failure.seq, failure.kind, failure.resolved_at) == (2, "obs", None)
        assert "simulated apply failure" in failure.error
        assert sync.open_failures(s) == 1


def test_retry_converges_without_duplicates_once_the_cause_is_fixed(pair, flaky):
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [first] = curate(a_maker, a_ctx, "curator", [{"external_id": "1", "title": "Failing"}])
    flaky.add(2)  # 1 is wire_start
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    flaky.clear()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)  # nothing new; the retry still runs
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert s.get(Canonical, first.canonical_id).view["title"] == "Failing"
        assert len(s.scalars(select(Observation).where(Observation.external_id == "1")).all()) == 1
        [failure] = s.scalars(select(SyncFailure)).all()
        assert failure.resolved_at is not None
        assert sync.open_failures(s) == 0


def test_automatic_retries_back_off_but_an_administrator_can_retry_at_once(pair, flaky):
    """C13 §5.4 (replaces the fixed 20-attempt limit): the first retry runs on the next pull, later ones after
    1, 2, 4 ... minutes (at most every 6 hours), so a broken entry is neither re-run on every pull nor abandoned."""
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [first] = curate(a_maker, a_ctx, "curator", [{"external_id": "1", "title": "Failing"}])
    flaky.add(2)  # 1 is wire_start
    for _ in range(23):
        sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        failure = s.scalars(select(SyncFailure)).one()
        assert failure.attempts == 2  # the first attempt and the immediate retry; the next is a minute away
        assert sync.next_retry_at(failure) > datetime.now(UTC)
        failure.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)  # a minute and more passes
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        failure = s.scalars(select(SyncFailure)).one()
        assert failure.attempts == 3 and failure.resolved_at is None
        assert sync.next_retry_at(failure) - failure.last_attempt_at.replace(tzinfo=UTC) == timedelta(minutes=2)
    assert [sync.retry_delay(n) for n in (1, 2, 3, 4)] == [timedelta(0), timedelta(minutes=1), timedelta(minutes=2),
                                                           timedelta(minutes=4)]
    assert sync.retry_delay(40) == timedelta(hours=6)
    flaky.clear()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert sync.open_failures(s) == 1  # not due yet
        result = sync.retry_failures(s, m_ctx, due_only=False)
        s.commit()
        assert result == {"retried": 1, "resolved": 1, "still_failing": 0}
        assert s.get(Canonical, first.canonical_id) is not None
