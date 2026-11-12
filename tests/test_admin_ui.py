"""Admin UI batch: import keeps the admin nav (#65), a users page (#66), uncached artwork retry (#53),
failed mirror entries on the peers page (#90), typed inputs (#97) and no inline styles (CSP)."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from atlas import facets
from atlas.models import Artwork, AuditEntry, ReplicatedChange, ReplicatedFeed, SyncFailure, User

from .test_web import _FakeProvider, fresh, signup, token
from .test_web import site as web_site  # noqa: F401  (the fixture, re-exported under another name)


@pytest.fixture
def site(web_site):  # noqa: F811
    return web_site


TEMPLATES = Path(__file__).resolve().parent.parent / "atlas" / "web" / "templates"


def _uid(site, name):
    with site.st.maker() as s:
        return s.scalars(select(User).where(User.username == name)).one().id


def test_import_page_keeps_the_admin_nav_for_admins_only(site):
    """#65: the Administration tab "Import" stays inside administration (/admin/import), its search and import
    forms post back there, and the top-navigation Import stays the plain curator page."""
    site.st.providers.providers["fakeprov"] = _FakeProvider()
    signup(site, "alice")  # the first account is the administrator
    page = site.get("/admin/import").text
    assert 'aria-label="Administration"' in page and 'href="/admin/users"' in page
    assert re.search(r'<a href="/admin/import" aria-current="page">Import</a>', page)
    assert re.search(r'<a href="/admin" aria-current="page">Admin</a>', page)  # the top bar says where you are
    assert 'action="/admin/import"' in page and 'action="/import"' not in page
    found = site.get("/admin/import?provider=fakeprov&q=celeste").text
    assert 'aria-label="Administration"' in found and re.search(r'<form method="post" action="/admin/import">', found)
    r = site.post("/admin/import", data={"csrf": token(site, "/admin/import"), "provider": "fakeprov",
                                         "external_id": "77", "q": "celeste"})
    assert r.status_code == 200 and "Imported" in r.text and 'aria-label="Administration"' in r.text
    plain = site.get("/import").text  # the curator page for an administrator too: no admin tabs there
    assert "Import from a provider" in plain and 'aria-label="Administration"' not in plain
    assert 'action="/import"' in plain
    bob = fresh(site)
    signup(bob, "bob")
    with site.st.maker() as s:
        s.get(User, _uid(site, "bob")).role = "curator"
        s.commit()
    curator = bob.get("/import").text
    assert "Import from a provider" in curator and 'aria-label="Administration"' not in curator
    assert bob.get("/admin/import").status_code == 403  # curators import from /import, not from administration
    assert bob.post("/admin/import", data={"csrf": token(bob, "/import"), "provider": "fakeprov",
                                           "external_id": "77"}).status_code == 403


def test_users_page_filters_changes_roles_and_explains_a_refusal(site):
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    uid = _uid(site, "bob")
    page = site.get("/admin/users?q=bo")
    assert 'data-user="bob"' in page.text and 'data-user="alice"' not in page.text
    assert 'id="uq" type="search" name="q" value="bo"' in page.text
    header = re.search(r'<form class="search".*?</form>', page.text, re.S).group(0)
    assert 'value="bo"' not in header  # the site search box is not filled by the filter
    t = token(site, "/admin/users")
    r = site.post(f"/admin/users/{uid}", data={"csrf": t, "role": "curator", "status": "active", "fq": "bo"})
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/users?q=bo")
    with site.st.maker() as s:
        assert s.get(User, uid).role == "curator"
    assert "curator" in site.get("/admin/users?role=curator").text
    bad = site.post(f"/admin/users/{uid}", data={"csrf": t, "role": "wizard", "status": "suspended"})
    assert bad.status_code == 400 and "Could not change bob: unknown role" in bad.text
    assert 'data-user="bob"' in bad.text  # the list and its controls are still there
    with site.st.maker() as s:
        assert (s.get(User, uid).role, s.get(User, uid).status) == ("curator", "active")
    assert site.get("/admin/users?role=nonsense&status=nonsense").status_code == 200
    like = site.get("/admin/users?q=%25_")
    assert like.status_code == 200 and 'data-user="bob"' not in like.text  # wildcards are literals
    assert fresh(site).get("/admin/users").status_code == 303
    other = fresh(site)
    signup(other, "carol")
    assert other.get("/admin/users").status_code == 403


def test_users_page_has_no_controls_for_your_own_account(site):
    signup(site, "alice")
    row = re.search(r'<tr data-user="alice">.*?</tr>', site.get("/admin/users").text, re.S).group(0)
    assert "<select" not in row and "cannot be changed" in row


def test_admin_retries_uncached_artwork_for_one_game(site):
    signup(site, "alice")
    with site.st.maker() as s:
        facets.set_artwork(s, site.st.ctx, site.gid, "curator",
                           [{"kind": "poster", "url": "https://img.example.invalid/a.png"}])
        art = s.scalars(select(Artwork)).one()
        art.fail_count, art.next_try_at = 4, datetime.now(UTC)
        s.commit()
    page = site.get(f"/game/{site.gid}").text
    assert "1 image not cached yet" in page and "Caching is switched off" in page and "Retry downloads now" not in page
    site.st.ctx.settings.media_cache_enabled = True
    page = site.get(f"/game/{site.gid}").text
    assert "Download failed 4 times" in page and "Retry downloads now" in page
    t = token(site, f"/game/{site.gid}")
    bob = fresh(site)
    signup(bob, "bob")
    assert bob.post(f"/game/{site.gid}/artwork/retry", data={"csrf": token(bob, "/me")}).status_code == 403
    r = site.post(f"/game/{site.gid}/artwork/retry", data={"csrf": t})
    assert r.status_code == 303 and r.headers["location"].endswith("?m=artretry")
    with site.st.maker() as s:
        art = s.scalars(select(Artwork)).one()
        assert (art.fail_count, art.next_try_at) == (0, None)
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "media_retry")).one().detail == {"images": 1}
    assert "Download retry scheduled" in site.get(r.headers["location"]).text


def test_peers_page_lists_failed_entries_and_retry_resolves_what_it_can(site):
    signup(site, "alice")
    node = "n" * 64
    now = datetime.now(UTC)
    with site.st.maker() as s:
        s.add(ReplicatedFeed(node_id=node, public_key="synthetic"))
        s.flush()
        s.add(ReplicatedChange(node_id=node, seq=1, kind="key_rotation", body={}, body_hash="0" * 64,
                               signature="s", key_id="k"))
        for seq, attempts in ((1, 3), (2, 20)):
            s.add(SyncFailure(node_id=node, seq=seq, kind="obs", error="ValueError: synthetic", attempts=attempts,
                              first_failed_at=now, last_attempt_at=now))
        s.commit()
    page = site.get("/admin/peers").text
    assert 'data-failure="1"' in page and 'data-failure="2"' in page and "next try" in page  # C13 backoff
    assert "ValueError: synthetic" in page
    r = site.post("/admin/peers/failures/retry", data={"csrf": token(site, "/admin/peers")})
    assert r.status_code == 200
    assert "Retried 2 entries: 1 applied, 1 still failing" in r.text
    assert 'data-failure="1"' not in r.text and 'data-failure="2"' in r.text
    assert "retained entry is missing" in r.text
    with site.st.maker() as s:
        by_seq = {f.seq: f for f in s.scalars(select(SyncFailure))}
        assert by_seq[1].resolved_at is not None and by_seq[2].resolved_at is None and by_seq[2].attempts == 21
    bob = fresh(site)
    signup(bob, "bob")
    assert bob.post("/admin/peers/failures/retry", data={"csrf": token(bob, "/me")}).status_code == 403


def test_peers_page_says_so_when_nothing_failed(site):
    signup(site, "alice")
    assert "No failed entries" in site.get("/admin/peers").text


def test_every_input_declares_its_type():
    """An <input> without a type is a text input the stylesheet used not to style (#97)."""
    untyped = []
    for path in TEMPLATES.glob("*.html"):
        for tag in re.findall(r"<input\b[^>]*>", path.read_text(encoding="utf-8")):
            if not re.search(r"\btype=", tag):
                untyped.append((path.name, tag[:80]))
    assert untyped == []


def test_templates_use_no_inline_style_attributes():
    """The content security policy has style-src 'self', so a style attribute is blocked (the /admin/backup error)."""
    offenders = [p.name for p in TEMPLATES.glob("*.html")
                 if re.search(r"<[^>]+\sstyle=", p.read_text(encoding="utf-8"))]
    assert offenders == []


# ---- federation compatibility on the peers page (atlas#33, CONTRACTS-v1 C1.6) -------------------------

def test_peers_page_shows_wire_and_parked_entries(site):
    from atlas.models import FollowerWire

    signup(site, "alice")
    node = "p" * 16
    now = datetime.now(UTC)
    with site.st.maker() as s:
        s.add(SyncFailure(node_id=node, seq=7, kind="relation", error="unsupported kind; kind 'relation'", attempts=1,
                          first_failed_at=now, last_attempt_at=now, disposition="parked", reason="unsupported_kind"))
        s.add(FollowerWire(token_id="t" * 26, refused="peer_v0", legacy_protocol="1.4", last_seen_at=now))
        s.add(FollowerWire(token_id="u" * 26, wire_min=1, wire_max=1, use=1, last_seen_at=now))
        s.commit()
    page = site.get("/admin/peers").text
    assert 'data-wire-max="1"' in page and "speaks wire 1" in page
    assert 'data-failure="7" data-disposition="parked"' in page and "unsupported kind" in page
    assert 'data-follower-refused="peer_v0"' in page and "(v0, 1.4)" in page and "(deleted token)" in page
    assert 'data-follower-wire="1"' in page


def test_users_page_one_save_per_row_keeps_the_filter_and_sets_external_sign_in(site):
    """#66: role, status and external sign-in are one form with one Save; Mark confirmed keeps the filter."""
    signup(site, "alice")
    bob = fresh(site)
    signup(bob, "bob")
    uid = _uid(site, "bob")
    with site.st.maker() as s:
        s.get(User, uid).email, s.get(User, uid).email_verified_at = "bob@example.invalid", None
        s.commit()
    page = site.get("/admin/users?q=bo").text
    row = re.search(r'<tr data-user="bob">.*?</tr>', page, re.S).group(0)
    assert row.count('action="/admin/users/') == 2  # the row form and Mark confirmed; no separate external form
    assert 'name="oidc"' in row and "Save bob" in row and f'action="/admin/users/{uid}/oidc"' not in row
    t = token(site, "/admin/users")
    r = site.post(f"/admin/users/{uid}", data={"csrf": t, "role": "user", "status": "active", "has_oidc": "1",
                                               "oidc": "on", "fq": "bo"})
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/users?q=bo")
    with site.st.maker() as s:
        assert s.get(User, uid).oidc_allowed is True
    r = site.post(f"/admin/users/{uid}", data={"csrf": t, "role": "user", "status": "active", "has_oidc": "1"})
    with site.st.maker() as s:
        assert s.get(User, uid).oidc_allowed is False  # unticked in a full-row form means off
    r = site.post(f"/admin/users/{uid}", data={"csrf": t, "role": "curator", "status": "active"})
    with site.st.maker() as s:  # an older client that does not send the field leaves it alone
        assert s.get(User, uid).oidc_allowed is False and s.get(User, uid).role == "curator"
    r = site.post(f"/admin/users/{uid}/verify", data={"csrf": t, "fq": "bo", "frole": "curator"})
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/users?q=bo&role=curator")
    assert "Group grants" not in site.get("/admin/users").text  # this node is in no group


def test_users_page_shows_group_grants_read_only(tmp_path, monkeypatch):
    """#66: rights given through the node group show next to the local role, and are not editable here."""
    from fastapi.testclient import TestClient

    from atlas import groups
    from atlas.ids import ulid
    from atlas.models import GroupAccountLink

    from .group_harness import ROOT_PW, Dispatcher, create, fast_kdf, finish_creation, make_node
    from .test_web import PW  # noqa: F401

    fast_kdf(monkeypatch)
    a = make_node(tmp_path, Dispatcher(), "a")
    try:
        browser = TestClient(a.app, base_url=a.settings.public_url, follow_redirects=False)
        signup(browser, "alice")
        other = TestClient(a.app, base_url=a.settings.public_url, follow_redirects=False)
        signup(other, "bob")
        create(a)
        finish_creation(a)
        account = ulid()
        with a.session() as s:
            bob_id = s.scalars(select(User).where(User.username == "bob")).one().id
            group = groups.load(s)
            s.add(GroupAccountLink(group_id=group.group_id, account_uid=account, user_id=bob_id, minted_here=True))
            s.flush()
            root = groups.unlock_root(s, a.settings, group, ROOT_PW)
            groups.issue_grant(s, a.settings, group, a.identity, account_uid=account, role="curator",
                               nodes=[a.node_id], days=30, root=root)
            s.commit()
        page = browser.get("/admin/users").text
        cell = re.search(r'<td data-label="Group grants" data-user-grants="bob">(.*?)</td>', page, re.S).group(1)
        assert '<span class="chip"' in cell and "curator" in cell
        alice = re.search(r'<td data-label="Group grants" data-user-grants="alice">(.*?)</td>', page, re.S).group(1)
        assert "none" in alice
        assert 'href="/admin/group/grants"' in page
        with a.session() as s:
            assert s.get(User, bob_id).role == "user"  # a grant is shown, never turned into a local role
    finally:
        a.close()
