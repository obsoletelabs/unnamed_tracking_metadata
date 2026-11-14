"""CONTRACT-C14 C14-2 guards: which tables assist code reads and writes (T-A-PRIV1, T-A-AUTHZ1), who may make a
node ask (T-A-AUTHZ2), that nothing about the group reaches anonymous pages (T-A-GRP1), the signing domains
(T-A-DOM1), the admin waiting-list lines and buttons, the CLI, and `ratelimit.take`. Synthetic data only."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import event, select

from atlas import assist, auth, groups, ratelimit
from atlas import group_crypto as gc
from atlas.api import group_routes
from atlas.models import Artwork, Canonical, GroupCert, Observation, RateCounter

from . import test_c14_assist as base
from .test_c14_assist import run, waiting

world = base.world  # the shared fixture
_fast = base._fast  # autouse

#: tables an assist op must never read on the helper (T-A-PRIV1)
PRIVATE = {"app_user", "user_session", "user_mfa", "proposal", "review_item", "review_candidate", "audit_entry",
           "api_token", "group_grant", "email_token", "profile_field", "profile_version", "profile_conflict",
           "profile_sync_consent", "profile_superseded", "group_account_link", "group_account_node", "oidc_client",
           "oidc_code", "oidc_access", "oidc_link", "user_list", "list_item", "notification", "comment", "report",
           "follow", "user_permission", "artwork_vote", "reputation_event"}
#: tables an answer must never write on the asker (T-A-AUTHZ1)
PRIVILEGED = {"field_lock", "merge_record", "merge_record_item", "review_item", "review_candidate", "proposal",
              "group_grant", "app_user", "user_session", "api_token", "relation", "user_permission", "change_log"}
TABLE = re.compile(r'(?:FROM|JOIN|INTO|UPDATE)\s+"?([a-z_0-9]+)"?', re.IGNORECASE)


class Recorder:
    def __init__(self, engine):
        self.engine, self.on, self.reads, self.writes = engine, False, set(), set()
        event.listen(engine, "before_cursor_execute", self.seen)

    def seen(self, conn, cursor, statement, params, context, executemany):
        if not self.on:
            return
        tables = set(TABLE.findall(statement))
        verb = statement.lstrip().split(None, 1)[0].upper()
        (self.writes if verb in ("INSERT", "UPDATE", "DELETE") else self.reads).update(tables)

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self.seen)


def engine_of(node):
    return node.st.maker.kw["bind"]


def test_assist_reads_only_allowed_tables(world, monkeypatch):
    """T-A-PRIV1 + helper half of T-A-AUTHZ1: while the helper runs an assist op it reads no account, profile,
    proposal, review, audit, session, token or grant table, and writes nothing but rate counters."""
    _u, n1, n2, cid = world
    rec = Recorder(engine_of(n2))
    real = group_routes._assist_op

    def watched(*args, **kw):
        rec.on = True
        try:
            return real(*args, **kw)
        finally:
            rec.on = False

    monkeypatch.setattr(group_routes, "_assist_op", watched)
    try:
        assert run(n1)["applied"] == 1
    finally:
        rec.close()
    assert rec.reads, "the op was not observed"
    assert not rec.reads & PRIVATE, rec.reads & PRIVATE
    assert rec.writes <= {"rate_counter"}, rec.writes


def test_assist_helper_never_shares_host_or_plugin_or_group_data(world):
    """§3.7: host and plugin observations, and anything this helper itself got from the group, never travel."""
    _u, _n1, n2, cid = world
    with n2.session() as s:
        s.add(Observation(canonical_id=cid, source_kind="host", source="playnite", source_scope="pc",
                          external_id="host-1", payload={"title": "Host title"}, redistributable=False))
        s.add(Observation(canonical_id=cid, source_kind="metadata", source="atlas-group", source_scope="f" * 16,
                          external_id=cid, payload={"title": "From the group"}, redistributable=False))
        s.flush()
        held = assist._holding(s, n2.settings, s.get(Canonical, cid))
        sources = {o.source for o in held["public"] + held["provider"]}
        assert "playnite" not in sources and "atlas-group" not in sources
        s.rollback()


def test_assist_bundle_carries_no_local_lock_or_host_ids(world):
    """§3.7 (review C14-2): the identity in the fetched bundle (and the `find` bundle hash) is merged from the
    shared observations only: a `local_only` title lock and a host observation's provider ids never travel."""
    from atlas import service
    from atlas.models import Lock

    from .test_c14_assist import ctx

    _u, n1, n2, cid = world
    with n2.session() as s:
        before = assist.find_one(s, n2.settings, cid)["bundle_hash"]
        s.add(Lock(canonical_id=cid, field="title", value="SECRET LOCAL TITLE", local_only=True))
        s.add(Observation(canonical_id=cid, source_kind="host", source="playnite", source_scope="pc",
                          external_id="host-1", payload={"title": "Host title", "provider_ids": {"gog": "999111"}},
                          redistributable=False))
        s.flush()
        service.rebuild(s, ctx(n2), cid)
        assert s.get(Canonical, cid).view["title"] == "SECRET LOCAL TITLE"  # the local view does change
        s.commit()
    nonce = b"\x05" * 16
    with n1.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n1.node_id)
        member = tr.members()[n2.node_id]["body"]["member"]
        got = group_routes.call(s, n1.settings, n1.identity, group, member, "assist.fetch",
                                {"id": cid, "want": ["record", "artwork"], "signed_entries": True,
                                 "on_behalf": None}, n1.net, nonce=nonce)
        found = group_routes.call(s, n1.settings, n1.identity, group, member, "assist.find",
                                  {"ids": [cid], "want": ["record", "artwork"], "reason": "waiting_for_record",
                                   "on_behalf": None}, n1.net)
    text = repr(got) + repr(found)
    for secret in ("SECRET LOCAL TITLE", "Host title", "999111", "playnite", "host-1"):
        assert secret not in text, secret
    record = got["bundle"]["body"]["record"]
    assert record["title"] == "Subnautica 2" and record["provider_ids"] == {"steam": "1962700"}
    assert found["items"][0]["bundle_hash"] == before  # the hash ignores the local-only changes too


