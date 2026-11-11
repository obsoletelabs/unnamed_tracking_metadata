"""CONTRACT-C15 §9-§10 (A24): the client API v1 that external tools (the atlas-connectors) are written against.

`/site.client_api`, `If-None-Match`/304 on a record, `POST /games/batch`, the `/records` alias, the artwork
`primary_by_role` member, `public_read`, the `atlas_client_requests_total` metric and the pairing flow (T-B18). The
client OpenAPI subset, its stability rules and the generated fixtures are in `test_client_api_stability.py`. Every
record here is invented."""

from __future__ import annotations

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import client_api, custom, facets, instruments, main, metrics, pairing, qr, service
from atlas.metrics import REGISTRY
from atlas.models import AuditEntry, NodeState, Token

from .conftest import batch, build_settings
from .test_web import _wait_for_startup_replay, signup

ADMIN = "c" * 40
BASE = "http://localhost:8000"


def H(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make(tmp_path, **kw):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/client.db", bootstrap_admin_token=ADMIN, role="local",
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False, public_url=BASE, **kw)
    app = main.create_app(s)
    return app


@pytest.fixture
def atlas(tmp_path):
    app = _make(tmp_path)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        st = app.state.atlas
        _wait_for_startup_replay(st)
        c.st, c.app = st, app
        r = c.post("/api/v2/tokens", json={"name": "reader", "scopes": ["read"]}, headers=H(ADMIN))
        assert r.status_code == 201, r.text
        c.read = H(r.json()["token"])
        yield c


@pytest.fixture
def clock(monkeypatch):
    """The pairing clock, moved by hand (seconds)."""
    now = {"t": 1_900_000_000.0}
    monkeypatch.setattr(pairing, "clock", lambda: now["t"])
    return now


def add_game(st, ext: str, title: str, **facts) -> str:
    with st.maker() as s:
        [r] = service.ingest(s, st.ctx, batch("igdb", [{"external_id": ext, "title": title,
                                                        "facts": facts or {"developer": "Lumen Works"}}]),
                             system=True)
        s.commit()
    return r.canonical_id


def add_book(st, ext: str, title: str) -> str:
    item = {"external_id": ext, "title": title, "provider_ids": {"fixturebooks": ext},
            "ext": {"book": {"authors": [{"name": "Ada Quillfeather", "role": "author"}]}}}
    with st.maker() as s:
        [r] = service.ingest(s, st.ctx, batch("fixturebooks", [item]), system=True, media_type="book")
        s.commit()
    return r.canonical_id


def set_art(st, cid: str, items: list[dict]) -> None:
    with st.maker() as s:
        facets.set_artwork(s, st.ctx, cid, "curator", items)
        s.commit()


def lock_title(st, cid: str, title: str) -> None:
    with st.maker() as s:
        service.set_lock(s, st.ctx, cid, "title", title, actor="test")
        s.commit()


# ---- /site.client_api (§9.1) -----------------------------------------------------------------------------------

def test_site_advertises_client_api_v1(atlas):
    site = atlas.get("/api/v2/site").json()
    block = site["client_api"]
    assert block["version"] == client_api.VERSION == "1.0"
    assert block["min_supported"] == "1.0"
    assert block["deprecations"] == []
    assert {"books", "pairing", "etag", "batch"} <= set(block["capabilities"])
    assert "public_read" not in block["capabilities"]  # off by default (D15.15)
    assert site["contract"] == 1  # the general /api/v2 marker is unchanged and independent (§9.1 critic)


def test_books_capability_follows_the_book_type(atlas):
    with atlas.st.maker() as s:
        book = next(t for t in custom.types(s) if t.key == "book")
        book.enabled = False
        s.commit()
    assert "books" not in atlas.get("/api/v2/site").json()["client_api"]["capabilities"]


# ---- If-None-Match / 304 (§10.1) --------------------------------------------------------------------------------

def test_record_answers_304_for_a_matching_etag(atlas):
    cid = add_game(atlas.st, "1", "Glassbrook")
    first = atlas.get(f"/api/v2/games/{cid}", headers=atlas.read)
    etag = first.headers["ETag"]
    assert first.status_code == 200 and etag == f'"{cid}-{first.json()["version"]}"'
    for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        again = atlas.get(f"/api/v2/games/{cid}", headers={**atlas.read, "If-None-Match": header})
        assert again.status_code == 304, header
        assert again.content == b"" and again.headers["ETag"] == etag
    assert atlas.get(f"/api/v2/games/{cid}", headers={**atlas.read, "If-None-Match": '"nope"'}).status_code == 200
    # a change to the record moves its version, so the old validator no longer matches
    lock_title(atlas.st, cid, "Glassbrook Remade")
    fresh = atlas.get(f"/api/v2/games/{cid}", headers={**atlas.read, "If-None-Match": etag})
    assert fresh.status_code == 200 and fresh.json()["title"] == "Glassbrook Remade"
    assert fresh.headers["ETag"] != etag


