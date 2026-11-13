"""C16 Z2: the upgrade journal (C16 section 6.3), the restore feed freeze (section 6.4, triaged Critical) and the
every-pull fork check (section 6.4 rule 4). Release gates are in `tests/test_release_gates.py`."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import auth, backup, cli, db, feed, feed_resume, main, readiness, sync, upgrade
from atlas.models import ChangeLog, NodeState, Token
from atlas.signing import load_identity

from .conftest import build_settings
from .test_sync_compat import Node, Wire, pull


def state(node: Node, key: str):
    with node.st.maker() as session:
        row = session.get(NodeState, key)
        return None if row is None else row.value


def restore_into(tmp_path, source: Node, archive, name: str, role: str = "authority") -> Node:
    """Back `source` up (at its current head), then restore the archive into a fresh database, as an operator does."""
    settings = source.st.ctx.settings
    engine = db.make_engine(settings.database_url)
    backup.create(settings, engine, archive, include_media=False)
    url = f"sqlite:///{tmp_path}/{name}.db"
    target = build_settings(database_url=url, role=role, media_dir=str(tmp_path / name), media_cache_enabled=False)
    db.upgrade(url, settings=target)
    target_engine = db.make_engine(url)
    backup.restore(target, target_engine, archive, include_media=False)
    return adopt(target)


def adopt(settings) -> Node:
    node = Node.__new__(Node)
    node.app = main.create_app(settings)
    node.st = node.app.state.atlas
    node.client = TestClient(node.app, headers={"Atlas-Wire": "min=1, max=1"})
    with node.st.maker() as session:
        node.token_row, node.token = auth.create_token(session, "follower", ["read", "sync"])
        session.commit()
    return node


def frozen(node: Node):
    with node.st.maker() as session:
        return feed.frozen(session)


@pytest.fixture
def pair(tmp_path):
    """An authority that has published once, the archive taken then, a follower that has since pulled two more
    entries, and the authority restored from the archive (so the follower is ahead of it)."""
    authority = Node(tmp_path, "authority", "authority")
    authority.publish("One")
    archive = tmp_path / "pnr.tar.gz"
    settings = authority.st.ctx.settings
    backup.create(settings, db.make_engine(settings.database_url), archive, include_media=False)
    follower = Node(tmp_path, "follower", "mirror")
    authority.publish("Two")
    authority.publish("Three")
    pull(follower, authority)
    url = f"sqlite:///{tmp_path}/restored.db"
    target = build_settings(database_url=url, role="authority", media_dir=str(tmp_path / "restored"),
                            media_cache_enabled=False)
    db.upgrade(url, settings=target)
    backup.restore(target, db.make_engine(url), archive, include_media=False)
    restored = adopt(target)
    return authority, follower, restored


# ---- the feed freeze (C16-2) --------------------------------------------------------------------------------------

def test_a_restored_authority_starts_feed_frozen_and_says_so(pair):
    authority, _, restored = pair
    record = frozen(restored)
    assert record is not None and 0 < record["head_seq"] < authority.head()
    assert record["head_hash"] and record["archive_created_at"]
    assert restored.client.get("/api/v2/ready").json() == {"status": "degraded"}
    with restored.st.maker() as session:
        assert readiness.check(session, restored.st.ctx.settings).frozen


def test_a_frozen_authority_signs_nothing_and_answers_409_feed_frozen(pair):
    _, _, restored = pair
    before = restored.head()
    with restored.st.maker() as session, pytest.raises(feed.FeedFrozen):
        feed.append(session, restored.st.ctx.identity, "obs", {"x": 1})
    with restored.st.maker() as session, pytest.raises(feed.FeedFrozen):
        feed.append_unchecked(session, restored.st.ctx.identity, "obs", {"x": 1})
    with pytest.raises(feed.FeedFrozen):  # a curator edit signs through `emit`: refused, nothing written
        restored.publish("Frozen Out")

    @restored.app.post("/_test/sign")
    def sign():  # any request that reaches a signing write answers through the app's handlers
        with restored.st.maker() as s:
            feed.append(s, restored.st.ctx.identity, "obs", {"x": 1})

    r = restored.client.post("/_test/sign")
    assert r.status_code == 409 and r.json()["error"]["code"] == "feed_frozen"
    assert "Signing is paused" in r.json()["error"]["message"] and r.headers["Cache-Control"] == "no-store"
    assert restored.head() == before


def test_freeze_does_not_apply_to_a_mirror_restore(tmp_path):
    source = Node(tmp_path, "m1", "mirror")
    restored = restore_into(tmp_path, source, tmp_path / "m.tar.gz", "m2", role="mirror")
    assert frozen(restored) is None


def test_resume_from_node_reinserts_the_signed_tail_verbatim_and_unfreezes(pair):
    authority, follower, restored = pair
    srcs = [feed_resume.Source("follower", "http://upstream", follower.token)]
    import atlas.feed_resume as fr

    http = httpx.Client(transport=httpx.MockTransport(Wire(follower)))
    # the follower retains the authority's feed; the restored node asks it for the tail by its own node id
    assert restored.node_id == authority.node_id
    result = fr.resume_from_nodes(restored.st.maker, restored.st.ctx, srcs, http)
    assert result["resumed"] == "from_node" and result["inserted"] == authority.head() - result["head"]
    assert restored.own_feed() == authority.own_feed()  # the same bytes, never re-signed
    assert frozen(restored) is None and restored.head() == authority.head()
    assert json.loads(state(restored, "feed_resumed"))["how"] == "from_node"
    restored.publish("Four")  # signing works again, and continues the numbering the follower already holds
    assert restored.head() == authority.head() + 1
    with restored.st.maker() as session:
        assert session.scalar(select(ChangeLog.seq).order_by(ChangeLog.seq.desc())) == restored.head()


def test_resume_ignores_a_forged_tail_and_refuses_when_a_follower_is_known_to_be_ahead(pair):
    authority, follower, restored = pair

    def forged(body):  # a source serving tampered bodies: no signature verifies, so nothing is inserted
        for e in body["entries"]:
            e["body"] = {**e["body"], "tampered": True}
        return body

    with restored.st.maker() as session:
        session.add(NodeState(key="follower_since:TOK", value=str(authority.head() - 1)))
        session.commit()
    bad = httpx.Client(transport=httpx.MockTransport(Wire(follower, changes=forged)))
    with pytest.raises(feed_resume.ResumeError, match="no source retains"):
        feed_resume.resume_from_nodes(restored.st.maker, restored.st.ctx,
                                      [feed_resume.Source("bad", "http://upstream", follower.token)], bad)
    assert frozen(restored) is not None and restored.own_feed() != authority.own_feed()


def test_confirm_no_follower_ahead_is_refused_while_a_follower_position_exceeds_the_head(pair):
    authority, follower, restored = pair
    # the follower polls the frozen authority; a wire follower asks for `verified - 1`
    pull_error = None
    try:
        pull(follower, restored, authority)
    except sync.SyncRewound as exc:
        pull_error = exc
    assert pull_error is not None and pull_error.kind == "rewind"  # the authority advertises less than it verified
    with restored.st.maker() as session:
        positions = feed_resume.follower_positions(session)
    assert positions and max(positions.values()) > frozen(restored)["head_seq"]
    token = next(iter(positions))
    with restored.st.maker() as session, pytest.raises(feed_resume.ResumeError) as err:
        feed_resume.confirm_no_follower_ahead(session)
    assert token in str(err.value) and "ahead" in str(err.value)
    assert frozen(restored) is not None


def test_revoking_the_follower_lets_the_operator_confirm(pair):
    authority, follower, restored = pair
    with pytest.raises(sync.SyncRewound):
        pull(follower, restored, authority)
    with restored.st.maker() as session:
        [token] = feed_resume.follower_positions(session)
        assert session.get(Token, token) is not None
        result = feed_resume.confirm_no_follower_ahead(session, revoke=[token])
        session.commit()
    assert result["revoked"] == [token] and frozen(restored) is None
    with restored.st.maker() as session:
        assert session.get(Token, token).revoked_at is not None


def test_confirm_with_no_follower_ahead_resumes_and_is_audited(tmp_path):
    node = Node(tmp_path, "solo", "authority")
    node.publish("One")
    restored = restore_into(tmp_path, node, tmp_path / "solo.tar.gz", "solo2")
    assert frozen(restored) is not None
    with restored.st.maker() as session:
        feed_resume.confirm_no_follower_ahead(session)
        session.commit()
    assert frozen(restored) is None
    restored.publish("Two")
    with restored.st.maker() as session:
        from atlas.models import AuditEntry

        assert session.scalar(select(AuditEntry).where(AuditEntry.action == "feed_resume")) is not None


def test_cli_feed_resume_reports_status_and_confirms(tmp_path, monkeypatch, capsys):
    node = Node(tmp_path, "cli", "authority")
    node.publish("One")
    restored = restore_into(tmp_path, node, tmp_path / "cli.tar.gz", "cli2")
    s = restored.st.ctx.settings
    monkeypatch.setattr(cli, "_maker", lambda: (s, restored.st.maker))
    monkeypatch.setattr(cli, "_ctx", lambda settings, maker: restored.st.ctx)
    assert cli.main(["feed", "resume", "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["frozen"] is True
    assert cli.main(["feed", "resume", "--confirm-no-follower-ahead", "--from-node", "http://x"]) == 2  # not both
    assert cli.main(["feed", "resume", "--confirm-no-follower-ahead"]) == 0 and frozen(restored) is None
    capsys.readouterr()
    assert cli.main(["feed", "resume", "--status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"frozen": False}


# ---- every pull re-checks the last verified entry (rule 4) ------------------------------------------------------

def test_a_restored_authority_that_signs_past_the_follower_is_detected_as_fork_not_ok(pair):
    """T-U17: the old behaviour (fetch from `verified`) accepted the new entries silently and kept the old ones."""
    authority, follower, restored = pair
    with restored.st.maker() as session:  # an authority restored with the OLD binary: no freeze record
        session.execute(NodeState.__table__.delete().where(NodeState.key == feed.FEED_RESTORED))
        session.commit()
    while restored.head() <= authority.head() + 1:
        restored.publish(f"Diverged {restored.head()}")
    with pytest.raises(sync.SyncRewound) as err:
        pull(follower, restored, authority)
    assert err.value.kind == "fork"


def test_an_idle_pull_refetches_one_entry_and_reports_zero_new(pair):
    authority, follower, _ = pair
    n, w = pull(follower, authority)
    assert n == 0
    asked = [r for r in w.seen if r[0].endswith("/sync/changes")]
    assert asked  # and the since it sent is one below the verified head:
    with follower.st.maker() as session:
        verified = sync.verified_head(session, authority.node_id)
    seen = []
    http = httpx.Client(transport=httpx.MockTransport(lambda req: (seen.append(dict(req.url.params)),
                                                                     Wire(authority)(req))[1]))
    sync.pull_all(follower.st.maker, follower.st.ctx, http, "http://upstream", authority.token,
                  trusted={authority.node_id: authority.st.ctx.identity.trust_public_b64})
    changes = [p for p in seen if "since" in p]
    assert changes and int(changes[-1]["since"]) == verified - 1


# ---- the upgrade journal (C16-1) -----------------------------------------------------------------------------------

def finish(maker, ctx, rounds: int = 12) -> dict:
    """What the sync loop does each round: the startup replay once, then the journal's next batch."""
    sync.startup_replay(maker, ctx)
    for _ in range(rounds):
        journal = upgrade.step(maker, ctx)
        if upgrade.complete(journal):
            return journal
    return journal


