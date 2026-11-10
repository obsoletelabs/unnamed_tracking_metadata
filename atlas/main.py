from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from . import (
    __version__,
    auth,
    background,
    backup,
    client_changes,
    custom,
    dates_rebuild,
    enrich,
    facets,
    feed,
    mail,
    matching,
    media,
    merges,
    oidc,
    prices,
    ratelimit,
    relations,
    sync,
)
from . import (
    metrics as telemetry,
)
from .api.deps import AppState, get_session, require
from .api.group_routes import router as group_router
from .api.routes import router
from .db import instrument as instrument_engine
from .db import make_engine, make_sessionmaker, upgrade
from .observability import RequestTelemetry, Stats, configure_logging
from .pages import seed_pages
from .providers import Fetcher, build_registry
from .service import AtlasError, Ctx
from .settings import Settings, get_settings
from .signing import load_identity, signing_key_at_rest
from .web import STATIC_DIR, templates
from .web.account import router as account_router
from .web.achievements import router as achievements_router
from .web.admin import router as admin_router
from .web.backups import router as backups_router
from .web.deps import LoginRequired, MfaRequired
from .web.federation import router as federation_router
from .web.group import router as group_web_router
from .web.group_link import router as group_link_router
from .web.importing import router as import_router
from .web.mfa import router as mfa_router
from .web.oidc import router as oidc_router
from .web.profile_pages import router as profile_router
from .web.routes import router as web_router
from .web.routes import web_headers
from .web.schema import router as schema_router
from .web.social import router as social_router

log = logging.getLogger("atlas")
_STATUS = {"invalid": 400, "forbidden": 403, "not_found": 404, "conflict": 409,
           "emit_protocol": 409,  # a valid request this node cannot publish yet (C1.6)
           "scope_ceiling": 409,  # C14 §5.4: a mixed entry (ceiling'd + public record) is never signed
           # C11 §11.9-11.12: merge / unmerge / lock refusals
           "distinct": 409, "merge_too_large": 409, "already_reverted": 409, "unmerge_blocked": 409,
           "legacy_merge": 409, "upstream_owned": 409, "stale_version": 412, "invalid_lock": 400}


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
    last_price_purge = 0.0
    last_decision_prune = 0.0
    while not stop.is_set():
        try:
            with background.operation("housekeeping"):
                ratelimit.purge(maker)
                with maker() as s:
                    oidc.purge(s)
                    s.commit()
                with background.operation("scope_projection"):  # C14 §5.7, after an upgrade or a setting
                    from . import scope

                    scope.reproject_pending(maker, ctx, max_batches=5)
                with maker() as s:  # daily retention of the local client change log (C3.2)
                    if client_changes.maybe_prune(s, ctx.settings.client_changes_retain_days) is not None:
                        s.commit()
            if time.monotonic() - last_price_purge > 3600:  # hourly: price observations past retention (C8.2)
                last_price_purge = time.monotonic()
                with background.operation("price_purge"), maker() as s:
                    if purged := prices.purge(s):
                        log.info("purged %d expired price observations", purged)
                    s.commit()
            if time.monotonic() - last_decision_prune > 86400:  # daily: match decision retention (C11 §11.4)
                last_decision_prune = time.monotonic()
                with background.operation("decision_prune"), maker() as s:
                    if pruned := matching.prune_decisions(s, ctx.settings):
                        log.info("pruned %d old match decisions", pruned)
                    s.commit()
                # daily: achievements retired longer than the retention (C12 §12.1.3)
                with background.operation("retired_purge"), maker() as s:
                    if purged := facets.purge_retired(s, ctx.settings):
                        log.info("purged %d retired achievement rows", purged)
                    s.commit()
        except Exception as exc:
            log.warning("housekeeping failed: %s", exc)
        if background.wait(stop, "housekeeping", 60):
            break


def _cache_media(maker: sessionmaker[Session], ctx: Ctx, stop: threading.Event) -> None:
    from . import assist

    group_http = httpx.Client(trust_env=False)  # sealed member calls for images (C14 §3.6), never via a proxy
    group_fetch = assist.blob_fetcher(ctx, group_http)
    while not stop.is_set():
        settings = ctx.settings
        try:
            if settings.media_cache_enabled:
                with background.operation("media_cache"):
                    cached = media.cache_pending(maker, settings, group_fetch=group_fetch)
            else:
                background.skipped("media_cache")
                cached = 0
        except media.MediaStorageError as exc:
            log.warning("artwork cache cannot store images: %s", exc)
            cached = 0
        except Exception as exc:
            log.warning("artwork cache failed: %s", type(exc).__name__)
            cached = 0
        # Drain full successful batches, but avoid a busy loop on an empty or failing queue.
        if background.wait(stop, "media_cache", 0 if cached >= settings.media_cache_batch else 5):
            break


