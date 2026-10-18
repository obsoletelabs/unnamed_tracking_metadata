from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from . import __version__, auth, custom, mail, media, ratelimit, sync
from .api.deps import AppState
from .api.routes import router
from .db import make_engine, make_sessionmaker, upgrade
from .pages import seed_pages
from .providers import Fetcher, build_registry
from .service import AtlasError, Ctx
from .settings import Settings, get_settings
from .signing import load_identity
from .web import STATIC_DIR, templates
from .web.account import router as account_router
from .web.admin import router as admin_router
from .web.deps import LoginRequired
from .web.routes import router as web_router
from .web.routes import web_headers
from .web.schema import router as schema_router

log = logging.getLogger("atlas")
_STATUS = {"invalid": 400, "forbidden": 403, "not_found": 404, "conflict": 409}


class BodyLimit:
    """Reject oversized request bodies, counting bytes as they stream in."""

    def __init__(self, app, limit: int) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope["headers"]:
            if k == b"content-length" and v.isdigit() and int(v) > self.limit:
                return await self._reject(send)
        seen = 0
        exceeded = False
        started = False

        async def limited():
            nonlocal seen, exceeded
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.limit:
                    exceeded = True
                    raise _TooLarge
            return msg

        async def guarded_send(message):
            nonlocal started
            if exceeded:  # the app may turn our error into its own response; replace it
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited, guarded_send)
        except _TooLarge:
            pass
        if exceeded and not started:
            await self._reject(send)

    async def _reject(self, send):
        body = b'{"error":{"code":"too_large","message":"request body too large"}}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


def _housekeeping(maker: sessionmaker[Session], ctx: Ctx, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            ratelimit.purge(maker)
            if ctx.settings.media_cache_enabled:
                media.cache_pending(maker, ctx.settings)
        except Exception as exc:
            log.warning("housekeeping failed: %s", exc)
        stop.wait(60)


def _mailer(maker: sessionmaker[Session], ctx: Ctx, stop: threading.Event) -> None:
    last_purge = 0.0
    while not stop.is_set():
        try:
            mail.deliver_pending(maker, ctx.settings)
            if time.monotonic() - last_purge > 3600:
                last_purge = time.monotonic()
                with maker() as s:
                    mail.purge_old(s)
                    s.commit()
        except Exception as exc:
            log.warning("mail worker failed: %s", exc)
        stop.wait(3)


def _sync_loop(maker: sessionmaker[Session], ctx: Ctx, stop: threading.Event) -> None:
    delay = ctx.settings.sync_interval_seconds
    with httpx.Client() as client:
        while not stop.is_set():
            wait = delay
            try:
                sync.pull_all(maker, ctx, client, ctx.settings.upstream_url, ctx.settings.upstream_token)  # type: ignore[arg-type]
            except Exception as exc:  # keep serving local data through any upstream failure
                log.warning("sync failed: %s", exc)
                wait = min(delay * 4, 900)
            stop.wait(wait)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    maker = make_sessionmaker(engine)
    auto = settings.auto_migrate if settings.auto_migrate is not None else settings.database_url.startswith("sqlite")
    if auto:
        upgrade(settings.database_url)
    with maker() as s:
        identity = load_identity(s)
        auth.sync_bootstrap(s, settings.bootstrap_admin_token)
        seed_pages(s)
        custom.seed(s)
        s.commit()
    ctx = Ctx(settings, identity)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        threading.Thread(target=_housekeeping, args=(maker, ctx, stop), daemon=True,
                         name="atlas-housekeeping").start()
        threading.Thread(target=_mailer, args=(maker, ctx, stop), daemon=True, name="atlas-mail").start()
        if settings.upstream_url:
            threading.Thread(target=_sync_loop, args=(maker, ctx, stop), daemon=True, name="atlas-sync").start()
        yield
        stop.set()

    app = FastAPI(title="Atlas Metadata", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.environment == "dev" else None, redoc_url=None)
    app.state.atlas = AppState(ctx=ctx, maker=maker, providers=Fetcher(settings, build_registry(settings)), base=settings)
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)
    app.include_router(router)
    app.include_router(web_router)
    app.include_router(admin_router)
    app.include_router(account_router)
    app.include_router(schema_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        web_headers(response, request.url.path)
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _: LoginRequired):
        target = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(target, safe="/"), status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        resp = templates.TemplateResponse(request, "error.html", {
            "status": exc.status_code, "message": exc.detail if isinstance(exc.detail, str) else "Something went wrong.",
            "app_name": app.state.atlas.ctx.settings.app_name, "s": app.state.atlas.ctx.settings,
            "registration_open": app.state.atlas.ctx.settings.registration == "open", "pending_count": 0,
            "nav_pages": [], "p": _AnonPage()}, status_code=exc.status_code, headers=exc.headers)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(AtlasError)
    async def _atlas_error(_: Request, exc: AtlasError):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=_STATUS.get(exc.code, 400))

    return app


class _AnonPage:
    """Minimal page context for error pages, which never reveal who is signed in."""

    user = None
    csrf = ""
    flash = None
    can_review = False
    is_admin = False


def app_factory() -> FastAPI:  # `uvicorn atlas.main:app_factory --factory`
    return create_app()
