"""CONTRACT-C14 §4 (lookups on behalf, shared headroom, per-credential budget) and §7.7 (H67 shared contribution
state). Group {N1, N2}; N2 has a (synthetic) Steam key, N1 has none. The Steam adapter is the real class with its
network call replaced, so no request leaves the test."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from atlas import assist, assist_lookup, contrib_state, groups, provider_budget
from atlas import group_crypto as gc
from atlas.api import group_routes
from atlas.models import AssistLookupOp, GroupAssistItem, GroupContribState, NodeState, Observation
from atlas.providers.base import ProviderError, ProviderResult
from atlas.providers.registry import Fetcher
from atlas.providers.steam import Steam
from atlas.service import Ctx

from .channel_harness import group_of
from .group_harness import Dispatcher, fast_kdf, make_node
from .test_c13_follower import provider_only

STEAM_KEY = "synthetic-steam-key-0f9e8d7c6b5a"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    fast_kdf(monkeypatch)


def configure(node, **changes) -> None:
    node.st.ctx.settings = node.st.ctx.settings.model_copy(update=changes)
    node.st.providers.settings = node.st.ctx.settings


@pytest.fixture
def pair(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(self, client, external_id):
        calls.append(external_id)
        if external_id == "404":
            return None
        return ProviderResult(
            external_id=external_id, title=f"Game {external_id}", provider_ids={"steam": external_id},
            facts={"developer": "Synthetic Dev", "website": f"https://store.example.org/app/{external_id}?key=abc"},
            artwork=[{"kind": "key_art", "url": "https://cdn.example.org/k.png?token=zzz"},
                     {"kind": "banner", "url": "https://cdn.example.org/b.png", "secret": "x"}])

    monkeypatch.setattr(Steam, "fetch", fake_fetch)
    net = Dispatcher()
    n1 = make_node(tmp_path, net, "n1", group_assist_lookups="answer+ask")
    n2 = make_node(tmp_path, net, "n2", steam_enabled=True, steam_api_key=STEAM_KEY, group_assist_lookups="answer")
    group_of(n1, n2)
    poll(n1)
    yield n1, n2, calls
    for n in (n1, n2):
        n.close()


def poll(node):
    from atlas import group_channel

    group_channel._HELLO.clear()
    with node.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=node.node_id)
        assist.learn_ops(s, node.settings, node.identity, group, tr, node.net, group_routes.call)
        s.execute(delete(NodeState).where(NodeState.key.like("assist_status:%")))
        assist_lookup.poll_status(s, node.settings, node.identity, group, tr, node.net, group_routes.call)
        s.commit()


def lookup(node, external_id="10", cid=None):
    return assist_lookup.lookup_via_group(node.st.maker, Ctx(node.settings, node.identity), node.net, "steam",
                                          "fetch", external_id=external_id, cid=cid)


def test_lookup_on_behalf_keeps_keys_on_helper(pair):
    """T-L1 + T-L10: the answer carries no key, header or error text; keyed URLs and unknown fields are dropped."""
    n1, n2, calls = pair
    seen = []
    real = group_routes.call

    def spy(*args, **kw):
        out = real(*args, **kw)
        seen.append(json.dumps(out))
        return out

    got = assist_lookup.lookup_via_group(n1.st.maker, Ctx(n1.settings, n1.identity), n1.net, "steam", "fetch",
                                         external_id="10", call=spy)
    assert got["helper"] == n2.node_id and calls == ["10"]
    [result] = got["results"]
    assert result["title"] == "Game 10" and "website" not in result["facts"]
    assert [a["url"] for a in result["artwork"]] == ["https://cdn.example.org/b.png"]
    assert "secret" not in result["artwork"][0]
    wire = "".join(seen)
    assert STEAM_KEY not in wire and "token=zzz" not in wire and "key=abc" not in wire


def test_lookup_result_ingested_as_group_scope_not_on_helper(pair):
    """T-L3 + T-L6: with a record, the results become `steam` observations scoped `group:<helper>` on it and the
    attestation is kept; the helper's catalogue is unchanged."""
    n1, n2, _calls = pair
    cid = provider_only(n1, "Local copy", "77")
    got = lookup(n1, "10", cid)
    assert got["ingested"] == 1
    with n1.session() as s:
        obs = s.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                  Observation.source_scope == "group:" + n2.node_id)).all()
        assert [o.source for o in obs] == ["steam"]
        item = s.scalars(select(GroupAssistItem).where(GroupAssistItem.kind == "lookup")).one()
        assert item.envelope["body"]["request"]["external_id"] == "10"
    with n2.session() as s:
        assert not s.scalars(select(Observation)).first()


