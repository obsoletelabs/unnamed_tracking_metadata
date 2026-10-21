import re
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import main, service
from atlas.models import Proposal, Token, User

from .conftest import batch, build_settings

PW = "correct horse battery"


@pytest.fixture
def site(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/w.db", bootstrap_admin_token="b" * 40,
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False,
                       public_url="http://localhost:8000")
    app = main.create_app(s)
    st = app.state.atlas
    with st.maker() as sess:
        [r] = service.ingest(sess, st.ctx, batch("igdb", [{"external_id": "1", "title": "Hades",
                             "release_date": "2020-09-17", "facts": {"developer": "Wrong Dev", "tags": ["Action"]}}]),
                             system=True)
        sess.commit()
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        c.gid = r.canonical_id
        c.st = st
        c.st_app = app
        yield c


def token(client, path="/login") -> str:
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def signup(client, name, pw=PW):
    c = client
    t = token(c, "/register")
    r = c.post("/register", data={"csrf": t, "username": name, "password": pw})
    assert r.status_code == 303, r.text
    return r


def fresh(site):
    return TestClient(site.st_app, base_url="http://localhost:8000", follow_redirects=False)


def login(client, name, pw=PW):
    t = token(client)
    r = client.post("/login", data={"csrf": t, "username": name, "password": pw})
    return r


def csrf_in(client, path):
    return token(client, path)


def test_public_pages_render_without_login(site):
    home = site.get("/")
    assert home.status_code == 200 and "Hades" in home.text
    assert "Hades" in site.get("/search?q=hades").text
    page = site.get(f"/game/{site.gid}")
    assert page.status_code == 200 and "Wrong Dev" in page.text and "Where this came from" in page.text
    assert "Sign in to suggest an edit" in page.text
    assert site.get("/game/atlas:nope").status_code == 404
    assert site.get("/robots.txt").text.startswith("User-agent")


def test_security_headers_and_no_inline_script(site):
    r = site.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "<script>" not in r.text and " onclick=" not in r.text
    for path in ("/", f"/game/{site.gid}", "/login"):
        assert "style=" not in site.get(path).text, path  # inline styles would be blocked by the CSP
    assert "content-security-policy" not in site.get("/api/v2/health").headers


def test_registration_login_logout_flow(site):
    signup(site, "alice")  # first account is the administrator
    page = site.get("/me")
    assert page.status_code == 200 and "alice" in page.text and "admin" in page.text
    cookie = site.cookies.get("atlas_session")
    assert cookie
    t = token(site, "/me")
    out = site.post("/logout", data={"csrf": t})
    assert out.status_code == 303
    assert site.get("/me").status_code == 303  # back to the login page
    r = login(site, "alice")
    assert r.status_code == 303 and site.cookies.get("atlas_session") != cookie


def test_session_cookie_flags(site):
    r = signup(site, "alice")
    header = [h for h in r.headers.get_list("set-cookie") if h.startswith("atlas_session")][0].lower()
    assert "httponly" in header and "samesite=lax" in header and "secure" not in header  # http test origin


def test_wrong_password_and_throttle(site):
    signup(site, "alice")
    other = fresh(site)
    bad = login(other, "alice", "not the password")
    assert bad.status_code == 400 and "Wrong username or password" in bad.text
    codes = [login(other, "alice", "nope nope nope").status_code for _ in range(12)]
    assert 429 in codes


def test_open_redirect_is_blocked(site):
    signup(site, "alice")
    other = fresh(site)
    t = token(other)
    r = other.post("/login", data={"csrf": t, "username": "alice", "password": PW, "next": "//evil.example/x"})
    assert r.headers["location"].split("?")[0] == "/"
    other = fresh(site)
    t = token(other)
    r = other.post("/login", data={"csrf": t, "username": "alice", "password": PW, "next": "/game/x"})
    assert r.headers["location"].startswith("/game/x")