def _backups(engine, ctx: Ctx, stop: threading.Event) -> None:
    interval = ctx.settings.backup_interval_hours * 3600
    stop.wait(60)  # let startup finish first
    while not stop.is_set():
        try:
            latest = backup.latest(ctx.settings)
            due = not latest or (datetime.now(UTC) - latest[0]["when"]).total_seconds() >= interval
            if due:
                with background.operation("backup"):
                    out = backup.scheduled(ctx.settings, engine)
                    backup.verify(out)
                backup.record(engine, backup.VERIFIED)
                log.info("backup written and verified: %s", out)
            else:
                background.skipped("backup")
        except Exception as exc:
            log.error("scheduled backup failed: %s", exc)
        if background.wait(stop, "backup", min(interval, 900)):
            break


def _mailer(maker: sessionmaker[Session], ctx: Ctx, stop: threading.Event) -> None:
    last_purge = 0.0
    while not stop.is_set():
        try:
            if ctx.settings.mail_backend == "smtp" and ctx.settings.smtp_host:
                with background.operation("mail_delivery"):
                    mail.deliver_pending(maker, ctx.settings)
            if time.monotonic() - last_purge > 3600:
                last_purge = time.monotonic()
                with background.operation("mail_purge"), maker() as s:
                    mail.purge_old(s)
                    s.commit()
        except Exception as exc:
            log.warning("mail worker failed: %s", exc)
        if background.wait(stop, "mail", 3):
            break


def _flush_quietly(stats: Stats) -> None:
    try:
        stats.flush()
    except OSError as exc:
        log.warning("metrics snapshot not written: %s", type(exc).__name__)


def _metrics_flush(stats: Stats, stop: threading.Event) -> None:
    """Multi-worker metrics (ATLAS_METRICS_DIR): publish this worker's counters every few seconds, so a scrape
    answered by another worker includes them, and a heartbeat that tells live workers from stopped ones."""
    while not stop.wait(5):
        _flush_quietly(stats)


def _sync_loop(maker: sessionmaker[Session], ctx: Ctx, stop: threading.Event, st: Any = None) -> None:
    delay = ctx.settings.sync_interval_seconds
    try:  # once after an upgrade: entries parked by the previous release may apply now (C1.9)
        sync.startup_replay(maker, ctx)
    except Exception as exc:
        log.warning("startup replay failed: %s", exc)
    with httpx.Client() as client:
        while not stop.is_set():
            wait = delay
            try:  # C13 §4.2: an `introduce` scheduled when the emit protocol was raised into 1.5
                with background.operation("scheduled_introduce"):
                    feed.run_pending(maker, ctx)
            except Exception as exc:
                log.warning("scheduled introduce failed: %s", exc)
            try:
                with background.operation("sync_round"):
                    results = sync.sync_peers(maker, ctx, client)
                if any(not v.startswith("ok") for v in results.values()):
                    wait = min(delay * 4, 900)
            except Exception as exc:  # keep serving local data through any upstream failure
                log.warning("sync failed: %s", exc)
                wait = min(delay * 4, 900)
            if st is not None and enrich.enabled(ctx.settings):  # C13 §5.1 step 4, H56: off by default
                try:
                    enrich.run(maker, st)
                except Exception as exc:
                    log.warning("enrichment from own providers failed: %s", exc)
            if background.wait(stop, "sync", wait):
                break