def test_lookup_attestation_verifies_and_is_stored(pair):
    """T-L6 (extended, C14-15): a result with a different query, swapped achievements or other results fails."""
    n1, n2, _calls = pair
    nonce = b"\x05" * 16
    req = assist_lookup.validate({"provider": "steam", "call": "fetch", "external_id": "10", "query": None,
                                  "id": None, "on_behalf": None, "op_id": "01JC0000000000000000000001"})
    with n1.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n1.node_id)
        member = tr.members()[n2.node_id]["body"]["member"]
        got = group_routes.call(s, n1.settings, n1.identity, group, member, "assist.lookup", req, n1.net, nonce=nonce)
        nb = gc.b64(nonce)
        assert assist_lookup.verify_attestation(s, n1.identity, tr, n2.node_id, nb, req, got)["kind"] == "lookup"
        for bad in ({**got, "achievements": [{"api_name": "x"}]}, {**got, "results": []}):
            with pytest.raises(assist.AssistError) as exc:
                assist_lookup.verify_attestation(s, n1.identity, tr, n2.node_id, nb, req, bad)
            assert exc.value.code == "hash_mismatch"
        with pytest.raises(assist.AssistError):
            assist_lookup.verify_attestation(s, n1.identity, tr, n2.node_id, nb, {**req, "external_id": "11"}, got)


def test_lookup_op_id_idempotent(pair):
    """T-L11: a retry of a completed op returns the stored result without a second provider call or take; the same
    op id with another request is refused."""
    n1, n2, calls = pair
    params = {"provider": "steam", "call": "fetch", "external_id": "10", "query": None, "id": None,
              "on_behalf": None, "op_id": "01JC0000000000000000000002"}
    with n1.session() as s:
        group = groups.active(s)
        member = groups.trust(s, group, own_node=n1.node_id).members()[n2.node_id]["body"]["member"]
        first = group_routes.call(s, n1.settings, n1.identity, group, member, "assist.lookup", params, n1.net)
        again = group_routes.call(s, n1.settings, n1.identity, group, member, "assist.lookup", params, n1.net)
        assert calls == ["10"] and first["results"] == again["results"]
        assert first["attestation"]["body"]["request_nonce"] != again["attestation"]["body"]["request_nonce"]
        with pytest.raises(group_routes.CallError) as exc:
            group_routes.call(s, n1.settings, n1.identity, group, member, "assist.lookup",
                              {**params, "external_id": "11"}, n1.net)
        assert str(exc.value) == "invalid_id"
    with n2.session() as s:
        assert s.scalars(select(AssistLookupOp)).one().state == "completed"


def test_lookup_respects_helper_reserve(pair):
    """T-L2: once only the reserve is left the helper answers `no_headroom` with `retry_after`; its own calls still
    succeed."""
    n1, n2, calls = pair
    configure(n2, group_assist_lookup_reserve=95)  # 40/min × 5 % = 2 assist lookups a minute
    lookup(n1, "1")
    lookup(n1, "2")
    with n1.session() as s:
        group = groups.active(s)
        member = groups.trust(s, group, own_node=n1.node_id).members()[n2.node_id]["body"]["member"]
        with pytest.raises(group_routes.CallError) as exc:
            group_routes.call(s, n1.settings, n1.identity, group, member, "assist.lookup",
                              {"provider": "steam", "call": "fetch", "external_id": "3", "query": None, "id": None,
                               "on_behalf": None, "op_id": "01JC0000000000000000000003"}, n1.net)
        assert str(exc.value) == "no_headroom" and exc.value.result["retry_after"] >= 1
    with n2.session() as s:
        assert n2.st.providers.lookup(s, "steam", external_id="4")[0].title == "Game 4"
    assert calls == ["1", "2", "4"]


