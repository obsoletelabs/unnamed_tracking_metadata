"""C11 and federation (A17): nothing new is signed in this release. The merge-undo kinds (`unmerge`, `distinct`,
protocol 1.6), version 2 lock bodies (explicit clear, the merge guard) and the later body versions are reserved and
refused by `check_emit` at every protocol this release speaks; followers of this release and of 1.1 park (never
drop) such entries; a replicated merge is recorded as owned by the upstream; local-only constructs never enter
`feed_schema`."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from atlas import auth, feed, feed_schema, matching, merges, service
from atlas.models import Canonical, Lock, MatchRejection, MergeRecord, ReviewItem

from .test_prices import play
from .test_sync_compat import Node, pull

C11_KINDS = ("unmerge", "distinct")


@pytest.fixture
def fed(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol=feed_schema.PROTOCOL)
    with authority.st.maker() as s:
        _, authority.curate = auth.create_token(s, "curator", ["read", "curate"])
        s.commit()
    return authority, Node(tmp_path, "mirror", "mirror")


def test_registry_reserves_the_c11_constructs_and_keeps_the_since_maps():
    assert feed_schema.PROTOCOL == "1.5"  # C13's record; the C11 constructs moved up one (H52)
    for kind in C11_KINDS:
        assert feed_schema.RESERVED_KINDS[kind] == "1.7" == feed_schema.KIND_SINCE[kind]
        assert kind not in feed_schema.FEED_KINDS
    for token, since in {"field-clear-v1": "1.7", "merge-guard-v1": "1.7", "ach-v2": "1.8", "art-v2": "1.9"}.items():
        assert feed_schema.RESERVED_TOKENS[token] == since == feed_schema.TOKEN_SINCE[token]
        assert token not in feed_schema.known_tokens()
    assert feed_schema.BODY_SINCE[("lock", 2)] == "1.7" and feed_schema.BODY_SINCE[("artwork", 2)] == "1.9"
    table = feed_schema.markdown_table()
    for kind in C11_KINDS:
        assert f"| `{kind}` | 1.7 (reserved) | not in this release | parked by this release |" in table


@pytest.mark.parametrize("protocol", feed_schema.protocol_choices())
def test_check_emit_refuses_every_c11_construct_at_every_protocol(protocol):
    for kind, body in (("unmerge", {"merge_id": "m", "src": "atlas:a", "dst": "atlas:b"}),
                       ("distinct", {"a": "atlas:a", "b": "atlas:b"})):
        with pytest.raises(feed_schema.EmitGateError) as err:
            feed_schema.check_emit(kind, body, protocol)
        assert err.value.needed == "1.7"
    with pytest.raises(feed_schema.EmitGateError) as err:  # an explicit clear is a lock body version 2
        feed_schema.check_emit("lock", {"_v": 2, "canonical_id": "atlas:a", "field": "title", "clear": True,
                                        "_requires": ["field-clear-v1"]}, protocol)
    assert err.value.needed == "1.7"
    with pytest.raises(feed_schema.EmitGateError):  # the merge guard is never a version 1 lock
        feed_schema.check_emit("lock", {"canonical_id": "atlas:a", "field": "_no_merge", "value": "x"}, protocol)
    with pytest.raises(feed_schema.EmitGateError) as err:
        feed_schema.check_emit("artwork", {"_v": 2, "canonical_id": "atlas:a", "source": "s", "items": []}, protocol)
    assert err.value.needed == "1.9"
    with pytest.raises(feed_schema.EmitGateError) as err:
        feed_schema.check_emit("artwork", {"canonical_id": "atlas:a", "source": "s", "items": [],
                                           "_requires": ["art-v2"]}, protocol)


def test_check_emit_since_maps_hold_for_every_registered_kind():
    """KIND_SINCE is permanent: every kind it names is refused below its protocol, whatever release ships it."""
    for kind, since in feed_schema.KIND_SINCE.items():
        for protocol in feed_schema.protocol_choices():
            if feed_schema.parse_protocol(protocol) >= feed_schema.parse_protocol(since):
                continue
            with pytest.raises(feed_schema.EmitGateError):
                feed_schema.check_emit(kind, {"canonical_id": "atlas:x"}, protocol)


def test_local_only_constructs_are_not_federation_kinds():
    for name in ("match_decision", "match_rejection", "merge_record", "field_conflict", "record_live", "review"):
        assert name not in feed_schema.FEED_KINDS and name not in feed_schema.RESERVED_KINDS


@pytest.mark.parametrize("release", ["this", "a15", "1.1"])
@pytest.mark.parametrize("kind", C11_KINDS)
def test_followers_park_an_unmerge_or_distinct_entry_instead_of_dropping_it(fed, monkeypatch, release, kind):
    authority, mirror = fed
    a = authority.publish("Park Left")
    b = authority.publish("Park Right")
    body = {"merge_id": "01JX0000000000000000000000", "src": b, "dst": a} if kind == "unmerge" else {"a": a, "b": b}
    seq = authority.emit(kind, body)
    authority.publish("Park After")
    with monkeypatch.context() as m:
        play(m, release)
        pull(mirror, authority)
        assert mirror.failures(authority) == [(seq, kind, "parked", "unsupported_kind")]
        heads = mirror.heads(authority)
        assert heads["applied_head"] == seq - 1 and heads["verified_head"] == authority.head()
        assert "Park After" not in mirror.titles() and seq in mirror.retained(authority)
    with mirror.st.maker() as s:
        assert s.scalars(select(MatchRejection)).all() == []


@pytest.mark.parametrize("release", ["this", "1.1"])
def test_followers_park_a_version_2_lock(fed, monkeypatch, release):
    authority, mirror = fed
    a = authority.publish("Lock Two")
    seq = authority.emit("lock", {"_v": 2, "canonical_id": a, "field": "description", "clear": True,
                                  "_requires": ["field-clear-v1"]})
    with monkeypatch.context() as m:
        play(m, release)
        pull(mirror, authority)
        [(fseq, fkind, disposition, _reason)] = mirror.failures(authority)
        assert (fseq, fkind, disposition) == (seq, "lock", "parked")
    with mirror.st.maker() as s:
        assert s.get(Lock, (a, "description")) is None


def test_a_replicated_merge_is_recorded_and_owned_by_the_upstream(fed):
    authority, mirror = fed
    a = authority.publish("Fed Left")
    b = authority.publish("Fed Right")
    with authority.st.maker() as s:
        service.merge_canonicals(s, authority.st.ctx, b, a, actor="cur")
        s.commit()
        arec = s.scalars(select(MergeRecord)).one()
        assert arec.feed_seq == feed.head_seq(s)  # the authority knows which feed entry published it
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    with mirror.st.maker() as s:
        rec = s.scalars(select(MergeRecord)).one()
        assert (rec.src, rec.dst, rec.cause, rec.origin_node) == (b, a, "sync", authority.node_id)
        assert rec.origin_seq == arec.feed_seq and rec.feed_seq is None
        assert s.get(Canonical, b).merged_into == a
        with pytest.raises(merges.MergeError) as err:
            merges.unmerge(s, mirror.st.ctx, rec.id, actor="local-cur")
        assert err.value.code == "upstream_owned"


def test_a_local_distinct_mark_stops_a_fold_in(fed):
    """A mirror's own record that turns out to be an upstream record is folded in, unless a curator here said
    the two are different works: then both stay (C11 §11.6)."""
    authority, mirror = fed
    from .conftest import batch

    with mirror.st.maker() as s:
        [own] = service.ingest(s, mirror.st.ctx, batch("steam", [{"external_id": "77", "title": "Mine"}]),
                               curator=True)
        s.commit()
    with authority.st.maker() as s:
        [r] = service.ingest(s, authority.st.ctx, batch("manual", [{"external_id": "up", "title": "Theirs",
                                                                    "provider_ids": {"steam": "77"}}],
                                                         kind="manual"), curator=True)
        s.commit()
        upstream = r.canonical_id
    pull(mirror, authority)
    with mirror.st.maker() as s:
        folded = s.get(Canonical, own.canonical_id).merged_into
    assert folded == upstream  # without a distinct mark the purely local twin is folded in, and recorded
    with mirror.st.maker() as s:
        rec = s.scalars(select(MergeRecord).where(MergeRecord.src == own.canonical_id)).one()
        assert rec.cause == "sync" and rec.origin_node == authority.node_id
    # now a second local record, marked distinct from the upstream one before the upstream claims its key
    with mirror.st.maker() as s:
        [own2] = service.ingest(s, mirror.st.ctx, batch("gog", [{"external_id": "88", "title": "Mine Two"}]),
                                curator=True)
        matching.add_rejection(s, "record", own2.canonical_id, upstream, reason="curator", actor="cur")
        s.commit()
    with authority.st.maker() as s:
        service.ingest(s, authority.st.ctx, batch("manual", [{"external_id": "up", "title": "Theirs",
                                                              "provider_ids": {"steam": "77", "gog": "88"}}],
                                                   kind="manual"), curator=True)
        s.commit()
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    with mirror.st.maker() as s:
        assert s.get(Canonical, own2.canonical_id).merged_into is None
        assert s.scalars(select(MergeRecord).where(MergeRecord.src == own2.canonical_id)).all() == []
        assert matching.active_rejection(s, own2.canonical_id, upstream) is not None
        # §11.5 rule 2: a pair marked as different works is never put up for review again
        items = s.scalars(select(ReviewItem).where(ReviewItem.status == "open")).all()
        assert not any({own2.canonical_id, upstream} <= {item.canonical_id, *(c for c, _ in item.candidates)}
                       for item in items)
