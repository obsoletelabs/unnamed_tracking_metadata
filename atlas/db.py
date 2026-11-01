from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

_TX = "atlas_sqlite_tx"  # key in the pooled connection's `info`: the open transaction's lock state
_WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER")
_READS = ("SELECT", "WITH")


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})
        _sqlite_transactions(engine)
        return engine
    return create_engine(url, pool_pre_ping=True)


def _sqlite_transactions(engine: Engine) -> None:
    """Real transactions on SQLite (the SQLAlchemy pysqlite recipe, "Serializable isolation / Savepoints").

    pysqlite's legacy mode begins a transaction only before INSERT/UPDATE/DELETE. A SAVEPOINT issued while
    none is open starts one, and RELEASE of that outermost savepoint commits it, so a later rollback()
    could not undo it (`atlas sync replay --dry-run` persisted writes). Here pysqlite's own handling is off
    and every SQLAlchemy transaction emits BEGIN, so savepoints always nest inside it and roll back with it.

    Until a transaction first writes it behaves as before (and like PostgreSQL's READ COMMITTED): outside a
    savepoint each read starts from a fresh snapshot, so a session sees what other sessions committed.
    The first write outside a savepoint, or `write_lock()`, ends the read-only part and re-opens the
    transaction with BEGIN IMMEDIATE, which waits (busy_timeout) for SQLite's single writer lock. Upgrading
    a stale read snapshot to a writer would instead fail at once with SQLITE_BUSY. So `write_lock()` now
    really serialises check-then-write sections on SQLite, as `pg_advisory_xact_lock` does on PostgreSQL.
    Inside a savepoint nothing is re-opened; the transaction keeps its snapshot and upgrades in place.
    `begin_snapshot()` pins one snapshot for consistent multi-table reads (backups, promotion)."""

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
        dbapi_conn.isolation_level = None  # SQLAlchemy emits BEGIN itself (below)

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")
        conn.info[_TX] = {"mode": "deferred", "savepoints": 0, "read": False}

    @event.listens_for(engine, "commit")
    @event.listens_for(engine, "rollback")
    def _end(conn):
        conn.info.pop(_TX, None)

    @event.listens_for(engine, "savepoint")
    def _savepoint(conn, name):
        state = conn.info.get(_TX)
        if state is not None:
            state["savepoints"] += 1

    @event.listens_for(engine, "release_savepoint")
    @event.listens_for(engine, "rollback_savepoint")
    def _savepoint_end(conn, name, context):
        state = conn.info.get(_TX)
        if state is not None:
            state["savepoints"] = max(0, state["savepoints"] - 1)

    @event.listens_for(engine, "before_cursor_execute")
    def _before_execute(conn, cursor, statement, parameters, context, executemany):
        state = conn.info.get(_TX)
        if state is None or state["mode"] != "deferred":
            return
        head = statement.lstrip()[:8].upper()
        if head.startswith(_WRITES):
            _take_write_lock(conn, state)
        elif head.startswith(_READS) and not state["savepoints"]:
            if state["read"]:  # nothing written yet: drop the old snapshot, as pysqlite's legacy mode did
                dbapi_conn = conn.connection.dbapi_connection
                dbapi_conn.execute("COMMIT")
                dbapi_conn.execute("BEGIN")
            state["read"] = True


def _take_write_lock(conn: Any, state: dict[str, Any]) -> None:
    if state["savepoints"]:
        # Re-opening would discard the savepoint; the write upgrades this transaction in place instead.
        state["mode"] = "writing"
        return
    dbapi_conn = conn.connection.dbapi_connection
    dbapi_conn.execute("COMMIT")  # only reads so far: nothing to lose
    state["mode"] = "immediate"
    dbapi_conn.execute("BEGIN IMMEDIATE")


def sqlite_write_lock(session: Session) -> None:
    """Hold SQLite's writer lock for the rest of this transaction (see `_sqlite_transactions`). Rows read
    before this call may be stale, as with a PostgreSQL advisory lock taken after a read; read again after it.
    A no-op on engines not made by `make_engine`, inside a savepoint, and once the transaction has written."""
    conn = session.connection()
    state = conn.info.get(_TX)
    if state is not None and state["mode"] == "deferred":
        _take_write_lock(conn, state)


def begin_snapshot(conn: Any) -> None:
    """Make every following read on this SQLite connection or session see one snapshot. With `make_engine`
    the transaction already began with BEGIN and stops refreshing its snapshot; an engine in pysqlite's
    legacy mode does not begin for SELECTs, so BEGIN is issued here."""
    if isinstance(conn, Session):
        conn = conn.connection()  # begins the session's transaction
    elif not conn.in_transaction():
        conn.begin()  # SQLAlchemy's begin first: with `make_engine` that already emits BEGIN
    state = conn.info.get(_TX)
    if state is not None and state["mode"] == "deferred":
        state["mode"] = "snapshot"
    dbapi_conn = conn.connection.dbapi_connection
    if not dbapi_conn.in_transaction:
        dbapi_conn.execute("BEGIN")


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
