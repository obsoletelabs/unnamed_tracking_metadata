"""CONTRACT-C14 Part C: linking a local node to a main Atlas (C14-3m main side, C14-3n node side).

Test names follow the contract's matrix (§10, T-K1...T-K26). Two in-process nodes: `tests/link_harness.py`."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from atlas import auth, dpop, link, link_client, proposals, scope
from atlas import group_crypto as gc
from atlas import link_crypto as lc
from atlas.models import (
    LinkBannedKey,
    LinkDevice,
    NodeLink,
    OidcAccess,
    Proposal,
    UpstreamAlias,
    UpstreamContribution,
    User,
    now,
)
from atlas.signing import canonical_json

from .link_harness import MAIN, Pair, age, same_game_on_node
from .test_web import PW, signup, token


@pytest.fixture
def pair(tmp_path):
    with Pair(tmp_path) as w:
        yield w


def _user(s, name) -> User:
    return s.scalars(select(User).where(User.username == name)).one()


# ---- raw protocol helpers (what a node sends, built by hand to break one part at a time) -------------------------

def _node_reg(private: Ed25519PrivateKey, *, confirm="BCDFGHJK", rotations=None, current=None, **extra) -> dict:
    root_pub = lc.public_b64(private)
    cur = current or private
    node = {"node_id": lc.key_id(root_pub), "root_public_key": root_pub, "key_id": lc.key_id(lc.public_b64(cur)),
            "public_key": lc.public_b64(cur), "name": "Handmade node", "public_url": None,
            "rotations": rotations or [], "confirm_hash": hashlib.sha256(confirm.encode()).hexdigest(),
            "role": "local"}
    node.update(extra)
    return node


def _device(pair, private, node, *, challenge=None, pop_key=None, proof_key=None, pop=None):
    http = pair.nst.http
    if challenge is None:
        challenge = http.post(f"{MAIN}/oidc/device/challenge").json()["challenge"]
    signer = pop_key or private
    if pop is None:
        pop = lc.sign_pop(signer, lc.registration_parts(MAIN, node, challenge))
    proof = dpop.make_proof(proof_key or private, "POST", f"{MAIN}/oidc/device")
    return http.post(f"{MAIN}/oidc/device", headers={"DPoP": proof},
                     data={"client_id": "atlas-node", "scope": "openid atlas.link atlas.contribute",
                           "atlas_node": canonical_json(node).decode(), "atlas_challenge": challenge,
                           "atlas_node_pop": pop}), challenge


def _token(pair, private, device_code):
    proof = dpop.make_proof(private, "POST", f"{MAIN}/oidc/token")
    return pair.nst.http.post(f"{MAIN}/oidc/token", headers={"DPoP": proof}, data={
        "grant_type": link.DEVICE_GRANT, "client_id": "atlas-node", "device_code": device_code})


def _api(pair, method, path, *, key=None, access=None, body=None, proof=None, htu=None):
    """A resource request with the node's current key and token unless overridden."""
    with pair.nsession() as s:
        row = pair.upstream(s)
        settings = pair.nst.ctx.settings
        key = key or link_client._key(settings, row.link_key)
        access = access if access is not None else link_client._open(settings, row.access_enc)
    proof = proof or dpop.make_proof(key, method, htu or f"{MAIN}{path}", access_token=access)
    return pair.nst.http.request(method, f"{MAIN}{path}", json=body,
                                 headers={"Authorization": f"DPoP {access}", "DPoP": proof})


def _approve_all(pair, reviewer="boss"):
    with pair.msession() as s:
        boss = _user(s, reviewer)
        for p in s.scalars(select(Proposal).where(Proposal.status == "pending")):
            proposals.review(s, pair.mst.ctx, boss, p.id, approve=True)
        s.commit()


# ---- vectors ---------------------------------------------------------------------------------------------------

def test_dpop_thumbprint_vector_and_proof_rules():
    # RFC 8037 appendix A.3: the thumbprint of the RFC 8032 test key
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}
    assert dpop.thumbprint(jwk) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    key = Ed25519PrivateKey.generate()
    proof = dpop.make_proof(key, "POST", "https://main.example/oidc/token?x=1")
    got = dpop.verify_proof(proof, htm="POST", htu="https://MAIN.example/oidc/token")
    assert got.jkt == dpop.jkt_of(lc.public_b64(key))
    for kw, reason in [(dict(htm="GET", htu="https://main.example/oidc/token"), "wrong_method"),
                       (dict(htm="POST", htu="https://main.example/other"), "wrong_url"),
                       (dict(htm="POST", htu="https://main.example/oidc/token", require_ath=True), "bad_ath"),
                       (dict(htm="POST", htu="https://main.example/oidc/token", now=got.iat + 301), "stale_proof")]:
        with pytest.raises(dpop.DpopError) as exc:
            dpop.verify_proof(proof, **kw)
        assert exc.value.reason == reason
    bound = dpop.make_proof(key, "GET", "https://main.example/api/v2/link", access_token="tok")
    dpop.verify_proof(bound, htm="GET", htu="https://main.example/api/v2/link", access_token="tok")
    with pytest.raises(dpop.DpopError):
        dpop.verify_proof(bound, htm="GET", htu="https://main.example/api/v2/link", access_token="other")


def test_link_envelope_vectors_sign_t_and_v():
    """T-K18: pinned canonical vectors; `t` and `_v` are signed; the group verifier refuses a link object."""
    k = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pub = lc.public_b64(k)
    kid = lc.key_id(pub)
    assert (pub, kid) == ("A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=", "56475aa75463474c")
    body = {"link_id": "01J00000000000000000000000", "node_id": kid,
            "subject": "8c3a0f62-1e7b-8d4f-a5c2-3b9e1d7f0a64", "seq": 1, "kind": "edit",
            "canonical_id": "atlas:01j00000000000000000000000", "payload": {"developer": "Supergiant Games"},
            "note": "", "base": None, "created_at": "2026-11-11T00:00:00Z", "local_canonical_id": None}
    obj = lc.make("atlas.contribution", kid, f"contrib:{kid}/01J00000000000000000000001", body,
                  {"kind": "node", "id": kid, "key_id": kid}, k)
    assert obj["body_hash"] == "1f29518f0c8b42d2b414784e87abc1de90c752d08f0d8847e4b75e704aece9a5"
    assert obj["sig"] == ("aAB+vucY+s8LebgvP4r69StjoffCSbovmYeDwDlPqlbfUBaS2V6kcX/CD32dLrTEjgIerg/bHyVkUVoWGFgMDA==")
    grant = lc.make("atlas.link", kid, "link:01J00000000000000000000000:1:1793000000",
                    {"link_id": "01J00000000000000000000000", "key_epoch": 1},
                    {"kind": "node", "id": kid, "key_id": kid}, k)
    assert grant["body_hash"] == "31029f917a6257cb22f54b4cbbab72bb930284a8e6e36426dbcb862584a1a0c5"
    assert grant["sig"] == ("XhWZSKJRMy08yU1FJEtnnuDWrnTruvKdJmKOKdedjw5P8IXbr0sjV2as4o98X3kkNl9aABqReTS+Su6VswR/BQ==")
    assert lc.verify(obj, pub, t="atlas.contribution") and lc.verify(grant, pub, t="atlas.link")
    assert not lc.verify(obj, pub, t="atlas.link")
    assert not lc.verify({**obj, "t": "atlas.link"}, pub)  # `t` is signed
    assert not lc.verify({**obj, "_v": 2}, pub)  # `_v` is signed (and unsupported)
    assert not lc.verify({**obj, "body": {**body, "seq": 2}}, pub)
    assert not gc.verify_object(obj, pub)  # never a group object
    domains = gc.all_domains()
    assert all(not a.startswith(b) for a in domains for b in domains if a != b)  # still prefix-free


