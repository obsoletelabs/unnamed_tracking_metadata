"""Deterministic conflict semantics of the per-field register (CONTRACT-C10 §4.4, §4.6, §11 A11-d/v/z).

The property test replays seeded random concurrent edits, delays, reordering and redelivery across three real
databases and asserts identical displayed values, heads and open conflicts at the end (500 runs, stdlib `random`).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from atlas import ids, profile
from atlas.db import make_sessionmaker
from atlas.models import Base, ProfileConflict, ProfileField, User

from .conftest import build_settings

NODES = ("a" * 16, "b" * 16, "c" * 16)
VALUES = ("dark", "light", "auto", profile.CLEAR)


@dataclass
class Ident:
    node_id: str


def V(hlc, value, base=(), cause="user", origin="a" * 16, cleared=False):
    return profile.V(hlc, value, cleared, tuple(base), cause, origin)


def h(ms: int, node: str = "a", counter: int = 0) -> str:
    return profile.fmt_hlc(ms, counter, node * 16)


# ---- the pure register ---------------------------------------------------------------------------------------

def test_single_head_wins_without_conflict():
    s = profile.compute([V(h(1), "dark"), V(h(2, "b"), "light", base=[h(1)])])
    assert s.winner.value == "light" and s.conflicts == () and [x.hlc for x in s.heads] == [h(2, "b")]


def test_concurrent_user_heads_conflict_highest_hlc_displayed():
    s = profile.compute([V(h(5), "dark"), V(h(7, "b"), "light")])
    assert s.winner.value == "light" and s.conflicts == ((h(7, "b"), h(5)),)


def test_equal_concurrent_values_do_not_conflict():
    s = profile.compute([V(h(5), "dark"), V(h(7, "b"), "dark")])
    assert s.conflicts == () and len(s.heads) == 2


def test_user_edit_outranks_newer_default_or_migration_seed():
    s = profile.compute([V(h(5), "dark"), V(h(9, "b"), "auto", cause="default"), V(h(8, "c"), "light",
                                                                                   cause="migration")])
    assert s.winner.value == "dark" and s.conflicts == ()


def test_merge_version_closes_the_conflict():
    s = profile.compute([V(h(5), "dark"), V(h(7, "b"), "light"), V(h(9), "light", base=[h(5), h(7, "b")])])
    assert s.conflicts == () and [x.hlc for x in s.heads] == [h(9)]


def test_order_of_versions_does_not_matter():
    vs = [V(h(3), "dark"), V(h(4, "b"), "light", base=[h(3)]), V(h(4, "c"), "auto", base=[h(3)]),
          V(h(6), "dark", cleared=True, base=[h(4, "b")])]
    first = profile.compute(vs)
    rng = random.Random(7)
    for _ in range(50):
        rng.shuffle(vs)
        assert profile.compute(vs) == first


# ---- property test: three databases, random delivery -------------------------------------------------------

def _replica(settings):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return make_sessionmaker(engine, settings)


def _observe(session, uid):
    row = session.get(ProfileField, (uid, "theme"))
    heads = tuple(r.hlc for r in profile.heads(session, uid, "theme"))
    open_rows = tuple(sorted((r.winner_hlc, r.loser_hlc) for r in session.scalars(select(ProfileConflict).where(
        ProfileConflict.user_id == uid, ProfileConflict.status == "open"))))
    return (None if row is None else (row.value, row.hlc)), heads, open_rows


def test_property_three_nodes_converge_whatever_the_arrival_order():
    settings = build_settings()
    makers = [_replica(settings) for _ in NODES]
    sessions = [m() for m in makers]
    rng = random.Random(20261101)
    for run in range(500):
        uid = ids.ulid()
        for s in sessions:
            s.add(User(id=uid, username=f"p{run}", password_hash="!", role="user", status="active"))
            s.flush()
        users = [s.get(User, uid) for s in sessions]
        queue: list[tuple[int, dict]] = []  # (target replica, version) messages in flight
        for _ in range(rng.randint(2, 10)):
            if not queue or rng.random() < 0.45:
                i = rng.randrange(3)
                row = profile.set_local(sessions[i], settings, Ident(NODES[i]), users[i], "theme",
                                        rng.choice(VALUES), emit=False)
                msg = {"hlc": row.hlc, "value": row.value, "cleared": row.cleared, "base": list(row.base),
                       "origin": NODES[i]}
                queue += [(j, msg) for j in range(3) if j != i]
            else:
                j, msg = queue.pop(rng.randrange(len(queue)))
                if rng.random() < 0.2:
                    queue.append((j, msg))  # redelivered later
                _ingest(sessions[j], settings, j, users[j], msg)
        rng.shuffle(queue)
        for j, msg in queue + queue[: rng.randint(0, len(queue))]:  # heal the partition, with duplicates
            _ingest(sessions[j], settings, j, users[j], msg)
        seen = {_observe(s, uid) for s in sessions}
        assert len(seen) == 1, (run, seen)
    for s in sessions:
        s.close()


def _ingest(session, settings, j, user, msg):
    out = profile.ingest_remote(session, settings, NODES[j], user, "theme", hlc=msg["hlc"], value=msg["value"],
                                cleared=msg["cleared"], base=msg["base"], cause="user", origin=msg["origin"],
                                entry_ref="x")
    assert out in ("applied", "held", "noop")


# ---- resolution ---------------------------------------------------------------------------------------------

@pytest.fixture
def one(session, settings):
    from atlas.accounts import register

    u = register(session, settings, "dana", "synthetic-Pass-for-tests-4821")
    session.commit()
    return session, settings, u


def _conflict(session, settings, u):
    profile.set_local(session, settings, Ident(NODES[0]), u, "display_name", "Dana A", emit=False)
    now = profile.utcnow()
    hlc = profile.fmt_hlc(int(now.timestamp() * 1000) + 1, 0, NODES[1])
    profile.ingest_remote(session, settings, NODES[0], u, "display_name", hlc=hlc, value="Dana B", cleared=False,
                          base=[], cause="user", origin=NODES[1], entry_ref="x")
    return session.scalars(select(ProfileConflict).where(ProfileConflict.user_id == u.id)).one()


@pytest.mark.parametrize("choice,expected", [("winner", "Dana B"), ("loser", "Dana A"), ("custom", "Dana C")])
def test_resolve_choices_create_a_merging_version(one, choice, expected):
    session, settings, u = one
    row = _conflict(session, settings, u)
    profile.resolve(session, settings, Ident(NODES[0]), u, row.id, choice, custom="Dana C")
    session.commit()
    assert session.get(ProfileField, (u.id, "display_name")).value == expected
    assert len(profile.heads(session, u.id, "display_name")) == 1
    assert session.get(ProfileConflict, row.id).status == "resolved"
    assert profile.open_count(session, u.id) == 0


def test_a_new_edit_while_a_conflict_shows_also_resolves_it(one):
    session, settings, u = one
    row = _conflict(session, settings, u)
    profile.set_local(session, settings, Ident(NODES[0]), u, "display_name", "Dana D", emit=False)
    session.commit()
    assert session.get(ProfileConflict, row.id).status == "merged"


def test_resolving_someone_elses_conflict_is_refused(one):
    session, settings, u = one
    row = _conflict(session, settings, u)
    from atlas.accounts import register

    other = register(session, settings, "eve", "synthetic-Pass-for-tests-4821")
    with pytest.raises(profile.ProfileError):
        profile.resolve(session, settings, Ident(NODES[0]), other, row.id, "winner")
    assert session.get(ProfileConflict, row.id).status == "open"


def test_invalid_custom_value_is_refused(one):
    session, settings, u = one
    row = _conflict(session, settings, u)
    with pytest.raises(profile.ProfileError):
        profile.resolve(session, settings, Ident(NODES[0]), u, row.id, "custom", custom="x" * 65)
    assert session.get(ProfileConflict, row.id).status == "open"


def test_prune_keeps_superseded_hlcs_so_late_redelivery_stays_a_noop(one):
    from datetime import timedelta

    session, settings, u = one
    first = profile.set_local(session, settings, Ident(NODES[0]), u, "theme", "dark", emit=False)
    profile.set_local(session, settings, Ident(NODES[0]), u, "theme", "light", emit=False)
    first.created_at = profile.utcnow() - timedelta(days=181)
    session.flush()
    assert profile.prune(session) == 1
    out = profile.ingest_remote(session, settings, NODES[0], u, "theme", hlc=first.hlc, value="dark", cleared=False,
                                base=[], cause="user", origin=NODES[0], entry_ref="x")
    assert out == "noop"
    assert session.get(ProfileField, (u.id, "theme")).value == "light"
