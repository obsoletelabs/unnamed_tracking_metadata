"""Compatibility check: the Unnamed Tracking App's OIDC client stack (Authlib, registered exactly as in its
src/core/oidc.py) signing in through a live Atlas provider. Needs `pip install authlib itsdangerous httpx`.

    python tools/compat/tracking_app_oidc.py        # from the repository root

Prints the authorize parameters the app sends (no PKCE), the consent step and the claims it receives.
"""
"""Run the tracking app's OIDC client stack (Authlib, same registration) against a live Atlas provider."""
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, ".")
import httpx

D = tempfile.mkdtemp()
BASE = "http://127.0.0.1:8796"
env = dict(os.environ, ATLAS_DATABASE_URL=f"sqlite:///{D}/o.db", ATLAS_ROLE="authority", ATLAS_PUBLIC_URL=BASE,
           ATLAS_OIDC_PROVIDER_ENABLED="true", ATLAS_EMAIL_VERIFICATION="off", ATLAS_MEDIA_CACHE_ENABLED="false",
           PYTHONPATH=".")
os.environ.update(env)
from atlas import oidc
from atlas.db import make_engine, make_sessionmaker, upgrade
from atlas.settings import Settings

s = Settings(_env_file=None); upgrade(s.database_url); maker = make_sessionmaker(make_engine(s.database_url))
with maker() as sess:
    c1, sec1 = oidc.create_client(sess, "Tracking app", ["http://localhost:9000/api/auth/oidc/callback/atlas"], require_pkce=False)
    sess.commit(); CID, SECRET = c1.client_id, sec1
srv = subprocess.Popen([sys.executable, "-m", "atlas.cli", "serve", "--port", "8796"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
try:
    for _ in range(40):
        try: httpx.get(BASE + "/api/v2/health"); break
        except Exception: time.sleep(0.5)
    atlas = httpx.Client(base_url=BASE, follow_redirects=False)
    tok = lambda path: re.search(r'name="csrf" value="([^"]+)"', atlas.get(path).text).group(1)
    atlas.post("/register", data={"csrf": tok("/register"), "username": "dave", "password": "correct horse battery", "email": "dave@example.org"})

    # ---- the relying party, configured exactly like src/core/oidc.py in the tracking app ------------------
    from authlib.integrations.starlette_client import OAuth
    from starlette.applications import Starlette
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    oauth = OAuth()
    oauth.register(name="atlas", client_id=CID, client_secret=SECRET,
                   server_metadata_url=f"{BASE}/.well-known/openid-configuration", client_kwargs={"scope": "openid profile email"},
                   overwrite=True)
    async def login(request: Request):
        return await oauth.atlas.authorize_redirect(request, "http://localhost:9000/api/auth/oidc/callback/atlas")
    async def callback(request: Request):
        token = await oauth.atlas.authorize_access_token(request)
        userinfo = token.get("userinfo") or await oauth.atlas.userinfo(token=token)
        return JSONResponse(dict(userinfo))
    app = Starlette(routes=[Route("/login", login), Route("/api/auth/oidc/callback/atlas", callback)])
    app.add_middleware(SessionMiddleware, secret_key="x" * 32)
    rp = TestClient(app, base_url="http://localhost:9000", follow_redirects=False)

    r = rp.get("/login")
    auth_url = r.headers["location"]
    print("authorize url params:", sorted(k for k in dict(httpx.URL(auth_url).params)))
    path = auth_url[len(BASE):]
    consent = atlas.get(path)  # registering signed dave in on Atlas
    print("consent page:", consent.status_code, "Allow" in consent.text)
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', consent.text))
    back = atlas.post("/oidc/authorize", data={**fields, "decision": "allow", "csrf": re.search(r'name="csrf" value="([^"]+)"', consent.text).group(1)})
    loc = back.headers["location"]
    print("back to app:", loc.split("?")[0])
    done = rp.get(loc.replace("http://localhost:9000", ""))
    print("callback:", done.status_code, done.text[:300])
finally:
    srv.terminate()