def test_assist_apply_never_writes_privileged_tables(world):
    """T-A-AUTHZ1 (asker half): applying an answer writes no lock, merge, review, proposal, grant, user, session,
    relation or own-feed row, and no curator or community observation."""
    _u, n1, _n2, cid = world
    rec = Recorder(engine_of(n1))
    rec.on = True
    try:
        assert run(n1)["applied"] == 1
    finally:
        rec.close()
    assert not rec.writes & PRIVILEGED, rec.writes & PRIVILEGED
    with n1.session() as s:
        assert not s.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                       Observation.source.in_(("curator", "community")))).first()


def _token(node, scopes):
    with node.session() as s:
        _row, token = auth.create_token(s, "t-" + "-".join(scopes), scopes)
        s.commit()
        return {"Authorization": f"Bearer {token}"}


def test_ask_button_needs_read_and_lookup_needs_contribute_rights(world):
    """T-A-AUTHZ2: asking needs `read`, a lookup through the group needs `contribute`, the queue counts `admin`."""
    from fastapi.testclient import TestClient

    _u, n1, _n2, cid = world
    client = TestClient(n1.app)
    assert client.post("/api/v2/group/assist/ask", json={"ids": [cid]}).status_code == 401
    assert client.post("/api/v2/group/assist/ask", json={"ids": [cid]},
                       headers=_token(n1, ["read"])).json()["queued"] == [cid]
    assert client.post("/api/v2/group/assist/ask", json={"ids": ["not-an-id"]},
                       headers=_token(n1, ["read"])).status_code == 400
    body = {"provider": "steam", "external_id": "10"}
    assert client.post("/api/v2/group/assist/lookup", json=body,
                       headers=_token(n1, ["read"])).status_code == 403
    assert client.post("/api/v2/group/assist/lookup", json=body,
                       headers=_token(n1, ["contribute"])).status_code == 409  # lookups are off by default
    assert client.get("/api/v2/group/assist", headers=_token(n1, ["read"])).status_code == 403
    assert client.get("/api/v2/group/assist", headers=_token(n1, ["admin"])).json()["due"] >= 1