def v0_authority(tmp_path, name="up", **extra):
    """A database as a v0 release left it: an authority with signed entries and no journal, no wire_start."""
    url = f"sqlite:///{tmp_path}/{name}.db"
    s = build_settings(database_url=url, role="authority", media_dir=str(tmp_path / name), media_cache_enabled=False,
                       **extra)
    db.upgrade(url, settings=s)
    engine = db.make_engine(url)
    maker = db.make_sessionmaker(engine, s)
    with maker() as session:
        identity = load_identity(session, s)
        feed.append_unchecked(session, identity, "obs", {"canonical_id": "c" * 26, "legacy": True})
        feed.append_unchecked(session, identity, "obs", {"canonical_id": "d" * 26, "legacy": True})
        session.commit()
    return s, engine, maker


def test_the_journal_exists_before_the_app_serves_and_fixes_the_point_of_no_return(tmp_path):
    s, _, maker = v0_authority(tmp_path)
    app = main.create_app(s)  # not started as a server: create_app alone must have written the journal
    with maker() as session:
        journal = upgrade._read(session)
        assert journal["from"] == "v0-1.x" or journal["from"] == "v0-1.0" or journal["from"] == "fresh"
        assert journal["pre_upgrade_head"]["seq"] == 2
        assert journal["first_v1_seq"] == 3 == feed.wire_start_seq(session)  # wire_start is the first wire entry
        kinds = [r.kind for r in session.scalars(select(ChangeLog).order_by(ChangeLog.seq))]
        assert kinds == ["obs", "obs", "wire_start"]
        assert not upgrade.complete(journal)  # the introduce stages are still pending: readiness is degraded
    assert TestClient(app).get("/api/v2/ready").json() == {"status": "degraded"}


