import re
import smtplib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import accounts, mail, main, proposals, service
from atlas.models import EmailToken, Outbox, User, now
from atlas.service import AtlasError, Ctx
from atlas.signing import load_identity

from .conftest import batch, build_settings
from .test_community import PW, user
from .test_web import fresh, login, site, token  # noqa: F401

LINK = re.compile(r"https?://[^\s/]+(/(?:verify|reset)/[A-Za-z0-9_-]+)")


def mails(site):  # noqa: F811
    with site.st.maker() as s:
        return list(s.scalars(select(Outbox).order_by(Outbox.created_at)))


def link_in(site, kind, nth=-1):  # noqa: F811
    found = [LINK.search(m.body).group(1) for m in mails(site) if m.body and f"/{kind}/" in m.body]
    return found[nth]


def register(client, name, email="", pw=PW):
    t = token(client, "/register")
    return client.post("/register", data={"csrf": t, "username": name, "password": pw, "email": email})


@pytest.fixture
def central(tmp_path):
    """An authority: verification is required by default."""
    s = build_settings(database_url=f"sqlite:///{tmp_path}/c.db", bootstrap_admin_token="c" * 40,
                       media_cache_enabled=False, mail_backend="outbox", email_verification=None)
    app = main.create_app(s)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        c.st = app.state.atlas
        c.st_app = app
        yield c


@pytest.fixture
def usernode(tmp_path):
    s = build_settings(role="local", database_url=f"sqlite:///{tmp_path}/u.db", media_cache_enabled=False,
                       email_verification=None)
    app = main.create_app(s)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        c.st = app.state.atlas
        c.st_app = app
        yield c


def test_registration_requires_email_on_central_and_sends_verification(central):
    register(central, "alice", "alice@example.org")  # first account: the administrator
    other = fresh(central)
    no_mail = register(other, "bob")
    assert no_mail.status_code == 400 and "email address is required" in no_mail.text
    ok = register(other, "bob", "bob@example.org")
    assert ok.status_code == 303 and "m=mailsent" in ok.headers["location"]
    sent = [m for m in mails(central) if m.to_address == "bob@example.org"]
    assert len(sent) == 1 and "Confirm your email" in sent[0].subject and "/verify/" in sent[0].body


def test_verification_link_needs_a_post_and_works_once(central):
    register(central, "alice", "alice@example.org")
    bob = fresh(central)
    register(bob, "bob", "bob@example.org")
    path = link_in(central, "verify")
    assert bob.get(path).status_code == 200  # a mail scanner opening the link changes nothing
    with central.st.maker() as s:
        assert s.scalars(select(User).where(User.username == "bob")).one().email_verified_at is None
    t = token(bob, path)
    assert bob.post(path, data={"csrf": t}).status_code == 303
    with central.st.maker() as s:
        assert s.scalars(select(User).where(User.username == "bob")).one().email_verified_at is not None
    again = fresh(central)
    assert again.post(path, data={"csrf": token(again, "/forgot")}).status_code == 400  # single use


def test_unverified_users_cannot_contribute_on_central_but_can_on_a_user_node(central, usernode):
    register(central, "alice", "alice@example.org")
    bob = fresh(central)
    register(bob, "bob", "bob@example.org")
    with central.st.maker() as s:
        [r] = service.ingest(s, central.st.ctx, batch("igdb", [{"external_id": "1", "title": "Hades",
                             "release_date": "2020-09-17"}]), system=True)
        s.commit()
    t = token(bob, f"/game/{r.canonical_id}/edit") if bob.get(f"/game/{r.canonical_id}/edit").status_code == 200 else ""
    blocked = bob.post(f"/game/{r.canonical_id}/edit", data={"csrf": t, "developer": "Dev"})
    assert blocked.status_code == 400 and "confirm your email" in blocked.text
    path = link_in(central, "verify")
    bob.post(path, data={"csrf": token(bob, path)})
    t = token(bob, f"/game/{r.canonical_id}/edit")
    assert bob.post(f"/game/{r.canonical_id}/edit", data={"csrf": t, "developer": "Dev"}).status_code == 303
    # a user node asks for nothing
    register(usernode, "carol")
    dave = fresh(usernode)
    register(dave, "dave")
    with usernode.st.maker() as s:
        [g] = service.ingest(s, usernode.st.ctx, batch("igdb", [{"external_id": "1", "title": "Hades"}]), system=True)
        s.commit()
    t = token(dave, f"/game/{g.canonical_id}/edit")
    assert dave.post(f"/game/{g.canonical_id}/edit", data={"csrf": t, "developer": "Dev"}).status_code == 303