def test_sync_info_and_public_pages_reveal_no_group(world):
    """T-A-GRP1: after an answer, the anonymous record page names no helper and shows no group data (it is a
    group-only record: the same 404 as an unknown one), and `/sync/info` says nothing about the group."""
    from fastapi.testclient import TestClient

    _u, n1, n2, cid = world
    run(n1)
    client = TestClient(n1.app)
    page = client.get(f"/game/{cid}")
    unknown = client.get("/game/atlas:" + "0" * 26)
    assert page.status_code == unknown.status_code == 404
    assert n2.node_id not in page.text and "Node N2" not in page.text
    info = client.get("/api/v2/sync/info").text
    assert n2.node_id not in info and "assist" not in info and "group" not in info.lower()


def test_new_domains_prefix_free():
    """T-A-DOM1: the assist and contribution-state domains keep every domain prefix-free."""
    domains = gc.all_domains()
    assert gc.DOMAINS["group.assist"] in domains and gc.DOMAINS["group.contrib_state"] in domains
    assert gc.prefix_free(domains)
    assert "group.assist" in gc.OBJECT_TYPES and "group.contrib_state" in gc.OBJECT_TYPES


def test_group_assist_objects_are_refused_by_state_ingest(world):
    """T-A-DOM1 (second half): a bundle passed off as a certificate is refused by `ingest_state`."""
    _u, n1, n2, cid = world
    nonce = b"\x07" * 16
    with n1.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n1.node_id)
        member = tr.members()[n2.node_id]["body"]["member"]
        got = group_routes.call(s, n1.settings, n1.identity, group, member, "assist.fetch",
                                {"id": cid, "want": ["record"], "signed_entries": False, "on_behalf": None},
                                n1.net, nonce=nonce)
        before = len(s.scalars(select(GroupCert)).all())
        groups.ingest_state(s, group, {"genesis": group.genesis, "certs": [got["bundle"]]}, n1.node_id,
                            settings=n1.settings)
        assert len(s.scalars(select(GroupCert)).all()) == before
        s.rollback()


def login_admin(node):
    from fastapi.testclient import TestClient

    from atlas.models import User

    from .channel_harness import PASSWORD, user
    from .test_web import login

    uid = user(node, "boss")
    with node.session() as s:
        s.get(User, uid).role = "admin"
        s.commit()
    client = TestClient(node.app, base_url=node.settings.public_url, follow_redirects=False)
    assert login(client, "boss", PASSWORD).status_code == 303
    return client


def test_admin_waiting_list_shows_the_group_lines_and_buttons(world):
    """The waiting card says what the group answered, with "Ask my nodes" and "Use what <member> has"; both
    buttons work."""
    from .test_c14_assist import _drop_introducing
    from .test_web import token

    u, n1, n2, cid = world
    client = login_admin(n1)
    page = client.get("/admin/peers").text
    assert 'data-assist-state="queued"' in page
    _drop_introducing(n2, cid)
    run(n1)
    page = client.get("/admin/peers").text
    assert 'data-assist-state="offer"' in page and "Use what Node N2 has" in page
    item = re.search(r'name="item" value="([^"]+)"', page).group(1)
    r = client.post("/admin/peers/assist/adopt", data={"csrf": token(client, "/admin/peers"), "item": item})
    assert r.status_code == 303
    assert waiting(n1, u.node_id) == []
    r = client.post("/admin/peers/assist/ask", data={"csrf": token(client, "/admin/peers"), "cid": cid})
    assert r.status_code == 303