def test_csrf_is_required_on_every_post(site):
    signup(site, "alice")
    for path in ("/logout", "/new", f"/game/{site.gid}/edit", "/me/tokens", "/me/password", "/admin/users/x",
                 f"/game/{site.gid}/merge", "/review/xyz"):
        assert site.post(path, data={"title": "x"}).status_code in (403, 422), path
        assert site.post(path, data={"csrf": "wrong", "title": "x"}).status_code in (403, 422), path
    anon = fresh(site)
    assert anon.post("/login", data={"username": "alice", "password": PW}).status_code in (403, 422)


def test_edit_proposal_review_and_reputation(site):
    signup(site, "alice")  # admin
    bob = fresh(site)
    signup(bob, "bob")
    t = csrf_in(bob, f"/game/{site.gid}/edit")
    r = bob.post(f"/game/{site.gid}/edit", data={"csrf": t, "title": "Hades", "developer": "Supergiant Games",
                                                  "release_date": "2020-09-17", "tags": "Action\nRoguelike",
                                                  "note": "store page"})
    assert r.status_code == 303 and "m=proposed" in r.headers["location"]
    assert "Supergiant" not in site.get(f"/game/{site.gid}").text  # not published yet
    assert bob.get("/review").status_code == 403
    queue = site.get("/review")
    assert "Supergiant Games" in queue.text and "Wrong Dev" in queue.text and "store page" in queue.text
    pid = re.search(r'action="/review/([0-9A-Z]{26})"', queue.text).group(1)
    t = token(site, "/review")
    assert site.post(f"/review/{pid}", data={"csrf": t, "decision": "approve", "note": "ok"}).status_code == 303
    page = site.get(f"/game/{site.gid}").text
    assert "Supergiant Games" in page and "Roguelike" in page and "Recent changes" in page
    assert "reputation <strong>5</strong>" in bob.get("/me").text
    # reviewers see curation tools, others do not
    assert "Merge into another record" in site.get(f"/game/{site.gid}").text
    assert "Merge into another record" not in bob.get(f"/game/{site.gid}").text


def test_unchanged_and_invalid_edits_show_errors(site):
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    t = csrf_in(bob, f"/game/{site.gid}/edit")
    same = bob.post(f"/game/{site.gid}/edit", data={"csrf": t, "title": "Hades", "developer": "Wrong Dev"})
    assert same.status_code == 400 and "did not change anything" in same.text
    bad = bob.post(f"/game/{site.gid}/edit", data={"csrf": t, "release_date": "17/09/2020"})
    assert bad.status_code == 400 and "YYYY-MM-DD" in bad.text


def test_user_input_is_escaped(site):
    signup(site, "alice")
    t = csrf_in(site, "/new")
    payload = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    r = site.post("/new", data={"csrf": t, "title": payload, "developer": payload, "note": payload})
    assert r.status_code == 303
    gid = r.headers["location"].split("?")[0].split("/game/")[1]
    page = site.get(f"/game/{gid}").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page and "<img src=x" not in page
    assert unescape(page).count(payload) >= 1


def test_new_game_by_admin_applies_and_by_user_waits(site):
    signup(site, "alice")
    t = csrf_in(site, "/new")
    r = site.post("/new", data={"csrf": t, "title": "Brand New", "release_date": "2024-02-02",
                                "provider_ids": "steam:424242", "platforms": "pc"})
    assert r.headers["location"].startswith("/game/atlas:") and "m=applied" in r.headers["location"]
    bob = fresh(site)
    signup(bob, "bob")
    t = csrf_in(bob, "/new")
    r = bob.post("/new", data={"csrf": t, "title": "Pending Game"})
    assert r.headers["location"].startswith("/me")
    assert "Pending Game" not in site.get("/search?q=pending").text