# ---- discovery and the switch (T-K12) -------------------------------------------------------------------------

def test_link_disabled_main_advertises_nothing(tmp_path):
    with Pair(tmp_path, main_kw={"link_enabled": False, "oidc_provider_enabled": True}) as off:
        doc = off.main.get("/.well-known/openid-configuration").json()
        assert "atlas_link" not in doc and "device_authorization_endpoint" not in doc
        assert doc["grant_types_supported"] == ["authorization_code"]
        assert off.main.post("/oidc/device/challenge").status_code == 404
        assert off.main.get("/api/v2/link").status_code == 404
        assert off.main.get("/link").status_code == 404
        with off.nsession() as s, pytest.raises(link_client.LinkClientError, match="does not accept node links"):
            link_client.start(s, off.nst.ctx.settings, off.nst.ctx.identity, off.nst.http, MAIN)
    with Pair(tmp_path, name="on") as on:  # links are on by default on an authority (H73), provider off
        doc = on.main.get("/.well-known/openid-configuration").json()
        assert doc["atlas_link"]["v"] == 1 and doc["dpop_signing_alg_values_supported"] == ["EdDSA"]
        assert link.DEVICE_GRANT in doc["grant_types_supported"] and "pairwise" in doc["subject_types_supported"]
        assert "sync" not in json.dumps(doc["atlas_link"])  # H68: no link sync member
        assert on.node.get("/.well-known/openid-configuration").status_code == 404  # a local node is no main


# ---- §6.3 the device flow (T-K1, T-K23, T-K15) ---------------------------------------------------------------

def test_device_flow_registers_node_key_with_pop(pair):
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    node = _node_reg(key)
    r, _ = _device(pair, key, node, pop_key=other)
    assert r.status_code == 400 and "possession" in r.json()["error_description"]
    r, _ = _device(pair, key, node, proof_key=other)
    assert r.json()["error"] == "invalid_dpop_proof"
    succ = Ed25519PrivateKey.generate()
    bad_cert = lc.rotation_certificate(other, node["node_id"], 1, lc.public_b64(succ))  # signed by a stranger
    r, _ = _device(pair, succ, _node_reg(key, rotations=[bad_cert], current=succ))
    assert r.status_code == 400 and "history" in r.json()["error_description"]
    r, _ = _device(pair, key, _node_reg(key, role="authority"))
    assert r.json()["error_description"] == "node_is_authority"
    with pair.msession() as s:
        s.add(LinkBannedKey(node_id=node["node_id"], reason="test", banned_at=now()))
        s.commit()
    r, _ = _device(pair, key, node)
    assert r.status_code == 403 and r.json()["error"] == "access_denied"
    good = Ed25519PrivateKey.generate()
    r, _ = _device(pair, good, _node_reg(good))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert re.fullmatch(r"[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}", body["user_code"])
    assert body["verification_uri_complete"] == f"{MAIN}/link?code={body['user_code']}"
    with pair.msession() as s:  # codes are kept only as a hash and an HMAC
        row = s.scalars(select(LinkDevice)).one()
        assert row.device_code_hash == hashlib.sha256(body["device_code"].encode()).hexdigest()
        assert body["user_code"].replace("-", "") not in row.user_code_hmac


def test_device_registration_challenge_and_atomic_consume(pair):
    """T-K23: the challenge is single use, polls are bound to the registering key, one success only."""
    key = Ed25519PrivateKey.generate()
    node = _node_reg(key)
    r, challenge = _device(pair, key, node)
    assert r.status_code == 200
    again, _ = _device(pair, key, node, challenge=challenge)  # a replayed PoP over a used challenge
    assert again.status_code == 400 and "challenge" in again.json()["error_description"]
    device_code = r.json()["device_code"]
    pending = _token(pair, key, device_code)
    assert pending.json()["error"] == "authorization_pending" and pending.headers["cache-control"] == "no-store"
    assert _token(pair, key, device_code).json()["error"] == "slow_down"  # polled faster than the interval
    assert _token(pair, Ed25519PrivateKey.generate(), device_code).json()["error"] == "invalid_grant"
    # approve on the main, then two polls: one success, the other refused
    view = {"user_code": r.json()["user_code"], "confirm": "BCDF-GHJK"}
    assert pair.approve(view).status_code == 303
    first, second = _token(pair, key, device_code), _token(pair, key, device_code)
    assert first.status_code == 200 and first.json()["token_type"] == "DPoP"
    assert second.json()["error"] == "invalid_grant"
    # the token-endpoint proof carries no `ath`; a resource request without `ath` is refused
    access = first.json()["access_token"]
    no_ath = dpop.make_proof(key, "GET", f"{MAIN}/api/v2/link")
    r = pair.nst.http.get(f"{MAIN}/api/v2/link", headers={"Authorization": f"DPoP {access}", "DPoP": no_ath})
    assert r.status_code == 401 and r.json()["error_description"] == "bad_ath"
    ok = dpop.make_proof(key, "GET", f"{MAIN}/api/v2/link", access_token=access)
    r = pair.nst.http.get(f"{MAIN}/api/v2/link", headers={"Authorization": f"DPoP {access}", "DPoP": ok})
    assert r.status_code == 200 and r.json()["trust"] == "probation" and r.headers["cache-control"] == "no-store"


def test_unknown_code_alarm_never_pauses_code_entry(pair, monkeypatch):
    monkeypatch.setattr(link, "UCODE_MISS_GLOBAL", (2, 600))
    t = token(pair.main, "/link")
    for _ in range(2):
        r = pair.main.post("/link", data={"csrf": t, "code": "BBBB-BBBB"})
        assert r.status_code == 400
    r = pair.main.post("/link", data={"csrf": t, "code": "BBBB-BBBB"})  # the alarm: a 429, no worker sleeps
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    with pair.msession() as s:
        assert link.alarm(s) is not None
    view = pair.start()  # a real code still works for everyone
    assert pair.approve(view).status_code == 303


def test_link_approval_resists_device_code_phishing(pair):
    """T-K15: GET never approves; CSRF; the node's confirmation code is required; recent authentication."""
    view = pair.start()
    page = pair.main.get(f"/link?code={view['user_code']}")
    assert page.status_code == 200 and "data-link-request" not in page.text  # only prefilled
    with pair.msession() as s:
        assert s.scalars(select(LinkDevice)).one().status == "pending"
    t = token(pair.main, "/link")
    assert pair.main.post("/link/decide", data={"code": view["user_code"], "decision": "approve",
                                                "confirm": view["confirm"], "password": PW}).status_code == 403
    shown = pair.main.post("/link", data={"csrf": t, "code": view["user_code"]})
    with pair.msession() as s:
        node_id = s.scalars(select(LinkDevice)).one().node["node_id"]
    assert link.fingerprint(node_id) in shown.text and "testclient" in shown.text  # fingerprint, IP
    bad_pw = pair.approve(view, password="wrong password")
    assert bad_pw.status_code == 400 and "Confirm your password" in bad_pw.text
    for _ in range(4):
        r = pair.approve(view, confirm="ZZZZ-ZZZZ")
        assert r.status_code == 400 and "does not match" in r.text
    r = pair.approve(view, confirm="ZZZZ-ZZZZ")  # the fifth wrong code refuses the flow
    assert "Too many wrong confirmation codes" in r.text
    with pair.msession() as s:
        assert s.scalars(select(LinkDevice)).one().status == "denied"
    assert pair.poll() == "failed"