def group_round(maker: sessionmaker[Session], ctx: Ctx, client) -> dict[str, str]:
    """One owner-group round (CONTRACT-C10 §3.8): expire enrolments, purge nonces, pull every member's state and
    sign this node's view. Does nothing on a node in no group. A halted group keeps pulling: `ingest_state` then
    takes only CRLs, rotations and views, because revocation keeps working (§1.8)."""
    from . import groups
    from .api import group_routes

    with maker() as session:
        groups.expire_enrolments(session)
        groups.purge_nonces(session)
        group = groups.active(session)
        if group is None:
            session.commit()
            return {}
        groups.rebuild(session, group, ctx.identity.node_id, settings=ctx.settings)
        outcomes = group_routes.pull(session, ctx.settings, ctx.identity, group, client)
        session.commit()
    # The private channel (§3.3-§3.8) runs in its own transaction after the trust state is current.
    from . import group_channel, profile

    with maker() as session:
        group = groups.active(session)
        if group is not None:
            channel = group_channel.sync(session, ctx.settings, ctx.identity, group, client, group_routes.call)
            for node, outcome in channel.items():
                if outcome != "ok":
                    outcomes[node] = outcomes.get(node, "ok") + f"; {outcome}"
        profile.prune(session)
        session.commit()
    # CONTRACT-C14 Part A and §7.7: ask the group for what this node waits for, learn the members' provider
    # headroom, and pull the members' shared contribution state. Each step is its own transaction; a failing step
    # never stops the round (the next round retries).
    from . import assist, assist_lookup, contrib_state

    try:
        summary = assist.run(maker, ctx, client)
        if summary.get("applied") or summary.get("rejected"):
            log.info("asked the group: %s", {k: v for k, v in summary.items() if k != "backfill"})
    except Exception as exc:  # noqa: BLE001
        log.warning("asking the group failed: %s", type(exc).__name__)
    with maker() as session:
        group = groups.active(session)
        if group is not None:
            tr = groups.trust(session, group, own_node=ctx.identity.node_id)
            for step in (assist_lookup.poll_status, contrib_state.pull):
                try:
                    with session.begin_nested():
                        step(session, ctx.settings, ctx.identity, group, tr, client, group_routes.call)
                except Exception as exc:  # noqa: BLE001
                    log.warning("group step %s failed: %s", step.__module__, type(exc).__name__)
            assist.purge(session)
            assist_lookup.purge(session)
        session.commit()
    return outcomes


def _group_loop(maker: sessionmaker[Session], ctx: Ctx, stop: threading.Event) -> None:
    with httpx.Client(timeout=15, trust_env=False) as client:
        from .group_channel import WAKE

        while not stop.is_set():
            WAKE.wait(ctx.settings.group_sync_interval)  # an inbound `notify` wakes the loop early
            WAKE.clear()
            if stop.is_set():
                break
            try:
                with background.operation("group_round"):
                    group_round(maker, ctx, client)
            except Exception as exc:  # noqa: BLE001 - the next round retries; local service continues
                log.warning("group sync failed", extra={"atlas_event": {"event": "group.sync_failed",
                                                                        "error_type": type(exc).__name__}})


