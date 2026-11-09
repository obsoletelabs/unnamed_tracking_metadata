"""Fix pass on the C10 review #1 hardening (PR #133 review). Each test replays one finding of that review against
the code: the `root succession` CLI, the deliverer of a pushed succession, the expiry-grace release of a whole run,
the journal replay after compaction, a restore without a journal, two successions with missing CRLs, and the
trust epoch of a non-holder in a two-member group. Keys are generated at runtime; passphrases are synthetic."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from atlas import group_cli, group_journal, groups
from atlas import group_crypto as gc
from atlas.api import group_routes
from atlas.models import AuditEntry, GroupBannedNode, GroupChange, GroupCrl, GroupRevoked, GroupStream, NodeState

from .channel_harness import deliver, edit, entries_of, link, rounds, shown, uid_of, user
from .group_harness import REC_PW, ROOT_PW, Dispatcher, create, enrol, fast_kdf, finish_creation, make_node
from .group_world import World, synth


@pytest.fixture
def w(monkeypatch):
    world = World(monkeypatch)
    yield world
    world.s.close()
    world.engine.dispose()


@pytest.fixture
def net(tmp_path, monkeypatch):
    fast_kdf(monkeypatch)
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


def audited(w, action):
    return w.s.scalars(select(AuditEntry).where(AuditEntry.action == action)).all()


def later(hours: float = 25):
    return groups.utcnow() + timedelta(hours=hours)


def last_of(crl: dict) -> dict:
    return {"crl_seq": crl["body"]["crl_seq"], "body_hash": crl["body_hash"]}


def carry_of(*certs) -> list[dict]:
    return [{"serial": c["body"]["serial"], "body_hash": c["body_hash"]} for c in certs]


def node_view(group_id: str, ident, at, **body) -> dict:
    issued = gc.stamp(at)
    b = {"node_id": ident.node_id, "state_hash": "0" * 64, "crl_seq": {}, "revoked_count": 0, "succession_index": 0,
         "succession_pending": False, "issued_at": issued, **body}
    return gc.make_object("group.view", group_id, f"view:{ident.node_id}/{issued}", b,
                          {"kind": "node", "id": ident.node_id, "key_id": ident.key_id}, ident.private)


# ---- finding 1: `atlas group root succession` names the old root's last CRL with the owner's input ------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch, net):
    nodes = {"a": net.node("a"), "b": net.node("b"), "c": net.node("c")}
    state = {"node": None, "answers": []}
    monkeypatch.setattr(group_cli, "_maker", lambda: (state["node"].settings, state["node"].st.maker))
    monkeypatch.setattr(group_cli.getpass, "getpass", lambda prompt="": state["answers"].pop(0))
    monkeypatch.setattr(group_cli, "_http", lambda: state["node"].net)

    def run(node, *argv, answers=()):
        from atlas import cli

        state["node"], state["answers"] = nodes[node], list(answers)
        return cli.main(["group", *argv])

    return run, nodes, tmp_path


def _compromised_group(cli_env):
    """a holds the root; c was revoked as key_compromise by the root, so the root's newest CRL bans a node."""
    run, nodes, tmp = cli_env
    a, b, c = nodes["a"], nodes["b"], nodes["c"]
    assert run("a", "create", "--name", "Home", "--recovery-out", str(tmp),
               answers=[ROOT_PW, ROOT_PW, REC_PW[0], REC_PW[0]]) == 0
    enrol(a, b)
    enrol(a, c)
    suspect = gc.stamp(groups.utcnow() - timedelta(hours=1))
    assert run("a", "revoke", "--node", c.node_id, "--reason", "key_compromise", "--suspect-since", suspect,
               answers=[ROOT_PW]) == 0
    with a.session() as s:
        (row,) = [r for r in s.scalars(select(GroupRevoked)) if r.reason == "key_compromise"]
        serial = row.serial
    (file,) = [str(p) for p in tmp.glob("atlas-group-*-recovery-1.json")]
    out = tmp / "succ"
    out.mkdir()
    return run, a, file, out, serial


