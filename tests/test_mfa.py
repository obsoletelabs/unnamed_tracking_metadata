import base64
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import accounts, main, mfa, secrets_box
from atlas.models import AuditEntry, User, UserMfa, UserSession, now

from .conftest import build_settings
from .test_web import PW, login, signup, token


@pytest.fixture
def secured(tmp_path, monkeypatch):
    settings = build_settings(database_url=f"sqlite:///{tmp_path}/mfa.db", secret_key="isolated-mfa-test-key",
                              media_dir=str(tmp_path / "media"), media_cache_enabled=False)
    app = main.create_app(settings)
    monkeypatch.setattr(mfa, "_counter", lambda: 100000)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        client.st = app.state.atlas
        client.app = app
        yield client


def post(client, path, **data):
    return client.post(path, data={"csrf": token(client, "/me/security"), **data})


def enrol(client):
    started = post(client, "/me/security/start", password=PW)
    assert started.status_code == 200, started.text
    secret = re.search(r"Setup key: <code>([A-Z2-7]+)</code>", started.text).group(1)
    enabled = post(client, "/me/security/enable", code=mfa.totp_code(secret, 100000))
    assert enabled.status_code == 200, enabled.text
    codes = re.findall(r"<code>([a-f0-9]{5}(?:-[a-f0-9]{5}){3})</code>", enabled.text)
    assert len(codes) == 10
    return secret, codes


@pytest.mark.parametrize(("seconds", "expected"), [(59, "287082"), (1111111109, "081804"),
                          (1111111111, "050471"), (1234567890, "005924"),
                          (2000000000, "279037"), (20000000000, "353130")])