def test_young_account_cannot_link(pair):
    signup(pair.main, "newbie")
    r = pair.main.get("/link")
    assert "younger than one day" in r.text
    t = token(pair.main, "/link")
    assert pair.main.post("/link", data={"csrf": t, "code": "BCDF-GHJK"}).status_code == 403


def test_authority_never_links_upward(tmp_path):
    with Pair(tmp_path, node_kw={"role": "authority"}) as w:
        with w.nsession() as s, pytest.raises(link_client.LinkClientError, match="authority"):
            link_client.start(s, w.nst.ctx.settings, w.nst.ctx.identity, w.nst.http, MAIN)
        page = w.node.get("/admin/main")
        assert page.status_code == 200 and "cannot link to another one" in page.text


# ---- §6.4 subject (T-K2) and §6.5 DPoP (T-K3, T-K4) ----------------------------------------------------------

def test_pairwise_subject_stable_per_user_and_node(pair):
    pair.link()
    with pair.msession() as s:
        first = pair.link_row(s)
        dave = _user(s, "dave")
        sub, node_id = first.subject, first.node_id
        assert sub != dave.id and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                                               sub)
        assert link.subject_for(s, pair.mst.ctx.settings, dave.id, node_id) == sub
    with pair.nsession() as s:
        row = pair.upstream(s)
        assert row.subject == sub and row.display_name is None  # pseudonymous by default (H65)
    # relink: the same link row and subject
    with pair.nsession() as s:
        pair.upstream(s).status = "remote_revoked"
        s.commit()
    view = pair.start(relink=True)
    assert pair.approve(view).status_code == 303 and pair.poll() == "linked"
    with pair.msession() as s:
        rows = list(s.scalars(select(NodeLink)))
        assert len(rows) == 1 and rows[0].subject == sub and rows[0].key_epoch == 2
    # a new link after an unlink uses a fresh per-main key (C14-11): another node id, another subject
    with pair.nsession() as s:
        link_client.unlink(s, pair.nst.ctx.settings, pair.nst.ctx.identity, pair.nst.http, pair.upstream(s))
        s.commit()
    pair.link()
    with pair.msession() as s:
        subs = {r.node_id: r.subject for r in s.scalars(select(NodeLink))}
        assert len(subs) == 2 and len(set(subs.values())) == 2


def test_dpop_binding_replay_and_rotation(pair):
    pair.link()
    assert _api(pair, "GET", "/api/v2/link").status_code == 200
    stranger = Ed25519PrivateKey.generate()
    r = _api(pair, "GET", "/api/v2/link", key=stranger)
    assert r.status_code == 401 and r.json()["error_description"] == "key_mismatch"
    with pair.nsession() as s:
        row = pair.upstream(s)
        key = link_client._key(pair.nst.ctx.settings, row.link_key)
        access = link_client._open(pair.nst.ctx.settings, row.access_enc)
    proof = dpop.make_proof(key, "GET", f"{MAIN}/api/v2/link", access_token=access)
    assert _api(pair, "GET", "/api/v2/link", proof=proof).status_code == 200
    assert _api(pair, "GET", "/api/v2/link", proof=proof).json()["error_description"] == "replayed_proof"
    assert _api(pair, "GET", "/api/v2/link", htu=f"{MAIN}/api/v2/other").json()["error_description"] == "wrong_url"
    stale = dpop.make_proof(key, "GET", f"{MAIN}/api/v2/link", access_token=access, iat=int(now().timestamp()) - 400)
    assert _api(pair, "GET", "/api/v2/link", proof=stale).json()["error_description"] == "stale_proof"
    # rotation: only the new key with the newly issued tokens works afterwards
    with pair.nsession() as s:
        link_client.rotate_key(s, pair.nst.ctx.settings, pair.nst.ctx.identity, pair.nst.http, pair.upstream(s))
        s.commit()
    assert _api(pair, "GET", "/api/v2/link").json()["key_epoch"] == 2
    r = _api(pair, "GET", "/api/v2/link", key=key)  # the superseded key with the new token
    assert r.status_code == 401
    r = _api(pair, "GET", "/api/v2/link", key=key, access=access)  # the old key with its own old token
    assert r.status_code == 401
    with pair.nsession() as s:
        new_key = link_client._key(pair.nst.ctx.settings, pair.upstream(s).link_key)
    r = _api(pair, "GET", "/api/v2/link", key=new_key, access=access)  # the new key with an old-epoch token
    assert r.status_code == 401
    with pair.msession() as s:
        assert pair.link_row(s).status == "active"  # a plain GET by an old key does not freeze


def test_rotation_request_needs_current_key_and_new_key_pop(pair):
    pair.link()
    settings = pair.nst.ctx.settings
    with pair.nsession() as s:
        row = pair.upstream(s)
        current = link_client._key(settings, row.link_key)
        succ = Ed25519PrivateKey.generate()
        cert = lc.rotation_certificate(current, link_client.link_node_id(row), 1, lc.public_b64(succ))
        parts = lc.rotation_parts(MAIN, row.link_id, link_client.link_node_id(row), cert["body"]["key_id"], 2)
        access = link_client._open(settings, row.access_enc)
    body = {"rotations": [cert], "new_key_pop": lc.sign_pop(current, parts)}  # PoP by the old key
    r = _api(pair, "POST", "/api/v2/link/rotation", body=body)
    assert r.status_code == 400 and "new_key_pop" in r.text
    by_new = dpop.make_proof(succ, "POST", f"{MAIN}/api/v2/link/rotation", access_token=access)
    r = _api(pair, "POST", "/api/v2/link/rotation", body={**body, "new_key_pop": lc.sign_pop(succ, parts)},
             proof=by_new)
    assert r.status_code == 401  # a rotation proven by the NEW key is refused
    r = _api(pair, "POST", "/api/v2/link/rotation", body={**body, "new_key_pop": lc.sign_pop(succ, parts)})
    assert r.status_code == 200 and r.json()["atlas_link"]["body"]["key_epoch"] == 2


def test_refresh_reuse_revokes_link_tokens(pair):
    """T-K4."""
    pair.link()
    settings = pair.nst.ctx.settings
    with pair.nsession() as s:
        row = pair.upstream(s)
        key = link_client._key(settings, row.link_key)
        refresh = link_client._open(settings, row.refresh_enc)

    def use():
        proof = dpop.make_proof(key, "POST", f"{MAIN}/oidc/token")
        return pair.nst.http.post(f"{MAIN}/oidc/token", headers={"DPoP": proof}, data={
            "grant_type": "refresh_token", "client_id": "atlas-node", "refresh_token": refresh})

    fresh = use()
    assert fresh.status_code == 200 and "id_token" not in fresh.json()
    assert use().json()["error"] == "invalid_grant"  # reused
    new_access = fresh.json()["access_token"]
    r = _api(pair, "GET", "/api/v2/link", key=key, access=new_access)
    assert r.status_code == 401  # the reuse revoked every token of the link
    with pair.msession() as s:
        assert pair.link_row(s).status == "active"  # revoked tokens, not the link


