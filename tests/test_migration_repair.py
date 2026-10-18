from sqlalchemy import inspect

from app.main import Base, engine
from app.migrations import run_migrations


def test_recorded_migrations_with_missing_tables_are_replayed():
    run_migrations()
    Base.metadata.drop_all(engine)  # keeps schema_migrations, as a partial restore would
    run_migrations()
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    run_migrations()  # and an intact schema is left alone


def test_production_app_seeds_default_providers_after_migrating():
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.asgi import app
    from app.main import MetadataProvider, SchemaDefinition, SessionLocal

    Base.metadata.drop_all(engine)
    with TestClient(app):
        with SessionLocal() as db:
            slugs = set(db.scalars(select(MetadataProvider.slug)))
            assert {"steam", "igdb", "rawg"} <= slugs
            assert db.scalar(select(SchemaDefinition).where(SchemaDefinition.name == "game"))