def test_cli_succession_shows_last_crl_and_needs_typed_confirmation(cli_env, capsys):
    """The finding: the CLI had no way to confirm a key_compromise entry, so any group whose root ever banned a node
    for key_compromise could not run a succession at all."""
    run, a, file, out, serial = _compromised_group(cli_env)
    capsys.readouterr()
    since = gc.stamp(groups.utcnow() + timedelta(minutes=1))
    # --show: the chosen CRL and its entries with reasons, nothing issued, no passphrase asked
    assert run("a", "root", "succession", "--recovery-file", file, "--suspect-since", since, "--show") == 0
    shown_out = capsys.readouterr().out
    assert serial in shown_out and "key_compromise" in shown_out and "crl_seq 1" in shown_out
    with a.session() as s:
        assert not groups.pending_successions(groups.load(s))
    # without the typed confirmation of the key_compromise serial: refused, the serial named
    assert run("a", "root", "succession", "--recovery-file", file, "--suspect-since", since,
               "--recovery-out", str(out), answers=[REC_PW[0]]) == 1
    err = capsys.readouterr().err
    assert serial in err and "--confirm-compromise" in err
    root2, rec2 = "synthetic successor root phrase 4a55", "synthetic new recovery phrase 0d1e"
    assert run("a", "root", "succession", "--recovery-file", file, "--suspect-since", since,
               "--confirm-compromise", serial, "--recovery-out", str(out),
               answers=[REC_PW[0], root2, root2, rec2, rec2]) == 0
    with a.session() as s:
        (pending,) = groups.pending_successions(groups.load(s))
        assert pending["body"]["body"]["old_root_last_crl"]["crl_seq"] == 1


def test_cli_compromised_succession_needs_suspect_time_or_named_crl(cli_env, capsys):
    """Without --suspect-since (or --last-crl) the default was the newest CRL, which can be the thief's."""
    run, a, file, out, serial = _compromised_group(cli_env)
    capsys.readouterr()
    assert run("a", "root", "succession", "--recovery-file", file, "--confirm-compromise", serial,
               "--recovery-out", str(out), answers=[REC_PW[0]]) == 1
    assert "--suspect-since" in capsys.readouterr().err
    with a.session() as s:
        assert not groups.pending_successions(groups.load(s))


def test_succession_default_skips_crl_received_after_suspect_time(w):
    """The plan's seq-16 case: the thief's CRL arrived after the owner's suspect time, so it is not the default."""
    x = synth()
    cx = w.enrol(x)
    root = w.root()
    honest = w.crl(None, [], 1, root=root)
    w.apply(honest)
    w.s.get(GroupCrl, (w.gid, "root", 1, honest["body_hash"])).received_at = groups.utcnow() - timedelta(hours=3)
    forged = w.crl(None, [groups.crl_entry(cx, "key_compromise", groups.utcnow(), groups.utcnow())], 2, root=root)
    w.apply(forged)
    with pytest.raises(groups.GroupError, match="suspect"):
        groups.issue_succession(w.s, w.settings, w.group, w.me, [gc.open_recovery_file(w.files[0], REC_PW[0])],
                                "synthetic successor root phrase 4a55", reason="compromised",
                                new_recovery_passphrase="synthetic new recovery phrase 0d1e")
    obj, _ = groups.issue_succession(w.s, w.settings, w.group, w.me, [gc.open_recovery_file(w.files[0], REC_PW[0])],
                                     "synthetic successor root phrase 4a55", reason="compromised",
                                     suspect_since=groups.utcnow() - timedelta(hours=1),
                                     new_recovery_passphrase="synthetic new recovery phrase 0d1e")
    assert obj["body"]["old_root_last_crl"] == last_of(honest)


# ---- finding 2: a pushed succession is attributed to the pushing member ------------------------------------------