def test_artwork_proposal_url_validation_and_media_only_after_cache(site):
    signup(site, "alice")
    t = csrf_in(site, f"/game/{site.gid}")
    bad = site.post(f"/game/{site.gid}/artwork", data={"csrf": t, "kind": "key_art", "url": "javascript:alert(1)"})
    assert bad.status_code == 400
    ok = site.post(f"/game/{site.gid}/artwork", data={"csrf": t, "kind": "key_art", "url": "https://img.test/c.png"})
    assert ok.status_code == 303
    page = site.get(f"/game/{site.gid}").text
    assert "https://img.test/c.png" not in page  # never hotlinked: only cached images are displayed
    assert "1 image known, 0 cached" in page


def test_permission_boundaries(site):
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    anon = fresh(site)
    for client, path, want in ((anon, "/me", 303), (anon, "/new", 303), (anon, "/admin", 303), (bob, "/admin", 403),
                               (bob, "/review", 403), (bob, "/review/duplicates", 403)):
        assert client.get(path).status_code == want, (path, want)
    t = token(bob, "/me")
    assert bob.post(f"/game/{site.gid}/merge", data={"csrf": t, "into": "atlas:x"}).status_code == 403
    assert bob.post("/admin/users/anything", data={"csrf": t, "role": "admin", "status": "active"}).status_code == 403


def test_admin_manages_roles_and_suspension(site):
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    with site.st.maker() as s:
        uid = s.scalars(select(User).where(User.username == "bob")).one().id
    t = token(site, "/admin")
    r = site.post(f"/admin/users/{uid}", data={"csrf": t, "role": "curator", "status": "active"})
    assert r.status_code == 303 and bob.get("/review").status_code == 200
    site.post(f"/admin/users/{uid}", data={"csrf": t, "role": "curator", "status": "suspended"})
    assert bob.get("/me").status_code == 303  # suspended: session ended
    assert login(fresh(site), "bob").status_code == 400
    page = site.get("/admin").text
    assert "suspended" in page and "set_status" in page


def test_personal_tokens_work_against_the_api_and_are_scoped(site):
    signup(site, "alice")
    t = token(site, "/me")
    r = site.post("/me/tokens", data={"csrf": t, "name": "my app", "scope": "read"})
    secret = re.search(r"(atl_[A-Za-z0-9_-]+)", r.text).group(1)
    api = {"Authorization": "Bearer " + secret}
    assert site.get("/api/v2/games?q=hades", headers=api).status_code == 200
    assert site.post("/api/v2/tokens", json={"name": "x", "scopes": ["read"]}, headers=api).status_code == 403
    assert site.get("/api/v2/review", headers=api).status_code == 403
    with site.st.maker() as s:
        tok = s.scalars(select(Token).where(Token.name == "my app")).one()
        tid = tok.id
    site.post(f"/me/tokens/{tid}/revoke", data={"csrf": t})
    assert site.get("/api/v2/games?q=hades", headers=api).status_code == 401
    other = fresh(site)
    signup(other, "bob")
    t2 = token(other, "/me")
    assert other.post(f"/me/tokens/{tid}/revoke", data={"csrf": t2}).status_code == 404


def test_hidden_achievements_reveal_only_for_signed_in_user_tokens(site):
    admin = {"Authorization": "Bearer " + "b" * 40}
    assert site.put(f"/api/v2/games/{site.gid}/achievements", headers=admin, json={"items": [
        {"name": "Secret Ending", "description": "Spoiler", "hidden": True, "external_id": "S1"},
        {"name": "Warm Up"}]}).status_code == 200
    signup(site, "alice")
    t = token(site, "/me")
    secret = re.search(r"(atl_[A-Za-z0-9_-]+)", site.post(
        "/me/tokens", data={"csrf": t, "name": "app", "scope": "read"}).text).group(1)
    user = {"Authorization": "Bearer " + secret}
    url = f"/api/v2/games/{site.gid}/achievements"
    plain = site.get(url, headers=user).json()
    assert plain["hidden_count"] == 1 and "Secret Ending" not in str(plain) and "Spoiler" not in str(plain)
    hid = next(r["id"] for r in plain["results"] if r["hidden"])
    assert site.get(f"{url}?reveal=all", headers=user).json()["results"][0]["name"] in ("Secret Ending", "Warm Up")
    assert "Secret Ending" in str(site.get(f"{url}?reveal={hid}", headers=user).json())
    assert "Warm Up" in str(site.get(f"{url}?reveal=nope", headers=user).json())
    assert "Secret Ending" not in str(site.get(f"{url}?reveal=nope", headers=user).json())
    # a plain read token (not a person's) cannot reveal; the public page redacts too
    read_only = site.post("/api/v2/tokens", headers=admin, json={"name": "ro", "scopes": ["read"]}).json()["token"]
    assert "Secret Ending" not in str(site.get(f"{url}?reveal=all", headers={"Authorization": "Bearer " + read_only}).json())
    assert "Secret Ending" not in site.get(f"/game/{site.gid}").text