def test_cli_forget_dry_run_changes_nothing(world, capsys, monkeypatch):
    from atlas import cli

    _u, n1, n2, cid = world
    run(n1)
    monkeypatch.setattr(cli, "_maker", lambda: (n1.settings, n1.st.maker))
    from atlas import group_cli

    parser_args = type("A", (), {"assist_cmd": "forget", "node": n2.node_id, "dry_run": True})()
    assert group_cli.cmd_assist(parser_args) == 0
    assert "would" in capsys.readouterr().out
    with n1.session() as s:
        assert s.get(Canonical, cid) is not None
    parser_args.dry_run = False
    assert group_cli.cmd_assist(parser_args) == 0
    with n1.session() as s:
        assert s.get(Canonical, cid) is None


# ---- ratelimit.take (C14-0 critic, §4.3 step 6(a)) ------------------------------------------------------------------

def _maker(tmp_path):
    from atlas import db

    url = f"sqlite:///{tmp_path}/rl.db"
    db.upgrade(url)
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=db.make_engine(url))


def test_take_is_all_or_nothing_and_refusals_count_nothing(tmp_path):
    maker = _maker(tmp_path)
    clock = lambda: 1_000_000.0  # noqa: E731
    windows = [("k:min", 3, 60), ("k:day", 4, 86400)]
    assert ratelimit.take(maker, windows, clock=clock) == (True, 0)
    assert ratelimit.take(maker, windows, amount=2, clock=clock) == (True, 0)
    ok, retry = ratelimit.take(maker, windows, clock=clock)  # the minute window is full
    assert not ok and 0 < retry <= 60
    with maker() as s:
        assert ratelimit.used(s, "k:min", 60, clock) == 3 and ratelimit.used(s, "k:day", 86400, clock) == 3
    later = lambda: 1_000_000.0 + 61  # noqa: E731
    assert ratelimit.take(maker, windows, clock=later)[0]  # the day window takes its last one
    assert not ratelimit.take(maker, windows, clock=later)[0]
    with maker() as s:
        assert ratelimit.used(s, "k:min", 60, later) == 1  # the refused take counted nothing


def test_take_with_a_sub_share_on_the_same_key_and_in_a_session(tmp_path):
    maker = _maker(tmp_path)
    clock = lambda: 2_000_000.0  # noqa: E731
    budget = [("b", 10, 60)]
    share = budget + [("b", 5, 60)]  # an assist sub-share of the same counter
    for _ in range(5):
        assert ratelimit.take(maker, share, clock=clock)[0]
    assert not ratelimit.take(maker, share, clock=clock)[0]
    assert ratelimit.take(maker, budget, clock=clock)[0]  # the node's own calls still have room
    with maker() as s:
        assert ratelimit.take(s, budget, amount=4, clock=clock)[0]
        assert not ratelimit.take(s, budget, clock=clock)[0]
        s.rollback()  # a session take goes with the caller's transaction
    with maker() as s:
        assert ratelimit.used(s, "b", 60, clock) == 6
        assert len(s.scalars(select(RateCounter)).all()) == 1


@pytest.mark.parametrize("amount", [0, -1])
def test_take_refuses_bad_amounts(tmp_path, amount):
    with pytest.raises(ValueError):
        ratelimit.take(_maker(tmp_path), [("x", 1, 60)], amount=amount)


# ---- T-L10: untrusted URLs (Review #1 C14-4) -----------------------------------------------------------------------

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def resolver(monkeypatch):
    import socket

    table = {"img.test": PUBLIC_IP, "lan.test": "10.0.0.5", "meta.test": "169.254.169.254", "lo.test": "127.0.0.1",
             "v6lo.test": "::1", "mapped.test": "::ffff:10.0.0.1"}

    def fake(host, port, type=0, **kw):
        ip = table.get(host, host)
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


