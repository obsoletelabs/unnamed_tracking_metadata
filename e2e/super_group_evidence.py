"""Evidence screenshots of the C14-4 super group (opt-in, not part of the test suite).

Forms a throwaway owner group of three nodes in one process (the test harness: own SQLite files, synthetic keys,
in-process sealed calls between the members), with the super group on. A game is added on node A, a list is made
by an ordinary person on A, two administrators edit the same field on B and C concurrently (one conflict), and
then node B is served on loopback (port 8813) and photographed with Playwright by `e2e/link_evidence_shoot.js`
(desktop and mobile, light and dark):

* what a person sees on B (home, the game, the list written on A, their lists, a page while their own write is
  still syncing): no node name, id, fingerprint, "served by" or "also on";
* what an administrator sees on B: the group bar on every page and Admin > Node group with the members, the
  replicated writes and the conflict entry.

Nothing talks to a real service; every name, title and password is a synthetic test value.

    uv run python e2e/super_group_evidence.py <evidence dir>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas import group_crypto as gc  # noqa: E402
from atlas import groups, main, proposals, scope, social, super_group  # noqa: E402
from atlas.api import group_routes  # noqa: E402
from atlas.models import User, UserList  # noqa: E402
from atlas.service import Ctx  # noqa: E402
from tests.channel_harness import PASSWORD, link, user  # noqa: E402
from tests.conftest import build_settings  # noqa: E402
from tests.group_harness import Dispatcher, Node, create, enrol, finish_creation  # noqa: E402

PORT = 8813
BASE = f"http://127.0.0.1:{PORT}"
NAMES = {"na": "Attic box", "nb": "Basement box", "nc": "Cloud box"}


def make_node(tmp: Path, net: Dispatcher, name: str) -> Node:
    """As the test harness's `make_node`, with one site name for the whole group (what its users see)."""
    from fastapi.testclient import TestClient

    settings = build_settings(database_url=f"sqlite:///{tmp}/{name}.db", role="local", app_name="Home Atlas",
                              secret_key=f"synthetic-evidence-secret-{name}-0123456789abcdef",
                              media_dir=str(tmp / f"{name}-media"), media_cache_enabled=False,
                              public_url=f"http://{name}.test", group_super=True, group_sync_interval=86400)
    app = main.create_app(settings)
    net.clients[f"{name}.test"] = TestClient(app, base_url=f"http://{name}.test")
    app.state.atlas.http = net
    return Node(name, app, app.state.atlas, net)


def serve(app) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.1)
    raise RuntimeError("server did not start")


def rnd(node) -> None:
    with node.session() as s:
        group = groups.active(s)
        tr = groups.trust(s, group, own_node=node.node_id)
        super_group.pull(s, Ctx(node.settings, node.identity), group, tr, node.net, group_routes.call)
        s.commit()


def proposal(node, user_id: str, kind: str, cid: str | None, payload: dict) -> str:
    with node.session() as s:
        p = proposals.create(s, Ctx(node.settings, node.identity), s.get(User, user_id), kind, cid, payload, "")
        s.commit()
        return p.canonical_id


def admin_of(node) -> str:
    uid = user(node, "boss")
    with node.session() as s:
        s.get(User, uid).role = "admin"
        s.commit()
    return uid


def signin(name: str) -> httpx.Client:
    client = httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False, timeout=20)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    r = client.post("/login", data={"csrf": token, "username": name, "password": PASSWORD})
    assert r.status_code == 303, r.text[:300]
    return client


def cookies(client: httpx.Client) -> list[dict]:
    return [{"name": k, "value": v, "url": BASE} for k, v in client.cookies.items()]


