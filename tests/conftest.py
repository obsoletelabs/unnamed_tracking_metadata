from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool

from atlas.db import make_engine, make_sessionmaker
from atlas.models import Base
from atlas.service import Ctx
from atlas.settings import Settings
from atlas.signing import load_identity


def build_settings(**kw) -> Settings:
    # Most tests are not about email; the policy tests ask for "required" or None (by role) explicitly.
    base = dict(role="authority", database_url="sqlite://", environment="dev", email_verification="optional")
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def maker():
    engine = make_engine("sqlite://")
    engine.dispose()
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return make_sessionmaker(engine)


@pytest.fixture
def session(maker):
    with maker() as s:
        yield s


@pytest.fixture
def ctx(session, settings) -> Ctx:
    identity = load_identity(session)
    session.commit()
    return Ctx(settings=settings, identity=identity)


def batch(source: str, items: list[dict], *, kind: str = "metadata", scope: str = "test"):
    from atlas.contract import ObservationBatchV1

    return ObservationBatchV1.model_validate(
        {"connection": {"source_kind": kind, "source": source, "source_scope": scope}, "observations": items}
    )
