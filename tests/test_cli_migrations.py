import sqlite3

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select

from atlas import cli, db
from atlas.legacy_import import import_legacy
from atlas.models import Base, Canonical, Crosswalk
from atlas.service import Ctx
from atlas.signing import load_identity

from .conftest import build_settings


def test_migrations_match_models(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    db.upgrade(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff


def test_cli_token_and_node(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ATLAS_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    monkeypatch.setenv("ATLAS_ROLE", "authority")
    from atlas.settings import get_settings
    get_settings.cache_clear()
    assert cli.main(["migrate"]) == 0
    assert cli.main(["token", "--name", "x", "--scopes", "read,contribute"]) == 0
    out = capsys.readouterr().out
    assert '"token": "atl_' in out
    assert cli.main(["node"]) == 0
    assert "ATLAS_TRUSTED_KEYS" in capsys.readouterr().out
    get_settings.cache_clear()


def test_import_legacy(tmp_path, session):
    legacy = tmp_path / "legacy.db"
    c = sqlite3.connect(legacy)
    c.executescript("""
      CREATE TABLE games (id INTEGER PRIMARY KEY, canonical_id TEXT, title TEXT, developer TEXT, description TEXT);
      CREATE TABLE metadata_providers (id INTEGER PRIMARY KEY, slug TEXT);
      CREATE TABLE game_external_ids (id INTEGER PRIMARY KEY, game_id INT, provider_id INT, external_id TEXT);
      CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
      CREATE TABLE game_tags (id INTEGER PRIMARY KEY, game_id INT, tag_id INT);
      INSERT INTO games VALUES (1,'gm_a','Hades','Supergiant','Roguelike'),(2,'gm_b','','x','');
      INSERT INTO metadata_providers VALUES (1,'steam'),(2,'Bad Slug!');
      INSERT INTO game_external_ids VALUES (1,1,1,'1145360'),(2,1,2,'zzz');
      INSERT INTO tags VALUES (1,'action');
      INSERT INTO game_tags VALUES (1,1,1);
    """)
    c.commit()
    c.close()
    ctx = Ctx(build_settings(), load_identity(session))
    stats = import_legacy(session, ctx, str(legacy))
    session.commit()
    assert stats == {"games": 2, "created": 1, "updated": 0, "skipped": 1}
    row = session.scalars(select(Canonical)).one()
    assert row.view["title"] == "Hades" and row.view["tags"] == ["action"]
    assert session.get(Crosswalk, ("steam", "1145360")).canonical_id == row.id
    # re-running is idempotent
    assert import_legacy(session, ctx, str(legacy))["created"] == 0