def test_invalid_ids_never_reach_the_helper_provider(pair):
    """T-L7: an id that fails the adapter pattern is refused before the call, on both sides."""
    n1, n2, calls = pair
    with pytest.raises(assist.AssistError) as exc:
        lookup(n1, "../etc/passwd")
    assert exc.value.code == "invalid_id" and calls == []


def test_closed_provider_set_and_trusted_adapters(pair):
    """C14-4: a provider outside the shipped set, or a configured name whose adapter is not the built-in class,
    is `not_configured`."""
    n1, n2, calls = pair

    class Lookalike(Steam):
        pass

    n2.st.providers.providers["steam"] = Lookalike()
    with n1.session() as s:
        group = groups.active(s)
        member = groups.trust(s, group, own_node=n1.node_id).members()[n2.node_id]["body"]["member"]
        for provider in ("steam", "evil"):
            with pytest.raises(group_routes.CallError) as exc:
                group_routes.call(s, n1.settings, n1.identity, group, member, "assist.lookup",
                                  {"provider": provider, "call": "fetch", "external_id": "5", "query": None,
                                   "id": None, "on_behalf": None, "op_id": "01JC0000000000000000000004"}, n1.net)
            assert str(exc.value) == "not_configured"
    assert calls == []


def test_status_headroom_routes_to_member_with_capacity_and_never_to_a_shared_key(pair):
    """T-L4 + shared credentials: N1 configured with N2's key never routes to N2, and both budgets split."""
    n1, n2, _calls = pair
    with n1.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n1.node_id)
        assert assist_lookup.pick_helper(s, n1.settings, n1.identity, group, tr, "steam") == [n2.node_id]
    configure(n1, steam_enabled=True, steam_api_key=STEAM_KEY)
    poll(n1)
    with n1.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n1.node_id)
        assert assist_lookup.pick_helper(s, n1.settings, n1.identity, group, tr, "steam") == []
        assert provider_budget.share(s, "steam") == 2
        assert provider_budget.limits(s, n1.settings, Steam())[0][1] == Steam.requests_per_minute // 2


def test_provider_budget_per_credential_across_processes_and_days(tmp_path, monkeypatch):
    """T-L9: two Fetchers (the web process and the worker) share one database-counted budget per credential,
    minute and day windows."""
    net = Dispatcher()
    node = make_node(tmp_path, net, "solo", steam_enabled=True, steam_api_key=STEAM_KEY)
    monkeypatch.setattr(Steam, "requests_per_minute", 3)
    monkeypatch.setattr(Steam, "requests_per_day", 4, raising=False)
    monkeypatch.setattr(Steam, "fetch", lambda self, client, eid: ProviderResult(external_id=eid, title=eid))
    web, worker = (Fetcher(node.settings, {"steam": Steam()}) for _ in range(2))
    with node.session() as s:
        web.lookup(s, "steam", external_id="1")
        worker.lookup(s, "steam", external_id="2")
        web.lookup(s, "steam", external_id="3")
        with pytest.raises(ProviderError, match="budget"):
            worker.lookup(s, "steam", external_id="4")
        s.commit()
        key, _limit, _size = provider_budget.limits(s, node.settings, Steam())[0]
        assert key.startswith("prov-budget:steam:") and STEAM_KEY not in key
    node.close()


# ---- §7.7 H67 ----------------------------------------------------------------------------------------------------------