def test_refresh_reuse_after_rotation_freezes(pair):
    pair.link()
    settings = pair.nst.ctx.settings
    with pair.nsession() as s:
        row = pair.upstream(s)
        old_key = link_client._key(settings, row.link_key)
        old_refresh = link_client._open(settings, row.refresh_enc)
    proof = dpop.make_proof(old_key, "POST", f"{MAIN}/oidc/token")
    assert pair.nst.http.post(f"{MAIN}/oidc/token", headers={"DPoP": proof}, data={
        "grant_type": "refresh_token", "client_id": "atlas-node", "refresh_token": old_refresh}).status_code == 200
    with pair.nsession() as s:
        link_client.rotate_key(s, settings, pair.nst.ctx.identity, pair.nst.http, pair.upstream(s))
        s.commit()
    proof = dpop.make_proof(old_key, "POST", f"{MAIN}/oidc/token")
    r = pair.nst.http.post(f"{MAIN}/oidc/token", headers={"DPoP": proof}, data={
        "grant_type": "refresh_token", "client_id": "atlas-node", "refresh_token": old_refresh})
    assert r.json()["error"] == "invalid_grant"
    with pair.msession() as s:
        assert pair.link_row(s).status == "frozen"


# ---- §7.4 contributions (T-K5, T-K6, T-K8, T-K16, T-K21) --------------------------------------------------------

def test_contribution_becomes_proposal_under_main_rules(pair):
    pair.link()
    same_game_on_node(pair)
    pair.propose()
    assert pair.send()["sent"] == 1
    with pair.msession() as s:
        p = s.scalars(select(Proposal)).one()
        dave = _user(s, "dave")
        assert p.status == "pending" and p.proposer_id == dave.id and p.via_link_id == pair.link_row(s).id
        assert p.payload == {"developer": "Supergiant Games"}
        from atlas import accounts

        accounts.set_status(s, _user(s, "boss"), dave, "suspended")  # suspending revokes the links (§6.7)
        s.commit()
    with pair.msession() as s:
        assert pair.link_row(s).status == "revoked"  # suspending revokes the links (§6.7)


def test_contribution_refusals_carry_reasons(pair):
    pair.link()
    same_game_on_node(pair)
    with pair.nsession() as s:
        row = pair.upstream(s)
        bad = link_client.propose(s, pair.nst.ctx.settings, pair.nst.ctx.identity, row, kind="edit",
                                  local_cid="atlas:00000000000000000000000000", payload={"developer": "X"})
        s.commit()
    out = pair.send()
    assert out["failed"] == 1
    with pair.nsession() as s:
        c = s.get(UpstreamContribution, bad.id)
        assert c.state == "failed" and c.last_error == "unknown_game"


def test_contribution_idempotent_and_seq_monotone(pair):
    """T-K6: a resent envelope gets its stored answer; a lower seq is refused for good."""
    pair.link()
    same_game_on_node(pair)
    cid = pair.propose()
    pair.send()
    with pair.nsession() as s:
        env = s.get(UpstreamContribution, cid).envelope
    r = _api(pair, "POST", "/api/v2/link/contributions", body={"items": [env]})
    [ans] = r.json()["items"]
    assert ans["status"] == "duplicate" and ans["state"] == "pending"
    with pair.nsession() as s:  # a new item (new id) reusing seq 1, properly signed
        row = pair.upstream(s)
        key = link_client._key(pair.nst.ctx.settings, row.link_key)
        node_id = link_client.link_node_id(row)
        body = {**env["body"], "payload": {"publisher": "Supergiant"}}
        replay = lc.make("atlas.contribution", node_id, f"contrib:{node_id}/01J00000000000000000000009", body,
                         {"kind": "node", "id": node_id, "key_id": link_client.current_key_id(row)}, key)
    [ans] = _api(pair, "POST", "/api/v2/link/contributions", body={"items": [replay]}).json()["items"]
    assert ans["status"] == "refused" and ans["reason"] == "seq_backwards"
    with pair.msession() as s:
        assert len(list(s.scalars(select(Proposal)))) == 1


def test_contribution_signature_and_link_checked(pair):
    pair.link()
    same_game_on_node(pair)
    cid = pair.propose()
    with pair.nsession() as s:
        env = dict(s.get(UpstreamContribution, cid).envelope)
    tampered = {**env, "body": {**env["body"], "payload": {"developer": "Evil"}}}
    [ans] = _api(pair, "POST", "/api/v2/link/contributions", body={"items": [tampered]}).json()["items"]
    assert ans["reason"] == "bad_signature"


def test_stale_base_flagged(pair):
    """T-K8."""
    pair.link()
    same_game_on_node(pair)
    with pair.msession() as s:  # the main's record moves on after the node saw it
        from atlas.models import Canonical

        g = s.get(Canonical, pair.gid)
        g.view = {**g.view, "developer": "Someone Else"}
        g.version += 1
        s.commit()
    pair.propose()
    pair.send()
    with pair.msession() as s:
        assert s.scalars(select(Proposal)).one().stale_base is True
    page = pair.main.get("/review")  # dave is no reviewer; the admin sees it
    assert page.status_code in (200, 303, 403)


def test_new_link_probation_until_link_reputation(tmp_path):
    """T-K21 + T-K16: a trusted reviewer's fresh link is queued; the link earns trust from approved work; through a
    link auto-apply follows reputation, never the reviewer role; `review=always` keeps queueing."""
    with Pair(tmp_path, main_kw={"auto_approve_reputation": 5}) as w:
        with w.msession() as s:
            dave = _user(s, "dave")
            dave.reputation, dave.role = 100, "curator"
            s.commit()
        w.link()
        same_game_on_node(w)
        w.propose()
        w.send()
        with w.msession() as s:
            assert s.scalars(select(Proposal)).one().status == "pending"  # probation, despite reputation
            with pytest.raises(Exception, match="own proposal"):  # never self-approved, even by a reviewer
                proposals.review(s, w.mst.ctx, _user(s, "dave"), s.scalars(select(Proposal)).one().id, approve=True)
        _approve_all(w)
        with w.msession() as s:
            assert w.link_row(s).trust == "trusted" and w.link_row(s).reputation == 5
        w.propose({"publisher": "Supergiant Games"})
        w.send()
        with w.msession() as s:
            newest = s.scalars(select(Proposal).order_by(Proposal.created_at.desc())).first()
            assert newest.status == "approved" and newest.auto_applied
        with w.msession() as s:  # the reviewer role does not auto-apply through a link: reputation does
            dave = _user(s, "dave")
            dave.reputation = 0
            s.commit()
        w.propose({"series": "Hades"})
        w.send()
        with w.msession() as s:
            newest = s.scalars(select(Proposal).order_by(Proposal.created_at.desc())).first()
            assert newest.status == "pending"
            dave = _user(s, "dave")
            dave.reputation = 100
            w.link_row(s).review = "always"
            s.commit()
        w.propose({"description": "A rogue-like."})
        w.send()
        with w.msession() as s:
            assert s.scalars(select(Proposal).order_by(Proposal.created_at.desc())).first().status == "pending"