def test_achievement_proposal_lines_accept_extras_in_any_order(site):
    from fastapi import HTTPException

    from atlas.web.routes import parse_achievement_line

    assert parse_achievement_line("Escape | Leave | gold | win condition | 50 | hidden | https://i.test/a.png"
                                  " | https://i.test/b.png") == {
        "name": "Escape", "description": "Leave", "tier": "gold", "kind": "win_condition", "points": 50,
        "hidden": True, "icon_url": "https://i.test/a.png", "locked_icon_url": "https://i.test/b.png"}
    assert parse_achievement_line("Plain") == {"name": "Plain", "description": ""}
    with pytest.raises(HTTPException):
        parse_achievement_line("X | y | platinum")
    signup(site, "alice")
    t = token(site, f"/game/{site.gid}")
    ok = site.post(f"/game/{site.gid}/achievements", data={"csrf": t, "lines": "Escape | Leave | gold | 5"})
    assert ok.status_code == 303
    bad = site.post(f"/game/{site.gid}/achievements", data={"csrf": t, "lines": "Escape | Leave | platinum"})
    assert bad.status_code == 400
    page = site.get(f"/game/{site.gid}")
    assert page.status_code == 200 and "Add achievements" in page.text


def test_password_change_signs_out_other_sessions(site):
    signup(site, "alice")
    second = fresh(site)
    login(second, "alice")
    assert second.get("/me").status_code == 200
    t = token(site, "/me")
    bad = site.post("/me/password", data={"csrf": t, "current": "wrong wrong", "new": "another long password"})
    assert bad.status_code == 400
    ok = site.post("/me/password", data={"csrf": t, "current": PW, "new": "another long password"})
    assert ok.status_code == 303
    assert second.get("/me").status_code == 303 and site.get("/me").status_code == 200
    assert login(fresh(site), "alice", "another long password").status_code == 303


def test_duplicates_queue_merge_and_dismiss(site):
    signup(site, "alice")
    with site.st.maker() as s:
        service.ingest(s, site.st.ctx, batch("gogplugin", [{"external_id": "1", "title": "Hades"}]))
        s.commit()
    q = site.get("/review/duplicates")
    assert "possible duplicate" in q.text and "Hades" in q.text
    rid = re.search(r"/review/duplicates/([0-9A-Z]{26})/merge", q.text).group(1)
    into = re.search(r'name="into" value="(atlas:[0-9a-z]+)"', q.text).group(1)
    t = token(site, "/review/duplicates")
    assert site.post(f"/review/duplicates/{rid}/merge", data={"csrf": t, "into": "atlas:other"}).status_code == 400
    assert site.post(f"/review/duplicates/{rid}/merge", data={"csrf": t, "into": into}).status_code == 303
    assert "No open items" in site.get("/review/duplicates").text


