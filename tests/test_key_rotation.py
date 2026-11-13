import copy

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import feed, key_rotation, sync
from atlas.models import ChangeLog, NodeState, SyncCursor
from atlas.signing import load_identity

from .test_relay import node, publish, pull
from .test_web import PW, signup, token


def rotate(st, expected=None):
    with st.maker() as session:
        identity = load_identity(session)
        successor = key_rotation.rotate(session, expected or identity.key_id)
        session.commit()
        return successor


def test_rotation_keeps_identity_and_stale_workers_use_the_current_key(tmp_path):
    a, mirror, local = node(tmp_path, "a", "authority"), node(tmp_path, "m", "mirror"), node(tmp_path, "l", "local")
    publish(a[2], "Before", "before_field")
    assert pull(mirror[2], a) > 0
    original = a[2].ctx.identity
    first = rotate(a[2])
    assert first.node_id == original.node_id and first.key_id != original.key_id
    # publish intentionally still holds the pre-rotation Ctx, as another API worker would.
    publish(a[2], "After", "after_field")
    second = rotate(a[2])
    publish(a[2], "After Again", "again_field")
    assert second.node_id == original.node_id and second.key_id not in (original.key_id, first.key_id)
    assert pull(mirror[2], a) > 0
    assert pull(local[2], mirror, original) > 0
    info = a[1].get("/api/v2/sync/info", headers={"Authorization": f"Bearer {a[3]}"}).json()
    assert info["public_key"] == original.public_b64 and info["node_id"] == original.node_id
    assert info["signing_key_id"] == second.key_id and len(info["rotations"]) == 2
    history, keys = key_rotation.validate_history(original.node_id, original.public_b64, info["rotations"], second.key_id)
    with a[2].maker() as session:
        rows = list(session.scalars(select(ChangeLog).order_by(ChangeLog.seq)))
        assert rows[-1].key_id == second.key_id
        for row in rows:
            assert key_rotation.check_entry(original.node_id, history, keys, key_rotation.proof(row, original.node_id))


def mutated_transport(remote, mutate_info=None, mutate_entries=None):
    def respond(request):
        response = remote[1].request(request.method, request.url.raw_path.decode(), headers=dict(request.headers))
        data = copy.deepcopy(response.json())
        if request.url.path.endswith("/sync/info") and mutate_info:
            mutate_info(data)
        elif request.url.path.endswith("/sync/changes") and mutate_entries:
            mutate_entries(data)
        return httpx.Response(response.status_code, json=data)
    return httpx.Client(transport=httpx.MockTransport(respond))


@pytest.mark.parametrize("attack", ["signature", "successor", "sequence", "identity", "tip", "repeated"])
def test_bad_successor_proofs_are_rejected_before_state_changes(tmp_path, attack):
    a, local = node(tmp_path, "a", "authority"), node(tmp_path, "l", "local")
    publish(a[2], "Before", "before_field")
    rotate(a[2])

    def mutate(info):
        proof = info["rotations"][0]
        if attack == "signature":
            proof["signature"] = "invalid"
        elif attack == "successor":
            proof["body"]["public_key"] = local[2].ctx.identity.public_b64
        elif attack == "sequence":
            proof["seq"] += 1
        elif attack == "identity":
            proof["node_id"] = local[2].ctx.identity.node_id
        elif attack == "tip":
            info["signing_key_id"] = local[2].ctx.identity.key_id
        else:
            info["rotations"].append(proof)

    with mutated_transport(a, mutate_info=mutate) as http:
        with pytest.raises(sync.SyncError):
            sync.pull_once(local[2].maker, local[2].ctx, http, "http://a", a[3],
                           trusted={a[2].ctx.identity.node_id: a[2].ctx.identity.public_b64})
    with local[2].maker() as session:
        assert session.scalars(select(SyncCursor)).first() is None


def test_old_signing_key_is_rejected_after_cutover_and_history_cannot_rewind(tmp_path):
    a, local = node(tmp_path, "a", "authority"), node(tmp_path, "l", "local")
    original = a[2].ctx.identity
    publish(a[2], "Before", "before_field")
    rotate(a[2])
    pull(local[2], a)

    def rewind(info):
        info["rotations"] = []
        info["signing_key_id"] = original.key_id

    with mutated_transport(a, mutate_info=rewind) as http:
        with pytest.raises(sync.SyncError, match="rewound or forked"):
            sync.pull_once(local[2].maker, local[2].ctx, http, "http://a", a[3],
                           trusted={original.node_id: original.public_b64})
    publish(a[2], "Future", "future_field")

    def old_signer(data):
        entry = data["entries"][-1]
        entry["key_id"] = original.key_id
        entry["signature"] = original.sign(entry["seq"], entry["kind"], entry["body_hash"])

    with mutated_transport(a, mutate_entries=old_signer) as http:
        with pytest.raises(sync.SyncError, match="bad signature"):
            sync.pull_once(local[2].maker, local[2].ctx, http, "http://a", a[3],
                           trusted={original.node_id: original.public_b64})


def test_lost_signing_key_is_not_silently_replaced(tmp_path):
    a = node(tmp_path, "a", "authority")
    with a[2].maker() as session:
        session.delete(session.get(NodeState, "signing_key"))
        session.commit()
    with a[2].maker() as session:
        with pytest.raises(ValueError, match="signing key is missing"):
            load_identity(session)
        assert session.get(NodeState, "signing_key") is None


def test_same_transaction_uses_successor_immediately(tmp_path):
    a = node(tmp_path, "a", "authority")
    with a[2].maker() as session:
        old = load_identity(session)
        feed.append(session, old, "schema", {"types": [], "fields": []})
        successor = key_rotation.rotate(session, old.key_id)
        feed.append(session, old, "schema", {"types": [], "fields": []})
        session.commit()
        rows = list(session.scalars(select(ChangeLog).where(ChangeLog.kind != "wire_start").order_by(ChangeLog.seq)))
        assert [row.key_id for row in rows] == [old.key_id, old.key_id, successor.key_id]


def test_admin_confirmation_password_csrf_and_stale_form(tmp_path):
    app, _, st, _ = node(tmp_path, "a", "authority")
    client = TestClient(app, base_url="http://localhost:8000", follow_redirects=False)
    signup(client, "alice")
    assert "No rotations" in client.get("/admin/keys").text
    current = st.ctx.identity.key_id
    data = {"csrf": token(client, "/me"), "confirm": current, "password": PW, "followers_upgraded": "on"}
    assert client.post("/admin/keys/rotate", data={**data, "csrf": "wrong"}).status_code == 403
    assert client.post("/admin/keys/rotate", data={**data, "password": "wrong"}).status_code == 400
    assert client.post("/admin/keys/rotate", data=data).status_code == 303
    assert client.post("/admin/keys/rotate", data=data).status_code == 409
    assert client.post("/admin/keys/rotate", data=data).status_code == 429
    with st.maker() as session:
        assert len(key_rotation.own_history(session, st.ctx.identity.node_id)) == 1
