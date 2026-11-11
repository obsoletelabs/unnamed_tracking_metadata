"""Two in-process nodes for the node-link tests (CONTRACT-C14 Part C): a main (authority) and a local node whose
outbound HTTP goes to the main's app. Accounts on the main are back-dated past the one-day minimum (§6.3)."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import link, link_client, main, service
from atlas.models import Canonical, NodeLink, UpstreamLink, User, now

from .conftest import batch, build_settings
from .test_web import PW, signup, token

MAIN = "http://central.test"
NODE = "http://localhost:8000"


class Pair:
    def __init__(self, tmp_path, main_kw: dict | None = None, node_kw: dict | None = None, name: str = ""):
        self.m_app = main.create_app(build_settings(
            database_url=f"sqlite:///{tmp_path}/m{name}.db", role="authority", public_url=MAIN,
            media_cache_enabled=False, secret_key="m" * 32, app_name="Central Atlas", **(main_kw or {})))
        self.n_app = main.create_app(build_settings(**{
            "database_url": f"sqlite:///{tmp_path}/n{name}.db", "role": "local", "public_url": NODE,
            "media_cache_enabled": False, "secret_key": "k" * 32, "app_name": "NAS Atlas", **(node_kw or {})}))
        self.main = TestClient(self.m_app, base_url=MAIN, follow_redirects=False)
        self.node = TestClient(self.n_app, base_url=NODE, follow_redirects=False)

    def __enter__(self):
        self.main.__enter__()
        self.node.__enter__()
        self.n_app.state.atlas.http = TestClient(self.m_app, base_url=MAIN)
        signup(self.main, "boss")  # the main's first account administers it
        self.main.cookies.clear()
        signup(self.main, "dave")  # the person who links their node
        age(self.m_app, "dave")
        signup(self.node, "nodeadmin")
        self.gid = seed_game(self.m_app, "1", "Hades")
        return self

    def __exit__(self, *a):
        self.node.__exit__(*a)
        self.main.__exit__(*a)

    # ---- handles -----------------------------------------------------------------------------------------------
    @property
    def mst(self):
        return self.m_app.state.atlas

    @property
    def nst(self):
        return self.n_app.state.atlas

    def nsession(self):
        return self.nst.maker()

    def msession(self):
        return self.mst.maker()

    def upstream(self, s) -> UpstreamLink:
        return s.scalars(select(UpstreamLink)).first()

    def link_row(self, s) -> NodeLink:
        return s.scalars(select(NodeLink)).first()

    # ---- the flow ----------------------------------------------------------------------------------------------
    def start(self, relink=False) -> dict:
        with self.nsession() as s:
            row = link_client.start(s, self.nst.ctx.settings, self.nst.ctx.identity, self.nst.http, MAIN,
                                    relink=relink)
            view = link_client.flow_view(self.nst.ctx.settings, row)
            s.commit()
        return view

    def approve(self, view: dict, *, who=None, confirm=None, show_name=False, password=PW):
        c = who or self.main
        t = token(c, "/link")
        r = c.post("/link", data={"csrf": t, "code": view["user_code"]})
        assert r.status_code == 200 and "data-link-request" in r.text, r.text[:500]
        data = {"csrf": t, "code": view["user_code"], "decision": "approve",
                "confirm": confirm if confirm is not None else view["confirm"], "password": password}
        if show_name:
            data["show_name"] = "on"
        return c.post("/link/decide", data=data)

    def poll(self) -> str:
        with self.nsession() as s:
            out = link_client.poll(s, self.nst.ctx.settings, self.nst.http, self.upstream(s), force=True)
            s.commit()
        return out

    def link(self, **kw) -> None:
        view = self.start()
        r = self.approve(view, **kw)
        assert r.status_code == 303, r.text[:500]
        assert self.poll() == "linked"

    def propose(self, payload=None, kind="edit", cid=None) -> str:
        with self.nsession() as s:
            c = link_client.propose(s, self.nst.ctx.settings, self.nst.ctx.identity, self.upstream(s), kind=kind,
                                    local_cid=cid if cid is not None else (None if kind == "new_game" else self.gid),
                                    payload=payload or {"developer": "Supergiant Games"})
            s.commit()
            return c.id

    def send(self) -> dict:
        with self.nsession() as s:
            out = link_client.send(s, self.nst.ctx.settings, self.nst.ctx.identity, self.nst.http, self.upstream(s),
                                   force=True)
            s.commit()
        return out

    def pull(self) -> int:
        with self.nsession() as s:
            out = link_client.pull(s, self.nst.ctx.settings, self.nst.ctx.identity, self.nst.http, self.upstream(s))
            s.commit()
        return out


def age(app, username: str, days: int = 3) -> None:
    with app.state.atlas.maker() as s:
        u = s.scalars(select(User).where(User.username == username)).one()
        u.created_at = now() - timedelta(days=days)
        s.commit()


def seed_game(app, ext: str, title: str) -> str:
    st = app.state.atlas
    with st.maker() as s:
        [r] = service.ingest(s, st.ctx, batch("igdb", [{"external_id": ext, "title": title,
                                                         "release_date": "2020-09-17",
                                                         "facts": {"developer": "Wrong Dev"}}]), system=True)
        s.commit()
        return r.canonical_id


def same_game_on_node(pair: Pair) -> None:
    """Give the node the main's record under the same id (as following the main's feed would)."""
    with pair.msession() as ms:
        g = ms.get(Canonical, pair.gid)
        view, version, mtype = dict(g.view), g.version, g.media_type
    with pair.nsession() as ns:
        if ns.get(Canonical, pair.gid) is None:
            ns.add(Canonical(id=pair.gid, view=view, version=version, media_type=mtype))
            ns.commit()


def user_code_from(text: str) -> str:
    return re.search(r"data-user-code>([A-Z]{4}-[A-Z]{4})<", text).group(1)


_ = link
