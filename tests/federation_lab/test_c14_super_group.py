"""C14-4 "super group" on real servers (opt-in lab): the owner's group {A, B, C} presents to its users as one node.

* A = `local`, B = `local2`, C = `indie` (an authority, so the test also shows that grouping publishes nothing).
  Every server runs with `ATLAS_GROUP_SUPER=true`; the group is formed by real enrolment over HTTP.
* Add on A: a game created through A's web form appears on B and C under the same id after their group rounds.
* Conflict: B and C edit the same field through their web forms before hearing of each other. After the rounds
  all three show the same value, hold the same subject state and the same single conflict entry, which the admin
  group page lists; no user page mentions it.
* User pages show no node identity: the pages a signed-in person and a visitor open on every node carry no node
  id, node name, key fingerprint, "served by" or "also on".
* Offline member: with C stopped, an edit on A reaches B and nothing users see on A or B changes except the
  freshness and the generic "syncing" line; when C is back it converges, B relaying what A wrote.

The group is formed with full rounds (`main.group_round`); after that the test drives the super-group step of the
round (`super_group.pull`, its own transaction, as the worker runs it) in-process on each node's own database, while
each server's own group worker may also run rounds when a sibling's `notify` wakes it, exactly as in production.
Every call between members is a real sealed HTTP call between the servers. All titles, names and passwords are
synthetic test values."""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from atlas import group_crypto as gc
from atlas import groups, main, scope, super_group
from atlas.api import group_routes
from atlas.models import Canonical, ChangeLog, GroupWriteConflict, GroupWriteState, User
from atlas.service import Ctx
from tests.group_harness import create, enrol, finish_creation
from tools.federation_lab import Lab
from tools.multinode_drill import csrf

from .test_c14_group_as_one_node import Member

pytestmark = pytest.mark.federation_lab