def test_rfc6238_sha1_vectors(seconds, expected):
    key = base64.b32encode(b"12345678901234567890").decode()
    assert mfa.totp_code(key, seconds // 30) == expected


def test_enrolment_encryption_recovery_and_new_signin(secured):
    signup(secured, "alice")
    secret, codes = enrol(secured)
    with secured.st.maker() as session:
        factor = session.scalars(select(UserMfa)).one()
        assert secret not in factor.secret_enc and codes[0] not in str(factor.recovery_hashes)
        assert secrets_box.decrypt(secured.st.ctx.settings, factor.secret_enc) == secret
        assert all(code not in str(row.detail) for row in session.scalars(select(AuditEntry)) for code in codes)
    assert codes[0] not in secured.get("/me/security").text
    other = TestClient(secured.app, base_url="http://localhost:8000", follow_redirects=False)
    assert login(other, "alice").status_code == 303
    for path in ("/me", "/admin", "/me/export.json", "/oidc/authorize", "/me/security"):
        assert other.get(path).headers["location"] == "/mfa"
    # The enrolment time step has already been consumed.
    csrf = token(other, "/mfa")
    assert other.post("/mfa", data={"csrf": csrf, "code": mfa.totp_code(secret, 100000)}).status_code == 400
    old_cookie = other.cookies.get("atlas_session")
    assert other.post("/mfa", data={"csrf": csrf, "code": codes[0]}).status_code == 303
    assert other.cookies.get("atlas_session") != old_cookie
    assert other.get("/me").status_code == 200
    assert "9 recovery codes remain" in other.get("/me/security").text
    third = TestClient(secured.app, base_url="http://localhost:8000", follow_redirects=False)
    login(third, "alice")
    assert third.post("/mfa", data={"csrf": token(third, "/mfa"), "code": codes[0]}).status_code == 400


def test_required_policy_includes_reputation_reviewers_and_blocks_bypass(secured):
    signup(secured, "alice")
    secured.st.ctx.settings.require_mfa_for_privileged = True
    secured.st.base.require_mfa_for_privileged = True
    for path in ("/admin", "/me", "/review", "/me/export.json"):
        assert secured.get(path).headers["location"] == "/me/security"
    enrol(secured)
    assert secured.get("/admin").status_code == 200
    response = post(secured, "/me/security/disable", password=PW, code="000000")
    assert response.status_code == 409
    ordinary = TestClient(secured.app, base_url="http://localhost:8000", follow_redirects=False)
    signup(ordinary, "bob")
    assert ordinary.get("/me").status_code == 200
    with secured.st.maker() as session:
        user = session.scalars(select(User).where(User.username == "bob")).one()
        user.reputation = secured.st.ctx.settings.curator_reputation
        session.commit()
    assert ordinary.get("/review").headers["location"] == "/me/security"


def test_setup_requires_reauthentication_and_is_bound_to_session(secured):
    signup(secured, "alice")
    other = TestClient(secured.app, base_url="http://localhost:8000", follow_redirects=False)
    login(other, "alice")
    assert post(secured, "/me/security/start", password="wrong").status_code == 400
    started = post(secured, "/me/security/start", password=PW)
    secret = re.search(r"Setup key: <code>([A-Z2-7]+)</code>", started.text).group(1)
    assert secret not in other.get("/me/security").text
    assert post(other, "/me/security/enable", code=mfa.totp_code(secret, 100000)).status_code == 400
    with secured.st.maker() as session:
        session.scalars(select(UserMfa)).one().created_at = now() - timedelta(minutes=11)
        session.commit()
    assert post(secured, "/me/security/enable", code=mfa.totp_code(secret, 100000)).status_code == 400


def test_second_factor_limits_and_csrf(secured):
    signup(secured, "alice")
    enrol(secured)
    other = TestClient(secured.app, base_url="http://localhost:8000", follow_redirects=False)
    login(other, "alice")
    assert other.post("/mfa", data={"code": "000000"}).status_code == 403
    csrf = token(other, "/mfa")
    results = [other.post("/mfa", data={"csrf": csrf, "code": "000000"}).status_code for _ in range(12)]
    assert 429 in results


def test_linked_signin_and_password_reset_do_not_bypass_factor(secured):
    signup(secured, "alice")
    enrol(secured)
    with secured.st.maker() as session:
        user = session.scalars(select(User)).one()
        # An OIDC callback creates a primary-authenticated session by the same public helper.
        secret, row = accounts.create_session(session, secured.st.ctx.settings, user)
        assert row.mfa_verified_at is None
        session.commit()
    other = TestClient(secured.app, base_url="http://localhost:8000", follow_redirects=False)
    other.cookies.set("atlas_session", secret)
    assert other.get("/me").headers["location"] == "/mfa"
    with secured.st.maker() as session:
        row = session.scalars(select(UserSession).where(UserSession.id == row.id)).one()
        row.created_at = now() - timedelta(minutes=11)
        session.commit()
    assert other.get("/me").headers["location"].startswith("/login")
    with secured.st.maker() as session:
        assert session.scalars(select(UserMfa)).one().enabled_at is not None


def test_disable_and_recovery_rotation(secured, monkeypatch):
    signup(secured, "alice")
    secret, old_codes = enrol(secured)
    monkeypatch.setattr(mfa, "_counter", lambda: 100001)
    rotated = post(secured, "/me/security/recovery", password=PW, code=mfa.totp_code(secret, 100001))
    codes = re.findall(r"<code>([a-f0-9]{5}(?:-[a-f0-9]{5}){3})</code>", rotated.text)
    assert len(codes) == 10 and not set(codes) & set(old_codes)
    assert post(secured, "/me/security/disable", password=PW, code=old_codes[0]).status_code == 400
    assert post(secured, "/me/security/disable", password=PW, code=codes[0]).status_code == 303
    with secured.st.maker() as session:
        assert session.scalars(select(UserMfa)).first() is None


def test_missing_key_and_lost_key_fail_closed(secured):
    signup(secured, "alice")
    secured.st.ctx.settings.secret_key = None
    assert "ATLAS_SECRET_KEY" in secured.get("/me/security").text
    assert post(secured, "/me/security/start", password=PW).status_code == 400
    secured.st.ctx.settings.secret_key = "isolated-mfa-test-key"
    secret, _ = enrol(secured)
    secured.st.ctx.settings.secret_key = "different-key"
    assert post(secured, "/me/security/disable", password=PW, code=mfa.totp_code(secret, 100001)).status_code == 503


def test_recovery_code_has_one_winner_under_concurrency(secured):
    signup(secured, "alice")
    _, codes = enrol(secured)
    with secured.st.maker() as session:
        user_id = session.scalars(select(User.id)).one()

    def consume(_):
        with secured.st.maker() as session:
            result = mfa.verify(session, secured.st.ctx.settings, user_id, codes[0])
            session.commit()
            return result

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]
