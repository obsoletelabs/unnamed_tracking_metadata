"""C14-4 "super group": the owner group presents to its users as one node (owner request 2026-11-10 and refinement).

Three real Atlas apps (own SQLite files and synthetic secrets) form one owner group with the super group on. Writes
made on one member reach the others through the sealed `group.writes` op during the normal group round
(`main.group_round`); concurrent writes settle the same way everywhere with one admin-visible conflict entry; user
pages carry no node identity; an offline member changes nothing users see but freshness; D1, D2 and D7 hold.
Every key, password and title is a synthetic test value."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from atlas import group_crypto as gc
from atlas import groups, main, proposals, scope, social, super_group
from atlas.api import group_routes
from atlas.models import (
    Canonical,
    ChangeLog,
    GroupWrite,
    GroupWriteConflict,
    GroupWriteState,
    ListItem,
    Observation,
    UpstreamContribution,
    User,
    UserList,
)
from atlas.service import Ctx

from .channel_harness import PASSWORD, link, user
from .group_harness import Dispatcher, create, enrol, fast_kdf, finish_creation, make_node
from .test_web import login

NAMES = {"na": "Attic box", "nb": "Basement box", "nc": "Cloud box"}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    fast_kdf(monkeypatch)


def configure(node, **changes) -> None:
    node.st.ctx.settings = node.st.ctx.settings.model_copy(update=changes)
    node.st.providers.settings = node.st.ctx.settings


@pytest.fixture
def trio(tmp_path):
    net = Dispatcher()
    nodes = [make_node(tmp_path, net, n, group_super=True) for n in ("na", "nb", "nc")]
    for n in nodes:  # one site to its users: the same name everywhere; members are told apart by their own names
        configure(n, app_name="Home Atlas")
    a, b, c = nodes
    create(a, member_name=NAMES["na"])
    finish_creation(a)
    enrol(a, b, name=NAMES["nb"])
    enrol(a, c, name=NAMES["nc"])
    rounds(a, b, c)
    yield a, b, c
    for n in nodes:
        n.close()


def rnd(node) -> dict:
    return main.group_round(node.st.maker, Ctx(node.settings, node.identity), node.net)


def rounds(*nodes, n: int = 2) -> None:
    for _ in range(n):
        for node in nodes:
            rnd(node)


def boss(node) -> str:
    with node.session() as s:
        found = s.scalars(select(User.id).where(User.username == "boss")).first()
    if found:
        return found
    uid = user(node, "boss")
    with node.session() as s:
        s.get(User, uid).role = "admin"
        s.commit()
    return uid


def new_game(node, user_id: str, title: str, **facts) -> str:
    with node.session() as s:
        p = proposals.create(s, Ctx(node.settings, node.identity), s.get(User, user_id), "new_game", None,
                             {"title": title, "media_type": "game", **facts}, "")
        assert p.auto_applied
        s.commit()
        return p.canonical_id


def edit(node, user_id: str, cid: str, **fields) -> None:
    with node.session() as s:
        p = proposals.create(s, Ctx(node.settings, node.identity), s.get(User, user_id), "edit", cid, fields, "")
        assert p.auto_applied
        s.commit()


def view(node, cid: str) -> dict:
    with node.session() as s:
        row = s.get(Canonical, cid)
        return dict(row.view) if row is not None else {}


def states(node) -> dict:
    with node.session() as s:
        return {r.subject: (r.op_id, r.hlc, r.deleted, r.value) for r in s.scalars(select(GroupWriteState))}


def conflict_ids(node) -> set[str]:
    with node.session() as s:
        return set(s.scalars(select(GroupWriteConflict.id)))


# ---- write once, synced to all ----------------------------------------------------------------------------------

def test_add_on_one_member_appears_on_the_others(trio):
    a, b, c = trio
    ua = boss(a)
    cid = new_game(a, ua, "Starfall Harbor", developer="Old Lantern", release_date="2021-05-04")
    with b.session() as s:
        b_feed = s.scalar(select(func.count()).select_from(ChangeLog))
    rounds(b, c)
    for node in (b, c):
        v = view(node, cid)
        assert v.get("title") == "Starfall Harbor" and v.get("developer") == "Old Lantern", node.name
        with node.session() as s:
            row = s.get(Canonical, cid)
            assert (row.scope_ceiling, row.scope_ceiling_by) == ("group", "write")  # same id, group data only
            [obs] = s.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                        Observation.is_current.is_(True))).all()
            assert (obs.source, obs.source_scope, obs.redistributable) == ("group-write", "group:write", False)
    with b.session() as s:  # nothing about it signed into a member's public feed (H59, C14 §2.2)
        assert s.scalar(select(func.count()).select_from(ChangeLog)) == b_feed
    assert states(a) == states(b) == states(c)
    assert view(a, cid)["title"] == view(b, cid)["title"] == view(c, cid)["title"]


def test_lists_follow_the_linked_account_and_deletes_tombstone(trio):
    a, b, c = trio
    ua = boss(a)
    cid = new_game(a, ua, "Paper Comets")
    rounds(b, c)
    kim = {n.name: user(n, "kim") for n in (a, b, c)}
    uid = link(a, kim["na"], b, kim["nb"])
    link(a, kim["na"], c, kim["nc"])
    ctx = Ctx(a.settings, a.identity)
    with a.session() as s:
        me = s.get(User, kim["na"])
        lst = social.create_list(s, ctx, me, "Cosy evenings", "Short and calm", public=False)
        assert super_group.list_written(s, ctx, me, lst)
        social.add_to_list(s, ctx, me, lst.id, cid, "with tea")
        assert super_group.list_item_written(s, ctx, me, lst, cid, "with tea")
        lid = lst.id
        s.commit()
    rounds(b, c)
    for node in (b, c):
        with node.session() as s:
            row = s.get(UserList, lid)
            assert row is not None and row.user_id == kim[node.name] and row.name == "Cosy evenings"
            assert s.get(ListItem, (lid, cid)).note == "with tea"
    with b.session() as s:  # a removal on another member is a tombstone that wins the same way
        me = s.get(User, kim["nb"])
        lst = s.get(UserList, lid)
        social.remove_from_list(s, me, lid, cid)
        assert super_group.list_item_written(s, Ctx(b.settings, b.identity), me, lst, cid, deleted=True)
        s.commit()
    rounds(a, c)
    for node in (a, b, c):
        with node.session() as s:
            assert s.get(ListItem, (lid, cid)) is None, node.name
    assert uid and states(a) == states(b) == states(c)


def test_list_write_waits_for_an_unlinked_account(trio):
    a, b, _c = trio
    ua = boss(a)
    cid = new_game(a, ua, "Tin Kite")
    kim_a = user(a, "kim")
    kim_b = user(b, "kim")
    from .channel_harness import mint

    mint(a, kim_a)
    ctx = Ctx(a.settings, a.identity)
    with a.session() as s:
        me = s.get(User, kim_a)
        lst = social.create_list(s, ctx, me, "Later", "", public=False)
        assert super_group.list_written(s, ctx, me, lst)
        social.add_to_list(s, ctx, me, lst.id, cid)
        super_group.list_item_written(s, ctx, me, lst, cid)
        lid = lst.id
        s.commit()
    rnd(b)
    with b.session() as s:
        assert s.get(UserList, lid) is None
        assert {r.waiting for r in s.scalars(select(GroupWriteState).where(
            GroupWriteState.subject.like("list:%")))} == {"no_account"}
    link(a, kim_a, b, kim_b)
    rnd(b)
    with b.session() as s:
        assert s.get(UserList, lid).user_id == kim_b and s.get(ListItem, (lid, cid)) is not None


# ---- deterministic conflicts ------------------------------------------------------------------------------------

def test_concurrent_edits_resolve_identically_with_one_admin_entry(trio):
    a, b, c = trio
    cid = new_game(a, boss(a), "Starfall Harbor", developer="Old Lantern")
    rounds(b, c)
    ub, uc = boss(b), boss(c)
    edit(b, ub, cid, developer="Lantern Works")  # neither has heard of the other's edit
    edit(c, uc, cid, developer="Tin Kite Studio")
    rounds(a, b, c)
    devs = {n.name: view(n, cid).get("developer") for n in (a, b, c)}
    assert len(set(devs.values())) == 1, devs
    subject = f"rec:{cid}/developer"
    assert states(a)[subject] == states(b)[subject] == states(c)[subject]
    ids = conflict_ids(a)
    assert len(ids) == 1 and ids == conflict_ids(b) == conflict_ids(c)
    with a.session() as s:
        conflict = s.get(GroupWriteConflict, next(iter(ids)))
        assert {conflict.winner_node, conflict.loser_node} == {b.node_id, c.node_id}
        assert devs["na"] == ("Lantern Works" if conflict.winner_node == b.node_id else "Tin Kite Studio")
    # admins see it (with the member names); the record page for a person shows nothing of it
    admin = TestClient(b.app, base_url=b.settings.public_url, follow_redirects=False)
    assert login(admin, "boss", PASSWORD).status_code == 303
    page = admin.get("/admin/group").text
    assert f'data-sg-conflict="{next(iter(ids))}"' in page and NAMES["nc"] in page
    assert "data-group-bar" in admin.get(f"/game/{cid}").text
    r = admin.post(f"/admin/group/conflicts/{next(iter(ids))}/seen", data={"csrf": _csrf(admin, "/admin/group")})
    assert r.status_code == 303
    with b.session() as s:
        assert s.get(GroupWriteConflict, next(iter(ids))).seen_at is not None


def test_sequential_edits_are_not_conflicts(trio):
    a, b, c = trio
    cid = new_game(a, boss(a), "Quiet Orchard", developer="One")
    rounds(b, c)
    edit(b, boss(b), cid, developer="Two")
    rounds(a, c)
    edit(c, boss(c), cid, developer="Three")
    rounds(a, b, c)
    assert {view(n, cid)["developer"] for n in (a, b, c)} == {"Three"}
    assert conflict_ids(a) == conflict_ids(b) == conflict_ids(c) == set()


def test_order_of_arrival_never_changes_the_result(trio):
    """Every subject's state is a function of the set of writes, not of the order they arrived in."""
    a, b, c = trio
    cid = new_game(a, boss(a), "Glass Lighthouse")
    edit(a, boss(a), cid, publisher="North")  # b hears A first, c hears B first
    rnd(b)
    edit(b, boss(b), cid, publisher="South")
    rnd(c)
    rounds(a, b, c)
    assert states(a) == states(b) == states(c)