def test_304_needs_a_token_and_follows_redirects(atlas):
    keep = add_game(atlas.st, "2", "Saltmarsh")
    gone = add_game(atlas.st, "3", "Saltmarsh (dup)")
    with atlas.st.maker() as s:
        service.merge_canonicals(s, atlas.st.ctx, gone, keep, actor="test")
        s.commit()
    etag = atlas.get(f"/api/v2/games/{keep}", headers=atlas.read).headers["ETag"]
    old = atlas.get(f"/api/v2/games/{gone}", headers={**atlas.read, "If-None-Match": etag})
    assert old.status_code == 304  # the redirect lands on the same representation
    assert atlas.get(f"/api/v2/games/{keep}", headers={"If-None-Match": etag}).status_code == 401


# ---- POST /games/batch (§10.1) ----------------------------------------------------------------------------------

def test_batch_returns_records_in_request_order_with_redirects(atlas):
    a = add_game(atlas.st, "10", "Ember Hollow")
    b = add_game(atlas.st, "11", "Ember Hollow II")
    c = add_game(atlas.st, "12", "Ember Hollow (dup)")
    with atlas.st.maker() as s:
        service.merge_canonicals(s, atlas.st.ctx, c, a, actor="test")
        s.commit()
    ids = [b, c, "atlas:00000000000000000000000000", a, b, "not-an-id"]
    r = atlas.post("/api/v2/games/batch", json={"ids": ids}, headers=atlas.read)
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [x["requested"] for x in results] == ids
    assert [x["status"] for x in results] == ["ok", "ok", "not_found", "ok", "ok", "not_found"]
    assert results[1]["game"]["id"] == a and results[1]["game"]["redirected_from"] == c
    assert results[2]["game"] is None
    single = atlas.get(f"/api/v2/games/{b}", headers=atlas.read).json()
    assert results[0]["game"] == single  # the same shape as GET /games/{id}, relations included


def test_batch_limits_and_scope(atlas):
    assert atlas.post("/api/v2/games/batch", json={"ids": []}, headers=atlas.read).status_code == 422
    too_many = [f"atlas:{i:026d}" for i in range(201)]
    assert atlas.post("/api/v2/games/batch", json={"ids": too_many}, headers=atlas.read).status_code == 422
    assert atlas.post("/api/v2/games/batch", json={"ids": ["atlas:x"]}).status_code == 401
    assert atlas.post("/api/v2/games/batch", json={"ids": ["atlas:x"], "extra": 1},
                      headers=atlas.read).status_code == 422


# ---- /records alias (D15.21) ------------------------------------------------------------------------------------

def test_records_alias_answers_like_games(atlas):
    cid = add_game(atlas.st, "20", "Quiet Orchard")
    set_art(atlas.st, cid, [{"kind": "key_art", "url": "https://media.example.org/fixtures/orchard.png"}])
    pairs = [("GET", f"/games/{cid}", None), ("GET", "/games?q=orchard", None),
             ("GET", f"/games/{cid}/relations", None), ("GET", f"/games/{cid}/artwork", None),
             ("GET", f"/games/{cid}/achievements", None), ("POST", "/games/batch", {"ids": [cid]})]
    for method, path, body in pairs:
        games = atlas.request(method, "/api/v2" + path, json=body, headers=atlas.read)
        records = atlas.request(method, "/api/v2" + path.replace("/games", "/records", 1), json=body,
                                headers=atlas.read)
        assert games.status_code == records.status_code == 200, path
        assert games.json() == records.json(), path
        assert "Deprecation" not in games.headers and "Deprecation" not in records.headers  # /games stays
    etag = atlas.get(f"/api/v2/records/{cid}", headers=atlas.read).headers["ETag"]
    assert atlas.get(f"/api/v2/records/{cid}", headers={**atlas.read, "If-None-Match": etag}).status_code == 304


# ---- artwork primary_by_role (§10.1) ----------------------------------------------------------------------------