def test_admin_is_exempt_and_can_mark_confirmed(central):
    register(central, "alice", "alice@example.org")
    with central.st.maker() as s:
        alice = s.scalars(select(User).where(User.username == "alice")).one()
        assert mail.is_verified(central.st.ctx.settings, alice)  # admin: never locked out
    bob = fresh(central)
    register(bob, "bob", "bob@example.org")
    with central.st.maker() as s:
        uid = s.scalars(select(User).where(User.username == "bob")).one().id
    t = token(central, "/admin")
    assert central.post(f"/admin/users/{uid}/verify", data={"csrf": t}).status_code == 303
    with central.st.maker() as s:
        assert s.get(User, uid).email_verified_at is not None


def test_password_reset_end_to_end(central):
    register(central, "alice", "alice@example.org")
    bob = fresh(central)
    register(bob, "bob", "bob@example.org")
    other = fresh(central)
    t = token(other, "/forgot")
    r = other.post("/forgot", data={"csrf": t, "who": "bob@example.org"})
    assert r.status_code == 200 and "we have sent it a message" in r.text
    path = link_in(central, "reset")
    page = other.get(path)
    assert page.status_code == 200 and page.headers["referrer-policy"] == "no-referrer"
    assert "Choose a new password" in page.text and "invalid" not in page.text
    t = token(other, path)
    assert other.post(path, data={"csrf": t, "password": "short"}).status_code == 400
    assert other.post(path, data={"csrf": t, "password": "a brand new password"}).status_code == 303
    assert bob.get("/me").status_code == 303  # existing sessions are gone
    assert login(fresh(central), "bob", PW).status_code == 400
    assert login(fresh(central), "bob", "a brand new password").status_code == 303
    with central.st.maker() as s:
        assert s.scalars(select(User).where(User.username == "bob")).one().email_verified_at is not None
    again = fresh(central)
    assert again.post(path, data={"csrf": token(again, "/forgot"), "password": "another new password"}).status_code == 400


def test_reset_gives_the_same_answer_for_unknown_accounts_and_is_throttled(central):
    register(central, "alice", "alice@example.org")
    c = fresh(central)
    known = c.post("/forgot", data={"csrf": token(c, "/forgot"), "who": "alice"}).text
    unknown = c.post("/forgot", data={"csrf": token(c, "/forgot"), "who": "nobody@example.org"}).text
    strip = lambda h: re.sub(r'name="csrf" value="[^"]+"', "", h)  # noqa: E731
    assert strip(known) == strip(unknown)
    assert len([m for m in mails(central) if "Reset your" in m.subject]) == 1  # nothing for the unknown one
    codes = [c.post("/forgot", data={"csrf": token(c, "/forgot"), "who": f"x{i}@example.org"}).status_code
             for i in range(8)]
    assert 429 in codes


def test_expired_and_replaced_tokens_are_refused(central):
    register(central, "alice", "alice@example.org")
    c = fresh(central)
    c.post("/forgot", data={"csrf": token(c, "/forgot"), "who": "alice"})
    first = link_in(central, "reset")
    c.post("/forgot", data={"csrf": token(c, "/forgot"), "who": "alice"})
    second = link_in(central, "reset")
    assert first != second
    assert "invalid" in c.get(first).text  # a newer request invalidates the older link
    with central.st.maker() as s:
        row = s.scalars(select(EmailToken).where(EmailToken.purpose == "reset")).one()
        row.expires_at = now() - timedelta(minutes=1)
        s.commit()
    assert "invalid, already used, or has expired" in c.get(second).text
    assert c.post(second, data={"csrf": token(c, "/forgot"), "password": "a brand new password"}).status_code == 400


def test_changing_email_resets_verification_and_warns_the_old_address(central):
    register(central, "alice", "alice@example.org")
    t = token(central, "/me")
    assert central.post("/me/email", data={"csrf": t, "email": "new@example.org", "current": "wrong password"}
                        ).status_code == 400
    assert central.post("/me/email", data={"csrf": t, "email": "new@example.org", "current": PW}).status_code == 303
    to = {m.to_address: m.subject for m in mails(central)}
    assert "was changed" in to["alice@example.org"] and "Confirm your email" in to["new@example.org"]
    with central.st.maker() as s:
        u = s.scalars(select(User).where(User.username == "alice")).one()
        assert u.email == "new@example.org" and u.email_verified_at is None
    bob = fresh(central)
    register(bob, "bob", "bob@example.org")
    assert central.post("/me/email", data={"csrf": t, "email": "bob@example.org", "current": PW}).status_code == 409