def test_revert_is_replicated(trio):
    a, b, _c = trio
    ua = boss(a)
    cid = new_game(a, ua, "Hollow Pines", developer="First")
    rnd(b)
    with a.session() as s:
        p = proposals.create(s, Ctx(a.settings, a.identity), s.get(User, ua), "edit", cid, {"developer": "Second"}, "")
        pid = p.id
        s.commit()
    rnd(b)
    assert view(b, cid)["developer"] == "Second"
    with a.session() as s:
        proposals.revert(s, Ctx(a.settings, a.identity), s.get(User, ua), pid)
        s.commit()
    assert view(a, cid)["developer"] == "First"
    rnd(b)
    assert view(b, cid)["developer"] == "First"


# ---- invisible topology -----------------------------------------------------------------------------------------

def identity_markers(*nodes) -> list[str]:
    out = []
    for n in nodes:
        out += [n.node_id, groups.gc_fp(n.node_id), NAMES[n.name]]
    return out + ["served by", "Served by", "also on", "Also on", "available on"]


def _csrf(client, path: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_user_pages_show_no_node_identity(trio):
    a, b, c = trio
    cid = new_game(a, boss(a), "Starfall Harbor", developer="Old Lantern", description="A calm harbour game.")
    rounds(b, c)
    for node in (b, c):
        boss(node)  # the first account on a node is its administrator; kim is an ordinary person
        kim = user(node, "kim")
        with node.session() as s:
            assert s.get(User, kim).role == "user"
            scope.grant(s, s.get(User, kim), scope.DETAIL, None)
            me = s.get(User, kim)
            lst = social.create_list(s, Ctx(node.settings, node.identity), me, "Mine", "", public=True)
            lid = lst.id
            s.commit()
        client = TestClient(node.app, base_url=node.settings.public_url, follow_redirects=False)
        anon = TestClient(node.app, base_url=node.settings.public_url, follow_redirects=False)
        assert login(client, "kim", PASSWORD).status_code == 303
        pages = ["/", "/search?q=Starfall", f"/game/{cid}", "/me", "/me/lists", f"/list/{lid}", "/me/settings",
                 "/notifications", f"/game/{cid}/edit"]
        game = client.get(f"/game/{cid}").text
        assert "Starfall Harbor" in game and "Old Lantern" in game
        for path in pages:
            for who, cl in (("user", client), ("anonymous", anon)):
                r = cl.get(path)
                if r.status_code >= 300:
                    continue
                for marker in identity_markers(a, b, c):
                    assert marker not in r.text, f"{node.name} {who} {path} shows {marker!r}"


def test_d1_visitors_see_only_the_partial_projection(trio):
    a, b, _c = trio
    cid = new_game(a, boss(a), "Starfall Harbor", developer="Old Lantern", description="Long store text.")
    rnd(b)
    anon = TestClient(b.app, base_url=b.settings.public_url, follow_redirects=False)
    assert anon.get(f"/game/{cid}").status_code == 404  # group data is not public by default (H64, D1)
    configure(b, group_data_public_pages=True)
    with b.session() as s:
        from atlas import service

        scope.reproject(s, Ctx(b.settings, b.identity), cid)
        service.rebuild(s, Ctx(b.settings, b.identity), cid)
        s.commit()
    page = anon.get(f"/game/{cid}").text
    assert "Starfall Harbor" in page and "Old Lantern" in page  # public tier: title and credits
    assert "Long store text." not in page  # descriptions stay for signed-in viewers (D1 default)


# ---- offline, D7, D2, tolerant readers --------------------------------------------------------------------------

def test_offline_member_changes_only_freshness(trio):
    a, b, c = trio
    cid = new_game(a, boss(a), "Starfall Harbor", developer="Old Lantern")
    rounds(b, c)
    before = {n.name: view(n, cid) for n in (a, b, c)}
    c.net.down.add("nc.test")
    edit(a, boss(a), cid, publisher="Northwind Toys")
    rounds(a, b)  # A's write reaches B while C is offline; nothing fails for anyone
    assert view(b, cid)["publisher"] == "Northwind Toys"
    assert view(a, cid)["title"] == before["na"]["title"] and view(b, cid)["title"] == before["nb"]["title"]
    with a.session() as s:  # A's own write is still on its way to C: the generic "syncing" state only
        ua = s.scalars(select(User).where(User.username == "boss")).one().id
        assert super_group.user_status(s, a.settings, ua)["syncing"] is True
    c.net.down.discard("nc.test")
    edit(b, boss(b), cid, series="Harbor Tales")  # written on B: C gets it from B, A's from either
    a.net.down.add("na.test")  # A goes offline before C pulls: B relays A's write
    rnd(c)
    a.net.down.discard("na.test")
    assert view(c, cid)["publisher"] == "Northwind Toys" and view(c, cid)["series"] == "Harbor Tales"
    rounds(a, b, c)
    assert states(a) == states(b) == states(c)
    with a.session() as s:
        assert super_group.user_status(s, a.settings, ua)["syncing"] is False


def test_writes_of_an_expired_member_are_held_until_renewal(trio, monkeypatch):
    """D7: a write dated while its member's certificate had lapsed is not dropped; it waits for a renewal."""
    a, b, _c = trio
    real = super_group.standing
    lapsed = {"on": True}
    monkeypatch.setattr(super_group, "standing", lambda tr, origin, hlc: "origin_expired"
                        if lapsed["on"] and origin == a.node_id else real(tr, origin, hlc))
    cid = new_game(a, boss(a), "Starfall Harbor")
    with a.session() as s:  # the origin judges its own write by the same rule: held there too
        assert {w.status for w in s.scalars(select(GroupWrite))} == {"held"}
    rnd(b)
    with b.session() as s:
        assert s.get(Canonical, cid) is None
        assert {w.reason for w in s.scalars(select(GroupWrite).where(GroupWrite.status == "held"))} == {
            "origin_expired"}
    lapsed["on"] = False  # renewed
    rounds(a, b)
    assert view(b, cid)["title"] == "Starfall Harbor"
    assert states(a) == states(b)


def test_replicated_writes_never_go_upstream_or_touch_links(trio):
    """D2: links stay per node (a relink restarts probation there); a replicated write is never a contribution."""
    a, b, c = trio
    cid = new_game(a, boss(a), "Starfall Harbor")
    rounds(b, c)
    for node in (b, c):
        with node.session() as s:
            assert s.scalar(select(func.count()).select_from(UpstreamContribution)) == 0
    import inspect

    src = inspect.getsource(super_group)
    assert "link_client" not in src and "UpstreamLink" not in src and cid


def test_unknown_kind_is_kept_and_relayed_not_applied(trio):
    """Tolerant reader: a write of a later release is stored, served onward and never applied or refused."""
    a, b, c = trio
    with a.session() as s:
        group = groups.active(s)
        seq = super_group._own_seq(s, a.node_id)
        body = {"v": 2, "op_id": f"{a.node_id}:{seq}", "origin": a.node_id, "seq": seq, "kind": "comment.body",
                "subject": "comment:01JTEST", "hlc": super_group.tick(s, a.node_id), "future": {"x": 1}}
        obj = gc.make_object("group.write", group.group_id, body["op_id"], body,
                             {"kind": "node", "id": a.node_id, "key_id": a.identity.key_id}, a.identity.private)
        s.add(GroupWrite(op_id=body["op_id"], origin_node=a.node_id, seq=seq, kind="comment.body",
                         subject=body["subject"], hlc=body["hlc"], status="held", reason="unknown_kind",
                         envelope=obj))
        s.commit()
    a.net.down.add("na.test")
    rnd(b)  # B is down? no: A is down, so B cannot pull it yet
    a.net.down.discard("na.test")
    rnd(b)
    a.net.down.add("na.test")
    rnd(c)  # C gets it from B
    a.net.down.discard("na.test")
    for node in (b, c):
        with node.session() as s:
            row = s.scalars(select(GroupWrite).where(GroupWrite.kind == "comment.body")).one()
            assert (row.status, row.reason) == ("held", "unknown_kind")


def test_forged_or_relabelled_writes_are_refused(trio):
    a, b, _c = trio
    cid = new_game(a, boss(a), "Starfall Harbor")
    with a.session() as s:
        obj = s.scalars(select(GroupWrite).where(GroupWrite.subject == f"rec:{cid}/title")).one().envelope
    ctx = Ctx(b.settings, b.identity)
    with b.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=b.node_id)
        tampered = {**obj, "body": {**obj["body"], "value": "Something else"}}
        assert super_group.receive(s, ctx, group, tr, tampered) == "invalid"
        relabelled = {**obj, "signer": {**obj["signer"], "id": b.node_id}}
        assert super_group.receive(s, ctx, group, tr, relabelled) == "invalid"
        assert super_group.receive(s, ctx, group, tr, obj) == "applied"
        assert super_group.receive(s, ctx, group, tr, obj) == "known"  # idempotent by op id
        s.rollback()