def test_open_journal_is_idempotent_and_never_moves_first_v1_seq(tmp_path):
    s, engine, maker = v0_authority(tmp_path)
    app = main.create_app(s)
    ctx = app.state.atlas.ctx
    first = upgrade.open_journal(maker, ctx, engine)
    again = upgrade.open_journal(maker, ctx, engine)
    assert first == again and first["first_v1_seq"] == 3
    main.create_app(s)  # a second worker
    with maker() as session:
        assert upgrade._read(session)["first_v1_seq"] == 3
        assert session.scalar(select(ChangeLog.seq).where(ChangeLog.kind == "wire_start")) == 3


def test_an_authority_with_a_feed_gets_a_backup_before_anything_can_sign(tmp_path):
    out = tmp_path / "backups"
    s, _, maker = v0_authority(tmp_path, backup_dir=str(out))
    main.create_app(s)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    assert len(files) == 1
    manifest = backup.read_manifest(files[0])
    assert manifest["tables"]["change_log"] == 2  # taken before wire_start: the 2 pre-upgrade entries
    with maker() as session:
        assert upgrade._read(session)["pnr_backup"]


def test_steps_run_the_stages_to_completion_and_readiness_recovers(tmp_path):
    s, _, maker = v0_authority(tmp_path)
    app = main.create_app(s)
    ctx = app.state.atlas.ctx
    client = TestClient(app)
    assert client.get("/api/v2/ready").json() == {"status": "degraded"}
    journal = finish(maker, ctx)
    assert upgrade.complete(journal) and journal["authority_for"] == ctx.identity.node_id
    with maker() as session:
        report = upgrade.status(session)
    assert report["complete"] and report["stage"] is None
    assert client.get("/api/v2/ready").json() == {"status": "ready"}
    assert upgrade.step(maker, ctx) == journal  # a cheap no-op afterwards


