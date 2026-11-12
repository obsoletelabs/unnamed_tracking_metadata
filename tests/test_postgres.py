"""Runs against real PostgreSQL: ATLAS_TEST_PG_URL, or a throwaway local server if `pgserver` is installed."""

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text

from atlas import custom, db, feed, service
from atlas.models import Canonical, ChangeLog, Crosswalk, Observation
from atlas.service import Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings


@pytest.fixture(scope="module")
def pg_url():
    url = os.environ.get("ATLAS_TEST_PG_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("no PostgreSQL available")
    srv = pgserver.get_server(tempfile.mkdtemp())
    yield srv.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    srv.cleanup()


@pytest.fixture
def pg(pg_url):
    engine = db.make_engine(pg_url)
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.upgrade(pg_url)
    maker = db.make_sessionmaker(engine)
    settings = build_settings(database_url=pg_url)
    with maker() as s:
        ctx = Ctx(settings, load_identity(s))
        s.commit()
    return maker, ctx


def test_concurrent_ingest_keeps_feed_gap_free_and_ids_unique(pg):
    maker, ctx = pg

    def work(i: int):
        with maker() as s:
            items = [{"external_id": f"shared-{n}", "title": f"Shared Game {n}", "release_date": "2020-01-01"}
                     for n in range(5)]
            items.append({"external_id": f"own-{i}", "title": f"Own Game {i}", "release_date": "2021-01-01"})
            service.ingest(s, ctx, batch("manual", items, kind="manual", scope=f"w{i}"), curator=True)
            s.commit()

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, range(8)))

    with maker() as s:
        seqs = list(s.scalars(select(ChangeLog.seq).order_by(ChangeLog.seq)))
        assert seqs == list(range(1, len(seqs) + 1))  # no gaps, no duplicates
        assert feed.head_seq(s) == len(seqs)
        # each shared title resolved to exactly one live canonical
        for n in range(5):
            cids = {c for (c,) in s.execute(select(Observation.canonical_id).where(
                Observation.external_id == f"shared-{n}"))}
            assert len(cids) == 1, cids
        live = s.scalar(select(func.count()).select_from(Canonical).where(Canonical.merged_into.is_(None)))
        assert live >= 5 + 8
        dupes = s.execute(select(Crosswalk.namespace, Crosswalk.external_id, func.count())
                          .group_by(Crosswalk.namespace, Crosswalk.external_id).having(func.count() > 1)).all()
        assert dupes == []


def test_rate_limit_counts_exactly_under_concurrency(pg):
    from atlas import ratelimit

    maker, _ = pg
    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(lambda _: ratelimit.hit(maker, "k", 10, clock=lambda: 1000.0)[0], range(40)))
    assert results.count(True) == 10 and results.count(False) == 30


@pytest.mark.parametrize("legacy", [False, True])
def test_concurrent_first_start_uses_one_node_identity(pg, legacy):
    from threading import Barrier

    from sqlalchemy import delete

    from atlas.models import NodeState

    maker, _ = pg
    with maker() as session:
        session.execute(delete(NodeState).where(NodeState.key != "signing_key") if legacy else delete(NodeState))
        session.commit()
    barrier = Barrier(8)

    def first_start(_):
        with maker() as session:
            barrier.wait(timeout=20)
            identity = load_identity(session)
            session.commit()
            return identity.node_id

    with ThreadPoolExecutor(8) as pool:
        assert len(set(pool.map(first_start, range(8)))) == 1


def test_mfa_recovery_code_is_consumed_once_under_concurrency(pg):
    from atlas import accounts, mfa
    from atlas.models import now

    maker, ctx = pg
    settings = ctx.settings.model_copy(update={"secret_key": "postgres-mfa-test-key"})
    with maker() as session:
        user = accounts.register(session, settings, "mfa-user", "postgres-test-password")
        factor = mfa.start(session, settings, user, "s" * 64)
        factor.enabled_at = now()
        code = mfa.recovery_codes(factor)[0]
        session.commit()

    def verify(_):
        with maker() as session:
            result = mfa.verify(session, settings, user.id, code)
            session.commit()
            return result

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(verify, range(8)))
    assert results.count(True) == 1 and results.count(False) == 7


