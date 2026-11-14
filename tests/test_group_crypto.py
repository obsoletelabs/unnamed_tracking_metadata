"""Group cryptography (CONTRACT-C10 §1.1-§1.3, §1.9, §11 A10 a-e, r, u, cc, hh)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text

from atlas import group_crypto as gc
from atlas import groups, secrets_box, signing
from atlas.signing import NodeIdentity, key_id_for, sign_message, verify_message

from .conftest import build_settings
from .group_harness import REC_PW, ROOT_PW
from .group_world import SECRET, World, synth

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "group_vectors.json").read_text(encoding="utf-8"))


@pytest.fixture
def w(monkeypatch):
    world = World(monkeypatch)
    yield world
    world.s.close()
    world.engine.dispose()


def test_domains_prefix_free():
    domains = gc.all_domains()
    assert signing.DOMAIN in domains
    assert len(set(domains)) == len(domains)
    assert gc.prefix_free(domains)
    for a in domains:
        for b in domains:
            assert a == b or not b.startswith(a)


def test_golden_vectors():
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTORS["seed_hex"]))
    assert gc.ed_public_b64(key) == VECTORS["public_key"]
    for vector in VECTORS["sign_message"]:
        domain = vector["domain"].encode()
        assert sign_message(key, domain, vector["parts"]) == vector["signature"], vector["name"]
        assert verify_message(VECTORS["public_key"], domain, vector["parts"], vector["signature"])
    feed = VECTORS["feed"]  # the feed message and signature are byte-identical to before
    identity = NodeIdentity(node_id=key_id_for(base64.b64decode(VECTORS["public_key"])), private=key)
    assert identity.node_id == feed["node_id"]
    assert identity.sign(feed["seq"], feed["kind"], feed["digest"]) == feed["signature"]


def test_embedded_newline_space_empty_and_non_ascii_parts_refused():
    key = Ed25519PrivateKey.generate()
    for parts in VECTORS["rejected_parts"]:
        with pytest.raises(ValueError):
            sign_message(key, gc.DOMAINS["group.cert"], parts)
        assert not verify_message(gc.ed_public_b64(key), gc.DOMAINS["group.cert"], parts, "A" * 88)
    with pytest.raises(ValueError):
        sign_message(key, b"not-atlas\n", ["x"])


def test_envelope_signer_id_is_signed_and_body_issuer_must_match(w):
    """cc."""
    d, x = synth(), synth()
    parent = w.delegate(d)["body"]["serial"]
    cert = w.node_cert(d, x, 1, parent)
    assert w.verdict(cert) == "ok"
    moved = {**cert, "signer": {**cert["signer"], "kind": "root", "id": w.gid}}
    assert w.verdict(moved) != "ok"
    other = w.node_cert(d, x, 1, parent, body_issuer=x.node_id)  # body names another issuer than signer.id
    assert w.verdict(other) == "signer_mismatch"


def test_root_wrap_round_trip_and_wrong_passphrase(monkeypatch):
    monkeypatch.setattr(gc, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(gc, "SCRYPT_MIN_N", 2 ** 10)
    root = Ed25519PrivateKey.generate()
    wrapped = gc.wrap_root(root, ROOT_PW, "9c1e4b7a2f30d811")
    again = gc.unwrap_root(wrapped, ROOT_PW, "9c1e4b7a2f30d811")
    assert gc.ed_public_b64(again) == gc.ed_public_b64(root)
    with pytest.raises(gc.WrongPassphrase):
        gc.unwrap_root(wrapped, ROOT_PW + "x", "9c1e4b7a2f30d811")
    with pytest.raises(gc.WrongPassphrase):  # bound to the group id (AAD)
        gc.unwrap_root(wrapped, ROOT_PW, "0000000000000000")


def test_root_key_needs_passphrase_even_with_secret_key(w):
    """c (T1): with ATLAS_SECRET_KEY available the sealed root opens only to the passphrase-wrapped blob; G itself
    needs the root passphrase, and a wrong one is refused with a typed error."""
    wrapped = json.loads(groups.open_secret(w.settings, w.group.root_sealed))
    assert set(wrapped) >= {"kdf", "salt", "nonce", "ct"} and "root_private" not in json.dumps(wrapped)
    with pytest.raises(gc.WrongPassphrase):
        gc.unwrap_root(wrapped, "", w.gid)
    with pytest.raises(groups.GroupError, match="wrong root passphrase") as exc:
        groups.unlock_root(w.s, w.settings, w.group, ROOT_PW + " not it")
    assert exc.value.code == "forbidden"
    assert gc.key_id_of(gc.ed_public_b64(w.root())) == w.gid


def test_production_kdf_parameters_are_stored():
    assert gc.SCRYPT_N == 2 ** 17 and (gc.SCRYPT_R, gc.SCRYPT_P) == (8, 1)


def test_database_dump_contains_no_root_key(w):
    root = w.root()
    raw = gc.ed_private_raw(root)
    needles = [base64.b64encode(raw).decode(), raw.hex(), base64.urlsafe_b64encode(raw).decode().rstrip("=")]
    dump = []
    with w.engine.connect() as c:
        for (name,) in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'")):
            dump += [repr(tuple(row)) for row in c.execute(text(f'SELECT * FROM "{name}"'))]
    blob = "\n".join(dump)
    assert all(n not in blob for n in needles)
    assert w.group.root_sealed.startswith("enc:v1:") and w.group.box_sealed.startswith("enc:v1:")


def test_recovery_file_one_of_one_holds_root(w):
    keys = gc.open_recovery_file(w.files[0], REC_PW[0])
    assert keys.group_id == w.gid and keys.root is not None
    assert gc.key_id_of(gc.ed_public_b64(keys.root)) == w.gid
    with pytest.raises(gc.WrongPassphrase):
        gc.open_recovery_file(w.files[0], REC_PW[1])
    assert REC_PW[0] not in json.dumps(w.files[0])


def test_recovery_file_two_of_three_has_no_root(monkeypatch):
    w = World(monkeypatch, recovery=3)
    assert len(w.files) == 3
    opened = [gc.open_recovery_file(f, p) for f, p in zip(w.files, REC_PW, strict=True)]
    assert all(k.root is None for k in opened)
    assert w.group.recovery["threshold"] == 2 and len({k.recovery for k in opened}) == 3


def test_genesis_pops_verify_and_tampering_is_refused(w):
    genesis = w.group.genesis
    assert groups.verify_genesis(genesis, w.gid)
    tampered = json.loads(json.dumps(genesis))
    tampered["body"]["policy"]["max_members"] = 31
    assert not groups.verify_genesis(tampered)
    no_pop = {**genesis, "pop": []}
    assert not groups.verify_genesis(no_pop)
    bad_threshold = json.loads(json.dumps(genesis))
    bad_threshold["body"]["recovery"]["threshold"] = 0
    assert not groups.verify_genesis(bad_threshold)


@pytest.mark.parametrize("phrase", ["short pass", "aaaaaaaaaaaaaaaaaaaaaaaa", "passwordpassword1234",
                                    "qwertyuiopasdfghjkl", "Node A the home group!!"])
def test_weak_passphrases_refused(phrase):
    with pytest.raises(gc.WeakPassphrase):
        gc.check_passphrase(phrase, ["Node A", "the home group"])


def test_reasonable_passphrase_accepted():
    gc.check_passphrase("violet tram kettle orbit 42", ["Atlas"])
    with pytest.raises(gc.WeakPassphrase):  # the famous example is on the list
        gc.check_passphrase("correct horse battery staple", ["Atlas"])


def test_create_refuses_weak_or_reused_passphrases(monkeypatch):
    world = World(monkeypatch)
    world.group.status = "left"
    world.group.root_sealed = None
    world.s.flush()
    with pytest.raises(groups.GroupError, match="passphrase refused"):
        groups.create_group(world.s, world.settings, world.me, "Again", "password1", [REC_PW[0]])
    with pytest.raises(groups.GroupError, match="differs"):
        groups.create_group(world.s, world.settings, world.me, "Again", ROOT_PW, [ROOT_PW])


def test_rewrap_covers_group_secrets_and_is_all_or_nothing(w):
    before = (w.group.root_sealed, w.group.box_sealed)
    rotated = build_settings(secret_key="synthetic-new-secret-0123456789abcdef", secret_key_previous=SECRET)
    counts = secrets_box.rewrap(w.s, rotated)
    assert counts["group_secrets"] == 2
    assert (w.group.root_sealed, w.group.box_sealed) != before
    only_new = build_settings(secret_key="synthetic-new-secret-0123456789abcdef")
    assert groups.unlock_root(w.s, only_new, w.group, ROOT_PW)  # readable with the new secret alone
    w.s.commit()
    w.group.box_sealed = "enc:v1:" + secrets_box.encrypt(build_settings(secret_key="synthetic-unrelated-0123456789"),
                                                         "eA==")
    snapshot = w.group.root_sealed
    with pytest.raises(secrets_box.SecretsError):
        secrets_box.rewrap(w.s, rotated)
    w.s.rollback()
    assert w.group.root_sealed == snapshot


def test_rewrap_covers_the_pending_succession_root(w):
    """A G' created by `root succession` waits sealed in node_state; `rewrap` re-encrypts it too, and it still
    becomes this node's root at activation under the new ATLAS_SECRET_KEY alone."""
    from datetime import timedelta

    phrase = "synthetic succession root phrase 51aa"
    keys = gc.open_recovery_file(w.files[0], REC_PW[0])
    obj, _ = groups.issue_succession(w.s, w.settings, w.group, w.me, [keys], phrase,
                                     new_recovery_passphrase="synthetic succession recovery phrase 8e21")
    before = w.s.get(groups.NodeState, groups.SUCCESSION_ROOT).value
    rotated = build_settings(secret_key="synthetic-new-secret-0123456789abcdef", secret_key_previous=SECRET)
    counts = secrets_box.rewrap(w.s, rotated)
    assert counts["group_secrets"] == 3 and w.s.get(groups.NodeState, groups.SUCCESSION_ROOT).value != before
    groups.activate_successions(w.s, w.group, groups.utcnow() + timedelta(hours=25))
    only_new = build_settings(secret_key="synthetic-new-secret-0123456789abcdef")
    root = groups.unlock_root(w.s, only_new, w.group, phrase)
    assert gc.key_id_of(gc.ed_public_b64(root)) == obj["body"]["new_root_key_id"]


def test_object_types_are_not_feed_kinds():
    """G-V4: no group object type is a public feed kind."""
    from atlas import feed_schema

    names = set(getattr(feed_schema, "KINDS", {}) or {}) | set(getattr(feed_schema, "SCHEMAS", {}) or {})
    assert not names & set(gc.OBJECT_TYPES)
    assert all(not t.startswith("group.") for t in names)