def test_pushed_succession_not_confirmed_by_its_pusher(net):
    a, b, c = net.node("a"), net.node("b"), net.node("c")
    created = create(a)
    finish_creation(a)
    enrol(a, b)
    enrol(a, c)
    with a.session() as s:
        group = groups.load(s)
        obj, _ = groups.issue_succession(s, a.settings, group, a.identity,
                                         [gc.open_recovery_file(created.recovery_files[0], REC_PW[0])],
                                         "synthetic successor root phrase 4a55", reason="lost",
                                         new_recovery_passphrase="synthetic new recovery phrase 0d1e")
        s.commit()
    with a.session() as s:
        assert group_routes.push_state(s, a.settings, a.identity, groups.load(s), a.net,
                                       only={c.node_id}) == {c.node_id: "ok"}
    with c.session() as s:
        group = groups.load(s)
        (entry,) = groups.pending_successions(group)
        assert entry.get("delivered_by") == a.node_id
        due = groups.t(entry["activates_at"]) + timedelta(minutes=1)
        groups.note_view_for_succession(s, group, node_view(group.group_id, a.identity, due, succession_pending=True),
                                        c.node_id, due)
        assert groups.activate_successions(s, group, due, c.node_id) == 0  # the pusher's own view never confirms
        groups.note_view_for_succession(s, group, node_view(group.group_id, b.identity, due, succession_pending=True),
                                        c.node_id, due)
        assert groups.activate_successions(s, group, due, c.node_id) == 1


def test_enrolment_state_attributed_to_inviter(net, monkeypatch):
    from atlas import group_enrol

    a, b = net.node("a"), net.node("b")
    create(a)
    finish_creation(a)
    seen = []
    real = groups.ingest_state

    def spy(*args, **kw):
        seen.append(kw.get("served_by"))
        return real(*args, **kw)

    monkeypatch.setattr(group_enrol.groups, "ingest_state", spy)
    enrol(a, b)
    assert seen and seen[0] == a.node_id


def test_journal_keeps_deliverer_of_pending_succession(w):
    x, y = synth(), synth()
    w.enrol(x)
    w.enrol(y)
    (rec,) = w.recovery_keys()
    succ = w.succession(1, Ed25519PrivateKey.generate(), [rec])
    groups.ingest_succession(w.s, w.group, succ, groups.utcnow(), delivered_by=x.node_id)
    w.group.succession_pending = []  # a restore from before the succession arrived
    w.s.merge(NodeState(key=group_journal.REPLAY_DUE, value="1"))
    w.s.flush()
    group_journal.replay(w.s, w.group, w.me.node_id)
    (entry,) = groups.pending_successions(w.group)
    assert entry.get("delivered_by") == x.node_id


# ---- finding 3: every entry of an origin's grace run is applied after renewal -------------------------------------

def test_grace_run_applied_in_order_after_renewal(net):
    from .channel_harness import group_of

    a, b = group_of(net.node("a"), net.node("b"))
    ua, ub = user(a, "alice"), user(b, "alicia")
    link(a, ua, b, ub)
    rounds(a, b)
    edit(b, ub, "theme", "dark")
    edit(b, ub, "theme", "light")
    uid = uid_of(b, ub)
    run = [e for e in entries_of(b, b.node_id, uid) if e["subject"] == "theme"]
    assert len(run) == 2
    with a.session() as s:
        end = gc.parse_time(groups.trust(s, groups.load(s)).in_force(b.node_id)["body"]["not_after"])
        gid = groups.load(s).group_id
        row = s.get(GroupStream, (gid, b.node_id, run[0]["epoch"], uid))
        row.advertised_head, row.advertised_at = run[-1]["seq"], end - timedelta(hours=1)  # announced before expiry
        s.commit()
    entries = entries_of(b, b.node_id, uid)
    assert deliver(a, entries, now=end + timedelta(hours=1), served_by=b.node_id)[-1][1:] == ("held", "origin_expired")
    with a.session() as s:
        group = groups.load(s)
        groups.renew(s, a.settings, group, a.identity, [b.node_id],
                     root=groups.unlock_root(s, a.settings, group, ROOT_PW), now=end - timedelta(hours=1))
        s.commit()
    at = end + timedelta(hours=2)
    from atlas import group_channel

    with a.session() as s:
        group = groups.load(s)
        group_channel.release_expired_held(s, a.settings, a.identity, group,
                                           groups.trust(s, group, at, own_node=a.node_id), at)
        s.commit()
    outs = deliver(a, entries, now=at, served_by=b.node_id)  # the rest of the run, on the next pull
    assert all(o[1] == "applied" for o in outs), outs
    assert shown(a, ua, "theme") == "light"
    with a.session() as s:
        dispositions = [s.get(GroupChange, (gid, b.node_id, e["epoch"], uid, e["seq"])).disposition for e in run]
        assert dispositions == ["applied", "applied"]


