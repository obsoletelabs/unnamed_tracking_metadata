"""The feed shape the opt-in federation lab's CONTRACT-C13 §10.2 scenarios expect, in the default test run.

`tests/federation_lab/test_c13_contract_shape.py` hard-codes the sequence numbers of the change feed its upstream
writes. The lab is opt-in (its workflow runs only when lab files change), so when #78 (f07c442) made an authority
publish its seeded multiplayer schema just before the first signed multiplayer value, both scenarios started to fail
there ("one more item") without anything else noticing. This runs the lab's own `_sidecars` step on an in-process
authority and checks it writes exactly `SIDECAR_KINDS`, so the next change to the feed shape fails here first.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from atlas.models import ChangeLog

from .federation_lab.test_c13_contract_shape import SIDECAR_KINDS, _kinds, _sidecars
from .test_p7_backlog import _mp_record
from .test_sync import pair  # noqa: F401  (fixture)


def test_lab_sidecars_write_the_kinds_the_lab_expects(pair):  # noqa: F811
    a_maker, a_ctx, *_ = pair
    cid = _mp_record(a_maker, a_ctx, "1962700", "Subnautica 2")
    with a_maker() as s:
        head = s.scalar(select(ChangeLog.seq).order_by(ChangeLog.seq.desc()).limit(1)) or 0
    node = SimpleNamespace(state=SimpleNamespace(maker=a_maker, ctx=a_ctx))
    _sidecars(node, cid)
    assert tuple(kind for _, kind in _kinds(node, after=head)) == SIDECAR_KINDS
    # the schema entry is the node's schema, not about the record: it can never wait for S on a follower
    with a_maker() as s:
        [schema] = s.scalars(select(ChangeLog).where(ChangeLog.seq > head, ChangeLog.kind == "schema")).all()
        assert "canonical_id" not in schema.body and {"types", "fields"} <= set(schema.body)


def test_a_second_run_adds_no_schema_entry(pair):  # noqa: F811
    """Published once: the lab's later steps (and its head arithmetic) see no further schema entry."""
    a_maker, a_ctx, *_ = pair
    cid = _mp_record(a_maker, a_ctx, "1962700", "Subnautica 2")
    node = SimpleNamespace(state=SimpleNamespace(maker=a_maker, ctx=a_ctx))
    _sidecars(node, cid)
    with a_maker() as s:
        head = s.scalar(select(ChangeLog.seq).order_by(ChangeLog.seq.desc()).limit(1))
    _sidecars(node, cid)
    assert "schema" not in [kind for _, kind in _kinds(node, after=head)]