def test_rotation_and_stale_feed_readers_share_one_gap_free_history(pg):
    from threading import Barrier

    from atlas import key_rotation

    maker, ctx = pg
    barrier = Barrier(9)

    def append(index):
        with maker() as session:
            # Cache the old ORM head before another worker advances it.
            feed.head_seq(session)
            barrier.wait(timeout=20)
            if index == 0:
                key_rotation.rotate(session, ctx.identity.key_id)
            else:
                feed.append(session, ctx.identity, "schema", {"types": [], "fields": []})
            session.commit()

    with ThreadPoolExecutor(9) as pool:
        list(pool.map(append, range(9)))
    with maker() as session:
        current = load_identity(session)
        history, keys = key_rotation.validate_history(current.node_id, current.trust_public_b64,
                      key_rotation.own_history(session, current.node_id), current.key_id)
        rows = list(session.scalars(select(ChangeLog).order_by(ChangeLog.seq)))
        assert [row.seq for row in rows] == list(range(1, 11))  # one wire_start (C16 Z1), eight schemas, a rotation
        assert [row.kind for row in rows].count("wire_start") == 1
        assert feed.head_seq(session) == 10
        assert all(key_rotation.check_entry(current.node_id, history, keys, key_rotation.proof(row, current.node_id))
                   for row in rows)


def test_offline_recovery_reads_a_postgres_mirror_without_mutating_it(pg, tmp_path):
    from types import SimpleNamespace

    from atlas import backup, promotion

    from .test_relay import node, publish, pull

    maker, ctx = pg
    authority = node(tmp_path, "source-authority", "authority")
    publish(authority[2], "PostgreSQL Recovery", "pg_note")
    settings = ctx.settings.model_copy(update={"role": "mirror"})
    mirror = SimpleNamespace(maker=maker, ctx=Ctx(settings, ctx.identity))
    assert pull(mirror, authority) > 0
    out = tmp_path / "postgres-candidate.tar.gz"
    result = promotion.candidate(settings, maker.kw["bind"], authority[2].ctx.identity.node_id, ctx.identity.node_id, out)
    assert result["node_id"] != ctx.identity.node_id
    assert backup.verify(out)["tables"]["canonical"] == 1
    with maker() as session:
        assert load_identity(session).node_id == ctx.identity.node_id
        assert feed.head_seq(session) == 0


def test_concurrent_community_races(pg):
    """Two reviewers approving one proposal, and two simultaneous first registrations."""
    from atlas import accounts, proposals
    from atlas.models import Proposal, User

    maker, ctx = pg
    with maker() as s:
        [r] = service.ingest(s, ctx, batch("igdb", [{"external_id": "1", "title": "Hades",
                                                     "release_date": "2020-09-17"}]), system=True)
        a = accounts.register(s, ctx.settings, "alice", "correct horse battery")
        b = accounts.register(s, ctx.settings, "bob", "correct horse battery")
        c = accounts.register(s, ctx.settings, "carol", "correct horse battery")
        b.role = c.role = "curator"
        u = accounts.register(s, ctx.settings, "dave", "correct horse battery")
        p = proposals.create(s, ctx, u, "edit", r.canonical_id, {"developer": "Dev"})
        s.commit()
        pid, rid, uid = p.id, r.canonical_id, u.id
        assert a.role == "admin"

    def approve(name):
        with maker() as s:
            reviewer = s.scalars(select(User).where(User.username == name)).one()
            try:
                proposals.review(s, ctx, reviewer, pid, approve=True)
                s.commit()
                return "ok"
            except Exception as exc:
                s.rollback()
                return type(exc).__name__

    with ThreadPoolExecutor(2) as ex:
        results = list(ex.map(approve, ["bob", "carol"]))
    assert sorted(results) == ["AtlasError", "ok"], results
    with maker() as s:
        assert s.get(User, uid).reputation == proposals.REWARD["edit"]  # paid once
        assert s.get(Proposal, pid).status == "approved"

    def register(name):
        with maker() as s:
            try:
                accounts.register(s, ctx.settings, name, "correct horse battery")
                s.commit()
            except Exception:
                s.rollback()

    with maker() as s:  # start the first-registration race from an empty user table
        s.execute(text("TRUNCATE app_user CASCADE"))
        s.commit()
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(register, [f"racer{i}" for i in range(6)]))
    with maker() as s:
        roles = [u.role for u in s.scalars(select(User))]
        assert roles.count("admin") == 1 and len(roles) == 6, roles
    _ = rid


def test_a_record_of_the_longest_media_type_key_is_stored(pg):
    maker, ctx = pg
    key = "a" * 32
    with maker() as s:
        custom.seed(s)
        custom.save_type(s, ctx, key, "Long")
        [r] = service.ingest(s, ctx, batch("manual", [{"external_id": "long", "title": "Long Type"}], kind="manual"),
                             curator=True, media_type=key)
        s.commit()
        assert s.get(Canonical, r.canonical_id).media_type == key