def main_() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    gc.SCRYPT_N = gc.SCRYPT_MIN_N = 2 ** 10  # throwaway keys: a fast KDF
    tmp = Path(tempfile.mkdtemp(prefix="atlas-sg-evidence-"))
    net = Dispatcher()
    a, b, c = (make_node(tmp, net, n) for n in NAMES)
    create(a, member_name=NAMES["na"])
    finish_creation(a)
    enrol(a, b, name=NAMES["nb"])
    enrol(a, c, name=NAMES["nc"])
    for _ in range(2):
        for n in (a, b, c):
            main.group_round(n.st.maker, Ctx(n.settings, n.identity), n.net)
    boss = {n.name: admin_of(n) for n in (a, b, c)}
    cid = proposal(a, boss["na"], "new_game", None, {"title": "Starfall Harbor", "media_type": "game",
                                                     "developer": "Old Lantern", "release_date": "2021-05-04",
                                                     "description": "A calm harbour game (invented example)."})
    proposal(a, boss["na"], "new_game", None, {"title": "Paper Comets", "media_type": "game",
                                               "developer": "Tin Kite"})
    for n in (b, c):
        rnd(n)
    kim = {n.name: user(n, "kim") for n in (a, b, c)}
    link(a, kim["na"], b, kim["nb"])
    link(a, kim["na"], c, kim["nc"])
    for n in (a, b, c):
        with n.session() as s:
            scope.grant(s, s.get(User, kim[n.name]), scope.DETAIL, None)
            s.commit()
    with a.session() as s:  # an ordinary person makes a list on A
        ctx = Ctx(a.settings, a.identity)
        me = s.get(User, kim["na"])
        lst = social.create_list(s, ctx, me, "Cosy evenings", "Short and calm", public=False)
        super_group.list_written(s, ctx, me, lst)
        social.add_to_list(s, ctx, me, lst.id, cid, "with tea")
        super_group.list_item_written(s, ctx, me, lst, cid, "with tea")
        lid = lst.id
        s.commit()
    # two administrators edit the same field on B and C before hearing of each other: one conflict entry
    proposal(b, boss["nb"], "edit", cid, {"developer": "Lantern Works"})
    proposal(c, boss["nc"], "edit", cid, {"developer": "Tin Kite Studio"})
    for _ in range(2):
        for n in (a, b, c):
            rnd(n)
    # C goes offline; the person's own edit of a list on B is still on its way to C ("syncing")
    net.down.add("nc.test")
    with b.session() as s:
        ctx = Ctx(b.settings, b.identity)
        me = s.get(User, kim["nb"])
        row = s.get(UserList, lid)
        row.description = "Short, calm and cosy"
        super_group.list_written(s, ctx, me, row)
        s.commit()
    rnd(b)
    rnd(a)

    serve(b.app)
    person, admin = signin("kim"), signin("boss")
    plan = [
        {"name": "person-home", "url": f"{BASE}/", "cookies": cookies(person)},
        {"name": "person-game", "url": f"{BASE}/game/{cid}", "cookies": cookies(person)},
        {"name": "person-list-from-another-node", "url": f"{BASE}/list/{lid}", "cookies": cookies(person)},
        {"name": "person-my-lists", "url": f"{BASE}/me/lists", "cookies": cookies(person)},
        {"name": "admin-game-group-bar", "url": f"{BASE}/game/{cid}", "cookies": cookies(admin)},
        {"name": "admin-node-group", "url": f"{BASE}/admin/group", "cookies": cookies(admin)},
    ]
    markers = ["served by", "Served by", "also on", "Also on", "available on"]
    for n in (a, b, c):
        markers += [n.node_id, groups.gc_fp(n.node_id), NAMES[n.name]]
    for shot in plan:  # the evidence is only worth taking if the person's pages really carry no node identity
        if shot["name"].startswith("person-"):
            text = person.get(shot["url"][len(BASE):]).text
            leaked = [m for m in markers if m in text]
            assert not leaked, (shot["name"], leaked)
    run(out, plan)
    return 0


def run(out: Path, plan: list[dict]) -> None:
    plan_file = out / "_plan.json"
    plan_file.write_text(json.dumps({"dir": str(out), "shots": plan}), encoding="utf-8")
    try:
        subprocess.run(["node", str(ROOT / "e2e" / "link_evidence_shoot.js"), str(plan_file)], check=True,
                       env={**os.environ})
    finally:
        plan_file.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main_())
