"""C14-0 prerequisites (CONTRACT-C14 §3.10, §6.2, §6.4, §12, decision 2).

* T-K0 / T-K17: OIDC EdDSA keeps working after the provider rotates its signing key (F-C10-1); `jwks` publishes
  only the current key under `kid = key_id`, and a relying party refuses a token whose `kid` is a superseded key.
* T-RL1: rate counters with windows longer than about five minutes survive housekeeping; `amount=` counts bytes.
* T-K2 (helper only): the pairwise UUIDv8 subject is stable per user and node and unrelated across nodes.
* QR snapshot: `atlas.qr.svg` renders server-side SVG that a strict CSP accepts.
* V2: the group-call client caps the answer size per op and reads the answer by streaming.

All keys, users and addresses are synthetic and generated at runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from atlas import key_rotation, oidc, qr, ratelimit
from atlas.api import group_routes
from atlas.models import OidcIssuer, OidcLink, RateCounter, User, now
from atlas.signing import load_identity

from .group_harness import Dispatcher, create, enrol, fast_kdf, finish_creation, make_node
from .test_oidc import PROVIDER, World, follow_callback, stranger_identity
from .test_web import fresh

FIXTURES = Path(__file__).parent / "fixtures"


# ---- F-C10-1: OIDC EdDSA across provider key rotations (T-K0, T-K17) ---------------------------------------------

def _rotate_provider(world: World, *, update_ctx: bool = True):
    st = world.p_app.state.atlas
    with st.maker() as s:
        identity = load_identity(s, st.ctx.settings)
        successor = key_rotation.rotate(s, identity.key_id, st.ctx.settings)
        s.commit()
    if update_ctx:
        st.ctx.identity = successor
    return identity, successor


def _sign_in(world: World, iid: str):
    browser = fresh(type("S", (), {"st_app": world.n_app}))
    done = follow_callback(browser, world.sign_in_via(browser, iid)[0])
    return done, browser


def test_oidc_eddsa_after_provider_key_rotation(tmp_path):
    """T-K0 (F-C10-1): pinned before a rotation, sign-in still works after one, and after a second one."""
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on")
        root, first = _rotate_provider(w)
        done, browser = _sign_in(w, iid)
        assert done.status_code == 303, done.text[:300]
        assert browser.get("/me").status_code == 200
        with w.n_app.state.atlas.maker() as s:
            issuer = s.get(OidcIssuer, iid)
            assert issuer.node_id == root.node_id and issuer.public_key == root.trust_public_b64  # still the root
            assert [h["body"]["key_id"] for h in key_rotation.trusted_history(s, root.node_id)] == [first.key_id]
        # a stale provider worker (its Ctx still holds the old key) signs with the current key from the database
        _, second = _rotate_provider(w, update_ctx=False)
        assert w.p_app.state.atlas.ctx.identity.key_id == first.key_id
        done, _ = _sign_in(w, iid)
        assert done.status_code == 303, done.text[:300]
        with w.n_app.state.atlas.maker() as s:
            assert len(s.scalars(select(OidcLink)).all()) == 1
            assert key_rotation.expected_key(root.node_id, key_rotation.trusted_history(s, root.node_id),
                                             2**63 - 1) == second.key_id


def test_issuer_added_after_a_rotation_pins_the_root(tmp_path):
    with World(tmp_path) as w:
        root, first = _rotate_provider(w)
        iid, _, _ = w.add_issuer(auto_create="on")
        with w.n_app.state.atlas.maker() as s:
            issuer = s.get(OidcIssuer, iid)
            assert issuer.node_id == root.node_id and issuer.public_key == root.trust_public_b64
        done, _ = _sign_in(w, iid)
        assert done.status_code == 303, done.text[:300]


def test_jwks_publishes_current_key_only_and_rp_refuses_superseded_kid(tmp_path):
    """T-K17: an unrotated provider keeps `kid = node_id` (existing issuers keep working); after a rotation `jwks`
    lists only the current key under `kid = key_id`; a token signed by the superseded root key is refused."""
    with World(tmp_path) as w:
        root = w.p_app.state.atlas.ctx.identity
        okp = [k for k in w.provider.get("/oidc/jwks").json()["keys"] if k["kty"] == "OKP"]
        assert [k["kid"] for k in okp] == [root.node_id] and root.key_id == root.node_id
        disc = w.provider.get("/.well-known/openid-configuration").json()
        assert disc["atlas_node"] == {"node_id": root.node_id, "root_public_key": root.trust_public_b64,
                                      "signing_key_id": root.node_id, "rotations": []}
        iid, cid, _ = w.add_issuer(auto_create="on")
        assert _sign_in(w, iid)[0].status_code == 303  # unrotated: works exactly as before
        _, first = _rotate_provider(w)
        okp = [k for k in w.provider.get("/oidc/jwks").json()["keys"] if k["kty"] == "OKP"]
        assert [k["kid"] for k in okp] == [first.key_id]
        assert oidc.b64u_dec(okp[0]["x"]) == __import__("base64").b64decode(first.public_b64)
        claims = {"iss": PROVIDER, "aud": cid, "sub": "u1", "iat": int(now().timestamp()),
                  "exp": int(now().timestamp()) + 300}
        old_token = oidc.sign_jwt(root, claims)  # the superseded key, as a thief of it would sign
        new_token = oidc.sign_jwt(first, claims)
        assert json.loads(oidc.b64u_dec(new_token.split(".")[0]))["kid"] == first.key_id
        st = w.n_app.state.atlas
        with st.maker() as s:
            issuer = s.get(OidcIssuer, iid)
            assert oidc.verify_issuer_token(s, st.http, issuer, new_token, audience=cid, nonce=None)["sub"] == "u1"
            with pytest.raises(oidc.TokenError, match="superseded or unknown"):
                oidc.verify_issuer_token(s, st.http, issuer, old_token, audience=cid, nonce=None)
            stranger = stranger_identity()
            with pytest.raises(oidc.TokenError, match="superseded or unknown"):
                oidc.verify_issuer_token(s, st.http, issuer, oidc.sign_jwt(stranger, claims), audience=cid,
                                         nonce=None)


def test_rp_refuses_a_rewound_or_foreign_rotation_history(tmp_path):
    with World(tmp_path) as w:
        iid, cid, _ = w.add_issuer(auto_create="on")
        root, first = _rotate_provider(w)
        assert _sign_in(w, iid)[0].status_code == 303  # the node now remembers root -> first
        st = w.n_app.state.atlas
        claims = {"iss": PROVIDER, "aud": cid, "sub": "u1", "iat": int(now().timestamp()),
                  "exp": int(now().timestamp()) + 300}
        real = st.http

        def lying(mutate):
            def respond(request):
                response = real.request(request.method, request.url.raw_path.decode())
                data = response.json()
                if request.url.path.endswith("/openid-configuration"):
                    mutate(data)
                return httpx.Response(response.status_code, json=data)
            return httpx.Client(transport=httpx.MockTransport(respond))

        def rewind(data):  # the old root key presented as current again (a stolen-key replay)
            data["atlas_node"].update(rotations=[], signing_key_id=root.node_id)

        def foreign(data):
            other = stranger_identity()
            data["atlas_node"].update(node_id=other.node_id, root_public_key=other.public_b64)

        with st.maker() as s:
            issuer = s.get(OidcIssuer, iid)
            for mutate in (rewind, foreign):
                with pytest.raises(oidc.TokenError):
                    oidc.verify_issuer_token(s, lying(mutate), issuer, oidc.sign_jwt(root, claims), audience=cid,
                                             nonce=None)


# ---- T-RL1: long-window rate counters ----------------------------------------------------------------------------

def _maker(tmp_path):
    from atlas import main

    from .conftest import build_settings

    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/rl.db", media_cache_enabled=False))
    return app.state.atlas.maker


def test_long_window_rate_counters_survive_purge(tmp_path):
    maker = _maker(tmp_path)
    base = 1_699_920_000.0  # the start of an hour and of a day window
    assert base % 86400 == 0
    t = [base]
    clock = lambda: t[0]  # noqa: E731
    assert ratelimit.hit(maker, "hourly", 3, 3600, clock=clock)[0]
    assert ratelimit.hit(maker, "daily", 2, 86400, clock=clock)[0]
    for _ in range(11):  # an hour of housekeeping cycles, five minutes apart (each one used to drop both rows)
        t[0] += 300
        ratelimit.purge(maker, clock=clock)
    assert ratelimit.hit(maker, "hourly", 3, 3600, clock=clock)[0]
    assert ratelimit.hit(maker, "hourly", 3, 3600, clock=clock)[0]
    ok, retry = ratelimit.hit(maker, "hourly", 3, 3600, clock=clock)
    assert not ok and retry == 300  # the fourth hit within the hour is refused until the hour ends
    assert ratelimit.hit(maker, "daily", 2, 86400, clock=clock)[0]
    assert not ratelimit.hit(maker, "daily", 2, 86400, clock=clock)[0]
    t[0] = base + 3600  # a new hour; the day goes on
    ratelimit.purge(maker, clock=clock)
    assert ratelimit.hit(maker, "hourly", 3, 3600, clock=clock)[0]
    assert not ratelimit.hit(maker, "daily", 2, 86400, clock=clock)[0]


def test_purge_keeps_live_windows_and_drops_expired_ones(tmp_path):
    maker = _maker(tmp_path)
    start = 1_700_000_000.0
    ratelimit.hit(maker, "hourly", 5, 3600, clock=lambda: start)
    ratelimit.hit(maker, "minute", 5, 60, clock=lambda: start)
    ratelimit.purge(maker, clock=lambda: start + 61)  # the minute window has ended, the hour has not
    with maker() as s:
        assert {r.key for r in s.scalars(select(RateCounter))} == {"hourly"}
    ratelimit.purge(maker, clock=lambda: start + 3601)
    with maker() as s:
        assert s.scalars(select(RateCounter)).all() == []


def test_windows_survive_a_purge_loop_hour_by_hour(tmp_path):
    """The defect as housekeeping saw it: purge every cycle with its defaults, a 3600 s limit of 3."""
    maker = _maker(tmp_path)
    base = 1_699_999_200.0  # the start of an hour window (divisible by 3600)
    assert base % 3600 == 0
    for i in range(3):
        assert ratelimit.hit(maker, "h", 3, 3600, clock=lambda i=i: base + i * 600)[0]
        ratelimit.purge(maker, clock=lambda i=i: base + i * 600 + 300)
    assert not ratelimit.hit(maker, "h", 3, 3600, clock=lambda: base + 3000)[0]
    assert ratelimit.hit(maker, "h", 3, 3600, clock=lambda: base + 3600)[0]  # a new hour


def test_hit_amount_counts_bytes_in_one_row(tmp_path):
    maker = _maker(tmp_path)
    clock = lambda: 1_700_000_000.0  # noqa: E731
    budget = 512 * 1024 * 1024
    assert ratelimit.hit(maker, "assist-bytes:x", budget, 3600, clock=clock, amount=500 * 1024 * 1024)[0]
    assert ratelimit.hit(maker, "assist-bytes:x", budget, 3600, clock=clock, amount=12 * 1024 * 1024)[0]
    assert not ratelimit.hit(maker, "assist-bytes:x", budget, 3600, clock=clock, amount=1)[0]
    with maker() as s:
        rows = s.scalars(select(RateCounter).where(RateCounter.key == "assist-bytes:x")).all()
        assert len(rows) == 1 and rows[0].count == budget + 1
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            ratelimit.hit(maker, "k", 1, 60, clock=clock, amount=bad)


def test_refused_amounts_cannot_overflow_the_counter(tmp_path):
    """Review fix: the stored total is capped at `limit + 1`, so an hour of refused byte requests stays inside the
    int32 `count` column on PostgreSQL (a 429, never 'integer out of range')."""
    maker = _maker(tmp_path)
    clock = lambda: 1_700_000_000.0  # noqa: E731
    budget = 512 * 1024 * 1024
    for _ in range(10):  # 10 x 1 GiB refused: 10 GiB would overflow an int32 many times over
        assert not ratelimit.hit(maker, "assist-bytes:y", budget, 3600, clock=clock, amount=1024 * 1024 * 1024)[0]
    assert not ratelimit.hit(maker, "assist-bytes:y", budget, 3600, clock=clock, amount=1)[0]
    with maker() as s:
        assert s.get(RateCounter, ("assist-bytes:y", 1_700_002_800)).count == budget + 1
    huge = 2**40  # a limit past int32 behaves as the largest storable one and still never overflows
    assert ratelimit.hit(maker, "h", huge, 60, clock=clock, amount=2**31 - 2)[0]
    assert ratelimit.hit(maker, "h", huge, 60, clock=clock, amount=2**31 - 1)[0] is False
    with maker() as s:
        assert max(r.count for r in s.scalars(select(RateCounter).where(RateCounter.key == "h"))) == 2**31 - 1


def test_key_documents_are_not_cached_by_shared_caches(tmp_path):
    """Review fix: discovery (signing_key_id, rotation history) and jwks must revalidate after a rotation."""
    with World(tmp_path) as w:
        for path in ("/.well-known/openid-configuration", "/oidc/jwks"):
            assert w.provider.get(path).headers["cache-control"] == "no-cache"


def test_retry_after_is_the_rest_of_the_window(tmp_path):
    maker = _maker(tmp_path)
    ratelimit.hit(maker, "r", 1, 600, clock=lambda: 1_700_000_100.0)
    ok, retry = ratelimit.hit(maker, "r", 1, 600, clock=lambda: 1_700_000_100.0)
    assert not ok and retry == 600 - (1_700_000_100 % 600)


# ---- T-K2 (helper): pairwise UUIDv8 subjects ---------------------------------------------------------------------

def test_pairwise_subject_stable_per_user_and_node():
    key = bytes(range(32))
    a = oidc.pairwise_subject(key, "01JUSER0000000000000000001", "7b11c2d39e04f5a6")
    assert a == oidc.pairwise_subject(key, "01JUSER0000000000000000001", "7b11c2d39e04f5a6")  # relink: same sub
    assert a != oidc.pairwise_subject(key, "01JUSER0000000000000000001", "0000c2d39e04f5a6")  # other node
    assert a != oidc.pairwise_subject(key, "01JUSER0000000000000000002", "7b11c2d39e04f5a6")  # other user
    assert a != oidc.pairwise_subject(bytes(32), "01JUSER0000000000000000001", "7b11c2d39e04f5a6")  # other key
    u = uuid.UUID(a)
    assert str(u) == a and u.version == 8 and u.variant == uuid.RFC_4122
    digest = hmac.new(key, b"atlas-link-sub-v1\n01JUSER0000000000000000001\n7b11c2d39e04f5a6",
                      hashlib.sha256).digest()
    raw = bytearray(digest[:16])
    raw[6], raw[8] = (raw[6] & 0x0F) | 0x80, (raw[8] & 0x3F) | 0x80
    assert u.bytes == bytes(raw)  # exactly the derivation of C14 §6.4
    # no ambiguity between ("ab", "c") and ("a", "bc"): the parts are newline separated and must not contain one
    with pytest.raises(ValueError):
        oidc.pairwise_subject(key, "a\nb", "c")
    with pytest.raises(ValueError):
        oidc.pairwise_subject(b"short", "u", "n")


def test_existing_clients_keep_public_subjects():
    user = User(id="01JUSER0000000000000000001", username="dave", reputation=0)
    assert oidc.claims_for(user, "openid")["sub"] == user.id


# ---- QR helper (decision 2) --------------------------------------------------------------------------------------

def test_qr_svg_snapshot_and_csp_safety():
    out = qr.svg("https://atlas.example/link?code=WDJB-MJHT", label="Link code WDJB-MJHT")
    snapshot = (FIXTURES / "qr_link_snapshot.svg").read_text(encoding="utf-8").strip()
    assert out == snapshot
    assert out.startswith('<svg role="img" aria-label="Link code WDJB-MJHT" viewBox=')
    for forbidden in ("style", "<script", "javascript:", "href", "<?xml", "<image", "<foreignObject"):
        assert forbidden not in out, forbidden
    assert not re.search(r"\son\w+=", out)  # no event handler attributes
    assert re.fullmatch(r'<svg [^>]*>(<title>[^<]*</title>)?(<path [^>]*/>)+</svg>', out)


def test_qr_svg_escapes_and_limits_input():
    out = qr.svg("x", label='"><script>alert(1)</script>')
    assert "<script" not in out and "&lt;script&gt;" in out and '&quot;' in out
    assert qr.svg("same") == qr.svg("same")
    assert qr.svg("a") != qr.svg("b")
    with pytest.raises(ValueError):
        qr.svg("")
    with pytest.raises(ValueError):
        qr.svg("x" * (qr.MAX_BYTES + 1))


# ---- V2: response-size cap on the group-call client --------------------------------------------------------------

def test_response_caps_are_per_op():
    assert group_routes.response_cap("assist.find") == 1024 * 1024
    assert group_routes.response_cap("assist.fetch") == 1024 * 1024
    for op in group_routes.CALL_OPS:
        assert group_routes.response_cap(op) == 8 * 1024 * 1024


@pytest.fixture
def pair(tmp_path, monkeypatch):
    fast_kdf(monkeypatch)
    d = Dispatcher()
    a, b = make_node(tmp_path, d, "a"), make_node(tmp_path, d, "b")
    create(a)
    finish_creation(a)
    enrol(a, b)
    yield a, b, d
    a.close()
    b.close()


def _member(a, b):
    from atlas import groups

    with b.session() as s:
        group = groups.load(s)
        return groups.trust(s, group, own_node=b.node_id).in_force(a.node_id)["body"]["member"]


def test_group_call_refuses_an_answer_over_the_cap(pair, monkeypatch):
    from atlas import groups

    a, b, net = pair
    member = _member(a, b)
    with b.session() as s:
        assert "certs" in group_routes.call(s, b.settings, b.identity, groups.load(s), member, "state", {}, net)
        monkeypatch.setattr(group_routes, "RESPONSE_CAP", 200)
        with pytest.raises(group_routes.CallError, match="answer too large"):
            group_routes.call(s, b.settings, b.identity, groups.load(s), member, "state", {}, net)


class _Endless(httpx.SyncByteStream):
    """A chunked answer with no Content-Length that would never end: the client must stop at the cap."""

    def __init__(self):
        self.sent = 0

    def __iter__(self):
        while self.sent < 64 * 1024 * 1024:
            self.sent += 65536
            yield b"x" * 65536


def test_group_call_stops_reading_at_the_cap(pair):
    from atlas import groups

    a, b, _ = pair
    member = _member(a, b)
    endless = _Endless()
    big = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=endless)))
    declared = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"Content-Length": str(group_routes.RESPONSE_CAP + 1)}, content=b"")))
    with b.session() as s:
        with pytest.raises(group_routes.CallError, match="answer too large"):
            group_routes.call(s, b.settings, b.identity, groups.load(s), member, "state", {}, big)
        assert endless.sent <= group_routes.RESPONSE_CAP + 65536  # stopped reading at the cap
        with pytest.raises(group_routes.CallError, match="answer too large"):
            group_routes.call(s, b.settings, b.identity, groups.load(s), member, "state", {}, declared)


def test_rate_fix_needs_no_migration(tmp_path):
    """The fix needs no migration: `rate_counter.window` now holds the window's end (Unix seconds)."""
    maker = _maker(tmp_path)
    ratelimit.hit(maker, "k", 5, 600, clock=lambda: 1_700_000_100.0)
    with maker() as s:
        row = s.scalars(select(RateCounter)).one()
        assert row.window == 1_700_000_400 and row.window % 600 == 0
