"""Backups must restore byte-for-byte on both databases and refuse anything damaged, foreign or unsafe."""

import io
import os
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from atlas import backup, db, main, service
from atlas.models import Base, Canonical, ChangeLog

from .conftest import batch, build_settings

DIGEST = "ab" + "c" * 62


@pytest.fixture(scope="module")
def pg_url():
    url = os.environ.get("ATLAS_TEST_PG_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("no PostgreSQL available")
    srv = pgserver.get_server(tempfile.mkdtemp())
    yield srv.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    srv.cleanup()


def _wipe_pg(url):
    engine = db.make_engine(url)
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    return engine


def _populate(url, media):
    s = build_settings(database_url=url, media_dir=str(media), media_cache_enabled=False, bootstrap_admin_token="b" * 40)
    app = main.create_app(s)
    st = app.state.atlas
    with st.maker() as sess:
        a, = service.ingest(sess, st.ctx, batch("steam", [{"external_id": "1", "title": "Portal 2",
                            "release_date": "2011-04-19", "platform": "pc"}]), system=True)
        b, = service.ingest(sess, st.ctx, batch("gog", [{"external_id": "9", "title": "Totally Other Game"}]), system=True)
        service.ingest(sess, st.ctx, batch("curator", [{"external_id": "c1", "title": "Curated Game"}]), curator=True)
        sess.commit()
    (media / "ab").mkdir(parents=True, exist_ok=True)
    (media / "ab" / f"{DIGEST}.png").write_bytes(b"\x89PNG fake image bytes")
    return s, st


def _naive(v):
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(UTC).replace(tzinfo=None)
    return v


def _dump(engine):
    out = {}
    with engine.connect() as c:
        for t in Base.metadata.sorted_tables:
            pk = [col for col in t.primary_key.columns]
            rows = c.execute(select(t).order_by(*pk)).all()
            out[t.name] = [tuple(_naive(v) for v in r) for r in rows]
    return out


def _roundtrip(src_url, dst_url, tmp_path, dst_engine):
    media = tmp_path / "media"
    settings, st = _populate(src_url, media)
    engine = st.maker.kw["bind"]
    archive = tmp_path / "b.tar.gz"
    manifest = backup.create(settings, engine, archive)
    assert manifest["tables"]["canonical"] >= 2 and f"media/{DIGEST}.png" in manifest["files"]
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    assert backup.verify(archive)["revision"] == manifest["revision"]
    before = _dump(engine)
    assert before["change_log"], "the fixture must put something in the signed feed"

    db.upgrade(dst_url)
    restored_media = tmp_path / "restored-media"
    dst_settings = build_settings(database_url=dst_url, media_dir=str(restored_media))
    loaded = backup.restore(dst_settings, dst_engine, archive)
    assert loaded["canonical"] == manifest["tables"]["canonical"]
    assert _dump(dst_engine) == before  # every row, including the signed change feed, is identical
    assert (restored_media / "ab" / f"{DIGEST}.png").read_bytes() == b"\x89PNG fake image bytes"
    with pytest.raises(backup.BackupError, match="already has data"):
        backup.restore(dst_settings, dst_engine, archive)
    backup.restore(dst_settings, dst_engine, archive, force=True)
    assert _dump(dst_engine) == before
    # the feed keeps counting from where it stopped
    with dst_engine.connect() as c:
        head = c.execute(select(ChangeLog.seq).order_by(ChangeLog.seq.desc())).scalar()
    assert head == max(r[0] for r in before["change_log"])
    return archive, dst_settings


def test_sqlite_roundtrip(tmp_path):
    src, dst = f"sqlite:///{tmp_path}/a.db", f"sqlite:///{tmp_path}/b.db"
    _roundtrip(src, dst, tmp_path, db.make_engine(dst))


def test_postgres_roundtrip_and_sequences_resume(tmp_path, pg_url):
    engine = _wipe_pg(pg_url)
    src = f"sqlite:///{tmp_path}/a.db"
    archive, settings = _roundtrip(src, pg_url, tmp_path, engine)
    # a new write after the restore must not collide with restored primary keys or feed sequence numbers
    from sqlalchemy.orm import sessionmaker

    from atlas.service import Ctx
    from atlas.signing import load_identity
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with maker() as sess:
        ctx = Ctx(settings, load_identity(sess))
        [r] = service.ingest(sess, ctx, batch("steam", [{"external_id": "2", "title": "Another Game"}]), system=True)
        sess.commit()
        assert sess.get(Canonical, r.canonical_id) is not None


def test_verify_rejects_tampering_truncation_and_garbage(tmp_path):
    src = f"sqlite:///{tmp_path}/a.db"
    settings, st = _populate(src, tmp_path / "media")
    archive = tmp_path / "b.tar.gz"
    backup.create(settings, st.maker.kw["bind"], archive)

    tampered = tmp_path / "t.tar.gz"
    with tarfile.open(archive) as old, tarfile.open(tampered, "w:gz") as new:
        for m in old:
            data = old.extractfile(m).read()
            if m.name == "tables/canonical.jsonl":
                data = data.replace(b"Portal 2", b"Portal 3")
            m.size = len(data)
            new.addfile(m, io.BytesIO(data))
    with pytest.raises(backup.BackupError, match="checksum mismatch"):
        backup.verify(tampered)

    cut = tmp_path / "c.tar.gz"
    cut.write_bytes(archive.read_bytes()[:-200])
    with pytest.raises(backup.BackupError):
        backup.verify(cut)
    junk = tmp_path / "j.tar.gz"
    junk.write_bytes(b"not an archive")
    with pytest.raises(backup.BackupError):
        backup.verify(junk)


def test_unsafe_archive_members_are_refused(tmp_path):
    evil = tmp_path / "e.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../../etc/cron.d/x")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(backup.BackupError, match="unsafe|manifest|not a readable"):
        backup.verify(evil)


