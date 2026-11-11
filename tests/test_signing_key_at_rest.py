"""The node signing key is encrypted at rest with ATLAS_SECRET_KEY (CONTRACTS-v1 C9.1, D8, decision H4).

Migration 0024 runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import base64
import json
import logging
import tarfile

import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text

from atlas import backup, cli, db, feed, key_rotation, main, promotion, secrets_box, signing
from atlas.models import AuditEntry, ChangeLog, NodeState, Peer, Setting, UserMfa
from atlas.signing import ENC_PREFIX, load_identity

from .conftest import build_settings
from .test_migration_0022 import url  # noqa: F401  (sqlite + postgresql fixture)
from .test_postgres import pg_url  # noqa: F401
from .test_relay import publish, pull

SECRET = "synthetic-test-secret-one-0123456789abcdef"
OTHER = "synthetic-test-secret-two-fedcba9876543210"
ADMIN = "a" * 40


def raw_b64(identity) -> str:
    raw = identity.private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                         serialization.NoEncryption())
    return base64.b64encode(raw).decode()


def stored(engine) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT value FROM node_state WHERE key = 'signing_key'")).scalar()


def dump(engine) -> str:
    """Every row of every table, as text: what a database dump would hold."""
    out = []
    with engine.connect() as c:
        for (name,) in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")):
            out += [repr(tuple(row)) for row in c.execute(text(f'SELECT * FROM "{name}"'))]
    return "\n".join(out)


def make_node(tmp_path, name="n", secret=SECRET, **kw):
    settings = build_settings(database_url=f"sqlite:///{tmp_path}/{name}.db", secret_key=secret,
                              media_dir=str(tmp_path / f"{name}-media"), media_cache_enabled=False,
                              bootstrap_admin_token=ADMIN, **kw)
    app = main.create_app(settings)
    return app, app.state.atlas


def close(st):
    st.http.close()
    st.providers.client.close()
    st.maker.kw["bind"].dispose()


def test_new_node_stores_only_the_encrypted_key(tmp_path):
    _, st = make_node(tmp_path)
    engine = st.maker.kw["bind"]
    value = stored(engine)
    assert value.startswith(ENC_PREFIX)
    plain = raw_b64(st.ctx.identity)
    assert plain not in dump(engine)  # a DB dump without the secret holds no usable key
    with st.maker() as session:  # it loads back as the same key, through the maker's settings
        again = load_identity(session)
        assert again.public_b64 == st.ctx.identity.public_b64 and again.node_id == st.ctx.identity.node_id
    close(st)


@pytest.mark.parametrize("secret", [None, OTHER])
def test_missing_or_wrong_secret_is_a_typed_error_and_never_regenerates(tmp_path, secret):
    _, st = make_node(tmp_path)
    publish(st, "Signed Before", "before_note")
    engine = st.maker.kw["bind"]
    before = stored(engine)
    close(st)
    with db.make_sessionmaker(db.make_engine(f"sqlite:///{tmp_path}/n.db"))() as session:
        with pytest.raises(secrets_box.SecretsError, match="signing key is encrypted; ATLAS_SECRET_KEY is missing or wrong"):
            load_identity(session, build_settings(secret_key=secret))
        session.rollback()
        assert session.scalar(select(func.count()).select_from(NodeState).where(NodeState.key == "signing_key")) == 1
    assert stored(engine) == before  # the row is untouched: no new identity was generated
    with pytest.raises(secrets_box.SecretsError):  # the app refuses to start rather than serve a new identity
        main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/n.db", secret_key=secret,
                                       media_dir=str(tmp_path / "m2"), media_cache_enabled=False))
    assert stored(engine) == before


def test_legacy_plaintext_key_is_encrypted_in_place_keeping_its_identity(tmp_path, caplog):
    _, legacy = make_node(tmp_path, secret=None)
    publish(legacy, "Legacy Era", "legacy_note")
    engine = legacy.maker.kw["bind"]
    plain = stored(engine)
    assert not plain.startswith(ENC_PREFIX) and len(plain) == 44
    old = legacy.ctx.identity
    close(legacy)

    caplog.set_level(logging.INFO)
    _, st = make_node(tmp_path, secret=SECRET)
    engine = st.maker.kw["bind"]
    assert stored(engine).startswith(ENC_PREFIX)
    assert plain not in dump(engine)
    ident = st.ctx.identity
    assert (ident.node_id, ident.key_id, ident.public_b64) == (old.node_id, old.key_id, old.public_b64)
    assert raw_b64(ident) == plain  # the same private key, only its stored form changed
    assert "signing_key_encrypted" in caplog.text and plain not in caplog.text
    with st.maker() as session:
        audits = session.scalars(select(AuditEntry).where(AuditEntry.action == "signing_key_encrypted")).all()
        assert [(a.target, a.detail["key_id"]) for a in audits] == [(old.node_id, old.key_id)]
        load_identity(session)  # already encrypted: no second audit, no rewrite
        session.commit()
        assert session.scalar(select(func.count()).select_from(AuditEntry)
                              .where(AuditEntry.action == "signing_key_encrypted")) == 1
        # entries signed before and after the change verify under the same identity
        publish(st, "Encrypted Era", "encrypted_note")
        history = key_rotation.own_history(session, ident.node_id)
        _, keys = key_rotation.validate_history(ident.node_id, ident.trust_public_b64, history, ident.key_id)
        for row in session.scalars(select(ChangeLog)):
            assert key_rotation.check_entry(ident.node_id, history, keys, key_rotation.proof(row, ident.node_id))
    close(st)


def test_status_reports_the_key_at_rest_and_no_api_leaks_it(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app, st = make_node(tmp_path, secret=None)
    plain = raw_b64(st.ctx.identity)
    with TestClient(app) as client:
        status = client.get("/api/v2/status", headers={"Authorization": f"Bearer {ADMIN}"})
        assert status.json()["signing_key_at_rest"] == "plaintext"
        info = client.get("/api/v2/sync/info", headers={"Authorization": f"Bearer {ADMIN}"})
        assert info.status_code == 200
        bodies = status.text + info.text
    assert "signing_key_plaintext" in caplog.text  # warned at start
    close(st)
    app, st = make_node(tmp_path, secret=SECRET)
    with TestClient(app) as client:
        status = client.get("/api/v2/status", headers={"Authorization": f"Bearer {ADMIN}"})
        assert status.json()["signing_key_at_rest"] == "encrypted"
        bodies += status.text + client.get("/api/v2/sync/info", headers={"Authorization": f"Bearer {ADMIN}"}).text
    for secret in (plain, stored(st.maker.kw["bind"])):
        assert secret not in bodies and secret not in caplog.text
    assert plain not in repr(st.ctx.identity) and "private" not in repr(st.ctx.identity)
    close(st)


def test_production_authority_without_a_secret_warns_but_starts(caplog):
    # Decision H4: warn in this release; enforcement comes after signed updates (A10).
    settings = build_settings(environment="production", role="authority", public_url="https://atlas.example.test",
                              database_url="postgresql+psycopg://synthetic@localhost/never-connected",
                              initial_admin="admin")
    with caplog.at_level(logging.WARNING, logger="atlas"):
        main._warn_key_at_rest(settings, "plaintext")
    assert "production authority" in caplog.text and "future release will refuse" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="atlas"):
        main._warn_key_at_rest(settings, "encrypted")
    assert caplog.text == ""


@pytest.mark.parametrize("secret", [SECRET, None])
def test_rotation_writes_the_stored_form_for_the_secret(tmp_path, secret):
    _, st = make_node(tmp_path, secret=secret)
    old = st.ctx.identity
    with st.maker() as session:
        successor = key_rotation.rotate(session, old.key_id)
        session.commit()
    value = stored(st.maker.kw["bind"])
    assert value.startswith(ENC_PREFIX) is (secret is not None)
    assert raw_b64(successor) not in dump(st.maker.kw["bind"]) or secret is None
    with st.maker() as session:
        assert load_identity(session).key_id == successor.key_id != old.key_id
        publish(st, "After Rotation", "after_note")  # the feed signer decrypts the successor
        last = session.scalars(select(ChangeLog).order_by(ChangeLog.seq.desc())).first()
        assert last.key_id == successor.key_id
    close(st)


def test_rewrap_moves_every_secret_to_the_new_key(tmp_path):
    _, st = make_node(tmp_path, secret=SECRET)
    old = build_settings(secret_key=SECRET)
    with st.maker() as session:
        session.add(Peer(name="p", url="https://peer.example.test", node_id="n" * 16, public_key="k",
                         token_enc=secrets_box.encrypt(old, "peer-token")))
        session.add(Peer(name="tokenless", url="https://peer2.example.test", node_id="m" * 16, public_key="k"))
        session.add(Setting(key="integration.steam_api_key", value=secrets_box.encrypt(old, "steam-key")))
        session.commit()
    identity = st.ctx.identity
    close(st)
    rotated = build_settings(secret_key=OTHER, secret_key_previous=f" unrelated-retired-secret , {SECRET}")
    engine = db.make_engine(f"sqlite:///{tmp_path}/n.db")
    maker = db.make_sessionmaker(engine, rotated)
    with maker() as session:
        assert load_identity(session).key_id == identity.key_id  # MultiFernet: the previous secret still decrypts
        counts = secrets_box.rewrap(session, rotated)
        session.commit()
    assert counts == {"signing_key": 1, "peer_tokens": 1, "oidc_client_secrets": 0, "integration_secrets": 1,
                      "mfa_secrets": 0, "group_secrets": 0, "link_secrets": 0}
    only_new = build_settings(secret_key=OTHER)
    with db.make_sessionmaker(engine, only_new)() as session:
        assert load_identity(session).public_b64 == identity.public_b64
        peer = session.scalars(select(Peer).where(Peer.name == "p")).one()
        assert secrets_box.decrypt(only_new, peer.token_enc) == "peer-token"
        assert secrets_box.decrypt(only_new, session.get(Setting, "integration.steam_api_key").value) == "steam-key"
        assert session.scalar(select(func.count()).select_from(AuditEntry)
                              .where(AuditEntry.action == "secrets_rewrapped")) == 1
        with pytest.raises(secrets_box.SecretsError):  # the old secret alone no longer opens anything
            load_identity(session, build_settings(secret_key=SECRET))
    engine.dispose()


def test_rewrap_changes_nothing_when_a_value_does_not_decrypt(tmp_path):
    _, st = make_node(tmp_path, secret=SECRET)
    with st.maker() as session:
        session.add(Peer(name="p", url="https://peer.example.test", node_id="n" * 16, public_key="k",
                         token_enc=secrets_box.encrypt(build_settings(secret_key="a-forgotten-third-secret"), "x")))
        session.commit()
    engine = st.maker.kw["bind"]
    before = stored(engine)
    with st.maker() as session:
        with pytest.raises(secrets_box.SecretsError, match="peer .* cannot be decrypted .* nothing was changed"):
            secrets_box.rewrap(session, build_settings(secret_key=OTHER, secret_key_previous=SECRET))
        session.rollback()
    assert stored(engine) == before
    close(st)


def test_rewrap_and_secret_check_from_the_cli(tmp_path, monkeypatch, capsys):
    from atlas.settings import get_settings

    _, st = make_node(tmp_path, secret=SECRET)
    archive = tmp_path / "before.tar.gz"
    backup.create(st.ctx.settings, st.maker.kw["bind"], archive, include_media=False)
    close(st)
    monkeypatch.setenv("ATLAS_DATABASE_URL", f"sqlite:///{tmp_path}/n.db")
    monkeypatch.setenv("ATLAS_ROLE", "authority")
    monkeypatch.setenv("ATLAS_SECRET_KEY", OTHER)
    get_settings.cache_clear()
    try:
        assert cli.main(["backup", "verify", str(archive), "--secret-check"]) == 1  # wrong secret
        assert "cannot be decrypted" in capsys.readouterr().err
        assert cli.main(["secrets", "rewrap"]) == 1  # the old secret is not listed as previous
        monkeypatch.setenv("ATLAS_SECRET_KEY_PREVIOUS", SECRET)
        get_settings.cache_clear()
        assert cli.main(["backup", "verify", str(archive), "--secret-check"]) == 0
        assert "signing key encrypted" in capsys.readouterr().out
        assert cli.main(["secrets", "rewrap"]) == 0
        assert "1 signing key" in capsys.readouterr().out
        monkeypatch.delenv("ATLAS_SECRET_KEY_PREVIOUS")
        get_settings.cache_clear()
        assert cli.main(["node"]) == 0  # loads with the new secret alone
        assert st.ctx.identity.node_id in capsys.readouterr().out
        monkeypatch.delenv("ATLAS_SECRET_KEY")
        get_settings.cache_clear()
        assert cli.main(["backup", "verify", str(archive), "--secret-check"]) == 1
        assert "set ATLAS_SECRET_KEY" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()


def test_backup_restore_keeps_the_encrypted_key_and_identity(tmp_path):
    _, st = make_node(tmp_path, secret=SECRET)
    publish(st, "Backed Up", "backed_note")
    archive = tmp_path / "a.tar.gz"
    backup.create(st.ctx.settings, st.maker.kw["bind"], archive, include_media=False)
    identity = st.ctx.identity
    close(st)
    with tarfile.open(archive) as tar:
        rows = [json.loads(line) for line in tar.extractfile("tables/node_state.jsonl")]
        everything = b"".join(tar.extractfile(m).read() for m in tar if m.isreg())
    [key] = [r["value"] for r in rows if r["key"] == "signing_key"]
    assert key.startswith(ENC_PREFIX) and raw_b64(identity).encode() not in everything
    assert backup.secret_check(build_settings(secret_key=SECRET), archive) == {
        "signing_key": "encrypted", "secrets": 0, "signing_key_id": identity.key_id}
    with pytest.raises(backup.BackupError, match="cannot be decrypted"):
        backup.secret_check(build_settings(secret_key=OTHER), archive)
    target_url = f"sqlite:///{tmp_path}/restored.db"
    target = build_settings(database_url=target_url, secret_key=SECRET, media_dir=str(tmp_path / "r"))
    db.upgrade(target_url, settings=target)
    engine = db.make_engine(target_url)
    backup.restore(target, engine, archive)
    assert stored(engine) == key
    with db.make_sessionmaker(engine, target)() as session:
        restored = load_identity(session)
        assert (restored.node_id, restored.key_id, raw_b64(restored)) == (identity.node_id, identity.key_id,
                                                                         raw_b64(identity))
    engine.dispose()


def test_restore_refuses_an_encrypted_key_it_cannot_decrypt_before_replacing_data(tmp_path):
    _, st = make_node(tmp_path, secret=SECRET)
    archive = tmp_path / "a.tar.gz"
    backup.create(st.ctx.settings, st.maker.kw["bind"], archive, include_media=False)
    close(st)
    # An occupied target with its own (different) encrypted identity.
    _, target_st = make_node(tmp_path, name="target", secret=OTHER)
    engine = target_st.maker.kw["bind"]
    before = stored(engine)
    for secret in (None, OTHER):
        settings = target_st.ctx.settings.model_copy(update={"secret_key": secret, "secret_key_previous": None})
        with pytest.raises(backup.BackupError, match="cannot decrypt it.*Nothing was changed"):
            backup.restore(settings, engine, archive, force=True)
        assert stored(engine) == before
    # With the source secret kept as a previous secret, the restore goes ahead.
    settings = target_st.ctx.settings.model_copy(update={"secret_key": OTHER, "secret_key_previous": SECRET})
    backup.restore(settings, engine, archive, force=True)
    assert stored(engine) != before and stored(engine).startswith(ENC_PREFIX)
    close(target_st)


def test_a_plaintext_backup_is_reported_and_encrypted_after_restore(tmp_path):
    _, st = make_node(tmp_path, secret=None)
    archive = tmp_path / "legacy.tar.gz"
    backup.create(st.ctx.settings, st.maker.kw["bind"], archive, include_media=False)
    identity = st.ctx.identity
    close(st)
    assert backup.secret_check(build_settings(secret_key=SECRET), archive)["signing_key"] == "plaintext"
    target_url = f"sqlite:///{tmp_path}/restored.db"
    target = build_settings(database_url=target_url, secret_key=SECRET, media_dir=str(tmp_path / "r"))
    db.upgrade(target_url, settings=target)
    engine = db.make_engine(target_url)
    backup.restore(target, engine, archive)
    with db.make_sessionmaker(engine, target)() as session:
        assert load_identity(session).key_id == identity.key_id
        session.commit()
    assert stored(engine).startswith(ENC_PREFIX)
    engine.dispose()


def test_promotion_candidate_key_is_encrypted_under_the_operator_secret(tmp_path):
    from .test_relay import node

    a = node(tmp_path, "a", "authority")
    publish(a[2], "Recovered", "recovered_note")
    mirror_settings = build_settings(database_url=f"sqlite:///{tmp_path}/m.db", role="mirror", secret_key=SECRET,
                                     media_dir=str(tmp_path / "m"), media_cache_enabled=False)
    mirror = main.create_app(mirror_settings).state.atlas
    pull(mirror, a)
    out = tmp_path / "candidate.tar.gz"
    result = promotion.candidate(mirror.ctx.settings, mirror.maker.kw["bind"], a[2].ctx.identity.node_id,
                                 mirror.ctx.identity.node_id, out)
    with tarfile.open(out) as tar:
        rows = [json.loads(line) for line in tar.extractfile("tables/node_state.jsonl")]
    [key] = [r["value"] for r in rows if r["key"] == "signing_key"]
    assert key.startswith(ENC_PREFIX)
    assert backup.secret_check(mirror_settings, out)["signing_key"] == "encrypted"
    private = signing.unseal(mirror_settings, key)
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert signing.key_id_for(public) == result["node_id"]
    close(mirror)


def test_feed_append_signs_with_an_encrypted_key_through_session_info(tmp_path):
    _, st = make_node(tmp_path, secret=SECRET)
    with st.maker() as session:
        assert session.info["atlas_settings"] is st.ctx.settings
    publish(st, "Signed", "signed_note")  # feed.append loads the signer from the session alone
    with st.maker() as session:
        rows = session.scalars(select(ChangeLog)).all()
        assert rows and all(signing.verify(st.ctx.identity.public_b64, st.ctx.identity.node_id, r.seq, r.kind,
                                           r.body, r.body_hash, r.signature) for r in rows)
        assert feed.head_seq(session) == len(rows)
    close(st)


# Migration 0024 ----------------------------------------------------------------------------------------------


def legacy_db(url):  # noqa: F811
    """A database at 0023 holding a plaintext key, as the previous release left it."""
    db.upgrade(url, "0023")
    engine = create_engine(url)
    with db.make_sessionmaker(engine)() as session:
        identity = load_identity(session)  # no settings: the legacy write path
        session.commit()
    assert not stored(engine).startswith(ENC_PREFIX)
    return engine, identity


def test_migration_0024_encrypts_and_downgrade_decrypts(url):  # noqa: F811
    engine, identity = legacy_db(url)
    plain = stored(engine)
    settings = build_settings(database_url=url, secret_key=SECRET)
    db.upgrade(url, "0024", settings=settings)  # this revision exactly; later ones (0028) do not touch the key
    value = stored(engine)
    assert value.startswith(ENC_PREFIX) and plain not in value
    with db.make_sessionmaker(engine, settings)() as session:
        again = load_identity(session)
        assert (again.node_id, again.key_id, raw_b64(again)) == (identity.node_id, identity.key_id, plain)
        session.commit()
    db.upgrade(url, "0024", settings=settings)  # idempotent once applied

    with pytest.raises(RuntimeError, match="set ATLAS_SECRET_KEY"):  # no secret: refused, nothing changed
        db.downgrade(url, "0023", settings=build_settings(database_url=url, secret_key=None))
    with pytest.raises(RuntimeError, match="cannot be decrypted"):  # wrong secret: refused, nothing changed
        db.downgrade(url, "0023", settings=build_settings(database_url=url, secret_key=OTHER))
    assert stored(engine) == value
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0024"

    db.downgrade(url, "0023", settings=build_settings(database_url=url, secret_key=OTHER,
                                                      secret_key_previous=SECRET))
    assert stored(engine) == plain  # exactly the value the previous release wrote
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0023"
    engine.dispose()


def test_migration_0024_without_a_secret_leaves_the_key_for_the_app(url, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ATLAS_SECRET_KEY", raising=False)
    engine, identity = legacy_db(url)
    plain = stored(engine)
    db.upgrade(url)  # no settings, no environment secret: a no-op
    assert stored(engine) == plain
    db.downgrade(url, "0023")  # a plaintext key downgrades without a secret
    assert stored(engine) == plain
    monkeypatch.setenv("ATLAS_SECRET_KEY", SECRET)  # the plain `alembic upgrade` path reads the environment
    db.upgrade(url)
    assert stored(engine).startswith(ENC_PREFIX)
    with db.make_sessionmaker(engine, build_settings(database_url=url, secret_key=SECRET))() as session:
        assert raw_b64(load_identity(session)) == plain
    engine.dispose()


def test_migration_0024_on_an_empty_database(url):  # noqa: F811
    db.upgrade(url, settings=build_settings(database_url=url, secret_key=SECRET))
    db.downgrade(url, "0023", settings=build_settings(database_url=url, secret_key=None))
    engine = create_engine(url)
    assert stored(engine) is None
    engine.dispose()


def test_mfa_secret_is_rewrapped(tmp_path):
    from atlas import accounts, mfa

    _, st = make_node(tmp_path, secret=SECRET, initial_admin="admin")
    with st.maker() as session:
        user = accounts.register(session, st.ctx.settings, "admin", "synthetic-password-1", "a@example.test")
        _, login = accounts.create_session(session, st.ctx.settings, user)
        factor = mfa.start(session, st.ctx.settings, user, login.id)
        expected = secrets_box.decrypt(st.ctx.settings, factor.secret_enc)
        session.commit()
        rotated = build_settings(secret_key=OTHER, secret_key_previous=SECRET)
        assert secrets_box.rewrap(session, rotated)["mfa_secrets"] == 1
        session.commit()
        assert secrets_box.decrypt(build_settings(secret_key=OTHER), session.get(UserMfa, user.id).secret_enc) == expected
    close(st)