@pytest.mark.parametrize("url, ok", [
    ("https://img.test/a.png", True),
    ("https://img.test:443/a.png", True),
    ("http://img.test/a.png", False),            # https only
    ("https://img.test:8443/a.png", False),      # port allowlist
    ("https://u:p@img.test/a.png", False),       # userinfo
    ("https://img.test/a.png?api_key=1", False),  # key-like query names are dropped, never redacted
    ("https://img.test/a.png?X-Amz-Signature=1", False),
    ("https://img.test/a.png?access_token=1", False),
    ("https://img.test/a b.png", False),
    ("ftp://img.test/a.png", False),
])
def test_bundle_and_lookup_urls_pass_the_https_allowlist(url, ok):
    assert assist._safe_url(url) is ok


def test_lookup_results_scrubbed_and_urls_untrusted(world, resolver, tmp_path):
    """T-L10: a URL that arrived from the group is fetched only through `media._request`: every resolved address
    must be public (private, loopback, link-local metadata and IPv4-mapped forms refused before any connection),
    the connection goes to the pinned IP, and every redirect is re-validated."""
    import httpx

    from atlas import media

    from .conftest import build_settings

    settings = build_settings(media_dir=str(tmp_path))
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["host"], request.url.path))
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "https://meta.test/latest/meta-data/"})
        return httpx.Response(200, content=base._png())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for host in ("lan.test", "meta.test", "lo.test", "v6lo.test", "mapped.test"):
        with pytest.raises(media.MediaError) as exc:
            media._request(client, f"https://{host}/a.png", settings)
        assert exc.value.code == "not_public", host
    assert seen == []  # nothing was connected to
    with pytest.raises(media.MediaError) as exc:
        media._request(client, "https://img.test/hop", settings)
    assert exc.value.code == "not_public"
    assert seen == [(PUBLIC_IP, "img.test", "/hop")]  # the pinned public IP only; the redirect never followed
    seen.clear()
    assert media._request(client, "https://img.test/ok.png", settings)
    assert seen == [(PUBLIC_IP, "img.test", "/ok.png")]

    # A group row whose URL points at the metadata address is refused on the origin path, not fetched.
    _u, n1, _n2, cid = world
    with n1.session() as s:
        if s.get(Canonical, cid) is None:
            s.add(Canonical(id=cid, view={}, explain={}, media_type="game"))
            s.flush()
        art = Artwork(canonical_id=cid, source="curator", kind="banner", url="https://meta.test/x.png",
                      licence="cc0", redistributable=False, fetched_from="group:" + "a" * 16)
        s.add(art)
        s.flush()
        seen.clear()
        assert not media.cache_one(s, settings, art, client, None)
        assert art.last_error == "not_public" and seen == []
        s.rollback()


def test_api_ask_is_audited_and_rate_limited_per_token(world, monkeypatch):
    """Review C14-2: a forced ask through the API writes a `group_assist_ask` audit entry and is limited per
    token (it skips the backoff and wakes the group round)."""
    from fastapi.testclient import TestClient

    from atlas.models import AuditEntry

    _u, n1, _n2, cid = world
    monkeypatch.setattr(assist, "ASK_PER_HOUR", 2)
    client = TestClient(n1.app)
    headers = _token(n1, ["read"])
    for _ in range(2):
        assert client.post("/api/v2/group/assist/ask", json={"ids": [cid]}, headers=headers).status_code == 200
    refused = client.post("/api/v2/group/assist/ask", json={"ids": [cid]}, headers=headers)
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) >= 1
    with n1.session() as s:
        rows = s.scalars(select(AuditEntry).where(AuditEntry.action == "group_assist_ask")).all()
        assert len(rows) == 2 and rows[0].detail["via"] == "api" and rows[0].detail["ids"] == [cid]