def test_restore_needs_the_matching_revision_and_scheduled_backups_prune(tmp_path):
    src = f"sqlite:///{tmp_path}/a.db"
    settings, st = _populate(src, tmp_path / "media")
    engine = st.maker.kw["bind"]
    archive = tmp_path / "b.tar.gz"
    backup.create(settings, engine, archive, include_media=False)
    empty = f"sqlite:///{tmp_path}/empty.db"
    e = db.make_engine(empty)  # never migrated: no revision
    with pytest.raises(backup.BackupError, match="revision"):
        backup.restore(build_settings(database_url=empty), e, archive)

    s2 = build_settings(database_url=src, backup_dir=str(tmp_path / "auto"), backup_keep=2, media_dir=str(tmp_path / "media"))  # noqa: E501
    names = set()
    for i in range(4):
        out = Path(s2.backup_dir) / f"atlas-2030010{i}-000000.tar.gz"
        backup.create(s2, engine, out)
        backup.prune(Path(s2.backup_dir), s2.backup_keep)
        names.add(out.name)
    left = sorted(p.name for p in Path(s2.backup_dir).glob("atlas-*.tar.gz"))
    assert left == ["atlas-20300102-000000.tar.gz", "atlas-20300103-000000.tar.gz"]
    assert backup.latest(s2)[0]["name"] == "atlas-20300103-000000.tar.gz"


def test_admin_can_take_a_backup_but_never_download_one(tmp_path):
    from fastapi.testclient import TestClient

    from .test_web import signup, token

    s = build_settings(database_url=f"sqlite:///{tmp_path}/w.db", media_dir=str(tmp_path / "media"), media_cache_enabled=False,  # noqa: E501
                       bootstrap_admin_token="b" * 40, public_url="http://localhost:8000", backup_dir=str(tmp_path / "bk"))
    app = main.create_app(s)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        assert c.get("/admin/backup").status_code in (303, 401, 403)
        signup(c, "alice")
        page = c.get("/admin/backup")
        assert page.status_code == 200 and "Back up now" in page.text
        assert c.post("/admin/backup", data={"csrf": token(c, "/admin/backup")}).status_code == 303
        files = list((tmp_path / "bk").glob("atlas-*.tar.gz"))
        assert len(files) == 1 and backup.verify(files[0])["tables"]["app_user"] == 1
        again = c.get("/admin/backup").text
        assert files[0].name in again and 'href="' + files[0].name not in again
        assert c.post("/admin/backup", data={"csrf": "wrong"}).status_code == 403
