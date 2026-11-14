"""Profile sync over the private channel (CONTRACT-C10 §4, §11 A11-e/f/g/m/q/r/s/t/u/y/z): the PART 4.7 matrix.
Every assertion reads persistent database state of real nodes linked through signed assertions."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from atlas import group_channel, group_schema, groups, profile, profile_schema
from atlas import group_crypto as gc
from atlas.models import (
    AuditEntry,
    GroupAccountLink,
    GroupAccountNode,
    GroupChange,
    GroupStream,
    OidcLink,
    ProfileConflict,
    ProfileField,
    ProfileVersion,
    User,
)

from .channel_harness import (
    changes,
    conflicts,
    deliver,
    edit,
    entries_of,
    group_of,
    heads,
    link,
    rounds,
    shown,
    sync,
    uid_of,
    user,
    versions,
)
from .group_harness import ROOT_PW, Dispatcher, fast_kdf, make_node


@pytest.fixture
def net(tmp_path, monkeypatch):
    fast_kdf(monkeypatch)
    group_channel._HELLO.clear()
    d = Dispatcher()
    nodes = []

    def node(name, **kw):
        n = make_node(tmp_path, d, name, **kw)
        nodes.append(n)
        return n

    d.node = node
    yield d
    for n in nodes:
        n.close()


@pytest.fixture
def pair(net):
    a, b = group_of(net.node("a"), net.node("b"))
    ua, ub = user(a, "alice"), user(b, "alicia")
    link(a, ua, b, ub)
    rounds(a, b)
    return a, b, ua, ub


@pytest.fixture
def trio(net):
    a, b, c = group_of(net.node("a"), net.node("b"), net.node("c"))
    ua, ub, uc = user(a, "alice"), user(b, "alicia"), user(c, "ally")
    link(a, ua, b, ub)
    link(a, ua, c, uc)
    rounds(a, b, c)
    return a, b, c, ua, ub, uc


def consent_to(node, user_id: str, *extra: str) -> None:
    with node.session() as s:
        profile.set_consent(s, user_id, groups.load(s).group_id, profile_schema.default_consent() + list(extra))
        s.commit()


# ---- PART 4.7 matrix ---------------------------------------------------------------------------------------

def test_two_node_edit_syncs_with_origin_and_revision(pair):
    a, b, ua, ub = pair
    hlc, entry = edit(a, ua, "theme", "dark")
    assert entry is not None and entry.startswith(a.node_id + "/")
    sync(b)
    assert shown(b, ub, "theme") == "dark"
    with b.session() as s:
        row = s.get(ProfileField, (ub, "theme"))
        assert (row.hlc, row.origin_node, row.cause) == (hlc, a.node_id, "user")
        got = s.scalars(select(GroupChange).where(GroupChange.subject == "theme")).one()
        assert (got.disposition, got.origin, got.received_from, got.hlc) == ("applied", a.node_id, a.node_id, hlc)


def test_two_node_concurrent_edits_record_a_conflict_and_lose_nothing(pair):
    a, b, ua, ub = pair
    ha, _ = edit(a, ua, "display_name", "Alice A")
    hb, _ = edit(b, ub, "display_name", "Alice B")
    rounds(a, b)
    winner, loser = max(ha, hb), min(ha, hb)
    assert shown(a, ua, "display_name") == shown(b, ub, "display_name")
    assert heads(a, ua, "display_name") == heads(b, ub, "display_name") == sorted([ha, hb])
    assert conflicts(a, ua) == conflicts(b, ub) == [("display_name", "concurrent", winner, loser)]
    assert {v.value for v in versions(b, ub, "display_name")} == {"Alice A", "Alice B"}


def test_resolving_on_one_node_clears_the_queue_everywhere(pair):
    a, b, ua, ub = pair
    edit(a, ua, "display_name", "Alice A")
    edit(b, ub, "display_name", "Alice B")
    rounds(a, b)
    with b.session() as s:
        row = s.scalars(select(ProfileConflict).where(ProfileConflict.user_id == ub)).one()
        loser_value = s.get(ProfileVersion, (ub, "display_name", row.loser_hlc)).value
        profile.resolve(s, b.settings, b.identity, s.get(User, ub), row.id, "loser")
        s.commit()
    rounds(a, b)
    assert shown(a, ua, "display_name") == shown(b, ub, "display_name") == loser_value
    assert conflicts(a, ua) == conflicts(b, ub) == []
    assert len(heads(a, ua, "display_name")) == 1
    assert "profile.conflict.resolve" in {c.kind for c in changes(a, scope=uid_of(b, ub))}


def test_three_nodes_converge_through_relay(trio, net):
    a, b, c, ua, ub, uc = trio
    edit(b, ub, "theme", "light")
    sync(a)  # a holds b's entry
    net.down.add("b.test")
    sync(c)  # c never reaches b: store and forward through a (group.relay)
    assert shown(c, uc, "theme") == "light"
    got = changes(c, subject="theme")[0]
    assert (got.origin, got.received_from, got.disposition) == (b.node_id, a.node_id, "applied")
    net.down.clear()
    edit(c, uc, "a11y.font_scale", 1.25)
    rounds(a, b, c)
    for node, uid in ((a, ua), (b, ub), (c, uc)):
        assert shown(node, uid, "theme") == "light"
        assert shown(node, uid, "a11y.font_scale") == 1.25
        assert conflicts(node, uid) == []


def test_disconnected_edits_then_reconnect_converge_with_a_conflict(trio, net):
    a, b, c, ua, ub, uc = trio
    net.down.add("b.test")
    ha, _ = edit(a, ua, "date_format", "iso")
    hb, _ = edit(b, ub, "date_format", "dmy")
    edit(b, ub, "time_format", "24h")
    rounds(a, c)
    assert shown(c, uc, "date_format") == "iso" and shown(c, uc, "time_format") is None
    net.down.clear()
    rounds(a, b, c, n=3)
    state = {(shown(n, u, "date_format"), shown(n, u, "time_format"), tuple(heads(n, u, "date_format")))
             for n, u in ((a, ua), (b, ub), (c, uc))}
    assert len(state) == 1
    assert state.pop()[1] == "24h"
    expected = [("date_format", "concurrent", max(ha, hb), min(ha, hb))]
    assert conflicts(a, ua) == conflicts(b, ub) == conflicts(c, uc) == expected


def test_repeated_delivery_is_a_noop(pair):
    a, b, ua, ub = pair
    edit(a, ua, "theme", "dark")
    rounds(a, b, n=4)
    rows = changes(b, origin=a.node_id)
    assert len({(r.epoch, r.scope, r.seq) for r in rows}) == len(rows)
    with b.session() as s:
        group = groups.load(s)
        rx = group_channel.Rx(s, b.settings, b.identity, group, groups.trust(s, group, own_node=b.node_id),
                              groups.utcnow(), a.node_id)
        last = rows[-1]
        entry = group_channel.row_entry(s.get(GroupChange, (group.group_id, last.origin, last.epoch, last.scope,
                                                            last.seq)))
        out = group_channel.receive(rx, entry, set(profile_schema.fields()))
        s.commit()
    assert (out.disposition, out.reason) == ("noop", "repeat")
    assert len(changes(b, origin=a.node_id)) == len(rows)
    assert len(versions(b, ub, "theme")) == 1


def test_revoked_node_entries_are_rejected_even_when_backdated(trio):
    a, b, c, ua, ub, uc = trio
    edit(b, ub, "theme", "dark")
    sync(c)  # c holds b's entry and relays it
    with a.session() as s:
        group = groups.load(s)
        groups.revoke_node(s, a.settings, group, a.identity, b.node_id, "removed",
                           root=groups.unlock_root(s, a.settings, group, ROOT_PW))
        s.commit()
    sync(a)
    row = changes(a, origin=b.node_id, subject="theme")[0]
    assert (row.disposition, row.reason, row.received_from) == ("rejected", "origin_revoked", c.node_id)
    assert shown(a, ua, "theme") is None
    with a.session() as s:
        assert s.get(ProfileVersion, (ua, "theme", row.hlc)) is None


def _cert_end(node, of) -> object:
    with node.session() as s:
        group = groups.load(s)
        return gc.parse_time(groups.trust(s, group).in_force(of.node_id)["body"]["not_after"])


def test_expired_certificate_inside_grace_takes_only_the_advertised_head(pair):
    """After `not_after`, only entries up to the head advertised before expiry, inside 72 h (§1.5, A11-g/y)."""
    a, b, ua, ub = pair
    edit(b, ub, "theme", "dark")
    edit(b, ub, "ui.density", "compact")
    uid = uid_of(b, ub)
    entries = entries_of(b, b.node_id, uid)
    theme = next(e for e in entries if e["subject"] == "theme")
    end = _cert_end(a, b)
    with a.session() as s:  # what a's last `heads` call before not_after recorded
        group = groups.load(s)
        row = s.get(GroupStream, (group.group_id, b.node_id, theme["epoch"], uid))
        assert row.hwm == theme["seq"] - 1
        row.advertised_head, row.advertised_at = theme["seq"], end - timedelta(hours=1)
        s.commit()
    outs = deliver(a, entries, now=end + timedelta(hours=1), served_by=b.node_id)
    assert outs == [(theme["seq"], "applied", ""), (theme["seq"] + 1, "rejected", "origin_expired")]
    assert shown(a, ua, "theme") == "dark" and shown(a, ua, "ui.density") is None


def test_expired_certificate_outside_grace_is_rejected(pair):
    a, b, ua, ub = pair
    edit(b, ub, "theme", "dark")
    uid = uid_of(b, ub)
    entries = entries_of(b, b.node_id, uid)
    end = _cert_end(a, b)
    with a.session() as s:
        group = groups.load(s)
        row = s.get(GroupStream, (group.group_id, b.node_id, entries[-1]["epoch"], uid))
        row.advertised_head, row.advertised_at = entries[-1]["seq"], end - timedelta(hours=1)
        s.commit()
    assert deliver(a, entries, now=end + timedelta(hours=73))[-1][1:] == ("rejected", "origin_expired")
    assert shown(a, ua, "theme") is None


def test_expired_certificate_cannot_open_an_epoch(pair):
    a, b, ua, ub = pair
    end = _cert_end(a, b)
    with b.session() as s:
        group_channel.open_epoch(s, groups.load(s), b.identity, "manual")
        s.commit()
    epoch = next(e for e in entries_of(b, b.node_id, group_schema.NODE_SCOPE)
                 if e["kind"] == "stream.epoch" and e["body"]["reason"] == "manual")
    assert deliver(a, [epoch], now=end + timedelta(hours=1))[0][1:] == (None, "origin_expired")
    with a.session() as s:  # not refused for good: a renewed certificate lets the same epoch through
        assert s.get(GroupStream, (groups.load(s).group_id, b.node_id, epoch["epoch"], "_node")) is None
    assert deliver(a, [epoch])[0][1:] == ("applied", "epoch")


def test_deleted_account_unlinks_and_never_propagates(pair):
    a, b, ua, ub = pair
    edit(a, ua, "theme", "dark")
    edit(b, ub, "display_name", "Deleted Person 7c1a")
    rounds(a, b)
    from atlas import social
    from atlas.service import Ctx

    uid = uid_of(b, ub)
    with b.session() as s:
        assert any(r.body for r in s.scalars(select(GroupChange).where(GroupChange.scope == uid,
                                                                       GroupChange.origin == a.node_id)))
    other = user(b, "bob")
    with b.session() as s:
        s.get(User, other).role, s.get(User, other).status = "admin", "active"  # the only admin cannot leave
        s.flush()
        social.delete_account(s, s.get(User, ub), Ctx(b.settings, b.identity))
        s.commit()
    with b.session() as s:
        for model in (ProfileField, ProfileVersion, ProfileConflict, GroupAccountLink):
            assert s.scalar(select(func.count()).select_from(model).where(model.user_id == ub)) == 0
        # no synced personal data of the account stays in b's change log, own or relayed (§4.5)
        rows = list(s.scalars(select(GroupChange).where(GroupChange.scope == uid)))
        assert {r.origin for r in rows} == {b.node_id}
        assert {r.kind for r in rows if r.body is not None} <= {"account.link", "account.unlink"}
        assert "Deleted Person 7c1a" not in json.dumps([r.body for r in rows])
        assert any(r.kind == "profile.field.set" and r.body is None and r.body_hash for r in rows)  # envelope kept
        assert s.scalar(select(func.count()).select_from(GroupStream).where(
            GroupStream.scope == uid, GroupStream.origin != b.node_id)) == 0
        for node in s.scalars(select(GroupAccountNode).where(GroupAccountNode.account_uid == uid)):
            assert (node.evidence or {}).get("evidence") is None
        group = groups.load(s)
        served = group_channel.serve_changes(
            s, group, b.node_id, a.node_id, groups.trust(s, group, own_node=b.node_id),
            {"origin": b.node_id, "epoch": rows[0].epoch, "scope": uid, "since": 0, "accept": ["display_name"]})
        assert "Deleted Person 7c1a" not in json.dumps(served)
    sync(a)
    uid = uid_of(a, ua)
    with a.session() as s:
        group = groups.load(s)
        node = s.get(GroupAccountNode, (group.group_id, uid, b.node_id))
        assert node.status == "unlinked" and node.evidence == {"reason": "deleted"}
        assert s.get(User, ua).deleted_at is None  # deletion does not propagate
        assert not group_channel.may_serve_account(s, group.group_id, b.node_id, uid)
    assert shown(a, ua, "theme") == "dark"
    hlc, _ = edit(a, ua, "theme", "light")
    sync(b)
    assert [r for r in changes(b) if r.hlc == hlc] == []  # nothing of that account reaches b any more


def test_unlinking_keeps_local_values_and_stops_sync(pair):
    a, b, ua, ub = pair
    edit(a, ua, "theme", "dark")
    rounds(a, b)
    with b.session() as s:
        assert group_channel.unlink(s, b.settings, b.identity, s.get(User, ub))
        s.commit()
    assert uid_of(b, ub) is None
    assert shown(b, ub, "theme") == "dark"  # local data stays
    sync(a)
    edit(a, ua, "theme", "light")
    edit(b, ub, "ui.density", "compact")  # local only now
    rounds(a, b)
    assert shown(b, ub, "theme") == "dark"
    assert shown(a, ua, "ui.density") is None
    with a.session() as s:
        assert s.get(GroupAccountNode, (groups.load(s).group_id, uid_of(a, ua), b.node_id)).status == "unlinked"
    with b.session() as s:
        assert s.scalar(select(func.count()).select_from(AuditEntry).where(
            AuditEntry.action == "account_unlinked")) == 1


def _forged(node, uid: str, field: str, value) -> str:
    """An entry signed by `node` in its current epoch for `uid`, bypassing its own link and policy checks."""
    with node.session() as s:
        group = groups.load(s)
        epoch = group_channel.ensure_epoch(s, group, node.identity)
        hlc = profile.next_hlc(s, node.node_id)
        body = {"_v": 1, "account_uid": uid, "field": field, "value": value, "hlc": hlc, "base": [], "cause": "user",
                "issued_at": gc.stamp(groups.utcnow())}
        group_channel._append(s, group, node.identity, epoch, uid, "profile.field.set", body, groups.utcnow())
        s.commit()
        return hlc


def test_update_from_an_unlinked_origin_is_rejected_and_recorded(trio):
    """m: c unlinked, then signs a value for the account anyway: refused `not_linked`, value never kept (T8)."""
    a, b, c, ua, ub, uc = trio
    with c.session() as s:
        assert group_channel.unlink(s, c.settings, c.identity, s.get(User, uc))
        s.commit()
    sync(a)
    uid = uid_of(a, ua)
    hlc = _forged(c, uid, "theme", "dark")
    outs = deliver(a, entries_of(c, c.node_id, uid), served_by=c.node_id)
    assert outs[-1][1:] == ("rejected", "not_linked")
    row = [r for r in changes(a, origin=c.node_id) if r.hlc == hlc][0]
    assert row.body is None and row.hlc == hlc  # the envelope stays, the value is dropped
    assert shown(a, ua, "theme") is None


def test_unauthorized_preference_update_field_not_allowed(net):
    a, b = group_of(net.node("a", group_profile_fields="theme"), net.node("b"))
    ua, ub = user(a, "alice"), user(b, "alicia")
    link(a, ua, b, ub)
    rounds(a, b)
    edit(b, ub, "display_name", "Alicia")
    outs = deliver(a, entries_of(b, b.node_id, uid_of(b, ub)), accept={"display_name", "theme"},
                   served_by=b.node_id)
    assert outs[-1][1:] == ("rejected", "field_not_allowed")
    assert changes(a, subject="display_name")[0].body is None
    assert shown(a, ua, "display_name") is None


def test_no_consent_is_rejected_on_receive(pair):
    a, b, ua, ub = pair
    edit(b, ub, "bio", "hello")  # bio is off by default on both nodes: b never sends it
    assert changes(b, subject="bio") == []
    hlc = _forged(b, uid_of(b, ub), "bio", "hello")
    outs = deliver(a, entries_of(b, b.node_id, uid_of(b, ub)), accept={"bio"}, served_by=b.node_id)
    assert outs[-1][1:] == ("rejected", "no_consent")
    assert [r.body for r in changes(a) if r.hlc == hlc] == [None]
    with a.session() as s:
        assert s.get(User, ua).bio == ""


def test_operator_policy_and_consent_apply_on_send_and_receive(net):
    a, b = group_of(net.node("a"), net.node("b", group_profile_fields="theme language"))
    ua, ub = user(a, "alice"), user(b, "alicia")
    link(a, ua, b, ub)
    rounds(a, b)
    edit(a, ua, "display_name", "Alice")
    edit(a, ua, "theme", "dark")
    sync(b)
    assert shown(b, ub, "theme") == "dark" and shown(b, ub, "display_name") is None
    elided = changes(b, subject="display_name")[0]
    assert elided.disposition == "elided" and elided.body is None  # never received at all (u2)
    edit(b, ub, "a11y.high_contrast", True)  # not allowed by b's operator: local only
    assert changes(b, subject="a11y.high_contrast") == []
    with a.session() as s:  # alice withdraws consent for theme on a
        profile.set_consent(s, ua, groups.load(s).group_id,
                            [f for f in profile_schema.default_consent() if f != "theme"])
        s.commit()
    edit(b, ub, "theme", "light")
    sync(a)
    assert shown(a, ua, "theme") == "dark"
    row = changes(a, subject="theme", origin=b.node_id)[-1]
    assert row.disposition == "elided" and row.body is None
    hlc, entry = edit(a, ua, "theme", "auto")  # and a sends nothing for it either
    assert entry is None and [r for r in changes(a) if r.hlc == hlc] == []


def test_future_hlc_is_not_applied(pair):
    a, b, ua, ub = pair
    future = groups.utcnow() + timedelta(minutes=30)
    edit(a, ua, "theme", "dark", now=future)
    sync(b)
    assert shown(b, ub, "theme") is None
    assert changes(b, subject="theme") == []  # retry:future: nothing stored, retried later
    sync(b, now=future)
    assert shown(b, ub, "theme") == "dark"


def test_privacy_widening_waits_for_review(pair):
    a, b, ua, ub = pair
    consent_to(a, ua, "privacy.profile_public")
    consent_to(b, ub, "privacy.profile_public")
    edit(b, ub, "privacy.profile_public", False)
    sync(a)
    with a.session() as s:
        assert s.get(User, ua).profile_public is False  # narrowing applies at once
    edit(b, ub, "privacy.profile_public", True)
    sync(a)
    with a.session() as s:
        assert s.get(User, ua).profile_public is False  # widening waits
    assert [c[1] for c in conflicts(a, ua)] == ["privacy_widen"]
    with a.session() as s:
        row = s.scalars(select(ProfileConflict).where(ProfileConflict.user_id == ua)).one()
        profile.resolve(s, a.settings, a.identity, s.get(User, ua), row.id, "accept")
        s.commit()
    with a.session() as s:
        assert s.get(User, ua).profile_public is True
    assert conflicts(a, ua) == []


def test_identity_refs_never_create_an_oidc_link(pair):
    a, b, ua, ub = pair
    consent_to(a, ua, "identity.refs")
    consent_to(b, ub, "identity.refs")
    refs = [{"issuer_url": "https://idp.example.invalid", "issuer_name": "Example", "linked_at": "2026-01-01"}]
    edit(a, ua, "identity.refs", refs)
    sync(b)
    assert shown(b, ub, "identity.refs") == refs
    with b.session() as s:
        assert s.scalar(select(func.count()).select_from(OidcLink)) == 0


def test_denied_fields_cannot_be_emitted_or_applied(pair):
    a, b, ua, ub = pair
    edit(a, ua, "display_name", "Alice")
    sync(b)
    assert not profile_schema.DENIED & set(profile_schema.REGISTRY)
    with a.session() as s:
        with pytest.raises(profile.ProfileError):
            profile.set_local(s, a.settings, a.identity, s.get(User, ua), "password_hash", "x")
    out = group_schema.classify("profile.field.set", {
        "account_uid": "0" * 26, "field": "password_hash", "value": "x", "hlc": f"{'1' * 13}-0000-{'a' * 16}",
        "base": [], "cause": "user", "issued_at": "2026-11-01T00:00:00Z"})
    assert out.action == "held"  # unknown to the registry: never applied
    rows = changes(a) + changes(b)
    assert rows
    for row in rows:
        text = json.dumps(row.body or {})
        for column in profile_schema.AUTH_COLUMNS:
            assert f'"{column}"' not in text


def test_hlc_counter_overflow_bumps_the_millisecond():
    assert profile.tick(profile.fmt_hlc(1000, 0xFFFF, "a" * 16), "b" * 16, 1000) == profile.fmt_hlc(1001, 0, "b" * 16)
    assert profile.tick(profile.fmt_hlc(1000, 3, "a" * 16), "b" * 16, 900) == profile.fmt_hlc(1000, 4, "b" * 16)


def test_missing_base_is_held_bad_base_refused_and_timeout_opens_review(pair):
    a, b, ua, ub = pair
    node = "c" * 16
    ms = int(groups.utcnow().timestamp() * 1000)
    h1, h2, h4 = (profile.fmt_hlc(ms - d, 0, node) for d in (5000, 4000, 1000))
    with b.session() as s:
        user_b = s.get(User, ub)

        def ingest(field, hlc, value, base):
            return profile.ingest_remote(s, b.settings, b.node_id, user_b, field, hlc=hlc, value=value,
                                         cleared=False, base=base, cause="user", origin=node, entry_ref=None)

        assert ingest("theme", h2, "dark", [h1]) == "held"
        assert ingest("theme", h1, "light", [h2]) == "bad_base"
        assert s.get(ProfileField, (ub, "theme")) is None
        assert ingest("theme", h1, "light", []) == "applied"
        assert s.get(ProfileField, (ub, "theme")).value == "dark"  # h2 released once its base arrived
        ingest("ui.density", h4, "compact", [profile.fmt_hlc(1, 0, node)])
        profile.release_held(s, b.settings, b.node_id, user_b, "ui.density", timeout=timedelta(days=7),
                             now=groups.utcnow() + timedelta(days=8))
        s.commit()
    assert shown(b, ub, "ui.density") == "compact"
    assert ("ui.density", "missing_base", None, h4) in conflicts(b, ub)