def test_primary_by_role_names_one_image_per_role(atlas):
    game = add_game(atlas.st, "30", "Northwind")
    set_art(atlas.st, game, [{"kind": k, "url": f"https://media.example.org/fixtures/northwind-{k}.png"}
                             for k in ("poster", "key_art", "banner", "hero", "icon", "logo", "screenshot")])
    art = atlas.get(f"/api/v2/games/{game}/artwork", headers=atlas.read).json()
    by_kind = {a["kind"]: a["id"] for a in art["results"]}
    roles = art["primary_by_role"]
    assert set(roles) == set(client_api.ART_ROLES) == {"cover", "background", "icon", "logo"}
    assert roles == {"cover": by_kind["key_art"], "background": by_kind["hero"], "icon": by_kind["icon"],
                     "logo": by_kind["logo"]}
    assert art["primary"]["key_art"]["id"] == by_kind["key_art"]  # the kind-keyed `primary` is unchanged
    quality = atlas.get(f"/api/v2/games/{game}/artwork?order=quality", headers=atlas.read).json()
    assert quality["primary_by_role"] == roles  # always the C12 ranking, whatever the list order

    book = add_book(atlas.st, "B1", "The Tidewright")
    set_art(atlas.st, book, [{"kind": k, "url": f"https://media.example.org/fixtures/tidewright-{k}.png"}
                             for k in ("key_art", "poster", "screenshot")])
    art = atlas.get(f"/api/v2/games/{book}/artwork", headers=atlas.read).json()
    by_kind = {a["kind"]: a["id"] for a in art["results"]}
    assert art["primary_by_role"] == {"cover": by_kind["poster"], "background": by_kind["screenshot"],
                                      "icon": None, "logo": None}  # a book's cover is its poster (D15.5)
    empty = add_game(atlas.st, "31", "Bare Record")
    got = atlas.get(f"/api/v2/games/{empty}/artwork", headers=atlas.read).json()["primary_by_role"]
    assert got == {"cover": None, "background": None, "icon": None, "logo": None}


# ---- public_read (D15.15) ---------------------------------------------------------------------------------------

def test_public_read_off_by_default(atlas):
    cid = add_game(atlas.st, "40", "Lowfield")
    assert atlas.get(f"/api/v2/games/{cid}").status_code == 401
    assert atlas.post("/api/v2/resolve", json={"items": [{"title": "Lowfield"}]}).status_code == 401


def test_public_read_opens_the_client_reads_only(tmp_path):
    app = _make(tmp_path, public_read=True)
    with TestClient(app, base_url=BASE) as c:
        st = app.state.atlas
        _wait_for_startup_replay(st)
        cid = add_game(st, "41", "Highfield")
        assert "public_read" in c.get("/api/v2/site").json()["client_api"]["capabilities"]
        for path in (f"/api/v2/games/{cid}", f"/api/v2/records/{cid}", "/api/v2/games?q=highfield",
                     f"/api/v2/games/{cid}/relations", f"/api/v2/games/{cid}/artwork",
                     f"/api/v2/games/{cid}/achievements"):
            assert c.get(path).status_code == 200, path
        assert c.post("/api/v2/games/batch", json={"ids": [cid]}).status_code == 200
        assert c.post("/api/v2/resolve", json={"items": [{"title": "Highfield"}]}).status_code == 200
        # outside the client subset a token is still needed, and a bad token is never treated as anonymous
        assert c.get(f"/api/v2/games/{cid}/explain").status_code == 401
        assert c.get(f"/api/v2/games/{cid}/provenance").status_code == 401
        assert c.get(f"/api/v2/games/{cid}", headers=H("atl_wrong")).status_code == 401
        # anonymous reads share the per-IP budget of the anonymous path (60/min)
        codes = [c.get(f"/api/v2/games/{cid}").status_code for _ in range(70)]
        assert 429 in codes and codes.index(429) <= 60


# ---- the client metric (§10.2) ----------------------------------------------------------------------------------

@pytest.fixture
def recording():
    was = metrics.ENABLED
    metrics.enable()
    REGISTRY.reset()
    yield
    REGISTRY.reset()
    metrics.enable(was)


