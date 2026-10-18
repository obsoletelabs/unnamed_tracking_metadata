"""Regression tests for the second independent review (community, tokens, media)."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import accounts, cli, main, media, proposals, service
from atlas.models import Canonical, Token, User
from atlas.service import AtlasError, Ctx
from atlas.settings import get_settings
from atlas.signing import load_identity

from .conftest import batch, build_settings
from .test_community import PW, user
from .test_web import fresh, login, signup, site, token  # noqa: F401


@pytest.fixture
def game(session, ctx):
    [r] = service.ingest(session, ctx, batch("igdb", [{"external_id": "1", "title": "Hades",
                                                       "release_date": "2020-09-17"}]), system=True)
    session.commit()
    return r.canonical_id


def view(session, cid):
    session.flush()
    session.expire_all()
    return session.get(Canonical, cid).view


def test_personal_tokens_cannot_write_even_if_scopes_were_widened(site):  # noqa: F811
    signup(site, "alice")
    t = token(site, "/me")
    r = site.post("/me/tokens", data={"csrf": t, "name": "app", "scope": "read,contribute"})
    secret = re.search(r"(atl_[A-Za-z0-9_-]+)", r.text).group(1)
    with site.st.maker() as s:
        row = s.scalars(select(Token).where(Token.name == "app")).one()
        assert row.scopes == ["read"]
        row.scopes = ["read", "contribute", "curate"]  # e.g. an old row or a hand edit
        s.commit()
    body = {"connection": {"source_kind": "plugin", "source": "spam", "source_scope": "x"},
            "observations": [{"external_id": "1", "title": "Spam Game"}]}
    api = {"Authorization": "Bearer " + secret}
    assert site.post("/api/v2/observations", json=body, headers=api).status_code == 403
    assert site.get("/api/v2/games?q=hades", headers=api).status_code == 200


def test_suspending_a_user_kills_their_tokens(site):  # noqa: F811
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    t = token(bob, "/me")
    secret = re.search(r"(atl_[A-Za-z0-9_-]+)", bob.post("/me/tokens", data={"csrf": t, "name": "n"}).text).group(1)
    api = {"Authorization": "Bearer " + secret}
    assert site.get("/api/v2/games?q=x", headers=api).status_code == 200
    with site.st.maker() as s:
        uid = s.scalars(select(User).where(User.username == "bob")).one().id
        bob_user = s.get(User, uid)
        bob_user.status = "suspended"  # without going through set_status: the token must still be refused
        s.commit()
    assert site.get("/api/v2/games?q=x", headers=api).status_code == 401
    with site.st.maker() as s:
        s.get(User, uid).status = "active"
        s.commit()
        admin = s.scalars(select(User).where(User.username == "alice")).one()
        accounts.set_status(s, admin, s.get(User, uid), "suspended")
        s.commit()
        assert s.scalars(select(Token).where(Token.user_id == uid)).one().revoked_at is not None


def test_self_approved_edits_earn_no_reputation_and_are_capped_daily(session, game):
    s = build_settings(trusted_daily_proposals=3, auto_approve_reputation=50)
    c = Ctx(s, load_identity(session))
    user(session, s, "alice")
    farmer = user(session, s, "bob", rep=50)
    for i in range(3):
        proposals.create(session, c, farmer, "edit", game, {"title": "AB"[i % 2] + " game"})
    assert farmer.reputation == 50  # nothing earned without a reviewer
    with pytest.raises(AtlasError, match="daily"):
        proposals.create(session, c, farmer, "edit", game, {"title": "C game"})
    reviewer = user(session, s, "dave")
    reviewer.role = "curator"
    for i in range(5):  # reviewers are not capped
        proposals.create(session, c, reviewer, "edit", game, {"series": f"S{i}"})


def test_reviewer_approval_still_rewards_and_reverting_costs_only_what_was_earned(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    p = proposals.create(session, ctx, bob, "edit", game, {"developer": "Dev"})
    proposals.review(session, ctx, admin, p.id, approve=True)
    assert bob.reputation == proposals.REWARD["edit"]
    proposals.revert(session, ctx, admin, p.id)
    assert bob.reputation == proposals.REWARD["edit"] - proposals.REWARD["edit"] + proposals.REVERT_PENALTY


def test_revert_guard_follows_order_of_application_not_submission(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    a = user(session, ctx.settings, "bob")
    trusted = user(session, ctx.settings, "carol", rep=100)
    p1 = proposals.create(session, ctx, a, "edit", game, {"developer": "ADev"})  # submitted first, pending
    p2 = proposals.create(session, ctx, trusted, "edit", game, {"series": "Trusted"})  # applied at once
    proposals.review(session, ctx, admin, p1.id, approve=True)  # applied after p2
    with pytest.raises(AtlasError, match="newer"):
        proposals.revert(session, ctx, admin, p2.id)
    assert view(session, game)["developer"] == "ADev"
    proposals.revert(session, ctx, admin, p1.id)  # newest first works
    proposals.revert(session, ctx, admin, p2.id)


def test_proposals_that_could_never_be_approved_are_refused_up_front(session, game):
    s = build_settings(community_art_cap=3)
    c = Ctx(s, load_identity(session))
    user(session, s, "alice")
    bob = user(session, s, "bob", rep=100)
    items = [{"kind": "screenshot", "url": f"https://x.test/{i}.png"} for i in range(3)]
    proposals.create(session, c, bob, "artwork", game, {"items": items})
    with pytest.raises(AtlasError, match="limited to 3"):
        proposals.create(session, c, bob, "artwork", game, {"items": [{"kind": "logo", "url": "https://x.test/l.png"}]})


def test_a_stranger_cannot_lock_a_user_out(site):  # noqa: F811
    signup(site, "alice")
    attacker = TestClient(site.st_app, base_url="http://localhost:8000", follow_redirects=False,
                          client=("6.6.6.6", 5555))
    codes = [login(attacker, "alice", "wrong wrong wrong").status_code for _ in range(12)]
    assert 429 in codes
    victim = TestClient(site.st_app, base_url="http://localhost:8000", follow_redirects=False,
                        client=("7.7.7.7", 5555))
    assert login(victim, "alice").status_code == 303  # the real user, from elsewhere, still gets in


@pytest.mark.parametrize("address", ["64:ff9b::7f00:1", "64:ff9b::a00:1", "2002:7f00:1::1", "::ffff:127.0.0.1",
                                     "2001:0:4136:e378:8000:63bf:3fff:fdd2", "fe80::1", "10.0.0.1", "169.254.169.254"])
def test_tunnelled_and_private_addresses_are_not_public(address):
    assert not media._public(address)


def test_public_addresses_still_pass():
    assert media._public("93.184.216.34") and media._public("2606:2800:220:1:248:1893:25c8:1946")


def test_cache_does_not_use_environment_proxies(monkeypatch, tmp_path, maker):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    seen = {}
    import httpx
    real = httpx.Client

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(media.httpx, "Client", spy)
    media.cache_pending(maker, build_settings(media_dir=str(tmp_path)))
    assert seen.get("trust_env") is False


def test_initial_admin_setting_decides_who_administers(session):
    s = build_settings(initial_admin="Boss")
    first = accounts.register(session, s, "mallory", PW)
    boss = accounts.register(session, s, "boss", PW)
    assert first.role == "user" and boss.role == "admin"


def test_production_with_open_registration_needs_an_initial_admin(tmp_path):
    with pytest.raises(ValueError, match="INITIAL_ADMIN"):
        build_settings(environment="production", public_url="https://x.test", role="local",
                       database_url=f"sqlite:///{tmp_path}/p.db")
    build_settings(environment="production", public_url="https://x.test", role="local", initial_admin="boss",
                   database_url=f"sqlite:///{tmp_path}/p.db")
    build_settings(environment="production", public_url="https://x.test", role="local", registration="closed",
                   database_url=f"sqlite:///{tmp_path}/p.db")


def test_make_admin_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ATLAS_DATABASE_URL", f"sqlite:///{tmp_path}/m.db")
    get_settings.cache_clear()
    cli.main(["migrate"])
    app = main.create_app(get_settings())
    with app.state.atlas.maker() as s:
        accounts.register(s, get_settings(), "alice", PW)
        accounts.register(s, get_settings(), "bob", PW)
        s.commit()
    assert cli.main(["make-admin", "Bob"]) == 0 and cli.main(["make-admin", "nobody"]) == 1
    with app.state.atlas.maker() as s:
        assert s.scalars(select(User).where(User.username == "bob")).one().role == "admin"
    get_settings.cache_clear()
