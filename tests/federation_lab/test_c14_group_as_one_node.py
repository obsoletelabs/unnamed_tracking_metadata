"""CONTRACT-C14 §10.5 steps 1-3 on real servers (opt-in lab): the owner's group {N1, N2} asks for what one member
waits for.

* U (`official`) in legacy emission: a provider-only record S with four curated sidecars, never introduced.
* N1 (`local`) follows U and waits for S. Then U introduces S, and N2 (`local2`) follows U and holds it all.
* N1 and N2 form an owner group (real enrolment over HTTP). N1's assist round (driven in-process on N1's database,
  as its group worker would) asks N2's live server with the sealed call, verifies N2's bundle and U's signed
  `record` entry N2 carries (class 1a), and wakes the four waiting entries. N1's own feed signs nothing about S,
  and S stays group-scoped until N1's own pull of U introduces it.

Steps 4-5 of §10.5 (linking to a main and contributions) belong to C14-3m/3n and are not here."""

from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy import select

from atlas import assist, feed, groups
from atlas import group_crypto as gc
from atlas.models import Canonical, ChangeLog, GroupAssistItem, SyncFailure
from atlas.service import Ctx
from tests.group_harness import create, enrol, finish_creation
from tools.federation_lab import Lab

from .test_c13_contract_shape import _provider_only, _sidecars

pytestmark = pytest.mark.federation_lab


class Member:
    """The group harness's view of a lab node (its in-process state on the node's own database)."""

    def __init__(self, node):
        self.node, self.st, self.name = node, node.state, node.name
        self.net = httpx.Client(timeout=20, trust_env=False)

    @property
    def settings(self):
        return self.st.ctx.settings

    @property
    def identity(self):
        return self.st.ctx.identity

    @property
    def node_id(self):
        return self.identity.node_id

    def session(self):
        return self.st.maker()


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setattr(gc, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(gc, "SCRYPT_MIN_N", 2 ** 10)
    with Lab(postgres_admin_url=os.environ.get("ATLAS_LAB_PG_URL") or None) as lab:
        lab.start("official", "local", "local2")
        yield lab


def _waiting(node, origin) -> list[tuple[int, str]]:
    with node.state.maker() as s:
        return [(f.seq, f.kind) for f in s.scalars(select(SyncFailure).where(
            SyncFailure.node_id == origin.node_id, SyncFailure.resolved_at.is_(None),
            SyncFailure.disposition == "wait").order_by(SyncFailure.seq))]


def test_a_member_answers_what_another_waits_for(network):
    official, local, local2 = (network.nodes[n] for n in ("official", "local", "local2"))
    s_id = _provider_only(official, "Subnautica 2", "1962700")
    _sidecars(official, s_id)
    peer1 = local.follow(official)
    assert local.sync(peer1) is None
    assert [k for _seq, k in _waiting(local, official)] == ["artwork", "achievements", "custom", "achievements"]

    official.state.ctx.settings = official.state.ctx.settings.model_copy(update={"emit_protocol": "1.5"})
    with official.state.maker() as s:
        assert feed.introduce_batch(s, official.state.ctx, "missing")["introduced"] >= 1
        s.commit()
    peer2 = local2.follow(official)
    assert local2.sync(peer2) is None
    with local2.state.maker() as s:
        assert s.get(Canonical, s_id) is not None

    n1, n2 = Member(local), Member(local2)
    create(n1)
    finish_creation(n1)
    enrol(n1, n2, name="Second node")
    with n1.session() as s:
        assert len(groups.trust(s, groups.active(s), own_node=n1.node_id).members()) == 2
        own_feed = s.scalars(select(ChangeLog.seq)).all()

    out = assist.run(n1.st.maker, Ctx(n1.settings, n1.identity), n1.net)
    assert out["applied"] == 1, out
    assert _waiting(local, official) == []
    with n1.session() as s:
        row = s.get(Canonical, s_id)
        assert (row.scope_ceiling, row.scope_ceiling_by) == ("group", "assist")
        assert {i.kind for i in s.scalars(select(GroupAssistItem).where(GroupAssistItem.cid == s_id))} >= {
            "bundle", "entry"}
        assert s.scalars(select(ChangeLog.seq)).all() == own_feed  # nothing about S signed by N1

    assert local.sync(peer1) is None  # N1's own pull: U's `record` now introduces S here
    with n1.session() as s:
        row = s.get(Canonical, s_id)
        assert row.scope_ceiling is None and row.view.get("title") == "Subnautica 2"
    for m in (n1, n2):
        m.net.close()