def test_a_crash_between_stages_resumes_and_never_repeats_a_finished_one(tmp_path):
    s, _, maker = v0_authority(tmp_path)
    app = main.create_app(s)
    ctx = app.state.atlas.ctx
    upgrade.step(maker, ctx)  # one batch: the first pending stage
    with maker() as session:
        done_after_one = {k for k, v in upgrade._read(session)["stages"].items() if v == "done"}
    assert done_after_one
    app2 = main.create_app(s)  # "the process restarted": the journal is read, not recreated
    with maker() as session:
        again = upgrade._read(session)
        assert {k for k, v in again["stages"].items() if v == "done"} >= done_after_one
    assert upgrade.complete(finish(maker, app2.state.atlas.ctx))


def test_a_stage_that_fails_keeps_pending_with_last_error_and_is_retried(tmp_path, monkeypatch):
    s, _, maker = v0_authority(tmp_path)
    ctx = main.create_app(s).state.atlas.ctx
    boom = {"n": 0}
    real = feed.introduce_batch

    def flaky(*a, **kw):
        boom["n"] += 1
        if boom["n"] == 1:
            raise feed.IntroduceRefused("simulated refusal")
        return real(*a, **kw)

    monkeypatch.setattr(feed, "introduce_batch", flaky)
    journal = upgrade.step(maker, ctx)
    assert "simulated refusal" in journal["last_error"] and not upgrade.complete(journal)
    final = finish(maker, ctx)
    assert upgrade.complete(final) and final["last_error"] is None