# ---- finding 4: the journal is not replayed again and again after compaction ---------------------------------------

def test_journal_not_replayed_on_every_pull_after_compaction(w):
    d = synth()
    parent = w.delegate(d, max_members=2)["body"]["serial"]
    first, second = w.node_cert(d, synth(), 1, parent), w.node_cert(d, synth(), 2, parent)
    w.ingest(first)
    w.ingest(second)
    e1 = groups.crl_entry(first, "removed", groups.utcnow())
    e2 = groups.crl_entry(second, "removed", groups.utcnow())
    for seq, entries in ((1, [e1]), (2, [e1]), (3, [e1, e2])):
        assert w.apply(w.crl(d, entries, seq)) == "stored"
    root = w.root()
    honest, excluded = w.crl(None, [], 1, root=root), w.crl(None, [], 2, root=root)
    w.apply(honest)
    w.apply(excluded)
    (rec,) = w.recovery_keys()
    succ = w.succession(1, Ed25519PrivateKey.generate(), [rec], last_crl=last_of(honest))
    assert groups.ingest_succession(w.s, w.group, succ, groups.utcnow(), delivered_by=d.node_id) == "pending"
    assert groups.activate_successions(w.s, w.group, later(), w.me.node_id) == 1
    assert w.s.get(GroupCrl, (w.gid, "root", 2, excluded["body_hash"])) is None  # deleted at activation
    w.s.commit()
    lines = group_journal.read(w.settings, w.gid)
    assert not group_journal.missing(w.s, w.group, lines)
    for _ in range(3):
        group_journal.replay_if_needed(w.s, w.group, w.me.node_id, w.settings)
    assert len(audited(w, "group_journal_replayed")) == 0


def test_journal_replays_once_for_an_object_the_db_lost(w):
    x = synth()
    cx = w.enrol(x)
    w.apply(w.crl(None, [groups.crl_entry(cx, "removed", groups.utcnow())], 1))
    from atlas.models import GroupCrl, GroupRevoked

    w.s.query(GroupCrl).delete()
    w.s.query(GroupRevoked).delete()
    w.s.flush()
    assert group_journal.replay_if_needed(w.s, w.group, w.me.node_id, w.settings) is not None
    assert w.s.get(GroupRevoked, (w.gid, cx["body"]["serial"])) is not None
    for _ in range(3):
        assert group_journal.replay_if_needed(w.s, w.group, w.me.node_id, w.settings) is None
    assert len(audited(w, "group_journal_replayed")) == 1


# ---- finding 5: a restore with the journal off still finishes its resync ------------------------------------------

def test_resync_finishes_after_restore_with_journal_off(w):
    x = synth()
    w.enrol(x)
    w.settings = w.settings.model_copy(update={"group_journal_dir": None})
    w.s.merge(NodeState(key=group_journal.REPLAY_DUE, value="1"))  # what an older restore wrote
    w.group.status = "resyncing"
    w.s.flush()
    group_journal.replay_if_needed(w.s, w.group, w.me.node_id, w.settings)
    group_routes._finish_resync(w.s, w.settings, w.me, w.group, {x.node_id: "ok"})
    assert w.group.status == "active"


def test_restore_with_journal_off_marks_no_replay(w, tmp_path):
    from atlas import backup

    x = synth()
    w.enrol(x)
    w.s.commit()
    archive = tmp_path / "before.tar"
    backup.create(w.settings, w.engine, archive, include_media=False)
    w.s.close()
    off = w.settings.model_copy(update={"group_journal_dir": None})
    backup.restore(off, w.engine, archive, force=True, include_media=False)
    with w.maker() as s:
        assert s.get(NodeState, group_journal.REPLAY_DUE) is None
        assert s.get(NodeState, "group_epoch_due") is not None
    w.s = w.maker()


