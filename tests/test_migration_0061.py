"""Migration 0061 (CONTRACT-C15 §2, A21; T-B17): `relation.position`, the `book_creator` table, the upgrade of a node
that already has a media type `book` (labels and custom fields kept, achievements off, a notice recorded), and the
type-aware title key recomputed for existing book records. Downgrade and upgrade again.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import custom, db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

PREVIOUS, HEAD = "0060", "0061"
NOW = "2026-11-10 10:00:00"


@pytest.fixture(params=["sqlite", "postgresql"])
def url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path}/m.db"
    pg = request.getfixturevalue("pg_url")
    engine = create_engine(pg)
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    engine.dispose()
    return pg


def version(engine) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def record(c, cid: str, media_type: str, title: str, norm: str, view: dict | None = None) -> None:
    c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, updated_at) "
                   "VALUES (:i, :m, :n, :v, '{}', 1, :t, :t)"),
              {"i": cid, "m": media_type, "n": norm, "v": json.dumps({"title": title, **(view or {})}), "t": NOW})


@pytest.mark.parametrize("origin", ["local", "upstream"])
def test_t_b17_existing_book_type_is_adopted_and_book_keys_recomputed(url, origin):
    db.upgrade(url, PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO media_type (key, label, enabled, position, labels, origin, achievements) "
                           "VALUES ('book', 'Novels', :e, 9, :l, :o, :e)"),
                      {"e": True, "l": json.dumps({"developer": "Writer"}), "o": origin})
            for n, (key, label) in enumerate((("authors", "Authors (old)"), ("shelf", "Shelf"))):
                c.execute(text("INSERT INTO field_def (id, media_type, origin, key, label, kind, choices, help, public, "
                               "editable, position) VALUES (:i, 'book', :o, :k, :lab, 'text', '[]', '', :t, :t, 1)"),
                          {"i": f"01J0000000000000000000000{n}", "o": origin, "k": key, "lab": label, "t": True})
            # the old key stripped "Special Edition"; a game keeps the old rule
            record(c, "atlas:b1", "book", "A Special Edition", "a",
                   {"authors": [{"name": "Ione Märsh"}, {"name": "Teo Varga", "role": "translator"}]})
            record(c, "atlas:g1", "game", "Hades: Special Edition", "hades")
            c.execute(text("INSERT INTO media_type (key, label, enabled, position, labels, origin, achievements) "
                           "VALUES ('anime', 'Anime', :e, 1, '{}', 'local', :e)"), {"e": True})
        db.upgrade(url, HEAD)
        assert version(engine) == HEAD
        with engine.connect() as c:
            row = c.execute(text("SELECT label, position, labels, origin, achievements FROM media_type "
                                 "WHERE key = 'book'")).one()
            assert (row.label, row.position, row.origin, bool(row.achievements)) == ("Novels", 9, origin, False)
            labels = row.labels if isinstance(row.labels, dict) else json.loads(row.labels)
            assert labels == {"developer": "Writer"}
            assert bool(c.execute(text("SELECT achievements FROM media_type WHERE key = 'anime'")).scalar())
            assert sorted(c.execute(text("SELECT key FROM field_def WHERE media_type = 'book'")).scalars()) == \
                ["authors", "shelf"]
            detail = c.execute(text("SELECT detail FROM audit_entry WHERE action = 'media_type_builtin'")).scalar()
            detail = detail if isinstance(detail, dict) else json.loads(detail)
            assert detail == {"key": "book", "label": "Novels", "origin": origin, "fields": 2, "shadowed": ["authors"]}
            notice = c.execute(text("SELECT value FROM setting WHERE key = :k"), {"k": custom.ADOPTED_NOTICE}).scalar()
            assert (notice if isinstance(notice, list) else json.loads(notice)) == [detail]
            keys = dict(c.execute(text("SELECT id, norm_title FROM canonical")).all())
            assert keys == {"atlas:b1": "a special edition", "atlas:g1": "hades"}
            creators = c.execute(text("SELECT position, name, norm_name, role FROM book_creator "
                                      "ORDER BY position")).all()
            assert [tuple(r) for r in creators] == [(0, "Ione Märsh", "ione marsh", "author"),
                                                    (1, "Teo Varga", "teo varga", "translator")]
        assert "position" in {col["name"] for col in inspect(engine).get_columns("relation")}
        db.downgrade(url, PREVIOUS)
        assert version(engine) == PREVIOUS
        assert "position" not in {col["name"] for col in inspect(engine).get_columns("relation")}
        assert "book_creator" not in inspect(engine).get_table_names()
        db.upgrade(url, HEAD)
        with engine.connect() as c:
            notice = c.execute(text("SELECT value FROM setting WHERE key = :k"), {"k": custom.ADOPTED_NOTICE}).scalar()
            assert len(notice if isinstance(notice, list) else json.loads(notice)) == 1  # one notice per type
    finally:
        engine.dispose()


def test_a_node_without_a_book_type_records_no_notice(url):
    db.upgrade(url, HEAD)
    engine = create_engine(url)
    try:
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM setting WHERE key = :k"),
                             {"k": custom.ADOPTED_NOTICE}).scalar() == 0
            assert c.execute(text("SELECT count(*) FROM audit_entry WHERE action = 'media_type_builtin'")).scalar() == 0
    finally:
        engine.dispose()


def test_admin_media_types_page_shows_the_notice(tmp_path):
    from fastapi.testclient import TestClient

    from atlas.models import Setting

    from .test_sync_compat import Node
    from .test_web import signup

    node = Node(tmp_path, "n", "authority")
    with node.st.maker() as s:
        s.add(Setting(key=custom.ADOPTED_NOTICE, value=[{"key": "book", "label": "Novels", "origin": "local",
                                                          "fields": 2, "shadowed": ["authors"]}]))
        s.commit()
    web = TestClient(node.app, base_url="http://localhost:8000", follow_redirects=False)
    signup(web, "admin")
    page = web.get("/admin/schema").text
    assert "Media types that became built-in" in page and "<strong>Novels</strong> <code>book</code>" in page
    assert "2 custom fields kept" in page and "<code>authors</code>" in page
