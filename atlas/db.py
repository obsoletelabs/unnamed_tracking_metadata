from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: Engine, settings: Any = None) -> sessionmaker[Session]:
    """`settings` travels in `session.info["atlas_settings"]`, so code that only has a session (the feed signer)
    can still decrypt the node signing key (CONTRACTS-v1 C9.1)."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, info={"atlas_settings": settings})


@contextmanager
def session_scope(maker: sessionmaker[Session]) -> Iterator[Session]:
    session = maker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def alembic_config(url: str, settings: Any = None):
    """With `settings`, data migrations that need ATLAS_SECRET_KEY (0024) use its secrets; without, they read
    ATLAS_SECRET_KEY and ATLAS_SECRET_KEY_PREVIOUS from the environment."""
    from pathlib import Path

    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    if settings is not None:
        cfg.attributes["atlas_secrets"] = (settings.secret_key, settings.secret_key_previous)
    return cfg


@functools.lru_cache(maxsize=8)
def _script_heads(url: str) -> frozenset[str]:
    """The newest revisions shipped in this build. They cannot change while the process runs, and parsing the
    migration scripts on every readiness probe or scrape is wasted work."""
    from alembic.script import ScriptDirectory

    return frozenset(ScriptDirectory.from_config(alembic_config(url)).get_heads())


def schema_state(session: Session, url: str) -> str:
    """"current" when the database is at the newest migration, "behind" when migrations are pending,
    "unknown" when it cannot be told (never raises: this feeds readiness checks)."""
    try:
        from sqlalchemy import text

        heads = set(_script_heads(url))
        applied = set(session.execute(text("SELECT version_num FROM alembic_version")).scalars())
    except Exception:
        return "unknown"
    return "current" if applied == heads else "behind"


def upgrade(url: str, revision: str = "head", settings: Any = None) -> None:
    from alembic import command

    command.upgrade(alembic_config(url, settings), revision)


def downgrade(url: str, revision: str, settings: Any = None) -> None:
    from alembic import command

    command.downgrade(alembic_config(url, settings), revision)
