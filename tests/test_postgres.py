"""Runs against real PostgreSQL: ATLAS_TEST_PG_URL, or a throwaway local server if `pgserver` is installed."""

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from atlas import db, feed, service
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
