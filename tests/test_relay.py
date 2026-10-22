from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import auth, custom, main, service, sync
from atlas.models import Canonical, FieldDef, ReplicatedChange, SyncCursor
from atlas.signing import verify

from .conftest import batch, build_settings

STANDARD = ({key for key, *_ in custom.MULTIPLAYER_FIELDS}
            | {key for _, _, _, fields in custom.BUILTIN_MEDIA_TYPES.values() for key, *_ in fields})


def node(tmp_path, name, role):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/{name}.db", role=role,
                         media_dir=str(tmp_path / name), media_cache_enabled=False))
    st = app.state.atlas
    with st.maker() as session:
        _, secret = auth.create_token(session, "sync-test", ["read", "sync"])
        session.commit()
    return app, TestClient(app), st, secret


def transport(client):
    def json_response(request):
        response = client.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

    return httpx.Client(transport=httpx.MockTransport(json_response))


def publish(st, title, field):
    with st.maker() as session:
        result, = service.ingest(session, st.ctx, batch("manual", [{"external_id": title, "title": title}], kind="manual"),
                                  curator=True)
        custom.save_field(session, st.ctx, "game", field, field, "text")
        session.commit()
    return result.canonical_id


def pull(target, remote, root=None):
    _, client, source, token = remote
    identity = root or source.ctx.identity
    with transport(client) as http:
        return sync.pull_all(target.maker, target.ctx, http, "http://upstream", token,
                             trusted={identity.node_id: identity.public_b64})


def test_two_authorities_relay_to_an_offline_capable_downstream(tmp_path):
    a, b = node(tmp_path, "a", "authority"), node(tmp_path, "b", "authority")
    mirror, local = node(tmp_path, "mirror", "mirror"), node(tmp_path, "local", "local")
    aid, bid = publish(a[2], "Authority Alpha", "alpha_note"), publish(b[2], "Authority Beta", "beta_note")
    assert pull(mirror[2], a) > 0 and pull(mirror[2], b) > 0
    with mirror[2].maker() as session:
        assert {f.key for f in session.scalars(select(FieldDef))} - STANDARD == {"alpha_note", "beta_note"}
        assert {f.origin_node for f in session.scalars(select(FieldDef)) if f.key not in STANDARD} == {
            a[2].ctx.identity.node_id, b[2].ctx.identity.node_id}
    assert pull(local[2], mirror, a[2].ctx.identity) > 0
    assert pull(local[2], mirror, b[2].ctx.identity) > 0
    with local[2].maker() as session:
        assert session.get(Canonical, aid).view["title"] == "Authority Alpha"
        assert session.get(Canonical, bid).view["title"] == "Authority Beta"
        assert len(list(session.scalars(select(SyncCursor)))) == 2
    # Relay entries preserve the authority's exact signed body, not a new mirror signature.
    headers = {"Authorization": f"Bearer {mirror[3]}"}
    entries = mirror[1].get("/api/v2/sync/changes", params={"node_id": a[2].ctx.identity.node_id}, headers=headers).json()
    for row in entries["entries"]:
        assert verify(a[2].ctx.identity.public_b64, row["node_id"], row["seq"], row["kind"], row["body"],
                      row["body_hash"], row["signature"])
        assert row["key_id"] != mirror[2].ctx.identity.node_id
    # No request to A/B is needed for a fresh downstream to recover from the mirror.
    another = node(tmp_path, "another", "local")
    assert pull(another[2], mirror, a[2].ctx.identity) > 0
    assert pull(local[2], mirror, a[2].ctx.identity) == 0


def test_media_type_survives_authority_to_mirror_and_local_federation(tmp_path):
    authority = node(tmp_path, "media-authority", "authority")
    mirror = node(tmp_path, "media-mirror", "mirror")
    local = node(tmp_path, "media-local", "local")
    with authority[2].maker() as session:
        (result,) = service.ingest(
            session,
            authority[2].ctx,
            batch("manual", [{"external_id": "spirited-away", "title": "Spirited Away"}], kind="manual"),
            curator=True,
            media_type="movie",
        )
        session.commit()

    assert pull(mirror[2], authority) > 0
    assert pull(local[2], mirror, authority[2].ctx.identity) > 0
    with local[2].maker() as session:
        record = session.get(Canonical, result.canonical_id)
        assert record is not None
        assert record.media_type == "movie"
        assert record.view["title"] == "Spirited Away"


def test_upgraded_mirror_backfills_without_reverting_current_schema(tmp_path):
    authority, mirror = node(tmp_path, "a", "authority"), node(tmp_path, "m", "mirror")
    publish(authority[2], "Original", "original_field")
    pull(mirror[2], authority)
    with mirror[2].maker() as session:
        # Represents an old mirror which applied entries but did not retain the signed feed.
        for entry in session.scalars(select(ReplicatedChange)).all():
            session.delete(entry)
        session.add(FieldDef(media_type="game", key="local_only", label="Local", kind="text", origin="local"))
        session.commit()
    assert pull(mirror[2], authority) > 0
    with mirror[2].maker() as session:
        assert session.scalars(select(FieldDef).where(FieldDef.key == "local_only")).first()
        assert len(list(session.scalars(select(ReplicatedChange)))) > 0


def test_untrusted_or_ambiguous_relay_is_rejected(tmp_path):
    a, b = node(tmp_path, "a", "authority"), node(tmp_path, "b", "authority")
    mirror, local = node(tmp_path, "m", "mirror"), node(tmp_path, "l", "local")
    publish(a[2], "Alpha", "a_note")
    publish(b[2], "Beta", "b_note")
    pull(mirror[2], a)
    pull(mirror[2], b)
    with transport(mirror[1]) as http:
        with pytest.raises(sync.SyncError, match="not trusted"):
            sync.pull_once(local[2].maker, local[2].ctx, http, "http://mirror", mirror[3], trusted={})
        keys = {st.ctx.identity.node_id: st.ctx.identity.public_b64 for st in (a[2], b[2])}
        with pytest.raises(sync.SyncError, match="several trusted feeds"):
            sync.pull_once(local[2].maker, local[2].ctx, http, "http://mirror", mirror[3], trusted=keys)


def test_concurrent_sync_does_not_rewind_or_duplicate_retained_entries(tmp_path):
    authority, mirror = node(tmp_path, "a", "authority"), node(tmp_path, "m", "mirror")
    publish(authority[2], "Shared", "note")
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: pull(mirror[2], authority), range(2)))
    with mirror[2].maker() as session:
        cursor = session.scalars(select(SyncCursor)).one()
        entries = list(session.scalars(select(ReplicatedChange).order_by(ReplicatedChange.seq)))
        assert [r.seq for r in entries] == list(range(1, cursor.last_seq + 1))
