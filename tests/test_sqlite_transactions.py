"""SQLite transaction handling in `atlas.db.make_engine` (PHASE2 defects D1 and D2).

Every assertion reads the database file back through a fresh session, so it checks what was persisted,
not what one session believes."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select, text

from atlas import db, service, sync
from atlas.models import Base, NodeState


@pytest.fixture
def maker(tmp_path):
    engine = db.make_engine(f"sqlite:///{tmp_path}/tx.db")
    Base.metadata.create_all(engine)
    yield db.make_sessionmaker(engine)
    engine.dispose()


def _value(maker, key):
    with maker() as session:
        row = session.get(NodeState, key)
        return row.value if row else None


def test_a_released_savepoint_is_undone_by_the_outer_rollback(maker):
    with maker() as session:
        session.get(NodeState, "probe")  # a read first, as `sync replay` does before its first entry
        with session.begin_nested():
            session.add(NodeState(key="probe", value="inside the savepoint"))
            session.flush()
        session.rollback()
    assert _value(maker, "probe") is None


def test_a_savepoint_as_the_first_statement_is_undone_too(maker):
    with maker() as session:
        with session.begin_nested():
            session.add(NodeState(key="probe", value="inside the savepoint"))
        session.rollback()
    assert _value(maker, "probe") is None


def test_a_rolled_back_savepoint_keeps_the_rest_of_the_transaction(maker):
    with maker() as session:
        session.add(NodeState(key="kept", value="1"))
        session.flush()
        with pytest.raises(RuntimeError), session.begin_nested():
            session.add(NodeState(key="dropped", value="1"))
            session.flush()
            raise RuntimeError("entry failed")
        session.commit()
    assert _value(maker, "kept") == "1" and _value(maker, "dropped") is None


def test_reads_then_a_write_after_another_writer_committed_still_succeed(maker):
    """A deferred BEGIN pins a read snapshot; upgrading it to a writer after someone else committed fails
    with SQLITE_BUSY at once. The first write re-opens the transaction as BEGIN IMMEDIATE instead."""
    with maker() as session:
        session.add(NodeState(key="seen", value="0"))
        session.commit()
    with maker() as reader, maker() as other:
        assert reader.scalar(select(func.count()).select_from(NodeState)) == 1
        other.add(NodeState(key="other", value="1"))
        other.commit()
        reader.add(NodeState(key="reader", value="1"))
        reader.commit()
    assert _value(maker, "other") == "1" and _value(maker, "reader") == "1"


def test_reads_before_the_first_write_see_other_commits_unless_a_snapshot_is_pinned(maker):
    """Code written against pysqlite's legacy mode expects a long session to see other sessions' commits
    (as PostgreSQL's READ COMMITTED does); backups and promotion pin one snapshot instead."""
    with maker() as session, maker() as pinned:
        db.begin_snapshot(pinned)
        assert session.scalar(select(func.count()).select_from(NodeState)) == 0
        assert pinned.scalar(select(func.count()).select_from(NodeState)) == 0
        with maker() as other:
            other.add(NodeState(key="committed", value="1"))
            other.commit()
        assert session.scalar(select(func.count()).select_from(NodeState)) == 1
        assert pinned.scalar(select(func.count()).select_from(NodeState)) == 0


def test_write_lock_serialises_check_then_write_sections(maker):
    """Two sessions read a counter, wait for each other, then write. Without a writer lock both read 0 and
    the result is 1 (a lost update); with `service.write_lock` the second waits and the result is 2."""
    with maker() as session:
        session.add(NodeState(key="counter", value="0"))
        session.commit()
    both_read = threading.Barrier(2, timeout=1)

    def bump(_):
        with maker() as session:
            service.write_lock(session)
            value = int(session.get(NodeState, "counter").value)
            try:
                both_read.wait()  # without the lock both sessions get here; with it, this times out
            except threading.BrokenBarrierError:
                pass
            session.get(NodeState, "counter").value = str(value + 1)
            session.commit()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(bump, range(2)))
    assert _value(maker, "counter") == "2"


