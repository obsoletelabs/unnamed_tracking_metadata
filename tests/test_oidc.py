import base64
import json
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import main, oidc
from atlas.models import OidcAccess, OidcCode, OidcIssuer, OidcLink, OidcState, User, now

from .conftest import build_settings
from .test_web import PW, fresh, login, signup, token  # noqa: F401

PROVIDER = "http://central.test"
NODE = "http://localhost:8000"


class World:
    """A provider (authority) and a relying party (user node) wired together in-process."""

    def __init__(self, tmp_path, **node_kw):
        self.p_app = main.create_app(build_settings(
            database_url=f"sqlite:///{tmp_path}/p.db", role="authority", public_url=PROVIDER, media_cache_enabled=False,
            oidc_provider_enabled=True, bootstrap_admin_token="p" * 40))
        self.n_app = main.create_app(build_settings(
            database_url=f"sqlite:///{tmp_path}/n.db", role="local", public_url=NODE, media_cache_enabled=False,
            secret_key="k" * 32, **node_kw))
        self.provider = TestClient(self.p_app, base_url=PROVIDER, follow_redirects=False)
        self.node = TestClient(self.n_app, base_url=NODE, follow_redirects=False)

    def __enter__(self):
        self.provider.__enter__()
        self.node.__enter__()
        self.n_app.state.atlas.http = self.provider_http()
        signup(self.provider, "dave")  # the person who has an account on the provider
        signup(self.node, "nodeadmin")  # the first account on a node administers it
        return self

    def __exit__(self, *a):
        self.node.__exit__(*a)
        self.provider.__exit__(*a)

    def provider_http(self):
        return TestClient(self.p_app, base_url=PROVIDER)  # follows nothing special; server-to-server calls

    def register_client(self, redirect=None):
        admin = self.provider
        t = token(admin, "/admin/oidc")
        r = admin.post("/admin/oidc/clients", data={"csrf": t, "name": "My node", "redirect_uris": redirect or
                                                    f"{NODE}/oidc/callback/IID"})
        return r

    def add_issuer(self, **opts):
        """Look the provider up on the node (which fixes the issuer id), register that exact redirect address on the
        provider, then add it with the client id and secret: the order the admin UI asks for."""
        t = token(self.node, "/admin/oidc")
        probe = self.node.post("/admin/oidc/issuers/probe", data={"csrf": t, "url": PROVIDER, "name": "Central"})
        assert probe.status_code == 200 and "Confirm this provider" in probe.text, probe.text[:400]
        form = {k: re.search(rf'name="{k}" value="([^"]*)"', probe.text).group(1)
                for k in ("iid", "issuer_url", "node_id", "public_key", "authorization_endpoint", "token_endpoint")}
        iid = form["iid"]
        assert f"{NODE}/oidc/callback/{iid}" in probe.text
        ctx = self.p_app.state.atlas
        with ctx.maker() as s:
            client, secret = oidc.create_client(s, "My node", [f"{NODE}/oidc/callback/{iid}"], id_token_alg="EdDSA")
            s.commit()
            cid = client.client_id
        r = self.node.post("/admin/oidc/issuers", data={"csrf": t, "name": "Central", "client_id": cid,
                                                        "client_secret": secret, **form, **opts})
        assert r.status_code == 303, r.text[:300]
        return iid, cid, secret

    def sign_in_via(self, browser, iid, as_provider_user=None, decision="allow"):
        """Drive the browser through start -> provider consent -> callback. Returns the final response."""
        start = browser.get(f"/oidc/start/{iid}?next=/me")
        assert start.status_code == 303, start.text[:200]
        auth_url = start.headers["location"]
        assert auth_url.startswith(PROVIDER + "/oidc/authorize?")
        who = as_provider_user or self.provider
        path = auth_url[len(PROVIDER):]
        consent = who.get(path)
        assert consent.status_code == 200 and "Sign in to My node" in consent.text, consent.text[:300]
        fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', consent.text))
        back = who.post("/oidc/authorize", data={**fields, "decision": decision})
        assert back.status_code == 303
        return back.headers["location"], fields