def test_policy_off_writes_nothing_and_serves_nothing(trio):
    a, b, _c = trio
    configure(a, group_super=False)
    cid = new_game(a, boss(a), "Starfall Harbor")
    with a.session() as s:
        assert s.scalar(select(func.count()).select_from(GroupWrite)) == 0
    rnd(b)
    assert view(b, cid) == {}
    with b.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=b.node_id)
        with pytest.raises(group_routes.CallError, match="forbidden"):
            group_routes.call(s, b.settings, b.identity, group, tr.members()[a.node_id]["body"]["member"],
                              "group.writes", {"since": 0}, b.net)


# ---- review findings (C14-4 static review) ----------------------------------------------------------------------

class _Rev:
    def __init__(self, reason, at):
        self.reason, self.revoked_at = reason, at


class _FakeTrust:
    """Only what `standing` reads: one node's certificates, revocations and ban."""

    def __init__(self, certs, revoked=None, banned=None):
        self.certs = {c["body"]["serial"]: c for c in certs}
        self.revoked, self.banned = revoked or {}, banned or {}

    def node_certs(self, node):
        return [c for c in self.certs.values() if c["body"]["member"]["node_id"] == node]

    def genuine(self, c):
        return True


def _cert(serial, node, nb, na):
    return {"body": {"serial": serial, "member": {"node_id": node}, "not_before": gc.stamp(nb),
                     "not_after": gc.stamp(na)}}


