from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

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


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


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


def alembic_config(url: str):
    from pathlib import Path

    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def schema_state(session: Session, url: str) -> str:
    """"current" when the database is at the newest migration, "behind" when migrations are pending,
    "unknown" when it cannot be told (never raises: this feeds readiness checks)."""
    try:
        from alembic.script import ScriptDirectory
        from sqlalchemy import text

        heads = set(ScriptDirectory.from_config(alembic_config(url)).get_heads())
        applied = set(session.execute(text("SELECT version_num FROM alembic_version")).scalars())
    except Exception:
        return "unknown"
    return "current" if applied == heads else "behind"


def upgrade(url: str, revision: str = "head") -> None:
    from alembic import command

    command.upgrade(alembic_config(url), revision)
