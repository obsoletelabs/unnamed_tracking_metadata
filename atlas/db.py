from __future__ import annotations

import functools
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

_TX = "atlas_sqlite_tx"  # key in the pooled connection's `info`: the open transaction's lock state
# First keyword of a statement (after comments). SELECT and WITH run as reads, but a WITH can carry an
# INSERT/UPDATE/DELETE: `_reopen` therefore checks `total_changes` (which counts every DML row, CTE or not)
# and never COMMITs a transaction that changed rows. Known DML/DDL takes the writer lock first, transaction
# control is left alone, and anything else (PRAGMA, VACUUM, an unknown form) may write in ways
# `total_changes` does not count, so the transaction is never re-opened after it.
_READS = frozenset({"SELECT", "WITH"})
_WRITES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"})
_CONTROL = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"})
_COMMENT = re.compile(r"\s*(?:--[^\n]*(?:\n|$)|/\*.*?(?:\*/|$))", re.S)


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
    Opening the outermost savepoint before anything was written also drops the old read snapshot (as the
    legacy mode's SAVEPOINT began a fresh transaction). Inside a savepoint nothing is re-opened; the
    transaction keeps its snapshot and upgrades in place.
    `begin_snapshot()` pins one snapshot for consistent multi-table reads (backups, promotion).

    Invariant: the COMMIT that ends a read-only part (`_reopen`) never commits a write. `_reopen` refuses to
    COMMIT once the connection's `total_changes` moved since the transaction began (a CTE that ran DML), and
    a statement that is neither a read, known DML/DDL nor transaction control (PRAGMA, VACUUM, an unknown
    form) ends the read-only part for good. Either way the transaction is never re-opened again, upgrades in
    place, and rolls back as a whole."""

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
        changes = conn.connection.dbapi_connection.total_changes  # rows changed so far on this connection
        conn.info[_TX] = {"mode": "deferred", "savepoints": 0, "read": False, "changes": changes}

    @event.listens_for(engine, "commit")
    @event.listens_for(engine, "rollback")
    def _end(conn):
        conn.info.pop(_TX, None)

    @event.listens_for(engine, "savepoint")
    def _savepoint(conn, name):  # runs before SAVEPOINT is sent
        state = conn.info.get(_TX)
        if state is None:
            return
        if not state["savepoints"] and state["mode"] == "deferred" and state["read"]:
            # Nothing written yet: drop the snapshot of the earlier reads before the savepoint pins it, as
            # pysqlite's legacy mode did (its SAVEPOINT started a fresh transaction). Otherwise a write inside
            # the savepoint after another writer committed would fail at once with "database is locked".
            if _reopen(conn, state, "BEGIN"):
                state["read"] = False
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
        kind = _first_keyword(statement)
        if kind in _READS:
            if not state["savepoints"]:
                if state["read"]:  # nothing written yet: drop the old snapshot, as pysqlite's legacy mode did
                    _reopen(conn, state, "BEGIN")
                state["read"] = True
        elif kind in _WRITES:
            _take_write_lock(conn, state)
        elif kind not in _CONTROL:
            state["mode"] = "writing"  # may write: never re-open (COMMIT) this transaction again


def _first_keyword(statement: str) -> str:
    pos, end = 0, len(statement)
    while pos < end and (m := _COMMENT.match(statement, pos)) and m.end() > pos:
        pos = m.end()
    m = re.match(r"\s*([A-Za-z]+)", statement[pos:])
    return m.group(1).upper() if m else ""


def _reopen(conn: Any, state: dict[str, Any], begin: str) -> bool:
    """End the read-only transaction on the DBAPI connection and open a new one with `begin`. If `begin`
    fails (BEGIN IMMEDIATE past busy_timeout), a plain BEGIN is issued before re-raising, so the connection
    still has a real transaction for SQLAlchemy to roll back and later writes are not autocommitted.
    Returns False, doing nothing, when this transaction changed rows: the COMMIT would persist them."""
    dbapi_conn = conn.connection.dbapi_connection
    if dbapi_conn.total_changes != state["changes"]:
        state["mode"] = "writing"  # a write slipped past the classifier: keep it inside this transaction
        return False
    if dbapi_conn.in_transaction:
        dbapi_conn.execute("COMMIT")  # only reads so far (checked above): nothing to lose
    try:
        dbapi_conn.execute(begin)
    except BaseException:
        if not dbapi_conn.in_transaction:
            dbapi_conn.execute("BEGIN")
        raise
    return True


def _take_write_lock(conn: Any, state: dict[str, Any]) -> None:
    if state["savepoints"]:
        # Re-opening would discard the savepoint; the write upgrades this transaction in place instead.
        state["mode"] = "writing"
        return
    if _reopen(conn, state, "BEGIN IMMEDIATE"):
        state["mode"] = "immediate"  # only once the lock is held; on failure the transaction stays deferred
        state["read"] = False


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
    if revision == "head":
        finish_data_steps(url, settings)


def finish_data_steps(url: str, settings: Any = None) -> None:
    """Data steps a migration leaves to application code, run once the schema is current (and again at start
    until done): after migration 0032 restored kept relations, replay the merges made while downgraded; then,
    after a downgrade of 0031 or 0034, re-derive the views, explains and conflict index those downgrades removed
    (`dates_rebuild.finish_pending`; it needs the settings, so without them the next start runs it)."""
    from . import dates_rebuild, relations

    engine = make_engine(url)
    try:
        maker = make_sessionmaker(engine, settings)
        with maker() as session:
            if relations.finish_restore(session) is not None:
                session.commit()
        if settings is not None:
            dates_rebuild.finish_pending(maker, settings)
    finally:
        engine.dispose()


def downgrade(url: str, revision: str, settings: Any = None) -> None:
    from alembic import command

    command.downgrade(alembic_config(url, settings), revision)