def test_client_requests_counted_by_connector(atlas, recording):
    cid = add_game(atlas.st, "50", "Copperline")
    for agent, label in (("atlas-connector-jellyfin/1.0.0", "jellyfin"), ("atlas-connector-playnite/0.3.1", "playnite"),
                         ("atlas-connector-kodi/1.0", "other"), ("curl/8.0", "other")):
        atlas.get(f"/api/v2/games/{cid}", headers={**atlas.read, "User-Agent": agent})
        assert instruments.client_requests.value(client=label) >= 1, agent
    before = instruments.client_requests.value(client="jellyfin")
    atlas.get(f"/api/v2/games/{cid}/explain", headers={**atlas.read, "User-Agent": "atlas-connector-jellyfin/1"})
    assert instruments.client_requests.value(client="jellyfin") == before  # only the client subset counts
    assert client_api.client_label("atlas-connector-EMBY/2") == "emby"
    assert client_api.client_label(None) == "other"
    assert set(instruments.client_requests.labels["client"]) == {"playnite", "jellyfin", "emby", "plex", "other"}


# ---- pairing (§10.1 item 1, T-B18) ------------------------------------------------------------------------------

def pair(c, label="Jellyfin on nas", agent="atlas-connector-jellyfin/1.0.0"):
    r = c.post("/api/v2/client/pair", json={"label": label}, headers={"User-Agent": agent})
    assert r.status_code == 200, r.text
    return r.json()


def poll(c, poll_token):
    r = c.post("/api/v2/client/pair/poll", json={"poll_token": poll_token})
    assert r.status_code == 200, r.text
    return r.json()


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def approve(c, code, decision="approve"):
    page = c.post("/pair", data={"csrf": csrf_of(c.get("/pair").text), "code": code})
    assert page.status_code == 200, page.text
    return c.post("/pair/decide", data={"csrf": csrf_of(page.text), "code": code, "decision": decision})


def test_pairing_security_rules(atlas, clock):  # T-B18
    started = pair(atlas)
    code, token = started["code"], started["poll_token"]
    # user code: >= 8 characters of an unambiguous alphabet, >= 40 bits; expiry <= 600 s; poll token >= 128 bits
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}", code)
    assert pairing.CODE_BITS >= 40 and len(pairing.ALPHABET) ** pairing.CODE_LENGTH >= 2 ** 40
    assert 0 < started["expires_in"] <= 600 and started["interval"] == 5
    assert len(token) >= 22 and pairing.POLL_TOKEN_BYTES * 8 >= 128
    assert started["verify_url"] == f"{BASE}/pair"
    assert started["verify_url_complete"] == f"{BASE}/pair?code={code}"
    # the QR encodes only the address and the code, never a token
    assert started["qr_svg"] == qr.svg(started["verify_url_complete"], label="Pairing code QR")
    assert token not in started["verify_url_complete"]
    # nothing secret is stored in clear
    with atlas.st.maker() as s:
        stored = " ".join(r.value for r in s.scalars(select(NodeState).where(NodeState.key.like("client_pair%"))))
    assert stored and token not in stored and code not in stored and code.replace("-", "") not in stored
    # polling: pending, faster than every 5 s -> slow_down (and the interval grows), then pending again
    assert poll(atlas, token)["status"] == "pending"
    clock["t"] += 1
    slow = poll(atlas, token)
    assert slow["status"] == "slow_down" and slow["interval"] == 10
    clock["t"] += 11
    assert poll(atlas, token)["status"] == "pending"
    assert poll(atlas, "x" * 43)["status"] == "expired"  # an unknown poll token says nothing more


