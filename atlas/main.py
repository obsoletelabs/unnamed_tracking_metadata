from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from . import __version__, auth, ratelimit, sync
from .api.deps import AppState
from .api.routes import router
from .db import make_engine, make_sessionmaker, upgrade
from .providers import Fetcher, build_registry
from .service import AtlasError, Ctx
from .settings import Settings, get_settings
from .signing import load_identity

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


def _housekeeping(maker: sessionmaker[Session], stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            ratelimit.purge(maker)
        except Exception as exc:
            log.warning("housekeeping failed: %s", exc)
        stop.wait(60)


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
        s.commit()
    ctx = Ctx(settings, identity)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        threading.Thread(target=_housekeeping, args=(maker, stop), daemon=True, name="atlas-housekeeping").start()
        if settings.upstream_url:
            threading.Thread(target=_sync_loop, args=(maker, ctx, stop), daemon=True, name="atlas-sync").start()
        yield
        stop.set()

    app = FastAPI(title="Atlas Metadata", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.environment == "dev" else None, redoc_url=None)
    app.state.atlas = AppState(ctx=ctx, maker=maker, providers=Fetcher(settings, build_registry(settings)))
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)
    app.include_router(router)

    @app.exception_handler(AtlasError)
    async def _atlas_error(_: Request, exc: AtlasError):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=_STATUS.get(exc.code, 400))

    return app


def app_factory() -> FastAPI:  # `uvicorn atlas.main:app_factory --factory`
    return create_app()
