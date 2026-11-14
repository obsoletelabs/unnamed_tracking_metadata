"""Migration 0036 (C13 federation ergonomics, A19), T-F9: upgrade a 0035 database that holds the live failure's shape
(an authority feed with an observation-less record named by signed sidecars; a follower with those sidecars
quarantined as "unknown canonical", a peer and replicated artwork), downgrade to 0035 and upgrade again.

Runs on SQLite always and on PostgreSQL when ATLAS_TEST_PG_URL is set (or `pgserver` is installed)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, text

from atlas import db

from .test_postgres import pg_url  # noqa: F401  (module-scoped PostgreSQL fixture)

PREVIOUS, HEAD = "0035", "0036"
T = "2026-11-03 10:00:00"
A, S, Q, M, N = ("atlas:01aaaaaaaaaaaaaaaaaaaaaaaa", "atlas:01ssssssssssssssssssssssss", "atlas:01qqqqqqqqqqqqqqqqqqqqqqqq",
                 "atlas:01mmmmmmmmmmmmmmmmmmmmmmmm", "atlas:01nnnnnnnnnnnnnnnnnnnnnnnn")
UP = "356cae19243bb510"
ART = "https://img.example.org/s-key.png"


@pytest.fixture(params=["sqlite", "postgresql"])
def url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path}/m.db"
    pg = request.getfixturevalue("pg_url")
    engine = create_engine(pg)
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    engine.dispose()
    return pg


def version(engine) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def columns(engine, table) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def seed(engine):
    """Both sides of the observed pair in one database (an authority that also follows: what the migration reads is
    per table, so one database exercises every backfill)."""
    with engine.begin() as c:
        for cid in (A, S, Q, M):
            c.execute(text("INSERT INTO canonical (id, media_type, norm_title, view, explain, version, created_at, "
                           "updated_at) VALUES (:i, 'game', '', '{}', '{}', 1, :t, :t)"), {"i": cid, "t": T})
        c.execute(text("UPDATE canonical SET merged_into = :a WHERE id = :m"), {"a": A, "m": M})
        own = [(1, "obs", {"canonical_id": A, "media_type": "game", "observation": {"id": "o1"}}),
               (2, "artwork", {"canonical_id": S, "source": "curator", "items": [{"kind": "key_art", "url": ART}]}),
               (3, "achievements", {"canonical_id": S, "source": "curator", "items": []}),
               (4, "merge", {"src": M, "dst": A}),
               (5, "detach", {"observation_id": "o1", "canonical_id": A, "new_canonical_id": N})]
        for seq, kind, body in own:
            c.execute(text("INSERT INTO change_log (seq, kind, body, body_hash, signature, key_id, created_at) "
                           "VALUES (:s, :k, :b, 'h', 'sig', 'k', :t)"), {"s": seq, "k": kind, "b": json.dumps(body),
                                                                       "t": T})
        c.execute(text("INSERT INTO replicated_feed (node_id, public_key) VALUES (:n, 'pk')"), {"n": UP})
        retained = [(6, "artwork", {"canonical_id": "atlas:01zzzzzzzzzzzzzzzzzzzzzzzz", "source": "curator",
                                    "items": [{"kind": "key_art", "url": ART}]}),
                    (7, "achievements", {"canonical_id": "atlas:01zzzzzzzzzzzzzzzzzzzzzzzz", "source": "curator",
                                         "items": []}),
                    (8, "merge", {"src": "atlas:01yyyyyyyyyyyyyyyyyyyyyyyy", "dst": A}),
                    (9, "custom", {"canonical_id": M, "source": "curator", "values": {}, "clear": []}),
                    (10, "lock", {"canonical_id": A, "field": "title", "value": "x"}),
                    (11, "artwork", {"canonical_id": A, "source": "Curator", "items": [{"kind": "logo", "url": ART}]})]
        for seq, kind, body in retained:
            c.execute(text("INSERT INTO replicated_change (node_id, seq, kind, body, body_hash, signature, key_id, "
                           "created_at) VALUES (:n, :s, :k, :b, 'h', 'sig', 'k', :t)"),
                      {"n": UP, "s": seq, "k": kind, "b": json.dumps(body), "t": T})
        failures = [(6, "artwork", "AtlasError: unknown or merged canonical", "apply_error", "quarantined", 20),
                    (7, "achievements", "AtlasError: unknown or merged canonical", "apply_error", "quarantined", 3),
                    (8, "merge", "AtlasError: merge of an unknown record", "apply_error", "quarantined", 1),
                    (9, "custom", "AtlasError: unknown canonical", "apply_error", "quarantined", 2),
                    (10, "lock", "AtlasError: something else", "apply_error", "quarantined", 4)]
        for seq, kind, error, reason, disposition, attempts in failures:
            c.execute(text("INSERT INTO sync_failure (node_id, seq, kind, error, attempts, first_failed_at, "
                           "last_attempt_at, disposition, reason) VALUES (:n, :s, :k, :e, :a, :t, :t, :d, :r)"),
                      {"n": UP, "s": seq, "k": kind, "e": error, "a": attempts, "t": T, "d": disposition, "r": reason})
        c.execute(text("INSERT INTO peer (id, name, url, node_id, public_key, enabled, created_at) VALUES "
                       "('01pppppppppppppppppppppppp', 'Atlas Central', 'https://central.example', :n, 'pk', true, :t)"),
                  {"n": UP, "t": T})
        for aid, cid, source, kind in (("01art1aaaaaaaaaaaaaaaaaaaa", A, "curator", "logo"),
                                       ("01art2aaaaaaaaaaaaaaaaaaaa", S, "curator", "key_art")):
            c.execute(text("INSERT INTO artwork (id, canonical_id, source, kind, url, priority, redistributable, "
                           "fail_count, created_at, preferred, local_only) VALUES (:i, :c, :s, :k, :u, 10, true, 0, :t, "
                           "false, false)"), {"i": aid, "c": cid, "s": source, "k": kind, "u": ART, "t": T})


def failures(engine) -> dict[int, tuple]:
    """(disposition, reason, waiting_for, attempts, error) per seq; without `waiting_for` before 0036."""
    waiting = ", waiting_for" if "waiting_for" in columns(engine, "sync_failure") else ""
    with engine.connect() as c:
        return {r[0]: tuple(r[1:]) for r in c.execute(text(
            f"SELECT seq, disposition, reason{waiting}, attempts, error FROM sync_failure ORDER BY seq"))}


def test_migration_converts_unknown_record_quarantine_to_wait(url):
    db.upgrade(url, PREVIOUS)
    engine = db.make_engine(url)
    seed(engine)

    db.upgrade(url, HEAD)
    assert version(engine) == HEAD
    assert {"follow", "via_node_id"} <= columns(engine, "peer")
    assert "waiting_for" in columns(engine, "sync_failure")
    assert {"origin_node", "fetched_from", "sources_tried", "peer_digest", "digest_provenance"} <= columns(
        engine, "artwork")
    with engine.connect() as c:
        introduced = {r[0]: (r[1], r[2]) for r in c.execute(text("SELECT canonical_id, seq, via FROM feed_introduced"))}
        arts = dict(c.execute(text("SELECT id, origin_node FROM artwork")).all())
    # obs at 1 introduces A; the detach at 5 introduces N; S is named by signed sidecars only and is live: legacy.
    # M is named by the merge but is no longer live, so nothing marks it.
    assert introduced == {A: (1, "obs"), N: (5, "detach"), S: (None, "legacy")}
    # The artwork row the retained entry 11 lists (source normalised like the application does) came from UP; the
    # row on S came from this node's own feed (entry 2) and has no origin.
    assert arts == {"01art1aaaaaaaaaaaaaaaaaaaa": UP, "01art2aaaaaaaaaaaaaaaaaaaa": None}
    rows = failures(engine)
    assert rows[6][:3] == ("wait", "waiting_for_record", "atlas:01zzzzzzzzzzzzzzzzzzzzzzzz")
    assert rows[7][:3] == ("wait", "waiting_for_record", "atlas:01zzzzzzzzzzzzzzzzzzzzzzzz")
    assert rows[8][:3] == ("wait", "waiting_for_record", "atlas:01yyyyyyyyyyyyyyyyyyyyyyyy")  # the missing src
    assert rows[9][:3] == ("quarantined", "apply_error", None)  # known but merged: the retry follows the redirect
    assert rows[10][:3] == ("quarantined", "apply_error", None)  # another error stays what it was
    assert rows[6][3] == 20  # attempts are history; a waiting row never counts them again

    with engine.begin() as c:  # the operator then confirms which feed the peer follows
        c.execute(text("UPDATE peer SET follow = 'own'"))

    db.downgrade(url, PREVIOUS)
    assert version(engine) == PREVIOUS
    assert "follow" not in columns(engine, "peer") and "waiting_for" not in columns(engine, "sync_failure")
    assert "origin_node" not in columns(engine, "artwork")
    assert not inspect(engine).has_table("feed_introduced") and not inspect(engine).has_table("peer_0036_kept")
    with engine.connect() as c:  # the choice is kept aside in node_state: the schema is exactly 0035's
        kept = c.execute(text("SELECT value FROM node_state WHERE key = 'peer_0036_kept'")).scalar()
    assert [k["follow"] for k in __import__("json").loads(kept)] == ["own"]
    rows = failures(engine)
    for seq in (6, 7, 8):  # the previous release retries them as it did before, from attempt 0
        assert rows[seq][:2] == ("quarantined", "apply_error") and rows[seq][2] == 0
        assert "unknown canonical" in rows[seq][3]
    assert rows[9][:2] == ("quarantined", "apply_error")  # it never waited: unchanged
    assert rows[10] == ("quarantined", "apply_error", 4, "AtlasError: something else")

    db.upgrade(url, HEAD)
    rows = failures(engine)
    assert {seq: rows[seq][:2] for seq in (6, 7, 8)} == {seq: ("wait", "waiting_for_record") for seq in (6, 7, 8)}
    with engine.connect() as c:
        assert c.execute(text("SELECT follow FROM peer")).scalar() == "own"  # the choice came back
        assert c.execute(text("SELECT count(*) FROM feed_introduced")).scalar() == 3
        assert c.execute(text("SELECT count(*) FROM node_state WHERE key = 'peer_0036_kept'")).scalar() == 0
    assert not inspect(engine).has_table("peer_0036_kept")
    engine.dispose()
