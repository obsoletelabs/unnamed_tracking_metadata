from __future__ import annotations

import json
import logging
import time
from threading import Lock

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from .authority import config
from .main import engine

router = APIRouter()
_access_logger = logging.getLogger("atlas.http")
_started_at = time.monotonic()
_counter_lock = Lock()
_request_count = 0
_error_count = 0


def record_request(status_code: int) -> None:
    global _request_count, _error_count
    with _counter_lock:
        _request_count += 1
        if status_code >= 500:
            _error_count += 1


def install_request_metrics(app) -> None:
    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        started = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            record_request(status_code)
            event = {
                "event": "http_request",
                "request_id": getattr(request.state, "request_id", ""),
                "method": request.method,
                # Never log query strings: they may contain search terms or user-supplied values.
                "path": request.url.path[:500],
                "status_code": status_code,
                "duration_ms": round(duration_ms, 3),
            }
            _access_logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))


def database_ready() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/healthz")
def healthz():
    return {"status": "ok", "authority_mode": config.mode, "instance_id": config.instance_id}


@router.get("/readyz")
def readyz():
    ready = database_ready()
    return Response(
        content='{"status":"ready"}' if ready else '{"status":"not_ready"}',
        status_code=200 if ready else 503,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/metrics")
def metrics():
    with _counter_lock:
        requests = _request_count
        errors = _error_count
    uptime = max(0.0, time.monotonic() - _started_at)
    body = (
        "# TYPE metadata_uptime_seconds gauge\n"
        f"metadata_uptime_seconds {uptime:.3f}\n"
        "# TYPE metadata_http_requests_total counter\n"
        f"metadata_http_requests_total {requests}\n"
        "# TYPE metadata_http_errors_total counter\n"
        f"metadata_http_errors_total {errors}\n"
    )
    return Response(content=body, media_type="text/plain; version=0.0.4", headers={"Cache-Control": "no-store"})