def test_concurrent_downloaded_markers_do_not_collide(maker):
    """D2: `_note_downloaded` reads the `sync_downloaded:<node>` row and inserts it when missing. Two pulls of
    the same upstream both saw no row and both inserted it: UNIQUE constraint failed: node_state.key."""
    both_read = threading.Barrier(2, timeout=1)
    real = sync._int_state

    def int_state(session, key):
        value = real(session, key)
        if key.startswith("sync_downloaded:"):
            try:
                both_read.wait()  # force the interleaving the CI run hit by chance
            except threading.BrokenBarrierError:
                pass
        return value

    sync._int_state = int_state
    try:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda seq: sync._note_downloaded(maker, "upstream", seq), (5, 7)))
    finally:
        sync._int_state = real
    assert _value(maker, "sync_downloaded:upstream") == "7"


def test_write_lock_inside_a_savepoint_keeps_the_savepoint(maker):
    with maker() as session:
        session.get(NodeState, "x")
        with session.begin_nested():
            service.write_lock(session)  # cannot re-open the transaction here; must not break the savepoint
            session.add(NodeState(key="x", value="1"))
        session.commit()
    assert _value(maker, "x") == "1"


def test_a_write_in_a_savepoint_after_reads_and_another_commit_succeeds(maker):
    """Legacy pysqlite began a fresh transaction at SAVEPOINT; the recipe drops the earlier read snapshot when
    the outermost savepoint opens, so the write inside it waits for the writer lock instead of failing."""
    with maker() as session, maker() as other:
        assert session.get(NodeState, "mine") is None  # pins a read snapshot
        other.add(NodeState(key="other", value="1"))
        other.commit()
        with session.begin_nested():
            session.add(NodeState(key="mine", value="1"))
            session.flush()
        session.commit()
    assert _value(maker, "other") == "1" and _value(maker, "mine") == "1"


def test_a_failed_write_lock_leaves_a_transaction_that_still_rolls_back(maker):
    """BEGIN IMMEDIATE past busy_timeout raises; the session must still have a real transaction afterwards,
    or its later writes would be autocommitted and survive the rollback."""
    with maker() as holder, maker() as session:
        service.write_lock(holder)
        holder.add(NodeState(key="holder", value="1"))
        holder.flush()  # holds SQLite's writer lock
        session.connection().connection.dbapi_connection.execute("PRAGMA busy_timeout=50")  # connection setting
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            service.write_lock(session)
        assert session.connection().connection.dbapi_connection.in_transaction
        holder.rollback()  # the lock is free again
        session.add(NodeState(key="probe", value="must not persist"))
        session.flush()  # takes the writer lock now (BEGIN IMMEDIATE), inside the session's transaction
        session.rollback()
        session.connection().connection.dbapi_connection.execute("PRAGMA busy_timeout=5000")  # make_engine's
    assert _value(maker, "probe") is None and _value(maker, "holder") is None
    with maker() as session:  # the connection is usable for a normal write afterwards
        service.write_lock(session)
        session.add(NodeState(key="after", value="1"))
        session.commit()
    assert _value(maker, "after") == "1"


def test_snapshot_helper_works_with_and_without_the_recipe(tmp_path):
    legacy = create_engine(f"sqlite:///{tmp_path}/legacy.db")  # pysqlite legacy mode, as before this change
    with legacy.connect() as conn:
        db.begin_snapshot(conn)
        assert conn.connection.dbapi_connection.in_transaction
        conn.execute(text("SELECT 1"))
    legacy.dispose()
    engine = db.make_engine(f"sqlite:///{tmp_path}/recipe.db")
    with engine.connect() as conn:
        db.begin_snapshot(conn)  # as `backup.create` calls it: first, on a fresh connection
        assert conn.connection.dbapi_connection.in_transaction
        assert conn.execute(text("SELECT 1")).scalar() == 1  # no second BEGIN from SQLAlchemy's autobegin
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        db.begin_snapshot(conn)  # already inside BEGIN: must not issue a second one
        assert conn.connection.dbapi_connection.in_transaction
    maker = db.make_sessionmaker(engine)
    with maker() as session:
        db.begin_snapshot(session)  # as `promotion.candidate` calls it
        assert session.execute(text("SELECT 1")).scalar() == 1
    engine.dispose()