def test_relink_restarts_probation(tmp_path):
    with Pair(tmp_path, main_kw={"auto_approve_reputation": 5}) as w:
        w.link()
        same_game_on_node(w)
        w.propose()
        w.send()
        _approve_all(w)
        with w.msession() as s:
            assert w.link_row(s).trust == "trusted"
        view = w.start(relink=True)
        assert w.approve(view).status_code == 303 and w.poll() == "linked"
        with w.msession() as s:
            assert w.link_row(s).trust == "probation" and w.link_row(s).reputation == 0


# ---- §6.7 revocation (T-K9, T-K26), rotation (T-K24) ----------------------------------------------------------

def test_revocation_is_immediate_and_pending_kept(pair):
    pair.link()
    same_game_on_node(pair)
    pair.propose()
    pair.send()
    with pair.msession() as s:
        link_id = pair.link_row(s).id
    t = token(pair.main, "/me/links")
    assert pair.main.post(f"/me/links/{link_id}/unlink", data={"csrf": t}).status_code == 303
    with pair.msession() as s:
        p = s.scalars(select(Proposal)).one()
        assert p.status == "pending" and p.link_flag == "link_revoked"  # H69: kept for review
    assert _api(pair, "GET", "/api/v2/link").status_code == 401
    pair.propose({"publisher": "X"})
    pair.send()
    with pair.nsession() as s:
        row = pair.upstream(s)
        assert row.status == "remote_revoked"
        assert {c.state for c in s.scalars(select(UpstreamContribution))} >= {"link_revoked"}


def test_review_lists_and_bulk_reverts_everything_a_link_applied(pair):
    """C14 §6.7 / Review #1 C14-10: `/review?via_link=<id>` lists applied link contributions too, and one action
    reverts all of them (newest first); a change that cannot be undone automatically stays listed."""
    pair.link()
    same_game_on_node(pair)
    pair.propose({"developer": "Dev A"})
    pair.propose({"publisher": "Pub B"})
    assert pair.send()["sent"] == 2
    _approve_all(pair)
    with pair.msession() as s:
        link_id = pair.link_row(s).id
        applied = s.scalars(select(Proposal).where(Proposal.via_link_id == link_id)).all()
        assert len(applied) == 2 and {p.status for p in applied} == {"approved"}
    from .test_web import login

    pair.main.cookies.clear()
    login(pair.main, "boss")
    page = pair.main.get(f"/review?via_link={link_id}")
    assert page.status_code == 200 and "Applied from this link (2)" in page.text
    assert f'action="/review/link/{link_id}/revert"' in page.text
    t = token(pair.main, f"/review?via_link={link_id}")
    r = pair.main.post(f"/review/link/{link_id}/revert", data={"csrf": t})
    assert r.status_code == 303 and "m=link_reverted" in r.headers["location"]
    with pair.msession() as s:
        assert {p.status for p in s.scalars(select(Proposal).where(Proposal.via_link_id == link_id))} == {"reverted"}
    page = pair.main.get(f"/review?via_link={link_id}")
    assert "Applied from this link (0)" in page.text and "Nothing from this link is applied." in page.text


def test_bulk_revert_needs_review_rights(pair):
    pair.link()
    with pair.msession() as s:
        link_id = pair.link_row(s).id
        with pytest.raises(Exception, match="review rights"):
            proposals.revert_link(s, pair.mst.ctx, _user(s, "dave"), link_id)


def test_admin_ban_flags_compromised_and_blocks_relink(pair):
    pair.link()
    same_game_on_node(pair)
    pair.propose()
    pair.send()
    with pair.msession() as s:
        link_id = pair.link_row(s).id
    pair.main.cookies.clear()
    from .test_web import login

    login(pair.main, "boss")
    t = token(pair.main, "/admin/links")
    assert pair.main.post(f"/admin/links/{link_id}/ban", data={"csrf": t, "compromised": "on"}).status_code == 303
    with pair.msession() as s:
        p = s.scalars(select(Proposal)).one()
        assert p.link_flag == "link_compromised" and pair.link_row(s).status == "banned"
        with pytest.raises(Exception, match="I checked this change"):
            proposals.review(s, pair.mst.ctx, _user(s, "boss"), p.id, approve=True)
        proposals.review(s, pair.mst.ctx, _user(s, "boss"), p.id, approve=True, checked=True)
        s.commit()
    page = pair.main.get(f"/review?via_link={link_id}")
    assert page.status_code == 200
    # the same key cannot link again under any account
    with pair.nsession() as s:
        row = pair.upstream(s)
        key = link_client._key(pair.nst.ctx.settings, row.link_key)
    r, _ = _device(pair, key, _node_reg(key))
    assert r.status_code == 403


def test_revoked_link_proposals_flagged_never_auto_applied(tmp_path):
    """T-K26 including the node side `revoked_locally -> pending_remote -> confirmed`."""
    with Pair(tmp_path) as w:
        w.link()
        same_game_on_node(w)
        w.propose()
        w.send()
        real_http = w.nst.http

        class Down:
            def request(self, *a, **kw):
                import httpx

                raise httpx.ConnectError("down")

            post = get = request

        with w.nsession() as s:
            out = link_client.unlink(s, w.nst.ctx.settings, w.nst.ctx.identity, Down(), w.upstream(s))
            s.commit()
        assert out == "pending_remote"
        with w.msession() as s:
            assert w.link_row(s).status == "active"  # the main has not heard yet
        with w.nsession() as s:
            assert link_client.confirm_unlink(s, w.nst.ctx.settings, real_http, w.upstream(s)) == "confirmed"
            s.commit()
        with w.msession() as s:
            p = s.scalars(select(Proposal)).one()
            assert w.link_row(s).status == "revoked" and p.link_flag == "link_revoked" and p.status == "pending"


def test_node_reports_stolen_key(pair):
    pair.link()
    same_game_on_node(pair)
    pair.propose()
    pair.send()
    t = token(pair.node, "/admin/main")
    with pair.nsession() as s:
        main_id = pair.upstream(s).main_node_id
    r = pair.node.post(f"/admin/main/{main_id}/unlink", data={"csrf": t, "compromised": "on"})
    assert r.status_code == 303
    with pair.msession() as s:
        assert s.scalars(select(Proposal)).one().link_flag == "link_compromised"
        assert pair.link_row(s).revoke_reason == "compromised"


def test_rotation_append_only_fork_freezes_link(pair):
    """T-K24."""
    pair.link()
    same_game_on_node(pair)
    pair.propose()
    pair.send()
    settings = pair.nst.ctx.settings
    with pair.nsession() as s:  # a routine rotation
        link_client.rotate_key(s, settings, pair.nst.ctx.identity, pair.nst.http, pair.upstream(s))
        s.commit()
    with pair.nsession() as s:
        row = pair.upstream(s)
        root_id = link_client.link_node_id(row)
        current = link_client._key(settings, row.link_key)
        # a diverging history: another successor of the root (as a thief holding the root key would make)
        root_priv = None
        assert len(row.link_rotations) == 1
        epoch = row.key_epoch
    other = Ed25519PrivateKey.generate()
    fork_cert = lc.rotation_certificate(current, root_id, 2, lc.public_b64(other))
    with pair.nsession() as s:
        row = pair.upstream(s)
        bad = [{**row.link_rotations[0], "seq": 1}, fork_cert]
    parts = lc.rotation_parts(MAIN, row.link_id, root_id, fork_cert["body"]["key_id"], epoch + 1)
    forked = [bad[1]]  # drops the registered first transition: not append-only
    r = _api(pair, "POST", "/api/v2/link/rotation",
             body={"rotations": forked, "new_key_pop": lc.sign_pop(other, parts)})
    assert r.status_code == 400 and "history refused" in r.text  # not a valid history from the root at all
    with pair.msession() as s:
        lr = pair.link_row(s)
        assert lr.status == "active" and len(lr.rotations) == 1  # an invalid request changes nothing
    _ = (root_priv, bad)