def test_acceptance_is_decided_by_the_write_not_by_when_it_arrived():
    """Review (high): a removed member's write is judged by its own signed clock against the signed removal, so a
    member that receives it before the revocation and one that receives it after decide the same way."""
    from datetime import UTC, datetime, timedelta

    node = "ab" * 8
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    removed = t0 + timedelta(days=10)
    tr = _FakeTrust([_cert("s1", node, t0, t0 + timedelta(days=90))], revoked={"s1": _Rev("removed", removed)})

    def hlc(at):
        return super_group._hlc(int(at.timestamp() * 1000), 0, node)

    assert super_group.standing(tr, node, hlc(removed - timedelta(minutes=1))) is None
    assert super_group.standing(tr, node, hlc(removed + timedelta(minutes=1))) == "origin_removed"
    # a rotation (`superseded`) is not a removal
    tr2 = _FakeTrust([_cert("s1", node, t0, t0 + timedelta(days=90))], revoked={"s1": _Rev("superseded", removed)})
    assert super_group.standing(tr2, node, hlc(removed + timedelta(minutes=1))) is None
    # D7: written after its certificate lapsed: held, then accepted once a renewal exists
    lapsed = t0 + timedelta(days=95)
    tr3 = _FakeTrust([_cert("s1", node, t0, t0 + timedelta(days=90))])
    assert super_group.standing(tr3, node, hlc(lapsed)) == "origin_expired"
    tr4 = _FakeTrust([_cert("s1", node, t0, t0 + timedelta(days=90)),
                      _cert("s2", node, t0 + timedelta(days=100), t0 + timedelta(days=190))])
    assert super_group.standing(tr4, node, hlc(lapsed)) is None
    # a ban dated before the write wins
    tr5 = _FakeTrust([_cert("s1", node, t0, t0 + timedelta(days=90))], banned={node: _Rev("removed", removed)})
    assert super_group.standing(tr5, node, hlc(removed + timedelta(seconds=1))) == "origin_removed"


