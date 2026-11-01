"""The private group channel (CONTRACT-C10 §3.3-§3.8, §11 A11-a/b/c/h/h2/i/j3/l/n/o/p/u2/y/aa/bb): sealed transport,
negotiation, per-stream ordering, epochs, forks, elision, link evidence, and the negative tests proving nothing
private reaches the public feed, `/sync/*`, `/api/v2/changes`, logs or metrics. Synthetic data only."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from atlas import accounts, auth, backup, feed, feed_schema, group_channel, group_schema, groups, profile
from atlas import group_crypto as gc
from atlas.models import (
    AuditEntry,
    ChangeLog,
    ClientChange,
    GroupAccountLink,
    GroupAccountNode,
    GroupChange,
    GroupStream,
    NodeState,
    ProfileConflict,
    ProfileField,
    ProfileSuperseded,
    ProfileVersion,
    User,
    UserMfa,
    UserSession,
)
from atlas.signing import body_hash, sign_message

from .channel_harness import (
    assertion,
    changes,
    deliver,
    edit,
    entries_of,
    full_round,
    group_of,
    link,
    mint,
    rounds,
    shown,
    sync,
    uid_of,
    user,
)
from .group_harness import ROOT_PW, Dispatcher, fast_kdf, make_node

SECRET_VALUE = "Private Display Name 5f2c"


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
    d.tmp = tmp_path
    yield d
    for n in nodes:
        n.close()


@pytest.fixture
def pair(net):
    a, b = group_of(net.node("a", metrics_enabled=True), net.node("b"))
    ua, ub = user(a, "alice"), user(b, "alicia")
    link(a, ua, b, ub)
    rounds(a, b)
    return a, b, ua, ub


def gid(node) -> str:
    with node.session() as s:
        return groups.load(s).group_id


def audited(node, action: str) -> list[dict]:
    with node.session() as s:
        return [r.detail or {} for r in s.scalars(select(AuditEntry).where(AuditEntry.action == action))]


def sign_into(node, uid: str, kind: str, body: dict, identity=None) -> dict:
    """Append and sign an entry as `node` (or `identity`) in its current epoch, bypassing emission policy."""
    with node.session() as s:
        group = groups.load(s)
        epoch = group_channel.ensure_epoch(s, group, node.identity)
        body = {"_v": 1, "issued_at": gc.stamp(groups.utcnow()), **body}
        ref = group_channel._append(s, group, identity or node.identity, epoch, uid, kind, body, groups.utcnow())
        s.commit()
    origin, epoch, scope, seq = ref.split("/")
    return next(e for e in entries_of(node, origin, scope) if e["epoch"] == epoch and e["seq"] == int(seq))


def value_body(node, uid: str, field: str, value) -> dict:
    with node.session() as s:
        hlc = profile.next_hlc(s, node.node_id)
        s.commit()
    return {"account_uid": uid, "field": field, "value": value, "hlc": hlc, "base": [], "cause": "user"}


# ---- a: nothing private in public outputs -------------------------------------------------------------------

def test_group_kinds_are_refused_by_the_public_feed(pair):
    a, *_ = pair
    assert not set(group_schema.GROUP_KINDS) & set(feed_schema.known_kinds())
    with a.session() as s:
        for kind in group_schema.GROUP_KINDS:
            with pytest.raises(Exception):  # noqa: B017 - the emission gate refuses any unknown kind
                feed.append(s, a.identity, kind, {"account_uid": "0" * 26})
        s.rollback()


def test_public_outputs_carry_no_group_data(pair):
    a, b, ua, ub = pair
    edit(a, ua, "display_name", SECRET_VALUE)
    edit(b, ub, "theme", "dark")
    rounds(a, b)
    uid = uid_of(a, ua)
    with a.session() as s:
        _, secret = auth.create_token(s, "reader", ["read", "sync"])
        s.commit()
    client = a.net.clients["a.test"]
    headers = {"Authorization": f"Bearer {secret}"}
    bodies = []
    for path, params in (("/api/v2/sync/info", {}), ("/api/v2/sync/changes", {"since": 0}),
                         ("/api/v2/changes", {"since": 0}), ("/api/v2/changes", {"feed": "local", "since": 0})):
        r = client.get(path, params=params, headers=headers)
        assert r.status_code == 200, (path, r.status_code)
        bodies.append(r.text)
    with a.session() as s:
        for model in (ChangeLog, ClientChange):
            bodies += [json.dumps({c.name: str(getattr(row, c.name)) for c in model.__table__.columns})
                       for row in s.scalars(select(model))]
    forbidden = [uid, SECRET_VALUE, gid(a), "profile.field", "account.link", "stream.epoch", "display_name"]
    for text in bodies:
        for needle in forbidden:
            assert needle not in text


def test_metrics_and_logs_carry_no_private_data(pair, caplog):
    a, b, ua, ub = pair
    caplog.set_level(logging.DEBUG)
    edit(a, ua, "display_name", SECRET_VALUE)
    edit(b, ub, "display_name", "Other Private 77aa")
    rounds(a, b)
    uid = uid_of(a, ua)
    with a.session() as s:
        _, secret = auth.create_token(s, "scraper", ["metrics"])
        s.commit()
    r = a.net.clients["a.test"].get("/metrics", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200
    assert 'atlas_group_entries{disposition="applied"}' in r.text
    assert "atlas_profile_conflicts_open 1" in r.text
    assert f'atlas_group_stream_lag{{origin="{b.node_id}"}} 0' in r.text
    logged = "\n".join(f"{rec.getMessage()} {rec.__dict__}" for rec in caplog.records)
    for needle in (uid, SECRET_VALUE, "Other Private 77aa"):
        assert needle not in r.text
        assert needle not in logged
    assert "display_name" not in r.text


def test_credentials_and_sessions_never_sync(pair):
    a, b, ua, ub = pair
    with a.session() as s:
        alice = s.get(User, ua)
        cookie, row = accounts.create_session(s, a.settings, alice)
        s.add(UserMfa(user_id=ua, secret_enc="synthetic-mfa-secret-enc-91c3", setup_session_id=row.id,
                      recovery_hashes=["synthetic-recovery-hash-4d1e"]))
        s.commit()
        secrets = [alice.password_hash, cookie, row.id, row.csrf, "synthetic-mfa-secret-enc-91c3",
                   "synthetic-recovery-hash-4d1e", alice.email]
    for field, value in (("display_name", "Alice"), ("theme", "dark"), ("a11y.font_scale", 1.5)):
        edit(a, ua, field, value)
    rounds(a, b)
    for node in (a, b):
        with node.session() as s:
            dump = []
            for model in (GroupChange, GroupAccountLink, GroupAccountNode, GroupStream, ProfileField,
                          ProfileVersion, ProfileConflict, ProfileSuperseded):
                dump += [json.dumps({c.name: str(getattr(r, c.name)) for c in model.__table__.columns})
                         for r in s.scalars(select(model))]
            text = "\n".join(dump)
            for needle in secrets:
                assert needle not in text
            for column in ("password_hash", "secret_enc", "recovery_hashes", "csrf", "token_hash"):
                assert f'"{column}"' not in "\n".join(json.dumps(r.body) for r in s.scalars(select(GroupChange))
                                                    if r.body)
    with b.session() as s:
        assert s.scalar(select(func.count()).select_from(UserSession)) == 0
        assert s.scalar(select(func.count()).select_from(UserMfa)) == 0
        assert s.get(User, ub).password_hash != secrets[0]


# ---- b / bb: sealed transport ------------------------------------------------------------------------------

def test_recorded_exchange_carries_no_identifiers_in_clear(pair, monkeypatch):
    a, b, ua, ub = pair
    edit(a, ua, "theme", "dark")
    recorded: list[str] = []
    original = b.net.post

    def post(url, json=None, content=None, headers=None, timeout=None):
        resp = original(url, json=json, content=content, headers=headers, timeout=timeout)
        recorded.append(f"{url}\n{headers}\n{content!r}\n{resp.headers}\n{resp.text}")
        return resp

    monkeypatch.setattr(b.net, "post", post)
    group_channel._HELLO.clear()
    sync(b)
    assert shown(b, ub, "theme") == "dark"
    assert recorded
    needles = [gid(a), a.node_id, b.node_id, uid_of(a, ua), "theme", "dark", "heads", "changes", "hello",
               "profile.field"]
    for text in recorded:
        for needle in needles:
            assert needle not in text, needle


def test_changes_answer_cannot_be_opened_without_the_box_key(pair, monkeypatch):
    a, b, ua, ub = pair
    edit(a, ua, "theme", "dark")
    captured = {}
    original = b.net.post

    def post(url, json=None, content=None, headers=None, timeout=None):
        resp = original(url, json=json, content=content, headers=headers, timeout=timeout)
        captured["resp"] = resp.json()
        return resp

    monkeypatch.setattr(b.net, "post", post)
    with b.session() as s:
        group = groups.load(s)
        group_channel.serve_heads  # noqa: B018 - the op exists
        from atlas.api import group_routes

        member = groups.trust(s, group, own_node=b.node_id).in_force(a.node_id)["body"]["member"]
        group_routes.call(s, b.settings, b.identity, group, member, "heads", {}, b.net)
        stranger = gc.X25519PrivateKey.generate()
        with pytest.raises(gc.GroupCryptoError):
            gc.open_response([stranger], b"\0" * 16, group.group_id, a.node_id, b.node_id, captured["resp"])
        tampered = dict(captured["resp"])
        tampered["ct"] = tampered["ct"][:-4] + ("AAAA" if not tampered["ct"].endswith("AAAA") else "BBBB")
        with pytest.raises(gc.GroupCryptoError):
            gc.open_response(groups.box_keys(b.settings, group), b"\0" * 16, group.group_id, a.node_id, b.node_id,
                             tampered)


def test_response_for_other_request_refused(pair, monkeypatch):
    """A11-j3 (T22): a member's genuine answer to one request is refused as the answer to another request of the
    same op, because the response key and the response signature are bound to the request nonce."""
    from atlas.api import group_routes

    a, b, ua, ub = pair
    edit(a, ua, "theme", "dark")
    answers = []
    original = b.net.post

    def record(url, json=None, content=None, headers=None, timeout=None):
        resp = original(url, json=json, content=content, headers=headers, timeout=timeout)
        answers.append(resp)
        return resp

    def replay_first(url, json=None, content=None, headers=None, timeout=None):
        original(url, json=json, content=content, headers=headers, timeout=timeout)
        return answers[0]

    with b.session() as s:
        group = groups.load(s)
        member = groups.trust(s, group, own_node=b.node_id).in_force(a.node_id)["body"]["member"]
        monkeypatch.setattr(b.net, "post", record)
        assert group_routes.call(s, b.settings, b.identity, group, member, "heads", {}, b.net) is not None
        monkeypatch.setattr(b.net, "post", replay_first)
        with pytest.raises(group_routes.CallError, match="not sealed to this node"):
            group_routes.call(s, b.settings, b.identity, group, member, "heads", {}, b.net)


# ---- c: negotiation, park and held ----------------------------------------------------------------------------

def test_negotiation_n_minus_one_and_refusal():
    assert group_schema.negotiate({"group_protocol": "1.1", "min_group_protocol": "1.0"}) == "1.0"
    assert group_schema.negotiate({"group_protocol": "1.0", "min_group_protocol": "1.0"}) == "1.0"
    assert group_schema.negotiate({"group_protocol": "2.0", "min_group_protocol": "2.0"}) is None
    assert group_schema.negotiate({"group_protocol": "0.9", "min_group_protocol": "0.9"}) is None
    assert group_schema.negotiate({"group_protocol": "x"}) is None
    assert group_schema.acceptable("1.0") and not group_schema.acceptable("2.0")


def test_hello_advertises_kinds_fields_and_capabilities(pair):
    a, b, *_ = pair
    from atlas.api import group_routes

    with b.session() as s:
        group = groups.load(s)
        member = groups.trust(s, group, own_node=b.node_id).in_force(a.node_id)["body"]["member"]
        hello = group_routes.call(s, b.settings, b.identity, group, member, "hello", {}, b.net)
    assert {"heads", "changes", "snapshot", "notify"} <= set(hello["ops"])
    assert hello["kinds"]["profile.field.set"] == {"emit": 1, "apply": 1}
    assert "display_name" in hello["profile_fields"] and "password_hash" not in hello["profile_fields"]
    assert "store-forward-v1" in hello["capabilities"]


def test_unknown_kind_parks_only_its_stream_and_unknown_field_is_held(pair):
    a, b, ua, ub = pair
    ua2, ub2 = user(a, "bert"), user(b, "berta")
    link(a, ua2, b, ub2)
    rounds(a, b)
    uid1, uid2 = uid_of(b, ub), uid_of(b, ub2)
    held = sign_into(b, uid1, "profile.field.set", value_body(b, uid1, "future.colour_scheme", "x"))
    # a requester that accepts the newer field gets the body; this 1.0 node does not know the field
    out = deliver(a, entries_of(b, b.node_id, uid1), accept=["theme", "future.colour_scheme"], served_by=b.node_id)
    assert out[-1] == (held["seq"], "held", "unsupported_field")
    after_held = edit(b, ub, "theme", "dark")[0]
    parked = sign_into(b, uid2, "profile.list.upsert", {"account_uid": uid2})
    edit(b, ub2, "theme", "light")  # behind the parked entry in the same stream
    sync(a)
    rows = {r.seq: r for r in changes(a, origin=b.node_id, scope=uid1)}
    assert (rows[held["seq"]].disposition, rows[held["seq"]].reason) == ("held", "unsupported_field")
    assert shown(a, ua, "theme") == "dark" and rows[held["seq"] + 1].hlc == after_held  # did not block
    with a.session() as s:
        stream = s.get(GroupStream, (gid(a), b.node_id, parked["epoch"], uid2))
        assert (stream.blocked_seq, stream.blocked_reason) == (parked["seq"], "unsupported_kind")
        assert stream.hwm == parked["seq"] - 1
    assert shown(a, ua2, "theme") is None  # only that stream waits for a newer release


# ---- h / h2: keys and rotation ---------------------------------------------------------------------------------

def test_unvouched_key_is_rejected(pair):
    from tests.group_world import synth

    a, b, ua, ub = pair
    stranger = synth()
    e = sign_into(b, uid_of(b, ub), "profile.field.set", value_body(b, uid_of(b, ub), "theme", "dark"))
    forged = dict(e, key_id=stranger.key_id)
    forged["sig"] = sign_message(stranger.private, gc.DOMAINS["change"], group_channel.entry_parts(gid(a), forged))
    assert deliver(a, [forged], served_by=b.node_id) == [(e["seq"], None, "bad_signature")]
    assert audited(a, "group_peer_suspect")[-1]["reason"] == "bad_signature"
    assert shown(a, ua, "theme") is None


def test_rotated_out_key_cannot_extend_streams_or_open_epochs(pair):
    from atlas import key_rotation

    a, b, ua, ub = pair
    uid = uid_of(b, ub)
    edit(b, ub, "theme", "dark")
    sync(a)
    old = b.identity
    old_epoch = entries_of(b, b.node_id, uid)[-1]
    with b.session() as s:
        successor = key_rotation.rotate(s, b.identity.key_id, b.settings)
        s.commit()
    b.st.ctx.identity = successor
    with b.session() as s:
        group = groups.load(s)
        groups.rebuild(s, group, b.node_id, settings=b.settings)
        new_epoch = group_channel.ensure_epoch(s, group, successor)
        s.commit()
    edit(b, ub, "theme", "light")
    from .channel_harness import group_routes_pull

    group_routes_pull(a)
    sync(a)
    assert shown(a, ua, "theme") == "light"  # the rotation epoch and its entries are accepted
    with a.session() as s:
        assert s.get(GroupStream, (gid(a), b.node_id, new_epoch, "_node")).key_id == successor.key_id
    # the stolen old key signs one more entry in the old epoch: above previous.heads, refused
    body = {"_v": 1, "issued_at": gc.stamp(groups.utcnow()), **value_body(b, uid, "ui.density", "compact")}
    stale = {"origin": b.node_id, "epoch": old_epoch["epoch"], "scope": uid, "seq": old_epoch["seq"] + 1,
             "kind": "profile.field.set", "subject": "ui.density", "key_id": old.key_id, "body_hash": body_hash(body),
             "body": body}
    stale["sig"] = sign_message(old.private, gc.DOMAINS["change"], group_channel.entry_parts(gid(a), stale))
    assert deliver(a, [stale], served_by=b.node_id)[0][1:] == ("rejected", "key_superseded")
    assert shown(a, ua, "ui.density") is None
    # nor can it open a new epoch
    epoch_body = {"_v": 1, "epoch": "0badc0de", "previous": None, "reason": "manual",
                  "issued_at": gc.stamp(groups.utcnow())}
    ep = {"origin": b.node_id, "epoch": "0badc0de", "scope": "_node", "seq": 1, "kind": "stream.epoch",
          "subject": "-", "key_id": old.key_id, "body_hash": body_hash(epoch_body), "body": epoch_body}
    ep["sig"] = sign_message(old.private, gc.DOMAINS["change"], group_channel.entry_parts(gid(a), ep))
    assert deliver(a, [ep], served_by=b.node_id)[0][1:] == ("rejected", "key_superseded")


# ---- i / o: replay and forks -----------------------------------------------------------------------------------

def test_clone_fork_halts_the_origin(pair):
    a, b, ua, ub = pair
    uid = uid_of(b, ub)
    edit(b, ub, "theme", "dark")
    sync(a)
    original = entries_of(b, b.node_id, uid)[-1]
    body = dict(original["body"], value="light")
    clone = dict(original, body=body, body_hash=body_hash(body))
    clone["sig"] = sign_message(b.identity.private, gc.DOMAINS["change"], group_channel.entry_parts(gid(a), clone))
    with a.session() as s:
        group = groups.load(s)
        rx = group_channel.Rx(s, a.settings, a.identity, group, groups.trust(s, group, own_node=a.node_id),
                              groups.utcnow(), b.node_id)
        out = group_channel.receive(rx, clone, {"theme"})
        s.commit()
    assert (out.disposition, out.reason) == (None, "fork")
    with a.session() as s:
        assert b.node_id in json.loads(s.get(NodeState, group_channel.FORKED).value)
    assert audited(a, "group_stream_fork")[0]["seq"] == original["seq"]
    edit(b, ub, "theme", "auto")
    sync(a)
    assert shown(a, ua, "theme") == "dark"  # the origin stays halted


# ---- l: uid collisions across groups --------------------------------------------------------------------------

def test_uid_collision_across_groups_stays_isolated(pair):
    a, b, ua, ub = pair
    uid = uid_of(a, ua)
    other = user(a, "carol")
    with a.session() as s:
        s.add(GroupAccountLink(group_id="f" * 16, account_uid=uid, user_id=other, minted_here=False,
                               linked_at=groups.utcnow()))
        s.commit()
        assert group_channel.link_by_uid(s, gid(a), uid).user_id == ua
        assert group_channel.link_by_uid(s, "f" * 16, uid).user_id == other
        assert group_channel.local_link(s, gid(a), other) is None


# ---- n / aa: link evidence -------------------------------------------------------------------------------------

def resign(node, a: dict) -> dict:
    a = dict(a, body_hash=body_hash(a["body"]))
    a["sig"] = sign_message(node.identity.private, gc.DOMAINS["group.assertion"], gc.object_parts(a))
    return a


def link_body(node, uid: str, *, minted=None, evidence=None) -> dict:
    return {"account_uid": uid, "node_id": node.node_id, "minted": minted, "evidence": evidence}


def test_links_without_evidence_or_minted_by_a_non_minter_are_rejected(pair):
    a, b, ua, ub = pair
    ua2 = user(a, "bert")
    uid = mint(a, ua2)
    rounds(a, b)
    entries = [sign_into(b, uid, "account.link", link_body(b, uid)),
               sign_into(b, uid, "account.link", link_body(b, uid, minted=True))]
    outs = deliver(a, entries, served_by=b.node_id)
    assert [o[1:] for o in outs] == [("rejected", "invalid"), ("rejected", "not_minter")]
    with a.session() as s:
        assert s.get(GroupAccountNode, (gid(a), uid, b.node_id)) is None
    assert audited(a, "account_link_suspect")[-1]["reason"] == "not_minter"


def test_evidence_check_negative_cases(pair):
    a, b, ua, ub = pair
    ua2 = user(a, "bert")
    good = assertion(a, ua2, b)
    uid = good["body"]["account_uid"]
    rounds(a, b)
    bad_purpose = resign(a, dict(good, body=dict(good["body"], purpose="consent")))
    bad_aud = resign(a, dict(good, body=dict(good["body"], aud_node=a.node_id)))
    long_ttl = resign(a, dict(good, body=dict(good["body"], not_after=gc.stamp(
        gc.parse_time(good["body"]["issued_at"]) + timedelta(seconds=600)))))
    tampered = dict(good, body=dict(good["body"], username_hint="mallory"))  # body_hash no longer matches
    foreign = dict(good)
    foreign["sig"] = sign_message(b.identity.private, gc.DOMAINS["group.assertion"], gc.object_parts(good))
    old = resign(a, dict(good, body=dict(good["body"], issued_at=gc.stamp(groups.utcnow() - timedelta(hours=2)),
                                         not_after=gc.stamp(groups.utcnow() - timedelta(hours=2, seconds=-60)))))
    cases = [bad_purpose, bad_aud, long_ttl, tampered, foreign, old]
    outs = deliver(a, [sign_into(b, uid, "account.link", link_body(b, uid, evidence=ev)) for ev in cases],
                   served_by=b.node_id)
    assert [o[2] for o in outs] == ["bad_evidence"] * 5 + ["evidence_time"]
    ok = deliver(a, [sign_into(b, uid, "account.link", link_body(b, uid, evidence=good))], served_by=b.node_id)
    assert ok[0][1] == "applied"
    with a.session() as s:
        assert s.get(GroupAccountNode, (gid(a), uid, b.node_id)).status == "linked"


def test_signin_assertion_is_link_evidence_for_auto_provision(pair):
    """A12 (CONTRACT-AMENDMENTS): an auto-provisioned account at b links with the sign-in assertion as evidence."""
    a, b, ua, ub = pair
    good = assertion(a, user(a, "cleo"), b)
    uid = good["body"]["account_uid"]
    rounds(a, b)
    signin = resign(a, dict(good, body=dict(good["body"], purpose="signin")))
    ok = deliver(a, [sign_into(b, uid, "account.link", link_body(b, uid, evidence=signin))], served_by=b.node_id)
    assert ok[0][1] == "applied"
    with a.session() as s:
        assert s.get(GroupAccountNode, (gid(a), uid, b.node_id)).status == "linked"


def test_key_compromise_cascades_to_suspect_links(net):
    a, b, c = group_of(net.node("a"), net.node("b"), net.node("c"))
    uc, ub = user(c, "cara"), user(b, "carla")
    link(c, uc, b, ub)  # c is the asserting node of b's link
    rounds(a, b, c)
    uid = uid_of(b, ub)
    with b.session() as s:
        assert s.get(GroupAccountNode, (gid(a), uid, b.node_id)).status == "linked"
    with a.session() as s:
        group = groups.load(s)
        groups.revoke_node(s, a.settings, group, a.identity, c.node_id, "key_compromise",
                           root=groups.unlock_root(s, a.settings, group, ROOT_PW),
                           suspect_since=groups.utcnow() - timedelta(hours=1))
        s.commit()
    from .channel_harness import group_routes_pull

    group_routes_pull(b)
    sync(a)
    sync(b)
    with b.session() as s:  # a holds no local account for that uid, so only the holders see the link
        assert s.get(GroupAccountNode, (gid(a), uid, b.node_id)).status == "suspect"
        row = s.scalars(select(ProfileConflict).where(ProfileConflict.kind == "suspect_link")).one()
        assert (row.user_id, row.loser_hlc) == (ub, b.node_id)


# ---- p: restore ------------------------------------------------------------------------------------------------

def test_restore_opens_a_new_epoch_and_snapshots_recover_lost_values(pair, net):
    a, b, ua, ub = pair
    edit(b, ub, "theme", "dark")
    rounds(a, b)
    archive = net.tmp / "b-backup.tar.gz"
    engine = b.st.maker.kw["bind"]
    backup.create(b.settings, engine, archive, include_media=False)
    lost, _ = edit(b, ub, "theme", "light")  # emitted after the backup
    sync(a)
    assert shown(a, ua, "theme") == "light"
    before = {e["epoch"] for e in entries_of(b, b.node_id, "_node")}
    engine.dispose()
    backup.restore(b.settings, engine, archive, force=True, include_media=False)
    assert shown(b, ub, "theme") == "dark"
    full_round(b)
    full_round(b)
    assert shown(b, ub, "theme") == "light"  # its own signed entry came back from a's snapshot
    with b.session() as s:
        assert s.get(ProfileVersion, (ub, "theme", lost)) is not None
        assert s.get(NodeState, group_channel.SNAPSHOT_DUE) is None
    new = [e for e in entries_of(b, b.node_id, "_node") if e["epoch"] not in before]
    assert [e["body"]["reason"] for e in new] == ["restore"]
    edit(b, ub, "theme", "auto")
    sync(a)
    assert shown(a, ua, "theme") == "auto"  # no fork: the restored node writes in its new epoch
    assert audited(a, "group_stream_fork") == []


# ---- u2: elision -----------------------------------------------------------------------------------------------

def test_relay_that_elides_an_accepted_field_is_suspect(pair):
    a, b, ua, ub = pair
    e = sign_into(b, uid_of(b, ub), "profile.field.set", value_body(b, uid_of(b, ub), "theme", "dark"))
    elided = dict(e, body=None, elided=True)
    assert deliver(a, [elided], served_by=b.node_id)[0][1:] == (None, "bad_elision")
    assert audited(a, "group_peer_suspect")[-1]["reason"] == "bad_elision"
    assert changes(a, subject="theme") == []


# ---- y: epoch rate -----------------------------------------------------------------------------------------------

def test_epoch_rate_limit(pair):
    a, b, *_ = pair
    with b.session() as s:
        for _ in range(4):
            group_channel.open_epoch(s, groups.load(s), b.identity, "manual")
        s.commit()
    news = sorted((e for e in entries_of(b, b.node_id, "_node") if e["kind"] == "stream.epoch"
                   and e["body"]["reason"] == "manual"), key=lambda e: e["body"]["issued_at"])
    outs = [deliver(a, [e], served_by=b.node_id)[0][1:] for e in news]
    assert outs == [("applied", "epoch")] * 3 + [(None, "epoch_rate")]
    with a.session() as s:
        assert s.scalar(select(func.count()).select_from(GroupStream).where(
            GroupStream.origin == b.node_id, GroupStream.scope == "_node",
            GroupStream.blocked_reason.is_(None))) == 4
