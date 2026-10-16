from __future__ import annotations

from sqlalchemy import inspect, text

from .main import Base, engine

CURRENT_REVISION = 5


def _ensure_column(connection, table: str, column: str, definition: str) -> None:
    columns = {item["name"] for item in inspect(connection).get_columns(table)}
    if column not in columns:
        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))


def _revision_one(connection) -> None:
    Base.metadata.create_all(bind=connection)
    _ensure_column(connection, "games", "custom_fields", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(connection, "api_tokens", "scopes", "VARCHAR(200) NOT NULL DEFAULT 'read,write,achievements,discover,images'")
    _ensure_column(connection, "users", "email_verified", "BOOLEAN NOT NULL DEFAULT 1")
    _ensure_column(connection, "game_submissions", "target_game_id", "INTEGER")


def _revision_two(connection) -> None:
    return


def _revision_three(connection) -> None:
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS api_idempotency_keys ("
        "scope_key VARCHAR(64) PRIMARY KEY, request_hash VARCHAR(64) NOT NULL, status_code INTEGER NOT NULL, "
        "content_type VARCHAR(255) NOT NULL, response_body TEXT NOT NULL, created_at TIMESTAMP NOT NULL, expires_at TIMESTAMP NOT NULL)"
    ))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_api_idempotency_expires ON api_idempotency_keys (expires_at)"))


def _revision_four(connection) -> None:
    for table_name in ("provider_mappings", "game_aliases", "metadata_observations", "canonical_fields", "game_relationships", "game_identity_redirects"):
        table = Base.metadata.tables.get(table_name)
        if table is None:
            raise RuntimeError(f"Identity migration table is not registered: {table_name}")
        table.create(bind=connection, checkfirst=True)


def _revision_five(connection) -> None:
    table = Base.metadata.tables.get("image_assets")
    if table is None:
        raise RuntimeError("Image asset migration table is not registered")
    table.create(bind=connection, checkfirst=True)


MIGRATIONS = {1: _revision_one, 2: _revision_two, 3: _revision_three, 4: _revision_four, 5: _revision_five}


def _configure_sqlite() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")


def run_migrations() -> None:
    _configure_sqlite()
    with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=5000")
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (revision INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"))
        applied = {row[0] for row in connection.execute(text("SELECT revision FROM schema_migrations ORDER BY revision"))}
        for revision in sorted(MIGRATIONS):
            if revision in applied:
                continue
            MIGRATIONS[revision](connection)
            connection.execute(
                text("INSERT INTO schema_migrations (revision, applied_at) VALUES (:revision, CURRENT_TIMESTAMP)"),
                {"revision": revision},
            )
        if CURRENT_REVISION != max(MIGRATIONS):
            raise RuntimeError("Migration registry is inconsistent")
