import json
import tarfile

import pytest
from sqlalchemy import delete, select

from atlas import accounts, auth, backup, db, key_rotation, promotion, service
from atlas.models import Canonical, FieldDef, NodeState, ReplicatedChange, User
from atlas.settings import Settings
from atlas.signing import load_identity

from .conftest import batch
from .test_key_rotation import rotate
from .test_relay import node, publish, pull


def archive_rows(path, table):
    with tarfile.open(path) as archive:
        return [json.loads(line) for line in archive.extractfile(f"tables/{table}.jsonl")]


def test_recovery_uses_only_one_verified_public_feed_and_a_new_identity(tmp_path):
    a, b, mirror = node(tmp_path, "a", "authority"), node(tmp_path, "b", "authority"), node(tmp_path, "m", "mirror")
    aid = publish(a[2], "Recovered Alpha", "alpha_note")
    rotate(a[2])
    publish(a[2], "After Rotation", "after_note")
    bid = publish(b[2], "Excluded Beta", "beta_note")
    pull(mirror[2], a)
    pull(mirror[2], b)
    with mirror[2].maker() as session:
        user = accounts.register(session, mirror[2].ctx.settings, "local-user", "local-test-password", "u@example.test")
        auth.create_token(session, "private-token", ["curate"])
        accounts.create_session(session, mirror[2].ctx.settings, user)
        local, = service.ingest(session, mirror[2].ctx, batch("manual", [{"external_id": "local", "title": "Private Local"}],
                                                           kind="manual"), curator=True)
        session.commit()
        source_counts = {"users": len(list(session.scalars(select(User)))),
                         "canonicals": len(list(session.scalars(select(Canonical))))}
    settings = mirror[2].ctx.settings
    engine = mirror[2].maker.kw["bind"]
    out = tmp_path / "candidate.tar.gz"
    result = promotion.candidate(settings, engine, a[2].ctx.identity.node_id, mirror[2].ctx.identity.node_id, out)
    assert result["node_id"] not in {st.ctx.identity.node_id for st in (a[2], b[2], mirror[2])}
    assert out.stat().st_mode & 0o077 == 0
    assert backup.verify(out)["media_included"] is False
    for table in ("app_user", "user_session", "api_token", "peer", "user_mfa", "outbox"):
        assert archive_rows(out, table) == []
    ids = {row["id"] for row in archive_rows(out, "canonical")}
    assert aid in ids and bid not in ids and local.canonical_id not in ids
    assert {row["node_id"] for row in archive_rows(out, "replicated_change")} == {a[2].ctx.identity.node_id}
    assert all(row["origin"] == "local" for row in archive_rows(out, "field_def"))
    recovered = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/recovered.db",
                         media_dir=str(tmp_path / "media"))
    db.upgrade(recovered.database_url)
    recovered_engine = db.make_engine(recovered.database_url)
    try:
        backup.restore(recovered, recovered_engine, out)
        with db.make_sessionmaker(recovered_engine)() as session:
            identity = load_identity(session)
            assert identity.node_id == result["node_id"]
            assert session.get(Canonical, aid).view["title"] == "Recovered Alpha"
            assert {row.key for row in session.scalars(select(FieldDef))} == {"alpha_note", "after_note"}
            from atlas.models import ChangeLog

            for entry in session.scalars(select(ChangeLog)):
                assert key_rotation.check_entry(identity.node_id, [], {identity.node_id: identity.public_b64},
                                                key_rotation.proof(entry, identity.node_id))
    finally:
        recovered_engine.dispose()
    with mirror[2].maker() as session:
        assert len(list(session.scalars(select(User)))) == source_counts["users"]
        assert len(list(session.scalars(select(Canonical)))) == source_counts["canonicals"]
        assert load_identity(session).node_id == mirror[2].ctx.identity.node_id
        assert session.get(NodeState, "recovery_provenance") is None
    with pytest.raises(promotion.PromotionError, match="already exists"):
        promotion.candidate(settings, engine, a[2].ctx.identity.node_id, mirror[2].ctx.identity.node_id, out)


@pytest.mark.parametrize("attack", ["gap", "signature", "unknown_kind", "wrong_mirror", "wrong_role"])
def test_recovery_refuses_incomplete_or_unverifiable_source(tmp_path, attack):
    a, mirror = node(tmp_path, "a", "authority"), node(tmp_path, "m", "mirror")
    publish(a[2], "Alpha", "note")
    pull(mirror[2], a)
    with mirror[2].maker() as session:
        if attack == "gap":
            session.execute(delete(ReplicatedChange).where(ReplicatedChange.seq == 1))
        elif attack in ("signature", "unknown_kind"):
            entry = session.scalars(select(ReplicatedChange).where(ReplicatedChange.seq == 1)).one()
            if attack == "signature":
                entry.signature = "invalid"
            else:
                entry.kind = "future_kind"
                entry.signature = a[2].ctx.identity.sign(entry.seq, entry.kind, entry.body_hash)
        session.commit()
    settings = mirror[2].ctx.settings
    if attack == "wrong_role":
        settings = settings.model_copy(update={"role": "authority"})
    out = tmp_path / "refused.tar.gz"
    with pytest.raises(promotion.PromotionError):
        promotion.candidate(settings, mirror[2].maker.kw["bind"], a[2].ctx.identity.node_id,
                            "wrong" if attack == "wrong_mirror" else mirror[2].ctx.identity.node_id, out)
    assert not out.exists()


def test_production_mirror_configuration_can_build_an_offline_sqlite_candidate(tmp_path, monkeypatch):
    a, mirror = node(tmp_path, "a", "authority"), node(tmp_path, "m", "mirror")
    publish(a[2], "Production Recovery", "note")
    pull(mirror[2], a)
    monkeypatch.setenv("ATLAS_ENVIRONMENT", "production")
    settings = mirror[2].ctx.settings.model_copy(update={"environment": "production"})
    out = tmp_path / "production-candidate.tar.gz"
    promotion.candidate(settings, mirror[2].maker.kw["bind"], a[2].ctx.identity.node_id,
                        mirror[2].ctx.identity.node_id, out)
    assert backup.verify(out)["tables"]["canonical"] == 1