def test_curator_can_lock_and_unlock_fields(site):
    signup(site, "alice")
    t = token(site, f"/game/{site.gid}")
    r = site.post(f"/game/{site.gid}/lock", data={"csrf": t, "field": "developer", "value": "Supergiant Games"})
    assert r.status_code == 303 and "Supergiant Games" in site.get(f"/game/{site.gid}").text
    assert site.post(f"/game/{site.gid}/lock", data={"csrf": t, "field": "bogus", "value": "x"}).status_code == 400
    site.post(f"/game/{site.gid}/unlock", data={"csrf": t, "field": "developer"})
    assert "Wrong Dev" in site.get(f"/game/{site.gid}").text


def test_registration_can_be_closed(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/c.db", registration="closed")
    with TestClient(main.create_app(s), follow_redirects=False) as c:
        assert c.get("/register").status_code == 403
        assert "Create account" not in c.get("/").text


def test_proposals_are_listed_and_can_be_withdrawn(site):
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    t = csrf_in(bob, f"/game/{site.gid}/edit")
    bob.post(f"/game/{site.gid}/edit", data={"csrf": t, "series": "Hades series"})
    me = bob.get("/me").text
    assert "pending" in me
    pid = re.search(r"/proposal/([0-9A-Z]{26})/withdraw", me).group(1)
    assert bob.post(f"/proposal/{pid}/withdraw", data={"csrf": token(bob, "/me")}).status_code == 303
    with site.st.maker() as s:
        assert s.get(Proposal, pid).status == "withdrawn"


def test_changes_are_committed_before_the_response_is_sent(tmp_path):
    """FastAPI runs `yield` dependencies after the response by default; a client that follows a redirect
    immediately would then miss its own write. get_session uses scope="function" to commit first."""
    import asyncio

    s = build_settings(database_url=f"sqlite:///{tmp_path}/race.db", bootstrap_admin_token="r" * 40)
    app = main.create_app(s)
    seen: list[int] = []

    async def call(method, path, body=b"", headers=()):
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                with app.state.atlas.maker() as other:  # a different connection, as the next request would use
                    seen.append(other.scalar(select(func.count()).select_from(User)))

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
                 "raw_path": path.encode(), "query_string": b"", "headers": list(headers), "client": ("1.2.3.4", 1),
                 "server": ("localhost", 80), "scheme": "http", "root_path": ""}
        await app(scope, receive, send)

    async def run():
        async with app.router.lifespan_context(app):
            # fetch a CSRF token the way a browser would, then register
            token = "t" * 30
            body = f"csrf={token}&username=alice&password=correct+horse+battery".encode()
            await call("POST", "/register", body, [(b"content-type", b"application/x-www-form-urlencoded"),
                                                    (b"cookie", f"atlas_csrf={token}".encode()),
                                                    (b"content-length", str(len(body)).encode())])

    asyncio.run(run())
    assert seen == [1], "the new user must be visible to other connections when the response starts"


class _FakeProvider:
    name = "fakeprov"
    redistributable = False
    requests_per_minute = 600

    def search(self, client, query):
        from atlas.providers.base import ProviderResult
        return [ProviderResult(external_id="77", title="Celeste", platform="pc", release_date="2018-01-25")]

    def fetch(self, client, external_id):
        from atlas.providers.base import ProviderResult
        return ProviderResult(external_id="77", title="Celeste", platform="pc", release_date="2018-01-25",
                              provider_ids={"fakeprov": "77"},
                              artwork=[{"kind": "key_art", "url": "https://img.example/celeste.jpg"}])

    def achievements(self, client, external_id):
        return [{"name": "Chapter 1 Cleared", "description": "Reach the summit."}]


def test_curators_can_import_from_a_provider_with_media_and_achievements(site):
    site.st.providers.providers["fakeprov"] = _FakeProvider()
    assert site.get("/import").status_code in (303, 401, 403)  # sign-in required
    signup(site, "alice")
    page = site.get("/import?provider=fakeprov&q=celeste")
    assert page.status_code == 200 and "Celeste" in page.text and 'value="77"' in page.text
    t = token(site, "/import")
    r = site.post("/import", data={"csrf": t, "provider": "fakeprov", "external_id": "77"})
    assert r.status_code == 200, r.text
    assert "Imported" in r.text and "1 artwork, 1 achievements" in r.text, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))[:600]  # noqa: E501
    cid = re.search(r'href="/game/(atlas:[^"]+)"', r.text).group(1)
    game = site.get(f"/game/{cid}").text
    assert "Celeste" in game and "Chapter 1 Cleared" in game
    plain = fresh(site)
    assert plain.post("/import", data={"csrf": "x", "provider": "fakeprov", "external_id": "77"}).status_code in (303, 403)


