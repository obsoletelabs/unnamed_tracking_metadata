from datetime import timedelta

import pytest
from sqlalchemy import select

from atlas import accounts, feed, proposals, service
from atlas.models import Artwork, Canonical, Proposal, ReputationEvent, UserSession, now
from atlas.service import AtlasError

from .conftest import batch, build_settings

PW = "correct horse battery"


def user(session, settings, name, role=None, rep=0):
    u = accounts.register(session, settings, name, PW, f"{name}@example.org")
    if role:
        u.role = role
    u.reputation = rep
    session.flush()
    return u


@pytest.fixture
def game(session, ctx):
    [r] = service.ingest(session, ctx, batch("igdb", [{"external_id": "1", "title": "Hades",
                                                       "release_date": "2020-09-17",
                                                       "facts": {"developer": "Wrong Dev"}}]), system=True)
    session.commit()
    return r.canonical_id


def view(session, cid):
    session.flush()
    session.expire_all()
    return session.get(Canonical, cid).view


def test_registration_rules(session, settings):
    admin = accounts.register(session, settings, "Alice", PW)
    bob = accounts.register(session, settings, "bob", PW, "bob@example.org")
    assert admin.role == "admin" and bob.role == "user" and admin.username == "alice"
    for args in (("al", PW), ("bad name", PW), ("carol", "short"), ("carol", PW, "not-an-email"),
                 ("bob", PW), ("dave", PW, "bob@example.org")):
        with pytest.raises(AtlasError):
            accounts.register(session, settings, *args)
    with pytest.raises(AtlasError, match="closed"):
        accounts.register(session, build_settings(registration="closed"), "erin", PW)


def test_password_hashing_and_authentication(session, settings):
    u = accounts.register(session, settings, "alice", PW)
    assert u.password_hash.startswith("scrypt$") and PW not in u.password_hash
    assert accounts.authenticate(session, "ALICE", PW).id == u.id
    assert accounts.authenticate(session, "alice", "wrong password!") is None
    assert accounts.authenticate(session, "nobody", PW) is None
    u.status = "suspended"
    assert accounts.authenticate(session, "alice", PW) is None
    assert not accounts.verify_password(PW, "garbage") and not accounts.verify_password(PW, "scrypt$1$2$3$zz$zz")


def test_sessions_expire_end_and_suspend(session, settings):
    u = accounts.register(session, settings, "alice", PW)
    secret, row = accounts.create_session(session, settings, u)
    assert row.id != secret  # only the hash is stored
    assert accounts.session_user(session, secret)[0].id == u.id
    assert accounts.check_csrf(row, row.csrf)
    assert not accounts.check_csrf(row, "x") and not accounts.check_csrf(row, None)
    row.expires_at = now() - timedelta(seconds=1)
    assert accounts.session_user(session, secret) is None
    secret2, _ = accounts.create_session(session, settings, u)
    accounts.end_session(session, secret2)
    assert accounts.session_user(session, secret2) is None
    secret3, _ = accounts.create_session(session, settings, u)
    admin = accounts.register(session, settings, "boss", PW)
    admin.role = "admin"
    accounts.set_status(session, admin, u, "suspended")
    assert accounts.session_user(session, secret3) is None
    assert session.scalars(select(UserSession).where(UserSession.user_id == u.id)).first() is None


def test_role_changes_need_an_admin(session, settings):
    admin = user(session, settings, "alice")
    bob = user(session, settings, "bob")
    with pytest.raises(AtlasError):
        accounts.set_role(session, bob, bob, "admin")
    accounts.set_role(session, admin, bob, "curator")
    assert bob.role == "curator"
    with pytest.raises(AtlasError, match="demote yourself"):
        accounts.set_role(session, admin, admin, "user")