def test_pairing_approval_mints_a_personal_read_token(atlas, clock):
    signup(atlas, "pat")
    started = pair(atlas)
    assert atlas.get(f"/pair?code={started['code']}").status_code == 200  # a GET only pre-fills the form
    assert poll(atlas, started["poll_token"])["status"] == "pending"
    page = atlas.post("/pair", data={"csrf": csrf_of(atlas.get("/pair").text), "code": started["code"].lower()})
    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", page.text)).split())
    # the approval page names the client, its User-Agent and requesting address, and warns about phishing
    assert "Jellyfin on nas" in text and "atlas-connector-jellyfin/1.0.0" in text and "testclient" in text
    assert "only approve a code shown on your own device" in text.lower()
    # approving is a POST with the form token; without it nothing happens
    assert atlas.post("/pair/decide", data={"code": started["code"], "decision": "approve"}).status_code == 403
    done = atlas.post("/pair/decide", data={"csrf": csrf_of(page.text), "code": started["code"],
                                            "decision": "approve"})
    assert done.status_code == 200 and "approved" in done.text.lower()
    clock["t"] += 6
    got = poll(atlas, started["poll_token"])
    assert got["status"] == "approved" and got["token"].startswith("atl_") and got["scopes"] == ["read"]
    assert poll(atlas, started["poll_token"])["status"] == "expired"  # single use: the token is handed out once
    new = H(got["token"])
    cid = add_game(atlas.st, "60", "Brightwater")
    assert atlas.get(f"/api/v2/games/{cid}", headers=new).status_code == 200
    assert atlas.put(f"/api/v2/games/{cid}/locks/title", json={"value": "x"}, headers=new).status_code == 403
    with atlas.st.maker() as s:
        row = s.scalars(select(Token).where(Token.name == "Jellyfin on nas")).one()
        assert row.user_id is not None and row.scopes == ["read"]
        audit = s.scalars(select(AuditEntry).where(AuditEntry.action == "token_create",
                                                   AuditEntry.target == row.id)).one()
        assert audit.actor_id == row.user_id and audit.detail["via"] == "paired"
        tid = row.id
    me = atlas.get("/me").text
    assert "Jellyfin on nas" in me  # listed with the user's tokens, so it can be revoked there
    r = atlas.post(f"/me/tokens/{tid}/revoke", data={"csrf": csrf_of(me)})
    assert r.status_code == 303
    assert atlas.get(f"/api/v2/games/{cid}", headers=new).status_code == 401


def test_pairing_denied_and_expired(atlas, clock):
    signup(atlas, "sam")
    denied = pair(atlas)
    assert approve(atlas, denied["code"], "deny").status_code == 200
    clock["t"] += 6
    assert poll(atlas, denied["poll_token"])["status"] == "denied"
    late = pair(atlas)
    clock["t"] += 601
    assert poll(atlas, late["poll_token"])["status"] == "expired"
    page = atlas.post("/pair", data={"csrf": csrf_of(atlas.get("/pair").text), "code": late["code"]})
    assert page.status_code == 400  # an expired code is a wrong code


def test_wrong_codes_lock_the_approval_form(atlas, clock):
    signup(atlas, "kim")
    good = pair(atlas)
    for _ in range(5):
        r = atlas.post("/pair", data={"csrf": csrf_of(atlas.get("/pair").text), "code": "ZZZZ-ZZZZ"})
        assert r.status_code == 400
    locked = atlas.post("/pair", data={"csrf": csrf_of(atlas.get("/pair").text), "code": good["code"]})
    assert locked.status_code == 429 and "Retry-After" in locked.headers  # even the right code, for 10 minutes
    clock["t"] += 599
    assert atlas.post("/pair", data={"csrf": csrf_of(atlas.get("/pair").text),
                                     "code": good["code"]}).status_code == 429
    clock["t"] += 2
    late = pair(atlas)  # the first code expired meanwhile
    assert approve(atlas, late["code"]).status_code == 200


def test_pairing_needs_a_signed_in_user(atlas):
    r = atlas.get("/pair?code=ABCD-EFGH")
    assert r.status_code in (302, 303) and "/login" in r.headers["location"]


def test_pairing_limits(atlas, clock):
    for i in range(pairing.MAX_OPEN):
        pair(atlas, label=f"device {i}")
    r = atlas.post("/api/v2/client/pair", json={"label": "one too many"})
    assert r.status_code == 429  # at most 20 open pairings per node
    clock["t"] += 601  # they expire, room again
    pair(atlas)
    # labels are bounded and printable
    assert atlas.post("/api/v2/client/pair", json={"label": "x" * 65}).status_code == 422
    assert atlas.post("/api/v2/client/pair", json={"label": "bad\nlabel"}).status_code == 422


def test_pair_is_on_the_anonymous_per_ip_budget(atlas, clock):
    codes = []
    for i in range(70):
        clock["t"] += 700  # every pairing expires before the next, so only the IP budget can refuse
        codes.append(atlas.post("/api/v2/client/pair", json={"label": f"d{i}"}).status_code)
    assert 429 in codes and codes.index(429) <= 60


def test_token_cap_applies_to_paired_tokens(atlas, clock):
    signup(atlas, "lee")
    me = atlas.get("/me").text
    for i in range(10):
        r = atlas.post("/me/tokens", data={"csrf": csrf_of(me), "name": f"t{i}"})
        assert r.status_code == 200
    started = pair(atlas)
    r = approve(atlas, started["code"])
    assert r.status_code == 409 and "10 active tokens" in r.text
    with atlas.st.maker() as s:
        assert s.scalar(select(func.count()).select_from(Token).where(Token.user_id.is_not(None))) == 10