NAMES = {"local": "Attic box", "local2": "Basement box", "indie": "Cloud box"}
KEEP_LOGS = Path(os.environ["ATLAS_LAB_KEEP_LOGS"]) if os.environ.get("ATLAS_LAB_KEEP_LOGS") else None
ENV = {"ATLAS_GROUP_SUPER": "true", "ATLAS_GROUP_SYNC_INTERVAL": "3600"}


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setattr(gc, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(gc, "SCRYPT_MIN_N", 2 ** 10)
    with Lab(postgres_admin_url=os.environ.get("ATLAS_LAB_PG_URL") or None,
             env={n: dict(ENV) for n in NAMES}, keep_logs=KEEP_LOGS) as lab:
        lab.start(*NAMES)
        for name in NAMES:  # the in-process side of each node (its group worker) runs with the same policy
            st = lab.nodes[name].state
            st.ctx.settings = st.ctx.settings.model_copy(update={"group_super": True})
        yield lab


def full_round(m: Member) -> None:
    main.group_round(m.st.maker, Ctx(m.settings, m.identity), m.net)


def rnd(m: Member) -> None:
    """The super-group step of a round. SQLite allows one writer: when the server's own worker (woken by a sibling's
    `notify`) holds the write lock, this waits and tries again, as the next round would."""
    from sqlalchemy.exc import OperationalError

    for attempt in range(8):
        with m.st.maker() as s:
            try:
                group = groups.active(s)
                tr = groups.trust(s, group, own_node=m.node_id)
                super_group.pull(s, Ctx(m.settings, m.identity), group, tr, m.net, group_routes.call)
                s.commit()
                return
            except OperationalError as exc:
                s.rollback()
                if "locked" not in str(exc) or attempt == 7:
                    raise
        time.sleep(0.5 + attempt * 0.5)


def rounds(*ms: Member, n: int = 2) -> None:
    for _ in range(n):
        for m in ms:
            rnd(m)


def view(node, cid: str) -> dict:
    with node.state.maker() as s:
        row = s.get(Canonical, cid)
        return dict(row.view) if row is not None else {}


def states(node) -> dict:
    with node.state.maker() as s:
        return {r.subject: (r.op_id, r.hlc, r.deleted) for r in s.scalars(select(GroupWriteState))}


def conflicts(node) -> set[str]:
    with node.state.maker() as s:
        return set(s.scalars(select(GroupWriteConflict.id)))


def post_form(node, path: str, data: dict) -> httpx.Response:
    """A web form post. On SQLite a member's group worker can hold the single write lock past the 5 s busy
    timeout (lab servers share one machine); the person would simply submit again, and so does the test."""
    for attempt in range(4):
        r = node.admin.post(path, data={**data, "csrf": csrf(node.admin, path)})
        if r.status_code != 500 or attempt == 3:
            break
        time.sleep(1.0)
    assert r.status_code == 303, r.text[:400]
    return r


def edit_form(node, cid: str, **fields) -> None:
    page = node.admin.get(f"/game/{cid}/edit").text
    data = {"title": view(node, cid).get("title", "")}
    for key in ("developer", "publisher", "series"):
        m = re.search(rf'name="{key}"[^>]*value="([^"]*)"', page)
        data[key] = m.group(1) if m else ""
    data.update(fields)
    post_form(node, f"/game/{cid}/edit", data)


def markers(network) -> list[str]:
    out = ["served by", "Served by", "also on", "Also on", "available on"]
    for name, node in network.nodes.items():
        out += [node.node_id, groups.gc_fp(node.node_id), NAMES[name]]
    return out


def person(node) -> httpx.Client:
    """An ordinary account of the owner's household (holds `scope.detail`, as admin-created accounts do)."""
    with node.state.maker() as s:
        u = s.scalars(select(User).where(User.username == "drill-user")).one()
        scope.grant(s, u, scope.DETAIL, None)
        s.commit()
    return node.signin("drill-user")


def test_the_group_is_one_node_for_its_users(network):
    a, b, c = (network.nodes[n] for n in NAMES)
    ma, mb, mc = Member(a), Member(b), Member(c)
    create(ma, member_name=NAMES["local"])
    finish_creation(ma)
    enrol(ma, mb, name=NAMES["local2"])
    enrol(ma, mc, name=NAMES["indie"])
    for _ in range(2):
        for m in (ma, mb, mc):
            full_round(m)
    c_feed = len(c.own_feed())

    # 1. add on A (its web form) -> appears on B and C under the same id
    r = post_form(a, "/new", {"title": "Starfall Harbor", "media_type": "game", "developer": "Old Lantern",
                              "release_date": "2021-05-04"})
    cid = re.search(r"/game/(atlas:[0-9a-z]{26})", r.headers["location"]).group(1)
    rounds(mb, mc, n=1)
    for node in (b, c):
        assert view(node, cid).get("title") == "Starfall Harbor" and view(node, cid).get("developer") == "Old Lantern"
    with c.state.maker() as s:  # C is an authority: grouping signed nothing into its public feed (H59)
        assert len(s.scalars(select(ChangeLog.seq)).all()) == c_feed

    # 2. concurrent edits on B and C -> one result everywhere, one admin-visible conflict entry
    # Both forms are submitted at once: each server's own worker announces its write to the others (`notify`), so
    # edits made seconds apart would be sequential (the second one has seen the first), which is not a conflict.
    with ThreadPoolExecutor(2) as pool:
        for job in [pool.submit(edit_form, b, cid, developer="Lantern Works"),
                    pool.submit(edit_form, c, cid, developer="Tin Kite Studio")]:
            job.result()
    rounds(ma, mb, mc)
    devs = {n.name: view(n, cid).get("developer") for n in (a, b, c)}
    assert len(set(devs.values())) == 1, devs
    assert states(a) == states(b) == states(c)
    ids = conflicts(a)
    assert len(ids) == 1 and ids == conflicts(b) == conflicts(c)
    admin_page = b.admin.get("/admin/group").text
    assert f'data-sg-conflict="{next(iter(ids))}"' in admin_page and NAMES["indie"] in admin_page

    # 3. user pages show no node identity (person and visitor, every node)
    people = {n.name: person(n) for n in (a, b, c)}
    for node in (a, b, c):
        visitor = httpx.Client(base_url=node.url, timeout=20, follow_redirects=False, trust_env=False)
        for path in ("/", "/search?q=Starfall", f"/game/{cid}", "/me", "/me/lists", "/me/settings"):
            for who, client in (("person", people[node.name]), ("visitor", visitor)):
                resp = client.get(path)
                if resp.status_code >= 300:
                    continue
                for marker in markers(network):
                    assert marker not in resp.text, f"{node.name} {who} {path} shows {marker!r}"
        page = people[node.name].get(f"/game/{cid}").text
        assert "Starfall Harbor" in page and devs[node.name] in page
        visitor.close()

    # 4. an offline member changes nothing users see but freshness; it converges when it is back
    c.stop()
    edit_form(a, cid, publisher="Northwind Toys")
    rounds(ma, mb)
    for node in (a, b):
        page = people[node.name].get(f"/game/{cid}").text
        assert "Northwind Toys" in page and "Starfall Harbor" in page and devs[node.name] in page
        assert "not reached" not in page
    assert "data-syncing" in a.admin.get(f"/game/{cid}").text  # A's own write has not reached C yet
    c.start()
    a.stop()  # A goes away before C pulls: B relays A's write
    rnd(mc)
    a.start()
    assert view(c, cid).get("publisher") == "Northwind Toys"
    rounds(ma, mb, mc)
    assert states(a) == states(b) == states(c)
    assert {view(n, cid).get("publisher") for n in (a, b, c)} == {"Northwind Toys"}
    for m in (ma, mb, mc):
        m.net.close()
    for client in people.values():
        client.close()
