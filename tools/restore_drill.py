"""Create, verify and restore an authority backup into disposable databases."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from PIL import Image
from sqlalchemy import select

from atlas import accounts, auth, backup, client_changes, db, key_rotation, main, mfa, secrets_box, service, signing
from atlas.contract import ObservationBatchV1
from atlas.models import Artwork, Base, ChangeLog, NodeState, User, UserMfa, now
from atlas.settings import Settings
from atlas.signing import load_identity

from .drill_support import postgres_databases


def normalized(value):
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(type(value).__name__)


def epoch(engine):
    with engine.connect() as connection:
        return connection.execute(select(NodeState.value).where(NodeState.key == client_changes.EPOCH)).scalar()


def fingerprints(engine):
    """A hash per table. The client change log epoch is left out: every restore renews it on purpose (C3.2)."""
    result = {}
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            rows = [dict(row) for row in connection.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
            if table.name == "node_state":
                rows = [row for row in rows if row["key"] != client_changes.EPOCH]
            encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=normalized).encode()
            result[table.name] = hashlib.sha256(encoded).hexdigest()
    return result


def run(postgres_admin_url=None):
    report = {"started_at": datetime.now(UTC).isoformat(), "checks": []}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="atlas-restore-") as folder, postgres_databases(postgres_admin_url, 2) as urls:
        root = Path(folder)
        source_url = urls[0] or f"sqlite:///{root}/source.db"
        target_url = urls[1] or f"sqlite:///{root}/target.db"
        source = Settings(_env_file=None, database_url=source_url, role="authority", auto_migrate=True,
                          media_dir=str(root / "source-media"), media_cache_enabled=False,
                          initial_admin="drill-admin", email_verification="optional", secret_key=secrets.token_urlsafe(32))
        app = main.create_app(source)
        state = app.state.atlas
        engine = state.maker.kw["bind"]
        target_engine = db.make_engine(target_url)
        try:
            with state.maker() as session:
                user = accounts.register(session, source, "drill-admin", "isolated-drill-password", "admin@example.test")
                _, credential = auth.create_token(session, "drill-read", ["read"])
                _, login = accounts.create_session(session, source, user)
                factor = mfa.start(session, source, user, login.id)
                factor.enabled_at = now()
                mfa.recovery_codes(factor)
                payload = ObservationBatchV1.model_validate({"connection": {
                    "source_kind": "manual", "source": "manual", "source_scope": "restore-drill"},
                    "observations": [{"external_id": "restore-game", "title": "Restore Drill Game"}]})
                result, = service.ingest(session, state.ctx, payload, curator=True)
                image = root / "sample.png"
                Image.new("RGB", (2, 2), "#6aa8ff").save(image)
                digest = hashlib.sha256(image.read_bytes()).hexdigest()
                location = Path(source.media_dir) / digest[:2] / f"{digest}.png"
                location.parent.mkdir(parents=True)
                location.write_bytes(image.read_bytes())
                session.add(Artwork(canonical_id=result.canonical_id, source="manual", kind="key_art",
                                    url="https://example.test/synthetic.png", blob_hash=digest, content_type="image/png",
                                    size_bytes=location.stat().st_size, cached_at=now(), redistributable=True))
                key_rotation.rotate(session, state.ctx.identity.key_id)
                session.commit()
                expected_identity = load_identity(session)
                expected_secret = secrets_box.decrypt(source, factor.secret_enc)
                plain_key = base64.b64encode(expected_identity.private.private_bytes(
                    serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                    serialization.NoEncryption())).decode()
            before = fingerprints(engine)
            archive = root / "authority.tar.gz"
            manifest = backup.create(source, engine, archive)
            assert backup.verify(archive)["files"] == manifest["files"]
            checks.append("authority archive checksums and manifest verified")
            with tarfile.open(archive, "r:gz") as tar:
                dump = b"".join(tar.extractfile(m).read() for m in tar if m.isreg())
            assert plain_key.encode() not in dump and signing.ENC_PREFIX.encode() in dump
            secret = backup.secret_check(source, archive)
            assert secret["signing_key"] == "encrypted" and secret["signing_key_id"] == expected_identity.key_id
            checks.append("the archive holds the signing key only encrypted (enc:v1), and it decrypts with the secret")
            target = source.model_copy(update={"database_url": target_url, "media_dir": str(root / "restored-media")})
            db.upgrade(target_url)
            restored = backup.restore(target, target_engine, archive)
            assert fingerprints(target_engine) == before
            assert epoch(target_engine) and epoch(target_engine) != epoch(engine)
            checks.append("client change log epoch renewed, so client cursors reset")
            assert (Path(target.media_dir) / digest[:2] / f"{digest}.png").read_bytes() == image.read_bytes()
            checks.append("every table and cached artwork restored without changing data")
            with db.make_sessionmaker(target_engine)() as session:
                try:
                    load_identity(session, target.model_copy(update={"secret_key": None}))
                except secrets_box.SecretsError:
                    pass
                else:
                    raise AssertionError("an encrypted signing key loaded without ATLAS_SECRET_KEY")
            assert fingerprints(target_engine) == before
            checks.append("without ATLAS_SECRET_KEY the restored key is refused and never replaced")
            maker = db.make_sessionmaker(target_engine, target)
            with maker() as session:
                assert signing.signing_key_at_rest(session) == "encrypted"
                identity = load_identity(session)
                assert identity.node_id == expected_identity.node_id and identity.public_b64 == expected_identity.public_b64
                assert accounts.authenticate(session, "drill-admin", "isolated-drill-password") is not None
                assert auth.authenticate(session, f"Bearer {credential}").allows("read")
                factor = session.get(UserMfa, user.id)
                assert secrets_box.decrypt(target, factor.secret_enc) == expected_secret
                assert len(factor.recovery_hashes) == 10
                assert session.get(User, user.id).email == "admin@example.test"
                history, keys = key_rotation.validate_history(identity.node_id, identity.trust_public_b64,
                            key_rotation.own_history(session, identity.node_id), identity.key_id)
                for row in session.scalars(select(ChangeLog)):
                    entry = key_rotation.proof(row, identity.node_id)
                    assert key_rotation.check_entry(identity.node_id, history, keys, entry)
            checks.append("node identity, password/token authentication, encrypted MFA and signed feed remain valid")
            try:
                backup.restore(target, target_engine, archive)
            except backup.BackupError:
                pass
            else:
                raise AssertionError("restore overwrote a nonempty database without --force")
            assert fingerprints(target_engine) == before
            checks.append("a second restore refuses occupied storage without changing it")
            report.update(status="passed", source="PostgreSQL" if urls[0] else "SQLite",
                          target="PostgreSQL" if urls[1] else "SQLite", revision=manifest["revision"],
                          tables=len(restored), rows=sum(restored.values()))
        finally:
            state.http.close()
            state.providers.client.close()
            engine.dispose()
            target_engine.dispose()
    return report


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-admin-url", help="create two disposable databases on this test PostgreSQL server")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.postgres_admin_url)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main_cli()