def test_late_revocation_withdraws_what_was_applied_before_it_arrived(trio, monkeypatch):
    """Review (high): B applies C's write before it hears of C's removal; A hears of it first and refuses the write.
    Once B knows, it withdraws the write, so A and B hold the same state (the earlier write of C stays on both)."""
    a, b, c = trio
    cid = new_game(a, boss(a), "Starfall Harbor", developer="Old Lantern")
    rounds(b, c)
    edit(c, boss(c), cid, developer="Before removal")
    rounds(a, b)
    edit(c, boss(c), cid, developer="After removal")
    with c.session() as s:
        late = s.scalars(select(GroupWrite).where(GroupWrite.subject == f"rec:{cid}/developer")
                         .order_by(GroupWrite.id.desc())).first()
        late_op, late_hlc = late.op_id, late.hlc
    rnd(b)  # B takes it: it has not heard of any removal
    assert view(b, cid)["developer"] == "After removal"
    real_standing, real_cutoffs = super_group.standing, super_group._removed_cutoffs
    monkeypatch.setattr(super_group, "standing", lambda tr, origin, hlc: "origin_removed"
                        if origin == c.node_id and hlc >= late_hlc else real_standing(tr, origin, hlc))
    monkeypatch.setattr(super_group, "_removed_cutoffs",
                        lambda tr: {**real_cutoffs(tr), c.node_id: [f"removed-at:{late_hlc}"]})
    c.net.down.add("nc.test")
    rnd(a)  # A pulls the write from B and refuses it on arrival
    rnd(b)  # B rechecks what it applied
    for node in (a, b):
        with node.session() as s:
            row = s.scalars(select(GroupWrite).where(GroupWrite.op_id == late_op)).first()
            assert row is None or row.status == "refused", node.name
        assert view(node, cid)["developer"] == "Before removal", node.name
    sa, sb = states(a), states(b)
    assert sa[f"rec:{cid}/developer"] == sb[f"rec:{cid}/developer"]
    assert conflict_ids(a) == conflict_ids(b)


