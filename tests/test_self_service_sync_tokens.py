"""#84: members can create their own read-only sync token when the administrator allows it, so following a node
needs no administrator on that node. Off by default; no write scope ever; turning it off ends the sync access."""

from __future__ import annotations

import re

from sqlalchemy import select

from atlas import runtime
from atlas.models import AuditEntry, Token, User

from .test_web import fresh, signup, token
from .test_web import site as web_site  # noqa: F401  (the fixture, re-exported under another name)

SYNC = "/api/v2/sync/changes?since=0&limit=1"



def setp(site, **kv) -> None:
    with site.st.maker() as s:
        runtime.save(s, site.st.base, None, kv)
        s.commit()
        site.st.refresh(s, force=True)


def _member(site, name="bob"):
    signup(site, "alice")  # the first account is the administrator
    member = fresh(site)
    signup(member, name)
    return member


def _create(client, scope):
    return client.post("/me/tokens", data={"csrf": token(client, "/me"), "name": "my node", "scope": scope})


def _secret(html):
    return re.search(r"(atl_[A-Za-z0-9_-]+)", html).group(1)


def test_off_by_default_members_get_read_only_and_a_clear_reason(web_site):  # noqa: F811
    bob = _member(web_site)
    page = bob.get("/me").text
    assert 'value="sync"' not in page and "data-sync-help" in page
    assert "Admin → Tokens" in page and "http://localhost:8000" in page  # where to ask, and the address to use
    r = _create(bob, "sync")
    assert r.status_code == 403 and "No sync token" in r.text
    with web_site.st.maker() as s:
        assert s.scalars(select(Token).where(Token.name == "my node")).first() is None
    secret = _secret(_create(bob, "read").text)
    api = {"Authorization": f"Bearer {secret}"}
    assert web_site.get("/api/v2/games?q=hades", headers=api).status_code == 200
    assert web_site.get(SYNC, headers=api).status_code == 403


def test_enabled_member_creates_a_sync_token_that_follows_but_never_writes(web_site):  # noqa: F811
    setp(web_site, self_service_sync_tokens=True)
    bob = _member(web_site)
    assert 'value="sync"' in bob.get("/me").text
    r = _create(bob, "sync")
    assert r.status_code == 200 and "Use it with the address" in r.text
    secret = _secret(r.text)
    with web_site.st.maker() as s:
        row = s.scalars(select(Token).where(Token.name == "my node")).one()
        bob_id = s.scalars(select(User).where(User.username == "bob")).one().id
        assert row.scopes == ["read", "sync"] and row.user_id == bob_id
        audit = s.scalars(select(AuditEntry).where(AuditEntry.action == "token_create",
                                                   AuditEntry.target == row.id)).one()
        assert audit.detail == {"scopes": ["read", "sync"]}
    api = {"Authorization": f"Bearer {secret}"}
    feed = web_site.get(SYNC, headers=api)
    assert feed.status_code == 200 and "entries" in feed.json()
    body = {"connection": {"source_kind": "plugin", "source": "spam", "source_scope": "x"},
            "observations": [{"external_id": "1", "title": "Spam Game"}]}
    assert web_site.post("/api/v2/observations", json=body, headers=api).status_code == 403
    assert web_site.post("/api/v2/tokens", json={"name": "x", "scopes": ["admin"]}, headers=api).status_code == 403
    with web_site.st.maker() as s:  # even a widened row grants nothing beyond read and sync
        s.scalars(select(Token).where(Token.name == "my node")).one().scopes = ["read", "sync", "admin", "curate"]
        s.commit()
    assert web_site.post("/api/v2/tokens", json={"name": "x", "scopes": ["admin"]}, headers=api).status_code == 403
    assert web_site.get(SYNC, headers=api).status_code == 200
    # Turning the setting off ends the sync access of tokens already made, at once; reading still works.
    setp(web_site, self_service_sync_tokens=False)
    assert web_site.get(SYNC, headers=api).status_code == 403
    assert web_site.get("/api/v2/games?q=hades", headers=api).status_code == 200


def test_unverified_member_is_refused_when_the_node_requires_verification(web_site):  # noqa: F811
    signup(web_site, "alice")
    bob = fresh(web_site)
    signup(bob, "bob")  # no email; the node starts requiring verified addresses afterwards
    setp(web_site, self_service_sync_tokens=True, email_verification="required")
    page = bob.get("/me").text
    assert 'value="sync"' not in page and "confirm your email address first" in page
    assert _create(bob, "sync").status_code == 403


def test_admin_tokens_page_explains_the_flow(web_site):  # noqa: F811
    signup(web_site, "alice")
    page = web_site.get("/admin/tokens").text
    assert "data-token-flow" in page and 'href="/admin/settings"' in page and "read /metrics" in page
    setp(web_site, self_service_sync_tokens=True)
    assert "Members can also create their own" in web_site.get("/admin/tokens").text
    assert 'name="self_service_sync_tokens"' in web_site.get("/admin/settings").text