def test_fork_freezes_and_owner_relink_recovers(pair):
    pair.link()
    same_game_on_node(pair)
    pair.propose()
    pair.send()
    settings = pair.nst.ctx.settings
    with pair.nsession() as s:
        link_client.rotate_key(s, settings, pair.nst.ctx.identity, pair.nst.http, pair.upstream(s))
        s.commit()
    with pair.nsession() as s:
        row = pair.upstream(s)
        root_id = link_client.link_node_id(row)
        current = link_client._key(settings, row.link_key)
        previous = link_client._key(settings, row.link_key_previous)
        epoch, link_id = row.key_epoch, row.link_id
        stored = list(row.link_rotations)
    other = Ed25519PrivateKey.generate()
    # the stolen previous key signs another successor at seq 1: same length prefix differs -> a fork
    fork = [lc.rotation_certificate(previous, root_id, 1, lc.public_b64(other)),
            lc.rotation_certificate(other, root_id, 2, lc.public_b64(Ed25519PrivateKey.generate()))]
    parts = lc.rotation_parts(MAIN, link_id, root_id, fork[1]["body"]["key_id"], epoch + 1)
    r = _api(pair, "POST", "/api/v2/link/rotation", body={"rotations": fork, "new_key_pop": "x"})
    assert r.status_code == 409 and r.json()["error"]["reason"] == "fork"
    with pair.msession() as s:
        assert pair.link_row(s).status == "frozen"
        assert s.scalars(select(Proposal)).one().link_flag == "link_compromised"
    assert _api(pair, "GET", "/api/v2/link").status_code == 401
    # the owner relinks from the node (a new key appended), with fresh authentication: back on probation
    with pair.nsession() as s:
        pair.upstream(s).status = "remote_revoked"
        s.commit()
    view = pair.start(relink=True)
    assert pair.approve(view).status_code == 303 and pair.poll() == "linked"
    with pair.msession() as s:
        lr = pair.link_row(s)
        assert lr.status == "active" and lr.trust == "probation" and lr.key_epoch == epoch + 1
    assert _api(pair, "GET", "/api/v2/link").status_code == 200
    _ = (current, stored, parts)


def test_superseded_key_rotation_attempt_freezes(pair):
    pair.link()
    settings = pair.nst.ctx.settings
    with pair.nsession() as s:
        row = pair.upstream(s)
        old_key = link_client._key(settings, row.link_key)
        old_access = link_client._open(settings, row.access_enc)
        link_client.rotate_key(s, settings, pair.nst.ctx.identity, pair.nst.http, row)
        s.commit()
    r = _api(pair, "POST", "/api/v2/link/rotation", key=old_key, access=old_access,
             body={"rotations": [], "new_key_pop": "x"})
    assert r.status_code == 401
    with pair.msession() as s:
        assert pair.link_row(s).status == "frozen"