def _hooks(engine):
    names = ("begin", "commit", "rollback", "savepoint", "release_savepoint", "rollback_savepoint",
             "before_cursor_execute")
    return [len(getattr(engine.dispatch, n)) for n in names] + [len(engine.pool.dispatch.connect)]


def test_postgresql_engines_get_none_of_the_sqlite_hooks():
    url = "postgresql+psycopg://atlas:unused@127.0.0.1:1/atlas"  # never connected to
    engine, plain = db.make_engine(url), create_engine(url, pool_pre_ping=True)
    assert _hooks(engine) == _hooks(plain)
    sqlite, plain_sqlite = db.make_engine("sqlite://"), create_engine("sqlite://")
    assert _hooks(sqlite) != _hooks(plain_sqlite)  # the comparison does see the hooks when they are there
    for e in (engine, plain, sqlite, plain_sqlite):
        e.dispose()


_WRITE_FORMS = {
    # olie-ob's review of #108: a CTE can introduce DML, so a `WITH` statement is not a read
    "cte-update": "WITH x AS (SELECT 'probe') UPDATE node_state SET value = 'leaked' WHERE key IN (SELECT * FROM x)",
    "cte-insert": "WITH x AS (SELECT 'leak', 'leaked') INSERT INTO node_state (key, value) SELECT * FROM x",
    "cte-delete": "WITH x AS (SELECT 'probe') DELETE FROM node_state WHERE key IN (SELECT * FROM x)",
    # statements whose first word is hidden behind a comment are not classified either
    "comment-update": "/* note */ UPDATE node_state SET value = 'leaked' WHERE key = 'probe'",
    "line-comment-insert": "-- note\nINSERT INTO node_state (key, value) VALUES ('leak', 'leaked')",
}


@pytest.mark.parametrize("sql", _WRITE_FORMS.values(), ids=_WRITE_FORMS.keys())
def test_no_statement_form_is_committed_by_a_later_read(maker, sql):
    """The read-refresh path ends the read-only part of a transaction with COMMIT. A write the classifier
    took for a read (or did not recognise) must never be in a transaction it commits: the later rollback
    has to undo it. Reproduces the review's form: CTE UPDATE, then a SELECT, then rollback()."""
    with maker() as session:
        session.add(NodeState(key="probe", value="original"))
        session.commit()
    with maker() as session:
        session.execute(text("SELECT * FROM node_state")).all()
        session.execute(text(sql))
        session.execute(text("SELECT * FROM node_state")).all()  # the refresh that used to COMMIT the write
        session.rollback()
    with maker() as session:
        rows = {r.key: r.value for r in session.scalars(select(NodeState))}
    assert rows == {"probe": "original"}


def test_a_pragma_in_a_transaction_is_never_committed_by_a_later_read(maker):
    """`total_changes` does not count PRAGMA writes (or DDL behind an unrecognised form), so an unclassified
    statement ends the read-only part: no later read may COMMIT it."""
    with maker() as session:
        session.execute(text("SELECT 1"))
        session.execute(text("PRAGMA user_version = 7"))
        session.execute(text("SELECT 1"))
        session.rollback()
    with maker() as session:
        assert session.execute(text("PRAGMA user_version")).scalar() == 0


def test_a_read_only_cte_still_reads_and_the_transaction_can_write_after_it(maker):
    """A read-only CTE behaves like a SELECT: it takes no writer lock and later writes still wait for one."""
    with maker() as session, maker() as other:
        assert session.execute(text("WITH x AS (SELECT 1 AS n) SELECT n FROM x")).scalar() == 1
        other.add(NodeState(key="other", value="1"))
        other.commit()
        session.add(NodeState(key="mine", value="1"))
        session.commit()
    assert _value(maker, "other") == "1" and _value(maker, "mine") == "1"