# ---- finding 6: two successions whose old-root CRLs are both missing ----------------------------------------------

def test_two_missing_succession_crls_both_retired(w):
    x = synth()
    cx = w.enrol(x)
    me = w.trust().in_force(w.me.node_id)
    root0 = w.root()
    honest0 = w.crl(None, [], 2, root=root0)  # signed, never reached this node
    w.apply(w.crl(None, [groups.crl_entry(cx, "key_compromise", groups.utcnow(), groups.utcnow())], 3, root=root0))
    assert w.s.get(GroupBannedNode, (w.gid, x.node_id)) is not None  # the thief's ban, before the succession
    (rec,) = w.recovery_keys()
    key1 = Ed25519PrivateKey.generate()
    s1 = w.succession(1, key1, [rec], last_crl=last_of(honest0), carry=carry_of(me, cx))
    assert groups.ingest_succession(w.s, w.group, s1, groups.utcnow(), delivered_by=x.node_id) == "pending"
    at1 = later(25)
    assert groups.activate_successions(w.s, w.group, at1, w.me.node_id) == 1
    kid1 = w.group.root_key_id
    honest1 = w.crl(None, [], 1, root=key1, root_key_id=kid1)  # never reached this node either
    s2 = w.succession(2, Ed25519PrivateKey.generate(), [rec], last_crl=last_of(honest1), carry=carry_of(me, cx),
                      previous=kid1)
    assert groups.ingest_succession(w.s, w.group, s2, at1, delivered_by=x.node_id) == "pending"
    at2 = at1 + timedelta(hours=25)
    assert groups.activate_successions(w.s, w.group, at2, w.me.node_id) == 1
    assert groups.ingest_crl(w.s, w.group, honest1, at2) == "stored"
    fresh = groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24, at2)
    assert not fresh.ok and fresh.reason == "succession_crl_missing"  # succession 1's CRL is still missing
    assert groups.ingest_crl(w.s, w.group, honest0, at2) == "stored"
    assert w.s.get(NodeState, groups.SUCCESSION_CRL_MISSING) is None
    assert w.s.get(GroupBannedNode, (w.gid, x.node_id)) is None  # succession 1 retired the thief's ban
    assert groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24, at2).ok


# ---- finding 7: a non-holder in a two-member group can end its trust epoch ------------------------------------------

def test_non_holder_ends_trust_epoch_with_recovery_quorum(w):
    x = synth()
    w.enrol(x)
    w.group.status = "resyncing"
    groups.skip_resync(w.s, w.group)
    w.group.root_state, w.group.root_sealed = "none", None  # this node does not hold the root
    assert groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24).reason == "resync_skipped"
    with pytest.raises(groups.GroupError):
        groups.confirm_trust_epoch(w.s, w.settings, w.group, None)
    groups.confirm_trust_epoch(w.s, w.settings, w.group, None,
                               recovery_files=[gc.open_recovery_file(w.files[0], REC_PW[0])])
    assert groups.freshness(w.s, w.group, w.me.node_id, x.node_id, 24).waived
    (entry,) = audited(w, "group_resync_confirmed")
    assert entry.detail == {"by": "recovery"}


def test_cli_resync_skip_message_names_the_right_way_out(cli_env, capsys):
    run, nodes, tmp = cli_env
    a, b = nodes["a"], nodes["b"]
    assert run("a", "create", "--name", "Home", "--recovery-out", str(tmp),
               answers=[ROOT_PW, ROOT_PW, REC_PW[0], REC_PW[0]]) == 0
    enrol(a, b)
    with b.session() as s:
        groups.load(s).status = "resyncing"
        s.commit()
    assert run("b", "resync", "--skip") == 0
    out = capsys.readouterr().out
    assert "--confirm-root --recovery-file" in out
    (file,) = [str(p) for p in tmp.glob("atlas-group-*-recovery-1.json")]
    assert run("b", "resync", "--confirm-root", "--recovery-file", file, answers=[REC_PW[0]]) == 0
    with b.session() as s:
        assert s.get(NodeState, groups.TRUST_EPOCH) is None