def test_admin_integrations_page_shows_provider_status_without_secrets(site):
    assert site.get("/admin/integrations").status_code in (303, 401, 403)
    signup(site, "alice")  # the first account is the administrator
    page = site.get("/admin/integrations")
    assert page.status_code == 200
    assert "Steam" in page.text and "Active" in page.text and "Not configured" in page.text
    assert "ATLAS_IGDB_CLIENT_ID" in page.text and "ATLAS_RAWG_API_KEY" in page.text and "GOG" in page.text
    assert 'href="/admin/integrations"' in page.text
    assert "ATLAS_SECRET_KEY" in page.text  # no key configured: the page says why it cannot store credentials


def test_admin_can_store_provider_credentials_encrypted_and_providers_reload(site):
    from sqlalchemy import select

    from atlas import integrations, secrets_box
    from atlas.models import AuditEntry, Setting

    base = site.st.base.model_copy(update={"secret_key": "k" * 40})
    site.st.base = base
    signup(site, "alice")
    assert "igdb" not in site.st.providers.providers
    t = token(site, "/admin/integrations")
    r = site.post("/admin/integrations/igdb", data={"csrf": t, "igdb_client_id": "client-123",
                                                    "igdb_client_secret": "super-secret-value"})
    assert r.status_code == 303, r.text
    with site.st.maker() as s:
        raw = s.get(Setting, integrations.PREFIX + "igdb_client_secret").value
        assert "super-secret-value" not in raw and secrets_box.decrypt(base, raw) == "super-secret-value"
        audit = s.scalars(select(AuditEntry).where(AuditEntry.action == "integration")).one()
        assert audit.detail == {"fields": ["igdb_client_id", "igdb_client_secret"]}
    page = site.get("/admin/integrations")  # refresh picks up the change and rebuilds the providers
    assert "super-secret-value" not in page.text and "client-123" not in page.text
    assert "Stored in the database" in page.text and "igdb" in site.st.providers.providers
    # an empty box keeps the value; the checkbox removes it and the provider goes away
    site.post("/admin/integrations/igdb", data={"csrf": t, "igdb_client_id": "", "igdb_client_secret": ""})
    assert "igdb" in site.st.providers.providers
    site.post("/admin/integrations/igdb", data={"csrf": t, "clear_igdb_client_id": "on", "clear_igdb_client_secret": "on"})
    site.get("/admin/integrations")
    assert "igdb" not in site.st.providers.providers


def test_credentials_cannot_be_stored_without_a_secret_key_or_by_non_admins(site):
    signup(site, "alice")
    t = token(site, "/admin/integrations")
    assert site.post("/admin/integrations/rawg", data={"csrf": t, "rawg_api_key": "abc"}).status_code == 400
    assert site.post("/admin/integrations/nope", data={"csrf": t}).status_code == 404
    plain = fresh(site)
    assert plain.post("/admin/integrations/rawg", data={"csrf": "x", "rawg_api_key": "abc"}).status_code in (303, 401, 403)


def test_known_stores_link_only_safe_ids():
    from atlas import integrations

    assert integrations.store_link("gog", "the_witcher_3") == "https://www.gog.com/game/the_witcher_3"
    assert integrations.store_link("tgdb", "1234") == "https://thegamesdb.net/game.php?id=1234"
    assert integrations.store_link("steam", "../evil") is None and integrations.store_link("itch", "x") is None
    assert integrations.store_link("unknown", "1") is None and integrations.store_name("unknown") == "unknown"
