"""Resources for disposable release drills; never reuse a deployment database."""

from __future__ import annotations

import secrets
from contextlib import contextmanager

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


@contextmanager
def postgres_databases(admin_url: str | None, count: int):
    """Create and drop only uniquely named databases created by this invocation."""
    if admin_url is None:
        yield [None] * count
        return
    parsed = make_url(admin_url).set(drivername="postgresql+psycopg")
    connect_url = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    prefix = "atlas_drill_" + secrets.token_hex(6)
    created = []
    try:
        with psycopg.connect(connect_url, autocommit=True) as connection:
            for index in range(count):
                name = f"{prefix}_{index}"
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                created.append(name)
        yield [parsed.set(database=name).render_as_string(hide_password=False) for name in created]
    finally:
        with psycopg.connect(connect_url, autocommit=True) as connection:
            for name in created:
                if not name.startswith(prefix + "_"):
                    raise RuntimeError("refusing to drop a database outside this drill")
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