def test_contribution_state_shared_in_group(pair):
    """T-K22: a state recorded on N1 shows on N2; a newer state replaces it; no payload or account data travels;
    the signer must be the served member; policy off on N1 sends a tombstone and N2 drops the item."""
    n1, n2, _calls = pair
    with n1.session() as s:
        assert contrib_state.record(s, n1.settings, n1.identity, main="a" * 16, subject="abcdef0123",
                                    contribution_id="01JCCONTRIB0000000000000001", kind="edit", state="pending",
                                    local_cid="atlas:" + "1" * 26, fields=["release_date"])
        s.commit()

    def pull(node):
        with node.session() as s:
            group = groups.active(s)
            tr = groups.trust(s, group, own_node=node.node_id)
            assist.learn_ops(s, node.settings, node.identity, group, tr, node.net, group_routes.call)
            out = contrib_state.pull(s, node.settings, node.identity, group, tr, node.net, group_routes.call)
            s.commit()
            return out

    assert pull(n2)["stored"] == 1
    with n2.session() as s:
        [row] = contrib_state.received(s, n2.node_id)
        assert row["state"] == "pending" and row["node"] == n1.node_id and row["fields"] == ["release_date"]
        stored = s.scalars(select(GroupContribState)).one().envelope
        assert set(stored["body"]) == {"main", "link_subject_hint", "contribution_id", "kind", "local_cid",
                                       "main_cid", "fields", "state", "decided_at", "feed_seq", "updated_at"}
        assert stored["body"]["link_subject_hint"] == "abcd"
        # an item served by another member than its signer is refused
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n2.node_id)
        assert not contrib_state.accept(s, group, tr, n2.node_id, stored)
    with n1.session() as s:
        contrib_state.record(s, n1.settings, n1.identity, main="a" * 16, subject="abcd",
                             contribution_id="01JCCONTRIB0000000000000001", kind="edit", state="approved",
                             local_cid="atlas:" + "1" * 26, fields=["release_date"],
                             decided_at=datetime(2026, 11, 9, tzinfo=UTC))
        s.commit()
    pull(n2)
    with n2.session() as s:
        [row] = contrib_state.received(s, n2.node_id)
        assert row["state"] == "approved" and row["decided_at"].day == 9
    configure(n1, group_share_contribution_state=False)
    pull(n1)  # the round tombstones N1's own items
    pull(n2)
    with n2.session() as s:
        assert contrib_state.received(s, n2.node_id) == []


def test_contribution_state_policy_off_on_the_receiver(pair):
    n1, n2, _calls = pair
    configure(n2, group_share_contribution_state=False)
    with n1.session() as s:
        contrib_state.record(s, n1.settings, n1.identity, main="a" * 16, subject="abcd",
                             contribution_id="01JCCONTRIB0000000000000002", kind="edit", state="queued")
        s.commit()
    with n2.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=n2.node_id)
        assert contrib_state.pull(s, n2.settings, n2.identity, group, tr, n2.net, group_routes.call)["stored"] == 0


def test_no_transitive_assist(pair, monkeypatch):
    """T-L8: a member without the provider answers `not_configured` and never asks a third member for it."""
    n1, n2, calls = pair
    configure(n1, group_assist_lookups="answer+ask", steam_enabled=False)
    n1.st.providers.providers.pop("steam", None)  # N1 has no Steam; N2 (which has it) asks N1 anyway
    seen = []
    real = group_routes.call

    def counting(*args, **kw):
        seen.append(args[5] if len(args) > 5 else kw.get("op"))
        return real(*args, **kw)

    monkeypatch.setattr(group_routes, "call", counting)
    with n2.session() as s:
        group = groups.active(s)
        member = groups.trust(s, group, own_node=n2.node_id).members()[n1.node_id]["body"]["member"]
        with pytest.raises(group_routes.CallError) as exc:
            group_routes.call(s, n2.settings, n2.identity, group, member, "assist.lookup",
                              {"provider": "steam", "call": "fetch", "external_id": "6", "query": None, "id": None,
                               "on_behalf": None, "op_id": "01JC0000000000000000000006"}, n2.net)
    assert str(exc.value) == "not_configured"
    assert seen == ["assist.lookup"] and calls == []  # the one call from N2; N1 forwarded nothing