def test_private_or_local_records_never_replicate(trio):
    """Review (high): a record under a `private` or `local` ceiling keeps its edits and list entries on this node."""
    a, b, _c = trio
    ua = boss(a)
    cid = new_game(a, ua, "Hidden Garden", developer="Quiet")
    rnd(b)
    with a.session() as s:
        s.get(Canonical, cid).scope_ceiling = "local"
        s.commit()
    with a.session() as s:
        before = s.scalar(select(func.max(GroupWrite.id)))
    edit(a, ua, cid, developer="Kept here")
    kim_a, kim_b = user(a, "kim"), user(b, "kim")
    link(a, kim_a, b, kim_b)
    ctx = Ctx(a.settings, a.identity)
    with a.session() as s:
        me = s.get(User, kim_a)
        lst = social.create_list(s, ctx, me, "Secret", "", public=False)
        social.add_to_list(s, ctx, me, lst.id, cid, "")
        assert super_group.list_item_written(s, ctx, me, lst, cid) is False
        assert super_group.record_written(s, ctx, cid, {"developer": "X"}, "game") == 0
        s.commit()
    with a.session() as s:
        subjects = set(s.scalars(select(GroupWrite.subject).where(GroupWrite.id > before)))
        assert not any(cid in subj for subj in subjects), subjects
    rnd(b)
    assert view(b, cid)["developer"] == "Quiet"
    with a.session() as s:  # the reviewer's label says where an edit goes
        info = scope.record_summary(s, a.settings, s.get(Canonical, cid))
        assert info["edit_goes"] == scope.EDIT_GOES["local"]


