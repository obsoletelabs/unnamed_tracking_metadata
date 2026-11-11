"""Regression tests for the third independent review."""

import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from atlas import accounts, custom, mail, oidc, social, sync
from atlas.contract import SourceObservationV1
from atlas.models import (
    Canonical,
    Comment,
    FieldDef,
    Follow,
    MediaType,
    Notification,
    OidcLink,
    Outbox,
    Report,
    User,
    UserSession,
    now,
)
from atlas.service import AtlasError, Ctx

from .conftest import build_settings
from .test_community import PW, user
from .test_oidc import PROVIDER, World, follow_callback
from .test_social import game  # noqa: F401
from .test_web import fresh, signup, token


def other_browser(w):
    return fresh(type("S", (), {"st_app": w.n_app}))


# ---- OIDC: state is bound to the browser that started the sign-in -------------------------------------

def test_a_callback_carried_to_another_browser_does_not_sign_anyone_in(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on")
        attacker = other_browser(w)
        callback, _ = w.sign_in_via(attacker, iid)  # the attacker completes consent but withholds the callback
        victim = other_browser(w)
        done = follow_callback(victim, callback)  # the victim is tricked into opening the attacker's callback URL
        assert done.status_code == 400 and "different browser" in done.text
        assert victim.get("/me").status_code == 303  # still signed out
        with w.n_app.state.atlas.maker() as s:
            assert s.scalars(select(OidcLink)).first() is None  # nothing was created or linked


def test_a_link_flow_cannot_be_completed_by_another_signed_in_user(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(allow_all_users="on")
        bob, carol = other_browser(w), other_browser(w)
        signup(bob, "bob")
        signup(carol, "carol")
        t = token(bob, "/me/settings")
        link = bob.post(f"/me/oidc/{iid}/link", data={"csrf": t})
        consent = w.provider.get(link.headers["location"][len(PROVIDER):])
        fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', consent.text))
        callback = w.provider.post("/oidc/authorize", data={**fields, "decision": "allow"}).headers["location"]
        for name, value in bob.cookies.items():
            if name == "atlas_oidc":
                carol.cookies.set(name, value, path="/oidc/callback")  # even with the browser binding copied over
        done = follow_callback(carol, callback)
        assert done.status_code == 400 and "another signed-in user" in done.text
        assert "carol" in carol.get("/me").text  # carol was not swapped into bob's session
        with w.n_app.state.atlas.maker() as s:
            assert s.scalars(select(OidcLink)).first() is None


def test_oidc_probe_and_token_parsing_survive_hostile_bodies():
    class Http:
        def __init__(self, bodies):
            self.bodies = list(bodies)

        def get(self, url):
            class R:
                def json(inner):
                    return self.bodies.pop(0)
            return R()

    for bodies in ([[1, 2]], [{"issuer": "http://x.test", "authorization_endpoint": "http://x.test/a",
                               "token_endpoint": "http://x.test/t", "jwks_uri": "http://x.test/j"}, ["nope"]]):
        with pytest.raises(AtlasError):
            oidc.probe_issuer(Http(bodies), "http://x.test")
    import base64
    bad_header = base64.urlsafe_b64encode(b"[1,2]").rstrip(b"=").decode()
    with pytest.raises(oidc.TokenError):
        oidc.verify_jwt(f"{bad_header}.e30.AAAA", public_key_b64="AAAA", kid="k", issuer="i", audience="a", nonce=None)


def test_disabling_an_application_ends_its_access_tokens(tmp_path):
    from .test_oidc import exchange, issue
    with World(tmp_path) as w:
        cid, secret, code = issue(w)
        body = exchange(w, cid, secret, code, "https://app.test/cb", "v" * 50).json()
        h = {"Authorization": "Bearer " + body["access_token"]}
        assert w.provider.get("/oidc/userinfo", headers=h).status_code == 200
        with w.p_app.state.atlas.maker() as s:
            s.get(oidc.OidcClient, cid).enabled = False
            s.commit()
        assert w.provider.get("/oidc/userinfo", headers=h).status_code == 401


def test_failed_client_authentication_is_throttled_without_blocking_real_clients(tmp_path):
    from .test_oidc import exchange, issue
    with World(tmp_path) as w:
        codes = [w.provider.post("/oidc/token", data={"grant_type": "authorization_code", "client_id": "nope",
                                                       "client_secret": "x"}).status_code for _ in range(35)]
        assert 429 in codes and codes[0] == 401
        cid, secret, code = issue(w)
        assert exchange(w, cid, secret, code, "https://app.test/cb", "v" * 50).status_code == 200


def test_oidc_only_accounts_can_confirm_actions_shortly_after_signing_in(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on")
        b = other_browser(w)
        follow_callback(b, w.sign_in_via(b, iid)[0])
        t = token(b, "/me/settings")
        mail_change = b.post("/me/email", data={"csrf": t, "email": "dave@example.org"})
        assert mail_change.status_code == 303  # no password exists; the fresh sign-in is the confirmation
        with w.n_app.state.atlas.maker() as s:
            for row in s.scalars(select(UserSession)):
                row.created_at = now() - timedelta(minutes=30)
            s.commit()
        stale = b.post("/me/email", data={"csrf": t, "email": "other@example.org"})
        assert stale.status_code == 400 and "sign in again" in stale.text
        assert b.post("/me/delete", data={"csrf": t, "confirm": "dave"}).status_code == 400
        # a fresh sign-in allows deleting without any password
        b2 = other_browser(w)
        follow_callback(b2, w.sign_in_via(b2, iid)[0])
        t2 = token(b2, "/me/settings")
        assert b2.post("/me/delete", data={"csrf": t2, "confirm": "dave"}).status_code == 303
        with w.n_app.state.atlas.maker() as s:
            assert s.scalars(select(OidcLink)).first() is None


# ---- replication from a misbehaving upstream ----------------------------------------------------------

def test_hostile_schema_snapshots_are_filtered_not_applied(session, ctx):
    custom.seed(session)
    session.add(Canonical(id="atlas:used", view={}, explain={}, media_type="legacy"))
    session.add(MediaType(key="legacy", label="Legacy", origin="upstream"))
    session.commit()
    custom.apply_snapshot(session, {
        "types": [{"key": 'm/../x"', "label": "bad"}, {"key": "movie", "label": "Movie", "labels": {
            "developer": "Director\n<script>", "bogus": "x"}}, {"label": "no key"}, "junk",
                  {"key": "big", "label": "x" * 100_000}],
        "fields": [{"media_type": "movie", "key": "Bad Key", "label": "x", "kind": "text"},
                   {"media_type": "movie", "key": "evil", "label": "x", "kind": "weird"},
                   {"media_type": "movie", "key": "nolabel", "kind": "text"},
                   {"media_type": "movie", "key": "title", "label": "T", "kind": "text"},
                   {"media_type": "movie", "key": "ok_field", "label": "OK\x00 field", "kind": "text"}, 5]})
    session.commit()
    types = {t.key: t for t in session.scalars(select(MediaType))}
    assert set(types) == {"game", "legacy", "anime", "movie", "tv_show", "series", "collection", "book"}
    assert types["movie"].labels == {"developer": "Director", "publisher": "Studio", "series": "Series"}
    fields = {f.key: f for f in session.scalars(select(FieldDef))}
    assert fields["ok_field"].label == "OK field"
    assert not {"title", "evil", "nolabel"}.intersection(fields)
    assert types["legacy"].enabled is False  # records still use it: kept, not deleted
    assert session.get(Canonical, "atlas:used").media_type == "legacy"
    custom.apply_snapshot(session, {"types": "nope", "fields": None})  # garbage shapes are ignored too


def test_malformed_feed_entries_are_skipped_without_stalling(session, ctx):
    session.add(Canonical(id="atlas:g1", view={}, explain={}))
    session.commit()
    good_obs = {"id": "A" * 26, "source_kind": "manual", "source": "curator", "source_scope": "s", "external_id": "1",
                "payload": {"title": "Fine"}, "source_updated_at": None, "observed_at": now().isoformat()}
    cases = [
        ("obs", {"canonical_id": "atlas:g1", "observation": {**good_obs, "payload": {"title": "x" * 600}}}),
        ("obs", {"canonical_id": "atlas:g1", "observation": {**good_obs, "payload": {"surprise": 1}}}),
        ("obs", {"canonical_id": "atlas:g1", "observation": {**good_obs, "source_kind": "evil"}}),
        ("obs", {"canonical_id": "atlas:g1", "observation": {**good_obs, "id": "short"}}),
        ("obs", {"canonical_id": "atlas:g1", "observation": {**good_obs, "payload": {"facts": {"tags": ["x"] * 500}}}}),
        ("obs", {"canonical_id": "not-an-id", "observation": good_obs}),
        ("obs", {"observation": good_obs}), ("obs", {}), ("schema", {"types": 5}), ("custom", {"canonical_id": "atlas:g1"}),
        ("custom", {"canonical_id": "atlas:g1", "source": "x", "values": {"nope": "javascript:alert(1)"}}),
        ("lock", {"canonical_id": "atlas:g1", "field": "title", "value": 5}), ("merge", {"src": "a"}), ("detach", {}),
        ("artwork", {"canonical_id": "atlas:g1", "source": "x", "items": [{"kind": "x", "url": "javascript:1"}]}),
        ("mystery", {"anything": 1}),
    ]
    for kind, body in cases:
        sync.apply_entry(session, ctx, "node", 1, kind, body)  # none of these may raise
    session.commit()
    assert session.scalars(select(Canonical.id)).all() == ["atlas:g1"]
    sync.apply_entry(session, ctx, "node", 2, "obs", {"canonical_id": "atlas:g1", "observation": good_obs})
    session.commit()
    assert session.get(Canonical, "atlas:g1").view["title"] == "Fine"  # a good entry after bad ones still applies


def test_replicated_custom_values_are_validated_like_typed_ones(session, ctx):
    custom.seed(session)
    custom.save_field(session, ctx, "game", "site", "Website", "url")
    session.add(Canonical(id="atlas:g1", view={}, explain={}))
    session.commit()
    sync.apply_entry(session, ctx, "n", 1, "custom", {"canonical_id": "atlas:g1", "source": "curator",
                                                       "values": {"site": "javascript:alert(1)"}})
    assert custom.merged(session, ctx, "atlas:g1") == {}
    sync.apply_entry(session, ctx, "n", 2, "custom", {"canonical_id": "atlas:g1", "source": "curator",
                                                       "values": {"site": "https://ok.example"}})
    assert custom.merged(session, ctx, "atlas:g1") == {"site": "https://ok.example"}


# ---- email: nothing can poison the queue --------------------------------------------------------------

def test_line_breaks_in_titles_cannot_reach_headers_or_wedge_the_worker(session, ctx, maker, monkeypatch):
    item = SourceObservationV1.model_validate({"external_id": "1", "title": "Evil\nBcc: a@b.c\r\nX: y"})
    assert "\n" not in item.title and "\r" not in item.title
    row = mail.queue(session, "a@example.org\nBcc: x@y.z", "Subject\nInjected: header", "body")
    assert "\n" not in row.to_address and "\n" not in row.subject
    first = mail.queue(session, "a@example.org", "Poison", "body")
    mail.queue(session, "b@example.org", "Healthy", "body")
    session.commit()
    sent = []

    def fake(settings, r):
        if r.id == first.id:
            raise ValueError("header values must not contain newlines")
        sent.append(r.subject)

    monkeypatch.setattr(mail, "_send_smtp", fake)
    s = build_settings(mail_backend="smtp", smtp_host="mail.test")
    assert mail.deliver_pending(maker, s) >= 1
    assert "Healthy" in sent  # the healthy message behind the poisoned one still goes out
    session.refresh(first)
    assert first.attempts == 1 and "ValueError" in first.error


# ---- accounts and data --------------------------------------------------------------------------------

def test_regexes_do_not_accept_a_trailing_newline():
    assert accounts.USERNAME.match("abc\n") is None and accounts.USERNAME.match("abc") is not None
    assert accounts.EMAIL.match("a@b.co\n") is None and accounts.EMAIL.match("a@b.co") is not None
    from atlas.runtime import _COLOR, _SLUG
    assert _COLOR.match("#ffffff\n") is None and _SLUG.match("page\n") is None
    assert custom._KEY.match("key\n") is None and custom._TYPE.match("movie\n") is None


def test_deleting_an_account_scrubs_mail_links_and_mentions(session, ctx, game):  # noqa: F811
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bobby")
    carol = user(session, ctx.settings, "carol")
    bob.email = "bob@example.org"
    mail.queue(session, "bob@example.org", "Confirm", "link")
    social.notify(session, ctx, carol.id, "reply", "bobby replied to your comment", "/", actor_id=bob.id)
    mail.queue(session, "carol@example.org", "[Atlas] bobby replied to your comment", "x")
    rep = social.add_comment(session, ctx, carol, "hello", canonical_id=game)
    session.add(Report(comment_id=rep.id, reporter_id=bob.id, reason="personal reason mentioning bob"))
    session.flush()
    social.delete_account(session, bob)
    session.flush()
    assert session.scalars(select(Outbox).where(Outbox.to_address == "bob@example.org")).first() is None
    assert all("bobby" not in n.title for n in session.scalars(select(Notification)))
    assert all("bobby" not in m.subject for m in session.scalars(select(Outbox)))
    assert session.scalars(select(Report)).one().reason == ""
    assert admin.role == "admin"


def test_authors_cannot_delete_a_hidden_comment_to_dodge_moderation(session, ctx, game):  # noqa: F811
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob", rep=10)  # the hide below costs 3, which must not block the next comment
    carol = user(session, ctx.settings, "carol")
    row = social.add_comment(session, ctx, bob, "bad words", canonical_id=game)
    social.report_comment(session, ctx, carol, row.id, "abusive")
    social.moderate(session, ctx, admin, row.id, "hide")
    with pytest.raises(AtlasError, match="waiting for a moderator"):
        social.delete_comment(session, bob, row.id)
    assert session.get(Comment, row.id).status == "hidden"
    social.moderate(session, ctx, admin, row.id, "restore")
    other = social.add_comment(session, ctx, bob, "meh", canonical_id=game)
    social.report_comment(session, ctx, carol, other.id, "annoying")
    social.delete_comment(session, bob, other.id)  # a visible one may go, and its reports close with it
    assert session.scalars(select(Report).where(Report.comment_id == other.id)).one().status == "dismissed"


def test_going_private_stops_follows_and_activity_notifications(session, ctx, game):  # noqa: F811
    from atlas import proposals
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob", rep=100)
    carol = user(session, ctx.settings, "carol")
    social.follow(session, ctx, carol, "user", bob.id)
    bob.profile_public = False
    p = proposals.create(session, ctx, bob, "edit", game, {"series": "S"})
    assert p.status == "approved"
    assert [n.kind for n in session.scalars(select(Notification).where(Notification.user_id == carol.id))] == []
    assert admin.role == "admin"
    social.unfollow(session, carol, "user", bob.id)
    assert session.scalars(select(Follow)).first() is None


_ = (json, Ctx, User, PW)
