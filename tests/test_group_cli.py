"""`atlas group ...` exit codes and the persistent state they leave (CONTRACT-C10 §8.2, §11 A10 w)."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import select

from atlas import cli, group_cli, groups
from atlas.models import AuditEntry, GroupRevoked

from .group_harness import REC_PW, ROOT_PW, Dispatcher, fast_kdf, make_node


@pytest.fixture
def env(tmp_path, monkeypatch):
    fast_kdf(monkeypatch)
    net = Dispatcher()
    nodes = {n: make_node(tmp_path, net, n) for n in ("a", "b", "c")}
    state = {"node": None, "answers": []}
    monkeypatch.setattr(group_cli, "_maker", lambda: (state["node"].settings, state["node"].st.maker))
    monkeypatch.setattr(group_cli, "_http", lambda: net)
    monkeypatch.setattr(group_cli.getpass, "getpass", lambda prompt="": state["answers"].pop(0))

    def run(node, *argv, answers=()):
        state["node"], state["answers"] = nodes[node], list(answers)
        return cli.main(["group", *argv])

    yield run, nodes, tmp_path
    for n in nodes.values():
        n.close()


def test_cli_create_invite_join_confirm_members_revoke_status(env, capsys):
    run, nodes, tmp = env
    a, b = nodes["a"], nodes["b"]
    assert run("c", "status") == group_cli.NOT_MEMBER
    assert run("a", "create", "--name", "Home", "--recovery-out", str(tmp),
               answers=[ROOT_PW, ROOT_PW, REC_PW[0], REC_PW[0]]) == 0
    files = list(tmp.glob("atlas-group-*-recovery-1.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["contains"] == ["recovery_key", "root_key"]
    assert run("a", "status") == 0
    capsys.readouterr()

    assert run("a", "invite") == 0
    code = re.search(r"code:\s+(AG1-\S+)", capsys.readouterr().out).group(1)
    assert run("b", "join", "--url", "http://a.test", "--code", code) == 0
    out = capsys.readouterr().out
    eid = re.search(r"enrolment: (\S+)", out).group(1)
    sas = re.search(r"SAS:\s+(\S+)", out).group(1)
    assert run("a", "confirm", eid, "--sas", "0000-0000-0000", answers=[ROOT_PW]) == 1  # wrong SAS: refused
    assert run("a", "confirm", eid, "--sas", sas, answers=["wrong root passphrase!!"]) == 1
    assert run("a", "confirm", eid, "--sas", sas, answers=[ROOT_PW]) == 0
    assert run("b", "confirm", eid, "--sas", sas) == 0
    capsys.readouterr()

    assert run("b", "members", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert {r["node_id"] for r in listed} == {a.node_id, b.node_id}
    assert run("b", "sync") == 0

    assert run("a", "revoke", "--node", b.node_id, answers=[ROOT_PW]) == 0
    with a.session() as s:
        group = groups.load(s)
        assert s.scalars(select(GroupRevoked).where(GroupRevoked.node_id == b.node_id)).first().reason == "removed"
        cli_rows = [e for e in s.scalars(select(AuditEntry).where(AuditEntry.action == "group_member_revoked"))]
        assert cli_rows and cli_rows[0].detail["cli"] is True
        assert groups.trust(s, group, own_node=a.node_id).members().keys() == {a.node_id}
    assert run("b", "sync") == group_cli.DEGRADED  # b is no longer a member: a answers 404 to its calls
    assert run("a", "crl", "--json") == 0
    assert b.node_id in capsys.readouterr().out


def test_cli_leave_guard_and_root_export(env, capsys):
    run, nodes, tmp = env
    assert run("a", "create", "--name", "Home", "--recovery", "2-of-3", "--recovery-out", str(tmp),
               answers=[ROOT_PW, ROOT_PW, *[p for p in REC_PW for _ in (0, 1)]]) == 0
    assert len(list(tmp.glob("atlas-group-*-recovery-*.json"))) == 3
    assert run("a", "leave") == 1  # the 2-of-3 files hold no G: leaving would destroy it
    out = tmp / "root.json"
    phrase = "synthetic export phrase 55aa"
    assert run("a", "root", "export-recovery", "--out", str(out), answers=[ROOT_PW, phrase, phrase]) == 0
    assert json.loads(out.read_text())["contains"] == ["root_key"]
    assert run("a", "root", "verify-recovery", "--recovery-file", str(out), answers=[phrase]) == 0
    assert run("a", "leave") == 0
    with nodes["a"].session() as s:
        group = groups.load(s)
        assert group.status == "left" and group.root_sealed is None
    assert run("a", "status") == group_cli.NOT_MEMBER


def test_cli_weak_passphrase_refused(env, capsys):
    run, _, tmp = env
    assert run("a", "create", "--name", "Home", "--recovery-out", str(tmp),
               answers=["password12345678", "password12345678", REC_PW[0], REC_PW[0]]) == 1
    assert "passphrase refused" in capsys.readouterr().err