def _warn_key_at_rest(settings: Settings, at_rest: str | None) -> None:
    """Warn on every start while the signing key is plaintext (C9.1). Decision H4: a production authority will
    refuse to start in this state in the release after signed updates (A10); this release only warns."""
    if at_rest != "plaintext":
        return
    if settings.role == "authority" and settings.environment == "production":
        log.warning("signing_key_plaintext: this production authority stores its signing key unencrypted; set "
                    "ATLAS_SECRET_KEY (see `atlas secret-key`). A future release will refuse to start like this.")
    else:
        log.warning("signing_key_plaintext: the node signing key is stored unencrypted; set ATLAS_SECRET_KEY "
                    "(see `atlas secret-key`) to encrypt it")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    maker = make_sessionmaker(engine, settings)
    auto = settings.auto_migrate if settings.auto_migrate is not None else settings.database_url.startswith("sqlite")
    if auto:
        upgrade(settings.database_url, settings=settings)
    with maker() as s:  # a relation restore left by migration 0032 (a no-op unless one is pending)
        if relations.finish_restore(s) is not None:
            s.commit()
    if (rebuilt := dates_rebuild.finish_pending(maker, settings)) is not None and not rebuilt.get("busy"):
        log.info("re-derived %d record(s) after a downgrade and re-upgrade (%d changed)", rebuilt["rebuilt"],
                 rebuilt["changed"])
    with maker() as s:
        identity = load_identity(s, settings)
        _warn_key_at_rest(settings, signing_key_at_rest(s))
        auth.sync_bootstrap(s, settings.bootstrap_admin_token)
        seed_pages(s)
        custom.seed(s)
        s.commit()
    ctx = Ctx(settings, identity)
    with maker() as s:
        custom.seed_multiplayer(s, ctx)
        s.commit()
    try:  # C10 R1-3: revocations the journal holds and the database lacks (a restore) come back first
        from . import group_journal

        group_journal.startup_replay(maker, identity.node_id)
    except Exception as exc:  # noqa: BLE001 - the next group round retries through `pull`
        log.warning("group journal replay at startup failed: %s", exc)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if (problem := media.storage_problem(ctx.settings)):
            log.warning("artwork cannot be cached until this is fixed: %s", problem)
        threading.Thread(target=_housekeeping, args=(maker, ctx, stop), daemon=True,
                         name="atlas-housekeeping").start()
        threading.Thread(target=_cache_media, args=(maker, ctx, stop), daemon=True, name="atlas-media").start()
        threading.Thread(target=_mailer, args=(maker, ctx, stop), daemon=True, name="atlas-mail").start()
        if ctx.settings.backup_dir:
            threading.Thread(target=_backups, args=(engine, ctx, stop), daemon=True, name="atlas-backup").start()
        if True:  # peers can be added at runtime, so the loop always runs
            threading.Thread(target=_sync_loop, args=(maker, ctx, stop, app.state.atlas), daemon=True,
                             name="atlas-sync").start()
        threading.Thread(target=_group_loop, args=(maker, ctx, stop), daemon=True, name="atlas-group").start()
        stats = getattr(app.state, "observability", None)
        if stats is not None and settings.metrics_enabled and settings.metrics_dir:
            threading.Thread(target=_metrics_flush, args=(stats, stop), daemon=True, name="atlas-metrics").start()
        yield
        stop.set()
        if stats is not None and settings.metrics_enabled and settings.metrics_dir:
            _flush_quietly(stats)  # the last counts of a stopping worker are kept (folded once it is stale)
        from .group_channel import WAKE

        WAKE.set()

    app = FastAPI(title="Atlas Metadata", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.environment == "dev" else None, redoc_url=None)
    app.state.atlas = AppState(ctx=ctx, maker=maker, providers=Fetcher(settings, build_registry(settings)), base=settings,
                                 http=httpx.Client(timeout=15, trust_env=False))
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)
    if settings.json_logs or settings.metrics_enabled:
        stats = Stats()
        stats.settings = settings
        app.state.observability = stats
        if settings.metrics_enabled:
            telemetry.enable()
            instrument_engine(engine)
        if settings.json_logs:
            configure_logging()
        app.add_middleware(RequestTelemetry, state=app.state.atlas, stats=stats, json_logs=settings.json_logs)
    if settings.metrics_enabled:
        @app.get("/metrics", include_in_schema=False)
        def metrics(session: Annotated[Session, Depends(get_session, scope="function")],
                    _principal: Annotated[auth.Principal, Depends(require("metrics"))]):
            return PlainTextResponse(stats.render(app.state.atlas, session),
                                     media_type="text/plain; version=0.0.4", headers={"Cache-Control": "no-store"})
    app.include_router(router)
    app.include_router(achievements_router)
    app.include_router(web_router)
    app.include_router(admin_router)
    app.include_router(account_router)
    app.include_router(mfa_router)
    app.include_router(schema_router)
    app.include_router(social_router)
    app.include_router(federation_router)
    app.include_router(import_router)
    app.include_router(backups_router)
    app.include_router(oidc_router)
    app.include_router(group_router)
    app.include_router(group_web_router)
    app.include_router(profile_router)
    app.include_router(group_link_router)
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

    @app.exception_handler(MfaRequired)
    async def _mfa_required(_: Request, exc: MfaRequired):
        return RedirectResponse(exc.target, status_code=303, headers={"Cache-Control": "no-store"})

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
    @app.exception_handler(merges.MergeError)
    async def _atlas_error(_: Request, exc: AtlasError | merges.MergeError):
        body = {"code": exc.code, "message": str(exc)}
        for k, v in (getattr(exc, "detail", None) or {}).items():  # C11: machine-readable refusal detail
            body.setdefault(k, v)
        return JSONResponse({"error": body}, status_code=_STATUS.get(exc.code, 400))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        request_id = request.scope.get("atlas_request_id", "")
        log.error("request failed", extra={"atlas_event": {"event": "http.error", "request_id": request_id,
                  "node_id": ctx.identity.node_id, "error_type": type(exc).__name__}})
        headers = {"X-Request-ID": request_id} if request_id else {}
        error = HTTPException(500, "An internal error occurred. Please try again.", headers=headers)
        return await _http_error(request, error)

    return app


class _AnonPage:
    """Minimal page context for error pages, which never reveal who is signed in."""

    user = None
    csrf = ""
    flash = None
    can_review = False
    can_moderate = False
    is_admin = False
    is_local_admin = False
    rights = None


def app_factory() -> FastAPI:  # `uvicorn atlas.main:app_factory --factory`
    return create_app()
