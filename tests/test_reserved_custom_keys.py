import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, select

from atlas import accounts, custom, custom_compat, db, feed, main, sync
from atlas.models import Canonical, ChangeLog, CustomValue, FieldDef, MediaType, Proposal
from atlas.service import AtlasError
from atlas.signing import load_identity, verify

from .conftest import build_settings
from .test_web import PW, login


def test_populated_upgrade_preserves_fields_values_proposals_and_signed_history(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    db.upgrade(url, "0013")
    maker = db.make_sessionmaker(db.make_engine(url))
    settings = build_settings(database_url=url, media_cache_enabled=False, media_dir=str(tmp_path / "media"))
    with maker() as session:
        # Seed the actual old schema through reflected tables, not today's ORM columns.
        metadata = MetaData()
        old_types = Table("media_type", metadata, autoload_with=session.connection())
        old_fields = Table("field_def", metadata, autoload_with=session.connection())
        session.execute(old_types.insert().values(key="game", label="Game", enabled=True, position=0,
                                                  labels={}, origin="local"))
        user = accounts.register(session, settings, "alice", PW)
        identity = load_identity(session)
        game = Canonical(id="atlas:" + "g" * 26, view={"title": "Legacy game"})
        session.add(game)
        session.flush()
        field_id = "f" * 26
        for fid, key, label, kind in [(field_id, "genres", "My categories", "list"),
                                       ("e" * 26, "custom_genres", "Already used", "text")]:
            session.execute(old_fields.insert().values(id=fid, media_type="game", key=key, label=label, kind=kind,
                            choices=[], help="", public=True, editable=True, position=100, origin="local"))
        session.add_all([CustomValue(canonical_id=game.id, key="genres", source="manual", value=["Historical"],
                                    redistributable=True),
                         CustomValue(canonical_id=game.id, key="custom_genres", source="manual", value="Original")])
        proposal = Proposal(kind="edit", canonical_id=game.id, proposer_id=user.id,
                            payload={"custom": {"genres": ["New"]}}, previous={"custom": {"genres": ["Old"]}},
                            status="approved")
        session.add(proposal)
        session.flush()
        old_value_time = session.get(CustomValue, (game.id, "genres", "manual")).updated_at
        original_schema = {"types": [{"key": "game", "label": "Game", "enabled": True, "position": 0, "labels": {}}],
                           "fields": [{k: row[k] for k in ("media_type", "key", "label", "kind", "choices", "help",
                                                          "public", "editable", "position")}
                                      for row in session.execute(select(old_fields)).mappings()]}
        feed.append(session, identity, "schema", original_schema)
        session.commit()
        signed = session.scalars(select(ChangeLog)).one()
        original_signature, original_hash = signed.signature, signed.body_hash
        proposal_id = proposal.id
    db.upgrade(url)
    db.upgrade(url)  # repeat upgrade is a no-op
    with maker() as session:
        field = session.get(FieldDef, field_id)
        assert (field.key, field.label) == ("custom_genres_2", "My categories")
        value = session.get(CustomValue, (game.id, "custom_genres_2", "manual"))
        assert value.value == ["Historical"] and value.redistributable
        assert value.updated_at == old_value_time
        assert session.get(CustomValue, (game.id, "custom_genres", "manual")).value == "Original"
        proposal = session.get(Proposal, proposal_id)
        assert proposal.payload["custom"] == {"custom_genres_2": ["New"]}
        assert proposal.previous["custom"] == {"custom_genres_2": ["Old"]}
        signed = session.scalars(select(ChangeLog)).one()
        assert (signed.body, signed.signature, signed.body_hash) == (original_schema, original_signature, original_hash)
        assert verify(identity.public_b64, identity.node_id, signed.seq, signed.kind,
                      signed.body, signed.body_hash, signed.signature)
        assert custom.clean_values(session, "game", {"genres": ["Compatible"]}, editable_only=True) == {
            "custom_genres_2": ["Compatible"]}
        with pytest.raises(AtlasError, match="supplied twice"):
            custom.clean_values(session, "game", {"genres": ["A"], "custom_genres_2": ["B"]}, editable_only=True)
        with pytest.raises(AtlasError, match="supplied twice"):
            custom.clean_values(session, "game", {"genres": [], "custom_genres_2": ["B"]}, editable_only=True)
    app = main.create_app(settings)
    client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
    assert login(client, "alice").status_code == 303
    page = client.get("/admin/schema")
    assert page.status_code == 200 and "custom_genres_2" in page.text and "Custom fields preserved" in page.text


def test_historical_schema_and_values_replay_without_mutating_signed_body(session, ctx):
    custom.seed(session)
    schema = {"types": [{"key": "game", "label": "Game"}], "fields": [
        {"media_type": "game", "key": "genres", "kind": "list", "label": "Legacy genres"},
        {"media_type": "game", "key": "custom_genres", "kind": "text", "label": "Other field"}]}
    original = copy.deepcopy(schema)
    sync.apply_entry(session, ctx, "upstream", 1, "schema", schema)
    assert schema == original
    assert {f.key for f in custom.fields_for(session, "game")} == {"custom_genres", "custom_genres_2"}
    session.add(Canonical(id="atlas:" + "a" * 26, view={"title": "Replicated"}))
    session.flush()
    sync.apply_entry(session, ctx, "upstream", 2, "custom", {
        "canonical_id": "atlas:" + "a" * 26, "source": "manual", "values": {"genres": ["Legacy"]}})
    assert session.get(CustomValue, ("atlas:" + "a" * 26, "custom_genres_2", "manual")).value == ["Legacy"]
    sync.apply_entry(session, ctx, "upstream", 3, "custom", {
        "canonical_id": "atlas:" + "a" * 26, "source": "manual", "clear": ["genres"]})
    assert session.get(CustomValue, ("atlas:" + "a" * 26, "custom_genres_2", "manual")) is None
    custom.apply_snapshot(session, original)
    assert len(custom_compat.renames(session)) == 1


def test_custom_renames_are_scoped_to_media_type(session):
    custom.seed(session)
    session.add(MediaType(key="book", label="Book"))
    session.flush()
    schema = {"fields": [{"media_type": kind, "key": "genres", "kind": "text", "label": "Old"}
                         for kind in ("game", "book")]}
    # Local types are retained when the snapshot has no upstream types.
    custom.apply_snapshot(session, schema)
    assert {(r["media_type"], r["new"]) for r in custom_compat.renames(session)} == {
        ("game", "custom_genres"), ("book", "custom_genres")}
    # Malformed fields retain the existing per-field rejection behaviour.
    custom.apply_snapshot(session, {"fields": [{"key": []}, None]})


def test_mfa_downgrade_cannot_remove_enabled_protection(tmp_path):
    from alembic import command

    from atlas import mfa
    from atlas.models import UserMfa, now

    url = f"sqlite:///{tmp_path}/downgrade.db"
    db.upgrade(url, "0013")
    maker = db.make_sessionmaker(db.make_engine(url))
    settings = build_settings(secret_key="test-key")
    with maker() as session:
        user = accounts.register(session, settings, "alice", PW)
        factor = mfa.start(session, settings, user, "s" * 64)
        factor.enabled_at = now()
        session.commit()
    with pytest.raises(RuntimeError, match="cannot be silently removed"):
        command.downgrade(db.alembic_config(url), "0012")
    with maker() as session:
        assert session.get(UserMfa, user.id).enabled_at is not None