def test_another_workers_lease_keeps_this_worker_out(tmp_path):
    s, _, maker = v0_authority(tmp_path)
    ctx = main.create_app(s).state.atlas.ctx
    with maker() as session:
        session.add(NodeState(key=upgrade.LEASE, value=json.dumps(
            {"holder": "other-host:1:1", "until": (datetime.now(UTC) + timedelta(seconds=60)).isoformat()})))
        session.commit()
    before = upgrade.step(maker, ctx)
    assert before["stages"] == upgrade.step(maker, ctx)["stages"] and not upgrade.complete(before)
    with maker() as session:  # an expired lease is taken over
        row = session.get(NodeState, upgrade.LEASE)
        row.value = json.dumps({"holder": "other-host:1:1", "until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()})
        session.commit()
    assert upgrade.complete(finish(maker, ctx))


def test_a_non_authority_marks_the_authority_stages_not_applicable(tmp_path):
    node = Node(tmp_path, "mir", "mirror")
    with node.st.maker() as session:
        journal = upgrade._read(session)
    assert journal["stages"]["introduce_missing"] == "n/a" and journal["stages"]["relations"] == "n/a"
    assert journal["authority_for"] is None  # a later promotion runs them
    assert upgrade.complete(journal)


def test_a_fresh_authority_has_nothing_to_finish(tmp_path):
    node = Node(tmp_path, "fresh", "authority")
    with node.st.maker() as session:
        journal = upgrade._read(session)
        assert journal["from"] == "fresh" and upgrade.complete(journal) and journal["first_v1_seq"] is None
        assert not upgrade.unfinished(session)
    assert node.client.get("/api/v2/ready").json() == {"status": "ready"}


def test_an_unfinished_journal_older_than_a_day_is_reported_by_atlas_check(tmp_path):
    s, _, maker = v0_authority(tmp_path)
    main.create_app(s)
    with maker() as session:
        assert not upgrade.overdue(session)
        journal = upgrade._read(session)
        journal["started_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        upgrade._write(session, journal)
        session.commit()
    from atlas import consistency

    with maker() as session:
        assert upgrade.overdue(session)
        report = consistency.run(session, s, only=["upgrade_unfinished"])
    assert report["checks"]["upgrade_unfinished"]["count"] == 1 and not report["ok"]


def test_admin_status_shows_the_journal_and_the_freeze(pair):
    _, _, restored = pair
    with restored.st.maker() as session:
        _, admin = auth.create_token(session, "ops", ["admin", "read"])
        session.commit()
    body = restored.client.get("/api/v2/status", headers={"Authorization": f"Bearer {admin}"}).json()
    assert body["feed_frozen"]["head_seq"] > 0 and body["checks"]["feed"] == "frozen"
    assert "upgrade" in body and body["checks"]["readiness"] == "degraded"


def test_a_follower_exactly_one_entry_ahead_blocks_confirm(pair):
    """Review finding: a follower recorded at since=S holds S or S+1, so S == head may already be ahead."""
    _, _, restored = pair
    head = frozen(restored)["head_seq"]
    with restored.st.maker() as session:
        session.add(NodeState(key="follower_since:ONE", value=str(head)))
        session.commit()
    with restored.st.maker() as session, pytest.raises(feed_resume.ResumeError, match="ONE"):
        feed_resume.confirm_no_follower_ahead(session)
    with restored.st.maker() as session:
        session.get(NodeState, "follower_since:ONE").value = str(head - 1)  # holds the head at most: not ahead
        session.commit()
    with restored.st.maker() as session:
        feed_resume.confirm_no_follower_ahead(session)
        session.commit()
    assert frozen(restored) is None


def test_resume_survives_hostile_tail_members(pair):
    authority, follower, restored = pair

    def hostile(body):
        for e in body["entries"]:
            e["signature"], e["body_hash"] = 7, ["x"]
        return body

    http = httpx.Client(transport=httpx.MockTransport(Wire(follower, changes=hostile)))
    with pytest.raises(feed_resume.ResumeError, match="no source retains"):  # no traceback, no unfreeze on nothing
        feed_resume.resume_from_nodes(restored.st.maker, restored.st.ctx,
                                      [feed_resume.Source("bad", "http://upstream", follower.token)], http)
    assert frozen(restored) is not None and restored.own_feed() != authority.own_feed()
