from alembic import context
from sqlalchemy import create_engine

from atlas.models import Base

config = context.config
target_metadata = Base.metadata


def run() -> None:
    url = config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=url.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run()