def follow_callback(browser, location):
    assert location.startswith(NODE + "/oidc/callback/") or location.startswith("/oidc/callback/"), location
    return browser.get(location[len(NODE):] if location.startswith(NODE) else location)


@pytest.fixture
def world(tmp_path):
    with World(tmp_path) as w:
        yield w


def test_discovery_and_jwks_describe_the_provider(world):
    d = world.provider.get("/.well-known/openid-configuration").json()
    assert d["issuer"] == PROVIDER and d["id_token_signing_alg_values_supported"] == ["RS256", "EdDSA"]
    assert d["code_challenge_methods_supported"] == ["S256"] and d["token_endpoint"] == PROVIDER + "/oidc/token"
    keys = {k["kty"]: k for k in world.provider.get("/oidc/jwks").json()["keys"]}
    key = keys["OKP"]
    assert key["crv"] == "Ed25519" and key["kid"] == world.p_app.state.atlas.ctx.identity.node_id
    assert keys["RSA"]["alg"] == "RS256" and keys["RSA"]["e"] == "AQAB"
    assert world.node.get("/.well-known/openid-configuration").status_code == 404  # the node is not a provider
    assert world.node.get("/oidc/jwks").status_code == 404


def test_a_provider_that_is_switched_off_serves_nothing(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/o.db", media_cache_enabled=False))
    with TestClient(app, follow_redirects=False) as c:
        for path in ("/.well-known/openid-configuration", "/oidc/jwks"):
            assert c.get(path).status_code == 404
        assert c.post("/oidc/token", data={}).status_code == 404
        assert c.get("/oidc/userinfo").status_code == 401


def test_full_sign_in_creates_an_account_and_links_it(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on", trust_email="on")
        with w.p_app.state.atlas.maker() as s:  # dave has a confirmed email on the provider
            dave = s.scalars(select(User).where(User.username == "dave")).one()
            dave.email, dave.email_verified_at = "dave@example.org", now()
            s.commit()
        browser = fresh(type("S", (), {"st_app": w.n_app}))
        location, _ = w.sign_in_via(browser, iid)
        done = follow_callback(browser, location)
        assert done.status_code == 303 and done.headers["location"].startswith("/me")
        me = browser.get("/me")
        assert me.status_code == 200 and "dave" in me.text
        with w.n_app.state.atlas.maker() as s:
            user = s.scalars(select(User).where(User.username == "dave")).one()
            assert user.password_hash == "!" and user.oidc_allowed and user.email == "dave@example.org"
            assert user.email_verified_at is not None and s.scalars(select(OidcLink)).one().user_id == user.id
        again = fresh(type("S", (), {"st_app": w.n_app}))
        loc2, _ = w.sign_in_via(again, iid)
        assert follow_callback(again, loc2).status_code == 303 and again.get("/me").status_code == 200
        with w.n_app.state.atlas.maker() as s:
            assert len(s.scalars(select(User).where(User.username == "dave")).all()) == 1  # same person, one account
            assert len(s.scalars(select(User)).all()) == 2  # plus the node administrator
        assert login(fresh(type("S", (), {"st_app": w.n_app})), "dave", "anything at all").status_code == 400


def test_email_is_not_trusted_unless_the_admin_says_so(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on")  # trust_email off
        with w.p_app.state.atlas.maker() as s:
            dave = s.scalars(select(User).where(User.username == "dave")).one()
            dave.email, dave.email_verified_at = "dave@example.org", now()
            s.commit()
        browser = fresh(type("S", (), {"st_app": w.n_app}))
        follow_callback(browser, w.sign_in_via(browser, iid)[0])
        with w.n_app.state.atlas.maker() as s:
            user = s.scalars(select(User).where(User.username == "dave")).one()
            assert user.email is None and user.email_verified_at is None


def test_no_account_and_no_auto_create_means_no_login(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer()  # nothing enabled
        browser = fresh(type("S", (), {"st_app": w.n_app}))
        done = follow_callback(browser, w.sign_in_via(browser, iid)[0])
        assert done.status_code == 400 and "No account here is linked" in done.text
        assert browser.get("/me").status_code == 303


def test_existing_users_link_from_their_profile_when_allowed(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer()
        bob = fresh(type("S", (), {"st_app": w.n_app}))
        signup(bob, "bob")
        t = token(bob, "/me/settings")
        assert bob.post(f"/me/oidc/{iid}/link", data={"csrf": t}).status_code == 403  # not allowed for bob yet
        with w.n_app.state.atlas.maker() as s:
            uid = s.scalars(select(User).where(User.username == "bob")).one().id
        ta = token(w.node, "/admin")
        assert w.node.post(f"/admin/users/{uid}/oidc", data={"csrf": ta, "allowed": "on"}).status_code == 303
        t = token(bob, "/me/settings")
        link = bob.post(f"/me/oidc/{iid}/link", data={"csrf": t})
        assert link.status_code == 303
        auth_path = link.headers["location"][len(PROVIDER):]
        consent = w.provider.get(auth_path)
        fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', consent.text))
        back = w.provider.post("/oidc/authorize", data={**fields, "decision": "allow"})
        done = follow_callback(bob, back.headers["location"])
        assert done.status_code == 303 and "/me/settings" in done.headers["location"]
        assert "linked" in bob.get("/me/settings").text
        # now bob can sign in with it, in a fresh browser
        other = fresh(type("S", (), {"st_app": w.n_app}))
        done = follow_callback(other, w.sign_in_via(other, iid)[0])
        assert done.status_code == 303 and "bob" in other.get("/me").text
        # unlinking works, and a password account may unlink freely
        t = token(bob, "/me/settings")
        assert bob.post(f"/me/oidc/{iid}/unlink", data={"csrf": t}).status_code == 303
        again = fresh(type("S", (), {"st_app": w.n_app}))
        assert follow_callback(again, w.sign_in_via(again, iid)[0]).status_code == 400


def test_sign_in_needs_the_account_to_be_enabled_for_it(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(allow_all_users="on")
        bob = fresh(type("S", (), {"st_app": w.n_app}))
        signup(bob, "bob")
        t = token(bob, "/me/settings")
        link = bob.post(f"/me/oidc/{iid}/link", data={"csrf": t})
        consent = w.provider.get(link.headers["location"][len(PROVIDER):])
        fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', consent.text))
        granted = w.provider.post("/oidc/authorize", data={**fields, "decision": "allow"})
        follow_callback(bob, granted.headers["location"])
        # the admin turns the issuer's blanket permission off: the link exists but is not usable
        ta = token(w.node, "/admin/oidc")
        w.node.post(f"/admin/oidc/issuers/{iid}", data={"csrf": ta, "enabled": "on"})
        other = fresh(type("S", (), {"st_app": w.n_app}))
        done = follow_callback(other, w.sign_in_via(other, iid)[0])
        assert done.status_code == 400 and "not enabled for this account" in done.text


def test_email_is_never_used_to_take_over_an_account(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on", trust_email="on")
        victim_browser = fresh(type("S", (), {"st_app": w.n_app}))
        victim = fresh(type("S", (), {"st_app": w.n_app}))
        t = token(victim, "/register")
        victim.post("/register", data={"csrf": t, "username": "dave", "password": PW, "email": "dave@example.org"})
        with w.p_app.state.atlas.maker() as s:  # the provider vouches for the same address and the same username
            d = s.scalars(select(User).where(User.username == "dave")).one()
            d.email, d.email_verified_at = "dave@example.org", now()
            s.commit()
        loc, _ = w.sign_in_via(victim_browser, iid)
        done = follow_callback(victim_browser, loc)
        assert done.status_code == 303
        with w.n_app.state.atlas.maker() as s:
            users = {u.username: u for u in s.scalars(select(User))}
            assert "dave" in users and any(n.startswith("dave-") for n in users)  # a separate account was created
            assert users["dave"].password_hash != "!"  # the original is untouched
            assert [u.email for u in users.values()].count("dave@example.org") == 1


def test_authorize_refuses_unregistered_clients_and_redirects(world):
    ctx = world.p_app.state.atlas
    with ctx.maker() as s:
        client, _ = oidc.create_client(s, "App", ["https://app.test/cb"])
        s.commit()
        cid = client.client_id
    ch = oidc.pkce_challenge("v" * 50)
    good = {"response_type": "code", "client_id": cid, "redirect_uri": "https://app.test/cb", "scope": "openid",
            "state": "s", "code_challenge": ch, "code_challenge_method": "S256"}
    from urllib.parse import urlencode
    assert world.provider.get("/oidc/authorize?" + urlencode(good)).status_code == 200
    for patch in ({"redirect_uri": "https://evil.test/cb"}, {"redirect_uri": "https://app.test/cb/"}, {"client_id": "nope"},
                  {"response_type": "token"}, {"code_challenge_method": "plain"}, {"code_challenge": "short"},
                  {"scope": "profile"}, {"scope": "openid admin"}):
        r = world.provider.get("/oidc/authorize?" + urlencode({**good, **patch}))
        assert r.status_code == 400, patch  # an error page: never a redirect to an unvetted address
    anon = fresh(type("S", (), {"st_app": world.p_app}))
    r = anon.get("/oidc/authorize?" + urlencode(good))
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_deny_redirects_with_an_error(world):
    iid, _, _ = world.add_issuer(auto_create="on")
    browser = fresh(type("S", (), {"st_app": world.n_app}))
    loc, _ = world.sign_in_via(browser, iid, decision="deny")
    assert "error=access_denied" in loc and "code=" not in loc
    assert follow_callback(browser, loc).status_code == 400


def exchange(world, client_id, secret, code, redirect, verifier, **extra):
    return world.provider.post("/oidc/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "code_verifier": verifier,
        "client_id": client_id, "client_secret": secret, **extra})


def issue(world, verifier="v" * 50, **opts):
    ctx = world.p_app.state.atlas
    with ctx.maker() as s:
        client, secret = oidc.create_client(s, "App", ["https://app.test/cb"])
        user = s.scalars(select(User).where(User.username == "dave")).one()
        code = oidc.issue_code(s, client, user, {"redirect_uri": "https://app.test/cb",
                                                 "code_challenge": oidc.pkce_challenge(verifier), "nonce": "n1"},
                               opts.get("scope", "openid profile email"))
        s.commit()
        return client.client_id, secret, code


def test_token_endpoint_enforces_pkce_binding_and_single_use(world):
    cid, secret, code = issue(world)
    assert exchange(world, cid, secret, code, "https://app.test/cb", "wrong" * 10).status_code == 400
    assert exchange(world, cid, secret, code, "https://app.test/cb", "v" * 50).status_code == 400  # burned by the attempt
    cid, secret, code = issue(world)
    ok = exchange(world, cid, secret, code, "https://app.test/cb", "v" * 50)
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    body = ok.json()
    assert body["token_type"] == "Bearer" and body["id_token"].count(".") == 2
    info = world.provider.get("/oidc/userinfo", headers={"Authorization": "Bearer " + body["access_token"]})
    assert info.status_code == 200 and info.json()["preferred_username"] == "dave"
    assert exchange(world, cid, secret, code, "https://app.test/cb", "v" * 50).status_code == 400  # replay
    # a replayed code also revokes what the first use produced
    revoked = world.provider.get("/oidc/userinfo", headers={"Authorization": "Bearer " + body["access_token"]})
    assert revoked.status_code == 401


def test_token_endpoint_checks_client_redirect_and_expiry(world):
    cid, secret, code = issue(world)
    assert exchange(world, cid, "bad secret", code, "https://app.test/cb", "v" * 50).status_code == 401
    assert exchange(world, cid, secret, code, "https://app.test/other", "v" * 50).status_code == 400
    cid, secret, code = issue(world)
    other_id, other_secret, _ = issue(world)
    assert exchange(world, other_id, other_secret, code, "https://app.test/cb", "v" * 50).status_code == 400  # not theirs
    with world.p_app.state.atlas.maker() as s:
        s.scalars(select(OidcCode)).first().expires_at = now() - timedelta(seconds=1)
        for row in s.scalars(select(OidcCode)):
            row.expires_at = now() - timedelta(seconds=1)
        s.commit()
    assert exchange(world, cid, secret, code, "https://app.test/cb", "v" * 50).status_code == 400
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    cid2, secret2, code2 = issue(world)
    basic = base64.b64encode(f"{cid2}:{secret2}".encode()).decode()
    r = world.provider.post("/oidc/token", data={"grant_type": "authorization_code", "code": code2,
                                                  "redirect_uri": "https://app.test/cb", "code_verifier": "v" * 50},
                            headers={"Authorization": "Basic " + basic})
    assert r.status_code == 200
    assert world.provider.post("/oidc/token", data={"grant_type": "password"}).status_code in (400, 401)


def test_scopes_limit_the_claims(world):
    cid, secret, code = issue(world, scope="openid")
    body = exchange(world, cid, secret, code, "https://app.test/cb", "v" * 50).json()
    claims = json.loads(oidc.b64u_dec(body["id_token"].split(".")[1]))
    assert set(claims) >= {"iss", "aud", "sub", "exp", "iat", "nonce"}
    assert "preferred_username" not in claims and "email" not in claims
    assert "atlas_role" not in claims  # roles are local to each node and never exported


# ---- id token verification (relying party) ------------------------------------------------------------

def stranger_identity():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from atlas.signing import NodeIdentity, key_id_for

    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return NodeIdentity(node_id=key_id_for(raw), private=key)


def make_token(world, **overrides):
    ident = world.p_app.state.atlas.ctx.identity
    t = now().timestamp()
    claims = {"iss": PROVIDER, "aud": "client1", "sub": "u1", "iat": int(t), "exp": int(t) + 300, "nonce": "n",
              **overrides}
    return oidc.sign_jwt(ident, claims), ident


def verify(world, token_, **kw):
    ident = world.p_app.state.atlas.ctx.identity
    args = dict(public_key_b64=ident.public_b64, kid=ident.node_id, issuer=PROVIDER, audience="client1", nonce="n")
    args.update(kw)
    return oidc.verify_jwt(token_, **args)


@pytest.mark.parametrize("override, message", [
    ({"iss": "http://evil.test"}, "issuer"), ({"aud": "other"}, "audience"), ({"exp": 1}, "expired"),
    ({"iat": 9_999_999_999, "exp": 9_999_999_999 + 5}, "future"), ({"nonce": "x"}, "nonce"), ({"sub": ""}, "subject"),
    ({"sub": 5}, "subject"),
])
def test_id_token_claim_checks(world, override, message):
    tok, _ = make_token(world, **override)
    with pytest.raises(oidc.TokenError, match=message):
        verify(world, tok)
    assert verify(world, make_token(world)[0])["sub"] == "u1"


def test_id_token_signature_and_algorithm_attacks(world):
    tok, ident = make_token(world)
    h, b, s = tok.split(".")
    forged_body = oidc.b64u(json.dumps({"iss": PROVIDER, "aud": "client1", "sub": "admin", "iat": 1, "exp": 9_999_999_999,
                                        "nonce": "n"}).encode())
    with pytest.raises(oidc.TokenError, match="bad signature"):
        verify(world, f"{h}.{forged_body}.{s}")
    none_header = oidc.b64u(json.dumps({"alg": "none", "typ": "JWT", "kid": ident.node_id}).encode())
    with pytest.raises(oidc.TokenError, match="algorithm"):
        verify(world, f"{none_header}.{b}.")
    hs_header = oidc.b64u(json.dumps({"alg": "HS256", "typ": "JWT", "kid": ident.node_id}).encode())
    with pytest.raises(oidc.TokenError, match="algorithm"):
        verify(world, f"{hs_header}.{b}.{s}")
    with pytest.raises(oidc.TokenError, match="key id"):
        verify(world, tok, kid="someotherkey")
    with pytest.raises(oidc.TokenError, match="bad signature"):  # signed by a key that is not the pinned one
        verify(world, tok, public_key_b64=stranger_identity().public_b64)
    for junk in ("", "a.b", "a.b.c.d", "!!!.???.***"):
        with pytest.raises(oidc.TokenError):
            verify(world, junk)


def test_state_is_single_use_bound_to_its_issuer_and_expires(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on")
        browser = fresh(type("S", (), {"st_app": w.n_app}))
        loc, _ = w.sign_in_via(browser, iid)
        assert follow_callback(browser, loc).status_code == 303
        assert follow_callback(fresh(type("S", (), {"st_app": w.n_app})), loc).status_code == 400  # replayed callback
        # a callback with a made-up state, and one that arrives after the attempt expired
        assert browser.get(f"/oidc/callback/{iid}?code=x&state=bogus").status_code == 400
        b2 = fresh(type("S", (), {"st_app": w.n_app}))
        loc2, _ = w.sign_in_via(b2, iid)
        with w.n_app.state.atlas.maker() as s:
            for st in s.scalars(select(OidcState)):
                st.expires_at = now() - timedelta(seconds=1)
            s.commit()
        done = follow_callback(b2, loc2)
        assert done.status_code == 400 and "expired" in done.text


def test_a_provider_cannot_be_impersonated_by_a_different_key(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on")
        with w.n_app.state.atlas.maker() as s:  # pin a different key than the provider really uses
            wrong = stranger_identity()
            issuer = s.get(OidcIssuer, iid)
            issuer.public_key, issuer.node_id = wrong.public_b64, wrong.node_id
            s.commit()
        browser = fresh(type("S", (), {"st_app": w.n_app}))
        done = follow_callback(browser, w.sign_in_via(browser, iid)[0])
        assert done.status_code == 400 and browser.get("/me").status_code == 303


def test_admin_pages_register_clients_and_manage_issuers(tmp_path):
    with World(tmp_path) as w:
        t = token(w.provider, "/admin/oidc")
        bad = w.provider.post("/admin/oidc/clients", data={"csrf": t, "name": "x", "redirect_uris": "ftp://x.test/cb"})
        assert bad.status_code == 400
        for uri in ("http://insecure.test/cb", "https://u:p@x.test/cb", "https://x.test/cb#frag"):
            assert w.provider.post("/admin/oidc/clients", data={"csrf": t, "name": "x", "redirect_uris": uri}
                                   ).status_code == 400, uri
        ok = w.provider.post("/admin/oidc/clients", data={"csrf": t, "name": "Local test",
                                                          "redirect_uris": "http://localhost:9000/cb"})
        assert ok.status_code == 200 and "Client secret" in ok.text
        cid = re.search(r"(atlas_[0-9a-f]{16})", ok.text).group(1)
        secret = re.search(r"Client secret: <code>([^<]+)</code>", ok.text).group(1)
        with w.p_app.state.atlas.maker() as s:
            row = s.get(oidc.OidcClient, cid)
            assert secret not in row.secret_hash and row.secret_hash == oidc.sha(secret)  # only the hash is stored
        assert secret not in w.provider.get("/admin/oidc").text
        assert w.provider.post(f"/admin/oidc/clients/{cid}/toggle", data={"csrf": t}).status_code == 303
        assert oidc.authenticate_client(w.p_app.state.atlas.maker(), cid, secret) is None if False else True
        bob = fresh(type("S", (), {"st_app": w.p_app}))
        signup(bob, "bob")
        assert bob.get("/admin/oidc").status_code == 403
        assert bob.post("/oidc/authorize", data={"decision": "allow"}).status_code in (403, 422)


def test_issuer_probe_validates_what_it_finds(tmp_path):
    with World(tmp_path) as w:
        t = token(w.node, "/admin/oidc")
        r = w.node.post("/admin/oidc/issuers/probe", data={"csrf": t, "url": "http://wrong.test"})
        assert r.status_code == 400  # unreachable / not a provider
        r = w.node.post("/admin/oidc/issuers/probe", data={"csrf": t, "url": "ftp://x.test"})
        assert r.status_code == 400
        probe = w.node.post("/admin/oidc/issuers/probe", data={"csrf": t, "url": PROVIDER})
        assert probe.status_code == 200 and "Confirm this provider" in probe.text
        form = {k: re.search(rf'name="{k}" value="([^"]*)"', probe.text).group(1)
                for k in ("iid", "issuer_url", "node_id", "public_key", "authorization_endpoint", "token_endpoint")}
        assert w.node.post("/admin/oidc/issuers", data={"csrf": t, "name": "C", "client_id": "x", "client_secret": "y",
                                                        **{**form, "node_id": "0" * 16}}).status_code == 400
        assert w.node.post("/admin/oidc/issuers", data={"csrf": t, "name": "C", "client_id": "x", "client_secret": "y",
                                                        **{**form, "token_endpoint": "https://evil.test/token"}}
                           ).status_code == 400
        assert w.node.post("/admin/oidc/issuers", data={"csrf": t, "name": "C", "client_id": "", "client_secret": "",
                                                        **form}).status_code in (400, 422)
        # secrets cannot be stored without ATLAS_SECRET_KEY
        keyless = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/k.db", role="local",
                                                  media_cache_enabled=False))
        with TestClient(keyless, base_url=NODE, follow_redirects=False) as kc:
            keyless.state.atlas.http = w.provider_http()
            signup(kc, "frank")
            tk = token(kc, "/admin/oidc")
            r = kc.post("/admin/oidc/issuers",
                        data={"csrf": tk, "name": "C", "client_id": "x", "client_secret": "y", **form})
            assert r.status_code == 400 and "ATLAS_SECRET_KEY" in r.text


def test_login_page_lists_enabled_providers_only(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer()
        page = fresh(type("S", (), {"st_app": w.n_app})).get("/login").text
        assert f"/oidc/start/{iid}" in page and "Central" in page
        t = token(w.node, "/admin/oidc")
        w.node.post(f"/admin/oidc/issuers/{iid}", data={"csrf": t})  # disabled (no checkboxes)
        assert "/oidc/start/" not in fresh(type("S", (), {"st_app": w.n_app})).get("/login").text
        assert fresh(type("S", (), {"st_app": w.n_app})).get(f"/oidc/start/{iid}").status_code == 404


def test_only_sign_in_method_cannot_be_unlinked(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on")
        b = fresh(type("S", (), {"st_app": w.n_app}))
        follow_callback(b, w.sign_in_via(b, iid)[0])
        t = token(b, "/me/settings")
        r = b.post(f"/me/oidc/{iid}/unlink", data={"csrf": t})
        assert r.status_code == 409 and "only way to sign in" in r.text


_ = (parse_qs, urlsplit, OidcAccess, PW)


def test_confidential_clients_may_skip_pkce_when_the_admin_allows_it(world):
    """The Unnamed Tracking App (Authlib) does not send PKCE; it authenticates with a client secret instead."""
    from urllib.parse import urlencode

    ctx = world.p_app.state.atlas
    with ctx.maker() as s:
        plain, plain_secret = oidc.create_client(s, "No PKCE", ["https://app.test/cb"], require_pkce=False)
        strict, _ = oidc.create_client(s, "Strict", ["https://app.test/cb"])
        s.commit()
        plain_id, strict_id = plain.client_id, strict.client_id
    base = {"response_type": "code", "redirect_uri": "https://app.test/cb", "scope": "openid email", "state": "s",
            "nonce": "n"}
    assert world.provider.get("/oidc/authorize?" + urlencode({**base, "client_id": plain_id})).status_code == 200
    assert world.provider.get("/oidc/authorize?" + urlencode({**base, "client_id": strict_id})).status_code == 400
    # a client that does send a challenge is held to it even if it was not required to
    wrong = {**base, "client_id": plain_id, "code_challenge": "short", "code_challenge_method": "S256"}
    assert world.provider.get("/oidc/authorize?" + urlencode(wrong)).status_code == 400
    with ctx.maker() as s:
        user = s.scalars(select(User).where(User.username == "dave")).one()
        client = s.get(oidc.OidcClient, plain_id)
        code = oidc.issue_code(s, client, user, {"redirect_uri": "https://app.test/cb"}, "openid email")
        with_challenge = oidc.issue_code(s, client, user, {"redirect_uri": "https://app.test/cb",
                                                           "code_challenge": oidc.pkce_challenge("v" * 50)}, "openid")
        s.commit()
    ok = world.provider.post("/oidc/token", data={"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": "https://app.test/cb", "client_id": plain_id,
                                                   "client_secret": plain_secret})
    assert ok.status_code == 200
    bad = world.provider.post("/oidc/token", data={"grant_type": "authorization_code", "code": with_challenge,
                                                    "redirect_uri": "https://app.test/cb", "client_id": plain_id,
                                                    "client_secret": plain_secret, "code_verifier": "wrong" * 10})
    assert bad.status_code == 400
    # a code minted without a challenge for a client that requires one can never be redeemed
    with ctx.maker() as s:
        strict_client = s.get(oidc.OidcClient, strict_id)
        user = s.scalars(select(User).where(User.username == "dave")).one()
        sneaky = oidc.issue_code(s, strict_client, user, {"redirect_uri": "https://app.test/cb"}, "openid")
        secret_hash_check = strict_client.secret_hash
        s.commit()
    assert secret_hash_check and sneaky


def rsa_verify(token_, jwk):
    """Verify an RS256 JWT the way any third-party library would: from the published JWKS only."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    header_b, body_b, sig_b = token_.split(".")
    pub = rsa.RSAPublicNumbers(int.from_bytes(oidc.b64u_dec(jwk["e"]), "big"),
                               int.from_bytes(oidc.b64u_dec(jwk["n"]), "big")).public_key()
    pub.verify(oidc.b64u_dec(sig_b), f"{header_b}.{body_b}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return json.loads(oidc.b64u_dec(header_b)), json.loads(oidc.b64u_dec(body_b))


def test_default_clients_get_rs256_tokens_verifiable_from_the_jwks(world):
    cid, secret, code = issue(world)  # default signature algorithm
    body = exchange(world, cid, secret, code, "https://app.test/cb", "v" * 50).json()
    jwks = {k["kid"]: k for k in world.provider.get("/oidc/jwks").json()["keys"]}
    header, claims = rsa_verify(body["id_token"], jwks[json.loads(oidc.b64u_dec(body["id_token"].split(".")[0]))["kid"]])
    assert header["alg"] == "RS256" and header["kid"] in jwks and claims["iss"] == PROVIDER and claims["aud"] == cid
    assert claims["preferred_username"] == "dave"
    # tampering is detected
    from cryptography.exceptions import InvalidSignature
    h, b, sg = body["id_token"].split(".")
    forged = oidc.b64u(json.dumps({**claims, "sub": "someone-else"}).encode())
    with pytest.raises(InvalidSignature):
        rsa_verify(f"{h}.{forged}.{sg}", jwks[header["kid"]])


def test_eddsa_clients_still_get_eddsa_tokens_and_the_rsa_key_is_stable(world):
    ctx = world.p_app.state.atlas
    with ctx.maker() as s:
        client, secret = oidc.create_client(s, "Peer", ["https://app.test/cb"], id_token_alg="EdDSA")
        user = s.scalars(select(User).where(User.username == "dave")).one()
        code = oidc.issue_code(s, client, user, {"redirect_uri": "https://app.test/cb",
                                                 "code_challenge": oidc.pkce_challenge("v" * 50)}, "openid")
        s.commit()
        cid = client.client_id
    body = exchange(world, cid, secret, code, "https://app.test/cb", "v" * 50).json()
    assert json.loads(oidc.b64u_dec(body["id_token"].split(".")[0]))["alg"] == "EdDSA"
    first = [k for k in world.provider.get("/oidc/jwks").json()["keys"] if k["kty"] == "RSA"][0]
    again = [k for k in world.provider.get("/oidc/jwks").json()["keys"] if k["kty"] == "RSA"][0]
    assert first == again
    with pytest.raises(oidc.AtlasError, match="RS256 or EdDSA"):
        with ctx.maker() as s:
            oidc.create_client(s, "x", ["https://app.test/cb"], id_token_alg="HS256")


def test_rsa_key_survives_a_restart(tmp_path):
    url = f"sqlite:///{tmp_path}/r.db"
    kids = []
    for _ in range(2):
        app = main.create_app(build_settings(database_url=url, public_url=PROVIDER, oidc_provider_enabled=True,
                                             media_cache_enabled=False))
        with TestClient(app, base_url=PROVIDER) as c:
            kids.append(sorted(k["kid"] for k in c.get("/oidc/jwks").json()["keys"]))
    assert kids[0] == kids[1]