def test_edit_label_says_synced_not_this_node_only(trio):
    """The owner's original complaint: in a super group an edit is never labelled "this node only"."""
    a, b, _c = trio
    cid = new_game(a, boss(a), "Open Field")
    rnd(b)
    with b.session() as s:  # a group record on a member: its edits are written once and synced to all
        info = scope.record_summary(s, b.settings, s.get(Canonical, cid))
        assert info["edit_goes"] == scope.EDIT_GOES["synced"]
        assert "node" not in info["edit_goes"].lower()
    configure(b, group_super=False)
    with b.session() as s:
        info = scope.record_summary(s, b.st.ctx.settings, s.get(Canonical, cid))
        assert info["edit_goes"] == scope.EDIT_GOES["local"]


def test_duplicate_add_keeps_one_note_everywhere(trio):
    """Review (medium): adding a game that is already on a list keeps its note here, so it keeps it everywhere."""
    a, b, _c = trio
    cid = new_game(a, boss(a), "Paper Comets")
    rnd(b)
    kim_a, kim_b = user(a, "kim"), user(b, "kim")
    link(a, kim_a, b, kim_b)
    ctx = Ctx(a.settings, a.identity)
    with a.session() as s:
        me = s.get(User, kim_a)
        lst = social.create_list(s, ctx, me, "Notes", "", public=False)
        super_group.list_written(s, ctx, me, lst)
        social.add_to_list(s, ctx, me, lst.id, cid, "first")
        super_group.list_item_written(s, ctx, me, lst, cid, "first")
        social.add_to_list(s, ctx, me, lst.id, cid, "second")  # already there: the note stays "first" here
        super_group.list_item_written(s, ctx, me, lst, cid, "second")
        lid = lst.id
        s.commit()
    rounds(b, a)
    notes = set()
    for node in (a, b):
        with node.session() as s:
            notes.add(s.get(ListItem, (lid, cid)).note)
    assert notes == {"first"}


def test_lists_made_before_the_link_are_backfilled(trio):
    """Review (medium): lists, favourites and entries an account had before it was linked reach every member."""
    a, b, _c = trio
    cid = new_game(a, boss(a), "Old Favourite")
    rnd(b)
    kim_a, kim_b = user(a, "kim"), user(b, "kim")
    ctx_a, ctx_b = Ctx(a.settings, a.identity), Ctx(b.settings, b.identity)
    with a.session() as s:  # made while kim was not linked: nothing is written then
        me = s.get(User, kim_a)
        lst = social.create_list(s, ctx_a, me, "From before", "kept", public=False)
        social.add_to_list(s, ctx_a, me, lst.id, cid, "old note")
        fav_a = social.ensure_default_lists(s, ctx_a, me)[0]
        social.add_to_list(s, ctx_a, me, fav_a.id, cid, "")
        lid = lst.id
        s.commit()
    with b.session() as s:
        social.ensure_default_lists(s, ctx_b, s.get(User, kim_b))
        s.commit()
    link(a, kim_a, b, kim_b)
    rounds(a, b, a, b)
    with b.session() as s:
        row = s.get(UserList, lid)
        assert row is not None and row.user_id == kim_b and row.name == "From before"
        assert s.get(ListItem, (lid, cid)).note == "old note"
        favs = s.scalars(select(UserList).where(UserList.user_id == kim_b, UserList.kind == "favourites")).all()
        assert len(favs) == 1 and s.get(ListItem, (favs[0].id, cid)) is not None  # one favourites list, merged
    assert states(a) == states(b)


