"""Evidence screenshots of the C14-3 node link (opt-in, not part of the test suite).

Boots two throwaway nodes on loopback, a main Atlas (authority, port 8811) and a local node (port 8812), with
synthetic accounts and invented records, and drives the link through both web UIs. At each step it writes the
pages to capture to a JSON plan that `e2e/link_evidence_shoot.js` photographs with Playwright (desktop and mobile,
light and dark). Nothing talks to a real service; every name, title and password is a synthetic test value.

    uv run python e2e/link_evidence.py <evidence dir>
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
from datetime import timedelta
from pathlib import Path

import httpx
import uvicorn
from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas import link_client, main, proposals, service  # noqa: E402
from atlas.models import Canonical, User, now  # noqa: E402
from atlas.settings import Settings  # noqa: E402

MAIN, NODE = "http://127.0.0.1:8811", "http://127.0.0.1:8812"
PW = "e2e synthetic password 5521"  # synthetic, test-only


def app_for(tmp: Path, name: str, role: str, url: str, title: str):
    settings = Settings(_env_file=None, role=role, database_url=f"sqlite:///{(tmp / f'{name}.db').as_posix()}",
                        public_url=url, environment="dev", email_verification="optional",
                        media_cache_enabled=False, secret_key=name * 32, app_name=title,
                        group_journal_dir=str(tmp / f"journal-{name}"), auto_approve_reputation=50)
    return main.create_app(settings)


def serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.1)
    raise RuntimeError("server did not start")


def csrf(client: httpx.Client, path: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def signup(client: httpx.Client, name: str) -> None:
    r = client.post("/register", data={"csrf": csrf(client, "/register"), "username": name, "password": PW})
    assert r.status_code == 303, r.text[:300]


def login(client: httpx.Client, name: str) -> None:
    r = client.post("/login", data={"csrf": csrf(client, "/login"), "username": name, "password": PW})
    assert r.status_code == 303, r.text[:300]


def seed(app, ext: str, title: str, developer: str) -> str:
    from tests.conftest import batch

    st = app.state.atlas
    with st.maker() as s:
        [r] = service.ingest(s, st.ctx, batch("igdb", [{"external_id": ext, "title": title,
                                                         "release_date": "2021-05-04",
                                                         "facts": {"developer": developer}}]), system=True)
        s.commit()
        return r.canonical_id


def cookies(client: httpx.Client, url: str) -> list[dict]:
    return [{"name": k, "value": v, "url": url} for k, v in client.cookies.items()]


def main_() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="atlas-link-evidence-"))
    m_app = app_for(tmp, "m", "authority", MAIN, "Central Atlas")
    n_app = app_for(tmp, "n", "local", NODE, "Home Atlas")
    serve(m_app, 8811)
    serve(n_app, 8812)
    n_app.state.atlas.http = httpx.Client(timeout=15, trust_env=False)
    boss = httpx.Client(base_url=MAIN, follow_redirects=False)
    dave = httpx.Client(base_url=MAIN, follow_redirects=False)
    admin = httpx.Client(base_url=NODE, follow_redirects=False)
    signup(boss, "curator_kim")
    signup(dave, "dave")
    signup(admin, "dave")
    with m_app.state.atlas.maker() as s:
        s.scalars(select(User).where(User.username == "dave")).one().created_at = now() - timedelta(days=30)
        s.commit()
    gid = seed(m_app, "9001", "Starfall Harbor", "Old Lantern")
    seed(m_app, "9002", "Paper Comets", "Tin Kite")
    with m_app.state.atlas.maker() as s:
        g = s.get(Canonical, gid)
        view, version = dict(g.view), g.version
    with n_app.state.atlas.maker() as s:  # the node follows the main: the same record under the same id
        s.add(Canonical(id=gid, view=view, version=version, media_type="game"))
        s.commit()
    plan: list[dict] = []

    def shot(name, client, base, path, **kw):
        plan.append({"name": name, "url": base + path, "cookies": cookies(client, base), **kw})

    # 1. the node: check the main and start the link
    t = csrf(admin, "/admin/main")
    shot("node-main-empty", admin, NODE, "/admin/main")
    admin.post("/admin/main/probe", data={"csrf": t, "url": MAIN})
    main_id = m_app.state.atlas.ctx.identity.node_id
    r = admin.post("/admin/main/start", data={"csrf": t, "url": MAIN, "node_id": main_id})
    assert r.status_code == 303, r.text[:500]
    page = admin.get("/admin/main").text
    user_code = re.search(r"data-user-code>([A-Z]{4}-[A-Z]{4})<", page).group(1)
    confirm = re.search(r"data-confirm-code>([A-Z]{4}-[A-Z]{4})<", page).group(1)
    shot("node-main-flow", admin, NODE, "/admin/main")
    # 2. the main: the code entry and the approval page (Playwright types the code itself)
    shot("main-link-approve", dave, MAIN, f"/link?code={user_code}", fill={"#code": user_code},
         click="form[action=\"/link\"] button[type=submit]", wait="[data-link-request]")
    run(out, plan)
    plan.clear()
    t = csrf(dave, "/link")
    dave.post("/link", data={"csrf": t, "code": user_code})
    r = dave.post("/link/decide", data={"csrf": t, "code": user_code, "decision": "approve", "confirm": confirm,
                                        "password": PW})
    assert r.status_code == 303, r.text[:500]
    admin.post(f"/admin/main/{main_id}/poll", data={"csrf": csrf(admin, "/admin/main")})
    # 3. edits on the node, proposed to the main
    shot("node-edit-upstream", admin, NODE, f"/game/{gid}/edit")
    et = csrf(admin, f"/game/{gid}/edit")
    r = admin.post(f"/game/{gid}/edit", data={"csrf": et, "title": "Starfall Harbor", "developer": "Lantern Works",
                                              "release_date": "2021-05-04", "upstream": "on",
                                              "note": "Studio renamed in 2023 (invented example)"})
    assert r.status_code == 303, r.text[:500]
    st = n_app.state.atlas
    with st.maker() as s:
        row = link_client.linked(s)
        link_client.propose(s, st.ctx.settings, st.ctx.identity, row, kind="edit", local_cid=gid,
                            payload={"publisher": "Northwind Toys"})
        s.commit()
    admin.post(f"/admin/main/{main_id}/send", data={"csrf": csrf(admin, "/admin/main")})
    with m_app.state.atlas.maker() as s:  # the first one approved, so the queue shows a decision
        kim = s.scalars(select(User).where(User.username == "curator_kim")).one()
        kim.role = "admin"
        s.commit()
    shot("main-review-queue", boss, MAIN, "/review")
    with m_app.state.atlas.maker() as s:
        from atlas.models import Proposal

        first = s.scalars(select(Proposal).order_by(Proposal.created_at)).first()
        proposals.review(s, m_app.state.atlas.ctx, s.scalars(select(User).where(User.username == "curator_kim")).one(),
                         first.id, approve=True, note="Thanks, matches the studio site.")
        s.commit()
    admin.post(f"/admin/main/{main_id}/send", data={"csrf": csrf(admin, "/admin/main")})
    shot("node-main-linked", admin, NODE, "/admin/main")
    shot("main-me-links", dave, MAIN, "/me/links")
    shot("main-admin-links", boss, MAIN, "/admin/links")
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