class _LoseAnswer:
    """Forwards a request to the main, then loses the answer (`times` times) for requests whose URL matches."""

    def __init__(self, real, match, times=1, status=None, description=None):
        self.real, self.match, self.left, self.status, self.description = real, match, times, status, description

    def request(self, method, url, **kw):
        import httpx

        if self.match in url and self.left > 0:
            self.left -= 1
            if self.status is not None:  # answer with a canned refusal instead of forwarding
                return httpx.Response(self.status, json={"error": "invalid_token",
                                                         "error_description": self.description})
            self.real.request(method, url, **kw)
            raise httpx.ReadTimeout("answer lost")
        return self.real.request(method, url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


def test_lost_rotation_answer_is_retried_not_frozen(pair):
    """Review fix: the main committed the rotation but the node never saw the answer. Pressing rotate again
    repeats the identical rotation; the main recognises it and issues tokens for the new key, no freeze."""
    pair.link()
    same_game_on_node(pair)
    pair.propose()
    settings = pair.nst.ctx.settings
    lossy = _LoseAnswer(pair.nst.http, "/api/v2/link/rotation")
    with pair.nsession() as s:
        with pytest.raises(link_client.LinkClientError, match="retried"):
            link_client.rotate_key(s, settings, pair.nst.ctx.identity, lossy, pair.upstream(s))
        s.commit()
    with pair.msession() as s:
        assert pair.link_row(s).key_epoch == 2  # the main moved
    with pair.nsession() as s:
        row = pair.upstream(s)
        assert row.status == "linked" and link_client.pending_rotation(row) is not None and row.key_epoch == 1
        with pytest.raises(link_client.LinkClientError, match="rotation is waiting"):
            link_client.call(s, settings, pair.nst.http, row, "GET", "/api/v2/link")
        # sending waits (nothing is sent with the old key, nothing is marked revoked)
        assert link_client.send(s, settings, pair.nst.ctx.identity, pair.nst.http, row, force=True)["sent"] == 0
        assert row.status == "linked"
        s.rollback()
    # the worker finishes the rotation before sending anything (the same as pressing the button again)
    link_client.round_(pair.nst.maker, pair.nst.ctx, pair.nst.http)
    with pair.nsession() as s:
        row = pair.upstream(s)
        assert row.status == "linked" and link_client.pending_rotation(row) is None and row.key_epoch == 2
        assert len(row.link_rotations) == 1
    with pair.msession() as s:
        lr = pair.link_row(s)
        assert lr.status == "active" and lr.key_epoch == 2
    assert _api(pair, "GET", "/api/v2/link").status_code == 200
    assert pair.send()["sent"] == 1  # the queue was re-signed with the new key and goes through


def test_rotation_answer_that_does_not_check_out_is_retried(pair, monkeypatch):
    pair.link()
    settings = pair.nst.ctx.settings
    real = link_client.main_key
    calls = {"n": 0}

    def flaky(http, row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise link_client.LinkClientError("main key unavailable")
        return real(http, row)

    monkeypatch.setattr(link_client, "main_key", flaky)
    with pair.nsession() as s:
        with pytest.raises(link_client.LinkClientError, match="did not check out"):
            link_client.rotate_key(s, settings, pair.nst.ctx.identity, pair.nst.http, pair.upstream(s))
        s.commit()
    with pair.nsession() as s:  # the admin presses the button again
        link_client.rotate_key(s, settings, pair.nst.ctx.identity, pair.nst.http, pair.upstream(s))
        s.commit()
    with pair.msession() as s:
        assert pair.link_row(s).status == "active"
    assert _api(pair, "GET", "/api/v2/link").json()["key_epoch"] == 2


def test_rotation_retry_needs_the_identical_rotation(pair):
    """A repeat with another successor (what a thief holding the old key would send) still freezes."""
    pair.link()
    settings = pair.nst.ctx.settings
    with pair.nsession() as s:
        row = pair.upstream(s)
        old_key = link_client._key(settings, row.link_key)
        old_access = link_client._open(settings, row.access_enc)
        root_id, link_id = link_client.link_node_id(row), row.link_id
    with pair.nsession() as s:
        link_client.rotate_key(s, settings, pair.nst.ctx.identity, pair.nst.http, pair.upstream(s))
        s.commit()
    other = Ed25519PrivateKey.generate()
    cert = lc.rotation_certificate(old_key, root_id, 1, lc.public_b64(other))
    parts = lc.rotation_parts(MAIN, link_id, root_id, cert["body"]["key_id"], 2)
    r = _api(pair, "POST", "/api/v2/link/rotation", key=old_key, access=old_access,
             body={"rotations": [cert], "new_key_pop": lc.sign_pop(other, parts)})
    assert r.status_code == 401
    with pair.msession() as s:
        assert pair.link_row(s).status == "frozen"


@pytest.mark.parametrize("description", ["stale_proof", "replayed_proof", "malformed_proof", "links_off"])
def test_transient_401_keeps_the_link_and_queue(pair, description):
    """Review fix: clock skew or a bad proof is retried; it never ends the link or the queue."""
    pair.link()
    same_game_on_node(pair)
    cid = pair.propose()
    canned = _LoseAnswer(pair.nst.http, "/api/v2/link", status=401, description=description)
    with pair.nsession() as s:
        with pytest.raises(link_client.LinkClientError):
            link_client.call(s, pair.nst.ctx.settings, canned, pair.upstream(s), "GET", "/api/v2/link")
        s.commit()
    with pair.nsession() as s:
        assert pair.upstream(s).status == "linked"
        assert s.get(UpstreamContribution, cid).state == "queued"
    assert pair.send()["sent"] == 1


def test_link_refusal_ends_the_link(pair):
    pair.link()
    same_game_on_node(pair)
    cid = pair.propose()
    canned = _LoseAnswer(pair.nst.http, "/api/v2/link", status=401, description="link_revoked")
    with pair.nsession() as s:
        with pytest.raises(link_client.LinkGone):
            link_client.call(s, pair.nst.ctx.settings, canned, pair.upstream(s), "GET", "/api/v2/link")
        s.commit()
    with pair.nsession() as s:
        assert pair.upstream(s).status == "remote_revoked"
        assert s.get(UpstreamContribution, cid).state == "link_revoked"


def test_expired_token_is_refreshed_not_revoked(pair):
    pair.link()
    with pair.msession() as s:  # the main ended the access token early (the node still thinks it is fresh)
        for row in s.scalars(select(OidcAccess).where(OidcAccess.link_id.is_not(None))):
            row.expires_at = now() - timedelta(seconds=1)
        s.commit()
    with pair.nsession() as s:
        r = link_client.call(s, pair.nst.ctx.settings, pair.nst.http, pair.upstream(s), "GET", "/api/v2/link")
        s.commit()
    assert r.status_code == 200
    with pair.nsession() as s:
        assert pair.upstream(s).status == "linked"


# ---- §7.5 limits (T-K11, T-K25) -----------------------------------------------------------------------------

def test_link_rate_limits_per_identity(pair):
    pair.link()
    same_game_on_node(pair)
    with pair.msession() as s:
        pair.link_row(s).limits = {"per_minute": 2, "per_day": 200, "max_pending": 50}
        s.commit()
    ids = [pair.propose({"developer": f"Dev {i}"}) for i in range(4)]
    out = pair.send()
    assert out["sent"] == 2 and out["waiting"] == 2  # the first retryable refusal stops the batch
    with pair.nsession() as s:
        states = [(s.get(UpstreamContribution, i).state, s.get(UpstreamContribution, i).last_error) for i in ids]
    assert states[2] == ("queued", "rate_limited") and states[3][0] == "queued"
    r = _api(pair, "GET", "/api/v2/link").json()
    assert r["used_today"] == 2 and r["pending"] == 2


def test_link_aggregate_limits(tmp_path):
    """T-K25: `link-main` (contributions per day across every link) and `link-main-new` (new links per day)."""
    with Pair(tmp_path, main_kw={"link_main_daily": 1, "link_main_new_daily": 1}) as w:
        w.link()
        same_game_on_node(w)
        w.propose()
        w.propose({"publisher": "X"})
        out = w.send()
        assert out["sent"] == 1 and out["waiting"] == 1
        with w.msession() as s:
            from atlas.models import NodeState

            assert s.get(NodeState, "link_main_alarm") is not None
        signup(w.main, "erin")
        age(w.m_app, "erin")
        with w.nsession() as s:
            link_client.unlink(s, w.nst.ctx.settings, w.nst.ctx.identity, w.nst.http, w.upstream(s))
            s.commit()
        view = w.start()
        r = w.approve(view)
        assert r.status_code == 400 and "Too many links today" in r.text


# ---- H65 name mapping (T-K19), H68 feed.access (T-K14) ----------------------------------------------------------

def test_link_name_mapping_both_modes(pair):
    pair.link()
    with pair.msession() as s:
        lr = pair.link_row(s)
        dave = _user(s, "dave")
        assert link.label(lr, dave).startswith("a linked node (") and "dave" not in link.label(lr, dave)
        link_id = lr.id
    t = token(pair.main, "/me/links")
    pair.main.post(f"/me/links/{link_id}/name", data={"csrf": t, "show": "on"})
    with pair.msession() as s:
        lr, dave = pair.link_row(s), _user(s, "dave")
        assert link.label(lr, dave) == "dave (via node NAS Atlas)"
        assert dave.email is None or dave.email not in link.label(lr, dave)
    assert _api(pair, "GET", "/api/v2/link").json()["preferred_username"] == "dave"
    pair.main.post(f"/me/links/{link_id}/name", data={"csrf": t})
    assert "preferred_username" not in _api(pair, "GET", "/api/v2/link").json()
    pair.main.post(f"/me/links/{link_id}/name", data={"csrf": t, "show": "on"})
    pair.main.post(f"/me/links/{link_id}/unlink", data={"csrf": t})
    with pair.msession() as s:
        assert pair.link_row(s).show_name is False  # unlinking deletes the mapping


def test_feed_access_right_allows_own_sync_tokens(pair):
    """T-K14 (H68, §7.6): `feed.access` is per account, granted by a local admin; a link carries no sync."""
    with pair.msession() as s:
        dave = _user(s, "dave")
        row, secret = auth.create_token(s, "mine", ["read", "sync"])
        row.user_id = dave.id
        s.commit()
        assert "sync" not in auth.authenticate(s, f"Bearer {secret}").scopes
        scope.grant(s, dave, scope.FEED, None)
        s.commit()
        assert "sync" in auth.authenticate(s, f"Bearer {secret}").scopes
    pair.link()
    with pair.msession() as s:
        access = s.scalars(select(OidcAccess).where(OidcAccess.link_id.is_not(None))).first()
        assert "sync" not in access.scope


# ---- the node's pages (T-K13) and the lab: two nodes through the web ----------------------------------------------

def test_link_qr_svg_and_fingerprints_shown(pair):
    t = token(pair.node, "/admin/main")
    probe = pair.node.post("/admin/main/probe", data={"csrf": t, "url": MAIN})
    with pair.msession() as s:
        main_id = pair.mst.ctx.identity.node_id
        _ = s
    assert probe.status_code == 200 and link.fingerprint(main_id) in probe.text
    r = pair.node.post("/admin/main/start", data={"csrf": t, "url": MAIN, "node_id": main_id})
    assert r.status_code == 303
    page = pair.node.get("/admin/main")
    assert "<svg" in page.text and "data-user-code" in page.text and "data-confirm-code" in page.text
    assert page.headers["cache-control"] == "no-store"
    with pair.nsession() as s:
        row = pair.upstream(s)
        assert link.fingerprint(link_client.link_node_id(row)) in page.text
        assert row.link_root_public != pair.nst.ctx.identity.public_b64  # a per-main key, not the node key
        assert link_client.link_node_id(row) != pair.nst.ctx.identity.node_id
    wrong = pair.node.post("/admin/main/start", data={"csrf": t, "url": MAIN, "node_id": "0" * 16})
    assert wrong.status_code == 400 and "another key" in wrong.text


def test_lab_two_nodes_link_propose_review_and_fold(pair):
    """The lab: a node links to a main through the web pages on both sides, proposes an edit and a new record,
    the main reviews them, and the node folds the decisions back (alias for the new record)."""
    same_game_on_node(pair)
    t = token(pair.node, "/admin/main")
    main_id = pair.mst.ctx.identity.node_id
    pair.node.post("/admin/main/probe", data={"csrf": t, "url": MAIN})
    assert pair.node.post("/admin/main/start", data={"csrf": t, "url": MAIN, "node_id": main_id}).status_code == 303
    page = pair.node.get("/admin/main").text
    user_code = re.search(r"data-user-code>([A-Z]{4}-[A-Z]{4})<", page).group(1)
    confirm = re.search(r"data-confirm-code>([A-Z]{4}-[A-Z]{4})<", page).group(1)
    assert pair.approve({"user_code": user_code, "confirm": confirm}, show_name=True).status_code == 303
    assert pair.node.post(f"/admin/main/{main_id}/poll", data={"csrf": t}).status_code == 303
    assert 'data-upstream-status="linked"' in pair.node.get("/admin/main").text
    # an edit on the node, proposed to the main with the "Where this edit goes" control
    form = pair.node.get(f"/game/{pair.gid}/edit")
    assert "Where this edit goes" in form.text and "Central Atlas" in form.text
    from .test_web import token as tok

    et = tok(pair.node, f"/game/{pair.gid}/edit")
    r = pair.node.post(f"/game/{pair.gid}/edit", data={"csrf": et, "title": "Hades", "developer": "Supergiant Games",
                                                       "release_date": "2020-09-17", "upstream": "on"})
    assert r.status_code == 303, r.text[:400]
    with pair.nsession() as s:
        row = pair.upstream(s)
        local_new = "atlas:" + "0" * 25 + "1"
        link_client.propose(s, pair.nst.ctx.settings, pair.nst.ctx.identity, row, kind="new_game",
                            local_cid=local_new, payload={"title": "Hades II", "media_type": "game"})
        s.commit()
    assert pair.node.post(f"/admin/main/{main_id}/send", data={"csrf": t}).status_code == 303
    pair.main.cookies.clear()
    from .test_web import login

    login(pair.main, "boss")
    review = pair.main.get("/review")
    assert "via node NAS Atlas" in review.text and "on probation" in review.text
    _approve_all(pair)
    pair.pull()
    with pair.nsession() as s:
        states = {c.kind: c.state for c in s.scalars(select(UpstreamContribution))}
        assert states == {"edit": "approved", "new_game": "approved"}
        alias = s.get(UpstreamAlias, local_new)
        assert alias is not None and alias.main_cid.startswith("atlas:")
    with pair.msession() as s:
        from atlas.models import Canonical

        assert s.get(Canonical, pair.gid).view["developer"] == "Supergiant Games"
    assert "approved 2" in pair.node.get("/admin/main").text


def test_auto_push_queues_locally_approved_proposals(tmp_path):
    with Pair(tmp_path, node_kw={"link_push": "auto", "auto_approve_reputation": 0}) as w:
        w.link()
        same_game_on_node(w)
        signup(w.node, "local")
        with w.nsession() as s:
            local = _user(s, "local")
            p = proposals.create(s, w.nst.ctx, local, "edit", w.gid, {"developer": "Local Dev"})
            proposals.review(s, w.nst.ctx, _user(s, "nodeadmin"), p.id, approve=True)
            s.commit()
            c = s.scalars(select(UpstreamContribution)).one()
            assert c.state == "queued" and c.body["payload"] == {"developer": "Local Dev"}


def test_artwork_upload_not_proposed(pair):
    pair.link()
    same_game_on_node(pair)
    with pair.nsession() as s, pytest.raises(link_client.LinkClientError, match="not available yet"):
        link_client.propose(s, pair.nst.ctx.settings, pair.nst.ctx.identity, pair.upstream(s), kind="artwork",
                            local_cid=pair.gid, payload={"items": [{"url": "http://192.168.1.2/a.png"}]})


def test_link_secrets_sealed_and_rewrapped(pair):
    from atlas import secrets_box
    from atlas.models import NodeState
    from atlas.signing import ENC_PREFIX

    pair.link()
    with pair.nsession() as s:
        row = pair.upstream(s)
        for value in (row.link_key, row.access_enc, row.refresh_enc):
            assert value.startswith(ENC_PREFIX)
        counts = secrets_box.rewrap(s, pair.nst.ctx.settings)
        assert counts["link_secrets"] >= 3
        s.rollback()
    with pair.msession() as s:
        assert s.get(NodeState, "link_pairwise_key").value.startswith(ENC_PREFIX)


def test_purge_keeps_revocation_record(pair):
    pair.link()
    with pair.msession() as s:
        lr = pair.link_row(s)
        link.revoke(s, pair.mst.ctx, lr, by=None, reason="test")
        lr.revoked_at = now() - timedelta(days=91)
        s.commit()
        link.purge(s)
        s.commit()
        lr = pair.link_row(s)
        assert lr.status == "revoked" and lr.name == "" and lr.node_id


_ = base64


def test_node_only_shows_the_issuers_own_link_page():
    """Review fix: the QR code and link on /admin/main never send the admin's phone to another origin."""
    ok = link_client._own_link_page
    assert ok(MAIN, f"{MAIN}/link") and ok(MAIN, f"{MAIN}/link?code=BCDF-GHJK") and ok(MAIN + "/", f"{MAIN}/link")
    for bad in ["https://evil.example/link", f"{MAIN}.evil.example/link", f"{MAIN}/linkx", f"{MAIN}/login",
                f"{MAIN}@evil.example/link", f"{MAIN}/link?code=x#@evil", "javascript:alert(1)", "",
                f"{MAIN}/link?code=a\nb"]:
        assert not ok(MAIN, bad), bad


def test_start_refuses_a_foreign_verification_uri(pair, monkeypatch):
    real = pair.nst.http

    class Rewrite(_LoseAnswer):
        def request(self, method, url, **kw):
            import httpx

            resp = self.real.request(method, url, **kw)
            if url.endswith("/oidc/device") and resp.status_code == 200:
                body = {**resp.json(), "verification_uri_complete": "https://evil.example/link?code=X"}
                return httpx.Response(200, json=body)
            return resp

    with pair.nsession() as s:
        with pytest.raises(link_client.LinkClientError, match="not its own /link page"):
            link_client.start(s, pair.nst.ctx.settings, pair.nst.ctx.identity, Rewrite(real, ""), MAIN)