def test_email_forms_require_csrf(central):
    register(central, "alice", "alice@example.org")
    assert central.post("/me/email", data={"email": "x@example.org", "current": PW}).status_code in (403, 422)
    assert central.post("/me/verify", data={}).status_code in (403, 422)
    c = fresh(central)
    assert c.post("/forgot", data={"who": "alice"}).status_code in (403, 422)
    assert c.post("/reset/abc", data={"password": "a brand new password"}).status_code in (403, 422)


def test_outbox_is_admin_only_and_shows_bodies_only_for_the_outbox_backend(central):
    register(central, "alice", "alice@example.org")
    assert "Confirm your email" in central.get("/admin/outbox").text and "/verify/" in central.get("/admin/outbox").text
    bob = fresh(central)
    register(bob, "bob", "bob@example.org")
    assert bob.get("/admin/outbox").status_code == 403


# ---- SMTP delivery ------------------------------------------------------------------------------------

class FakeSMTP:
    sent: list = []
    fail = 0

    def __init__(self, host, port, timeout=None, **kw):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kw):
        pass

    def login(self, user, password):
        FakeSMTP.login_args = (user, password)

    def send_message(self, msg):
        if FakeSMTP.fail:
            FakeSMTP.fail -= 1
            raise smtplib.SMTPException("temporary failure")
        FakeSMTP.sent.append(msg)


def test_smtp_delivery_clears_bodies_and_retries_failures(session, maker, monkeypatch):
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent, FakeSMTP.fail = [], 1
    s = build_settings(mail_backend="smtp", smtp_host="mail.test", smtp_user="u", smtp_password="pw",
                       mail_from="noreply@example.org")
    mail.queue(session, "a@example.org", "Hello", "secret body with /reset/abc")
    session.commit()
    assert mail.deliver_pending(maker, s) == 0  # first attempt fails
    row = session.scalars(select(Outbox)).one()
    session.refresh(row)
    assert row.sent_at is None and row.attempts == 1 and "SMTPException" in row.error and row.body
    assert mail.deliver_pending(maker, s) == 0  # backoff: not yet
    row.next_attempt_at = now() - timedelta(seconds=1)
    session.commit()
    assert mail.deliver_pending(maker, s) == 1
    session.refresh(row)
    assert row.sent_at is not None and row.body is None and row.error is None
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "a@example.org" and "secret body" in msg.get_content()
    assert FakeSMTP.login_args == ("u", "pw")


def test_outbox_backend_never_sends(session, maker, monkeypatch):
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    mail.queue(session, "a@example.org", "Hello", "body")
    session.commit()
    assert mail.deliver_pending(maker, build_settings(mail_backend="outbox", smtp_host="mail.test")) == 0
    assert mail.deliver_pending(maker, build_settings(mail_backend="smtp", smtp_host="")) == 0
    assert FakeSMTP.sent == []


def test_gives_up_after_max_attempts(session, maker, monkeypatch):
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent, FakeSMTP.fail = [], 100
    s = build_settings(mail_backend="smtp", smtp_host="mail.test")
    row = mail.queue(session, "a@example.org", "Hello", "body")
    session.commit()
    for _ in range(10):
        session.refresh(row)
        row.next_attempt_at = now() - timedelta(seconds=1)
        session.commit()
        mail.deliver_pending(maker, s)
    session.refresh(row)
    assert row.attempts == mail.MAX_ATTEMPTS and row.sent_at is None


def test_proposals_gate_uses_the_live_policy(session):
    s = build_settings(role="local")
    c = Ctx(s, load_identity(session))
    user(session, s, "alice")
    bob = user(session, s, "bob")
    [r] = service.ingest(session, c, batch("igdb", [{"external_id": "1", "title": "Hades"}]), system=True)
    proposals.create(session, c, bob, "edit", r.canonical_id, {"series": "a"})  # optional: allowed
    strict = s.model_copy(update={"email_verification": "required"})
    with pytest.raises(AtlasError, match="confirm your email"):
        proposals.create(session, Ctx(strict, c.identity), bob, "edit", r.canonical_id, {"series": "b"})
    bob.email_verified_at = now()
    proposals.create(session, Ctx(strict, c.identity), bob, "edit", r.canonical_id, {"series": "c"})
    assert accounts.can_review(s, bob) is False