def test_pending_edit_applies_on_approval_and_rewards(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    p = proposals.create(session, ctx, bob, "edit", game, {"developer": "Supergiant Games", "tags": ["Roguelike"]},
                         "see the store page")
    assert p.status == "pending" and view(session, game)["developer"] == "Wrong Dev"
    with pytest.raises(AtlasError, match="review rights"):
        proposals.review(session, ctx, bob, p.id, approve=True)
    proposals.review(session, ctx, admin, p.id, approve=True, note="verified")
    v = view(session, game)
    assert v["developer"] == "Supergiant Games" and v["tags"] == ["Roguelike"]  # community (85) > igdb (70)
    assert v["title"] == "Hades" and bob.reputation == proposals.REWARD["edit"]
    with pytest.raises(AtlasError, match="approved"):
        proposals.review(session, ctx, admin, p.id, approve=False)


def test_cannot_review_own_proposal_unless_admin(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    bob.role = "curator"
    p = Proposal(kind="edit", canonical_id=game, proposer_id=bob.id, payload={"series": "x"})
    session.add(p)
    session.flush()
    with pytest.raises(AtlasError, match="own proposal"):
        proposals.review(session, ctx, bob, p.id, approve=True)
    other = Proposal(kind="edit", canonical_id=game, proposer_id=admin.id, payload={"series": "y"})
    session.add(other)
    session.flush()
    proposals.review(session, ctx, admin, other.id, approve=True)  # an admin may approve their own
    assert other.status == "approved"
    reviewer = user(session, ctx.settings, "dave", rep=ctx.settings.curator_reputation)
    assert accounts.can_review(ctx.settings, reviewer)


def test_rejection_penalises_and_withdraw(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob", rep=10)
    p = proposals.create(session, ctx, bob, "edit", game, {"developer": "Spam Inc"})
    proposals.review(session, ctx, admin, p.id, approve=False, note="no source")
    assert p.status == "rejected" and bob.reputation == 10 + proposals.REJECT_PENALTY
    assert view(session, game)["developer"] == "Wrong Dev"
    q = proposals.create(session, ctx, bob, "edit", game, {"developer": "Other"})
    proposals.withdraw(session, bob, q.id)
    assert q.status == "withdrawn"
    with pytest.raises(AtlasError):
        proposals.withdraw(session, user(session, ctx.settings, "mallory"), q.id)


def test_trusted_edits_apply_immediately_and_can_be_reverted(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    trusted = user(session, ctx.settings, "bob", rep=ctx.settings.auto_approve_reputation)
    p = proposals.create(session, ctx, trusted, "edit", game, {"developer": "Supergiant Games"})
    assert p.status == "approved" and p.auto_applied and view(session, game)["developer"] == "Supergiant Games"
    q = proposals.create(session, ctx, trusted, "edit", game, {"developer": "Vandalised", "title": "HADES!!!"})
    assert view(session, game)["title"] == "HADES!!!"
    before = trusted.reputation
    proposals.revert(session, ctx, admin, q.id, "vandalism")
    v = view(session, game)
    assert v["title"] == "Hades" and v["developer"] == "Supergiant Games" and q.status == "reverted"
    assert trusted.reputation == before + proposals.REVERT_PENALTY  # self-approved work earned nothing to lose
    assert trusted.reputation < ctx.settings.auto_approve_reputation  # the revert cost them their fast lane
    trusted.reputation = 100
    r = proposals.create(session, ctx, trusted, "edit", game, {"series": "S"})
    with pytest.raises(AtlasError, match="newer"):
        proposals.revert(session, ctx, admin, p.id)
    proposals.revert(session, ctx, admin, r.id)
    proposals.revert(session, ctx, admin, p.id)
    assert view(session, game)["developer"] == "Wrong Dev"  # back to the provider value
    events = session.scalars(select(ReputationEvent).where(ReputationEvent.user_id == trusted.id)).all()
    assert [e.reason for e in events][-1] == "reverted:edit"


def test_edits_by_different_users_combine_and_latest_wins_per_field(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    a = user(session, ctx.settings, "bob", rep=100)
    b = user(session, ctx.settings, "carol", rep=100)
    proposals.create(session, ctx, a, "edit", game, {"developer": "Dev A", "publisher": "Pub A"})
    proposals.create(session, ctx, b, "edit", game, {"developer": "Dev B", "series": "Series B"})
    v = view(session, game)
    assert (v["developer"], v["publisher"], v["series"]) == ("Dev B", "Pub A", "Series B")
    assert admin.reputation == 0


@pytest.mark.parametrize("payload", [
    {}, {"title": ""}, {"release_date": "17/09/2020"}, {"release_date": "2020-13-45"}, {"secret": "x"},
    {"tags": "not-a-list"}, {"tags": ["x" * 200]}, {"developer": "x" * 500},
])
def test_invalid_edits_rejected(session, ctx, game, payload):
    bob = user(session, ctx.settings, "bob")
    with pytest.raises(AtlasError):
        proposals.create(session, ctx, bob, "edit", game, payload)


def test_limits_on_pending_and_daily_proposals(session, game):
    s = build_settings(max_pending_proposals=3, new_user_daily_proposals=2)
    from atlas.service import Ctx
    from atlas.signing import load_identity
    c = Ctx(s, load_identity(session))
    user(session, s, "alice")  # the first account is the administrator
    bob = user(session, s, "bob")
    proposals.create(session, c, bob, "edit", game, {"series": "a"})
    proposals.create(session, c, bob, "edit", game, {"series": "b"})
    with pytest.raises(AtlasError, match="daily"):
        proposals.create(session, c, bob, "edit", game, {"series": "c"})
    old = session.scalars(select(Proposal)).all()
    for p in old:
        p.created_at = now() - timedelta(days=2)
    session.flush()
    proposals.create(session, c, bob, "edit", game, {"series": "c"})
    with pytest.raises(AtlasError, match="pending"):
        proposals.create(session, c, bob, "edit", game, {"series": "d"})


def test_suspended_and_unknown_targets(session, ctx, game):
    bob = user(session, ctx.settings, "bob")
    with pytest.raises(AtlasError, match="unknown game"):
        proposals.create(session, ctx, bob, "edit", "atlas:nope", {"series": "x"})
    bob.status = "suspended"
    with pytest.raises(AtlasError, match="suspended"):
        proposals.create(session, ctx, bob, "edit", game, {"series": "x"})


def test_new_game_proposal_creates_a_record_with_ids(session, ctx):
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    p = proposals.create(session, ctx, bob, "new_game", None, {
        "title": "Brand New Game", "release_date": "2024-02-02", "developer": "Indie Co", "platforms": ["pc"],
        "provider_ids": {"steam": "424242"}})
    proposals.review(session, ctx, admin, p.id, approve=True)
    assert p.canonical_id
    v = view(session, p.canonical_id)
    assert v["title"] == "Brand New Game" and v["platforms"] == ["pc"] and v["provider_ids"]["steam"] == "424242"
    with pytest.raises(AtlasError, match="title is required"):
        proposals.create(session, ctx, bob, "new_game", None, {"developer": "x"})


def test_artwork_proposals_accumulate_and_revert(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    t = user(session, ctx.settings, "bob", rep=100)
    p1 = proposals.create(session, ctx, t, "artwork", game, {"items": [{"kind": "key_art", "url": "https://x.test/1.png"}]})
    p2 = proposals.create(session, ctx, t, "artwork", game, {"items": [{"kind": "logo", "url": "https://x.test/2.png"}]})
    assert {a.kind for a in session.scalars(select(Artwork))} == {"key_art", "logo"}
    proposals.revert(session, ctx, admin, p2.id)
    assert {a.kind for a in session.scalars(select(Artwork))} == {"key_art"}
    assert p1.status == "approved"
    with pytest.raises(AtlasError):
        proposals.create(session, ctx, t, "artwork", game, {"items": [{"kind": "logo", "url": "javascript:alert(1)"}]})


def test_approved_community_edits_federate_but_pending_ones_do_not(session, ctx, game):
    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    p = proposals.create(session, ctx, bob, "edit", game, {"developer": "Supergiant"})
    session.commit()
    assert feed.head_seq(session) == 0
    proposals.review(session, ctx, admin, p.id, approve=True)
    session.commit()
    entries = feed.read(session, 0, 10)
    assert [e.kind for e in entries] == ["obs"] and entries[0].body["observation"]["source"] == "community"


def test_store_id_link_proposal_applies_on_approval_conflicts_are_refused_and_revert_undoes_it(session, ctx, game):
    from atlas.models import Crosswalk

    admin = user(session, ctx.settings, "alice")
    bob = user(session, ctx.settings, "bob")
    p = proposals.create(session, ctx, bob, "link_ids", game, {"provider_ids": {"GOG": " hades_gog "}}, "store page")
    assert p.status == "pending" and p.payload == {"provider_ids": {"gog": "hades_gog"}}
    assert "gog" not in (view(session, game).get("provider_ids") or {})
    proposals.review(session, ctx, admin, p.id, approve=True)
    assert view(session, game)["provider_ids"]["gog"] == "hades_gog"
    assert session.get(Crosswalk, ("gog", "hades_gog")).canonical_id == game
    assert bob.reputation == proposals.REWARD["link_ids"]

    # another game cannot claim the same id, and this game cannot silently swap a different id for the store
    [other] = service.ingest(session, ctx, batch("igdb", [{"external_id": "2", "title": "Celeste"}]), system=True)
    with pytest.raises(AtlasError, match="already linked to another game"):
        proposals.create(session, ctx, bob, "link_ids", other.canonical_id, {"provider_ids": {"gog": "hades_gog"}})
    with pytest.raises(AtlasError, match="already has gog id"):
        proposals.create(session, ctx, bob, "link_ids", game, {"provider_ids": {"gog": "different"}})
    # the same link again is harmless
    again = proposals.create(session, ctx, bob, "link_ids", game, {"provider_ids": {"gog": "hades_gog"}})
    assert again.status == "pending"

    proposals.revert(session, ctx, admin, p.id, note="wrong game")
    assert "gog" not in (view(session, game).get("provider_ids") or {})
    assert session.get(Crosswalk, ("gog", "hades_gog")) is None


@pytest.mark.parametrize("bad", [{}, {"": "1"}, {"Bad Store!": "1"}, {"gog": ""}, {"gog": "a b"}, {"gog": "x" * 257},
                                 {f"s{i}": "1" for i in range(9)}, "gog:1", None])
def test_store_id_link_validation(session, ctx, game, bad):
    bob = user(session, ctx.settings, "bob")
    with pytest.raises(AtlasError):
        proposals.create(session, ctx, bob, "link_ids", game, {"provider_ids": bad})