def test_same_list_name_on_two_members_resolves_the_same_way(trio):
    """Review (low): two lists of one account with one name (made on two members before the link) get the same
    names on every member, whichever arrived first."""
    a, b, _c = trio
    kim_a, kim_b = user(a, "kim"), user(b, "kim")
    ids = {}
    for node, kim in ((a, kim_a), (b, kim_b)):
        with node.session() as s:
            ids[node.name] = social.create_list(s, Ctx(node.settings, node.identity), s.get(User, kim), "Backlog",
                                                "", public=False).id
            s.commit()
    link(a, kim_a, b, kim_b)
    rounds(a, b, a, b)
    names = []
    for node in (a, b):
        with node.session() as s:
            names.append({lid: s.get(UserList, lid).name for lid in ids.values()})
    assert names[0] == names[1], names
    assert sorted(names[0].values())[0] == "Backlog"


def test_replicated_lists_follow_the_local_list_rules(trio):
    """Review (medium): lists turned off or a full list on a member: the write waits there, it is not forced in."""
    a, b, _c = trio
    kim_a, kim_b = user(a, "kim"), user(b, "kim")
    link(a, kim_a, b, kim_b)
    configure(b, lists_enabled=False)
    ctx = Ctx(a.settings, a.identity)
    with a.session() as s:
        me = s.get(User, kim_a)
        lst = social.create_list(s, ctx, me, "Waits", "", public=False)
        super_group.list_written(s, ctx, me, lst)
        lid = lst.id
        s.commit()
    rnd(b)
    with b.session() as s:
        assert s.get(UserList, lid) is None
        assert s.get(GroupWriteState, f"list:{lid}").waiting == "lists_off"
    configure(b, lists_enabled=True, max_lists_per_user=1)
    with b.session() as s:
        social.create_list(s, Ctx(b.settings, b.identity), s.get(User, kim_b), "Full", "", public=False)
        s.commit()
    rnd(b)
    with b.session() as s:
        assert s.get(UserList, lid) is None and s.get(GroupWriteState, f"list:{lid}").waiting == "limit"
    configure(b, max_lists_per_user=50)
    rnd(b)
    with b.session() as s:
        assert s.get(UserList, lid) is not None


def test_retry_never_starves_behind_permanent_waits(trio, monkeypatch):
    """Review (medium): states that wait for good do not keep the others from being retried."""
    a, _b, _c = trio
    monkeypatch.setattr(super_group, "RETRY_BATCH", 3)
    tried: list[str] = []
    monkeypatch.setattr(super_group, "materialize", lambda s, ctx, subject: tried.append(subject) or "no_account")
    with a.session() as s:
        for i in range(7):
            s.add(GroupWriteState(subject=f"list:{i:026d}", op_id=f"{'0' * 16}:{i + 1}",
                                  hlc=f"0000000000001.{i:05d}.{'0' * 16}", deleted=False, value=None,
                                  waiting="no_account"))
        s.commit()
        tr = groups.trust(s, groups.active(s), own_node=a.node_id)
        ctx = Ctx(a.settings, a.identity)
        for _ in range(3):
            super_group.retry(s, ctx, tr)
    assert set(tried) == {f"list:{i:026d}" for i in range(7)}


def test_serve_clamps_the_acknowledgement(trio):
    """Review (low): a member cannot claim to have pulled writes this node does not hold."""
    a, b, _c = trio
    new_game(a, boss(a), "Clamp")
    with a.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=a.node_id)
        head = s.scalar(select(func.max(GroupWrite.id)))
        super_group.serve(s, a.settings, a.identity, tr, b.node_id, {"since": head + 10_000})
        assert super_group._int(s, super_group.ACK + b.node_id) == head
